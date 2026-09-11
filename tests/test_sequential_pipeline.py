"""ORCH-1 tests: 编排值模型（RunStatus/StepSpec/RunState/StepRecord/RunOutcome）。

值层级（CU-ORCH-1，只立值、零执行逻辑）：

    RunStatus   编排域局部词表（恰四值；与控制域生命周期词表零混用）
    StepSpec    步规格（opaque slot 引用 + request 构造函数）
    StepRecord  步投影记录（只承载既有执行事实的引用，零铸造）
    RunState    caller 持有的瞬态续走值（恰三字段，零控制/观察缓存）
    RunOutcome  单次 run 调用的返回值（结构不变量构造期强制）

锁定语义（值级）：
- 续走值非空 ⟺ 停驻态；终局态（完成/失败/中止）零续走值。
- 普通错误仅失败态可携带；final_result 仅完成/失败态可携带。
- 一切值 frozen；构造期拒绝复用 ControlModelError（零新类型）。
- 本阶段生产文件零执行逻辑：无管线、无槽查取、无委托调用、
  无准入、零捕获、零锁——AST 结构锁钉死。
"""
import ast
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
import sys
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from sequential_pipeline import (  # noqa: E402
    RunOutcome,
    RunState,
    RunStatus,
    StepRecord,
    StepSpec,
)
from control_boundary import (  # noqa: E402
    ControlLifecycle,
    ControlModelError,
)

MODULE_PATH = SCRIPTS / "sequential_pipeline.py"


def _builder(previous_result):
    return "request"


def make_record(step_index=0, slot_id="a", status="SUCCESS",
                invocation_id=None):
    return StepRecord(step_index=step_index, slot_id=slot_id,
                      status=status, invocation_id=invocation_id)


class RunStatusTests(unittest.TestCase):
    """词表恰四值；与控制域生命周期词表概念隔离。"""

    def test_vocabulary_exactly_four_values(self):
        self.assertEqual(
            {member.name for member in RunStatus},
            {"COMPLETED", "FAILED", "ABORTED", "PARKED"})
        for member in RunStatus:
            self.assertEqual(member.value, member.name)  # str 枚举、值即名

    def test_distinct_from_control_lifecycle(self):
        # 类型隔离即域隔离：编排返回状态不是控制域生命周期成员
        for name in ("COMPLETED", "FAILED", "ABORTED"):
            self.assertNotIsInstance(getattr(RunStatus, name),
                                     ControlLifecycle)
            self.assertIsNot(getattr(RunStatus, name),
                             getattr(ControlLifecycle, name))


class StepSpecTests(unittest.TestCase):
    """步规格：opaque slot 引用 + request 构造函数。"""

    def test_valid_construction_preserves_fields(self):
        spec = StepSpec(slot_id="coder", request_builder=_builder)
        self.assertEqual(spec.slot_id, "coder")
        self.assertIs(spec.request_builder, _builder)

    def test_bad_slot_id_rejected(self):
        for bad in ("", "   ", 42, None):
            with self.subTest(bad=bad):
                with self.assertRaises(ControlModelError):
                    StepSpec(slot_id=bad, request_builder=_builder)

    def test_non_callable_request_builder_rejected(self):
        for bad in (42, object(), "fn"):
            with self.subTest(bad=bad):
                with self.assertRaises(ControlModelError):
                    StepSpec(slot_id="a", request_builder=bad)

    def test_frozen_value(self):
        spec = StepSpec(slot_id="a", request_builder=_builder)
        with self.assertRaises(FrozenInstanceError):
            spec.slot_id = "b"

    def test_surface_exactly_two_fields(self):
        spec = StepSpec(slot_id="a", request_builder=_builder)
        self.assertEqual(
            sorted(name for name in dir(spec) if not name.startswith("_")),
            ["request_builder", "slot_id"])


class StepRecordTests(unittest.TestCase):
    """步投影记录：恰四字段、原样保存、零铸造。"""

    def test_valid_construction_preserves_fields(self):
        sentinel = object()
        record = StepRecord(step_index=2, slot_id="b", status=sentinel,
                            invocation_id="inv-x")
        self.assertEqual(record.step_index, 2)
        self.assertEqual(record.slot_id, "b")
        self.assertIs(record.status, sentinel)  # 调用状态原样（零转换）
        self.assertEqual(record.invocation_id, "inv-x")

    def test_bad_step_index_rejected(self):
        for bad in (-1, True, "0", 1.5):
            with self.subTest(bad=bad):
                with self.assertRaises(ControlModelError):
                    make_record(step_index=bad)

    def test_bad_slot_id_rejected(self):
        for bad in ("", "  ", 42):
            with self.subTest(bad=bad):
                with self.assertRaises(ControlModelError):
                    make_record(slot_id=bad)

    def test_invocation_id_none_or_nonempty_string(self):
        make_record(invocation_id=None)  # trace 缺席 ⇒ None 合法
        make_record(invocation_id="inv-1")
        for bad in ("", "  ", 42):
            with self.subTest(bad=bad):
                with self.assertRaises(ControlModelError):
                    make_record(invocation_id=bad)

    def test_frozen_value(self):
        record = make_record()
        with self.assertRaises(FrozenInstanceError):
            record.step_index = 9

    def test_surface_exactly_four_fields(self):
        record = make_record()
        self.assertEqual(
            sorted(name for name in dir(record) if not name.startswith("_")),
            ["invocation_id", "slot_id", "status", "step_index"])


