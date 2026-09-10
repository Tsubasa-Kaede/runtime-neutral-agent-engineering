"""REV-1 tests: RevisionAdapter（NEXT_INVOCATION 消费 + prompt overlay）。

栈位（V3.2 组合根未来接线）：

    RevisionAdapter → UsageCapture → raw adapter

契约：
- 只消费 pending NEXT_INVOCATION（CTRL-5 队列结构性只含该类；
  SUBMISSION 是 DISPATCH 阶语义，绝不入 overlay）。
- overlay 精确格式（FIFO、逐字、不重排不合并不去重）：
    <原 prompt 逐字不动>
    [USER REVISION <revision_id 逐字>]
    <修订文本逐字>
    [Obey all format rules above. END OF REVISION]
- 无 revision ⇒ request 原对象原样下传（等价调用）。
- 新 request 仅改 prompt（dataclasses.replace，其余字段恒等）；
  原 request 绝不原地修改。
- 不裁决/不落账/不 REVISION_APPLIED/不铸 invocation_id/零执行事件；
  revision ids 仅作 correlation 传给下层（OBS-4b 冻结缝）。
- raw 结果/异常（含 ControlAborted）原样透传；零 retry/fallback。
"""
import ast
import dataclasses
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from revision_adapter import RevisionAdapter  # noqa: E402
from control_boundary import (  # noqa: E402
    ControlBoundary,
    ControlCommand,
    ControlCommandType,
    ControlLifecycle,
    ControlModelError,
    PendingIntentKind,
    RevisionPayload,
    RevisionTarget,
)
from control_gate import ControlAborted  # noqa: E402  测试侧引用
from control_journal import ControlJournal  # noqa: E402
from external_runtime import ExternalAgentRequest  # noqa: E402  测试侧构造

MODULE_PATH = SCRIPTS / "revision_adapter.py"
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


def submission_rev(command_id, task, execution_id="exec-1"):
    return ControlCommand(
        command_id=command_id, execution_id=execution_id,
        command=ControlCommandType.REVISE,
        payload=RevisionPayload(target=RevisionTarget.SUBMISSION, task=task),
        expected_version=0)


def make_request(prompt="base prompt", task_id="task-1", agent_id="agent-1"):
    return ExternalAgentRequest(task_id=task_id, prompt=prompt,
                                agent_id=agent_id, role="coder")


class _SpyNext:
    """下层替身：记录 (request, applied_revisions)；可配置返回/抛出。"""

    def __init__(self, result=None, raises=None):
        self.calls = []  # [(request, applied_revisions or None)]
        self._result = result
        self._raises = raises

    def invoke(self, request, *, applied_revisions=()):
        self.calls.append((request, applied_revisions))
        if self._raises is not None:
            raise self._raises
        return self._result


def make_adapter(next_layer=None, execution_id="exec-1"):
    boundary, journal = make_boundary(execution_id)
    spy = next_layer if next_layer is not None else _SpyNext(
        result="raw-result")
    adapter = RevisionAdapter(boundary, spy)
    return adapter, boundary, journal, spy


class ConstructionTests(unittest.TestCase):
    def test_requires_control_boundary(self):
        with self.assertRaises(ControlModelError):
            RevisionAdapter(object(), _SpyNext())

    def test_requires_callable_next_invoke(self):
        _, boundary, _, _ = make_adapter()
        with self.assertRaises(ControlModelError):
            RevisionAdapter(boundary, object())


class NoRevisionTests(unittest.TestCase):
    """§3：无 revision ⇒ 完全等价 request（对象原样、调用原样）。"""

    def test_empty_queue_passes_same_request_object(self):
        adapter, _, _, spy = make_adapter()
        request = make_request()
        adapter.invoke(request)
        self.assertEqual(len(spy.calls), 1)
        seen, correlation = spy.calls[0]
        self.assertIs(seen, request)  # 同一对象，未重建
        self.assertEqual(correlation, ())  # 无 correlation

    def test_empty_queue_result_passthrough(self):
        sentinel = object()
        adapter, _, _, _ = make_adapter(_SpyNext(result=sentinel))
        self.assertIs(adapter.invoke(make_request()), sentinel)

    def test_pause_only_does_not_overlay(self):
        # PAUSE 不产生队列条目（CTRL-5）：照常等价下传
        adapter, boundary, _, spy = make_adapter()
        boundary.submit(ControlCommand(
            command_id="p1", execution_id="exec-1",
            command=ControlCommandType.PAUSE))
        request = make_request()
        adapter.invoke(request)
        seen, _ = spy.calls[0]
        self.assertIs(seen, request)
        self.assertEqual(seen.prompt, "base prompt")


