"""CU-CTRL-7 tests: ControlGate（B2 ADMISSION 控制门）。

核心契约：
- ControlGate 是 CONTROL PLANE：在真实 Stage Admission 边界读
  ControlBoundary 只读控制事实（pending_intent 公共属性），决定
  admission 放行 / 停驻 / abort。
- gate HOLD 是 PAUSED 的唯一实际控制确认来源（is_holding →
  composition 层据此向 CTRL-6 投影供应 park_point=ADMISSION）。
- PAUSE_REQUESTED ≠ PAUSED：HOLD 前/无 HOLD 时绝不呈现停驻。
- ABORT 优先于 PAUSE；RESUME 不得解除 ABORT；REVISE 不影响 gate。
- ControlAborted（授权在 control_gate.py 定义一次）：gate 只 raise
  不 catch；catch 属未来 COMP-*。gate 不写 ControlJournal、不写
  ExecutionEvent、不触 runtime/adapter/subprocess、不 cancel 在途。
- 唤醒：composition 在 RESUME/ABORT accepted 后调 release()/abort()；
  等待者唤醒后重读控制真值自决（真值校验、不信任信号本身）；
  Condition 无 busy polling；锁内复查真值杜绝 lost-wakeup。
"""
import ast
import sys
import threading
import time
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
from control_journal import ControlJournal  # noqa: E402
from control_gate import ControlAborted, ControlGate  # noqa: E402

MODULE_PATH = SCRIPTS / "control_gate.py"


def make_gate(execution_id="exec-1"):
    journal = ControlJournal()
    boundary = ControlBoundary(journal=journal, execution_id=execution_id)
    return ControlGate(boundary), boundary, journal


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


def start_holder(gate):
    """起一个 worker 线程调用 gate.check()；done 置位即已返回。"""
    done = threading.Event()
    outcome = {}

    def run():
        try:
            gate.check()
            outcome["result"] = "pass"
        except ControlAborted:
            outcome["result"] = "aborted"
        except BaseException as error:  # 防御记录
            outcome["result"] = f"unexpected:{error!r}"
        finally:
            done.set()

    thread = threading.Thread(target=run)
    thread.start()
    return thread, done, outcome


def eventually(predicate, timeout=2.0):
    """测试辅助：有界等待断言条件成立（仅测试文件内使用）。"""
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.002)
    return True


class ConstructionTests(unittest.TestCase):
    def test_gate_requires_boundary(self):
        gate, boundary, _ = make_gate()
        self.assertIsInstance(gate, ControlGate)
        self.assertFalse(gate.is_holding)

    def test_non_boundary_rejected(self):
        with self.assertRaises(ControlModelError):
            ControlGate(boundary="not-a-boundary")

    def test_control_aborted_is_plain_exception(self):
        # 授权裁决 1：在 control_gate.py 定义一次；gate 只 raise 不 catch
        self.assertTrue(issubclass(ControlAborted, Exception))
        self.assertNotIsInstance(ControlAborted("x"), ControlModelError)


