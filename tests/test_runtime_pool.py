import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from runtime_pool import RuntimeHealthCache
from runtime_status import HealthEvidence, ReasonCode, RuntimeState, RuntimeStatus


def status(state, checked=100, expires=200, runtime_id="runtime-a"):
    return RuntimeStatus(
        runtime_id=runtime_id,
        executable="agent.exe",
        version="1.0",
        status=state,
        provider="provider",
        model="model",
        auth_method="apikey",
        reason_code=ReasonCode.NONE if state is RuntimeState.READY else ReasonCode.AUTH_REQUIRED,
        evidence=HealthEvidence("verified", "authenticated", "verified", "verified", "passed"),
        checked_at=checked,
        expires_at=expires,
    )


class RuntimeHealthCacheTests(unittest.TestCase):
    def test_valid_ttl_returns_cached_status_without_probe(self):
        cache = RuntimeHealthCache(clock=lambda: 150)
        cached = status(RuntimeState.READY)
        cache.store("runtime-a", "fingerprint-a", cached)
        calls = []

        result = cache.get_or_refresh("runtime-a", "fingerprint-a", lambda: calls.append(1) or status(RuntimeState.ERROR))

        self.assertEqual(result, cached)
        self.assertEqual(calls, [])

    def test_expired_entry_refreshes(self):
        cache = RuntimeHealthCache(clock=lambda: 250)
        cache.store("runtime-a", "fingerprint-a", status(RuntimeState.READY))
        calls = []

        result = cache.get_or_refresh("runtime-a", "fingerprint-a", lambda: calls.append(1) or status(RuntimeState.AUTH_REQUIRED))

        self.assertEqual(result.status, RuntimeState.AUTH_REQUIRED)
        self.assertEqual(calls, [1])

    def test_fingerprint_change_refreshes(self):
        cache = RuntimeHealthCache(clock=lambda: 150)
        cache.store("runtime-a", "fingerprint-a", status(RuntimeState.READY))
        calls = []

        result = cache.get_or_refresh("runtime-a", "fingerprint-b", lambda: calls.append(1) or status(RuntimeState.READY))

        self.assertEqual(result.status, RuntimeState.READY)
        self.assertEqual(calls, [1])

    def test_auth_failure_invalidates_cached_ready_status(self):
        cache = RuntimeHealthCache(clock=lambda: 150)
        cache.store("runtime-a", "fingerprint-a", status(RuntimeState.READY))

        cache.invalidate("runtime-a", reason=ReasonCode.AUTH_REQUIRED)

        calls = []
        result = cache.get_or_refresh("runtime-a", "fingerprint-a", lambda: calls.append(1) or status(RuntimeState.AUTH_REQUIRED))
        self.assertEqual(result.status, RuntimeState.AUTH_REQUIRED)
        self.assertEqual(calls, [1])

    def test_ready_pool_excludes_non_ready_statuses(self):
        cache = RuntimeHealthCache(clock=lambda: 150)
        cache.store("ready", "a", status(RuntimeState.READY, runtime_id="ready"))
        cache.store("auth", "b", status(RuntimeState.AUTH_REQUIRED, runtime_id="auth"))

        self.assertEqual([item.runtime_id for item in cache.ready_statuses()], ["ready"])


if __name__ == "__main__":
    unittest.main()
