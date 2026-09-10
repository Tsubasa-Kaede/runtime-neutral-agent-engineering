"""V3.2 CU-CTRL-1: Control Domain value models (RED-first contract tests).

Sections 1-6 冻结架构的 control-plane 数据契约：封闭词表（command /
reason / revision-target / lifecycle / park-point / pending-intent）、
不可变值模型（ControlCommand / ControlResult / ControlSnapshot /
PendingIntent / PendingRevision / RevisionPayload）、构造期结构校验、
以及 runtime-neutral 边界（零 engine/adapter/UI 依赖）。

本 CU 只测 contract：不测裁决、版本递增、journal、replay、admission ——
那些是后续 CU（ControlJournal / ControlBoundary / ControlGate）的行为面。
"""
import ast
import sys
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from control_boundary import (
    ControlCommand,
    ControlCommandType,
    ControlLifecycle,
    ControlModelError,
    ControlReason,
    ControlResult,
    ControlSnapshot,
    ControlStatus,
    ParkPoint,
    PendingIntent,
    PendingIntentKind,
    PendingRevision,
    RevisionPayload,
    RevisionTarget,
)

MODULE_PATH = SCRIPTS / "control_boundary.py"

_ALLOWED_FIELD_TYPES = (
    str, int, tuple,
    ControlCommandType, ControlStatus, ControlReason, ControlLifecycle,
    ParkPoint, PendingIntent, PendingIntentKind, PendingRevision,
    RevisionPayload, RevisionTarget,
)


def valid_pause_command(command_id="cmd-pause", execution_id="exec-1"):
    return ControlCommand(command_id=command_id, execution_id=execution_id,
                          command=ControlCommandType.PAUSE)


def valid_revise_command(command_id="cmd-rev", execution_id="exec-1", version=0):
    return ControlCommand(
        command_id=command_id, execution_id=execution_id,
        command=ControlCommandType.REVISE,
        payload=RevisionPayload(target=RevisionTarget.NEXT_INVOCATION,
                                text="prefer the minimal design"),
        expected_version=version)


class VocabularyContractTests(unittest.TestCase):
    """封闭词表精确锁定：不得扩词、不得改成员。"""

    def test_command_vocabulary_is_exactly_four(self):
        self.assertEqual({item.name for item in ControlCommandType},
                         {"PAUSE", "RESUME", "REVISE", "ABORT"})

    def test_ui_key_words_are_not_domain_commands(self):
        for word in ("EDIT", "CONTINUE", "CANCEL", "STOP", "KILL", "FORCE_STOP"):
            with self.assertRaises(ValueError):
                ControlCommandType(word)

    def test_reason_vocabulary_is_exactly_the_frozen_set(self):
        self.assertEqual(
            {item.name for item in ControlReason},
            {"USER_PAUSED", "ALREADY_REQUESTED", "ALREADY_PAUSED",
             "ALREADY_ABORTING", "ALREADY_TERMINAL", "NOT_PAUSED",
             "STALE_VERSION", "INVALID_STATE", "INVALID_TARGET",
             "COMMAND_ID_CONFLICT", "LENGTH_EXCEEDED",
             "REVISION_BUDGET_EXCEEDED", "UNSAFE_CONTENT"})

    def test_revision_target_vocabulary_is_exactly_two(self):
        self.assertEqual({item.name for item in RevisionTarget},
                         {"SUBMISSION", "NEXT_INVOCATION"})

    def test_forbidden_revision_targets_rejected(self):
        for word in ("CURRENT_INVOCATION", "WHOLE_EXECUTION", "INSTRUCTION",
                     "PROMPT", "RUNTIME"):
            with self.assertRaises(ValueError):
                RevisionTarget(word)

    def test_lifecycle_vocabulary_is_exactly_seven(self):
        self.assertEqual({item.name for item in ControlLifecycle},
                         {"RUNNING", "PAUSE_PENDING", "PAUSED", "ABORT_PENDING",
                          "ABORTED", "COMPLETED", "FAILED"})

    def test_park_point_vocabulary_is_exactly_three(self):
        self.assertEqual({item.name for item in ParkPoint},
                         {"NONE", "DISPATCH", "ADMISSION"})

    def test_pending_intent_kinds_exclude_resume_and_revise(self):
        # RESUME / REVISE 不形成 pending intent —— 词表层面结构性排除。
        self.assertEqual({item.name for item in PendingIntentKind},
                         {"NONE", "PAUSE", "ABORT"})
        for word in ("RESUME", "REVISE"):
            with self.assertRaises(ValueError):
                PendingIntentKind(word)


