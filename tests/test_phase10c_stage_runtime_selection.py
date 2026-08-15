"""Phase 10C-C: Offline Single / Multi-Runtime Selection.

Consumes ranked RoleCandidateSets (10C-B) and decides, per complexity and
the Phase-9C specialization semantics, whether stages run on a single
runtime or multiple runtimes — and which runtime each stage gets. Pure
decision: no health, no scoring, no plans, no invocation.
"""
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from role_candidates import RoleCandidate, RoleCandidateSet
from stage_runtime_selection import (
    RuntimeSelectionResult,
    SelectionMode,
    StageRuntimeSelector,
    StageSelection,
)
from task_budget import BudgetUsage
from loop_guard import LoopGuard


def candidate(role, runtime, agent=None, score=0.9, rank=1):
    return RoleCandidate(
        role=role, runtime_id=runtime, agent_id=agent or f"agent-{runtime}",
        score=score, rank=rank, evidence=role, required_capabilities=(role,),
    )


def candidate_set(role, specs):
    """specs: (runtime, agent, score). The set is constructed in the same
    order 10C-B guarantees: score DESC, agent_id ASC, runtime_id ASC."""
    ordered = sorted(specs, key=lambda item: (-item[2], item[1], item[0]))
    return RoleCandidateSet(role, tuple(
        candidate(role, rid, agent, score, rank=index + 1)
        for index, (rid, agent, score) in enumerate(ordered)
    ))


def complementary_sets():
    """A best at architecture/review, B best at coding/testing."""
    return {
        "architect": candidate_set("architect", [("ra", "agent-a", 0.95), ("rb", "agent-b", 0.68)]),
        "coder": candidate_set("coder", [("rb", "agent-b", 0.96), ("ra", "agent-a", 0.62)]),
        "test": candidate_set("test", [("rb", "agent-b", 0.95), ("ra", "agent-a", 0.60)]),
        "review": candidate_set("review", [("ra", "agent-a", 0.95), ("rb", "agent-b", 0.60)]),
    }


def one_runtime_all_roles(runtime="ra", agent="agent-a"):
    return {role: candidate_set(role, [(runtime, agent, 0.9)]) for role in ("architect", "coder", "test", "review")}


