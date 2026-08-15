"""Phase 10H-E2: CollaborationOrchestrator — composition routing facade.

Routes by Mode + Complexity between verbatim delegation to the injected
VerifiedOrchestrator (SINGLE) and dual-agent collaboration through an
injected session factory (DUAL), records decisions/outcomes/failures
into the 10H-E1 ledger, and falls back to SINGLE only on zero-call dual
failures (usage-delta rule). Fully offline; no runtime is ever touched.
"""
import json
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from collaboration_orchestrator import CollaborationOrchestrator
from collaboration_packet import (
    CollaborationPacket,
    CollaborationPayloadType,
    serialize_collaboration_packet,
)
from collaboration_session import (
    CollaborationOutcome,
    CollaborationSession,
    CollaborationStatus,
    collab_agent_address,
)
from collaboration_state import CollaborationDirection, TraceSummary
from candidate_validation import (
    CandidateValidationResult,
    CandidateValidationStatus,
    GateResult,
    GateVerdict,
    ValidationGate,
)
from external_runtime import InvocationResult, InvocationStatus, InvocationTrace
from loop_guard import LoopGuard
from mode_gate import Mode
from remote_transport import LoopbackRemoteTransport, RemoteExchangeError
from runtime_status import (
    HealthEvidence,
    ReasonCode,
    RuntimeState,
    RuntimeStatus,
)
from task_budget import BudgetUsage, TaskBudget
from verified_selection_bridge import agent_id_for

IDENTITY_X = ("rt-x", "provider-x", "model-x", "fp-x")
IDENTITY_Y = ("rt-y", "provider-y", "model-y", "fp-y")
ARCH_ADDR_X = collab_agent_address(IDENTITY_X, "architect")
CODER_ADDR_X = collab_agent_address(IDENTITY_X, "coder")
ARCH_ADDR_Y = collab_agent_address(IDENTITY_Y, "architect")
CODER_ADDR_Y = collab_agent_address(IDENTITY_Y, "coder")

SECRET_MARKERS = ("token", "secret", "api_key", "authorization", "bearer", "stdout", "stderr")

TASK_COMPLEX = "redesign architecture across modules"
TASK_SIMPLE = "fix one simple bug"
TASK_MEDIUM = "update two related files"
TASK_UNRESOLVED = "do the thing"

ALL_CAPS = ("architecture", "coding", "testing", "review")


def arch_dict(task_id="T1"):
    return {
        "task_id": task_id, "role": "architect",
        "goal": ["g"], "constraints": ["c"], "architecture": ["a"],
        "interfaces": [{}], "implementation_steps": [{}],
        "acceptance_criteria": ["ac"], "risks": [{}],
    }


def impl_dict(task_id="T1"):
    return {
        "task_id": task_id, "role": "coder", "changed_files": ["f.py"],
        "implementation_summary": "s", "implementation_details": ["d"],
        "assumptions": [], "unresolved_items": [], "test_requirements": ["tr"],
    }


def trace(status=InvocationStatus.SUCCESS, exit_code=0):
    return InvocationTrace(
        invocation_id="inv-1", task_id="T1", agent_id="a", runtime="rt",
        provider=None, model=None, role=None, status=status,
        started_at=1.0, finished_at=2.0, duration_ms=10,
        exit_code=exit_code, input_tokens="unknown", output_tokens="unknown",
        error=None)


def ok_result(payload_dict):
    return InvocationResult(InvocationStatus.SUCCESS,
                            output=json.dumps(payload_dict), trace=trace())


class FakeAgentAdapter:
    def __init__(self, results):
        self.results = list(results)
        self.requests = []

    def invoke(self, request):
        self.requests.append(request)
        return self.results.pop(0)


class RepeatingAdapter:
    """Always succeeds with the same payload — for repeated runs."""

    def __init__(self, result):
        self.result = result
        self.requests = []

    def invoke(self, request):
        self.requests.append(request)
        return self.result