class ControlCommandTests(unittest.TestCase):
    def test_valid_pause_command(self):
        command = valid_pause_command()
        self.assertEqual(command.command, ControlCommandType.PAUSE)
        self.assertIsNone(command.payload)
        self.assertIsNone(command.expected_version)

    def test_valid_resume_and_abort_commands(self):
        for kind in (ControlCommandType.RESUME, ControlCommandType.ABORT):
            command = ControlCommand(command_id=f"cmd-{kind.name}",
                                     execution_id="exec-1", command=kind)
            self.assertEqual(command.command, kind)

    def test_valid_revise_command_with_payload_and_version(self):
        command = valid_revise_command(version=3)
        self.assertEqual(command.payload.target,
                         RevisionTarget.NEXT_INVOCATION)
        self.assertEqual(command.expected_version, 3)

    def test_empty_command_id_rejected(self):
        with self.assertRaises(ControlModelError):
            ControlCommand(command_id="", execution_id="exec-1",
                           command=ControlCommandType.PAUSE)
        with self.assertRaises(ControlModelError):
            ControlCommand(command_id="   ", execution_id="exec-1",
                           command=ControlCommandType.PAUSE)

    def test_non_string_command_id_rejected(self):
        with self.assertRaises(ControlModelError):
            ControlCommand(command_id=42, execution_id="exec-1",
                           command=ControlCommandType.PAUSE)

    def test_empty_execution_id_rejected(self):
        with self.assertRaises(ControlModelError):
            ControlCommand(command_id="cmd-1", execution_id="",
                           command=ControlCommandType.PAUSE)

    def test_invalid_command_rejected(self):
        with self.assertRaises(ControlModelError):
            ControlCommand(command_id="cmd-1", execution_id="exec-1",
                           command="EDIT")

    def test_raw_string_command_not_silently_coerced(self):
        with self.assertRaises(ControlModelError):
            ControlCommand(command_id="cmd-1", execution_id="exec-1",
                           command="PAUSE")

    def test_command_id_has_no_implicit_default(self):
        # caller-supplied identity：无默认值 = 结构性禁止隐式自动生成。
        with self.assertRaises(TypeError):
            ControlCommand(execution_id="exec-1",
                           command=ControlCommandType.PAUSE)

    def test_revise_without_payload_rejected(self):
        with self.assertRaises(ControlModelError):
            ControlCommand(command_id="cmd-1", execution_id="exec-1",
                           command=ControlCommandType.REVISE,
                           expected_version=0)

    def test_revise_without_expected_version_rejected(self):
        with self.assertRaises(ControlModelError):
            ControlCommand(command_id="cmd-1", execution_id="exec-1",
                           command=ControlCommandType.REVISE,
                           payload=RevisionPayload(
                               target=RevisionTarget.NEXT_INVOCATION,
                               text="revise this"))

    def test_non_revise_command_with_payload_rejected(self):
        with self.assertRaises(ControlModelError):
            ControlCommand(command_id="cmd-1", execution_id="exec-1",
                           command=ControlCommandType.PAUSE,
                           payload=RevisionPayload(
                               target=RevisionTarget.NEXT_INVOCATION,
                               text="revise this"))

    def test_non_revise_command_with_expected_version_rejected(self):
        with self.assertRaises(ControlModelError):
            ControlCommand(command_id="cmd-1", execution_id="exec-1",
                           command=ControlCommandType.PAUSE,
                           expected_version=0)

    def test_expected_version_zero_is_valid(self):
        command = valid_revise_command(version=0)
        self.assertEqual(command.expected_version, 0)

    def test_negative_expected_version_rejected(self):
        with self.assertRaises(ControlModelError):
            valid_revise_command(version=-1)

    def test_non_integer_expected_version_rejected(self):
        with self.assertRaises(ControlModelError):
            valid_revise_command(version="3")

    def test_boolean_expected_version_rejected(self):
        with self.assertRaises(ControlModelError):
            valid_revise_command(version=True)

    def test_command_is_immutable(self):
        command = valid_pause_command()
        with self.assertRaises(FrozenInstanceError):
            command.command = ControlCommandType.ABORT

    def test_command_id_preserved_exactly(self):
        marker = "cmd-9f1c-keep-exact"
        command = valid_pause_command(command_id=marker)
        self.assertEqual(command.command_id, marker)


