"""CU-TUI-3 (V3.2): cockpit projection layer — offline pure-function tests.

Mandate matrix (CU-TUI-3 implementation authorization, 2026-09-13):
scenarios 1-36 of the 45-scenario plan — 2/3/4-agent layouts, four width
breakpoints, runtime/provider absence, usage three-state honesty, six
lifecycle outcomes under the frozen four-level priority (PAUSE accepted
!= PAUSED; ABORT accepted != ABORTED; terminal overrides everything),
CJK-aware truncation, result wrapping, context panel gating, NO_COLOR,
ASCII fallback, OBS/CTRL/USAGE trace source fidelity, and source guards
locking the projection layer to a pure read-only determinstic function.

All fixtures are hand-rolled offline values: no network, no REAL
providers, no filesystem access outside this file's own reads of the
projection source (guard tests), no threads, no time.
"""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from console_observation import format_event_line
from control_boundary import ControlReason, ControlStatus
from control_journal import ControlFactType, JournalFact
from execution_observation import (
    ExecutionEvent,
    ExecutionEventType,
)
from sequential_pipeline import RunStatus
from usage_log import UsageObservation, UsageRecord

import cockpit_projection as projection


# ---------------------------------------------------------------- helpers


def slot(role, runtime, stage=None, provider=None):
    return projection.AgentSlotView(
        stage=stage if stage is not None else role,
        role=role, runtime_id=runtime, provider=provider)


def ev(event_type, *, seq, stage, runtime, status="SUCCESS",
       reason="R", duration_ms=None):
    return ExecutionEvent(
        event_type=event_type, sequence=seq, task_id="t",
        correlation_id="e", stage=stage, runtime_id=runtime,
        status=status, reason=reason, duration_ms=duration_ms)


def fact(fact_type, *, seq=0, command_id="c", version=1, payload=None):
    return JournalFact(
        seq=seq, fact_type=fact_type, execution_id="e",
        command_id=command_id, execution_version=version,
        payload=payload)


def usage(*, runtime="rt-a", role="arch", status=UsageObservation.KNOWN,
          input_tokens=None, output_tokens=None, invocation="i"):
    if status is UsageObservation.KNOWN:
        input_tokens = 1000 if input_tokens is None else input_tokens
        output_tokens = 500 if output_tokens is None else output_tokens
    return UsageRecord(
        invocation_id=invocation, task_id="t", agent_id="a", role=role,
        runtime_id=runtime, status="SUCCESS", usage_status=status,
        input_tokens=input_tokens, output_tokens=output_tokens)


_TEMPLATES = {
    2: ("architect", "coder"),
    3: ("architect", "coder", "reviewer"),
    4: ("architect", "coder", "tester", "reviewer"),
}


def template_slots(count):
    return tuple(
        slot(role, f"rt-{index}")
        for index, role in enumerate(_TEMPLATES[count]))


def running_events(stages, runtime_of=None):
    """Minimal honest RUNNING stream: first stage fully finished, the
    remaining stage(s) started but not yet invoked."""
    runtime_of = runtime_of or (lambda index: f"rt-{index}")
    events = []
    seq = 0
    for index, role in enumerate(stages):
        events.append(ev(ExecutionEventType.STAGE_STARTED, seq=seq,
                         stage=role, runtime=runtime_of(index)))
        seq += 1
        if index == 0:
            events.append(ev(ExecutionEventType.INVOCATION_STARTED,
                             seq=seq, stage=role,
                             runtime=runtime_of(index), status="STARTED"))
            seq += 1
            events.append(ev(ExecutionEventType.INVOCATION_FINISHED,
                             seq=seq, stage=role,
                             runtime=runtime_of(index), status="SUCCESS"))
            seq += 1
    return events


def inputs(**kwargs):
    return projection.ProjectionInputs(**kwargs)


def build(**kwargs):
    return projection.build_projection(inputs(**kwargs))


# --------------------------------------------- scenarios 1-3: layouts 2/3/4


class AgentCountLayoutTests(unittest.TestCase):
    """Scenarios 1-3: fixed 2/3/4 role pipelines, zero padding slots."""

    def test_two_agent_layout(self):
        state = build(task="t", slots=template_slots(2),
                      events=running_events(("architect", "coder")),
                      width=100)
        self.assertEqual(len(state.collaboration_lines), 2)
        self.assertIn("ARCHITECT", state.collaboration_lines[0])
        self.assertIn("CODER", state.collaboration_lines[0])
        # first slot finished ok, second waiting, none invented
        self.assertIn("✓", state.collaboration_lines[1])
        self.assertIn("○", state.collaboration_lines[1])
        self.assertNotIn("REVIEWER", state.collaboration_lines[0])

    def test_three_agent_layout(self):
        state = build(task="t", slots=template_slots(3), events=(),
                      width=100)
        self.assertIn("REVIEWER", state.collaboration_lines[0])
        self.assertEqual(state.collaboration_lines[1].count("○"), 3)

    def test_four_agent_layout(self):
        state = build(task="t", slots=template_slots(4), events=(),
                      width=100)
        for role in ("ARCHITECT", "CODER", "TESTER", "REVIEWER"):
            self.assertIn(role, state.collaboration_lines[0])
        self.assertEqual(state.collaboration_lines[1].count("○"), 4)

    def test_symbol_by_invocation_state(self):
        slots_ = (slot("architect", "rt-0"), slot("coder", "rt-1"),
                  slot("reviewer", "rt-2"))
        events = (
            ev(ExecutionEventType.INVOCATION_STARTED, seq=0,
               stage="architect", runtime="rt-0", status="STARTED"),
            ev(ExecutionEventType.INVOCATION_FINISHED, seq=1,
               stage="architect", runtime="rt-0", status="SUCCESS"),
            ev(ExecutionEventType.INVOCATION_STARTED, seq=2,
               stage="coder", runtime="rt-1", status="STARTED"),
            ev(ExecutionEventType.INVOCATION_FINISHED, seq=3,
               stage="coder", runtime="rt-1", status="FAILED"),
            ev(ExecutionEventType.INVOCATION_STARTED, seq=4,
               stage="reviewer", runtime="rt-2", status="STARTED"),
        )
        state = build(task="t", slots=slots_, events=events, width=100)
        symbols = state.collaboration_lines[1]
        self.assertIn("✓", symbols)   # architect finished SUCCESS
        self.assertIn("✗", symbols)   # coder finished FAILED
        self.assertIn("●", symbols)   # reviewer in flight

    def test_runtime_ids_rendered_on_status_line(self):
        state = build(task="t", slots=template_slots(2),
                      events=running_events(("architect", "coder")),
                      width=100)
        self.assertIn("rt-0", state.collaboration_lines[1])
        self.assertIn("rt-1", state.collaboration_lines[1])

    def test_same_runtime_reuse_renders_both_slots(self):
        slots_ = (slot("architect", "rt-0"), slot("coder", "rt-0"))
        state = build(task="t", slots=slots_, events=(), width=100)
        self.assertEqual(state.collaboration_lines[1].count("rt-0"), 2)


