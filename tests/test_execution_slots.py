"""COMP-2 tests: Multi-Slot Composition / Single Execution Boundary。

    caller（供应 execution_id、journal、slot specs）
        ↓ build_execution_slots
    ExecutionSlots（不可变）
        ├─ 恰一 ControlBoundary（intent 单权威）
        ├─ 恰一 RevisionAppliedJournaler（APPLIED 唯一转译实例）
        └─ slot_id → WrapStack（每 slot 完整独立执行链）
             SlotHandle.invoke(request)（唯一公开执行面）

锁定语义：
- 单账本：所有事实（裁决回声 + APPLIED）恒落 caller 传入的同一
  journal；accept/applied 分账在构造上不可能。
- 单权威：intent 翻转只在 boundary.submit；slot 侧门/适配器只是
  同一真相的只读观察者；执行异常（含控制域中止异常）原样穿透，
  零捕获、零确认事实、零终态。
- 共享修订队列 execution-scoped：每 slot invoke 读同一 detached
  快照、当前全部 pending 修订随该次委托携带——零排干/零过滤/
  零路由（targeted revision 属未来编排单元）。
- identity 零新增：execution_id caller 供应；invocation 身份仍由
  既有唯一铸造点生成；slot_id 只是组合映射键。
- 组装一次性、不可变：零组合域锁、零动态拓扑；构造失败零残留。
"""
import ast
import threading
import time
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
import sys
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from execution_slots import (  # noqa: E402
    ExecutionSlotSpec,
    ExecutionSlots,
    SlotHandle,
    build_execution_slots,
)
from control_boundary import (  # noqa: E402
    ControlCommand,
    ControlCommandType,
    ControlModelError,
    ControlStatus,
    RevisionPayload,
    RevisionTarget,
)
from control_gate import ControlAborted  # noqa: E402
from control_journal import ControlFactType, ControlJournal  # noqa: E402
from external_runtime import (  # noqa: E402
    ExternalAgentRequest,
    InvocationResult,
    InvocationStatus,
)
from usage_capture import UsageCaptureError  # noqa: E402
from usage_log import UsageLog  # noqa: E402
from wrap_stack import build_wrap_stack  # noqa: E402

MODULE_PATH = SCRIPTS / "execution_slots.py"
WRAP_STACK_PATH = SCRIPTS / "wrap_stack.py"
FROZEN_COMPONENTS = {"ControlBoundary", "RevisionAppliedJournaler",
                     "UsageCapture", "RevisionAdapter", "AbortGate",
                     "WrapStack"}


def _success():
    return InvocationResult(status=InvocationStatus.SUCCESS)


class _SpyRaw:
    """真实 spy：方法体自记入口与 request；可阻塞/可抛。"""

    def __init__(self, result=None, raises=None, release=None):
        self.entries = []
        self.requests = []
        self._result = result
        self._raises = raises
        self._release = release

    def invoke(self, request):
        self.entries.append(request.task_id)
        self.requests.append(request)
        if self._release is not None:
            self._release.wait(5.0)
        if self._raises is not None:
            raise self._raises
        return self._result


def next_rev(command_id, text, execution_id="exec-1"):
    return ControlCommand(
        command_id=command_id, execution_id=execution_id,
        command=ControlCommandType.REVISE,
        payload=RevisionPayload(target=RevisionTarget.NEXT_INVOCATION,
                                text=text),
        expected_version=0)


def abort_cmd(command_id="a1", execution_id="exec-1"):
    return ControlCommand(command_id=command_id,
                          execution_id=execution_id,
                          command=ControlCommandType.ABORT)


def make_request(task_id="task-1", prompt="base prompt"):
    return ExternalAgentRequest(task_id=task_id, prompt=prompt,
                                agent_id="agent-1", role="coder")


def make_spec(slot_id, raw=None, usage_log=None, runtime_id=None,
              role="coder"):
    return ExecutionSlotSpec(
        slot_id=slot_id,
        raw_adapter=raw if raw is not None else _SpyRaw(result=_success()),
        usage_log=usage_log if usage_log is not None else UsageLog(),
        runtime_id=runtime_id if runtime_id is not None else f"rt-{slot_id}",
        role=role)


