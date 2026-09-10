"""CU-OBS-4a tests: AbortGate（pre-delegation 控制窗检查）。

职责窗：ControlGate 已 PASS、invocation 尚未真正 delegation 给
raw adapter 之间——只读 ControlBoundary 公开 pending_intent：
NONE → delegate；PAUSE → delegate（停驻在下一 admission 生效）；
ABORT → raise ControlAborted（复用 control_gate 唯一定义）。

契约：只 raise 不 catch；零 journal/执行事件/事件索引/用量/trace
副作用；不动 lifecycle/revision/replay；无取消面、无进程 API；
check→delegate 之间允许自然 race（不要求全局原子化）；无轮询、
无睡眠、无后台线程——纯同步包装。
"""
import ast
import sys
import threading
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import abort_gate as abort_gate_module  # noqa: E402
import control_gate as control_gate_module  # noqa: E402
from abort_gate import AbortGate  # noqa: E402
from control_boundary import (  # noqa: E402
    ControlBoundary,
    ControlCommand,
    ControlCommandType,
    ControlModelError,
    PendingIntentKind,
)
from control_gate import ControlAborted  # noqa: E402
from control_journal import ControlJournal  # noqa: E402

MODULE_PATH = SCRIPTS / "abort_gate.py"


def make_gate(execution_id="exec-1"):
    journal = ControlJournal()
    boundary = ControlBoundary(journal=journal, execution_id=execution_id)
    return AbortGate(boundary), boundary, journal


def pause_cmd(command_id="cmd-pause", execution_id="exec-1"):
    return ControlCommand(command_id=command_id, execution_id=execution_id,
                          command=ControlCommandType.PAUSE)


def abort_cmd(command_id="cmd-abort", execution_id="exec-1"):
    return ControlCommand(command_id=command_id, execution_id=execution_id,
                          command=ControlCommandType.ABORT)


