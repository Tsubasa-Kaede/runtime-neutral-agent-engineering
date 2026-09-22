"""CU-CONTEXT W3-Q — COMPILER METADATA DISCLOSURE 资格钉定测试。

对 W3-D 已批准设计的资格钉定（qualification），不是 disclosure 实现：
在零生产改动前提下，以当前真实代码结构证明编译元数据的边界事实——
未来任何披露（W3-P1/P2 呈现层授权）都不得改变这些事实。

钉定链（当前结构）：

    cockpit_entry（接缝，prompt 只消费 segments；W3-P 起另作
    只读披露转录——元数据唯一呈现消费者）
      → cockpit_context_wire（唯一公共面）
        → cockpit_context.compile_context（元数据唯一起源）
           metadata（selection_notes / budget / truncations /
           token_measure=UNKNOWN / policy_fingerprint）→ 唯一
           呈现消费者 = entry 披露映射（W3-P，只读、零 prompt 影响）

边界律：Compiler ≠ Observation Truth；Metadata ≠ Usage Truth；
Projection ≠ Source of Truth；UNKNOWN ≠ 0；Context ≠ Prompt Text；
UI ≠ Source of Truth。embedded_chars 永不表述为 input_tokens；
runtime usage 真值恒属 UsageCapture / InvocationTrace 域。

测试面：AST import 图 / dataclass 字段集 / 源结构扫描 / 纯函数行为。
零行号依赖；全部离线；REAL=0。
"""
import ast
import dataclasses
import inspect
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import cockpit_context_wire  # noqa: E402
import cockpit_entry  # noqa: E402
from cockpit_compile import (  # noqa: E402
    CompileModelError,
    CompilePolicy,
    CompiledInvocationContext,
    TokenMeasure,
    TokenStatus,
)
from cockpit_context_wire import compile_invocation_context  # noqa: E402
from cockpit_projection import ProjectionInputs  # noqa: E402
from control_journal import ControlFactType  # noqa: E402
from execution_observation import ExecutionEvent, ExecutionEventType  # noqa: E402
from external_runtime import ExternalAgentRequest, InvocationStatus, InvocationTrace  # noqa: E402

_ENTRY_SOURCE = Path(cockpit_entry.__file__).read_text(encoding="utf-8")
_WIRE_SOURCE = Path(cockpit_context_wire.__file__).read_text(encoding="utf-8")

_COMPILE_DOMAIN_ROOTS = ("cockpit_compile", "cockpit_context",
                         "cockpit_context_wire")


def _production_sources():
    for path in sorted(SCRIPTS.glob("*.py")):
        yield path.name, path.read_text(encoding="utf-8")


def _files_containing(token):
    return {name for name, text in _production_sources() if token in text}


def _imported_roots(filename):
    """AST 提取某生产文件的全部顶层 import 根名（含相对导入等价名）。"""
    tree = ast.parse((SCRIPTS / filename).read_text(encoding="utf-8"))
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                roots.add(node.module.split(".")[0])
            else:  # 纯相对导入 ".foo"
                for alias in node.names:
                    roots.add(alias.name.split(".")[0])
    return roots


def _prior_result(output="prior output text", invocation_id="w3-inv-prior"):
    return SimpleNamespace(
        output=output,
        trace=SimpleNamespace(invocation_id=invocation_id,
                              started_at=None, finished_at=None))


