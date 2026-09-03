"""R7-A3: collaboration-policy reason observability tests.

验证 additive observability：PolicyConstrainedAssigner 产生的
assignment.reason 在两条既有丢失路径上不再静默丢弃——

1) CollaborationOrchestrator 的 architect/coder None 路径：
   DUAL_NO_CAPABLE_AGENT 终态/status/fallback 语义逐字保持，
   但 DECISION reason 同时携带 assignment.reason（等价 additive 表达）。
2) ProductionFacade 的 test/review assignment 路径：
   reason 进入既有 DECISION 通道，不新增 ledger record type。

红线（逐项锁定）：reason deterministic、secret-safe、不含候选原始值/
prompt/packet payload；不新增 record type；sequence/correlation/append-only
不变；AUTO/ON 语义不变；成功路径不出现虚假 POLICY_COUNT_UNSATISFIED。
"""
import json
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from candidate_validation import (
    CandidateValidationResult,
    CandidateValidationStatus,
    GateResult,
    GateVerdict,
    ValidationGate,
)
from collaboration_orchestrator import CollaborationOrchestrator
from collaboration_policy import (
    CollaborationPolicy,
    PolicyConstrainedAssigner,
)
from collaboration_session import CollaborationStatus
from collaboration_state import CollaborationDirection
from execution_engine import ExecutionResult, ExecutionStatus
from external_runtime import InvocationResult, InvocationStatus, InvocationTrace
from loop_guard import LoopGuard
from mode_gate import Mode
from production_facade import ProductionFacade
from remote_transport import LoopbackRemoteTransport
from runtime_status import (
    HealthEvidence,
    ReasonCode,
    RuntimeState,
    RuntimeStatus,
)
from task_budget import BudgetUsage, TaskBudget
from verified_runtime_pool import VerifiedRuntimePool

IDENTITY = ("rt-a", "provider-a", None, "fp-a")
ALL_CAPS = ("architecture", "coding", "testing", "review")

TASK_COMPLEX = "redesign architecture across modules"
TASK_SIMPLE = "fix one simple bug"

SECRET_MARKERS = ("token", "secret", "api_key", "authorization", "bearer",
                  "stdout", "stderr")

# A2b-independent offline stack: one identity, mock adapters per role address.


def arch_dict(task_id="T1"):
    return {"task_id": task_id, "role": "architect", "goal": ["g"],
            "constraints": ["c"], "architecture": ["a"], "interfaces": [{}],
            "implementation_steps": [{}], "acceptance_criteria": ["ac"],
            "risks": [{}]}


def impl_dict(task_id="T1"):
    return {"task_id": task_id, "role": "coder", "changed_files": ["f.py"],
            "implementation_summary": "s", "implementation_details": ["d"],
            "assumptions": [], "unresolved_items": [],
            "test_requirements": ["tr"]}


def test_dict(task_id="T1"):
    return {"task_id": task_id, "role": "tester", "tests_run": ["t"],
            "tests_passed": ["t"], "tests_failed": [], "failures": [],
            "coverage_or_validation": [], "remaining_risks": []}


def review_dict(task_id="T1"):
    return {"task_id": task_id, "role": "reviewer", "status": "PASS",
            "findings": [], "severity": [], "affected_files": [],
            "required_changes": [], "acceptance_criteria_status": []}


def trace():
    return InvocationTrace(
        invocation_id="inv-3", task_id="T1", agent_id="a", runtime="rt",
        provider=None, model=None, role=None, status=InvocationStatus.SUCCESS,
        started_at=1.0, finished_at=2.0, duration_ms=10, exit_code=0,
        input_tokens="unknown", output_tokens="unknown", error=None)


class RepeatingAdapter:
    def __init__(self, result):
        self.result = result
        self.requests = []

    def invoke(self, request):
        self.requests.append(request)
        return self.result


