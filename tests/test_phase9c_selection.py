import os
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
    CapabilityRegistry,
)
from dual_agent_selection import (
    DecisionReason,
    DualAgentDecision,
    DualAgentMode,
    DualAgentSelection,
)
from external_runtime import InvocationResult, InvocationStatus
from orchestrator import Orchestrator
from runtime_status import HealthEvidence, ReasonCode, RuntimeState, RuntimeStatus
from structured_packets import ArchitecturePacket, ImplementationPacket, ReviewPacket, TestPacket
from task_budget import BudgetUsage, TaskBudget
from loop_guard import LoopGuard
from unittest.mock import Mock


def runtime(rid, state=RuntimeState.READY):
    return RuntimeStatus(rid, rid + ".exe", "1", state, "p", "m", "managed", ReasonCode.NONE, HealthEvidence("v", "v", "v", "v", "v"), 1, 100)


def profile(aid, rid, arch=None, coding=None, testing=None, review=None, success=None):
    def evidence(cap, spec):
        if spec is None:
            return None
        score, confidence = spec
        return CapabilityEvidence(cap, score, confidence, "test")

    caps = {}
    for cap, spec in (
        (CapabilityName.ARCHITECTURE, arch),
        (CapabilityName.CODING, coding),
        (CapabilityName.TESTING, testing),
        (CapabilityName.REVIEW, review),
    ):
        item = evidence(cap, spec)
        if item is not None:
            caps[cap] = item
    return AgentProfile(aid, rid, "p", "m", None, caps, success)


def ev(score, confidence=CapabilityConfidence.VERIFIED):
    return (score, confidence)


def unknown():
    return (None, CapabilityConfidence.UNKNOWN)


def complementary_profiles():
    return [
        profile("agent-a", "ra", arch=ev(0.95), coding=ev(0.60, CapabilityConfidence.DECLARED)),
        profile("agent-b", "rb", arch=ev(0.60, CapabilityConfidence.DECLARED), coding=ev(0.95)),
    ]


def identical_profiles():
    same = {"arch": ev(0.90), "coding": ev(0.90), "testing": ev(0.90), "review": ev(0.90)}
    return [
        profile("a1", "ra", **same),
        profile("b2", "rb", **same),
    ]