class MetadataProvenanceTests(unittest.TestCase):
    """事实 1/3：元数据唯一起源于 compile_context；类型符号不出
    编译域；W3-P 后属性词的额外出现处 = entry 披露转录（唯一）。"""

    def test_metadata_type_symbols_live_solely_in_compiler_module(self):
        for token in ("BudgetReport", "SelectionNote", "TruncationFact",
                      "CompiledInvocationContext("):
            self.assertEqual(_files_containing(token),
                             {"cockpit_compile.py"}, msg=token)

    def test_metadata_attribute_reads_only_in_compiler_and_entry_disclosure(
            self):
        # W3-P 迁移：entry 成为元数据的唯一呈现消费者——属性词
        # 只额外出现在 cockpit_entry.py 的 _compile_disclosure_record
        # 只读转录处；selection_notes 同时是呈现域 CompileDisclosure
        # 的字段名（忠实转录记录，cockpit_projection.py）。
        self.assertEqual(_files_containing("selection_notes"),
                         {"cockpit_compile.py", "cockpit_entry.py",
                          "cockpit_projection.py"})
        self.assertEqual(_files_containing("token_measure"),
                         {"cockpit_compile.py", "cockpit_entry.py"})

    def test_production_call_chain_is_exactly_two_nodes(self):
        # compile_context：定义 + wire 唯一调用
        self.assertEqual(_files_containing("compile_context("),
                         {"cockpit_compile.py", "cockpit_context_wire.py"})
        # compile_invocation_context：定义 + entry 唯一调用
        self.assertEqual(_files_containing("compile_invocation_context("),
                         {"cockpit_context_wire.py", "cockpit_entry.py"})


class SeamConsumptionTests(unittest.TestCase):
    """事实 2/4/5：prompt 组装只消费 segments；元数据不进 prompt/
    request（元数据读 = 披露转录专用，字节 oracle 仍 segments-only）。"""

    def test_entry_reads_on_compiled_are_prompt_and_disclosure_only(self):
        # W3-P 迁移：entry 对编译产物的全部属性读 = prompt 组装
        # （segments）+ 披露只读转录（六元数据面，唯一去向 =
        # CompileDisclosure）；prompt 仍只用 segments 组装由下一
        # 字节 oracle 测试单独钉定。
        tree = ast.parse(_ENTRY_SOURCE)
        attrs = {node.attr for node in ast.walk(tree)
                 if isinstance(node, ast.Attribute)
                 and isinstance(node.value, ast.Name)
                 and node.value.id == "compiled"}
        self.assertEqual(attrs, {
            "segments", "task_id", "step_index", "policy_fingerprint",
            "budget", "selection_notes", "token_measure"})

    def test_prompt_byte_equal_to_segments_assembly_without_metadata(self):
        task = "w3 qualification task"
        prior = "w3 prior output"
        builder = cockpit_entry._make_request_builder(
            task, "w3-task", "coder", "provider-w3", 60.0,
            previous_role="architect", step_index=1)
        request = builder(_prior_result(output=prior))
        expected = (
            cockpit_entry._PROMPT_TEMPLATE.format(role="coder", task=task)
            + cockpit_entry._PROMPT_PREVIOUS_SECTION + prior)
        self.assertEqual(request.prompt, expected)
        # 计量/指纹词汇零渗入 prompt
        for token in ("policy_", "UNKNOWN", "embedded_chars"):
            self.assertNotIn(token, request.prompt)

    def test_request_field_set_closed_and_metadata_free(self):
        names = {field.name for field in
                 dataclasses.fields(ExternalAgentRequest)}
        self.assertEqual(names, {
            "task_id", "prompt", "agent_id", "role", "provider",
            "model", "timeout_seconds", "handoff_packets"})
        for name in names:
            for banned in ("token", "meta", "compile", "context",
                           "budget", "fingerprint"):
                self.assertNotIn(banned, name)

    def test_request_values_carry_no_compile_metadata(self):
        builder = cockpit_entry._make_request_builder(
            "task", "w3-task", "coder", "provider-w3", 60.0, step_index=0)
        request = builder(None)
        fingerprint = CompilePolicy().fingerprint
        for value in (request.task_id, request.prompt, request.agent_id,
                      request.role, request.provider):
            if isinstance(value, str):
                self.assertNotIn(fingerprint, value)