class RevisionPayloadTests(unittest.TestCase):
    def test_valid_submission_payload_with_task(self):
        payload = RevisionPayload(target=RevisionTarget.SUBMISSION,
                                  task="revised task text")
        self.assertEqual(payload.target, RevisionTarget.SUBMISSION)

    def test_valid_submission_payload_with_prompt(self):
        payload = RevisionPayload(target=RevisionTarget.SUBMISSION,
                                  prompt="revised prompt text")
        self.assertEqual(payload.prompt, "revised prompt text")

    def test_valid_next_invocation_payload(self):
        payload = RevisionPayload(target=RevisionTarget.NEXT_INVOCATION,
                                  text="revision body")
        self.assertEqual(payload.text, "revision body")

    def test_submission_requires_task_or_prompt(self):
        with self.assertRaises(ControlModelError):
            RevisionPayload(target=RevisionTarget.SUBMISSION)

    def test_submission_with_text_rejected(self):
        with self.assertRaises(ControlModelError):
            RevisionPayload(target=RevisionTarget.SUBMISSION,
                            task="task", text="stray text")

    def test_next_invocation_requires_text(self):
        with self.assertRaises(ControlModelError):
            RevisionPayload(target=RevisionTarget.NEXT_INVOCATION)

    def test_next_invocation_with_submission_fields_rejected(self):
        with self.assertRaises(ControlModelError):
            RevisionPayload(target=RevisionTarget.NEXT_INVOCATION,
                            text="body", task="stray")

    def test_empty_text_rejected(self):
        with self.assertRaises(ControlModelError):
            RevisionPayload(target=RevisionTarget.NEXT_INVOCATION, text="  ")

    def test_invalid_target_rejected(self):
        with self.assertRaises(ControlModelError):
            RevisionPayload(target="PROMPT", text="body")

    def test_payload_is_immutable(self):
        payload = RevisionPayload(target=RevisionTarget.NEXT_INVOCATION,
                                  text="body")
        with self.assertRaises(FrozenInstanceError):
            payload.text = "mutated"


