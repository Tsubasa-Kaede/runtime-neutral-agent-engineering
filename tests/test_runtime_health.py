import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from runtime_health import (
    AuthenticationCheck,
    MinimalHealthCheck,
    ProviderModelCheck,
    RuntimeHealthController,
)
from runtime_status import AuthenticationState, ReasonCode, RuntimeState


class FakeHealthAdapter:
    def __init__(self, auth=AuthenticationState.AUTHENTICATED, provider=True, health=True):
        self.calls = []
        self.auth = auth
        self.provider = provider
        self.health = health

    def discover(self):
        self.calls.append("discover")
        return type("Discovery", (), {"runtime": "test-runtime", "available": True, "version": "1.0"})()

    def check_authentication(self):
        self.calls.append("authentication")
        return AuthenticationCheck(self.auth, "apikey")

    def check_provider_model(self):
        self.calls.append("provider_model")
        return ProviderModelCheck("provider", "model", self.provider)

    def minimal_health_check(self, timeout_seconds):
        self.calls.append(("health", timeout_seconds))
        return MinimalHealthCheck(self.health)


class RuntimeHealthTests(unittest.TestCase):
    def test_ready_requires_all_checks_in_order(self):
        adapter = FakeHealthAdapter()
        status = RuntimeHealthController(ttl_seconds=60, clock=lambda: 100).check(adapter)

        self.assertEqual(status.status, RuntimeState.READY)
        self.assertEqual(adapter.calls, [
            "discover", "authentication", "provider_model", ("health", 30.0),
        ])
        self.assertEqual(status.expires_at, 160)

    def test_auth_required_stops_before_minimal_health(self):
        adapter = FakeHealthAdapter(auth=AuthenticationState.AUTH_REQUIRED)
        status = RuntimeHealthController().check(adapter)

        self.assertEqual(status.status, RuntimeState.AUTH_REQUIRED)
        self.assertEqual(status.reason_code, ReasonCode.AUTH_REQUIRED)
        self.assertNotIn("health", str(adapter.calls))

    def test_provider_failure_does_not_call_model_health(self):
        adapter = FakeHealthAdapter(provider=False)
        status = RuntimeHealthController().check(adapter)

        self.assertEqual(status.status, RuntimeState.UNAVAILABLE)
        self.assertEqual(status.reason_code, ReasonCode.PROVIDER_UNREACHABLE)
        self.assertNotIn("health", str(adapter.calls))

    def test_health_failure_is_not_ready(self):
        adapter = FakeHealthAdapter(health=False)
        status = RuntimeHealthController().check(adapter)

        self.assertEqual(status.status, RuntimeState.ERROR)
        self.assertEqual(status.reason_code, ReasonCode.HEALTH_CHECK_FAILED)


if __name__ == "__main__":
    unittest.main()