class StubVerifiedOrchestrator:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def execute(self, task_id, task, prompt, mode):
        self.calls.append((task_id, task, prompt, mode))
        return self.result


class SpySession:
    def __init__(self, outcome):
        self.outcome = outcome
        self.kwargs = None

    def run(self, **kwargs):
        self.kwargs = kwargs
        return self.outcome


def health_ready(runtime_id):
    return RuntimeStatus(
        runtime_id=runtime_id, executable="exe", version="1",
        status=RuntimeState.READY, provider="p", model="m", auth_method=None,
        reason_code=ReasonCode.NONE,
        evidence=HealthEvidence("d", "a", "p", "m", "ok"),
        checked_at=1.0, expires_at=2.0)


def pool_result(identity, caps):
    return CandidateValidationResult(
        identity=identity, status=CandidateValidationStatus.VERIFIED,
        gates_passed=frozenset(ValidationGate),
        gate_results=tuple(GateResult(g, GateVerdict.PASS) for g in ValidationGate),
        block_reason=None, failure_point=None,
        experiment_id="exp-1", executed_at=1.0,
        validated_capabilities=caps, evidence={})


def make_pool(entries):
    from verified_runtime_pool import VerifiedRuntimePool
    pool = VerifiedRuntimePool(clock=lambda: 1.0)
    for identity, caps in entries:
        if caps:
            pool.admit(pool_result(identity, caps), caps, health_now="READY")
    return pool


def make_envelope(task_id, correlation_id, source, target, payload_type, payload,
                  source_role, target_role):
    return CollaborationPacket(
        correlation_id=correlation_id, task_id=task_id,
        source_agent=source, target_agent=target,
        source_role=source_role, target_role=target_role,
        payload_type=payload_type, payload=payload)


def success_outcome(task_id="T1", correlation_id="C1"):
    from structured_packets import ArchitecturePacket, ImplementationPacket
    return CollaborationOutcome(
        status=CollaborationStatus.SUCCESS, task_id=task_id,
        correlation_id=correlation_id, runtime_mode="SINGLE_RUNTIME",
        request_envelope=make_envelope(
            task_id, correlation_id, ARCH_ADDR_X, CODER_ADDR_X,
            CollaborationPayloadType.ARCHITECTURE,
            ArchitecturePacket.from_dict(arch_dict(task_id)),
            "architect", "coder"),
        reply_envelope=make_envelope(
            task_id, correlation_id, CODER_ADDR_X, ARCH_ADDR_X,
            CollaborationPayloadType.IMPLEMENTATION,
            ImplementationPacket.from_dict(impl_dict(task_id)),
            "coder", "architect"),
        receipts=(), traces=(trace(), trace()))


def make_facade(vo_result=None, pool_entries=None, session=None, budget=None,
                usage=None, guard=None, health=None, state=None):
    vo = StubVerifiedOrchestrator(vo_result if vo_result is not None else object())
    budget = budget or TaskBudget(8, 8, timeout_seconds=30.0)
    usage = usage or BudgetUsage()
    guard = guard or LoopGuard()
    if session is None:
        adapters = {
            ARCH_ADDR_X: RepeatingAdapter(ok_result(arch_dict())),
            CODER_ADDR_X: RepeatingAdapter(ok_result(impl_dict())),
        }
        session = CollaborationSession(LoopbackRemoteTransport(), adapters,
                                       budget, usage, guard)
    pool = make_pool(pool_entries if pool_entries is not None else [(IDENTITY_X, ALL_CAPS)])
    return (CollaborationOrchestrator(
        vo, pool, health or {IDENTITY_X[0]: health_ready(IDENTITY_X[0])},
        budget, usage, guard, lambda: session, state=state),
            vo, usage)


def run_facade(facade, task=TASK_COMPLEX, mode=Mode.AUTO, task_id="T1", prompt="p"):
    return facade.run(task_id=task_id, task=task, prompt=prompt, mode=mode)


