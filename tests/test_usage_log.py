"""CU-OBS-2 tests: append-only per-invocation UsageLog（usage 观察 store）。

覆盖冻结契约：
- canonical record 字段集 + 三态 usage 语义（KNOWN/UNKNOWN/UNSUPPORTED）
- record absent ≠ record exists + usage UNKNOWN（store 绝不代缺席编造）
- KNOWN⇒tokens 在场、UNKNOWN/UNSUPPORTED⇒tokens 缺席（结构性耦合，
  零编造：不猜数字、UNKNOWN 保持 UNKNOWN、UNSUPPORTED 保持 UNSUPPORTED）
- applied_revisions 只是 correlation 引用（immutable tuple，非 revision 真值）
- per-invocation 独立（同 task 多 invocation 不聚合；同 id 不去重）
- append-only、到达顺序、detached snapshot、since cursor
- 并发 append 不丢失
- import boundary：仅标准库；零控制域/事件索引/投影器/runtime 依赖
"""
import ast
import sys
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import usage_log  # noqa: E402
from usage_log import (  # noqa: E402
    UsageLog,
    UsageLogError,
    UsageObservation,
    UsageRecord,
)


def make_record(invocation_id="inv-1", task_id="task-1", **overrides):
    fields = dict(
        invocation_id=invocation_id, task_id=task_id,
        agent_id="agent-architect", role="architect", runtime_id="rt-1",
        status="SUCCESS", usage_status=UsageObservation.KNOWN,
        duration_ms=1200, input_tokens=100, output_tokens=200,
        applied_revisions=(),
    )
    fields.update(overrides)
    return UsageRecord(**fields)


class VocabularyTests(unittest.TestCase):
    def test_usage_observation_vocabulary_is_exact(self):
        self.assertEqual(
            {member.name for member in UsageObservation},
            {"KNOWN", "UNKNOWN", "UNSUPPORTED"})

    def test_raw_string_status_is_not_coerced(self):
        with self.assertRaises(UsageLogError):
            make_record(usage_status="KNOWN")


class RecordValidationTests(unittest.TestCase):
    def test_valid_known_record(self):
        record = make_record()
        self.assertEqual(record.invocation_id, "inv-1")
        self.assertEqual(record.input_tokens, 100)
        self.assertEqual(record.output_tokens, 200)
        self.assertEqual(record.usage_status, UsageObservation.KNOWN)

    def test_identifier_fields_must_be_non_empty(self):
        for field in ("invocation_id", "task_id", "agent_id", "role",
                      "runtime_id", "status"):
            with self.assertRaises(UsageLogError):
                make_record(**{field: "  "})

    def test_duration_may_be_absent(self):
        record = make_record(duration_ms=None)
        self.assertIsNone(record.duration_ms)

    def test_negative_duration_rejected(self):
        with self.assertRaises(UsageLogError):
            make_record(duration_ms=-1)

    def test_boolean_duration_rejected(self):
        with self.assertRaises(UsageLogError):
            make_record(duration_ms=True)

    def test_known_requires_both_tokens(self):
        with self.assertRaises(UsageLogError):
            make_record(input_tokens=None)
        with self.assertRaises(UsageLogError):
            make_record(output_tokens=None)

    def test_unknown_requires_absent_tokens(self):
        record = make_record(
            usage_status=UsageObservation.UNKNOWN,
            input_tokens=None, output_tokens=None)
        self.assertIsNone(record.input_tokens)
        with self.assertRaises(UsageLogError):
            make_record(
                usage_status=UsageObservation.UNKNOWN,
                input_tokens=5, output_tokens=None)
        with self.assertRaises(UsageLogError):
            make_record(
                usage_status=UsageObservation.UNKNOWN,
                input_tokens=None, output_tokens=9)

    def test_unsupported_requires_absent_tokens(self):
        record = make_record(
            usage_status=UsageObservation.UNSUPPORTED,
            input_tokens=None, output_tokens=None, duration_ms=None)
        self.assertEqual(record.usage_status, UsageObservation.UNSUPPORTED)
        with self.assertRaises(UsageLogError):
            make_record(
                usage_status=UsageObservation.UNSUPPORTED,
                input_tokens=5, output_tokens=5)

    def test_negative_tokens_rejected(self):
        with self.assertRaises(UsageLogError):
            make_record(input_tokens=-1)
        with self.assertRaises(UsageLogError):
            make_record(output_tokens=-5)

    def test_boolean_tokens_rejected(self):
        with self.assertRaises(UsageLogError):
            make_record(input_tokens=True)

    def test_record_is_frozen(self):
        record = make_record()
        with self.assertRaises(FrozenInstanceError):
            record.input_tokens = 0
        with self.assertRaises(FrozenInstanceError):
            record.invocation_id = "forged"


