"""Phase 10H-K: ProductionFacade — the single four-stage production entrypoint.

Wraps CollaborationOrchestrator (SINGLE/DUAL routing) and gates
VerificationCollaboration (tester+reviewer) strictly on DUAL success. Returns
one closed FacadeResult (no raw outcomes leak to callers). Upstream failure
never reaches downstream; missing tester/reviewer capability is an honest
terminal, never a silent two-stage success.
"""
import json
import sys
import unittest
from dataclasses import FrozenInstanceError, fields
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from collaboration_orchestrator import CollaborationOrchestrator
from collaboration_session import CollaborationSession, CollaborationStatus, collab_agent_address
from candidate_validation import (
    CandidateValidationResult, CandidateValidationStatus,
    GateResult, GateVerdict, ValidationGate,
)
from external_runtime import InvocationResult, InvocationStatus, InvocationTrace
from execution_engine import ExecutionResult, ExecutionStatus
from loop_guard import LoopGuard
from mode_gate import Mode
from production_facade import FacadeResult, ProductionFacade
from remote_transport import LoopbackRemoteTransport
from runtime_status import HealthEvidence, ReasonCode, RuntimeState, RuntimeStatus
from task_budget import BudgetUsage, TaskBudget
from verified_runtime_pool import VerifiedRuntimePool
from verification_collaboration import VerificationStatus

IDENTITY = ("rt-x", "provider-x", "model-x", "fp-x")
ALL_CAPS = ("architecture", "coding", "testing", "review")
ARCH_ADDR = collab_agent_address(IDENTITY, "architect")
CODER_ADDR = collab_agent_address(IDENTITY, "coder")
TESTER_ADDR = collab_agent_address(IDENTITY, "tester")
REVIEWER_ADDR = collab_agent_address(IDENTITY, "reviewer")
TASK = "redesign architecture across modules"


def arch_dict(task_id="T1"):
    return {"task_id": task_id, "role": "architect", "goal": ["g"], "constraints": ["c"],
            "architecture": ["a"], "interfaces": [{}], "implementation_steps": [{}],
            "acceptance_criteria": ["ac"], "risks": [{}]}


def impl_dict(task_id="T1"):
    return {"task_id": task_id, "role": "coder", "changed_files": ["f.py"],
            "implementation_summary": "s", "implementation_details": ["d"],
            "assumptions": [], "unresolved_items": [], "test_requirements": ["tr"]}


def test_dict(task_id="T1"):
    return {"task_id": task_id, "role": "tester", "tests_run": ["t"], "tests_passed": ["t"],
            "tests_failed": [], "failures": [], "coverage_or_validation": [],
            "remaining_risks": []}


def review_dict(task_id="T1"):
    return {"task_id": task_id, "role": "reviewer", "status": "PASS", "findings": [],
            "severity": [], "affected_files": [], "required_changes": [],
            "acceptance_criteria_status": []}


def trace(status=InvocationStatus.SUCCESS, exit_code=0):
    return InvocationTrace(
        invocation_id="inv-1", task_id="T1", agent_id="a", runtime="rt",
        provider=None, model=None, role=None, status=status,
        started_at=1.0, finished_at=2.0, duration_ms=10,
        exit_code=exit_code, input_tokens="unknown", output_tokens="unknown", error=None)


class RepeatingAdapter:
    def __init__(self, result):
        self.result = result
        self.requests = []

    def invoke(self, request):
        self.requests.append(request)
        return self.result


class StubVO:
    def __init__(self, result=None):
        self.result = result or ExecutionResult(ExecutionStatus.FAILED, (), (), ("MODE_OFF",))

    def execute(self, task_id, task, prompt, mode):
        return self.result


def ok(payload_dict):
    return InvocationResult(InvocationStatus.SUCCESS,
                            output=json.dumps(payload_dict), trace=trace())


def health_ready(runtime_id):
    return RuntimeStatus(runtime_id=runtime_id, executable="exe", version="1",
                         status=RuntimeState.READY, provider="p", model="m", auth_method=None,
                         reason_code=ReasonCode.NONE,
                         evidence=HealthEvidence("d", "a", "p", "m", "ok"),
                         checked_at=1.0, expires_at=2.0)


