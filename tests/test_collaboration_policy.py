"""R7-A1: collaboration policy core tests.

纯策略层测试：CollaborationPolicy 数据模型校验、apply_policy 过滤
语义、PolicyConstrainedAssigner 的 allowlist / min / max / reuse 行为。
候选集由真实 VerifiedSelectionBridge 从真实 VerifiedRuntimePool 投影
而来（与 test_role_assignment.py 相同的构造哲学），保证"候选只能来自
bridge 返回集合"在构造层成立。全部离线：不触 runtime、不读环境、
不走网络、不调 adapter。
"""
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
from collaboration_policy import (
    CollaborationPolicy,
    PolicyApplication,
    PolicyConstrainedAssigner,
    apply_policy,
)
from role_assignment import RoleAssignment
from runtime_status import (
    HealthEvidence,
    ReasonCode,
    RuntimeState,
    RuntimeStatus,
)
from task_classifier import Complexity
from verified_selection_bridge import VerifiedSelectionBridge
from verified_runtime_pool import VerifiedRuntimePool

CLAUDE = ("claude-cli", "anthropic", "model-c", "fp-c")
CODEX = ("codex-cli", "openai", "model-x", "fp-x")
PI = ("pi-cli", None, "model-p", "fp-p")
GEMINI = ("gemini-cli", "google", "model-g", "fp-g")

ALL_CAPS = ("architecture", "coding", "testing", "review")

SECRET_MARKERS = ("token", "secret", "api_key", "authorization", "bearer",
                  "stdout", "stderr")

_ROLES = ("architect", "coder", "review", "test")


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


def make_candidate_sets(identities):
    """真实 bridge 投影：四角色候选集（全部全能力，全部 READY）。"""
    pool = VerifiedRuntimePool(clock=lambda: 1.0)
    for identity in identities:
        pool.admit(pool_result(identity, ALL_CAPS), ALL_CAPS, health_now="READY")
    health = {identity[0]: health_ready(identity[0]) for identity in identities}
    bridge = VerifiedSelectionBridge()
    requirements = {"architect": ("architecture",), "coder": ("coding",),
                    "review": ("review",), "test": ("testing",)}
    return {role: bridge.candidates_for(pool, health, role, requirements[role])
            for role in _ROLES}


class CollaborationPolicyModelTests(unittest.TestCase):
    def test_none_allowlist_is_valid_unrestricted(self):
        policy = CollaborationPolicy()
        self.assertIsNone(policy.runtime_allowlist)
        self.assertIsNone(policy.min_distinct_runtimes)
        self.assertIsNone(policy.max_distinct_runtimes)
        self.assertTrue(policy.allow_runtime_reuse)

    def test_empty_allowlist_is_rejected(self):
        with self.assertRaises(ValueError):
            CollaborationPolicy(runtime_allowlist=())

    def test_empty_string_entry_is_rejected(self):
        with self.assertRaises(ValueError):
            CollaborationPolicy(runtime_allowlist=("claude-cli", ""))

    def test_non_string_entry_is_rejected(self):
        with self.assertRaises(ValueError):
            CollaborationPolicy(runtime_allowlist=("claude-cli", 7))

    def test_secret_marker_entry_is_rejected(self):
        with self.assertRaises(ValueError):
            CollaborationPolicy(runtime_allowlist=("claude-cli", "api_key"))

    def test_min_below_one_is_rejected(self):
        for bad in (0, -1):
            with self.assertRaises(ValueError):
                CollaborationPolicy(min_distinct_runtimes=bad)

    def test_max_below_one_is_rejected(self):
        for bad in (0, -2):
            with self.assertRaises(ValueError):
                CollaborationPolicy(max_distinct_runtimes=bad)

    def test_min_above_max_is_rejected(self):
        with self.assertRaises(ValueError):
            CollaborationPolicy(min_distinct_runtimes=3, max_distinct_runtimes=2)

    def test_allowlist_cardinality_below_min_is_rejected(self):
        with self.assertRaises(ValueError):
            CollaborationPolicy(runtime_allowlist=("claude-cli", "codex-cli"),
                                min_distinct_runtimes=3)

    def test_duplicate_allowlist_entries_are_normalized(self):
        policy = CollaborationPolicy(
            runtime_allowlist=("codex-cli", "claude-cli", "codex-cli"))
        self.assertEqual(policy.runtime_allowlist,
                         ("claude-cli", "codex-cli"))

    def test_policy_is_frozen(self):
        policy = CollaborationPolicy(min_distinct_runtimes=2)
        with self.assertRaises(Exception):
            policy.min_distinct_runtimes = 3

    def test_policy_repr_is_secret_free(self):
        policy = CollaborationPolicy(runtime_allowlist=("claude-cli",))
        surface = repr(policy).lower()
        for marker in SECRET_MARKERS:
            self.assertNotIn(marker, surface)


