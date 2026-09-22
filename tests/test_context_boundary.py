"""2.8-E Context Boundary：三律 + 零携带负向闸 + 接缝钉定（§17/§30-E）。

三律原文（§17/:209 master 逐字——boundary declaration，regression
protection，非运行时策略）：
1. Context ≠ Full History
2. Memory ≠ Truth
3. UI ≠ Truth

2.8-E 接缝侧重述（§30-E 授权面三命题）：
1. Context != Full History —— 跨 run 文本绝不进入任何 prompt；
2. Conversation Record != Conversation Context —— record 是呈现视图
   原料，存在 record ≠ 注入 context；
3. Cross-run text does not enter prompt —— run N 的 request 只消费
   本 run 授权输入（本 run task 文本 + 本 run 前一步输出）。

覆盖（授权 §测试要求 ①-④）：
- 零携带负向：真链双 run（_funnel_composition_closures + 离线
  adapter 真装配真驱动）——run1 哨兵任务实入 run1 prompts（捕获
  完备性 A），run2 全部 prompt 捕获（B），任何 run2 prompt 不含
  run1 任务原文/输出哨兵/task_id（C），请求按 task_id 精确划分、
  计数闭合零漏捕（D）；
- record 存在 ≠ context 注入：run1/run2 record 铸成后再跑 run3，
  run3 prompts 不含任何 run1/run2 文本与 record 派生物（老 run →
  record → prompt 与 老 run → /runs → 隐性 context → 下一
  request 两条走私路径同钉）；
- 接缝钉定：_make_request_builder 签名九参数精确冻结（W1 增必填
  keyword-only step_index——零缺省即零双路径）、模板占位符
  恰 {role}{task}、EMBED_LIMIT 截断与 HANDOFF 发射行为不变、边界
  声明段落在库；
- usage 诚实：混合三态真 UsageRecord 聚合（仅 KNOWN 求和）、
  结构耦合（UNKNOWN 带数字被构造期拒绝）；
- 纪律静态扫描：TUI/projection 零 entry import、UI 零 prompt 接缝
  触碰。

全部离线（offline adapters / 真类型只读构造）；REAL=0。
"""
import inspect
import io
import sys
import unittest
from pathlib import Path
from string import Formatter
from types import SimpleNamespace

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

TESTS = Path(__file__).resolve().parent
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))

import cockpit_entry  # noqa: E402
import host_entry  # noqa: E402
from event_index import EventIndex  # noqa: E402
from test_conversation_entry import (  # noqa: E402
    _OfflineAdapter,
    _factories,
    _verified_evidence,
)
from usage_log import (  # noqa: E402
    UsageLogError,
    UsageObservation,
    UsageRecord,
)

_RUN1_SENTINEL = "2_8_E_RUN1_SENTINEL_7F9A"
_RUN2_MARKER = "2_8_E_RUN2_DISTINCT_3C1B"
_RUN3_MARKER = "2_8_E_RUN3_DISTINCT_9D4E"
_STEPS_PER_RUN = 3  # 默认模板 3 角色（architect/coder/reviewer）


def _offline_funnel():
    """真装配离线漏斗：3 个离线 adapter 全 VERIFIED，单一铸造现场。"""
    adapters = tuple(
        _OfflineAdapter(f"rt-{name}", f"prov-{name}") for name in "abc")
    registry, skipped = host_entry.environment_registry(
        _factories(*adapters))
    evidence = _verified_evidence(registry, "rt-a", "rt-b", "rt-c")
    funnel = cockpit_entry._funnel_composition_closures(
        registry, skipped, evidence, timeout_seconds=None,
        event_index=EventIndex(),
        task_id_mint=cockpit_entry._task_id_mint(),
        terminal_emitted=set())
    return funnel, adapters


def _drive_to_terminal(composed, cap=12):
    """驱动到终态（镜像 _drive_loop 循环律，去除线程/PARKED 等待
    ——离线 run 无停驻，PARKED 仍按外循环语义续驱）。"""
    outcome = None
    for _ in range(cap):
        outcome = composed.drive()
        status = getattr(getattr(outcome, "status", None), "value", None)
        if status != "PARKED":
            return outcome
    raise AssertionError("run did not reach terminal within cap")


def _run(funnel, task_text):
    composed = funnel.start(task_text, funnel.preview())
    outcome = _drive_to_terminal(composed)
    return composed, outcome


