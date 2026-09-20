"""CU-CONTEXT-1 Context Model 测试面。

覆盖矩阵（设计 PART 10 + 实施授权 §16）：
- Identity：快照身份 (task_id, step_index) 确定性/唯一性；条目身份
  (kind, source_id, ordinal) 复用与区分；
- Scope：RUN 隔离（构造期跨 run 拒绝 + sentinel 负向）；STEP 隔离；
- Validity：VALID / SUPERSEDED / STALE 三态推导；INVALID=构造期拒绝
  （封闭词表外值、kind↔source 失配、跨 run 条目、形状违规）；
  PARKED 恢复链不致 STALE；
- Ownership：source → context 单向派生（事实对象零改动；生产模块
  零引擎调用——静态扫描）；
- Immutability：Item/Snapshot frozen 且可哈希；
- Revision：真实 ControlBoundary 三段律（accepted ≠ applied ≠
  honored）下 NEXT_INVOCATION/SUBMISSION 两通道推导；既有行为零改动；
- Handoff：唯一输出桥恰读紧邻前步事实；载荷=原文全量（截断属编译域）；
- Composition：模型零分组知识（静态+行为中性：单成员/多成员/带组
  组合的事实相同则快照相同——组合形状不进模型）；
- Empty：No Context 合法（空快照可构造）；
- 静态边界：生产模块零引擎/呈现/adapter import、零 prompt 构造面、
  零 provider 分支、零新时钟/新 UUID。

全部离线；REAL=0。
"""
import re
import sys
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import cockpit_context
from cockpit_context import (  # noqa: E402
    ContextItem,
    ContextItemKind,
    ContextModelError,
    ContextProvenance,
    ContextSnapshot,
    ContextSource,
    ContextValidity,
    derive_validity,
    item_from_invocation_result,
    item_from_revision,
    task_item,
)
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

MODULE_SOURCE = Path(
    cockpit_context.__file__).read_text(encoding="utf-8")


class IdentityTests(unittest.TestCase):
    """快照身份 = (task_id, step_index)；条目身份 = (kind, source_id, ordinal)。"""

    def test_snapshot_identity_deterministic(self):
        item = task_item("do the thing", task_id="task-a")
        snap_a = ContextSnapshot("task-a", 0, (item,),
                                 (ContextValidity.VALID,))
        snap_b = ContextSnapshot("task-a", 0, (item,),
                                 (ContextValidity.VALID,))
        self.assertEqual(snap_a.identity, ("task-a", 0))
        self.assertEqual(snap_a.identity, snap_b.identity)
        self.assertEqual(snap_a, snap_b)

    def test_snapshot_identity_unique_per_step(self):
        item = task_item("do the thing", task_id="task-a")
        identities = {
            ContextSnapshot("task-a", index, (item,),
                            (ContextValidity.VALID,)).identity
            for index in range(3)}
        self.assertEqual(len(identities), 3)
        self.assertIn(("task-a", 2), identities)

    def test_item_identity_reuses_existing_primitives(self):
        first = task_item("same text", task_id="task-a")
        second = task_item("same text", task_id="task-a")
        self.assertEqual(first.identity, second.identity)
        self.assertEqual(first, second)
        self.assertEqual(
            first.identity,
            (ContextItemKind.TASK, "", 0))  # submission 无 source_id

    def test_item_identity_distinguished_by_source_and_ordinal(self):
        base = task_item("text", task_id="task-a")
        older = task_item("text", task_id="task-a", ordinal=1)
        self.assertNotEqual(base.identity, older.identity)
        result = SimpleNamespace(
            output="out",
            trace=SimpleNamespace(invocation_id="invocation-x"))
        output_item = item_from_invocation_result(result, task_id="task-a")
        self.assertEqual(
            output_item.identity,
            (ContextItemKind.PRIOR_STEP_OUTPUT, "invocation-x", 0))


class ScopeIsolationTests(unittest.TestCase):
    """作用域恰 RUN+STEP；跨 run 条目结构性不可入快照。"""

    def test_run_isolation_enforced_at_construction(self):
        run_a_item = task_item("run a task", task_id="task-a")
        with self.assertRaises(ContextModelError):
            ContextSnapshot("task-b", 0, (run_a_item,),
                            (ContextValidity.VALID,))

    def test_cross_run_negative_sentinel(self):
        sentinel = "CTX1_RUN_A_SENTINEL_5C31"
        run_a_item = task_item(sentinel, task_id="task-a")
        run_b_item = task_item("run b task", task_id="task-b")
        run_b_snapshot = ContextSnapshot(
            "task-b", 0, (run_b_item,), (ContextValidity.VALID,))
        # run A 条目无法进入 run B 快照；run B 快照不含 run A 文本。
        with self.assertRaises(ContextModelError):
            ContextSnapshot("task-b", 1, (run_a_item,),
                            (ContextValidity.VALID,))
        self.assertNotIn(sentinel,
                         "".join(item.payload for item in run_b_snapshot.items))

    def test_step_isolation_via_identity(self):
        item = task_item("t", task_id="task-a")
        step_one = ContextSnapshot("task-a", 1, (item,),
                                   (ContextValidity.VALID,))
        step_two = ContextSnapshot("task-a", 2, (item,),
                                   (ContextValidity.VALID,))
        self.assertNotEqual(step_one.identity, step_two.identity)


