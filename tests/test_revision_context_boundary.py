"""CU-CONTEXT W2 — REVISION BOUNDARY QUALIFICATION 测试。

边界资格测试（boundary qualification），不是能力实现测试：在当前
冻结执行契约下钉死「REVISION 不进入 production Compiler path」。

钉定的三条现状边界：

- NEXT_INVOCATION 唯一生产链恒为（四用同读，RevisionAdapter 独占）：

      revision_queue → RevisionAdapter.invoke() → snapshot → overlay
      → applied_revisions → consume_revisions()

- SUBMISSION 恒经 TASK 文本通道（fresh segment 合并文本 → TASK
  条目 → Compiler）；生产树不存在任何 Revision Context bridge。
- REVISION compiler kind 恒 dormant：语义基础设施（item_from_revision
  桥 / SUPERSEDED 见证 / 政策位）自 Context-1/3 起存在且行为完好，
  但生产树零调用点。「基础设施存在」与「生产接线」在本文内被分开
  证明：前者是语义层既成能力，后者为零。

本文件不包含、也不暗示任何未来自动实现；钉定的是现状边界本身。

全部离线；REAL=0。
"""
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
from cockpit_compile import CompilePolicy, compile_context  # noqa: E402
from cockpit_context import (  # noqa: E402
    ContextItemKind,
    ContextSnapshot,
    ContextValidity,
    derive_validity,
    item_from_revision,
    task_item,
)
from cockpit_context_wire import compile_invocation_context  # noqa: E402
from control_boundary import (  # noqa: E402
    ControlBoundary,
    ControlCommand,
    ControlCommandType,
    ControlLifecycle,
    ControlStatus,
    RevisionPayload,
    RevisionTarget,
)
from control_journal import ControlJournal  # noqa: E402
from external_runtime import ExternalAgentRequest  # noqa: E402
from revision_adapter import RevisionAdapter  # noqa: E402

_WIRE_SOURCE = Path(
    cockpit_context_wire.__file__).read_text(encoding="utf-8")
_ENTRY_SOURCE = Path(cockpit_entry.__file__).read_text(encoding="utf-8")


def _production_sources():
    """scripts 树全部生产源，逐 (文件名, 源文本)。"""
    for path in sorted(SCRIPTS.glob("*.py")):
        yield path.name, path.read_text(encoding="utf-8")


def _files_containing(token):
    return {name for name, text in _production_sources() if token in text}


def _make_boundary(execution_id="w2-exec"):
    """离线真 boundary：REVISE 受理与队列真源（REAL=0，零 provider）。"""
    return ControlBoundary(journal=ControlJournal(),
                           execution_id=execution_id)


def _submit_next_invocation(boundary, command_id, text):
    """TUI DISPATCH 同形命令：NEXT_INVOCATION 只收 text。"""
    return boundary.submit(ControlCommand(
        command_id=command_id, execution_id=boundary.execution_id,
        command=ControlCommandType.REVISE,
        payload=RevisionPayload(target=RevisionTarget.NEXT_INVOCATION,
                                text=text),
        expected_version=boundary.execution_version))


def _request(prompt="base prompt", task_id="w2-task"):
    return ExternalAgentRequest(task_id=task_id, prompt=prompt,
                                agent_id="agent-w2", role="coder")


class _SpyNext:
    """下层替身：记录 (request, applied_revisions)；可配置返回/抛出。"""

    def __init__(self, result="spy-result", raises=None):
        self.calls = []
        self._result = result
        self._raises = raises

    def invoke(self, request, *, applied_revisions=()):
        self.calls.append((request, applied_revisions))
        if self._raises is not None:
            raise self._raises
        return self._result


