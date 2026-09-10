"""CU-CTRL-3 tests: ControlBoundary Core（intent authority + effect
non-authority）。

第一次合法组合 CTRL-1 值模型与 CTRL-2 账本：

    ControlCommand → ControlBoundary.submit → ControlJournal fact

覆盖冻结契约：
- submit 返回 ControlResult（绝不直接返回 lifecycle）
- 命令校验（模型约束之上：execution_id 归属、类型）
- 命令→事实映射恰四项 REQUESTED（不产生 CONFIRMED/APPLIED/SUPERSEDED）
- intent 裁决表（PAUSE/RESUME/ABORT/REVISE × pending 状态）
- ABORT > PAUSE intent 优先级（pending 单值翻转）
- effect non-authority：无 runtime 面、无 CONFIRMED、无生命周期声称
- version 不递增（CTRL-4/5 语义推迟）
- snapshot 为投影：lifecycle 由组合方供应；PAUSED 被结构性拒绝
- journal 原子性：append 失败则异常传播、状态不变、不声称 accepted
- 线程安全：并发 submit 无丢事实、无矛盾 pending
- 依赖方向恰 control_boundary → control_journal（反向禁止）
"""
import ast
import sys
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

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
    ParkPoint,
    PendingIntent,
    PendingIntentKind,
    RevisionPayload,
    RevisionTarget,
)
from control_journal import (  # noqa: E402
    ControlFactType,
    ControlJournal,
    ControlJournalError,
    JournalFact,
)

MODULE_PATH = SCRIPTS / "control_boundary.py"
JOURNAL_PATH = SCRIPTS / "control_journal.py"


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


class ConstructionTests(unittest.TestCase):
    def test_requires_journal_and_execution_id(self):
        boundary, _ = make_boundary()
        self.assertEqual(boundary.execution_id, "exec-1")
        self.assertEqual(boundary.execution_version, 0)
        self.assertEqual(boundary.pending_intent.kind, PendingIntentKind.NONE)

    def test_empty_execution_id_rejected(self):
        with self.assertRaises(ControlModelError):
            ControlBoundary(journal=ControlJournal(), execution_id="  ")

    def test_negative_initial_version_rejected(self):
        with self.assertRaises(ControlModelError):
            ControlBoundary(journal=ControlJournal(), execution_id="exec-1",
                            initial_version=-1)

    def test_boundary_does_not_expose_writers(self):
        # 外部调用方不能经 boundary 拿到 writer 来模拟 submit
        boundary, _ = make_boundary()
        for name in ("boundary_writer", "revision_writer",
                     "composition_writer", "writer"):
            self.assertFalse(hasattr(boundary, name), name)


class SubmitContractTests(unittest.TestCase):
    def test_submit_returns_control_result(self):
        boundary, _ = make_boundary()
        result = boundary.submit(pause_cmd())
        self.assertIsInstance(result, ControlResult)
        self.assertEqual(result.status, ControlStatus.ACCEPTED)
        self.assertIsNone(result.reason)

    def test_non_command_rejected(self):
        boundary, journal = make_boundary()
        for bad in (None, "PAUSE", {"command": "PAUSE"}):
            with self.assertRaises(ControlModelError):
                boundary.submit(bad)
        self.assertEqual(journal.snapshot(), ())

    def test_foreign_execution_id_rejected_without_fact(self):
        boundary, journal = make_boundary(execution_id="exec-1")
        result = boundary.submit(pause_cmd(execution_id="exec-other"))
        self.assertEqual(result.status, ControlStatus.REJECTED)
        self.assertEqual(result.reason, ControlReason.INVALID_TARGET)
        self.assertEqual(journal.snapshot(), ())
        self.assertEqual(boundary.pending_intent.kind, PendingIntentKind.NONE)

    def test_result_identity_fields_preserved(self):
        boundary, _ = make_boundary()
        result = boundary.submit(pause_cmd(command_id="cmd-123"))
        self.assertEqual(result.command_id, "cmd-123")
        self.assertEqual(result.execution_id, "exec-1")