class StubVO:
    """SINGLE-path stub mirroring tests/test_production_facade.py: returns a
    fixed ExecutionResult so AUTO-simple/medium delegation works offline."""

    def __init__(self, result=None):
        self.result = result or ExecutionResult(
            ExecutionStatus.FAILED, (), (), ("MODE_OFF",))

    def execute(self, task_id, task, prompt, mode):
        return self.result


def ok(payload_dict):
    return InvocationResult(InvocationStatus.SUCCESS,
                            output=json.dumps(payload_dict), trace=trace())


def health_ready(runtime_id=IDENTITY[0]):
    return RuntimeStatus(
        runtime_id=runtime_id, executable="exe", version="1",
        status=RuntimeState.READY, provider="provider-a", model=None,
        auth_method=None, reason_code=ReasonCode.NONE,
        evidence=HealthEvidence("d", "a", "p", "m", "ok"),
        checked_at=1.0, expires_at=2.0)


def validation(identity=IDENTITY):
    return CandidateValidationResult(
        identity=identity, status=CandidateValidationStatus.VERIFIED,
        gates_passed=frozenset(ValidationGate),
        gate_results=tuple(GateResult(g, GateVerdict.PASS)
                           for g in ValidationGate),
        block_reason=None, failure_point=None, experiment_id="exp-3",
        executed_at=1.0, validated_capabilities=ALL_CAPS, evidence={})


def make_pool():
    pool = VerifiedRuntimePool(clock=lambda: 1.0)
    pool.admit(validation(), ALL_CAPS, health_now="READY")
    return pool


def compose():
    """Shared budget/usage/guard; adapters for the single identity's four
    role addresses; a real orchestrator + facade over the real pool."""
    from collaboration_session import collab_agent_address
    budget = TaskBudget(8, 8, timeout_seconds=30.0)
    usage = BudgetUsage()
    guard = LoopGuard()
    addresses = {
        "arch": collab_agent_address(IDENTITY, "architect"),
        "coder": collab_agent_address(IDENTITY, "coder"),
        "tester": collab_agent_address(IDENTITY, "tester"),
        "reviewer": collab_agent_address(IDENTITY, "reviewer"),
    }
    collab_adapters = {
        addresses["arch"]: RepeatingAdapter(ok(arch_dict())),
        addresses["coder"]: RepeatingAdapter(ok(impl_dict())),
    }
    verify_adapters = {
        addresses["tester"]: RepeatingAdapter(ok(test_dict())),
        addresses["reviewer"]: RepeatingAdapter(ok(review_dict())),
    }

    def session_factory():
        return CollaborationSession(
            LoopbackRemoteTransport(), collab_adapters, budget, usage, guard)

    health = {IDENTITY[0]: health_ready()}
    orchestrator = CollaborationOrchestrator(
        StubVO(), make_pool(), health, budget, usage, guard, session_factory)
    facade = ProductionFacade(orchestrator, verify_adapters, make_pool(),
                              health, budget, usage, guard)
    return facade, addresses, usage


from collaboration_session import CollaborationSession  # noqa: E402


def decisions_of(history):
    return [record for record in history
            if record.direction is CollaborationDirection.DECISION]


def decision_reasons(history):
    return [record.reason for record in decisions_of(history)]