# -------------------------------------- scenarios 4-7: responsive breakpoints


class ResponsiveBreakpointTests(unittest.TestCase):
    """Scenarios 4-7: <80 degraded / 80-99 main / 100-139 capped / >=140
    main+context."""

    def test_degraded_below_80(self):
        state = build(task="t", slots=template_slots(3), events=(),
                      width=79)
        self.assertEqual(state.tier, "DEGRADED")
        self.assertEqual(state.context_lines, ())
        # stacked: one line per slot instead of the two-line pipeline
        self.assertEqual(len(state.collaboration_lines), 3)

    def test_main_tier_80_to_99(self):
        state = build(task="t", slots=template_slots(3), events=(),
                      width=90)
        self.assertEqual(state.tier, "MAIN")
        self.assertEqual(len(state.collaboration_lines), 2)
        self.assertEqual(state.context_lines, ())

    def test_main_wide_tier_caps_content_at_100(self):
        task = "x" * 400
        state = build(task=task, slots=template_slots(2), events=(),
                      width=139)
        self.assertEqual(state.tier, "MAIN_WIDE")
        self.assertEqual(state.context_lines, ())
        self.assertLessEqual(projection.display_width(state.task_line),
                             100)

    def test_full_tier_adds_context(self):
        state = build(task="t", slots=template_slots(2), events=(),
                      width=140)
        self.assertEqual(state.tier, "FULL")
        self.assertNotEqual(state.context_lines, ())
        for line in state.context_lines:
            self.assertLessEqual(projection.display_width(line), 28)

    def test_context_hidden_below_140_even_on_wide_main(self):
        state = build(task="t", slots=template_slots(2), events=(),
                      width=139)
        self.assertEqual(state.context_lines, ())


# ------------------------------------- scenarios 8-9: runtime/provider absence


class AbsenceHonestyTests(unittest.TestCase):
    """Scenarios 8-9: absent provider renders '-', never a guess."""

    def test_absent_provider_renders_dash(self):
        slots_ = (slot("architect", "rt-0", provider=None),)
        state = build(task="t", slots=slots_, events=(), width=140)
        joined = "\n".join(state.context_lines)
        self.assertIn("provider", joined)
        self.assertIn("—", joined)

    def test_present_provider_renders_truth(self):
        slots_ = (slot("architect", "rt-0", provider="prov-x"),)
        state = build(task="t", slots=slots_, events=(), width=140)
        self.assertIn("prov-x", "\n".join(state.context_lines))

    def test_unknown_unified_dash_no_na_words(self):
        slots_ = (slot("architect", "rt-0"),)
        state = build(task="t", slots=slots_, events=(), width=140)
        joined = "\n".join(state.context_lines)
        for banned in ("unknown", "UNKNOWN", "N/A", "n/a"):
            self.assertNotIn(banned, joined)
        self.assertIn("—", joined)


# ------------------------------------------ scenarios 10-13: usage three-state


class UsageHonestyTests(unittest.TestCase):
    """Scenarios 10-13: KNOWN sums; UNKNOWN/UNSUPPORTED show '-';
    absent records fabricate nothing."""

    def test_known_usage_sums_input_and_output(self):
        records = (
            usage(invocation="i1", input_tokens=1000, output_tokens=500),
            usage(invocation="i2", input_tokens=2000, output_tokens=592),
        )
        state = build(task="t", slots=template_slots(2),
                      usage_records=records, width=100)
        self.assertIn("4.1k", state.tokens_line)  # 1500 + 2592 = 4092

    def test_unknown_usage_is_dash_not_zero(self):
        records = (usage(status=UsageObservation.UNKNOWN),)
        state = build(task="t", slots=template_slots(2),
                      usage_records=records, width=100)
        self.assertIn("—", state.tokens_line)
        self.assertNotIn("0", state.tokens_line)

    def test_unsupported_usage_is_dash_and_excluded(self):
        records = (
            usage(status=UsageObservation.UNSUPPORTED),
            usage(invocation="i2", input_tokens=900,
                  output_tokens=100),
        )
        state = build(task="t", slots=template_slots(2),
                      usage_records=records, width=100)
        self.assertIn("1.0k", state.tokens_line)

    def test_zero_known_records_is_dash(self):
        state = build(task="t", slots=template_slots(2),
                      usage_records=(), width=100)
        self.assertIn("—", state.tokens_line)

    def test_absent_record_creates_no_trace_row_and_no_zero(self):
        records = (usage(invocation="i1"),)
        state = build(task="t", slots=template_slots(2),
                      usage_records=records, width=100)
        # only the real record's runtime renders in the usage trace
        runtimes = [line for line in state.trace_usage if "rt-1" in line]
        self.assertEqual(runtimes, [])
        self.assertEqual(len(state.trace_usage), 1)
        self.assertNotIn("in=0", "\n".join(state.trace_usage))

    def test_known_trace_row_shows_counts(self):
        records = (usage(invocation="i1", input_tokens=7,
                         output_tokens=3),)
        state = build(task="t", slots=template_slots(2),
                      usage_records=records, width=100)
        line = state.trace_usage[0]
        self.assertIn("in=7", line)
        self.assertIn("out=3", line)

    def test_non_known_trace_row_shows_dash_counts(self):
        records = (usage(status=UsageObservation.UNKNOWN),)
        state = build(task="t", slots=template_slots(2),
                      usage_records=records, width=100)
        self.assertIn("in=—", state.trace_usage[0])
        self.assertIn("out=—", state.trace_usage[0])

    def test_tokens_format_boundaries(self):
        self.assertEqual(projection.format_tokens(None), "—")
        self.assertEqual(projection.format_tokens(0), "—")
        self.assertEqual(projection.format_tokens(999), "999")
        self.assertEqual(projection.format_tokens(4096), "4.1k")
        self.assertEqual(projection.format_tokens(1_500_000), "1.5M")