class JournalMappingTests(unittest.TestCase):
    def test_pause_writes_pause_requested(self):
        boundary, journal = make_boundary()
        boundary.submit(pause_cmd(command_id="cmd-1"))
        facts = journal.snapshot()
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0].fact_type, ControlFactType.PAUSE_REQUESTED)
        self.assertEqual(facts[0].command_id, "cmd-1")
        self.assertEqual(facts[0].execution_id, "exec-1")
        self.assertEqual(facts[0].execution_version, 0)

    def test_resume_writes_resume_requested(self):
        boundary, journal = make_boundary()
        boundary.submit(pause_cmd())
        boundary.submit(resume_cmd())
        self.assertEqual(
            [fact.fact_type for fact in journal.snapshot()],
            [ControlFactType.PAUSE_REQUESTED,
             ControlFactType.RESUME_REQUESTED])

    def test_abort_writes_abort_requested(self):
        boundary, journal = make_boundary()
        boundary.submit(abort_cmd())
        self.assertEqual(journal.snapshot()[0].fact_type,
                         ControlFactType.ABORT_REQUESTED)

    def test_revise_writes_revise_requested_with_identity_payload(self):
        boundary, journal = make_boundary()
        boundary.submit(revise_cmd(command_id="cmd-rev"))
        fact = journal.snapshot()[0]
        self.assertEqual(fact.fact_type, ControlFactType.REVISE_REQUESTED)
        self.assertEqual(fact.payload["revision_id"], "cmd-rev")
        self.assertEqual(fact.payload["target"], "NEXT_INVOCATION")
        self.assertNotIn("text", fact.payload)  # 文本不入事实载荷

    def test_no_confirmation_facts_are_ever_written(self):
        # CTRL-3 绝不产生 CONFIRMED/APPLIED/SUPERSEDED —— 它们属于
        # 后续 composition / revision / lifecycle semantics
        boundary, journal = make_boundary()
        boundary.submit(pause_cmd())
        boundary.submit(revise_cmd())  # PAUSE pending 下仍可记录
        boundary.submit(abort_cmd())   # ABORT > PAUSE，intent 翻转
        written = {fact.fact_type for fact in journal.snapshot()}
        self.assertEqual(written, {
            ControlFactType.PAUSE_REQUESTED,
            ControlFactType.ABORT_REQUESTED,
            ControlFactType.REVISE_REQUESTED})
        for forbidden in (ControlFactType.PAUSE_CONFIRMED,
                          ControlFactType.ABORT_CONFIRMED,
                          ControlFactType.ABORT_SUPERSEDED,
                          ControlFactType.REVISION_APPLIED):
            self.assertNotIn(forbidden, written)

    def test_facts_share_one_sequence_stream(self):
        boundary, journal = make_boundary()
        boundary.submit(pause_cmd())
        boundary.submit(resume_cmd())
        self.assertEqual(
            [fact.seq for fact in journal.snapshot()], [0, 1])


