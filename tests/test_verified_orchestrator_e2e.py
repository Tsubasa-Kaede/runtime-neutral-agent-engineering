"""Phase 10F E2E: verified pool -> plan -> ExecutionEngine -> fake adapters.

Offline and mock-only: every adapter is a fake returning role-appropriate
structured packets; no runtime, provider or process is ever touched.
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from candidate_validation import (
    CandidateValidationResult,
    CandidateValidationStatus,
    GateResult,
    GateVerdict,
    ValidationGate,
)
from execution_engine import ExecutionStatus
from external_runtime import InvocationResult, InvocationStatus
from loop_guard import LoopGuard
from mode_gate import Mode
from runtime_status import HealthEvidence, ReasonCode, RuntimeState, RuntimeStatus
from structured_packets import (
    ArchitecturePacket,
    ImplementationPacket,
    ReviewPacket,
    TestPacket,
)
from task_budget import BudgetUsage, TaskBudget
from verified_runtime_pool import VerifiedRuntimePool
from verified_selection_bridge import agent_id_for
from verified_orchestrator import VerifiedOrchestrator


def runtime_status(rid, state=RuntimeState.READY):
    return RuntimeStatus(rid, rid + ".exe", "1", state, "p", None, "managed",
                         ReasonCode.NONE, HealthEvidence("v", "v", "v", "v", "v"), 1, 100)


def verified_result(identity, caps, experiment="exp-1"):
    return CandidateValidationResult(
        identity=identity,
        status=CandidateValidationStatus.VERIFIED,
        gates_passed=frozenset(ValidationGate),
        gate_results=tuple(GateResult(g, GateVerdict.PASS) for g in ValidationGate),
        block_reason=None,
        failure_point=None,
        experiment_id=experiment,
        executed_at=1.0,
        validated_capabilities=tuple(sorted(caps)),
        evidence={},
    )


def stage_packet(role, task_id):
    if role == "architect":
        return ArchitecturePacket(task_id, "architect", ("goal",), (), ("arch",), (), (), ("accept",), ())
    if role == "coder":
        return ImplementationPacket(task_id, "coder", ("f.py",), "summary", (), (), (), ())
    if role == "test":
        return TestPacket(task_id, "tester", ("t",), ("t",), (), (), ("local",), ())
    return ReviewPacket(task_id, "reviewer", "PASS", (), (), ("f.py",), (), ("verified",))


class FakeAdapter:
    """Records invocations; returns role-appropriate packets or failures."""

    def __init__(self, fail_roles=()):
        self.calls = []
        self.fail_roles = set(fail_roles)

    def invoke(self, request):
        self.calls.append(request)
        trace = Mock(invocation_id=f"inv-{request.agent_id[:12]}-{request.role}",
                     input_tokens="unknown", output_tokens="unknown")
        if request.role in self.fail_roles:
            return InvocationResult(InvocationStatus.FAILED, None, "mock failure", trace)
        return InvocationResult(InvocationStatus.SUCCESS, stage_packet(request.role, request.task_id), trace=trace)


IDENTITY_A = ("runtime-a", "provider-a", "model-a", "fp-a")
IDENTITY_B = ("runtime-b", "provider-b", "model-b", "fp-b")


def build_orchestrator(specs, health_state=RuntimeState.READY, calls=10,
                       fail_roles_a=(), fail_roles_b=(), usage=None, guard=None):
    pool = VerifiedRuntimePool(clock=lambda: 1.0)
    for identity, caps in specs:
        pool.admit(verified_result(identity, caps), frozenset(), RuntimeState.READY)
    health = {rid: runtime_status(rid, health_state)
              for rid in {i[0] for i, _ in specs}}
    adapter_a = FakeAdapter(fail_roles_a)
    adapter_b = FakeAdapter(fail_roles_b)
    adapters = {}
    if any(i == IDENTITY_A for i, _ in specs):
        adapters[agent_id_for(IDENTITY_A)] = adapter_a
    if any(i == IDENTITY_B for i, _ in specs):
        adapters[agent_id_for(IDENTITY_B)] = adapter_b
    orch = VerifiedOrchestrator(
        pool=pool, current_health=health, adapters=adapters,
        budget=TaskBudget(calls, 4), usage=usage or BudgetUsage(),
        loop_guard=guard or LoopGuard(max_iterations=4),
    )
    return orch, adapter_a, adapter_b


class VerifiedOrchestratorE2ETests(unittest.TestCase):
    ALL = ("architecture", "coding", "testing", "review")

    def test_simple_execute_success_via_fake_adapter(self):
        orch, adapter_a, _ = build_orchestrator([(IDENTITY_A, self.ALL)])
        result = orch.execute("e2e-simple", "fix one function", "prompt", mode=Mode.ON)
        self.assertEqual(result.status, ExecutionStatus.SUCCESS)
        self.assertEqual(len(adapter_a.calls), 1)
        self.assertEqual(adapter_a.calls[0].agent_id, agent_id_for(IDENTITY_A))
        self.assertEqual([type(p).__name__ for p in result.packets], ["ImplementationPacket"])

    def test_complex_budget_execution_accounting(self):
        usage = BudgetUsage()
        orch, adapter_a, _ = build_orchestrator(
            [(IDENTITY_A, ("architecture", "review")), (IDENTITY_B, ("coding", "testing"))],
            calls=6, usage=usage)
        result = orch.execute("e2e-complex", "redesign architecture across modules", "p", mode=Mode.ON)
        self.assertEqual(result.status, ExecutionStatus.SUCCESS)
        self.assertEqual(usage.total_agent_calls, 4)
        self.assertEqual([type(p).__name__ for p in result.packets],
                         ["ArchitecturePacket", "ImplementationPacket", "TestPacket", "ReviewPacket"])

    def test_loop_guard_execution_accounting(self):
        guard = LoopGuard(max_iterations=4)
        orch, adapter_a, adapter_b = build_orchestrator(
            [(IDENTITY_A, ("architecture", "review")), (IDENTITY_B, ("coding", "testing"))],
            calls=6, guard=guard)
        result = orch.execute("e2e-guard", "redesign architecture across modules", "p", mode=Mode.ON)
        self.assertEqual(result.status, ExecutionStatus.SUCCESS)
        for stage, agent in (("architect", agent_id_for(IDENTITY_A)),
                             ("coder", agent_id_for(IDENTITY_B)),
                             ("test", agent_id_for(IDENTITY_B)),
                             ("review", agent_id_for(IDENTITY_A))):
            self.assertNotEqual(guard.check("e2e-guard", stage, agent), "ALLOW")

    def test_health_flip_blocks_execution(self):
        orch, adapter_a, _ = build_orchestrator([(IDENTITY_A, self.ALL)])
        plan = orch.plan("e2e-health", "fix one function", mode=Mode.ON)
        self.assertTrue(plan.stages)
        orch.current_health["runtime-a"] = runtime_status("runtime-a", RuntimeState.AUTH_REQUIRED)
        result = orch.execute("e2e-health", "fix one function", "prompt", mode=Mode.ON)
        # execute() re-plans: the non-READY candidate is excluded upstream of
        # the engine, so the honest outcome is NO_CAPABLE_AGENT, zero calls.
        self.assertEqual(result.status, ExecutionStatus.FAILED)
        self.assertIn("NO_CAPABLE_AGENT", result.errors)
        self.assertEqual(adapter_a.calls, [])

    def test_no_fallback_and_honest_failure(self):
        usage = BudgetUsage()
        orch, adapter_a, _ = build_orchestrator(
            [(IDENTITY_A, self.ALL)], calls=4, usage=usage,
            fail_roles_a=("coder",))
        result = orch.execute("e2e-fallback", "fix one function", "prompt", mode=Mode.ON)
        self.assertEqual(result.status, ExecutionStatus.FAILED)
        self.assertIn("NO_FALLBACK_AGENT", result.errors)
        # no fallback invocations: exactly one (failed) call consumed budget
        self.assertEqual(len(adapter_a.calls), 1)
        self.assertEqual(usage.total_agent_calls, 1)

    def test_runtime_id_keyed_adapter_is_never_used(self):
        # an adapter wrongly keyed by runtime_id must stay untouched
        pool = VerifiedRuntimePool(clock=lambda: 1.0)
        pool.admit(verified_result(IDENTITY_A, self.ALL), frozenset(), RuntimeState.READY)
        health = {"runtime-a": runtime_status("runtime-a")}
        wrong_adapter = FakeAdapter()
        right_adapter = FakeAdapter()
        adapters = {"runtime-a": wrong_adapter,
                    agent_id_for(IDENTITY_A): right_adapter}
        orch = VerifiedOrchestrator(
            pool=pool, current_health=health, adapters=adapters,
            budget=TaskBudget(4, 4), usage=BudgetUsage(),
            loop_guard=LoopGuard(max_iterations=4))
        result = orch.execute("e2e-agentid", "fix one function", "prompt", mode=Mode.ON)
        self.assertEqual(result.status, ExecutionStatus.SUCCESS)
        self.assertEqual(wrong_adapter.calls, [])
        self.assertEqual(len(right_adapter.calls), 1)


if __name__ == "__main__":
    unittest.main()