# ------------------------------------- scenarios 14-23: lifecycle priority


class LifecyclePriorityTests(unittest.TestCase):
    """Scenarios 14-23: frozen four-level priority (P1 terminal > P2
    run_state+pause > P2' run_state > P3 active events > P4 idle)."""

    def test_running_from_active_stage_events(self):        # scenario 14
        state = build(task="t", slots=template_slots(2),
                      events=running_events(("architect",)), width=100)
        self.assertEqual(state.lifecycle, "RUNNING")

    def test_paused_requires_run_state_and_pause_fact(self):  # 15
        state = build(task="t", slots=template_slots(2),
                      run_state=object(),
                      facts=(fact(ControlFactType.PAUSE_REQUESTED, seq=0),),
                      width=100)
        self.assertEqual(state.lifecycle, "PAUSED")

    def test_parked_when_run_state_without_pause(self):        # 16
        state = build(task="t", slots=template_slots(2),
                      run_state=object(), width=100)
        self.assertEqual(state.lifecycle, "PARKED")

    def test_completed_from_real_terminal_only(self):          # 17
        state = build(task="t", slots=template_slots(2),
                      terminal=RunStatus.COMPLETED, width=100)
        self.assertEqual(state.lifecycle, "COMPLETED")

    def test_failed_from_real_terminal_only(self):             # 18
        state = build(task="t", slots=template_slots(2),
                      terminal=RunStatus.FAILED, width=100)
        self.assertEqual(state.lifecycle, "FAILED")

    def test_aborted_from_real_terminal_only(self):            # 19
        state = build(task="t", slots=template_slots(2),
                      terminal=RunStatus.ABORTED, width=100)
        self.assertEqual(state.lifecycle, "ABORTED")

    def test_pause_accepted_without_run_state_is_not_paused(self):  # 20
        events = running_events(("architect",))
        state = build(task="t", slots=template_slots(2), events=events,
                      facts=(fact(ControlFactType.PAUSE_REQUESTED, seq=0),),
                      width=100)
        self.assertEqual(state.lifecycle, "RUNNING")
        # honest pending note on the progress line, badge untouched
        self.assertIn("pause pending", state.progress_line)

    def test_pause_accepted_with_run_state_is_paused(self):    # 21
        state = build(task="t", slots=template_slots(2),
                      run_state=object(),
                      facts=(fact(ControlFactType.PAUSE_REQUESTED, seq=0),),
                      width=100)
        self.assertEqual(state.lifecycle, "PAUSED")

    def test_abort_accepted_is_not_aborted_until_outcome(self):  # 22
        events = running_events(("architect",))
        state = build(task="t", slots=template_slots(2), events=events,
                      facts=(fact(ControlFactType.ABORT_REQUESTED, seq=0),),
                      width=100)
        self.assertEqual(state.lifecycle, "RUNNING")

    def test_terminal_overrides_active_events(self):           # 23
        events = running_events(("architect",))  # still "active" shape
        state = build(task="t", slots=template_slots(2), events=events,
                      terminal=RunStatus.COMPLETED, width=100)
        self.assertEqual(state.lifecycle, "COMPLETED")

    def test_idle_when_no_facts_at_all(self):                  # P4
        state = build(task="t", slots=template_slots(2), events=(),
                      width=100)
        self.assertEqual(state.lifecycle, "IDLE")

    def test_resume_fact_clears_pause_validity(self):
        state = build(task="t", slots=template_slots(2),
                      run_state=object(),
                      facts=(fact(ControlFactType.PAUSE_REQUESTED, seq=0),
                             fact(ControlFactType.RESUME_REQUESTED,
                                  seq=1, command_id="r")),
                      width=100)
        self.assertEqual(state.lifecycle, "PARKED")

    def test_later_pause_fact_reestablishes_validity(self):
        state = build(task="t", slots=template_slots(2),
                      run_state=object(),
                      facts=(fact(ControlFactType.PAUSE_REQUESTED, seq=0),
                             fact(ControlFactType.RESUME_REQUESTED,
                                  seq=1, command_id="r"),
                             fact(ControlFactType.PAUSE_REQUESTED,
                                  seq=2, command_id="p2")),
                      width=100)
        self.assertEqual(state.lifecycle, "PAUSED")


# ------------------------------------------- scenarios 24-25: text projection


class TextProjectionTests(unittest.TestCase):
    """Scenarios 24-25: CJK-aware truncation and result wrapping."""

    def test_cjk_task_truncation_respects_display_width(self):
        task = "重构登录模块并补充测试" * 10
        state = build(task=task, slots=template_slots(2), events=(),
                      width=90)
        self.assertLessEqual(projection.display_width(state.task_line),
                             95)
        self.assertIn("...", state.task_line)

    def test_wide_char_counts_double(self):
        self.assertEqual(projection.display_width("ab汉字"), 6)

    def test_truncate_never_exceeds_limit(self):
        text = "x" * 300
        cut = projection.truncate_to_width(text, 20)
        self.assertLessEqual(projection.display_width(cut), 20)

    def test_result_wrapping_caps_lines(self):
        outcome = SimpleOutcome(
            status=RunStatus.COMPLETED,
            final=SimpleResult("word " * 400), error=None)
        state = build(task="t", slots=template_slots(2),
                      last_outcome=outcome, width=100)
        self.assertLessEqual(len(state.result_lines), 4)
        joined = "\n".join(state.result_lines)
        self.assertIn("more lines", joined)

    def test_error_projection_in_result_lines(self):
        outcome = SimpleOutcome(
            status=RunStatus.FAILED, final=None, error=RuntimeError("boom"))
        state = build(task="t", slots=template_slots(2),
                      last_outcome=outcome, width=100)
        joined = "\n".join(state.result_lines)
        self.assertIn("ERROR", joined)
        self.assertIn("boom", joined)

    def test_parked_result_line_is_honest(self):
        outcome = SimpleOutcome(status=RunStatus.PARKED, final=None,
                                error=None)
        state = build(task="t", slots=template_slots(2),
                      last_outcome=outcome, width=100)
        self.assertIn("PARKED", "\n".join(state.result_lines))

    def test_no_outcome_renders_no_result(self):
        state = build(task="t", slots=template_slots(2), events=(),
                      width=100)
        self.assertEqual(state.result_lines, ())


