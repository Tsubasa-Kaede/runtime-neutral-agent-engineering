"""CU-CTRL-2 tests: append-only ControlJournal（受限 writer capability）。

覆盖冻结契约：
- 封闭 Control fact 词表（8 值）与跨域拒绝
- journal-local seq（首 0、连续、caller 不可指定）
- identity 保真（command_id/execution_id/execution_version 原样保存）
- append-only（无 mutator API、fact 不可变、读取为 detached view）
- 三类 writer capability 权限矩阵（RevisionWriter 只可 REVISION_APPLIED）
- 无裁决 / 无 version 递增 / 无 replay 去重 / 无 lifecycle 投影
- 与执行观察事件域、runtime 域的词汇分离
- 并发 append 线性化（seq 唯一、无丢失、连续）
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

from control_journal import (  # noqa: E402
    BoundaryWriter,
    CompositionWriter,
    ControlFactType,
    ControlJournal,
    ControlJournalError,
    JournalFact,
    RevisionWriter,
)

FROZEN_FACTS = {
    "PAUSE_REQUESTED", "PAUSE_CONFIRMED", "RESUME_REQUESTED",
    "REVISE_REQUESTED", "REVISION_APPLIED", "ABORT_REQUESTED",
    "ABORT_CONFIRMED", "ABORT_SUPERSEDED",
}

BOUNDARY_FACTS = FROZEN_FACTS - {"REVISION_APPLIED"}
COMPOSITION_FACTS = {"ABORT_CONFIRMED", "ABORT_SUPERSEDED"}

FORBIDDEN_MUTATORS = (
    "clear", "delete", "remove", "pop", "update", "replace",
    "reset", "truncate", "rewind", "extend", "insert",
)


def make_journal_with_boundary_writer():
    journal = ControlJournal()
    return journal, journal.boundary_writer()


class FactVocabularyTests(unittest.TestCase):
    def test_frozen_fact_vocabulary_is_exact(self):
        self.assertEqual(
            {member.name for member in ControlFactType}, FROZEN_FACTS)

    def test_invented_fact_names_are_not_members(self):
        for name in ("CANCELLED", "KILLED", "PROCESS_STOPPED",
                     "INVOCATION_CANCELLED", "RUNTIME_ABORTED", "EDIT",
                     "STOP", "FORCE_STOP"):
            self.assertFalse(hasattr(ControlFactType, name), name)

    def test_raw_string_fact_type_is_not_coerced(self):
        _, writer = make_journal_with_boundary_writer()
        with self.assertRaises(ControlJournalError):
            writer.append(
                fact_type="PAUSE_REQUESTED", execution_id="exec-1",
                command_id="cmd-1", execution_version=0)

    def test_command_vocabulary_does_not_leak_in(self):
        from control_boundary import ControlCommandType
        _, writer = make_journal_with_boundary_writer()
        with self.assertRaises(ControlJournalError):
            writer.append(
                fact_type=ControlCommandType.PAUSE, execution_id="exec-1",
                command_id="cmd-1", execution_version=0)

    def test_observation_domain_vocabulary_does_not_leak_in(self):
        from execution_observation import ExecutionEventType
        _, writer = make_journal_with_boundary_writer()
        with self.assertRaises(ControlJournalError):
            writer.append(
                fact_type=next(iter(ExecutionEventType)),
                execution_id="exec-1", command_id="cmd-1",
                execution_version=0)


class JournalFactValidationTests(unittest.TestCase):
    def _fact(self, **overrides):
        fields = dict(
            seq=0, fact_type=ControlFactType.PAUSE_REQUESTED,
            execution_id="exec-1", command_id="cmd-1",
            execution_version=0, payload=None,
        )
        fields.update(overrides)
        return JournalFact(**fields)

    def test_negative_seq_rejected(self):
        with self.assertRaises(ControlJournalError):
            self._fact(seq=-1)

    def test_boolean_seq_rejected(self):
        with self.assertRaises(ControlJournalError):
            self._fact(seq=True)

    def test_non_int_seq_rejected(self):
        with self.assertRaises(ControlJournalError):
            self._fact(seq="0")

    def test_empty_execution_id_rejected(self):
        with self.assertRaises(ControlJournalError):
            self._fact(execution_id="   ")

    def test_empty_command_id_rejected(self):
        with self.assertRaises(ControlJournalError):
            self._fact(command_id="")

    def test_negative_execution_version_rejected(self):
        with self.assertRaises(ControlJournalError):
            self._fact(execution_version=-1)

    def test_boolean_execution_version_rejected(self):
        with self.assertRaises(ControlJournalError):
            self._fact(execution_version=True)

    def test_non_enum_fact_type_rejected_at_fact_level(self):
        with self.assertRaises(ControlJournalError):
            self._fact(fact_type="PAUSE_REQUESTED")

    def test_non_mapping_payload_rejected(self):
        with self.assertRaises(ControlJournalError):
            self._fact(payload=["not", "a", "mapping"])

    def test_mutable_list_payload_value_rejected(self):
        with self.assertRaises(ControlJournalError):
            self._fact(payload={"items": ["a"]})

    def test_mutable_dict_payload_value_rejected(self):
        with self.assertRaises(ControlJournalError):
            self._fact(payload={"nested": {"k": "v"}})

    def test_scalar_payload_values_accepted(self):
        fact = self._fact(payload={"revision_id": "rev-1", "count": 2,
                                   "ratio": 0.5, "flag": True, "note": None})
        self.assertEqual(fact.payload["revision_id"], "rev-1")
        self.assertIsNone(fact.payload["note"])


class SequenceTests(unittest.TestCase):
    def test_first_seq_is_zero_and_next_is_increment(self):
        _, writer = make_journal_with_boundary_writer()
        first = writer.append(
            fact_type=ControlFactType.PAUSE_REQUESTED,
            execution_id="exec-1", command_id="cmd-1", execution_version=0)
        second = writer.append(
            fact_type=ControlFactType.PAUSE_CONFIRMED,
            execution_id="exec-1", command_id="cmd-1", execution_version=1)
        self.assertEqual(first.seq, 0)
        self.assertEqual(second.seq, 1)

    def test_sequence_is_strictly_monotonic_and_contiguous(self):
        _, writer = make_journal_with_boundary_writer()
        seqs = [writer.append(
            fact_type=ControlFactType.RESUME_REQUESTED,
            execution_id="exec-1", command_id=f"cmd-{i}",
            execution_version=i).seq for i in range(100)]
        self.assertEqual(seqs, list(range(100)))

    def test_seq_is_independent_of_command_id_and_version(self):
        _, writer = make_journal_with_boundary_writer()
        fact = writer.append(
            fact_type=ControlFactType.PAUSE_REQUESTED,
            execution_id="exec-9", command_id="cmd-123",
            execution_version=7)
        self.assertEqual(fact.seq, 0)
        self.assertEqual(fact.command_id, "cmd-123")
        self.assertEqual(fact.execution_version, 7)

    def test_caller_cannot_specify_seq(self):
        _, writer = make_journal_with_boundary_writer()
        with self.assertRaises(TypeError):
            writer.append(
                seq=5, fact_type=ControlFactType.PAUSE_REQUESTED,
                execution_id="exec-1", command_id="cmd-1",
                execution_version=0)

    def test_direct_fact_construction_does_not_consume_seq(self):
        journal, writer = make_journal_with_boundary_writer()
        JournalFact(
            seq=99, fact_type=ControlFactType.ABORT_REQUESTED,
            execution_id="elsewhere", command_id="other", execution_version=0)
        fact = writer.append(
            fact_type=ControlFactType.PAUSE_REQUESTED,
            execution_id="exec-1", command_id="cmd-1", execution_version=0)
        self.assertEqual(fact.seq, 0)
        self.assertEqual(journal.snapshot(), (fact,))


class IdentityPreservationTests(unittest.TestCase):
    def test_caller_supplied_command_id_is_preserved_exactly(self):
        _, writer = make_journal_with_boundary_writer()
        fact = writer.append(
            fact_type=ControlFactType.PAUSE_REQUESTED,
            execution_id="exec-1", command_id="cmd-123", execution_version=0)
        self.assertEqual(fact.command_id, "cmd-123")

    def test_execution_id_is_preserved(self):
        _, writer = make_journal_with_boundary_writer()
        fact = writer.append(
            fact_type=ControlFactType.ABORT_REQUESTED,
            execution_id="exec-42", command_id="cmd-1", execution_version=3)
        self.assertEqual(fact.execution_id, "exec-42")

    def test_execution_version_is_recorded_never_incremented(self):
        _, writer = make_journal_with_boundary_writer()
        for _ in range(3):
            fact = writer.append(
                fact_type=ControlFactType.PAUSE_REQUESTED,
                execution_id="exec-1", command_id="cmd-1", execution_version=3)
            self.assertEqual(fact.execution_version, 3)


class AppendOnlyTests(unittest.TestCase):
    def test_journal_exposes_no_mutator_api(self):
        journal = ControlJournal()
        for name in FORBIDDEN_MUTATORS:
            self.assertFalse(hasattr(journal, name), name)

    def test_writers_expose_no_mutator_api(self):
        journal = ControlJournal()
        for maker in (journal.boundary_writer, journal.revision_writer,
                      journal.composition_writer):
            writer = maker()
            for name in FORBIDDEN_MUTATORS:
                self.assertFalse(hasattr(writer, name), name)

    def test_fact_fields_are_frozen(self):
        _, writer = make_journal_with_boundary_writer()
        fact = writer.append(
            fact_type=ControlFactType.PAUSE_REQUESTED,
            execution_id="exec-1", command_id="cmd-1", execution_version=0)
        with self.assertRaises(FrozenInstanceError):
            fact.seq = 5
        with self.assertRaises(FrozenInstanceError):
            fact.command_id = "other"

    def test_payload_is_immutable_view(self):
        journal = ControlJournal()
        writer = journal.revision_writer()
        fact = writer.append(
            fact_type=ControlFactType.REVISION_APPLIED,
            execution_id="exec-1", command_id="cmd-1", execution_version=1,
            payload={"revision_id": "rev-1"})
        with self.assertRaises(TypeError):
            fact.payload["revision_id"] = "forged"

    def test_source_payload_dict_is_defensively_copied(self):
        journal = ControlJournal()
        writer = journal.revision_writer()
        source = {"revision_id": "rev-1"}
        fact = writer.append(
            fact_type=ControlFactType.REVISION_APPLIED,
            execution_id="exec-1", command_id="cmd-1", execution_version=1,
            payload=source)
        source["revision_id"] = "mutated-after-append"
        self.assertEqual(fact.payload["revision_id"], "rev-1")

    def test_snapshot_is_a_detached_immutable_tuple(self):
        journal, writer = make_journal_with_boundary_writer()
        fact = writer.append(
            fact_type=ControlFactType.PAUSE_REQUESTED,
            execution_id="exec-1", command_id="cmd-1", execution_version=0)
        snapshot = journal.snapshot()
        self.assertEqual(snapshot, (fact,))
        self.assertIsInstance(snapshot, tuple)
        self.assertEqual(journal.snapshot(), (fact,))

    def test_mutating_returned_snapshot_cannot_touch_journal(self):
        journal, writer = make_journal_with_boundary_writer()
        for i in range(3):
            writer.append(
                fact_type=ControlFactType.PAUSE_REQUESTED,
                execution_id="exec-1", command_id=f"cmd-{i}",
                execution_version=0)
        snapshot = journal.snapshot()
        with self.assertRaises(AttributeError):
            snapshot.append("forged")
        self.assertEqual(len(journal.snapshot()), 3)

    def test_default_payload_is_empty_mapping(self):
        _, writer = make_journal_with_boundary_writer()
        fact = writer.append(
            fact_type=ControlFactType.PAUSE_REQUESTED,
            execution_id="exec-1", command_id="cmd-1", execution_version=0)
        self.assertEqual(dict(fact.payload), {})


class WriterCapabilityTests(unittest.TestCase):
    def test_boundary_writer_may_append_all_non_revision_facts(self):
        journal = ControlJournal()
        writer = journal.boundary_writer()
        self.assertIsInstance(writer, BoundaryWriter)
        for i, name in enumerate(sorted(BOUNDARY_FACTS)):
            fact = writer.append(
                fact_type=ControlFactType[name], execution_id="exec-1",
                command_id=f"cmd-{i}", execution_version=0)
            self.assertEqual(fact.fact_type.value, name)

    def test_boundary_writer_cannot_append_revision_applied(self):
        journal = ControlJournal()
        writer = journal.boundary_writer()
        with self.assertRaises(ControlJournalError):
            writer.append(
                fact_type=ControlFactType.REVISION_APPLIED,
                execution_id="exec-1", command_id="cmd-1",
                execution_version=1, payload={"revision_id": "rev-1"})

    def test_revision_writer_may_append_revision_applied(self):
        journal = ControlJournal()
        writer = journal.revision_writer()
        self.assertIsInstance(writer, RevisionWriter)
        fact = writer.append(
            fact_type=ControlFactType.REVISION_APPLIED,
            execution_id="exec-1", command_id="cmd-1", execution_version=1,
            payload={"revision_id": "rev-1"})
        self.assertEqual(fact.fact_type, ControlFactType.REVISION_APPLIED)

    def test_revision_writer_cannot_append_any_other_fact(self):
        journal = ControlJournal()
        writer = journal.revision_writer()
        for name in sorted(BOUNDARY_FACTS):
            with self.assertRaises(ControlJournalError):
                writer.append(
                    fact_type=ControlFactType[name], execution_id="exec-1",
                    command_id="cmd-1", execution_version=0)

    def test_composition_writer_may_append_abort_outcome_facts(self):
        journal = ControlJournal()
        writer = journal.composition_writer()
        self.assertIsInstance(writer, CompositionWriter)
        for i, name in enumerate(sorted(COMPOSITION_FACTS)):
            fact = writer.append(
                fact_type=ControlFactType[name], execution_id="exec-1",
                command_id=f"cmd-{i}", execution_version=0)
            self.assertEqual(fact.fact_type.value, name)

    def test_composition_writer_cannot_append_revision_applied(self):
        journal = ControlJournal()
        writer = journal.composition_writer()
        with self.assertRaises(ControlJournalError):
            writer.append(
                fact_type=ControlFactType.REVISION_APPLIED,
                execution_id="exec-1", command_id="cmd-1",
                execution_version=1)

    def test_composition_writer_cannot_append_boundary_echo_facts(self):
        journal = ControlJournal()
        writer = journal.composition_writer()
        for name in sorted(BOUNDARY_FACTS - COMPOSITION_FACTS):
            with self.assertRaises(ControlJournalError):
                writer.append(
                    fact_type=ControlFactType[name], execution_id="exec-1",
                    command_id="cmd-1", execution_version=0)

    def test_all_writers_share_one_sequence_stream(self):
        journal = ControlJournal()
        boundary = journal.boundary_writer()
        revision = journal.revision_writer()
        composition = journal.composition_writer()
        first = boundary.append(
            fact_type=ControlFactType.PAUSE_REQUESTED,
            execution_id="exec-1", command_id="cmd-1", execution_version=0)
        second = revision.append(
            fact_type=ControlFactType.REVISION_APPLIED,
            execution_id="exec-1", command_id="cmd-2", execution_version=1,
            payload={"revision_id": "rev-1"})
        third = composition.append(
            fact_type=ControlFactType.ABORT_SUPERSEDED,
            execution_id="exec-1", command_id="cmd-3", execution_version=2)
        self.assertEqual((first.seq, second.seq, third.seq), (0, 1, 2))


class ReadAPITests(unittest.TestCase):
    def _filled_journal(self):
        journal, writer = make_journal_with_boundary_writer()
        for i in range(3):
            writer.append(
                fact_type=ControlFactType.PAUSE_REQUESTED,
                execution_id="exec-1", command_id=f"cmd-{i}",
                execution_version=i)
        return journal

    def test_snapshot_preserves_seq_order(self):
        journal = self._filled_journal()
        self.assertEqual([fact.seq for fact in journal.snapshot()], [0, 1, 2])

    def test_since_returns_facts_strictly_after_cursor(self):
        journal = self._filled_journal()
        self.assertEqual(
            [fact.seq for fact in journal.since(0)], [1, 2])

    def test_since_at_tail_returns_empty(self):
        journal = self._filled_journal()
        self.assertEqual(journal.since(2), ())

    def test_since_beyond_tail_returns_empty(self):
        journal = self._filled_journal()
        self.assertEqual(journal.since(99), ())

    def test_since_rejects_invalid_cursors(self):
        journal = self._filled_journal()
        for bad in (-1, "0", 1.5, True):
            with self.assertRaises(ControlJournalError):
                journal.since(bad)

    def test_empty_journal_reads_are_empty(self):
        journal = ControlJournal()
        self.assertEqual(journal.snapshot(), ())
        self.assertEqual(journal.since(0), ())

    def test_reads_do_not_mutate_journal(self):
        journal = self._filled_journal()
        journal.snapshot()
        journal.since(0)
        journal.since(2)
        self.assertEqual(len(journal.snapshot()), 3)


class NoAdjudicationTests(unittest.TestCase):
    def test_duplicate_identity_appends_are_both_recorded(self):
        # 无 ALREADY_REQUESTED 裁决：同 (execution_id, command_id) 重复
        # append 得到两条事实；去重/replay 缓存属于 ControlBoundary。
        journal, writer = make_journal_with_boundary_writer()
        first = writer.append(
            fact_type=ControlFactType.PAUSE_REQUESTED,
            execution_id="exec-1", command_id="cmd-dup", execution_version=0)
        second = writer.append(
            fact_type=ControlFactType.PAUSE_REQUESTED,
            execution_id="exec-1", command_id="cmd-dup", execution_version=0)
        self.assertEqual((first.seq, second.seq), (0, 1))
        self.assertEqual(len(journal.snapshot()), 2)

    def test_append_has_no_adjudication_parameters(self):
        # 无 expected_version 参数：journal 不做 STALE_VERSION 判断。
        _, writer = make_journal_with_boundary_writer()
        with self.assertRaises(TypeError):
            writer.append(
                fact_type=ControlFactType.PAUSE_REQUESTED,
                execution_id="exec-1", command_id="cmd-1",
                execution_version=0, expected_version=3)

    def test_journal_exposes_no_state_or_adjudication_surface(self):
        journal = ControlJournal()
        for name in ("lifecycle", "version", "current_version", "state",
                     "replay", "dedupe", "adjudicate", "can_pause",
                     "is_paused", "project"):
            self.assertFalse(hasattr(journal, name), name)


class SeparationDisciplineTests(unittest.TestCase):
    def test_module_import_roots_are_stdlib_only(self):
        source = (SCRIPTS / "control_journal.py").read_text(encoding="utf-8")
        roots = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.ImportFrom):
                roots.add(node.module.split(".")[0])
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    roots.add(alias.name.split(".")[0])
        self.assertEqual(
            roots - {"__future__"},
            {"collections", "dataclasses", "enum", "threading", "types"})

    def test_module_does_not_reference_observation_domain(self):
        source = (SCRIPTS / "control_journal.py").read_text(encoding="utf-8")
        for token in ("ExecutionEvent", "execution_observation",
                      "ObservationSink", "EventType"):
            self.assertNotIn(token, source)

    def test_module_is_runtime_neutral(self):
        source = (SCRIPTS / "control_journal.py").read_text(encoding="utf-8")
        lowered = source.lower()
        for token in ("claude", "codex", "deepseek", "openai", "anthropic",
                      "gemini", "tiny-agents", "tiny_agents", "subprocess"):
            self.assertNotIn(token, lowered)


class ConcurrencyTests(unittest.TestCase):
    def test_concurrent_appends_are_linearizable(self):
        journal = ControlJournal()
        writer = journal.boundary_writer()
        fact_choices = (ControlFactType.PAUSE_REQUESTED,
                        ControlFactType.ABORT_REQUESTED,
                        ControlFactType.RESUME_REQUESTED)

        def worker(worker_index):
            return [writer.append(
                fact_type=fact_choices[(worker_index + j) % len(fact_choices)],
                execution_id="exec-c",
                command_id=f"cmd-{worker_index}-{j}",
                execution_version=1) for j in range(25)]

        with ThreadPoolExecutor(max_workers=8) as pool:
            batches = list(pool.map(worker, range(8)))

        returned = [fact for batch in batches for fact in batch]
        snapshot = journal.snapshot()
        self.assertEqual(len(snapshot), 200)
        self.assertEqual(
            sorted(fact.seq for fact in snapshot), list(range(200)))
        seqs = [fact.seq for fact in returned]
        self.assertEqual(len(set(seqs)), len(seqs))  # 无重复 seq
        self.assertEqual(
            {id(fact) for fact in snapshot},
            {id(fact) for fact in returned})  # 每个返回值都是真实条目

    def test_concurrent_reads_observe_contiguous_prefixes(self):
        journal, writer = make_journal_with_boundary_writer()
        samples = []
        stop = threading.Event()

        def reader():
            while not stop.is_set():
                samples.append([fact.seq for fact in journal.snapshot()])

        thread = threading.Thread(target=reader)
        thread.start()
        try:
            for i in range(200):
                writer.append(
                    fact_type=ControlFactType.RESUME_REQUESTED,
                    execution_id="exec-r", command_id=f"cmd-{i}",
                    execution_version=0)
        finally:
            stop.set()
            thread.join()
        self.assertTrue(samples)
        for seqs in samples:
            self.assertEqual(seqs, list(range(len(seqs))))


if __name__ == "__main__":
    unittest.main()