class IntentSemanticsTests(unittest.TestCase):
    def test_pause_accepted_then_pending_pause(self):
        boundary, _ = make_boundary()
        result = boundary.submit(pause_cmd())
        self.assertEqual(result.status, ControlStatus.ACCEPTED)
        self.assertEqual(boundary.pending_intent.kind,
                         PendingIntentKind.PAUSE)

    def test_duplicate_pause_is_no_op(self):
        boundary, journal = make_boundary()
        boundary.submit(pause_cmd())
        result = boundary.submit(pause_cmd(command_id="cmd-2"))
        self.assertEqual(result.status, ControlStatus.NO_OP)
        self.assertEqual(result.reason, ControlReason.ALREADY_REQUESTED)
        self.assertEqual(len(journal.snapshot()), 1)  # 无新事实
        self.assertEqual(boundary.pending_intent.kind, PendingIntentKind.PAUSE)

    def test_resume_clears_pending_pause(self):
        boundary, _ = make_boundary()
        boundary.submit(pause_cmd())
        result = boundary.submit(resume_cmd())
        self.assertEqual(result.status, ControlStatus.ACCEPTED)
        self.assertEqual(boundary.pending_intent.kind, PendingIntentKind.NONE)

    def test_resume_without_pending_pause_is_no_op(self):
        boundary, journal = make_boundary()
        result = boundary.submit(resume_cmd())
        self.assertEqual(result.status, ControlStatus.NO_OP)
        self.assertEqual(result.reason, ControlReason.NOT_PAUSED)
        self.assertEqual(journal.snapshot(), ())

    def test_abort_accepted_then_pending_abort(self):
        boundary, _ = make_boundary()
        result = boundary.submit(abort_cmd())
        self.assertEqual(result.status, ControlStatus.ACCEPTED)
        self.assertEqual(boundary.pending_intent.kind,
                         PendingIntentKind.ABORT)

    def test_duplicate_abort_is_no_op(self):
        boundary, journal = make_boundary()
        boundary.submit(abort_cmd())
        result = boundary.submit(abort_cmd(command_id="cmd-2"))
        self.assertEqual(result.status, ControlStatus.NO_OP)
        self.assertEqual(result.reason, ControlReason.ALREADY_REQUESTED)
        self.assertEqual(len(journal.snapshot()), 1)

    def test_abort_supersedes_pending_pause(self):
        # ABORT > PAUSE：最终 pending 必须是 ABORT（intent 层优先级；
        # 不写 ABORT_SUPERSEDED —— 那是 composition 语义）
        boundary, journal = make_boundary()
        boundary.submit(pause_cmd())
        result = boundary.submit(abort_cmd())
        self.assertEqual(result.status, ControlStatus.ACCEPTED)
        self.assertEqual(boundary.pending_intent.kind,
                         PendingIntentKind.ABORT)
        self.assertEqual(
            [fact.fact_type for fact in journal.snapshot()],
            [ControlFactType.PAUSE_REQUESTED,
             ControlFactType.ABORT_REQUESTED])

    def test_pause_cannot_supersede_pending_abort(self):
        boundary, journal = make_boundary()
        boundary.submit(abort_cmd())
        result = boundary.submit(pause_cmd())
        self.assertEqual(result.status, ControlStatus.REJECTED)
        self.assertEqual(result.reason, ControlReason.ALREADY_ABORTING)
        self.assertEqual(boundary.pending_intent.kind,
                         PendingIntentKind.ABORT)
        self.assertEqual(len(journal.snapshot()), 1)

    def test_resume_rejected_while_abort_pending(self):
        boundary, _ = make_boundary()
        boundary.submit(abort_cmd())
        result = boundary.submit(resume_cmd())
        self.assertEqual(result.status, ControlStatus.REJECTED)
        self.assertEqual(result.reason, ControlReason.ALREADY_ABORTING)
        self.assertEqual(boundary.pending_intent.kind,
                         PendingIntentKind.ABORT)

    def test_revise_rejected_while_abort_pending(self):
        boundary, journal = make_boundary()
        boundary.submit(abort_cmd())
        result = boundary.submit(revise_cmd())
        self.assertEqual(result.status, ControlStatus.REJECTED)
        self.assertEqual(result.reason, ControlReason.ALREADY_ABORTING)
        self.assertEqual(journal.snapshot()[0].fact_type,
                         ControlFactType.ABORT_REQUESTED)  # 无 REVISE 事实

    def test_revise_accepted_without_execution(self):
        # CTRL-3 只记录请求；不执行 revision、不动 queue
        boundary, journal = make_boundary()
        result = boundary.submit(revise_cmd())
        self.assertEqual(result.status, ControlStatus.ACCEPTED)
        result2 = boundary.submit(revise_cmd(command_id="cmd-rev-2"))
        self.assertEqual(result2.status, ControlStatus.ACCEPTED)
        self.assertEqual(len(journal.snapshot()), 2)

    def test_revise_accepted_while_pause_pending(self):
        boundary, journal = make_boundary()
        boundary.submit(pause_cmd())
        result = boundary.submit(revise_cmd())
        self.assertEqual(result.status, ControlStatus.ACCEPTED)
        self.assertEqual(boundary.pending_intent.kind,
                         PendingIntentKind.PAUSE)  # REVISE 不改 pending


