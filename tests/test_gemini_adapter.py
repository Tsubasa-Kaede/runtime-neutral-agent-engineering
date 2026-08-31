"""GeminiAdapter 的离线、确定性测试（R3 — TDD RED→GREEN）。

本文件先于 gemini_adapter.py 存在（RED：模块缺席时 collection 失败）。
全部用 fake 进程对象替身驱动：不调用真实 gemini、不访问网络、
不读取凭据，也不依赖本机是否安装了 Gemini CLI。仅有的真实
subprocess 调用点（_probe 与 check_authentication 的 subprocess.run、
invoke 的 Popen）全部被 patch。

Gemini CLI 非交互形态（本 adapter 的目标面，均离线 fixture 证明，
不做任何 REAL 断言）：
- 非交互调用：`gemini -p <prompt>`（prompt 也可以经 stdin 传入，
  本 adapter 选择 stdin —— 与 claude/pi 家族一致，规避 argv 长度
  限制，且 prompt 永不进入 shell 可达位置）
- 机器可读输出：`--output-format json`（CLI 的 JSON 封装）
- 只读 auth 观测：Gemini CLI 自身的 auth 状态命令输出（分类化
  JSON / 文本，绝不使用任何凭据打印面）
"""
import json
import subprocess
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from gemini_adapter import GeminiAdapter
from external_runtime import ExternalAgentRequest, InvocationStatus, RuntimeProfile
from runtime_status import AuthenticationState, ReasonCode


class FakeProcess:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.pid = 654
        self.killed = False
        self.calls = []

    def communicate(self, input=None, timeout=None):
        self.calls.append((input, timeout))
        return self.stdout, self.stderr

    def kill(self):
        self.killed = True