class PendingIntentTests(unittest.TestCase):
    def test_default_intent_is_none(self):
        self.assertEqual(PendingIntent().kind, PendingIntentKind.NONE)

    def test_pause_and_abort_intents_construct(self):
        self.assertEqual(
            PendingIntent(PendingIntentKind.PAUSE).kind, PendingIntentKind.PAUSE)
        self.assertEqual(
            PendingIntent(PendingIntentKind.ABORT).kind, PendingIntentKind.ABORT)

    def test_invalid_kind_rejected(self):
        with self.assertRaises(ControlModelError):
            PendingIntent("RESUME")

    def test_abort_supersedes_pending_pause(self):
        pause = PendingIntent(PendingIntentKind.PAUSE)
        self.assertEqual(pause.superseded_by(PendingIntentKind.ABORT).kind,
                         PendingIntentKind.ABORT)

    def test_pause_cannot_supersede_pending_abort(self):
        abort = PendingIntent(PendingIntentKind.ABORT)
        with self.assertRaises(ControlModelError):
            abort.superseded_by(PendingIntentKind.PAUSE)

    def test_any_intent_can_be_cleared_to_none(self):
        for kind in (PendingIntentKind.NONE, PendingIntentKind.PAUSE,
                     PendingIntentKind.ABORT):
            intent = PendingIntent(kind)
            self.assertEqual(
                intent.superseded_by(PendingIntentKind.NONE).kind,
                PendingIntentKind.NONE)

    def test_superseded_by_is_pure(self):
        pause = PendingIntent(PendingIntentKind.PAUSE)
        pause.superseded_by(PendingIntentKind.ABORT)
        self.assertEqual(pause.kind, PendingIntentKind.PAUSE)

    def test_single_kind_structurally_prevents_simultaneous_pending(self):
        # 单 kind 值对象：PAUSE 与 ABORT 不可能同时 pending（无组合字段）。
        self.assertEqual(
            len([f.name for f in __import__("dataclasses").fields(PendingIntent)]),
            1)

    def test_intent_is_immutable(self):
        intent = PendingIntent(PendingIntentKind.PAUSE)
        with self.assertRaises(FrozenInstanceError):
            intent.kind = PendingIntentKind.ABORT


class ControlResultTests(unittest.TestCase):
    def accepted_result(self, version=0):
        return ControlResult(command_id="cmd-1", execution_id="exec-1",
                             status=ControlStatus.ACCEPTED,
                             execution_version=version + 1)

    def test_accepted_result(self):
        result = self.accepted_result(version=4)
        self.assertEqual(result.status, ControlStatus.ACCEPTED)
        self.assertEqual(result.execution_version, 5)
        self.assertIsNone(result.reason)

    def test_no_op_result_with_reason(self):
        result = ControlResult(command_id="cmd-1", execution_id="exec-1",
                               status=ControlStatus.NO_OP,
                               execution_version=2,
                               reason=ControlReason.ALREADY_REQUESTED)
        self.assertEqual(result.reason, ControlReason.ALREADY_REQUESTED)

    def test_rejected_result_with_reason(self):
        result = ControlResult(command_id="cmd-1", execution_id="exec-1",
                               status=ControlStatus.REJECTED,
                               execution_version=2,
                               reason=ControlReason.STALE_VERSION)
        self.assertEqual(result.status, ControlStatus.REJECTED)

    def test_accepted_with_reason_rejected(self):
        with self.assertRaises(ControlModelError):
            ControlResult(command_id="cmd-1", execution_id="exec-1",
                          status=ControlStatus.ACCEPTED,
                          execution_version=1,
                          reason=ControlReason.USER_PAUSED)

    def test_no_op_without_reason_rejected(self):
        with self.assertRaises(ControlModelError):
            ControlResult(command_id="cmd-1", execution_id="exec-1",
                          status=ControlStatus.NO_OP, execution_version=1)

    def test_rejected_without_reason_rejected(self):
        with self.assertRaises(ControlModelError):
            ControlResult(command_id="cmd-1", execution_id="exec-1",
                          status=ControlStatus.REJECTED, execution_version=1)

    def test_invalid_status_rejected(self):
        with self.assertRaises(ControlModelError):
            ControlResult(command_id="cmd-1", execution_id="exec-1",
                          status="DEFERRED", execution_version=1)

    def test_invalid_version_rejected(self):
        with self.assertRaises(ControlModelError):
            ControlResult(command_id="cmd-1", execution_id="exec-1",
                          status=ControlStatus.ACCEPTED,
                          execution_version=-1)

    def test_result_is_immutable(self):
        result = self.accepted_result()
        with self.assertRaises(FrozenInstanceError):
            result.status = ControlStatus.REJECTED

    def test_equal_values_support_exact_replay_identity(self):
        # 值相等 + 不可变 = 同一 ControlResult 可被原样重放（replay 本身
        # 属于后续 CU；这里只证明模型可承载）。
        first = self.accepted_result(version=1)
        second = self.accepted_result(version=1)
        self.assertEqual(first, second)
        self.assertEqual(hash(first), hash(second))


