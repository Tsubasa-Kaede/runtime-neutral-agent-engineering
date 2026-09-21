"""CU-CONTEXT-3 Context Compiler 测试面。

覆盖矩阵（设计 PART 25 + 实施授权 §二十二，26 点）：
- 选择：TASK 必进 / PRIOR iff 邻接旁证+VALID / REVISION pending
  FIFO / STALE·SUPERSEDED 永排（封闭原因词表）/ 身份全等坍缩 /
  非候选零伪造报告；
- 排序：kind 秩 TASK<PRIOR<REVISION、kind 内保输入序、确定性重放、
  输出身份=(快照身份, 政策指纹)；
- 预算：默认 4000 头部截断逐位复现 / 边界恰值不截 / TASK 恒不截 /
  修订字符上限 / 修订条数上限（FIFO 头部保留）/ 全量上限（逆优先
  丢弃序）/ 任务独超=政策矛盾拒绝 / 记账聚合 / 政策值校验；
- token：编译时点恒 UNKNOWN、UNKNOWN ≠ 0、KNOWN 携带计数、
  UNSUPPORTED 态、构造校验；
- 装箱：段字段四元组 / provenance 逐字引用 / 空快照空编译零占位 /
  frozen+哈希 / 任务链旧条目排除；
- 边界（负向行为）：Memory 域载体拒绝 / 非快照拒绝 / 跨 run 条目
  构造期拒绝（编译结构性不可达）/ PARKED 续走同构编译 / 旁证校验；
- 静态墙：import 面恰 {dataclasses,enum,hashlib,json,cockpit_context}、
  零 Memory 域依赖、零提示词文本面、零 provider 知识、零 UI 依赖、
  零引擎/观察/控制/用量调用、零持久化 IO、零时钟零随机、零越域
  词表、json.dumps 恰一次、闭面 __all__。

全部离线；REAL=0。
"""
import ast
import re
import sys
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import cockpit_compile
from cockpit_compile import (  # noqa: E402
    BUDGET_EXCLUDED,
    CompileModelError,
    CompilePolicy,
    CompiledInvocationContext,
    CompiledSegment,
    DUPLICATE_COLLAPSED,
    STALE_SOURCE,
    SUPERSEDED,
    SelectionNote,
    TokenMeasure,
    TokenStatus,
    TruncationFact,
    compile_context,
)
from cockpit_context import (  # noqa: E402
    ContextItemKind,
    ContextModelError,
    ContextSnapshot,
    ContextValidity,
    item_from_invocation_result,
    item_from_revision,
    task_item,
)

MODULE_SOURCE = Path(
    cockpit_compile.__file__).read_text(encoding="utf-8")

VALID = ContextValidity.VALID
STALE = ContextValidity.STALE
SUPER = ContextValidity.SUPERSEDED
TASK = ContextItemKind.TASK
PRIOR = ContextItemKind.PRIOR_STEP_OUTPUT
REVISION = ContextItemKind.REVISION


def _task(text="write the parser", task_id="task-a", ordinal=0):
    return task_item(text, task_id=task_id, ordinal=ordinal)


def _prior(output="prior step body", task_id="task-a",
           invocation_id="invocation-7", role="architect"):
    result = SimpleNamespace(
        output=output,
        trace=SimpleNamespace(
            invocation_id=invocation_id, started_at=None, finished_at=None))
    return item_from_invocation_result(
        result, task_id=task_id, producer_role=role)


def _rev(text="tighten error handling", task_id="task-a", revision_id="rev-1"):
    return item_from_revision(
        SimpleNamespace(revision_id=revision_id, text=text), task_id=task_id)


def _snapshot(items, validity=None, task_id="task-a", step_index=2):
    if validity is None:
        validity = tuple(VALID for _ in items)
    return ContextSnapshot(task_id, step_index, tuple(items), tuple(validity))


def _kinds(ctx):
    return [segment.kind for segment in ctx.segments]


