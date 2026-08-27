"""CodexAdapter 的离线、确定性测试。

全部用 fake 进程对象替身驱动：不调用真实 codex、不访问网络、
不读取凭据，也不依赖本机是否安装了 Codex CLI。仅有的真实
subprocess 调用点（_probe 的 subprocess.run、invoke 的 Popen）
全部被 patch。
"""
import subprocess
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from codex_adapter import CodexAdapter
from external_runtime import ExternalAgentRequest, InvocationStatus, RuntimeProfile


class FakeProcess:
    def __init__(self, stdout="ok\n", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.pid = 789
        self.killed = False
        self.calls = []

    def communicate(self, input=None, timeout=None):
        self.calls.append((input, timeout))
        return self.stdout, self.stderr

    def kill(self):
        self.killed = True


class CodexAdapterTests(unittest.TestCase):
    def profile(self):
        return RuntimeProfile(
            agent_id="coding-agent",
            runtime="codex-cli",
            provider="openai",
            model=None,
            role="coder",
            capabilities=frozenset(),
        )

    def request(self, timeout_seconds=3, model=None):
        return ExternalAgentRequest(
            task_id="task-1",
            prompt="Return exactly OK.",
            agent_id="coding-agent",
            role="coder",
            provider="test-provider",
            model=model,
            timeout_seconds=timeout_seconds,
        )

    def adapter(self):
        return CodexAdapter(profile=self.profile(), executable="codex")

    def test_invoke_uses_exec_argv_with_positional_prompt(self):
        process = FakeProcess()
        with patch("codex_adapter.subprocess.Popen", return_value=process) as popen:
            result = self.adapter().invoke(self.request())

        self.assertEqual(result.status, InvocationStatus.SUCCESS)
        self.assertEqual(result.output, "ok")
        self.assertEqual(result.trace.runtime, "codex-cli")
        self.assertEqual(result.trace.status, InvocationStatus.SUCCESS)
        self.assertEqual(result.trace.exit_code, 0)
        # The prompt rides in argv, never through stdin.
        self.assertEqual(process.calls, [(None, 3)])
        argv = popen.call_args.args[0]
        self.assertEqual(argv, ["codex", "exec", "Return exactly OK."])
        self.assertFalse(popen.call_args.kwargs["shell"])

    def test_invoke_appends_model_flag_when_requested(self):
        process = FakeProcess()
        with patch("codex_adapter.subprocess.Popen", return_value=process) as popen:
            result = self.adapter().invoke(self.request(model="test-model"))

        self.assertEqual(result.status, InvocationStatus.SUCCESS)
        argv = popen.call_args.args[0]
        self.assertEqual(
            argv, ["codex", "exec", "--model", "test-model", "Return exactly OK."])

    def test_nonzero_exit_is_failed_and_does_not_report_success(self):
        process = FakeProcess(stdout="", stderr="bad", returncode=2)
        with patch("codex_adapter.subprocess.Popen", return_value=process):
            result = self.adapter().invoke(self.request())

        self.assertEqual(result.status, InvocationStatus.FAILED)
        self.assertEqual(result.error, "bad")
        self.assertEqual(result.trace.status, InvocationStatus.FAILED)
        self.assertEqual(result.trace.exit_code, 2)

    def test_timeout_kills_process_and_records_timeout(self):
        class TimeoutProcess(FakeProcess):
            def communicate(self, input=None, timeout=None):
                raise subprocess.TimeoutExpired(cmd="codex", timeout=timeout)

        process = TimeoutProcess()
        with patch("codex_adapter.subprocess.Popen", return_value=process):
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
        with patch("codex_adapter.subprocess.Popen", return_value=process):
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

        redacted = CodexAdapter._safe_error(error)

        for secret in (
            "alpha", "beta", "gamma", "delta", "json-secret",
            "hf_1234567890", "sk-abcdefghij",
        ):
            self.assertNotIn(secret, redacted)
        self.assertGreaterEqual(redacted.count("[REDACTED]"), 7)

    def test_trace_keeps_identity_fields_separate_and_tokens_unknown(self):
        with patch("codex_adapter.subprocess.Popen", return_value=FakeProcess()):
            result = self.adapter().invoke(self.request(model="test-model"))

        self.assertEqual(result.trace.agent_id, "coding-agent")
        self.assertEqual(result.trace.runtime, "codex-cli")
        self.assertEqual(result.trace.provider, "test-provider")
        self.assertEqual(result.trace.model, "test-model")
        self.assertEqual(result.trace.role, "coder")
        self.assertEqual(result.trace.input_tokens, "unknown")
        self.assertEqual(result.trace.output_tokens, "unknown")

    def test_discovery_reports_version_without_capability_claim(self):
        adapter = self.adapter()
        with patch.object(adapter, "_probe", return_value=(True, "codex-cli 0.20.0")):
            discovery = adapter.discover()

        self.assertEqual(discovery.runtime, "codex-cli")
        self.assertTrue(discovery.available)
        self.assertEqual(discovery.version, "codex-cli 0.20.0")
        self.assertIsNone(discovery.reason)
        self.assertEqual(discovery.capabilities, frozenset())

    def test_discovery_failure_reports_reason(self):
        adapter = self.adapter()
        with patch.object(adapter, "_probe", return_value=(False, "probe failed")):
            discovery = adapter.discover()

        self.assertEqual(discovery.runtime, "codex-cli")
        self.assertFalse(discovery.available)
        self.assertIsNone(discovery.version)
        self.assertEqual(discovery.reason, "probe failed")
        self.assertEqual(discovery.capabilities, frozenset())

    def test_probe_uses_version_flag_with_minimal_env(self):
        completed = subprocess.CompletedProcess(
            args=["codex", "--version"], returncode=0,
            stdout="codex-cli 0.20.0\n", stderr="")

        class ProbeSpy:
            calls = []

            @classmethod
            def run(cls, argv, **kwargs):
                cls.calls.append((argv, kwargs))
                return completed

        with patch("codex_adapter.subprocess.run", new=ProbeSpy.run):
            ok, detail = self.adapter()._probe()

        self.assertTrue(ok)
        self.assertEqual(detail, "codex-cli 0.20.0")
        argv, kwargs = ProbeSpy.calls[0]
        self.assertEqual(argv, ["codex", "--version"])
        self.assertFalse(kwargs["shell"])
        # Whitelist env: only present allow-listed keys ever reach the child.
        self.assertIn("PATH", kwargs["env"])
        self.assertLessEqual(
            set(kwargs["env"]),
            {"PATH", "HOME", "USERPROFILE", "SYSTEMROOT"})

    def test_probe_failure_redacts_secret_material(self):
        completed = subprocess.CompletedProcess(
            args=["codex", "--version"], returncode=1,
            stdout="", stderr="token=super-secret-value")

        with patch("codex_adapter.subprocess.run", return_value=completed):
            ok, detail = self.adapter()._probe()

        self.assertFalse(ok)
        self.assertNotIn("super-secret-value", detail)
        self.assertIn("[REDACTED]", detail)

    def test_missing_executable_is_unavailable(self):
        with patch("codex_adapter.shutil.which", return_value=None):
            adapter = CodexAdapter.from_environment(profile=self.profile())

        self.assertIsNone(adapter)

    def test_three_method_protocol_conformance(self):
        # Level B 的协议面就是这三个方法；health 三方法的有意缺席
        # 由其不存在来证明（否则 Level B 语义被破坏）。
        for name in ("discover", "invoke", "cancel"):
            self.assertTrue(callable(getattr(CodexAdapter, name)))
        for health in ("check_authentication", "check_provider_model",
                       "minimal_health_check"):
            self.assertFalse(hasattr(CodexAdapter, health))


if __name__ == "__main__":
    unittest.main()
