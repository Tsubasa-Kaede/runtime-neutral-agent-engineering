"""Phase 10C-D: bridge RuntimeSelectionResult into the existing InvocationPlan.

Pure translation: no rescoring, no reselection, no fallback, no invocation.
Illegal selections become a structured, deterministic, secret-free plan with
error reasons and empty stages — never a fabricated runtime or agent.
"""
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from invocation_plan import InvocationPlan
from selection_plan_bridge import bridge_selection
from stage_runtime_selection import (
    RuntimeSelectionResult,
    SelectionMode,
    SelectionReason,
    StageSelection,
)
from task_budget import BudgetUsage, TaskBudget
from loop_guard import LoopGuard


def selection(mode, stages, reason=SelectionReason.CLEAR_SPECIALIZATION):
    return RuntimeSelectionResult(mode, tuple(stages), reason)


def stage(stage_name, runtime_id, agent_id, score=0.9, rank=1):
    return StageSelection(stage_name, runtime_id, agent_id, score, rank)


def budget():
    return TaskBudget(4, 4)


class Phase10DBridgeTests(unittest.TestCase):
    def bridge(self, sel, complexity="COMPLEX", task_id="task-1", mode="AUTO", usage=None):
        return bridge_selection(
            selection=sel, task_id=task_id, mode=mode,
            complexity=complexity, budget=budget(), usage=usage or BudgetUsage(),
        )

    def test_simple_selection_bridges_to_single_stage_plan(self):
        plan = self.bridge(selection(SelectionMode.SINGLE, [stage("coder", "ra", "agent-a")], SelectionReason.SIMPLE_TASK), "SIMPLE")
        self.assertIsInstance(plan, InvocationPlan)
        self.assertEqual([s.stage for s in plan.stages], ["coder"])
        self.assertEqual(plan.stages[0].runtime_id, "ra")
        self.assertEqual(plan.stages[0].agent_id, "agent-a")
        self.assertEqual(plan.complexity, "SIMPLE")

    def test_medium_selection_bridges_two_stages(self):
        sel = selection(SelectionMode.SINGLE, [
            stage("coder", "ra", "agent-a"), stage("test", "ra", "agent-a"),
        ], SelectionReason.SINGLE_RUNTIME_POOL)
        plan = self.bridge(sel, "MEDIUM")
        self.assertEqual([s.stage for s in plan.stages], ["coder", "test"])

    def test_complex_multi_runtime_plan(self):
        sel = selection(SelectionMode.MULTI, [
            stage("architect", "ra", "agent-a"),
            stage("coder", "rb", "agent-b"),
            stage("test", "rb", "agent-b"),
            stage("review", "ra", "agent-a"),
        ])
        plan = self.bridge(sel)
        mapping = {s.stage: (s.agent_id, s.runtime_id) for s in plan.stages}
        self.assertEqual(mapping["architect"], ("agent-a", "ra"))
        self.assertEqual(mapping["coder"], ("agent-b", "rb"))
        self.assertEqual(mapping["test"], ("agent-b", "rb"))
        self.assertEqual(mapping["review"], ("agent-a", "ra"))
        self.assertEqual(plan.selected_agents, ("agent-a", "agent-b", "agent-b", "agent-a"))

    def test_four_runtime_distribution_preserved(self):
        sel = selection(SelectionMode.MULTI, [
            stage("architect", "r1", "a1"), stage("coder", "r2", "a2"),
            stage("test", "r3", "a3"), stage("review", "r4", "a4"),
        ])
        plan = self.bridge(sel)
        self.assertEqual([s.runtime_id for s in plan.stages], ["r1", "r2", "r3", "r4"])

    def test_single_selection_is_not_upgraded(self):
        sel = selection(SelectionMode.SINGLE, [
            stage("architect", "ra", "agent-a"), stage("coder", "ra", "agent-a"),
            stage("test", "ra", "agent-a"), stage("review", "ra", "agent-a"),
        ], SelectionReason.SINGLE_RUNTIME_POOL)
        plan = self.bridge(sel)
        self.assertEqual(len({s.runtime_id for s in plan.stages}), 1)
        self.assertEqual(plan.fallback_agents, ())

    def test_stage_order_in_selection_does_not_change_plan(self):
        forward = selection(SelectionMode.MULTI, [
            stage("architect", "ra", "agent-a"), stage("coder", "rb", "agent-b"),
            stage("test", "rb", "agent-b"), stage("review", "ra", "agent-a"),
        ])
        shuffled = selection(SelectionMode.MULTI, [
            stage("review", "ra", "agent-a"), stage("test", "rb", "agent-b"),
            stage("coder", "rb", "agent-b"), stage("architect", "ra", "agent-a"),
        ])
        self.assertEqual(
            {s.stage: s.runtime_id for s in self.bridge(forward).stages},
            {s.stage: s.runtime_id for s in self.bridge(shuffled).stages},
        )

    def test_deterministic_output(self):
        sel = selection(SelectionMode.MULTI, [
            stage("architect", "ra", "agent-a"), stage("coder", "rb", "agent-b"),
            stage("test", "rb", "agent-b"), stage("review", "ra", "agent-a"),
        ])
        self.assertEqual(self.bridge(sel), self.bridge(sel))

    def test_empty_selection_is_rejected(self):
        plan = self.bridge(selection(SelectionMode.SINGLE, [], SelectionReason.NO_CAPABLE_AGENT))
        self.assertEqual(plan.stages, ())
        self.assertTrue(any("EMPTY_SELECTION" in r for r in plan.reasons))

    def test_missing_required_stage_is_rejected(self):
        sel = selection(SelectionMode.MULTI, [
            stage("architect", "ra", "agent-a"), stage("coder", "rb", "agent-b"),
            stage("test", "rb", "agent-b"),
        ])
        plan = self.bridge(sel, "COMPLEX")
        self.assertEqual(plan.stages, ())
        self.assertTrue(any("MISSING_STAGE" in r for r in plan.reasons))

    def test_unknown_stage_is_rejected(self):
        sel = selection(SelectionMode.SINGLE, [stage("deploy", "ra", "agent-a")], SelectionReason.SIMPLE_TASK)
        plan = self.bridge(sel, "SIMPLE")
        self.assertEqual(plan.stages, ())
        self.assertTrue(any("UNKNOWN_STAGE" in r for r in plan.reasons))

    def test_missing_runtime_id_is_rejected(self):
        sel = selection(SelectionMode.SINGLE, [stage("coder", "", "agent-a")], SelectionReason.SIMPLE_TASK)
        plan = self.bridge(sel, "SIMPLE")
        self.assertEqual(plan.stages, ())
        self.assertTrue(any("MISSING_RUNTIME_ID" in r for r in plan.reasons))

    def test_missing_agent_id_is_rejected(self):
        sel = selection(SelectionMode.SINGLE, [stage("coder", "ra", "")], SelectionReason.SIMPLE_TASK)
        plan = self.bridge(sel, "SIMPLE")
        self.assertEqual(plan.stages, ())
        self.assertTrue(any("MISSING_AGENT_ID" in r for r in plan.reasons))

    def test_stage_set_mismatch_with_complexity_is_rejected(self):
        sel = selection(SelectionMode.SINGLE, [
            stage("architect", "ra", "agent-a"), stage("coder", "ra", "agent-a"),
        ], SelectionReason.SINGLE_RUNTIME_POOL)
        plan = self.bridge(sel, "MEDIUM")  # MEDIUM expects coder+test, no architect
        self.assertEqual(plan.stages, ())
        self.assertTrue(any("MISSING_STAGE" in r or "UNKNOWN_STAGE" in r for r in plan.reasons))

    def test_no_fabricated_runtime_or_agent(self):
        bad = self.bridge(selection(SelectionMode.SINGLE, [stage("coder", "", "")], SelectionReason.SIMPLE_TASK), "SIMPLE")
        self.assertEqual(bad.stages, ())
        self.assertEqual(bad.selected_agents, ())

    def test_budget_snapshot_captured_without_mutation(self):
        usage = BudgetUsage()
        plan = self.bridge(selection(SelectionMode.SINGLE, [stage("coder", "ra", "agent-a")], SelectionReason.SIMPLE_TASK), "SIMPLE", usage=usage)
        self.assertEqual(plan.budget_snapshot.get("max_agent_calls"), 4)
        self.assertEqual(usage.total_agent_calls, 0)

    def test_no_budget_guard_or_invocation_effects(self):
        usage = BudgetUsage()
        guard = LoopGuard()
        self.bridge(selection(SelectionMode.MULTI, [
            stage("architect", "ra", "agent-a"), stage("coder", "rb", "agent-b"),
            stage("test", "rb", "agent-b"), stage("review", "ra", "agent-a"),
        ]))
        self.assertEqual(usage.total_agent_calls, 0)
        self.assertEqual(usage.iterations_used, 0)
        self.assertEqual(guard.check("t", "architect", "a"), "ALLOW")

    def test_plan_is_immutable_and_secret_free(self):
        plan = self.bridge(selection(SelectionMode.SINGLE, [stage("coder", "ra", "agent-a")], SelectionReason.SIMPLE_TASK), "SIMPLE")
        with self.assertRaises(Exception):
            plan.stages = ()
        surface = repr(plan).lower()
        for marker in ("secret", "api_key", "authorization", "stdout", "stderr", "token="):
            self.assertNotIn(marker, surface)

    def test_bridge_is_runtime_neutral_and_non_executing(self):
        import selection_plan_bridge as module
        source = Path(module.__file__).read_text(encoding="utf-8")
        lowered = source.lower()
        for name in ("claude", "codex", "gemini", "deepseek"):
            self.assertNotIn(name, lowered)
        self.assertNotIn("subprocess", source)
        # explicit: the bridge must not import adapters or runtime layers
        for forbidden in ("claude_code_adapter", "tiny_agents_adapter", "runtime_health", "orchestrator", "execution_engine"):
            self.assertNotIn(forbidden, source)
        self.assertNotIn("invoke", source)

    def test_role_and_capabilities_are_populated(self):
        plan = self.bridge(selection(SelectionMode.MULTI, [
            stage("architect", "ra", "agent-a"), stage("coder", "rb", "agent-b"),
            stage("test", "rb", "agent-b"), stage("review", "ra", "agent-a"),
        ]))
        architect_stage = plan.stages[0]
        self.assertEqual(architect_stage.role, "architect")
        self.assertIn("architecture", architect_stage.required_capabilities)


if __name__ == "__main__":
    unittest.main()
