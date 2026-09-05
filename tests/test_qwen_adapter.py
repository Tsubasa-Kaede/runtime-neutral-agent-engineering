"""QwenCodeAdapter 的离线、确定性测试（R1-Step3 — TDD RED→GREEN）。

本文件先于 qwen_adapter.py 存在（RED：模块缺席时 collection 失败）。
全部用 fake 进程对象替身驱动：不调用真实 qwen、不访问网络、不读取
凭据，也不依赖本机是否安装了 Qwen Code。仅有的真实 subprocess 调用点
（_probe 与 check_authentication 的 subprocess.run、invoke 的 Popen）
全部被 patch。

Qwen Code 非交互形态（本 adapter 的目标面，均离线 fixture 证明，
不做任何 REAL 断言；证据=官方 headless 文档 + R1-Step2 边界调查）：
- 非交互调用：prompt 经 stdin 传入（`echo "..." | qwen` 的文档形态；
  不使用 -p —— 与 claude/pi/gemini 家族一致，规避 argv 长度限制，
  且 prompt 永不进入 shell 可达位置。注意 qwen 语义：带 -p 时 stdin
  是"上下文"而非 prompt，故主 transport 必须不带 -p）
- 机器可读输出：`--output-format json` = 末尾缓冲的 JSON 数组
  （system/session_start、assistant、result 三类消息；末元素 result
  携带 subtype/is_error/result/usage —— 不得仅凭 exit code 判定业务
  成功）
- 只读 auth 观测：`qwen auth status`（#3612：该命令不识别
  OpenAI-compatible 配置，故 UNKNOWN 是合法结果，绝不强行
  AUTHENTICATED）
"""
import json
import os
import subprocess
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from qwen_adapter import QwenCodeAdapter
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