class PendingRevisionTests(unittest.TestCase):
    def test_valid_pending_revision(self):
        entry = PendingRevision(revision_id="cmd-rev", text="revision body")
        self.assertEqual(entry.target, RevisionTarget.NEXT_INVOCATION)

    def test_empty_revision_id_rejected(self):
        with self.assertRaises(ControlModelError):
            PendingRevision(revision_id="", text="body")

    def test_empty_text_rejected(self):
        with self.assertRaises(ControlModelError):
            PendingRevision(revision_id="cmd-rev", text=" ")

    def test_submission_target_cannot_be_queued(self):
        # Section 3：SUBMISSION 是 set semantics（改 draft），从不入队。
        with self.assertRaises(ControlModelError):
            PendingRevision(revision_id="cmd-rev",
                            target=RevisionTarget.SUBMISSION, text="body")

    def test_entry_is_immutable(self):
        entry = PendingRevision(revision_id="cmd-rev", text="body")
        with self.assertRaises(FrozenInstanceError):
            entry.text = "mutated"


class ControlSnapshotTests(unittest.TestCase):
    def snapshot(self, **overrides):
        defaults = dict(execution_id="exec-1", execution_version=0,
                        lifecycle=ControlLifecycle.RUNNING)
        defaults.update(overrides)
        return ControlSnapshot(**defaults)

    def test_minimal_running_snapshot_defaults(self):
        snapshot = self.snapshot()
        self.assertEqual(snapshot.lifecycle, ControlLifecycle.RUNNING)
        self.assertEqual(snapshot.pending_intent.kind, PendingIntentKind.NONE)
        self.assertEqual(snapshot.park_point, ParkPoint.NONE)
        self.assertEqual(snapshot.revision_queue, ())

    def test_paused_snapshot_with_dispatch_park_point(self):
        snapshot = self.snapshot(lifecycle=ControlLifecycle.PAUSED,
                                 park_point=ParkPoint.DISPATCH)
        self.assertEqual(snapshot.park_point, ParkPoint.DISPATCH)

    def test_paused_snapshot_with_admission_park_point(self):
        snapshot = self.snapshot(lifecycle=ControlLifecycle.PAUSED,
                                 park_point=ParkPoint.ADMISSION)
        self.assertEqual(snapshot.park_point, ParkPoint.ADMISSION)

    def test_paused_without_valid_park_point_rejected(self):
        with self.assertRaises(ControlModelError):
            self.snapshot(lifecycle=ControlLifecycle.PAUSED,
                          park_point=ParkPoint.NONE)

    def test_non_paused_lifecycle_with_park_point_rejected(self):
        for lifecycle in (ControlLifecycle.RUNNING,
                          ControlLifecycle.PAUSE_PENDING,
                          ControlLifecycle.ABORT_PENDING,
                          ControlLifecycle.COMPLETED,
                          ControlLifecycle.FAILED,
                          ControlLifecycle.ABORTED):
            with self.assertRaises(ControlModelError):
                self.snapshot(lifecycle=lifecycle,
                              park_point=ParkPoint.DISPATCH)

    def test_snapshot_with_pending_revision_queue(self):
        entry = PendingRevision(revision_id="cmd-rev", text="body")
        snapshot = self.snapshot(
            lifecycle=ControlLifecycle.PAUSED,
            park_point=ParkPoint.ADMISSION,
            revision_queue=(entry,))
        self.assertEqual(snapshot.revision_queue, (entry,))

    def test_queue_must_be_tuple_not_list(self):
        entry = PendingRevision(revision_id="cmd-rev", text="body")
        with self.assertRaises(ControlModelError):
            self.snapshot(lifecycle=ControlLifecycle.PAUSED,
                          park_point=ParkPoint.ADMISSION,
                          revision_queue=[entry])

    def test_queue_entries_must_be_pending_revisions(self):
        with self.assertRaises(ControlModelError):
            self.snapshot(lifecycle=ControlLifecycle.PAUSED,
                          park_point=ParkPoint.ADMISSION,
                          revision_queue=(("cmd-rev", "body"),))

    def test_invalid_lifecycle_rejected(self):
        with self.assertRaises(ControlModelError):
            self.snapshot(lifecycle="PAUSED")

    def test_empty_execution_id_rejected(self):
        with self.assertRaises(ControlModelError):
            self.snapshot(execution_id="")

    def test_invalid_version_rejected(self):
        with self.assertRaises(ControlModelError):
            self.snapshot(execution_version=-2)

    def test_snapshot_is_immutable(self):
        snapshot = self.snapshot()
        with self.assertRaises(FrozenInstanceError):
            snapshot.lifecycle = ControlLifecycle.COMPLETED
        with self.assertRaises(FrozenInstanceError):
            snapshot.revision_queue = ()


