"""ClaudeCodeAdapter 的离线、确定性测试（安全/生命周期家族对齐）。

本文件锁定 10H-D 后只读审查发现的 P0/P1 缺口：Claude adapter 必须与
codex/pi adapter 家族在错误脱敏（_safe_error）、最小 env 白名单
（probe/auth 子进程）、UTF-8 显式解码、并发/取消簿记（_state_lock /
_cancelled / _completed）、超时分支取消复查、cancel 的 OSError 保护
上逐行同构。

全部用 fake 进程对象替身驱动：不调用真实 claude、不访问网络、
不读取凭据，也不依赖本机是否安装了 Claude Code CLI。仅有的真实
subprocess 调用点（_probe 与 check_authentication 的 subprocess.run、
invoke 的 Popen）全部被 patch。
"""
import os
import subprocess
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from claude_code_adapter import ClaudeCodeAdapter
from external_runtime import ExternalAgentRequest, InvocationStatus, RuntimeProfile


class FakeProcess:
    def __init__(self, stdout="ok\n", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.pid = 321
        self.killed = False
        self.calls = []

    def communicate(self, input=None, timeout=None):
        self.calls.append((input, timeout))
        return self.stdout, self.stderr

    def kill(self):
        self.killed = True


class ClaudeCodeAdapterTests(unittest.TestCase):
    def profile(self, provider=None, model=None):
        return RuntimeProfile(
            agent_id="coding-agent",
            runtime="claude-cli",
            provider=provider,
            model=model,
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

    def adapter(self, provider=None):
        return ClaudeCodeAdapter(profile=self.profile(provider), executable="claude")

    # -- _safe_error：错误脱敏（P0-1） ---------------------------------------

    def test_safe_error_redacts_common_secret_forms(self):
        error = (
            'api_key=alpha token: beta secret=gamma '
            'Authorization: Bearer delta '
            '"token":"json-secret" hf_1234567890 sk-abcdefghij'
        )

        redacted = ClaudeCodeAdapter._safe_error(error)

        for secret in (
            "alpha", "beta", "gamma", "delta", "json-secret",
            "hf_1234567890", "sk-abcdefghij",
        ):
            self.assertNotIn(secret, redacted)
        self.assertGreaterEqual(redacted.count("[REDACTED]"), 7)

    def test_nonzero_exit_failure_redacts_credential_material_in_stderr(self):
        process = FakeProcess(
            stdout="",
            stderr="failed because token=super-secret-value leaked",
            returncode=2,
        )
        with patch("claude_code_adapter.subprocess.Popen", return_value=process):
            result = self.adapter().invoke(self.request())

        self.assertEqual(result.status, InvocationStatus.FAILED)
        self.assertNotIn("super-secret-value", result.error)
        self.assertNotIn("super-secret-value", result.trace.error)
        self.assertIn("[REDACTED]", result.error)

    def test_nonzero_exit_failure_without_stderr_reports_family_error(self):
        process = FakeProcess(stdout="", stderr="", returncode=2)
        with patch("claude_code_adapter.subprocess.Popen", return_value=process):
            result = self.adapter().invoke(self.request())

        self.assertEqual(result.status, InvocationStatus.FAILED)
        self.assertEqual(result.error, "external runtime failed")

    def test_oserror_failure_is_sanitized_not_raw(self):
        with patch("claude_code_adapter.subprocess.Popen",
                   side_effect=OSError("spawn failed token=raw-secret-value")):
            result = self.adapter().invoke(self.request())

        self.assertEqual(result.status, InvocationStatus.UNAVAILABLE)
        self.assertNotIn("raw-secret-value", result.error)
        self.assertIn("[REDACTED]", result.error)

    # -- _probe：最小 env + UTF-8 + 脱敏（P0-2 / P1-2） ----------------------

    def test_probe_uses_version_flag_with_minimal_env(self):
        completed = subprocess.CompletedProcess(
            args=["claude", "--version"], returncode=0,
            stdout="2.1.0 (Claude Code)\n", stderr="")

        class ProbeSpy:
            calls = []

            @classmethod
            def run(cls, argv, **kwargs):
                cls.calls.append((argv, kwargs))
                return completed

        with patch("claude_code_adapter.subprocess.run", new=ProbeSpy.run):
            ok, detail = self.adapter()._probe()

        self.assertTrue(ok)
        self.assertEqual(detail, "2.1.0 (Claude Code)")
        argv, kwargs = ProbeSpy.calls[0]
        self.assertEqual(argv, ["claude", "--version"])
        self.assertFalse(kwargs["shell"])
        # Whitelist env: only present allow-listed keys ever reach the child.
        self.assertIn("PATH", kwargs["env"])
        self.assertLessEqual(
            set(kwargs["env"]),
            {"PATH", "HOME", "USERPROFILE", "SYSTEMROOT"})

    def test_probe_decodes_child_streams_as_utf_8(self):
        completed = subprocess.CompletedProcess(
            args=["claude", "--version"], returncode=0,
            stdout="2.1.0 (Claude Code)\n", stderr="")

        class ProbeSpy:
            calls = []

            @classmethod
            def run(cls, argv, **kwargs):
                cls.calls.append((argv, kwargs))
                return completed

        with patch("claude_code_adapter.subprocess.run", new=ProbeSpy.run):
            self.adapter()._probe()

        _, kwargs = ProbeSpy.calls[0]
        self.assertEqual(kwargs.get("encoding"), "utf-8")
        self.assertEqual(kwargs.get("errors"), "replace")

    def test_probe_failure_redacts_secret_material(self):
        completed = subprocess.CompletedProcess(
            args=["claude", "--version"], returncode=1,
            stdout="", stderr="token=super-secret-value")
        with patch("claude_code_adapter.subprocess.run", return_value=completed):
            ok, detail = self.adapter()._probe()

        self.assertFalse(ok)
        self.assertNotIn("super-secret-value", detail)
        self.assertIn("[REDACTED]", detail)

    def test_probe_subprocess_exception_is_sanitized(self):
        with patch("claude_code_adapter.subprocess.run",
                   side_effect=OSError("no claude api_key=raw-secret-value")):
            ok, detail = self.adapter()._probe()

        self.assertFalse(ok)
        self.assertNotIn("raw-secret-value", detail)
        self.assertIn("[REDACTED]", detail)

    # -- check_authentication：最小 env + UTF-8（P0-2 / P1-2） ---------------

    def test_check_authentication_argv_env_utf8_and_read_only(self):
        completed = subprocess.CompletedProcess(
            args=["claude", "auth", "status", "--json"], returncode=0,
            stdout='{"loggedIn": true, "authMethod": "oauth_token", "apiProvider": "firstParty"}\n',
            stderr="")

        class AuthSpy:
            calls = []

            @classmethod
            def run(cls, argv, **kwargs):
                cls.calls.append((argv, kwargs))
                return completed

        with patch("claude_code_adapter.subprocess.run", new=AuthSpy.run):
            result = self.adapter().check_authentication()

        argv, kwargs = AuthSpy.calls[0]
        self.assertEqual(argv, ["claude", "auth", "status", "--json"])
        self.assertFalse(kwargs["shell"])
        # Whitelist env + explicit UTF-8 decode on the auth observation too.
        self.assertIn("PATH", kwargs["env"])
        self.assertLessEqual(
            set(kwargs["env"]),
            {"PATH", "HOME", "USERPROFILE", "SYSTEMROOT"})
        self.assertEqual(kwargs.get("encoding"), "utf-8")
        self.assertEqual(kwargs.get("errors"), "replace")
        from runtime_status import AuthenticationState
        self.assertEqual(result.state, AuthenticationState.AUTHENTICATED)

    # -- invoke：UTF-8 既有行为不回归 ----------------------------------------

    def test_invoke_decodes_child_streams_as_utf_8(self):
        process = FakeProcess()
        with patch("claude_code_adapter.subprocess.Popen", return_value=process) as popen:
            self.adapter().invoke(self.request())

        self.assertEqual(popen.call_args.kwargs.get("encoding"), "utf-8")
        self.assertEqual(popen.call_args.kwargs.get("errors"), "replace")

    def test_invoke_print_mode_argv_and_prompt_via_stdin(self):
        process = FakeProcess(stdout='{"result": "ok"}\n')
        with patch("claude_code_adapter.subprocess.Popen", return_value=process) as popen:
            result = self.adapter().invoke(self.request())

        self.assertEqual(result.status, InvocationStatus.SUCCESS)
        self.assertEqual(result.output, "ok")
        argv = popen.call_args.args[0]
        self.assertEqual(
            argv,
            ["claude", "--print", "--output-format", "json", "--no-session-persistence"])
        self.assertFalse(popen.call_args.kwargs["shell"])
        self.assertEqual(process.calls, [("Return exactly OK.", 3)])

    # -- 生命周期簿记（P1-1）：锁 / 取消复查 / completed ----------------------

    def test_timeout_kills_process_and_records_timeout(self):
        class TimeoutProcess(FakeProcess):
            def communicate(self, input=None, timeout=None):
                raise subprocess.TimeoutExpired(cmd="claude", timeout=timeout)

        process = TimeoutProcess()
        with patch("claude_code_adapter.subprocess.Popen", return_value=process):
            result = self.adapter().invoke(self.request(timeout_seconds=0.1))

        self.assertEqual(result.status, InvocationStatus.TIMEOUT)
        self.assertEqual(result.trace.status, InvocationStatus.TIMEOUT)
        self.assertTrue(process.killed)

    def test_timeout_after_cancel_reports_cancelled_not_timeout(self):
        # 在超时过程中被取消的调用必须上报 CANCELLED（调用方意图），
        # 而不是 TIMEOUT —— 两者是不同的诚实结果，与 codex/pi 一致。
        adapter = self.adapter()
        started = threading.Event()
        released = threading.Event()

        class TimeoutAfterCancelProcess(FakeProcess):
            def communicate(self, input=None, timeout=None):
                started.set()
                released.wait(timeout=2)
                raise subprocess.TimeoutExpired(cmd="claude", timeout=timeout)

            def kill(self):
                super().kill()
                released.set()

        process = TimeoutAfterCancelProcess()
        results = []
        with patch("claude_code_adapter.subprocess.Popen", return_value=process):
            worker = threading.Thread(
                target=lambda: results.append(adapter.invoke(self.request())))
            worker.start()
            self.assertTrue(started.wait(timeout=1))
            invocation_id = adapter.last_invocation_id
            self.assertIsNotNone(invocation_id)
            cancel_result = adapter.cancel(invocation_id)
            worker.join(timeout=2)

        self.assertEqual(cancel_result.status, InvocationStatus.CANCELLED)
        self.assertEqual(results[0].status, InvocationStatus.CANCELLED)
        self.assertEqual(results[0].trace.status, InvocationStatus.CANCELLED)
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
        with patch("claude_code_adapter.subprocess.Popen", return_value=process):
            worker = threading.Thread(
                target=lambda: results.append(adapter.invoke(self.request())))
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

    def test_cancel_process_vanished_is_unavailable_not_crash(self):
        # kill() 抛 OSError（进程恰好已退出）时不得让异常逃出 cancel()。
        adapter = self.adapter()

        class VanishedProcess(FakeProcess):
            def kill(self):
                raise OSError("process already terminated")

        adapter._processes["invocation-1"] = VanishedProcess()
        result = adapter.cancel("invocation-1")

        self.assertEqual(result.status, InvocationStatus.UNAVAILABLE)
        self.assertEqual(result.error, "invocation is no longer active")

    def test_completed_invocation_cancel_is_unavailable(self):
        adapter = self.adapter()
        process = FakeProcess()
        adapter._processes["invocation-1"] = process
        adapter._completed.add("invocation-1")

        result = adapter.cancel("invocation-1")

        self.assertEqual(result.status, InvocationStatus.UNAVAILABLE)
        self.assertFalse(process.killed)

    def test_invoke_records_invocation_id_for_cancel_path(self):
        adapter = self.adapter()
        with patch("claude_code_adapter.subprocess.Popen",
                   return_value=FakeProcess(stdout='{"result": "ok"}\n')):
            adapter.invoke(self.request())

        self.assertIsNotNone(adapter.last_invocation_id)
        # 已收尾的调用不再占用进程表。
        self.assertEqual(adapter._processes, {})

    def test_six_method_protocol_conformance(self):
        # 事实面是全部六个方法：三个协议方法 + 三个 health 方法。
        # "具备方法"不等于 REAL VERIFIED —— 资格只由门控运行授予。
        for name in (
            "discover", "invoke", "cancel",
            "check_authentication", "check_provider_model",
            "minimal_health_check",
        ):
            self.assertTrue(callable(getattr(ClaudeCodeAdapter, name)))


if __name__ == "__main__":
    unittest.main()