class Phase10CStageRuntimeSelectionTests(unittest.TestCase):
    def select(self, sets, complexity):
        return StageRuntimeSelector().select(sets, complexity)

    def test_simple_is_single_runtime(self):
        result = self.select(complementary_sets(), "SIMPLE")
        self.assertEqual(result.mode, SelectionMode.SINGLE)
        self.assertEqual([s.stage for s in result.stage_selections], ["coder"])

    def test_simple_multiple_ready_stays_single(self):
        result = self.select(complementary_sets(), "SIMPLE")
        self.assertEqual(len({s.runtime_id for s in result.stage_selections}), 1)

    def test_medium_defaults_single_without_specialization(self):
        sets = one_runtime_all_roles()
        result = self.select(sets, "MEDIUM")
        self.assertEqual(result.mode, SelectionMode.SINGLE)
        self.assertEqual([s.stage for s in result.stage_selections], ["coder", "test"])

    def test_medium_weak_specialization_stays_single(self):
        sets = {
            "architect": candidate_set("architect", [("ra", "a1", 0.90), ("rb", "b1", 0.88)]),
            "coder": candidate_set("coder", [("rb", "b1", 0.90), ("ra", "a1", 0.88)]),
            "test": candidate_set("test", [("rb", "b1", 0.90)]),
            "review": candidate_set("review", [("ra", "a1", 0.90)]),
        }
        result = self.select(sets, "MEDIUM")
        self.assertEqual(result.mode, SelectionMode.SINGLE)

    def test_medium_clear_specialization_goes_multi(self):
        result = self.select(complementary_sets(), "MEDIUM")
        self.assertEqual(result.mode, SelectionMode.MULTI)
        mapping = {s.stage: s.runtime_id for s in result.stage_selections}
        self.assertEqual(mapping["coder"], "rb")
        self.assertEqual(mapping["test"], "rb")

    def test_complex_can_go_multi(self):
        result = self.select(complementary_sets(), "COMPLEX")
        self.assertEqual(result.mode, SelectionMode.MULTI)
        mapping = {s.stage: s.runtime_id for s in result.stage_selections}
        self.assertEqual(mapping["architect"], "ra")
        self.assertEqual(mapping["coder"], "rb")
        self.assertEqual(mapping["test"], "rb")
        self.assertEqual(mapping["review"], "ra")

    def test_complex_does_not_force_multi(self):
        result = self.select(one_runtime_all_roles(), "COMPLEX")
        self.assertEqual(result.mode, SelectionMode.SINGLE)
        self.assertEqual(len({s.runtime_id for s in result.stage_selections}), 1)

    def test_single_runtime_pool_is_single(self):
        result = self.select(one_runtime_all_roles(), "COMPLEX")
        self.assertEqual(result.mode, SelectionMode.SINGLE)

    def test_four_stages_can_map_to_distinct_runtimes(self):
        sets = {
            "architect": candidate_set("architect", [("ra", "a1", 0.95)]),
            "coder": candidate_set("coder", [("rb", "b1", 0.95)]),
            "test": candidate_set("test", [("rc", "c1", 0.95)]),
            "review": candidate_set("review", [("rd", "d1", 0.95)]),
        }
        result = self.select(sets, "COMPLEX")
        mapping = {s.stage: s.runtime_id for s in result.stage_selections}
        self.assertEqual(mapping, {"architect": "ra", "coder": "rb", "test": "rc", "review": "rd"})

    def test_missing_stage_candidates_is_explicit_not_fake(self):
        sets = complementary_sets()
        sets["coder"] = candidate_set("coder", [])
        result = self.select(sets, "COMPLEX")
        self.assertEqual(result.stage_selections, ())
        self.assertEqual(result.reason.value, "NO_CAPABLE_AGENT")

    def test_score_orders_candidates(self):
        sets = {
            "coder": candidate_set("coder", [("rb", "b1", 0.99), ("ra", "a1", 0.50)]),
        }
        result = self.select(sets, "SIMPLE")
        self.assertEqual(result.stage_selections[0].runtime_id, "rb")

    def test_tie_break_agent_then_runtime(self):
        sets = {
            "coder": candidate_set("coder", [("rz", "zz-agent", 0.9), ("ra", "aa-agent", 0.9)]),
        }
        result = self.select(sets, "SIMPLE")
        self.assertEqual(result.stage_selections[0].agent_id, "aa-agent")
        same_agent = candidate_set("coder", [("rz", "same", 0.9), ("ra", "same", 0.9)])
        result2 = StageRuntimeSelector().select({"coder": same_agent}, "SIMPLE")
        self.assertEqual(result2.stage_selections[0].runtime_id, "ra")

    def test_input_order_does_not_change_result(self):
        base = complementary_sets()
        first = self.select(base, "COMPLEX")
        alt = {
            "architect": candidate_set("architect", [("rb", "agent-b", 0.68), ("ra", "agent-a", 0.95)]),
            "coder": candidate_set("coder", [("ra", "agent-a", 0.62), ("rb", "agent-b", 0.96)]),
            "test": candidate_set("test", [("ra", "agent-a", 0.60), ("rb", "agent-b", 0.95)]),
            "review": candidate_set("review", [("rb", "agent-b", 0.60), ("ra", "agent-a", 0.95)]),
        }
        second = StageRuntimeSelector().select(alt, "COMPLEX")
        self.assertEqual(first.mode, second.mode)
        self.assertEqual(
            {s.stage: s.runtime_id for s in first.stage_selections},
            {s.stage: s.runtime_id for s in second.stage_selections},
        )

    def test_repeated_calls_are_identical(self):
        sets = complementary_sets()
        self.assertEqual(self.select(sets, "COMPLEX"), self.select(sets, "COMPLEX"))

    def test_no_invocation_adapter_budget_guard_or_plan(self):
        import stage_runtime_selection as module
        usage = BudgetUsage()
        guard = LoopGuard()
        self.select(complementary_sets(), "COMPLEX")
        self.assertEqual(usage.total_agent_calls, 0)
        self.assertEqual(usage.iterations_used, 0)
        self.assertEqual(guard.check("t", "architect", "a"), "ALLOW")
        source = Path(module.__file__).read_text(encoding="utf-8")
        self.assertNotIn("invocation_plan", source)
        self.assertNotIn("invoke", source)
        self.assertFalse(hasattr(StageRuntimeSelector, "plan"))

    def test_no_dual_agent_pair_type(self):
        import stage_runtime_selection as module
        source = Path(module.__file__).read_text(encoding="utf-8")
        self.assertNotIn("DualAgentPair", source)
        self.assertNotIn("dual_agent_pair", source)

    def test_result_is_immutable_and_secret_free(self):
        result = self.select(complementary_sets(), "COMPLEX")
        with self.assertRaises(Exception):
            result.mode = SelectionMode.SINGLE
        with self.assertRaises(Exception):
            result.stage_selections[0].runtime_id = "x"
        surface = repr(result).lower()
        for marker in ("token", "secret", "api_key", "authorization", "stdout", "stderr"):
            self.assertNotIn(marker, surface)

    def test_runtime_neutral_source(self):
        import stage_runtime_selection as module
        text = Path(module.__file__).read_text(encoding="utf-8").lower()
        for name in ("claude", "codex", "gemini", "deepseek"):
            self.assertNotIn(name, text)

    def test_single_converges_when_specialization_insufficient(self):
        # rank1 crosses runtimes but gains are below threshold -> single,
        # deterministically converged onto the coder-best runtime.
        sets = {
            "architect": candidate_set("architect", [("ra", "a1", 0.90), ("rb", "b1", 0.88)]),
            "coder": candidate_set("coder", [("rb", "b1", 0.91), ("ra", "a1", 0.88)]),
            "test": candidate_set("test", [("rb", "b1", 0.90), ("ra", "a1", 0.85)]),
        }
        result = self.select(sets, "MEDIUM")
        self.assertEqual(result.mode, SelectionMode.SINGLE)
        runtimes = {s.runtime_id for s in result.stage_selections}
        self.assertEqual(runtimes, {"rb"})


if __name__ == "__main__":
    unittest.main()