class ContractSeparationTests(unittest.TestCase):
    """runtime-neutral / 零 engine·adapter·UI 依赖的契约分离。"""

    def test_module_import_roots_are_locked(self):
        # CU-CTRL-3 起授权组合 control_journal + threading（V3.2 唯一
        # 获准的组合方向）；仍为精确集纪律锁，其余依赖一概拒绝。
        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        roots = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                roots.add(node.module.split(".")[0])
        self.assertEqual(
            roots - {"__future__"},
            {"dataclasses", "enum", "threading", "control_journal"})

    def test_module_is_runtime_neutral(self):
        text = MODULE_PATH.read_text(encoding="utf-8").lower()
        for name in ("claude", "codex", "deepseek", "openai", "anthropic",
                     "gemini", "tiny-agents", "tiny_agents"):
            self.assertNotIn(name, text)

    def test_module_does_not_reference_execution_event_contract(self):
        # Command ≠ ExecutionEvent：控制域模型绝不引用观察契约。
        text = MODULE_PATH.read_text(encoding="utf-8")
        self.assertNotIn("ExecutionEvent", text)
        self.assertNotIn("execution_observation", text)
        self.assertNotIn("ObservationSink", text)

    def test_command_field_values_are_runtime_neutral_value_types(self):
        command = valid_revise_command()
        for name in ("command_id", "execution_id", "command", "payload",
                     "expected_version"):
            value = getattr(command, name)
            self.assertIsInstance(value, _ALLOWED_FIELD_TYPES + (type(None),),
                                  name)

    def test_result_field_values_are_runtime_neutral_value_types(self):
        result = ControlResult(command_id="cmd-1", execution_id="exec-1",
                               status=ControlStatus.REJECTED,
                               execution_version=1,
                               reason=ControlReason.STALE_VERSION)
        for name in ("command_id", "execution_id", "status",
                     "execution_version", "reason"):
            value = getattr(result, name)
            self.assertIsInstance(value, _ALLOWED_FIELD_TYPES + (type(None),),
                                  name)

    def test_snapshot_field_values_are_runtime_neutral_value_types(self):
        entry = PendingRevision(revision_id="cmd-rev", text="body")
        snapshot = ControlSnapshot(
            execution_id="exec-1", execution_version=4,
            lifecycle=ControlLifecycle.PAUSED,
            pending_intent=PendingIntent(PendingIntentKind.ABORT),
            park_point=ParkPoint.ADMISSION,
            revision_queue=(entry,))
        for name in ("execution_id", "execution_version", "lifecycle",
                     "pending_intent", "park_point", "revision_queue"):
            value = getattr(snapshot, name)
            self.assertIsInstance(value, _ALLOWED_FIELD_TYPES + (type(None),),
                                  name)
        for item in snapshot.revision_queue:
            self.assertIsInstance(item, PendingRevision)


if __name__ == "__main__":
    unittest.main()
