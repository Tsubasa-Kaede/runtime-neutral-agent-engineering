"""REV-2 tests: UsageCapture handoff observation seam（CU-REV-2）。

栈位：

    RevisionAdapter → UsageCapture → raw adapter
                              │
                              └─ handoff observer（未来 wrap 层挂点）

本套只证明 seam 本身：
- APPLIED 认识论等级 = 对已绑定 raw invoke callable 的委托调用
  表达式在当前线程被发起（B 级）。不声称 method body 一定进入、
  不声称 runtime 进程启动。
- 委托证据一律取自真实 spy/fake raw adapter 自身方法体的入口
  记录——绝不使用"marker 传入 observer"式循环证明。
- observer 触发条件：observer 在场 且 applied_revisions 非空；
  两参逐字（invocation_id 原值、applied_revisions 原对象）。
- Exception 级隔离：observer 异常吞没、outcome 不变；raw 的
  异常（含 BaseException 如 KeyboardInterrupt）identity 原样传播。
- 时序：raw 入口 → observer → UsageLog record。
- invocation_id 唯一铸造点仍在 UsageCapture。
"""
import ast
import inspect
import sys
import threading
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from usage_capture import UsageCapture, UsageCaptureError  # noqa: E402
from external_runtime import (  # noqa: E402  测试侧构造真实协议形状
    ExternalAgentRequest,
    InvocationResult,
    InvocationStatus,
)
from usage_log import UsageLog, UsageObservation  # noqa: E402

MODULE_PATH = SCRIPTS / "usage_capture.py"


def make_request(task_id="task-1", agent_id="agent-1"):
    return ExternalAgentRequest(task_id=task_id, prompt="do work",
                                agent_id=agent_id, role="coder")


def _success():
    return InvocationResult(status=InvocationStatus.SUCCESS)


class _SpyRaw:
    """真实 spy raw adapter：在自己的方法体首行记录入口（委托证据）。

    委托是否发生，只由本类自身的 entries 记录作证——observer 与
    marker 互不相干，杜绝循环证明。
    """

    def __init__(self, result=None, raises=None, events=None):
        self.entries = []  # 方法体入口记录（delegation 证据）
        self._result = result
        self._raises = raises
        self._events = events

    def invoke(self, request):
        self.entries.append(request.task_id)  # 方法体首行：入口已发生
        if self._events is not None:
            self._events.append("entry")
        if self._raises is not None:
            raise self._raises
        return self._result


class _Observer:
    """observer 替身：记录 (invocation_id, applied_revisions)；可抛出。"""

    def __init__(self, raises=None):
        self.calls = []
        self._raises = raises

    def __call__(self, invocation_id, applied_revisions):
        self.calls.append((invocation_id, applied_revisions))
        if self._raises is not None:
            raise self._raises


def make_capture(raw=None, observer=None, usage_log=None, **kwargs):
    if usage_log is None:
        usage_log = UsageLog()
    if raw is None:
        raw = _SpyRaw(result=_success())
    if observer is not None:
        kwargs["handoff_observer"] = observer
    capture = UsageCapture(raw, usage_log, runtime_id="runtime-x",
                           role="coder", **kwargs)
    return capture, usage_log


class CompatibilityTests(unittest.TestCase):
    """§9-1..4：无 observer 时与 OBS-4b 完全兼容；API 不变。"""

    def test_no_observer_preserves_obs4b_behavior(self):
        raw = _SpyRaw(result=_success())
        log = UsageLog()
        capture = UsageCapture(raw, log, runtime_id="runtime-x", role="coder")
        result = capture.invoke(make_request(),
                                applied_revisions=("rev-1",))
        self.assertIs(result, raw._result)  # 结果原样透传
        record = log.snapshot()[0]  # 记录照写、correlation 照带
        self.assertEqual(record.applied_revisions, ("rev-1",))

    def test_explicit_none_observer_fully_compatible(self):
        capture, log = make_capture(observer=None)
        capture.invoke(make_request(), applied_revisions=("rev-1",))
        self.assertEqual(len(log.snapshot()), 1)  # 零行为差异

    def test_non_callable_observer_rejected(self):
        with self.assertRaises(UsageCaptureError):
            UsageCapture(_SpyRaw(), UsageLog(), runtime_id="runtime-x",
                         role="coder", handoff_observer=object())

    def test_invoke_signature_unchanged(self):
        params = list(
            inspect.signature(UsageCapture.invoke).parameters.values())
        self.assertEqual([p.name for p in params],
                         ["self", "request", "applied_revisions"])
        self.assertEqual(params[2].kind, inspect.Parameter.KEYWORD_ONLY)
        self.assertEqual(params[2].default, ())