class WireRevisionWallTests(unittest.TestCase):
    """边界 1/2：wire 零修订操作词汇、零控制面触碰。"""

    def test_wire_source_free_of_revision_operational_tokens(self):
        for token in ("item_from_revision", "revision_queue",
                      "PendingRevision", "consume_revisions",
                      "ControlBoundary", "control_boundary",
                      "snapshot(", "REVISE", "expected_version"):
            self.assertNotIn(token, _WIRE_SOURCE)

    def test_wire_public_face_is_sole_module_surface(self):
        self.assertEqual(cockpit_context_wire.__all__,
                         ("compile_invocation_context",))
        for name in ("item_from_revision", "PendingRevision",
                     "ControlBoundary"):
            self.assertFalse(hasattr(cockpit_context_wire, name))

    def test_wire_never_imports_control_plane(self):
        self.assertNotIn("from control_boundary import", _WIRE_SOURCE)
        # 双形态各恰一（flat + package 分支字面不同：后者带前导点）
        self.assertEqual(_WIRE_SOURCE.count("from cockpit_context import"), 1)
        self.assertEqual(
            _WIRE_SOURCE.count("from .cockpit_context import"), 1)
        self.assertEqual(_WIRE_SOURCE.count("from cockpit_compile import"), 1)
        self.assertEqual(
            _WIRE_SOURCE.count("from .cockpit_compile import"), 1)


class QueueArbitrationTests(unittest.TestCase):
    """边界 3/7：队列唯一消费性读取、唯一消费调用点、applied truth 归属。"""

    def test_consume_revisions_sole_production_call_site(self):
        self.assertEqual(_files_containing(".consume_revisions("),
                         {"revision_adapter.py"})

    def test_queue_reads_outside_owner_are_adapter_and_entry_projection(self):
        hits = _files_containing(".revision_queue") - {"control_boundary.py"}
        self.assertEqual(hits, {"revision_adapter.py", "cockpit_entry.py"})

    def test_entry_queue_read_is_length_only_projection(self):
        lines = _ENTRY_SOURCE.splitlines()
        indexes = [i for i, line in enumerate(lines)
                   if ".revision_queue" in line]
        self.assertEqual(len(indexes), 1)
        window = lines[indexes[0] - 1] + "\n" + lines[indexes[0]]
        self.assertIn("len(", window)
        self.assertIn("snapshot(", window)

    def test_boundary_remains_queue_owner(self):
        source = (SCRIPTS / "control_boundary.py").read_text(
            encoding="utf-8")
        self.assertIn("revision_queue=tuple(self._queue)", source)
        self.assertIn("self._queue.append(PendingRevision(", source)

    def test_correlation_tuple_minted_only_in_revision_adapter(self):
        self.assertEqual(
            _files_containing("tuple(entry.revision_id for entry in queue)"),
            {"revision_adapter.py"})

    def test_correlation_passed_to_invoke_only_by_revision_adapter(self):
        self.assertEqual(
            _files_containing("invoke(overlaid, applied_revisions=applied)"),
            {"revision_adapter.py"})

    def test_applied_truth_journaler_never_reads_queue_or_consumes(self):
        source = (SCRIPTS / "revision_applied_journaler.py").read_text(
            encoding="utf-8")
        self.assertIn("def on_handoff", source)
        for token in ("revision_queue", "consume_revisions", ".snapshot("):
            self.assertNotIn(token, source)

    def test_usage_capture_references_correlation_without_queue_access(self):
        source = (SCRIPTS / "usage_capture.py").read_text(encoding="utf-8")
        self.assertIn("applied_revisions", source)
        for token in ("revision_queue", "consume_revisions"):
            self.assertNotIn(token, source)


class OverlayOwnershipTests(unittest.TestCase):
    """边界 4：overlay 格式与 prompt application 真值唯一属主。"""

    def test_overlay_format_lives_solely_in_revision_adapter(self):
        for token in ("[USER REVISION", "END OF REVISION"):
            self.assertEqual(_files_containing(token),
                             {"revision_adapter.py"})