class OrchestratorReasonPreservationTests(unittest.TestCase):
    """Test 1 — architect/coder assignment reason preserved.

    场景：allowlist 只点名一个缺席 runtime（rt-zz）→ 双角色 None →
    既有 DUAL_NO_CAPABLE_AGENT 终态；assignment.reason=
    POLICY_RUNTIME_ABSENT=rt-zz 必须进入 DECISION，不再丢失。"""

    def test_on_mode_absent_runtime_reason_enters_decision(self):
        facade, _, usage = compose()
        policy = CollaborationPolicy(runtime_allowlist=("rt-zz",))
        outcome = facade.run(task_id="T1", task=TASK_COMPLEX, prompt="p",
                             mode=Mode.ON, policy=policy)
        # 既有终态语义逐字保持（ON 不 fallback、零 invocation）。
        self.assertEqual(outcome.status, "DUAL_NO_CAPABLE_AGENT")
        self.assertEqual(usage.total_agent_calls, 0)
        # observability：assignment.reason 不再丢失。
        reasons = decision_reasons(facade.state.history("T1"))
        self.assertTrue(
            any("POLICY_RUNTIME_ABSENT=rt-zz" in reason for reason in reasons),
            reasons)
        # 双标记共存：既有词表 + POLICY_*（等价 additive 表达）。
        self.assertTrue(
            any("DUAL_NO_CAPABLE_AGENT" in reason for reason in reasons),
            reasons)

    def test_auto_mode_absent_runtime_falls_back_with_reason_visible(self):
        facade, _, usage = compose()
        policy = CollaborationPolicy(runtime_allowlist=("rt-zz",))
        outcome = facade.run(task_id="T1", task=TASK_COMPLEX, prompt="p",
                             mode=Mode.AUTO, policy=policy)
        # AUTO 既有 fallback 语义保持：single 路径（stub VO 返回 object()）。
        self.assertEqual(usage.total_agent_calls, 0)
        reasons = decision_reasons(facade.state.history("T1"))
        self.assertTrue(
            any("POLICY_RUNTIME_ABSENT=rt-zz" in reason for reason in reasons),
            reasons)

    def test_existing_none_path_reason_stays_exact_without_policy(self):
        # 无 policy 的历史 None 路径：DECISION reason 仍逐字
        # DUAL_NO_CAPABLE_AGENT（零漂移；不带任何 POLICY 后缀）。
        from candidate_validation import CandidateValidationStatus as CVS
        empty_pool = VerifiedRuntimePool(clock=lambda: 1.0)
        bad = CandidateValidationResult(
            identity=IDENTITY, status=CVS.FAILED,
            gates_passed=frozenset(), gate_results=(),
            block_reason="x", failure_point=None, experiment_id="",
            executed_at=1.0, validated_capabilities=(), evidence={})
        empty_pool.admit(bad, (), health_now="READY")
        budget = TaskBudget(2, 2, timeout_seconds=30.0)
        usage = BudgetUsage()
        guard = LoopGuard()
        orchestrator = CollaborationOrchestrator(
            StubVO(), empty_pool, {IDENTITY[0]: health_ready()},
            budget, usage, guard, lambda: object())
        outcome = orchestrator.run(task_id="T1", task=TASK_COMPLEX, prompt="p",
                                   mode=Mode.ON)
        self.assertEqual(outcome.status, "DUAL_NO_CAPABLE_AGENT")
        decisions = decisions_of(orchestrator.state.history("T1"))
        self.assertEqual(decisions[0].reason, "DUAL_NO_CAPABLE_AGENT")


