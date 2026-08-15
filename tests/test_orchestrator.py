import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from invocation_plan import InvocationPlan
from orchestrator import Orchestrator
from task_budget import BudgetUsage, TaskBudget
from loop_guard import LoopGuard
from capability_registry import AgentProfile, CapabilityEvidence, CapabilityConfidence, CapabilityName, CapabilityRegistry
from runtime_status import HealthEvidence, ReasonCode, RuntimeState, RuntimeStatus


def runtime(rid, state=RuntimeState.READY):
    return RuntimeStatus(rid, rid + ".exe", "1", state, "p", "m", "managed", ReasonCode.NONE, HealthEvidence("v", "v", "v", "v", "v"), 1, 100)


def profile(aid, rid, caps):
    return AgentProfile(aid, rid, "p", "m", None, {cap: CapabilityEvidence(cap, .9, CapabilityConfidence.VERIFIED, "test") for cap in caps}, .8)


class OrchestratorTests(unittest.TestCase):
    def setup_orchestrator(self, profiles, runtimes):
        return Orchestrator(
            capability_registry=CapabilityRegistry(profiles),
            runtimes=runtimes,
            budget=TaskBudget(max_agent_calls=5, max_iterations=3),
            usage=BudgetUsage(),
            loop_guard=LoopGuard(),
        )

    def test_simple_plan_is_single_coder_fast_path(self):
        orchestrator = self.setup_orchestrator(
            [profile("coder", "r", {CapabilityName.CODING})], {"r": runtime("r")}
        )
        plan = orchestrator.plan("task-1", "fix one function", mode="AUTO")
        self.assertEqual([stage.stage for stage in plan.stages], ["coder"])

    def test_complex_plan_has_architect_coder_test_review(self):
        orchestrator = self.setup_orchestrator([
            profile("architect", "ra", {CapabilityName.ARCHITECTURE}),
            profile("coder", "rc", {CapabilityName.CODING}),
            profile("tester", "rt", {CapabilityName.TESTING}),
            profile("reviewer", "rr", {CapabilityName.REVIEW}),
        ], {rid: runtime(rid) for rid in ("ra", "rc", "rt", "rr")})
        plan = orchestrator.plan("task-1", "redesign architecture across modules", mode="AUTO")
        self.assertEqual([stage.stage for stage in plan.stages], ["architect", "coder", "test", "review"])

    def test_off_returns_empty_plan_without_selection(self):
        orchestrator = self.setup_orchestrator([], {})
        plan = orchestrator.plan("task-1", "redesign architecture", mode="OFF")
        self.assertEqual(plan.stages, ())
        self.assertEqual(plan.reasons, ("MODE_OFF",))

    def test_no_capable_agent_is_explicit(self):
        orchestrator = self.setup_orchestrator([], {})
        plan = orchestrator.plan("task-1", "redesign architecture", mode="ON")
        self.assertIn("NO_CAPABLE_AGENT", plan.reasons)

    def test_plan_is_immutable_serializable_and_deterministic(self):
        orchestrator = self.setup_orchestrator(
            [profile("coder", "r", {CapabilityName.CODING})], {"r": runtime("r")}
        )
        first = orchestrator.plan("task-1", "fix one function", mode="AUTO")
        second = orchestrator.plan("task-1", "fix one function", mode="AUTO")
        self.assertEqual(first, second)
        self.assertIsInstance(first.to_dict(), dict)


if __name__ == "__main__":
    unittest.main()
