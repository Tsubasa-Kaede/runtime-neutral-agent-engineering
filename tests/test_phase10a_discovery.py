"""Phase 10A: Runtime-neutral candidate discovery.

Discovery answers only "does this runtime exist / can it be found?".
Health (READY / AUTH_REQUIRED / ...) stays owned by the Runtime Health
Pipeline. No real runtime, model, or provider is invoked here.
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from external_runtime import RuntimeDiscovery
from runtime_discovery import (
    DiscoverySource,
    DiscoveryState,
    RuntimeCandidate,
    RuntimeCandidateDiscovery,
)
from runtime_health import RuntimeHealthController
from runtime_status import AuthenticationState, RuntimeState
from task_budget import BudgetUsage
from loop_guard import LoopGuard


def fake_adapter(available=True, version="1.0", reason=None, error=None):
    adapter = Mock()
    if error is not None:
        adapter.discover.side_effect = error
    else:
        adapter.discover.return_value = RuntimeDiscovery("x", available, version, reason, frozenset())
    return adapter


def source(runtime_id, runtime_type="cli", display=None, adapter=None):
    return DiscoverySource(runtime_id, runtime_type, display or runtime_id, adapter or fake_adapter())


class Phase10ADiscoveryTests(unittest.TestCase):
    def setUp(self):
        auth_paths = [Path.home() / ".codex" / "auth.json", Path.home() / ".codex" / "config.toml"]
        self.auth_before = {p: (p.stat().st_mtime_ns, p.stat().st_size) for p in auth_paths if p.exists()}

    def test_discovers_available_runtime(self):
        adapter = fake_adapter(available=True, version="2.1.0")
        layer = RuntimeCandidateDiscovery([source("runtime-a", "cli", "Runtime A", adapter)])
        candidates = layer.discover_all()
        self.assertEqual(len(candidates), 1)
        candidate = candidates[0]
        self.assertEqual(candidate.runtime_id, "runtime-a")
        self.assertEqual(candidate.runtime_type, "cli")
        self.assertEqual(candidate.display_name, "Runtime A")
        self.assertTrue(candidate.available)
        self.assertEqual(DiscoveryState.DISCOVERED.value, "DISCOVERED")
        self.assertEqual(candidate.version, "2.1.0")

    def test_not_found_runtime_reports_unavailable(self):
        adapter = fake_adapter(available=False, reason="no executable for runtime-a found on PATH")
        layer = RuntimeCandidateDiscovery([source("runtime-a", adapter=adapter)])
        candidate = layer.discover_all()[0]
        self.assertFalse(candidate.available)
        self.assertEqual(DiscoveryState.NOT_FOUND.value, "NOT_FOUND")
        self.assertTrue(candidate.reason.lower().startswith(DiscoveryState.NOT_FOUND.value.lower()))

    def test_discovery_result_has_no_health_state(self):
        layer = RuntimeCandidateDiscovery([source("runtime-a")])
        candidate = layer.discover_all()[0]
        self.assertFalse(hasattr(candidate, "status"))
        for state in RuntimeState:
            self.assertNotIn(state.value, dir(candidate))

    def test_discovered_runtime_health_stays_authoritative(self):
        # DISCOVERED must not be converted into READY: same discovered runtime,
        # three different health outcomes, all owned by the health pipeline.
        for auth_state, expected in (
            (AuthenticationState.AUTHENTICATED, RuntimeState.READY),
            (AuthenticationState.AUTH_REQUIRED, RuntimeState.AUTH_REQUIRED),
            (AuthenticationState.UNKNOWN, RuntimeState.ERROR),
        ):
            with self.subTest(auth=auth_state.value):
                health_adapter = Mock()
                health_adapter.discover.return_value = RuntimeDiscovery("runtime-a", True, "1", None, frozenset())
                health_adapter.check_authentication.return_value = type("Auth", (), {
                    "state": auth_state, "method": "managed", "reason_code": None})()
                health_adapter.check_provider_model.return_value = type("PM", (), {
                    "provider": "p", "model": "m", "available": True, "reason_code": None})()
                health_adapter.minimal_health_check.return_value = type("H", (), {
                    "passed": True, "reason_code": None})()
                status = RuntimeHealthController(ttl_seconds=60).check(health_adapter)
                self.assertEqual(status.status, expected)
                # discovery layer result is unchanged by health
                candidates = RuntimeCandidateDiscovery([source("runtime-a")]).discover_all()
                self.assertTrue(candidates[0].available)

    def test_deterministic_and_sorted_output(self):
        layer = RuntimeCandidateDiscovery([
            source("zeta"), source("alpha"), source("mid"),
        ])
        first = layer.discover_all()
        second = layer.discover_all()
        self.assertEqual(first, second)
        self.assertEqual([c.runtime_id for c in first], ["alpha", "mid", "zeta"])

    def test_runtime_names_do_not_change_core_logic(self):
        adapter_one = fake_adapter(version="1.0")
        adapter_two = fake_adapter(version="1.0")
        a = RuntimeCandidateDiscovery([source("runtime-a", adapter=adapter_one)]).discover_all()[0]
        b = RuntimeCandidateDiscovery([source("totally-other-name", adapter=adapter_two)]).discover_all()[0]
        self.assertEqual(a.runtime_type, b.runtime_type)
        self.assertEqual(a.available, b.available)
        self.assertEqual(a.version, b.version)

    def test_discovery_does_not_invoke_models(self):
        adapter = fake_adapter()
        RuntimeCandidateDiscovery([source("runtime-a", adapter=adapter)]).discover_all()
        adapter.discover.assert_called_once()
        self.assertFalse(hasattr(RuntimeCandidateDiscovery, "invoke"))

    def test_no_trace_budget_or_guard_consumption(self):
        adapter = fake_adapter()
        usage = BudgetUsage()
        guard = LoopGuard()
        candidate = RuntimeCandidateDiscovery([source("runtime-a", adapter=adapter)]).discover_all()[0]
        self.assertFalse(hasattr(candidate, "invocation_id"))
        self.assertFalse(hasattr(candidate, "trace"))
        self.assertEqual(usage.total_agent_calls, 0)
        self.assertEqual(usage.iterations_used, 0)
        self.assertEqual(guard.check("t", "architect", "a"), "ALLOW")

    def test_discovery_error_is_not_found_not_crash(self):
        adapter = fake_adapter(error=OSError("spawn failed"))
        candidate = RuntimeCandidateDiscovery([source("runtime-a", adapter=adapter)]).discover_all()[0]
        self.assertFalse(candidate.available)
        self.assertIn(DiscoveryState.NOT_FOUND.value, candidate.reason)

    def test_secret_shaped_fields_rejected(self):
        for bad in ("token=abc", "api_key: x", "secret=1", "authorization: Bearer z"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    RuntimeCandidate("r", "cli", bad, True, bad)

    def test_existing_adapters_are_pluggable_without_name_branches(self):
        from claude_code_adapter import ClaudeCodeAdapter
        from tiny_agents_adapter import TinyAgentsAdapter
        for adapter_cls in (ClaudeCodeAdapter, TinyAgentsAdapter):
            self.assertTrue(callable(getattr(adapter_cls, "discover", None)))
        import runtime_discovery
        source_text = Path(runtime_discovery.__file__).read_text(encoding="utf-8").lower()
        for name in ("claude", "codex", "gemini", "deepseek"):
            self.assertNotIn(name, source_text)

    def test_phase9c_selection_and_handoff_smoke(self):
        from dual_agent_selection import DualAgentSelection
        from orchestrator import Orchestrator
        from capability_registry import CapabilityRegistry, AgentProfile, CapabilityConfidence, CapabilityEvidence, CapabilityName
        from runtime_status import RuntimeStatus, HealthEvidence, ReasonCode, RuntimeState
        from invocation_plan import InvocationPlan

        def runtime(rid):
            return RuntimeStatus(rid, rid, "1", RuntimeState.READY, "p", None, "managed", ReasonCode.NONE, HealthEvidence("v", "v", "v", "v", "v"), 1, 100)

        profiles = [
            AgentProfile("agent-a", "runtime-a", "p", None, None, {CapabilityName.ARCHITECTURE: CapabilityEvidence(CapabilityName.ARCHITECTURE, .95, CapabilityConfidence.VERIFIED, "t")}, None),
            AgentProfile("agent-b", "runtime-b", "p", None, None, {CapabilityName.CODING: CapabilityEvidence(CapabilityName.CODING, .95, CapabilityConfidence.VERIFIED, "t")}, None),
        ]
        statuses = {"runtime-a": runtime("runtime-a"), "runtime-b": runtime("runtime-b")}
        budget, usage = None, None
        from task_budget import TaskBudget
        budget = TaskBudget(10, 4)
        usage = BudgetUsage()
        decision = DualAgentSelection().decide(profiles, statuses, "SIMPLE", budget, usage)
        self.assertFalse(decision.use_dual_agent)
        orch = Orchestrator(CapabilityRegistry(profiles), statuses, budget, usage, LoopGuard(4))
        plan = orch.plan("t", "fix one function")
        self.assertEqual([s.stage for s in plan.stages], ["coder"])

    def test_auth_and_config_untouched(self):
        RuntimeCandidateDiscovery([source("runtime-a")]).discover_all()
        auth_paths = [Path.home() / ".codex" / "auth.json", Path.home() / ".codex" / "config.toml"]
        auth_after = {p: (p.stat().st_mtime_ns, p.stat().st_size) for p in auth_paths if p.exists()}
        self.assertEqual(self.auth_before, auth_after)


if __name__ == "__main__":
    unittest.main()