class ExecutionIsolationTests(unittest.TestCase):
    """事实 6：执行栈/控制/路由/用量域零 compiler 依赖。"""

    EXECUTION_FILES = (
        "sequential_pipeline.py", "cockpit_session.py", "execution_slots.py",
        "wrap_stack.py", "abort_gate.py", "control_boundary.py",
        "control_journal.py", "revision_adapter.py", "usage_capture.py",
        "event_index.py", "execution_observation.py", "cockpit_route.py")

    def test_execution_stack_never_imports_compile_domain(self):
        for filename in self.EXECUTION_FILES:
            roots = _imported_roots(filename)
            self.assertFalse(
                set(_COMPILE_DOMAIN_ROOTS) & roots,
                msg=f"{filename} imports compile domain: "
                    f"{sorted(set(_COMPILE_DOMAIN_ROOTS) & roots)}")


class ObservationJournalIsolationTests(unittest.TestCase):
    """事实 7/8/9：观察与控制域不承载编译元数据。"""

    @staticmethod
    def _build_with_recorder():
        events = []

        def recorder(event_type, **kwargs):
            events.append((event_type, kwargs))

        builder = cockpit_entry._make_request_builder(
            "task", "w3-task", "coder", "provider-w3", 60.0,
            emit=recorder, runtime_id="rt-w3",
            previous_role="architect", step_index=1)
        builder(_prior_result())
        return events

    def test_handoff_embedded_event_shape_unchanged(self):
        events = self._build_with_recorder()
        self.assertEqual(len(events), 1)
        event_type, kwargs = events[0]
        self.assertIs(event_type, ExecutionEventType.HANDOFF)
        self.assertEqual(kwargs, {
            "stage": "architect", "runtime_id": "rt-w3",
            "status": "EMBEDDED", "reason": "EMBEDDED"})

    def test_execution_event_field_set_closed_and_metadata_free(self):
        names = {field.name for field in dataclasses.fields(ExecutionEvent)}
        self.assertEqual(names, {
            "event_type", "sequence", "task_id", "correlation_id",
            "stage", "runtime_id", "status", "reason", "duration_ms"})
        for name in names:
            for banned in ("token", "compile", "budget", "fingerprint",
                           "chars", "truncation", "selection"):
                self.assertNotIn(banned, name)

    def test_control_fact_vocabulary_is_the_frozen_eight(self):
        self.assertEqual(
            {fact.name for fact in ControlFactType},
            {"PAUSE_REQUESTED", "PAUSE_CONFIRMED", "RESUME_REQUESTED",
             "REVISE_REQUESTED", "REVISION_APPLIED", "ABORT_REQUESTED",
             "ABORT_CONFIRMED", "ABORT_SUPERSEDED"})

    def test_journal_source_free_of_compile_symbols(self):
        source = (SCRIPTS / "control_journal.py").read_text(encoding="utf-8")
        for token in ("cockpit_compile", "cockpit_context", "BudgetReport",
                      "TruncationFact", "SelectionNote"):
            self.assertNotIn(token, source)


class UsageTruthBoundaryTests(unittest.TestCase):
    """事实 10/11/12：字符记账 ≠ token 计量；UNKNOWN≠0；runtime 真值归属。"""

    @staticmethod
    def _compile(task="w3 task text", prior="w3 prior text"):
        return compile_invocation_context(
            task, "w3-task", 1, _prior_result(output=prior), "architect",
            prior_char_limit=4000)

    def test_budget_field_names_never_claim_tokens(self):
        for cls in (CompiledInvocationContext,):
            names = {field.name for field in dataclasses.fields(cls)}
            self.assertEqual(names, {
                "task_id", "step_index", "segments", "selection_notes",
                "budget", "token_measure", "policy_fingerprint"})
        report_fields = ("item_counts_by_kind", "embedded_chars_by_kind",
                         "total_embedded_chars", "truncations")
        from cockpit_compile import BudgetReport, TruncationFact
        self.assertEqual(
            {field.name for field in dataclasses.fields(BudgetReport)},
            set(report_fields))
        self.assertEqual(
            {field.name for field in dataclasses.fields(TruncationFact)},
            {"kind", "original_chars", "embedded_chars"})
        for field in dataclasses.fields(BudgetReport):
            self.assertNotIn("token", field.name)

    def test_embedded_chars_are_char_counts_not_token_measures(self):
        compiled = self._compile(task="12345", prior="1234567")
        self.assertEqual(compiled.budget.total_embedded_chars, 5 + 7)
        self.assertIs(compiled.token_measure.status, TokenStatus.UNKNOWN)
        self.assertIsNone(compiled.token_measure.tokens)

    def test_unknown_token_measure_structurally_carries_no_count(self):
        with self.assertRaises(CompileModelError):
            TokenMeasure(status=TokenStatus.UNKNOWN, tokens=5)
        measure = TokenMeasure.unknown()
        self.assertIs(measure.status, TokenStatus.UNKNOWN)
        self.assertIsNone(measure.tokens)

    def test_runtime_usage_truth_home_is_invocation_trace(self):
        names = {field.name for field in dataclasses.fields(InvocationTrace)}
        self.assertIn("input_tokens", names)
        self.assertIn("output_tokens", names)
        trace = InvocationTrace(
            invocation_id="inv-w3", task_id="w3-task", agent_id="a",
            runtime="rt", provider=None, model=None, role="coder",
            status=InvocationStatus.SUCCESS)
        # 诚实缺省：字面量 "unknown"，绝不折算为 0
        self.assertEqual(trace.input_tokens, "unknown")
        self.assertEqual(trace.output_tokens, "unknown")