def make_group(journal=None, execution_id="exec-1", specs=None):
    if journal is None:
        journal = ControlJournal()
    if specs is None:
        specs = [make_spec("a"), make_spec("b")]
    group = build_execution_slots(journal, specs, execution_id=execution_id)
    return group, journal, specs


def spec_raw(spec):
    return spec.raw_adapter


def spec_log(spec):
    return spec.usage_log


class SingleSlotFlowTests(unittest.TestCase):
    """T1：单 slot 全链（与 COMP-1 行为同构）。"""

    def test_single_slot_full_chain(self):
        spec = make_spec("only")
        group, journal, _ = make_group(specs=[spec])
        group.boundary.submit(next_rev("r1", "use X"))
        result = group.slot("only").invoke(make_request())
        self.assertIsInstance(result, InvocationResult)
        raw = spec_raw(spec)
        self.assertEqual(len(raw.entries), 1)
        self.assertIn("[USER REVISION r1]", raw.requests[0].prompt)
        self.assertIn("use X", raw.requests[0].prompt)
        self.assertEqual(
            [f.fact_type for f in journal.snapshot()],
            [ControlFactType.REVISE_REQUESTED,
             ControlFactType.REVISION_APPLIED])


class TwoSlotTests(unittest.TestCase):
    """T2：双 slot 各自 raw 收到各自 request / overlay。"""

    def test_two_slots_receive_own_requests_and_overlay(self):
        group, _, specs = make_group()
        group.boundary.submit(next_rev("r1", "shared text"))
        group.slot("a").invoke(make_request(task_id="task-a"))
        group.slot("b").invoke(make_request(task_id="task-b"))
        raw_a, raw_b = spec_raw(specs[0]), spec_raw(specs[1])
        self.assertEqual(raw_a.entries, ["task-a"])  # 各自只收自己的
        self.assertEqual(raw_b.entries, ["task-b"])
        for raw in (raw_a, raw_b):
            self.assertIn("[USER REVISION r1]", raw.requests[0].prompt)
            self.assertIn("shared text", raw.requests[0].prompt)


class SingleBoundaryTests(unittest.TestCase):
    """T3：所有 slot 引用同一个 Boundary（行为证明：控制真相全 slot 生效）。"""

    def test_revision_and_abort_obeyed_by_all_slots(self):
        # 经 THE boundary 提交的修订到达所有 slot 的 overlay；
        # 经 THE boundary 提交的中止拒绝所有 slot 的后续委托
        group, _, specs = make_group()
        group.boundary.submit(next_rev("r1", "t"))
        group.slot("a").invoke(make_request(task_id="ta"))
        group.slot("b").invoke(make_request(task_id="tb"))
        self.assertIn("[USER REVISION r1]", spec_raw(specs[0]).requests[0].prompt)
        self.assertIn("[USER REVISION r1]", spec_raw(specs[1]).requests[0].prompt)
        group.boundary.submit(abort_cmd())
        for slot_id in ("a", "b"):
            with self.assertRaises(ControlAborted):
                group.slot(slot_id).invoke(make_request())
        self.assertEqual(spec_raw(specs[0]).entries, ["ta"])  # 零新进入
        self.assertEqual(spec_raw(specs[1]).entries, ["tb"])


class SingleJournalTests(unittest.TestCase):
    """T4：所有事实进入同一个 caller Journal。"""

    def test_all_facts_land_in_caller_journal(self):
        journal = ControlJournal()
        group, returned_journal, specs = make_group(journal=journal)
        self.assertIs(returned_journal, journal)
        group.boundary.submit(next_rev("r1", "t"))
        group.slot("a").invoke(make_request())
        group.slot("b").invoke(make_request())
        types = [f.fact_type for f in journal.snapshot()]
        self.assertEqual(types,
                         [ControlFactType.REVISE_REQUESTED,
                          ControlFactType.REVISION_APPLIED,
                          ControlFactType.REVISION_APPLIED])


class InvocationIdentityTests(unittest.TestCase):
    """T5：不同 slot invocation_id 独立。"""

    def test_invocation_ids_distinct_per_slot(self):
        group, _, specs = make_group()
        group.slot("a").invoke(make_request(task_id="ta"))
        group.slot("b").invoke(make_request(task_id="tb"))
        id_a = spec_log(specs[0]).snapshot()[0].invocation_id
        id_b = spec_log(specs[1]).snapshot()[0].invocation_id
        self.assertNotEqual(id_a, id_b)  # 独立铸造、互异
        self.assertEqual(len(spec_log(specs[0]).snapshot()), 1)
        self.assertEqual(len(spec_log(specs[1]).snapshot()), 1)