def _prompts(adapters, *task_ids):
    ids = set(task_ids)
    return [request.prompt
            for adapter in adapters
            for request in adapter.requests
            if request.task_id in ids]


# ------------------------------------------------ 三律文档化（§17 逐字）


class ThreeLawsDeclarationTests(unittest.TestCase):
    """三律原文在本模块 docstring 在场（静态防漂移——AC-03）。"""

    def test_module_docstring_carries_master_three_laws(self):
        # §17/:209 逐字（≠ 原符号）
        for phrase in ("Context ≠ Full History",
                       "Memory ≠ Truth",
                       "UI ≠ Truth"):
            self.assertIn(phrase, __doc__)

    def test_module_docstring_carries_seam_restatements(self):
        for phrase in (
                "Context != Full History",
                "Conversation Record != Conversation Context",
                "Cross-run text does not enter prompt"):
            self.assertIn(phrase, __doc__)

    def test_prompt_seam_docstring_carries_boundary_pin(self):
        doc = cockpit_entry._make_request_builder.__doc__
        self.assertIn("ONE AND ONLY prompt seam", doc)
        self.assertIn("Context != Full History", doc)
        self.assertIn("Conversation Record != Conversation Context", doc)
        self.assertIn("cross-run text", doc)


# ------------------------------------------------ 零携带负向（核心闸）


class ZeroCarryNegativeTests(unittest.TestCase):
    """真链双 run：run2 的任何 prompt 不含 run1 的任何文本。"""

    def test_run2_prompts_carry_nothing_from_run1(self):
        funnel, adapters = _offline_funnel()
        first, outcome1 = _run(funnel, f"build {_RUN1_SENTINEL} flange")
        second, outcome2 = _run(funnel, f"polish {_RUN2_MARKER} bracket")

        self.assertEqual(outcome1.status.value, "COMPLETED")
        self.assertEqual(outcome2.status.value, "COMPLETED")
        # 身份隔离前提：两 run task_id 互异（划分判据）
        self.assertNotEqual(first.task_id, second.task_id)

        # D（捕获完备性）：全部请求按 task_id 精确二分、计数恰合
        all_requests = [request for adapter in adapters
                        for request in adapter.requests]
        run1_requests = [request for request in all_requests
                         if request.task_id == first.task_id]
        run2_requests = [request for request in all_requests
                         if request.task_id == second.task_id]
        self.assertEqual(len(all_requests),
                         len(run1_requests) + len(run2_requests))
        self.assertEqual(len(run1_requests), _STEPS_PER_RUN)
        self.assertEqual(len(run2_requests), _STEPS_PER_RUN)

        # A（哨兵真值前提）：run1 哨兵确曾实入 run1 全部 prompt
        run1_prompts = [request.prompt for request in run1_requests]
        self.assertEqual(len(run1_prompts), _STEPS_PER_RUN)
        self.assertTrue(all(
            _RUN1_SENTINEL in prompt for prompt in run1_prompts))

        # B（run2 捕获在场）：run2 全部 prompt 含 run2 任务标记
        run2_prompts = [request.prompt for request in run2_requests]
        self.assertTrue(all(
            _RUN2_MARKER in prompt for prompt in run2_prompts))

        # C（零携带核心断言）：run2 任何 prompt 不含 run1 任务原文/
        #    run1 task_id/任何 run1 步输出（离线 adapter 计数为
        #    per-runtime 递增：run1 三步输出 = *-1、run2 = *-2）
        run1_outputs = tuple(
            f"output-rt-{name}-1" for name in "abc")
        for prompt in run2_prompts:
            self.assertNotIn(_RUN1_SENTINEL, prompt)
            self.assertNotIn(first.task_id, prompt)
            for output in run1_outputs:
                self.assertNotIn(output, prompt)

        # 同 run handoff 只嵌本 run 先前输出（run2 步 2/3 嵌 run2
        # 步 1/2 输出——嵌入语义与 run 间边界正交）
        self.assertIn("output-rt-a-2", run2_prompts[1])
        self.assertIn("output-rt-b-2", run2_prompts[2])

    def test_record_existence_is_not_context_injection(self):
        """record 铸成后再跑 run3：run3 prompts 不含 run1/run2 文本
        与 record 派生物（老 run→record→prompt / 老 run→/runs→隐性
        context→下一 request 两条走私路径同钉）。"""
        from cockpit_projection import (
            conversation_record_lines,
            runs_summary_lines,
        )
        funnel, adapters = _offline_funnel()
        first, outcome1 = _run(funnel, f"build {_RUN1_SENTINEL} flange")
        second, outcome2 = _run(funnel, f"polish {_RUN2_MARKER} bracket")

        # record 铸成（终态时点快照）——存在本身不得成为注入源
        records = [
            cockpit_entry.conversation_record(first, outcome1, "DONE"),
            cockpit_entry.conversation_record(second, outcome2, "DONE"),
        ]
        self.assertEqual(records[0].status, "COMPLETED")
        self.assertEqual(records[0].usage.unknown_count, _STEPS_PER_RUN)
        # /runs 呈现面全量渲染（摘要 + 详情块）后仍不构成 context
        rendered = list(runs_summary_lines(
            ((records[0].task, records[0].steps, records[0].status),
             (records[1].task, records[1].steps, records[1].status))))
        for record in records:
            rendered.extend(conversation_record_lines(record, width=100))

        third, outcome3 = _run(funnel, f"ship {_RUN3_MARKER} widget")
        self.assertEqual(outcome3.status.value, "COMPLETED")
        run3_prompts = _prompts(adapters, third.task_id)
        self.assertEqual(len(run3_prompts), _STEPS_PER_RUN)
        self.assertTrue(all(
            _RUN3_MARKER in prompt for prompt in run3_prompts))
        forbidden = (
            _RUN1_SENTINEL, _RUN2_MARKER,
            first.task_id, second.task_id,
            "output-rt-a-1", "output-rt-b-1", "output-rt-c-1",
            "output-rt-a-2", "output-rt-b-2", "output-rt-c-2",
        )
        for prompt in run3_prompts:
            for text in forbidden:
                self.assertNotIn(text, prompt)