class FacadeReasonPreservationTests(unittest.TestCase):
    """Test 2 — test/review assignment reason preserved.

    场景 A：tester/reviewer None（allowlist 点名缺席 runtime）→
    NO_VERIFICATION_CAPABILITY 终态 + reason 进 DECISION。
    场景 B：assignment 成功但 reason=POLICY_COUNT_UNSATISFIED
    （单 runtime + min=2）→ 成功跑完四阶段，且 reason 可观察。"""

    def test_missing_verification_reason_enters_decision(self):
        facade, addresses, usage = compose()
        # 池只有 rt-a；allowlist 只点名 rt-zz → tester/reviewer None。
        # 但 architect/coder 也会 None（同一 allowlist）——为只打中
        # verification 半场，用 role 专用能力缺口替代：换 caps 池。
        pool = VerifiedRuntimePool(clock=lambda: 1.0)
        no_verify = CandidateValidationResult(
            identity=IDENTITY, status=CandidateValidationStatus.VERIFIED,
            gates_passed=frozenset(ValidationGate),
            gate_results=tuple(GateResult(g, GateVerdict.PASS)
                               for g in ValidationGate),
            block_reason=None, failure_point=None, experiment_id="exp-3",
            executed_at=1.0,
            validated_capabilities=("architecture", "coding"), evidence={})
        pool.admit(no_verify, ("architecture", "coding"), health_now="READY")
        budget = TaskBudget(8, 8, timeout_seconds=30.0)
        usage2 = BudgetUsage()
        guard = LoopGuard()

        from collaboration_session import collab_agent_address
        arch_addr = collab_agent_address(IDENTITY, "architect")
        coder_addr = collab_agent_address(IDENTITY, "coder")
        collab_adapters = {arch_addr: RepeatingAdapter(ok(arch_dict())),
                           coder_addr: RepeatingAdapter(ok(impl_dict()))}

        def session_factory():
            return CollaborationSession(
                LoopbackRemoteTransport(), collab_adapters, budget, usage2, guard)

        orchestrator = CollaborationOrchestrator(
            StubVO(), pool, {IDENTITY[0]: health_ready()},
            budget, usage2, guard, session_factory)
        facade2 = ProductionFacade(
            orchestrator, {addresses["tester"]: RepeatingAdapter(ok(test_dict())),
                           addresses["reviewer"]: RepeatingAdapter(ok(review_dict()))},
            pool, {IDENTITY[0]: health_ready()}, budget, usage2, guard)
        result = facade2.run(task_id="T1", task=TASK_COMPLEX, prompt="p",
                             mode=Mode.ON)
        # 既有终态语义逐字保持。
        self.assertEqual(result.status, "NO_VERIFICATION_CAPABILITY")
        self.assertEqual(result.stages, ("architect", "coder"))
        # observability（verification 半场）：tester/reviewer assignment 的
        # reason 必须出现在 IMPLEMENTATION reply 之后追加的 DECISION 里，
        # 而不是只借用 dual 半场（sequence 1）的 DECISION。
        history = facade2.state.history("T1")
        reply_index = next(i for i, r in enumerate(history)
                           if r.payload_type == "IMPLEMENTATION")
        late_decisions = [r for r in history[reply_index + 1:]
                          if r.direction is CollaborationDirection.DECISION]
        self.assertTrue(late_decisions, "no DECISION after IMPLEMENTATION reply")
        self.assertTrue(
            any("ROLE_ASSIGNMENT=" in r.reason for r in late_decisions),
            [r.reason for r in late_decisions])

    def test_unsatisfied_min_reason_observable_on_success(self):
        # 单 runtime + min=2：四阶段照常成功，assignment.reason=
        # POLICY_COUNT_UNSATISFIED 不再丢失（成功路径也可观察）。
        facade, _, usage = compose()
        policy = CollaborationPolicy(min_distinct_runtimes=2)
        result = facade.run(task_id="T1", task=TASK_COMPLEX, prompt="p",
                            mode=Mode.ON, policy=policy)
        self.assertEqual(result.status, "SUCCESS")
        self.assertEqual(result.path, "FOUR_STAGE")
        self.assertEqual(usage.total_agent_calls, 4)
        reasons = decision_reasons(facade.state.history("T1"))
        self.assertTrue(
            any("POLICY_COUNT_UNSATISFIED" in reason for reason in reasons),
            reasons)