class DomainIsolationTests(unittest.TestCase):
    """事实 13：Compiler 指纹与 Router 指纹同名异物体、两域互不 import。"""

    def test_router_and_compiler_domains_never_import_each_other(self):
        self.assertFalse({"cockpit_compile", "cockpit_context",
                          "cockpit_context_wire"} & _imported_roots(
                              "cockpit_route.py"))
        self.assertNotIn("cockpit_route", _imported_roots("cockpit_compile.py"))

    def test_router_policy_fingerprint_is_own_homonym(self):
        route_source = (SCRIPTS / "cockpit_route.py").read_text(
            encoding="utf-8")
        self.assertIn("policy_fingerprint", route_source)  # 自有字段在
        for token in ("cockpit_compile", "compile_context",
                      "CompilePolicy"):
            self.assertNotIn(token, route_source)

    def test_compile_policy_fingerprint_is_content_derived(self):
        self.assertEqual(CompilePolicy().fingerprint,
                         CompilePolicy().fingerprint)
        self.assertTrue(CompilePolicy().fingerprint.startswith("policy_"))
        self.assertNotEqual(CompilePolicy().fingerprint,
                            CompilePolicy(
                                prior_output_char_limit=1234).fingerprint)


class PresentationSeamTests(unittest.TestCase):
    """事实 14/15：TUI/投影零 compiler 依赖；披露缝 W3-P 已激活
    （compile_metadata additive 位，缺省 None = 逐字节一致）。"""

    def test_tui_import_graph_free_of_compile_domain(self):
        roots = _imported_roots("cockpit_tui.py")
        self.assertFalse(set(_COMPILE_DOMAIN_ROOTS) & roots,
                         msg=f"cockpit_tui imports: {sorted(roots)}")

    def test_projection_import_graph_free_of_compile_domain(self):
        roots = _imported_roots("cockpit_projection.py")
        self.assertFalse(set(_COMPILE_DOMAIN_ROOTS) & roots,
                         msg=f"cockpit_projection imports: {sorted(roots)}")

    def test_projection_inputs_disclosure_slot_is_w3p_active(self):
        slots = set(ProjectionInputs.__slots__)
        # W3-P 激活的 additive 位：字段在、缺省 None、投影缺省
        # 渲染逐字节一致（capabilities 先例同构）
        self.assertIn("compile_metadata", slots)
        default = inspect.signature(
            ProjectionInputs.__init__).parameters[
                "compile_metadata"].default
        self.assertIsNone(default)
        # 其余 slot 仍零 compile 形词根（不扩散）
        for slot in slots - {"compile_metadata"}:
            for banned in ("compile", "truncation", "selection",
                           "fingerprint"):
                self.assertNotIn(banned, slot)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