# -------------------------------------- scenarios 26-29: context/no-color/ascii


class ContextPanelTests(unittest.TestCase):
    """Scenarios 26-27 (projection half): hidden <140, built >=140."""

    def test_context_hidden_below_140(self):
        state = build(task="t", slots=template_slots(2), events=(),
                      width=139)
        self.assertEqual(state.context_lines, ())

    def test_context_blocks_present_at_140(self):
        state = build(task="t", slots=template_slots(2), events=(),
                      width=141)
        joined = "\n".join(state.context_lines)
        for block in ("CURRENT", "RUNTIME", "CAPABILITIES", "SESSION"):
            self.assertIn(block, joined)

    def test_context_session_block_shows_known_total(self):
        records = (usage(invocation="i1", input_tokens=1500,
                         output_tokens=500),)
        state = build(task="t", slots=template_slots(2),
                      usage_records=records, width=141)
        self.assertIn("2.0k", "\n".join(state.context_lines))


class AccessibilityTests(unittest.TestCase):
    """Scenarios 28-29: zero ANSI always; ASCII symbol fallback."""

    def _all_strings(self, state):
        fields = [state.header_line, state.task_line, state.badge,
                  state.progress_line, state.tokens_line]
        fields.extend(state.collaboration_lines)
        fields.extend(state.result_lines)
        fields.extend(state.context_lines)
        fields.extend(state.trace_obs)
        fields.extend(state.trace_ctrl)
        fields.extend(state.trace_usage)
        return fields

    def test_no_ansi_sequences_in_any_projection(self):
        state = build(task="t", slots=template_slots(3),
                      events=running_events(("architect",)),
                      terminal=RunStatus.COMPLETED, width=141)
        for text in self._all_strings(state):
            self.assertNotIn("\x1b[", text)

    def test_ascii_mode_replaces_every_unicode_symbol(self):
        events = (
            ev(ExecutionEventType.STAGE_STARTED, seq=0,
               stage="architect", runtime="rt-0"),
            ev(ExecutionEventType.INVOCATION_STARTED, seq=1,
               stage="architect", runtime="rt-0", status="STARTED"),
        )
        state = build(task="t", slots=template_slots(3), events=events,
                      terminal=RunStatus.ABORTED, width=100,
                      ascii_only=True)
        for text in self._all_strings(state):
            for symbol in "✓●○✗→‖■":
                self.assertNotIn(symbol, text)
                self.assertIn("[RUN]", state.collaboration_lines[1])
        self.assertIn("[STOP]", state.badge)  # ABORTED badge wins

    def test_ascii_arrow_map(self):
        state = build(task="t", slots=template_slots(2), events=(),
                      ascii_only=True, width=100)
        self.assertIn("->", state.collaboration_lines[0])


# ---------------------------------------- scenarios 30-32: trace source truth


class TraceFidelityTests(unittest.TestCase):
    """Scenarios 30-32: OBS = format_event_line verbatim; CTRL = journal
    facts only; USAGE = usage records only."""

    def test_obs_lines_are_format_event_line_verbatim(self):
        events = running_events(("architect", "coder"))
        state = build(task="t", slots=template_slots(2), events=events,
                      width=100)
        expected = [format_event_line(e).rstrip("\n") for e in events]
        self.assertEqual(list(state.trace_obs), expected)
        # sequence numbers are the event's own, never renumbered
        self.assertIn("[0]", state.trace_obs[0])

    def test_ctrl_lines_come_from_journal_facts_only(self):
        facts = (
            fact(ControlFactType.PAUSE_REQUESTED, seq=0,
                 command_id="p1"),
            fact(ControlFactType.REVISE_REQUESTED, seq=1,
                 command_id="r1", payload={"revision_id": "r1",
                                           "target": "NEXT_INVOCATION"}),
        )
        state = build(task="t", slots=template_slots(2), facts=facts,
                      width=100)
        self.assertEqual(len(state.trace_ctrl), 2)
        joined = "\n".join(state.trace_ctrl)
        self.assertIn("PAUSE_REQUESTED", joined)
        self.assertIn("p1", joined)
        self.assertIn("REVISE_REQUESTED", joined)
        self.assertIn("revision_id=r1", joined)

    def test_usage_lines_come_from_records_only(self):
        records = (
            usage(invocation="i1", runtime="rt-0", role="architect"),
            usage(invocation="i2", runtime="rt-1", role="coder",
                  status=UsageObservation.UNKNOWN),
        )
        state = build(task="t", slots=template_slots(2),
                      usage_records=records, width=100)
        self.assertEqual(len(state.trace_usage), 2)
        self.assertIn("rt-0", state.trace_usage[0])
        self.assertIn("KNOWN", state.trace_usage[0])
        self.assertIn("UNKNOWN", state.trace_usage[1])


# ------------------------------------- scenarios 33-36: projection source locks


class ProjectionSourceGuardTests(unittest.TestCase):
    """Scenarios 33-36: the projection layer stays a pure deterministic
    read-only function — no textual, no time, no randomness, no
    identity minting, no truth construction, no execution surface."""

    def setUp(self):
        with open(projection.__file__, "r", encoding="utf-8") as handle:
            self.source = handle.read()

    def test_no_textual_dependency(self):
        self.assertNotIn("textual", self.source)

    def test_no_time_or_datetime_or_random_or_uuid(self):
        for token in ("import time", "time.time", "datetime", "random",
                      "uuid"):
            self.assertNotIn(token, self.source)

    def test_no_truth_construction(self):
        for token in ("ExecutionEvent(", "UsageRecord(",
                      "ControlCommand(", "JournalFact("):
            self.assertNotIn(token, self.source)

    def test_no_execution_or_io_surface(self):
        for token in (".run_segment(", ".invoke(", ".submit(",
                      "run_segment", "subprocess", "socket", "open(",
                      "print(", "Thread", "sleep"):
            self.assertNotIn(token, self.source)

    def test_deterministic_same_inputs_same_output(self):
        kwargs = dict(task="t", slots=template_slots(3),
                      events=running_events(("architect",)),
                      facts=(fact(ControlFactType.PAUSE_REQUESTED),),
                      usage_records=(usage(),),
                      terminal=RunStatus.COMPLETED, width=141)
        first = build(**kwargs)
        second = build(**kwargs)
        self.assertEqual(first, second)