class ConstructionTests(unittest.TestCase):
    def test_gate_requires_boundary(self):
        gate, boundary, _ = make_gate()
        self.assertIsInstance(gate, AbortGate)

    def test_non_boundary_rejected(self):
        with self.assertRaises(ControlModelError):
            AbortGate(boundary=object())

    def test_control_aborted_reuses_control_gate_definition(self):
        # §2：复用 CTRL-7 唯一定义，绝不重新定义
        self.assertIs(abort_gate_module.ControlAborted,
                      control_gate_module.ControlAborted)

    def test_no_redeclaration_in_source(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        self.assertNotIn("class ControlAborted", source)


class DelegationTests(unittest.TestCase):
    """§9-1/2/3/4/5/6：三分支 + 委托保真。"""

    def test_none_intent_delegates(self):
        gate, _, _ = make_gate()
        calls = []

        def delegate(value):
            calls.append(value)
            return f"done:{value}"

        self.assertEqual(gate.delegate(delegate, "x"), "done:x")
        self.assertEqual(calls, ["x"])

    def test_pause_intent_still_delegates(self):
        gate, boundary, _ = make_gate()
        boundary.submit(pause_cmd())
        self.assertEqual(boundary.pending_intent.kind, PendingIntentKind.PAUSE)
        result = gate.delegate(lambda: "delegated")
        self.assertEqual(result, "delegated")

    def test_abort_intent_raises(self):
        gate, boundary, _ = make_gate()
        boundary.submit(abort_cmd())
        calls = []
        with self.assertRaises(ControlAborted):
            gate.delegate(lambda: calls.append(1))
        self.assertEqual(calls, [])

    def test_abort_does_not_invoke_delegate(self):
        gate, boundary, _ = make_gate()
        boundary.submit(abort_cmd())
        counter = []
        try:
            gate.delegate(lambda *a, **k: counter.append((a, k)))
        except ControlAborted:
            pass
        self.assertEqual(counter, [])

    def test_aborted_carries_execution_id(self):
        gate, boundary, _ = make_gate("exec-42")
        boundary.submit(abort_cmd(execution_id="exec-42"))
        with self.assertRaises(ControlAborted) as caught:
            gate.delegate(lambda: None)
        self.assertEqual(caught.exception.execution_id, "exec-42")
        self.assertIn("exec-42", str(caught.exception))

    def test_delegate_arguments_forwarded(self):
        gate, _, _ = make_gate()
        recorded = {}
        gate.delegate(lambda *args, **kwargs: recorded.update(
            args=args, kwargs=kwargs), 1, "two", key=None, other=[3])
        self.assertEqual(recorded["args"], (1, "two"))
        self.assertEqual(recorded["kwargs"], {"key": None, "other": [3]})

    def test_delegate_return_preserved_identically(self):
        gate, _, _ = make_gate()
        sentinel = object()
        self.assertIs(gate.delegate(lambda: sentinel), sentinel)

    def test_delegate_exception_propagates_unchanged(self):
        gate, _, _ = make_gate()
        sentinel = ValueError("raw adapter failure")

        def delegate():
            raise sentinel

        with self.assertRaises(ValueError) as caught:
            gate.delegate(delegate)
        self.assertIs(caught.exception, sentinel)

    def test_delegate_raised_control_aborted_passes_through(self):
        # delegate 自身抛出的 ControlAborted 原样透传（gate 不拦截）
        gate, _, _ = make_gate()
        sentinel = ControlAborted("inner-exec")

        def delegate():
            raise sentinel

        with self.assertRaises(ControlAborted) as caught:
            gate.delegate(delegate)
        self.assertIs(caught.exception, sentinel)


class IsolationTests(unittest.TestCase):
    """§9-7：execution 隔离。"""

    def test_execution_isolation(self):
        gate_a, boundary_a, _ = make_gate("exec-a")
        gate_b, boundary_b, _ = make_gate("exec-b")
        boundary_a.submit(abort_cmd(execution_id="exec-a"))
        with self.assertRaises(ControlAborted) as caught:
            gate_a.delegate(lambda: None)
        self.assertEqual(caught.exception.execution_id, "exec-a")
        # exec-b 干净：照常 delegation，互不串扰
        self.assertEqual(gate_b.delegate(lambda: "ok-b"), "ok-b")


class PurityTests(unittest.TestCase):
    """§9-8/9/10/11/13/14：纯度与架构守卫（源扫描 + AST）。"""

    def test_no_journal_side_effects(self):
        gate, boundary, journal = make_gate()
        boundary.submit(pause_cmd())
        facts_at_pause = journal.snapshot()
        gate.delegate(lambda: None)              # PAUSE 窗 delegation
        boundary.submit(abort_cmd())
        facts_at_abort = journal.snapshot()
        with self.assertRaises(ControlAborted):
            gate.delegate(lambda: None)          # abort raise 路径
        self.assertEqual(len(facts_at_pause), 1)
        self.assertEqual(len(facts_at_abort), 2)
        self.assertEqual(journal.snapshot(), facts_at_abort)  # gate 零贡献

    def test_no_private_or_journal_access(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        for token in ("ControlJournal", "_replay", "_queue",
                      "boundary_writer", "_writer", "revision"):
            self.assertNotIn(token, source, token)

    def test_no_observation_domains(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        for token in ("execution_observation", "event_index", "usage_log",
                      "trace_projector", "ExecutionEvent", "cockpit"):
            self.assertNotIn(token, source, token)

    def test_no_lifecycle_management(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        for token in ("ControlLifecycle", "ParkPoint", "snapshot(",
                      "_lifecycle", "lifecycle"):
            self.assertNotIn(token, source, token)

    def test_no_cancel_or_process_api(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        for token in ("subprocess", "Popen", "kill", "terminate", "cancel",
                      "invocation_id"):
            self.assertNotIn(token, source, token)

    def test_no_polling_no_threads(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        for token in ("time.sleep", "sleep(", "while True", "threading",
                      "Thread", "monotonic"):
            self.assertNotIn(token, source, token)

    def test_import_roots_locked(self):
        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        roots = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                roots.add(node.module.split(".")[0])
        self.assertEqual(roots, {"__future__", "control_boundary",
                                 "control_gate"})


class ConcurrencyTests(unittest.TestCase):
    """§9-12：并发（Barrier；自然 race 合法双出口）。"""

    def test_concurrent_clean_delegation_all_succeed(self):
        gate, _, _ = make_gate()
        count = 8
        barrier = threading.Barrier(count)
        results = {}
        calls = []

        def worker(index):
            barrier.wait()
            results[index] = gate.delegate(lambda index=index:
                                           calls.append(index) or f"ok{index}")

        threads = [threading.Thread(target=worker, args=(i,))
                   for i in range(count)]
        for thread in threads:
            thread.start()
            self.addCleanup(thread.join, 3.0)
        for thread in threads:
            thread.join()
        self.assertEqual(len(results), count)
        self.assertEqual(len(calls), count)
        self.assertTrue(all(value.startswith("ok") for value in results.values()))

    def test_concurrent_abort_all_raise(self):
        gate, boundary, _ = make_gate()
        boundary.submit(abort_cmd())
        count = 8
        barrier = threading.Barrier(count)
        raised = []
        calls = []

        def worker(index):
            barrier.wait()
            try:
                gate.delegate(lambda: calls.append(index))
                raised.append(("pass", index))
            except ControlAborted:
                raised.append(("aborted", index))

        threads = [threading.Thread(target=worker, args=(i,))
                   for i in range(count)]
        for thread in threads:
            thread.start()
            self.addCleanup(thread.join, 3.0)
        for thread in threads:
            thread.join()
        self.assertEqual([kind for kind, _ in raised], ["aborted"] * count)
        self.assertEqual(calls, [])

    def test_natural_race_abort_vs_delegate(self):
        # §5：check→delegate 之间允许自然 race——出口合法双值：
        # 已 delegation 或已 raise，无悬挂、无第三态
        gate, boundary, _ = make_gate()
        count = 8
        barrier = threading.Barrier(count)
        outcomes = {"delegated": 0, "aborted": 0}

        def worker(index):
            if index == 0:
                boundary.submit(abort_cmd())  # 与其余线程 delegation 竞争
            barrier.wait()
            try:
                gate.delegate(lambda: None)
                outcomes["delegated"] += 1
            except ControlAborted:
                outcomes["aborted"] += 1

        threads = [threading.Thread(target=worker, args=(i,))
                   for i in range(count)]
        for thread in threads:
            thread.start()
            self.addCleanup(thread.join, 3.0)
        for thread in threads:
            thread.join()
        self.assertEqual(outcomes["delegated"] + outcomes["aborted"], count)
        self.assertGreaterEqual(outcomes["aborted"], 1)  # ABORT 最终可见


if __name__ == "__main__":
    unittest.main()
