"""CU-CTRL-5 tests: ControlBoundary revision queue semantics。

CTRL-1 冻结值模型（沿用，零改动）：
- PendingRevision 仅承载 NEXT_INVOCATION（SUBMISSION 是 set semantics
  修改 draft，从不入队——构造期结构性拒绝）
- ControlSnapshot.revision_queue 即 pending 投影字段

本 CU 语义：
- REVISE accepted → REVISE_REQUESTED 事实（text 永不入 journal payload）
  + NEXT_INVOCATION 进入 FIFO pending 队列（不覆盖/不重排/不合并）
- revision_id ≡ command_id（构造不变量：journal payload 与队列条目
  的 revision_id 均取自 command_id）
- CTRL-4 replay 完整继承：exact replay 零新事实/零入队/零版本变化；
  同 id 异 payload → COMMAND_ID_CONFLICT、队列不变
- 原子性：journal append 失败 → 零队列变化、零 replay entry、不声称
  accepted；失败不留痕迹（同 id 重试走完整首发路径）
- 消费/APPLIED 属 REV-1/REV-2：本 CU 绝不产生 REVISION_APPLIED，
  queue 只表示 CURRENTLY PENDING
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
    ControlReason,
    ControlResult,
    ControlStatus,
    PendingRevision,
    RevisionPayload,
    RevisionTarget,
)
from control_journal import (  # noqa: E402
    ControlFactType,
    ControlJournal,
    ControlJournalError,
)

MODULE_PATH = SCRIPTS / "control_boundary.py"


def make_boundary(execution_id="exec-1", initial_version=0):
    journal = ControlJournal()
    return ControlBoundary(
        journal=journal, execution_id=execution_id,
        initial_version=initial_version), journal


def pause_cmd(command_id="cmd-pause", execution_id="exec-1"):
    return ControlCommand(command_id=command_id, execution_id=execution_id,
                          command=ControlCommandType.PAUSE)


def next_rev(command_id, execution_id="exec-1", version=0, text=None):
    payload = RevisionPayload(target=RevisionTarget.NEXT_INVOCATION,
                              text=text if text is not None
                              else f"overlay-{command_id}")
    return ControlCommand(command_id=command_id, execution_id=execution_id,
                          command=ControlCommandType.REVISE,
                          payload=payload, expected_version=version)


def submission_rev(command_id, execution_id="exec-1", version=0, task=None):
    payload = RevisionPayload(
        target=RevisionTarget.SUBMISSION,
        task=task if task is not None else f"task-{command_id}")
    return ControlCommand(command_id=command_id, execution_id=execution_id,
                          command=ControlCommandType.REVISE,
                          payload=payload, expected_version=version)


class _FailingJournal(ControlJournal):
    """append 可控失败的账本：复刻 CTRL-3 原子性测试装置。"""

    def __init__(self):
        super().__init__()
        self.fail = False

    def boundary_writer(self):
        journal = self

        class _Writer:
            allowed_facts = frozenset(
                fact for fact in ControlFactType
                if fact is not ControlFactType.REVISION_APPLIED)

            def append(self, **kwargs):
                if journal.fail:
                    raise ControlJournalError("injected journal failure")
                journal._append(**kwargs)

        return _Writer()


class RevisionAcceptanceTests(unittest.TestCase):
    """§4 accepted 条件 + §3 双 target 语义（沿用 CTRL-1 值模型）。"""

    def test_submission_revision_accepted(self):
        boundary, journal = make_boundary()
        result = boundary.submit(submission_rev("sub-1"))
        self.assertEqual(result.status, ControlStatus.ACCEPTED)
        facts = journal.snapshot()
        self.assertEqual(len(facts), 1)
        self.assertIs(facts[0].fact_type, ControlFactType.REVISE_REQUESTED)
        self.assertEqual(facts[0].payload["target"],
                         RevisionTarget.SUBMISSION.value)
        # CTRL-1 冻结裁决：SUBMISSION 是 set semantics，从不入队
        self.assertEqual(boundary.snapshot(
            ControlLifecycle.RUNNING).revision_queue, ())

    def test_next_invocation_revision_accepted(self):
        boundary, journal = make_boundary()
        result = boundary.submit(next_rev("rev-1"))
        self.assertEqual(result.status, ControlStatus.ACCEPTED)
        self.assertEqual(len(journal.snapshot()), 1)
        queue = boundary.snapshot(ControlLifecycle.RUNNING).revision_queue
        self.assertEqual(len(queue), 1)

    def test_queue_entry_shape(self):
        boundary, _ = make_boundary()
        boundary.submit(next_rev("rev-1", text="overlay text"))
        entry = boundary.snapshot(ControlLifecycle.RUNNING).revision_queue[0]
        self.assertIsInstance(entry, PendingRevision)
        self.assertEqual(entry.revision_id, "rev-1")
        self.assertEqual(entry.text, "overlay text")
        self.assertIs(entry.target, RevisionTarget.NEXT_INVOCATION)

    def test_revision_id_equals_command_id_invariant(self):
        # §4.5 构造不变量：journal payload 与队列条目的 revision_id
        # 均取自 command_id（RevisionPayload 无 caller revision_id 字段）
        boundary, journal = make_boundary()
        boundary.submit(next_rev("rev-9"))
        fact = journal.snapshot()[0]
        self.assertEqual(fact.payload["revision_id"], "rev-9")
        entry = boundary.snapshot(ControlLifecycle.RUNNING).revision_queue[0]
        self.assertEqual(entry.revision_id, "rev-9")


class FifoQueueTests(unittest.TestCase):
    """§6 FIFO：append-only、不覆盖、不重排、不合并。"""

    def test_fifo_order_preserved(self):
        boundary, _ = make_boundary()
        for index in range(1, 4):
            boundary.submit(next_rev(f"rev-{index}"))
        queue = boundary.snapshot(ControlLifecycle.RUNNING).revision_queue
        self.assertEqual([entry.revision_id for entry in queue],
                         ["rev-1", "rev-2", "rev-3"])

    def test_multiple_revisions_retained(self):
        boundary, _ = make_boundary()
        for index in range(5):
            boundary.submit(next_rev(f"rev-{index}"))
        queue = boundary.snapshot(ControlLifecycle.RUNNING).revision_queue
        self.assertEqual(len(queue), 5)

    def test_same_text_different_ids_not_merged(self):
        # 不同 command_id 即使 text 相同也不是 replay、不合并
        boundary, journal = make_boundary()
        boundary.submit(next_rev("rev-a", text="same text"))
        boundary.submit(next_rev("rev-b", text="same text"))
        queue = boundary.snapshot(ControlLifecycle.RUNNING).revision_queue
        self.assertEqual(len(queue), 2)
        self.assertEqual(len(journal.snapshot()), 2)

    def test_empty_queue_snapshot(self):
        boundary, _ = make_boundary()
        self.assertEqual(boundary.snapshot(
            ControlLifecycle.RUNNING).revision_queue, ())

    def test_queue_is_intent_state_not_lifecycle_gated(self):
        # §5：停驻门是消费门（REV-1/2）；队列是当前 intent 状态，
        # 在未停驻的投影下同样可见 CURRENTLY PENDING
        boundary, _ = make_boundary()
        boundary.submit(next_rev("rev-1"))
        for lifecycle in (ControlLifecycle.RUNNING,
                          ControlLifecycle.PAUSE_PENDING):
            queue = boundary.snapshot(lifecycle).revision_queue
            self.assertEqual([entry.revision_id for entry in queue],
                             ["rev-1"])


class ValidationTests(unittest.TestCase):
    """§4 校验：沿用 CTRL-1 值模型，零新版本机。"""

    def test_invalid_target_rejected(self):
        with self.assertRaises(ControlModelError):
            ControlCommand(command_id="rev-bad", execution_id="exec-1",
                           command=ControlCommandType.REVISE,
                           payload=RevisionPayload(target="DISPATCH"),
                           expected_version=0)

    def test_invalid_payload_rejected(self):
        # NEXT_INVOCATION 只带 text；SUBMISSION 必须 task/prompt 其一
        with self.assertRaises(ControlModelError):
            RevisionPayload(target=RevisionTarget.NEXT_INVOCATION,
                            text="t", task="x")
        with self.assertRaises(ControlModelError):
            RevisionPayload(target=RevisionTarget.SUBMISSION)

    def test_execution_id_mismatch_rejected(self):
        boundary, journal = make_boundary("exec-1")
        result = boundary.submit(next_rev("rev-1", execution_id="exec-other"))
        self.assertEqual(result.status, ControlStatus.REJECTED)
        self.assertEqual(result.reason, ControlReason.INVALID_TARGET)
        self.assertEqual(len(journal.snapshot()), 0)
        self.assertEqual(boundary.snapshot(
            ControlLifecycle.RUNNING).revision_queue, ())

    def test_expected_version_required_by_value_model(self):
        # §4.8：沿用值模型规则（REVISE 必带非负 expected_version），
        # 不新增 version machine
        with self.assertRaises(ControlModelError):
            ControlCommand(command_id="rev-1", execution_id="exec-1",
                           command=ControlCommandType.REVISE,
                           payload=RevisionPayload(
                               target=RevisionTarget.NEXT_INVOCATION,
                               text="t"))
        with self.assertRaises(ControlModelError):
            next_rev("rev-1", version=-1)


class ReplayIntegrationTests(unittest.TestCase):
    """§7 CTRL-4 replay 完整继承（不得绕过 fingerprint / 二套缓存）。"""

    def test_exact_replay_no_queue_append(self):
        boundary, _ = make_boundary()
        first = boundary.submit(next_rev("rev-1"))
        self.assertEqual(first.status, ControlStatus.ACCEPTED)
        replayed = boundary.submit(next_rev("rev-1"))
        self.assertEqual(replayed, first)
        self.assertIs(replayed, first)
        queue = boundary.snapshot(ControlLifecycle.RUNNING).revision_queue
        self.assertEqual(len(queue), 1)

    def test_exact_replay_no_new_journal_fact(self):
        boundary, journal = make_boundary()
        boundary.submit(next_rev("rev-1"))
        boundary.submit(next_rev("rev-1"))
        self.assertEqual(len(journal.snapshot()), 1)

    def test_conflict_queue_unchanged(self):
        boundary, journal = make_boundary()
        boundary.submit(next_rev("rev-1", text="a"))
        conflict = boundary.submit(next_rev("rev-1", text="b"))
        self.assertEqual(conflict.status, ControlStatus.REJECTED)
        self.assertEqual(conflict.reason, ControlReason.COMMAND_ID_CONFLICT)
        queue = boundary.snapshot(ControlLifecycle.RUNNING).revision_queue
        self.assertEqual([entry.text for entry in queue], ["a"])
        self.assertEqual(len(journal.snapshot()), 1)

    def test_replay_all_commands_queue_stable(self):
        # 全量重放不增不减队列（§13.20：applied 历史绝不出现——
        # 队列只含 accepted-pending，重放不移动条目）
        boundary, _ = make_boundary()
        for index in range(3):
            boundary.submit(next_rev(f"rev-{index}"))
        for index in range(3):
            boundary.submit(next_rev(f"rev-{index}"))
        queue = boundary.snapshot(ControlLifecycle.RUNNING).revision_queue
        self.assertEqual([entry.revision_id for entry in queue],
                         ["rev-0", "rev-1", "rev-2"])


class AtomicityTests(unittest.TestCase):
    """§11 journal append 失败 → 零部分状态、零 replay entry。"""

    def test_journal_failure_leaves_queue_unchanged(self):
        journal = _FailingJournal()
        boundary = ControlBoundary(journal=journal, execution_id="exec-1")
        journal.fail = True
        with self.assertRaises(ControlJournalError):
            boundary.submit(next_rev("rev-1"))
        self.assertEqual(boundary.snapshot(
            ControlLifecycle.RUNNING).revision_queue, ())
        self.assertEqual(boundary.pending_intent.kind.value, "NONE")

    def test_failed_submit_not_cached_for_replay(self):
        journal = _FailingJournal()
        boundary = ControlBoundary(journal=journal, execution_id="exec-1")
        journal.fail = True
        with self.assertRaises(ControlJournalError):
            boundary.submit(next_rev("rev-1"))
        journal.fail = False
        # 失败不留痕迹：同 id 重试走完整首发路径
        result = boundary.submit(next_rev("rev-1"))
        self.assertEqual(result.status, ControlStatus.ACCEPTED)
        queue = boundary.snapshot(ControlLifecycle.RUNNING).revision_queue
        self.assertEqual(len(queue), 1)
        self.assertEqual(len(journal.snapshot()), 1)


class JournalBoundaryTests(unittest.TestCase):
    """§8 journal 边界：text 不入 payload；绝不产生 REVISION_APPLIED。"""

    def test_revision_text_never_in_journal_payload(self):
        boundary, journal = make_boundary()
        boundary.submit(next_rev("rev-1", text="secret overlay"))
        boundary.submit(submission_rev("sub-1", task="secret task"))
        for fact in journal.snapshot():
            self.assertNotIn("text", fact.payload)
            for value in fact.payload.values():
                self.assertNotIn("secret", str(value))

    def test_revision_applied_never_produced(self):
        boundary, journal = make_boundary()
        boundary.submit(next_rev("rev-1"))
        boundary.submit(next_rev("rev-1"))      # replay
        boundary.submit(submission_rev("sub-1"))
        boundary.submit(next_rev("rev-1", text="other"))  # conflict
        for fact in journal.snapshot():
            self.assertIsNot(fact.fact_type,
                             ControlFactType.REVISION_APPLIED)


class ConcurrencyTests(unittest.TestCase):
    """§12 并发：不丢、不重、FIFO=linearization、replay 不增队列。"""

    def test_concurrent_distinct_revisions_all_present_fifo(self):
        boundary, journal = make_boundary()
        count = 8
        barrier = threading.Barrier(count)

        def worker(index):
            barrier.wait()
            boundary.submit(next_rev(f"rev-{index}"))

        threads = [threading.Thread(target=worker, args=(i,))
                   for i in range(count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        queue = boundary.snapshot(ControlLifecycle.RUNNING).revision_queue
        queued = [entry.revision_id for entry in queue]
        self.assertEqual(len(queued), count)
        self.assertEqual(len(set(queued)), count)  # 不丢、不重
        # FIFO 与 linearization 一致：队列序 == 账本事实序
        fact_order = [fact.command_id for fact in journal.snapshot()
                      if fact.fact_type is ControlFactType.REVISE_REQUESTED]
        self.assertEqual(queued, fact_order)

    def test_concurrent_same_command_id_exactly_once(self):
        boundary, journal = make_boundary()
        command = next_rev("rev-race")
        barrier = threading.Barrier(6)
        results = {}

        def worker(key):
            barrier.wait()
            results[key] = boundary.submit(command)

        threads = [threading.Thread(target=worker, args=(i,))
                   for i in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual({result.status for result in results.values()},
                         {ControlStatus.ACCEPTED})
        self.assertEqual(len({id(result) for result in results.values()}), 1)
        self.assertEqual(len(journal.snapshot()), 1)
        queue = boundary.snapshot(ControlLifecycle.RUNNING).revision_queue
        self.assertEqual(len(queue), 1)

    def test_concurrent_replay_no_queue_growth(self):
        boundary, journal = make_boundary()
        boundary.submit(next_rev("rev-1"))
        barrier = threading.Barrier(8)

        def worker():
            barrier.wait()
            boundary.submit(next_rev("rev-1"))

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        queue = boundary.snapshot(ControlLifecycle.RUNNING).revision_queue
        self.assertEqual(len(queue), 1)
        self.assertEqual(len(journal.snapshot()), 1)

    def test_concurrent_mixed_targets_queue_only_next_invocation(self):
        boundary, journal = make_boundary()
        barrier = threading.Barrier(8)
        commands = [next_rev(f"rev-{i}") for i in range(4)]
        commands += [submission_rev(f"sub-{i}") for i in range(4)]

        def worker(command):
            barrier.wait()
            boundary.submit(command)

        threads = [threading.Thread(target=worker, args=(cmd,))
                   for cmd in commands]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(journal.snapshot()), 8)
        queue = boundary.snapshot(ControlLifecycle.RUNNING).revision_queue
        queued = [entry.revision_id for entry in queue]
        self.assertEqual(len(queued), 4)
        self.assertTrue(all(entry.startswith("rev-") for entry in queued))


if __name__ == "__main__":
    unittest.main()