class SharedRevisionTests(unittest.TestCase):
    """T6：shared revision 双 slot 各自携带 → 同 journal 两条 APPLIED。"""

    def test_shared_revision_two_applied_distinct_invocations(self):
        group, journal, specs = make_group()
        group.boundary.submit(next_rev("R1", "t"))
        group.slot("a").invoke(make_request(task_id="ta"))
        group.slot("b").invoke(make_request(task_id="tb"))
        applied = [f for f in journal.snapshot()
                   if f.fact_type is ControlFactType.REVISION_APPLIED]
        self.assertEqual(len(applied), 2)
        self.assertEqual({f.command_id for f in applied}, {"R1"})
        ids = [f.payload["invocation_id"] for f in applied]
        self.assertEqual(len(set(ids)), 2)  # invocation_id 互异
        # 每条 APPLIED 的 invocation_id 与对应 slot 的 record 对应
        log_ids = {spec_log(specs[0]).snapshot()[0].invocation_id,
                   spec_log(specs[1]).snapshot()[0].invocation_id}
        self.assertEqual(set(ids), log_ids)


class AbortTests(unittest.TestCase):
    """T7 / T8 / T9：execution-scoped 中止语义。"""

    def test_t7_abort_rejects_all_slots_no_confirm_fact(self):
        group, journal, specs = make_group()
        group.boundary.submit(abort_cmd())
        for slot_id in ("a", "b"):
            with self.assertRaises(ControlAborted):
                group.slot(slot_id).invoke(make_request())
        self.assertEqual(spec_raw(specs[0]).entries, [])  # raw 零进入
        self.assertEqual(spec_raw(specs[1]).entries, [])
        types = [f.fact_type for f in journal.snapshot()]
        self.assertEqual(types, [ControlFactType.ABORT_REQUESTED])
        # 无 APPLIED、无 ABORT_CONFIRMED、无终态事实
        for fact in journal.snapshot():
            self.assertIs(fact.fact_type, ControlFactType.ABORT_REQUESTED)

    def test_t8_in_flight_not_cancelled_new_calls_rejected(self):
        release = threading.Event()
        result = _success()
        specs = [make_spec("a", raw=_SpyRaw(result=result, release=release)),
                 make_spec("b")]
        group, journal, _ = make_group(specs=specs)
        outcome = []

        def worker():
            try:
                outcome.append(group.slot("a").invoke(make_request()))
            except BaseException as exc:  # noqa: BLE001  测试侧收集
                outcome.append(exc)

        thread = threading.Thread(target=worker)
        thread.start()
        self.addCleanup(thread.join, 10.0)
        for _ in range(2000):
            if spec_raw(specs[0]).entries or not thread.is_alive():
                break
            time.sleep(0.005)
        group.boundary.submit(abort_cmd())  # 在途不受影响
        release.set()
        thread.join()
        self.assertEqual(outcome, [result])  # 在途正常返回
        for slot_id in ("a", "b"):  # 之后的新调用被拒
            with self.assertRaises(ControlAborted):
                group.slot(slot_id).invoke(make_request())

    def test_t9_no_second_abort_authority(self):
        group, journal, specs = make_group()
        group.boundary.submit(abort_cmd(command_id="a1"))
        with self.assertRaises(ControlAborted):
            group.slot("a").invoke(make_request())
        # gate/adapter 零写入：intent 不变、事实不增
        self.assertEqual(group.boundary.pending_intent.kind.name, "ABORT")
        self.assertEqual(
            [f.fact_type for f in journal.snapshot()],
            [ControlFactType.ABORT_REQUESTED])
        # 第二条（不同 command_id）ABORT 经同一权威去重：NO_OP
        second = group.boundary.submit(abort_cmd(command_id="a2"))
        self.assertEqual(second.status, ControlStatus.NO_OP)
        self.assertEqual(
            [f.fact_type for f in journal.snapshot()],
            [ControlFactType.ABORT_REQUESTED])  # 零新事实


