"""CU-CTRL-4 tests: ControlBoundary command replay / idempotency。

replay identity = (execution_id, command_id)：
- exact replay：纯查找返回首次 ControlResult（零事实 / 零 pending
  变化 / 零版本变化 / 不重新裁决——即使 pending 已前进）
- 同 id 不同 fingerprint：REJECTED/COMMAND_ID_CONFLICT（零副作用、
  不损坏原 replay）
- 不同 id 相同语义：走 CTRL-3 裁决（NO_OP/ALREADY_REQUESTED），
  不是 replay，不新增事实
- fingerprint：覆盖全部语义字段的不可变规格元组（command 类型、
  execution_id、command_id、revision 载荷、expected_version）——
  不用 hash()、不落 JSON
- per-instance 缓存（跨 boundary 不共享）；并发同一新命令恰走一次
  accept 路径；replay 与 conflict 均零 epoch 递增
"""
import dataclasses
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
    ControlModelError,
    ControlReason,
    ControlResult,
    ControlStatus,
    PendingIntentKind,
    RevisionPayload,
    RevisionTarget,
)
from control_journal import ControlJournal  # noqa: E402

MODULE_PATH = SCRIPTS / "control_boundary.py"


def make_boundary(execution_id="exec-1", initial_version=0):
    journal = ControlJournal()
    return ControlBoundary(
        journal=journal, execution_id=execution_id,
        initial_version=initial_version), journal


def pause_cmd(command_id="cmd-pause", execution_id="exec-1"):
    return ControlCommand(command_id=command_id, execution_id=execution_id,
                          command=ControlCommandType.PAUSE)


def resume_cmd(command_id="cmd-resume", execution_id="exec-1"):
    return ControlCommand(command_id=command_id, execution_id=execution_id,
                          command=ControlCommandType.RESUME)


def abort_cmd(command_id="cmd-abort", execution_id="exec-1"):
    return ControlCommand(command_id=command_id, execution_id=execution_id,
                          command=ControlCommandType.ABORT)


def revise_cmd(command_id="cmd-rev", execution_id="exec-1", version=0,
               target=RevisionTarget.NEXT_INVOCATION, text="overlay text",
               task=None, prompt=None):
    return ControlCommand(
        command_id=command_id, execution_id=execution_id,
        command=ControlCommandType.REVISE,
        payload=RevisionPayload(target=target, text=text, task=task,
                                prompt=prompt),
        expected_version=version)