class SelectionTests(unittest.TestCase):
    """选择律：TASK 必进、PRIOR iff 邻接+VALID、REVISION pending FIFO、
    非 VALID 永排（封闭词表）、身份全等坍缩。"""

    def test_task_valid_mandatory_enters(self):
        ctx = compile_context(_snapshot([_task()]))
        self.assertEqual(_kinds(ctx), [TASK])
        self.assertEqual(ctx.segments[0].payload, "write the parser")

    def test_prior_enters_iff_adjacent_witnessed(self):
        snap = _snapshot([_task(), _prior(invocation_id="invocation-7")])
        ctx = compile_context(snap, adjacent_invocation_ids=("invocation-7",))
        self.assertEqual(_kinds(ctx), [TASK, PRIOR])

    def test_prior_absent_without_adjacent_witness(self):
        """旁证缺席 = 首步同构：零前步段，零占位。"""
        snap = _snapshot([_task(), _prior(invocation_id="invocation-7")])
        ctx = compile_context(snap)
        self.assertEqual(_kinds(ctx), [TASK])
        self.assertEqual(ctx.selection_notes, ())

    def test_non_adjacent_prior_excluded(self):
        """非相邻前步输出：非候选——零段且零伪造原因（非候选≠被排除）。"""
        snap = _snapshot([_task(), _prior(invocation_id="invocation-old")])
        ctx = compile_context(snap, adjacent_invocation_ids=("invocation-9",))
        self.assertEqual(_kinds(ctx), [TASK])
        self.assertEqual(ctx.selection_notes, ())

    def test_revision_valid_enters_fifo(self):
        snap = _snapshot([
            _task(), _rev(text="first", revision_id="rev-1"),
            _rev(text="second", revision_id="rev-2")])
        ctx = compile_context(snap)
        self.assertEqual(_kinds(ctx), [TASK, REVISION, REVISION])
        self.assertEqual(
            [segment.payload for segment in ctx.segments[1:]],
            ["first", "second"])

    def test_stale_excluded_with_report(self):
        snap = _snapshot(
            [_task(), _prior(invocation_id="invocation-7")],
            (VALID, STALE))
        ctx = compile_context(snap, adjacent_invocation_ids=("invocation-7",))
        self.assertEqual(_kinds(ctx), [TASK])
        self.assertEqual(ctx.selection_notes, (
            SelectionNote((PRIOR, "invocation-7", 0), STALE_SOURCE),))

    def test_superseded_revision_excluded_with_report(self):
        snap = _snapshot(
            [_task(), _rev(revision_id="rev-1")], (VALID, SUPER))
        ctx = compile_context(snap)
        self.assertEqual(_kinds(ctx), [TASK])
        self.assertEqual(ctx.selection_notes, (
            SelectionNote((REVISION, "rev-1", 0), SUPERSEDED),))

    def test_task_chain_superseded_old_excluded(self):
        """SUBMISSION 上游替换：旧任务条目 SUPERSEDED 排除，现任务进入。"""
        old = _task(text="old wording", ordinal=0)
        new = _task(text="new wording", ordinal=1)
        snap = _snapshot([old, new], (SUPER, VALID))
        ctx = compile_context(snap)
        self.assertEqual(_kinds(ctx), [TASK])
        self.assertEqual(ctx.segments[0].payload, "new wording")
        self.assertEqual(ctx.selection_notes, (
            SelectionNote((TASK, "", 0), SUPERSEDED),))

    def test_duplicate_identity_collapses(self):
        rev = _rev(revision_id="rev-1")
        snap = _snapshot([_task(), rev, _rev(revision_id="rev-1")])
        ctx = compile_context(snap)
        self.assertEqual(_kinds(ctx), [TASK, REVISION])
        self.assertEqual(ctx.selection_notes, (
            SelectionNote((REVISION, "rev-1", 0), DUPLICATE_COLLAPSED),))

    def test_selection_reason_vocabulary_closed(self):
        for bad in ("NOT_A_REASON", "NON_ADJACENT", "EXPIRED", ""):
            with self.assertRaises(CompileModelError):
                SelectionNote(("x",), bad)
        self.assertEqual(
            {STALE_SOURCE, SUPERSEDED, BUDGET_EXCLUDED, DUPLICATE_COLLAPSED},
            {"STALE_SOURCE", "SUPERSEDED", "BUDGET_EXCLUDED",
             "DUPLICATE_COLLAPSED"})