class TriggerTests(unittest.TestCase):
    """§9-5..8：触发条件 = observer 在场 且 revisions 非空。"""

    def test_empty_revisions_return_not_fired(self):
        observer = _Observer()
        capture, _ = make_capture(observer=observer)
        capture.invoke(make_request())  # applied_revisions 缺省 ()
        self.assertEqual(observer.calls, [])

    def test_empty_revisions_raise_not_fired(self):
        observer = _Observer()
        capture, _ = make_capture(
            raw=_SpyRaw(raises=RuntimeError("boom")), observer=observer)
        with self.assertRaises(RuntimeError):
            capture.invoke(make_request())
        self.assertEqual(observer.calls, [])

    def test_single_revision_return_fired_exactly_once(self):
        observer = _Observer()
        capture, _ = make_capture(observer=observer)
        capture.invoke(make_request(), applied_revisions=("rev-1",))
        self.assertEqual(len(observer.calls), 1)
        self.assertEqual(observer.calls[0][1], ("rev-1",))

    def test_multiple_revisions_single_firing_full_order(self):
        observer = _Observer()
        capture, _ = make_capture(observer=observer)
        capture.invoke(make_request(),
                       applied_revisions=("rev-b", "rev-a"))
        self.assertEqual(len(observer.calls), 1)  # 一次委托一次通知
        self.assertEqual(observer.calls[0][1], ("rev-b", "rev-a"))  # 原顺序


class DelegationProofTests(unittest.TestCase):
    """§9-9..14：fired ⇔ 真实 delegation（证据=spy 自身入口记录）。"""

    def test_firing_iff_real_delegation(self):
        # return / timeout / raise 三态下：spy 入口恰一次 ⇔ 触发恰一次
        cases = [
            ("return", _success(), None),
            ("timeout", InvocationResult(status=InvocationStatus.TIMEOUT),
             None),
            ("raise", None, RuntimeError("late crash")),
        ]
        for name, result, raises in cases:
            raw = _SpyRaw(result=result, raises=raises)
            observer = _Observer()
            capture = UsageCapture(raw, UsageLog(), runtime_id="r",
                                   role="coder",
                                   handoff_observer=observer)
            if raises is not None:
                with self.assertRaises(RuntimeError):
                    capture.invoke(make_request(task_id=name),
                                   applied_revisions=("rev-x",))
            else:
                capture.invoke(make_request(task_id=name),
                               applied_revisions=("rev-x",))
            self.assertEqual(len(raw.entries), 1, name)  # 委托证据
            self.assertEqual(len(observer.calls), 1, name)  # fired ⇔ entered

    def test_raw_body_entry_precedes_firing(self):
        sequence = []

        class _SeqRaw:
            def invoke(self, request):
                sequence.append("raw-entry")  # 方法体首行自证
                return _success()

        def observer(invocation_id, applied_revisions):
            sequence.append("observer")

        capture = UsageCapture(_SeqRaw(), UsageLog(), runtime_id="r",
                               role="coder", handoff_observer=observer)
        capture.invoke(make_request(), applied_revisions=("rev-1",))
        self.assertEqual(sequence, ["raw-entry", "observer"])

    def test_first_line_raise_still_fires(self):
        # spy 方法体首行即 raise：入口已发生 ⇒ 仍触发
        raw = _SpyRaw(raises=RuntimeError("instant"))
        observer = _Observer()
        capture = UsageCapture(raw, UsageLog(), runtime_id="r",
                               role="coder", handoff_observer=observer)
        with self.assertRaises(RuntimeError):
            capture.invoke(make_request(), applied_revisions=("rev-1",))
        self.assertEqual(len(raw.entries), 1)  # 委托真实发生
        self.assertEqual(len(observer.calls), 1)

    def test_attribute_resolution_failure_does_not_fire(self):
        # 对抗替身：invoke 属性在构造校验后的下一次解析时消失。
        # 解引用发生在 try 外 ⇒ AttributeError 传播、零触发、零记录
        # ——证明触发 ⟺ 调用表达式被发起，而非 wrapper 被进入。
        class _FlakyAttrRaw:
            def __init__(self):
                self.accesses = 0

            @property
            def invoke(self):
                self.accesses += 1
                if self.accesses > 1:
                    raise AttributeError("invoke attribute vanished")

                def _invoke(request):
                    raise AssertionError("must never be called")

                return _invoke

        raw = _FlakyAttrRaw()
        observer = _Observer()
        log = UsageLog()
        capture = UsageCapture(raw, log, runtime_id="r", role="coder",
                               handoff_observer=observer)
        with self.assertRaises(AttributeError):
            capture.invoke(make_request(), applied_revisions=("rev-1",))
        self.assertEqual(raw.accesses, 2)  # 构造校验 1 次 + 解引用 1 次
        self.assertEqual(observer.calls, [])  # 未触发
        self.assertEqual(log.snapshot(), ())  # 无记录