class ConcurrencyTests(unittest.TestCase):
    """T10 / T11 / T12：并发证明（PROVEN / STRUCTURALLY SAFE 分级见报告）。"""

    def _run_threads(self, count, worker):
        barrier = threading.Barrier(count)
        errors = []

        def wrapped(n):
            try:
                barrier.wait()
                worker(n)
            except BaseException as exc:  # noqa: BLE001  测试侧收集
                errors.append(exc)

        threads = [threading.Thread(target=wrapped, args=(n,))
                   for n in range(count)]
        for thread in threads:
            thread.start()
            self.addCleanup(thread.join, 5.0)
        for thread in threads:
            thread.join()
        return errors

    def test_t10_concurrent_two_slot_invoke(self):
        group, journal, specs = make_group()
        per_slot = 4
        errors = self._run_threads(
            per_slot * 2,
            lambda n: group.slot("a" if n < per_slot else "b").invoke(
                make_request(task_id=f"task-{n}")))
        self.assertEqual(errors, [])
        self.assertEqual(len(spec_raw(specs[0]).entries), per_slot)
        self.assertEqual(len(spec_raw(specs[1]).entries), per_slot)
        # 零串线：各 raw 只见自己的 task 集合
        self.assertEqual(
            {r.task_id for r in spec_raw(specs[0]).requests},
            {f"task-{n}" for n in range(per_slot)})
        self.assertEqual(
            {r.task_id for r in spec_raw(specs[1]).requests},
            {f"task-{n}" for n in range(per_slot, per_slot * 2)})

    def test_t11_two_revisions_concurrent_two_slots(self):
        # 冻结语义：队列 execution-scoped ⇒ 每 slot 的该次委托
        # 携带当前全部 pending（R1+R2），各自 handoff
        group, journal, specs = make_group()
        group.boundary.submit(next_rev("R1", "text-1"))
        group.boundary.submit(next_rev("R2", "text-2"))
        errors = self._run_threads(
            2, lambda n: group.slot("a" if n == 0 else "b").invoke(
                make_request(task_id=f"task-{n}")))
        self.assertEqual(errors, [])
        for raw in (spec_raw(specs[0]), spec_raw(specs[1])):
            prompt = raw.requests[0].prompt
            self.assertIn("[USER REVISION R1]", prompt)  # 双修订全携带
            self.assertIn("[USER REVISION R2]", prompt)
        applied = [f for f in journal.snapshot()
                   if f.fact_type is ControlFactType.REVISION_APPLIED]
        self.assertEqual(len(applied), 4)  # 2 invocation × 2 revisions
        ids = {f.payload["invocation_id"] for f in applied}
        self.assertEqual(len(ids), 2)  # 恰两个 invocation（互异）
        # 每 invocation 恰携带 {R1, R2}，零串线零丢失
        per_invocation = {}
        for fact in applied:
            per_invocation.setdefault(fact.payload["invocation_id"],
                                      set()).add(fact.command_id)
        for revision_set in per_invocation.values():
            self.assertEqual(revision_set, {"R1", "R2"})

    def test_t12_same_revision_concurrent_two_slots(self):
        group, journal, specs = make_group()
        group.boundary.submit(next_rev("R1", "t"))
        errors = self._run_threads(
            2, lambda n: group.slot("a" if n == 0 else "b").invoke(
                make_request(task_id=f"task-{n}")))
        self.assertEqual(errors, [])
        facts = journal.snapshot()
        self.assertEqual([f.fact_type for f in facts],
                         [ControlFactType.REVISE_REQUESTED,
                          ControlFactType.REVISION_APPLIED,
                          ControlFactType.REVISION_APPLIED])
        # journal 锁证明：seq 严格连续无重复
        self.assertEqual([f.seq for f in facts], [0, 1, 2])
        applied = facts[1:]
        self.assertEqual({f.command_id for f in applied}, {"R1"})
        ids = [f.payload["invocation_id"] for f in applied]
        self.assertEqual(len(set(ids)), 2)  # 互异
        log_ids = {spec_log(specs[0]).snapshot()[0].invocation_id,
                   spec_log(specs[1]).snapshot()[0].invocation_id}
        self.assertEqual(set(ids), log_ids)  # 与各 slot record 对应