class OrderingTests(unittest.TestCase):
    """排序律：kind 秩 + kind 内保输入序 + 确定性重放 + 输出身份。"""

    def test_kind_rank_ordering(self):
        snap = _snapshot([
            _rev(revision_id="rev-1"), _prior(invocation_id="invocation-7"),
            _task()])
        ctx = compile_context(snap, adjacent_invocation_ids=("invocation-7",))
        self.assertEqual(_kinds(ctx), [TASK, PRIOR, REVISION])

    def test_within_kind_snapshot_order_preserved(self):
        """kind 内保输入序（修订 FIFO=输入序；非字典序非 id 序）。"""
        snap = _snapshot([
            _task(), _rev(text="zeta", revision_id="rev-z"),
            _rev(text="alpha", revision_id="rev-a")])
        ctx = compile_context(snap)
        self.assertEqual(
            [segment.payload for segment in ctx.segments[1:]],
            ["zeta", "alpha"])

    def test_deterministic_replay(self):
        snap = _snapshot([
            _task(), _prior(invocation_id="invocation-7"),
            _rev(revision_id="rev-1")])
        ctx_a = compile_context(
            snap, adjacent_invocation_ids=("invocation-7",))
        ctx_b = compile_context(
            snap, adjacent_invocation_ids=("invocation-7",))
        self.assertEqual(ctx_a, ctx_b)
        self.assertIsNot(ctx_a, ctx_b)

    def test_output_identity_shape(self):
        policy = CompilePolicy()
        snap = _snapshot([_task()])
        ctx = compile_context(snap, policy)
        self.assertEqual(
            ctx.identity, (("task-a", 2), policy.fingerprint))
        self.assertEqual(ctx.task_id, "task-a")
        self.assertEqual(ctx.step_index, 2)


class BudgetTests(unittest.TestCase):
    """预算律：字符单位（默认 4000 头部逐位复现）/ TASK 恒不截 /
    条数上限 / 全量上限逆优先丢弃 / 政策校验 / 记账聚合。"""

    def test_default_policy_prior_4000_head_truncation(self):
        body = "x" * 5001
        snap = _snapshot([_task(), _prior(output=body)])
        ctx = compile_context(snap, adjacent_invocation_ids=("invocation-7",))
        prior_segment = ctx.segments[1]
        self.assertEqual(len(prior_segment.payload), 4000)
        self.assertEqual(prior_segment.payload, "x" * 4000)
        self.assertEqual(ctx.budget.truncations, (
            TruncationFact(PRIOR, 5001, 4000),))
        self.assertEqual(
            dict(ctx.budget.embedded_chars_by_kind)[PRIOR], 4000)

    def test_boundary_exactly_4000_not_truncated(self):
        snap = _snapshot([_task(), _prior(output="y" * 4000)])
        ctx = compile_context(snap, adjacent_invocation_ids=("invocation-7",))
        self.assertEqual(len(ctx.segments[1].payload), 4000)
        self.assertEqual(ctx.budget.truncations, ())

    def test_task_never_truncated(self):
        """任务原文恒不截断（mandatory；现行无任务限长）。"""
        snap = _snapshot([_task(text="t" * 9000)])
        ctx = compile_context(snap)
        self.assertEqual(len(ctx.segments[0].payload), 9000)
        self.assertEqual(ctx.budget.truncations, ())

    def test_revision_char_limit(self):
        policy = CompilePolicy(revision_char_limit=4)
        snap = _snapshot([_task(), _rev(text="abcdefgh")])
        ctx = compile_context(snap, policy=policy)
        self.assertEqual(ctx.segments[1].payload, "abcd")
        self.assertEqual(ctx.budget.truncations, (
            TruncationFact(REVISION, 8, 4),))

    def test_revision_item_budget_keeps_fifo_head(self):
        policy = CompilePolicy(max_revision_items=1)
        snap = _snapshot([
            _task(), _rev(text="one", revision_id="rev-1"),
            _rev(text="two", revision_id="rev-2"),
            _rev(text="three", revision_id="rev-3")])
        ctx = compile_context(snap, policy=policy)
        revisions = [s for s in ctx.segments if s.kind is REVISION]
        self.assertEqual([s.payload for s in revisions], ["one"])
        self.assertEqual(ctx.selection_notes, (
            SelectionNote((REVISION, "rev-2", 0), BUDGET_EXCLUDED),
            SelectionNote((REVISION, "rev-3", 0), BUDGET_EXCLUDED)))

    def test_total_budget_drops_revisions_before_prior(self):
        """全量上限：逆优先丢弃——修订尾部先行、前步继后、任务永存。"""
        snap = _snapshot([
            _task(text="t" * 10), _prior(output="p" * 20),
            _rev(text="r1x", revision_id="rev-1"),
            _rev(text="r2xy", revision_id="rev-2")])
        ctx = compile_context(
            snap, policy=CompilePolicy(total_char_limit=30),
            adjacent_invocation_ids=("invocation-7",))
        self.assertEqual(_kinds(ctx), [TASK, PRIOR])
        self.assertEqual(
            [note.item_identity for note in ctx.selection_notes],
            [(REVISION, "rev-2", 0), (REVISION, "rev-1", 0)])
        self.assertEqual(ctx.budget.total_embedded_chars, 30)

    def test_total_budget_can_drop_prior_too(self):
        snap = _snapshot([
            _task(text="t" * 10), _prior(output="p" * 20),
            _rev(text="r1", revision_id="rev-1")])
        ctx = compile_context(
            snap, policy=CompilePolicy(total_char_limit=25),
            adjacent_invocation_ids=("invocation-7",))
        self.assertEqual(_kinds(ctx), [TASK])
        self.assertEqual(
            [note.reason for note in ctx.selection_notes],
            [BUDGET_EXCLUDED, BUDGET_EXCLUDED])

    def test_total_budget_contradiction_rejected(self):
        """任务独超全量上限 = 政策矛盾（mandatory 不可丢弃）。"""
        snap = _snapshot([_task(text="t" * 50)])
        with self.assertRaises(CompileModelError):
            compile_context(
                snap, policy=CompilePolicy(total_char_limit=10))

    def test_budget_report_aggregates(self):
        snap = _snapshot([
            _task(text="t" * 11), _prior(output="x" * 5001),
            _rev(text="abcdefgh")])
        ctx = compile_context(snap, adjacent_invocation_ids=("invocation-7",))
        self.assertEqual(dict(ctx.budget.item_counts_by_kind), {
            TASK: 1, PRIOR: 1, REVISION: 1})
        self.assertEqual(dict(ctx.budget.embedded_chars_by_kind), {
            TASK: 11, PRIOR: 4000, REVISION: 8})
        self.assertEqual(ctx.budget.total_embedded_chars, 4019)

    def test_policy_validation_rejects(self):
        for bad in (0, -1, True, "4000", 4.5):
            with self.assertRaises(CompileModelError):
                CompilePolicy(prior_output_char_limit=bad)
            with self.assertRaises(CompileModelError):
                CompilePolicy(revision_char_limit=bad)
            with self.assertRaises(CompileModelError):
                CompilePolicy(max_revision_items=bad)
            with self.assertRaises(CompileModelError):
                CompilePolicy(total_char_limit=bad)