class NextInvocationChainTests(unittest.TestCase):
    """唯一生产链行为证明（真 boundary，全离线，REAL=0）：
    snapshot → overlay → applied_revisions → consume_revisions。"""

    def test_snapshot_overlay_correlation_consume_single_chain(self):
        boundary = _make_boundary()
        result = _submit_next_invocation(
            boundary, "rev-1", "REV TEXT ONE")
        self.assertIs(result.status, ControlStatus.ACCEPTED)
        queue = boundary.snapshot(ControlLifecycle.RUNNING).revision_queue
        self.assertEqual(
            tuple(entry.revision_id for entry in queue), ("rev-1",))

        spy = _SpyNext()
        adapter = RevisionAdapter(boundary, spy)
        request = _request()
        self.assertEqual(adapter.invoke(request), "spy-result")

        overlaid, applied = spy.calls[0]
        self.assertEqual(applied, ("rev-1",))
        self.assertTrue(overlaid.prompt.startswith(request.prompt))
        self.assertEqual(overlaid.prompt.count("[USER REVISION"), 1)
        self.assertIn("REV TEXT ONE", overlaid.prompt)
        self.assertIn(
            "[Obey all format rules above. END OF REVISION]",
            overlaid.prompt)
        # 原 request 绝不被原地修改
        self.assertNotIn("REV TEXT ONE", request.prompt)
        # 委托发起后一次性消费：队列已空
        self.assertEqual(
            boundary.snapshot(ControlLifecycle.RUNNING).revision_queue, ())

    def test_fifo_two_revisions_in_submit_order(self):
        boundary = _make_boundary()
        self.assertIs(_submit_next_invocation(
            boundary, "rev-1", "TEXT A").status, ControlStatus.ACCEPTED)
        self.assertIs(_submit_next_invocation(
            boundary, "rev-2", "TEXT B").status, ControlStatus.ACCEPTED)
        spy = _SpyNext()
        RevisionAdapter(boundary, spy).invoke(_request())
        overlaid, applied = spy.calls[0]
        self.assertEqual(applied, ("rev-1", "rev-2"))
        self.assertLess(overlaid.prompt.index("[USER REVISION rev-1]"),
                        overlaid.prompt.index("[USER REVISION rev-2]"))
        self.assertEqual(
            boundary.snapshot(ControlLifecycle.RUNNING).revision_queue, ())

    def test_empty_queue_passes_request_object_unchanged(self):
        spy = _SpyNext()
        request = _request()
        RevisionAdapter(_make_boundary(), spy).invoke(request)
        overlaid, applied = spy.calls[0]
        self.assertIs(overlaid, request)
        self.assertEqual(applied, ())

    def test_consume_survives_delegate_failure(self):
        boundary = _make_boundary()
        _submit_next_invocation(boundary, "rev-1", "TEXT A")
        spy = _SpyNext(raises=RuntimeError("boom"))
        adapter = RevisionAdapter(boundary, spy)
        with self.assertRaises(RuntimeError):
            adapter.invoke(_request())
        # 消费事实不因结果成败改变（G1：委托已发起即消费）
        self.assertEqual(
            boundary.snapshot(ControlLifecycle.RUNNING).revision_queue, ())


class CompilerPathPurityTests(unittest.TestCase):
    """边界 5/6：W1 编译面只产 TASK/PRIOR；NEXT_INVOCATION 恰一次应用。"""

    @staticmethod
    def _prior(output="prior output", invocation_id="w2-inv-prior"):
        return SimpleNamespace(
            output=output,
            trace=SimpleNamespace(invocation_id=invocation_id,
                                  started_at=None, finished_at=None))

    def test_wire_signature_has_no_revision_channel(self):
        parameters = list(
            inspect.signature(compile_invocation_context).parameters)
        self.assertEqual(parameters, [
            "task_text", "task_id", "step_index", "previous_result",
            "producer_role", "prior_char_limit"])

    def test_compiled_kinds_subset_of_task_and_prior(self):
        for previous in (None, self._prior()):
            compiled = compile_invocation_context(
                "task text", "w2-task", 1, previous, "architect",
                prior_char_limit=4000)
            kinds = tuple(segment.kind for segment in compiled.segments)
            self.assertTrue(set(kinds) <= {
                ContextItemKind.TASK,
                ContextItemKind.PRIOR_STEP_OUTPUT})
            self.assertIs(kinds[0], ContextItemKind.TASK)

    def test_revision_text_absent_from_compiled_prompt_applied_once(self):
        boundary = _make_boundary()
        _submit_next_invocation(
            boundary, "rev-1", "W2 UNIQUE REVISION PAYLOAD 7C31")
        builder = cockpit_entry._make_request_builder(
            "boundary task", "w2-task", "coder", "provider-x", 60.0,
            step_index=0)
        request = builder(None)
        # Compiler path 零 REVISION：编译产物不含修订文本与标记
        self.assertNotIn("W2 UNIQUE REVISION PAYLOAD 7C31", request.prompt)
        self.assertNotIn("[USER REVISION", request.prompt)

        spy = _SpyNext()
        RevisionAdapter(boundary, spy).invoke(request)
        overlaid, applied = spy.calls[0]
        # 恰一次（结构上不可能双重应用）：编译基础逐字在前 + overlay 一块
        self.assertEqual(applied, ("rev-1",))
        self.assertTrue(overlaid.prompt.startswith(request.prompt))
        self.assertEqual(overlaid.prompt.count("[USER REVISION"), 1)
        self.assertIn("W2 UNIQUE REVISION PAYLOAD 7C31", overlaid.prompt)


