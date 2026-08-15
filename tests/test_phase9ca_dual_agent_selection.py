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
)
from dual_agent_selection import (
    DecisionReason,
    DualAgentMode,
    DualAgentSelection,
)
from runtime_status import HealthEvidence, ReasonCode, RuntimeState, RuntimeStatus
from task_classifier import Complexity
from task_budget import BudgetUsage, TaskBudget


def runtime(rid, state=RuntimeState.READY):
    return RuntimeStatus(rid, rid + ".exe", "1", state, "p", "m", "managed", ReasonCode.NONE, HealthEvidence("v", "v", "v", "v", "v"), 1, 100)


def profile(aid, rid, caps, role):
    return AgentProfile(aid, rid, "p", "m", role, {c: CapabilityEvidence(c, .9, CapabilityConfidence.VERIFIED, "test") for c in caps}, .8)


class DualAgentSelectionTests(unittest.TestCase):
    def select(self, profiles, statuses, complexity=Complexity.COMPLEX, calls=10, usage=None):
        usage = usage or BudgetUsage()
        return DualAgentSelection().select(
            profiles=profiles,
            runtimes=statuses,
            complexity=complexity,
            budget=TaskBudget(calls, 4),
            usage=usage,
        )

    def full_profiles(self):
        return [
            profile("a-architect", "ra", {CapabilityName.ARCHITECTURE, CapabilityName.REVIEW}, "architect"),
            profile("b-coder", "rb", {CapabilityName.CODING, CapabilityName.TESTING}, "coder"),
        ]

    def full_statuses(self, state=RuntimeState.READY):
        return {"ra": runtime("ra", state), "rb": runtime("rb", state)}

    def test_two_ready_agents_complex_returns_two_agent(self):
        result = self.select(self.full_profiles(), self.full_statuses(), Complexity.COMPLEX)
        self.assertEqual(result.decision, DualAgentMode.TWO_AGENT)
        self.assertEqual(result.primary_agent, "a-architect")
        self.assertEqual(result.secondary_agent, "b-coder")

    def test_single_ready_agent_downgrades_to_single_agent(self):
        profiles = [profile("b-coder", "rb", {CapabilityName.CODING}, "coder")]
        result = self.select(profiles, {"rb": runtime("rb")}, Complexity.COMPLEX)
        self.assertEqual(result.decision, DualAgentMode.SINGLE_AGENT)
        self.assertIsNone(result.secondary_agent)

    def test_no_ready_agent_returns_no_agent(self):
        result = self.select(self.full_profiles(), {}, Complexity.COMPLEX)
        self.assertEqual(result.decision, DualAgentMode.NO_AGENT)

    def test_second_agent_missing_capability_stays_single(self):
        profiles = [
            profile("a-architect", "ra", {CapabilityName.ARCHITECTURE, CapabilityName.REVIEW, CapabilityName.CODING, CapabilityName.TESTING}, "architect"),
            profile("b-coder", "rb", {CapabilityName.CODING}, "coder"),
        ]
        result = self.select(profiles, self.full_statuses(), Complexity.COMPLEX)
        self.assertEqual(result.decision, DualAgentMode.SINGLE_AGENT)

    def test_auth_required_never_selected(self):
        statuses = self.full_statuses()
        statuses["rb"] = runtime("rb", RuntimeState.AUTH_REQUIRED)
        result = self.select(self.full_profiles(), statuses, Complexity.COMPLEX)
        self.assertEqual(result.decision, DualAgentMode.SINGLE_AGENT)
        self.assertEqual(result.primary_agent, "a-architect")

    def test_unavailable_never_selected(self):
        statuses = self.full_statuses()
        statuses["rb"] = runtime("rb", RuntimeState.UNAVAILABLE)
        result = self.select(self.full_profiles(), statuses, Complexity.COMPLEX)
        self.assertEqual(result.decision, DualAgentMode.SINGLE_AGENT)

    def test_error_never_selected(self):
        statuses = self.full_statuses()
        statuses["ra"] = runtime("ra", RuntimeState.ERROR)
        result = self.select(self.full_profiles(), statuses, Complexity.COMPLEX)
        self.assertEqual(result.decision, DualAgentMode.SINGLE_AGENT)

    def test_same_agent_never_counts_as_two(self):
        profiles = [self.full_profiles()[0]]
        result = self.select(profiles, {"ra": runtime("ra")}, Complexity.COMPLEX)
        self.assertEqual(result.decision, DualAgentMode.SINGLE_AGENT)
        self.assertEqual(result.primary_agent, "a-architect")
        self.assertIsNone(result.secondary_agent)

    def test_simple_defaults_to_single_agent(self):
        result = self.select(self.full_profiles(), self.full_statuses(), Complexity.SIMPLE)
        self.assertEqual(result.decision, DualAgentMode.SINGLE_AGENT)
        self.assertEqual(result.reason, DecisionReason.SIMPLE_TASK)

    def test_complex_with_two_capable_agents_uses_two(self):
        result = self.select(self.full_profiles(), self.full_statuses(), Complexity.COMPLEX)
        self.assertEqual(result.decision, DualAgentMode.TWO_AGENT)
        self.assertEqual(result.assignments["architect"], "a-architect")
        self.assertEqual(result.assignments["coder"], "b-coder")

    def test_selection_is_deterministic(self):
        first = self.select(self.full_profiles(), self.full_statuses(), Complexity.COMPLEX)
        second = self.select(self.full_profiles(), self.full_statuses(), Complexity.COMPLEX)
        self.assertEqual(first, second)

    def test_runtime_names_do_not_change_decision(self):
        renamed = [
            profile("a-architect", "runtime-z", {CapabilityName.ARCHITECTURE, CapabilityName.REVIEW}, "architect"),
            profile("b-coder", "runtime-y", {CapabilityName.CODING, CapabilityName.TESTING}, "coder"),
        ]
        statuses = {"runtime-z": runtime("runtime-z"), "runtime-y": runtime("runtime-y")}
        result = self.select(renamed, statuses, Complexity.COMPLEX)
        self.assertEqual(result.decision, DualAgentMode.TWO_AGENT)
        self.assertEqual(result.primary_agent, "a-architect")

    def test_budget_too_small_avoids_two_agent(self):
        usage = BudgetUsage()
        usage.total_agent_calls = 9
        result = self.select(self.full_profiles(), self.full_statuses(), Complexity.COMPLEX, calls=1, usage=usage)
        self.assertNotEqual(result.decision, DualAgentMode.TWO_AGENT)

    def test_selection_does_not_invoke_anything(self):
        result = self.select(self.full_profiles(), self.full_statuses(), Complexity.COMPLEX)
        self.assertNotIn("invoke", dir(result))
        self.assertEqual(result.decision, DualAgentMode.TWO_AGENT)


if __name__ == "__main__":
    unittest.main()