class AppliedRevisionsTests(unittest.TestCase):
    def test_default_is_empty_tuple(self):
        self.assertEqual(make_record(applied_revisions=()).applied_revisions, ())

    def test_list_is_converted_to_immutable_tuple(self):
        source = ["rev-1", "rev-2"]
        record = make_record(applied_revisions=source)
        self.assertEqual(record.applied_revisions, ("rev-1", "rev-2"))
        self.assertIsInstance(record.applied_revisions, tuple)
        source.append("rev-3")
        self.assertEqual(record.applied_revisions, ("rev-1", "rev-2"))

    def test_revision_entries_must_be_non_empty_strings(self):
        with self.assertRaises(UsageLogError):
            make_record(applied_revisions=["rev-1", "  "])
        with self.assertRaises(UsageLogError):
            make_record(applied_revisions=[42])

    def test_raw_string_revisions_rejected(self):
        with self.assertRaises(UsageLogError):
            make_record(applied_revisions="rev-1")

    def test_mutable_entries_cannot_back_mutate_record(self):
        record = make_record(applied_revisions=["rev-1"])
        with self.assertRaises(AttributeError):
            record.applied_revisions.append("rev-2")
        self.assertEqual(record.applied_revisions, ("rev-1",))


class AppendTests(unittest.TestCase):
    def test_non_record_rejected(self):
        log = UsageLog()
        for bad in (None, "record", {"invocation_id": "inv-1"}, 42):
            with self.assertRaises(UsageLogError):
                log.append(bad)
        self.assertEqual(log.snapshot(), ())

    def test_single_record_stored_as_same_object(self):
        log = UsageLog()
        record = make_record()
        log.append(record)
        self.assertIs(log.snapshot()[0], record)

    def test_arrival_order_preserved(self):
        log = UsageLog()
        records = [make_record(invocation_id=f"inv-{i}") for i in range(3)]
        for record in records:
            log.append(record)
        self.assertEqual(log.snapshot(), tuple(records))

    def test_invocation_id_is_preserved_exactly(self):
        log = UsageLog()
        log.append(make_record(invocation_id="cmd-123"))
        self.assertEqual(log.snapshot()[0].invocation_id, "cmd-123")


class SnapshotDetachedTests(unittest.TestCase):
    def test_snapshot_is_detached_tuple(self):
        log = UsageLog()
        log.append(make_record())
        snapshot = log.snapshot()
        self.assertIsInstance(snapshot, tuple)
        with self.assertRaises(AttributeError):
            snapshot.append(make_record(invocation_id="inv-2"))
        self.assertEqual(len(log.snapshot()), 1)

    def test_reads_do_not_mutate_store(self):
        log = UsageLog()
        log.append(make_record())
        first = log.snapshot()
        log.since(0)
        self.assertEqual(log.snapshot(), first)