class SimpleResult:
    def __init__(self, output):
        self.output = output
        self.status = "SUCCESS"


class SimpleOutcome:
    def __init__(self, status, final, error, transcript=()):
        self.status = status
        self.final_result = final
        self.error = error
        self.transcript = transcript


# ------------------------------- CU-TUI-4: receipt / revision / detail views


def receipt(status, reason=None, version=3, command_id="ui-1"):
    """ControlResult duck：与冻结值同字段的只读投影输入。"""
    return SimpleNamespace(status=status, reason=reason,
                           execution_version=version,
                           command_id=command_id, execution_id="e")


def transcript_record(slot_id, status="SUCCESS", step_index=0,
                      invocation_id="inv-1"):
    """RunOutcome.transcript 条目 duck（metadata-only 真相）。"""
    return SimpleNamespace(step_index=step_index, slot_id=slot_id,
                           invocation_id=invocation_id, status=status)


class ControlReceiptLineTests(unittest.TestCase):
    """CU-TUI-4 §9/§五：ControlResult 原样进回执——三态 + reason 逐字，
    零新 ControlStatus 铸造。"""

    def test_accepted_without_reason(self):
        line = projection.control_receipt_line(
            receipt(ControlStatus.ACCEPTED))
        self.assertEqual(line, "receipt ui-1: ACCEPTED v3")

    def test_rejected_reason_verbatim(self):
        line = projection.control_receipt_line(
            receipt(ControlStatus.REJECTED,
                    reason=ControlReason.ALREADY_TERMINAL,
                    version=2, command_id="ui-2"))
        self.assertEqual(line, "receipt ui-2: REJECTED v2 · ALREADY_TERMINAL")

    def test_no_op_reason_verbatim(self):
        line = projection.control_receipt_line(
            receipt(ControlStatus.NO_OP, reason=ControlReason.NOT_PAUSED,
                    version=1, command_id="ui-3"))
        self.assertEqual(line, "receipt ui-3: NO_OP v1 · NOT_PAUSED")

    def test_plain_string_values_match_enum_shape(self):
        line = projection.control_receipt_line(
            receipt("ACCEPTED", reason="ALREADY_REQUESTED",
                    version=4, command_id="ui-4"))
        self.assertEqual(
            line, "receipt ui-4: ACCEPTED v4 · ALREADY_REQUESTED")

    def test_missing_reason_attribute_renders_status_only(self):
        line = projection.control_receipt_line(
            SimpleNamespace(status="ACCEPTED", execution_version=1,
                            command_id="ui-5"))
        self.assertEqual(line, "receipt ui-5: ACCEPTED v1")


class RevisionStatusLineTests(unittest.TestCase):
    """CU-TUI-4 §10：pending 只来自注入读数（None → —），accepted/applied
    只来自账本事实，SUBMISSION 附引擎契约说明，honored 永不呈现。"""

    def test_absent_provider_renders_dash(self):
        self.assertEqual(projection.revision_status_lines((), None),
                         ("pending —",))

    def test_pending_count_rendered(self):
        self.assertEqual(projection.revision_status_lines((), 2),
                         ("pending 2",))

    def test_zero_pending_without_facts(self):
        self.assertEqual(projection.revision_status_lines((), 0),
                         ("pending 0",))

    def test_accepted_next_invocation_line(self):
        facts = (fact(ControlFactType.REVISE_REQUESTED, seq=1,
                      command_id="ui-1",
                      payload={"revision_id": "ui-1",
                               "target": "NEXT_INVOCATION"}),)
        self.assertEqual(projection.revision_status_lines(facts, 1),
                         ("pending 1",
                          "[1] REVISE_REQUESTED target=NEXT_INVOCATION"))

    def test_accepted_submission_carries_contract_note_only(self):
        facts = (fact(ControlFactType.REVISE_REQUESTED, seq=3,
                      command_id="ui-2",
                      payload={"revision_id": "ui-2",
                               "target": "SUBMISSION"}),)
        lines = projection.revision_status_lines(facts, 0)
        self.assertIn("[3] REVISE_REQUESTED target=SUBMISSION"
                      " · applies at next fresh segment", lines)
        joined = "\n".join(lines)
        self.assertNotIn("applied", joined.replace("applies", ""))

    def test_applied_line_from_journal_fact_only(self):
        facts = (fact(ControlFactType.REVISE_REQUESTED, seq=1,
                      command_id="ui-1",
                      payload={"revision_id": "ui-1",
                               "target": "NEXT_INVOCATION"}),
                 fact(ControlFactType.REVISION_APPLIED, seq=2,
                      command_id="ui-1"),
                 fact(ControlFactType.PAUSE_REQUESTED, seq=4,
                      command_id="ui-9"))
        lines = projection.revision_status_lines(facts, 0)
        self.assertIn("[2] REVISION_APPLIED", lines)
        joined = "\n".join(lines)
        self.assertNotIn("[4]", joined)   # 非修订事实零行
        self.assertNotIn("PAUSE_REQUESTED", joined)

    def test_honored_is_never_claimed(self):
        for facts in ((), (fact(ControlFactType.REVISE_REQUESTED),)):
            joined = "\n".join(
                projection.revision_status_lines(facts, 1))
            self.assertNotIn("honored", joined)

    def test_deterministic_repeat(self):
        facts = (fact(ControlFactType.REVISE_REQUESTED, seq=1,
                      command_id="ui-1",
                      payload={"revision_id": "ui-1",
                               "target": "NEXT_INVOCATION"}),)
        first = projection.revision_status_lines(facts, 1)
        second = projection.revision_status_lines(facts, 1)
        self.assertEqual(first, second)


class EventDetailLineTests(unittest.TestCase):
    """CU-TUI-4 §7：事件详情 = 既有字段子集逐字；缺席字段零行。"""

    def test_full_fields_verbatim(self):
        event = ev(ExecutionEventType.INVOCATION_FINISHED, seq=12,
                   stage="architect", runtime="rt-0", status="SUCCESS",
                   reason="SUCCESS", duration_ms=1420)
        self.assertEqual(projection.event_detail_line(event),
                         ("seq 12", "type INVOCATION_FINISHED",
                          "stage architect", "runtime rt-0",
                          "status SUCCESS", "reason SUCCESS",
                          "duration 1420ms"))

    def test_absent_duration_omits_line(self):
        event = ev(ExecutionEventType.STAGE_STARTED, seq=0,
                   stage="arch", runtime="rt-0", status="STARTED",
                   reason="STARTED")
        lines = projection.event_detail_line(event)
        self.assertEqual(lines[0], "seq 0")
        self.assertTrue(lines[1].startswith("type "))
        self.assertNotIn("duration", "\n".join(lines))

    def test_seq_line_always_present(self):
        lines = projection.event_detail_line(
            ev(ExecutionEventType.TERMINAL, seq=7, stage="SEQUENTIAL",
               runtime="ORCHESTRATION", status="COMPLETED",
               reason="COMPLETED"))
        self.assertEqual(lines[0], "seq 7")