class OverlayFormatTests(unittest.TestCase):
    """§3：精确 delimiter、逐字保真、FIFO。"""

    def test_single_revision_overlay_exact(self):
        adapter, boundary, _, spy = make_adapter()
        boundary.submit(next_rev("rev-1", "use output format X"))
        adapter.invoke(make_request())
        seen, _ = spy.calls[0]
        self.assertEqual(seen.prompt,
                         "base prompt\n"
                         "[USER REVISION rev-1]\n"
                         "use output format X\n"
                         + END_MARKER)

    def test_delimiter_lines_exact(self):
        adapter, boundary, _, spy = make_adapter()
        boundary.submit(next_rev("rev-1", "use output format X"))
        adapter.invoke(make_request())
        seen, _ = spy.calls[0]
        self.assertEqual(seen.prompt.split("\n"),
                         ["base prompt", "[USER REVISION rev-1]",
                          "use output format X", END_MARKER])

    def test_original_prompt_verbatim_prefix(self):
        original = "multi\nline\nprompt with 中文与 symbols !@#"
        adapter, boundary, _, spy = make_adapter()
        boundary.submit(next_rev("rev-1", "t"))
        adapter.invoke(make_request(prompt=original))
        seen, _ = spy.calls[0]
        self.assertTrue(seen.prompt.startswith(original + "\n"))
        self.assertEqual(seen.prompt.count(original), 1)  # 原文不被复制改动

    def test_revision_id_verbatim_in_marker(self):
        # revision_id 逐字入 marker（不前缀、不改写——correlation 精确）
        adapter, boundary, _, spy = make_adapter()
        boundary.submit(next_rev("cmd-42", "adjust tone"))
        adapter.invoke(make_request())
        seen, _ = spy.calls[0]
        self.assertIn("[USER REVISION cmd-42]\n", seen.prompt)
        self.assertNotIn("rev-cmd-42", seen.prompt)

    def test_revision_text_verbatim(self):
        text = "修订文本 keeps\nnewlines and spaces  intact 中文"
        adapter, boundary, _, spy = make_adapter()
        boundary.submit(next_rev("rev-1", text))
        adapter.invoke(make_request())
        seen, _ = spy.calls[0]
        self.assertIn(f"\n{text}\n", seen.prompt)

    def test_multiple_revisions_fifo_exact(self):
        adapter, boundary, _, spy = make_adapter()
        boundary.submit(next_rev("rev-1", "first change"))
        boundary.submit(next_rev("rev-2", "second change"))
        adapter.invoke(make_request())
        seen, _ = spy.calls[0]
        self.assertEqual(
            seen.prompt,
            "base prompt\n"
            "[USER REVISION rev-1]\nfirst change\n" + END_MARKER + "\n"
            "[USER REVISION rev-2]\nsecond change\n" + END_MARKER)

    def test_overlay_boundary_between_blocks(self):
        # 块边界：上一块 sentinel 行之后紧跟下一块 opener，零空行/零改写
        adapter, boundary, _, spy = make_adapter()
        boundary.submit(next_rev("rev-1", "a"))
        boundary.submit(next_rev("rev-2", "b"))
        adapter.invoke(make_request())
        lines = spy.calls[0][0].prompt.split("\n")
        self.assertEqual(lines,
                         ["base prompt",
                          "[USER REVISION rev-1]", "a", END_MARKER,
                          "[USER REVISION rev-2]", "b", END_MARKER])

    def test_empty_prompt_behavior_defined(self):
        # frozen request 无 prompt 校验（空可构造）：行为由本层定义——
        # 空原 prompt 时 overlay 以换行后首 delimiter 开始
        adapter, boundary, _, spy = make_adapter()
        boundary.submit(next_rev("rev-1", "t"))
        adapter.invoke(make_request(prompt=""))
        seen, _ = spy.calls[0]
        self.assertEqual(seen.prompt,
                         "\n[USER REVISION rev-1]\nt\n" + END_MARKER)