class SinceCursorTests(unittest.TestCase):
    def _filled(self):
        log = UsageLog()
        records = [make_record(invocation_id=f"inv-{i}") for i in range(3)]
        for record in records:
            log.append(record)
        return log, records

    def test_since_returns_records_after_cursor(self):
        log, records = self._filled()
        self.assertEqual(log.since(0), (records[1], records[2]))

    def test_since_at_tail_returns_empty(self):
        log, _ = self._filled()
        self.assertEqual(log.since(2), ())

    def test_since_beyond_tail_returns_empty(self):
        log, _ = self._filled()
        self.assertEqual(log.since(99), ())

    def test_since_rejects_invalid_cursors(self):
        log, _ = self._filled()
        for bad in (-1, "0", 1.5, True):
            with self.assertRaises(UsageLogError):
                log.since(bad)

    def test_empty_log_reads_are_empty(self):
        log = UsageLog()
        self.assertEqual(log.snapshot(), ())
        self.assertEqual(log.since(0), ())


class AppendOnlyTests(unittest.TestCase):
    def test_log_exposes_no_mutator_api(self):
        log = UsageLog()
        for name in ("clear", "delete", "remove", "pop", "update",
                     "replace", "reset", "truncate", "rewind", "extend",
                     "insert"):
            self.assertFalse(hasattr(log, name), name)

    def test_log_exposes_no_adjudication_surface(self):
        # 不推断 invocation 存在性/成败/生命周期 —— 只有记录与读取
        log = UsageLog()
        for name in ("invocation_exists", "is_success", "lifecycle",
                     "state", "unknown_count", "project", "version"):
            self.assertFalse(hasattr(log, name), name)

    def test_duplicate_invocation_ids_are_not_deduped(self):
        log = UsageLog()
        first = make_record(invocation_id="inv-dup")
        second = make_record(invocation_id="inv-dup")
        log.append(first)
        log.append(second)
        self.assertEqual(len(log.snapshot()), 2)


class PerInvocationTests(unittest.TestCase):
    def test_invocations_are_independent(self):
        log = UsageLog()
        a = make_record(invocation_id="inv-a", input_tokens=10,
                        output_tokens=20)
        b = make_record(invocation_id="inv-b", input_tokens=30,
                        output_tokens=40)
        log.append(a)
        log.append(b)
        snapshot = log.snapshot()
        self.assertEqual(
            [(r.invocation_id, r.input_tokens) for r in snapshot],
            [("inv-a", 10), ("inv-b", 30)])

    def test_same_task_multiple_invocations_not_aggregated(self):
        log = UsageLog()
        for i in range(3):
            log.append(make_record(
                invocation_id=f"inv-{i}", task_id="task-1",
                input_tokens=100 + i, output_tokens=200 + i))
        snapshot = log.snapshot()
        self.assertEqual(len(snapshot), 3)
        self.assertEqual(
            {r.invocation_id for r in snapshot}, {"inv-0", "inv-1", "inv-2"})
        self.assertEqual(
            [r.input_tokens for r in snapshot], [100, 101, 102])


