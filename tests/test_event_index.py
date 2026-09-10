"""CU-OBS-1 tests: per-execution append-only EventIndex（观察索引）。

覆盖冻结契约：
- 空 index 读取
- observe 单/多事件、原样保留（identity 不变）
- per-execution（task_id）隔离
- 到达顺序保真（不排序、不去重、不裁决）
- replay / late join（后到读者可读全量历史 + cursor 增量）
- since(cursor) 按 event.sequence 严格大于过滤
- append-only（无 mutator API；TERMINAL 后仍可继续 observe）
- 读取为 detached immutable view，不可反向修改内部状态
- 并发 observe/query 无丢失、无异常
- import boundary：恰 execution_observation + threading；零控制域、
  零 runtime adapter、零投影器依赖；不重定义事件 schema
"""
import ast
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import event_index  # noqa: E402
from event_index import EventIndex  # noqa: E402
from execution_observation import (  # noqa: E402
    ExecutionEvent,
    ExecutionEventType,
    ObservationError,
)


def make_event(task_id="task-1", sequence=0, **overrides):
    fields = dict(
        event_type=ExecutionEventType.STAGE_STARTED, sequence=sequence,
        task_id=task_id, correlation_id="corr-1", stage="architect",
        runtime_id="rt-1", status="ok", reason="unit-test",
        duration_ms=None,
    )
    fields.update(overrides)
    return ExecutionEvent(**fields)


class EmptyIndexTests(unittest.TestCase):
    def test_empty_index_reads_are_empty(self):
        index = EventIndex()
        self.assertEqual(index.execution_ids(), ())
        self.assertEqual(index.snapshot("task-1"), ())
        self.assertEqual(index.since("task-1", 0), ())


class ObserveTests(unittest.TestCase):
    def test_single_event_is_stored(self):
        index = EventIndex()
        event = make_event()
        index.observe(event)
        self.assertEqual(index.snapshot("task-1"), (event,))

    def test_multiple_events_preserve_arrival_order(self):
        index = EventIndex()
        events = [make_event(sequence=i) for i in range(3)]
        for event in events:
            index.observe(event)
        self.assertEqual(index.snapshot("task-1"), tuple(events))

    def test_non_event_rejected(self):
        index = EventIndex()
        for bad in (None, "event", {"event_type": "DECISION"}, 42):
            with self.assertRaises(ObservationError):
                index.observe(bad)
        self.assertEqual(index.execution_ids(), ())

    def test_all_seven_event_types_are_accepted(self):
        index = EventIndex()
        events = [
            make_event(sequence=i, event_type=member)
            for i, member in enumerate(ExecutionEventType)
        ]
        for event in events:
            index.observe(event)
        self.assertEqual(len(index.snapshot("task-1")), 7)


class PerExecutionIsolationTests(unittest.TestCase):
    def test_executions_are_isolated_by_task_id(self):
        index = EventIndex()
        first = [make_event(task_id="task-1", sequence=i) for i in range(2)]
        second = [make_event(task_id="task-2", sequence=i) for i in range(3)]
        for event in first + second:
            index.observe(event)
        self.assertEqual(index.snapshot("task-1"), tuple(first))
        self.assertEqual(index.snapshot("task-2"), tuple(second))

    def test_execution_ids_are_listed_in_first_observe_order(self):
        index = EventIndex()
        index.observe(make_event(task_id="task-b"))
        index.observe(make_event(task_id="task-a"))
        self.assertEqual(index.execution_ids(), ("task-b", "task-a"))

    def test_interleaved_observes_keep_per_execution_order(self):
        index = EventIndex()
        b1 = make_event(task_id="task-b", sequence=0)
        a1 = make_event(task_id="task-a", sequence=0)
        b2 = make_event(task_id="task-b", sequence=1)
        for event in (b1, a1, b2):
            index.observe(event)
        self.assertEqual(index.snapshot("task-a"), (a1,))
        self.assertEqual(index.snapshot("task-b"), (b1, b2))