class PassTests(unittest.TestCase):
    """§18-1/11/13/14/23：无意图 → PASS；gate 不被无关控制扰动。"""

    def test_no_intent_passes_immediately(self):  # 1
        gate, _, _ = make_gate()
        self.assertIsNone(gate.check())

    def test_revise_does_not_affect_gate(self):  # 11
        gate, boundary, _ = make_gate()
        self.assertEqual(boundary.submit(revise_cmd()).status.value,
                         "ACCEPTED")
        self.assertIsNone(gate.check())
        self.assertEqual(boundary.pending_intent.kind, PendingIntentKind.NONE)

    def test_conflict_does_not_affect_gate(self):  # 13
        gate, boundary, journal = make_gate()
        boundary.submit(revise_cmd(command_id="x1"))
        boundary.submit(revise_cmd(command_id="x1", version=1))  # conflict
        facts_before = journal.snapshot()
        self.assertIsNone(gate.check())
        self.assertEqual(journal.snapshot(), facts_before)

    def test_terminal_truth_prevents_new_hold(self):  # 14
        gate, boundary, _ = make_gate()
        boundary.submit(pause_cmd())
        # 终态真值：gate 不 resurrect / hold / abort（§11）
        for terminal in (ControlLifecycle.COMPLETED,
                         ControlLifecycle.ABORTED,
                         ControlLifecycle.FAILED):
            self.assertIsNone(gate.check(terminal=terminal))
        self.assertFalse(gate.is_holding)
        boundary_b = ControlBoundary(journal=ControlJournal(),
                                     execution_id="exec-b")
        gate_b = ControlGate(boundary_b)
        boundary_b.submit(abort_cmd())
        self.assertIsNone(gate_b.check(terminal=ControlLifecycle.COMPLETED))

    def test_invalid_terminal_rejected(self):
        gate, _, _ = make_gate()
        with self.assertRaises(ControlModelError):
            gate.check(terminal="COMPLETED")

    def test_repeated_pass_does_not_mutate_control_facts(self):  # 23
        gate, boundary, journal = make_gate()
        boundary.submit(revise_cmd())
        facts_before = journal.snapshot()
        queue_before = boundary.snapshot(
            ControlLifecycle.RUNNING).revision_queue
        for _ in range(3):
            gate.check()
        self.assertEqual(journal.snapshot(), facts_before)
        self.assertEqual(boundary.execution_version, 0)
        self.assertEqual(boundary.pending_intent.kind, PendingIntentKind.NONE)
        self.assertEqual(boundary.snapshot(
            ControlLifecycle.RUNNING).revision_queue, queue_before)