class ExactReplayTests(unittest.TestCase):
    """同一 (execution_id, command_id) 的完全重放：纯查找、零副作用。"""

    def test_exact_replay_returns_first_result(self):
        boundary, _ = make_boundary()
        command = pause_cmd(command_id="p1")
        first = boundary.submit(command)
        self.assertEqual(first.status, ControlStatus.ACCEPTED)
        replayed = boundary.submit(command)
        self.assertEqual(replayed.status, ControlStatus.ACCEPTED)
        self.assertEqual(replayed, first)

    def test_exact_replay_returns_stored_result_object(self):
        boundary, _ = make_boundary()
        command = pause_cmd(command_id="p1")
        first = boundary.submit(command)
        replayed = boundary.submit(command)
        self.assertIs(replayed, first)

    def test_exact_replay_adds_no_journal_fact(self):
        boundary, journal = make_boundary()
        boundary.submit(pause_cmd(command_id="p1"))
        facts_after_first = len(journal.snapshot())
        boundary.submit(pause_cmd(command_id="p1"))
        self.assertEqual(len(journal.snapshot()), facts_after_first)
        self.assertEqual(facts_after_first, 1)

    def test_exact_replay_zero_epoch_increment(self):
        boundary, _ = make_boundary(initial_version=7)
        first = boundary.submit(pause_cmd(command_id="p1"))
        replayed = boundary.submit(pause_cmd(command_id="p1"))
        self.assertEqual(boundary.execution_version, 7)
        self.assertEqual(replayed.execution_version, first.execution_version)

    def test_exact_replay_leaves_pending_unchanged(self):
        boundary, _ = make_boundary()
        boundary.submit(pause_cmd(command_id="p1"))
        self.assertEqual(boundary.pending_intent.kind, PendingIntentKind.PAUSE)
        boundary.submit(pause_cmd(command_id="p1"))
        self.assertEqual(boundary.pending_intent.kind, PendingIntentKind.PAUSE)

    def test_replay_after_pending_change_returns_original_result(self):
        # pending 已前进到 ABORT：重放 pause 仍返回首次 ACCEPTED，
        # 而非按当前状态重新裁决为 REJECTED/ALREADY_ABORTING
        boundary, journal = make_boundary()
        pause = boundary.submit(pause_cmd(command_id="p1"))
        self.assertEqual(pause.status, ControlStatus.ACCEPTED)
        boundary.submit(abort_cmd(command_id="a1"))
        facts_before = len(journal.snapshot())
        replayed = boundary.submit(pause_cmd(command_id="p1"))
        self.assertEqual(replayed, pause)
        self.assertIs(replayed, pause)
        self.assertEqual(len(journal.snapshot()), facts_before)
        self.assertEqual(boundary.pending_intent.kind, PendingIntentKind.ABORT)

    def test_replay_of_no_op_result_after_state_change(self):
        # 幂等本质：pause-2 首次 NO_OP；pending 恢复 NONE 后重放
        # 仍返回原 NO_OP，而不是重新裁决成一次新的 ACCEPTED
        boundary, journal = make_boundary()
        boundary.submit(pause_cmd(command_id="p1"))
        no_op = boundary.submit(pause_cmd(command_id="p2"))
        self.assertEqual(no_op.status, ControlStatus.NO_OP)
        self.assertEqual(no_op.reason, ControlReason.ALREADY_REQUESTED)
        boundary.submit(resume_cmd(command_id="r1"))
        self.assertEqual(boundary.pending_intent.kind, PendingIntentKind.NONE)
        facts_before = len(journal.snapshot())
        replayed = boundary.submit(pause_cmd(command_id="p2"))
        self.assertEqual(replayed, no_op)
        self.assertEqual(len(journal.snapshot()), facts_before)

    def test_replay_of_rejected_result(self):
        boundary, journal = make_boundary()
        boundary.submit(abort_cmd(command_id="a1"))
        rejected = boundary.submit(pause_cmd(command_id="p1"))
        self.assertEqual(rejected.status, ControlStatus.REJECTED)
        self.assertEqual(rejected.reason, ControlReason.ALREADY_ABORTING)
        facts_before = len(journal.snapshot())
        replayed = boundary.submit(pause_cmd(command_id="p1"))
        self.assertEqual(replayed, rejected)
        self.assertEqual(len(journal.snapshot()), facts_before)