class ApplyPolicyTests(unittest.TestCase):
    def test_output_candidates_are_subset_of_input(self):
        sets = make_candidate_sets([CLAUDE, CODEX, PI])
        policy = CollaborationPolicy(
            runtime_allowlist=("claude-cli", "pi-cli"),
            min_distinct_runtimes=2)
        application = apply_policy(sets, policy)
        for role in _ROLES:
            self.assertTrue(
                set(application.filtered_sets[role].candidates)
                <= set(sets[role].candidates), role)

    def test_allowlist_removes_non_members(self):
        sets = make_candidate_sets([CLAUDE, CODEX, PI])
        policy = CollaborationPolicy(
            runtime_allowlist=("claude-cli", "pi-cli"),
            min_distinct_runtimes=2)
        application = apply_policy(sets, policy)
        for role in _ROLES:
            for candidate in application.filtered_sets[role].candidates:
                self.assertNotEqual(candidate.runtime_id, "codex-cli")

    def test_none_policy_returns_sets_unchanged(self):
        sets = make_candidate_sets([CLAUDE, CODEX])
        application = apply_policy(sets, CollaborationPolicy())
        for role in _ROLES:
            self.assertEqual(
                application.filtered_sets[role].candidates,
                sets[role].candidates, role)
        self.assertEqual(application.absent_runtimes, ())

    def test_absent_runtime_is_reported(self):
        # allowlist 提到 gemini-cli，但候选集中只有 claude-cli。
        sets = make_candidate_sets([CLAUDE])
        policy = CollaborationPolicy(
            runtime_allowlist=("claude-cli", "gemini-cli"))
        application = apply_policy(sets, policy)
        self.assertEqual(application.absent_runtimes, ("gemini-cli",))

    def test_absent_report_covers_allowlist_only(self):
        # 未被点名的缺席 runtime（codex 不在 allowlist）不计入缺席。
        sets = make_candidate_sets([CLAUDE])
        policy = CollaborationPolicy(runtime_allowlist=("claude-cli",))
        application = apply_policy(sets, policy)
        self.assertEqual(application.absent_runtimes, ())

    def test_max_distinct_trims_in_existing_order(self):
        # 三个 runtime、max=1：只能在既有候选顺序里保留一个 runtime。
        sets = make_candidate_sets([CLAUDE, CODEX, PI])
        policy = CollaborationPolicy(max_distinct_runtimes=1)
        application = apply_policy(sets, policy)
        kept = {candidate.runtime_id
                for role in _ROLES
                for candidate in application.filtered_sets[role].candidates}
        self.assertEqual(kept, {"claude-cli"})  # bridge 顺序（sorted identities）首个

    def test_deterministic(self):
        sets = make_candidate_sets([CLAUDE, CODEX, PI])
        policy = CollaborationPolicy(
            runtime_allowlist=("claude-cli", "codex-cli", "pi-cli"))
        first = apply_policy(sets, policy)
        for _ in range(3):
            self.assertEqual(first, apply_policy(sets, policy))