class QwenCodeAdapterTests(unittest.TestCase):
    def profile(self, provider="qwen"):
        return RuntimeProfile(
            agent_id="coding-agent",
            runtime="qwen-code",
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

    def adapter(self, provider="qwen"):
        return QwenCodeAdapter(profile=self.profile(provider), executable="qwen")

    @staticmethod
    def qwen_json(result_text="ok", usage=None, is_error=False,
                  subtype="success", with_messages=True):
        """`qwen --output-format json` 数组封装的离线 fixture 形态。"""
        payload = []
        if with_messages:
            payload.append({"type": "system", "subtype": "session_start",
                            "session_id": "s-1", "model": "qwen3-coder-plus"})
            payload.append({"type": "assistant", "session_id": "s-1",
                            "message": {"role": "assistant",
                                        "content": [{"type": "text",
                                                     "text": result_text}]}})
        result = {"type": "result", "subtype": subtype, "session_id": "s-1",
                  "is_error": is_error, "duration_ms": 1234,
                  "result": result_text}
        if usage is not None:
            result["usage"] = usage
        payload.append(result)
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
            self.assertTrue(callable(getattr(QwenCodeAdapter, name)), name)

    # -- invoke：argv / stdin / env / UTF-8 ------------------------------------

    def test_invoke_uses_json_output_argv_with_stdin_prompt(self):
        process = FakeProcess(stdout=self.qwen_json("ok"))
        with patch("qwen_adapter.subprocess.Popen", return_value=process) as popen:
            result = self.adapter().invoke(self.request())

        self.assertEqual(result.status, InvocationStatus.SUCCESS)
        self.assertEqual(result.output, "ok")
        self.assertEqual(result.trace.runtime, "qwen-code")
        self.assertEqual(result.trace.status, InvocationStatus.SUCCESS)
        self.assertEqual(result.trace.exit_code, 0)
        # prompt 经 stdin 传入（不进 argv，永不进入 shell 可达位置）；
        # 且不带 -p —— qwen 语义里带 -p 时 stdin 是上下文而非 prompt。
        self.assertEqual(process.calls, [("Return exactly OK and nothing else.", 3)])
        argv = popen.call_args.args[0]
        self.assertEqual(argv, ["qwen", "--output-format", "json"])
        self.assertNotIn("-p", argv)
        self.assertNotIn("--prompt", argv)
        self.assertFalse(popen.call_args.kwargs["shell"])

    def test_invoke_appends_model_flag_when_requested(self):
        process = FakeProcess(stdout=self.qwen_json("ok"))
        with patch("qwen_adapter.subprocess.Popen", return_value=process) as popen:
            self.adapter().invoke(self.request(model="test-model"))

        argv = popen.call_args.args[0]
        self.assertEqual(argv, [
            "qwen", "--output-format", "json", "--model", "test-model",
        ])

    def test_invoke_env_is_whitelist_only(self):
        process = FakeProcess(stdout=self.qwen_json("ok"))
        with patch("qwen_adapter.subprocess.Popen", return_value=process) as popen:
            self.adapter().invoke(self.request())

        env = popen.call_args.kwargs["env"]
        self.assertIsInstance(env, dict)
        self.assertIn("PATH", env)
        self.assertLessEqual(
            set(env), {"PATH", "HOME", "USERPROFILE", "SYSTEMROOT",
             "TEMP", "TMP"})

    def test_invoke_does_not_forward_credential_or_qwen_env(self):
        # 即使父环境带着凭据/CLI 配置变量，子进程也一概收不到：
        # 白名单只转发放置与定位自身状态所需的变量。
        process = FakeProcess(stdout=self.qwen_json("ok"))
        injected = {
            "OPENAI_API_KEY": "sk-parent-secret-123456",
            "DASHSCOPE_API_KEY": "ds-parent-secret-123456",
            "QWEN_CODE_UNATTENDED_RETRY": "1",
            "QWEN_CODE_SAFE_MODE": "true",
            "CI_FORCE_NO_INTERACTIVE": "1",
        }
        with patch.dict(os.environ, injected, clear=False):
            with patch("qwen_adapter.subprocess.Popen",
                       return_value=process) as popen:
                self.adapter().invoke(self.request())

        env = popen.call_args.kwargs["env"]
        for var in injected:
            self.assertNotIn(var, env, var)
        for key in env:
            self.assertFalse(key.upper().startswith(("QWEN_", "CI_")), key)

    def test_invoke_decodes_child_streams_as_utf_8(self):
        process = FakeProcess(stdout=self.qwen_json("ok"))
        with patch("qwen_adapter.subprocess.Popen", return_value=process) as popen:
            self.adapter().invoke(self.request())

        self.assertEqual(popen.call_args.kwargs.get("encoding"), "utf-8")
        self.assertEqual(popen.call_args.kwargs.get("errors"), "replace")

    def test_invoke_survives_non_ascii_stdout(self):
        # errors="replace"：解码绝不抛异常；调用本身必须成功收尾。
        process = FakeProcess(stdout=self.qwen_json("résumé → 中文 ✓"))
        with patch("qwen_adapter.subprocess.Popen", return_value=process):
            result = self.adapter().invoke(self.request())

        self.assertEqual(result.status, InvocationStatus.SUCCESS)
        self.assertEqual(result.output, "résumé → 中文 ✓")

    def test_stderr_heartbeat_never_breaks_stdout_parse(self):
        # 官方承诺：persistent-retry 心跳只走 stderr，stdout 保持纯 JSON
        # —— adapter 只信任 stdout，stderr 的诊断文本不参与解析。
        process = FakeProcess(
            stdout=self.qwen_json("ok", usage={"input_tokens": 10,
                                               "output_tokens": 4}),
            stderr=("[qwen-code] Waiting for API capacity... "
                    "attempt 3, retry in 45s\n"))
        with patch("qwen_adapter.subprocess.Popen", return_value=process):
            result = self.adapter().invoke(self.request())

        self.assertEqual(result.status, InvocationStatus.SUCCESS)
        self.assertEqual(result.output, "ok")
        self.assertEqual(result.trace.input_tokens, 10)

    # -- JSON 数组 normalization（成功 / 异常 / 旧版本） ------------------------

    def test_full_message_array_success(self):
        # 文档完整形态：system/session_start + assistant + result。
        process = FakeProcess(stdout=self.qwen_json("the answer", with_messages=True))
        with patch("qwen_adapter.subprocess.Popen", return_value=process):
            result = self.adapter().invoke(self.request())

        self.assertEqual(result.status, InvocationStatus.SUCCESS)
        self.assertEqual(result.output, "the answer")

    def test_result_only_array_success(self):
        # 只有 result 消息的最小数组同样是合法成功。
        process = FakeProcess(stdout=self.qwen_json("ok", with_messages=False))
        with patch("qwen_adapter.subprocess.Popen", return_value=process):
            result = self.adapter().invoke(self.request())

        self.assertEqual(result.status, InvocationStatus.SUCCESS)
        self.assertEqual(result.output, "ok")

    def test_empty_json_array_is_failed_not_forged_success(self):
        # 空 JSON 数组 = 没有任何消息 = 结构化输出解析失败 —— 保留
        # 截断 raw 后诚实 FAILED，绝不伪造成功。
        process = FakeProcess(stdout="[]\n")
        with patch("qwen_adapter.subprocess.Popen", return_value=process):
            result = self.adapter().invoke(self.request())

        self.assertEqual(result.status, InvocationStatus.FAILED)
        self.assertIn("[]", result.error)
        self.assertEqual(result.trace.status, InvocationStatus.FAILED)

    def test_non_array_json_is_failed(self):
        # JSON 合法但不是数组（格式异常）→ FAILED + 截断 raw 保留。
        process = FakeProcess(stdout='{"unexpected": true}\n')
        with patch("qwen_adapter.subprocess.Popen", return_value=process):
            result = self.adapter().invoke(self.request())

        self.assertEqual(result.status, InvocationStatus.FAILED)
        self.assertIn("unexpected", result.error)

    def test_array_without_result_message_is_failed(self):
        # 数组只有 system/assistant 消息、没有 result —— 无法证明业务
        # 成功 → FAILED。
        payload = json.dumps([
            {"type": "system", "subtype": "session_start", "model": "m"},
            {"type": "assistant", "message": {"role": "assistant",
                                              "content": []}},
        ]) + "\n"
        process = FakeProcess(stdout=payload)
        with patch("qwen_adapter.subprocess.Popen", return_value=process):
            result = self.adapter().invoke(self.request())

        self.assertEqual(result.status, InvocationStatus.FAILED)

    def test_is_error_true_is_failed_despite_exit_zero(self):
        # 不得仅凭 exit code 判定业务成功：result.is_error=true 时
        # 即使进程退出码为 0 也必须 FAILED。
        process = FakeProcess(stdout=self.qwen_json("boom", is_error=True))
        with patch("qwen_adapter.subprocess.Popen", return_value=process):
            result = self.adapter().invoke(self.request())

        self.assertEqual(result.status, InvocationStatus.FAILED)
        self.assertEqual(result.trace.exit_code, 0)

    def test_non_success_subtype_is_failed_despite_exit_zero(self):
        # subtype != "success"（如预算/回合上限路径）→ FAILED。
        process = FakeProcess(
            stdout=self.qwen_json("hit the cap", subtype="error_max_turns"))
        with patch("qwen_adapter.subprocess.Popen", return_value=process):
            result = self.adapter().invoke(self.request())

        self.assertEqual(result.status, InvocationStatus.FAILED)
        self.assertIn("error_max_turns", result.error)

    def test_result_field_missing_is_failed(self):
        payload = json.dumps([
            {"type": "result", "subtype": "success", "is_error": False},
        ]) + "\n"
        process = FakeProcess(stdout=payload)
        with patch("qwen_adapter.subprocess.Popen", return_value=process):
            result = self.adapter().invoke(self.request())

        self.assertEqual(result.status, InvocationStatus.FAILED)

    def test_raw_text_stdout_falls_back_to_raw_output(self):
        # 完全非 JSON 的 stdout：跟随家族契约（conformance 锁定）——
        # 调用级成功 + 原样 raw 文本，是否可用/安全由上游 packet 验证
        # 与内容扫描裁决；本 adapter 绝不伪造内容可用性。
        process = FakeProcess(stdout="just plain text\n")
        with patch("qwen_adapter.subprocess.Popen", return_value=process):
            result = self.adapter().invoke(self.request())

        self.assertEqual(result.status, InvocationStatus.SUCCESS)
        self.assertEqual(result.output, "just plain text")

    def test_raw_text_with_nonzero_exit_is_failed(self):
        # raw 文本 + 非零退出 = 真失败，绝不 raw-fallback 成成功。
        process = FakeProcess(stdout="panic text\n", stderr="", returncode=1)
        with patch("qwen_adapter.subprocess.Popen", return_value=process):
            result = self.adapter().invoke(self.request())

        self.assertEqual(result.status, InvocationStatus.FAILED)

    def test_historical_output_format_unsupported_is_failed(self):
        # #873：旧版本（0.0.14 一代）不认识 --output-format —— 非零
        # 退出 + 错误文本。必须诚实 FAILED（细节经抹除保留），且绝不
        # 因为是"文档说支持"就假定成功。
        process = FakeProcess(
            stdout="",
            stderr=("error: unknown option '--output-format' "
                    "(qwen 0.0.14)"),
            returncode=2)
        with patch("qwen_adapter.subprocess.Popen", return_value=process):
            result = self.adapter().invoke(self.request())

        self.assertEqual(result.status, InvocationStatus.FAILED)
        self.assertEqual(result.trace.exit_code, 2)
        self.assertIn("--output-format", result.error)

    # -- usage 防御式解析（离线 fixture 证据支撑 CAPTURE）--------------------------

    def test_valid_usage_is_captured_exactly(self):
        process = FakeProcess(stdout=self.qwen_json(
            "ok", usage={"input_tokens": 160, "output_tokens": 60}))
        with patch("qwen_adapter.subprocess.Popen", return_value=process):
            result = self.adapter().invoke(self.request())

        self.assertEqual(result.status, InvocationStatus.SUCCESS)
        self.assertEqual(result.trace.input_tokens, 160)
        self.assertEqual(result.trace.output_tokens, 60)

    def test_missing_usage_stays_unknown(self):
        process = FakeProcess(stdout=self.qwen_json("ok"))
        with patch("qwen_adapter.subprocess.Popen", return_value=process):
            result = self.adapter().invoke(self.request())

        self.assertEqual(result.status, InvocationStatus.SUCCESS)
        self.assertEqual(result.trace.input_tokens, "unknown")
        self.assertEqual(result.trace.output_tokens, "unknown")

    def test_negative_usage_stays_unknown(self):
        process = FakeProcess(stdout=self.qwen_json(
            "ok", usage={"input_tokens": -1, "output_tokens": -5}))
        with patch("qwen_adapter.subprocess.Popen", return_value=process):
            result = self.adapter().invoke(self.request())

        self.assertEqual(result.status, InvocationStatus.SUCCESS)
        self.assertEqual(result.trace.input_tokens, "unknown")
        self.assertEqual(result.trace.output_tokens, "unknown")

    def test_malformed_usage_stays_unknown_not_zero(self):
        process = FakeProcess(stdout=self.qwen_json(
            "ok", usage={"input_tokens": "lots", "output_tokens": None}))
        with patch("qwen_adapter.subprocess.Popen", return_value=process):
            result = self.adapter().invoke(self.request())

        self.assertEqual(result.status, InvocationStatus.SUCCESS)
        self.assertNotEqual(result.trace.input_tokens, 0)
        self.assertNotEqual(result.trace.output_tokens, 0)
        self.assertEqual(result.trace.input_tokens, "unknown")
        self.assertEqual(result.trace.output_tokens, "unknown")

    def test_partial_usage_is_not_fabricated(self):
        # 只有 input_tokens 合法：可观测的一侧如实上报，另一侧保持
        # unknown —— 绝不补 0。
        process = FakeProcess(stdout=self.qwen_json(
            "ok", usage={"input_tokens": 90}))
        with patch("qwen_adapter.subprocess.Popen", return_value=process):
            result = self.adapter().invoke(self.request())

        self.assertEqual(result.status, InvocationStatus.SUCCESS)
        self.assertEqual(result.trace.input_tokens, 90)
        self.assertEqual(result.trace.output_tokens, "unknown")

    def test_bool_shaped_usage_is_rejected(self):
        # True/False 在 Python 里是 int：诚实的解析器必须拒收。
        process = FakeProcess(stdout=self.qwen_json(
            "ok", usage={"input_tokens": True, "output_tokens": False}))
        with patch("qwen_adapter.subprocess.Popen", return_value=process):
            result = self.adapter().invoke(self.request())

        self.assertEqual(result.status, InvocationStatus.SUCCESS)
        self.assertEqual(result.trace.input_tokens, "unknown")
        self.assertEqual(result.trace.output_tokens, "unknown")

    def test_assistant_message_usage_is_fallback_source(self):
        # result 消息不带 usage 时，最后一条 assistant 消息的
        # message.usage 是允许的回退观测面（文档两面都携带 usage）。
        payload = json.dumps([
            {"type": "assistant",
             "message": {"role": "assistant",
                         "content": [{"type": "text", "text": "ok"}],
                         "usage": {"input_tokens": 55, "output_tokens": 25}}},
            {"type": "result", "subtype": "success", "is_error": False,
             "result": "ok"},
        ]) + "\n"
        process = FakeProcess(stdout=payload)
        with patch("qwen_adapter.subprocess.Popen", return_value=process):
            result = self.adapter().invoke(self.request())

        self.assertEqual(result.status, InvocationStatus.SUCCESS)
        self.assertEqual(result.trace.input_tokens, 55)
        self.assertEqual(result.trace.output_tokens, 25)

    def test_usage_parse_failure_never_fails_invocation(self):
        process = FakeProcess(stdout="not json at all \x00\xff")
        with patch("qwen_adapter.subprocess.Popen", return_value=process):
            result = self.adapter().invoke(self.request())

        self.assertEqual(result.status, InvocationStatus.SUCCESS)
        self.assertEqual(result.trace.input_tokens, "unknown")

    # -- 超时 / 取消 / 失败语义 ---------------------------------------------------

    def test_timeout_kills_process_and_records_timeout(self):
        class TimeoutProcess(FakeProcess):
            def communicate(self, input=None, timeout=None):
                raise subprocess.TimeoutExpired(cmd="qwen", timeout=timeout)

        process = TimeoutProcess()
        with patch("qwen_adapter.subprocess.Popen", return_value=process):
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
        with patch("qwen_adapter.subprocess.Popen", return_value=process):
            worker = threading.Thread(
                target=lambda: results.append(adapter.invoke(self.request())))
            worker.start()
            self.assertTrue(started.wait(timeout=1))
            invocation_id = adapter.last_invocation_id
            self.assertIsNotNone(invocation_id)
            cancel_result = adapter.cancel(invocation_id)
            worker.join(timeout=1)

        # cancel() 立即返回 CANCELLED；被取消的 invoke 收尾也是
        # CANCELLED（调用方意图）而非 TIMEOUT —— 两者是不同的诚实结果。
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

    def test_successful_invoke_releases_process_slot(self):
        adapter = self.adapter()
        process = FakeProcess(stdout=self.qwen_json("ok"))
        with patch("qwen_adapter.subprocess.Popen", return_value=process):
            adapter.invoke(self.request())

        self.assertEqual(adapter._processes, {})
        self.assertIn(adapter.last_invocation_id, adapter._completed)

    def test_concurrent_invocations_keep_bookkeeping_consistent(self):
        # 并发调用下 _state_lock 保证簿记不损坏：全部收尾后活动表
        # 清空、完成表恰好 N 条、每次调用都是独立的结构化结果。
        adapter = self.adapter()
        processes = [FakeProcess(stdout=self.qwen_json("ok")) for _ in range(8)]
        results = []
        lock = threading.Lock()

        def run_one():
            result = adapter.invoke(self.request())
            with lock:
                results.append(result)

        with patch("qwen_adapter.subprocess.Popen", side_effect=processes):
            workers = [threading.Thread(target=run_one) for _ in range(8)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(timeout=2)

        self.assertEqual(len(results), 8)
        for result in results:
            self.assertEqual(result.status, InvocationStatus.SUCCESS)
        self.assertEqual(adapter._processes, {})
        self.assertEqual(len(adapter._completed), 8)
        ids = {result.trace.invocation_id for result in results}
        self.assertEqual(len(ids), 8)

    def test_nonzero_exit_is_failed_and_does_not_report_success(self):
        process = FakeProcess(stdout="", stderr="bad", returncode=2)
        with patch("qwen_adapter.subprocess.Popen", return_value=process):
            result = self.adapter().invoke(self.request())

        self.assertEqual(result.status, InvocationStatus.FAILED)
        self.assertEqual(result.error, "bad")
        self.assertEqual(result.trace.status, InvocationStatus.FAILED)
        self.assertEqual(result.trace.exit_code, 2)

    def test_nonzero_exit_stderr_is_redacted(self):
        process = FakeProcess(
            stdout="",
            stderr=("api_key=sk-live-secret123456 token: tok-xyz987654 "
                    "dashscope_key=ds-live-secret123456"),
            returncode=1)
        with patch("qwen_adapter.subprocess.Popen", return_value=process):
            result = self.adapter().invoke(self.request())

        self.assertEqual(result.status, InvocationStatus.FAILED)
        self.assertNotIn("sk-live-secret123456", result.error)
        self.assertNotIn("tok-xyz987654", result.error)
        self.assertNotIn("ds-live-secret123456", result.error)
        self.assertIn("[REDACTED]", result.error)

    def test_oserror_failure_is_sanitized_not_raw(self):
        with patch("qwen_adapter.subprocess.Popen",
                   side_effect=OSError("no such file api_key=raw-secret-value")):
            result = self.adapter().invoke(self.request())

        self.assertEqual(result.status, InvocationStatus.UNAVAILABLE)
        self.assertNotIn("raw-secret-value", result.error)
        self.assertIn("[REDACTED]", result.error)

    def test_safe_error_redacts_qwen_family_key_shapes(self):
        redactor = QwenCodeAdapter._safe_error
        text = ("api_key=alpha token: beta secret=gamma "
                "Authorization: Bearer delta "
                "hf_1234567890 sk-abcdefghij "
                "dashscope_key=ds-omega123456 qwen_api_key=qq-zeta123456")
        redacted = redactor(text)
        for secret in ("alpha", "beta", "gamma", "delta", "hf_1234567890",
                       "sk-abcdefghij", "ds-omega123456", "qq-zeta123456"):
            self.assertNotIn(secret, redacted, secret)
        self.assertGreaterEqual(redacted.count("[REDACTED]"), 8)

    # -- trace 诚实性 ------------------------------------------------------------

    def test_trace_keeps_identity_fields_separate_and_tokens_unknown(self):
        process = FakeProcess(stdout=self.qwen_json("ok"))
        with patch("qwen_adapter.subprocess.Popen", return_value=process):
            result = self.adapter().invoke(self.request())

        self.assertEqual(result.trace.agent_id, "coding-agent")
        self.assertEqual(result.trace.runtime, "qwen-code")
        self.assertEqual(result.trace.provider, "test-provider")
        self.assertEqual(result.trace.model, None)
        self.assertEqual(result.trace.role, "coder")
        # usage 缺失（fixture 无 usage 键）→ 诚实 unknown，绝不 0。
        self.assertEqual(result.trace.input_tokens, "unknown")
        self.assertEqual(result.trace.output_tokens, "unknown")

    # -- discovery -----------------------------------------------------------------

    def test_discovery_reports_version_without_capability_claim(self):
        completed = subprocess.CompletedProcess(
            args=["qwen", "--version"], returncode=0,
            stdout="qwen-code 0.42.0\n", stderr="")

        class ProbeSpy:
            calls = []

            @classmethod
            def run(cls, argv, **kwargs):
                cls.calls.append((argv, kwargs))
                return completed

        with patch("qwen_adapter.subprocess.run", new=ProbeSpy.run):
            discovery = self.adapter().discover()

        self.assertTrue(discovery.available)
        self.assertEqual(discovery.runtime, "qwen-code")
        self.assertEqual(discovery.version, "qwen-code 0.42.0")
        self.assertEqual(discovery.capabilities, frozenset())
        argv, kwargs = ProbeSpy.calls[0]
        self.assertEqual(argv, ["qwen", "--version"])
        self.assertFalse(kwargs["shell"])
        self.assertLessEqual(
            set(kwargs["env"]), {"PATH", "HOME", "USERPROFILE", "SYSTEMROOT",
             "TEMP", "TMP"})
        self.assertEqual(kwargs.get("encoding"), "utf-8")
        self.assertEqual(kwargs.get("errors"), "replace")

    def test_discovery_failure_reports_reason_without_secrets(self):
        with patch("qwen_adapter.subprocess.run",
                   side_effect=OSError("gone token=super-secret-xyz")):
            discovery = self.adapter().discover()

        self.assertFalse(discovery.available)
        self.assertNotIn("super-secret-xyz", discovery.reason)
        self.assertIn("[REDACTED]", discovery.reason)

    def test_discovery_unavailable_when_probe_rejects(self):
        completed = subprocess.CompletedProcess(
            args=["qwen", "--version"], returncode=1,
            stdout="", stderr="nope")
        with patch("qwen_adapter.subprocess.run", return_value=completed):
            discovery = self.adapter().discover()

        self.assertFalse(discovery.available)

    def test_missing_executable_is_honest_absence(self):
        with patch("qwen_adapter.shutil.which", return_value=None):
            adapter = QwenCodeAdapter.from_environment()

        self.assertIsNone(adapter)

    def test_from_environment_builds_adapter_when_present(self):
        with patch("qwen_adapter.shutil.which",
                   return_value="/fake/bin/qwen"):
            adapter = QwenCodeAdapter.from_environment()

        self.assertIsNotNone(adapter)
        self.assertEqual(adapter.profile.runtime, "qwen-code")
        self.assertEqual(adapter.profile.provider, "qwen")
        self.assertEqual(adapter.profile.model, None)

    # -- check_authentication：只观测，不猜测 ------------------------------------------

    def test_check_authentication_logged_in_maps_to_authenticated(self):
        completed = subprocess.CompletedProcess(
            args=["qwen", "auth", "status"], returncode=0,
            stdout="logged in", stderr="")

        class AuthSpy:
            calls = []

            @classmethod
            def run(cls, argv, **kwargs):
                cls.calls.append((argv, kwargs))
                return completed

        with patch("qwen_adapter.subprocess.run", new=AuthSpy.run):
            result = self.adapter().check_authentication()

        self.assertEqual(result.state, AuthenticationState.AUTHENTICATED)
        argv, kwargs = AuthSpy.calls[0]
        # 只读观测面：qwen auth status；绝不 login/logout/refresh。
        self.assertEqual(argv, ["qwen", "auth", "status"])
        self.assertFalse(kwargs["shell"])
        self.assertLessEqual(
            set(kwargs["env"]), {"PATH", "HOME", "USERPROFILE", "SYSTEMROOT",
             "TEMP", "TMP"})

    def test_check_authentication_not_logged_in_is_auth_required(self):
        completed = subprocess.CompletedProcess(
            args=["qwen", "auth", "status"], returncode=0,
            stdout="not logged in", stderr="")
        with patch("qwen_adapter.subprocess.run", return_value=completed):
            result = self.adapter().check_authentication()

        self.assertEqual(result.state, AuthenticationState.AUTH_REQUIRED)
        self.assertEqual(result.reason_code, ReasonCode.AUTH_REQUIRED)

    def test_check_authentication_junk_output_is_unknown_not_faked(self):
        completed = subprocess.CompletedProcess(
            args=["qwen", "auth", "status"], returncode=0,
            stdout="total garbage \x00\x01", stderr="")
        with patch("qwen_adapter.subprocess.run", return_value=completed):
            result = self.adapter().check_authentication()

        self.assertEqual(result.state, AuthenticationState.UNKNOWN)
        self.assertEqual(result.reason_code, ReasonCode.PROTOCOL_ERROR)

    def test_check_authentication_subprocess_failure_is_unknown(self):
        with patch("qwen_adapter.subprocess.run",
                   side_effect=OSError("gone")):
            result = self.adapter().check_authentication()

        self.assertEqual(result.state, AuthenticationState.UNKNOWN)
        self.assertEqual(result.reason_code, ReasonCode.PROTOCOL_ERROR)

    def test_check_authentication_3612_ambiguous_output_is_unknown(self):
        # #3612：auth status 不识别 settings/env 配置的
        # OpenAI-compatible provider —— 这类"描述了配置但没说登录态"
        # 的输出必须归 UNKNOWN，绝不强行 AUTHENTICATED。
        completed = subprocess.CompletedProcess(
            args=["qwen", "auth", "status"], returncode=0,
            stdout="Auth configured via settings.json "
                   "(openai-compatible provider)", stderr="")
        with patch("qwen_adapter.subprocess.run", return_value=completed):
            result = self.adapter().check_authentication()

        self.assertEqual(result.state, AuthenticationState.UNKNOWN)

    # -- check_provider_model：以观测 auth 为门 ---------------------------------------

    def test_check_provider_model_unavailable_before_observed_auth(self):
        check = self.adapter().check_provider_model()

        self.assertFalse(check.available)
        self.assertEqual(check.reason_code, ReasonCode.PROVIDER_UNREACHABLE)

    def test_check_provider_model_available_after_observed_auth(self):
        adapter = self.adapter()
        completed = subprocess.CompletedProcess(
            args=["qwen", "auth", "status"], returncode=0,
            stdout="logged in", stderr="")
        with patch("qwen_adapter.subprocess.run", return_value=completed):
            adapter.check_authentication()
        # provider 检查不再 spawn 子进程：它由已观测的 auth 推导。
        with patch("qwen_adapter.subprocess.run",
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
        env = {k: v for k, v in os.environ.items()
               if k != "RUN_REAL_PROVIDER_TESTS"}
        with patch.dict(os.environ, env, clear=True):
            result = self.adapter().minimal_health_check(timeout_seconds=5)

        self.assertFalse(result.passed)
        self.assertEqual(result.reason_code, ReasonCode.UNSUPPORTED_HEALTH_CHECK)
        self.assertEqual(result.output_class, "skipped")

    def test_minimal_health_check_exact_ok_when_gated(self):
        process = FakeProcess(stdout=self.qwen_json("OK"))
        with patch.dict(os.environ, {"RUN_REAL_PROVIDER_TESTS": "1"}):
            with patch("qwen_adapter.subprocess.Popen", return_value=process):
                result = self.adapter().minimal_health_check(timeout_seconds=5)

        self.assertTrue(result.passed)
        self.assertEqual(result.reason_code, ReasonCode.NONE)
        self.assertEqual(result.output_class, "exact_ok")

    def test_minimal_health_check_unexpected_output_is_not_passed(self):
        process = FakeProcess(stdout=self.qwen_json("something else"))
        with patch.dict(os.environ, {"RUN_REAL_PROVIDER_TESTS": "1"}):
            with patch("qwen_adapter.subprocess.Popen", return_value=process):
                result = self.adapter().minimal_health_check(timeout_seconds=5)

        self.assertFalse(result.passed)
        self.assertEqual(result.reason_code, ReasonCode.PROTOCOL_ERROR)
        self.assertEqual(result.output_class, "unexpected_response")


if __name__ == "__main__":
    unittest.main()