class TokenMeasureTests(unittest.TestCase):
    """token 三态：编译时点恒 UNKNOWN、UNKNOWN ≠ 0、KNOWN 携带计数、
    构造校验封闭。零字符→token 换算（静态墙另测）。"""

    def test_compile_time_token_always_unknown(self):
        ctx = compile_context(_snapshot([_task()]))
        self.assertIs(ctx.token_measure.status, TokenStatus.UNKNOWN)
        self.assertIsNone(ctx.token_measure.tokens)

    def test_unknown_is_not_zero(self):
        unknown = TokenMeasure.unknown()
        self.assertNotEqual(unknown, TokenMeasure.known(0))
        self.assertIsNone(unknown.tokens)
        ctx = compile_context(_snapshot([_task()]))
        self.assertNotEqual(ctx.token_measure, 0)

    def test_known_carries_count(self):
        measure = TokenMeasure.known(5)
        self.assertIs(measure.status, TokenStatus.KNOWN)
        self.assertEqual(measure.tokens, 5)
        with self.assertRaises(CompileModelError):
            TokenMeasure.known(-1)
        with self.assertRaises(CompileModelError):
            TokenMeasure.unknown().__class__(
                status=TokenStatus.UNKNOWN, tokens=3)

    def test_unsupported_state(self):
        measure = TokenMeasure.unsupported()
        self.assertIs(measure.status, TokenStatus.UNSUPPORTED)
        self.assertIsNone(measure.tokens)


