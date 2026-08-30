import os
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from external_runtime import ExternalAgentRequest, InvocationStatus
from codex_adapter import CodexAdapter


# 与生产四处 gate 站点（claude/codex/pi adapter 的 minimal_health_check
# 与 RealGateExecutor）逐字对齐：只认 "1"，不接受 true/yes —— 同一
# 进程内"门开没开"不允许出现两种答案。
RUN_REAL_PROVIDER_TESTS = os.environ.get("RUN_REAL_PROVIDER_TESTS", "") == "1"


@unittest.skipUnless(
    RUN_REAL_PROVIDER_TESTS,
    "Real codex invocation is opt-in; set RUN_REAL_PROVIDER_TESTS=1",
)
class RealCodexIntegrationTests(unittest.TestCase):
    def test_real_runtime_invocation(self):
        adapter = CodexAdapter.from_environment()
        if adapter is None:
            self.skipTest("codex executable is required")

        result = adapter.invoke(
            ExternalAgentRequest(
                task_id="codex-integration-task",
                prompt="Return exactly OK and nothing else.",
                agent_id=adapter.profile.agent_id,
                role=adapter.profile.role,
                provider=adapter.profile.provider,
                model=adapter.profile.model,
                timeout_seconds=30,
            )
        )

        if result.status == InvocationStatus.UNAVAILABLE:
            self.skipTest(result.error or "codex runtime unavailable")
        self.assertEqual(result.status, InvocationStatus.SUCCESS)
        self.assertIsNotNone(result.trace)
        self.assertEqual(result.trace.status, InvocationStatus.SUCCESS)
        self.assertEqual(result.trace.runtime, "codex-cli")
        self.assertTrue(result.trace.invocation_id)
        self.assertIsNotNone(result.trace.started_at)
        self.assertIsNotNone(result.trace.finished_at)
        self.assertGreaterEqual(result.trace.duration_ms, 0)
        self.assertIn("OK", str(result.output))


if __name__ == "__main__":
    unittest.main()