class EffectNonAuthorityTests(unittest.TestCase):
    def test_pause_produces_only_result_and_requested_fact(self):
        boundary, journal = make_boundary()
        result = boundary.submit(pause_cmd())
        self.assertEqual(result.status, ControlStatus.ACCEPTED)
        self.assertEqual(
            [fact.fact_type for fact in journal.snapshot()],
            [ControlFactType.PAUSE_REQUESTED])

    def test_boundary_has_no_runtime_or_effect_surface(self):
        boundary, _ = make_boundary()
        for name in ("invoke", "call", "run", "execute", "cancel",
                     "notify", "wake", "set_event", "park", "kill",
                     "terminate", "adapter", "runtime"):
            self.assertFalse(hasattr(boundary, name), name)

    def test_boundary_only_uses_its_own_journal(self):
        # spy journal：boundary 只经自己的 writer 写事实，别无他面
        calls = []

        class _SpyJournal(ControlJournal):
            def boundary_writer(self):
                calls.append("boundary_writer")
                return super().boundary_writer()

        journal = _SpyJournal()
        boundary = ControlBoundary(journal=journal, execution_id="exec-1")
        boundary.submit(pause_cmd())
        self.assertEqual(calls, ["boundary_writer"])
        self.assertEqual(len(journal.snapshot()), 1)


class VersionSemanticsTests(unittest.TestCase):
    def test_version_does_not_increment_on_submit(self):
        # accepted-command epoch / version 递增语义属 CTRL-4/5
        boundary, journal = make_boundary(initial_version=7)
        boundary.submit(pause_cmd())
        boundary.submit(resume_cmd())
        boundary.submit(abort_cmd())
        self.assertEqual(boundary.execution_version, 7)
        self.assertEqual(
            [fact.execution_version for fact in journal.snapshot()],
            [7, 7, 7])

    def test_result_carries_current_version(self):
        boundary, _ = make_boundary(initial_version=3)
        result = boundary.submit(pause_cmd())
        self.assertEqual(result.execution_version, 3)


class SnapshotProjectionTests(unittest.TestCase):
    def test_snapshot_reflects_control_plane_fields(self):
        boundary, _ = make_boundary(initial_version=5)
        boundary.submit(pause_cmd())
        snapshot = boundary.snapshot(ControlLifecycle.RUNNING)
        self.assertEqual(snapshot.execution_id, "exec-1")
        self.assertEqual(snapshot.execution_version, 5)
        self.assertEqual(snapshot.pending_intent.kind, PendingIntentKind.PAUSE)
        self.assertEqual(snapshot.park_point, ParkPoint.NONE)
        self.assertEqual(snapshot.revision_queue, ())

    def test_snapshot_paused_is_structurally_refused(self):
        # PAUSED 需要 park point，而 park 权威属于 Gate（CTRL-6）——
        # Boundary 结构性拒绝伪造 parked 投影
        boundary, _ = make_boundary()
        with self.assertRaises(ControlModelError):
            boundary.snapshot(ControlLifecycle.PAUSED)

    def test_snapshot_is_pure_projection(self):
        boundary, _ = make_boundary()
        boundary.submit(pause_cmd())
        boundary.snapshot(ControlLifecycle.RUNNING)
        boundary.snapshot(ControlLifecycle.COMPLETED)
        self.assertEqual(boundary.pending_intent.kind, PendingIntentKind.PAUSE)


class JournalAtomicityTests(unittest.TestCase):
    def test_journal_failure_propagates_without_state_change(self):
        class _FailingJournal(ControlJournal):
            def boundary_writer(self):
                writer = super().boundary_writer()
                writer.append = lambda **kwargs: (_ for _ in ()).throw(
                    ControlJournalError("append failed"))
                return writer

        boundary = ControlBoundary(
            journal=_FailingJournal(), execution_id="exec-1")
        with self.assertRaises(ControlJournalError):
            boundary.submit(pause_cmd())
        # 不声称 accepted：pending 未变；不伪造 fact
        self.assertEqual(boundary.pending_intent.kind, PendingIntentKind.NONE)

    def test_state_changes_only_after_successful_append(self):
        boundary, journal = make_boundary()
        boundary.submit(abort_cmd())  # pending=ABORT，1 fact
        boundary.submit(pause_cmd())  # REJECTED，仍 1 fact
        self.assertEqual(len(journal.snapshot()), 1)
        self.assertEqual(boundary.pending_intent.kind, PendingIntentKind.ABORT)


