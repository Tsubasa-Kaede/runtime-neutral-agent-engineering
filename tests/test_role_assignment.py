"""Phase 10H-E: role assignment policy layer tests.

纯策略层测试：两个 assigner 都只从 bridge 候选集内选择，无 score、
无 runtime 名、确定性、secret-free。DiversityAssigner 是 10H-E 的
核心目标 —— 全能力对称证据下，spread 是显式的 POLICY 选择（部署级
多样化决策），绝不在 reason 里冒充证据（CLEAR_SPECIALIZATION 属于
能力差异化路径，不是这里的词）。

全部离线：不触 runtime、不读环境、不走网络。候选集由真实
VerifiedSelectionBridge 从真实 VerifiedRuntimePool 投影而来，保证
"候选只能来自 bridge 返回集合"在构造层就成立。
"""
import sys
import unittest
from dataclasses import FrozenInstanceError
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
from role_assignment import (
    ConvergingAssigner,
    DiversityAssigner,
    RoleAssignment,
)
from runtime_status import (
    HealthEvidence,
    ReasonCode,
    RuntimeState,
    RuntimeStatus,
)
from task_classifier import Complexity
from verified_selection_bridge import VerifiedSelectionBridge
from verified_runtime_pool import VerifiedRuntimePool

IDENTITY_X = ("rt-x", "provider-x", "model-x", "fp-x")
IDENTITY_Y = ("rt-y", "provider-y", "model-y", "fp-y")

ALL_CAPS = ("architecture", "coding", "testing", "review")
ARCH_ONLY = ("architecture",)
CODING_ONLY = ("coding",)

SECRET_MARKERS = ("token", "secret", "api_key", "authorization", "bearer", "stdout", "stderr")

_ROLE_REQUIREMENTS = {
    "architect": ("architecture",),
    "coder": ("coding",),
}


def candidate_identity(candidate):
    return (candidate.runtime_id, candidate.provider_id,
            candidate.model_id, candidate.config_fingerprint)


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
    """entries: [(identity, caps)] — caps 空/能力不足的身份不入池。"""
    pool = VerifiedRuntimePool(clock=lambda: 1.0)
    for identity, caps in entries:
        if caps:
            pool.admit(pool_result(identity, caps), caps, health_now="READY")
    return pool


def make_candidate_sets(pool_entries, health=None):
    """真实 bridge 投影：architect/coder 两个角色候选集。"""
    pool = make_pool(pool_entries)
    health = health if health is not None else {
        identity[0]: health_ready(identity[0]) for identity, caps in pool_entries if caps
    }
    bridge = VerifiedSelectionBridge()
    return {
        role: bridge.candidates_for(pool, health, role, requirements)
        for role, requirements in _ROLE_REQUIREMENTS.items()
    }


class ConvergingAssignerTests(unittest.TestCase):
    def test_dual_runtime_all_caps_pool_converges_both_roles_to_same_identity(self):
        # 默认策略逐字复刻现行行为：sorted(pool.identities()) 的首个
        # 全能力候选对两个角色都中标（candidates[0] 折叠）。
        sets = make_candidate_sets([(IDENTITY_X, ALL_CAPS), (IDENTITY_Y, ALL_CAPS)])
        assignment = ConvergingAssigner().assign(sets, Complexity.COMPLEX)

        self.assertIsInstance(assignment, RoleAssignment)
        architect = assignment.assignments["architect"]
        coder = assignment.assignments["coder"]
        self.assertEqual(candidate_identity(architect), IDENTITY_X)
        self.assertEqual(candidate_identity(coder), IDENTITY_X)
        self.assertEqual(assignment.reason, "POLICY_CONVERGED")

    def test_single_runtime_pool_converges(self):
        sets = make_candidate_sets([(IDENTITY_X, ALL_CAPS)])
        assignment = ConvergingAssigner().assign(sets, Complexity.COMPLEX)

        self.assertEqual(candidate_identity(assignment.assignments["architect"]), IDENTITY_X)
        self.assertEqual(candidate_identity(assignment.assignments["coder"]), IDENTITY_X)
        self.assertEqual(assignment.reason, "POLICY_CONVERGED")

    def test_empty_architect_set_assigns_none(self):
        # architect 候选集为空 → None → orchestrator 走既有
        # DUAL_NO_CAPABLE_AGENT，绝不硬造 MULTI。
        sets = make_candidate_sets([(IDENTITY_Y, CODING_ONLY)])
        assignment = ConvergingAssigner().assign(sets, Complexity.COMPLEX)

        self.assertIsNone(assignment.assignments["architect"])
        self.assertIsNotNone(assignment.assignments["coder"])

    def test_capability_differentiated_pool_matches_current_behavior(self):
        # 能力天然差异化（X=architecture、Y=coding）时，与现行行为
        # 一致：每角色各自候选项的首个。
        sets = make_candidate_sets(
            [(IDENTITY_X, ARCH_ONLY), (IDENTITY_Y, CODING_ONLY)])
        assignment = ConvergingAssigner().assign(sets, Complexity.COMPLEX)

        self.assertEqual(candidate_identity(assignment.assignments["architect"]), IDENTITY_X)
        self.assertEqual(candidate_identity(assignment.assignments["coder"]), IDENTITY_Y)
        self.assertEqual(assignment.reason, "POLICY_CONVERGED")