class RunStateTests(unittest.TestCase):
    """续走值：恰三字段、瞬态、零控制/观察缓存。"""

    def test_minimal_construction_defaults(self):
        state = RunState(next_step_index=0)
        self.assertEqual(state.next_step_index, 0)
        self.assertIsNone(state.previous_result)
        self.assertEqual(state.transcript, ())

    def test_bad_next_step_index_rejected(self):
        for bad in (-1, True, "0", 1.5, None):
            with self.subTest(bad=bad):
                with self.assertRaises(ControlModelError):
                    RunState(next_step_index=bad)

    def test_previous_result_any_value_verbatim(self):
        sentinel = object()
        state = RunState(next_step_index=1, previous_result=sentinel)
        self.assertIs(state.previous_result, sentinel)  # 原样、零解释

    def test_transcript_must_be_tuple_of_step_records(self):
        with self.assertRaises(ControlModelError):  # 非 tuple
            RunState(next_step_index=0, transcript=[make_record()])
        with self.assertRaises(ControlModelError):  # 条目非 StepRecord
            RunState(next_step_index=0, transcript=("not-a-record",))
        records = (make_record(0), make_record(1))
        state = RunState(next_step_index=2, transcript=records)
        self.assertEqual(state.transcript, records)  # 原样保存

    def test_frozen_value(self):
        state = RunState(next_step_index=0)
        with self.assertRaises(FrozenInstanceError):
            state.next_step_index = 5

    def test_surface_exactly_three_fields_no_identity(self):
        # 零身份字段（execution/invocation/runtime/agent 均不在场）
        state = RunState(next_step_index=0)
        surface = sorted(name for name in dir(state)
                         if not name.startswith("_"))
        self.assertEqual(surface,
                         ["next_step_index", "previous_result",
                          "transcript"])


class RunOutcomeTests(unittest.TestCase):
    """返回值结构不变量（构造期强制，全部矩阵）。"""

    def test_completed_minimal_valid(self):
        outcome = RunOutcome(status=RunStatus.COMPLETED)
        self.assertIs(outcome.status, RunStatus.COMPLETED)
        self.assertEqual(outcome.transcript, ())
        self.assertIsNone(outcome.final_result)
        self.assertIsNone(outcome.error)
        self.assertIsNone(outcome.run_state)

    def test_completed_with_final_result_valid(self):
        sentinel = object()
        outcome = RunOutcome(status=RunStatus.COMPLETED,
                             final_result=sentinel)
        self.assertIs(outcome.final_result, sentinel)

    def test_failed_with_error_valid(self):
        boom = ValueError("step crashed")
        outcome = RunOutcome(status=RunStatus.FAILED, error=boom)
        self.assertIs(outcome.error, boom)  # 原异常对象原样

    def test_failed_with_final_result_valid(self):
        sentinel = object()
        outcome = RunOutcome(status=RunStatus.FAILED,
                             final_result=sentinel)
        self.assertIs(outcome.final_result, sentinel)

    def test_failed_without_error_valid(self):
        RunOutcome(status=RunStatus.FAILED)  # status 失败路径：error 可空

    def test_aborted_all_none_valid(self):
        outcome = RunOutcome(status=RunStatus.ABORTED)
        self.assertIsNone(outcome.run_state)

    def test_parked_with_run_state_valid(self):
        state = RunState(next_step_index=1)
        outcome = RunOutcome(status=RunStatus.PARKED, run_state=state)
        self.assertIs(outcome.run_state, state)

    def test_run_state_presence_iff_parked(self):
        state = RunState(next_step_index=0)
        with self.assertRaises(ControlModelError):  # PARKED 缺续走值
            RunOutcome(status=RunStatus.PARKED)
        for name in ("COMPLETED", "FAILED", "ABORTED"):
            with self.subTest(status=name):
                with self.assertRaises(ControlModelError):
                    RunOutcome(status=getattr(RunStatus, name),
                               run_state=state)

    def test_error_only_on_failed(self):
        boom = ValueError("x")
        for name in ("COMPLETED", "PARKED", "ABORTED"):
            with self.subTest(status=name):
                with self.assertRaises(ControlModelError):
                    RunOutcome(status=getattr(RunStatus, name), error=boom)

    def test_final_result_only_on_completed_or_failed(self):
        sentinel = object()
        for name in ("PARKED", "ABORTED"):
            with self.subTest(status=name):
                with self.assertRaises(ControlModelError):
                    RunOutcome(status=getattr(RunStatus, name),
                               final_result=sentinel)

    def test_bad_status_rejected(self):
        for bad in ("COMPLETED", None, 42, "completed"):
            with self.subTest(bad=bad):
                with self.assertRaises(ControlModelError):
                    RunOutcome(status=bad)

    def test_transcript_must_be_tuple_of_step_records(self):
        with self.assertRaises(ControlModelError):
            RunOutcome(status=RunStatus.COMPLETED,
                       transcript=[make_record()])
        with self.assertRaises(ControlModelError):
            RunOutcome(status=RunStatus.COMPLETED,
                       transcript=("nope",))
        records = (make_record(0), make_record(1))
        outcome = RunOutcome(status=RunStatus.COMPLETED, transcript=records)
        self.assertEqual(outcome.transcript, records)

    def test_frozen_value(self):
        outcome = RunOutcome(status=RunStatus.COMPLETED)
        with self.assertRaises(FrozenInstanceError):
            outcome.status = RunStatus.PARKED

    def test_surface_exactly_five_fields(self):
        outcome = RunOutcome(status=RunStatus.COMPLETED)
        self.assertEqual(
            sorted(name for name in dir(outcome)
                   if not name.startswith("_")),
            ["error", "final_result", "run_state", "status", "transcript"])