class PublicSurfaceTests(unittest.TestCase):
    """T13：公开面恰最小集合；slot() 缺席 KeyError。"""

    def test_execution_slots_surface(self):
        group, _, _ = make_group()
        self.assertIsInstance(group, ExecutionSlots)
        self.assertEqual(
            sorted(name for name in dir(group) if not name.startswith("_")),
            ["boundary", "slot"])  # 无 facts / 无 journal / 无映射本体

    def test_slot_handle_surface(self):
        group, _, _ = make_group()
        handle = group.slot("a")
        self.assertIsInstance(handle, SlotHandle)
        self.assertEqual(
            sorted(name for name in dir(handle) if not name.startswith("_")),
            ["invoke"])  # 唯一公开执行面

    def test_slot_handle_from_same_slot_is_pure_delegate(self):
        group, _, specs = make_group()
        sentinel = _success()
        specs[0].raw_adapter._result = sentinel
        self.assertIs(group.slot("a").invoke(make_request()), sentinel)

    def test_missing_slot_key_error(self):
        group, _, _ = make_group()
        with self.assertRaises(KeyError):
            group.slot("nope")

    def test_spec_surface_frozen_value(self):
        spec = make_spec("a")
        self.assertEqual(
            sorted(name for name in dir(spec) if not name.startswith("_")),
            ["capabilities", "raw_adapter", "role", "runtime_id",
             "slot_id", "usage_log"])
        with self.assertRaises(FrozenInstanceError):  # frozen：不可变值
            spec.slot_id = "x"

    def test_same_runtime_and_role_allowed(self):
        # 零人为 identity policy：同 runtime_id / 同 role 双 slot 合法
        raws = [_SpyRaw(result=_success()), _SpyRaw(result=_success())]
        specs = [make_spec("a", raw=raws[0], runtime_id="rt-same"),
                 make_spec("b", raw=raws[1], runtime_id="rt-same")]
        group, _, _ = make_group(specs=specs)
        group.slot("a").invoke(make_request())
        group.slot("b").invoke(make_request())
        self.assertEqual(len(raws[0].entries), 1)
        self.assertEqual(len(raws[1].entries), 1)


class EquivalenceTests(unittest.TestCase):
    """T15：单槽结构等价 + 独立行为等价于 build_wrap_stack。"""

    def _scenario(self, compose):
        journal = ControlJournal()  # 各自独立账本：绝不制造第二个 boundary
        entry = compose(journal)
        entry.boundary.submit(next_rev("r1", "rev text", "exec-eq"))
        entry.invoke(make_request())
        fact_types = [f.fact_type for f in journal.snapshot()]
        abort_flow = []
        entry.boundary.submit(abort_cmd("ab", "exec-eq"))
        try:
            entry.invoke(make_request(task_id="t2"))
        except ControlAborted:
            abort_flow.append("aborted")
        return fact_types, abort_flow

    def test_behavioral_equivalence_single_slot(self):
        holder = {}

        def via_slots(journal):
            raw = _SpyRaw(result=_success())
            holder["slots"] = raw
            group = build_execution_slots(
                journal, [make_spec("s", raw=raw)], execution_id="exec-eq")
            return _Entry(group.slot("s"), group.boundary)

        def via_stack(journal):
            raw = _SpyRaw(result=_success())
            holder["stack"] = raw
            stack = build_wrap_stack(
                journal, raw, UsageLog(), execution_id="exec-eq",
                runtime_id="rt-s", role="coder")
            return _Entry(stack, stack.boundary)

        self.assertEqual(self._scenario(via_slots),
                         self._scenario(via_stack))
        for key in ("slots", "stack"):
            prompt = holder[key].requests[0].prompt
            self.assertIn("[USER REVISION r1]", prompt)  # overlay 逐字同构
            self.assertIn("rev text", prompt)

    def test_structural_equivalence_component_set(self):
        # 两组装点使用同一冻结组件集（漂移 tripwire）
        def construction_names(path):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            names = set()
            for node in ast.walk(tree):
                if (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Name)
                        and node.func.id in FROZEN_COMPONENTS):
                    names.add(node.func.id)
            return names

        self.assertEqual(construction_names(MODULE_PATH),
                         FROZEN_COMPONENTS)
        self.assertEqual(construction_names(WRAP_STACK_PATH),
                         FROZEN_COMPONENTS)