class RequestIntegrityTests(unittest.TestCase):
    """§6：immutable 原件 + 仅 prompt 变化的新构造。"""

    def test_original_request_object_unchanged(self):
        adapter, boundary, _, _ = make_adapter()
        boundary.submit(next_rev("rev-1", "t"))
        request = make_request()
        before = dataclasses.asdict(request)
        adapter.invoke(request)
        self.assertEqual(dataclasses.asdict(request), before)
        self.assertEqual(request.prompt, "base prompt")

    def test_non_prompt_fields_unchanged(self):
        adapter, boundary, _, spy = make_adapter()
        boundary.submit(next_rev("rev-1", "t"))
        handoff = (object(),)
        request = ExternalAgentRequest(
            task_id="task-9", prompt="p", agent_id="agent-9", role="coder",
            provider="prov", model="m", timeout_seconds=33.0,
            handoff_packets=handoff)
        adapter.invoke(request)
        seen, _ = spy.calls[0]
        self.assertIsNot(seen, request)  # 新构造，非原地
        self.assertIs(seen.task_id, request.task_id)
        self.assertIs(seen.agent_id, request.agent_id)
        self.assertIs(seen.role, request.role)
        self.assertIs(seen.provider, request.provider)
        self.assertIs(seen.model, request.model)
        self.assertEqual(seen.timeout_seconds, 33.0)
        self.assertIs(seen.handoff_packets, handoff)  # 不可变元组共享

    def test_reconstruction_changes_prompt_only(self):
        adapter, boundary, _, spy = make_adapter()
        boundary.submit(next_rev("rev-1", "t"))
        request = make_request()
        adapter.invoke(request)
        seen, _ = spy.calls[0]
        self.assertIs(type(seen), type(request))
        changed = {field.name for field in dataclasses.fields(request)
                   if getattr(seen, field.name)
                   != getattr(request, field.name)}
        self.assertEqual(changed, {"prompt"})

    def test_no_revision_no_reconstruction(self):
        # 无 revision 时绝不重建（等价下传同一对象）
        adapter, _, _, spy = make_adapter()
        request = make_request()
        adapter.invoke(request)
        self.assertIs(spy.calls[0][0], request)


class QueueSemanticsTests(unittest.TestCase):
    """§2：只消费 NEXT_INVOCATION；不裁决；不排干（REV-2 边界）。"""

    def test_submission_revision_not_consumed(self):
        adapter, boundary, _, spy = make_adapter()
        boundary.submit(submission_rev("sub-1", "new task text"))
        boundary.submit(next_rev("rev-1", "next-invocation text"))
        queue = boundary.snapshot(ControlLifecycle.RUNNING).revision_queue
        self.assertEqual(len(queue), 1)  # SUBMISSION 结构性不入队
        adapter.invoke(make_request())
        seen, _ = spy.calls[0]
        self.assertIn("next-invocation text", seen.prompt)
        self.assertNotIn("new task text", seen.prompt)  # SUBMISSION 绝不 overlay

    def test_no_readjudication_or_state_change(self):
        adapter, boundary, journal, _ = make_adapter()
        boundary.submit(next_rev("rev-1", "t"))
        facts_before = journal.snapshot()
        queue_before = boundary.snapshot(
            ControlLifecycle.RUNNING).revision_queue
        version_before = boundary.execution_version
        pending_before = boundary.pending_intent
        adapter.invoke(make_request())
        self.assertEqual(journal.snapshot(), facts_before)
        self.assertEqual(boundary.snapshot(
            ControlLifecycle.RUNNING).revision_queue, queue_before)
        self.assertEqual(boundary.execution_version, version_before)
        self.assertEqual(boundary.pending_intent, pending_before)
        self.assertEqual(boundary.pending_intent.kind,
                         PendingIntentKind.NONE)

    def test_queue_not_drained_by_rev1(self):
        # 移除/排干属 REV-2（APPLIED @ raw handoff）：REV-1 只读不删
        adapter, boundary, _, _ = make_adapter()
        boundary.submit(next_rev("rev-1", "a"))
        boundary.submit(next_rev("rev-2", "b"))
        adapter.invoke(make_request())
        queue = boundary.snapshot(ControlLifecycle.RUNNING).revision_queue
        self.assertEqual([entry.revision_id for entry in queue],
                         ["rev-1", "rev-2"])

    def test_repeated_invoke_same_overlay_when_queue_stable(self):
        # 构造纯函数：队列不变 ⇒ 两次 overlay 逐字相同、顺序稳定
        adapter, boundary, _, spy = make_adapter()
        boundary.submit(next_rev("rev-1", "a"))
        adapter.invoke(make_request())
        adapter.invoke(make_request())
        first, second = spy.calls[0][0], spy.calls[1][0]
        self.assertEqual(first.prompt, second.prompt)


