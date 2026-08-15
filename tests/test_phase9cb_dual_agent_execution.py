import sys
import unittest
from pathlib import Path
from unittest.mock import Mock

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from capability_registry import (
    AgentProfile,
    CapabilityConfidence,
    CapabilityEvidence,
    CapabilityName,
    CapabilityRegistry,
)
from dual_agent_selection import (
    DecisionReason,
    DualAgentMode,
    DualAgentSelectionResult,
)
from external_runtime import InvocationResult, InvocationStatus
from orchestrator import Orchestrator
from runtime_status import HealthEvidence, ReasonCode, RuntimeState, RuntimeStatus
from structured_packets import ArchitecturePacket, ImplementationPacket, ReviewPacket, TestPacket
from task_budget import BudgetUsage, TaskBudget
from loop_guard import LoopGuard
from execution_engine import ExecutionStatus


def runtime(rid, state=RuntimeState.READY):
    return RuntimeStatus(rid, rid + ".exe", "1", state, "p", "m", "managed", ReasonCode.NONE, HealthEvidence("v", "v", "v", "v", "v"), 1, 100)


def profile(aid, rid, caps, role):
    return AgentProfile(aid, rid, "p", "m", role, {c: CapabilityEvidence(c, .9, CapabilityConfidence.VERIFIED, "test") for c in caps}, .8)


def stage_packet(role):
    if role == "architect":
        return ArchitecturePacket("task", "architect", ("goal",), (), ("architecture",), (), (), ("accept",), ())
    if role == "coder":
        return ImplementationPacket("task", "coder", ("file.py",), "summary", (), (), (), ())
    if role == "test":
        return TestPacket("task", "tester", ("test",), ("test",), (), (), ("validation",), ())
    return ReviewPacket("task", "reviewer", "PASS", (), (), (), (), ("passed",))


def selection(decision, assignments, primary=None, secondary=None, reason=DecisionReason.TWO_CAPABLE_AGENTS):
    evidence = tuple(f"{stage}={agent}" for stage, agent in sorted(assignments.items()))
    return DualAgentSelectionResult(decision, dict(assignments), primary, secondary, reason, evidence)