class PolicySatisfiedTests(unittest.TestCase):
    """Test 3 — policy satisfied：不出现虚假 POLICY_COUNT_UNSATISFIED。"""

    def test_satisfied_policy_spreads_without_unsatisfied_reason(self):
        from collaboration_session import collab_agent_address
        ID_B = ("rt-b", "provider-b", None, "fp-b")
        budget = TaskBudget(8, 8, timeout_seconds=30.0)
        usage = BudgetUsage()
        guard = LoopGuard()
        pool = VerifiedRuntimePool(clock=lambda: 1.0)
        for ident in (IDENTITY, ID_B):
            pool.admit(validation(ident), ALL_CAPS, health_now="READY")
        health = {IDENTITY[0]: health_ready(),
                  ID_B[0]: health_ready(ID_B[0])}
        adapters = {}
        for ident in (IDENTITY, ID_B):
            for role, payload in (("architect", arch_dict()),
                                  ("coder", impl_dict())):
                adapters[collab_agent_address(ident, role)] = \
                    RepeatingAdapter(ok(payload))
        verify_adapters = {}
        for ident in (IDENTITY, ID_B):
            for role, payload in (("tester", test_dict()),
                                  ("reviewer", review_dict())):
                verify_adapters[collab_agent_address(ident, role)] = \
                    RepeatingAdapter(ok(payload))

        def session_factory():
            return CollaborationSession(
                LoopbackRemoteTransport(), adapters, budget, usage, guard)

        orchestrator = CollaborationOrchestrator(
            StubVO(), pool, health, budget, usage, guard, session_factory)
        facade = ProductionFacade(orchestrator, verify_adapters, pool,
                                  health, budget, usage, guard)
        policy = CollaborationPolicy(min_distinct_runtimes=2)
        result = facade.run(task_id="T1", task=TASK_COMPLEX, prompt="p",
                            mode=Mode.ON, policy=policy)
        self.assertEqual(result.status, "SUCCESS")
        self.assertEqual(result.path, "FOUR_STAGE")
        reasons = decision_reasons(facade.state.history("T1"))
        self.assertFalse(
            any("POLICY_COUNT_UNSATISFIED" in reason for reason in reasons),
            reasons)
        self.assertTrue(
            any("POLICY_SPREAD" in reason for reason in reasons), reasons)


class ModeSemanticsTests(unittest.TestCase):
    """Test 4/5 — AUTO/ON 语义不变。"""

    def test_auto_simple_keeps_fast_path_with_policy(self):
        facade, _, usage = compose()
        policy = CollaborationPolicy(min_distinct_runtimes=2)
        result = facade.run(task_id="T1", task=TASK_SIMPLE, prompt="p",
                            mode=Mode.AUTO, policy=policy)
        self.assertEqual(result.path, "SINGLE")  # 既有快速路径
        self.assertEqual(result.mode, "AUTO")    # policy 不隐式升级 Mode

    def test_auto_complex_enters_collaboration_with_policy(self):
        facade, _, usage = compose()
        policy = CollaborationPolicy(min_distinct_runtimes=2)
        result = facade.run(task_id="T1", task=TASK_COMPLEX, prompt="p",
                            mode=Mode.AUTO, policy=policy)
        self.assertEqual(result.path, "FOUR_STAGE")

    def test_on_failure_does_not_fallback_with_reason_visible(self):
        # Test 5：ON + policy failure → 无 fallback（终态保持），reason 可见。
        facade, _, usage = compose()
        policy = CollaborationPolicy(runtime_allowlist=("rt-zz",))
        result = facade.run(task_id="T1", task=TASK_COMPLEX, prompt="p",
                            mode=Mode.ON, policy=policy)
        self.assertEqual(result.status, "DUAL_NO_CAPABLE_AGENT")
        self.assertEqual(result.stages, ())
        self.assertEqual(usage.total_agent_calls, 0)  # 零 invocation、零 retry
        reasons = decision_reasons(facade.state.history("T1"))
        self.assertTrue(
            any("POLICY_RUNTIME_ABSENT" in reason for reason in reasons),
            reasons)