class ArchitectureTests(unittest.TestCase):
    """ORCH-1 结构锁：3+1 roots、零 Try、恰类集、字段数、零执行词。"""

    def _tree(self):
        return ast.parse(MODULE_PATH.read_text(encoding="utf-8"))

    def test_import_roots_locked(self):
        tree = self._tree()
        roots = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots.update(alias.name.split(".")[0]
                             for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                roots.add(node.module.split(".")[0])
        # ERRATUM-1：spec §22 清单遗漏 stdlib enum（RunStatus 枚举必需）
        self.assertEqual(roots, {"__future__", "control_boundary",
                                 "dataclasses", "enum"})

    def test_zero_try_zero_handler(self):
        for node in ast.walk(self._tree()):
            self.assertNotIsInstance(node, ast.Try)
            self.assertNotIsInstance(node, ast.ExceptHandler)

    def test_class_set_exactly_five(self):
        classes = {node.name for node in self._tree().body
                   if isinstance(node, ast.ClassDef)}
        self.assertEqual(classes, {"RunStatus", "StepSpec", "StepRecord",
                                   "RunState", "RunOutcome"})

    def test_zero_public_module_level_functions(self):
        # 零公开生产函数（管线构造入口属后续单元）；
        # 模块级私有校验助手合法（沿 control_boundary 家法）
        functions = {node.name for node in self._tree().body
                     if isinstance(node, ast.FunctionDef)}
        self.assertEqual(
            {name for name in functions if not name.startswith("_")},
            set())

    def test_field_counts_and_method_surfaces(self):
        expected = {"StepSpec": 2, "StepRecord": 4, "RunState": 3,
                    "RunOutcome": 5}
        for node in self._tree().body:
            if isinstance(node, ast.ClassDef) and node.name in expected:
                fields = [item.target.id for item in node.body
                          if isinstance(item, ast.AnnAssign)
                          and isinstance(item.target, ast.Name)]
                methods = {item.name for item in node.body
                           if isinstance(item, ast.FunctionDef)}
                with self.subTest(cls=node.name):
                    self.assertEqual(len(fields), expected[node.name])
                    self.assertEqual(methods, {"__post_init__"})

    def test_no_execution_or_forbidden_tokens(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        for token in ("threading", "Lock", "acquire", "release", "uuid",
                      "random", "monotonic", "subprocess", "Popen",
                      "cockpit", "claude", "gemini", "codex", "qwen",
                      "opencode", "cline", "deepseek", "ExecutionEvent",
                      "INVOCATION_STARTED", "INVOCATION_FINISHED",
                      "event_index", "trace_projector", "boundary_writer",
                      "revision_writer", "composition_writer",
                      "ABORT_CONFIRMED", "PAUSE_CONFIRMED",
                      "ABORT_SUPERSEDED", "submit(", "snapshot(",
                      "drain", "broadcast", "routing", "schedule",
                      "orchestrat", "ControlLifecycle", "external_runtime",
                      "control_gate", "execution_slots",
                      "build_sequential", "SequentialPipeline",
                      "pending_intent", "slot(", "invoke"):
            self.assertNotIn(token, source, token)


if __name__ == "__main__":
    unittest.main()
