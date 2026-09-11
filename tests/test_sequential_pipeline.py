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
    SequentialPipeline,
    StepRecord,
    StepSpec,
    build_sequential_pipeline,
)
from control_boundary import (  # noqa: E402
    ControlCommand,
    ControlCommandType,
    ControlLifecycle,
    ControlModelError,
)
from control_gate import ControlAborted  # noqa: E402
from execution_slots import (  # noqa: E402
    ExecutionSlotSpec,
    build_execution_slots,
)
from external_runtime import (  # noqa: E402
    InvocationResult,
    InvocationStatus,
    InvocationTrace,
)
from usage_log import UsageLog  # noqa: E402

MODULE_PATH = SCRIPTS / "sequential_pipeline.py"


def _builder(previous_result):
    return "request"


def _success(trace=None):
    return InvocationResult(status=InvocationStatus.SUCCESS, trace=trace)


def _failed():
    return InvocationResult(status=InvocationStatus.FAILED)


def make_trace(invocation_id="inv-1", status=InvocationStatus.SUCCESS):
    return InvocationTrace(invocation_id=invocation_id, task_id="task-1",
                           agent_id="agent-1", runtime="rt-x",
                           provider=None, model=None, role="coder",
                           status=status)


def pause_cmd(command_id="p1", execution_id="exec-1"):
    return ControlCommand(command_id=command_id,
                          execution_id=execution_id,
                          command=ControlCommandType.PAUSE)


def abort_cmd(command_id="a1", execution_id="exec-1"):
    return ControlCommand(command_id=command_id,
                          execution_id=execution_id,
                          command=ControlCommandType.ABORT)


class _SpyRaw:
    """真实 spy：方法体自记入口与 request；可抛。"""

    def __init__(self, result=None, raises=None):
        self.entries = []
        self.requests = []
        self._result = result
        self._raises = raises

    def invoke(self, request):
        self.entries.append(getattr(request, "task_id", None))
        self.requests.append(request)
        if self._raises is not None:
            raise self._raises
        return self._result


def make_group(slot_ids=("a",), raws=None, execution_id="exec-1"):
    """单槽或双槽组：返回 (group, journal, {slot_id: raw})。"""
    from control_journal import ControlJournal
    journal = ControlJournal()
    raw_map = {}
    specs = []
    for index, slot_id in enumerate(slot_ids):
        raw = (raws[index] if raws is not None
               else _SpyRaw(result=_success()))
        raw_map[slot_id] = raw
        specs.append(ExecutionSlotSpec(
            slot_id=slot_id, raw_adapter=raw, usage_log=UsageLog(),
            runtime_id=f"rt-{slot_id}", role="coder"))
    group = build_execution_slots(journal, specs,
                                  execution_id=execution_id)
    return group, journal, raw_map