class AgentDetailTests(unittest.TestCase):
    """CU-TUI-4 §8：Role ≠ Runtime；Status/Duration/Handoff/Result 只来自
    既有事实；缺席呈现 —；零 agent registry、零 itemization truth。"""

    def detail(self, slots, events=(), usage_records=(), outcome=None,
               **kwargs):
        return projection.agent_detail(
            slots, events, usage_records, outcome, **kwargs)

    def test_role_header_and_runtime_line_are_separate(self):
        lines = self.detail(
            (slot("architect", "rt-0", provider="prov-a"),))
        self.assertTrue(lines[0].startswith("ARCHITECT"))
        joined = "\n".join(lines)
        self.assertIn("rt-0", joined)
        self.assertIn("provider prov-a", joined)
        self.assertNotIn("ARCHITECTrt", joined.replace("\n", ""))

    def test_absent_provider_omits_note(self):
        joined = "\n".join(self.detail((slot("architect", "rt-0"),)))
        self.assertNotIn("provider", joined)

    def test_status_success_verbatim_with_duration(self):
        events = (ev(ExecutionEventType.INVOCATION_FINISHED, seq=1,
                     stage="architect", runtime="rt-0", status="SUCCESS",
                     reason="SUCCESS", duration_ms=1420),)
        joined = "\n".join(self.detail((slot("architect", "rt-0"),),
                                       events=events))
        self.assertIn("✓ SUCCESS", joined)
        self.assertIn("duration  1420ms", joined)

    def test_status_failed_verbatim(self):
        events = (ev(ExecutionEventType.INVOCATION_FINISHED, seq=1,
                     stage="architect", runtime="rt-0", status="FAILED",
                     reason="FAILED"),)
        joined = "\n".join(self.detail((slot("architect", "rt-0"),),
                                       events=events))
        self.assertIn("✗ FAILED", joined)
        self.assertIn("duration  —", joined)   # 无时长事实 → —

    def test_in_flight_and_waiting_words(self):
        started = (ev(ExecutionEventType.INVOCATION_STARTED, seq=0,
                      stage="architect", runtime="rt-0", status="STARTED",
                      reason="STARTED"),)
        joined = "\n".join(self.detail(
            (slot("architect", "rt-0"), slot("coder", "rt-1")),
            events=started))
        self.assertIn("● in-flight", joined)
        self.assertIn("○ waiting", joined)

    def test_events_of_other_slots_do_not_count(self):
        events = (ev(ExecutionEventType.INVOCATION_FINISHED, seq=1,
                     stage="coder", runtime="rt-1", status="SUCCESS",
                     reason="SUCCESS"),)
        joined = "\n".join(self.detail((slot("architect", "rt-0"),),
                                       events=events))
        self.assertIn("○ waiting", joined)

    def test_handoff_from_real_handoff_event_only(self):
        events = (ev(ExecutionEventType.HANDOFF, seq=2,
                     stage="architect", runtime="rt-1",
                     status="EMBEDDED", reason="EMBEDDED"),)
        joined = "\n".join(self.detail((slot("architect", "rt-0"),),
                                       events=events))
        self.assertIn("→ rt-1 (EMBEDDED)", joined)

    def test_no_handoff_is_dash(self):
        joined = "\n".join(self.detail((slot("architect", "rt-0"),)))
        self.assertIn("handoff   —", joined)

    def test_usage_known_rendered_unknown_dash(self):
        records = (usage(runtime="rt-0", role="architect"),
                   usage(runtime="rt-1", role="coder",
                         status=UsageObservation.UNKNOWN))
        joined = "\n".join(self.detail(
            (slot("architect", "rt-0"), slot("coder", "rt-1")),
            usage_records=records))
        self.assertIn("in=1000 out=500", joined)
        self.assertIn("usage     —", joined)

    def test_result_from_transcript_metadata_only(self):
        outcome = SimpleOutcome(
            RunStatus.COMPLETED, SimpleResult("real agent output text"),
            None, transcript=(transcript_record("step-0-architect"),))
        joined = "\n".join(self.detail(
            (slot("architect", "rt-0", stage="step-0-architect"),),
            outcome=outcome))
        self.assertIn("result    SUCCESS", joined)
        self.assertNotIn("real agent output text", joined)  # 不伪装 output

    def test_result_dash_without_outcome(self):
        joined = "\n".join(self.detail((slot("architect", "rt-0"),)))
        self.assertIn("result    —", joined)

    def test_collapsed_truncates_to_width_expanded_keeps_full(self):
        long_provider = "p" * 80
        view = slot("architect", "rt-0", provider=long_provider)
        collapsed = "\n".join(self.detail((view,), width=40))
        self.assertTrue(all(projection.display_width(line) <= 40
                            for line in collapsed.splitlines()))
        self.assertIn("...", collapsed)
        expanded = "\n".join(self.detail((view,), width=40, expanded=True))
        self.assertIn(long_provider, expanded)

    def test_ascii_mode_maps_symbols(self):
        events = (ev(ExecutionEventType.INVOCATION_FINISHED, seq=1,
                     stage="architect", runtime="rt-0", status="SUCCESS",
                     reason="SUCCESS"),
                  ev(ExecutionEventType.HANDOFF, seq=2,
                     stage="architect", runtime="rt-1",
                     status="EMBEDDED", reason="EMBEDDED"))
        joined = "\n".join(self.detail(
            (slot("architect", "rt-0"),), events=events, ascii_only=True))
        for symbol in "✓●○✗→‖■":
            self.assertNotIn(symbol, joined)
        self.assertIn("[OK]", joined)
        self.assertIn("->", joined)

    def test_sections_follow_plan_order(self):
        joined = "\n".join(self.detail(
            (slot("architect", "rt-0"), slot("coder", "rt-1"))))
        self.assertLess(joined.index("ARCHITECT"), joined.index("CODER"))

    def test_deterministic_repeat(self):
        events = running_events(("architect", "coder"))
        first = self.detail(template_slots(2), events=events)
        second = self.detail(template_slots(2), events=events)
        self.assertEqual(first, second)


