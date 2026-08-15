import sys
import unittest
from pathlib import Path
from unittest.mock import Mock

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from capability_registry import AgentProfile, CapabilityConfidence, CapabilityEvidence, CapabilityName
from external_runtime import InvocationResult, InvocationStatus
from orchestrator import Orchestrator
from runtime_status import HealthEvidence, ReasonCode, RuntimeState, RuntimeStatus
from task_budget import BudgetUsage, TaskBudget
from loop_guard import LoopGuard
from fallback_policy import FallbackPolicy
from execution_engine import ExecutionStatus


def runtime(rid, state=RuntimeState.READY):
    return RuntimeStatus(rid, rid+".exe", "1", state, "p", "m", "managed", ReasonCode.NONE, HealthEvidence("v", "v", "v", "v", "v"), 1, 100)


def profile(aid, rid, caps, role):
    return AgentProfile(aid, rid, "p", "m", role, {c: CapabilityEvidence(c, .9, CapabilityConfidence.VERIFIED, "test") for c in caps}, .8)


def stage_packet(stage="coder", task_id="task"):
    from structured_packets import ArchitecturePacket, ImplementationPacket, TestPacket, ReviewPacket
    if stage == "architect":
        return ArchitecturePacket(task_id, "architect", ("goal",), (), ("architecture",), (), (), ("accept",), ())
    if stage == "coder":
        return ImplementationPacket(task_id, "coder", ("file.py",), "summary", ("detail",), (), (), ("test",))
    if stage == "test":
        return TestPacket(task_id, "tester", ("test",), ("test",), (), (), ("validation",), ())
    return ReviewPacket(task_id, "reviewer", "PASS", (), (), (), (), ("passed",))


class Phase9AOrchestrationTests(unittest.TestCase):
    def setup(self, profiles, statuses, calls=10):
        budget = TaskBudget(calls, 4)
        usage = BudgetUsage()
        guard = LoopGuard(max_iterations=4)
        adapters = {p.agent_id: Mock() for p in profiles}
        stages = {"a": "architect", "c": "coder", "t": "test", "v": "review", "r": "review", "b": "coder"}
        for agent_id, adapter in adapters.items():
            stage = stages.get(agent_id, "coder")
            adapter.invoke.return_value = InvocationResult(
                InvocationStatus.SUCCESS,
                stage_packet(stage),
                trace=Mock(input_tokens="unknown", output_tokens="unknown"),
            )
        from capability_registry import CapabilityRegistry
        orchestrator = Orchestrator(CapabilityRegistry(profiles), statuses, budget, usage, guard)
        return orchestrator, adapters, budget, usage, guard

    def test_simple_coder_executes(self):
        orchestrator, adapters, budget, usage, guard = self.setup([profile("c", "r", {CapabilityName.CODING}, "coder")], {"r": runtime("r")})
        result = orchestrator.execute("task", "fix one function", adapters, "Return OK", mode="AUTO")
        self.assertEqual(result.status, ExecutionStatus.SUCCESS)
        adapters["c"].invoke.assert_called_once()

    def test_medium_plans_coder_then_test(self):
        profiles = [profile("c", "rc", {CapabilityName.CODING}, "coder"), profile("t", "rt", {CapabilityName.TESTING}, "tester")]
        orchestrator, adapters, *_ = self.setup(profiles, {"rc": runtime("rc"), "rt": runtime("rt")})
        result = orchestrator.execute("task", "change two related files and add tests", adapters, "Return OK", mode="AUTO")
        self.assertEqual(result.status, ExecutionStatus.SUCCESS)
        self.assertEqual(adapters["c"].invoke.call_count, 1)
        self.assertEqual(adapters["t"].invoke.call_count, 1)

    def test_complex_plans_all_four_stages(self):
        profiles = [profile("a", "ra", {CapabilityName.ARCHITECTURE}, "architect"), profile("c", "rc", {CapabilityName.CODING}, "coder"), profile("t", "rt", {CapabilityName.TESTING}, "tester"), profile("v", "rv", {CapabilityName.REVIEW}, "reviewer")]
        orchestrator, adapters, *_ = self.setup(profiles, {x: runtime(x) for x in ("ra", "rc", "rt", "rv")})
        result = orchestrator.execute("task", "redesign architecture across modules", adapters, "Return OK", mode="AUTO")
        self.assertEqual(result.status, ExecutionStatus.SUCCESS)
        self.assertEqual(sum(a.invoke.call_count for a in adapters.values()), 4)

    def test_non_ready_runtime_does_not_invoke(self):
        orchestrator, adapters, *_ = self.setup([profile("c", "r", {CapabilityName.CODING}, "coder")], {"r": runtime("r", RuntimeState.AUTH_REQUIRED)})
        result = orchestrator.execute("task", "fix one function", adapters, "Return OK", mode="AUTO")
        self.assertEqual(result.status, ExecutionStatus.FAILED)
        adapters["c"].invoke.assert_not_called()

    def test_fallback_shares_budget_and_preserves_unknown_tokens(self):
        profiles = [profile("a", "ra", {CapabilityName.CODING}, "coder"), profile("b", "rb", {CapabilityName.CODING}, "coder")]
        orchestrator, adapters, budget, usage, guard = self.setup(profiles, {"ra": runtime("ra"), "rb": runtime("rb")}, calls=2)
        adapters["a"].invoke.return_value = InvocationResult(InvocationStatus.FAILED, error="failed")
        result = orchestrator.execute("task", "fix one function", adapters, "Return OK", mode="AUTO")
        self.assertEqual(result.status, ExecutionStatus.SUCCESS)
        self.assertEqual(usage.total_agent_calls, 2)
        self.assertEqual(result.traces[-1].input_tokens, "unknown")


class CapabilityRegistryShim:
    def __init__(self, profiles):
        from capability_registry import CapabilityRegistry
        self._registry = CapabilityRegistry(profiles)
    def select(self, *args, **kwargs):
        return self._registry.select(*args, **kwargs)


if __name__ == "__main__":
    unittest.main()