class Phase9CBDualAgentExecutionTests(unittest.TestCase):
    def setup(self, profiles, statuses, calls=10):
        budget = TaskBudget(calls, 4)
        usage = BudgetUsage()
        guard = LoopGuard(max_iterations=4)
        adapters = {}
        for item in profiles:
            adapter = Mock()
            adapter.invoke.side_effect = lambda request: InvocationResult(
                InvocationStatus.SUCCESS,
                stage_packet(request.role),
                trace=Mock(invocation_id=f"inv-{request.agent_id}-{request.role}", input_tokens="unknown", output_tokens="unknown"),
            )
            adapters[item.agent_id] = adapter
        orchestrator = Orchestrator(CapabilityRegistry(profiles), statuses, budget, usage, guard)
        return orchestrator, adapters, budget, usage, guard

    def dual_profiles(self):
        return [
            profile("agent-a", "ra", {CapabilityName.ARCHITECTURE, CapabilityName.REVIEW}, "architect"),
            profile("agent-b", "rb", {CapabilityName.CODING, CapabilityName.TESTING}, "coder"),
        ]

    def dual_statuses(self, state=RuntimeState.READY):
        return {"ra": runtime("ra", state), "rb": runtime("rb", state)}

    def test_two_agent_complex_maps_each_stage_owner(self):
        o, adapters, *_ = self.setup(self.dual_profiles(), self.dual_statuses())
        result = o.execute(
            "task", "redesign architecture across modules", adapters, "prompt",
            dual_selection=selection(
                DualAgentMode.TWO_AGENT,
                {"architect": "agent-a", "coder": "agent-b", "test": "agent-b", "review": "agent-a"},
                "agent-a", "agent-b",
            ),
        )
        self.assertEqual(result.status, ExecutionStatus.SUCCESS)
        self.assertEqual([type(p).__name__ for p in result.packets],
                         ["ArchitecturePacket", "ImplementationPacket", "TestPacket", "ReviewPacket"])

    def test_each_stage_invoked_exactly_once_with_role_owners(self):
        o, adapters, *_ = self.setup(self.dual_profiles(), self.dual_statuses())
        result = o.execute(
            "task", "redesign architecture across modules", adapters, "prompt",
            dual_selection=selection(
                DualAgentMode.TWO_AGENT,
                {"architect": "agent-a", "coder": "agent-b", "test": "agent-b", "review": "agent-a"},
                "agent-a", "agent-b",
            ),
        )
        self.assertEqual(result.status, ExecutionStatus.SUCCESS)
        a_roles = [call.args[0].role for call in adapters["agent-a"].invoke.call_args_list]
        b_roles = [call.args[0].role for call in adapters["agent-b"].invoke.call_args_list]
        self.assertEqual(a_roles, ["architect", "review"])
        self.assertEqual(b_roles, ["coder", "test"])

    def test_single_agent_all_stages_use_one_agent(self):
        profiles = [
            profile("agent-a", "ra", {CapabilityName.ARCHITECTURE, CapabilityName.REVIEW, CapabilityName.CODING, CapabilityName.TESTING}, "architect"),
            profile("agent-b", "rb", {CapabilityName.CODING}, "coder"),
        ]
        o, adapters, *_ = self.setup(profiles, self.dual_statuses())
        result = o.execute(
            "task", "redesign architecture across modules", adapters, "prompt",
            dual_selection=selection(
                DualAgentMode.SINGLE_AGENT,
                {"architect": "agent-a", "coder": "agent-a", "test": "agent-a", "review": "agent-a"},
                "agent-a", None, DecisionReason.SINGLE_CAPABLE_AGENT,
            ),
        )
        self.assertEqual(result.status, ExecutionStatus.SUCCESS)
        self.assertEqual(adapters["agent-b"].invoke.call_count, 0)
        self.assertEqual(adapters["agent-a"].invoke.call_count, 4)

    def test_no_agent_does_not_invoke_adapters(self):
        o, adapters, *_ = self.setup(self.dual_profiles(), self.dual_statuses())
        result = o.execute(
            "task", "redesign architecture across modules", adapters, "prompt",
            dual_selection=selection(DualAgentMode.NO_AGENT, {}, None, None, DecisionReason.NO_CAPABLE_AGENT),
        )
        self.assertEqual(result.status, ExecutionStatus.FAILED)
        adapters["agent-a"].invoke.assert_not_called()
        adapters["agent-b"].invoke.assert_not_called()
        self.assertIn("NO_CAPABLE_AGENT", result.errors)

    def test_plan_carries_correct_runtime_id_per_stage(self):
        o, adapters, *_ = self.setup(self.dual_profiles(), self.dual_statuses())
        plan = o.plan(
            "task", "redesign architecture across modules",
            dual_selection=selection(
                DualAgentMode.TWO_AGENT,
                {"architect": "agent-a", "coder": "agent-b", "test": "agent-b", "review": "agent-a"},
                "agent-a", "agent-b",
            ),
        )
        mapping = {stage.stage: (stage.agent_id, stage.runtime_id) for stage in plan.stages}
        self.assertEqual(mapping["architect"], ("agent-a", "ra"))
        self.assertEqual(mapping["coder"], ("agent-b", "rb"))
        self.assertEqual(mapping["test"], ("agent-b", "rb"))
        self.assertEqual(mapping["review"], ("agent-a", "ra"))

    def test_non_ready_runtime_blocks_execution(self):
        statuses = self.dual_statuses(RuntimeState.AUTH_REQUIRED)
        o, adapters, *_ = self.setup(self.dual_profiles(), statuses)
        result = o.execute(
            "task", "redesign architecture across modules", adapters, "prompt",
            dual_selection=selection(
                DualAgentMode.TWO_AGENT,
                {"architect": "agent-a", "coder": "agent-b", "test": "agent-b", "review": "agent-a"},
                "agent-a", "agent-b",
            ),
        )
        self.assertEqual(result.status, ExecutionStatus.FAILED)
        adapters["agent-a"].invoke.assert_not_called()
        adapters["agent-b"].invoke.assert_not_called()
        self.assertIn("RUNTIME_NOT_READY", result.errors)

    def test_dual_agent_shares_one_lifecycle_budget(self):
        o, adapters, budget, usage, _ = self.setup(self.dual_profiles(), self.dual_statuses())
        result = o.execute(
            "task", "redesign architecture across modules", adapters, "prompt",
            dual_selection=selection(
                DualAgentMode.TWO_AGENT,
                {"architect": "agent-a", "coder": "agent-b", "test": "agent-b", "review": "agent-a"},
                "agent-a", "agent-b",
            ),
        )
        self.assertEqual(result.status, ExecutionStatus.SUCCESS)
        self.assertEqual(usage.total_agent_calls, 4)

    def test_budget_exhausted_stops_before_more_calls(self):
        o, adapters, *_ = self.setup(self.dual_profiles(), self.dual_statuses(), calls=1)
        result = o.execute(
            "task", "redesign architecture across modules", adapters, "prompt",
            dual_selection=selection(
                DualAgentMode.TWO_AGENT,
                {"architect": "agent-a", "coder": "agent-b", "test": "agent-b", "review": "agent-a"},
                "agent-a", "agent-b",
            ),
        )
        self.assertEqual(result.status, ExecutionStatus.FAILED)
        self.assertIn("BUDGET_EXHAUSTED", result.errors)
        self.assertEqual(adapters["agent-a"].invoke.call_count + adapters["agent-b"].invoke.call_count, 1)

    def test_loop_guard_still_blocks_stage(self):
        o, adapters, budget, usage, guard = self.setup(self.dual_profiles(), self.dual_statuses())
        guard.record("task", "architect", "agent-a")
        result = o.execute(
            "task", "redesign architecture across modules", adapters, "prompt",
            dual_selection=selection(
                DualAgentMode.TWO_AGENT,
                {"architect": "agent-a", "coder": "agent-b", "test": "agent-b", "review": "agent-a"},
                "agent-a", "agent-b",
            ),
        )
        self.assertEqual(result.status, ExecutionStatus.FAILED)
        self.assertTrue(any("LOOP_GUARD" in error for error in result.errors))
        adapters["agent-a"].invoke.assert_not_called()

    def test_fallback_after_failure_shares_budget(self):
        o, adapters, budget, usage, _ = self.setup(self.dual_profiles(), self.dual_statuses(), calls=4)
        adapters["agent-b"].invoke.side_effect = None
        adapters["agent-b"].invoke.return_value = InvocationResult(InvocationStatus.FAILED, error="failed")
        result = o.execute(
            "task", "redesign architecture across modules", adapters, "prompt",
            dual_selection=selection(
                DualAgentMode.TWO_AGENT,
                {"architect": "agent-a", "coder": "agent-b", "test": "agent-b", "review": "agent-a"},
                "agent-a", "agent-b",
            ),
        )
        self.assertEqual(result.status, ExecutionStatus.FAILED)
        self.assertLessEqual(usage.total_agent_calls, 4)
        self.assertIn("INVOKE_FAILED", result.errors)

    def test_trace_and_unknown_tokens_preserved(self):
        o, adapters, *_ = self.setup(self.dual_profiles(), self.dual_statuses())
        adapters["agent-a"].invoke.side_effect = lambda request: InvocationResult(
            InvocationStatus.SUCCESS,
            stage_packet(request.role),
            trace=Mock(invocation_id="trace-a", input_tokens="unknown", output_tokens="unknown"),
        )
        result = o.execute(
            "task", "redesign architecture across modules", adapters, "prompt",
            dual_selection=selection(
                DualAgentMode.TWO_AGENT,
                {"architect": "agent-a", "coder": "agent-b", "test": "agent-b", "review": "agent-a"},
                "agent-a", "agent-b",
            ),
        )
        self.assertEqual(result.status, ExecutionStatus.SUCCESS)
        self.assertTrue(all(trace.input_tokens == "unknown" and trace.output_tokens == "unknown" for trace in result.traces))
        self.assertIn("trace-a", [trace.invocation_id for trace in result.traces])

    def test_simple_with_single_selection_runs_one_call(self):
        profiles = [
            profile("agent-a", "ra", {CapabilityName.CODING}, "coder"),
            profile("agent-b", "rb", {CapabilityName.CODING}, "coder"),
        ]
        o, adapters, *_ = self.setup(profiles, self.dual_statuses())
        result = o.execute(
            "task", "fix one function", adapters, "prompt",
            dual_selection=selection(DualAgentMode.SINGLE_AGENT, {"coder": "agent-a"}, "agent-a", None, DecisionReason.SIMPLE_TASK),
        )
        self.assertEqual(result.status, ExecutionStatus.SUCCESS)
        adapters["agent-b"].invoke.assert_not_called()
        self.assertEqual(adapters["agent-a"].invoke.call_count, 1)

    def test_without_selection_legacy_plan_still_works(self):
        o, adapters, *_ = self.setup(self.dual_profiles(), self.dual_statuses())
        result = o.execute("task", "redesign architecture across modules", adapters, "prompt")
        self.assertEqual(result.status, ExecutionStatus.SUCCESS)
        self.assertEqual(len(result.packets), 4)

    def test_without_selection_medium_keeps_phase9a_stages(self):
        o, adapters, *_ = self.setup(self.dual_profiles(), self.dual_statuses())
        plan = o.plan("task", "change two related files and add tests")
        self.assertEqual([stage.stage for stage in plan.stages], ["coder", "test"])


if __name__ == "__main__":
    unittest.main()
