"""Phase 10B-B: wire the real Claude adapter into the generic health contract.

Uses the genuine ClaudeCodeAdapter class with its process layer mocked, so no
real runtime is started. Covers the full chain:
RuntimeCandidateDiscovery -> GenericRuntimeHealth -> RuntimeHealthCache,
plus the READY gate into selection/invocation.
"""
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from claude_code_adapter import ClaudeCodeAdapter
from external_runtime import RuntimeProfile
from generic_runtime_health import GenericRuntimeHealth
from runtime_discovery import DiscoverySource, RuntimeCandidateDiscovery
from runtime_pool import RuntimeHealthCache
from runtime_status import RuntimeState
from task_budget import BudgetUsage, TaskBudget
from loop_guard import LoopGuard


def completed(returncode=0, stdout="", stderr=""):
    return type("Completed", (), {"returncode": returncode, "stdout": stdout, "stderr": stderr})()


class FakeProcess:
    def __init__(self, stdout='{"type":"result","result":"OK"}', stderr="", returncode=0):
        self._stdout, self._stderr, self.returncode = stdout, stderr, returncode

    def communicate(self, input=None, timeout=None):
        return self._stdout, self._stderr

    def kill(self):
        pass


def claude_adapter():
    return ClaudeCodeAdapter(
        RuntimeProfile("claude-agent", "claude-cli", "anthropic", None, "coder", frozenset()),
        "claude",
    )


def run_dispatch(version_rc=0, auth_rc=0, auth_stdout='{"loggedIn": true, "authMethod": "oauth_token", "apiProvider": "firstParty"}'):
    def dispatch(argv, **kwargs):
        args = argv if isinstance(argv, list) else list(argv)
        if "--version" in args:
            return completed(version_rc, "2.1.227 (Claude Code)")
        if "auth" in args:
            return completed(auth_rc, auth_stdout)
        return completed(0, "")
    return dispatch