class FailureSemanticsTests(unittest.TestCase):
    """§9-15..19：return/raise/timeout/observer 异常全谱。"""

    def test_timeout_fires_and_records_timeout(self):
        observer = _Observer()
        capture, log = make_capture(
            raw=_SpyRaw(result=InvocationResult(
                status=InvocationStatus.TIMEOUT)),
            observer=observer)
        capture.invoke(make_request(), applied_revisions=("rev-1",))
        self.assertEqual(len(observer.calls), 1)  # 执行失败不撤销 handoff
        self.assertEqual(log.snapshot()[0].status, "TIMEOUT")
        self.assertEqual(log.snapshot()[0].usage_status,
                         UsageObservation.UNKNOWN)

    def test_raw_exception_fires_identity_preserved_no_record(self):
        sentinel = RuntimeError("adapter exploded")
        observer = _Observer()
        capture, log = make_capture(raw=_SpyRaw(raises=sentinel),
                                    observer=observer)
        with self.assertRaises(RuntimeError) as caught:
            capture.invoke(make_request(), applied_revisions=("rev-1",))
        self.assertIs(caught.exception, sentinel)  # identity 原样
        self.assertEqual(len(observer.calls), 1)  # handoff 事实成立
        self.assertEqual(log.snapshot(), ())  # 诚实缺席（≠ UNKNOWN）

    def test_observer_exception_outcome_preserved(self):
        result = _success()
        observer = _Observer(raises=ValueError("observer down"))
        capture, log = make_capture(raw=_SpyRaw(result=result),
                                    observer=observer)
        self.assertIs(capture.invoke(make_request(),
                                     applied_revisions=("rev-1",)),
                      result)  # outcome 不变
        self.assertEqual(len(log.snapshot()), 1)  # record 照写
        self.assertEqual(len(observer.calls), 1)

    def test_observer_and_raw_exceptions_raw_identity_wins(self):
        raw_sentinel = RuntimeError("raw original")
        observer = _Observer(raises=ValueError("observer noise"))
        capture, log = make_capture(raw=_SpyRaw(raises=raw_sentinel),
                                    observer=observer)
        with self.assertRaises(RuntimeError) as caught:
            capture.invoke(make_request(), applied_revisions=("rev-1",))
        self.assertIs(caught.exception, raw_sentinel)  # 禁令：原始异常不被覆盖
        self.assertEqual(len(observer.calls), 1)
        self.assertEqual(log.snapshot(), ())

    def test_keyboard_interrupt_fires_then_propagates(self):
        ki = KeyboardInterrupt()
        observer = _Observer()
        capture, log = make_capture(raw=_SpyRaw(raises=ki),
                                    observer=observer)
        with self.assertRaises(KeyboardInterrupt) as caught:
            capture.invoke(make_request(), applied_revisions=("rev-1",))
        self.assertIs(caught.exception, ki)  # BaseException 原样传播
        self.assertEqual(len(observer.calls), 1)  # delegation 已发生 ⇒ 触发
        self.assertEqual(log.snapshot(), ())


class OrderingTests(unittest.TestCase):
    """§9-20..22：entry < observer < record。"""

    def test_ordering_entry_observer_record(self):
        sequence = []

        class _SeqRaw:
            def invoke(self, request):
                sequence.append("entry")
                return _success()

        class _SeqLog:
            def append(self, record):
                sequence.append("record")

        def observer(invocation_id, applied_revisions):
            sequence.append("observer")

        capture = UsageCapture(_SeqRaw(), _SeqLog(), runtime_id="r",
                               role="coder", handoff_observer=observer)
        capture.invoke(make_request(), applied_revisions=("rev-1",))
        self.assertEqual(sequence, ["entry", "observer", "record"])


