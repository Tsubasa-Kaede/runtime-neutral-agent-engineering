import subprocess
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from external_runtime import ExternalAgentRequest, InvocationStatus, RuntimeProfile
from tiny_agents_adapter import TinyAgentsAdapter


class FakeProcess:
    def __init__(self, stdout="ok\n", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.pid = 456
        self.killed = False
        self.calls = []

    def communicate(self, input=None, timeout=None):
        self.calls.append((input, timeout))
        return self.stdout, self.stderr

    def kill(self):
        self.killed = True


class TinyAgentsAdapterTests(unittest.TestCase):
    def profile(self):
        return RuntimeProfile(
            agent_id="tiny-agent",
            runtime="tiny-agents",
            provider=None,
            model=None,
            role="coder",
            capabilities=frozenset(),
        )

    def request(self, timeout_seconds=3):
        return ExternalAgentRequest(
            task_id="task-1",
            prompt="Return exactly OK.",
            agent_id="tiny-agent",
            role="coder",
            provider="test-provider",
            model="test-model",
            timeout_seconds=timeout_seconds,
        )

    def adapter(self):
        return TinyAgentsAdapter(
            profile=self.profile(),
            executable="tiny-agents",
            agent_path="agent-config",
            command="agent-command",
            command_args=("--fixed", "value"),
        )

    def test_invoke_uses_configured_argv_and_returns_trace(self):
        process = FakeProcess()
        adapter = self.adapter()
        with patch("tiny_agents_adapter.subprocess.Popen", return_value=process) as popen:
            result = adapter.invoke(self.request())

        self.assertEqual(result.status, InvocationStatus.SUCCESS)
        self.assertEqual(result.output, "ok")
        self.assertEqual(result.trace.runtime, "tiny-agents")
        self.assertEqual(result.trace.status, InvocationStatus.SUCCESS)
        self.assertEqual(result.trace.exit_code, 0)
        self.assertEqual(process.calls, [("Return exactly OK.", 3)])
        argv = popen.call_args.args[0]
        self.assertEqual(argv, [
            "tiny-agents", "run", "agent-config", "agent-command", "--fixed", "value",
        ])
        self.assertFalse(popen.call_args.kwargs["shell"])

    def test_nonzero_exit_is_failed_and_does_not_report_success(self):
        process = FakeProcess(stdout="", stderr="bad", returncode=2)
        with patch("tiny_agents_adapter.subprocess.Popen", return_value=process):
            result = self.adapter().invoke(self.request())

        self.assertEqual(result.status, InvocationStatus.FAILED)
        self.assertEqual(result.error, "bad")
        self.assertEqual(result.trace.status, InvocationStatus.FAILED)
        self.assertEqual(result.trace.exit_code, 2)

    def test_timeout_kills_process_and_records_timeout(self):
        class TimeoutProcess(FakeProcess):
            def communicate(self, input=None, timeout=None):
                raise subprocess.TimeoutExpired(cmd="tiny-agents", timeout=timeout)

        process = TimeoutProcess()
        with patch("tiny_agents_adapter.subprocess.Popen", return_value=process):
            result = self.adapter().invoke(self.request(timeout_seconds=0.1))

        self.assertEqual(result.status, InvocationStatus.TIMEOUT)
        self.assertEqual(result.trace.status, InvocationStatus.TIMEOUT)
        self.assertTrue(process.killed)

    def test_cancelled_invoke_returns_cancelled_terminal_status(self):
        adapter = self.adapter()
        started = threading.Event()
        released = threading.Event()

        class BlockingProcess(FakeProcess):
            def communicate(self, input=None, timeout=None):
                started.set()
                released.wait(timeout=1)
                self.returncode = -9
                return "", ""

            def kill(self):
                super().kill()
                released.set()

        process = BlockingProcess()
        results = []
        with patch("tiny_agents_adapter.subprocess.Popen", return_value=process):
            worker = threading.Thread(target=lambda: results.append(adapter.invoke(self.request())))
            worker.start()
            self.assertTrue(started.wait(timeout=1))
            invocation_id = adapter.last_invocation_id
            self.assertIsNotNone(invocation_id)
            cancel_result = adapter.cancel(invocation_id)
            worker.join(timeout=1)

        self.assertEqual(cancel_result.status, InvocationStatus.CANCELLED)
        self.assertEqual(results[0].status, InvocationStatus.CANCELLED)
        self.assertEqual(results[0].trace.status, InvocationStatus.CANCELLED)
        self.assertTrue(process.killed)

    def test_cancel_kills_active_process(self):
        adapter = self.adapter()
        process = FakeProcess()
        adapter._processes["invocation-1"] = process

        result = adapter.cancel("invocation-1")

        self.assertEqual(result.status, InvocationStatus.CANCELLED)
        self.assertTrue(process.killed)
        self.assertIn("invocation-1", adapter._cancelled)

    def test_cancel_unknown_invocation_is_unavailable(self):
        result = self.adapter().cancel("unknown")

        self.assertEqual(result.status, InvocationStatus.UNAVAILABLE)

    def test_error_redacts_common_secret_forms(self):
        error = (
            'api_key=alpha token: beta secret=gamma '
            'Authorization: Bearer delta '
            '"token":"json-secret" hf_1234567890 sk-abcdefghij'
        )

        redacted = TinyAgentsAdapter._safe_error(error)

        for secret in (
            "alpha", "beta", "gamma", "delta", "json-secret",
            "hf_1234567890", "sk-abcdefghij",
        ):
            self.assertNotIn(secret, redacted)
        self.assertGreaterEqual(redacted.count("[REDACTED]"), 7)

    def test_trace_keeps_identity_fields_separate_and_tokens_unknown(self):
        with patch("tiny_agents_adapter.subprocess.Popen", return_value=FakeProcess()):
            result = self.adapter().invoke(self.request())

        self.assertEqual(result.trace.agent_id, "tiny-agent")
        self.assertEqual(result.trace.runtime, "tiny-agents")
        self.assertEqual(result.trace.provider, "test-provider")
        self.assertEqual(result.trace.model, "test-model")
        self.assertEqual(result.trace.role, "coder")
        self.assertEqual(result.trace.input_tokens, "unknown")
        self.assertEqual(result.trace.output_tokens, "unknown")

    def test_discovery_only_reports_runtime_availability(self):
        adapter = self.adapter()
        with patch.object(adapter, "_probe", return_value=(True, None)):
            discovery = adapter.discover()

        self.assertEqual(discovery.runtime, "tiny-agents")
        self.assertTrue(discovery.available)
        self.assertIsNone(discovery.version)
        self.assertEqual(discovery.capabilities, frozenset())

    def test_missing_executable_is_unavailable(self):
        with patch("tiny_agents_adapter.shutil.which", return_value=None):
            adapter = TinyAgentsAdapter.from_environment(
                profile=self.profile(),
                agent_path="agent-config",
                command="agent-command",
            )

        self.assertIsNone(adapter)


if __name__ == "__main__":
    unittest.main()
