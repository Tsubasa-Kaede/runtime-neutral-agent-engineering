"""OpenCodeAdapter 的离线、确定性测试（R1-Step7 — TDD RED→GREEN）。

本文件先于 opencode_adapter.py 存在（RED：模块缺席时 collection 失败）。
全部用 fake 进程对象替身驱动：不调用真实 opencode、不访问网络、不读取
凭据，也不依赖本机是否安装了 OpenCode。仅有的真实 subprocess 调用点
（_probe 与 check_authentication 的 subprocess.run、invoke 的 Popen）
全部被 patch。

OpenCode 非交互形态（本 adapter 的目标面，均离线 fixture 证明，不做
任何 REAL 断言；证据=官方 CLI 文档 + run.ts 源码 + R1-Step6 边界调查）：
- 非交互调用：`opencode run "<prompt>" --format json` —— prompt 是
  位置参数（argv，codex 家族同构）；Coder 位固定 `--agent build`
  （官方默认全权 agent，显式钉住防 config default_agent 漂移）
- stdin：源码实证非 TTY stdin 会被读取并追加到 message —— 因此
  stdin=DEVNULL（读空立即 EOF，无害）是源码正确性而非防御；
  PIPE 不关会挂起，绝不使用
- 机器可读输出：`--format json` = stdout JSONL 事件流，每行
  {type, timestamp, sessionID, ...data}；事件名 step_start/step_finish/
  text/tool_use/error（源码级）。少数 UI.println 警告不受 format 门控
  —— 非 JSON 行必须忽略
- 错误优先：error 事件即使跟随 returncode 0 也必须 FAILED（源码：
  session.error 只累积不置非零退出码）；session.idle 不进 stdout 流
  —— 干净退出（rc=0）+ 无 error 事件是 idle 的可观测代理
- usage：step_finish.part.tokens.{input,output} 逐 step 实测（schema
  为 COMMUNITY 级，REAL 待逐字段核对）—— 同调用同模型 step 求和；
  任一 step 不可观测则该侧整体 unknown（部分观测污染总和）
- 只读 auth 观测：`opencode auth list`（官方文档："Lists all the
  authenticated providers as stored in the credentials file"）；输出
  形态未实证 → 文本优先分类 + UNKNOWN 兜底，绝不猜测成功
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

from opencode_adapter import OpenCodeAdapter
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


class OpenCodeAdapterTests(unittest.TestCase):
    def profile(self, provider="anthropic"):
        return RuntimeProfile(
            agent_id="coding-agent",
            runtime="opencode",
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

    def adapter(self, provider="anthropic"):
        return OpenCodeAdapter(profile=self.profile(provider), executable="opencode")

    @staticmethod
    def opencode_stream(texts=("ok",), step_usages=None, error_event=None,
                        noise=()):
        """`opencode run --format json` stdout 的 JSONL 离线 fixture。

        每个文本事件跟随一个 step_finish；step_usages 条目是
        (input, output) 元组，None 表示该侧键缺失（无 tokens 键的
        step_finish 由 (None, None) 产生）。noise 是前置的非 JSON
        行（不受 --format 门控的 UI 警告形态）。
        """
        def event(kind, **data):
            payload = {"type": kind, "timestamp": 1700000000000,
                       "sessionID": "ses_1"}
            payload.update(data)
            return json.dumps(payload)

        # 防御：单个字符串是"一条文本"，不是"字符的可迭代"。
        if isinstance(texts, str):
            texts = (texts,)
        if isinstance(noise, str):
            noise = (noise,)
        lines = [str(line) for line in noise]
        if texts or step_usages:
            lines.append(event("step_start", part={"type": "step-start"}))
        for index, text in enumerate(texts):
            lines.append(event("text", part={"type": "text", "text": text}))
            if step_usages and index < len(step_usages):
                tokens = {}
                if step_usages[index][0] is not None:
                    tokens["input"] = step_usages[index][0]
                if step_usages[index][1] is not None:
                    tokens["output"] = step_usages[index][1]
                part = {"type": "step-finish"}
                if tokens:
                    part["tokens"] = tokens
                lines.append(event("step_finish", part=part))
        if error_event is not None:
            lines.append(event("error", error=error_event))
        return "\n".join(lines) + "\n"

    # -- 六方法契约 ----------------------------------------------------------

    def test_six_method_protocol_conformance(self):
        # 事实面是全部六个方法：三个协议方法 + 三个 health 方法。
        # "具备方法"不等于 REAL VERIFIED —— 资格只由门控运行授予。
        for name in (
            "discover", "invoke", "cancel",
            "check_authentication", "check_provider_model",
            "minimal_health_check",
        ):
            self.assertTrue(callable(getattr(OpenCodeAdapter, name)), name)

    # -- 构造 / from_environment ---------------------------------------------

    def test_construction_keeps_profile_and_executable(self):
        adapter = self.adapter()
        self.assertEqual(adapter.profile.runtime, "opencode")
        self.assertEqual(adapter.profile.role, "coder")
        self.assertEqual(adapter.executable, "opencode")
        self.assertEqual(adapter._processes, {})
        self.assertIsNone(adapter.last_invocation_id)

    def test_from_environment_builds_adapter_when_present(self):
        with patch("opencode_adapter.shutil.which",
                   return_value="/fake/bin/opencode"):
            adapter = OpenCodeAdapter.from_environment()

        self.assertIsNotNone(adapter)
        self.assertEqual(adapter.profile.runtime, "opencode")
        # 默认 provider=None：opencode 是多 provider harness，provider
        # 隐含在 "provider/model" 串内，绝不把 opencode 当 provider。
        self.assertIsNone(adapter.profile.provider)
        self.assertIsNone(adapter.profile.model)
        self.assertEqual(adapter.profile.role, "coder")

    def test_missing_executable_is_honest_absence(self):
        with patch("opencode_adapter.shutil.which", return_value=None):
            adapter = OpenCodeAdapter.from_environment()

        self.assertIsNone(adapter)

    # -- invoke：argv / stdin / env / UTF-8 ------------------------------------

    def test_invoke_uses_run_argv_with_json_format_and_build_agent(self):
        process = FakeProcess(stdout=self.opencode_stream("ok"))
        with patch("opencode_adapter.subprocess.Popen",
                   return_value=process) as popen:
            result = self.adapter().invoke(self.request())

        self.assertEqual(result.status, InvocationStatus.SUCCESS)
        self.assertEqual(result.output, "ok")
        self.assertEqual(result.trace.runtime, "opencode")
        self.assertEqual(result.trace.status, InvocationStatus.SUCCESS)
        self.assertEqual(result.trace.exit_code, 0)
        argv = popen.call_args.args[0]
        # 官方非交互形态：prompt 是位置参数（codex 家族同构），
        # --format json 固定，Coder 位钉住 --agent build（防 config
        # default_agent 漂移）。
        self.assertEqual(argv, [
            "opencode", "run", "Return exactly OK and nothing else.",
            "--format", "json", "--agent", "build",
        ])
        self.assertFalse(popen.call_args.kwargs["shell"])

    def test_invoke_stdin_is_devnull(self):
        # 源码实证：非 TTY stdin 会被读取并追加到 message —— DEVNULL
        # 读空立即 EOF 无害；PIPE 不关会挂起（#11891 机制），绝不使用。
        process = FakeProcess(stdout=self.opencode_stream("ok"))
        with patch("opencode_adapter.subprocess.Popen",
                   return_value=process) as popen:
            self.adapter().invoke(self.request())

        self.assertEqual(popen.call_args.kwargs.get("stdin"),
                         subprocess.DEVNULL)

    def test_invoke_prompt_rides_argv_not_stdin(self):
        # prompt 已在 argv 位置参数上；communicate 不再携带任何输入。
        process = FakeProcess(stdout=self.opencode_stream("ok"))
        with patch("opencode_adapter.subprocess.Popen", return_value=process):
            self.adapter().invoke(self.request(timeout_seconds=3))

        self.assertEqual(process.calls, [(None, 3)])

    def test_invoke_appends_model_flag_when_requested(self):
        # 情况 A：provider 隐含在 "provider/model" 串内，原样透传，
        # 绝不拆分、绝不猜测。
        process = FakeProcess(stdout=self.opencode_stream("ok"))
        with patch("opencode_adapter.subprocess.Popen",
                   return_value=process) as popen:
            self.adapter().invoke(self.request(
                model="anthropic/claude-sonnet-5"))

        argv = popen.call_args.args[0]
        self.assertEqual(argv, [
            "opencode", "run", "Return exactly OK and nothing else.",
            "--format", "json", "--agent", "build",
            "--model", "anthropic/claude-sonnet-5",
        ])

    def test_invoke_without_model_has_no_model_flag(self):
        process = FakeProcess(stdout=self.opencode_stream("ok"))
        with patch("opencode_adapter.subprocess.Popen",
                   return_value=process) as popen:
            self.adapter().invoke(self.request())

        self.assertNotIn("--model", popen.call_args.args[0])

    def test_invoke_env_is_whitelist_plus_disable_autoupdate(self):
        # 最小白名单 + 唯一的 adapter 自有安全旋钮：官方文档 autoupdate
        # 默认开，一次调用不得触发自更新副作用。
        process = FakeProcess(stdout=self.opencode_stream("ok"))
        with patch("opencode_adapter.subprocess.Popen",
                   return_value=process) as popen:
            self.adapter().invoke(self.request())

        env = popen.call_args.kwargs["env"]
        self.assertIsInstance(env, dict)
        self.assertIn("PATH", env)
        self.assertLessEqual(set(env), {
            "PATH", "HOME", "USERPROFILE", "SYSTEMROOT",
            "TEMP", "TMP", "OPENCODE_DISABLE_AUTOUPDATE",
        })
        self.assertEqual(env.get("OPENCODE_DISABLE_AUTOUPDATE"), "1")

    def test_invoke_does_not_forward_credential_or_opencode_config_env(self):
        # 父环境的凭据与 CLI 配置变量一概不转发；子环境里唯一的
        # OPENCODE_* 变量是 adapter 自己注入的 disable 旋钮。
        process = FakeProcess(stdout=self.opencode_stream("ok"))
        injected = {
            "OPENAI_API_KEY": "sk-parent-secret-123456",
            "ANTHROPIC_API_KEY": "sk-ant-parent-123456",
            "OPENCODE_CONFIG": "/parent/opencode.json",
            "OPENCODE_CONFIG_CONTENT": '{"share": "auto"}',
            "OPENCODE_PERMISSION": '{"edit": "allow"}',
        }
        with patch.dict(os.environ, injected, clear=False):
            with patch("opencode_adapter.subprocess.Popen",
                       return_value=process) as popen:
                self.adapter().invoke(self.request())

        env = popen.call_args.kwargs["env"]
        for var in injected:
            self.assertNotIn(var, env, var)
        opencode_keys = [key for key in env
                         if key.upper().startswith("OPENCODE_")]
        self.assertEqual(opencode_keys, ["OPENCODE_DISABLE_AUTOUPDATE"])

    def test_invoke_decodes_child_streams_as_utf_8(self):
        process = FakeProcess(stdout=self.opencode_stream("ok"))
        with patch("opencode_adapter.subprocess.Popen",
                   return_value=process) as popen:
            self.adapter().invoke(self.request())

        self.assertEqual(popen.call_args.kwargs.get("encoding"), "utf-8")
        self.assertEqual(popen.call_args.kwargs.get("errors"), "replace")

    def test_invoke_survives_non_ascii_stdout(self):
        # errors="replace"：解码绝不抛异常；非 ASCII 文本如实透传。
        process = FakeProcess(
            stdout=self.opencode_stream("résumé → 中文 ✓"))
        with patch("opencode_adapter.subprocess.Popen", return_value=process):
            result = self.adapter().invoke(self.request())

        self.assertEqual(result.status, InvocationStatus.SUCCESS)
        self.assertEqual(result.output, "résumé → 中文 ✓")

    def test_stderr_noise_never_breaks_stdout_parse(self):
        # stderr 是诊断面，不参与解析；stdout 保持纯 JSONL。
        process = FakeProcess(
            stdout=self.opencode_stream("ok"),
            stderr="▲ opencode v0.4.5 loading plugins...\n")
        with patch("opencode_adapter.subprocess.Popen", return_value=process):
            result = self.adapter().invoke(self.request())

        self.assertEqual(result.status, InvocationStatus.SUCCESS)
        self.assertEqual(result.output, "ok")

    # -- JSONL 解析（成功 / 错误优先 / 异常形态） ------------------------------

    def test_multiple_text_events_are_joined_in_order(self):
        # 多 step 的文本事件按序拼接为业务结果。
        process = FakeProcess(
            stdout=self.opencode_stream(("first", "second")))
        with patch("opencode_adapter.subprocess.Popen", return_value=process):
            result = self.adapter().invoke(self.request())

        self.assertEqual(result.status, InvocationStatus.SUCCESS)
        self.assertEqual(result.output, "first\nsecond")

    def test_non_json_lines_are_ignored(self):
        # 源码事实：少数 UI.println 警告不受 --format 门控 —— 非 JSON
        # 行必须忽略，不影响事件流判定。
        process = FakeProcess(stdout=self.opencode_stream(
            "ok",
            noise=('! agent "custom" not found. Falling back to default',)))
        with patch("opencode_adapter.subprocess.Popen", return_value=process):
            result = self.adapter().invoke(self.request())

        self.assertEqual(result.status, InvocationStatus.SUCCESS)
        self.assertEqual(result.output, "ok")

    def test_error_event_is_failed_despite_exit_zero(self):
        # 源码关键事实：session.error 只累积不置非零退出码 —— error
        # 事件优先于 returncode，rc=0 绝不能掩盖 FAILED。
        process = FakeProcess(stdout=self.opencode_stream(
            "partial text", error_event={
                "name": "AuthorizationError",
                "data": {"message": "provider rejected the request"},
            }))
        with patch("opencode_adapter.subprocess.Popen", return_value=process):
            result = self.adapter().invoke(self.request())

        self.assertEqual(result.status, InvocationStatus.FAILED)
        self.assertEqual(result.trace.exit_code, 0)
        self.assertIn("provider rejected the request", result.error)

    def test_error_event_precedes_text_events(self):
        # 错误优先：即使流里已有文本事件，error 事件仍然 FAILED。
        process = FakeProcess(stdout=self.opencode_stream(
            ("ok",), error_event="session aborted"))
        with patch("opencode_adapter.subprocess.Popen", return_value=process):
            result = self.adapter().invoke(self.request())

        self.assertEqual(result.status, InvocationStatus.FAILED)
        self.assertIn("session aborted", result.error)

    def test_error_event_payload_is_redacted(self):
        # error 事件的消息文本可能携带凭据形态 —— 离开模块前必须抹除。
        process = FakeProcess(stdout=self.opencode_stream(
            "ok", error_event={
                "name": "ProviderError",
                "data": {"message":
                         "401 api_key=sk-live-secret123456 rejected"},
            }))
        with patch("opencode_adapter.subprocess.Popen", return_value=process):
            result = self.adapter().invoke(self.request())

        self.assertEqual(result.status, InvocationStatus.FAILED)
        self.assertNotIn("sk-live-secret123456", result.error)
        self.assertIn("[REDACTED]", result.error)

    def test_structured_stream_without_text_events_is_failed(self):
        # 只有工具/步骤事件、没有任何 text 事件 —— 结构化流证明 CLI
        # 在工作，但没有可交付的业务文本：诚实 FAILED，绝不伪造成功。
        stream = json.dumps({
            "type": "tool_use", "timestamp": 1, "sessionID": "ses_1",
            "part": {"tool": "bash", "state": {"status": "completed"}},
        }) + "\n"
        process = FakeProcess(stdout=stream)
        with patch("opencode_adapter.subprocess.Popen", return_value=process):
            result = self.adapter().invoke(self.request())

        self.assertEqual(result.status, InvocationStatus.FAILED)

    def test_pure_non_json_stdout_falls_back_to_raw_output(self):
        # 完全非 JSON 的 stdout：跟随家族契约（conformance 锁定）——
        # 调用级成功 + 原样 raw 文本，是否可用/安全由上游 packet 验证
        # 与内容扫描裁决；本 adapter 绝不伪造内容可用性。
        process = FakeProcess(stdout="just plain text\n")
        with patch("opencode_adapter.subprocess.Popen", return_value=process):
            result = self.adapter().invoke(self.request())

        self.assertEqual(result.status, InvocationStatus.SUCCESS)
        self.assertEqual(result.output, "just plain text")

    def test_raw_text_with_nonzero_exit_is_failed(self):
        # raw 文本 + 非零退出 = 真失败，绝不 raw-fallback 成成功。
        process = FakeProcess(stdout="panic text\n", stderr="", returncode=1)
        with patch("opencode_adapter.subprocess.Popen", return_value=process):
            result = self.adapter().invoke(self.request())

        self.assertEqual(result.status, InvocationStatus.FAILED)

    def test_empty_stdout_is_failed(self):
        # 空输出 = 没有任何可判定证据 —— 诚实 FAILED，绝不空转成功。
        process = FakeProcess(stdout="")
        with patch("opencode_adapter.subprocess.Popen", return_value=process):
            result = self.adapter().invoke(self.request())

        self.assertEqual(result.status, InvocationStatus.FAILED)

    def test_nonzero_exit_is_failed_and_does_not_report_success(self):
        process = FakeProcess(stdout="", stderr="bad", returncode=2)
        with patch("opencode_adapter.subprocess.Popen", return_value=process):
            result = self.adapter().invoke(self.request())

        self.assertEqual(result.status, InvocationStatus.FAILED)
        self.assertEqual(result.error, "bad")
        self.assertEqual(result.trace.status, InvocationStatus.FAILED)
        self.assertEqual(result.trace.exit_code, 2)

    def test_nonzero_exit_stderr_is_redacted(self):
        process = FakeProcess(
            stdout="",
            stderr="api_key=sk-live-secret123456 token: tok-xyz987654",
            returncode=1)
        with patch("opencode_adapter.subprocess.Popen", return_value=process):
            result = self.adapter().invoke(self.request())

        self.assertEqual(result.status, InvocationStatus.FAILED)
        self.assertNotIn("sk-live-secret123456", result.error)
        self.assertNotIn("tok-xyz987654", result.error)
        self.assertIn("[REDACTED]", result.error)

    def test_oserror_failure_is_sanitized_not_raw(self):
        with patch("opencode_adapter.subprocess.Popen",
                   side_effect=OSError("no such file api_key=raw-secret-value")):
            result = self.adapter().invoke(self.request())

        self.assertEqual(result.status, InvocationStatus.UNAVAILABLE)
        self.assertNotIn("raw-secret-value", result.error)
        self.assertIn("[REDACTED]", result.error)

    def test_safe_error_redacts_provider_key_material(self):
        redactor = OpenCodeAdapter._safe_error
        text = ("api_key=alpha token: beta secret=gamma "
                "Authorization: Bearer delta "
                "hf_1234567890 sk-abcdefghij")
        redacted = redactor(text)
        for secret in ("alpha", "beta", "gamma", "delta",
                       "hf_1234567890", "sk-abcdefghij"):
            self.assertNotIn(secret, redacted, secret)
        self.assertGreaterEqual(redacted.count("[REDACTED]"), 6)

    # -- usage 防御式解析（step-sum，CAPTURE） ----------------------------------

    def test_valid_usage_step_sum_is_captured_exactly(self):
        # 多 step 同调用同模型：token 用量 = 各 step_finish 之和。
        process = FakeProcess(stdout=self.opencode_stream(
            ("first", "second"), step_usages=[(100, 40), (60, 20)]))
        with patch("opencode_adapter.subprocess.Popen", return_value=process):
            result = self.adapter().invoke(self.request())

        self.assertEqual(result.status, InvocationStatus.SUCCESS)
        self.assertEqual(result.trace.input_tokens, 160)
        self.assertEqual(result.trace.output_tokens, 60)

    def test_single_step_usage_is_captured(self):
        process = FakeProcess(stdout=self.opencode_stream(
            ("ok",), step_usages=[(12, 7)]))
        with patch("opencode_adapter.subprocess.Popen", return_value=process):
            result = self.adapter().invoke(self.request())

        self.assertEqual(result.trace.input_tokens, 12)
        self.assertEqual(result.trace.output_tokens, 7)

    def test_missing_usage_stays_unknown(self):
        # step_finish 不带 tokens 键 → 诚实 unknown，绝不 0。
        process = FakeProcess(stdout=self.opencode_stream(
            ("ok",), step_usages=[(None, None)]))
        with patch("opencode_adapter.subprocess.Popen", return_value=process):
            result = self.adapter().invoke(self.request())

        self.assertEqual(result.status, InvocationStatus.SUCCESS)
        self.assertEqual(result.trace.input_tokens, "unknown")
        self.assertEqual(result.trace.output_tokens, "unknown")

    def test_malformed_usage_stays_unknown_not_zero(self):
        process = FakeProcess(stdout=self.opencode_stream(
            ("ok",), step_usages=[("lots", -5)]))
        with patch("opencode_adapter.subprocess.Popen", return_value=process):
            result = self.adapter().invoke(self.request())

        self.assertEqual(result.status, InvocationStatus.SUCCESS)
        self.assertEqual(result.trace.input_tokens, "unknown")
        self.assertEqual(result.trace.output_tokens, "unknown")

    def test_partial_usage_is_not_fabricated(self):
        # 只有 input 一侧合法：可观测的一侧如实上报，另一侧保持
        # unknown —— 绝不补 0。
        process = FakeProcess(stdout=self.opencode_stream(
            ("ok",), step_usages=[(90, None)]))
        with patch("opencode_adapter.subprocess.Popen", return_value=process):
            result = self.adapter().invoke(self.request())

        self.assertEqual(result.status, InvocationStatus.SUCCESS)
        self.assertEqual(result.trace.input_tokens, 90)
        self.assertEqual(result.trace.output_tokens, "unknown")

    def test_bool_shaped_usage_is_rejected(self):
        # True/False 在 Python 里是 int：诚实的解析器必须拒收。
        process = FakeProcess(stdout=self.opencode_stream(
            ("ok",), step_usages=[(True, False)]))
        with patch("opencode_adapter.subprocess.Popen", return_value=process):
            result = self.adapter().invoke(self.request())

        self.assertEqual(result.status, InvocationStatus.SUCCESS)
        self.assertEqual(result.trace.input_tokens, "unknown")
        self.assertEqual(result.trace.output_tokens, "unknown")

    def test_one_bad_step_poisons_only_that_side(self):
        # step-sum 的诚实性：某一 step 的某一侧不可观测，该侧总和
        # 即不可知（部分观测污染总和）；另一侧仍然如实求和。
        process = FakeProcess(stdout=self.opencode_stream(
            ("first", "second"), step_usages=[(100, 40), ("lots", 20)]))
        with patch("opencode_adapter.subprocess.Popen", return_value=process):
            result = self.adapter().invoke(self.request())

        self.assertEqual(result.status, InvocationStatus.SUCCESS)
        self.assertEqual(result.trace.input_tokens, "unknown")
        self.assertEqual(result.trace.output_tokens, 60)

    def test_usage_without_step_finish_events_stays_unknown(self):
        process = FakeProcess(stdout=self.opencode_stream(("ok",)))
        with patch("opencode_adapter.subprocess.Popen", return_value=process):
            result = self.adapter().invoke(self.request())

        self.assertEqual(result.status, InvocationStatus.SUCCESS)
        self.assertEqual(result.trace.input_tokens, "unknown")
        self.assertEqual(result.trace.output_tokens, "unknown")

    def test_usage_parse_failure_never_fails_invocation(self):
        process = FakeProcess(stdout="not json at all \x00\xff")
        with patch("opencode_adapter.subprocess.Popen", return_value=process):
            result = self.adapter().invoke(self.request())

        self.assertEqual(result.status, InvocationStatus.SUCCESS)
        self.assertEqual(result.trace.input_tokens, "unknown")

    # -- 超时 / 取消 / 失败语义 ---------------------------------------------------

    def test_timeout_kills_process_and_records_timeout(self):
        class TimeoutProcess(FakeProcess):
            def communicate(self, input=None, timeout=None):
                raise subprocess.TimeoutExpired(cmd="opencode", timeout=timeout)

        process = TimeoutProcess()
        with patch("opencode_adapter.subprocess.Popen", return_value=process):
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
        with patch("opencode_adapter.subprocess.Popen", return_value=process):
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
        process = FakeProcess(stdout=self.opencode_stream("ok"))
        with patch("opencode_adapter.subprocess.Popen", return_value=process):
            adapter.invoke(self.request())

        self.assertEqual(adapter._processes, {})
        self.assertIn(adapter.last_invocation_id, adapter._completed)

    def test_concurrent_invocations_keep_bookkeeping_consistent(self):
        # 并发调用下 _state_lock 保证簿记不损坏：全部收尾后活动表
        # 清空、完成表恰好 N 条、每次调用都是独立的结构化结果。
        adapter = self.adapter()
        processes = [FakeProcess(stdout=self.opencode_stream("ok"))
                     for _ in range(8)]
        results = []
        lock = threading.Lock()

        def run_one():
            result = adapter.invoke(self.request())
            with lock:
                results.append(result)

        with patch("opencode_adapter.subprocess.Popen",
                   side_effect=processes):
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

    # -- trace 诚实性 ------------------------------------------------------------

    def test_trace_keeps_identity_fields_separate_and_tokens_unknown(self):
        process = FakeProcess(stdout=self.opencode_stream("ok"))
        with patch("opencode_adapter.subprocess.Popen", return_value=process):
            result = self.adapter().invoke(self.request())

        self.assertEqual(result.trace.agent_id, "coding-agent")
        self.assertEqual(result.trace.runtime, "opencode")
        self.assertEqual(result.trace.provider, "test-provider")
        self.assertEqual(result.trace.model, None)
        self.assertEqual(result.trace.role, "coder")
        # usage 缺失（fixture 无 step tokens）→ 诚实 unknown，绝不 0。
        self.assertEqual(result.trace.input_tokens, "unknown")
        self.assertEqual(result.trace.output_tokens, "unknown")

    # -- discovery -----------------------------------------------------------------

    def test_discovery_reports_version_without_capability_claim(self):
        completed = subprocess.CompletedProcess(
            args=["opencode", "--version"], returncode=0,
            stdout="opencode 0.4.5\n", stderr="")

        class ProbeSpy:
            calls = []

            @classmethod
            def run(cls, argv, **kwargs):
                cls.calls.append((argv, kwargs))
                return completed

        with patch("opencode_adapter.subprocess.run", new=ProbeSpy.run):
            discovery = self.adapter().discover()

        self.assertTrue(discovery.available)
        self.assertEqual(discovery.runtime, "opencode")
        self.assertEqual(discovery.version, "opencode 0.4.5")
        self.assertEqual(discovery.capabilities, frozenset())
        argv, kwargs = ProbeSpy.calls[0]
        self.assertEqual(argv, ["opencode", "--version"])
        self.assertFalse(kwargs["shell"])
        self.assertLessEqual(set(kwargs["env"]), {
            "PATH", "HOME", "USERPROFILE", "SYSTEMROOT",
            "TEMP", "TMP", "OPENCODE_DISABLE_AUTOUPDATE",
        })
        self.assertEqual(kwargs.get("encoding"), "utf-8")
        self.assertEqual(kwargs.get("errors"), "replace")

    def test_discovery_failure_reports_reason_without_secrets(self):
        with patch("opencode_adapter.subprocess.run",
                   side_effect=OSError("gone token=super-secret-xyz")):
            discovery = self.adapter().discover()

        self.assertFalse(discovery.available)
        self.assertNotIn("super-secret-xyz", discovery.reason)
        self.assertIn("[REDACTED]", discovery.reason)

    def test_discovery_unavailable_when_probe_rejects(self):
        completed = subprocess.CompletedProcess(
            args=["opencode", "--version"], returncode=1,
            stdout="", stderr="nope")
        with patch("opencode_adapter.subprocess.run", return_value=completed):
            discovery = self.adapter().discover()

        self.assertFalse(discovery.available)

    # -- check_authentication：只观测，不猜测 ------------------------------------------

    def test_check_authentication_authenticated_listing_maps_to_authenticated(self):
        completed = subprocess.CompletedProcess(
            args=["opencode", "auth", "list"], returncode=0,
            stdout="authenticated providers:\n- anthropic (oauth)\n",
            stderr="")

        class AuthSpy:
            calls = []

            @classmethod
            def run(cls, argv, **kwargs):
                cls.calls.append((argv, kwargs))
                return completed

        with patch("opencode_adapter.subprocess.run", new=AuthSpy.run):
            result = self.adapter().check_authentication()

        self.assertEqual(result.state, AuthenticationState.AUTHENTICATED)
        argv, kwargs = AuthSpy.calls[0]
        # 只读观测面：opencode auth list；绝不 login/logout/refresh，
        # 绝不打开 credentials 文件。
        self.assertEqual(argv, ["opencode", "auth", "list"])
        self.assertNotIn("login", argv)
        self.assertNotIn("logout", argv)
        self.assertFalse(kwargs["shell"])
        self.assertLessEqual(set(kwargs["env"]), {
            "PATH", "HOME", "USERPROFILE", "SYSTEMROOT",
            "TEMP", "TMP", "OPENCODE_DISABLE_AUTOUPDATE",
        })

    def test_check_authentication_not_authenticated_is_auth_required(self):
        completed = subprocess.CompletedProcess(
            args=["opencode", "auth", "list"], returncode=0,
            stdout="no authenticated providers\n", stderr="")
        with patch("opencode_adapter.subprocess.run", return_value=completed):
            result = self.adapter().check_authentication()

        self.assertEqual(result.state, AuthenticationState.AUTH_REQUIRED)
        self.assertEqual(result.reason_code, ReasonCode.AUTH_REQUIRED)

    def test_check_authentication_empty_listing_is_auth_required(self):
        # 空列表 = credentials 文件里没有任何已认证 provider ——
        # 诚实的 AUTH_REQUIRED，不是 UNKNOWN。
        completed = subprocess.CompletedProcess(
            args=["opencode", "auth", "list"], returncode=0,
            stdout="", stderr="")
        with patch("opencode_adapter.subprocess.run", return_value=completed):
            result = self.adapter().check_authentication()

        self.assertEqual(result.state, AuthenticationState.AUTH_REQUIRED)

    def test_check_authentication_json_shaped_listing_is_unknown(self):
        # 输出形态未实证（REAL 项）：无法可靠解释的形态一律 UNKNOWN，
        # 绝不强行 AUTHENTICATED。
        completed = subprocess.CompletedProcess(
            args=["opencode", "auth", "list"], returncode=0,
            stdout='{"providers": []}\n', stderr="")
        with patch("opencode_adapter.subprocess.run", return_value=completed):
            result = self.adapter().check_authentication()

        self.assertEqual(result.state, AuthenticationState.UNKNOWN)
        self.assertEqual(result.reason_code, ReasonCode.PROTOCOL_ERROR)

    def test_check_authentication_junk_output_is_unknown_not_faked(self):
        completed = subprocess.CompletedProcess(
            args=["opencode", "auth", "list"], returncode=0,
            stdout="total garbage \x00\x01", stderr="")
        with patch("opencode_adapter.subprocess.run", return_value=completed):
            result = self.adapter().check_authentication()

        self.assertEqual(result.state, AuthenticationState.UNKNOWN)
        self.assertEqual(result.reason_code, ReasonCode.PROTOCOL_ERROR)

    def test_check_authentication_nonzero_exit_is_auth_required(self):
        completed = subprocess.CompletedProcess(
            args=["opencode", "auth", "list"], returncode=1,
            stdout="", stderr="boom")
        with patch("opencode_adapter.subprocess.run", return_value=completed):
            result = self.adapter().check_authentication()

        self.assertEqual(result.state, AuthenticationState.AUTH_REQUIRED)

    def test_check_authentication_subprocess_failure_is_unknown(self):
        with patch("opencode_adapter.subprocess.run",
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
            args=["opencode", "auth", "list"], returncode=0,
            stdout="authenticated providers:\n- anthropic (oauth)\n",
            stderr="")
        with patch("opencode_adapter.subprocess.run", return_value=completed):
            adapter.check_authentication()
        # provider 检查不再 spawn 子进程：它由已观测的 auth 推导。
        with patch("opencode_adapter.subprocess.run",
                   side_effect=AssertionError("must not probe")):
            check = adapter.check_provider_model()

        self.assertTrue(check.available)
        self.assertEqual(check.reason_code, ReasonCode.NONE)

    def test_check_provider_model_without_provider_is_unsupported(self):
        # opencode 是多 provider harness：默认 profile provider=None
        # → 如实 UNSUPPORTED，绝不猜测默认 provider。
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
        process = FakeProcess(stdout=self.opencode_stream("OK"))
        with patch.dict(os.environ, {"RUN_REAL_PROVIDER_TESTS": "1"}):
            with patch("opencode_adapter.subprocess.Popen",
                       return_value=process):
                result = self.adapter().minimal_health_check(timeout_seconds=5)

        self.assertTrue(result.passed)
        self.assertEqual(result.reason_code, ReasonCode.NONE)
        self.assertEqual(result.output_class, "exact_ok")

    def test_minimal_health_check_unexpected_output_is_not_passed(self):
        process = FakeProcess(stdout=self.opencode_stream("something else"))
        with patch.dict(os.environ, {"RUN_REAL_PROVIDER_TESTS": "1"}):
            with patch("opencode_adapter.subprocess.Popen",
                       return_value=process):
                result = self.adapter().minimal_health_check(timeout_seconds=5)

        self.assertFalse(result.passed)
        self.assertEqual(result.reason_code, ReasonCode.PROTOCOL_ERROR)
        self.assertEqual(result.output_class, "unexpected_response")


if __name__ == "__main__":
    unittest.main()