class IdentityTests(unittest.TestCase):
    """§9-23..25：单一 id 来源；revisions 原对象透传。"""

    def test_observer_id_equals_record_id(self):
        observer = _Observer()
        capture, log = make_capture(observer=observer)
        capture.invoke(make_request(), applied_revisions=("rev-1",))
        fired_id, _ = observer.calls[0]
        record = log.snapshot()[0]
        self.assertEqual(fired_id, record.invocation_id)  # 同一铸造
        self.assertRegex(fired_id, r"^[0-9a-f]{32}$")  # opaque hex

    def test_applied_revisions_same_object_verbatim(self):
        observer = _Observer()
        capture, _ = make_capture(observer=observer)
        revisions = ["rev-9", "rev-1"]
        capture.invoke(make_request(), applied_revisions=revisions)
        _, seen = observer.calls[0]
        self.assertIs(seen, revisions)  # 同一 list 对象：零转换/零复制
        self.assertEqual(seen, ["rev-9", "rev-1"])  # 不排序、不去重

    def test_single_uuid_mint_site(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        self.assertEqual(source.count("uuid4("), 1)  # 唯一铸造点
        for token in ("uuid1", "uuid3", "uuid5", "uuid.UUID"):
            self.assertNotIn(token, source, token)


class ConcurrencyTests(unittest.TestCase):
    """§9-26..29：并发无串线。"""

    def test_concurrent_distinct_ids_own_revisions_no_crosstalk(self):
        observer = _Observer()
        capture, log = make_capture(observer=observer)
        count = 8
        barrier = threading.Barrier(count)
        errors = []

        def worker(n):
            try:
                barrier.wait()
                capture.invoke(make_request(task_id=f"task-{n}"),
                               applied_revisions=(f"rev-{n}",))
            except BaseException as exc:  # noqa: BLE001  测试侧收集
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(n,))
                   for n in range(count)]
        for thread in threads:
            thread.start()
            self.addCleanup(thread.join, 3.0)
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(observer.calls), count)  # 每次调用恰一次
        ids = [invocation_id for invocation_id, _ in observer.calls]
        self.assertEqual(len(set(ids)), count)  # id 无重复
        seen = [revisions for _, revisions in observer.calls]
        self.assertEqual(sorted(r[0] for r in seen),
                         sorted(f"rev-{n}" for n in range(count)))
        for revisions in seen:
            self.assertEqual(len(revisions), 1)  # 零串线/零合并
        records = log.snapshot()
        self.assertEqual(len(records), count)
        self.assertEqual({r.invocation_id for r in records}, set(ids))


class ArchitectureTests(unittest.TestCase):
    """§13：import 锁 + 禁字面量 + try 结构机械 AST 检查。"""

    def test_import_roots_locked(self):
        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        roots = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots.update(alias.name.split(".")[0]
                             for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                roots.add(node.module.split(".")[0])
        self.assertEqual(roots, {"__future__", "time", "uuid",
                                 "observation_capability", "usage_log"})

    def test_forbidden_dependencies_absent(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        for token in ("control_boundary", "control_journal", "control_gate",
                      "event_index", "trace_projector", "cockpit",
                      "external_runtime", "subprocess", "Popen", "kill",
                      "terminate", "REVISION_APPLIED",
                      "claude", "gemini", "codex", "qwen", "opencode",
                      "cline", "deepseek"):
            self.assertNotIn(token, source, token)

    def test_try_body_single_call_expression(self):
        # 铁律机械化：handoff try = 唯一"单语句赋值调用式"Try，且其
        # 调用恰为 raw_invoke(request)；raw_invoke 全模块唯一调用点。
        # （模块内另有两个 Exception 隔离 try：UsageLog append 与
        # observer 通知——它们不是 handoff try，不在此约束内。）
        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        handoff_tries = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Try):
                body = node.body
                if (len(body) == 1 and isinstance(body[0], ast.Assign)
                        and isinstance(body[0].value, ast.Call)):
                    handoff_tries.append(node)
        self.assertEqual(len(handoff_tries), 1)
        statement = handoff_tries[0].body[0]
        call = statement.value
        self.assertIsInstance(call.func, ast.Name)
        self.assertEqual(call.func.id, "raw_invoke")
        self.assertEqual(len(call.args), 1)
        self.assertEqual(len(call.keywords), 0)
        raw_calls = [node for node in ast.walk(tree)
                     if isinstance(node, ast.Call)
                     and isinstance(node.func, ast.Name)
                     and node.func.id == "raw_invoke"]
        self.assertEqual(len(raw_calls), 1)  # 委托调用唯一出现在该 try 内

    def test_dereference_outside_try(self):
        # §6.A：raw_invoke 解引用必须发生在一切 Try 结构之外
        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        inside = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Try):
                for part in (node.body, node.handlers, node.orelse,
                             node.finalbody):
                    for statement in part:
                        for sub in ast.walk(statement):
                            inside.add(id(sub))
        derefs = [node for node in ast.walk(tree)
                  if isinstance(node, ast.Assign)
                  and isinstance(node.value, ast.Attribute)
                  and node.value.attr == "invoke"]
        self.assertEqual(len(derefs), 1)  # 唯一解引用：构造期 getattr 不算
        self.assertNotIn(id(derefs[0]), inside)


if __name__ == "__main__":
    unittest.main()