class RoutingMatrixTests(unittest.TestCase):
    def test_off_delegates_verbatim_with_sentinel(self):
        sentinel = object()
        facade, vo, _ = make_facade(vo_result=sentinel)
        mode = Mode.OFF
        result = run_facade(facade, task=TASK_MEDIUM, mode=mode, prompt="PROMPT")
        self.assertIs(result, sentinel)
        self.assertEqual(len(vo.calls), 1)
        task_id, task, prompt, seen_mode = vo.calls[0]
        self.assertEqual((task_id, task, prompt), ("T1", TASK_MEDIUM, "PROMPT"))
        self.assertIs(seen_mode, mode)
        decision = facade.state.history("T1")[0]
        self.assertEqual(decision.direction, CollaborationDirection.DECISION)
        self.assertEqual(decision.reason, "MODE_OFF")

    def test_auto_simple_delegates_verbatim(self):
        sentinel = object()
        facade, vo, _ = make_facade(vo_result=sentinel)
        self.assertIs(run_facade(facade, task=TASK_SIMPLE), sentinel)
        self.assertEqual(len(vo.calls), 1)

    def test_auto_medium_delegates_verbatim(self):
        sentinel = object()
        facade, vo, _ = make_facade(vo_result=sentinel)
        self.assertIs(run_facade(facade, task=TASK_MEDIUM), sentinel)
        decision = facade.state.history("T1")[0]
        self.assertEqual(decision.path, "SINGLE")

    def test_auto_unresolved_delegates_verbatim(self):
        sentinel = object()
        facade, vo, _ = make_facade(vo_result=sentinel)
        self.assertIs(run_facade(facade, task=TASK_UNRESOLVED), sentinel)

    def test_auto_complex_routes_dual(self):
        facade, vo, usage = make_facade()
        outcome = run_facade(facade)
        self.assertEqual(outcome.status, CollaborationStatus.SUCCESS)
        self.assertEqual(len(vo.calls), 0)
        self.assertEqual(usage.total_agent_calls, 2)

    def test_on_routes_dual_even_for_simple(self):
        facade, vo, usage = make_facade()
        outcome = run_facade(facade, task=TASK_SIMPLE, mode=Mode.ON)
        self.assertEqual(outcome.status, CollaborationStatus.SUCCESS)
        self.assertEqual(len(vo.calls), 0)
        self.assertEqual(usage.total_agent_calls, 2)