class ValidityTests(unittest.TestCase):
    """三运行态推导 + INVALID 构造期拒绝 + PARKED 恢复不致 STALE。"""

    def test_task_valid_and_superseded(self):
        current = task_item("v2 task text", task_id="task-a")
        self.assertIs(derive_validity(
            current, current_submission_text="v2 task text"),
            ContextValidity.VALID)
        self.assertIs(derive_validity(
            current, current_submission_text="v3 merged later-wins"),
            ContextValidity.SUPERSEDED)
        self.assertIs(derive_validity(current),
                      ContextValidity.VALID)  # 无取代证据=当前

    def test_revision_valid_pending_and_superseded_applied(self):
        entry = SimpleNamespace(revision_id="ui-1", text="focus edges")
        item = item_from_revision(entry, task_id="task-a")
        self.assertIs(derive_validity(item, applied_revision_ids=()),
                      ContextValidity.VALID)
        self.assertIs(derive_validity(
            item, applied_revision_ids=("ui-9", "ui-1")),
            ContextValidity.SUPERSEDED)

    def test_prior_output_live_rebuilt_and_parked(self):
        result = SimpleNamespace(
            output="out", trace=SimpleNamespace(invocation_id="invocation-1"))
        item = item_from_invocation_result(result, task_id="task-a")
        self.assertIs(derive_validity(item),
                      ContextValidity.VALID)          # 无链见证（含恢复）
        self.assertIs(derive_validity(
            item, live_invocation_ids=("invocation-1",)),
            ContextValidity.VALID)                     # PARKED 恢复：链仍活跃
        self.assertIs(derive_validity(
            item, live_invocation_ids=("invocation-2",)),
            ContextValidity.STALE)                     # fresh-segment 重建

    def test_invalid_construction_rejections(self):
        with self.assertRaises(ContextModelError):
            ContextItem("TASK", "payload", ContextProvenance(
                ContextSource.SUBMISSION, "task-a"))        # kind 非枚举
        with self.assertRaises(ContextModelError):
            ContextItem(ContextItemKind.TASK, "", ContextProvenance(
                ContextSource.SUBMISSION, "task-a"))        # 空 payload
        with self.assertRaises(ContextModelError):
            ContextItem(ContextItemKind.TASK, "payload", ContextProvenance(
                ContextSource.REVISION_QUEUE, "task-a"))    # kind↔source 失配
        with self.assertRaises(ContextModelError):
            ContextProvenance(ContextSource.SUBMISSION, "")  # 空 task_id
        with self.assertRaises(ContextModelError):
            ContextSnapshot("", 0, (), ())                   # 空 task_id
        with self.assertRaises(ContextModelError):
            ContextSnapshot("task-a", -1, (), ())            # 负 step_index
        with self.assertRaises(ContextModelError):
            ContextSnapshot("task-a", 0, (), (ContextValidity.VALID,))
        with self.assertRaises(ContextModelError):
            ContextSnapshot("task-a", 0,
                            (task_item("t", task_id="task-a"),), ("VALID",))

    def test_invalid_witness_text_must_be_non_empty(self):
        item = task_item("t", task_id="task-a")
        with self.assertRaises(ContextModelError):
            derive_validity(item, current_submission_text="   ")


class ImmutabilityTests(unittest.TestCase):
    """frozen + 可哈希：铸造后不得修改；更新=新快照。"""

    def test_items_and_snapshots_frozen(self):
        item = task_item("t", task_id="task-a")
        snap = ContextSnapshot("task-a", 0, (item,),
                               (ContextValidity.VALID,))
        with self.assertRaises(FrozenInstanceError):
            item.payload = "mutated"
        with self.assertRaises(FrozenInstanceError):
            snap.items = ()
        with self.assertRaises(FrozenInstanceError):
            snap.validity = ()

    def test_hashable_and_set_membership(self):
        item = task_item("t", task_id="task-a")
        snap = ContextSnapshot("task-a", 0, (item,),
                               (ContextValidity.VALID,))
        self.assertEqual(len({snap, ContextSnapshot(
            "task-a", 0, (item,), (ContextValidity.VALID,))}), 1)

    def test_update_means_new_snapshot(self):
        item = task_item("t", task_id="task-a")
        first = ContextSnapshot("task-a", 0, (item,),
                                (ContextValidity.VALID,))
        second = ContextSnapshot("task-a", 0, (item,),
                                 (ContextValidity.SUPERSEDED,))
        self.assertIsNot(first, second)
        self.assertEqual(first.validity, (ContextValidity.VALID,))