class CommandIdConflictTests(unittest.TestCase):
    """同 command_id + 不同 fingerprint：REJECTED/COMMAND_ID_CONFLICT，
    journal / pending / version 全零变化，且不损坏原 replay。"""

    def test_same_id_different_revision_target_conflict(self):
        boundary, _ = make_boundary()
        first = boundary.submit(revise_cmd(command_id="x1"))
        self.assertEqual(first.status, ControlStatus.ACCEPTED)
        conflict = boundary.submit(revise_cmd(
            command_id="x1", target=RevisionTarget.SUBMISSION, text=None,
            task="new task"))
        self.assertEqual(conflict.status, ControlStatus.REJECTED)
        self.assertEqual(conflict.reason, ControlReason.COMMAND_ID_CONFLICT)

    def test_same_id_different_command_type_conflict(self):
        boundary, _ = make_boundary()
        boundary.submit(pause_cmd(command_id="c1"))
        conflict = boundary.submit(abort_cmd(command_id="c1"))
        self.assertEqual(conflict.status, ControlStatus.REJECTED)
        self.assertEqual(conflict.reason, ControlReason.COMMAND_ID_CONFLICT)

    def test_same_id_different_expected_version_conflict(self):
        boundary, _ = make_boundary()
        boundary.submit(revise_cmd(command_id="x1", version=0))
        conflict = boundary.submit(revise_cmd(command_id="x1", version=1))
        self.assertEqual(conflict.status, ControlStatus.REJECTED)
        self.assertEqual(conflict.reason, ControlReason.COMMAND_ID_CONFLICT)

    def test_same_id_different_revision_text_conflict(self):
        boundary, _ = make_boundary()
        boundary.submit(revise_cmd(command_id="x1", text="overlay-a"))
        conflict = boundary.submit(revise_cmd(command_id="x1", text="overlay-b"))
        self.assertEqual(conflict.status, ControlStatus.REJECTED)
        self.assertEqual(conflict.reason, ControlReason.COMMAND_ID_CONFLICT)

    def test_same_id_different_submission_field_conflict(self):
        boundary, _ = make_boundary()
        boundary.submit(revise_cmd(
            command_id="x1", target=RevisionTarget.SUBMISSION, text=None,
            task="task-a"))
        conflict = boundary.submit(revise_cmd(
            command_id="x1", target=RevisionTarget.SUBMISSION, text=None,
            task="task-b"))
        self.assertEqual(conflict.status, ControlStatus.REJECTED)
        self.assertEqual(conflict.reason, ControlReason.COMMAND_ID_CONFLICT)

    def test_conflict_adds_no_journal_fact(self):
        boundary, journal = make_boundary()
        boundary.submit(pause_cmd(command_id="c1"))
        facts_before = len(journal.snapshot())
        boundary.submit(abort_cmd(command_id="c1"))
        self.assertEqual(len(journal.snapshot()), facts_before)

    def test_conflict_leaves_pending_unchanged(self):
        # 同 id 的 ABORT 形状冲突：pending 不得被冲突命令翻转
        boundary, _ = make_boundary()
        boundary.submit(pause_cmd(command_id="c1"))
        boundary.submit(abort_cmd(command_id="c1"))
        self.assertEqual(boundary.pending_intent.kind, PendingIntentKind.PAUSE)

    def test_conflict_leaves_version_unchanged(self):
        boundary, _ = make_boundary(initial_version=5)
        boundary.submit(revise_cmd(command_id="x1", version=5))
        boundary.submit(revise_cmd(command_id="x1", version=6))
        self.assertEqual(boundary.execution_version, 5)

    def test_conflict_does_not_damage_original_replay(self):
        boundary, _ = make_boundary()
        original_cmd = revise_cmd(command_id="x1", version=0)
        original = boundary.submit(original_cmd)
        boundary.submit(revise_cmd(command_id="x1", version=1))
        replayed = boundary.submit(original_cmd)
        self.assertEqual(replayed, original)
        self.assertIs(replayed, original)