class DualSelectionTests(unittest.TestCase):
    def test_auto_complex_without_capabilities_falls_back_to_single(self):
        sentinel = object()
        facade, vo, usage = make_facade(
            vo_result=sentinel, pool_entries=[(IDENTITY_X, ())])  # real shape: caps empty
        result = run_facade(facade)
        self.assertIs(result, sentinel)
        self.assertEqual(usage.total_agent_calls, 0)
        decisions = [r for r in facade.state.history("T1")
                     if r.direction is CollaborationDirection.DECISION]
        self.assertEqual(decisions[0].reason, "DUAL_NO_CAPABLE_AGENT")
        self.assertEqual(decisions[1].path, "SINGLE")
        self.assertTrue(decisions[1].reason.startswith("FALLBACK"))

    def test_on_without_capabilities_hard_fails(self):
        facade, vo, usage = make_facade(pool_entries=[(IDENTITY_X, ())])
        outcome = run_facade(facade, mode=Mode.ON)
        self.assertEqual(outcome.status, "DUAL_NO_CAPABLE_AGENT")
        self.assertEqual(len(vo.calls), 0)
        self.assertEqual(usage.total_agent_calls, 0)
        failures = facade.state.failures("T1")
        self.assertEqual(failures[0].status, "DUAL_NO_CAPABLE_AGENT")

    def test_none_pool_behaves_like_no_candidates(self):
        sentinel = object()
        facade, vo, usage = make_facade(vo_result=sentinel, pool_entries=[])
        self.assertIs(run_facade(facade), sentinel)
        self.assertEqual(usage.total_agent_calls, 0)

    def test_candidates_zero_is_deterministic(self):
        spy = SpySession(success_outcome())
        facade, _, usage = make_facade(
            pool_entries=[(IDENTITY_Y, ALL_CAPS), (IDENTITY_X, ALL_CAPS)],
            session=spy)
        outcome = run_facade(facade)
        self.assertEqual(outcome.status, CollaborationStatus.SUCCESS)
        self.assertEqual(spy.kwargs["architect_address"],
                         collab_agent_address(IDENTITY_X, "architect"))
        self.assertEqual(spy.kwargs["coder_address"],
                         collab_agent_address(IDENTITY_X, "coder"))

    def test_single_runtime_mode_and_role_qualified_addresses(self):
        spy = SpySession(success_outcome())
        facade, _, _ = make_facade(session=spy)
        run_facade(facade)
        self.assertEqual(spy.kwargs["runtime_mode"], "SINGLE_RUNTIME")
        self.assertEqual(spy.kwargs["architect_address"], ARCH_ADDR_X)
        self.assertEqual(spy.kwargs["coder_address"], CODER_ADDR_X)

    def test_multi_runtime_mode_when_roles_live_on_distinct_runtimes(self):
        spy = SpySession(success_outcome())
        facade, _, _ = make_facade(
            pool_entries=[(IDENTITY_X, ("architecture",)), (IDENTITY_Y, ("coding",))],
            session=spy,
            health={IDENTITY_X[0]: health_ready(IDENTITY_X[0]),
                    IDENTITY_Y[0]: health_ready(IDENTITY_Y[0])})
        run_facade(facade)
        self.assertEqual(spy.kwargs["runtime_mode"], "MULTI")
        self.assertEqual(spy.kwargs["architect_address"], ARCH_ADDR_X)
        self.assertEqual(spy.kwargs["coder_address"], CODER_ADDR_Y)

    def test_provenance_passes_through_to_session(self):
        spy = SpySession(success_outcome())
        facade, _, _ = make_facade(session=spy)
        facade.run(task_id="T1", task=TASK_COMPLEX, prompt="p",
                   mode=Mode.AUTO, provenance="OFFLINE")
        self.assertEqual(spy.kwargs["provenance"], "OFFLINE")


class BudgetGuardTests(unittest.TestCase):
    def test_usage_instance_is_shared_across_successive_dual_runs(self):
        facade, _, usage = make_facade()
        run_facade(facade, task_id="T1")
        run_facade(facade, task=TASK_COMPLEX, task_id="T2")
        self.assertEqual(usage.total_agent_calls, 4)

    def test_coder_stage_guard_rejection_does_not_fall_back(self):
        guard = LoopGuard()
        guard.record("T1", "coder", CODER_ADDR_X)
        sentinel = object()
        facade, vo, usage = make_facade(vo_result=sentinel, guard=guard)
        outcome = run_facade(facade)
        self.assertEqual(outcome.status, CollaborationStatus.LOOP_GUARD_REJECTED)
        self.assertEqual(len(vo.calls), 0)
        self.assertEqual(usage.total_agent_calls, 1)  # architect ran, coder refused

    def test_coder_stage_budget_exhaustion_does_not_fall_back(self):
        budget = TaskBudget(3, 3, timeout_seconds=30.0)
        usage = BudgetUsage()
        usage.total_agent_calls = 2
        sentinel = object()
        facade, vo, _ = make_facade(vo_result=sentinel, budget=budget, usage=usage)
        outcome = run_facade(facade)
        self.assertEqual(outcome.status, CollaborationStatus.BUDGET_EXHAUSTED)
        self.assertEqual(len(vo.calls), 0)

    def test_pre_exhausted_budget_fails_directly_without_fallback(self):
        budget = TaskBudget(2, 2, timeout_seconds=30.0)
        usage = BudgetUsage()
        usage.total_agent_calls = 2
        sentinel = object()
        facade, vo, _ = make_facade(vo_result=sentinel, budget=budget, usage=usage)
        outcome = run_facade(facade)
        self.assertEqual(outcome.status, CollaborationStatus.BUDGET_EXHAUSTED)
        self.assertEqual(len(vo.calls), 0)
        self.assertEqual(usage.total_agent_calls, 2)


