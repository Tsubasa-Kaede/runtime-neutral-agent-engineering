import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from claude_code_adapter import ClaudeCodeAdapter
from external_runtime import RuntimeProfile
from runtime_health import AuthenticationCheck, MinimalHealthCheck, ProviderModelCheck
from runtime_status import AuthenticationState, ReasonCode


class ClaudeHealthTests(unittest.TestCase):
    def profile(self, provider=None, model=None):
        return RuntimeProfile("agent-a", "claude-cli", provider, model, "coder", frozenset())

    def test_authentication_check_classifies_official_logged_in_status(self):
        adapter = ClaudeCodeAdapter(self.profile(), "claude")
        completed = type("Completed", (), {
            "returncode": 0,
            "stdout": '{"loggedIn": true, "authMethod": "oauth_token", "apiProvider": "firstParty"}',
            "stderr": "",
        })()
        with patch("claude_code_adapter.subprocess.run", return_value=completed) as run:
            result = adapter.check_authentication()

        self.assertIsInstance(result, AuthenticationCheck)
        self.assertEqual(result.state, AuthenticationState.AUTHENTICATED)
        self.assertEqual(result.method, "oauth_token")
        self.assertEqual(run.call_args.kwargs["shell"], False)

    def test_authentication_required_never_exposes_output(self):
        adapter = ClaudeCodeAdapter(self.profile(), "claude")
        completed = type("Completed", (), {
            "returncode": 1,
            "stdout": "",
            "stderr": "login required token=secret-value",
        })()
        with patch("claude_code_adapter.subprocess.run", return_value=completed):
            result = adapter.check_authentication()

        self.assertEqual(result.state, AuthenticationState.AUTH_REQUIRED)
        self.assertNotIn("secret", repr(result).lower())
        self.assertEqual(result.reason_code, ReasonCode.AUTH_REQUIRED)

    def test_provider_model_without_verified_profile_is_unsupported(self):
        adapter = ClaudeCodeAdapter(self.profile(), "claude")
        result = adapter.check_provider_model()
        self.assertFalse(result.available)
        self.assertEqual(result.reason_code, ReasonCode.UNSUPPORTED_HEALTH_CHECK)

    def test_minimal_health_is_skipped_without_explicit_opt_in(self):
        adapter = ClaudeCodeAdapter(self.profile("anthropic", "claude-opus-5"), "claude")
        with patch.dict(os.environ, {}, clear=True), patch("claude_code_adapter.subprocess.Popen") as popen:
            result = adapter.minimal_health_check(1)
        self.assertFalse(result.passed)
        self.assertEqual(result.reason_code, ReasonCode.UNSUPPORTED_HEALTH_CHECK)
        popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