class PackingTests(unittest.TestCase):
    """装箱律：段四元组 / provenance 逐字引用 / 空进空出零占位 /
    frozen+哈希 / 确定性政策指纹。"""

    def test_segment_fields(self):
        prior = _prior(invocation_id="invocation-7")
        snap = _snapshot([_task(), prior])
        ctx = compile_context(snap, adjacent_invocation_ids=("invocation-7",))
        segment = ctx.segments[1]
        self.assertIsInstance(segment, CompiledSegment)
        self.assertEqual(segment.kind, PRIOR)
        self.assertEqual(segment.payload, "prior step body")
        self.assertIs(segment.provenance, prior.provenance)
        self.assertIs(segment.validity_at_compile, VALID)

    def test_empty_snapshot_compiles_empty(self):
        """No Context 合法：空快照 → 空编译产物，零占位零伪段。"""
        ctx = compile_context(_snapshot([], step_index=0))
        self.assertIsInstance(ctx, CompiledInvocationContext)
        self.assertEqual(ctx.segments, ())
        self.assertEqual(ctx.selection_notes, ())
        self.assertEqual(ctx.budget.total_embedded_chars, 0)
        self.assertEqual(ctx.budget.truncations, ())
        self.assertIs(ctx.token_measure.status, TokenStatus.UNKNOWN)
        self.assertEqual(ctx.identity, (("task-a", 0), ctx.policy_fingerprint))

    def test_frozen_immutability(self):
        snap = _snapshot([_task()])
        ctx = compile_context(snap)
        with self.assertRaises(FrozenInstanceError):
            ctx.segments = ()
        with self.assertRaises(FrozenInstanceError):
            ctx.segments[0].payload = "x"
        with self.assertRaises(FrozenInstanceError):
            CompilePolicy().prior_output_char_limit = 1
        with self.assertRaises(FrozenInstanceError):
            TokenMeasure.unknown().status = TokenStatus.KNOWN

    def test_hashable_and_set_dedup(self):
        snap = _snapshot([_task()])
        ctx_a = compile_context(snap)
        ctx_b = compile_context(snap)
        self.assertEqual(len({ctx_a, ctx_b}), 1)

    def test_policy_fingerprint_deterministic(self):
        self.assertEqual(
            CompilePolicy().fingerprint, CompilePolicy().fingerprint)
        self.assertEqual(
            CompilePolicy(prior_output_char_limit=100).fingerprint,
            CompilePolicy(prior_output_char_limit=100).fingerprint)
        self.assertNotEqual(
            CompilePolicy(prior_output_char_limit=100).fingerprint,
            CompilePolicy(prior_output_char_limit=200).fingerprint)
        self.assertRegex(
            CompilePolicy().fingerprint, r"^policy_[0-9a-f]{12}$")


class BoundaryBehaviorTests(unittest.TestCase):
    """边界（负向行为）：单通道律 + run 隔离结构性不可达 + PARKED 续走
    同构 + 旁证校验。"""

    def test_memory_record_rejected(self):
        """Memory 域载体永不直接作编译输入（④缝关闭的行为面）。"""
        import cockpit_memory
        record = cockpit_memory.mint_record(
            cockpit_memory.candidate_from_run_summary(SimpleNamespace(
                task="t", steps=(("architect", "runtime-a"),),
                status="COMPLETED", plan=(), groups=(), member_ids=(),
                final_preview="done", usage=None, task_id="task-a")),
            cockpit_memory.MemoryScope.SESSION)
        with self.assertRaises(CompileModelError):
            compile_context(record)

    def test_non_snapshot_inputs_rejected(self):
        for bad in (None, "snapshot", {}, [_task()], 42):
            with self.assertRaises(CompileModelError):
                compile_context(bad)

    def test_cross_run_item_structurally_unreachable(self):
        """run 隔离=快照构造期强制：跨 run 条目在编译入口前即被拒。"""
        foreign = _task(task_id="task-b")
        with self.assertRaises(ContextModelError):
            _snapshot([foreign])

    def test_parked_continuity_compiles(self):
        """PARKED 续走：前步链原样携带（VALID 无链重建见证）→ 照常编译。"""
        snap = _snapshot([_task(), _prior(invocation_id="invocation-7")])
        ctx = compile_context(snap, adjacent_invocation_ids=("invocation-7",))
        self.assertEqual(_kinds(ctx), [TASK, PRIOR])
        self.assertEqual(ctx.segments[1].payload, "prior step body")

    def test_adjacent_witness_validated(self):
        snap = _snapshot([_task(), _prior()])
        with self.assertRaises(CompileModelError):
            compile_context(snap, adjacent_invocation_ids=(123,))
        with self.assertRaises(CompileModelError):
            compile_context(snap, adjacent_invocation_ids=("",))

    def test_policy_type_rejected(self):
        with self.assertRaises(CompileModelError):
            compile_context(_snapshot([_task()]), policy="default")