class OrderingSemanticsTests(unittest.TestCase):
    def test_arrival_order_not_sequence_sorted(self):
        # 索引只保存已发生事件的到达顺序；不排序、不补号、不裁决。
        index = EventIndex()
        late = make_event(sequence=2)
        early = make_event(sequence=0)
        index.observe(late)
        index.observe(early)
        self.assertEqual(index.snapshot("task-1"), (late, early))

    def test_duplicate_sequences_are_both_kept(self):
        index = EventIndex()
        first = make_event(sequence=1, reason="first")
        second = make_event(sequence=1, reason="second")
        index.observe(first)
        index.observe(second)
        self.assertEqual(index.snapshot("task-1"), (first, second))


class ReplayLateJoinTests(unittest.TestCase):
    def test_late_joiner_reads_full_history(self):
        index = EventIndex()
        events = [make_event(sequence=i) for i in range(3)]
        for event in events:
            index.observe(event)
        # 后到的读者从零开始 replay 全量历史
        self.assertEqual(index.snapshot("task-1"), tuple(events))

    def test_cursor_catch_up_after_new_observes(self):
        index = EventIndex()
        for i in range(2):
            index.observe(make_event(sequence=i))
        seen = index.snapshot("task-1")
        cursor = seen[-1].sequence
        self.assertEqual(index.since("task-1", cursor), ())
        fresh = make_event(sequence=2)
        index.observe(fresh)
        self.assertEqual(index.since("task-1", cursor), (fresh,))


class SinceCursorTests(unittest.TestCase):
    def _filled(self):
        index = EventIndex()
        events = [
            make_event(sequence=0),
            make_event(sequence=1),
            make_event(sequence=4),
        ]
        for event in events:
            index.observe(event)
        return index, events

    def test_since_returns_events_strictly_after_cursor(self):
        index, events = self._filled()
        self.assertEqual(index.since("task-1", 0), (events[1], events[2]))

    def test_since_cursor_between_values_skips_by_sequence(self):
        index, events = self._filled()
        # sequence 不必连续：cursor=2 仍按事件自身 sequence 过滤
        self.assertEqual(index.since("task-1", 2), (events[2],))

    def test_since_beyond_all_sequences_returns_empty(self):
        index, _ = self._filled()
        self.assertEqual(index.since("task-1", 99), ())

    def test_since_is_scoped_to_one_execution(self):
        index, _ = self._filled()
        index.observe(make_event(task_id="task-2", sequence=0))
        self.assertEqual(index.since("task-1", 0), tuple(
            index.snapshot("task-1")[1:]))

    def test_since_rejects_invalid_cursors(self):
        index, _ = self._filled()
        for bad in (-1, "0", 1.5, True):
            with self.assertRaises(ObservationError):
                index.since("task-1", bad)

    def test_snapshot_rejects_empty_task_id(self):
        index, _ = self._filled()
        with self.assertRaises(ObservationError):
            index.snapshot("   ")
        with self.assertRaises(ObservationError):
            index.since("", 0)


class AppendOnlyTests(unittest.TestCase):
    def test_index_exposes_no_mutator_api(self):
        index = EventIndex()
        for name in ("clear", "delete", "remove", "pop", "update",
                     "replace", "reset", "truncate", "rewind", "extend",
                     "insert"):
            self.assertFalse(hasattr(index, name), name)

    def test_reads_do_not_close_the_index(self):
        index = EventIndex()
        first = make_event(sequence=0)
        index.observe(first)
        index.snapshot("task-1")
        index.since("task-1", 0)
        second = make_event(sequence=1)
        index.observe(second)
        self.assertEqual(index.snapshot("task-1"), (first, second))

    def test_terminal_event_does_not_close_execution(self):
        # 无终态裁决：TERMINAL 之后仍可继续 observe（索引不是状态机）
        index = EventIndex()
        terminal = make_event(event_type=ExecutionEventType.TERMINAL)
        index.observe(terminal)
        after = make_event(sequence=1)
        index.observe(after)
        self.assertEqual(index.snapshot("task-1"), (terminal, after))


