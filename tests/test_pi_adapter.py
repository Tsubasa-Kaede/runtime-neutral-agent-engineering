"""PiAdapter 的离线、确定性测试。

全部用 fake 进程对象替身驱动：不调用真实 pi、不访问网络、
不读取凭据，也不依赖本机是否安装了 pi CLI。仅有的真实
subprocess 调用点（_probe 与 check_authentication 的
subprocess.run、invoke 的 Popen）全部被 patch。
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from pi_adapter import PiAdapter
from external_runtime import ExternalAgentRequest, InvocationStatus, RuntimeProfile
from runtime_status import AuthenticationState, ReasonCode


def agent_end(*texts):
    """构造一个 agent_end 事件：user 消息 + 每段文本一条 assistant 消息。

    assistant 消息带一个 thinking 部分与一个 text 部分 —— 文本提取
    必须只认 text 部分。
    """
    messages = [{"role": "user", "content": [{"type": "text", "text": "q"}]}]
    for text in texts:
        messages.append({
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "internal"},
                {"type": "text", "text": text},
            ],
        })
    return {"type": "agent_end", "messages": messages}


def stream(*events):
    """构造 pi --mode json 的 JSON-lines stdout：会话头 + 事件序列。"""
    lines = [json.dumps({"type": "session", "version": 3, "id": "u", "cwd": "/tmp"})]
    lines.extend(json.dumps(event) for event in events)
    return "\n".join(lines) + "\n"


class FakeProcess:
    def __init__(self, stdout="ok\n", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.pid = 451
        self.killed = False
        self.calls = []

    def communicate(self, input=None, timeout=None):
        self.calls.append((input, timeout))
        return self.stdout, self.stderr

    def kill(self):
        self.killed = True


class PiAdapterTests(unittest.TestCase):
    def profile(self, provider=None):
        return RuntimeProfile(
            agent_id="coding-agent",
            runtime="pi-cli",
            provider=provider,
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

    def adapter(self, provider=None):
        return PiAdapter(profile=self.profile(provider), executable="pi")

    def test_invoke_uses_print_mode_argv_with_stdin_prompt(self):
        process = FakeProcess(stdout=stream(agent_end("ok")))
        with patch("pi_adapter.subprocess.Popen", return_value=process) as popen:
            result = self.adapter().invoke(self.request())

        self.assertEqual(result.status, InvocationStatus.SUCCESS)
        self.assertEqual(result.output, "ok")
        self.assertEqual(result.trace.runtime, "pi-cli")
        self.assertEqual(result.trace.status, InvocationStatus.SUCCESS)
        self.assertEqual(result.trace.exit_code, 0)
        # The prompt rides through stdin (pi merges piped stdin into the
        # initial prompt in print mode), never through argv.
        self.assertEqual(process.calls, [("Return exactly OK.", 3)])
        argv = popen.call_args.args[0]
        self.assertEqual(argv, [
            "pi", "-p", "--mode", "json", "--no-session", "--no-tools",
            "--no-extensions", "--no-skills", "--no-context-files",
        ])
        self.assertFalse(popen.call_args.kwargs["shell"])

    def test_invoke_appends_model_flag_when_requested(self):
        process = FakeProcess(stdout=stream(agent_end("ok")))
        with patch("pi_adapter.subprocess.Popen", return_value=process) as popen:
            result = self.adapter().invoke(self.request(model="test-model"))

        self.assertEqual(result.status, InvocationStatus.SUCCESS)
        argv = popen.call_args.args[0]
        self.assertEqual(argv, [
            "pi", "-p", "--mode", "json", "--no-session", "--no-tools",
            "--no-extensions", "--no-skills", "--no-context-files",
            "--model", "test-model",
        ])

    def test_invoke_extracts_final_assistant_message_from_agent_end(self):
        process = FakeProcess(stdout=stream(
            {"type": "agent_start"},
            agent_end("Hello", "Final answer"),
        ))
        with patch("pi_adapter.subprocess.Popen", return_value=process):
            result = self.adapter().invoke(self.request())

        self.assertEqual(result.status, InvocationStatus.SUCCESS)
        # Last assistant message wins, and only its text part is taken.
        self.assertEqual(result.output, "Final answer")

    def test_invoke_uses_last_agent_end_event(self):
        process = FakeProcess(stdout=stream(agent_end("first"), agent_end("second")))
        with patch("pi_adapter.subprocess.Popen", return_value=process):
            result = self.adapter().invoke(self.request())

        self.assertEqual(result.status, InvocationStatus.SUCCESS)
        self.assertEqual(result.output, "second")

    def test_invoke_falls_back_to_raw_stdout_without_agent_end(self):
        process = FakeProcess(stdout="plain text\nno agent_end here\n")
        with patch("pi_adapter.subprocess.Popen", return_value=process):
            result = self.adapter().invoke(self.request())

        self.assertEqual(result.status, InvocationStatus.SUCCESS)
        self.assertEqual(result.output, "plain text\nno agent_end here")

    def test_nonzero_exit_is_failed_and_does_not_report_success(self):
        process = FakeProcess(stdout="", stderr="bad", returncode=2)
        with patch("pi_adapter.subprocess.Popen", return_value=process):
            result = self.adapter().invoke(self.request())

        self.assertEqual(result.status, InvocationStatus.FAILED)
        self.assertEqual(result.error, "bad")
        self.assertEqual(result.trace.status, InvocationStatus.FAILED)
        self.assertEqual(result.trace.exit_code, 2)

    def test_timeout_kills_process_and_records_timeout(self):
        class TimeoutProcess(FakeProcess):
            def communicate(self, input=None, timeout=None):
                raise subprocess.TimeoutExpired(cmd="pi", timeout=timeout)

        process = TimeoutProcess()
        with patch("pi_adapter.subprocess.Popen", return_value=process):
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
        with patch("pi_adapter.subprocess.Popen", return_value=process):
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

        redacted = PiAdapter._safe_error(error)

        for secret in (
            "alpha", "beta", "gamma", "delta", "json-secret",
            "hf_1234567890", "sk-abcdefghij",
        ):
            self.assertNotIn(secret, redacted)
        self.assertGreaterEqual(redacted.count("[REDACTED]"), 7)

    def test_trace_keeps_identity_fields_separate_and_tokens_unknown(self):
        with patch(
            "pi_adapter.subprocess.Popen",
            return_value=FakeProcess(stdout=stream(agent_end("ok"))),
        ):
            result = self.adapter().invoke(self.request(model="test-model"))

        self.assertEqual(result.trace.agent_id, "coding-agent")
        self.assertEqual(result.trace.runtime, "pi-cli")
        self.assertEqual(result.trace.provider, "test-provider")
        self.assertEqual(result.trace.model, "test-model")
        self.assertEqual(result.trace.role, "coder")
        self.assertEqual(result.trace.input_tokens, "unknown")
        self.assertEqual(result.trace.output_tokens, "unknown")

    def test_discovery_reports_version_without_capability_claim(self):
        adapter = self.adapter()
        with patch.object(adapter, "_probe", return_value=(True, "pi 0.84.3")):
            discovery = adapter.discover()

        self.assertEqual(discovery.runtime, "pi-cli")
        self.assertTrue(discovery.available)
        self.assertEqual(discovery.version, "pi 0.84.3")
        self.assertIsNone(discovery.reason)
        self.assertEqual(discovery.capabilities, frozenset())

    def test_discovery_failure_reports_reason(self):
        adapter = self.adapter()
        with patch.object(adapter, "_probe", return_value=(False, "probe failed")):
            discovery = adapter.discover()

        self.assertEqual(discovery.runtime, "pi-cli")
        self.assertFalse(discovery.available)
        self.assertIsNone(discovery.version)
        self.assertEqual(discovery.reason, "probe failed")
        self.assertEqual(discovery.capabilities, frozenset())

    def test_probe_uses_version_flag_with_minimal_env(self):
        completed = subprocess.CompletedProcess(
            args=["pi", "--version"], returncode=0,
            stdout="pi 0.84.3\n", stderr="")

        class ProbeSpy:
            calls = []

            @classmethod
            def run(cls, argv, **kwargs):
                cls.calls.append((argv, kwargs))
                return completed

        with patch("pi_adapter.subprocess.run", new=ProbeSpy.run):
            ok, detail = self.adapter()._probe()

        self.assertTrue(ok)
        self.assertEqual(detail, "pi 0.84.3")
        argv, kwargs = ProbeSpy.calls[0]
        self.assertEqual(argv, ["pi", "--version"])
        self.assertFalse(kwargs["shell"])
        # Whitelist env: only present allow-listed keys ever reach the child.
        self.assertIn("PATH", kwargs["env"])
        self.assertLessEqual(
            set(kwargs["env"]),
            {"PATH", "HOME", "USERPROFILE", "SYSTEMROOT",
             "TEMP", "TMP"})

    def test_probe_failure_redacts_secret_material(self):
        completed = subprocess.CompletedProcess(
            args=["pi", "--version"], returncode=1,
            stdout="", stderr="token=super-secret-value")

        with patch("pi_adapter.subprocess.run", return_value=completed):
            ok, detail = self.adapter()._probe()

        self.assertFalse(ok)
        self.assertNotIn("super-secret-value", detail)
        self.assertIn("[REDACTED]", detail)

    def test_missing_executable_is_unavailable(self):
        with patch("pi_adapter.shutil.which", return_value=None):
            adapter = PiAdapter.from_environment(profile=self.profile())

        self.assertIsNone(adapter)

    def test_six_method_protocol_conformance(self):
        # 事实面是全部六个方法：三个协议方法 + 三个 health 方法。
        for name in (
            "discover", "invoke", "cancel",
            "check_authentication", "check_provider_model",
            "minimal_health_check",
        ):
            self.assertTrue(callable(getattr(PiAdapter, name)))

    def test_check_authentication_ready_maps_to_authenticated(self):
        completed = subprocess.CompletedProcess(
            args=["pi", "auth", "check"], returncode=0,
            stdout='{"status":"ready","provider":"anthropic","authType":"oauth"}\n',
            stderr="")

        class AuthSpy:
            calls = []

            @classmethod
            def run(cls, argv, **kwargs):
                cls.calls.append((argv, kwargs))
                return completed

        with patch("pi_adapter.subprocess.run", new=AuthSpy.run):
            result = self.adapter(provider="anthropic").check_authentication()

        self.assertEqual(result.state, AuthenticationState.AUTHENTICATED)
        self.assertEqual(result.method, "oauth")
        self.assertEqual(result.reason_code, ReasonCode.NONE)
        argv, kwargs = AuthSpy.calls[0]
        # Readiness variant only: JSON status, no refresh, and never the
        # credential-printing flags.
        self.assertEqual(
            argv,
            ["pi", "auth", "check", "--provider", "anthropic", "--json", "--no-refresh"])
        self.assertNotIn("--credentials", argv)
        self.assertFalse(kwargs["shell"])
        self.assertLessEqual(
            set(kwargs["env"]),
            {"PATH", "HOME", "USERPROFILE", "SYSTEMROOT",
             "TEMP", "TMP"})

    def test_check_authentication_not_ready_maps_to_auth_required(self):
        completed = subprocess.CompletedProcess(
            args=["pi", "auth", "check"], returncode=1,
            stdout='{"status":"not_ready","provider":"anthropic",'
                   '"reason":"credentials_not_configured"}\n',
            stderr="")

        with patch("pi_adapter.subprocess.run", return_value=completed):
            result = self.adapter(provider="anthropic").check_authentication()

        self.assertEqual(result.state, AuthenticationState.AUTH_REQUIRED)
        self.assertEqual(result.reason_code, ReasonCode.AUTH_REQUIRED)
        # pi 的原始 reason 字符串不进入结果（分类化词汇 only）。
        self.assertIsNone(result.method)

    def test_check_authentication_invalid_state_maps_to_unknown(self):
        completed = subprocess.CompletedProcess(
            args=["pi", "auth", "check"], returncode=2,
            stdout='{"status":"invalid","provider":"anthropic",'
                   '"reason":"invalid_state"}\n',
            stderr="")

        with patch("pi_adapter.subprocess.run", return_value=completed):
            result = self.adapter(provider="anthropic").check_authentication()

        self.assertEqual(result.state, AuthenticationState.UNKNOWN)
        self.assertEqual(result.reason_code, ReasonCode.PROTOCOL_ERROR)

    def test_check_authentication_without_provider_is_unsupported_and_runs_nothing(self):
        with patch("pi_adapter.subprocess.run") as run:
            result = self.adapter().check_authentication()

        self.assertEqual(result.state, AuthenticationState.UNKNOWN)
        self.assertEqual(result.reason_code, ReasonCode.UNSUPPORTED_HEALTH_CHECK)
        # 无法定位 provider 时绝不盲目发起任何探测。
        run.assert_not_called()

    def test_check_authentication_subprocess_failure_is_unknown(self):
        with patch("pi_adapter.subprocess.run", side_effect=OSError("no pi")):
            result = self.adapter(provider="anthropic").check_authentication()

        self.assertEqual(result.state, AuthenticationState.UNKNOWN)
        self.assertEqual(result.reason_code, ReasonCode.PROTOCOL_ERROR)

    def test_check_provider_model_requires_observed_authentication(self):
        adapter = self.adapter(provider="anthropic")
        before = adapter.check_provider_model()
        self.assertFalse(before.available)
        self.assertEqual(before.reason_code, ReasonCode.PROVIDER_UNREACHABLE)

        completed = subprocess.CompletedProcess(
            args=["pi", "auth", "check"], returncode=0,
            stdout='{"status":"ready","provider":"anthropic","authType":"api_key"}\n',
            stderr="")
        with patch("pi_adapter.subprocess.run", return_value=completed):
            adapter.check_authentication()
        after = adapter.check_provider_model()

        self.assertTrue(after.available)
        self.assertEqual(after.provider, "anthropic")
        self.assertEqual(after.reason_code, ReasonCode.NONE)

    def test_check_provider_model_without_provider_is_unsupported(self):
        result = self.adapter().check_provider_model()

        self.assertIsNone(result.provider)
        self.assertFalse(result.available)
        self.assertEqual(result.reason_code, ReasonCode.UNSUPPORTED_HEALTH_CHECK)

    def test_minimal_health_check_skips_without_real_gate(self):
        env = {k: v for k, v in os.environ.items() if k != "RUN_REAL_PROVIDER_TESTS"}
        with patch.dict(os.environ, env, clear=True):
            result = self.adapter(provider="anthropic").minimal_health_check(timeout_seconds=5)

        self.assertFalse(result.passed)
        self.assertEqual(result.reason_code, ReasonCode.UNSUPPORTED_HEALTH_CHECK)
        self.assertEqual(result.output_class, "skipped")

    def test_minimal_health_check_exact_ok_when_gated(self):
        process = FakeProcess(stdout=stream(agent_end("OK")))
        with patch.dict(os.environ, {"RUN_REAL_PROVIDER_TESTS": "1"}):
            with patch("pi_adapter.subprocess.Popen", return_value=process):
                result = self.adapter(provider="anthropic").minimal_health_check(timeout_seconds=5)

        self.assertTrue(result.passed)
        self.assertEqual(result.reason_code, ReasonCode.NONE)
        self.assertEqual(result.output_class, "exact_ok")


class FromEnvironmentDefaultProviderTests(unittest.TestCase):
    """P1-1b：from_environment 的 provider 只来自 Pi 自己的 settings
    （~/.pi/agent/settings.json 的 defaultProvider —— runtime-owned
    声明的观察，不是猜测）。model 恒 None（defaultModel 绝不读）；
    settings 缺失/损坏/空值 → provider=None 诚实跳过；绝不依赖凭据
    （auth.json 若被读取即测试失败）。全部离线：executable 与 settings
    路径均注入。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.settings = self.base / "settings.json"

    def _write_settings(self, payload):
        if isinstance(payload, str):
            self.settings.write_text(payload, encoding="utf-8")
        else:
            self.settings.write_text(json.dumps(payload), encoding="utf-8")

    def _from_environment(self, **kwargs):
        with patch("pi_adapter.shutil.which", return_value="pi"):
            return PiAdapter.from_environment(settings_path=self.settings,
                                              **kwargs)

    def test_settings_default_provider_is_observed(self):
        # A：正常 settings —— provider 来自 defaultProvider。
        self._write_settings({"defaultProvider": "deepseek",
                              "defaultModel": "deepseek-v4-pro"})
        adapter = self._from_environment()
        self.assertIsNotNone(adapter)
        self.assertEqual(adapter.profile.provider, "deepseek")
        self.assertIsNone(adapter.profile.model)

    def test_default_model_never_leaks_into_profile(self):
        # E：defaultModel 在场与否，provider/model 完全一致且 model
        # 恒 None —— 证明 provider discovery 不读 defaultModel。
        self._write_settings({"defaultProvider": "deepseek",
                              "defaultModel": "deepseek-v4-pro"})
        with_model = self._from_environment()
        self._write_settings({"defaultProvider": "deepseek"})
        without_model = self._from_environment()
        self.assertEqual(with_model.profile.provider, "deepseek")
        self.assertIsNone(with_model.profile.model)
        self.assertEqual(
            (without_model.profile.provider, without_model.profile.model),
            (with_model.profile.provider, with_model.profile.model))

    def test_missing_settings_keeps_provider_none(self):
        # B：settings 缺席 → provider=None（诚实跳过，不猜测）。
        adapter = self._from_environment()
        self.assertIsNotNone(adapter)
        self.assertIsNone(adapter.profile.provider)

    def test_malformed_settings_keeps_provider_none_without_raising(self):
        # C：坏 JSON → provider=None，且异常绝不出 discovery 层。
        self._write_settings('{"defaultProvider": "deepseek')
        adapter = self._from_environment()
        self.assertIsNotNone(adapter)
        self.assertIsNone(adapter.profile.provider)

    def test_non_object_settings_keeps_provider_none(self):
        # C 补：合法 JSON 但非对象 → provider=None。
        self._write_settings(["deepseek"])
        adapter = self._from_environment()
        self.assertIsNotNone(adapter)
        self.assertIsNone(adapter.profile.provider)

    def test_empty_default_provider_keeps_provider_none(self):
        # D：空串 / 纯空白 → provider=None。
        for value in ("", "   "):
            self._write_settings({"defaultProvider": value})
            adapter = self._from_environment()
            self.assertIsNotNone(adapter)
            self.assertIsNone(adapter.profile.provider)

    def test_non_string_default_provider_keeps_provider_none(self):
        # D 补：非字符串类型 → provider=None，绝不代造。
        self._write_settings({"defaultProvider": 42})
        adapter = self._from_environment()
        self.assertIsNotNone(adapter)
        self.assertIsNone(adapter.profile.provider)

    def test_provider_discovery_never_reads_credentials(self):
        # F：同目录放一个"若被读取必然失败"的 auth.json（目录形态，
        # 任何 read 都抛 OSError）；provider 仍观察到 deepseek 即证明
        # discovery 完全不依赖凭据。
        self._write_settings({"defaultProvider": "deepseek"})
        (self.base / "auth.json").mkdir()
        adapter = self._from_environment()
        self.assertIsNotNone(adapter)
        self.assertEqual(adapter.profile.provider, "deepseek")

    def test_explicit_profile_wins_over_settings_observation(self):
        # 调用方显式 profile 优先：settings 存在也不改写其 provider。
        self._write_settings({"defaultProvider": "deepseek"})
        explicit = RuntimeProfile(
            agent_id="coding-agent", runtime="pi-cli",
            provider="anthropic", model=None, role="coder",
            capabilities=frozenset())
        adapter = self._from_environment(profile=explicit)
        self.assertIsNotNone(adapter)
        self.assertEqual(adapter.profile.provider, "anthropic")

    def test_missing_executable_returns_none_even_with_settings(self):
        # 可执行文件缺席 = 未安装：即使 settings 声明了 provider 也
        # 诚实缺席（None），绝不半配置。
        self._write_settings({"defaultProvider": "deepseek"})
        with patch("pi_adapter.shutil.which", return_value=None):
            adapter = PiAdapter.from_environment(settings_path=self.settings)
        self.assertIsNone(adapter)


if __name__ == "__main__":
    unittest.main()