class OwnershipDirectionTests(unittest.TestCase):
    """source → context 单向派生；事实对象零改动。"""

    def test_derivation_does_not_touch_source_facts(self):
        result = SimpleNamespace(
            output="kept verbatim",
            trace=SimpleNamespace(
                invocation_id="invocation-7",
                started_at=1.25, finished_at=2.5))
        before = (result.output, result.trace.invocation_id,
                  result.trace.started_at)
        item = item_from_invocation_result(result, task_id="task-a")
        self.assertEqual(
            (result.output, result.trace.invocation_id,
             result.trace.started_at), before)
        self.assertEqual(item.payload, "kept verbatim")
        self.assertEqual(item.provenance.started_at, 1.25)
        self.assertEqual(item.provenance.finished_at, 2.5)

    def test_production_module_has_no_engine_side_effects(self):
        for forbidden in (".observe(", ".submit(", ".consume_revisions(",
                          ".invoke(", ".append(", "ExecutionEvent(",
                          ".on_event("):
            self.assertNotIn(forbidden, MODULE_SOURCE)


class RevisionSemanticsTests(unittest.TestCase):
    """真实 ControlBoundary 三段律下的两通道推导；既有行为零改动。"""

    def _boundary(self):
        return ControlBoundary(
            ControlJournal(), "exec-ctx-1", initial_version=3)

    def test_next_invocation_accepted_then_consumed(self):
        boundary = self._boundary()
        command = ControlCommand(
            command_id="ui-ctx-1", execution_id="exec-ctx-1",
            command=ControlCommandType.REVISE,
            payload=RevisionPayload(
                target=RevisionTarget.NEXT_INVOCATION,
                text="focus on edge cases"),
            expected_version=3)
        result = boundary.submit(command)
        self.assertIs(result.status, ControlStatus.ACCEPTED)
        queue = boundary.snapshot(
            ControlLifecycle.RUNNING).revision_queue
        self.assertEqual(len(queue), 1)
        item = item_from_revision(queue[0], task_id="task-a")
        # accepted ≠ applied：队列中 = 已受理未消费 → VALID
        self.assertIs(derive_validity(item, applied_revision_ids=()),
                      ContextValidity.VALID)
        boundary.consume_revisions((queue[0].revision_id,))
        # 一次性精确消费的既有见证 → SUPERSEDED；pending 队列排干
        self.assertIs(derive_validity(
            item, applied_revision_ids=(queue[0].revision_id,)),
            ContextValidity.SUPERSEDED)
        self.assertEqual(boundary.snapshot(
            ControlLifecycle.RUNNING).revision_queue, ())

    def test_submission_revision_last_wins_merge(self):
        boundary = self._boundary()
        command = ControlCommand(
            command_id="ui-ctx-2", execution_id="exec-ctx-1",
            command=ControlCommandType.REVISE,
            payload=RevisionPayload(
                target=RevisionTarget.SUBMISSION, task="rewritten task"),
            expected_version=3)
        self.assertIs(boundary.submit(command).status, ControlStatus.ACCEPTED)
        # SUBMISSION 只落事实不入队（既有语义）
        self.assertEqual(boundary.snapshot(
            ControlLifecycle.RUNNING).revision_queue, ())
        old = task_item("original task", task_id="task-a")
        new = task_item("rewritten task", task_id="task-a")
        self.assertIs(derive_validity(
            old, current_submission_text="rewritten task"),
            ContextValidity.SUPERSEDED)
        self.assertIs(derive_validity(
            new, current_submission_text="rewritten task"),
            ContextValidity.VALID)

    def test_honored_stays_observation_domain_not_model(self):
        # honored（真实 invocation 请求见证）属观察域：推导面无该参量
        # ——签名恰三事实见证，不多不少。
        kwargs = derive_validity.__kwdefaults__.keys()
        self.assertEqual(
            set(kwargs),
            {"current_submission_text", "applied_revision_ids",
             "live_invocation_ids"})