class DetachedReadTests(unittest.TestCase):
    def test_snapshot_returns_detached_tuple(self):
        index = EventIndex()
        index.observe(make_event())
        snapshot = index.snapshot("task-1")
        self.assertIsInstance(snapshot, tuple)
        with self.assertRaises(AttributeError):
            snapshot.append(make_event(sequence=1))
        self.assertEqual(len(index.snapshot("task-1")), 1)

    def test_reads_never_mutate_internal_state(self):
        index = EventIndex()
        event = make_event()
        index.observe(event)
        first = index.snapshot("task-1")
        index.since("task-1", 0)
        index.execution_ids()
        self.assertEqual(index.snapshot("task-1"), first)


class IdentityPreservationTests(unittest.TestCase):
    def test_stored_event_is_the_same_object(self):
        index = EventIndex()
        event = make_event()
        index.observe(event)
        self.assertIs(index.snapshot("task-1")[0], event)

    def test_event_fields_are_untouched(self):
        index = EventIndex()
        event = make_event()
        index.observe(event)
        stored = index.snapshot("task-1")[0]
        self.assertEqual(stored.to_dict(), event.to_dict())


class ConcurrencyTests(unittest.TestCase):
    def test_concurrent_observes_are_not_lost(self):
        index = EventIndex()

        def worker(worker_index):
            events = [
                make_event(
                    task_id=f"task-{worker_index % 2}",
                    sequence=worker_index * 25 + j,
                    correlation_id=f"corr-{worker_index}-{j}",
                )
                for j in range(25)
            ]
            for event in events:
                index.observe(event)
            return events

        with ThreadPoolExecutor(max_workers=8) as pool:
            batches = list(pool.map(worker, range(8)))

        all_events = [event for batch in batches for event in batch]
        self.assertEqual(len(index.snapshot("task-0")), 100)
        self.assertEqual(len(index.snapshot("task-1")), 100)
        self.assertEqual(index.execution_ids(), ("task-0", "task-1"))
        stored_ids = {id(event) for event in
                      index.snapshot("task-0") + index.snapshot("task-1")}
        self.assertEqual(len(stored_ids), 200)
        self.assertEqual(stored_ids, {id(event) for event in all_events})

    def test_concurrent_query_during_observes_is_consistent(self):
        index = EventIndex()
        observations = []

        def reader():
            for _ in range(200):
                snapshot = index.snapshot("task-1")
                observations.append(len(snapshot))

        import threading
        thread = threading.Thread(target=reader)
        thread.start()
        try:
            for i in range(100):
                index.observe(make_event(sequence=i))
        finally:
            thread.join()
        self.assertTrue(observations)
        # 读取者观察到的长度单调不减且绝不超过最终值
        self.assertEqual(observations, sorted(observations))
        self.assertLessEqual(max(observations), 100)


class ImportBoundaryTests(unittest.TestCase):
    def test_module_import_roots_are_exact(self):
        source = (SCRIPTS / "event_index.py").read_text(encoding="utf-8")
        roots = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.ImportFrom):
                roots.add(node.module.split(".")[0])
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    roots.add(alias.name.split(".")[0])
        self.assertEqual(roots, {"__future__", "threading",
                                 "execution_observation"})

    def test_module_does_not_reference_forbidden_domains(self):
        source = (SCRIPTS / "event_index.py").read_text(encoding="utf-8")
        for token in ("control_boundary", "control_journal", "usage_log",
                      "trace_projector", "cockpit", "subprocess"):
            self.assertNotIn(token, source)

    def test_module_is_runtime_neutral(self):
        source = (SCRIPTS / "event_index.py").read_text(encoding="utf-8")
        lowered = source.lower()
        for token in ("claude", "codex", "deepseek", "openai", "anthropic",
                      "gemini", "tiny-agents", "tiny_agents"):
            self.assertNotIn(token, lowered)

    def test_module_does_not_redefine_event_schema(self):
        source = (SCRIPTS / "event_index.py").read_text(encoding="utf-8")
        self.assertNotIn("class ExecutionEvent", source)
        self.assertNotIn("class ExecutionEventType", source)
        self.assertEqual(event_index.__all__, ("EventIndex",))

    def test_index_exposes_no_adjudication_surface(self):
        index = EventIndex()
        for name in ("lifecycle", "state", "status", "is_paused",
                     "is_running", "project", "version", "adjudicate"):
            self.assertFalse(hasattr(index, name), name)


if __name__ == "__main__":
    unittest.main()