def make_pool(caps=ALL_CAPS):
    pool = VerifiedRuntimePool(clock=lambda: 1.0)
    result = CandidateValidationResult(
        identity=IDENTITY, status=CandidateValidationStatus.VERIFIED,
        gates_passed=frozenset(ValidationGate),
        gate_results=tuple(GateResult(g, GateVerdict.PASS) for g in ValidationGate),
        block_reason=None, failure_point=None, experiment_id="exp-1", executed_at=1.0,
        validated_capabilities=caps, evidence={})
    pool.admit(result, caps, health_now="READY")
    return pool


def compose_facade(arch_result=None, coder_result=None, tester_result=None,
                   reviewer_result=None, caps=ALL_CAPS):
    budget = TaskBudget(4, 4, timeout_seconds=30.0)
    usage = BudgetUsage()
    guard = LoopGuard()
    arch_adapters = {
        ARCH_ADDR: RepeatingAdapter(arch_result if arch_result is not None else ok(arch_dict())),
        CODER_ADDR: RepeatingAdapter(coder_result if coder_result is not None else ok(impl_dict())),
    }

    def session_factory():
        return CollaborationSession(LoopbackRemoteTransport(), arch_adapters, budget, usage, guard)

    orchestrator = CollaborationOrchestrator(
        StubVO(), make_pool(caps), {IDENTITY[0]: health_ready(IDENTITY[0])},
        budget, usage, guard, session_factory)
    verify_adapters = {
        TESTER_ADDR: RepeatingAdapter(tester_result if tester_result is not None else ok(test_dict())),
        REVIEWER_ADDR: RepeatingAdapter(reviewer_result if reviewer_result is not None else ok(review_dict())),
    }
    facade = ProductionFacade(orchestrator, verify_adapters, make_pool(caps),
                              {IDENTITY[0]: health_ready(IDENTITY[0])}, budget, usage, guard)
    return facade, verify_adapters, budget, usage, guard


class ContractTests(unittest.TestCase):
    def test_facade_result_field_set_and_frozen(self):
        self.assertEqual(
            {field.name for field in fields(FacadeResult)},
            {"status", "mode", "path", "task_id", "provenance", "stages",
             "failure_category", "safe_summary"},
        )
        result = FacadeResult("SUCCESS", "AUTO", "FOUR_STAGE", "T1", "OFFLINE",
                              ("architect", "coder", "tester", "reviewer"), "", {})
        with self.assertRaises(FrozenInstanceError):
            result.status = "MUTATED"

    def test_facade_result_rejects_secret_shaped_fields(self):
        with self.assertRaises(ValueError):
            FacadeResult("SUCCESS", "AUTO", "FOUR_STAGE", "token=leak", "OFFLINE",
                         ("architect",), "", {})


class FourStageTests(unittest.TestCase):
    def test_full_four_stage_composition(self):
        facade, _, _, usage, _ = compose_facade()
        result = facade.run(task_id="T1", task=TASK, prompt="p", mode=Mode.ON)
        self.assertEqual(result.path, "FOUR_STAGE")
        self.assertEqual(result.status, "SUCCESS")
        self.assertEqual(result.stages, ("architect", "coder", "tester", "reviewer"))
        self.assertEqual(result.failure_category, "")
        self.assertEqual(usage.total_agent_calls, 4)
        self.assertEqual(len(facade.state.history("T1")), 5)

    def test_architect_failure_gates_coder_and_verification(self):
        bad = InvocationResult(InvocationStatus.SUCCESS, output="free text", trace=trace())
        facade, verify_adapters, _, usage, _ = compose_facade(arch_result=bad)
        result = facade.run(task_id="T1", task=TASK, prompt="p", mode=Mode.ON)
        self.assertEqual(result.path, "DUAL")
        self.assertEqual(result.status, "ARCHITECT_PACKET_INVALID")
        self.assertEqual(result.stages, ())
        self.assertEqual(verify_adapters[TESTER_ADDR].requests, [])
        self.assertEqual(verify_adapters[REVIEWER_ADDR].requests, [])
        # no TEST/REVIEW fabricated
        self.assertNotIn("TEST", [r.payload_type for r in facade.state.history("T1")])

    def test_coder_failure_gates_tester(self):
        bad = InvocationResult(InvocationStatus.FAILED, error="x", trace=trace(InvocationStatus.FAILED, 1))
        facade, verify_adapters, _, _, _ = compose_facade(coder_result=bad)
        result = facade.run(task_id="T1", task=TASK, prompt="p", mode=Mode.ON)
        self.assertEqual(result.path, "DUAL")
        self.assertEqual(result.status, "CODER_INVOKE_FAILED")
        self.assertEqual(verify_adapters[TESTER_ADDR].requests, [])

    def test_tester_failure_gates_reviewer(self):
        bad = InvocationResult(InvocationStatus.FAILED, error="x", trace=trace(InvocationStatus.FAILED, 1))
        facade, verify_adapters, _, _, _ = compose_facade(tester_result=bad)
        result = facade.run(task_id="T1", task=TASK, prompt="p", mode=Mode.ON)
        self.assertEqual(result.path, "FOUR_STAGE")
        self.assertEqual(result.status, "TESTER_INVOKE_FAILED")
        self.assertEqual(verify_adapters[REVIEWER_ADDR].requests, [])

    def test_reviewer_failure_is_terminal(self):
        bad = InvocationResult(InvocationStatus.TIMEOUT, error="timeout", trace=trace(InvocationStatus.TIMEOUT, None))
        facade, _, _, _, _ = compose_facade(reviewer_result=bad)
        result = facade.run(task_id="T1", task=TASK, prompt="p", mode=Mode.ON)
        self.assertEqual(result.path, "FOUR_STAGE")
        self.assertEqual(result.status, "REVIEWER_INVOKE_FAILED")