class FailurePassThroughTests(unittest.TestCase):
    def failing_facade(self, adapters, transport=None, **facade_kwargs):
        budget = TaskBudget(8, 8, timeout_seconds=30.0)
        usage = BudgetUsage()
        guard = LoopGuard()
        session = CollaborationSession(
            transport or LoopbackRemoteTransport(), adapters, budget, usage, guard)
        facade, vo, _ = make_facade(session=session, budget=budget, usage=usage,
                                    guard=guard, **facade_kwargs)
        return facade, vo

    def test_architect_invoke_failure_does_not_fall_back(self):
        adapters = {
            ARCH_ADDR_X: FakeAgentAdapter([
                InvocationResult(InvocationStatus.FAILED, error="x",
                                 trace=trace(InvocationStatus.FAILED, 1))]),
            CODER_ADDR_X: FakeAgentAdapter([ok_result(impl_dict())]),
        }
        facade, vo = self.failing_facade(adapters)
        outcome = run_facade(facade)
        self.assertEqual(outcome.status, CollaborationStatus.ARCHITECT_INVOKE_FAILED)
        self.assertEqual(len(vo.calls), 0)

    def test_architect_packet_invalid_never_falls_back(self):
        adapters = {
            ARCH_ADDR_X: FakeAgentAdapter([
                InvocationResult(InvocationStatus.SUCCESS, output="free text",
                                 trace=trace())]),
            CODER_ADDR_X: FakeAgentAdapter([ok_result(impl_dict())]),
        }
        facade, vo = self.failing_facade(adapters)
        outcome = run_facade(facade)
        self.assertEqual(outcome.status, CollaborationStatus.ARCHITECT_PACKET_INVALID)
        self.assertEqual(len(vo.calls), 0)

    def test_coder_packet_invalid_never_falls_back(self):
        adapters = {
            ARCH_ADDR_X: FakeAgentAdapter([ok_result(arch_dict())]),
            CODER_ADDR_X: FakeAgentAdapter([
                InvocationResult(InvocationStatus.SUCCESS, output="nope",
                                 trace=trace())]),
        }
        facade, vo = self.failing_facade(adapters)
        outcome = run_facade(facade)
        self.assertEqual(outcome.status, CollaborationStatus.CODER_PACKET_INVALID)
        self.assertEqual(len(vo.calls), 0)

    def test_transport_failure_does_not_fall_back(self):
        def failing(target_agent, wire, sink):
            raise RemoteExchangeError("REMOTE_UNAVAILABLE")

        adapters = {
            ARCH_ADDR_X: FakeAgentAdapter([ok_result(arch_dict())]),
            CODER_ADDR_X: FakeAgentAdapter([ok_result(impl_dict())]),
        }
        facade, vo = self.failing_facade(adapters, transport=LoopbackRemoteTransport(failing))
        outcome = run_facade(facade)
        self.assertEqual(outcome.status, CollaborationStatus.TRANSPORT_FAILED)
        self.assertEqual(len(vo.calls), 0)

    def test_correlation_mismatch_never_falls_back(self):
        from structured_packets import ArchitecturePacket

        def mismatch(target_agent, wire, sink):
            if target_agent == CODER_ADDR_X:
                wrong = make_envelope(
                    "T1", "C999", ARCH_ADDR_X, CODER_ADDR_X,
                    CollaborationPayloadType.ARCHITECTURE,
                    ArchitecturePacket.from_dict(arch_dict()),
                    "architect", "coder")
                sink(target_agent, serialize_collaboration_packet(wrong))
                return
            sink(target_agent, wire)

        adapters = {
            ARCH_ADDR_X: FakeAgentAdapter([ok_result(arch_dict())]),
            CODER_ADDR_X: FakeAgentAdapter([ok_result(impl_dict())]),
        }
        facade, vo = self.failing_facade(adapters, transport=LoopbackRemoteTransport(mismatch))
        outcome = run_facade(facade)
        self.assertEqual(outcome.status, CollaborationStatus.CORRELATION_MISMATCH)
        self.assertEqual(len(vo.calls), 0)