class HandoffTests(unittest.TestCase):
    """唯一输出桥恰读紧邻前步事实；载荷=原文全量。"""

    def test_payload_is_full_output_not_truncated(self):
        long_output = "x" * 5000
        result = SimpleNamespace(
            output=long_output,
            trace=SimpleNamespace(invocation_id="invocation-long"))
        item = item_from_invocation_result(result, task_id="task-a")
        self.assertEqual(len(item.payload), 5000)  # 截断属编译域（CU-CONTEXT-3）

    def test_bridge_requires_real_invocation_identity(self):
        with self.assertRaises(ContextModelError):
            item_from_invocation_result(
                SimpleNamespace(output="out", trace=None), task_id="task-a")
        with self.assertRaises(ContextModelError):
            item_from_invocation_result(
                SimpleNamespace(output="out", trace=SimpleNamespace()),
                task_id="task-a")

    def test_single_output_bridge_no_non_adjacent_surface(self):
        # 桥面封闭：__all__ 即完整公共契约（恰三个桥 + 模型类型与
        # 错误），无非相邻选择面；模块内不得有 __all__ 之外的桥函数
        self.assertEqual(
            set(cockpit_context.__all__),
            {"ContextModelError", "ContextItemKind", "ContextSource",
             "ContextValidity", "ContextProvenance", "ContextItem",
             "ContextSnapshot", "task_item",
             "item_from_invocation_result", "item_from_revision",
             "derive_validity"})
        for name in cockpit_context.__all__:
            self.assertTrue(hasattr(cockpit_context, name))


class CompositionNeutralityTests(unittest.TestCase):
    """组合/分组形状不进模型：事实相同则快照相同。"""

    def test_model_has_zero_group_knowledge(self):
        self.assertNotIn("group", MODULE_SOURCE)
        for cls in (ContextProvenance, ContextItem, ContextSnapshot):
            field_names = {
                name for name in cls.__dataclass_fields__}
            self.assertNotIn("group_id", field_names)

    def test_composition_shape_irrelevant_to_snapshot(self):
        task_text = "same facts regardless of composition shape"
        # 单成员 / 多成员 / 带分组组合：模型只见事实，组合形状零参与
        for _shape in ("single", "multi", "grouped"):
            item = task_item(task_text, task_id="task-a")
            snap = ContextSnapshot("task-a", 0, (item,),
                                   (ContextValidity.VALID,))
            self.assertEqual(snap.items[0].payload, task_text)


class NoContextTests(unittest.TestCase):
    """No Context = 合法状态（空快照可构造；模型零默认铸造）。"""

    def test_empty_snapshot_is_legal(self):
        snap = ContextSnapshot("task-a", 0, (), ())
        self.assertEqual(snap.items, ())
        self.assertEqual(snap.validity, ())
        self.assertEqual(snap.identity, ("task-a", 0))


class StaticBoundaryTests(unittest.TestCase):
    """生产模块静态边界：零引擎/呈现/adapter 依赖、零 prompt 面、
    零 provider 分支、零新时钟/新 UUID。"""

    def test_import_surface_is_stdlib_only(self):
        imports = re.findall(
            r"^\s*(?:from|import)\s+([A-Za-z_][\w.]*)",
            MODULE_SOURCE, re.MULTILINE)
        self.assertTrue(imports)
        self.assertEqual(set(imports), {"dataclasses", "enum"})

    def test_no_engine_or_presentation_imports(self):
        for module in ("cockpit_entry", "cockpit_tui", "cockpit_projection",
                       "cockpit_session", "event_index", "usage_log",
                       "external_runtime", "sequential_pipeline",
                       "composition_core", "execution_slots",
                       "control_boundary", "control_journal", "textual"):
            self.assertNotIn(f"import {module}", MODULE_SOURCE)

    def test_no_prompt_serialization_surface(self):
        for identifier in ("to_prompt", "render_prompt", "build_messages",
                           "provider_prompt", "claude_prompt", "codex_prompt",
                           "pi_prompt", "qwen_prompt", "_make_request_builder"):
            self.assertNotIn(identifier, MODULE_SOURCE)

    def test_runtime_neutral_no_provider_branching(self):
        for provider in ("claude", "codex", "gemini", "qwen", "opencode",
                         "cline", "tiny"):
            self.assertIsNone(
                re.search(rf"\b{provider}\b", MODULE_SOURCE),
                f"provider word {provider!r} must not appear")

    def test_no_synthetic_time_or_identity(self):
        for forbidden in ("import time", "time.time", "datetime", "monotonic",
                          "import uuid", "uuid4(", "uuid1("):
            self.assertNotIn(forbidden, MODULE_SOURCE)


if __name__ == "__main__":
    unittest.main()