# ------------------------------- CU-TUI-5: first-run funnel pure renderers


def binding(role, runtime, provider, identity=None):
    """CompositionBinding duck：projection 只读三字段呈现面；
    canonical_runtime_identity 是不透明载荷（本层零计算零消费）。"""
    return SimpleNamespace(role=role, runtime_id=runtime,
                           provider_id=provider,
                           canonical_runtime_identity=identity)


def composition(bindings=(), blocked_reason=None, blocked_hint=None):
    """DefaultComposition duck（cockpit_entry 值对象的呈现面形状）。"""
    return SimpleNamespace(roles=tuple(b.role for b in bindings),
                           bindings=tuple(bindings),
                           blocked_reason=blocked_reason,
                           blocked_hint=blocked_hint)


class FunnelPreviewLinesTests(unittest.TestCase):
    """CU-TUI-5 §十三/G：preview 披露行——分配结果可预期、角色对齐、
    blocked 时零预览块。"""

    def test_two_runtime_preview(self):
        lines = projection.funnel_preview_lines(composition((
            binding("architect", "codex-cli", "openai"),
            binding("coder", "claude-cli", "anthropic"))))
        self.assertEqual(lines, (
            "Collaboration plan (default)",
            "  architect  ← codex-cli · openai",
            "  coder      ← claude-cli · anthropic"))

    def test_four_runtime_preview_sorted_roles(self):
        lines = projection.funnel_preview_lines(composition((
            binding("architect", "rt-a", "p-a"),
            binding("coder", "rt-b", "p-b"),
            binding("tester", "rt-c", "p-c"),
            binding("reviewer", "rt-d", "p-d"))))
        self.assertEqual(lines, (
            "Collaboration plan (default)",
            "  architect  ← rt-a · p-a",
            "  coder      ← rt-b · p-b",
            "  tester     ← rt-c · p-c",
            "  reviewer   ← rt-d · p-d"))

    def test_blocked_composition_renders_no_preview(self):
        blocked = composition(blocked_reason="needs 2 VERIFIED (found 1)",
                              blocked_hint="run qualify")
        self.assertEqual(projection.funnel_preview_lines(blocked), ())

    def test_ascii_mode_maps_arrow(self):
        lines = projection.funnel_preview_lines(composition((
            binding("architect", "rt-a", "p"),
            binding("coder", "rt-b", "p"))), ascii_only=True)
        self.assertEqual(lines, (
            "Collaboration plan (default)",
            "  architect  <- rt-a · p",
            "  coder      <- rt-b · p"))

    def test_deterministic_repeat(self):
        pool = composition((binding("architect", "rt-a", "p"),
                            binding("coder", "rt-b", "p")))
        self.assertEqual(projection.funnel_preview_lines(pool),
                         projection.funnel_preview_lines(pool))


class FunnelBlockedLineTests(unittest.TestCase):
    """CU-TUI-5 §十三：BLOCKED 时恰一行诚实原因（hint 独立于
    Enter 反馈，不混入首屏原因行）。"""

    def test_blocked_reason_verbatim_single_line(self):
        blocked = composition(blocked_reason="default collaboration needs "
                                             "at least 2 VERIFIED runtimes "
                                             "(found 1)")
        self.assertEqual(
            projection.funnel_blocked_line(blocked),
            "default collaboration needs at least 2 VERIFIED runtimes "
            "(found 1)")

    def test_unblocked_renders_none(self):
        ok = composition((binding("architect", "rt-a", "p"),
                          binding("coder", "rt-b", "p")))
        self.assertIsNone(projection.funnel_blocked_line(ok))


class FunnelEnterFeedbackTests(unittest.TestCase):
    """CU-TUI-5 §十二 Enter 语义的状态行（纯渲染面）：空白 no-op 提示、
    BLOCKED 原因+hint、就绪零行（Start 交回 TUI/entry）。"""

    def test_blank_task_is_no_op_hint(self):
        for text in ("", "   "):
            self.assertEqual(
                projection.funnel_enter_lines(
                    composition((binding("architect", "rt-a", "p"),
                                 binding("coder", "rt-b", "p"))), text),
                ("describe the task first",))

    def test_blocked_preview_shows_reason_and_hint(self):
        blocked = composition(
            blocked_reason="default collaboration needs at least 2 "
                           "VERIFIED runtimes (found 1)",
            blocked_hint="no persisted qualification evidence: run "
                         "`dual-agent qualify` first")
        self.assertEqual(
            projection.funnel_enter_lines(blocked, "my task"),
            ("default collaboration needs at least 2 VERIFIED runtimes "
             "(found 1)",
             "no persisted qualification evidence: run "
             "`dual-agent qualify` first"))

    def test_blocked_without_hint_renders_reason_only(self):
        blocked = composition(blocked_reason="pool gone")
        self.assertEqual(projection.funnel_enter_lines(blocked, "t"),
                         ("pool gone",))

    def test_ready_renders_no_lines(self):
        ok = composition((binding("architect", "rt-a", "p"),
                          binding("coder", "rt-b", "p")))
        self.assertEqual(projection.funnel_enter_lines(ok, "my task"), ())


class FunnelChangedLinesTests(unittest.TestCase):
    """CU-TUI-5 §十：COMPOSITION_CHANGED 横幅 = TUI 私有呈现词 +
    逐条诚实原因（不进 engine observation 词表——守卫另测）。"""

    def test_banner_and_indented_reasons(self):
        lines = projection.funnel_changed_lines((
            "runtime rt-b no longer VERIFIED",
            "runtime rt-a identity changed"))
        self.assertEqual(lines, (
            "collaboration plan changed:",
            "  runtime rt-b no longer VERIFIED",
            "  runtime rt-a identity changed"))

    def test_ascii_mode(self):
        lines = projection.funnel_changed_lines(
            ("new VERIFIED runtime rt-c changes the default plan",),
            ascii_only=True)
        self.assertEqual(lines, (
            "collaboration plan changed:",
            "  new VERIFIED runtime rt-c changes the default plan"))