class PauseHoldTests(unittest.TestCase):
    """§18-2/3/4/5/6/12：HOLD 语义与 ADMISSION 确认。"""

    def test_pause_intent_at_admission_holds(self):  # 2
        gate, boundary, _ = make_gate()
        boundary.submit(pause_cmd())
        thread, done, _ = start_holder(gate)
        self.addCleanup(thread.join, 2.0)
        self.assertTrue(eventually(lambda: gate.is_holding))
        self.assertFalse(done.wait(0.2))  # 停驻：check 不返回
        # 释放收尾（本测试只验证停驻事实）
        boundary.submit(resume_cmd())
        gate.release()

    def test_hold_is_admission_park_confirmation(self):  # 3
        gate, boundary, _ = make_gate()
        boundary.submit(pause_cmd())
        thread, done, _ = start_holder(gate)
        self.addCleanup(thread.join, 2.0)
        self.assertTrue(eventually(lambda: gate.is_holding))
        # HOLD = PAUSED 的唯一实际控制确认（ADMISSION）：
        # composition 持 is_holding 向 CTRL-6 投影供应 park_point
        snapshot = boundary.snapshot(ControlLifecycle.PAUSED,
                                     park_point=ParkPoint.ADMISSION)
        self.assertEqual(snapshot.lifecycle, ControlLifecycle.PAUSED)
        self.assertEqual(snapshot.park_point.value, "ADMISSION")
        boundary.submit(resume_cmd())
        gate.release()
        self.assertTrue(done.wait(2.0))

    def test_paused_not_inferred_before_hold(self):  # 4
        gate, boundary, _ = make_gate()
        boundary.submit(pause_cmd())  # PAUSE_REQUESTED ≠ PAUSED
        self.assertFalse(gate.is_holding)
        # 无 gate 确认时 CTRL-6 拒绝 PAUSED 投影
        with self.assertRaises(ControlModelError):
            boundary.snapshot(ControlLifecycle.PAUSED)

    def test_gate_never_produces_dispatch(self):  # §6
        source = MODULE_PATH.read_text(encoding="utf-8")
        self.assertNotIn("DISPATCH", source)

    def test_resume_releases_hold(self):  # 5
        gate, boundary, _ = make_gate()
        boundary.submit(pause_cmd())
        thread, done, outcome = start_holder(gate)
        self.addCleanup(thread.join, 2.0)
        self.assertTrue(eventually(lambda: gate.is_holding))
        boundary.submit(resume_cmd())  # control truth 先清
        gate.release()                 # 唤醒信号后到亦可
        self.assertTrue(done.wait(2.0))
        self.assertEqual(outcome["result"], "pass")
        self.assertTrue(eventually(lambda: not gate.is_holding))

    def test_release_before_truth_change_keeps_holding(self):
        # 信号不携带真相：RESUME 未被接受时 release 不放行
        gate, boundary, _ = make_gate()
        boundary.submit(pause_cmd())
        thread, done, _ = start_holder(gate)
        self.addCleanup(thread.join, 2.0)
        self.assertTrue(eventually(lambda: gate.is_holding))
        gate.release()  # 无 RESUME：等待者复查真值后继续停驻
        self.assertFalse(done.wait(0.2))
        self.assertTrue(gate.is_holding)
        boundary.submit(resume_cmd())
        gate.release()
        self.assertTrue(done.wait(2.0))

    def test_wake_creates_no_new_facts_or_events(self):  # 6
        gate, boundary, journal = make_gate()
        boundary.submit(pause_cmd())
        facts_at_hold = journal.snapshot()
        thread, done, _ = start_holder(gate)
        self.addCleanup(thread.join, 2.0)
        self.assertTrue(eventually(lambda: gate.is_holding))
        boundary.submit(resume_cmd())
        gate.release()
        self.assertTrue(done.wait(2.0))
        # gate/re wake 零新增事实：恰 pause+resume 两条 REQUESTED
        facts = journal.snapshot()
        self.assertEqual(len(facts), 2)
        self.assertEqual(facts_at_hold, facts[:1])

    def test_replay_does_not_create_additional_hold(self):  # 12
        gate, boundary, journal = make_gate()
        boundary.submit(pause_cmd())
        thread, done, _ = start_holder(gate)
        self.addCleanup(thread.join, 2.0)
        self.assertTrue(eventually(lambda: gate.is_holding))
        replay = boundary.submit(pause_cmd())  # exact replay
        self.assertEqual(replay.status.value, "ACCEPTED")
        self.assertEqual(len(journal.snapshot()), 1)
        self.assertFalse(done.wait(0.2))  # 单一停驻不变
        boundary.submit(resume_cmd())
        gate.release()
        self.assertTrue(done.wait(2.0))

    def test_revise_during_hold_keeps_holding(self):
        gate, boundary, _ = make_gate()
        boundary.submit(pause_cmd())
        thread, done, _ = start_holder(gate)
        self.addCleanup(thread.join, 2.0)
        self.assertTrue(eventually(lambda: gate.is_holding))
        boundary.submit(revise_cmd())  # REVISE 不动 pending、不唤醒
        self.assertFalse(done.wait(0.2))
        self.assertTrue(gate.is_holding)
        boundary.submit(resume_cmd())
        gate.release()
        self.assertTrue(done.wait(2.0))


class AbortTests(unittest.TestCase):
    """§18-7/8/9/10/25：abort 语义与优先级。"""

    def test_abort_pending_raises_immediately(self):  # 7
        gate, boundary, _ = make_gate()
        boundary.submit(abort_cmd())
        with self.assertRaises(ControlAborted):
            gate.check()
        self.assertFalse(gate.is_holding)

    def test_abort_wakes_pause_waiter(self):  # 8
        gate, boundary, _ = make_gate()
        boundary.submit(pause_cmd())
        thread, done, outcome = start_holder(gate)
        self.addCleanup(thread.join, 2.0)
        self.assertTrue(eventually(lambda: gate.is_holding))
        boundary.submit(abort_cmd())  # ABORT > PAUSE：真值翻转
        gate.abort()                  # 唤醒等待者
        self.assertTrue(done.wait(2.0))
        self.assertEqual(outcome["result"], "aborted")

    def test_abort_beats_pause(self):  # 9
        gate, boundary, _ = make_gate()
        boundary.submit(pause_cmd())
        boundary.submit(abort_cmd())
        with self.assertRaises(ControlAborted):
            gate.check()

    def test_resume_cannot_clear_abort(self):  # 10
        gate, boundary, _ = make_gate()
        boundary.submit(abort_cmd())
        # CTRL-3 冻结表：ABORT-pending 下 RESUME = REJECTED/ALREADY_ABORTING
        result = boundary.submit(resume_cmd())
        self.assertEqual(result.status.value, "REJECTED")
        self.assertEqual(result.reason.value, "ALREADY_ABORTING")
        with self.assertRaises(ControlAborted):
            gate.check()

    def test_abort_raises_once_per_observed_condition(self):  # 25
        gate, boundary, _ = make_gate()
        boundary.submit(pause_cmd())
        thread, done, outcome = start_holder(gate)
        self.addCleanup(thread.join, 2.0)
        self.assertTrue(eventually(lambda: gate.is_holding))
        boundary.submit(abort_cmd())
        gate.abort()
        gate.abort()  # 重复唤醒信号：等待者仍只 raise 一次
        self.assertTrue(done.wait(2.0))
        self.assertEqual(outcome["result"], "aborted")
        self.assertTrue(eventually(lambda: not gate.is_holding))

    def test_abort_exception_carries_execution(self):
        gate, boundary, _ = make_gate()
        boundary.submit(abort_cmd())
        with self.assertRaises(ControlAborted) as caught:
            gate.check()
        self.assertIn("exec-1", str(caught.exception))


