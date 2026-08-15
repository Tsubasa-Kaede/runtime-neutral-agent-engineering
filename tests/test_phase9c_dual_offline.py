"""Phase 9C-Dual-Offline: full offline dual-agent loop on two mock READY runtimes.

Chain under test:
  decide() -> to_selection_result() -> Orchestrator.plan(dual_selection=...)
  -> ExecutionEngine -> Architect(A) -> Coder(B) -> Tester -> Reviewer
with structured packets crossing runtime boundaries. No real runtime, model,
or provider is ever invoked.
"""
import os
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
    DualAgentDecision,
    DualAgentMode,
    DualAgentSelection,
    DualAgentSelectionResult,
)
from external_runtime import InvocationResult, InvocationStatus
from orchestrator import Orchestrator
from runtime_status import HealthEvidence, ReasonCode, RuntimeState, RuntimeStatus
from structured_packets import (
    ArchitecturePacket,
    ImplementationPacket,
    ReviewPacket,
    TestPacket,
    deserialize_packet,
    serialize_packet,
)
from task_budget import BudgetUsage, TaskBudget
from loop_guard import LoopGuard
from execution_engine import ExecutionStatus


def runtime(rid, state=RuntimeState.READY):
    return RuntimeStatus(rid, rid + ".exe", "1", state, "p", "m", "managed", ReasonCode.NONE, HealthEvidence("v", "v", "v", "v", "v"), 1, 100)


def profile(aid, rid, spec):
    caps = {cap: CapabilityEvidence(cap, score, conf, "dual-offline") for cap, (score, conf) in spec.items()}
    return AgentProfile(aid, rid, "p", "m", None, caps, None)


def complementary_pair(ra="runtime-a", rb="runtime-b"):
    return [
        profile("agent-a", ra, {
            CapabilityName.ARCHITECTURE: (0.95, CapabilityConfidence.VERIFIED),
            CapabilityName.CODING: (0.60, CapabilityConfidence.DECLARED),
            CapabilityName.TESTING: (0.60, CapabilityConfidence.DECLARED),
            CapabilityName.REVIEW: (0.95, CapabilityConfidence.VERIFIED),
        }),
        profile("agent-b", rb, {
            CapabilityName.ARCHITECTURE: (0.60, CapabilityConfidence.DECLARED),
            CapabilityName.CODING: (0.95, CapabilityConfidence.VERIFIED),
            CapabilityName.TESTING: (0.95, CapabilityConfidence.VERIFIED),
            CapabilityName.REVIEW: (0.60, CapabilityConfidence.DECLARED),
        }),
    ]


def stage_packet(role, task_id):
    if role == "architect":
        return ArchitecturePacket(task_id, "architect", ("goal",), (), ("arch",), (), (), ("accept",), ())
    if role == "coder":
        return ImplementationPacket(task_id, "coder", ("f.py",), "summary", (), (), (), ("python -m unittest",))
    if role == "test":
        return TestPacket(task_id, "tester", ("t",), ("t",), (), (), ("local run",), ())
    return ReviewPacket(task_id, "reviewer", "PASS", (), (), ("f.py",), (), ("verified",))


class DualOfflineLoop:
    """Mock dual-runtime environment: per-agent adapters that consume their
    structured handoff input and emit validated packets. Pure Python mocks."""

    def __init__(self, profiles, statuses, fail_agents=()):
        self.profiles = profiles
        self.statuses = statuses
        self.requests = []
        self.adapters = {}
        for item in profiles:
            adapter = Mock()
            if item.agent_id in fail_agents:
                adapter.invoke.side_effect = lambda request: InvocationResult(InvocationStatus.FAILED, None, "mock runtime failure")
            else:
                adapter.invoke.side_effect = lambda request: InvocationResult(
                    InvocationStatus.SUCCESS,
                    stage_packet(request.role, request.task_id),
                    trace=Mock(invocation_id=f"inv-{request.agent_id}-{request.role}",
                               input_tokens="unknown", output_tokens="unknown"),
                )
            self.adapters[item.agent_id] = adapter

    def orchestrator(self, budget, usage, guard):
        return Orchestrator(CapabilityRegistry(self.profiles), self.statuses, budget, usage, guard)

    def run_complex(self, task_id="dual-task", calls=10, fail_agents=()):
        budget = TaskBudget(calls, 4)
        usage = BudgetUsage()
        guard = LoopGuard(max_iterations=4)
        selector = DualAgentSelection()
        decision = selector.decide(self.profiles, self.statuses, "COMPLEX", budget, usage)
        selection = selector.to_selection_result(decision)
        orch = self.orchestrator(budget, usage, guard)
        plan = orch.plan(task_id, "redesign architecture across modules", dual_selection=selection)
        result = orch.execute(task_id, "redesign architecture across modules", self.adapters, "prompt", dual_selection=selection)
        return decision, selection, plan, result, usage, guard