class StaticWallTests(unittest.TestCase):
    """静态墙：import 面 / 零 Memory 域 / 零提示词文本面 / 零 provider /
    零 UI / 零引擎调用 / 零持久化 / 零时钟 / 零越域词表 / 闭面 __all__。"""

    def test_import_face_exact(self):
        tree = ast.parse(MODULE_SOURCE)
        modules = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    modules.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules.add(node.module.split(".")[0])
        self.assertEqual(
            modules, {"dataclasses", "enum", "hashlib", "json",
                      "cockpit_context"})

    def test_no_memory_domain_import(self):
        for token in ("cockpit_memory", "MemoryRecord", "MemoryCandidate",
                      "MemoryScope", "mint_record"):
            self.assertNotIn(token, MODULE_SOURCE)

    def test_no_prompt_text_surface(self):
        for token in ("prompt", "_make_request_builder", "to_prompt",
                      "render_prompt", "build_messages", "=== TASK ===",
                      "=== PREVIOUS", "[USER REVISION", "template"):
            self.assertNotIn(token, MODULE_SOURCE.lower())

    def test_no_provider_words(self):
        for word in ("claude", "codex", "gemini", "qwen", "opencode",
                     "cline", "tiny"):
            self.assertIsNone(
                re.search(rf"\b{word}\b", MODULE_SOURCE, re.IGNORECASE))

    def test_no_ui_dependencies(self):
        for token in ("textual", "cockpit_tui", "cockpit_projection",
                      "ConversationRecord"):
            self.assertNotIn(token, MODULE_SOURCE)

    def test_no_engine_module_references(self):
        for token in ("cockpit_entry", "cockpit_session",
                      "sequential_pipeline", "external_runtime",
                      "revision_adapter", "execution_slots", "wrap_stack",
                      "control_boundary", "host_entry"):
            self.assertNotIn(token, MODULE_SOURCE)

    def test_no_observation_control_usage_calls(self):
        for token in ("EventIndex", "ControlJournal", "UsageLog",
                      "ExecutionEvent", ".observe(", ".submit(",
                      ".invoke(", ".append("):
            self.assertNotIn(token, MODULE_SOURCE)

    def test_no_persistence_or_file_io(self):
        for token in ("open(", "tempfile", "sqlite", "shelve", "dbm",
                      "mkdir", "write_text", "read_text", "pathlib",
                      "import os", "os.replace"):
            self.assertNotIn(token, MODULE_SOURCE)

    def test_no_clock_or_random(self):
        for token in ("datetime", "monotonic", "perf_counter", "uuid",
                      "random", "time.time", "time_ns", "now("):
            self.assertNotIn(token, MODULE_SOURCE)

    def test_no_banned_vocabulary(self):
        for token in ("ContextManager", "PromptBuilder", "Router",
                      "Orchestrator", "CompilerService", "MemoryManager"):
            self.assertNotIn(token, MODULE_SOURCE)

    def test_no_scope_creep_words(self):
        for token in ("embedding", "vector", "semantic search", "ranking",
                      "summariz", "compression", "pruning", "optimizer",
                      "estimate", "convert", "ratio", "scheduler", "c2c",
                      "a2a"):
            self.assertNotIn(token, MODULE_SOURCE.lower())

    def test_no_group_semantics(self):
        self.assertNotIn("group", MODULE_SOURCE.lower())

    def test_json_dumps_exactly_once(self):
        """json 仅用于政策指纹规范化（CU-CONTEXT-2 内容寻址同型）。"""
        self.assertEqual(MODULE_SOURCE.count("json.dumps"), 1)

    def test_closed_all_face(self):
        self.assertEqual(set(cockpit_compile.__all__), {
            "CompileModelError", "CompilePolicy", "CompiledSegment",
            "CompiledInvocationContext", "TokenStatus", "TokenMeasure",
            "BudgetReport", "TruncationFact", "SelectionNote",
            "STALE_SOURCE", "SUPERSEDED", "BUDGET_EXCLUDED",
            "DUPLICATE_COLLAPSED", "compile_context"})


if __name__ == "__main__":
    unittest.main()