class SubmissionTaskChannelTests(unittest.TestCase):
    """边界 8：SUBMISSION 恒经 TASK 文本通道，无 Revision Context bridge。"""

    def test_entry_steps_maps_submission_to_task_text_position(self):
        self.assertIn("submission.task if submission.task is not None",
                      _ENTRY_SOURCE)

    def test_merged_submission_text_compiles_as_task_item(self):
        merged = "SUBMISSION 合并后的任务文本 w2-5E2"
        compiled = compile_invocation_context(
            merged, "w2-task", 0, None, "coder", prior_char_limit=4000)
        task_segment = compiled.segments[0]
        self.assertIs(task_segment.kind, ContextItemKind.TASK)
        self.assertEqual(task_segment.payload, merged)

    def test_no_production_revision_context_bridge(self):
        # item_from_revision 全生产树零调用点：仅 __all__ 导出行 + 定义行
        self.assertEqual(_files_containing("item_from_revision"),
                         {"cockpit_context.py"})
        context_source = (SCRIPTS / "cockpit_context.py").read_text(
            encoding="utf-8")
        self.assertEqual(context_source.count("item_from_revision"), 2)


class DormantSemanticBridgeTests(unittest.TestCase):
    """边界 9：语义基础设施存在 ≠ 生产接线（两半分开证明）。

    前两/三项证明语义层既成能力（自 Context-1/3 起冻结在库）；
    「生产树零调用点」由 SubmissionTaskChannelTests 的 bridge 扫描
    与 WireRevisionWallTests 的词汇墙共同钉定——现状即：能力在库、
    接线为零。"""

    def test_bridge_mints_revision_items(self):
        item = item_from_revision(
            SimpleNamespace(revision_id="rev-9", text="semantic bridge text"),
            task_id="w2-task")
        self.assertIs(item.kind, ContextItemKind.REVISION)
        self.assertEqual(item.payload, "semantic bridge text")
        self.assertEqual(item.provenance.source_id, "rev-9")

    def test_superseded_witness_adjudicates_applied_ids(self):
        item = item_from_revision(
            SimpleNamespace(revision_id="rev-9", text="text"),
            task_id="w2-task")
        self.assertIs(derive_validity(item), ContextValidity.VALID)
        self.assertIs(
            derive_validity(item, applied_revision_ids=("rev-9",)),
            ContextValidity.SUPERSEDED)

    def test_compile_projects_revision_segment_when_fed_directly(self):
        item = item_from_revision(
            SimpleNamespace(revision_id="rev-9", text="direct feed text"),
            task_id="w2-task")
        task = task_item("task", task_id="w2-task")
        snapshot = ContextSnapshot(
            task_id="w2-task", step_index=0,
            items=(task, item),
            validity=(derive_validity(task), derive_validity(item)))
        compiled = compile_context(
            snapshot, CompilePolicy(), adjacent_invocation_ids=None)
        kinds = tuple(segment.kind for segment in compiled.segments)
        self.assertIn(ContextItemKind.REVISION, kinds)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