class IsolationTests(unittest.TestCase):
    """§18-20：execution 隔离。"""

    def test_multiple_executions_isolated(self):  # 20
        gate_a, boundary_a, _ = make_gate("exec-a")
        gate_b, boundary_b, _ = make_gate("exec-b")
        boundary_a.submit(pause_cmd(execution_id="exec-a"))
        thread_a, done_a, _ = start_holder(gate_a)
        self.addCleanup(thread_a.join, 2.0)
        self.assertTrue(eventually(lambda: gate_a.is_holding))
        # exec-b 无停驻：照常 PASS
        self.assertIsNone(gate_b.check())
        # 释放 A 不影响 B（各自 Condition / 各自真值）
        boundary_a.submit(resume_cmd(execution_id="exec-a"))
        gate_a.release()
        self.assertTrue(done_a.wait(2.0))
        self.assertIsNone(gate_b.check())
        self.assertFalse(gate_b.is_holding)


class PurityTests(unittest.TestCase):
    """§18-15/16/17/21/22/24：纯度与架构守卫。"""

    def test_no_inflight_cancel_surface(self):  # 15
        source = MODULE_PATH.read_text(encoding="utf-8")
        for token in ("subprocess", "kill", "terminate", "invocation_id",
                      "Popen", "adapter"):
            self.assertNotIn(token, source, token)

    def test_import_roots_locked(self):  # 16
        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        roots = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                roots.add(node.module.split(".")[0])
        self.assertEqual(roots, {"__future__", "threading",
                                 "control_boundary"})

    def test_no_busy_polling(self):  # 17
        source = MODULE_PATH.read_text(encoding="utf-8")
        for token in ("time.sleep", "while True", "time.time", "monotonic",
                      "time.monotonic"):
            self.assertNotIn(token, source, token)

    def test_gate_stores_no_lifecycle(self):  # 21
        gate, boundary, _ = make_gate()
        boundary.submit(pause_cmd())
        boundary.submit(resume_cmd())
        gate.check()
        gate.release()
        for owner, names in ((gate, ("_lifecycle", "_execution_state",
                                     "lifecycle")),
                             (boundary, ("_lifecycle", "_state",
                                         "lifecycle"))):
            for name in names:
                self.assertFalse(hasattr(owner, name), f"{owner}:{name}")

    def test_gate_never_mutates_journal(self):  # 22
        gate, boundary, journal = make_gate()
        boundary.submit(pause_cmd())
        thread, done, _ = start_holder(gate)
        self.addCleanup(thread.join, 2.0)
        self.assertTrue(eventually(lambda: gate.is_holding))
        boundary.submit(resume_cmd())
        gate.release()
        gate.abort()  # 空唤醒亦零副作用
        self.assertTrue(done.wait(2.0))
        facts = journal.snapshot()
        self.assertEqual([fact.fact_type.value for fact in facts],
                         ["PAUSE_REQUESTED", "RESUME_REQUESTED"])

    def test_repeated_hold_does_not_duplicate_control_facts(self):  # 24
        gate, boundary, journal = make_gate()
        # 第一轮 hold → resume release
        boundary.submit(pause_cmd(command_id="p1"))
        thread1, done1, _ = start_holder(gate)
        self.addCleanup(thread1.join, 2.0)
        self.assertTrue(eventually(lambda: gate.is_holding))
        boundary.submit(resume_cmd(command_id="r1"))
        gate.release()
        self.assertTrue(done1.wait(2.0))
        # 第二轮 hold → resume release（新 command_id，非 replay）
        boundary.submit(pause_cmd(command_id="p2"))
        thread2, done2, _ = start_holder(gate)
        self.addCleanup(thread2.join, 2.0)
        self.assertTrue(eventually(lambda: gate.is_holding))
        boundary.submit(resume_cmd(command_id="r2"))
        gate.release()
        self.assertTrue(done2.wait(2.0))
        # 恰四条 REQUESTED，全部来自 submit，gate 零贡献
        self.assertEqual([fact.fact_type.value for fact in journal.snapshot()],
                         ["PAUSE_REQUESTED", "RESUME_REQUESTED",
                          "PAUSE_REQUESTED", "RESUME_REQUESTED"])