class PolicyConstrainedAssignerTests(unittest.TestCase):
    def test_assigner_returns_role_assignment_shape(self):
        sets = make_candidate_sets([CLAUDE, CODEX])
        assignment = PolicyConstrainedAssigner(
            CollaborationPolicy(min_distinct_runtimes=2)).assign(
            sets, Complexity.COMPLEX)
        self.assertIsInstance(assignment, RoleAssignment)
        self.assertEqual(sorted(assignment.assignments),
                         ["architect", "coder", "review", "test"])

    def test_two_runtime_pool_spreads(self):
        sets = make_candidate_sets([CLAUDE, CODEX])
        assignment = PolicyConstrainedAssigner(
            CollaborationPolicy(min_distinct_runtimes=2)).assign(
            sets, Complexity.COMPLEX)
        self.assertEqual(assignment.assignments["architect"].runtime_id,
                         "claude-cli")
        self.assertEqual(assignment.assignments["coder"].runtime_id,
                         "codex-cli")
        self.assertEqual(assignment.reason, "POLICY_SPREAD")

    def test_allowlist_excludes_runtime_from_assignments(self):
        # 输入 claude/codex/pi；allowlist 只留 claude/pi → 不出现 codex。
        sets = make_candidate_sets([CLAUDE, CODEX, PI])
        assignment = PolicyConstrainedAssigner(
            CollaborationPolicy(
                runtime_allowlist=("claude-cli", "pi-cli"),
                min_distinct_runtimes=2)).assign(sets, Complexity.COMPLEX)
        used = {candidate.runtime_id
                for candidate in assignment.assignments.values()
                if candidate is not None}
        self.assertEqual(used, {"claude-cli", "pi-cli"})
        self.assertNotIn("codex-cli", used)
        self.assertEqual(assignment.reason, "POLICY_SPREAD")

    def test_absent_runtime_reason(self):
        sets = make_candidate_sets([CLAUDE])
        assignment = PolicyConstrainedAssigner(
            CollaborationPolicy(
                runtime_allowlist=("claude-cli", "gemini-cli"))).assign(
            sets, Complexity.COMPLEX)
        self.assertEqual(assignment.reason, "POLICY_RUNTIME_ABSENT=gemini-cli")

    def test_min_unsatisfied_is_honest_not_retried(self):
        # 只有一个 runtime，min=2：诚实返回 assignment + COUNT_UNSATISFIED。
        sets = make_candidate_sets([CLAUDE])
        assigner = PolicyConstrainedAssigner(
            CollaborationPolicy(min_distinct_runtimes=2))
        assignment = assigner.assign(sets, Complexity.COMPLEX)
        self.assertEqual(assignment.reason, "POLICY_COUNT_UNSATISFIED")
        # assignment 仍然完整（收敛在唯一 runtime 上）。
        for role in _ROLES:
            self.assertEqual(assignment.assignments[role].runtime_id,
                             "claude-cli")

    def test_max_one_converges_to_single_runtime(self):
        sets = make_candidate_sets([CLAUDE, CODEX, PI])
        assignment = PolicyConstrainedAssigner(
            CollaborationPolicy(max_distinct_runtimes=1)).assign(
            sets, Complexity.COMPLEX)
        used = {candidate.runtime_id
                for candidate in assignment.assignments.values()
                if candidate is not None}
        self.assertEqual(used, {"claude-cli"})

    def test_reuse_true_allows_same_runtime_multiple_roles(self):
        sets = make_candidate_sets([CLAUDE])
        assignment = PolicyConstrainedAssigner(
            CollaborationPolicy(allow_runtime_reuse=True)).assign(
            sets, Complexity.COMPLEX)
        for role in _ROLES:
            self.assertEqual(assignment.assignments[role].runtime_id,
                             "claude-cli")

    def test_reuse_false_is_injective(self):
        sets = make_candidate_sets([CLAUDE, CODEX, PI, GEMINI])
        assignment = PolicyConstrainedAssigner(
            CollaborationPolicy(allow_runtime_reuse=False)).assign(
            sets, Complexity.COMPLEX)
        used = [candidate.runtime_id
                for candidate in assignment.assignments.values()
                if candidate is not None]
        self.assertEqual(len(used), len(set(used)))

    def test_reuse_false_insufficient_runtimes_assigns_none(self):
        # 4 个角色只有 2 个 runtime 且禁 reuse：后两个角色 None，
        # 绝不 backfill、绝不扩候选。
        sets = make_candidate_sets([CLAUDE, CODEX])
        assignment = PolicyConstrainedAssigner(
            CollaborationPolicy(allow_runtime_reuse=False)).assign(
            sets, Complexity.COMPLEX)
        assigned = [candidate for candidate
                    in assignment.assignments.values()
                    if candidate is not None]
        self.assertEqual(len(assigned), 2)
        for role in ("review", "test"):
            self.assertIsNone(assignment.assignments[role], role)

    def test_assignments_come_only_from_input_sets(self):
        # 无铸造：每个被指派候选的完整 identity ∈ 原始候选集。
        sets = make_candidate_sets([CLAUDE, CODEX, PI])
        policies = (
            CollaborationPolicy(),
            CollaborationPolicy(min_distinct_runtimes=2),
            CollaborationPolicy(max_distinct_runtimes=1),
            CollaborationPolicy(allow_runtime_reuse=False),
            CollaborationPolicy(runtime_allowlist=("claude-cli", "pi-cli")),
        )
        for policy in policies:
            assignment = PolicyConstrainedAssigner(policy).assign(
                sets, Complexity.COMPLEX)
            for role, candidate in assignment.assignments.items():
                if candidate is not None:
                    self.assertIn(candidate_identity(candidate),
                                  [candidate_identity(c)
                                   for c in sets[role].candidates],
                                  f"{policy} {role}")

    def test_deterministic_across_repeated_calls(self):
        sets = make_candidate_sets([CLAUDE, CODEX, PI])
        policy = CollaborationPolicy(
            runtime_allowlist=("claude-cli", "codex-cli", "pi-cli"),
            min_distinct_runtimes=2)
        assigner = PolicyConstrainedAssigner(policy)
        first = assigner.assign(sets, Complexity.COMPLEX)
        for _ in range(3):
            self.assertEqual(first, assigner.assign(sets, Complexity.COMPLEX))

    def test_reason_vocabulary_is_closed(self):
        sets = make_candidate_sets([CLAUDE, CODEX])
        cases = [
            CollaborationPolicy(min_distinct_runtimes=2),
            CollaborationPolicy(),
            CollaborationPolicy(min_distinct_runtimes=2),
            CollaborationPolicy(runtime_allowlist=("claude-cli", "gemini-cli")),
        ]
        vocabulary = ("POLICY_SPREAD", "POLICY_CONVERGED",
                      "POLICY_COUNT_UNSATISFIED", "POLICY_RUNTIME_ABSENT")
        for policy in cases:
            reason = PolicyConstrainedAssigner(policy).assign(
                sets, Complexity.COMPLEX).reason
            if reason.startswith("POLICY_RUNTIME_ABSENT="):
                continue
            self.assertIn(reason, vocabulary)

    def test_score_stays_none(self):
        sets = make_candidate_sets([CLAUDE, CODEX, PI])
        assignment = PolicyConstrainedAssigner(
            CollaborationPolicy(min_distinct_runtimes=2)).assign(
            sets, Complexity.COMPLEX)
        for candidate in assignment.assignments.values():
            self.assertIsNone(candidate.score)

    def test_surface_is_secret_free(self):
        sets = make_candidate_sets([CLAUDE, CODEX])
        assignment = PolicyConstrainedAssigner(
            CollaborationPolicy(min_distinct_runtimes=2)).assign(
            sets, Complexity.COMPLEX)
        surface = repr(assignment).lower()
        for marker in SECRET_MARKERS:
            self.assertNotIn(marker, surface)


class SourceScanTests(unittest.TestCase):
    def test_no_parallel_or_runtime_branching(self):
        import collaboration_policy as module
        source = Path(module.__file__).read_text(encoding="utf-8")
        lowered = source.lower()
        for name in ("claude", "codex", "deepseek", "openai", "anthropic",
                     "gemini", "tiny-agents", "tiny_agents"):
            self.assertNotIn(name, lowered)
        for forbidden in ("import asyncio", "import threading",
                          "import multiprocessing", "import concurrent",
                          "os.environ", "getenv", "RUN_REAL_PROVIDER_TESTS",
                          "subprocess", "requests", "urllib", "socket",
                          "http", "websocket", "a2a", "async", "await",
                          "uuid", "random", "datetime", "import time",
                          "time.", "monotonic", "sleep", "clock",
                          "if runtime ==", "if runtime_id =="):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
