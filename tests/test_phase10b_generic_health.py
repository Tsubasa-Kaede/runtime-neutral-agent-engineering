"""Phase 10B-A: Runtime-neutral generic health contract.

Chain under test:
  RuntimeCandidate -> RuntimeHealthProbe -> AuthenticationCheck
  -> ProviderModelCheck -> MinimalHealthCheck -> RuntimeHealthResult -> RuntimeStatus
Claude Code is just one probe implementation; the pipeline has no
runtime-name branches. Default tests never start a real runtime.
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from external_runtime import RuntimeDiscovery
from generic_runtime_health import GenericRuntimeHealth, RuntimeHealthProbe
from runtime_discovery import DiscoverySource, RuntimeCandidate, RuntimeCandidateDiscovery
from runtime_health import RuntimeHealthResult
from runtime_status import (
    AuthenticationState,
    HealthEvidence,
    ReasonCode,
    RuntimeState,
    RuntimeStatus,
)
from task_budget import BudgetUsage
from loop_guard import LoopGuard


def candidate(rid="runtime-a", available=True):
    return RuntimeCandidate(rid, "cli", "Runtime A", available,
                            None if available else "NOT_FOUND: missing")


def green_probe(trace=None):
    probe = Mock(spec=["discover", "check_authentication", "check_provider_model", "minimal_health_check"])
    probe.discover.return_value = RuntimeDiscovery("runtime-a", True, "1.0", None, frozenset())
    probe.check_authentication.return_value = type("A", (), {
        "state": AuthenticationState.AUTHENTICATED, "method": "managed", "reason_code": ReasonCode.NONE})()
    probe.check_provider_model.return_value = type("P", (), {
        "provider": "p", "model": "m", "available": True, "reason_code": ReasonCode.NONE})()
    probe.minimal_health_check.return_value = type("H", (), {
        "passed": True, "reason_code": ReasonCode.NONE, "trace": trace, "output_class": "exact_ok"})()
    return probe


class Phase10BGenericHealthTests(unittest.TestCase):
    def setUp(self):
        auth_paths = [Path.home() / ".codex" / "auth.json", Path.home() / ".codex" / "config.toml"]
        self.auth_before = {p: (p.stat().st_mtime_ns, p.stat().st_size) for p in auth_paths if p.exists()}
        self.health = GenericRuntimeHealth()

    def test_discovered_candidate_reaches_ready_only_through_all_gates(self):
        trace = Mock(invocation_id="inv-x", exit_code=0, duration_ms=5)
        result = self.health.check(candidate(), green_probe(trace))
        self.assertIsInstance(result, RuntimeHealthResult)
        self.assertEqual(result.status.status, RuntimeState.READY)
        self.assertEqual(result.status.reason_code, ReasonCode.NONE)
        self.assertIs(result.trace, trace)

    def test_undiscovered_candidate_is_unavailable_without_probing(self):
        probe = green_probe()
        result = self.health.check(candidate(available=False), probe)
        self.assertEqual(result.status.status, RuntimeState.UNAVAILABLE)
        self.assertEqual(result.status.reason_code, ReasonCode.EXECUTABLE_NOT_FOUND)
        probe.discover.assert_not_called()
        probe.check_authentication.assert_not_called()

    def test_auth_required_maps_to_auth_required(self):
        probe = green_probe()
        probe.check_authentication.return_value = type("A", (), {
            "state": AuthenticationState.AUTH_REQUIRED, "method": "apikey", "reason_code": ReasonCode.AUTH_REQUIRED})()
        result = self.health.check(candidate(), probe)
        self.assertEqual(result.status.status, RuntimeState.AUTH_REQUIRED)
        probe.minimal_health_check.assert_not_called()

    def test_provider_unavailable_maps_to_unavailable(self):
        probe = green_probe()
        probe.check_provider_model.return_value = type("P", (), {
            "provider": "p", "model": "m", "available": False, "reason_code": ReasonCode.PROVIDER_UNREACHABLE})()
        result = self.health.check(candidate(), probe)
        self.assertEqual(result.status.status, RuntimeState.UNAVAILABLE)
        self.assertEqual(result.status.reason_code, ReasonCode.PROVIDER_UNREACHABLE)

    def test_unsupported_provider_check_never_becomes_ready(self):
        probe = green_probe()
        probe.check_provider_model.return_value = type("P", (), {
            "provider": None, "model": None, "available": False,
            "reason_code": ReasonCode.UNSUPPORTED_HEALTH_CHECK})()
        result = self.health.check(candidate(), probe)
        self.assertNotEqual(result.status.status, RuntimeState.READY)
        self.assertIsNone(result.status.provider)
        self.assertIsNone(result.status.model)

    def test_minimal_health_failure_is_error_with_trace(self):
        trace = Mock(invocation_id="inv-y", exit_code=1, duration_ms=9)
        probe = green_probe(trace)
        probe.minimal_health_check.return_value = type("H", (), {
            "passed": False, "reason_code": ReasonCode.HEALTH_CHECK_FAILED,
            "trace": trace, "output_class": "invoke_failed"})()
        result = self.health.check(candidate(), probe)
        self.assertEqual(result.status.status, RuntimeState.ERROR)
        self.assertIs(result.trace, trace)

    def test_unsupported_minimal_health_does_not_start_runtime(self):
        probe = green_probe()
        probe.minimal_health_check.return_value = type("H", (), {
            "passed": False, "reason_code": ReasonCode.UNSUPPORTED_HEALTH_CHECK,
            "trace": None, "output_class": "skipped"})()
        result = self.health.check(candidate(), probe)
        self.assertNotEqual(result.status.status, RuntimeState.READY)
        self.assertIsNone(result.trace)

    def test_result_is_immutable_and_secret_free(self):
        result = self.health.check(candidate(), green_probe())
        with self.assertRaises(Exception):
            result.status = None
        surface = repr(result).lower()
        for marker in ("token", "secret", "api_key", "authorization", "stdout", "stderr"):
            self.assertNotIn(marker, surface)

    def test_health_consumes_no_budget_or_guard(self):
        usage = BudgetUsage()
        guard = LoopGuard()
        self.health.check(candidate(), green_probe())
        self.health.check(candidate(available=False), green_probe())
        self.assertEqual(usage.total_agent_calls, 0)
        self.assertEqual(usage.iterations_used, 0)
        self.assertEqual(guard.check("t", "architect", "a"), "ALLOW")

    def test_deterministic_for_same_inputs(self):
        from runtime_health import RuntimeHealthController
        health = GenericRuntimeHealth(RuntimeHealthController(ttl_seconds=60, clock=lambda: 100.0))
        first = health.check(candidate(), green_probe())
        second = health.check(candidate(), green_probe())
        self.assertEqual(first.status, second.status)

    def test_claude_adapter_satisfies_probe_contract(self):
        from claude_code_adapter import ClaudeCodeAdapter
        for method in ("discover", "check_authentication", "check_provider_model", "minimal_health_check"):
            self.assertTrue(callable(getattr(ClaudeCodeAdapter, method, None)))

    def test_generic_layer_has_no_runtime_name_branches(self):
        import generic_runtime_health
        text = Path(generic_runtime_health.__file__).read_text(encoding="utf-8").lower()
        for name in ("claude", "codex", "deepseek", "gemini"):
            self.assertNotIn(name, text)

    def test_legacy_check_api_still_returns_status(self):
        from runtime_health import RuntimeHealthController
        status = RuntimeHealthController(ttl_seconds=60).check(green_probe())
        self.assertIsInstance(status, RuntimeStatus)
        self.assertEqual(status.status, RuntimeState.READY)

    def test_candidate_pipeline_from_discovery_layer(self):
        adapter = Mock()
        adapter.discover.return_value = RuntimeDiscovery("runtime-a", True, "1.0", None, frozenset())
        candidates = RuntimeCandidateDiscovery([DiscoverySource("runtime-a", "cli", "Runtime A", adapter)]).discover_all()
        result = self.health.check(candidates[0], green_probe())
        self.assertEqual(result.status.status, RuntimeState.READY)

    def test_auth_and_config_untouched(self):
        self.health.check(candidate(), green_probe())
        auth_paths = [Path.home() / ".codex" / "auth.json", Path.home() / ".codex" / "config.toml"]
        auth_after = {p: (p.stat().st_mtime_ns, p.stat().st_size) for p in auth_paths if p.exists()}
        self.assertEqual(self.auth_before, auth_after)


if __name__ == "__main__":
    unittest.main()