class CorrelationTests(unittest.TestCase):
    """§5：revision ids 仅作 correlation 下传（OBS-4b 冻结缝）。"""

    def test_correlation_ids_passed_verbatim_fifo(self):
        adapter, boundary, _, spy = make_adapter()
        boundary.submit(next_rev("rev-1", "a"))
        boundary.submit(next_rev("rev-2", "b"))
        adapter.invoke(make_request())
        _, correlation = spy.calls[0]
        self.assertEqual(correlation, ("rev-1", "rev-2"))

    def test_no_correlation_when_queue_empty(self):
        adapter, _, _, spy = make_adapter()
        adapter.invoke(make_request())
        self.assertEqual(spy.calls[0][1], ())


class PassthroughTests(unittest.TestCase):
    """§7：raw 语义原样透传；零 retry/fallback。"""

    def test_raw_result_identity_preserved(self):
        sentinel = InvocationResultLike()
        adapter, _, _, _ = make_adapter(_SpyNext(result=sentinel))
        self.assertIs(adapter.invoke(make_request()), sentinel)

    def test_raw_exception_identity_preserved(self):
        sentinel = ValueError("raw exploded")
        adapter, _, _, _ = make_adapter(_SpyNext(raises=sentinel))
        with self.assertRaises(ValueError) as caught:
            adapter.invoke(make_request())
        self.assertIs(caught.exception, sentinel)

    def test_control_aborted_identity_preserved(self):
        sentinel = ControlAborted("exec-1")
        adapter, _, _, _ = make_adapter(_SpyNext(raises=sentinel))
        with self.assertRaises(ControlAborted) as caught:
            adapter.invoke(make_request())
        self.assertIs(caught.exception, sentinel)

    def test_no_retry_on_exception(self):
        # 异常立即传播：下层恰被调用一次，绝不重试
        spy = _SpyNext(raises=RuntimeError("boom"))
        adapter, _, _, _ = make_adapter(spy)
        with self.assertRaises(RuntimeError):
            adapter.invoke(make_request())
        self.assertEqual(len(spy.calls), 1)


class InvocationResultLike:
    """结构化哨兵（不 import 运行时结果类型也能做同一性断言）。"""
    pass


class PurityTests(unittest.TestCase):
    """§8/9/12：零身份铸造/零执行事件/零 provider 分支/import 锁。"""

    def test_no_journal_mutation(self):
        adapter, boundary, journal, _ = make_adapter()
        boundary.submit(next_rev("rev-1", "t"))
        facts_before = journal.snapshot()
        adapter.invoke(make_request())
        adapter.invoke(make_request())
        self.assertEqual(journal.snapshot(), facts_before)
        for fact in journal.snapshot():
            self.assertIn(fact.fact_type.value,
                          ("REVISE_REQUESTED",))

    def test_no_invocation_id_creation(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        for token in ("uuid", "monotonic", "time.", "import time"):
            self.assertNotIn(token, source, token)

    def test_no_execution_event_vocabulary(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        for token in ("ExecutionEvent", "INVOCATION_STARTED",
                      "INVOCATION_FINISHED", "execution_observation",
                      "REVISION_APPLIED"):
            self.assertNotIn(token, source, token)

    def test_no_provider_branching(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        for token in ("claude", "gemini", "codex", "qwen", "opencode",
                      "cline", "deepseek"):
            self.assertNotIn(token, source, token)

    def test_import_roots_locked(self):
        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        roots = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                roots.add(node.module.split(".")[0])
        self.assertEqual(roots, {"__future__", "dataclasses",
                                 "control_boundary"})


class ExecutionIsolationTests(unittest.TestCase):
    """§10-30：不同 execution 队列互不污染。"""

    def test_execution_isolation_between_queues(self):
        spy_a, spy_b = _SpyNext(), _SpyNext()
        adapter_a, boundary_a, _, _ = make_adapter(spy_a, "exec-a")
        adapter_b, boundary_b, _, _ = make_adapter(spy_b, "exec-b")
        boundary_a.submit(next_rev("rev-a1", "alpha only", "exec-a"))
        boundary_b.submit(next_rev("rev-b1", "beta only", "exec-b"))
        adapter_a.invoke(make_request())
        adapter_b.invoke(make_request())
        prompt_a = spy_a.calls[0][0].prompt
        prompt_b = spy_b.calls[0][0].prompt
        self.assertIn("alpha only", prompt_a)
        self.assertNotIn("beta only", prompt_a)
        self.assertIn("beta only", prompt_b)
        self.assertNotIn("alpha only", prompt_b)
        self.assertEqual(spy_a.calls[0][1], ("rev-a1",))
        self.assertEqual(spy_b.calls[0][1], ("rev-b1",))


if __name__ == "__main__":
    unittest.main()