class ConcurrencyTests(unittest.TestCase):
    """§18-18/19/15 节：并发确定性（Barrier；无 sleep 技巧）。"""

    def test_concurrent_pause_resume_deterministic(self):  # 18
        gate, boundary, journal = make_gate()
        boundary.submit(pause_cmd())
        count = 4
        barrier = threading.Barrier(count)
        dones = []
        outcomes = []

        def worker(index):
            barrier.wait()
            try:
                gate.check()
                outcomes.append("pass")
            except ControlAborted:
                outcomes.append("aborted")
            finally:
                dones[index].set()

        dones = [threading.Event() for _ in range(count)]
        threads = [threading.Thread(target=worker, args=(i,))
                   for i in range(count)]
        for thread in threads:
            thread.start()
            self.addCleanup(thread.join, 3.0)
        self.assertTrue(eventually(lambda: gate.is_holding))
        boundary.submit(resume_cmd())
        gate.release()
        for done in dones:
            self.assertTrue(done.wait(3.0))
        self.assertEqual(outcomes, ["pass"] * count)
        self.assertEqual(len(journal.snapshot()), 2)

    def test_concurrent_pause_abort_deterministic(self):  # 19
        gate, boundary, journal = make_gate()
        boundary.submit(pause_cmd())
        count = 4
        barrier = threading.Barrier(count)
        dones = [threading.Event() for _ in range(count)]
        outcomes = []

        def worker(index):
            barrier.wait()
            try:
                gate.check()
                outcomes.append("pass")
            except ControlAborted:
                outcomes.append("aborted")
            finally:
                dones[index].set()

        threads = [threading.Thread(target=worker, args=(i,))
                   for i in range(count)]
        for thread in threads:
            thread.start()
            self.addCleanup(thread.join, 3.0)
        self.assertTrue(eventually(lambda: gate.is_holding))
        boundary.submit(abort_cmd())
        gate.abort()
        for done in dones:
            self.assertTrue(done.wait(3.0))
        self.assertEqual(outcomes, ["aborted"] * count)
        self.assertEqual([fact.fact_type.value for fact in journal.snapshot()],
                         ["PAUSE_REQUESTED", "ABORT_REQUESTED"])

    def test_pause_resume_race_resolved_by_linearization(self):
        # PAUSE accepted → check 观察 PAUSE 与 RESUME accepted 竞争：
        # 两种真实顺序都合法且确定性收敛为 pass，绝不悬挂
        gate, boundary, _ = make_gate()
        boundary.submit(pause_cmd())
        thread, done, outcome = start_holder(gate)
        self.addCleanup(thread.join, 3.0)
        boundary.submit(resume_cmd())  # 与 holder 观察竞争
        gate.release()
        self.assertTrue(done.wait(3.0))
        self.assertEqual(outcome["result"], "pass")


if __name__ == "__main__":
    unittest.main()