class ConcurrencyTests(unittest.TestCase):
    def test_concurrent_submits_keep_journal_and_pending_consistent(self):
        for trial in range(20):
            boundary, journal = make_boundary()
            commands = []
            for i in range(12):
                commands.append(pause_cmd(command_id=f"p{i}"))
                commands.append(abort_cmd(command_id=f"a{i}"))
                commands.append(resume_cmd(command_id=f"r{i}"))

            def submit_all(chunk):
                return [boundary.submit(command) for command in chunk]

            chunks = [commands[i::6] for i in range(6)]
            with ThreadPoolExecutor(max_workers=6) as pool:
                results = [
                    result for batch in pool.map(submit_all, chunks)
                    for result in batch]

            accepted = [r for r in results if r.status is
                        ControlStatus.ACCEPTED]
            facts = journal.snapshot()
            # 每个 ACCEPTED 恰对应一条 REQUESTED 事实（无丢失/无伪造）
            self.assertEqual(len(facts), len(accepted))
            self.assertEqual(
                {fact.command_id for fact in facts},
                {result.command_id for result in accepted})
            # pending 收敛为单一合法值（无矛盾双 intent）
            self.assertIn(boundary.pending_intent.kind,
                          (PendingIntentKind.NONE, PendingIntentKind.PAUSE,
                           PendingIntentKind.ABORT))

    def test_reader_thread_sees_consistent_pending(self):
        boundary, _ = make_boundary()
        observations = []
        stop = threading.Event()

        def reader():
            while not stop.is_set():
                observations.append(boundary.pending_intent.kind)

        thread = threading.Thread(target=reader)
        thread.start()
        try:
            for i in range(100):
                boundary.submit(pause_cmd(command_id=f"p{i}"))
                boundary.submit(resume_cmd(command_id=f"r{i}"))
        finally:
            stop.set()
            thread.join()
        self.assertTrue(observations)
        for kind in observations:
            self.assertIn(kind, (PendingIntentKind.NONE,
                                 PendingIntentKind.PAUSE,
                                 PendingIntentKind.ABORT))


class ArchitectureGuardTests(unittest.TestCase):
    def test_import_roots_are_stdlib_plus_journal_only(self):
        # 允许方向恰：control_boundary → control_journal（本 CU 第一次
        # 合法组合）+ 最小标准库；零其他域依赖
        source = MODULE_PATH.read_text(encoding="utf-8")
        roots = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                roots.update(alias.name.split(".")[0]
                             for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                roots.add(node.module.split(".")[0])
        self.assertEqual(
            roots - {"__future__"},
            {"dataclasses", "enum", "threading", "control_journal"})

    def test_journal_does_not_import_boundary(self):
        # 反向依赖禁止（import 级：journal 的 docstring 提及组合计划
        # 不构成依赖，import 才是）
        source = JOURNAL_PATH.read_text(encoding="utf-8")
        roots = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                roots.update(alias.name.split(".")[0]
                             for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                roots.add(node.module.split(".")[0])
        self.assertNotIn("control_boundary", roots)

    def test_boundary_does_not_reference_forbidden_domains(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        for token in ("event_index", "usage_log", "trace_projector",
                      "cockpit", "subprocess", "execution_observation",
                      "external_runtime", "claude_code_adapter",
                      "pi_adapter"):
            self.assertNotIn(token, source)

    def test_boundary_module_is_runtime_neutral(self):
        source = MODULE_PATH.read_text(encoding="utf-8").lower()
        for token in ("claude", "codex", "deepseek", "openai", "anthropic",
                      "gemini", "qwen", "tiny-agents", "tiny_agents"):
            self.assertNotIn(token, source)


if __name__ == "__main__":
    unittest.main()