def make_pipeline(slot_ids=("a",), steps=None, raws=None,
                  execution_id="exec-1"):
    group, journal, raw_map = make_group(slot_ids=slot_ids, raws=raws,
                                         execution_id=execution_id)
    if steps is None:
        steps = tuple(StepSpec(slot_id=slot_id, request_builder=_builder)
                      for slot_id in slot_ids)
    pipeline = build_sequential_pipeline(group, steps)
    return pipeline, group, journal, raw_map


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
    """结构锁（ORCH-2 终态版）：7 roots、恰一 Try 两 handler、
    slot 查取恰两处、pending_intent 恰一读点、恰类集、禁词全表。"""

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
        # ERRATUM-1 修正后的终态清单（含 stdlib enum）
        self.assertEqual(roots, {"__future__", "control_boundary",
                                 "control_gate", "dataclasses", "enum",
                                 "execution_slots", "external_runtime"})

    def test_exactly_one_try_two_handlers(self):
        tries = [node for node in ast.walk(self._tree())
                 if isinstance(node, ast.Try)]
        self.assertEqual(len(tries), 1)  # run 边界唯一捕获点
        handlers = tries[0].handlers
        self.assertEqual(len(handlers), 2)
        for handler, expected in zip(handlers, ["ControlAborted",
                                                "Exception"]):
            self.assertIsInstance(handler.type, ast.Name)
            self.assertEqual(handler.type.id, expected)

    def test_slot_lookup_exactly_two_sites(self):
        # 构造期探测一处 + run 循环公开查取一处（零第三查取）
        source = MODULE_PATH.read_text(encoding="utf-8")
        self.assertEqual(source.count("slot("), 2)

    def test_pending_intent_exactly_one_read_site(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        self.assertEqual(source.count("pending_intent"), 1)

    def test_zero_construction_of_frozen_composites(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        self.assertNotIn("ControlBoundary(", source)  # 只消费既有组
        self.assertNotIn("ExecutionSlots(", source)
        self.assertNotIn("build_execution_slots(", source)

    def test_class_set_exactly_six(self):
        classes = {node.name for node in self._tree().body
                   if isinstance(node, ast.ClassDef)}
        self.assertEqual(classes, {"RunStatus", "StepSpec", "StepRecord",
                                   "RunState", "RunOutcome",
                                   "SequentialPipeline"})

    def test_public_module_functions_exactly_builder(self):
        functions = {node.name for node in self._tree().body
                     if isinstance(node, ast.FunctionDef)}
        self.assertEqual(
            {name for name in functions if not name.startswith("_")},
            {"build_sequential_pipeline"})

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

    def test_no_forbidden_tokens(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        for token in ("threading", "Lock", "acquire", "release", "uuid",
                      "random", "monotonic", "subprocess", "Popen",
                      "cockpit", "claude", "gemini", "codex",
                      "qwen", "opencode", "cline", "deepseek",
                      "ExecutionEvent", "INVOCATION_STARTED",
                      "INVOCATION_FINISHED", "event_index",
                      "trace_projector", "boundary_writer",
                      "revision_writer", "composition_writer",
                      "ABORT_CONFIRMED", "PAUSE_CONFIRMED",
                      "ABORT_SUPERSEDED", "submit(", "snapshot(",
                      "drain", "broadcast", "routing", "schedule",
                      "orchestrat", "ControlLifecycle"):
            self.assertNotIn(token, source, token)


class ConstructionTests(unittest.TestCase):
    """构造校验矩阵：zero-step 拒绝、缺席 slot 构造期 KeyError、
    零 journal 残留、不可变面。"""

    def test_non_execution_slots_rejected(self):
        with self.assertRaises(ControlModelError):
            build_sequential_pipeline(object(), [StepSpec("a", _builder)])

    def test_zero_step_rejected(self):
        pipeline, group, journal, _ = make_pipeline()
        with self.assertRaises(ControlModelError):
            build_sequential_pipeline(group, [])
        self.assertEqual(journal.snapshot(), ())  # 零残留

    def test_non_stepspec_entry_rejected(self):
        pipeline, group, journal, _ = make_pipeline()
        with self.assertRaises(ControlModelError):
            build_sequential_pipeline(group, [_builder])
        self.assertEqual(journal.snapshot(), ())

    def test_missing_slot_rejected_at_construction_time(self):
        # 缺席 slot 必须在构造期被发现（KeyError 原样、零翻译），
        # 不能到 run 时才第一次发现
        group, journal, _ = make_group()
        with self.assertRaises(KeyError):
            build_sequential_pipeline(
                group, [StepSpec("nope", _builder)])
        self.assertEqual(journal.snapshot(), ())

    def test_valid_construction_surface_and_type(self):
        pipeline, _, _, _ = make_pipeline()
        self.assertIsInstance(pipeline, SequentialPipeline)
        self.assertEqual(
            sorted(name for name in dir(pipeline)
                   if not name.startswith("_")),
            ["run"])  # 唯一公开面；零运行状态暴露

    def test_duplicate_slot_reuse_allowed(self):
        # 同一 slot 可被多个 step 先后引用（Spec §7）
        group, _, raw_map = make_group(slot_ids=("a",))
        steps = tuple(StepSpec(slot_id="a", request_builder=_builder)
                      for _ in range(3))
        pipeline = build_sequential_pipeline(group, steps)
        self.assertIsInstance(pipeline, SequentialPipeline)


class SingleStepTests(unittest.TestCase):
    """E1–E8 单步版（Spec §21 矩阵，零扩展零重释）。"""

    def test_e7_single_step_completed(self):
        trace = make_trace("inv-1")
        raw = _SpyRaw(result=_success(trace=trace))
        captured = []

        def builder(previous_result):
            captured.append(previous_result)
            return "req"

        pipeline, group, journal, raw_map = make_pipeline(raws=[raw])
        pipeline = build_sequential_pipeline(
            group, [StepSpec("a", builder)])
        outcome = pipeline.run()
        self.assertIs(outcome.status, RunStatus.COMPLETED)
        self.assertIs(outcome.final_result, raw._result)  # 原样
        self.assertIsNone(outcome.error)
        self.assertIsNone(outcome.run_state)
        self.assertEqual(captured, [None])  # 首步 prev=None
        self.assertEqual(len(raw.entries), 1)
        self.assertEqual(len(outcome.transcript), 1)
        record = outcome.transcript[0]
        self.assertEqual((record.step_index, record.slot_id),
                         (0, "a"))
        self.assertIs(record.status, InvocationStatus.SUCCESS)
        self.assertEqual(record.invocation_id, "inv-1")  # 投影自 trace

    def test_e7_without_trace_invocation_id_none(self):
        raw = _SpyRaw(result=_success())  # trace 缺席
        pipeline, group, _, _ = make_pipeline(raws=[raw])
        outcome = pipeline.run()
        self.assertIsNone(outcome.transcript[0].invocation_id)

    def test_e1_abort_before_first_step(self):
        pipeline, group, journal, raw_map = make_pipeline()
        group.boundary.submit(abort_cmd())
        outcome = pipeline.run()
        self.assertIs(outcome.status, RunStatus.ABORTED)
        self.assertEqual(outcome.transcript, ())
        self.assertIsNone(outcome.final_result)
        self.assertIsNone(outcome.error)
        self.assertIsNone(outcome.run_state)
        self.assertEqual(raw_map["a"].entries, [])  # raw 零进入

    def test_e2_pause_before_first_step_then_resume_roundtrip(self):
        pipeline, group, journal, raw_map = make_pipeline()
        group.boundary.submit(pause_cmd())
        outcome = pipeline.run()
        self.assertIs(outcome.status, RunStatus.PARKED)
        self.assertEqual(outcome.transcript, ())
        self.assertIsNone(outcome.final_result)
        self.assertIsNotNone(outcome.run_state)
        self.assertEqual(outcome.run_state.next_step_index, 0)  # NEXT
        self.assertEqual(raw_map["a"].entries, [])  # 未执行
        # 显式 continuation 往返：RESUME 清 pending → run(state) 续走
        group.boundary.submit(ControlCommand(
            command_id="r1", execution_id="exec-1",
            command=ControlCommandType.RESUME))
        resumed = pipeline.run(outcome.run_state)
        self.assertIs(resumed.status, RunStatus.COMPLETED)
        self.assertEqual(len(raw_map["a"].entries), 1)  # step 0 执行
        self.assertEqual(len(resumed.transcript), 1)  # 跨续累积

    def test_e2_admission_rereads_fresh_not_cached(self):
        # PARKED 后控制真相前进（ABORT 压过 PAUSE）→ 续走按现读终止
        pipeline, group, journal, raw_map = make_pipeline()
        group.boundary.submit(pause_cmd())
        outcome = pipeline.run()
        self.assertIs(outcome.status, RunStatus.PARKED)
        group.boundary.submit(abort_cmd())  # ABORT 可替换 pending PAUSE
        resumed = pipeline.run(outcome.run_state)
        self.assertIs(resumed.status, RunStatus.ABORTED)
        self.assertEqual(raw_map["a"].entries, [])  # 零执行

    def test_e3_request_builder_failure(self):
        pipeline, group, journal, raw_map = make_pipeline()
        boom = ValueError("bad builder")
        pipeline = build_sequential_pipeline(
            group, [StepSpec("a", lambda prev: (_ for _ in ()).throw(boom))])
        outcome = pipeline.run()
        self.assertIs(outcome.status, RunStatus.FAILED)
        self.assertIs(outcome.error, boom)  # 原异常对象
        self.assertIsNone(outcome.final_result)
        self.assertEqual(outcome.transcript, ())  # 异常步不入册
        self.assertIsNone(outcome.run_state)
        self.assertEqual(raw_map["a"].entries, [])  # 从未委托

    def test_e4_ordinary_invoke_failure(self):
        boom = RuntimeError("raw crashed")
        raw = _SpyRaw(raises=boom)
        pipeline, group, _, _ = make_pipeline(raws=[raw])
        outcome = pipeline.run()
        self.assertIs(outcome.status, RunStatus.FAILED)
        self.assertIs(outcome.error, boom)
        self.assertIsNone(outcome.final_result)
        self.assertEqual(outcome.transcript, ())
        self.assertEqual(len(raw.entries), 1)  # 委托确实发生

    def test_e5_status_failure(self):
        result = _failed()
        raw = _SpyRaw(result=result)
        pipeline, group, journal, _ = make_pipeline(raws=[raw])
        outcome = pipeline.run()
        self.assertIs(outcome.status, RunStatus.FAILED)
        self.assertIsNone(outcome.error)
        self.assertIs(outcome.final_result, result)  # 失败结果原样
        self.assertEqual(len(outcome.transcript), 1)  # 失败步入册
        self.assertIs(outcome.transcript[0].status,
                      InvocationStatus.FAILED)

    def test_e6_control_aborted_from_chain_translated_at_boundary(self):
        raw = _SpyRaw(raises=ControlAborted("exec-1"))
        pipeline, group, _, _ = make_pipeline(raws=[raw])
        outcome = pipeline.run()
        self.assertIs(outcome.status, RunStatus.ABORTED)  # 翻译非失败
        self.assertIsNone(outcome.error)  # 控制信号不是 error
        self.assertIsNone(outcome.run_state)
        self.assertEqual(outcome.transcript, ())

    def test_e6_control_aborted_from_builder_translated_uniformly(self):
        def builder(prev):
            raise ControlAborted("exec-1")

        pipeline, group, _, _ = make_pipeline()
        pipeline = build_sequential_pipeline(
            group, [StepSpec("a", builder)])
        outcome = pipeline.run()
        self.assertIs(outcome.status, RunStatus.ABORTED)

    def test_e8_convergence_with_completed_state(self):
        pipeline, group, _, _ = make_pipeline()
        result = _success()
        record = StepRecord(step_index=0, slot_id="a",
                            status=InvocationStatus.SUCCESS,
                            invocation_id=None)
        state = RunState(next_step_index=1, previous_result=result,
                         transcript=(record,))
        outcome = pipeline.run(state)  # next==len ⇒ 立即收敛
        self.assertIs(outcome.status, RunStatus.COMPLETED)
        self.assertIs(outcome.final_result, result)  # previous_result
        self.assertEqual(outcome.transcript, (record,))  # 原样

    def test_e8_convergence_empty_transcript_final_result_none(self):
        pipeline, group, _, _ = make_pipeline()
        state = RunState(next_step_index=1, previous_result=None,
                         transcript=())
        outcome = pipeline.run(state)
        self.assertIs(outcome.status, RunStatus.COMPLETED)
        self.assertIsNone(outcome.final_result)  # 不变量允许

    def test_run_rejects_non_runstate_argument(self):
        pipeline, group, _, _ = make_pipeline()
        with self.assertRaises(ControlModelError):
            pipeline.run(object())

    def test_pipeline_stateless_rerun_no_internal_cursor(self):
        # Model A 证明：管线不持游标——同管线二次 run() 从头精确
        # 重走（无内部状态残留），与显式 RunState continuation 并存
        raw = _SpyRaw(result=_success())
        pipeline, group, _, _ = make_pipeline(raws=[raw])
        first = pipeline.run()
        second = pipeline.run()
        self.assertIs(first.status, RunStatus.COMPLETED)
        self.assertIs(second.status, RunStatus.COMPLETED)
        self.assertIsNot(first, second)  # 每次全新 RunOutcome
        self.assertEqual(len(raw.entries), 2)  # 两次都真实委托


if __name__ == "__main__":
    unittest.main()
