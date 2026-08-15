import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from claude_code_adapter import ClaudeCodeAdapter
from external_runtime import (
    ExternalAgentRequest,
    InvocationStatus,
    RuntimeProfile,
)


class FakeProcess:
    def __init__(self, stdout="{\"type\":\"result\",\"result\":\"ok\"}\n", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.pid = 123
        self.killed = False
        self.argv = None

    def communicate(self, input=None, timeout=None):
        self.input = input
        self.timeout = timeout
        return self.stdout, self.stderr

    def kill(self):
        self.killed = True


class ClaudeCodeAdapterTests(unittest.TestCase):
    def profile(self):
        return RuntimeProfile(
            agent_id="coding-agent",
            runtime="claude-cli",
            provider="anthropic",
            model="claude-opus-5",
            role="coder",
            capabilities=frozenset({"coding", "debugging", "tool_use"}),
        )

    def request(self):
        return ExternalAgentRequest(
            task_id="task-1",
            prompt="Return exactly OK.",
            agent_id="coding-agent",
            role="coder",
            provider="anthropic",
            model="claude-opus-5",
            timeout_seconds=3,
        )

    def test_invoke_starts_cli_and_returns_trace(self):
        process = FakeProcess()
        adapter = ClaudeCodeAdapter(profile=self.profile(), executable="claude")
        with patch("claude_code_adapter.subprocess.Popen", return_value=process) as popen:
            result = adapter.invoke(self.request())

        self.assertEqual(result.status, InvocationStatus.SUCCESS)
        self.assertEqual(result.output, "ok")
        self.assertEqual(result.trace.status, InvocationStatus.SUCCESS)
        self.assertEqual(result.trace.exit_code, 0)
        self.assertEqual(result.trace.runtime, "claude-cli")
        self.assertEqual(result.trace.agent_id, "coding-agent")
        argv = popen.call_args.args[0]
        self.assertIn("--print", argv)
        self.assertIn("--output-format", argv)
        self.assertNotIn("--dangerously-skip-permissions", argv)

    def test_nonzero_exit_is_failed_and_trace_is_not_success(self):
        process = FakeProcess(stdout="", stderr="bad", returncode=2)
        adapter = ClaudeCodeAdapter(profile=self.profile(), executable="claude")
        with patch("claude_code_adapter.subprocess.Popen", return_value=process):
            result = adapter.invoke(self.request())

        self.assertEqual(result.status, InvocationStatus.FAILED)
        self.assertEqual(result.trace.status, InvocationStatus.FAILED)
        self.assertEqual(result.trace.exit_code, 2)
        self.assertEqual(result.error, "bad")

    def test_timeout_kills_process_and_records_timeout(self):
        class TimeoutProcess(FakeProcess):
            def communicate(self, input=None, timeout=None):
                raise TimeoutError("expired")

        process = TimeoutProcess()
        adapter = ClaudeCodeAdapter(profile=self.profile(), executable="claude")
        with patch("claude_code_adapter.subprocess.Popen", return_value=process):
            result = adapter.invoke(self.request())

        self.assertEqual(result.status, InvocationStatus.TIMEOUT)
        self.assertEqual(result.trace.status, InvocationStatus.TIMEOUT)
        self.assertTrue(process.killed)

    def test_discovery_is_runtime_only_not_capability_claim(self):
        adapter = ClaudeCodeAdapter(profile=self.profile(), executable="claude")
        with patch.object(adapter, "_probe", return_value=(True, "2.1.0")):
            discovery = adapter.discover()

        self.assertEqual(discovery.runtime, "claude-cli")
        self.assertTrue(discovery.available)
        self.assertEqual(discovery.version, "2.1.0")
        self.assertEqual(discovery.capabilities, frozenset())


@unittest.skipUnless(
    os.environ.get("RUN_REAL_PROVIDER_TESTS", "").lower() in {"1", "true", "yes"},
    "Real Claude CLI integration is opt-in; set RUN_REAL_PROVIDER_TESTS=1",
)
class RealClaudeCodeIntegrationTests(unittest.TestCase):
    def test_real_runtime_invocation(self):
        adapter = ClaudeCodeAdapter.from_environment()
        if adapter is None:
            self.skipTest("Claude CLI unavailable")
        result = adapter.invoke(
            ExternalAgentRequest(
                task_id="integration-task",
                prompt="Return exactly OK and nothing else.",
                agent_id="coding-agent",
                role="coder",
                provider="anthropic",
                model=None,
                timeout_seconds=30,
            )
        )
        if result.status == InvocationStatus.UNAVAILABLE:
            self.skipTest(result.error or "Claude CLI unavailable")
        self.assertEqual(result.status, InvocationStatus.SUCCESS)
        self.assertEqual(result.trace.status, InvocationStatus.SUCCESS)
        self.assertTrue(result.trace.invocation_id)
        self.assertIsNotNone(result.trace.started_at)
        self.assertIsNotNone(result.trace.finished_at)
        self.assertGreaterEqual(result.trace.duration_ms, 0)


if __name__ == "__main__":
    unittest.main()