class Phase10BHealthCompatibilityTests(unittest.TestCase):
    def setUp(self):
        auth_paths = [Path.home() / ".codex" / "auth.json", Path.home() / ".codex" / "config.toml"]
        self.auth_before = {p: (p.stat().st_mtime_ns, p.stat().st_size) for p in auth_paths if p.exists()}

    def discover_candidate(self, adapter):
        candidates = RuntimeCandidateDiscovery([DiscoverySource("claude-cli", "cli", "Claude Code", adapter)]).discover_all()
        return candidates[0]

    def test_claude_adapter_reaches_ready_through_generic_contract(self):
        adapter = claude_adapter()
        with patch("claude_code_adapter.subprocess.run", side_effect=run_dispatch()), \
             patch("claude_code_adapter.subprocess.Popen", return_value=FakeProcess()) as popen, \
             patch.dict(os.environ, {"RUN_REAL_PROVIDER_TESTS": "1"}):
            candidate = self.discover_candidate(adapter)
            result = GenericRuntimeHealth().check(candidate, adapter)
        self.assertEqual(result.status.status, RuntimeState.READY)
        self.assertIsNotNone(result.trace)
        self.assertTrue(result.trace.invocation_id.startswith("invocation-"))
        self.assertEqual(popen.call_count, 1)
        argv = popen.call_args.args[0]
        self.assertNotIn("login", argv)
        self.assertNotIn("logout", argv)

    def test_auth_required_via_generic_contract(self):
        adapter = claude_adapter()
        with patch("claude_code_adapter.subprocess.run",
                   side_effect=run_dispatch(auth_rc=1, auth_stdout="")), \
             patch("claude_code_adapter.subprocess.Popen") as popen:
            candidate = self.discover_candidate(adapter)
            result = GenericRuntimeHealth().check(candidate, adapter)
        self.assertEqual(result.status.status, RuntimeState.AUTH_REQUIRED)
        self.assertIsNone(result.trace)
        popen.assert_not_called()

    def test_unavailable_when_executable_probe_fails(self):
        adapter = claude_adapter()
        with patch("claude_code_adapter.subprocess.run", side_effect=run_dispatch(version_rc=1, auth_rc=1, auth_stdout="")), \
             patch("claude_code_adapter.subprocess.Popen") as popen:
            candidate = self.discover_candidate(adapter)
            result = GenericRuntimeHealth().check(candidate, adapter)
        self.assertFalse(candidate.available)
        self.assertEqual(result.status.status, RuntimeState.UNAVAILABLE)
        self.assertIsNone(result.trace)
        popen.assert_not_called()

    def test_unknown_auth_maps_to_error_not_ready(self):
        adapter = claude_adapter()
        with patch("claude_code_adapter.subprocess.run",
                   side_effect=run_dispatch(auth_rc=0, auth_stdout="not-json")), \
             patch("claude_code_adapter.subprocess.Popen") as popen:
            candidate = self.discover_candidate(adapter)
            result = GenericRuntimeHealth().check(candidate, adapter)
        self.assertNotEqual(result.status.status, RuntimeState.READY)
        popen.assert_not_called()

    def test_cache_hit_skips_full_health_check(self):
        adapter = claude_adapter()
        clock = [100.0]
        cache = RuntimeHealthCache(clock=lambda: clock[0])
        with patch("claude_code_adapter.subprocess.run",
                   side_effect=run_dispatch(auth_rc=1, auth_stdout="")) as run_mock:
            generic = GenericRuntimeHealth()
            candidate = self.discover_candidate(adapter)
            first = generic.check_cached(candidate, adapter, cache, "fp-1")
            clock[0] = 110.0
            second = generic.check_cached(candidate, adapter, cache, "fp-1")
        self.assertEqual(first.status, second.status)
        # exactly one full health pass: the TTL hit skipped the auth recheck
        auth_calls = sum(1 for call in run_mock.call_args_list if "auth" in call.args[0])
        self.assertEqual(auth_calls, 1)

    def test_fingerprint_change_triggers_recheck(self):
        adapter = claude_adapter()
        cache = RuntimeHealthCache(clock=lambda: 100.0)
        with patch("claude_code_adapter.subprocess.run",
                   side_effect=run_dispatch(auth_rc=1, auth_stdout="")) as run_mock:
            generic = GenericRuntimeHealth()
            candidate = self.discover_candidate(adapter)
            generic.check_cached(candidate, adapter, cache, "fp-1")
            generic.check_cached(candidate, adapter, cache, "fp-2")
        auth_calls = sum(1 for call in run_mock.call_args_list if "auth" in call.args[0])
        self.assertEqual(auth_calls, 2)  # two full passes

    def test_cache_and_results_are_secret_free(self):
        adapter = claude_adapter()
        cache = RuntimeHealthCache(clock=lambda: 100.0)
        with patch("claude_code_adapter.subprocess.run", side_effect=run_dispatch(auth_rc=1, auth_stdout="")):
            result = GenericRuntimeHealth().check_cached(self.discover_candidate(adapter), adapter, cache, "fp")
        surface = (repr(result) + repr(cache._entries)).lower()
        for marker in ("token", "secret", "api_key", "authorization", "stdout", "stderr"):
            self.assertNotIn(marker, surface)

    def test_health_never_consumes_budget_or_guard(self):
        adapter = claude_adapter()
        usage = BudgetUsage()
        guard = LoopGuard()
        cache = RuntimeHealthCache(clock=lambda: 100.0)
        with patch("claude_code_adapter.subprocess.run", side_effect=run_dispatch()):
            generic = GenericRuntimeHealth()
            generic.check(self.discover_candidate(adapter), adapter)
            generic.check_cached(self.discover_candidate(adapter), adapter, cache, "fp")
        self.assertEqual(usage.total_agent_calls, 0)
        self.assertEqual(usage.iterations_used, 0)
        self.assertEqual(guard.check("t", "architect", "a"), "ALLOW")

    def test_non_ready_status_never_reaches_invocation(self):
        from capability_registry import (
            AgentProfile, CapabilityConfidence, CapabilityEvidence, CapabilityName, CapabilityRegistry,
        )
        from dual_agent_selection import DualAgentSelection
        from orchestrator import Orchestrator
        from runtime_status import HealthEvidence, ReasonCode, RuntimeStatus

        def blocked(rid, state):
            return RuntimeStatus(rid, rid, "1", state, "p", None, "managed", ReasonCode.AUTH_REQUIRED,
                                 HealthEvidence("v", "failed", "n", "n", "n"), 1, 100)

        profiles = [AgentProfile("agent-a", "claude-cli", "p", None, None,
                                 {CapabilityName.CODING: CapabilityEvidence(CapabilityName.CODING, .95, CapabilityConfidence.VERIFIED, "t")}, None)]
        statuses = {"claude-cli": blocked("claude-cli", RuntimeState.AUTH_REQUIRED)}
        budget, usage = TaskBudget(10, 4), BudgetUsage()
        decision = DualAgentSelection().decide(profiles, statuses, "SIMPLE", budget, usage)
        self.assertFalse(decision.use_dual_agent)
        adapter = Mock()
        orch = Orchestrator(CapabilityRegistry(profiles), statuses, budget, usage, LoopGuard(4))
        result = orch.execute("t", "fix one function", {"agent-a": adapter}, "prompt")
        self.assertNotEqual(result.status.value, "SUCCESS")
        adapter.invoke.assert_not_called()

    def test_auth_and_config_untouched(self):
        adapter = claude_adapter()
        with patch("claude_code_adapter.subprocess.run", side_effect=run_dispatch()):
            GenericRuntimeHealth().check(self.discover_candidate(adapter), adapter)
        auth_paths = [Path.home() / ".codex" / "auth.json", Path.home() / ".codex" / "config.toml"]
        auth_after = {p: (p.stat().st_mtime_ns, p.stat().st_size) for p in auth_paths if p.exists()}
        self.assertEqual(self.auth_before, auth_after)


if __name__ == "__main__":
    unittest.main()
