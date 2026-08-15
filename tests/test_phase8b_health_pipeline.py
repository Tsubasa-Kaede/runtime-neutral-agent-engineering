import os
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from claude_code_adapter import ClaudeCodeAdapter
from external_runtime import RuntimeProfile
from runtime_health import RuntimeHealthController
from runtime_status import RuntimeState, ReasonCode


class ClaudeRuntimeHealthPipelineTests(unittest.TestCase):
    def profile(self, provider="anthropic", model="claude-opus-5"):
        return RuntimeProfile("agent-a", "claude-cli", provider, model, "coder", frozenset())

    def adapter(self):
        return ClaudeCodeAdapter(self.profile(), "claude")

    def test_authenticated_provider_and_health_produce_ready(self):
        adapter = self.adapter()
        adapter.discover = Mock(return_value=type("Discovery", (), {"runtime": "claude-cli", "available": True, "version": "2"})())
        adapter.check_authentication = Mock(return_value=type("Auth", (), {"state": "AUTHENTICATED", "method": "oauth_token", "reason_code": ReasonCode.NONE})())
        adapter.check_provider_model = Mock(return_value=type("Provider", (), {"provider": "anthropic", "model": "claude-opus-5", "available": True, "reason_code": ReasonCode.NONE})())
        adapter.minimal_health_check = Mock(return_value=type("Health", (), {"passed": True, "reason_code": ReasonCode.NONE})())
        status = RuntimeHealthController(clock=lambda: 10).check(adapter)
        self.assertEqual(status.status, RuntimeState.READY)
        adapter.minimal_health_check.assert_called_once()

    def test_unsupported_provider_check_never_becomes_ready(self):
        adapter = self.adapter()
        adapter.discover = Mock(return_value=type("Discovery", (), {"runtime": "claude-cli", "available": True, "version": "2"})())
        adapter.check_authentication = Mock(return_value=type("Auth", (), {"state": "AUTHENTICATED", "method": "oauth_token", "reason_code": ReasonCode.NONE})())
        adapter.check_provider_model = Mock(return_value=type("Provider", (), {"provider": "anthropic", "model": "claude-opus-5", "available": False, "reason_code": ReasonCode.UNSUPPORTED_HEALTH_CHECK})())
        status = RuntimeHealthController().check(adapter)
        self.assertNotEqual(status.status, RuntimeState.READY)
        self.assertEqual(status.reason_code, ReasonCode.UNSUPPORTED_HEALTH_CHECK)

    def test_cache_hit_avoids_second_pipeline_check(self):
        from runtime_pool import RuntimeHealthCache
        adapter = self.adapter()
        adapter.discover = Mock(return_value=type("Discovery", (), {"runtime": "claude-cli", "available": True, "version": "2"})())
        adapter.check_authentication = Mock(return_value=type("Auth", (), {"state": "AUTHENTICATED", "method": "oauth_token", "reason_code": ReasonCode.NONE})())
        adapter.check_provider_model = Mock(return_value=type("Provider", (), {"provider": "anthropic", "model": "claude-opus-5", "available": True, "reason_code": ReasonCode.NONE})())
        adapter.minimal_health_check = Mock(return_value=type("Health", (), {"passed": True, "reason_code": ReasonCode.NONE})())
        controller = RuntimeHealthController(clock=lambda: 10)
        cache = RuntimeHealthCache(clock=lambda: 10)
        first = cache.get_or_refresh("claude-cli", "fp", lambda: controller.check(adapter))
        second = cache.get_or_refresh("claude-cli", "fp", lambda: controller.check(adapter))
        self.assertEqual(first, second)
        self.assertEqual(adapter.discover.call_count, 1)


if __name__ == "__main__":
    unittest.main()
