import sys
import unittest
from pathlib import Path
from unittest.mock import Mock

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from capability_registry import AgentProfile, CapabilityConfidence, CapabilityEvidence, CapabilityName, CapabilityRegistry
from execution_engine import ExecutionEngine, ExecutionStatus
from external_runtime import InvocationResult, InvocationStatus
from structured_packets import ImplementationPacket
from invocation_plan import InvocationPlan, StagePlan
from loop_guard import LoopGuard
from runtime_integration import RuntimeIntegration
from runtime_status import HealthEvidence, ReasonCode, RuntimeState, RuntimeStatus
from task_budget import BudgetUsage, TaskBudget


def runtime(runtime_id, state=RuntimeState.READY):
    return RuntimeStatus(
        runtime_id=runtime_id,
        executable=runtime_id + ".exe",
        version="1",
        status=state,
        provider="provider",
        model="model",
        auth_method="managed",
        reason_code=ReasonCode.NONE if state is RuntimeState.READY else ReasonCode.AUTH_REQUIRED,
        evidence=HealthEvidence("verified", "authenticated", "verified", "verified", "passed"),
        checked_at=1,
        expires_at=100,
    )


def agent(agent_id, runtime_id, capability=CapabilityName.CODING):
    return AgentProfile(
        agent_id=agent_id,
        runtime_id=runtime_id,
        provider="provider",
        model="model",
        role="coder",
        capabilities={capability: CapabilityEvidence(capability, .9, CapabilityConfidence.VERIFIED, "integration-test")},
        historical_success_rate=.8,
    )


class RuntimeIntegrationTests(unittest.TestCase):
    def test_ready_status_flows_through_selection_plan_and_execution(self):
        adapter = Mock()
        adapter.invoke.return_value = InvocationResult(InvocationStatus.SUCCESS, ImplementationPacket("task-1", "coder", ("file.py",), "summary", (), (), (), ()), trace=Mock(invocation_id="inv-1"))
        integration = RuntimeIntegration(
            adapters={"coder-agent": adapter},
            profiles=[agent("coder-agent", "runtime-a")],
            statuses={"runtime-a": runtime("runtime-a")},
            budget=TaskBudget(max_agent_calls=1, max_iterations=1),
        )

        plan = integration.plan("task-1", "fix one function")
        result = integration.execute(plan, "Return exactly OK")

        self.assertEqual(plan.stages[0].runtime_id, "runtime-a")
        self.assertEqual(result.status, ExecutionStatus.SUCCESS)
        adapter.invoke.assert_called_once()

    def test_non_ready_status_blocks_execution_before_adapter(self):
        adapter = Mock()
        integration = RuntimeIntegration(
            adapters={"coder-agent": adapter},
            profiles=[agent("coder-agent", "runtime-a")],
            statuses={"runtime-a": runtime("runtime-a", RuntimeState.AUTH_REQUIRED)},
            budget=TaskBudget(max_agent_calls=1, max_iterations=1),
        )

        plan = integration.plan("task-1", "fix one function")
        result = integration.execute(plan, "Return exactly OK")

        self.assertEqual(result.status, ExecutionStatus.FAILED)
        adapter.invoke.assert_not_called()

    def test_status_report_is_non_secret_and_preserves_trace(self):
        adapter = Mock()
        trace = Mock(invocation_id="inv-1", runtime="runtime-a", provider="provider", model="model")
        adapter.invoke.return_value = InvocationResult(InvocationStatus.SUCCESS, ImplementationPacket("task-1", "coder", ("file.py",), "summary", (), (), (), ()), trace=trace)
        integration = RuntimeIntegration(
            adapters={"coder-agent": adapter},
            profiles=[agent("coder-agent", "runtime-a")],
            statuses={"runtime-a": runtime("runtime-a")},
            budget=TaskBudget(max_agent_calls=1, max_iterations=1),
        )

        result = integration.execute(integration.plan("task-1", "fix one function"), "Return exactly OK")

        self.assertEqual(result.traces[0].invocation_id, "inv-1")
        self.assertNotIn("token", str(result).lower())
        self.assertNotIn("secret", str(result).lower())


if __name__ == "__main__":
    unittest.main()
