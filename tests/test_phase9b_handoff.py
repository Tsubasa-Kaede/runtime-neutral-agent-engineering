import sys
import unittest
from pathlib import Path
from unittest.mock import Mock

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from capability_registry import AgentProfile, CapabilityConfidence, CapabilityEvidence, CapabilityName, CapabilityRegistry
from external_runtime import InvocationResult, InvocationStatus
from orchestrator import Orchestrator
from runtime_status import HealthEvidence, ReasonCode, RuntimeState, RuntimeStatus
from task_budget import BudgetUsage, TaskBudget
from loop_guard import LoopGuard
from structured_packets import ArchitecturePacket, ImplementationPacket, TestPacket, ReviewPacket
from execution_engine import ExecutionStatus


def runtime(rid, state=RuntimeState.READY):
    return RuntimeStatus(rid, rid+".exe", "1", state, "p", "m", "managed", ReasonCode.NONE, HealthEvidence("v", "v", "v", "v", "v"), 1, 100)


def profile(aid, rid, caps, role):
    return AgentProfile(aid, rid, "p", "m", role, {c: CapabilityEvidence(c, .9, CapabilityConfidence.VERIFIED, "test") for c in caps}, .8)


def packet_output(stage, task_id="task"):
    if stage == "architect":
        return ArchitecturePacket(task_id, "architect", ("goal",), (), ("architecture",), (), (), ("accept",), ()).__dict__
    if stage == "coder":
        return ImplementationPacket(task_id, "coder", ("file.py",), "summary", ("detail",), (), (), ("test",)).__dict__
    if stage == "test":
        return TestPacket(task_id, "tester", ("test",), ("test",), (), (), ("validation",), ()).__dict__
    return ReviewPacket(task_id, "reviewer", "PASS", (), (), (), (), ("passed",)).__dict__


class Phase9BHandoffTests(unittest.TestCase):
    def setup(self, profiles, statuses):
        budget = TaskBudget(10, 4)
        usage = BudgetUsage()
        guard = LoopGuard(max_iterations=4)
        adapters = {}
        for p in profiles:
            adapter = Mock()
            adapter.invoke.side_effect = lambda request, stage=p.role: InvocationResult(InvocationStatus.SUCCESS, packet_output(stage))
            adapters[p.agent_id] = adapter
        from orchestrator import Orchestrator
        return Orchestrator(CapabilityRegistry(profiles), statuses, budget, usage, guard), adapters, usage

    def test_complex_handoff_packets_flow_in_order(self):
        profiles = [profile("a", "ra", {CapabilityName.ARCHITECTURE}, "architect"), profile("c", "rc", {CapabilityName.CODING}, "coder"), profile("t", "rt", {CapabilityName.TESTING}, "test"), profile("r", "rr", {CapabilityName.REVIEW}, "review")]
        o, adapters, usage = self.setup(profiles, {rid: runtime(rid) for rid in ("ra", "rc", "rt", "rr")})
        result = o.execute("task", "redesign architecture across modules", adapters, "prompt")
        self.assertEqual(result.status, ExecutionStatus.SUCCESS)
        self.assertEqual(usage.total_agent_calls, 4)
        self.assertEqual([type(packet).__name__ for packet in result.packets], ["ArchitecturePacket", "ImplementationPacket", "TestPacket", "ReviewPacket"])

    def test_invalid_packet_stops_next_stage(self):
        profiles = [profile("a", "ra", {CapabilityName.ARCHITECTURE}, "architect"), profile("c", "rc", {CapabilityName.CODING}, "coder"), profile("t", "rt", {CapabilityName.TESTING}, "test")]
        o, adapters, _ = self.setup(profiles, {rid: runtime(rid) for rid in ("ra", "rc", "rt")})
        adapters["c"].invoke.side_effect = None
        adapters["c"].invoke.return_value = InvocationResult(InvocationStatus.SUCCESS, {"bad": True})
        result = o.execute("task", "redesign architecture across modules", adapters, "prompt")
        self.assertEqual(result.status, ExecutionStatus.FAILED)
        adapters["t"].invoke.assert_not_called()
        self.assertIn("PACKET_VALIDATION_FAILED", result.errors)

    def test_missing_required_handoff_blocks_stage(self):
        profiles = [profile("t", "rt", {CapabilityName.TESTING}, "test")]
        o, adapters, _ = self.setup(profiles, {"rt": runtime("rt")})
        result = o.execute("task", "redesign architecture across modules", adapters, "prompt")
        self.assertEqual(result.status, ExecutionStatus.FAILED)
        self.assertIn("MISSING_HANDOFF", result.errors)

    def test_simple_has_only_implementation_packet(self):
        profiles = [profile("c", "rc", {CapabilityName.CODING}, "coder")]
        o, adapters, _ = self.setup(profiles, {"rc": runtime("rc")})
        result = o.execute("task", "fix one function", adapters, "prompt")
        self.assertEqual([type(packet).__name__ for packet in result.packets], ["ImplementationPacket"])

    def test_packet_handoff_does_not_change_trace_or_guess_tokens(self):
        profiles = [profile("c", "rc", {CapabilityName.CODING}, "coder")]
        o, adapters, _ = self.setup(profiles, {"rc": runtime("rc")})
        trace = Mock(invocation_id="trace-1", input_tokens="unknown", output_tokens="unknown")
        adapters["c"].invoke.side_effect = None
        adapters["c"].invoke.return_value = InvocationResult(InvocationStatus.SUCCESS, packet_output("coder"), trace=trace)
        result = o.execute("task", "fix one function", adapters, "prompt")
        self.assertEqual(result.traces[0].invocation_id, "trace-1")
        self.assertEqual(result.traces[0].input_tokens, "unknown")


if __name__ == "__main__":
    unittest.main()