# ------------------------------------------------ 接缝钉定（FROZEN 行为）


class SeamPinTests(unittest.TestCase):
    """_make_request_builder 签名/模板/嵌入行为逐项钉定（AC-02）。"""

    def test_signature_is_frozen(self):
        # 9 参数（5 位置 + 4 keyword-only：观察接缝三参 + W1 必填
        # step_index）——逐名逐序钉定；step_index 无缺省（零双路径）
        signature = inspect.signature(
            cockpit_entry._make_request_builder)
        parameters = list(signature.parameters.values())
        self.assertEqual(
            [parameter.name for parameter in parameters],
            ["task_text", "task_id", "role", "provider",
             "timeout_seconds", "emit", "runtime_id", "previous_role",
             "step_index"])
        for parameter in parameters[5:]:
            self.assertEqual(parameter.kind, inspect.Parameter.KEYWORD_ONLY)
        self.assertIs(parameters[8].default, inspect.Parameter.empty)

    def test_prompt_template_placeholders_exactly_role_and_task(self):
        fields = {
            field_name
            for _, field_name, _, _ in
            Formatter().parse(cockpit_entry._PROMPT_TEMPLATE)
            if field_name is not None
        }
        self.assertEqual(fields, {"role", "task"})
        previous_fields = {
            field_name
            for _, field_name, _, _ in
            Formatter().parse(cockpit_entry._PROMPT_PREVIOUS_SECTION)
            if field_name is not None
        }
        self.assertEqual(previous_fields, set())

    def test_builder_closure_carries_only_current_run_inputs(self):
        # 无先前输出：纯模板 prompt、零 HANDOFF 发射
        emitted = []

        def emit(event_type, **fields):
            emitted.append((event_type, fields))

        builder = cockpit_entry._make_request_builder(
            "seam task", "tid-seam", "coder", "prov-x", 30.0,
            emit=emit, runtime_id="rt-x", previous_role="architect",
            step_index=1)
        request = builder(None)
        self.assertIn("seam task", request.prompt)
        self.assertNotIn("PREVIOUS STEP OUTPUT", request.prompt)
        self.assertEqual(emitted, [])

    def test_embed_limit_truncates_and_emits_single_handoff(self):
        emitted = []

        def emit(event_type, **fields):
            emitted.append((event_type, fields))

        builder = cockpit_entry._make_request_builder(
            "seam task", "tid-seam", "coder", "prov-x", 30.0,
            emit=emit, runtime_id="rt-x", previous_role="architect",
            step_index=1)
        long_output = "A" * (cockpit_entry._EMBED_LIMIT + 500)
        request = builder(
            SimpleNamespace(
                output=long_output,
                trace=SimpleNamespace(invocation_id="inv-seam-prior")))
        self.assertIn(cockpit_entry._PROMPT_PREVIOUS_SECTION, request.prompt)
        # verbatim 截断：恰 _EMBED_LIMIT 字符嵌入、尾部 500 字不在场
        self.assertIn("A" * cockpit_entry._EMBED_LIMIT, request.prompt)
        self.assertNotIn("A" * (cockpit_entry._EMBED_LIMIT + 1),
                         request.prompt)
        self.assertEqual(len(emitted), 1)
        event_type, fields = emitted[0]
        self.assertEqual(getattr(event_type, "value", event_type),
                         "HANDOFF")
        self.assertEqual(
            fields, {"stage": "architect", "runtime_id": "rt-x",
                     "status": "EMBEDDED", "reason": "EMBEDDED"})


