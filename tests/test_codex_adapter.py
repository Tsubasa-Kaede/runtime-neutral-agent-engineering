"""CodexAdapter 的离线、确定性测试。

全部用 fake 进程对象替身驱动：不调用真实 codex、不访问网络、
不读取凭据，也不依赖本机是否安装了 Codex CLI。仅有的真实
subprocess 调用点（_probe 的 subprocess.run、invoke 的 Popen）
全部被 patch。
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

from codex_adapter import CodexAdapter
from external_runtime import ExternalAgentRequest, InvocationStatus, RuntimeProfile
from runtime_status import AuthenticationState, ReasonCode


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
    def profile(self, provider="openai", model=None):
        return RuntimeProfile(
            agent_id="coding-agent",
            runtime="codex-cli",
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

    def adapter(self):
        return CodexAdapter(profile=self.profile(), executable="codex")

    def test_invoke_sends_prompt_through_stdin_not_argv(self):
        # R6-C5 contract: the instructions ride via stdin (the CLI's
        # documented non-interactive form — "instructions are read from
        # stdin"; also the pi/gemini family shape). argv carries ONLY the
        # exec subcommand and optional flags, never the prompt payload.
        process = FakeProcess()
        with patch("codex_adapter.subprocess.Popen", return_value=process) as popen:
            result = self.adapter().invoke(self.request())

        self.assertEqual(result.status, InvocationStatus.SUCCESS)
        self.assertEqual(result.output, "ok")
        self.assertEqual(result.trace.runtime, "codex-cli")
        self.assertEqual(result.trace.status, InvocationStatus.SUCCESS)
        self.assertEqual(result.trace.exit_code, 0)
        # The prompt rides through stdin: communicate() must pass it.
        self.assertEqual(process.calls, [("Return exactly OK.", 3)])
        # argv never contains the prompt text.
        argv = popen.call_args.args[0]
        self.assertEqual(argv, ["codex", "exec"])
        self.assertNotIn("Return exactly OK.", argv)
        self.assertFalse(popen.call_args.kwargs["shell"])
        # stdin must be a pipe for the stdin shape to hold.
        self.assertEqual(popen.call_args.kwargs["stdin"], subprocess.PIPE)

    def test_invoke_stdin_carries_prompt_verbatim(self):
        # Long, non-ASCII, newline-bearing prompt text must arrive at the
        # child EXACTLY as written — one source, one wire, no mangling.
        prompt = ("Design 确定性 slug 工具\nline two\nline three "
                  + "padding " * 40 + "end")
        process = FakeProcess()
        with patch("codex_adapter.subprocess.Popen", return_value=process) as popen:
            self.adapter().invoke(
                ExternalAgentRequest(
                    task_id="task-1", prompt=prompt, agent_id="coding-agent",
                    role="coder", provider="test-provider", model=None,
                    timeout_seconds=3))

        self.assertEqual(process.calls[0][0], prompt)
        argv = popen.call_args.args[0]
        for item in argv:
            self.assertNotIn("确定性", item)
            self.assertNotIn("padding", item)

    def test_invoke_appends_model_flag_when_requested(self):
        process = FakeProcess()
        with patch("codex_adapter.subprocess.Popen", return_value=process) as popen:
            result = self.adapter().invoke(self.request(model="test-model"))

        self.assertEqual(result.status, InvocationStatus.SUCCESS)
        argv = popen.call_args.args[0]
        self.assertEqual(argv, ["codex", "exec", "--model", "test-model"])
        # The model flag stays in argv; the prompt still rides stdin.
        self.assertEqual(process.calls, [("Return exactly OK.", 3)])

    def test_invoke_decodes_child_streams_as_utf_8(self):
        process = FakeProcess()
        with patch("codex_adapter.subprocess.Popen", return_value=process) as popen:
            self.adapter().invoke(self.request())

        # GBK 控制台下 text=True 默认按本地码页解码，UTF-8 输出会变
        # 乱码；显式 UTF-8 + replace 与 pi adapter 家族保持一致。
        self.assertEqual(popen.call_args.kwargs.get("encoding"), "utf-8")
        self.assertEqual(popen.call_args.kwargs.get("errors"), "replace")

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

    def test_probe_decodes_child_streams_as_utf_8(self):
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
            self.adapter()._probe()

        _, kwargs = ProbeSpy.calls[0]
        self.assertEqual(kwargs.get("encoding"), "utf-8")
        self.assertEqual(kwargs.get("errors"), "replace")

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

    def test_six_method_protocol_conformance(self):
        # 事实面是全部六个方法：三个协议方法 + 三个 health 方法。
        # "具备方法"不等于 REAL VERIFIED —— 资格只由门控运行授予。
        for name in (
            "discover", "invoke", "cancel",
            "check_authentication", "check_provider_model",
            "minimal_health_check",
        ):
            self.assertTrue(callable(getattr(CodexAdapter, name)))

    # -- check_authentication ------------------------------------------------

    def auth_completed(self, stdout, returncode=0):
        return subprocess.CompletedProcess(
            args=["codex", "login", "status"], returncode=returncode,
            stdout=stdout, stderr="")

    def test_check_authentication_api_key_maps_to_authenticated(self):
        completed = self.auth_completed("Logged in using an API key")
        with patch("codex_adapter.subprocess.run", return_value=completed):
            result = self.adapter().check_authentication()

        self.assertEqual(result.state, AuthenticationState.AUTHENTICATED)
        self.assertEqual(result.method, "api-key")
        self.assertEqual(result.reason_code, ReasonCode.NONE)

    def test_check_authentication_chatgpt_maps_to_authenticated(self):
        completed = self.auth_completed("Logged in using ChatGPT")
        with patch("codex_adapter.subprocess.run", return_value=completed):
            result = self.adapter().check_authentication()

        self.assertEqual(result.state, AuthenticationState.AUTHENTICATED)
        self.assertEqual(result.method, "chatgpt")

    def test_check_authentication_real_form_status_on_stderr_maps_to_authenticated(self):
        # 真实 Codex 0.147.0 形态（2026-08-29 本机实测）：rc=0、stdout
        # 为空、状态行 "Logged in using ChatGPT" 输出在 stderr，且
        # 前置一行 PATH alias WARNING。分类观察面必须覆盖 stderr，
        # 否则真实已登录状态会被误判为 UNKNOWN。
        completed = subprocess.CompletedProcess(
            args=["codex", "login", "status"], returncode=0, stdout="",
            stderr="WARNING: proceeding, even though we could not create "
                   "PATH aliases: Refusing to create helper binaries under "
                   "temporary dir\nLogged in using ChatGPT\n")
        with patch("codex_adapter.subprocess.run", return_value=completed):
            result = self.adapter().check_authentication()

        self.assertEqual(result.state, AuthenticationState.AUTHENTICATED)
        self.assertEqual(result.method, "chatgpt")
        self.assertEqual(result.reason_code, ReasonCode.NONE)

    def test_check_authentication_unknown_method_label_is_none(self):
        completed = self.auth_completed("Logged in using myst")
        with patch("codex_adapter.subprocess.run", return_value=completed):
            result = self.adapter().check_authentication()

        self.assertEqual(result.state, AuthenticationState.AUTHENTICATED)
        self.assertIsNone(result.method)

    def test_check_authentication_not_logged_in_is_auth_required(self):
        completed = self.auth_completed("Not logged in", returncode=0)
        with patch("codex_adapter.subprocess.run", return_value=completed):
            result = self.adapter().check_authentication()

        self.assertEqual(result.state, AuthenticationState.AUTH_REQUIRED)
        self.assertEqual(result.reason_code, ReasonCode.AUTH_REQUIRED)
        self.assertIsNone(result.method)

    def test_check_authentication_nonzero_exit_is_auth_required(self):
        completed = self.auth_completed("", returncode=1)
        with patch("codex_adapter.subprocess.run", return_value=completed):
            result = self.adapter().check_authentication()

        self.assertEqual(result.state, AuthenticationState.AUTH_REQUIRED)
        self.assertEqual(result.reason_code, ReasonCode.AUTH_REQUIRED)

    def test_check_authentication_oserror_is_unknown(self):
        with patch("codex_adapter.subprocess.run", side_effect=OSError("gone")):
            result = self.adapter().check_authentication()

        self.assertEqual(result.state, AuthenticationState.UNKNOWN)
        self.assertEqual(result.reason_code, ReasonCode.PROTOCOL_ERROR)

    def test_check_authentication_timeout_is_unknown(self):
        with patch("codex_adapter.subprocess.run",
                   side_effect=subprocess.TimeoutExpired(cmd="codex", timeout=10)):
            result = self.adapter().check_authentication()

        self.assertEqual(result.state, AuthenticationState.UNKNOWN)
        self.assertEqual(result.reason_code, ReasonCode.PROTOCOL_ERROR)

    def test_check_authentication_unrecognized_output_is_unknown(self):
        completed = self.auth_completed("something entirely different", returncode=0)
        with patch("codex_adapter.subprocess.run", return_value=completed):
            result = self.adapter().check_authentication()

        self.assertEqual(result.state, AuthenticationState.UNKNOWN)
        self.assertEqual(result.reason_code, ReasonCode.PROTOCOL_ERROR)

    def test_check_authentication_argv_and_env_and_no_credential_flags(self):
        completed = self.auth_completed("Not logged in", returncode=1)

        class AuthSpy:
            calls = []

            @classmethod
            def run(cls, argv, **kwargs):
                cls.calls.append((argv, kwargs))
                return completed

        with patch("codex_adapter.subprocess.run", new=AuthSpy.run):
            self.adapter().check_authentication()

        argv, kwargs = AuthSpy.calls[0]
        # Read-only observation surface only: exactly login status, shell off,
        # whitelist env, and none of the credential-writing flags.
        self.assertEqual(argv, ["codex", "login", "status"])
        for flag in ("--with-api-key", "--with-access-token", "--device-auth"):
            self.assertNotIn(flag, argv)
        self.assertFalse(kwargs["shell"])
        self.assertLessEqual(
            set(kwargs["env"]),
            {"PATH", "HOME", "USERPROFILE", "SYSTEMROOT"})

    def test_check_authentication_raw_output_never_leaves_adapter(self):
        stdout = "Logged in using an API key - SENTINEL-raw-stdout-98765"
        completed = self.auth_completed(stdout)
        with patch("codex_adapter.subprocess.run", return_value=completed):
            result = self.adapter().check_authentication()

        self.assertNotIn("SENTINEL-raw-stdout-98765", repr(result))
        self.assertEqual(result.method, "api-key")

    # -- check_provider_model ------------------------------------------------

    def test_check_provider_model_unavailable_before_observed_auth(self):
        result = self.adapter().check_provider_model()

        self.assertEqual(result.provider, "openai")
        self.assertIsNone(result.model)
        self.assertFalse(result.available)
        self.assertEqual(result.reason_code, ReasonCode.PROVIDER_UNREACHABLE)

    def test_check_provider_model_available_after_observed_auth(self):
        adapter = self.adapter()
        completed = self.auth_completed("Logged in using ChatGPT")
        with patch("codex_adapter.subprocess.run", return_value=completed):
            adapter.check_authentication()
        result = adapter.check_provider_model()

        self.assertEqual(result.provider, "openai")
        self.assertTrue(result.available)
        self.assertEqual(result.reason_code, ReasonCode.NONE)

    def test_check_provider_model_without_provider_is_unsupported(self):
        with patch("codex_adapter.subprocess.run") as run:
            adapter = CodexAdapter(profile=self.profile(provider=None), executable="codex")
            result = adapter.check_provider_model()

        self.assertIsNone(result.provider)
        self.assertFalse(result.available)
        self.assertEqual(result.reason_code, ReasonCode.UNSUPPORTED_HEALTH_CHECK)
        run.assert_not_called()

    def test_check_provider_model_passes_model_through_without_guessing(self):
        adapter = CodexAdapter(
            profile=self.profile(provider="openai", model="test-model"),
            executable="codex")
        completed = self.auth_completed("Logged in using ChatGPT")
        with patch("codex_adapter.subprocess.run", return_value=completed):
            adapter.check_authentication()
        result = adapter.check_provider_model()

        self.assertEqual(result.model, "test-model")

    def test_check_provider_model_never_spawns_a_subprocess(self):
        adapter = self.adapter()
        with patch("codex_adapter.subprocess.run") as run, \
                patch("codex_adapter.subprocess.Popen") as popen:
            adapter.check_provider_model()

        run.assert_not_called()
        popen.assert_not_called()

    # -- minimal_health_check ------------------------------------------------

    def test_minimal_health_check_skips_without_real_gate(self):
        env = {k: v for k, v in os.environ.items() if k != "RUN_REAL_PROVIDER_TESTS"}
        with patch.dict(os.environ, env, clear=True):
            result = self.adapter().minimal_health_check(timeout_seconds=5)

        self.assertFalse(result.passed)
        self.assertEqual(result.reason_code, ReasonCode.UNSUPPORTED_HEALTH_CHECK)
        self.assertEqual(result.output_class, "skipped")

    def test_minimal_health_check_exact_ok_when_gated(self):
        with patch.dict(os.environ, {"RUN_REAL_PROVIDER_TESTS": "1"}):
            with patch("codex_adapter.subprocess.Popen",
                       return_value=FakeProcess(stdout="OK\n")):
                result = self.adapter().minimal_health_check(timeout_seconds=5)

        self.assertTrue(result.passed)
        self.assertEqual(result.reason_code, ReasonCode.NONE)
        self.assertEqual(result.output_class, "exact_ok")

    def test_minimal_health_check_unexpected_output_is_not_passed(self):
        with patch.dict(os.environ, {"RUN_REAL_PROVIDER_TESTS": "1"}):
            with patch("codex_adapter.subprocess.Popen",
                       return_value=FakeProcess(stdout="Sure thing\n")):
                result = self.adapter().minimal_health_check(timeout_seconds=5)

        self.assertFalse(result.passed)
        self.assertEqual(result.reason_code, ReasonCode.PROTOCOL_ERROR)
        self.assertEqual(result.output_class, "unexpected_response")

    def test_minimal_health_check_failed_invoke_is_not_passed(self):
        with patch.dict(os.environ, {"RUN_REAL_PROVIDER_TESTS": "1"}):
            with patch("codex_adapter.subprocess.Popen",
                       return_value=FakeProcess(stdout="", stderr="bad", returncode=2)):
                result = self.adapter().minimal_health_check(timeout_seconds=5)

        self.assertFalse(result.passed)
        self.assertEqual(result.reason_code, ReasonCode.HEALTH_CHECK_FAILED)
        self.assertEqual(result.output_class, "invoke_failed")

    def test_minimal_health_check_timeout_is_not_passed(self):
        class TimeoutProcess(FakeProcess):
            def communicate(self, input=None, timeout=None):
                raise subprocess.TimeoutExpired(cmd="codex", timeout=timeout)

        with patch.dict(os.environ, {"RUN_REAL_PROVIDER_TESTS": "1"}):
            with patch("codex_adapter.subprocess.Popen", return_value=TimeoutProcess()):
                result = self.adapter().minimal_health_check(timeout_seconds=5)

        self.assertFalse(result.passed)
        self.assertEqual(result.reason_code, ReasonCode.TIMEOUT)
        self.assertEqual(result.output_class, "timeout")

    def test_minimal_health_check_unavailable_is_not_passed(self):
        with patch.dict(os.environ, {"RUN_REAL_PROVIDER_TESTS": "1"}):
            with patch("codex_adapter.subprocess.Popen", side_effect=OSError("no codex")):
                result = self.adapter().minimal_health_check(timeout_seconds=5)

        self.assertFalse(result.passed)
        self.assertEqual(result.reason_code, ReasonCode.CLI_START_FAILED)
        self.assertEqual(result.output_class, "runtime_unavailable")

    def test_minimal_health_check_cancelled_is_not_passed(self):
        adapter = self.adapter()
        started = threading.Event()
        released = threading.Event()

        class BlockingProcess(FakeProcess):
            def communicate(self, input=None, timeout=None):
                started.set()
                released.wait(timeout=5)
                self.returncode = -9
                return "", ""

            def kill(self):
                super().kill()
                released.set()

        results = []

        def run_health():
            with patch.dict(os.environ, {"RUN_REAL_PROVIDER_TESTS": "1"}), \
                    patch("codex_adapter.subprocess.Popen", return_value=BlockingProcess()):
                results.append(adapter.minimal_health_check(timeout_seconds=5))

        worker = threading.Thread(target=run_health)
        worker.start()
        self.assertTrue(started.wait(timeout=2))
        invocation_id = adapter.last_invocation_id
        self.assertIsNotNone(invocation_id)
        adapter.cancel(invocation_id)
        worker.join(timeout=2)

        self.assertFalse(results[0].passed)
        self.assertEqual(results[0].reason_code, ReasonCode.HEALTH_CHECK_FAILED)
        self.assertEqual(results[0].output_class, "invoke_failed")


if __name__ == "__main__":
    unittest.main()