class ModeAndCapabilityTests(unittest.TestCase):
    def test_off_mode_delegates(self):
        facade, _, _, _, _ = compose_facade()
        result = facade.run(task_id="T1", task=TASK, prompt="p", mode=Mode.OFF)
        self.assertEqual(result.path, "OFF")
        self.assertEqual(result.status, "FAILED")  # MODE_OFF empty plan -> ExecutionResult FAILED

    def test_auto_simple_delegates_single(self):
        facade, _, _, _, _ = compose_facade()
        result = facade.run(task_id="T1", task="fix one simple bug", prompt="p", mode=Mode.AUTO)
        self.assertEqual(result.path, "SINGLE")

    def test_missing_tester_reviewer_capability_is_honest_not_silent(self):
        # pool only has architect+coder capability (no testing/review)
        facade, verify_adapters, _, _, _ = compose_facade(caps=("architecture", "coding"))
        result = facade.run(task_id="T1", task=TASK, prompt="p", mode=Mode.ON)
        self.assertEqual(result.path, "DUAL")
        self.assertEqual(result.status, "NO_VERIFICATION_CAPABILITY")
        self.assertEqual(verify_adapters[TESTER_ADDR].requests, [])
        self.assertNotIn("TEST", [r.payload_type for r in facade.state.history("T1")])


class SharedStateTests(unittest.TestCase):
    def test_correlation_and_sequence_are_correct(self):
        facade, _, _, _, _ = compose_facade()
        result = facade.run(task_id="T1", task=TASK, prompt="p", mode=Mode.ON)
        self.assertEqual(result.status, "SUCCESS")
        history = facade.state.history("T1")
        self.assertEqual([r.sequence for r in history], [1, 2, 3, 4, 5])
        self.assertEqual([r.payload_type for r in history],
                         ["", "ARCHITECTURE", "IMPLEMENTATION", "TEST", "REVIEW"])
        c1 = history[1].correlation_id
        self.assertEqual(history[2].correlation_id, c1)
        self.assertNotEqual(history[3].correlation_id, c1)
        self.assertNotEqual(history[4].correlation_id, history[3].correlation_id)

    def test_result_repr_and_summary_stay_clean(self):
        facade, _, _, _, _ = compose_facade()
        result = facade.run(task_id="T1", task=TASK, prompt="p", mode=Mode.ON)
        surface = repr(result).lower()
        for marker in ("token", "secret", "api_key", "authorization", "bearer", "stdout", "stderr"):
            self.assertNotIn(marker, surface)


class SourceScanTests(unittest.TestCase):
    def test_facade_is_runtime_neutral_and_mints_nothing(self):
        import production_facade as module
        source = Path(module.__file__).read_text(encoding="utf-8")
        lowered = source.lower()
        for name in ("claude", "codex", "deepseek", "openai", "anthropic", "gemini",
                     "tiny-agents", "tiny_agents"):
            self.assertNotIn(name, lowered)
        for forbidden in ("os.environ", "getenv", "subprocess", "requests", "urllib",
                          "socket", "uuid", "random", "datetime", "import time", "time.",
                          "monotonic", "TaskBudget(", "BudgetUsage(", "LoopGuard("):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