class Phase9CSelectionTests(unittest.TestCase):
    def decide(self, profiles, statuses, complexity, calls=10, usage=None):
        usage = usage or BudgetUsage()
        return DualAgentSelection().decide(
            profiles=profiles,
            runtimes=statuses,
            complexity=complexity,
            budget=TaskBudget(calls, 4),
            usage=usage,
            mode="AUTO",
        )

    def ready_statuses(self, profiles, overrides=None):
        statuses = {p.runtime_id: runtime(p.runtime_id) for p in profiles}
        if overrides:
            statuses.update(overrides)
        return statuses

    # 1. SIMPLE 不启用 Dual-Agent
    def test_simple_never_uses_dual_agent(self):
        result = self.decide(complementary_profiles(), self.ready_statuses(complementary_profiles()), "SIMPLE")
        self.assertFalse(result.use_dual_agent)
        self.assertEqual(result.reason, DecisionReason.SIMPLE_TASK)

    # 2. MEDIUM 默认 Single-Agent（能力无差异时）
    def test_medium_defaults_single_without_specialization(self):
        result = self.decide(identical_profiles(), self.ready_statuses(identical_profiles()), "MEDIUM")
        self.assertFalse(result.use_dual_agent)

    # 3. COMPLEX 可以启用 Dual-Agent
    def test_complex_can_use_dual_agent(self):
        profiles = complementary_profiles() + [profile("agent-c", "rc", testing=ev(0.90), review=ev(0.90))]
        result = self.decide(profiles, self.ready_statuses(profiles), "COMPLEX")
        self.assertTrue(result.use_dual_agent)

    # 4. 互补能力：Architect != Coder
    def test_complementary_capabilities_split_roles(self):
        result = self.decide(complementary_profiles(), self.ready_statuses(complementary_profiles()), "COMPLEX")
        self.assertEqual(result.architect_agent_id, "agent-a")
        self.assertEqual(result.coder_agent_id, "agent-b")
        self.assertTrue(result.use_dual_agent)

    # 5. 只有一个 READY Agent：Single-Agent Multi-Role
    def test_single_ready_agent_multi_role(self):
        profiles = [profile("agent-a", "ra", arch=ev(0.9), coding=ev(0.9), testing=ev(0.9), review=ev(0.9))]
        result = self.decide(profiles, self.ready_statuses(profiles), "COMPLEX")
        self.assertFalse(result.use_dual_agent)
        self.assertEqual(result.architect_agent_id, "agent-a")
        self.assertEqual(result.coder_agent_id, "agent-a")
        self.assertNotEqual(result.reason, DecisionReason.NO_CAPABLE_AGENT)

    # 6-8. 非 READY 状态不得被选择
    def test_auth_required_unavailable_error_never_selected(self):
        for state in (RuntimeState.AUTH_REQUIRED, RuntimeState.UNAVAILABLE, RuntimeState.ERROR):
            with self.subTest(state=state):
                profiles = complementary_profiles()
                statuses = self.ready_statuses(profiles, {"rb": runtime("rb", state)})
                result = self.decide(profiles, statuses, "COMPLEX")
                self.assertFalse(result.use_dual_agent)
                self.assertEqual(result.coder_agent_id, "agent-a")
                self.assertNotIn("agent-b", result.architect_fallback_agents + result.coder_fallback_agents)

    # 9. UNKNOWN Capability 不通过硬门槛
    def test_unknown_capability_rejected(self):
        profiles = [
            profile("agent-a", "ra", arch=ev(0.95), coding=ev(0.60, CapabilityConfidence.DECLARED)),
            profile("agent-b", "rb", coding=unknown()),
        ]
        result = self.decide(profiles, self.ready_statuses(profiles), "COMPLEX")
        self.assertEqual(result.coder_agent_id, "agent-a")
        self.assertNotIn("agent-b", result.coder_fallback_agents)

    # 10. architecture 最优 → Architect
    def test_best_architect_selected(self):
        result = self.decide(complementary_profiles(), self.ready_statuses(complementary_profiles()), "COMPLEX")
        self.assertEqual(result.architect_agent_id, "agent-a")
        self.assertEqual(result.architect_runtime_id, "ra")

    # 11. coding 最优 → Coder
    def test_best_coder_selected(self):
        result = self.decide(complementary_profiles(), self.ready_statuses(complementary_profiles()), "COMPLEX")
        self.assertEqual(result.coder_agent_id, "agent-b")
        self.assertEqual(result.coder_runtime_id, "rb")

    # 12. deterministic
    def test_same_input_same_output(self):
        profiles = complementary_profiles()
        statuses = self.ready_statuses(profiles)
        first = self.decide(profiles, statuses, "COMPLEX")
        second = self.decide(profiles, statuses, "COMPLEX")
        self.assertEqual(first, second)

    # 13. agent_id tie-break
    def test_agent_id_tie_break(self):
        profiles = identical_profiles()
        result = self.decide(profiles, self.ready_statuses(profiles), "COMPLEX")
        self.assertEqual(result.architect_agent_id, "a1")
        self.assertEqual(result.coder_agent_id, "a1")

    # 14-16. Fallback candidates
    def test_fallback_candidates_ready_only_and_excluding_primary(self):
        profiles = complementary_profiles() + [profile("agent-c", "rc", coding=ev(0.80))]
        result = self.decide(profiles, self.ready_statuses(profiles), "COMPLEX")
        self.assertNotIn(result.architect_agent_id, result.architect_fallback_agents)
        self.assertNotIn(result.coder_agent_id, result.coder_fallback_agents)
        self.assertEqual(result.architect_fallback_agents, ("agent-b",))  # a=主, b 有 architecture DECLARED 0.60, c 无 architecture
        self.assertEqual(result.coder_fallback_agents, ("agent-c", "agent-a"))  # c=0.80 > a=0.60, 按分数降序

    def test_fallback_excludes_non_ready(self):
        profiles = complementary_profiles()
        statuses = self.ready_statuses(profiles, {"ra": runtime("ra", RuntimeState.UNAVAILABLE)})
        result = self.decide(profiles, statuses, "COMPLEX")
        self.assertNotIn("agent-a", result.coder_fallback_agents)
        self.assertNotIn("agent-a", result.architect_fallback_agents)

    # 17. 不增加 total_agent_calls
    def test_decision_does_not_consume_budget(self):
        usage = BudgetUsage()
        self.decide(complementary_profiles(), self.ready_statuses(complementary_profiles()), "COMPLEX", usage=usage)
        self.assertEqual(usage.total_agent_calls, 0)
        self.assertEqual(usage.iterations_used, 0)

    # 18. Budget 不足 → BUDGET_INSUFFICIENT
    def test_budget_insufficient_rejects_dual(self):
        profiles = complementary_profiles() + [profile("agent-c", "rc", testing=ev(0.9), review=ev(0.9))]
        result = self.decide(profiles, self.ready_statuses(profiles), "COMPLEX", calls=3)
        self.assertFalse(result.use_dual_agent)
        self.assertEqual(result.reason, DecisionReason.BUDGET_INSUFFICIENT)

    # 19-20. 不产生 Invocation / InvocationTrace
    def test_no_invocation_or_trace_surface(self):
        result = self.decide(complementary_profiles(), self.ready_statuses(complementary_profiles()), "COMPLEX")
        self.assertFalse(hasattr(result, "trace"))
        self.assertFalse(hasattr(result, "invocation_id"))
        self.assertNotIn("invoke", dir(DualAgentSelection))

    # 21-22. 不修改 auth.json / config.toml
    def test_no_auth_or_config_mutation(self):
        paths = [Path.home() / ".codex" / "auth.json", Path.home() / ".codex" / "config.toml"]
        before = {p: (p.stat().st_mtime_ns, p.stat().st_size) for p in paths if p.exists()}
        self.decide(complementary_profiles(), self.ready_statuses(complementary_profiles()), "COMPLEX")
        after = {p: (p.stat().st_mtime_ns, p.stat().st_size) for p in paths if p.exists()}
        self.assertEqual(before, after)

    # 23. 不包含 Secret
    def test_result_contains_no_secrets(self):
        result = self.decide(complementary_profiles(), self.ready_statuses(complementary_profiles()), "COMPLEX")
        text = repr(result).lower()
        for marker in ("token", "secret", "api_key", "authorization"):
            self.assertNotIn(marker, text)

    # 24. Runtime 名称变化不影响选择
    def test_runtime_rename_does_not_change_selection(self):
        renamed = [
            profile("agent-a", "runtime-zeta", arch=ev(0.95), coding=ev(0.60, CapabilityConfidence.DECLARED)),
            profile("agent-b", "runtime-yoda", arch=ev(0.60, CapabilityConfidence.DECLARED), coding=ev(0.95)),
        ]
        result = self.decide(renamed, self.ready_statuses(renamed), "COMPLEX")
        self.assertTrue(result.use_dual_agent)
        self.assertEqual(result.architect_agent_id, "agent-a")
        self.assertEqual(result.coder_agent_id, "agent-b")

    # 25. 同一 Agent 双角色
    def test_same_agent_both_roles_allowed(self):
        profiles = [profile("agent-a", "ra", arch=ev(0.9), coding=ev(0.9), testing=ev(0.9), review=ev(0.9))]
        result = self.decide(profiles, self.ready_statuses(profiles), "MEDIUM")
        self.assertEqual(result.architect_agent_id, result.coder_agent_id)

    # 26. 不同 Agent 双角色（MEDIUM 有明显差异时允许 dual）
    def test_medium_allows_dual_with_clear_specialization(self):
        profiles = complementary_profiles() + [profile("agent-c", "rc", testing=ev(0.9))]
        result = self.decide(profiles, self.ready_statuses(profiles), "MEDIUM")
        self.assertTrue(result.use_dual_agent)

    # 27. SIMPLE / MEDIUM / COMPLEX 阶段规则保持
    def test_phase9a_stage_rules_unchanged(self):
        profiles = complementary_profiles() + [profile("agent-c", "rc", testing=ev(0.9), review=ev(0.9))]
        orchestrator = Orchestrator(
            CapabilityRegistry(profiles),
            self.ready_statuses(profiles),
            TaskBudget(10, 4),
            BudgetUsage(),
            LoopGuard(max_iterations=4),
        )
        self.assertEqual([s.stage for s in orchestrator.plan("t", "fix one function").stages], ["coder"])
        self.assertEqual([s.stage for s in orchestrator.plan("t", "change two related files and add tests").stages], ["coder", "test"])
        self.assertEqual([s.stage for s in orchestrator.plan("t", "redesign architecture across modules").stages],
                         ["architect", "coder", "test", "review"])

    # 28. Phase 9B Structured Handoff 保持
    def test_structured_handoff_intact_with_dual_selection(self):
        profiles = complementary_profiles() + [profile("agent-c", "rc", testing=ev(0.9), review=ev(0.9))]
        adapters = {}
        role_packet = {
            "architect": ArchitecturePacket("task", "architect", ("goal",), (), ("arch",), (), (), ("accept",), ()),
            "coder": ImplementationPacket("task", "coder", ("f.py",), "summary", (), (), (), ()),
            "test": TestPacket("task", "tester", ("t",), ("t",), (), (), ("v",), ()),
            "review": ReviewPacket("task", "reviewer", "PASS", (), (), (), (), ("passed",)),
        }
        for item in profiles:
            adapter = Mock()
            adapter.invoke.side_effect = lambda request: InvocationResult(
                InvocationStatus.SUCCESS, role_packet[request.role], trace=Mock(input_tokens="unknown", output_tokens="unknown"))
            adapters[item.agent_id] = adapter
        orchestrator = Orchestrator(
            CapabilityRegistry(profiles),
            self.ready_statuses(profiles),
            TaskBudget(10, 4),
            BudgetUsage(),
            LoopGuard(max_iterations=4),
        )
        decision = self.decide(profiles, self.ready_statuses(profiles), "COMPLEX")
        plan = orchestrator.plan("task", "redesign architecture across modules", dual_selection=self._as_selection_result(decision, profiles))
        result = orchestrator.execute("task", "redesign architecture across modules", adapters, "prompt")
        self.assertEqual(result.status.value, "SUCCESS")
        self.assertEqual(plan.stages[0].agent_id, "agent-a")
        self.assertEqual([type(p).__name__ for p in result.packets],
                         ["ArchitecturePacket", "ImplementationPacket", "TestPacket", "ReviewPacket"])

    @staticmethod
    def _as_selection_result(decision, profiles):
        from dual_agent_selection import DualAgentSelectionResult
        if decision.architect_agent_id == decision.coder_agent_id:
            mode, reason = DualAgentMode.SINGLE_AGENT, decision.reason
            assignments = {"architect": decision.architect_agent_id, "coder": decision.coder_agent_id,
                           "test": decision.coder_agent_id, "review": decision.architect_agent_id}
        else:
            mode, reason = DualAgentMode.TWO_AGENT, decision.reason
            assignments = {"architect": decision.architect_agent_id, "coder": decision.coder_agent_id,
                           "test": decision.coder_agent_id, "review": decision.architect_agent_id}
        return DualAgentSelectionResult(mode, assignments, decision.architect_agent_id, decision.coder_agent_id, reason,
                                        tuple(f"{k}={v}" for k, v in sorted(assignments.items())))


if __name__ == "__main__":
    unittest.main()