class IdentityScopeTests(unittest.TestCase):
    """identity 边界：不同 id 非重放、execution_id 隔离、外域拒绝。"""

    def test_different_command_id_same_intent_is_not_replay(self):
        # pause-002 是新 identity：走 CTRL-3 语义裁决 NO_OP，而非
        # pause-001 的重放；且 NO_OP 零事实
        boundary, journal = make_boundary()
        first = boundary.submit(pause_cmd(command_id="pause-001"))
        self.assertEqual(first.status, ControlStatus.ACCEPTED)
        second = boundary.submit(pause_cmd(command_id="pause-002"))
        self.assertEqual(second.status, ControlStatus.NO_OP)
        self.assertEqual(second.reason, ControlReason.ALREADY_REQUESTED)
        self.assertEqual(len(journal.snapshot()), 1)

    def test_semantic_duplicate_adjudicated_fresh_not_aliased(self):
        # 语义重复不得冒充对方结果：pause-002 的 NO_OP 是自己的裁决，
        # 不是 pause-001 ACCEPTED 的重放别名
        boundary, _ = make_boundary()
        first = boundary.submit(pause_cmd(command_id="pause-001"))
        second = boundary.submit(pause_cmd(command_id="pause-002"))
        self.assertNotEqual(second, first)
        self.assertNotEqual(second.status, first.status)
        replayed = boundary.submit(pause_cmd(command_id="pause-001"))
        self.assertEqual(replayed.command_id, "pause-001")
        self.assertEqual(replayed, first)

    def test_foreign_execution_id_rejected_without_polluting_cache(self):
        boundary, journal = make_boundary("exec-1")
        foreign = pause_cmd(command_id="cmd-pause", execution_id="exec-other")
        rejected = boundary.submit(foreign)
        self.assertEqual(rejected.status, ControlStatus.REJECTED)
        self.assertEqual(rejected.reason, ControlReason.INVALID_TARGET)
        self.assertEqual(len(journal.snapshot()), 0)
        # 外域命令不占用本域 command_id 缓存：本地同名命令正常受理
        local = boundary.submit(pause_cmd(command_id="cmd-pause"))
        self.assertEqual(local.status, ControlStatus.ACCEPTED)

    def test_cross_boundary_same_command_id_no_shared_cache(self):
        boundary_a, journal_a = make_boundary("exec-1")
        boundary_b, journal_b = make_boundary("exec-2")
        result_a = boundary_a.submit(pause_cmd(command_id="c1",
                                               execution_id="exec-1"))
        result_b = boundary_b.submit(pause_cmd(command_id="c1",
                                               execution_id="exec-2"))
        self.assertEqual(result_a.status, ControlStatus.ACCEPTED)
        self.assertEqual(result_b.status, ControlStatus.ACCEPTED)
        self.assertEqual(result_b.execution_id, "exec-2")
        self.assertIsNot(result_b, result_a)
        self.assertEqual(len(journal_a.snapshot()), 1)
        self.assertEqual(len(journal_b.snapshot()), 1)

    def test_shared_journal_executions_isolated(self):
        # 同一账本、两个 execution 的 boundary：同 command_id 各自受理，
        # 事实按 execution_id 分账，重放互不串线
        journal = ControlJournal()
        boundary_a = ControlBoundary(journal=journal, execution_id="exec-1")
        boundary_b = ControlBoundary(journal=journal, execution_id="exec-2")
        boundary_a.submit(pause_cmd(command_id="c1", execution_id="exec-1"))
        boundary_b.submit(pause_cmd(command_id="c1", execution_id="exec-2"))
        self.assertEqual(len(journal.snapshot()), 2)
        facts = journal.snapshot()
        self.assertEqual({fact.execution_id for fact in facts},
                         {"exec-1", "exec-2"})
        boundary_a.submit(pause_cmd(command_id="c1", execution_id="exec-1"))
        boundary_b.submit(pause_cmd(command_id="c1", execution_id="exec-2"))
        self.assertEqual(len(journal.snapshot()), 2)


