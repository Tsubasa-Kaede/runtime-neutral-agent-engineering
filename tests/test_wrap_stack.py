"""OBS-4c tests: WrapStack（cockpit 包装栈合法组合根）。

唯一执行路径（全真件，零编排）：

    WrapStack.invoke → AbortGate → RevisionAdapter → UsageCapture → raw

旁挂 observation：

    UsageCapture handoff seam → RevisionAppliedJournaler.on_handoff
        → RevisionWriter → 同一账本 REVISION_APPLIED

锁定语义：
- 单账本：boundary（accept 侧）与 journaler（applied 侧）同源于
  传入 journal——REVISE_REQUESTED 与 REVISION_APPLIED 恒共账本。
- accepted ≠ applied：ABORT pending ⇒ ControlAborted、raw 零进入、
  零 APPLIED；APPLIED 仅由真实 handoff 产生。
- APPLIED ≠ HONORED：B 级证据，不声称 runtime 遵守。
- 队列不排干：同队列二次 invoke ⇒ 两组 APPLIED（invocation_id
  互异）为合法真实物理事件，零 suppression。
- ControlGate 不接线（无 pause hold/park/lifecycle）。
"""
import ast
import threading
import time
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
import sys
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from wrap_stack import WrapStack, build_wrap_stack  # noqa: E402
from control_boundary import (  # noqa: E402
    ControlCommand,
    ControlCommandType,
    ControlLifecycle,
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

JOURNALER_PATH = SCRIPTS / "revision_applied_journaler.py"
WRAP_PATH = SCRIPTS / "wrap_stack.py"


def _success_result():
    return InvocationResult(status=InvocationStatus.SUCCESS)


class _SpyRaw:
    """真实 spy：方法体首行自记入口（委托证据）；可配置返回/抛出/阻塞。"""

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


def make_request(task_id="task-1", agent_id="agent-1", prompt="base prompt"):
    return ExternalAgentRequest(task_id=task_id, prompt=prompt,
                                agent_id=agent_id, role="coder")


def make_stack(raw=None, execution_id="exec-1", journal=None, usage_log=None):
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


class SurfaceTests(unittest.TestCase):
    """S1：最小公开面 + 单账本。"""

    def test_public_surface_and_same_journal(self):
        stack, journal, _, _ = make_stack()
        self.assertIsInstance(stack, WrapStack)
        self.assertTrue(callable(stack.invoke))
        self.assertIsNotNone(stack.boundary)
        # boundary 连于传入的同一 journal：submit 事实落入该账本
        stack.boundary.submit(next_rev("rev-1", "t"))
        facts = journal.snapshot()
        self.assertEqual(len(facts), 1)
        self.assertIs(facts[0].fact_type, ControlFactType.REVISE_REQUESTED)


class HappyPathTests(unittest.TestCase):
    """S2/S12：全链 overlay → 双事实 → correlation join → identity。"""

    def test_happy_path_full_chain(self):
        raw = _SpyRaw(result=_success_result())
        stack, journal, log, raw = make_stack(raw=raw)
        stack.boundary.submit(next_rev("rev-1", "use format X"))
        result = stack.invoke(make_request())
        self.assertIs(result, raw._result)  # S12: identity 贯穿全栈
        # overlay 到达 raw
        self.assertEqual(len(raw.entries), 1)
        self.assertIn("[USER REVISION rev-1]", raw.requests[0].prompt)
        # 账本：同 journal 内 REQUESTED → APPLIED
        facts = journal.snapshot()
        self.assertEqual([f.fact_type for f in facts],
                         [ControlFactType.REVISE_REQUESTED,
                          ControlFactType.REVISION_APPLIED])
        applied = facts[1]
        self.assertEqual(applied.command_id, "rev-1")  # join 键
        record = log.snapshot()[0]
        self.assertEqual(record.applied_revisions, ("rev-1",))
        self.assertEqual(applied.payload["invocation_id"],
                         record.invocation_id)  # 单一 id 来源跨账页


class FailureSemanticsTests(unittest.TestCase):
    """S3-S5：raw 异常 / KI / timeout 下 APPLIED 仍在。"""

    def test_raw_exception_applied_present(self):
        sentinel = RuntimeError("raw exploded")
        stack, journal, log, _ = make_stack(raw=_SpyRaw(raises=sentinel))
        stack.boundary.submit(next_rev("rev-1", "t"))
        with self.assertRaises(RuntimeError) as caught:
            stack.invoke(make_request())
        self.assertIs(caught.exception, sentinel)  # identity 保持
        self.assertEqual(len(applied_facts(journal)), 1)  # APPLIED durable
        self.assertEqual(log.snapshot(), ())  # OBS-4b：诚实缺席

    def test_keyboard_interrupt_applied_and_propagates(self):
        ki = KeyboardInterrupt()
        stack, journal, log, _ = make_stack(raw=_SpyRaw(raises=ki))
        stack.boundary.submit(next_rev("rev-1", "t"))
        with self.assertRaises(KeyboardInterrupt) as caught:
            stack.invoke(make_request())
        self.assertIs(caught.exception, ki)
        self.assertEqual(len(applied_facts(journal)), 1)
        self.assertEqual(log.snapshot(), ())

    def test_timeout_result(self):
        timed_out = InvocationResult(status=InvocationStatus.TIMEOUT)
        stack, journal, log, _ = make_stack(raw=_SpyRaw(result=timed_out))
        stack.boundary.submit(next_rev("rev-1", "t"))
        self.assertIs(stack.invoke(make_request()), timed_out)
        self.assertEqual(len(applied_facts(journal)), 1)
        self.assertEqual(log.snapshot()[0].status, "TIMEOUT")


class AbortSemanticsTests(unittest.TestCase):
    """S6-S7：ABORT 前置阻断 / ABORT 与 in-flight 并存。"""

    def test_abort_pending_blocks_and_no_applied(self):
        stack, journal, log, raw = make_stack()
        stack.boundary.submit(next_rev("rev-1", "t"))
        stack.boundary.submit(abort_cmd())
        with self.assertRaises(ControlAborted):
            stack.invoke(make_request())
        self.assertEqual(raw.entries, [])  # raw 零进入（spy 自证）
        fact_types = [f.fact_type for f in journal.snapshot()]
        self.assertIn(ControlFactType.ABORT_REQUESTED, fact_types)
        self.assertNotIn(ControlFactType.REVISION_APPLIED, fact_types)
        queue = stack.boundary.snapshot(
            ControlLifecycle.RUNNING).revision_queue
        self.assertEqual([e.revision_id for e in queue], ["rev-1"])  # 原封

    def test_abort_during_raw_coexists(self):
        release = threading.Event()
        result = _success_result()
        raw = _SpyRaw(result=result, release=release)
        stack, journal, _, _ = make_stack(raw=raw)
        stack.boundary.submit(next_rev("rev-1", "t"))
        outcome = []

        def worker():
            try:
                outcome.append(stack.invoke(make_request()))
            except BaseException as exc:  # noqa: BLE001  测试侧收集
                outcome.append(exc)

        thread = threading.Thread(target=worker)
        thread.start()
        self.addCleanup(thread.join, 10.0)
        for _ in range(2000):  # 等 raw 真实进入（委托证据）
            if raw.entries or not thread.is_alive():
                break
            time.sleep(0.005)
        stack.boundary.submit(abort_cmd())  # in-flight 期间 ABORT
        release.set()
        thread.join()
        self.assertEqual(outcome, [result])  # 不取消 in-flight，结果原样
        fact_types = [f.fact_type for f in journal.snapshot()]
        self.assertIn(ControlFactType.REVISION_APPLIED, fact_types)
        self.assertIn(ControlFactType.ABORT_REQUESTED, fact_types)
        self.assertEqual(stack.boundary.pending_intent.kind.name,
                         "ABORT")  # 与 APPLIED 并存


class IdempotencyTests(unittest.TestCase):
    """S8-S9：replay 零副作用 / 同队列两次两组 APPLIED。"""

    def test_command_replay_zero_side_effects(self):
        stack, journal, _, _ = make_stack()
        stack.boundary.submit(next_rev("rev-1", "t"))
        stack.invoke(make_request())
        facts_after_first = journal.snapshot()
        queue_after_first = stack.boundary.snapshot(
            ControlLifecycle.RUNNING).revision_queue
        replay = stack.boundary.submit(next_rev("rev-1", "t"))  # exact replay
        self.assertEqual(replay.status.value, "ACCEPTED")
        self.assertEqual(journal.snapshot(), facts_after_first)  # 零新事实
        self.assertEqual(stack.boundary.snapshot(
            ControlLifecycle.RUNNING).revision_queue,
            queue_after_first)  # 零新队列

    def test_same_queue_twice_two_applied_distinct_invocations(self):
        stack, journal, log, _ = make_stack()
        stack.boundary.submit(next_rev("rev-1", "t"))
        stack.invoke(make_request(task_id="task-a"))
        stack.invoke(make_request(task_id="task-b"))
        applied = applied_facts(journal)
        self.assertEqual(len(applied), 2)  # 合法重复，零 suppression
        self.assertEqual([f.command_id for f in applied], ["rev-1", "rev-1"])
        ids = [f.payload["invocation_id"] for f in applied]
        self.assertNotEqual(ids[0], ids[1])  # 物理事件互异
        record_ids = [r.invocation_id for r in log.snapshot()]
        self.assertEqual(sorted(ids), sorted(record_ids))  # 与记录对齐


class QueueSemanticsTests(unittest.TestCase):
    """S10-S11：FIFO 多修订 / 空队列等价下传。"""

    def test_multiple_fifo_revisions(self):
        raw = _SpyRaw(result=_success_result())
        stack, journal, log, _ = make_stack(raw=raw)
        stack.boundary.submit(next_rev("rev-1", "first"))
        stack.boundary.submit(next_rev("rev-2", "second"))
        stack.invoke(make_request())
        prompt = raw.requests[0].prompt
        self.assertLess(prompt.index("rev-1"), prompt.index("rev-2"))
        applied = applied_facts(journal)
        self.assertEqual([f.command_id for f in applied],
                         ["rev-1", "rev-2"])  # 账本 FIFO
        record = log.snapshot()[0]
        self.assertEqual(record.applied_revisions, ("rev-1", "rev-2"))

    def test_empty_queue_same_request_object(self):
        raw = _SpyRaw(result=_success_result())
        stack, journal, _, _ = make_stack(raw=raw)
        request = make_request()
        stack.invoke(request)
        self.assertIs(raw.requests[0], request)  # 原对象等价下传
        self.assertEqual(journal.snapshot(), ())  # 零事实


class ConstructionTests(unittest.TestCase):
    """S13：底层既有异常类型原样传播。"""

    def test_bad_raw_propagates(self):
        with self.assertRaises(UsageCaptureError):
            build_wrap_stack(ControlJournal(), object(), UsageLog(),
                             execution_id="exec-1", runtime_id="r",
                             role="coder")

    def test_bad_usage_log_propagates(self):
        with self.assertRaises(UsageCaptureError):
            build_wrap_stack(ControlJournal(), _SpyRaw(), object(),
                             execution_id="exec-1", runtime_id="r",
                             role="coder")

    def test_bad_journal_propagates(self):
        with self.assertRaises(AttributeError):
            build_wrap_stack(object(), _SpyRaw(), UsageLog(),
                             execution_id="exec-1", runtime_id="r",
                             role="coder")


class OrderingTests(unittest.TestCase):
    """S14：APPLIED 先于 UsageRecord append。"""

    def test_applied_precedes_usage_record(self):
        journal = ControlJournal()

        class _SnapLog:
            def __init__(self):
                self.snapshots = []

            def append(self, record):
                self.snapshots.append(journal.snapshot())

        snap_log = _SnapLog()
        stack, _, _, _ = make_stack(journal=journal, usage_log=snap_log)
        stack.boundary.submit(next_rev("rev-1", "t"))
        stack.invoke(make_request())
        # UsageRecord append 时刻，账本已含 REQUESTED + APPLIED
        self.assertEqual(
            [f.fact_type for f in snap_log.snapshots[0]],
            [ControlFactType.REVISE_REQUESTED,
             ControlFactType.REVISION_APPLIED])


class ArchitectureTests(unittest.TestCase):
    """S15：AST roots + 禁串 + provider 独立。"""

    def _roots(self, path):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        roots = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots.update(alias.name.split(".")[0]
                             for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                roots.add(node.module.split(".")[0])
        return roots

    def test_journaler_roots_locked(self):
        self.assertEqual(self._roots(JOURNALER_PATH),
                         {"__future__", "control_boundary",
                          "control_journal"})

    def test_wrap_roots_locked(self):
        self.assertEqual(self._roots(WRAP_PATH),
                         {"__future__", "abort_gate", "control_boundary",
                          "revision_applied_journaler", "revision_adapter",
                          "usage_capture"})  # 无 control_gate

    def test_no_forbidden_tokens(self):
        for path in (JOURNALER_PATH, WRAP_PATH):
            source = path.read_text(encoding="utf-8")
            for token in ("control_gate", "external_runtime", "subprocess",
                          "Popen", "kill", "terminate", "ExecutionEvent",
                          "INVOCATION_STARTED", "INVOCATION_FINISHED",
                          "event_index", "trace_projector", "cockpit",
                          "claude", "gemini", "codex", "qwen", "opencode",
                          "cline", "deepseek"):
                self.assertNotIn(token, source, f"{path.name}:{token}")


if __name__ == "__main__":
    unittest.main()
