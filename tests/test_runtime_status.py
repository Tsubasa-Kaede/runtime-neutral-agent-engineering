import sys
import unittest
from dataclasses import FrozenInstanceError, asdict
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from runtime_status import (
    AuthenticationState,
    HealthEvidence,
    ReasonCode,
    RuntimeState,
    RuntimeStatus,
)


class RuntimeStatusTests(unittest.TestCase):
    def test_supports_four_terminal_states(self):
        self.assertEqual(
            {state.value for state in RuntimeState},
            {"READY", "AUTH_REQUIRED", "UNAVAILABLE", "ERROR"},
        )

    def test_status_is_immutable_and_contains_only_non_secret_fields(self):
        status = RuntimeStatus(
            runtime_id="codex-cli",
            executable="codex.exe",
            version="0.147.0",
            status=RuntimeState.AUTH_REQUIRED,
            provider="deepseek",
            model="deepseek-v4-flash",
            auth_method="apikey",
            reason_code=ReasonCode.AUTH_REQUIRED,
            evidence=HealthEvidence(
                discovery="verified",
                authentication=AuthenticationState.AUTH_REQUIRED.value,
                provider="verified",
                model="verified",
                health="not_checked",
                exit_code=1,
                duration_ms=82,
                output_class="auth_required",
            ),
            checked_at=100.0,
            expires_at=160.0,
        )

        self.assertEqual(status.status, RuntimeState.AUTH_REQUIRED)
        self.assertEqual(status.provider, "deepseek")
        self.assertEqual(status.model, "deepseek-v4-flash")
        self.assertNotIn("token", asdict(status))
        self.assertNotIn("secret", asdict(status))
        with self.assertRaises(FrozenInstanceError):
            status.status = RuntimeState.READY

    def test_health_evidence_rejects_raw_output_fields(self):
        with self.assertRaises(ValueError):
            HealthEvidence(
                discovery="verified",
                authentication="authenticated",
                provider="verified",
                model="verified",
                health="passed",
                exit_code=0,
                duration_ms=10,
                output_class="raw stdout: secret-value",
            )

    def test_authentication_states_are_classified(self):
        self.assertEqual(AuthenticationState.AUTHENTICATED.value, "AUTHENTICATED")
        self.assertEqual(AuthenticationState.AUTH_REQUIRED.value, "AUTH_REQUIRED")
        self.assertEqual(AuthenticationState.REJECTED.value, "REJECTED")
        self.assertEqual(AuthenticationState.UNKNOWN.value, "UNKNOWN")


if __name__ == "__main__":
    unittest.main()
