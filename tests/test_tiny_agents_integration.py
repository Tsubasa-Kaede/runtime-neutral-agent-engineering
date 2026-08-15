import os
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from external_runtime import ExternalAgentRequest, InvocationStatus
from tiny_agents_adapter import TinyAgentsAdapter


RUN_REAL_PROVIDER_TESTS = os.environ.get("RUN_REAL_PROVIDER_TESTS", "").lower() in {
    "1",
    "true",
    "yes",
}


@unittest.skipUnless(
    RUN_REAL_PROVIDER_TESTS,
    "Real tiny-agents invocation is opt-in; set RUN_REAL_PROVIDER_TESTS=1",
)
class RealTinyAgentsIntegrationTests(unittest.TestCase):
    def test_real_runtime_invocation(self):
        adapter = TinyAgentsAdapter.from_environment()
        if adapter is None:
            self.skipTest(
                "tiny-agents executable, TINY_AGENTS_AGENT_PATH, and "
                "TINY_AGENTS_COMMAND are required"
            )

        result = adapter.invoke(
            ExternalAgentRequest(
                task_id="tiny-agents-integration-task",
                prompt="Return exactly OK and nothing else.",
                agent_id=adapter.profile.agent_id,
                role=adapter.profile.role,
                provider=adapter.profile.provider,
                model=adapter.profile.model,
                timeout_seconds=30,
            )
        )

        if result.status == InvocationStatus.UNAVAILABLE:
            self.skipTest(result.error or "tiny-agents runtime unavailable")
        self.assertEqual(result.status, InvocationStatus.SUCCESS)
        self.assertIsNotNone(result.trace)
        self.assertEqual(result.trace.status, InvocationStatus.SUCCESS)
        self.assertEqual(result.trace.runtime, "tiny-agents")
        self.assertTrue(result.trace.invocation_id)
        self.assertIsNotNone(result.trace.started_at)
        self.assertIsNotNone(result.trace.finished_at)
        self.assertGreaterEqual(result.trace.duration_ms, 0)
        self.assertIn("OK", str(result.output))


if __name__ == "__main__":
    unittest.main()