class GeminiAdapterTests(unittest.TestCase):
    def profile(self, provider="google"):
        return RuntimeProfile(
            agent_id="coding-agent",
            runtime="gemini-cli",
            provider=provider,
            model=None,
            role="coder",
            capabilities=frozenset(),
        )

    def request(self, timeout_seconds=3, model=None):
        return ExternalAgentRequest(
            task_id="task-1",
            prompt="Return exactly OK and nothing else.",
            agent_id="coding-agent",
            role="coder",
            provider="test-provider",
            model=model,
            timeout_seconds=timeout_seconds,
        )

    def adapter(self, provider="google"):
        return GeminiAdapter(profile=self.profile(provider), executable="gemini")

    @staticmethod
    def gemini_json(result_text="ok", usage=None):
        """gemini --output-format json 封装的离线 fixture 形态。"""
        payload = {"response": result_text}
        if usage is not None:
            payload["usage"] = usage
        return json.dumps(payload) + "\n"

    # -- 六方法契约 ----------------------------------------------------------

    def test_six_method_protocol_conformance(self):
        # 事实面是全部六个方法：三个协议方法 + 三个 health 方法。
        # "具备方法"不等于 REAL VERIFIED —— 资格只由门控运行授予。
        for name in (
            "discover", "invoke", "cancel",
            "check_authentication", "check_provider_model",
            "minimal_health_check",
        ):
            self.assertTrue(callable(getattr(GeminiAdapter, name)), name)

    # -- invoke：argv / stdin / env / UTF-8 ------------------------------------

    def test_invoke_uses_print_mode_argv_with_stdin_prompt(self):
        process = FakeProcess(stdout=self.gemini_json("ok"))
        with patch("gemini_adapter.subprocess.Popen", return_value=process) as popen:
            result = self.adapter().invoke(self.request())

        self.assertEqual(result.status, InvocationStatus.SUCCESS)
        self.assertEqual(result.output, "ok")
        self.assertEqual(result.trace.runtime, "gemini-cli")
        self.assertEqual(result.trace.status, InvocationStatus.SUCCESS)
        self.assertEqual(result.trace.exit_code, 0)
        # prompt 经 stdin 传入（不进 argv，永不进入 shell 可达位置）
        self.assertEqual(process.calls, [("Return exactly OK and nothing else.", 3)])
        argv = popen.call_args.args[0]
        self.assertEqual(argv, [
            "gemini", "-p", "--output-format", "json",
        ])
        self.assertFalse(popen.call_args.kwargs["shell"])

    def test_invoke_appends_model_flag_when_requested(self):
        process = FakeProcess(stdout=self.gemini_json("ok"))
        with patch("gemini_adapter.subprocess.Popen", return_value=process) as popen:
            self.adapter().invoke(self.request(model="test-model"))

        argv = popen.call_args.args[0]
        self.assertEqual(argv, [
            "gemini", "-p", "--output-format", "json", "--model", "test-model",
        ])

    def test_invoke_env_is_whitelist_only(self):
        process = FakeProcess(stdout=self.gemini_json("ok"))
        with patch("gemini_adapter.subprocess.Popen", return_value=process) as popen:
            self.adapter().invoke(self.request())

        env = popen.call_args.kwargs["env"]
        self.assertIsInstance(env, dict)
        self.assertIn("PATH", env)
        self.assertLessEqual(
            set(env), {"PATH", "HOME", "USERPROFILE", "SYSTEMROOT"})
        for var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY",
                    "GOOGLE_API_KEY", "DEEPSEEK_API_KEY"):
            self.assertNotIn(var, env)

    def test_invoke_decodes_child_streams_as_utf_8(self):
        process = FakeProcess(stdout=self.gemini_json("ok"))
        with patch("gemini_adapter.subprocess.Popen", return_value=process) as popen:
            self.adapter().invoke(self.request())

        self.assertEqual(popen.call_args.kwargs.get("encoding"), "utf-8")
        self.assertEqual(popen.call_args.kwargs.get("errors"), "replace")

    def test_invoke_survives_non_ascii_stdout(self):
        # errors="replace"：解码绝不抛异常；调用本身必须成功收尾。
        process = FakeProcess(stdout=self.gemini_json("résumé → 中文 ✓"))
        with patch("gemini_adapter.subprocess.Popen", return_value=process):
            result = self.adapter().invoke(self.request())

        self.assertEqual(result.status, InvocationStatus.SUCCESS)
        self.assertEqual(result.output, "résumé → 中文 ✓")

    def test_output_unparseable_falls_back_to_raw_text(self):
        # JSON 封装无法解析时按原始文本原样返回 —— 该文本是否可用、
        # 是否安全，仍由上游 packet 验证与内容扫描说了算。
        process = FakeProcess(stdout="just plain text\n")
        with patch("gemini_adapter.subprocess.Popen", return_value=process):
            result = self.adapter().invoke(self.request())

        self.assertEqual(result.status, InvocationStatus.SUCCESS)
        self.assertEqual(result.output, "just plain text")

    # -- 超时 / 取消 / 失败语义 ---------------------------------------------------

    def test_timeout_kills_process_and_records_timeout(self):
        class TimeoutProcess(FakeProcess):
            def communicate(self, input=None, timeout=None):
                raise subprocess.TimeoutExpired(cmd="gemini", timeout=timeout)

        process = TimeoutProcess()
        with patch("gemini_adapter.subprocess.Popen", return_value=process):
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
        with patch("gemini_adapter.subprocess.Popen", return_value=process):
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

    def test_cancel_unknown_invocation_is_unavailable(self):
        result = self.adapter().cancel("unknown")

        self.assertEqual(result.status, InvocationStatus.UNAVAILABLE)

    def test_cancel_kills_active_process(self):
        adapter = self.adapter()
        process = FakeProcess()
        adapter._processes["invocation-1"] = process

        result = adapter.cancel("invocation-1")

        self.assertEqual(result.status, InvocationStatus.CANCELLED)
        self.assertTrue(process.killed)
        self.assertIn("invocation-1", adapter._cancelled)

    def test_nonzero_exit_is_failed_and_does_not_report_success(self):
        process = FakeProcess(stdout="", stderr="bad", returncode=2)
        with patch("gemini_adapter.subprocess.Popen", return_value=process):
            result = self.adapter().invoke(self.request())

        self.assertEqual(result.status, InvocationStatus.FAILED)
        self.assertEqual(result.error, "bad")
        self.assertEqual(result.trace.status, InvocationStatus.FAILED)
        self.assertEqual(result.trace.exit_code, 2)

    def test_nonzero_exit_stderr_is_redacted(self):
        process = FakeProcess(
            stdout="", stderr="api_key=sk-live-secret123456 token: tok-xyz987654",
            returncode=1)
        with patch("gemini_adapter.subprocess.Popen", return_value=process):
            result = self.adapter().invoke(self.request())

        self.assertEqual(result.status, InvocationStatus.FAILED)
        self.assertNotIn("sk-live-secret123456", result.error)
        self.assertNotIn("tok-xyz987654", result.error)
        self.assertIn("[REDACTED]", result.error)

    def test_oserror_failure_is_sanitized_not_raw(self):
        with patch("gemini_adapter.subprocess.Popen",
                   side_effect=OSError("no such file api_key=raw-secret-value")):
            result = self.adapter().invoke(self.request())

        self.assertEqual(result.status, InvocationStatus.UNAVAILABLE)
        self.assertNotIn("raw-secret-value", result.error)
        self.assertIn("[REDACTED]", result.error)

    def test_successful_invoke_releases_process_slot(self):
        adapter = self.adapter()
        process = FakeProcess(stdout=self.gemini_json("ok"))
        with patch("gemini_adapter.subprocess.Popen", return_value=process):
            adapter.invoke(self.request())

        self.assertEqual(adapter._processes, {})

    # -- trace 诚实性 ------------------------------------------------------------

    def test_trace_keeps_identity_fields_separate_and_tokens_unknown(self):
        process = FakeProcess(stdout=self.gemini_json("ok"))
        with patch("gemini_adapter.subprocess.Popen", return_value=process):
            result = self.adapter().invoke(self.request())

        self.assertEqual(result.trace.agent_id, "coding-agent")
        self.assertEqual(result.trace.runtime, "gemini-cli")
        self.assertEqual(result.trace.provider, "test-provider")
        self.assertEqual(result.trace.model, None)
        self.assertEqual(result.trace.role, "coder")
        # usage 缺失（fixture 无 usage 键）→ 诚实 unknown，绝不 0。
        self.assertEqual(result.trace.input_tokens, "unknown")
        self.assertEqual(result.trace.output_tokens, "unknown")

    # -- usage 防御式解析（离线 fixture 证据支撑 CAPTURE）--------------------------

    def test_valid_usage_is_captured_exactly(self):
        process = FakeProcess(stdout=self.gemini_json(
            "ok", usage={"input_tokens": 150, "output_tokens": 70}))
        with patch("gemini_adapter.subprocess.Popen", return_value=process):
            result = self.adapter().invoke(self.request())

        self.assertEqual(result.status, InvocationStatus.SUCCESS)
        self.assertEqual(result.trace.input_tokens, 150)
        self.assertEqual(result.trace.output_tokens, 70)

    def test_missing_usage_stays_unknown(self):
        process = FakeProcess(stdout=self.gemini_json("ok"))
        with patch("gemini_adapter.subprocess.Popen", return_value=process):
            result = self.adapter().invoke(self.request())

        self.assertEqual(result.status, InvocationStatus.SUCCESS)
        self.assertEqual(result.trace.input_tokens, "unknown")
        self.assertEqual(result.trace.output_tokens, "unknown")

    def test_malformed_usage_stays_unknown_not_zero(self):
        process = FakeProcess(stdout=self.gemini_json(
            "ok", usage={"input_tokens": "lots", "output_tokens": -5}))
        with patch("gemini_adapter.subprocess.Popen", return_value=process):
            result = self.adapter().invoke(self.request())

        self.assertEqual(result.status, InvocationStatus.SUCCESS)
        self.assertNotEqual(result.trace.input_tokens, 0)
        self.assertNotEqual(result.trace.output_tokens, 0)
        self.assertEqual(result.trace.input_tokens, "unknown")
        self.assertEqual(result.trace.output_tokens, "unknown")

    def test_partial_usage_is_not_fabricated(self):
        # 只有 input_tokens 合法：可观测的一侧如实上报，另一侧保持
        # unknown —— 绝不补 0。
        process = FakeProcess(stdout=self.gemini_json(
            "ok", usage={"input_tokens": 90}))
        with patch("gemini_adapter.subprocess.Popen", return_value=process):
            result = self.adapter().invoke(self.request())

        self.assertEqual(result.status, InvocationStatus.SUCCESS)
        self.assertEqual(result.trace.input_tokens, 90)
        self.assertEqual(result.trace.output_tokens, "unknown")

    def test_bool_shaped_usage_is_rejected(self):
        # True/False 在 Python 里是 int：诚实的解析器必须拒收。
        process = FakeProcess(stdout=self.gemini_json(
            "ok", usage={"input_tokens": True, "output_tokens": False}))
        with patch("gemini_adapter.subprocess.Popen", return_value=process):
            result = self.adapter().invoke(self.request())

        self.assertEqual(result.status, InvocationStatus.SUCCESS)
        self.assertEqual(result.trace.input_tokens, "unknown")
        self.assertEqual(result.trace.output_tokens, "unknown")

    def test_usage_parse_failure_never_fails_invocation(self):
        process = FakeProcess(stdout="not json at all \x00\xff")
        with patch("gemini_adapter.subprocess.Popen", return_value=process):
            result = self.adapter().invoke(self.request())

        self.assertEqual(result.status, InvocationStatus.SUCCESS)
        self.assertEqual(result.trace.input_tokens, "unknown")

    # -- discovery -----------------------------------------------------------------

    def test_discovery_reports_version_without_capability_claim(self):
        completed = subprocess.CompletedProcess(
            args=["gemini", "--version"], returncode=0,
            stdout="gemini-cli 0.8.1\n", stderr="")

        class ProbeSpy:
            calls = []

            @classmethod
            def run(cls, argv, **kwargs):
                cls.calls.append((argv, kwargs))
                return completed

        with patch("gemini_adapter.subprocess.run", new=ProbeSpy.run):
            discovery = self.adapter().discover()

        self.assertTrue(discovery.available)
        self.assertEqual(discovery.runtime, "gemini-cli")
        self.assertEqual(discovery.version, "gemini-cli 0.8.1")
        self.assertEqual(discovery.capabilities, frozenset())
        argv, kwargs = ProbeSpy.calls[0]
        self.assertEqual(argv, ["gemini", "--version"])
        self.assertFalse(kwargs["shell"])
        self.assertLessEqual(
            set(kwargs["env"]), {"PATH", "HOME", "USERPROFILE", "SYSTEMROOT"})
        self.assertEqual(kwargs.get("encoding"), "utf-8")
        self.assertEqual(kwargs.get("errors"), "replace")

    def test_discovery_failure_reports_reason_without_secrets(self):
        with patch("gemini_adapter.subprocess.run",
                   side_effect=OSError("gone token=super-secret-xyz")):
            discovery = self.adapter().discover()

        self.assertFalse(discovery.available)
        self.assertNotIn("super-secret-xyz", discovery.reason)
        self.assertIn("[REDACTED]", discovery.reason)

    def test_missing_executable_is_honest_absence(self):
        with patch("gemini_adapter.shutil.which", return_value=None):
            adapter = GeminiAdapter.from_environment()

        self.assertIsNone(adapter)

    def test_from_environment_builds_adapter_when_present(self):
        with patch("gemini_adapter.shutil.which",
                   return_value="/fake/bin/gemini"):
            adapter = GeminiAdapter.from_environment()

        self.assertIsNotNone(adapter)
        self.assertEqual(adapter.profile.runtime, "gemini-cli")

    # -- check_authentication：只观测，不猜测 ------------------------------------------

    def test_check_authentication_logged_in_maps_to_authenticated(self):
        completed = subprocess.CompletedProcess(
            args=["gemini", "--help"], returncode=0,
            stdout="logged in", stderr="")

        class AuthSpy:
            calls = []

            @classmethod
            def run(cls, argv, **kwargs):
                cls.calls.append((argv, kwargs))
                return completed

        with patch("gemini_adapter.subprocess.run", new=AuthSpy.run):
            result = self.adapter().check_authentication()

        self.assertEqual(result.state, AuthenticationState.AUTHENTICATED)
        argv, kwargs = AuthSpy.calls[0]
        self.assertIsInstance(argv, list)
        self.assertFalse(kwargs["shell"])
        self.assertLessEqual(
            set(kwargs["env"]), {"PATH", "HOME", "USERPROFILE", "SYSTEMROOT"})

    def test_check_authentication_not_logged_in_is_auth_required(self):
        completed = subprocess.CompletedProcess(
            args=["gemini", "--help"], returncode=0,
            stdout="not logged in", stderr="")
        with patch("gemini_adapter.subprocess.run", return_value=completed):
            result = self.adapter().check_authentication()

        self.assertEqual(result.state, AuthenticationState.AUTH_REQUIRED)
        self.assertEqual(result.reason_code, ReasonCode.AUTH_REQUIRED)

    def test_check_authentication_junk_output_is_unknown_not_faked(self):
        completed = subprocess.CompletedProcess(
            args=["gemini", "--help"], returncode=0,
            stdout="total garbage \x00\x01", stderr="")
        with patch("gemini_adapter.subprocess.run", return_value=completed):
            result = self.adapter().check_authentication()

        self.assertEqual(result.state, AuthenticationState.UNKNOWN)
        self.assertEqual(result.reason_code, ReasonCode.PROTOCOL_ERROR)

    def test_check_authentication_subprocess_failure_is_unknown(self):
        with patch("gemini_adapter.subprocess.run",
                   side_effect=OSError("gone")):
            result = self.adapter().check_authentication()

        self.assertEqual(result.state, AuthenticationState.UNKNOWN)
        self.assertEqual(result.reason_code, ReasonCode.PROTOCOL_ERROR)

    # -- check_provider_model：以观测 auth 为门 ---------------------------------------

    def test_check_provider_model_unavailable_before_observed_auth(self):
        check = self.adapter().check_provider_model()

        self.assertFalse(check.available)
        self.assertEqual(check.reason_code, ReasonCode.PROVIDER_UNREACHABLE)

    def test_check_provider_model_available_after_observed_auth(self):
        adapter = self.adapter()
        completed = subprocess.CompletedProcess(
            args=["gemini", "--help"], returncode=0,
            stdout="logged in", stderr="")
        with patch("gemini_adapter.subprocess.run", return_value=completed):
            adapter.check_authentication()
        # provider 检查不再 spawn 子进程：它由已观测的 auth 推导。
        with patch("gemini_adapter.subprocess.run",
                   side_effect=AssertionError("must not probe")):
            check = adapter.check_provider_model()

        self.assertTrue(check.available)
        self.assertEqual(check.reason_code, ReasonCode.NONE)

    def test_check_provider_model_without_provider_is_unsupported(self):
        check = self.adapter(provider=None).check_provider_model()

        self.assertFalse(check.available)
        self.assertEqual(check.reason_code, ReasonCode.UNSUPPORTED_HEALTH_CHECK)

    # -- minimal_health_check：opt-in 且诚实 --------------------------------------------

    def test_minimal_health_check_skips_without_real_gate(self):
        import os
        env = {k: v for k, v in os.environ.items() if k != "RUN_REAL_PROVIDER_TESTS"}
        with patch.dict(os.environ, env, clear=True):
            result = self.adapter().minimal_health_check(timeout_seconds=5)

        self.assertFalse(result.passed)
        self.assertEqual(result.reason_code, ReasonCode.UNSUPPORTED_HEALTH_CHECK)
        self.assertEqual(result.output_class, "skipped")

    def test_minimal_health_check_exact_ok_when_gated(self):
        import os
        process = FakeProcess(stdout=self.gemini_json("OK"))
        with patch.dict(os.environ, {"RUN_REAL_PROVIDER_TESTS": "1"}):
            with patch("gemini_adapter.subprocess.Popen", return_value=process):
                result = self.adapter().minimal_health_check(timeout_seconds=5)

        self.assertTrue(result.passed)
        self.assertEqual(result.reason_code, ReasonCode.NONE)
        self.assertEqual(result.output_class, "exact_ok")

    def test_minimal_health_check_unexpected_output_is_not_passed(self):
        import os
        process = FakeProcess(stdout=self.gemini_json("something else"))
        with patch.dict(os.environ, {"RUN_REAL_PROVIDER_TESTS": "1"}):
            with patch("gemini_adapter.subprocess.Popen", return_value=process):
                result = self.adapter().minimal_health_check(timeout_seconds=5)

        self.assertFalse(result.passed)
        self.assertEqual(result.reason_code, ReasonCode.PROTOCOL_ERROR)
        self.assertEqual(result.output_class, "unexpected_response")


if __name__ == "__main__":
    unittest.main()