# ------------------------------------------------ usage 三态诚实（§20）


class UsageHonestyTests(unittest.TestCase):
    """混合桶聚合仅 KNOWN 求和；结构耦合拒绝非法形状（AC-05）。"""

    @staticmethod
    def _record(invocation_id, usage_status, input_tokens=None,
                output_tokens=None):
        return UsageRecord(
            invocation_id=invocation_id, task_id="tid-u",
            agent_id=f"agent-{invocation_id}", role="coder",
            runtime_id="rt-b", status="SUCCESS",
            usage_status=usage_status,
            input_tokens=input_tokens, output_tokens=output_tokens)

    def test_mixed_buckets_sum_known_count_others(self):
        records = (
            self._record("i1", UsageObservation.KNOWN, 100, 40),
            self._record("i2", UsageObservation.KNOWN, 23, 9),
            self._record("i3", UsageObservation.UNKNOWN),
            self._record("i4", UsageObservation.UNSUPPORTED),
        )
        summary = cockpit_entry.usage_summary(records)
        self.assertEqual(
            summary,
            cockpit_entry.UsageSummary(123, 49, 1, 1))

    def test_empty_records_are_all_zero(self):
        self.assertEqual(
            cockpit_entry.usage_summary(()),
            cockpit_entry.UsageSummary(0, 0, 0, 0))

    def test_unknown_with_tokens_is_structurally_rejected(self):
        with self.assertRaises(UsageLogError):
            self._record("bad", UsageObservation.UNKNOWN, 5, 5)
        with self.assertRaises(UsageLogError):
            UsageRecord(
                invocation_id="bad2", task_id="tid-u", agent_id="a",
                role="coder", runtime_id="rt-b", status="SUCCESS",
                usage_status=UsageObservation.KNOWN,
                input_tokens=5, output_tokens=None)

    def test_real_offline_run_usage_is_honestly_unknown(self):
        # 真链：离线 invocation 无 token 数字 → 全 UNKNOWN 计数，
        # 零编造（UNKNOWN 绝不进和）
        funnel, _adapters = _offline_funnel()
        composed, outcome = _run(funnel, "usage honesty probe")
        summary = cockpit_entry.usage_summary(composed.usage())
        self.assertEqual(
            summary,
            cockpit_entry.UsageSummary(0, 0, _STEPS_PER_RUN, 0))


# ------------------------------------------------ 纪律静态扫描（§31 扩面）


class DisciplineGuardTests(unittest.TestCase):
    """分层纪律：TUI/projection 零 entry import；UI 零 prompt 接缝。"""

    def test_tui_never_imports_entry(self):
        source = (SCRIPTS / "cockpit_tui.py").read_text(encoding="utf-8")
        self.assertNotIn("import cockpit_entry", source)
        self.assertNotIn("from cockpit_entry", source)

    def test_projection_never_imports_entry(self):
        source = (SCRIPTS / "cockpit_projection.py").read_text(
            encoding="utf-8")
        self.assertNotIn("import cockpit_entry", source)
        self.assertNotIn("from cockpit_entry", source)

    def test_tui_never_touches_prompt_seam(self):
        source = (SCRIPTS / "cockpit_tui.py").read_text(encoding="utf-8")
        self.assertNotIn("_make_request_builder", source)
        self.assertNotIn("ExternalAgentRequest(", source)

    def test_record_builder_is_pure_no_engine_write(self):
        # conversation_record 源级只读：无 dispatch/submit/append 写面
        source = io.StringIO(inspect.getsource(
            cockpit_entry.conversation_record)).read()
        for forbidden in ("dispatch", "submit", "append", "observe"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
