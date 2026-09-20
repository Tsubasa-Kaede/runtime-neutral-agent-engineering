"""CU-CONTEXT-2 Collaboration Memory 测试面。

覆盖矩阵（设计 PART 24 + 实施授权 §二十）：
- Candidate / Record 区分（mint 晋升面；检索结构性拒绝 Candidate）；
- Scope（恰 SESSION / USER；RUN / INVOCATION 排除；非法 scope 拒绝）；
- Identity（内容寻址确定性；(scope, digest) 复合身份；跨 scope 同
  内容不同身份；描述性 producer 不入身份；零新随机原语）；
- Cross-run 并存（同 task_id 异内容两记录合法并存，检索容忍并列）；
- Validity（INVALID = 构造期拒绝；零运行态失效面）；
- Immutability（frozen + 可哈希 + 集合去重）；
- Provenance（描述性视图直读；task_id = 关联键非全局身份）；
- Retention 资格（恰 COMPLETED 终态可铸；非终态 / 缺事实拒绝；
  usage 缺席诚实零聚合）；
- Failure 排除（FAILED / ABORTED 拒绝自动保留）；
- 转换边界（duck 读；源对象零改动；纯函数）；
- Retrieval（封闭结构过滤 AND；输入序保持；空 Memory 合法；
  非法过滤拒绝）；
- 静态墙：零 Context 依赖、零持久化 IO、零 runtime 知识、零
  engine/observation 调用、零组合分组语义、零新身份原语、零时钟、
  零失效词表、闭面 __all__。

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

import cockpit_memory
from cockpit_memory import (  # noqa: E402
    MemoryCandidate,
    MemoryModelError,
    MemoryProvenance,
    MemoryRecord,
    MemoryScope,
    MemorySourceKind,
    candidate_from_run_summary,
    mint_record,
    retrieve_records,
)

MODULE_SOURCE = Path(
    cockpit_memory.__file__).read_text(encoding="utf-8")


def _summary(task="summarize the module", task_id="task-a",
             status="COMPLETED", final_preview="done: 3 findings",
             steps=(("architect", "runtime-a"), ("coder", "runtime-b")),
             usage=None):
    """ConversationRecord 形鸭（九字段同形；呈现视图零 import）。"""
    return SimpleNamespace(
        task=task, steps=steps, status=status, plan=(), groups=(),
        member_ids=(), final_preview=final_preview,
        usage=usage if usage is not None else SimpleNamespace(
            known_input=10, known_output=20, unknown_count=1,
            unsupported_count=0),
        task_id=task_id)


def _record(task_id="task-a", final_preview="done: 3 findings",
             scope=MemoryScope.SESSION, task="summarize the module"):
    """便捷铸造：summary → candidate → record（显式两步，非跳步）。"""
    return mint_record(
        candidate_from_run_summary(_summary(
            task=task, task_id=task_id, final_preview=final_preview)),
        scope)


class CandidateRecordDistinctionTests(unittest.TestCase):
    """Candidate ≠ Record：晋升唯一面 = mint；检索不认 Candidate。"""

    def test_mint_wraps_candidate_with_scope(self):
        candidate = candidate_from_run_summary(_summary())
        record = mint_record(candidate, MemoryScope.SESSION)
        self.assertIs(record.candidate, candidate)
        self.assertIs(record.scope, MemoryScope.SESSION)

    def test_candidate_is_not_record_and_vice_versa(self):
        candidate = candidate_from_run_summary(_summary())
        record = mint_record(candidate, MemoryScope.SESSION)
        self.assertNotIsInstance(candidate, MemoryRecord)
        self.assertNotIsInstance(record, MemoryCandidate)

    def test_retrieval_rejects_candidates_as_facts(self):
        candidate = candidate_from_run_summary(_summary())
        with self.assertRaises(MemoryModelError):
            retrieve_records((candidate,))


class ScopeTests(unittest.TestCase):
    """作用域封闭恰两值；非法 scope 构造 / 铸造拒绝。"""

    def test_scope_vocabulary_exactly_two(self):
        self.assertEqual(
            {member.name for member in MemoryScope},
            {"SESSION", "USER"})
        self.assertNotIn("RUN", MemoryScope.__members__)
        self.assertNotIn("INVOCATION", MemoryScope.__members__)

    def test_mint_rejects_non_scope_values(self):
        candidate = candidate_from_run_summary(_summary())
        for bad in ("SESSION", None, 1):
            with self.assertRaises(MemoryModelError):
                mint_record(candidate, bad)


class IdentityTests(unittest.TestCase):
    """(scope, 内容寻址散列)：确定性 / 复合性 / 描述性排除。"""

    def test_identity_deterministic_and_derived(self):
        first = _record()
        second = _record()
        self.assertEqual(first.identity, second.identity)
        self.assertEqual(first, second)
        self.assertRegex(
            first.identity[1], r"^memory_[0-9a-f]{12}$")

    def test_scope_is_identity_component_not_content_hash(self):
        candidate = candidate_from_run_summary(_summary())
        session = mint_record(candidate, MemoryScope.SESSION)
        user = mint_record(candidate, MemoryScope.USER)
        # 同内容同散列；但身份不同——内容散列绝不单独作跨 scope 身份
        self.assertEqual(session.identity[1], user.identity[1])
        self.assertNotEqual(session.identity, user.identity)
        self.assertNotEqual(session, user)

    def test_content_discrimination(self):
        self.assertNotEqual(
            _record(final_preview="run one result").identity,
            _record(final_preview="run two result").identity)

    def test_descriptive_producer_not_in_identity(self):
        plain = candidate_from_run_summary(_summary())
        attributed = candidate_from_run_summary(
            _summary(), producer="collaboration-host")
        self.assertEqual(
            mint_record(plain, MemoryScope.SESSION).identity,
            mint_record(attributed, MemoryScope.SESSION).identity)


class CrossRunCoexistenceTests(unittest.TestCase):
    """同 task 重跑异果合法并存；task_id 仅关联键。"""

    def test_same_task_id_different_outcomes_coexist(self):
        first = _record(final_preview="run one result")
        second = _record(final_preview="run two result")
        self.assertEqual(
            first.candidate.task_id, second.candidate.task_id)
        self.assertNotEqual(first.identity, second.identity)
        found = retrieve_records((first, second), task_id="task-a")
        self.assertEqual(len(found), 2)  # 并列容忍，绝不合并
        self.assertEqual(set(found), {first, second})

    def test_task_id_is_correlation_not_global_identity(self):
        record = _record(task_id="task-a")
        self.assertEqual(record.provenance.task_id, "task-a")
        self.assertNotEqual(record.identity[1], "task-a")
        self.assertIsInstance(record.identity[0], MemoryScope)


class ValidityTests(unittest.TestCase):
    """INVALID = 仅构造期拒绝；零运行态失效面。"""

    def test_invalid_construction_rejections(self):
        good = candidate_from_run_summary(_summary())
        with self.assertRaises(MemoryModelError):
            MemoryCandidate("RUN_OUTCOME", "t", "COMPLETED", "",
                            good.steps, "task-a", good.usage)  # kind 非枚举
        with self.assertRaises(MemoryModelError):
            MemoryCandidate(MemorySourceKind.RUN_OUTCOME, "", "COMPLETED",
                            "", good.steps, "task-a", good.usage)
        with self.assertRaises(MemoryModelError):
            MemoryCandidate(MemorySourceKind.RUN_OUTCOME, "t", "", "",
                            good.steps, "task-a", good.usage)
        with self.assertRaises(MemoryModelError):
            MemoryCandidate(MemorySourceKind.RUN_OUTCOME, "t", "COMPLETED",
                            None, good.steps, "task-a", good.usage)
        with self.assertRaises(MemoryModelError):
            MemoryCandidate(MemorySourceKind.RUN_OUTCOME, "t", "COMPLETED",
                            "", ["bad"], "task-a", good.usage)  # steps 非 tuple
        with self.assertRaises(MemoryModelError):
            MemoryCandidate(MemorySourceKind.RUN_OUTCOME, "t", "COMPLETED",
                            "", (("architect",),), "task-a", good.usage)
        with self.assertRaises(MemoryModelError):
            MemoryCandidate(MemorySourceKind.RUN_OUTCOME, "t", "COMPLETED",
                            "", good.steps, "", good.usage)
        with self.assertRaises(MemoryModelError):
            MemoryCandidate(MemorySourceKind.RUN_OUTCOME, "t", "COMPLETED",
                            "", good.steps, "task-a", (0, 0, 0))  # 四元缺一
        with self.assertRaises(MemoryModelError):
            MemoryCandidate(MemorySourceKind.RUN_OUTCOME, "t", "COMPLETED",
                            "", good.steps, "task-a", (0, 0, 0, -1))
        with self.assertRaises(MemoryModelError):
            MemoryCandidate(MemorySourceKind.RUN_OUTCOME, "t", "COMPLETED",
                            "", good.steps, "task-a", (0, 0, True, 0))
        with self.assertRaises(MemoryModelError):
            MemoryCandidate(MemorySourceKind.RUN_OUTCOME, "t", "COMPLETED",
                            "", good.steps, "task-a", good.usage,
                            producer=" ")  # 空白 producer
        with self.assertRaises(MemoryModelError):
            MemoryRecord("SESSION", good)               # scope 非枚举
        with self.assertRaises(MemoryModelError):
            MemoryRecord(MemoryScope.SESSION, "candidate")  # 非候选
        with self.assertRaises(MemoryModelError):
            mint_record("candidate", MemoryScope.SESSION)
        with self.assertRaises(MemoryModelError):
            retrieve_records(("record",))

    def test_no_runtime_validity_surface(self):
        record = _record()
        for absent in ("expire", "expired", "ttl", "stale", "supersede",
                       "superseded", "refresh", "touch", "decay"):
            self.assertFalse(hasattr(record, absent), absent)
            self.assertFalse(hasattr(record.candidate, absent), absent)


class ImmutabilityTests(unittest.TestCase):
    """frozen + 可哈希；铸造后不可修改。"""

    def test_frozen_records_and_candidates(self):
        record = _record()
        with self.assertRaises(FrozenInstanceError):
            record.scope = MemoryScope.USER
        with self.assertRaises(FrozenInstanceError):
            record.candidate.task_text = "mutated"

    def test_hashable_and_set_dedup(self):
        first = _record()
        second = _record()
        self.assertEqual(len({first, second}), 1)


class ProvenanceTests(unittest.TestCase):
    """描述性视图直读 candidate 所载；不参与身份。"""

    def test_provenance_view_reads_candidate_facts(self):
        record = _record()
        provenance = record.provenance
        self.assertIsInstance(provenance, MemoryProvenance)
        self.assertIs(provenance.source_kind, MemorySourceKind.RUN_OUTCOME)
        self.assertEqual(provenance.task_id, "task-a")
        self.assertEqual(
            provenance.composition,
            (("architect", "runtime-a"), ("coder", "runtime-b")))
        self.assertEqual(provenance.usage, (10, 20, 1, 0))
        self.assertIsNone(provenance.producer)

    def test_provenance_carries_descriptive_producer(self):
        record = mint_record(
            candidate_from_run_summary(_summary(), producer="host"),
            MemoryScope.SESSION)
        self.assertEqual(record.provenance.producer, "host")


class RetentionEligibilityTests(unittest.TestCase):
    """恰 COMPLETED 终态可铸；非终态 / 缺事实拒绝。"""

    def test_completed_summary_mints_candidate_verbatim(self):
        candidate = candidate_from_run_summary(_summary())
        self.assertIs(candidate.source_kind, MemorySourceKind.RUN_OUTCOME)
        self.assertEqual(candidate.task_text, "summarize the module")
        self.assertEqual(candidate.outcome_status, "COMPLETED")
        self.assertEqual(candidate.final_preview, "done: 3 findings")
        self.assertEqual(candidate.steps,
                         (("architect", "runtime-a"),
                          ("coder", "runtime-b")))
        self.assertEqual(candidate.task_id, "task-a")
        self.assertEqual(candidate.usage, (10, 20, 1, 0))

    def test_usage_absent_means_honest_zero_quad(self):
        candidate = candidate_from_run_summary(
            SimpleNamespace(task="t", steps=(), status="COMPLETED",
                            final_preview="p", usage=None, task_id="task-a"))
        self.assertEqual(candidate.usage, (0, 0, 0, 0))

    def test_non_terminal_rejected(self):
        with self.assertRaises(MemoryModelError):
            candidate_from_run_summary(_summary(status=None))  # 在飞/停驻形
        with self.assertRaises(MemoryModelError):
            candidate_from_run_summary(_summary(status="PARKED"))

    def test_missing_facts_rejected(self):
        with self.assertRaises(MemoryModelError):
            candidate_from_run_summary(_summary(task=" "))
        with self.assertRaises(MemoryModelError):
            candidate_from_run_summary(_summary(task_id=""))


class FailureExclusionTests(unittest.TestCase):
    """失败不自动保留（双先例：资格层拒存失败 + 取证取走即清）。"""

    def test_failed_not_auto_retained(self):
        with self.assertRaises(MemoryModelError):
            candidate_from_run_summary(_summary(status="FAILED"))

    def test_aborted_not_auto_retained(self):
        with self.assertRaises(MemoryModelError):
            candidate_from_run_summary(_summary(status="ABORTED"))


class ConversionBoundaryTests(unittest.TestCase):
    """显式转换边界：duck 读、源零改动、纯函数。"""

    def test_duck_read_leaves_source_untouched(self):
        summary = _summary()
        before = (summary.task, summary.steps, summary.status,
                  summary.final_preview, summary.task_id,
                  summary.usage.known_input)
        candidate_from_run_summary(summary)
        self.assertEqual(
            (summary.task, summary.steps, summary.status,
             summary.final_preview, summary.task_id,
             summary.usage.known_input), before)

    def test_conversion_is_pure(self):
        first = candidate_from_run_summary(_summary())
        second = candidate_from_run_summary(_summary())
        self.assertEqual(first, second)  # 同源两转同候选，零模块级状态


class RetrievalTests(unittest.TestCase):
    """封闭结构过滤 AND；保序；空 Memory 合法；非法过滤拒绝。"""

    def setUp(self):
        self.records = (
            _record(task_id="task-a", final_preview="run one"),
            _record(task_id="task-a", final_preview="run two"),
            _record(task_id="task-b", final_preview="other task"),
        )

    def test_scope_filter(self):
        user_only = (_record(task_id="task-c", scope=MemoryScope.USER),)
        found = retrieve_records(
            self.records + user_only, scope=MemoryScope.USER)
        self.assertEqual(found, user_only)

    def test_task_id_filter_returns_all_coexisting(self):
        found = retrieve_records(self.records, task_id="task-a")
        self.assertEqual(len(found), 2)

    def test_status_filter(self):
        self.assertEqual(
            retrieve_records(self.records, status="COMPLETED"),
            self.records)

    def test_member_filter(self):
        found = retrieve_records(self.records,
                                 member=("coder", "runtime-b"))
        self.assertEqual(found, self.records)
        self.assertEqual(
            retrieve_records(self.records,
                             member=("reviewer", "runtime-b")), ())

    def test_filters_combine_with_and(self):
        found = retrieve_records(
            self.records, scope=MemoryScope.SESSION, task_id="task-a",
            status="COMPLETED", member=("architect", "runtime-a"))
        self.assertEqual(len(found), 2)

    def test_input_order_preserved(self):
        found = retrieve_records(self.records)
        self.assertEqual(found, self.records)  # 铸造序 = 检索序

    def test_empty_memory_is_legal(self):
        self.assertEqual(retrieve_records(()), ())

    def test_invalid_filters_rejected(self):
        with self.assertRaises(MemoryModelError):
            retrieve_records(self.records, scope="SESSION")
        with self.assertRaises(MemoryModelError):
            retrieve_records(self.records, task_id="")
        with self.assertRaises(MemoryModelError):
            retrieve_records(self.records, member="coder")


class NoContextDependencyTests(unittest.TestCase):
    """④ 缝保持关闭：零 Context 语义模块依赖、零 prompt 面。"""

    def test_no_context_model_dependency(self):
        for token in ("cockpit_context", "ContextItem", "ContextSnapshot",
                      "to_prompt", "render_prompt", "build_messages",
                      "provider_prompt", "_make_request_builder"):
            self.assertNotIn(token, MODULE_SOURCE)

    def test_import_surface_closed(self):
        imports = re.findall(
            r"^\s*(?:from|import)\s+([A-Za-z_][\w.]*)",
            MODULE_SOURCE, re.MULTILINE)
        self.assertEqual(set(imports),
                         {"dataclasses", "enum", "hashlib", "json"})

    def test_json_used_only_for_digest_canonicalization(self):
        self.assertEqual(MODULE_SOURCE.count("json."), 1)
        self.assertIn("json.dumps(", MODULE_SOURCE)


class NoPersistenceTests(unittest.TestCase):
    """零持久化：零文件 IO 词面；"Memory Item" 名称不存在。"""

    def test_no_file_io_surface(self):
        for token in ("open(", "tempfile", "sqlite", "shelve", "dbm",
                      "mkdir", "import os", "from pathlib", "os.replace",
                      "write_text", "read_text", "json.load"):
            self.assertNotIn(token, MODULE_SOURCE)

    def test_memory_item_name_absent(self):
        self.assertNotIn("MemoryItem", MODULE_SOURCE)


class RuntimeNeutralityTests(unittest.TestCase):
    """零 provider 知识；零引擎 / 观察 / 控制调用。"""

    def test_no_provider_words(self):
        for provider in ("claude", "codex", "gemini", "qwen", "opencode",
                         "cline", "tiny"):
            self.assertIsNone(
                re.search(rf"\b{provider}\b", MODULE_SOURCE),
                f"provider word {provider!r} must not appear")

    def test_no_engine_or_observation_calls(self):
        for token in ("EventIndex", "ControlJournal", "UsageLog",
                      "ExecutionEvent", "textual", ".observe(", ".submit(",
                      ".consume_revisions(", ".invoke(", ".append("):
            self.assertNotIn(token, MODULE_SOURCE)


class NoGroupSemanticsTests(unittest.TestCase):
    """组合分组面零直接 Memory 语义；组合事实仅描述性在场。"""

    def test_no_group_words_or_fields(self):
        self.assertNotIn("group", MODULE_SOURCE.lower())
        for cls in (MemoryCandidate, MemoryProvenance, MemoryRecord):
            names = set(cls.__dataclass_fields__)
            self.assertFalse(
                any("group" in name for name in names), cls.__name__)

    def test_composition_is_descriptive_only(self):
        record = _record()
        self.assertEqual(
            record.provenance.composition, record.candidate.steps)


class NoClockOrNewIdentityTests(unittest.TestCase):
    """零时钟、零新身份原语、零失效词表。"""

    def test_no_synthetic_time_or_uuid(self):
        for token in ("import time", "time.time", "datetime", "monotonic",
                      "import uuid", "uuid4(", "uuid1(", "created_at",
                      "remembered_at", "last_accessed_at"):
            self.assertNotIn(token, MODULE_SOURCE)

    def test_no_expiry_vocabulary(self):
        for token in ("expire", "supersed", "stale", "ttl"):
            self.assertNotIn(token, MODULE_SOURCE)


class ClosedSurfaceTests(unittest.TestCase):
    """公共契约闭面：__all__ 恰九名，全部可解析。"""

    def test_all_exports_closed_and_resolvable(self):
        self.assertEqual(
            set(cockpit_memory.__all__),
            {"MemoryModelError", "MemoryScope", "MemorySourceKind",
             "MemoryCandidate", "MemoryRecord", "MemoryProvenance",
             "candidate_from_run_summary", "mint_record",
             "retrieve_records"})
        for name in cockpit_memory.__all__:
            self.assertTrue(hasattr(cockpit_memory, name))


if __name__ == "__main__":
    unittest.main()
