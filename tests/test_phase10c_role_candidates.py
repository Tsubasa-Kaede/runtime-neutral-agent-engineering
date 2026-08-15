"""Phase 10C-B: Role Candidate Selection over a READY pool.

For each role, rank suitable READY candidates by reusing the existing
CapabilityRegistry scoring verbatim. This layer answers only "who is a
ranked candidate for this role?" — never pairing, never single/dual
decisions, never InvocationPlans.
"""
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from capability_registry import (
    AgentProfile,
    CapabilityConfidence,
    CapabilityEvidence,
    CapabilityName,
    CapabilityRegistry,
)
from role_candidates import RoleCandidate, RoleCandidateSelector, RoleCandidateSet
from runtime_discovery import RuntimeCandidate
from runtime_pool_construction import PooledRuntime, ReadyPool
from runtime_status import (
    HealthEvidence,
    ReasonCode,
    RuntimeState,
    RuntimeStatus,
)
from task_budget import BudgetUsage
from loop_guard import LoopGuard


def runtime_status(rid, state=RuntimeState.READY):
    return RuntimeStatus(rid, rid + ".exe", "1", state, "p", None, "managed", ReasonCode.NONE,
                         HealthEvidence("v", "v", "v", "v", "v"), 1, 100)


def pooled(rid, state=RuntimeState.READY):
    return PooledRuntime(RuntimeCandidate(rid, "cli", rid, state is RuntimeState.READY), runtime_status(rid, state))


def pool(ready_ids, excluded=()):
    return ReadyPool(tuple(pooled(rid) for rid in ready_ids), tuple(excluded))


def profile(aid, rid, spec):
    caps = {cap: CapabilityEvidence(cap, score, conf, "10c-b") for cap, (score, conf) in spec.items()}
    return AgentProfile(aid, rid, "p", None, None, caps, None)


FULL = {
    CapabilityName.ARCHITECTURE: (0.95, CapabilityConfidence.VERIFIED),
    CapabilityName.CODING: (0.95, CapabilityConfidence.VERIFIED),
    CapabilityName.TESTING: (0.95, CapabilityConfidence.VERIFIED),
    CapabilityName.REVIEW: (0.95, CapabilityConfidence.VERIFIED),
}


