"""CU-CTRL-6 tests: ControlSnapshot lifecycle projection。

核心原则：lifecycle 是 derived state——ControlBoundary 不拥有
execution lifecycle 存储、不成为第二个 Engine、不自行推进状态；
projection = execution truth（caller/composition 供应）+ control
truth（pending intent / revision queue / journal）→ 只读快照。

冻结词汇映射（CTRL-1 @ :94-96 恰七值，§2 不得新增）：
- 指令中的 "ABORTING" ≡ 冻结值 ABORT_PENDING
- 冻结词表无 IDLE（pre-execution 无输入可投影；初始可达态=RUNNING）

投影表（纯函数，零存储）：
  终态优先（§8）：COMPLETED/ABORTED 原样压倒一切 intent；
  FAILED+pending ABORT→ABORTED（§7 abort drain 失败语义），
  其余 FAILED 保持 FAILED
  PAUSED（须真实 park 确认参数）：pending ABORT→ABORT_PENDING；
  pending NONE（RESUME 已清）→RUNNING（§6）；否则 PAUSED
  PAUSE_PENDING：pending ABORT→ABORT_PENDING；pending NONE→
  RUNNING（§6 恢复非-paused）；否则 PAUSE_PENDING
  ABORT_PENDING→ABORT_PENDING；RUNNING：pending ABORT→
  ABORT_PENDING、pending PAUSE→PAUSE_PENDING、否则 RUNNING

PAUSED ≠ PAUSE_PENDING：前者只由真实 park 确认（DISPATCH/ADMISSION
参数）兑现，绝不由 PAUSE_REQUESTED 推导。snapshot 纯只读：不
mutate journal / queue / version / execution truth。
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
    ParkPoint,
    PendingIntentKind,
    RevisionPayload,
    RevisionTarget,
)
from control_journal import ControlFactType, ControlJournal  # noqa: E402

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


def revise_cmd(command_id="cmd-rev", execution_id="exec-1", version=0):
    return ControlCommand(
        command_id=command_id, execution_id=execution_id,
        command=ControlCommandType.REVISE,
        payload=RevisionPayload(target=RevisionTarget.NEXT_INVOCATION,
                                text="overlay text"),
        expected_version=version)


class VocabularyHonestyTests(unittest.TestCase):
    """§2 冻结词汇：恰七值、无 IDLE/ABORTING、不新增。"""

    def test_frozen_vocabulary_exactly_seven_members(self):
        self.assertEqual({item.name for item in ControlLifecycle},
                         {"RUNNING", "PAUSE_PENDING", "PAUSED",
                          "ABORT_PENDING", "ABORTED", "COMPLETED",
                          "FAILED"})
        # 指令词表中的 IDLE / ABORTING 不在冻结枚举（§2 不得新增）
        self.assertFalse(hasattr(ControlLifecycle, "IDLE"))
        self.assertFalse(hasattr(ControlLifecycle, "ABORTING"))

    def test_initial_projection_is_running(self):
        # 冻结词表无 IDLE：pre-execution 无输入可投影；初始可达投影
        # = RUNNING（不发明 IDLE，SINGLE 无 synthetic 事件）
        boundary, _ = make_boundary()
        snapshot = boundary.snapshot(ControlLifecycle.RUNNING)
        self.assertEqual(snapshot.lifecycle, ControlLifecycle.RUNNING)

    def test_running_without_intents_projects_running(self):
        boundary, _ = make_boundary()
        self.assertEqual(boundary.snapshot(
            ControlLifecycle.RUNNING).lifecycle,
            ControlLifecycle.RUNNING)

    def test_no_execution_event_types_introduced(self):
        # §12/§27：零 EventIndex/ExecutionEvent 耦合，词汇未动
        source = MODULE_PATH.read_text(encoding="utf-8")
        self.assertNotIn("execution_observation", source)
        self.assertNotIn("event_index", source)
        self.assertNotIn("ExecutionEvent", source)

    def test_snapshot_field_set_is_frozen(self):
        # §10/§25/§26：CurrentStage/CurrentInvocation 保持 "—"
        # （不存在该字段）；不新增 snapshot 字段
        boundary, _ = make_boundary()
        snapshot = boundary.snapshot(ControlLifecycle.RUNNING)
        self.assertEqual(
            {f for f in vars(snapshot)},
            {"execution_id", "execution_version", "lifecycle",
             "pending_intent", "park_point", "revision_queue"})
        self.assertFalse(hasattr(snapshot, "current_stage"))
        self.assertFalse(hasattr(snapshot, "current_invocation"))


class PauseProjectionTests(unittest.TestCase):
    """§5：accepted PAUSE → PAUSE_PENDING；PAUSED 只由真实确认兑现。"""

    def test_accepted_pause_projects_pause_pending(self):
        boundary, _ = make_boundary()
        boundary.submit(pause_cmd())
        snapshot = boundary.snapshot(ControlLifecycle.RUNNING)
        self.assertEqual(snapshot.lifecycle,
                         ControlLifecycle.PAUSE_PENDING)

    def test_pending_pause_is_not_paused(self):
        boundary, _ = make_boundary()
        boundary.submit(pause_cmd())
        snapshot = boundary.snapshot(ControlLifecycle.RUNNING)
        self.assertNotEqual(snapshot.lifecycle, ControlLifecycle.PAUSED)
        self.assertEqual(snapshot.park_point, ParkPoint.NONE)

    def test_dispatch_confirmation_projects_paused(self):
        boundary, _ = make_boundary()
        boundary.submit(pause_cmd())
        snapshot = boundary.snapshot(ControlLifecycle.PAUSED,
                                     park_point=ParkPoint.DISPATCH)
        self.assertEqual(snapshot.lifecycle, ControlLifecycle.PAUSED)
        self.assertEqual(snapshot.park_point, ParkPoint.DISPATCH)

    def test_admission_confirmation_projects_paused(self):
        boundary, _ = make_boundary()
        boundary.submit(pause_cmd())
        snapshot = boundary.snapshot(ControlLifecycle.PAUSED,
                                     park_point=ParkPoint.ADMISSION)
        self.assertEqual(snapshot.lifecycle, ControlLifecycle.PAUSED)
        self.assertEqual(snapshot.park_point, ParkPoint.ADMISSION)

    def test_paused_without_park_confirmation_refused(self):
        # §9/§24：无真实 park 确认不得 PAUSED（无 synthetic admission）
        boundary, _ = make_boundary()
        boundary.submit(pause_cmd())
        with self.assertRaises(ControlModelError):
            boundary.snapshot(ControlLifecycle.PAUSED)

    def test_park_point_with_non_paused_truth_refused(self):
        # park 证据只能伴随 PAUSED execution truth（不一致输入拒绝）
        boundary, _ = make_boundary()
        with self.assertRaises(ControlModelError):
            boundary.snapshot(ControlLifecycle.RUNNING,
                              park_point=ParkPoint.DISPATCH)

    def test_paused_resume_projects_running(self):
        boundary, _ = make_boundary()
        boundary.submit(pause_cmd())
        boundary.submit(resume_cmd())
        snapshot = boundary.snapshot(ControlLifecycle.PAUSED,
                                     park_point=ParkPoint.DISPATCH)
        self.assertEqual(snapshot.lifecycle, ControlLifecycle.RUNNING)
        self.assertEqual(snapshot.park_point, ParkPoint.NONE)

    def test_running_resume_projection_unchanged(self):
        boundary, _ = make_boundary()
        result = boundary.submit(resume_cmd())
        self.assertEqual(result.status.value, "NO_OP")
        self.assertEqual(boundary.snapshot(
            ControlLifecycle.RUNNING).lifecycle,
            ControlLifecycle.RUNNING)

    def test_pause_pending_resume_restores_running(self):
        boundary, _ = make_boundary()
        boundary.submit(pause_cmd())
        self.assertEqual(boundary.snapshot(
            ControlLifecycle.PAUSE_PENDING).lifecycle,
            ControlLifecycle.PAUSE_PENDING)
        boundary.submit(resume_cmd())
        self.assertEqual(boundary.snapshot(
            ControlLifecycle.PAUSE_PENDING).lifecycle,
            ControlLifecycle.RUNNING)


class AbortProjectionTests(unittest.TestCase):
    """§7：accepted ABORT → ABORT_PENDING（冻结词表 ABORTING 对应值）；
    ABORTED 只由真实 abort confirmation 兑现。"""

    def test_accepted_abort_projects_abort_pending(self):
        boundary, _ = make_boundary()
        boundary.submit(abort_cmd())
        snapshot = boundary.snapshot(ControlLifecycle.RUNNING)
        self.assertEqual(snapshot.lifecycle,
                         ControlLifecycle.ABORT_PENDING)

    def test_aborting_is_not_aborted(self):
        boundary, _ = make_boundary()
        boundary.submit(abort_cmd())
        snapshot = boundary.snapshot(ControlLifecycle.RUNNING)
        self.assertNotEqual(snapshot.lifecycle, ControlLifecycle.ABORTED)
        self.assertEqual(boundary.snapshot(
            ControlLifecycle.ABORT_PENDING).lifecycle,
            ControlLifecycle.ABORT_PENDING)

    def test_real_abort_confirmation_projects_aborted(self):
        boundary, _ = make_boundary()
        boundary.submit(abort_cmd())
        snapshot = boundary.snapshot(ControlLifecycle.ABORTED)
        self.assertEqual(snapshot.lifecycle, ControlLifecycle.ABORTED)

    def test_terminal_success_supersedes_abort_completed(self):
        boundary, journal = make_boundary()
        boundary.submit(abort_cmd())
        snapshot = boundary.snapshot(ControlLifecycle.COMPLETED)
        self.assertEqual(snapshot.lifecycle, ControlLifecycle.COMPLETED)
        # ABORT_SUPERSEDED 事实归 composition 层（CompositionWriter）；
        # 投影绝不 mutate journal
        for fact in journal.snapshot():
            self.assertIsNot(fact.fact_type,
                             ControlFactType.ABORT_SUPERSEDED)

    def test_terminal_failure_with_active_abort_aborted(self):
        # §7：abort drain 期间真实失败 → ABORTED（Section 1 语义）
        boundary, _ = make_boundary()
        boundary.submit(abort_cmd())
        snapshot = boundary.snapshot(ControlLifecycle.FAILED)
        self.assertEqual(snapshot.lifecycle, ControlLifecycle.ABORTED)

    def test_failure_without_abort_remains_failed(self):
        boundary, _ = make_boundary()
        snapshot = boundary.snapshot(ControlLifecycle.FAILED)
        self.assertEqual(snapshot.lifecycle, ControlLifecycle.FAILED)


class TerminalPrecedenceTests(unittest.TestCase):
    """§8：自然终态压倒 control intent；stale intent 不遮终态。"""

    def test_normal_terminal_supersedes_pause(self):
        boundary, _ = make_boundary()
        boundary.submit(pause_cmd())
        snapshot = boundary.snapshot(ControlLifecycle.COMPLETED)
        self.assertEqual(snapshot.lifecycle, ControlLifecycle.COMPLETED)
        self.assertNotEqual(snapshot.lifecycle,
                            ControlLifecycle.PAUSE_PENDING)

    def test_stale_pause_cannot_override_terminal(self):
        boundary, _ = make_boundary()
        boundary.submit(pause_cmd())
        for _ in range(3):
            self.assertEqual(boundary.snapshot(
                ControlLifecycle.COMPLETED).lifecycle,
                ControlLifecycle.COMPLETED)
        # pending intent 照实可见，但绝不改写终态投影
        self.assertEqual(boundary.pending_intent.kind,
                         PendingIntentKind.PAUSE)

    def test_pending_control_plus_terminal_race_projection(self):
        # pause 与 abort 均已 accepted；随后真实终态到达：
        # COMPLETED → COMPLETED；FAILED → ABORTED
        boundary, _ = make_boundary()
        boundary.submit(pause_cmd())
        boundary.submit(abort_cmd())
        self.assertEqual(boundary.snapshot(
            ControlLifecycle.COMPLETED).lifecycle,
            ControlLifecycle.COMPLETED)
        boundary_b, _ = make_boundary()
        boundary_b.submit(pause_cmd())
        boundary_b.submit(abort_cmd())
        self.assertEqual(boundary_b.snapshot(
            ControlLifecycle.FAILED).lifecycle,
            ControlLifecycle.ABORTED)

    def test_abort_vs_terminal_precedence_matrix(self):
        boundary, _ = make_boundary()
        boundary.submit(abort_cmd())
        self.assertEqual(boundary.snapshot(
            ControlLifecycle.COMPLETED).lifecycle,
            ControlLifecycle.COMPLETED)
        self.assertEqual(boundary.snapshot(
            ControlLifecycle.FAILED).lifecycle,
            ControlLifecycle.ABORTED)
        self.assertEqual(boundary.snapshot(
            ControlLifecycle.ABORTED).lifecycle,
            ControlLifecycle.ABORTED)
        boundary_b, _ = make_boundary()
        boundary_b.submit(pause_cmd())
        self.assertEqual(boundary_b.snapshot(
            ControlLifecycle.ABORTED).lifecycle,
            ControlLifecycle.ABORTED)
        self.assertEqual(boundary_b.snapshot(
            ControlLifecycle.COMPLETED).lifecycle,
            ControlLifecycle.COMPLETED)


class ImmutabilityTests(unittest.TestCase):
    """§11：projection 纯只读；§3：零 lifecycle 存储。"""

    def test_revision_accepted_does_not_change_lifecycle(self):
        boundary, _ = make_boundary()
        boundary.submit(revise_cmd())
        self.assertEqual(boundary.snapshot(
            ControlLifecycle.RUNNING).lifecycle,
            ControlLifecycle.RUNNING)

    def test_replay_does_not_change_lifecycle(self):
        boundary, _ = make_boundary()
        boundary.submit(pause_cmd())
        first = boundary.snapshot(ControlLifecycle.RUNNING)
        boundary.submit(pause_cmd())  # exact replay
        second = boundary.snapshot(ControlLifecycle.RUNNING)
        self.assertEqual(first, second)

    def test_conflict_does_not_change_lifecycle(self):
        boundary, _ = make_boundary()
        boundary.submit(revise_cmd())
        first = boundary.snapshot(ControlLifecycle.RUNNING)
        boundary.submit(revise_cmd(version=1))  # conflict
        second = boundary.snapshot(ControlLifecycle.RUNNING)
        self.assertEqual(first, second)

    def test_projection_does_not_mutate_execution_truth(self):
        boundary, _ = make_boundary()
        truth = ControlLifecycle.RUNNING
        boundary.submit(pause_cmd())
        boundary.snapshot(truth)
        self.assertIs(truth, ControlLifecycle.RUNNING)
        self.assertEqual(boundary.snapshot(truth).lifecycle,
                         ControlLifecycle.PAUSE_PENDING)

    def test_projection_does_not_mutate_journal(self):
        boundary, journal = make_boundary()
        boundary.submit(pause_cmd())
        facts_before = journal.snapshot()
        boundary.snapshot(ControlLifecycle.RUNNING)
        boundary.snapshot(ControlLifecycle.COMPLETED)
        boundary.snapshot(ControlLifecycle.PAUSED,
                          park_point=ParkPoint.DISPATCH)
        self.assertEqual(journal.snapshot(), facts_before)

    def test_snapshot_is_read_only(self):
        boundary, _ = make_boundary()
        boundary.submit(pause_cmd())
        first = boundary.snapshot(ControlLifecycle.RUNNING)
        second = boundary.snapshot(ControlLifecycle.RUNNING)
        self.assertEqual(first, second)
        self.assertEqual(boundary.execution_version, 0)
        self.assertEqual(boundary.pending_intent.kind,
                         PendingIntentKind.PAUSE)
        with self.assertRaises(AttributeError):
            first.lifecycle = ControlLifecycle.COMPLETED

    def test_no_lifecycle_storage_on_boundary(self):
        # §3：ControlBoundary 不持有 lifecycle 存储（纯每调用投影）
        boundary, _ = make_boundary()
        for name in ("_lifecycle", "_state", "lifecycle"):
            self.assertFalse(hasattr(boundary, name), name)

    def test_concurrent_snapshot_reads(self):
        boundary, _ = make_boundary()
        boundary.submit(pause_cmd())
        truths = [ControlLifecycle.RUNNING, ControlLifecycle.PAUSE_PENDING,
                  ControlLifecycle.COMPLETED, ControlLifecycle.ABORTED,
                  ControlLifecycle.FAILED, ControlLifecycle.ABORT_PENDING]
        barrier = threading.Barrier(8)
        results = {}
        errors = []

        def worker(key, truth):
            try:
                barrier.wait()
                results[key] = boundary.snapshot(truth).lifecycle
            except Exception as error:  # pragma: no cover - 防御记录
                errors.append(error)

        threads = [threading.Thread(target=worker, args=(i, truths[i % 6]))
                   for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 8)
        for projected in results.values():
            self.assertIn(projected, set(ControlLifecycle))
        # 快照风暴后状态零漂移
        self.assertEqual(boundary.pending_intent.kind,
                         PendingIntentKind.PAUSE)
        self.assertEqual(boundary.snapshot(
            ControlLifecycle.RUNNING).lifecycle,
            ControlLifecycle.PAUSE_PENDING)


if __name__ == "__main__":
    unittest.main()