class _Entry:
    """行为等价测试的统一入口（handle/boundary 二元组）。"""

    def __init__(self, invoke_target, boundary):
        self._invoke_target = invoke_target
        self.boundary = boundary

    def invoke(self, request):
        return self._invoke_target.invoke(request)


class TopologyTests(unittest.TestCase):
    """T16：组件拓扑 / import 守卫（AST 结构证明）。"""

    def _tree(self):
        return ast.parse(MODULE_PATH.read_text(encoding="utf-8"))

    def _sites(self, name):
        tree = self._tree()
        return [node for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == name]

    def _inside_loop(self, name):
        tree = self._tree()
        for node in ast.walk(tree):
            if isinstance(node, (ast.For, ast.While)):
                for sub in ast.walk(node):
                    if (isinstance(sub, ast.Call)
                            and isinstance(sub.func, ast.Name)
                            and sub.func.id == name):
                        return True
        return False

    def test_shared_constructions_outside_loop(self):
        # 恰一 boundary、恰一 journaler：唯一构造点且不在循环内
        for name in ("ControlBoundary", "RevisionAppliedJournaler"):
            self.assertEqual(len(self._sites(name)), 1, name)
            self.assertFalse(self._inside_loop(name), name)

    def test_per_slot_constructions_inside_loop(self):
        # 每 slot 组件：唯一构造点（循环体内）⇒ 每 spec 恰一实例
        for name in ("UsageCapture", "RevisionAdapter", "AbortGate",
                     "WrapStack"):
            self.assertEqual(len(self._sites(name)), 1, name)
            self.assertTrue(self._inside_loop(name), name)

    def test_single_boundary_construction_site(self):
        # 全源码恰一处 ControlBoundary( —— I2 结构证明
        source = MODULE_PATH.read_text(encoding="utf-8")
        self.assertEqual(source.count("ControlBoundary("), 1)

    def test_import_roots_locked(self):
        tree = self._tree()
        roots = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots.update(alias.name.split(".")[0]
                             for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                roots.add(node.module.split(".")[0])
        self.assertEqual(roots, {"__future__", "abort_gate",
                                 "control_boundary", "dataclasses",
                                 "revision_adapter",
                                 "revision_applied_journaler",
                                 "usage_capture", "wrap_stack"})


class FrozenFileTests(unittest.TestCase):
    """T14：冻结件零修改的结构守卫（git 边界在报告阶段另证）。"""

    def test_composition_root_public_surface_intact(self):
        import composition_root  # noqa: E402  依赖面未变的活证明
        self.assertEqual(
            sorted(name for name in dir(composition_root.CompositionRoot)
                   if not name.startswith("_")),
            ["compose_execution", "facts"])

    def test_wrap_stack_boundary_construction_untouched(self):
        source = WRAP_STACK_PATH.read_text(encoding="utf-8")
        self.assertEqual(source.count("ControlBoundary("), 1)  # 原样单点


class ConstructionFailureTests(unittest.TestCase):
    """T17：构造失败全矩阵——零残留、异常原样传播。"""

    def test_bad_raw_rejected(self):
        journal = ControlJournal()
        specs = [make_spec("a", raw=object())]
        with self.assertRaises(UsageCaptureError):
            build_execution_slots(journal, specs, execution_id="exec-f")
        self.assertEqual(journal.snapshot(), ())  # 零事实残留

    def test_bad_usage_log_rejected(self):
        journal = ControlJournal()
        specs = [make_spec("a", usage_log=object())]
        with self.assertRaises(UsageCaptureError):
            build_execution_slots(journal, specs, execution_id="exec-f")
        self.assertEqual(journal.snapshot(), ())

    def test_empty_specs_rejected(self):
        journal = ControlJournal()
        with self.assertRaises(ControlModelError):
            build_execution_slots(journal, [], execution_id="exec-f")
        self.assertEqual(journal.snapshot(), ())

    def test_duplicate_slot_id_rejected(self):
        journal = ControlJournal()
        specs = [make_spec("a"), make_spec("a")]
        with self.assertRaises(ControlModelError):
            build_execution_slots(journal, specs, execution_id="exec-f")
        self.assertEqual(journal.snapshot(), ())

    def test_non_spec_entry_rejected(self):
        journal = ControlJournal()
        with self.assertRaises(ControlModelError):
            build_execution_slots(journal, [("a", object())],
                                  execution_id="exec-f")
        self.assertEqual(journal.snapshot(), ())

    def test_bad_slot_id_shape_rejected(self):
        for bad_id in ("", "   "):
            with self.assertRaises(ControlModelError):
                ExecutionSlotSpec(slot_id=bad_id, raw_adapter=object(),
                                  usage_log=UsageLog(),
                                  runtime_id="rt", role="coder")
        with self.assertRaises(ControlModelError):
            ExecutionSlotSpec(slot_id=42, raw_adapter=object(),
                              usage_log=UsageLog(),
                              runtime_id="rt", role="coder")

    def test_empty_execution_id_rejected(self):
        journal = ControlJournal()
        with self.assertRaises(ControlModelError):  # boundary 既有校验
            build_execution_slots(
                journal, [make_spec("a")], execution_id="")
        self.assertEqual(journal.snapshot(), ())

    def test_journal_already_holds_execution_id_rejected(self):
        journal = ControlJournal()
        group, _, _ = make_group(journal=journal)
        group.boundary.submit(next_rev("r0", "t"))
        with self.assertRaises(ControlModelError):
            build_execution_slots(
                journal, [make_spec("a")], execution_id="exec-1")
        self.assertEqual(len(journal.snapshot()), 1)  # 原事实原封

    def test_failed_id_retry_succeeds(self):
        # 构造失败零登记：同 execution_id 用合法 spec 重来必须成功
        journal = ControlJournal()
        with self.assertRaises(UsageCaptureError):
            build_execution_slots(journal, [make_spec("a", raw=object())],
                                  execution_id="exec-f")
        self.assertEqual(journal.snapshot(), ())  # 失败零残留
        raw = _SpyRaw(result=_success())
        group = build_execution_slots(
            journal, [make_spec("a", raw=raw)], execution_id="exec-f")
        group.slot("a").invoke(make_request())
        self.assertEqual(len(raw.entries), 1)  # 重试成功且可执行


class DuplicateExecutionIdTests(unittest.TestCase):
    """T18：duplicate execution_id 拒绝（journal 历史证据路径）。"""

    def test_duplicate_execution_id_with_facts_rejected(self):
        journal = ControlJournal()
        group, _, _ = make_group(journal=journal, execution_id="exec-d")
        group.boundary.submit(next_rev("r0", "t", "exec-d"))  # 历史证据
        with self.assertRaises(ControlModelError):
            build_execution_slots(
                journal, [make_spec("a")], execution_id="exec-d")
        self.assertEqual(len(journal.snapshot()), 1)  # 原事实原封


class SourceScanTests(unittest.TestCase):
    """T19 / T20：源扫描——零生成、零锁、零捕获、零禁词。"""

    def test_no_identity_generation(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        for token in ("uuid", "uuid4", "random", "monotonic"):
            self.assertNotIn(token, source, token)

    def test_no_locking_no_threading(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        for token in ("threading", "Lock", "acquire", "release",
                      "RLock", "Semaphore"):
            self.assertNotIn(token, source, token)

    def test_no_try_no_catch(self):
        # 零 try/except：构造失败原样传播，绝不吞/译任何异常
        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            self.assertNotIsInstance(node, ast.Try)
            self.assertNotIsInstance(node, ast.ExceptHandler)

    def test_no_forbidden_tokens(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        for token in ("cockpit", "control_gate", "external_runtime",
                      "subprocess", "Popen", "ExecutionEvent",
                      "INVOCATION_STARTED", "INVOCATION_FINISHED",
                      "event_index", "trace_projector",
                      "ABORT_CONFIRMED", "PAUSE_CONFIRMED",
                      "ABORT_SUPERSEDED", "COMPLETED", "FAILED",
                      "drain", "broadcast", "routing", "schedule",
                      "orchestrat", "marketplace", "registry",
                      "claude", "gemini", "codex", "qwen", "opencode",
                      "cline", "deepseek"):
            self.assertNotIn(token, source, token)


if __name__ == "__main__":
    unittest.main()