class TriStateSemanticsTests(unittest.TestCase):
    def test_absent_record_is_not_unknown_record(self):
        # record absent ≠ record exists + usage UNKNOWN：
        # 空 store 的 snapshot 是零记录，绝不代缺席编造 UNKNOWN 记录
        empty = UsageLog()
        self.assertEqual(empty.snapshot(), ())
        observed = UsageLog()
        observed.append(make_record(
            usage_status=UsageObservation.UNKNOWN,
            input_tokens=None, output_tokens=None))
        self.assertEqual(len(observed.snapshot()), 1)
        self.assertEqual(
            observed.snapshot()[0].usage_status, UsageObservation.UNKNOWN)

    def test_unknown_stays_unknown(self):
        log = UsageLog()
        log.append(make_record(
            usage_status=UsageObservation.UNKNOWN,
            input_tokens=None, output_tokens=None))
        stored = log.snapshot()[0]
        self.assertIsNone(stored.input_tokens)
        self.assertIsNone(stored.output_tokens)
        self.assertEqual(stored.usage_status, UsageObservation.UNKNOWN)

    def test_unsupported_stays_unsupported(self):
        log = UsageLog()
        log.append(make_record(
            usage_status=UsageObservation.UNSUPPORTED,
            input_tokens=None, output_tokens=None))
        self.assertEqual(
            log.snapshot()[0].usage_status, UsageObservation.UNSUPPORTED)

    def test_known_numbers_are_preserved_exactly(self):
        log = UsageLog()
        log.append(make_record(input_tokens=123, output_tokens=456))
        stored = log.snapshot()[0]
        self.assertEqual((stored.input_tokens, stored.output_tokens),
                         (123, 456))

    def test_mixed_modalities_coexist(self):
        log = UsageLog()
        log.append(make_record(invocation_id="inv-known"))
        log.append(make_record(
            invocation_id="inv-unknown",
            usage_status=UsageObservation.UNKNOWN,
            input_tokens=None, output_tokens=None))
        log.append(make_record(
            invocation_id="inv-unsupported",
            usage_status=UsageObservation.UNSUPPORTED,
            input_tokens=None, output_tokens=None))
        self.assertEqual(
            [r.usage_status for r in log.snapshot()],
            [UsageObservation.KNOWN, UsageObservation.UNKNOWN,
             UsageObservation.UNSUPPORTED])


class ConcurrencyTests(unittest.TestCase):
    def test_concurrent_appends_are_not_lost(self):
        log = UsageLog()

        def worker(worker_index):
            records = [
                make_record(
                    invocation_id=f"inv-{worker_index}-{j}",
                    input_tokens=worker_index, output_tokens=j)
                for j in range(25)
            ]
            for record in records:
                log.append(record)
            return records

        with ThreadPoolExecutor(max_workers=8) as pool:
            batches = list(pool.map(worker, range(8)))

        appended = [record for batch in batches for record in batch]
        snapshot = log.snapshot()
        self.assertEqual(len(snapshot), 200)
        self.assertEqual(
            {id(record) for record in snapshot},
            {id(record) for record in appended})

    def test_concurrent_reads_observe_monotonic_prefixes(self):
        log = UsageLog()
        lengths = []

        def reader():
            for _ in range(200):
                lengths.append(len(log.snapshot()))

        thread = threading.Thread(target=reader)
        thread.start()
        try:
            for i in range(100):
                log.append(make_record(invocation_id=f"inv-{i}"))
        finally:
            thread.join()
        self.assertTrue(lengths)
        self.assertEqual(lengths, sorted(lengths))
        self.assertLessEqual(max(lengths), 100)


class ImportBoundaryTests(unittest.TestCase):
    def test_module_import_roots_are_stdlib_only(self):
        source = (SCRIPTS / "usage_log.py").read_text(encoding="utf-8")
        roots = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.ImportFrom):
                roots.add(node.module.split(".")[0])
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    roots.add(alias.name.split(".")[0])
        self.assertEqual(
            roots, {"__future__", "dataclasses", "enum", "threading"})

    def test_module_does_not_reference_forbidden_domains(self):
        source = (SCRIPTS / "usage_log.py").read_text(encoding="utf-8")
        for token in ("control_boundary", "control_journal", "event_index",
                      "trace_projector", "cockpit", "execution_observation",
                      "ExecutionEvent", "ObservationSink", "subprocess"):
            self.assertNotIn(token, source)

    def test_module_is_runtime_neutral(self):
        source = (SCRIPTS / "usage_log.py").read_text(encoding="utf-8")
        lowered = source.lower()
        for token in ("claude", "codex", "deepseek", "openai", "anthropic",
                      "gemini", "tiny-agents", "tiny_agents"):
            self.assertNotIn(token, lowered)

    def test_public_surface_is_minimal(self):
        self.assertEqual(
            usage_log.__all__,
            ("UsageLog", "UsageLogError", "UsageObservation", "UsageRecord"))


if __name__ == "__main__":
    unittest.main()