class ReasonSecurityTests(unittest.TestCase):
    """Test 6 — reason 输出 secret-safe 且不含候选原始值。"""

    def test_reasons_never_carry_secret_markers(self):
        facade, _, _ = compose()
        policy = CollaborationPolicy(runtime_allowlist=("rt-zz",))
        facade.run(task_id="T1", task=TASK_COMPLEX, prompt="p",
                   mode=Mode.ON, policy=policy)
        surface = repr(facade.state.history("T1")).lower()
        for marker in SECRET_MARKERS:
            self.assertNotIn(marker, surface)

    def test_facade_result_surface_stays_clean_with_policy_reasons(self):
        facade, _, _ = compose()
        policy = CollaborationPolicy(min_distinct_runtimes=2)
        result = facade.run(task_id="T1", task=TASK_COMPLEX, prompt="p",
                            mode=Mode.ON, policy=policy)
        surface = repr(result).lower()
        for marker in SECRET_MARKERS:
            self.assertNotIn(marker, surface)


class LedgerInvariantTests(unittest.TestCase):
    """Test 7 — ledger invariant：append-only、sequence、correlation、
    record type 不增加、DECISION 结构不被破坏。"""

    def test_five_record_shape_unchanged_on_success(self):
        facade, _, _ = compose()
        policy = CollaborationPolicy(min_distinct_runtimes=2)
        facade.run(task_id="T1", task=TASK_COMPLEX, prompt="p",
                   mode=Mode.ON, policy=policy)
        history = facade.state.history("T1")
        self.assertEqual(len(history), 5)  # 1 DECISION + 4 envelope
        self.assertEqual([r.sequence for r in history], [1, 2, 3, 4, 5])
        self.assertEqual(history[0].direction, CollaborationDirection.DECISION)
        self.assertEqual([r.payload_type for r in history],
                         ["", "ARCHITECTURE", "IMPLEMENTATION", "TEST", "REVIEW"])
        c1 = history[1].correlation_id
        self.assertEqual(history[2].correlation_id, c1)
        self.assertNotEqual(history[3].correlation_id, c1)
        self.assertNotEqual(history[4].correlation_id, history[3].correlation_id)

    def test_record_direction_vocabulary_not_extended(self):
        # 唯一新增的 observability 仍走 DECISION 通道；direction 词表不变。
        from collaboration_state import CollaborationDirection as D
        self.assertEqual(
            {d.value for d in D},
            {"DECISION", "REQUEST", "REPLY", "FAILURE"})

    def test_on_failure_ledger_shape_unchanged(self):
        facade, _, _ = compose()
        policy = CollaborationPolicy(runtime_allowlist=("rt-zz",))
        facade.run(task_id="T1", task=TASK_COMPLEX, prompt="p",
                   mode=Mode.ON, policy=policy)
        history = facade.state.history("T1")
        directions = [r.direction for r in history]
        self.assertIn(CollaborationDirection.DECISION, directions)
        self.assertIn(CollaborationDirection.FAILURE, directions)
        # ON 失败路径只有 DECISION + FAILURE，无 envelope（零 invocation）。
        self.assertNotIn(CollaborationDirection.REQUEST, directions)
        self.assertEqual([r.sequence for r in history],
                         list(range(1, len(history) + 1)))


class ZeroDriftRegressionTests(unittest.TestCase):
    """Test 8 的零漂移分量：无 policy 时 ledger/结果与历史逐字一致。"""

    def test_no_policy_ledger_reasons_unchanged(self):
        facade, _, _ = compose()
        facade.run(task_id="T1", task=TASK_COMPLEX, prompt="p", mode=Mode.ON)
        reasons = decision_reasons(facade.state.history("T1"))
        # 历史 reason 格式：.../ROLE_ASSIGNMENT=POLICY_CONVERGED（无附加）。
        self.assertEqual(len(reasons), 1)
        self.assertTrue(reasons[0].endswith("ROLE_ASSIGNMENT=POLICY_CONVERGED"),
                        reasons)

    def test_success_path_invocation_count_unchanged(self):
        facade, _, usage = compose()
        facade.run(task_id="T1", task=TASK_COMPLEX, prompt="p", mode=Mode.ON)
        self.assertEqual(usage.total_agent_calls, 4)


if __name__ == "__main__":
    unittest.main()
