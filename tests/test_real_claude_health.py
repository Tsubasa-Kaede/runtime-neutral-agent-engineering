import os
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from claude_code_adapter import ClaudeCodeAdapter
from runtime_health import RuntimeHealthController
from runtime_status import RuntimeState


@unittest.skipUnless(
    os.environ.get("RUN_REAL_PROVIDER_TESTS") == "1",
    "Real Claude health is opt-in; set RUN_REAL_PROVIDER_TESTS=1",
)
class RealClaudeHealthIntegrationTests(unittest.TestCase):
    def test_real_health_pipeline_returns_only_an_evidenced_status(self):
        adapter = ClaudeCodeAdapter.from_environment()
        if adapter is None:
            self.fail("UNAVAILABLE: Claude Code executable was not found")

        status = RuntimeHealthController(ttl_seconds=120).check(adapter)

        self.assertIn(
            status.status,
            {
                RuntimeState.READY,
                RuntimeState.AUTH_REQUIRED,
                RuntimeState.UNAVAILABLE,
                RuntimeState.ERROR,
            },
        )
        if status.status is RuntimeState.READY:
            self.assertEqual(status.reason_code.value, "NONE")
            self.assertEqual(status.evidence.health, "passed")
            self.assertEqual(status.evidence.output_class, "exact_ok")
        else:
            self.assertNotEqual(status.status, RuntimeState.READY)


if __name__ == "__main__":
    unittest.main()