class FingerprintContractTests(unittest.TestCase):
    """fingerprint 形状契约：不可变规格元组，无 hash / 无 JSON。"""

    def test_source_has_no_hash_or_json(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        self.assertNotIn("hashlib", source)
        self.assertNotIn("import json", source)
        self.assertNotIn("hash(", source)

    def test_replay_store_is_instance_scoped_not_global(self):
        # 两个 boundary 实例各自受理同一 command_id：行为级证明缓存
        # 不共享（无全局/模块级 replay 字典）
        boundary_a, journal_a = make_boundary("exec-1")
        boundary_b, journal_b = make_boundary("exec-2")
        boundary_a.submit(pause_cmd(command_id="c1", execution_id="exec-1"))
        boundary_a.submit(pause_cmd(command_id="c1", execution_id="exec-1"))
        boundary_b.submit(pause_cmd(command_id="c1", execution_id="exec-2"))
        self.assertEqual(len(journal_a.snapshot()), 1)
        self.assertEqual(len(journal_b.snapshot()), 1)


class ResultImmutabilityTests(unittest.TestCase):
    """replay 结果不可变：调用方无法借重放通道改写历史。"""

    def test_replay_result_rejects_mutation(self):
        boundary, _ = make_boundary()
        boundary.submit(pause_cmd(command_id="p1"))
        replayed = boundary.submit(pause_cmd(command_id="p1"))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            replayed.status = ControlStatus.REJECTED

    def test_replay_result_value_semantics_stable(self):
        # frozen 值语义：相等且可哈希，重复重放不产生新值
        boundary, _ = make_boundary()
        first = boundary.submit(pause_cmd(command_id="p1"))
        second = boundary.submit(pause_cmd(command_id="p1"))
        third = boundary.submit(pause_cmd(command_id="p1"))
        self.assertEqual(len({first, second, third}), 1)
        self.assertEqual(hash(first), hash(second))


class ConcurrencyTests(unittest.TestCase):
    """并发幂等：同一新命令的两线程恰走一次 accept 路径。"""

    def test_concurrent_identical_first_submit_single_accept_path(self):
        boundary, journal = make_boundary()
        command = pause_cmd(command_id="cmd-race")
        barrier = threading.Barrier(2)
        results = {}

        def worker(key):
            barrier.wait()
            results[key] = boundary.submit(command)

        threads = [threading.Thread(target=worker, args=(i,))
                   for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        first, second = results[0], results[1]
        self.assertEqual(first.status, ControlStatus.ACCEPTED)
        self.assertEqual(second.status, ControlStatus.ACCEPTED)
        self.assertEqual(first, second)
        self.assertIs(first, second)  # 输者拿到存储的同一结果对象
        self.assertEqual(len(journal.snapshot()), 1)

    def test_concurrent_replay_after_first_submit_no_fact(self):
        boundary, journal = make_boundary()
        boundary.submit(pause_cmd(command_id="cmd-race"))
        barrier = threading.Barrier(8)
        results = {}

        def worker(key):
            barrier.wait()
            results[key] = boundary.submit(pause_cmd(command_id="cmd-race"))

        threads = [threading.Thread(target=worker, args=(i,))
                   for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual({result.status for result in results.values()},
                         {ControlStatus.ACCEPTED})
        self.assertEqual(len({id(result) for result in results.values()}), 1)
        self.assertEqual(len(journal.snapshot()), 1)

    def test_concurrent_conflicting_payloads_one_owner(self):
        # 同 command_id、两种 fingerprint 各 6 线程同时首发：恰一个
        # fingerprint 走 accept 路径（1 事实），另一个全数 CONFLICT
        boundary, journal = make_boundary()
        cmd_a = revise_cmd(command_id="cmd-x", text="overlay-a")
        cmd_b = revise_cmd(command_id="cmd-x", text="overlay-b")
        barrier = threading.Barrier(12)
        results = {}

        def worker(key, command):
            barrier.wait()
            results[key] = boundary.submit(command)

        threads = [threading.Thread(target=worker, args=(i, cmd_a))
                   for i in range(6)]
        threads += [threading.Thread(target=worker, args=(i + 6, cmd_b))
                    for i in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        group_a = [results[i] for i in range(6)]
        group_b = [results[i + 6] for i in range(6)]
        statuses = {result.status for result in results.values()}
        self.assertEqual(statuses,
                         {ControlStatus.ACCEPTED, ControlStatus.REJECTED})
        winners, losers = ((group_a, group_b)
                           if group_a[0].status is ControlStatus.ACCEPTED
                           else (group_b, group_a))
        self.assertEqual(len({id(result) for result in winners}), 1)
        for result in losers:
            self.assertEqual(result.reason, ControlReason.COMMAND_ID_CONFLICT)
        self.assertEqual(len(journal.snapshot()), 1)

    def test_concurrent_distinct_commands_all_adjudicated(self):
        # distinct REVISE：pending 不因 REVISE 前进，六条各走各的裁决，
        # 互不被 replay 缓存串扰（PAUSE 会有语义重复 NO_OP，不选）
        boundary, journal = make_boundary()
        barrier = threading.Barrier(6)
        results = {}

        def worker(index):
            barrier.wait()
            results[index] = boundary.submit(
                revise_cmd(command_id=f"x-{index}"))

        threads = [threading.Thread(target=worker, args=(i,))
                   for i in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual({result.status for result in results.values()},
                         {ControlStatus.ACCEPTED})
        self.assertEqual(len(journal.snapshot()), 6)


if __name__ == "__main__":
    unittest.main()
