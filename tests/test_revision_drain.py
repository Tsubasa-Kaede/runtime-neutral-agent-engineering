"""G1 (V3.2): NEXT_INVOCATION revision drain — one-shot trailing FIFO.

G1 授权语义（CU-TUI-2 前置 MICRO-CU，最终授权修订边界 = §1.3 十项）：

    snapshot → overlay → invoke → finally consume

- 消费边界：只在本委托调用表达式真正发起之后（finally 边界）精确
  消费所携带的修订——accepted / queued / overlay 构造阶段均不消费。
- 失败语义（授权锁定）：raw 异常或失败结果 ⇒ 委托已发起，仍消费；
  ABORT pending 在 AbortGate 前置拒绝 ⇒ 委托未发起 ⇒ 队列原封；
  observer / journal 落账失败不阻断消费（journaler 单条隔离，
  consume 位于 finally）；消费按 revision_id 精确删除、FIFO 序保持、
  在途新提交的不同 id 绝不误删。
- D2 并发有界重复（授权接受）：并发委托可能各自快照同一批修订；
  单修订并发 APPLIED ∈ {1, 2}、双修订 ∈ {2, 4}；每 invocation 携带
  集只能完整 pending 集或空集（结构性无部分携带）。本文件锁
  adapter/boundary 层的确定性面；slot 级并发不变量在
  test_execution_slots.py T11/T12。
- 词汇冻结：ControlFact / ControlCommand vocabularies 恒为既值。

全部替身离线；REAL=0（零 provider、零网络、零凭据、零安装）。
"""
import sys
import threading
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from control_boundary import (  # noqa: E402
    ControlBoundary,
    ControlCommand,
    ControlCommandType,
    ControlLifecycle,
    ControlModelError,
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
from revision_adapter import RevisionAdapter  # noqa: E402
from usage_log import UsageLog  # noqa: E402
from wrap_stack import build_wrap_stack  # noqa: E402

END_MARKER = "[Obey all format rules above. END OF REVISION]"


def make_boundary(execution_id="exec-1"):
    journal = ControlJournal()
    boundary = ControlBoundary(journal=journal, execution_id=execution_id)
    return boundary, journal


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


def make_request(prompt="base prompt", task_id="task-1", agent_id="agent-1"):
    return ExternalAgentRequest(task_id=task_id, prompt=prompt,
                                agent_id=agent_id, role="coder")


class _SpyNext:
    """下层替身：记录 (request, applied_revisions)；可配置返回/抛出。"""

    def __init__(self, result=None, raises=None):
        self.calls = []
        self._result = result
        self._raises = raises

    def invoke(self, request, *, applied_revisions=()):
        self.calls.append((request, applied_revisions))
        if self._raises is not None:
            raise self._raises
        return self._result


class _SubmittingNext:
    """委托进行中经同一 boundary 提交新修订（in-flight submitter）。"""

    def __init__(self, boundary):
        self._boundary = boundary
        self.calls = []

    def invoke(self, request, *, applied_revisions=()):
        self.calls.append((request, applied_revisions))
        self._boundary.submit(next_rev("late-1", "late text"))
        return "raw-result"


class _DuckNonDataclassRequest:
    """非 dataclass 鸭型请求：使 overlay 构造阶段确定性失败。"""

    def __init__(self):
        self.prompt = "base prompt"


class _RaisingRevisionWriter:
    def append(self, **kwargs):
        raise RuntimeError("journal append failed")


class _FailingRevisionJournal(ControlJournal):
    """revision_writer 落账恒失败：观察侧失败必须不阻断消费。"""

    def revision_writer(self):
        return _RaisingRevisionWriter()


def _success_result():
    return InvocationResult(status=InvocationStatus.SUCCESS)


class _SpyRaw:
    """raw 替身：记录入口与请求；可配置返回/抛出。"""

    def __init__(self, result=None, raises=None):
        self.entries = []
        self.requests = []
        self._result = result
        self._raises = raises

    def invoke(self, request):
        self.entries.append(request.task_id)
        self.requests.append(request)
        if self._raises is not None:
            raise self._raises
        return self._result


def make_stack(raw=None, journal=None, usage_log=None, execution_id="exec-1"):
    if journal is None:
        journal = ControlJournal()
    if usage_log is None:
        usage_log = UsageLog()
    if raw is None:
        raw = _SpyRaw(result=_success_result())
    stack = build_wrap_stack(journal, raw, usage_log,
                             execution_id=execution_id,
                             runtime_id="runtime-x", role="coder")
    return stack, journal, usage_log, raw


def applied_facts(journal):
    return [fact for fact in journal.snapshot()
            if fact.fact_type is ControlFactType.REVISION_APPLIED]


def queue_ids(boundary):
    return [entry.revision_id for entry in boundary.snapshot(
        ControlLifecycle.RUNNING).revision_queue]


# ------------------------------------------------------ adapter 层确定性面


class OneShotDrainTests(unittest.TestCase):
    """G1 核心：委托发起后一次性精确消费（one-shot trailing FIFO）。"""

    def test_one_revision_consumed_after_delegation(self):
        adapter, boundary, _, spy = _make_adapter()
        boundary.submit(next_rev("rev-1", "text-1"))
        adapter.invoke(make_request())
        seen, correlation = spy.calls[0]
        self.assertIn("[USER REVISION rev-1]", seen.prompt)
        self.assertIn("text-1", seen.prompt)
        self.assertIn(END_MARKER, seen.prompt)
        self.assertEqual(correlation, ("rev-1",))
        self.assertEqual(queue_ids(boundary), [])  # 发起后一次性消费

    def test_fifo_order_in_overlay_and_consumption(self):
        adapter, boundary, _, spy = _make_adapter()
        boundary.submit(next_rev("rev-1", "first text"))
        boundary.submit(next_rev("rev-2", "second text"))
        adapter.invoke(make_request())
        prompt = spy.calls[0][0].prompt
        self.assertLess(prompt.index("[USER REVISION rev-1]"),
                        prompt.index("[USER REVISION rev-2]"))  # FIFO 逐字
        self.assertEqual(queue_ids(boundary), [])  # 全量一次性消费

    def test_second_invocation_carries_nothing(self):
        adapter, boundary, _, spy = _make_adapter()
        boundary.submit(next_rev("rev-1", "text-1"))
        adapter.invoke(make_request())
        adapter.invoke(make_request())
        first, second = spy.calls[0][0], spy.calls[1][0]
        self.assertIn("[USER REVISION rev-1]", first.prompt)
        self.assertEqual(second.prompt, "base prompt")  # 原样零 overlay
        self.assertEqual(spy.calls[1][1], ())  # correlation 零携带

    def test_three_invocations_exactly_one_carry(self):
        adapter, boundary, _, spy = _make_adapter()
        boundary.submit(next_rev("rev-1", "text-1"))
        for _ in range(3):
            adapter.invoke(make_request())
        carried = [request for request, _ in spy.calls
                   if "[USER REVISION rev-1]" in request.prompt]
        self.assertEqual(len(carried), 1)  # 恰首次委托携带
        self.assertEqual(queue_ids(boundary), [])

    def test_no_revision_passes_request_object_through(self):
        adapter, boundary, _, spy = _make_adapter()
        request = make_request()
        result = adapter.invoke(request)
        self.assertIs(result, "raw-result")
        self.assertIs(spy.calls[0][0], request)  # 对象原样下传
        self.assertEqual(spy.calls[0][1], ())
        self.assertEqual(queue_ids(boundary), [])  # 空队列零作用


def _make_adapter(next_layer=None):
    boundary, journal = make_boundary()
    spy = next_layer if next_layer is not None else _SpyNext(
        result="raw-result")
    adapter = RevisionAdapter(boundary, spy)
    return adapter, boundary, journal, spy


class ConstructionBoundaryTests(unittest.TestCase):
    """构造阶段失败 ⇒ 委托未发起 ⇒ 不消费。"""

    def test_construction_failure_leaves_queue_intact(self):
        adapter, boundary, _, _ = _make_adapter()
        boundary.submit(next_rev("rev-1", "text-1"))
        with self.assertRaises(TypeError):
            adapter.invoke(_DuckNonDataclassRequest())  # replace 构造失败
        self.assertEqual(queue_ids(boundary), ["rev-1"])  # 队列原封


# -------------------------------------------------- consume API 契约面


class ConsumeApiTests(unittest.TestCase):
    """consume_revisions：锁内精确删除、FIFO 保持、零事实、输入校验。"""

    def test_removes_only_named_ids_and_returns_removed(self):
        boundary, _ = make_boundary()
        for name in ("r1", "r2", "r3"):
            boundary.submit(next_rev(name, "t"))
        removed = boundary.consume_revisions(("r1",))
        self.assertEqual([entry.revision_id for entry in removed], ["r1"])
        self.assertEqual(queue_ids(boundary), ["r2", "r3"])

    def test_unknown_id_is_noop(self):
        boundary, _ = make_boundary()
        boundary.submit(next_rev("r1", "t"))
        boundary.submit(next_rev("r2", "t"))
        removed = boundary.consume_revisions(("r1", "zzz-absent"))
        self.assertEqual([entry.revision_id for entry in removed], ["r1"])
        self.assertEqual(queue_ids(boundary), ["r2"])  # 未匹配 id 零作用

    def test_middle_removal_preserves_fifo_of_survivors(self):
        boundary, _ = make_boundary()
        for name in ("r1", "r2", "r3", "r4"):
            boundary.submit(next_rev(name, "t"))
        boundary.consume_revisions(("r2", "r3"))
        self.assertEqual(queue_ids(boundary), ["r1", "r4"])  # 序保持

    def test_empty_iterable_is_noop(self):
        boundary, _ = make_boundary()
        boundary.submit(next_rev("r1", "t"))
        self.assertEqual(boundary.consume_revisions(()), ())
        self.assertEqual(queue_ids(boundary), ["r1"])

    def test_rejects_bare_string_input(self):
        boundary, _ = make_boundary()
        boundary.submit(next_rev("r1", "t"))
        with self.assertRaises(ControlModelError):
            boundary.consume_revisions("r1")  # 单字符串绝不当序列拆
        self.assertEqual(queue_ids(boundary), ["r1"])

    def test_rejects_non_iterable_input(self):
        boundary, _ = make_boundary()
        with self.assertRaises(ControlModelError):
            boundary.consume_revisions(42)

    def test_rejects_empty_or_non_string_id(self):
        boundary, _ = make_boundary()
        with self.assertRaises(ControlModelError):
            boundary.consume_revisions(("",))
        with self.assertRaises(ControlModelError):
            boundary.consume_revisions((None,))
        with self.assertRaises(ControlModelError):
            boundary.consume_revisions((7,))

    def test_consume_writes_no_facts(self):
        # 消费真值 = 既有 REVISION_APPLIED（journaler 旁路）；
        # consume 本身零事实
        boundary, journal = make_boundary()
        boundary.submit(next_rev("r1", "t"))
        facts_before = journal.snapshot()
        boundary.consume_revisions(("r1",))
        self.assertEqual(journal.snapshot(), facts_before)

    def test_concurrent_consume_and_submit_lock_safe(self):
        boundary, _ = make_boundary()
        boundary.submit(next_rev("a", "t"))
        boundary.submit(next_rev("b", "t"))
        start = threading.Barrier(2)
        errors = []
        removed_box = []

        def consume_side():
            try:
                start.wait(5.0)
                removed_box.append(boundary.consume_revisions(("a",)))
            except Exception as error:  # pragma: no cover - 诊断通道
                errors.append(error)

        def submit_side():
            try:
                start.wait(5.0)
                boundary.submit(next_rev("c", "t"))
            except Exception as error:  # pragma: no cover - 诊断通道
                errors.append(error)

        threads = [threading.Thread(target=consume_side),
                   threading.Thread(target=submit_side)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5.0)
        self.assertEqual(errors, [])
        self.assertEqual([entry.revision_id
                          for entry in removed_box[0]], ["a"])
        # b 恒先于 c（提交序保持）；a 恒被精确移除
        self.assertEqual(queue_ids(boundary), ["b", "c"])


# ------------------------------------------------------ replay 交互面


class ReplayInteractionTests(unittest.TestCase):
    """消费与 CTRL-4 replay 幂等的交互：replay 零副作用不复辟队列。"""

    def test_exact_replay_after_consume_zero_side_effects(self):
        boundary, journal = make_boundary()
        command = next_rev("rev-1", "a")
        first = boundary.submit(command)
        adapter, _, _ = _make_adapter_with(boundary)
        adapter.invoke(make_request())  # 消费
        self.assertEqual(queue_ids(boundary), [])
        replay = boundary.submit(command)  # 精确重放
        self.assertIs(replay, first)  # 纯查找返回首次结果对象
        self.assertEqual(queue_ids(boundary), [])  # 队列不复辟
        self.assertEqual(len(journal.snapshot()), 1)  # 零新事实

    def test_new_revision_after_consume_queues_again(self):
        # 消费后同语义新命令（不同 id）是新 intent：照常入队
        boundary, _ = make_boundary()
        boundary.submit(next_rev("rev-1", "same text"))
        adapter, _, _ = _make_adapter_with(boundary)
        adapter.invoke(make_request())
        result = boundary.submit(next_rev("rev-2", "same text"))
        self.assertIs(result.status.value
                      if hasattr(result.status, "value") else result.status,
                      "ACCEPTED")
        self.assertEqual(queue_ids(boundary), ["rev-2"])


def _make_adapter_with(boundary):
    spy = _SpyNext(result="raw-result")
    adapter = RevisionAdapter(boundary, spy)
    return adapter, boundary, spy


# ------------------------------------------------------ 在途提交面


class MidFlightSubmitTests(unittest.TestCase):
    """委托进行中提交的新修订绝不被本次消费误删。"""

    def test_revision_submitted_during_delegation_survives(self):
        boundary, journal = make_boundary()
        spy = _SubmittingNext(boundary)
        adapter = RevisionAdapter(boundary, spy)
        boundary.submit(next_rev("rev-1", "in-flight text"))
        adapter.invoke(make_request())
        self.assertEqual(spy.calls[0][1], ("rev-1",))  # 携带快照时的全集
        self.assertEqual(queue_ids(boundary), ["late-1"])  # 在途新提交幸存


# ------------------------------------------------- wrap 层失败语义面


class WrapFailureBoundaryTests(unittest.TestCase):
    """AbortGate → RevisionAdapter → UsageCapture 全链失败边界。"""

    def test_raw_raise_still_consumes_and_propagates(self):
        sentinel = RuntimeError("raw exploded")
        stack, journal, _, raw = make_stack(raw=_SpyRaw(raises=sentinel))
        stack.boundary.submit(next_rev("rev-1", "t"))
        with self.assertRaises(RuntimeError) as caught:
            stack.invoke(make_request())
        self.assertIs(caught.exception, sentinel)  # 异常原样透传
        self.assertEqual(queue_ids(stack.boundary), [])  # 已发起 ⇒ 仍消费
        self.assertEqual(len(applied_facts(journal)), 1)  # B 级 APPLIED 仍在

    def test_abort_pending_revisions_untouched(self):
        # ABORT pending ⇒ AbortGate 前置拒绝 ⇒ 委托未发起 ⇒ 不消费
        stack, journal, _, raw = make_stack()
        stack.boundary.submit(next_rev("rev-1", "t"))
        stack.boundary.submit(abort_cmd())
        with self.assertRaises(ControlAborted):
            stack.invoke(make_request())
        self.assertEqual(queue_ids(stack.boundary), ["rev-1"])  # 队列原封
        self.assertEqual(raw.entries, [])  # raw 零进入
        self.assertEqual(applied_facts(journal), [])  # 零 APPLIED

    def test_journal_append_failure_does_not_block_consume(self):
        # journaler 单条隔离 + finally 消费：观察侧失败不改变消费事实
        stack, journal, _, _ = make_stack(
            journal=_FailingRevisionJournal())
        stack.boundary.submit(next_rev("rev-1", "t"))
        result = stack.invoke(make_request())  # 委托 outcome 不受影响
        self.assertIs(result.status, InvocationStatus.SUCCESS)
        self.assertEqual(queue_ids(stack.boundary), [])  # 消费照常闭合
        self.assertEqual(applied_facts(journal), [])  # 失败条目诚实缺席


# ------------------------------------------------ APPLIED / usage 关联面


class WrapCorrelationTests(unittest.TestCase):
    """one-shot 后：APPLIED 恰一组，与首次 usage record 同 invocation_id。"""

    def test_one_applied_group_correlated_to_first_invocation(self):
        stack, journal, log, _ = make_stack()
        stack.boundary.submit(next_rev("rev-1", "t"))
        stack.invoke(make_request(task_id="task-a"))
        stack.invoke(make_request(task_id="task-b"))
        records = log.snapshot()
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0].applied_revisions, ("rev-1",))
        self.assertEqual(records[1].applied_revisions, ())
        applied = applied_facts(journal)
        self.assertEqual(len(applied), 1)  # 恰一组（第二次零携带）
        self.assertEqual(applied[0].command_id, "rev-1")
        self.assertEqual(applied[0].payload["invocation_id"],
                         records[0].invocation_id)  # 单一 id 来源

    def test_three_invocations_usage_carries_once(self):
        stack, journal, log, _ = make_stack()
        stack.boundary.submit(next_rev("rev-1", "t"))
        for index in range(3):
            stack.invoke(make_request(task_id=f"task-{index}"))
        self.assertEqual([record.applied_revisions
                          for record in log.snapshot()],
                         [("rev-1",), (), ()])


# ------------------------------------------------------ 词汇冻结锁


class VocabularyLockTests(unittest.TestCase):
    """G1 不得改变控制域词汇（授权硬边界）。"""

    def test_control_fact_vocabulary_unchanged(self):
        self.assertEqual(
            {member.value for member in ControlFactType},
            {"PAUSE_REQUESTED", "PAUSE_CONFIRMED", "RESUME_REQUESTED",
             "REVISE_REQUESTED", "REVISION_APPLIED", "ABORT_REQUESTED",
             "ABORT_CONFIRMED", "ABORT_SUPERSEDED"})

    def test_control_command_vocabulary_unchanged(self):
        self.assertEqual(
            {member.value for member in ControlCommandType},
            {"PAUSE", "RESUME", "REVISE", "ABORT"})


if __name__ == "__main__":
    unittest.main()