class Phase9CDualOfflineTests(unittest.TestCase):
    def setUp(self):
        auth_paths = [Path.home() / ".codex" / "auth.json", Path.home() / ".codex" / "config.toml"]
        self.auth_before = {p: (p.stat().st_mtime_ns, p.stat().st_size) for p in auth_paths if p.exists()}

    def statuses_for(self, profiles, overrides=None):
        statuses = {p.runtime_id: runtime(p.runtime_id) for p in profiles}
        if overrides:
            statuses.update(overrides)
        return statuses

    def test_full_dual_loop_on_two_ready_runtimes(self):
        profiles = complementary_pair()
        loop = DualOfflineLoop(profiles, self.statuses_for(profiles))
        decision, selection, plan, result, usage, guard = loop.run_complex()

        self.assertTrue(decision.use_dual_agent)
        self.assertEqual(decision.architect_agent_id, "agent-a")
        self.assertEqual(decision.coder_agent_id, "agent-b")
        self.assertEqual(plan.stages[0].agent_id, "agent-a")
        self.assertEqual(plan.stages[0].runtime_id, "runtime-a")
        self.assertEqual(plan.stages[1].agent_id, "agent-b")
        self.assertEqual(plan.stages[1].runtime_id, "runtime-b")

        self.assertEqual(result.status, ExecutionStatus.SUCCESS)
        self.assertEqual([type(p).__name__ for p in result.packets],
                         ["ArchitecturePacket", "ImplementationPacket", "TestPacket", "ReviewPacket"])
        a_roles = [call.args[0].role for call in loop.adapters["agent-a"].invoke.call_args_list]
        b_roles = [call.args[0].role for call in loop.adapters["agent-b"].invoke.call_args_list]
        self.assertEqual(a_roles, ["architect", "review"])
        self.assertEqual(b_roles, ["coder", "test"])
        self.assertEqual(usage.total_agent_calls, 4)

    def test_cross_runtime_structured_handoff(self):
        profiles = complementary_pair()
        loop = DualOfflineLoop(profiles, self.statuses_for(profiles))
        decision, selection, plan, result, usage, guard = loop.run_complex()

        by_role = {}
        for call in list(loop.adapters["agent-a"].invoke.call_args_list) + list(loop.adapters["agent-b"].invoke.call_args_list):
            request = call.args[0]
            by_role.setdefault(request.role, []).append(request)

        coder_request = by_role["coder"][0]
        self.assertEqual(coder_request.agent_id, "agent-b")
        self.assertIsInstance(coder_request.handoff_packets[0], ArchitecturePacket)
        self.assertEqual(coder_request.handoff_packets[0], result.packets[0])

        tester_request = by_role["test"][0]
        self.assertEqual(tester_request.agent_id, "agent-b")
        self.assertEqual(tester_request.handoff_packets[0], result.packets[1])

        reviewer_request = by_role["review"][0]
        self.assertEqual(reviewer_request.agent_id, "agent-a")
        self.assertEqual(reviewer_request.handoff_packets, (result.packets[0], result.packets[1], result.packets[2]))

    def test_packet_identity_and_roundtrip(self):
        profiles = complementary_pair()
        loop = DualOfflineLoop(profiles, self.statuses_for(profiles))
        decision, selection, plan, result, *_ = loop.run_complex(task_id="identity-task")

        self.assertEqual({p.task_id for p in result.packets}, {"identity-task"})
        self.assertEqual([p.role for p in result.packets], ["architect", "coder", "tester", "reviewer"])
        for packet in result.packets:
            self.assertEqual(type(packet).from_dict(vars(packet)), packet)
        self.assertEqual(deserialize_packet(serialize_packet(result.packets[3])), result.packets[3])

    def test_loop_guard_blocks_stage_reentry(self):
        profiles = complementary_pair()
        loop = DualOfflineLoop(profiles, self.statuses_for(profiles))
        decision, selection, plan, result, usage, guard = loop.run_complex()
        for stage in ("architect", "coder", "test", "review"):
            self.assertNotEqual(guard.check("dual-task", stage, "agent-a" if stage in ("architect", "review") else "agent-b"), "ALLOW")

    def test_non_ready_runtime_excluded_from_dual(self):
        profiles = complementary_pair()
        statuses = self.statuses_for(profiles, {"runtime-b": runtime("runtime-b", RuntimeState.AUTH_REQUIRED)})
        loop = DualOfflineLoop(profiles, statuses)
        budget = TaskBudget(10, 4)
        usage = BudgetUsage()
        decision = DualAgentSelection().decide(profiles, statuses, "COMPLEX", budget, usage)
        self.assertFalse(decision.use_dual_agent)
        self.assertEqual(decision.coder_agent_id, "agent-a")

    def test_cross_runtime_fallback_is_bounded(self):
        profiles = complementary_pair()
        loop = DualOfflineLoop(profiles, self.statuses_for(profiles), fail_agents=("agent-b",))
        decision, selection, plan, result, usage, guard = loop.run_complex(calls=10)

        self.assertEqual(result.status, ExecutionStatus.SUCCESS)
        # agent-b fails exactly its two primary stage calls; agent-a absorbs
        # coder/test via fallback; the loop terminates instead of retrying b.
        self.assertEqual(loop.adapters["agent-b"].invoke.call_count, 2)
        self.assertEqual(sorted(call.args[0].role for call in loop.adapters["agent-a"].invoke.call_args_list),
                         ["architect", "coder", "review", "test"])
        self.assertEqual(usage.total_agent_calls, 6)

    def test_selection_never_consumes_budget(self):
        profiles = complementary_pair()
        statuses = self.statuses_for(profiles)
        budget = TaskBudget(10, 4)
        usage = BudgetUsage()
        selector = DualAgentSelection()
        selector.decide(profiles, statuses, "COMPLEX", budget, usage)
        selector.to_selection_result(selector.decide(profiles, statuses, "COMPLEX", budget, usage))
        self.assertEqual(usage.total_agent_calls, 0)
        self.assertEqual(usage.iterations_used, 0)

    def test_simple_stays_single_agent(self):
        profiles = complementary_pair()
        statuses = self.statuses_for(profiles)
        budget = TaskBudget(10, 4)
        usage = BudgetUsage()
        selector = DualAgentSelection()
        decision = selector.decide(profiles, statuses, "SIMPLE", budget, usage)
        selection = selector.to_selection_result(decision)
        self.assertFalse(decision.use_dual_agent)
        loop = DualOfflineLoop(profiles, statuses)
        orch = loop.orchestrator(budget, usage, LoopGuard(max_iterations=4))
        result = orch.execute("simple-task", "fix one function", loop.adapters, "prompt", dual_selection=selection)
        self.assertEqual(result.status, ExecutionStatus.SUCCESS)
        invoked = [call.args[0].agent_id for adapter in loop.adapters.values() for call in adapter.invoke.call_args_list]
        self.assertEqual(invoked, ["agent-b"])

    def test_runtime_id_rename_does_not_change_selection(self):
        profiles = complementary_pair(ra="node-alpha", rb="node-beta")
        statuses = self.statuses_for(profiles)
        budget = TaskBudget(10, 4)
        usage = BudgetUsage()
        selector = DualAgentSelection()
        decision = selector.decide(profiles, statuses, "COMPLEX", budget, usage)
        self.assertTrue(decision.use_dual_agent)
        self.assertEqual(decision.architect_agent_id, "agent-a")
        self.assertEqual(decision.coder_agent_id, "agent-b")

    def test_no_real_invocation_and_no_secret_surface(self):
        profiles = complementary_pair()
        loop = DualOfflineLoop(profiles, self.statuses_for(profiles))
        decision, selection, plan, result, usage, guard = loop.run_complex()
        for adapter in loop.adapters.values():
            for call in adapter.invoke.call_args_list:
                request = call.args[0]
                self.assertFalse(hasattr(request, "stdout"))
                self.assertFalse(hasattr(request, "stderr"))
        surface = repr(result.packets).lower()
        for marker in ("token", "secret", "api_key", "authorization"):
            self.assertNotIn(marker, surface)
        self.assertNotIn("invoke", dir(decision))

    def test_auth_and_config_untouched(self):
        profiles = complementary_pair()
        loop = DualOfflineLoop(profiles, self.statuses_for(profiles))
        loop.run_complex()
        auth_paths = [Path.home() / ".codex" / "auth.json", Path.home() / ".codex" / "config.toml"]
        auth_after = {p: (p.stat().st_mtime_ns, p.stat().st_size) for p in auth_paths if p.exists()}
        self.assertEqual(self.auth_before, auth_after)

    def test_structured_handoff_not_degraded_to_strings(self):
        profiles = complementary_pair()
        loop = DualOfflineLoop(profiles, self.statuses_for(profiles))
        decision, selection, plan, result, *_ = loop.run_complex()
        for packet in result.packets:
            self.assertIsNotNone(type(packet).REQUIRED_FIELDS)
        requests = [call.args[0] for adapter in loop.adapters.values() for call in adapter.invoke.call_args_list]
        for request in requests:
            for item in request.handoff_packets:
                self.assertIn(type(item).__name__, ("ArchitecturePacket", "ImplementationPacket", "TestPacket"))


if __name__ == "__main__":
    unittest.main()