class Phase10CRoleCandidateTests(unittest.TestCase):
    def selector(self):
        return RoleCandidateSelector()

    def test_empty_pool_yields_empty_candidate_set(self):
        result = self.selector().candidates_for(pool([]), CapabilityRegistry([]), "architect")
        self.assertIsInstance(result, RoleCandidateSet)
        self.assertEqual(result.role, "architect")
        self.assertEqual(result.candidates, ())

    def test_single_ready_runtime_single_candidate(self):
        registry = CapabilityRegistry([profile("agent-a", "runtime-a", FULL)])
        result = self.selector().candidates_for(pool(["runtime-a"]), registry, "coder")
        self.assertEqual(len(result.candidates), 1)
        self.assertEqual(result.candidates[0].runtime_id, "runtime-a")
        self.assertEqual(result.candidates[0].agent_id, "agent-a")
        self.assertIsNotNone(result.candidates[0].score)

    def test_multiple_ready_sorted_by_score(self):
        registry = CapabilityRegistry([
            profile("agent-a", "runtime-a", {CapabilityName.CODING: (0.60, CapabilityConfidence.DECLARED)}),
            profile("agent-b", "runtime-b", {CapabilityName.CODING: (0.95, CapabilityConfidence.VERIFIED)}),
        ])
        result = self.selector().candidates_for(pool(["runtime-a", "runtime-b"]), registry, "coder")
        self.assertEqual([c.agent_id for c in result.candidates], ["agent-b", "agent-a"])

    def test_each_role_selects_its_own_best(self):
        registry = CapabilityRegistry([
            profile("arch-strong", "ra", {
                CapabilityName.ARCHITECTURE: (0.95, CapabilityConfidence.VERIFIED),
                CapabilityName.CODING: (0.60, CapabilityConfidence.DECLARED),
            }),
            profile("code-strong", "rb", {
                CapabilityName.CODING: (0.95, CapabilityConfidence.VERIFIED),
                CapabilityName.TESTING: (0.90, CapabilityConfidence.VERIFIED),
                CapabilityName.REVIEW: (0.60, CapabilityConfidence.DECLARED),
            }),
            profile("review-strong", "rc", {
                CapabilityName.REVIEW: (0.95, CapabilityConfidence.VERIFIED),
            }),
        ])
        ready = pool(["ra", "rb", "rc"])
        sel = self.selector()
        self.assertEqual(sel.candidates_for(ready, registry, "architect").candidates[0].agent_id, "arch-strong")
        self.assertEqual(sel.candidates_for(ready, registry, "coder").candidates[0].agent_id, "code-strong")
        self.assertEqual(sel.candidates_for(ready, registry, "test").candidates[0].agent_id, "code-strong")
        self.assertEqual(sel.candidates_for(ready, registry, "review").candidates[0].agent_id, "review-strong")

    def test_excluded_runtimes_never_become_candidates(self):
        registry = CapabilityRegistry([
            profile("agent-a", "runtime-a", FULL),
            profile("agent-x", "runtime-x", FULL),
        ])
        excluded = (pooled("runtime-x", RuntimeState.AUTH_REQUIRED),)
        result = self.selector().candidates_for(pool(["runtime-a"], excluded), registry, "coder")
        ids = [c.runtime_id for c in result.candidates]
        self.assertEqual(ids, ["runtime-a"])
        self.assertNotIn("runtime-x", ids)

    def test_all_non_ready_states_excluded(self):
        for state in (RuntimeState.AUTH_REQUIRED, RuntimeState.UNAVAILABLE, RuntimeState.ERROR):
            with self.subTest(state=state.value):
                registry = CapabilityRegistry([profile("agent-x", "runtime-x", FULL)])
                only_excluded = ReadyPool((), (pooled("runtime-x", state),))
                result = self.selector().candidates_for(only_excluded, registry, "coder")
                self.assertEqual(result.candidates, ())

    def test_unknown_capability_fails_hard_gate(self):
        registry = CapabilityRegistry([
            profile("agent-u", "runtime-u", {CapabilityName.CODING: (None, CapabilityConfidence.UNKNOWN)}),
        ])
        result = self.selector().candidates_for(pool(["runtime-u"]), registry, "coder")
        self.assertEqual(result.candidates, ())

    def test_verified_outranks_declared(self):
        registry = CapabilityRegistry([
            profile("agent-d", "runtime-d", {CapabilityName.CODING: (0.95, CapabilityConfidence.DECLARED)}),
            profile("agent-v", "runtime-v", {CapabilityName.CODING: (0.95, CapabilityConfidence.VERIFIED)}),
        ])
        result = self.selector().candidates_for(pool(["runtime-d", "runtime-v"]), registry, "coder")
        self.assertEqual(result.candidates[0].agent_id, "agent-v")

    def test_tie_break_is_agent_id_ascending(self):
        registry = CapabilityRegistry([
            profile("zz", "rz", FULL),
            profile("aa", "ra", FULL),
        ])
        result = self.selector().candidates_for(pool(["rz", "ra"]), registry, "coder")
        self.assertEqual([c.agent_id for c in result.candidates], ["aa", "zz"])

    def test_pool_order_does_not_change_result(self):
        registry = CapabilityRegistry([
            profile("agent-a", "runtime-a", {CapabilityName.CODING: (0.7, CapabilityConfidence.VERIFIED)}),
            profile("agent-b", "runtime-b", {CapabilityName.CODING: (0.9, CapabilityConfidence.VERIFIED)}),
        ])
        first = self.selector().candidates_for(pool(["runtime-a", "runtime-b"]), registry, "coder")
        second = self.selector().candidates_for(pool(["runtime-b", "runtime-a"]), registry, "coder")
        self.assertEqual(first, second)

    def test_deterministic_across_calls(self):
        registry = CapabilityRegistry([
            profile("agent-a", "runtime-a", FULL),
            profile("agent-b", "runtime-b", {CapabilityName.CODING: (0.5, CapabilityConfidence.DECLARED)}),
        ])
        ready = pool(["runtime-a", "runtime-b"])
        self.assertEqual(self.selector().candidates_for(ready, registry, "review"),
                         self.selector().candidates_for(ready, registry, "review"))

    def test_no_runtime_name_branches(self):
        import role_candidates
        text = Path(role_candidates.__file__).read_text(encoding="utf-8").lower()
        for name in ("claude", "codex", "gemini", "deepseek"):
            self.assertNotIn(name, text)

    def test_no_invocation_budget_or_guard_effects(self):
        registry = CapabilityRegistry([profile("agent-a", "runtime-a", FULL)])
        usage = BudgetUsage()
        guard = LoopGuard()
        self.selector().candidates_for(pool(["runtime-a"]), registry, "coder")
        self.assertEqual(usage.total_agent_calls, 0)
        self.assertEqual(usage.iterations_used, 0)
        self.assertEqual(guard.check("t", "architect", "a"), "ALLOW")
        self.assertFalse(hasattr(RoleCandidateSelector, "invoke"))

    def test_does_not_produce_invocation_plan(self):
        import role_candidates
        self.assertNotIn("invocation_plan", Path(role_candidates.__file__).read_text(encoding="utf-8"))
        self.assertFalse(hasattr(RoleCandidateSelector, "plan"))
        self.assertFalse(hasattr(RoleCandidateSelector, "decide"))

    def test_results_are_immutable_and_secret_free(self):
        registry = CapabilityRegistry([profile("agent-a", "runtime-a", FULL)])
        result = self.selector().candidates_for(pool(["runtime-a"]), registry, "coder")
        with self.assertRaises(Exception):
            result.candidates = ()
        with self.assertRaises(Exception):
            result.candidates[0].score = 99
        surface = repr(result).lower()
        for marker in ("token", "secret", "api_key", "authorization", "stdout", "stderr"):
            self.assertNotIn(marker, surface)

    def test_candidate_carries_evidence_metadata(self):
        registry = CapabilityRegistry([profile("agent-a", "runtime-a", FULL)])
        result = self.selector().candidates_for(pool(["runtime-a"]), registry, "architect")
        candidate = result.candidates[0]
        self.assertEqual(candidate.role, "architect")
        self.assertIn("architecture", candidate.evidence)
        self.assertEqual(candidate.rank, 1)


if __name__ == "__main__":
    unittest.main()