class FunnelErrorLinesTests(unittest.TestCase):
    """CU-TUI-5 §J：CompositionError → 漏斗红行（reason+detail 原词汇、
    hint 原文第二行；缺席诚实省略）。"""

    def test_reason_detail_and_hint(self):
        error = SimpleNamespace(
            reason="RUNTIME_NOT_QUALIFIED",
            detail="default collaboration needs at least 2 VERIFIED "
                   "runtimes (found 0)",
            hint="no persisted qualification evidence: run "
                 "`dual-agent qualify` first")
        self.assertEqual(projection.funnel_error_lines(error), (
            "RUNTIME_NOT_QUALIFIED: default collaboration needs at "
            "least 2 VERIFIED runtimes (found 0)",
            "no persisted qualification evidence: run "
            "`dual-agent qualify` first"))

    def test_without_hint_single_line(self):
        error = SimpleNamespace(reason="INVALID_TASK",
                                detail="task must be a non-empty string",
                                hint=None)
        self.assertEqual(
            projection.funnel_error_lines(error),
            ("INVALID_TASK: task must be a non-empty string",))


class FunnelFirstScreenTests(unittest.TestCase):
    """CU-TUI-5 §十三：首屏恰六要素（header / 指令 / 输入行 / 预览块 /
    两键提示 / blocked 原因行）——零 id 类调试元数据。"""

    OK = composition((binding("architect", "codex-cli", "openai"),
                      binding("coder", "claude-cli", "anthropic")))

    def test_six_elements_exact_order(self):
        lines = projection.funnel_first_screen("2.5.0", self.OK, "")
        self.assertEqual(lines, (
            "dual-agent cockpit · 2.5.0",
            "Describe the collaboration task",
            "> ",
            "Collaboration plan (default)",
            "  architect  ← codex-cli · openai",
            "  coder      ← claude-cli · anthropic",
            "Enter start · q quit"))

    def test_prefilled_buffer_in_input_line(self):
        lines = projection.funnel_first_screen("2.5.0", self.OK,
                                               "fix the login bug")
        self.assertIn("> fix the login bug", lines)

    def test_empty_version_omits_separator(self):
        lines = projection.funnel_first_screen("", self.OK, "")
        self.assertEqual(lines[0], "dual-agent cockpit")

    def test_blocked_screen_replaces_preview_with_reason_line(self):
        blocked = composition(
            blocked_reason="default collaboration needs at least 2 "
                           "VERIFIED runtimes (found 1)",
            blocked_hint="run qualify first")
        lines = projection.funnel_first_screen("2.5.0", blocked, "")
        self.assertEqual(lines, (
            "dual-agent cockpit · 2.5.0",
            "Describe the collaboration task",
            "> ",
            "default collaboration needs at least 2 VERIFIED runtimes "
            "(found 1)",
            "Enter start · q quit"))

    def test_no_id_class_debug_metadata(self):
        for lines in (projection.funnel_first_screen("2.5.0", self.OK, "t"),
                      projection.funnel_first_screen(
                          "2.5.0", composition(blocked_reason="b"), "t")):
            joined = "\n".join(lines)
            for token in ("task_id", "execution_id", "task-",
                          "cockpit-task", "UUID", "uuid", "packet",
                          "adapter"):
                self.assertNotIn(token, joined)

    def test_unsafe_buffer_redacted(self):
        secret = "sk-ant-api03-ABCDEFGHIJKLMNOPQRSTUVWXYZ1234"
        lines = projection.funnel_first_screen("2.5.0", self.OK, secret)
        self.assertIn("> [redacted: unsafe content]", lines)
        self.assertNotIn(secret, "\n".join(lines))

    def test_long_buffer_truncated_to_width(self):
        lines = projection.funnel_first_screen(
            "2.5.0", self.OK, "x" * 200, width=40)
        input_line = lines[2]
        self.assertLessEqual(projection.display_width(input_line), 40)
        self.assertTrue(input_line.endswith("..."))

    def test_ascii_mode(self):
        lines = projection.funnel_first_screen("2.5.0", self.OK, "t",
                                               ascii_only=True)
        joined = "\n".join(lines)
        self.assertNotIn("←", joined)
        self.assertIn("<-", joined)

    def test_deterministic_repeat(self):
        first = projection.funnel_first_screen("2.5.0", self.OK, "task")
        second = projection.funnel_first_screen("2.5.0", self.OK, "task")
        self.assertEqual(first, second)


class FunnelRendererGuardTests(unittest.TestCase):
    """CU-TUI-5 v2.1 P1-1 #6 / 守卫 H-2：projection 渲染面零 identity
    计算、零 binding 组合逻辑、零 engine 词表污染。"""

    def setUp(self):
        with open(projection.__file__, "r", encoding="utf-8") as handle:
            self.source = handle.read()

    def test_no_identity_computation_tokens(self):
        for token in ("canonical_runtime_identity", "config_fingerprint",
                      "fingerprint"):
            self.assertNotIn(token, self.source)

    def test_no_binding_composition_logic(self):
        # sorted+zip 默认指派只许存在于 entry 的
        # resolve_default_composition（单源绑定守卫 H-2）
        for token in ("resolve_default_composition", "DEFAULT_ROLE_TEMPLATES[",
                      "verified_pool", "zip("):
            self.assertNotIn(token, self.source)

    def test_funnel_words_absent_from_engine_observation_vocab(self):
        # COMPOSITION_CHANGED 等漏斗词绝不能写进引擎观察词表模块
        import console_observation as observation
        with open(observation.__file__, "r", encoding="utf-8") as handle:
            obs_source = handle.read()
        for token in ("COMPOSITION_CHANGED", "NOT_STARTED", "COMPOSING"):
            self.assertNotIn(token, obs_source)

    def test_real_entry_composition_renders(self):
        # duck 兼容：真实 cockpit_entry.DefaultComposition 直接渲染
        import cockpit_entry
        pool = (SimpleNamespace(runtime_id="rt-a", provider_id="p-a",
                                identity=("rt-a", "p-a", None, "f")),
                SimpleNamespace(runtime_id="rt-b", provider_id="p-b",
                                identity=("rt-b", "p-b", None, "f")))
        real = cockpit_entry.resolve_default_composition(pool)
        lines = projection.funnel_preview_lines(real)
        self.assertEqual(lines, (
            "Collaboration plan (default)",
            "  architect  ← rt-a · p-a",
            "  coder      ← rt-b · p-b"))


if __name__ == "__main__":
    unittest.main()
