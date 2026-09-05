"""ClineAdapter 的离线、确定性测试（R1 P2 — TDD RED→GREEN）。

本文件先于 cline_adapter.py 存在（RED：模块缺席时 collection 失败）。
全部用 fake 进程对象替身驱动：不调用真实 cline、不访问网络、不读取
凭据，也不依赖本机是否安装了 Cline CLI。仅有的真实 subprocess 调用点
（_probe 的 subprocess.run、invoke 的 Popen）全部被 patch。

Cline CLI 非交互形态（本 adapter 的目标面，均离线 fixture 证明，
不做任何 REAL 断言；证据=官方 apps/cli/README + P2 边界调查）：
- 非交互调用：`--json` 输出 NDJSON 事件流，官方要求 prompt 为 argv
  参数或 piped stdin —— 本 adapter 选 piped stdin（与 claude/pi/
  gemini/qwen 家族一致：规避 argv 长度限制，prompt 永不进入 shell
  可达位置）
- 事件形态（官方 jq 示例原文）：`.type == "agent_event"` 且文本在
  `.event.text`；终端/usage 事件形态未证（REAL 前不做任何假设）
- L0 边界：CLI 没有只读 auth 状态面（`cline auth` 是交互登录动作、
  `--key` 是凭据材料、env key 白名单不转发）—— health 三方法必须
  缺席而非伪造（conformance 家族纪律，tiny-agents 先例）
- usage：NDJSON 的 usage 字段形态未证 → HONEST_UNKNOWN，token 形
  文本绝不刮取
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

from cline_adapter import ClineAdapter
from external_runtime import ExternalAgentRequest, InvocationStatus, RuntimeProfile


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


class ClineAdapterTests(unittest.TestCase):
    def profile(self, provider="cline"):
        return RuntimeProfile(
            agent_id="coding-agent",
            runtime="cline-cli",
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

    def adapter(self, provider="cline"):
        return ClineAdapter(profile=self.profile(provider), executable="cline")

    @staticmethod
    def cline_ndjson(texts=("ok",), other_events=()):
        """`cline --json` NDJSON 流的离线 fixture：agent_event 文本事件
        形态取自官方 jq 示例（type=="agent_event"、文本在 event.text）。"""
        if isinstance(texts, str):
            texts = (texts,)
        lines = [json.dumps(event) for event in other_events]
        lines.extend(json.dumps({"type": "agent_event", "event": {"text": text}})
                     for text in texts)
        return "\n".join(lines) + "\n"

    # -- 方法事实面：L0（三个协议方法；health 不得伪造） ------------------------

    def test_protocol_three_methods_exist(self):
        for name in ("discover", "invoke", "cancel"):
            self.assertTrue(callable(getattr(ClineAdapter, name)), name)

    def test_health_methods_must_not_be_faked(self):
        # Cline CLI 没有可观测的只读 auth 状态面：提供 health 方法就
        # 意味着伪造语义 —— conformance L0 纪律（tiny-agents 先例）。
        for name in ("check_authentication", "check_provider_model",
                     "minimal_health_check"):
            self.assertFalse(callable(getattr(ClineAdapter, name, None)), name)

    # -- invoke：argv / stdin / env / UTF-8 ------------------------------------

    def test_invoke_uses_json_flag_with_stdin_prompt(self):
        process = FakeProcess(stdout=self.cline_ndjson("ok"))
        with patch("cline_adapter.subprocess.Popen", return_value=process) as popen:
            result = self.adapter().invoke(self.request())

        self.assertEqual(result.status, InvocationStatus.SUCCESS)
        self.assertEqual(result.output, "ok")
        self.assertEqual(result.trace.runtime, "cline-cli")
        self.assertEqual(result.trace.status, InvocationStatus.SUCCESS)
        self.assertEqual(result.trace.exit_code, 0)
        # prompt 经 stdin 传入（不进 argv，永不进入 shell 可达位置）。
        self.assertEqual(process.calls, [("Return exactly OK and nothing else.", 3)])
        argv = popen.call_args.args[0]
        self.assertEqual(argv, ["cline", "--json"])
        self.assertFalse(popen.call_args.kwargs["shell"])

    def test_invoke_appends_model_flag_when_requested(self):
        process = FakeProcess(stdout=self.cline_ndjson("ok"))
        with patch("cline_adapter.subprocess.Popen", return_value=process) as popen:
            self.adapter().invoke(self.request(model="test-model"))

        argv = popen.call_args.args[0]
        self.assertEqual(argv, ["cline", "--json", "--model", "test-model"])

    def test_invoke_env_is_whitelist_only(self):
        process = FakeProcess(stdout=self.cline_ndjson("ok"))
        with patch("cline_adapter.subprocess.Popen", return_value=process) as popen:
            self.adapter().invoke(self.request())

        env = popen.call_args.kwargs["env"]
        self.assertIsInstance(env, dict)
        self.assertIn("PATH", env)
        self.assertLessEqual(
            set(env), {"PATH", "HOME", "USERPROFILE", "SYSTEMROOT",
             "TEMP", "TMP"})

    def test_invoke_does_not_forward_credential_or_cline_env(self):
        # 即使父环境带着凭据/CLI 配置变量，子进程也一概收不到：
        # cline 的凭据面（--key > CLINE_API_KEY 等环境变量）全部被
        # 白名单挡在子进程外；状态读取走 ~/.cline 已存凭据。
        process = FakeProcess(stdout=self.cline_ndjson("ok"))
        injected = {
            "CLINE_API_KEY": "ck-parent-secret-123456",
            "ANTHROPIC_API_KEY": "sk-parent-secret-123456",
            "CLINE_DATA_DIR": "D:\\elsewhere",
            "CLINE_SANDBOX": "1",
            "CLINE_LOG_LEVEL": "trace",
        }
        with patch.dict(os.environ, injected, clear=False):
            with patch("cline_adapter.subprocess.Popen",
                       return_value=process) as popen:
                self.adapter().invoke(self.request())

        env = popen.call_args.kwargs["env"]
        for var in injected:
            self.assertNotIn(var, env, var)
        for key in env:
            self.assertFalse(key.upper().startswith("CLINE_"), key)

    def test_invoke_decodes_child_streams_as_utf_8(self):
        process = FakeProcess(stdout=self.cline_ndjson("ok"))
        with patch("cline_adapter.subprocess.Popen", return_value=process) as popen:
            self.adapter().invoke(self.request())

        self.assertEqual(popen.call_args.kwargs.get("encoding"), "utf-8")
        self.assertEqual(popen.call_args.kwargs.get("errors"), "replace")

    def test_invoke_survives_non_ascii_stdout(self):
        # errors="replace"：解码绝不抛异常；调用本身必须成功收尾。
        process = FakeProcess(stdout=self.cline_ndjson("résumé → 中文 ✓"))
        with patch("cline_adapter.subprocess.Popen", return_value=process):
            result = self.adapter().invoke(self.request())

        self.assertEqual(result.status, InvocationStatus.SUCCESS)
        self.assertEqual(result.output, "résumé → 中文 ✓")

    def test_stderr_never_breaks_stdout_parse(self):
        # adapter 只信任 stdout 的 NDJSON；stderr 的诊断文本（心跳、
        # verbose stats 等）不参与解析。
        process = FakeProcess(
            stdout=self.cline_ndjson("ok"),
            stderr="[cline] waiting for provider capacity...\n")
        with patch("cline_adapter.subprocess.Popen", return_value=process):
            result = self.adapter().invoke(self.request())

        self.assertEqual(result.status, InvocationStatus.SUCCESS)
        self.assertEqual(result.output, "ok")

    # -- NDJSON 解析策略（官方 jq 示例形态 + 家族 raw-fallback 契约） -----------

    def test_multiple_text_events_are_joined_in_order(self):
        # 官方 jq 示例把每条 agent_event 的文本作为独立输出块（jq -r
        # 恰以换行连接）—— 同语义：按序以换行连接。
        process = FakeProcess(stdout=self.cline_ndjson(["first", "second"]))
        with patch("cline_adapter.subprocess.Popen", return_value=process):
            result = self.adapter().invoke(self.request())

        self.assertEqual(result.status, InvocationStatus.SUCCESS)
        self.assertEqual(result.output, "first\nsecond")

    def test_other_event_types_do_not_contribute_text(self):
        # 混合事件流（工具、状态等未证形态）：只有 agent_event 的
        # event.text 进入输出，其余原样忽略。
        process = FakeProcess(stdout=self.cline_ndjson(
            texts=("the answer",),
            other_events=(
                {"type": "session_start", "cwd": "/tmp"},
                {"type": "tool_call", "tool": "read_file"},
            )))
        with patch("cline_adapter.subprocess.Popen", return_value=process):
            result = self.adapter().invoke(self.request())

        self.assertEqual(result.status, InvocationStatus.SUCCESS)
        self.assertEqual(result.output, "the answer")

    def test_blank_lines_are_tolerated(self):
        stdout = "\n".join([
            json.dumps({"type": "agent_event", "event": {"text": "ok"}}),
            "",
            "   ",
        ]) + "\n"
        process = FakeProcess(stdout=stdout)
        with patch("cline_adapter.subprocess.Popen", return_value=process):
            result = self.adapter().invoke(self.request())

        self.assertEqual(result.status, InvocationStatus.SUCCESS)
        self.assertEqual(result.output, "ok")

    def test_empty_stdout_is_failed_not_forged_success(self):
        # 完全没有结构化输出 = 诚实 FAILED，绝不伪造成功。
        process = FakeProcess(stdout="")
        with patch("cline_adapter.subprocess.Popen", return_value=process):
            result = self.adapter().invoke(self.request())

        self.assertEqual(result.status, InvocationStatus.FAILED)
        self.assertEqual(result.trace.status, InvocationStatus.FAILED)

    def test_ndjson_without_text_events_is_failed(self):
        # 事件流可解析但零条 agent_event 文本（如只有 session/tool 事件）
        # —— 无法证明产出 → FAILED + 截断 raw 保留现场。
        stdout = json.dumps({"type": "session_start", "cwd": "/tmp"}) + "\n"
        process = FakeProcess(stdout=stdout)
        with patch("cline_adapter.subprocess.Popen", return_value=process):
            result = self.adapter().invoke(self.request())

        self.assertEqual(result.status, InvocationStatus.FAILED)
        self.assertIn("session_start", result.error)

    def test_non_text_event_text_shapes_do_not_crash(self):
        # event.text 非 str（数字/布尔/缺失）→ 该事件不产出文本；整体
        # 零文本时如实 FAILED，绝不崩溃、绝不把非文本当内容。
        stdout = "\n".join([
            json.dumps({"type": "agent_event", "event": {"text": 7}}),
            json.dumps({"type": "agent_event", "event": {"text": True}}),
            json.dumps({"type": "agent_event", "event": {}}),
            json.dumps({"type": "agent_event"}),
        ]) + "\n"
        process = FakeProcess(stdout=stdout)
        with patch("cline_adapter.subprocess.Popen", return_value=process):
            result = self.adapter().invoke(self.request())

        self.assertEqual(result.status, InvocationStatus.FAILED)

    def test_unparseable_line_falls_back_to_raw_output(self):
        # 任一行不可解析 → stdout 不是可靠 NDJSON → 家族 raw-fallback
        # 契约：调用级成功 + 原样 raw 文本，内容裁决权在上游 packet
        # 验证与内容扫描；本 adapter 绝不伪造内容可用性。
        stdout = ("just plain text\nmore plain text\n")
        process = FakeProcess(stdout=stdout)
        with patch("cline_adapter.subprocess.Popen", return_value=process):
            result = self.adapter().invoke(self.request())

        self.assertEqual(result.status, InvocationStatus.SUCCESS)
        self.assertEqual(result.output, "just plain text\nmore plain text")

    def test_non_object_json_line_falls_back_to_raw_output(self):
        # 行是合法 JSON 但不是事件对象（数字/字符串/数组）—— 这同样
        # 不是事件流形态，走 raw-fallback 而非猜测语义。
        stdout = "5\n\"a string\"\n[1, 2]\n"
        process = FakeProcess(stdout=stdout)
        with patch("cline_adapter.subprocess.Popen", return_value=process):
            result = self.adapter().invoke(self.request())

        self.assertEqual(result.status, InvocationStatus.SUCCESS)
        self.assertEqual(result.output, "5\n\"a string\"\n[1, 2]")

    def test_raw_text_with_nonzero_exit_is_failed(self):
        # raw 文本 + 非零退出 = 真失败，绝不 raw-fallback 成成功。
        process = FakeProcess(stdout="panic text\n", stderr="", returncode=1)
        with patch("cline_adapter.subprocess.Popen", return_value=process):
            result = self.adapter().invoke(self.request())

        self.assertEqual(result.status, InvocationStatus.FAILED)

    def test_auth_fail_fast_error_is_failed_with_detail_kept(self):
        # 官方承诺：非交互运行遇 OAuth provider 无已存凭据时快速失败
        # （不弹浏览器）。这类失败走正常 FAILED 面（stderr 细节经抹除
        # 保留）—— auth 分类由上游依据错误文本裁决，本模块绝不猜测。
        process = FakeProcess(
            stdout="",
            stderr=("error: no saved credentials for provider 'cline'; "
                    "authenticate first with `cline auth cline`"),
            returncode=1)
        with patch("cline_adapter.subprocess.Popen", return_value=process):
            result = self.adapter().invoke(self.request())

        self.assertEqual(result.status, InvocationStatus.FAILED)
        self.assertIn("no saved credentials", result.error)

    def test_unknown_flag_error_is_failed(self):
        # 旧版/变体 CLI 不认识 --json —— 非零退出 + 错误文本，诚实
        # FAILED（细节经抹除保留）。
        process = FakeProcess(
            stdout="",
            stderr="error: unknown option '--json'",
            returncode=2)
        with patch("cline_adapter.subprocess.Popen", return_value=process):
            result = self.adapter().invoke(self.request())

        self.assertEqual(result.status, InvocationStatus.FAILED)
        self.assertEqual(result.trace.exit_code, 2)
        self.assertIn("--json", result.error)

    # -- usage 诚实性：HONEST_UNKNOWN（REAL 核对前不刮取任何 token 形态） -------

    def test_token_shaped_text_is_never_scraped(self):
        # token 形文本出现在事件文本里也绝不进入 usage 字段。
        stdout = self.cline_ndjson("ok token_usage=123 tokens: 456")
        process = FakeProcess(stdout=stdout)
        with patch("cline_adapter.subprocess.Popen", return_value=process):
            result = self.adapter().invoke(self.request())

        self.assertEqual(result.status, InvocationStatus.SUCCESS)
        self.assertEqual(result.trace.input_tokens, "unknown")
        self.assertEqual(result.trace.output_tokens, "unknown")

    def test_unverified_usage_fields_are_ignored(self):
        # NDJSON 里出现 usage 形态的字段（REAL 前形态未证）—— 不解析、
        # 不猜测：观测面尚未申报，一律保持 unknown。
        stdout = self.cline_ndjson(
            texts=("ok",),
            other_events=({"type": "run_stats", "usage":
                           {"input_tokens": 111, "output_tokens": 22}},))
        process = FakeProcess(stdout=stdout)
        with patch("cline_adapter.subprocess.Popen", return_value=process):
            result = self.adapter().invoke(self.request())

        self.assertEqual(result.status, InvocationStatus.SUCCESS)
        self.assertEqual(result.trace.input_tokens, "unknown")
        self.assertEqual(result.trace.output_tokens, "unknown")

    # -- 超时 / 取消 / 失败语义 ---------------------------------------------------

    def test_timeout_kills_process_and_records_timeout(self):
        class TimeoutProcess(FakeProcess):
            def communicate(self, input=None, timeout=None):
                raise subprocess.TimeoutExpired(cmd="cline", timeout=timeout)

        process = TimeoutProcess()
        with patch("cline_adapter.subprocess.Popen", return_value=process):
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
        with patch("cline_adapter.subprocess.Popen", return_value=process):
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
        process = FakeProcess(stdout=self.cline_ndjson("ok"))
        with patch("cline_adapter.subprocess.Popen", return_value=process):
            adapter.invoke(self.request())

        self.assertEqual(adapter._processes, {})
        self.assertIn(adapter.last_invocation_id, adapter._completed)

    def test_concurrent_invocations_keep_bookkeeping_consistent(self):
        # 并发调用下 _state_lock 保证簿记不损坏：全部收尾后活动表
        # 清空、完成表恰好 N 条、每次调用都是独立的结构化结果。
        adapter = self.adapter()
        processes = [FakeProcess(stdout=self.cline_ndjson("ok"))
                     for _ in range(8)]
        results = []
        lock = threading.Lock()

        def run_one():
            result = adapter.invoke(self.request())
            with lock:
                results.append(result)

        with patch("cline_adapter.subprocess.Popen", side_effect=processes):
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
        with patch("cline_adapter.subprocess.Popen", return_value=process):
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
        with patch("cline_adapter.subprocess.Popen", return_value=process):
            result = self.adapter().invoke(self.request())

        self.assertEqual(result.status, InvocationStatus.FAILED)
        self.assertNotIn("sk-live-secret123456", result.error)
        self.assertNotIn("tok-xyz987654", result.error)
        self.assertIn("[REDACTED]", result.error)

    def test_oserror_failure_is_sanitized_not_raw(self):
        with patch("cline_adapter.subprocess.Popen",
                   side_effect=OSError("no such file api_key=raw-secret-value")):
            result = self.adapter().invoke(self.request())

        self.assertEqual(result.status, InvocationStatus.UNAVAILABLE)
        self.assertNotIn("raw-secret-value", result.error)
        self.assertIn("[REDACTED]", result.error)

    def test_safe_error_redacts_family_key_shapes(self):
        redactor = ClineAdapter._safe_error
        text = ("api_key=alpha token: beta secret=gamma "
                "Authorization: Bearer delta "
                "hf_1234567890 sk-abcdefghij "
                "cline_api_key=ck-omega123456")
        redacted = redactor(text)
        for secret in ("alpha", "beta", "gamma", "delta", "hf_1234567890",
                       "sk-abcdefghij", "ck-omega123456"):
            self.assertNotIn(secret, redacted, secret)
        self.assertGreaterEqual(redacted.count("[REDACTED]"), 7)

    # -- trace 诚实性 ------------------------------------------------------------

    def test_trace_keeps_identity_fields_separate_and_tokens_unknown(self):
        process = FakeProcess(stdout=self.cline_ndjson("ok"))
        with patch("cline_adapter.subprocess.Popen", return_value=process):
            result = self.adapter().invoke(self.request())

        self.assertEqual(result.trace.agent_id, "coding-agent")
        self.assertEqual(result.trace.runtime, "cline-cli")
        self.assertEqual(result.trace.provider, "test-provider")
        self.assertEqual(result.trace.model, None)
        self.assertEqual(result.trace.role, "coder")
        # HONEST_UNKNOWN：usage 观测面未申报 → 诚实 unknown，绝不 0。
        self.assertEqual(result.trace.input_tokens, "unknown")
        self.assertEqual(result.trace.output_tokens, "unknown")

    # -- discovery -----------------------------------------------------------------

    def test_discovery_reports_version_without_capability_claim(self):
        completed = subprocess.CompletedProcess(
            args=["cline", "--version"], returncode=0,
            stdout="cline 3.0.7\n", stderr="")

        class ProbeSpy:
            calls = []

            @classmethod
            def run(cls, argv, **kwargs):
                cls.calls.append((argv, kwargs))
                return completed

        with patch("cline_adapter.subprocess.run", new=ProbeSpy.run):
            discovery = self.adapter().discover()

        self.assertTrue(discovery.available)
        self.assertEqual(discovery.runtime, "cline-cli")
        self.assertEqual(discovery.version, "cline 3.0.7")
        self.assertEqual(discovery.capabilities, frozenset())
        argv, kwargs = ProbeSpy.calls[0]
        self.assertEqual(argv, ["cline", "--version"])
        self.assertFalse(kwargs["shell"])
        self.assertLessEqual(
            set(kwargs["env"]), {"PATH", "HOME", "USERPROFILE", "SYSTEMROOT",
             "TEMP", "TMP"})
        self.assertEqual(kwargs.get("encoding"), "utf-8")
        self.assertEqual(kwargs.get("errors"), "replace")

    def test_discovery_failure_reports_reason_without_secrets(self):
        with patch("cline_adapter.subprocess.run",
                   side_effect=OSError("gone token=super-secret-xyz")):
            discovery = self.adapter().discover()

        self.assertFalse(discovery.available)
        self.assertNotIn("super-secret-xyz", discovery.reason)
        self.assertIn("[REDACTED]", discovery.reason)

    def test_discovery_unavailable_when_probe_rejects(self):
        completed = subprocess.CompletedProcess(
            args=["cline", "--version"], returncode=1,
            stdout="", stderr="nope")
        with patch("cline_adapter.subprocess.run", return_value=completed):
            discovery = self.adapter().discover()

        self.assertFalse(discovery.available)

    def test_missing_executable_is_honest_absence(self):
        with patch("cline_adapter.shutil.which", return_value=None):
            adapter = ClineAdapter.from_environment()

        self.assertIsNone(adapter)

    def test_from_environment_builds_adapter_when_present(self):
        with patch("cline_adapter.shutil.which",
                   return_value="/fake/bin/cline"):
            adapter = ClineAdapter.from_environment()

        self.assertIsNotNone(adapter)
        self.assertEqual(adapter.profile.runtime, "cline-cli")
        self.assertEqual(adapter.profile.provider, "cline")
        self.assertEqual(adapter.profile.model, None)


if __name__ == "__main__":
    unittest.main()