class DiversityAssignerTests(unittest.TestCase):
    def test_dual_runtime_all_caps_complex_spreads_roles(self):
        # 10H-E 核心目标：全能力对称证据下 spread 是 POLICY 决策。
        sets = make_candidate_sets([(IDENTITY_X, ALL_CAPS), (IDENTITY_Y, ALL_CAPS)])
        assignment = DiversityAssigner().assign(sets, Complexity.COMPLEX)

        architect = assignment.assignments["architect"]
        coder = assignment.assignments["coder"]
        self.assertEqual(candidate_identity(architect), IDENTITY_X)
        self.assertEqual(candidate_identity(coder), IDENTITY_Y)
        self.assertNotEqual(architect.runtime_id, coder.runtime_id)
        self.assertEqual(assignment.reason, "POLICY_SPREAD")

    def test_single_runtime_pool_converges_honestly(self):
        # 只有一个 runtime 时绝不造第二个：诚实收敛。
        sets = make_candidate_sets([(IDENTITY_X, ALL_CAPS)])
        assignment = DiversityAssigner().assign(sets, Complexity.COMPLEX)

        self.assertEqual(candidate_identity(assignment.assignments["architect"]), IDENTITY_X)
        self.assertEqual(candidate_identity(assignment.assignments["coder"]), IDENTITY_X)
        self.assertEqual(assignment.reason, "POLICY_CONVERGED")

    def test_simple_task_converges_even_with_two_runtimes(self):
        # SIMPLE 必须收敛：复杂度驱动 runtime 布局。
        sets = make_candidate_sets([(IDENTITY_X, ALL_CAPS), (IDENTITY_Y, ALL_CAPS)])
        assignment = DiversityAssigner().assign(sets, Complexity.SIMPLE)

        self.assertEqual(candidate_identity(assignment.assignments["architect"]), IDENTITY_X)
        self.assertEqual(candidate_identity(assignment.assignments["coder"]), IDENTITY_X)
        self.assertEqual(assignment.reason, "POLICY_CONVERGED")

    def test_capability_differentiated_pool_spreads_like_current(self):
        # 能力天然差异化时 spread 与现行行为一致（不是回归）。
        sets = make_candidate_sets(
            [(IDENTITY_X, ARCH_ONLY), (IDENTITY_Y, CODING_ONLY)])
        assignment = DiversityAssigner().assign(sets, Complexity.COMPLEX)

        self.assertEqual(candidate_identity(assignment.assignments["architect"]), IDENTITY_X)
        self.assertEqual(candidate_identity(assignment.assignments["coder"]), IDENTITY_Y)
        self.assertEqual(assignment.reason, "POLICY_SPREAD")

    def test_empty_architect_set_assigns_none(self):
        sets = make_candidate_sets([(IDENTITY_Y, CODING_ONLY)])
        assignment = DiversityAssigner().assign(sets, Complexity.COMPLEX)

        self.assertIsNone(assignment.assignments["architect"])
        self.assertIsNotNone(assignment.assignments["coder"])

    def test_deterministic_across_calls(self):
        sets = make_candidate_sets([(IDENTITY_X, ALL_CAPS), (IDENTITY_Y, ALL_CAPS)])
        assigner = DiversityAssigner()
        first = assigner.assign(sets, Complexity.COMPLEX)
        second = assigner.assign(sets, Complexity.COMPLEX)
        self.assertEqual(first, second)

    def test_assignments_come_only_from_bridge_sets(self):
        # 选出的候选必须逐字属于 bridge 候选集（不扩集、不新造）。
        sets = make_candidate_sets([(IDENTITY_X, ALL_CAPS), (IDENTITY_Y, ALL_CAPS)])
        assignment = DiversityAssigner().assign(sets, Complexity.COMPLEX)
        for role, candidate in assignment.assignments.items():
            self.assertIn(candidate, sets[role].candidates)

    def test_score_is_always_none(self):
        sets = make_candidate_sets([(IDENTITY_X, ALL_CAPS), (IDENTITY_Y, ALL_CAPS)])
        for assigner in (ConvergingAssigner(), DiversityAssigner()):
            assignment = assigner.assign(sets, Complexity.COMPLEX)
            for candidate in assignment.assignments.values():
                self.assertIsNone(candidate.score)


class RoleAssignmentValueTests(unittest.TestCase):
    def test_assignment_is_frozen(self):
        sets = make_candidate_sets([(IDENTITY_X, ALL_CAPS)])
        assignment = ConvergingAssigner().assign(sets, Complexity.COMPLEX)
        with self.assertRaises(FrozenInstanceError):
            assignment.reason = "MUTATED"

    def test_surface_stays_clean(self):
        sets = make_candidate_sets([(IDENTITY_X, ALL_CAPS), (IDENTITY_Y, ALL_CAPS)])
        for assigner in (ConvergingAssigner(), DiversityAssigner()):
            assignment = assigner.assign(sets, Complexity.COMPLEX)
            surface = repr(assignment).lower()
            for marker in SECRET_MARKERS:
                self.assertNotIn(marker, surface)


class SourceScanTests(unittest.TestCase):
    def test_no_runtime_names_or_forbidden_channels(self):
        import role_assignment as module
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


if __name__ == "__main__":
    unittest.main()