class LedgerTests(unittest.TestCase):
    def test_success_records_decision_request_reply(self):
        facade, _, _ = make_facade()
        outcome = run_facade(facade)
        history = facade.state.history("T1")
        directions = [record.direction for record in history]
        self.assertEqual(directions, [CollaborationDirection.DECISION,
                                      CollaborationDirection.REQUEST,
                                      CollaborationDirection.REPLY])
        request = history[1]
        self.assertEqual(request.wire,
                         serialize_collaboration_packet(outcome.request_envelope))
        self.assertEqual(request.correlation_id, outcome.correlation_id)
        self.assertEqual(request.provenance, "OFFLINE")
        self.assertEqual(len(request.trace_summaries), 1)
        self.assertIsInstance(request.trace_summaries[0], TraceSummary)
        self.assertEqual(history[2].payload_type, "IMPLEMENTATION")
        decision = history[0]
        self.assertEqual(decision.path, "DUAL")
        self.assertEqual(decision.runtime_mode, "SINGLE_RUNTIME")
        self.assertIn("COVERAGE=ARCHITECT_CODER", decision.reason)

    def test_failure_records_decision_and_failure(self):
        adapters = {
            ARCH_ADDR_X: FakeAgentAdapter([
                InvocationResult(InvocationStatus.FAILED, error="x",
                                 trace=trace(InvocationStatus.FAILED, 1))]),
            CODER_ADDR_X: FakeAgentAdapter([ok_result(impl_dict())]),
        }
        budget = TaskBudget(8, 8, timeout_seconds=30.0)
        usage = BudgetUsage()
        session = CollaborationSession(LoopbackRemoteTransport(), adapters,
                                       budget, usage, LoopGuard())
        facade, _, _ = make_facade(session=session, budget=budget, usage=usage)
        outcome = run_facade(facade)
        failures = facade.state.failures("T1")
        self.assertEqual(failures[0].status, "ARCHITECT_INVOKE_FAILED")
        self.assertEqual(len(failures[0].trace_summaries), 1)
        decisions = [r for r in facade.state.history("T1")
                     if r.direction is CollaborationDirection.DECISION]
        self.assertEqual(decisions[0].path, "DUAL")

    def test_ledger_surface_stays_clean(self):
        facade, _, _ = make_facade()
        run_facade(facade)
        surface = repr(facade.state).lower()
        for marker in SECRET_MARKERS:
            self.assertNotIn(marker, surface)


class SourceScanTests(unittest.TestCase):
    def test_no_runtime_names_or_forbidden_channels(self):
        import collaboration_orchestrator as module
        source = Path(module.__file__).read_text(encoding="utf-8")
        lowered = source.lower()
        for name in ("claude", "codex", "deepseek", "openai", "anthropic",
                     "gemini", "tiny-agents", "tiny_agents"):
            self.assertNotIn(name, lowered)
        for forbidden in ("os.environ", "getenv", "RUN_REAL_PROVIDER_TESTS",
                          "subprocess", "requests", "urllib", "socket",
                          "http", "websocket", "a2a", "async", "threading",
                          "uuid", "random", "datetime", "import time",
                          "time.", "monotonic", "sleep", "clock"):
            self.assertNotIn(forbidden, source)

    def test_facade_never_mints_budget_or_guard(self):
        import collaboration_orchestrator as module
        source = Path(module.__file__).read_text(encoding="utf-8")
        for forbidden in ("TaskBudget(", "BudgetUsage(", "LoopGuard("):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
