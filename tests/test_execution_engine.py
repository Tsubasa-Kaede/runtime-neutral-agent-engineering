import sys
import unittest
from pathlib import Path
from unittest.mock import Mock

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from execution_engine import ExecutionEngine, ExecutionStatus
from invocation_plan import InvocationPlan, StagePlan
from external_runtime import ExternalAgentRequest, InvocationResult, InvocationStatus
from structured_packets import ImplementationPacket
from runtime_status import HealthEvidence, ReasonCode, RuntimeState, RuntimeStatus
from task_budget import BudgetUsage, TaskBudget
from loop_guard import LoopGuard
from capability_registry import AgentProfile, CapabilityEvidence, CapabilityConfidence, CapabilityName
from fallback_policy import FallbackPolicy


def runtime(rid, state=RuntimeState.READY):
    return RuntimeStatus(rid, rid + ".exe", "1", state, "provider", "model", "managed", ReasonCode.NONE, HealthEvidence("v", "v", "v", "v", "v"), 1, 100)


def profile(aid, rid):
    return AgentProfile(aid, rid, "provider", "model", "coder", {CapabilityName.CODING: CapabilityEvidence(CapabilityName.CODING, .9, CapabilityConfidence.VERIFIED, "test")}, .8)


class ExecutionEngineTests(unittest.TestCase):
    def plan(self, agent_id="a"):
        return InvocationPlan("task-1", "ON", "SIMPLE", (StagePlan("coder", "coder", agent_id, ("coding",), "selected"),), (agent_id,), (), {}, ())

    def engine(self, adapters, runtimes, max_calls=2):
        return ExecutionEngine(adapters=adapters, runtimes=runtimes, budget=TaskBudget(max_calls, 2), usage=BudgetUsage(), loop_guard=LoopGuard(), fallback=FallbackPolicy([profile(k, k) for k in adapters]))

    def test_ready_agent_success_executes_one_real_adapter_boundary(self):
        adapter = Mock()
        adapter.invoke.return_value = InvocationResult(InvocationStatus.SUCCESS, output=ImplementationPacket("task-1", "coder", ("file.py",), "summary", (), (), (), ()), trace=Mock(invocation_id="inv-1"))
        engine = self.engine({"a": adapter}, {"a": runtime("a")})
        result = engine.execute(self.plan(), prompt="Return OK")
        self.assertEqual(result.status, ExecutionStatus.SUCCESS)
        adapter.invoke.assert_called_once()
        self.assertEqual(result.traces[0].invocation_id, "inv-1")

    def test_non_ready_agent_is_rejected_without_invoke(self):
        adapter = Mock()
        result = self.engine({"a": adapter}, {"a": runtime("a", RuntimeState.AUTH_REQUIRED)}).execute(self.plan(), prompt="x")
        self.assertEqual(result.status, ExecutionStatus.FAILED)
        adapter.invoke.assert_not_called()
        self.assertIn("RUNTIME_NOT_READY", result.errors)

    def test_budget_exhaustion_stops_before_invoke(self):
        adapter = Mock()
        result = self.engine({"a": adapter}, {"a": runtime("a")}, max_calls=0).execute(self.plan(), prompt="x")
        self.assertEqual(result.status, ExecutionStatus.FAILED)
        adapter.invoke.assert_not_called()
        self.assertIn("BUDGET_EXHAUSTED", result.errors)

    def test_loop_guard_rejection_stops_before_invoke(self):
        adapter = Mock()
        guard = LoopGuard()
        guard.record("task-1", "coder", "a")
        engine = self.engine({"a": adapter}, {"a": runtime("a")})
        engine.loop_guard = guard
        result = engine.execute(self.plan(), prompt="x")
        self.assertEqual(result.status, ExecutionStatus.FAILED)
        adapter.invoke.assert_not_called()
        self.assertIn("LOOP_GUARD", result.errors)

    def test_failed_call_falls_back_to_second_ready_agent_with_same_budget(self):
        first = Mock()
        first.invoke.return_value = InvocationResult(InvocationStatus.FAILED, error="failed")
        backup = Mock()
        backup.invoke.return_value = InvocationResult(InvocationStatus.SUCCESS, output=ImplementationPacket("task-1", "coder", ("file.py",), "summary", (), (), (), ()), trace=Mock(invocation_id="inv-2"))
        engine = self.engine({"a": first, "b": backup}, {"a": runtime("a"), "b": runtime("b")}, max_calls=2)
        result = engine.execute(self.plan(), prompt="x")
        self.assertEqual(result.status, ExecutionStatus.SUCCESS)
        backup.invoke.assert_called_once()
        self.assertEqual(engine.usage.total_agent_calls, 2)

    def test_trace_is_preserved_and_missing_token_values_remain_unknown(self):
        adapter = Mock()
        trace = Mock(invocation_id="inv-1", input_tokens="unknown", output_tokens="unknown")
        adapter.invoke.return_value = InvocationResult(InvocationStatus.SUCCESS, output=ImplementationPacket("task-1", "coder", ("file.py",), "summary", (), (), (), ()), trace=trace)
        result = self.engine({"a": adapter}, {"a": runtime("a")}).execute(self.plan(), prompt="x")
        self.assertEqual(result.traces[0].input_tokens, "unknown")
        self.assertEqual(result.traces[0].output_tokens, "unknown")


if __name__ == "__main__":
    unittest.main()
