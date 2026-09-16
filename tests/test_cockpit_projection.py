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
    """Scenarios 1-3 (R1 pipeline): fixed 2/3/4 role chains — 单协作组
    横排管线，零 padding 槽位，每个真实 agent 恰一个 cell。"""

    def test_two_agent_layout(self):
        state = build(task="t", slots=template_slots(2),
                      events=running_events(("architect", "coder")),
                      width=100)
        # MAIN_WIDE 档（width=100）2 cell 单行：heads/runtimes/states
        # 三行 + 选中指针行
        self.assertEqual(len(state.collaboration_lines), 4)
        self.assertIn("ARCHITECT", state.collaboration_lines[0])
        self.assertIn("CODER", state.collaboration_lines[0])
        # first slot finished ok, second waiting (stage started, not
        # yet invoked), none invented
        self.assertIn("✓", state.collaboration_lines[2])
        self.assertIn("◐", state.collaboration_lines[2])
        self.assertNotIn("REVIEWER",
                         "\n".join(state.collaboration_lines))

    def test_three_agent_layout(self):
        state = build(task="t", slots=template_slots(3), events=(),
                      width=100)
        joined = "\n".join(state.collaboration_lines)
        self.assertIn("REVIEWER", joined)
        self.assertEqual(joined.count("NOT_STARTED"), 3)

    def test_four_agent_layout(self):
        state = build(task="t", slots=template_slots(4), events=(),
                      width=100)
        joined = "\n".join(state.collaboration_lines)
        for role in ("ARCHITECT", "CODER", "TESTER", "REVIEWER"):
            self.assertIn(role, joined)
        self.assertEqual(joined.count("NOT_STARTED"), 4)

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
        joined = "\n".join(state.collaboration_lines)
        self.assertIn("✓", joined)   # architect finished SUCCESS
        self.assertIn("✗", joined)   # coder finished FAILED
        self.assertIn("●", joined)   # reviewer in flight

    def test_runtime_ids_rendered_on_rows(self):
        state = build(task="t", slots=template_slots(2),
                      events=running_events(("architect", "coder")),
                      width=100)
        joined = "\n".join(state.collaboration_lines)
        self.assertIn("rt-0", joined)
        self.assertIn("rt-1", joined)

    def test_same_runtime_reuse_renders_both_slots(self):
        slots_ = (slot("architect", "rt-0"), slot("coder", "rt-0"))
        state = build(task="t", slots=slots_, events=(), width=100)
        self.assertEqual(
            sum(line.count("rt-0") for line in state.collaboration_lines),
            2)


# -------------------------------------- scenarios 4-7: responsive breakpoints


class ResponsiveBreakpointTests(unittest.TestCase):
    """Scenarios 4-7: <80 degraded / 80-99 main / 100-139 capped / >=140
    main+context."""

    def test_degraded_below_80(self):
        state = build(task="t", slots=template_slots(3), events=(),
                      width=79)
        self.assertEqual(state.tier, "DEGRADED")
        self.assertEqual(state.context_lines, ())
        # 纵向链：↳ 续行前缀在场，且不出现水平连接符（同组竖排）
        joined = "\n".join(state.collaboration_lines)
        self.assertIn("ARCHITECT", joined)
        self.assertIn("↳", joined)
        self.assertNotIn("──→", joined)
        self.assertNotIn("┄┄→", joined)

    def test_main_tier_80_to_99(self):
        state = build(task="t", slots=template_slots(3), events=(),
                      width=90)
        self.assertEqual(state.tier, "MAIN")
        # MAIN 档 2 列：[A,B] 行块（3 行+指针）+ ↳[C] 续行块（3 行）
        self.assertEqual(len(state.collaboration_lines), 7)
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
        # Phase V：+1 状态头行（✓ COMPLETED）
        self.assertLessEqual(len(state.result_lines), 5)
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
        fields.extend(state.activity_lines)
        fields.extend(state.detail_lines)
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
            for symbol in ("✓", "●", "○", "✗", "→", "‖", "■", "❚", "▫",
                           "◐", "◉", "»", "▸", "⊘",
                           "─", "┄", "↓", "┆", "↳", "▲", "▶", "▼"):
                self.assertNotIn(symbol, text)
        # ABORTED 终态叠加：在途槽呈现 "!"，未开始槽呈现 "."
        self.assertIn(".", "\n".join(state.collaboration_lines))
        self.assertIn("!", state.badge)  # ABORTED badge wins

    def test_ascii_arrow_map(self):
        events = (ev(ExecutionEventType.HANDOFF, seq=0, stage="architect",
                     runtime="rt-1"),)
        state = build(task="t", slots=template_slots(2), events=events,
                      ascii_only=True, width=100)
        self.assertIn("->", "\n".join(state.activity_lines))


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
    """CU-TUI-4 §9/§五 + Phase P §九：控制结果原样进回执——三态 +
    reason 逐字，状态符号前缀；kind 为呈现层已知意图词。零新
    ControlStatus 铸造。"""

    def test_accepted_without_reason(self):
        line = projection.control_receipt_line(
            receipt(ControlStatus.ACCEPTED))
        self.assertEqual(line, "✓ ACCEPTED · ui-1 v3")

    def test_rejected_reason_verbatim(self):
        line = projection.control_receipt_line(
            receipt(ControlStatus.REJECTED,
                    reason=ControlReason.ALREADY_TERMINAL,
                    version=2, command_id="ui-2"))
        self.assertEqual(line, "✗ REJECTED · ui-2 v2 · ALREADY_TERMINAL")

    def test_no_op_reason_verbatim(self):
        line = projection.control_receipt_line(
            receipt(ControlStatus.NO_OP, reason=ControlReason.NOT_PAUSED,
                    version=1, command_id="ui-3"))
        self.assertEqual(line, "⊘ NO_OP · ui-3 v1 · NOT_PAUSED")

    def test_plain_string_values_match_enum_shape(self):
        line = projection.control_receipt_line(
            receipt("ACCEPTED", reason="ALREADY_REQUESTED",
                    version=4, command_id="ui-4"))
        self.assertEqual(
            line, "✓ ACCEPTED · ui-4 v4 · ALREADY_REQUESTED")

    def test_missing_reason_attribute_renders_status_only(self):
        line = projection.control_receipt_line(
            SimpleNamespace(status="ACCEPTED", execution_version=1,
                            command_id="ui-5"))
        self.assertEqual(line, "✓ ACCEPTED · ui-5 v1")

    def test_kind_rides_after_status(self):
        line = projection.control_receipt_line(
            receipt(ControlStatus.ACCEPTED), kind="PAUSE")
        self.assertEqual(line, "✓ ACCEPTED PAUSE · ui-1 v3")

    def test_ascii_only_degrades_glyphs(self):
        line = projection.control_receipt_line(
            receipt(ControlStatus.NO_OP), kind="RESUME", ascii_only=True)
        self.assertEqual(line, "[SKIP] NO_OP RESUME · ui-1 v3")


class WorkerFailureLineTests(unittest.TestCase):
    """Phase P §四：worker 残余异常的诚实呈现行。"""

    def test_error_type_and_message_visible(self):
        lines = projection.worker_failure_lines(
            RuntimeError("driver-honest-failure"))
        self.assertEqual(
            lines[0], "✗ WORKER ERROR RuntimeError: driver-honest-failure")
        self.assertIn("re-raise", lines[1])

    def test_long_message_truncated(self):
        lines = projection.worker_failure_lines(
            ValueError("x" * 200))
        self.assertLessEqual(len(lines[0]), 100)

    def test_ascii_only_degrades(self):
        lines = projection.worker_failure_lines(
            RuntimeError("boom"), ascii_only=True)
        self.assertTrue(lines[0].startswith("[FAIL] WORKER ERROR"))


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
            "Enter start · c compose · q quit"))

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
            "Enter start · c compose · q quit"))

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


# ------------------------ CU-TUI-6 Phase V: agent panel / activity / header


def _slot_events(role, runtime, *, started=True, invoked=False,
                 finished=None):
    """单槽位事件流构造器（Phase V 状态推导测试专用）。"""
    events = []
    seq = 0
    if started:
        events.append(ev(ExecutionEventType.STAGE_STARTED, seq=seq,
                         stage=role, runtime=runtime))
        seq += 1
    if invoked:
        events.append(ev(ExecutionEventType.INVOCATION_STARTED, seq=seq,
                         stage=role, runtime=runtime, status="STARTED"))
        seq += 1
    if finished is not None:
        events.append(ev(ExecutionEventType.INVOCATION_FINISHED, seq=seq,
                         stage=role, runtime=runtime, status=finished))
    return tuple(events)


class PipelineCellStateTests(unittest.TestCase):
    """R1 §五/§七：cell 状态词 + 符号——全部来自真实事件流（STAGE_*/
    INVOCATION_*）与既有 lifecycle 投影的叠加，零静默推断；selected /
    expanded 为独立呈现态（marker 列 + ▲ 指针）。"""

    def _panel(self, slots_, events, *, lifecycle="RUNNING", **kwargs):
        return projection.pipeline_lines(
            slots_, events, lifecycle=lifecycle, **kwargs)

    def test_two_agent_cells_with_derived_statuses(self):
        events = running_events(("architect", "coder"))
        lines = self._panel(template_slots(2), events)
        self.assertEqual(len(lines), 4)          # heads/runtimes/states/▲
        self.assertIn("ARCHITECT", lines[0])
        self.assertIn("rt-0", lines[1])
        self.assertIn("DONE", lines[2])
        self.assertIn("WAITING", lines[2])       # STAGE_STARTED 未 INVOCATION

    def test_all_eight_states_derive_from_real_facts(self):
        s = slot("a", "rt-0")
        cases = (
            ("NOT_STARTED", (), "RUNNING"),
            ("WAITING", _slot_events("a", "rt-0", invoked=False), "RUNNING"),
            ("RUNNING", _slot_events("a", "rt-0", invoked=True), "RUNNING"),
            ("DONE", _slot_events("a", "rt-0", invoked=True,
                                  finished="SUCCESS"), "RUNNING"),
            ("FAILED", _slot_events("a", "rt-0", invoked=True,
                                    finished="FAILED"), "RUNNING"),
            ("PAUSED", _slot_events("a", "rt-0", invoked=True), "PAUSED"),
            ("PARKED", _slot_events("a", "rt-0", invoked=True), "PARKED"),
            ("ABORTED", _slot_events("a", "rt-0", invoked=True), "ABORTED"),
        )
        for word, events, lifecycle in cases:
            lines = self._panel((s,), events, lifecycle=lifecycle)
            self.assertEqual(len(lines), 4, word)
            self.assertIn(word, lines[2], word)  # 状态词在 states 行

    def test_lifecycle_overlay_never_overwrites_finished_fact(self):
        s = slot("a", "rt-0")
        events = _slot_events("a", "rt-0", invoked=True, finished="SUCCESS")
        lines = self._panel((s,), events, lifecycle="PAUSED")
        self.assertIn("DONE", lines[2])

    def test_glyphs_match_approved_table(self):
        s = slot("a", "rt-0")
        table = (("NOT_STARTED", "○"), ("WAITING", "◐"), ("RUNNING", "●"),
                 ("DONE", "✓"), ("FAILED", "✗"), ("PAUSED", "❚❚"),
                 ("PARKED", "▫"), ("ABORTED", "⊘"))
        for word, glyph in table:
            events = {
                "NOT_STARTED": (),
                "WAITING": _slot_events("a", "rt-0"),
                "RUNNING": _slot_events("a", "rt-0", invoked=True),
                "DONE": _slot_events("a", "rt-0", invoked=True,
                                     finished="SUCCESS"),
                "FAILED": _slot_events("a", "rt-0", invoked=True,
                                       finished="FAILED"),
                "PAUSED": _slot_events("a", "rt-0", invoked=True),
                "PARKED": _slot_events("a", "rt-0", invoked=True),
                "ABORTED": _slot_events("a", "rt-0", invoked=True),
            }[word]
            lifecycle = word if word in ("PAUSED", "PARKED", "ABORTED") \
                else "RUNNING"
            lines = self._panel((s,), events, lifecycle=lifecycle)
            self.assertIn(glyph, lines[0], word)  # 符号在 heads 行
            self.assertIn(glyph, lines[2], word)  # 状态符号在 states 行

    def test_pulse_flips_only_running_glyph(self):
        running = (slot("a", "rt-0"), slot("b", "rt-1"))
        events = (_slot_events("a", "rt-0", invoked=True)
                  + _slot_events("b", "rt-1", invoked=False))
        off = "\n".join(self._panel(running, events))
        on = "\n".join(self._panel(running, events, pulse=True))
        self.assertIn("●", off)
        self.assertIn("◐", on)                # WAITING 不脉冲
        self.assertNotIn("◉", off)
        self.assertIn("◉", on)
        self.assertNotIn("●", on)

    def test_ascii_glyph_set_is_the_approved_one(self):
        s = slot("a", "rt-0")
        table = (("NOT_STARTED", ".", "RUNNING"),
                 ("WAITING", "~", "RUNNING"),
                 ("RUNNING", "*", "RUNNING"),
                 ("DONE", "+", "RUNNING"),
                 ("FAILED", "x", "RUNNING"),
                 ("PAUSED", "||", "PAUSED"),
                 ("PARKED", "-", "PARKED"),
                 ("ABORTED", "!", "ABORTED"))
        for word, glyph, lifecycle in table:
            events = {
                "NOT_STARTED": (),
                "WAITING": _slot_events("a", "rt-0"),
                "RUNNING": _slot_events("a", "rt-0", invoked=True),
                "DONE": _slot_events("a", "rt-0", invoked=True,
                                     finished="SUCCESS"),
                "FAILED": _slot_events("a", "rt-0", invoked=True,
                                       finished="FAILED"),
                "PAUSED": _slot_events("a", "rt-0", invoked=True),
                "PARKED": _slot_events("a", "rt-0", invoked=True),
                "ABORTED": _slot_events("a", "rt-0", invoked=True),
            }[word]
            joined = "\n".join(self._panel((s,), events,
                                           lifecycle=lifecycle,
                                           ascii_only=True))
            self.assertIn(glyph, joined, word)
            for symbol in "●○◐✓✗❚▫⊘◉":
                self.assertNotIn(symbol, joined, word)

    def test_pulse_ascii_stays_star(self):
        s = slot("a", "rt-0")
        events = _slot_events("a", "rt-0", invoked=True)
        for pulse in (False, True):
            joined = "\n".join(self._panel((s,), events, pulse=pulse,
                                           ascii_only=True))
            self.assertIn("*", joined)

    def test_four_agent_cells_carry_role_and_runtime(self):
        joined = "\n".join(self._panel(template_slots(4), (), width=120))
        for role in ("ARCHITECT", "CODER", "TESTER", "REVIEWER"):
            self.assertIn(role, joined)
        for runtime in ("rt-0", "rt-1", "rt-2", "rt-3"):
            self.assertIn(runtime, joined)

    def test_degraded_tier_blocks_never_exceed_width(self):
        lines = self._panel(template_slots(4), (), tier="DEGRADED",
                            width=60)
        for line in lines:
            self.assertLessEqual(projection.display_width(line), 60)

    def test_lines_never_exceed_width(self):
        long_rt = slot("architect", "rt-" + "x" * 90)
        lines = self._panel((long_rt,), (), width=80)
        for line in lines:
            self.assertLessEqual(projection.display_width(line), 80)

    def test_selected_marker_pointer_and_reserved_column(self):
        two = (slot("architect", "rt-0"), slot("coder", "rt-1"))
        lines = self._panel(two, (), selected_index=1)
        # 未选中 cell 预留 marker 列（两空格开头，布局稳定不跳动）
        self.assertTrue(lines[0].startswith("  "))
        self.assertIn("▶ ○ CODER", lines[0])
        # ▲ 指针对齐到选中 cell 的起始列
        offset = lines[0].index("▶")
        self.assertEqual(lines[3][:offset].strip(), "")
        self.assertEqual(lines[3][offset], "▲")

    def test_expanded_marker_variants(self):
        two = (slot("architect", "rt-0"), slot("coder", "rt-1"))
        # selected ∧ expanded → ▼（折叠提示）
        lines = self._panel(two, (), selected_index=0,
                            expanded_stage="architect")
        self.assertIn("▼ ○ ARCHITECT", lines[0])
        self.assertNotIn("▶", lines[0])
        # selected 未 expanded → ▶；expanded 在别处 → ▼ 留在原 cell
        lines = self._panel(two, (), selected_index=0,
                            expanded_stage="coder")
        self.assertIn("▶ ○ ARCHITECT", lines[0])
        self.assertIn("▼ ○ CODER", lines[0])


class ActivityTailTests(unittest.TestCase):
    """Phase V §十三/十四：活动尾窗 = EventIndex 同源只读尾 K 条——
    零第二套事件、零伪造时间戳（事件无时间戳字段是冻结事实）、
    空态诚实呈现。"""

    def test_empty_shows_honest_state(self):
        self.assertEqual(projection.activity_tail_lines(()),
                         ("No activity yet",))

    def test_one_event_line(self):
        events = (ev(ExecutionEventType.INVOCATION_STARTED, seq=3,
                     stage="coder", runtime="rt-1", status="STARTED"),)
        lines = projection.activity_tail_lines(events)
        self.assertEqual(len(lines), 1)
        self.assertIn("[3]", lines[0])
        self.assertIn("coder started", lines[0])

    def test_many_events_keep_only_tail_window(self):
        events = tuple(
            ev(ExecutionEventType.STAGE_STARTED, seq=index, stage="a",
               runtime="rt-0")
            for index in range(20))
        lines = projection.activity_tail_lines(events, limit=6)
        self.assertEqual(len(lines), 6)
        self.assertIn("[19]", lines[-1])
        self.assertIn("[14]", lines[0])
        self.assertNotIn("[13]", "\n".join(lines))

    def test_handoff_renders_real_arrow(self):
        events = (ev(ExecutionEventType.HANDOFF, seq=1, stage="architect",
                     runtime="rt-1"),)
        lines = projection.activity_tail_lines(events)
        self.assertIn("architect → rt-1", lines[0])

    def test_finished_carries_status_and_duration(self):
        events = (ev(ExecutionEventType.INVOCATION_FINISHED, seq=2,
                     stage="coder", runtime="rt-1", status="SUCCESS",
                     duration_ms=120),)
        lines = projection.activity_tail_lines(events)
        self.assertIn("coder finished SUCCESS (120ms)", lines[0])

    def test_reveal_marker_only_on_given_seqs(self):
        events = (_slot_events("a", "rt-0")[0],
                  ev(ExecutionEventType.INVOCATION_STARTED, seq=1,
                     stage="a", runtime="rt-0", status="STARTED"))
        lines = projection.activity_tail_lines(events, reveal_seqs=(1,))
        self.assertFalse(lines[0].startswith("▸"))
        self.assertTrue(lines[1].startswith("▸ "))

    def test_ascii_degrades_reveal_and_arrow(self):
        events = (ev(ExecutionEventType.HANDOFF, seq=0, stage="architect",
                     runtime="rt-1"),)
        lines = projection.activity_tail_lines(events, reveal_seqs=(0,),
                                               ascii_only=True)
        self.assertIn("->", lines[0])
        self.assertIn("> ", lines[0])
        self.assertNotIn("▸", lines[0])
        self.assertNotIn("→", lines[0])

    def test_no_synthetic_events(self):
        events = _slot_events("a", "rt-0", invoked=True)
        lines = projection.activity_tail_lines(events)
        self.assertEqual(len(lines), len(events))
        for line, event in zip(lines, events):
            self.assertIn(f"[{event.sequence}]", line)

    def test_long_lines_truncated_to_width(self):
        events = (ev(ExecutionEventType.STAGE_STARTED, seq=0,
                     stage="a" * 200, runtime="rt-0"),)
        lines = projection.activity_tail_lines(events, width=40)
        self.assertLessEqual(projection.display_width(lines[0]), 40)

    def test_deterministic(self):
        events = _slot_events("a", "rt-0", invoked=True)
        self.assertEqual(projection.activity_tail_lines(events),
                         projection.activity_tail_lines(events))


class HeaderStateTests(unittest.TestCase):
    """Phase V §九：header 承载 product identity + 会话状态（右对齐），
    生命周期符号按已批准表，ASCII 降级不失可读。"""

    def test_running_state_right_aligned_with_identity(self):
        state = build(task="t", slots=template_slots(2),
                      events=running_events(("architect",)), width=100,
                      version="9.9")
        self.assertTrue(state.header_line.endswith("● RUNNING"))
        self.assertIn("dual-agent cockpit", state.header_line)
        self.assertIn("v9.9", state.header_line)
        self.assertLessEqual(projection.display_width(state.header_line),
                             100)

    def test_each_lifecycle_glyph_in_header(self):
        run_state = SimpleNamespace()
        cases = (
            ("COMPLETED", "✓", dict(terminal=RunStatus.COMPLETED)),
            ("FAILED", "✗", dict(terminal=RunStatus.FAILED)),
            ("ABORTED", "⊘", dict(terminal=RunStatus.ABORTED)),
            ("PAUSED", "❚❚", dict(
                run_state=run_state,
                facts=(fact(ControlFactType.PAUSE_REQUESTED),))),
            ("PARKED", "▫", dict(run_state=run_state)),
        )
        for lifecycle, glyph, kwargs in cases:
            state = build(task="t", slots=template_slots(2), width=100,
                          **kwargs)
            self.assertIn(f"{glyph} {lifecycle}", state.header_line,
                          lifecycle)

    def test_ascii_lifecycle_glyphs(self):
        state = build(task="t", slots=template_slots(2),
                      events=running_events(("architect",)),
                      terminal=RunStatus.ABORTED, width=100,
                      ascii_only=True)
        self.assertIn("! ABORTED", state.header_line)
        self.assertNotIn("⊘", state.header_line)

    def test_narrow_header_never_overflows(self):
        state = build(task="t", slots=template_slots(2),
                      events=running_events(("architect",)), width=44)
        self.assertLessEqual(projection.display_width(state.header_line),
                             44)
        self.assertIn("RUNNING", state.header_line)


class ResultPresentationTests(unittest.TestCase):
    """Phase V §十五/十六：终态结果带状态头行；PARKED 不伪造结果；
    结果揭示前缀是纯呈现参数。"""

    def test_completed_header_then_content(self):
        outcome = SimpleOutcome(RunStatus.COMPLETED,
                                SimpleResult("done text"), None)
        state = build(task="t", slots=template_slots(2),
                      last_outcome=outcome, width=100)
        self.assertEqual(state.result_lines[0], "✓ COMPLETED")
        self.assertIn("done text", "\n".join(state.result_lines[1:]))

    def test_failed_header_and_error_lines(self):
        outcome = SimpleOutcome(RunStatus.FAILED, None, RuntimeError("boom"))
        state = build(task="t", slots=template_slots(2),
                      last_outcome=outcome, width=100)
        self.assertEqual(state.result_lines[0], "✗ FAILED")
        joined = "\n".join(state.result_lines)
        self.assertIn("ERROR", joined)
        self.assertIn("boom", joined)

    def test_parked_still_single_honest_line(self):
        outcome = SimpleOutcome(RunStatus.PARKED, None, None)
        state = build(task="t", slots=template_slots(2),
                      last_outcome=outcome, width=100)
        self.assertEqual(state.result_lines, ("PARKED · awaiting resume",))

    def test_result_reveal_prefix_is_pure_input(self):
        outcome = SimpleOutcome(RunStatus.COMPLETED,
                                SimpleResult("done text"), None)
        base = dict(task="t", slots=template_slots(2), width=100,
                    last_outcome=outcome)
        revealed = build(**base, result_reveal=True)
        plain = build(**base, result_reveal=False)
        self.assertEqual(revealed.result_lines[0], "» ✓ COMPLETED")
        self.assertEqual(revealed.result_lines[1:],
                         plain.result_lines[1:])
        self.assertNotIn("»", plain.result_lines[0])


class AnimationPurityTests(unittest.TestCase):
    """Phase V §四/§二十六：动画参数（pulse/reveal/result_reveal）只改
    rendering——除对应呈现字段外，一切投影输出逐字节不变；动画绝不
    被解释为执行状态。"""

    def _kwargs(self):
        # coder 在途（INVOCATION_STARTED 未 FINISHED）→ 面板存在
        # RUNNING 槽位，脉冲才有可见翻转面
        events = tuple(running_events(("architect", "coder"))) + (
            ev(ExecutionEventType.INVOCATION_STARTED, seq=3,
               stage="coder", runtime="rt-1", status="STARTED"),)
        return dict(task="t", slots=template_slots(2), events=events,
                    width=100)

    def test_pulse_changes_only_agent_panel(self):
        base = self._kwargs()
        off = build(**base, pulse=False)
        on = build(**base, pulse=True)
        self.assertNotEqual(off.collaboration_lines,
                            on.collaboration_lines)
        for name in ("header_line", "task_line", "badge", "progress_line",
                     "tokens_line", "result_lines", "context_lines",
                     "trace_obs", "trace_ctrl", "trace_usage", "lifecycle",
                     "tier", "activity_lines", "detail_lines"):
            self.assertEqual(getattr(off, name), getattr(on, name), name)

    def test_reveal_changes_only_activity(self):
        base = self._kwargs()
        plain = build(**base, reveal_seqs=())
        marked = build(**base, reveal_seqs=(0,))
        self.assertNotEqual(plain.activity_lines, marked.activity_lines)
        for name in ("collaboration_lines", "header_line", "task_line",
                     "badge", "progress_line", "tokens_line",
                     "result_lines", "context_lines", "trace_obs",
                     "trace_ctrl", "trace_usage", "lifecycle", "tier",
                     "detail_lines"):
            self.assertEqual(getattr(plain, name), getattr(marked, name),
                             name)

    def test_result_reveal_changes_only_result(self):
        base = dict(task="t", slots=template_slots(2), width=100,
                    last_outcome=SimpleOutcome(
                        RunStatus.COMPLETED, SimpleResult("out"), None))
        plain = build(**base, result_reveal=False)
        revealed = build(**base, result_reveal=True)
        self.assertNotEqual(plain.result_lines, revealed.result_lines)
        for name in ("collaboration_lines", "activity_lines",
                     "header_line", "lifecycle", "trace_obs",
                     "detail_lines"):
            self.assertEqual(getattr(plain, name),
                             getattr(revealed, name), name)

    def test_animation_inputs_keep_determinism(self):
        base = dict(self._kwargs(), pulse=True, reveal_seqs=(1,),
                    result_reveal=True)
        self.assertEqual(build(**base), build(**base))


class TraceStatusLineTests(unittest.TestCase):
    """Phase V §二十：follow 断开时的钉住状态行（纯呈现推导）。"""

    def test_following_renders_empty(self):
        self.assertEqual(projection.trace_status_line(True, 0), "")

    def test_pinned_shows_new_event_count(self):
        line = projection.trace_status_line(False, 3)
        self.assertIn("3 new events", line)
        self.assertIn("g/end", line)

    def test_ascii_variant_has_no_unicode(self):
        line = projection.trace_status_line(False, 5, ascii_only=True)
        self.assertIn("5 new events", line)
        self.assertNotIn("·", line)


# ------------------------------------------------ R1: pipeline + detail


def _handoff(seq, producer_role, receiver_runtime, status="EMBEDDED"):
    return ev(ExecutionEventType.HANDOFF, seq=seq, stage=producer_role,
              runtime=receiver_runtime, status=status, reason="R")


class PipelineLayoutTests(unittest.TestCase):
    """R1 P2：列容量（FULL 4 / MAIN_WIDE 3 / MAIN 2 / DEGRADED 1）+
    ↳ 换行续行 + 宽度铁律 + 组抽象（横向=组内协作，纵向=组间）。"""

    def test_full_four_in_one_row(self):
        lines = projection.pipeline_lines(
            template_slots(4), (), tier="FULL", width=100)
        self.assertEqual(len(lines), 4)   # heads/runtimes/states + ▲
        for role in ("ARCHITECT", "CODER", "TESTER", "REVIEWER"):
            self.assertIn(role, lines[0])

    def test_main_two_columns_wrap(self):
        lines = projection.pipeline_lines(
            template_slots(3), (), tier="MAIN", width=86)
        self.assertEqual(len(lines), 7)   # 行块(3)+▲+续行块(3)
        self.assertIn("ARCHITECT", lines[0])
        self.assertIn("CODER", lines[0])
        self.assertNotIn("REVIEWER", lines[0])
        self.assertTrue(lines[4].startswith("↳"))
        self.assertIn("REVIEWER", lines[4])

    def test_main_wide_three_columns_wrap(self):
        lines = projection.pipeline_lines(
            template_slots(4), (), tier="MAIN_WIDE", width=96)
        self.assertEqual(len(lines), 7)
        for role in ("ARCHITECT", "CODER", "TESTER"):
            self.assertIn(role, lines[0])
        self.assertNotIn("REVIEWER", lines[0])
        self.assertTrue(lines[4].startswith("↳"))
        self.assertIn("REVIEWER", lines[4])

    def test_every_line_never_exceeds_width(self):
        for width in (60, 76, 86, 96, 100):
            lines = projection.pipeline_lines(
                template_slots(4), (), width=width)
            self.assertTrue(lines, width)
            for line in lines:
                self.assertLessEqual(
                    projection.display_width(line), width, width)

    def test_explicit_groups_stack_vertically(self):
        slots_ = template_slots(3)
        lines = projection.pipeline_lines(
            slots_, (), width=100, groups=(slots_[:2], slots_[2:]))
        joined = "\n".join(lines)
        self.assertIn("", lines)               # 组间空行 = 纵向分界
        self.assertEqual(joined.count("┄┄→"), 1)  # 连接符仅存在于组内
        group2_head = [line for line in lines
                       if "REVIEWER" in line][0]
        self.assertFalse(group2_head.startswith("↳"))  # 新组 ≠ 续行


class DegradedVerticalTests(unittest.TestCase):
    """R1 P3：<80 纵向链——块间 ↓/┆ 真值连接行 + ↳ 续行前缀；仍是
    同一协作组（无组界空行、链序不变），身份与 runtime 不丢。"""

    def _lines(self, count=4, events=(), width=79):
        return projection.pipeline_lines(
            template_slots(count), events, tier="DEGRADED", width=width)

    def test_vertical_blocks_with_continuation_prefix(self):
        lines = self._lines()
        joined = "\n".join(lines)
        for role in ("ARCHITECT", "CODER", "TESTER", "REVIEWER"):
            self.assertIn(role, joined)
        self.assertEqual(joined.count("↳"), 9)   # 3 个续块 × 3 行
        self.assertIn("┆", joined)               # 零事件 → 计划连接

    def test_continuation_is_not_a_second_group(self):
        lines = self._lines()
        joined = "\n".join(lines)
        self.assertNotIn("", lines)              # 无组间空行
        order = [joined.index(role) for role in
                 ("ARCHITECT", "CODER", "TESTER", "REVIEWER")]
        self.assertEqual(order, sorted(order))   # 链序保持

    def test_identity_and_runtime_survive_narrow(self):
        lines = self._lines(width=79)
        joined = "\n".join(lines)
        for runtime in ("rt-0", "rt-1", "rt-2", "rt-3"):
            self.assertIn(runtime, joined)
        for line in lines:
            self.assertLessEqual(projection.display_width(line), 79)


class ConnectionTruthTests(unittest.TestCase):
    """R1 P4/§九：连接符真值——相邻绝不蕴含 HANDOFF；──→ 当且仅当真实
    HANDOFF 事件（stage=产出角色 ∧ runtime=接收方）在场；错位不命中。"""

    def test_observed_handoff_renders_solid_connector(self):
        slots_ = (slot("architect", "rt-0"), slot("coder", "rt-1"))
        events = (_handoff(0, "architect", "rt-1"),)
        joined = "\n".join(projection.pipeline_lines(
            slots_, events, width=100))
        self.assertIn("──→", joined)
        self.assertNotIn("┄┄→", joined)

    def test_adjacency_without_handoff_is_planned(self):
        slots_ = (slot("architect", "rt-0"), slot("coder", "rt-1"))
        joined = "\n".join(projection.pipeline_lines(
            slots_, (), width=100))
        self.assertIn("┄┄→", joined)
        self.assertNotIn("──→", joined)   # negative：相邻 ≠ HANDOFF

    def test_misdirected_handoff_misses_the_pair(self):
        slots_ = (slot("architect", "rt-0"), slot("coder", "rt-1"),
                  slot("reviewer", "rt-2"))
        wrong_receiver = (_handoff(0, "architect", "rt-2"),)
        joined = "\n".join(projection.pipeline_lines(
            slots_, wrong_receiver, width=100))
        self.assertNotIn("──→", joined)

    def test_reverse_direction_handoff_misses_the_pair(self):
        slots_ = (slot("architect", "rt-0"), slot("coder", "rt-1"))
        reverse = (_handoff(0, "coder", "rt-0"),)
        joined = "\n".join(projection.pipeline_lines(
            slots_, reverse, width=100))
        self.assertIn("┄┄→", joined)
        self.assertNotIn("──→", joined)

    def test_degraded_vertical_same_truth(self):
        slots_ = (slot("architect", "rt-0"), slot("coder", "rt-1"))
        observed = (_handoff(0, "architect", "rt-1"),)
        solid = "\n".join(projection.pipeline_lines(
            slots_, observed, tier="DEGRADED", width=60))
        self.assertIn("↓", solid)
        self.assertNotIn("┆", solid)
        dashed = "\n".join(projection.pipeline_lines(
            slots_, (), tier="DEGRADED", width=60))
        self.assertIn("┆", dashed)
        self.assertNotIn("↓", dashed)

    def test_connection_observed_predicate_is_exact(self):
        a = slot("architect", "rt-0")
        b = slot("coder", "rt-1")
        c = slot("reviewer", "rt-2")
        hit = (_handoff(0, "architect", "rt-1"),)
        self.assertTrue(projection.connection_observed(a, b, hit))
        self.assertFalse(projection.connection_observed(a, b, ()))
        self.assertFalse(projection.connection_observed(a, c, hit))
        self.assertFalse(projection.connection_observed(b, c, hit))


class DetailWindowTests(unittest.TestCase):
    """R1 P9-P13/§十：Agent Detail 有界投影窗——Prompt 诚实 —（观察
    契约冻结，零合成）、Handoff 双向只来自真实事件、Activity 与主屏/
    Trace 同源同格式器且 ⊆ Trace、Result 只来自 FINISHED+transcript、
    窗口 ≤ max_lines 且超窗指向 Trace。"""

    def _detail(self, slots_, events, stage="coder", **kwargs):
        return projection.agent_detail_window(
            slots_, events, stage=stage, **kwargs)

    def test_prompt_is_honest_dash(self):
        slots_ = (slot("architect", "rt-0"), slot("coder", "rt-1"))
        events = _slot_events("coder", "rt-1", invoked=True)
        lines = self._detail(slots_, events)
        prompt_lines = [line for line in lines if "prompt" in line]
        self.assertEqual(len(prompt_lines), 1)
        self.assertTrue(prompt_lines[0].rstrip().endswith("—"))

    def test_handoff_in_and_out_from_real_events(self):
        slots_ = (slot("architect", "rt-0"), slot("coder", "rt-1"))
        events = ((_handoff(0, "architect", "rt-1"),)
                  + _slot_events("coder", "rt-1", invoked=True))
        joined = "\n".join(self._detail(slots_, events))
        self.assertIn("in", joined)
        self.assertIn("architect", joined)     # 入向产出方来自事件 stage
        self.assertIn("EMBEDDED", joined)      # status 原词
        out_events = events + (_handoff(9, "coder", "rt-x"),)
        self.assertIn("rt-x", "\n".join(
            self._detail(slots_, out_events)))

    def test_adjacency_never_invents_handoff(self):
        slots_ = (slot("architect", "rt-0"), slot("coder", "rt-1"))
        lines = self._detail(
            slots_, _slot_events("coder", "rt-1", invoked=True))
        handoff_lines = [line for line in lines if "handoff" in line]
        self.assertEqual(len(handoff_lines), 2)   # in / out
        for line in handoff_lines:
            self.assertTrue(line.rstrip().endswith("—"))

    def test_activity_tail_same_source_subset_of_trace(self):
        slots_ = (slot("architect", "rt-0"), slot("coder", "rt-1"))
        many = tuple(
            ev(ExecutionEventType.INVOCATION_STARTED, seq=i,
               stage="coder", runtime="rt-1", status="STARTED")
            for i in range(12))
        state = build(task="t", slots=slots_, events=many, width=100,
                      selected_index=1, expanded_stage="coder")
        detail_text = "\n".join(state.detail_lines)

        def seq_tokens(text):
            found = set()
            for chunk in text.split("[")[1:]:
                digits = chunk.split("]", 1)[0]
                if digits.isdigit():
                    found.add(int(digits))
            return found

        trace_text = "\n".join(state.trace_obs)
        self.assertTrue(seq_tokens(detail_text) <= seq_tokens(trace_text))
        # 尾窗：只呈现最近事件，早期 seq 不在 detail；有界窗截尾时
        # (+k more · T) 指向 Trace（完整历史唯一出口）
        self.assertNotIn("[0]", detail_text)
        self.assertIn("[10]", detail_text)
        self.assertIn("more · T", detail_text)

    def test_result_from_finished_and_transcript(self):
        slots_ = (slot("architect", "rt-0"), slot("coder", "rt-1"))
        events = (_slot_events("coder", "rt-1", invoked=True,
                               finished="FAILED")
                  + (ev(ExecutionEventType.INVOCATION_FINISHED, seq=9,
                        stage="coder", runtime="rt-1", status="FAILED",
                        reason="R", duration_ms=1200),))
        lines = self._detail(slots_, events)
        result_line = [line for line in lines if "result" in line][0]
        self.assertIn("FAILED", result_line)
        self.assertIn("1200ms", result_line)      # 真实 duration
        outcome = SimpleOutcome(
            RunStatus.FAILED, None, RuntimeError("boom"),
            transcript=(transcript_record("coder", status="SUCCESS"),))
        result_line = [line for line in self._detail(
            slots_, events, last_outcome=outcome) if "result" in line][0]
        self.assertIn("step SUCCESS", result_line)  # transcript StepRecord

    def test_result_dash_without_any_fact(self):
        lines = self._detail((slot("coder", "rt-1"),), ())
        result_line = [line for line in lines if "result" in line][0]
        self.assertTrue(result_line.rstrip().endswith("—"))

    def test_state_uses_existing_state_word(self):
        slots_ = (slot("architect", "rt-0"), slot("coder", "rt-1"))
        events = _slot_events("coder", "rt-1", invoked=True)
        state_line = [line for line in self._detail(slots_, events)
                      if "state" in line][0]
        self.assertIn("RUNNING", state_line)
        parked = [line for line in self._detail(
            slots_, events, lifecycle="PARKED") if "state" in line][0]
        self.assertIn("PARKED", parked)

    def test_window_bound_with_trace_pointer(self):
        slots_ = (slot("architect", "rt-0"), slot("coder", "rt-1"))
        many = tuple(
            ev(ExecutionEventType.INVOCATION_STARTED, seq=i,
               stage="coder", runtime="rt-1", status="STARTED")
            for i in range(12))
        lines = self._detail(slots_, many)
        self.assertLessEqual(len(lines), 10)
        self.assertIn("more · T", lines[-1])

    def test_max_lines_parameter_is_honored(self):
        slots_ = (slot("architect", "rt-0"), slot("coder", "rt-1"))
        events = _slot_events("coder", "rt-1", invoked=True)
        lines = self._detail(slots_, events, max_lines=4)
        self.assertEqual(len(lines), 4)
        self.assertIn("more · T", lines[-1])

    def test_absent_stage_renders_empty(self):
        slots_ = (slot("architect", "rt-0"), slot("coder", "rt-1"))
        self.assertEqual(self._detail(slots_, (), stage="ghost"), ())

    def test_ascii_mode_maps_new_symbols(self):
        slots_ = (slot("architect", "rt-0"), slot("coder", "rt-1"))
        events = ((_handoff(0, "architect", "rt-1"),)
                  + _slot_events("coder", "rt-1", invoked=True))
        joined = "\n".join(self._detail(slots_, events, ascii_only=True))
        for symbol in ("▼", "▲", "▶", "↳", "─", "┄", "↓", "┆", "→"):
            self.assertNotIn(symbol, joined)


class SelectionPurityTests(unittest.TestCase):
    """R1 P14：selected/expanded 为纯呈现参数——除管线与 detail 外一切
    投影输出逐字节不变；同入同出确定性。"""

    UNCHANGED = ("header_line", "task_line", "badge", "activity_lines",
                 "progress_line", "tokens_line", "result_lines",
                 "context_lines", "trace_obs", "trace_ctrl",
                 "trace_usage", "lifecycle", "tier")

    def _base(self):
        events = running_events(("architect", "coder"))
        return dict(task="t", slots=template_slots(3), events=events,
                    width=100)

    def test_selection_changes_only_pipeline(self):
        a = build(**self._base(), selected_index=0)
        b = build(**self._base(), selected_index=2)
        self.assertNotEqual(a.collaboration_lines, b.collaboration_lines)
        self.assertEqual(a.detail_lines, b.detail_lines)  # 均未展开
        for name in self.UNCHANGED:
            self.assertEqual(getattr(a, name), getattr(b, name), name)

    def test_expansion_changes_only_pipeline_and_detail(self):
        a = build(**self._base(), selected_index=1)
        b = build(**self._base(), selected_index=1,
                  expanded_stage="coder")
        self.assertNotEqual(a.collaboration_lines, b.collaboration_lines)
        self.assertNotEqual(a.detail_lines, b.detail_lines)
        for name in self.UNCHANGED:
            self.assertEqual(getattr(a, name), getattr(b, name), name)

    def test_deterministic_repeat(self):
        kwargs = dict(self._base(), selected_index=1,
                      expanded_stage="coder")
        self.assertEqual(build(**kwargs), build(**kwargs))


# ------------------- R2: bilingual locale presentation (EN ⇄ ZH)


class UiLabelTests(unittest.TestCase):
    """R2-2（投影半）：_LOCALES/_LABELS 闭集词表 + ui_label 解析律。"""

    def test_locales_closed_pair(self):
        self.assertEqual(projection._LOCALES, ("en", "zh"))

    def test_en_column_is_canonical_key(self):
        for key, columns in projection._LABELS.items():
            self.assertEqual(len(columns), 2)
            self.assertEqual(columns[0], key)

    def test_en_default_and_unknown_locale_fall_back_to_english(self):
        for key in ("RUNNING", "TASK", "prompt", "No activity yet"):
            self.assertEqual(projection.ui_label(key), key)
            self.assertEqual(projection.ui_label(key, "en"), key)
            self.assertEqual(projection.ui_label(key, "fr"), key)

    def test_zh_column_values(self):
        expectations = {
            "RUNNING": "运行中", "PARKED": "已停驻", "DONE": "已完成",
            "FAILED": "失败", "WAITING": "等待中", "PAUSED": "已暂停",
            "ABORTED": "已中止", "NOT_STARTED": "未开始", "IDLE": "空闲",
            "COMPLETED": "已完成", "TASK": "任务", "prompt": "提示词",
            "handoff": "交接", "activity": "活动", "result": "结果",
            "state": "状态", "in": "入", "out": "出",
        }
        for key, zh in expectations.items():
            self.assertEqual(projection.ui_label(key, "zh"), zh, key)

    def test_missing_key_returns_key_verbatim(self):
        self.assertEqual(projection.ui_label("NOT_A_LABEL", "zh"),
                         "NOT_A_LABEL")

    def test_state_vocabulary_fully_covered(self):
        for word in ("RUNNING", "WAITING", "DONE", "FAILED", "PAUSED",
                     "PARKED", "ABORTED", "NOT_STARTED", "IDLE",
                     "COMPLETED"):
            self.assertIn(word, projection._LABELS)


def _locale_fixture(**over):
    """R2 共用投影夹具：2-agent 链——architect 完成（SUCCESS/1200ms/
    真实 HANDOFF→rt-1）、coder 在途 RUNNING；usage KNOWN。"""
    slots_ = template_slots(2)
    events = (
        ev(ExecutionEventType.STAGE_STARTED, seq=0, stage="architect",
           runtime="rt-0"),
        ev(ExecutionEventType.INVOCATION_STARTED, seq=1, stage="architect",
           runtime="rt-0", status="STARTED"),
        ev(ExecutionEventType.INVOCATION_FINISHED, seq=2, stage="architect",
           runtime="rt-0", status="SUCCESS", duration_ms=1200),
        ev(ExecutionEventType.HANDOFF, seq=3, stage="architect",
           runtime="rt-1", status="EMBEDDED"),
        ev(ExecutionEventType.STAGE_STARTED, seq=4, stage="coder",
           runtime="rt-1"),
        ev(ExecutionEventType.INVOCATION_STARTED, seq=5, stage="coder",
           runtime="rt-1", status="STARTED"),
    )
    kwargs = dict(
        task="demo task", slots=slots_, events=events,
        usage_records=(usage(runtime="rt-0", role="architect"),),
        width=100)
    kwargs.update(over)
    return inputs(**kwargs)


class LocaleProjectionTests(unittest.TestCase):
    """R2-1/R2-3/R2-4/R2-7（投影半）：默认 EN、locale 呈现纯度、
    事实面不可译。"""

    def test_projectioninputs_locale_defaults_to_en(self):
        self.assertEqual(_locale_fixture().locale, "en")

    def test_default_equals_explicit_en(self):
        self.assertEqual(projection.build_projection(_locale_fixture()),
                         projection.build_projection(_locale_fixture(locale="en")))

    def test_zh_switches_presentation_words_only(self):
        en = projection.build_projection(_locale_fixture())
        zh = projection.build_projection(_locale_fixture(locale="zh"))
        self.assertIn("TASK", en.task_line)
        self.assertIn("任务", zh.task_line)
        self.assertIn("demo task", zh.task_line)     # task 原文不译
        self.assertIn("RUNNING", en.badge)
        self.assertIn("运行中", zh.badge)
        self.assertIn("阶段", zh.progress_line)
        self.assertIn("coder", zh.progress_line)     # 活动阶段名原文
        self.assertIn("RUNNING", en.header_line)
        self.assertIn("运行中", zh.header_line)
        self.assertIn("dual-agent cockpit", zh.header_line)  # identity 不译

    def test_pipeline_state_words_localize_identity_stays(self):
        zh = projection.build_projection(_locale_fixture(locale="zh"))
        joined = "\n".join(zh.collaboration_lines)
        self.assertIn("已完成", joined)              # architect DONE
        self.assertIn("运行中", joined)              # coder RUNNING
        self.assertIn("ARCHITECT", joined)           # ROLE 不译
        self.assertIn("CODER", joined)
        self.assertIn("rt-0", joined)                # runtime 不译
        self.assertIn("──→", joined)                 # 真实 HANDOFF 符号不变

    def test_facts_and_trace_identical_across_locales(self):
        facts_ = (fact(ControlFactType.PAUSE_REQUESTED, seq=0),)
        en = projection.build_projection(_locale_fixture(facts=facts_))
        zh = projection.build_projection(
            _locale_fixture(facts=facts_, locale="zh"))
        for field in ("trace_obs", "trace_ctrl", "trace_usage",
                      "lifecycle", "tier"):
            self.assertEqual(getattr(en, field), getattr(zh, field), field)

    def test_immutable_truth_survives_zh_detail(self):
        values = _locale_fixture(locale="zh")
        detail = "\n".join(projection.agent_detail_window(
            values.slots, values.events, stage="architect", locale="zh"))
        self.assertIn("▼ ARCHITECT", detail)         # ROLE 不译
        self.assertIn("→ rt-1 (EMBEDDED)", detail)   # 出向事实原词
        self.assertIn("SUCCESS · 1200ms", detail)    # 事件 status 原词
        self.assertIn("[2]", detail)                 # sequence 原样
        coder = "\n".join(projection.agent_detail_window(
            values.slots, values.events, stage="coder", locale="zh"))
        self.assertIn("architect → here (EMBEDDED)", coder)  # 入向原词

    def test_terminal_result_header_keeps_raw_status(self):
        outcome = SimpleNamespace(
            status="COMPLETED",
            final_result=SimpleNamespace(output="done text"),
            error=None)
        en = projection.build_projection(_locale_fixture(last_outcome=outcome))
        zh = projection.build_projection(_locale_fixture(last_outcome=outcome,
                                             locale="zh"))
        self.assertEqual(en.result_lines[0], "✓ COMPLETED")
        self.assertEqual(zh.result_lines[0], "✓ COMPLETED")  # 原词不译

    def test_parked_result_line_localizes(self):
        outcome = SimpleNamespace(status="PARKED", final_result=None,
                                  error=None)
        en = projection.build_projection(_locale_fixture(last_outcome=outcome))
        zh = projection.build_projection(_locale_fixture(last_outcome=outcome,
                                             locale="zh"))
        self.assertEqual(en.result_lines, ("PARKED · awaiting resume",))
        self.assertEqual(zh.result_lines, ("已停驻 · 等待继续",))

    def test_progress_pause_pending_localizes(self):
        base = dict(task="t", slots=template_slots(2),
                    events=running_events(("architect", "coder")),
                    facts=(fact(ControlFactType.PAUSE_REQUESTED,
                                seq=0),), width=100)
        en = build(**base)
        zh = build(**base, locale="zh")
        self.assertIn("pause pending", en.progress_line)
        self.assertIn("暂停待生效", zh.progress_line)
        self.assertIn("STAGE 0/2", en.progress_line)  # 数字结构原样
        self.assertIn("阶段 0/2", zh.progress_line)


class PresentationLineLocaleTests(unittest.TestCase):
    """R2 §十一/§十八：短句类呈现词（trace 状态行/worker 第二行/
    revision 标签/结果溢出行）的 EN/ZH 双列与事实面不译。"""

    def test_trace_status_line_locale(self):
        en = projection.trace_status_line(False, 3)
        zh = projection.trace_status_line(False, 3, locale="zh")
        self.assertEqual(en, "pinned · 3 new events · g/end resumes tail")
        self.assertEqual(zh, "已钉住 · 3 条新事件 · g/end 恢复跟随")
        self.assertIn("g/end", zh)                    # 键位字面保留

    def test_worker_failure_second_line_localizes(self):
        error = RuntimeError("boom")
        en = projection.worker_failure_lines(error)
        zh = projection.worker_failure_lines(error, locale="zh")
        self.assertEqual(en[1], "[Q] quit re-raises the original error")
        self.assertEqual(zh[1], "[Q] 退出将重新抛出原始错误")
        self.assertEqual(en[0], zh[0])   # 首行（类型名+消息原文）不译

    def test_revision_pending_label_localizes(self):
        en = projection.revision_status_lines((), 2)
        zh = projection.revision_status_lines((), 2, locale="zh")
        self.assertEqual(en[0], "pending 2")
        self.assertEqual(zh[0], "待处理 2")

    def test_result_overflow_and_empty_output_lines(self):
        long_text = " ".join(f"word{i}" for i in range(60))
        base = dict(task="t", slots=template_slots(2),
                    events=running_events(("architect", "coder")),
                    width=100)
        outcome = SimpleNamespace(
            status="COMPLETED",
            final_result=SimpleNamespace(output=long_text), error=None)
        en = build(**base, last_outcome=outcome)
        zh = build(**base, last_outcome=outcome, locale="zh")
        self.assertTrue(any("more lines" in line
                            for line in en.result_lines))
        self.assertTrue(any("行未显示" in line
                            for line in zh.result_lines))
        empty = SimpleNamespace(status="COMPLETED",
                                final_result=SimpleNamespace(output=""),
                                error=None)
        zh2 = build(**base, last_outcome=empty, locale="zh")
        self.assertIn("(无文本输出)", "\n".join(zh2.result_lines))


class ActivityLocaleTests(unittest.TestCase):
    """R2 §十一：活动动词短语可译；stage/status/duration 原文拼装。"""

    def test_activity_verbs_localize_facts_stay_raw(self):
        events = (
            ev(ExecutionEventType.STAGE_STARTED, seq=0, stage="architect",
               runtime="rt-0"),
            ev(ExecutionEventType.INVOCATION_STARTED, seq=1,
               stage="architect", runtime="rt-0", status="STARTED"),
            ev(ExecutionEventType.INVOCATION_FINISHED, seq=2,
               stage="architect", runtime="rt-0", status="SUCCESS",
               duration_ms=1200),
            ev(ExecutionEventType.STAGE_FINISHED, seq=3, stage="architect",
               runtime="rt-0"),
        )
        en = projection.activity_tail_lines(events)
        zh = projection.activity_tail_lines(events, locale="zh")
        self.assertIn("architect stage started", en[0])
        self.assertIn("architect 阶段已开始", zh[0])
        self.assertIn("architect finished SUCCESS (1200ms)", en[2])
        self.assertIn("architect 已完成 SUCCESS (1200ms)", zh[2])
        self.assertIn("architect stage finished", en[3])
        self.assertIn("architect 阶段已完成", zh[3])
        for line_en, line_zh in zip(en, zh):
            self.assertIn("[", line_zh)              # sequence 锚定原样

    def test_empty_activity_localizes(self):
        self.assertEqual(projection.activity_tail_lines(()),
                         ("No activity yet",))
        self.assertEqual(projection.activity_tail_lines((), locale="zh"),
                         ("暂无活动",))


class DetailLocaleTests(unittest.TestCase):
    """R2-8：Detail 五标签 + in/out 可译；结构/事实行原样；CJK 标签列
    display-width 对齐。"""

    def _detail(self, locale):
        values = _locale_fixture()
        return projection.agent_detail_window(
            values.slots, values.events, stage="architect", locale=locale)

    def test_labels_localize_facts_stay_raw(self):
        en = "\n".join(self._detail("en"))
        zh = "\n".join(self._detail("zh"))
        for label in ("prompt", "handoff", "activity", "result", "state"):
            self.assertIn(label, en)
        for label in ("提示词", "交接", "活动", "结果", "状态"):
            self.assertIn(label, zh)
        self.assertIn(" in  ", en)
        self.assertIn(" out ", en)
        self.assertIn("入", zh)
        self.assertIn("出", zh)
        self.assertIn("▼ ARCHITECT", zh)

    def test_structure_identical_across_locales(self):
        self.assertEqual(len(self._detail("en")), len(self._detail("zh")))

    def test_label_value_column_alignment_display_width(self):
        en_lines = self._detail("en")
        zh_lines = self._detail("zh")
        en_prompt = next(line for line in en_lines
                         if "prompt" in line)
        zh_prompt = next(line for line in zh_lines
                         if "提示词" in line)

        def value_column(line):
            width = 0
            for character in line:
                if character == "—":
                    return width
                width += projection.display_width(character)
            return -1

        self.assertEqual(value_column(en_prompt),
                         value_column(zh_prompt))


class PipelineLocaleTests(unittest.TestCase):
    """R2-9：locale 与布局正交——同输入 EN/ZH 的行数、续行、marker、
    指针列、连接符完全一致，仅呈现词变化。"""

    _SYMBOLS = ("↳", "▲", "▶", "▼", "──→", "┄┄→", "↓", "┆")

    def _structure(self, lines):
        return [tuple(line.count(symbol) for symbol in self._SYMBOLS)
                for line in lines]

    def test_structure_invariant_across_tiers(self):
        cases = ((2, 100), (3, 100), (4, 100), (3, 89), (4, 140), (2, 79))
        for count, width in cases:
            slots_ = template_slots(count)
            events = running_events(_TEMPLATES[count])
            tier = projection._tier_for(width)
            content = (width - 4 if tier == "DEGRADED"
                       else min(width - 4, 100))
            common = dict(lifecycle="RUNNING", tier=tier, width=content,
                          selected_index=1)
            en = projection.pipeline_lines(slots_, events, **common)
            zh = projection.pipeline_lines(slots_, events, locale="zh",
                                           **common)
            self.assertEqual(len(en), len(zh), (count, width))
            self.assertEqual(self._structure(en), self._structure(zh),
                             (count, width))
            for en_line, zh_line in zip(en, zh):
                if "▲" in en_line:
                    # 指针行（空格+▲，无呈现词）逐字节一致
                    self.assertEqual(en_line, zh_line)

    def test_explicit_en_equals_default(self):
        slots_ = template_slots(3)
        events = running_events(_TEMPLATES[3])
        self.assertEqual(
            projection.pipeline_lines(slots_, events, tier="MAIN",
                                      width=96),
            projection.pipeline_lines(slots_, events, tier="MAIN",
                                      width=96, locale="en"))


class CJKWidthLocaleTests(unittest.TestCase):
    """R2-10：EN/ZH × 四档 × 2/3/4 agents——一切渲染行不溢出
    （display_width 逐行 ≤ content width）。"""

    def test_pipeline_and_detail_fit_width_all_tiers(self):
        for count in (2, 3, 4):
            slots_ = template_slots(count)
            events = running_events(_TEMPLATES[count])
            for width in (79, 89, 100, 140):
                tier = projection._tier_for(width)
                content = (width - 4 if tier == "DEGRADED"
                           else min(width - 4, 100))
                for locale in ("en", "zh"):
                    lines = projection.pipeline_lines(
                        slots_, events, lifecycle="RUNNING", tier=tier,
                        width=content, locale=locale,
                        expanded_stage=slots_[0].stage)
                    self.assertTrue(lines)
                    for line in lines:
                        self.assertLessEqual(
                            projection.display_width(line), content,
                            (count, width, locale, line))
                    detail = projection.agent_detail_window(
                        slots_, events, stage=slots_[0].stage,
                        lifecycle="RUNNING", width=content, locale=locale)
                    for line in detail:
                        self.assertLessEqual(
                            projection.display_width(line), content,
                            (count, width, locale, line))

    def test_build_projection_zh_end_to_end_fit(self):
        state = build(task="任务文本 task text", slots=template_slots(4),
                      events=running_events(_TEMPLATES[4]), width=79,
                      expanded_stage="architect", locale="zh")
        for line in (state.collaboration_lines + state.detail_lines
                     + state.activity_lines):
            self.assertLessEqual(projection.display_width(line), 75, line)


class AsciiLocaleTests(unittest.TestCase):
    """R2-11：ascii_only 与 locale 正交——四象限全部可用、互不污染。"""

    _UNICODE_SYMBOLS = ("─", "┄", "↓", "┆", "↳", "▲", "▶", "▼",
                        "●", "○", "✓", "✗", "❚❚", "▫", "◐", "◉")

    def _fixtures(self):
        slots_ = template_slots(2)
        events = (
            ev(ExecutionEventType.STAGE_STARTED, seq=0, stage="architect",
               runtime="rt-0"),
            ev(ExecutionEventType.INVOCATION_STARTED, seq=1,
               stage="architect", runtime="rt-0", status="STARTED"),
            ev(ExecutionEventType.INVOCATION_FINISHED, seq=2,
               stage="architect", runtime="rt-0", status="SUCCESS"),
            ev(ExecutionEventType.HANDOFF, seq=3, stage="architect",
               runtime="rt-1", status="EMBEDDED"),
            ev(ExecutionEventType.STAGE_STARTED, seq=4, stage="coder",
               runtime="rt-1"),
        )
        return slots_, events

    def test_four_quadrants(self):
        slots_, events = self._fixtures()
        unicode_en = projection.pipeline_lines(slots_, events)
        unicode_zh = projection.pipeline_lines(slots_, events,
                                               locale="zh")
        ascii_en = projection.pipeline_lines(slots_, events,
                                             ascii_only=True)
        ascii_zh = projection.pipeline_lines(slots_, events,
                                             ascii_only=True, locale="zh")
        # Unicode 象限：符号与 locale 无关
        self.assertIn("──→", "\n".join(unicode_en))
        self.assertIn("──→", "\n".join(unicode_zh))
        # ASCII 象限：零 Unicode 符号残留（含 ZH）
        joined = "\n".join(ascii_zh)
        for symbol in self._UNICODE_SYMBOLS:
            self.assertNotIn(symbol, joined)
        self.assertIn("已完成", joined)               # ZH 文字合法在场
        self.assertIn("等待中", joined)
        # EN ASCII 仍是 EN 词
        self.assertIn("DONE", "\n".join(ascii_en))
        # 显式 en 与默认逐字节一致
        self.assertEqual(unicode_en,
                         projection.pipeline_lines(slots_, events,
                                                   locale="en"))

    def test_detail_ascii_zh_quadrant(self):
        slots_, events = self._fixtures()
        text = "\n".join(projection.agent_detail_window(
            slots_, events, stage="architect", ascii_only=True,
            locale="zh"))
        for symbol in ("▼", "●", "✓"):
            self.assertNotIn(symbol, text)
        self.assertIn("提示词", text)
        self.assertIn("ARCHITECT", text)


# ------------------- CU-TUI-INPUT: funnel dock input + zone budgets


class FunnelInputLineTests(unittest.TestCase):
    """CU-TUI-INPUT A1：漏斗输入回显行——迁入底部 dock 的唯一回显面。
    安全门/宽度截断与既有首屏输入行同律；尾部光标 | 由调用方追加
    （TUI-4 composer 惯例，投影层不产光标）。"""

    def test_basic_echo_line(self):
        self.assertEqual(projection.funnel_input_line("fix the bug"),
                         "> fix the bug")

    def test_empty_buffer_echoes_bare_prompt(self):
        self.assertEqual(projection.funnel_input_line(""), "> ")

    def test_unsafe_buffer_redacted(self):
        secret = "sk-ant-api03-ABCDEFGHIJKLMNOPQRSTUVWXYZ1234"
        line = projection.funnel_input_line(secret)
        self.assertEqual(line, "> [redacted: unsafe content]")
        self.assertNotIn(secret, line)

    def test_long_buffer_truncated_to_width(self):
        line = projection.funnel_input_line("x" * 200, width=40)
        self.assertLessEqual(projection.display_width(line), 40)
        self.assertTrue(line.startswith("> "))
        self.assertTrue(line.endswith("..."))

    def test_ascii_mode_keeps_prompt_shape(self):
        self.assertEqual(
            projection.funnel_input_line("task", ascii_only=True),
            "> task")

    def test_deterministic_repeat(self):
        self.assertEqual(projection.funnel_input_line("task"),
                         projection.funnel_input_line("task"))


class FunnelKeysHintTests(unittest.TestCase):
    """CU-TUI-INPUT A1：两键提示行——迁入 dock-controls 的唯一提示面
    （漏斗呈现 EN 冻结，R2 ERRATA 同律）。"""

    def test_hint_line(self):
        # CU-COCKPIT-1：漏斗键面受控解冻——c 进入 COMPOSE 选择屏
        self.assertEqual(projection.funnel_keys_hint(),
                         "Enter start · c compose · q quit")

    def test_ascii_keeps_middle_dot(self):
        # · 不在 ASCII 降级表（既有行为保持）——ascii 变体逐字节相同
        self.assertEqual(projection.funnel_keys_hint(ascii_only=True),
                         projection.funnel_keys_hint())


class FunnelFirstScreenDockTests(unittest.TestCase):
    """CU-TUI-INPUT A1：include_input=False = 输入迁入底部 dock 的
    首屏组装形态——body 省略输入行与两键提示（回显入 dock-input、
    提示入 dock-controls）；默认 True 与既有输出逐字节一致。"""

    OK = composition((binding("architect", "codex-cli", "openai"),
                      binding("coder", "claude-cli", "anthropic")))

    def test_default_is_byte_identical_to_legacy(self):
        self.assertEqual(
            projection.funnel_first_screen("2.5.0", self.OK, "task"),
            ("dual-agent cockpit · 2.5.0",
             "Describe the collaboration task",
             "> task",
             "Collaboration plan (default)",
             "  architect  ← codex-cli · openai",
             "  coder      ← claude-cli · anthropic",
             "Enter start · c compose · q quit"))

    def test_dock_shape_omits_input_and_keys_hint(self):
        lines = projection.funnel_first_screen(
            "2.5.0", self.OK, "task", include_input=False)
        self.assertEqual(lines, (
            "dual-agent cockpit · 2.5.0",
            "Describe the collaboration task",
            "Collaboration plan (default)",
            "  architect  ← codex-cli · openai",
            "  coder      ← claude-cli · anthropic"))
        joined = "\n".join(lines)
        self.assertNotIn("> task", joined)
        self.assertNotIn("Enter start", joined)

    def test_dock_shape_blocked_composition(self):
        blocked = composition(
            blocked_reason="default collaboration needs at least 2 "
                           "VERIFIED runtimes (found 1)")
        lines = projection.funnel_first_screen(
            "2.5.0", blocked, "", include_input=False)
        self.assertEqual(lines, (
            "dual-agent cockpit · 2.5.0",
            "Describe the collaboration task",
            "default collaboration needs at least 2 VERIFIED runtimes "
            "(found 1)"))

    def test_dock_shape_ascii(self):
        lines = projection.funnel_first_screen(
            "2.5.0", self.OK, "t", ascii_only=True, include_input=False)
        joined = "\n".join(lines)
        self.assertNotIn("←", joined)
        self.assertIn("<-", joined)


class DetailBudgetTests(unittest.TestCase):
    """CU-TUI-INPUT A4：ProjectionInputs.detail_max_lines 呈现参数
    （None=默认 10 既有行为；int=有界窗收窄上限）——TUI 高度预算的
    投影面，纯呈现、零事实触碰。"""

    def _budget_fixture(self, **over):
        slots_ = (slot("architect", "rt-0"), slot("coder", "rt-1"))
        many = tuple(
            ev(ExecutionEventType.INVOCATION_STARTED, seq=i,
               stage="coder", runtime="rt-1", status="STARTED")
            for i in range(12))
        kwargs = dict(task="t", slots=slots_, events=many, width=100,
                      selected_index=1, expanded_stage="coder")
        kwargs.update(over)
        return build(**kwargs)

    def test_none_keeps_default_window_bound(self):
        state = self._budget_fixture()
        explicit = self._budget_fixture(detail_max_lines=None)
        self.assertEqual(state.detail_lines, explicit.detail_lines)
        self.assertLessEqual(len(state.detail_lines), 10)

    def test_int_shrinks_window_with_trace_pointer(self):
        state = self._budget_fixture(detail_max_lines=5)
        self.assertEqual(len(state.detail_lines), 5)
        self.assertIn("more · T", state.detail_lines[-1])

    def test_budget_above_natural_leaves_window_uncapped(self):
        # 预算 ≥ 自然高度 → 不收窄（逐字节等于默认形态）
        natural = self._budget_fixture()
        state = self._budget_fixture(detail_max_lines=10)
        self.assertEqual(state.detail_lines, natural.detail_lines)

    def test_budget_does_not_touch_other_fields(self):
        base = self._budget_fixture()
        shrunk = self._budget_fixture(detail_max_lines=5)
        self.assertEqual(base.collaboration_lines,
                         shrunk.collaboration_lines)
        self.assertEqual(base.activity_lines, shrunk.activity_lines)
        self.assertEqual(base.result_lines, shrunk.result_lines)
        self.assertEqual(base.progress_line, shrunk.progress_line)
        self.assertEqual(base.tokens_line, shrunk.tokens_line)


class ResultBudgetTests(unittest.TestCase):
    """CU-TUI-INPUT A4：ProjectionInputs.result_max_lines 呈现参数
    （None=既有行为；int=收窄——头行保留 + (+N more lines) 诚实溢出
    标记）。交付物绝不整区隐没。"""

    def _outcome_fixture(self, **over):
        # 每词 90 列 → textwrap(96) 每词恰一行：5 词 = 5 内容行（确定
        # 性包装，既有形态 = 头行 + 3 行 + "(+2 more lines)"）
        output = " ".join("x" * 90 for _ in range(5))
        outcome = SimpleOutcome(RunStatus.COMPLETED,
                                SimpleResult(output), None)
        kwargs = dict(task="t", slots=template_slots(2),
                      events=running_events(("architect", "coder")),
                      width=100, last_outcome=outcome)
        kwargs.update(over)
        return build(**kwargs)

    def test_none_keeps_legacy_shape(self):
        state = self._outcome_fixture()
        explicit = self._outcome_fixture(result_max_lines=None)
        self.assertEqual(state.result_lines, explicit.result_lines)
        # 既有形态：头行 + ≤3 内容行 + 溢出标记
        self.assertEqual(len(state.result_lines), 5)
        self.assertTrue(state.result_lines[0].startswith("✓ COMPLETED"))
        self.assertEqual(state.result_lines[-1], "(+2 more lines)")

    def test_int_caps_to_header_plus_marker(self):
        state = self._outcome_fixture(result_max_lines=2)
        self.assertEqual(len(state.result_lines), 2)
        self.assertTrue(state.result_lines[0].startswith("✓ COMPLETED"))
        # 折叠行数 = 头行之外的全部既有行（3 内容行 + 旧溢出标记）
        self.assertEqual(state.result_lines[1], "(+4 more lines)")

    def test_status_word_is_never_dropped(self):
        # 收窄永不吞头行（RunOutcome.status 事实直显）
        for cap in (2, 3, 4):
            state = self._outcome_fixture(result_max_lines=cap)
            self.assertTrue(state.result_lines[0].startswith("✓ COMPLETED"))

    def test_budget_does_not_touch_other_fields(self):
        base = self._outcome_fixture()
        shrunk = self._outcome_fixture(result_max_lines=2)
        self.assertEqual(base.collaboration_lines,
                         shrunk.collaboration_lines)
        self.assertEqual(base.detail_lines, shrunk.detail_lines)
        self.assertEqual(base.activity_lines, shrunk.activity_lines)
        self.assertEqual(base.tokens_line, shrunk.tokens_line)


# ---------------------- CU-PERF-1: trace ownership / narrowing / fast width


class IncludeTraceEquivalenceTests(unittest.TestCase):
    """T1：include_trace=False 只清三 trace 字段，其余逐字节一致。"""

    def test_false_empties_exactly_three_trace_fields(self):
        events = running_events(("architect", "coder"))
        facts = (fact(ControlFactType.PAUSE_REQUESTED, seq=0),)
        records = (usage(), usage(runtime="rt-1", role="code"))
        common = dict(
            task="demo", slots=template_slots(2), events=events,
            facts=facts, usage_records=records, width=100,
            expanded_stage="architect")
        full = build(**common)
        without = build(include_trace=False, **common)
        self.assertEqual(without.trace_obs, ())
        self.assertEqual(without.trace_ctrl, ())
        self.assertEqual(without.trace_usage, ())
        for name in ("header_line", "task_line", "badge",
                     "collaboration_lines", "activity_lines",
                     "detail_lines", "progress_line", "tokens_line",
                     "result_lines", "context_lines", "lifecycle",
                     "tier"):
            self.assertEqual(
                getattr(full, name), getattr(without, name),
                f"field {name} diverged with include_trace=False")

    def test_default_true_matches_explicit_true(self):
        events = running_events(("architect", "coder"))
        kwargs = dict(task="t", slots=template_slots(2), events=events)
        self.assertEqual(build(**kwargs),
                         build(include_trace=True, **kwargs))

    def test_false_ascii_path_empties_trace_fields_too(self):
        events = running_events(("arch",))
        kwargs = dict(task="t", slots=template_slots(2), events=events,
                      ascii_only=True, width=60)
        without = build(include_trace=False, **kwargs)
        self.assertEqual((without.trace_obs, without.trace_ctrl,
                          without.trace_usage), ((), (), ()))


class TraceFunctionsGoldenTests(unittest.TestCase):
    """T2：三函数 == 全量 build_projection 对应字段（含 ascii 态）。"""

    def test_observation_lines_match_projection_field(self):
        events = running_events(("architect", "coder"))
        state = build(task="t", slots=template_slots(2), events=events)
        self.assertEqual(
            projection.trace_observation_lines(events), state.trace_obs)

    def test_control_lines_match_projection_field(self):
        facts = (fact(ControlFactType.PAUSE_REQUESTED, seq=0,
                      payload={"k": "v"}),
                 fact(ControlFactType.RESUME_REQUESTED, seq=1))
        state = build(task="t", slots=template_slots(2), facts=facts)
        self.assertEqual(
            projection.trace_control_lines(facts), state.trace_ctrl)

    def test_usage_lines_match_projection_field(self):
        records = (usage(), usage(runtime="rt-1", role="code",
                                  status=UsageObservation.UNSUPPORTED))
        state = build(task="t", slots=template_slots(2),
                      usage_records=records)
        self.assertEqual(
            projection.trace_usage_lines(records), state.trace_usage)

    def test_ascii_flags_match_ascii_state(self):
        events = running_events(("arch",))
        facts = (fact(ControlFactType.PAUSE_REQUESTED, seq=0),)
        records = (usage(),)
        state = build(task="t", slots=template_slots(2), events=events,
                      facts=facts, usage_records=records, ascii_only=True)
        self.assertEqual(
            projection.trace_observation_lines(
                events, ascii_only=True), state.trace_obs)
        self.assertEqual(
            projection.trace_control_lines(
                facts, ascii_only=True), state.trace_ctrl)
        self.assertEqual(
            projection.trace_usage_lines(
                records, ascii_only=True), state.trace_usage)


class ApplyBudgetNarrowingGoldenTests(unittest.TestCase):
    """T3：窄域重算 == 携带同参数的全量重建（字段级矩阵）。"""

    def _matrix_inputs(self, *, ascii_only, locale, expanded,
                       terminal=None, final=None):
        events = running_events(("architect", "coder"))
        return inputs(
            task="demo", slots=template_slots(2), events=events,
            width=100, ascii_only=ascii_only, locale=locale,
            expanded_stage=("arch" if expanded else None),
            terminal=terminal,
            last_outcome=SimpleOutcome("COMPLETED", final, None)
            if final is not None else None)

    def _assert_narrow_equals_full(self, values, detail_max,
                                   result_max):
        base = projection.build_projection(values)
        narrow = projection.apply_budget_narrowing(
            base, values,
            detail_max_lines=detail_max, result_max_lines=result_max)
        values2 = inputs(
            **{k: getattr(values, k) for k in (
                "task", "slots", "events", "facts", "usage_records",
                "terminal", "run_state", "last_outcome", "capabilities",
                "version", "width", "ascii_only", "pulse", "reveal_seqs",
                "result_reveal", "selected_index", "expanded_stage",
                "locale")})
        values2.detail_max_lines = detail_max
        values2.result_max_lines = result_max
        full = projection.build_projection(values2)
        for name in ("detail_lines", "result_lines"):
            self.assertEqual(
                getattr(narrow, name), getattr(full, name),
                f"{name} diverged (detail_max={detail_max} "
                f"result_max={result_max})")
        for name in ("header_line", "task_line", "badge",
                     "collaboration_lines", "activity_lines",
                     "progress_line", "tokens_line", "context_lines",
                     "trace_obs", "trace_ctrl", "trace_usage",
                     "lifecycle", "tier"):
            self.assertEqual(
                getattr(narrow, name), getattr(base, name),
                f"untouched field {name} diverged")

    def test_golden_matrix_detail_x_result_x_ascii_x_locale(self):
        for ascii_only in (False, True):
            for locale in ("en", "zh"):
                for expanded in (False, True):
                    values = self._matrix_inputs(
                        ascii_only=ascii_only, locale=locale,
                        expanded=expanded,
                        final="line one\nline two\nline three")
                    for detail_max in (None, 3, 7):
                        for result_max in (None, 2, 4):
                            self._assert_narrow_equals_full(
                                values, detail_max, result_max)

    def test_reveal_and_terminal_axes(self):
        for reveal in (False, True):
            values = self._matrix_inputs(
                ascii_only=False, locale="zh", expanded=True,
                terminal="COMPLETED" if reveal else None,
                final="x\ny\nz")
            values.result_reveal = reveal
            self._assert_narrow_equals_full(values, 4, 3)

    def test_narrowing_without_limits_returns_same_state(self):
        values = self._matrix_inputs(ascii_only=False, locale="en",
                                     expanded=True, final="r")
        base = projection.build_projection(values)
        same = projection.apply_budget_narrowing(base, values)
        self.assertEqual(same, base)


class DisplayWidthFastPathTests(unittest.TestCase):
    """T8：快路径与既有线性扫描逐点等价（参考实现对照）。"""

    def _reference_width(self, text):
        total = 0
        for character in text:
            code = ord(character)
            wide = any(low <= code <= high
                       for low, high in projection._WIDE_RANGES)
            total += 2 if wide else 1
        return total

    def test_wide_range_boundary_endpoints(self):
        points = []
        for low, high in projection._WIDE_RANGES:
            points.extend((low - 1, low, low + 1, high - 1, high,
                           high + 1))
        for code in points:
            if code < 0:
                continue
            text = chr(code)
            self.assertEqual(
                projection.display_width(text),
                self._reference_width(text),
                f"codepoint U+{code:04X} diverged")

    def test_assorted_probes(self):
        probes = (
            "", "ascii only 123", "任务 运行中",
            "混合 abc 任务 → ▸ · ‖",
            "ひらがな カタカナ", "한국어",
            "７８９　ＡＢ",
            chr(0x1100) + chr(0x115F) + chr(0x1160)
            + chr(0xFF60) + chr(0xFF61),
            chr(0xFFE6) + chr(0xFFE7),
        )
        for text in probes:
            self.assertEqual(
                projection.display_width(text),
                self._reference_width(text))

    def test_truncate_to_width_equivalence(self):
        samples = (
            "short",
            "ascii text exactly long enough to need"
            " truncation at small limits yes indeed",
            "任务文本需要截断的任务文本需要截断的任务文本",
            "mixed 混合 abc → ok",
        )
        for text in samples:
            for limit in (1, 3, 5, 10, 20, 60):
                self.assertEqual(
                    projection.truncate_to_width(text, limit),
                    self._reference_truncate(text, limit))

    def test_truncate_ascii_fast_path_consistent(self):
        text = "pure ascii sentence for truncation checks"
        for limit in (5, 12, len(text)):
            self.assertEqual(
                projection.truncate_to_width(text, limit),
                self._reference_truncate(text, limit))

    def _reference_truncate(self, text, limit):
        if self._reference_width(text) <= limit:
            return text
        budget = max(0, limit - 3)
        parts = []
        used = 0
        for character in text:
            code = ord(character)
            cost = 2 if any(
                low <= code <= high
                for low, high in projection._WIDE_RANGES) else 1
            if used + cost > budget:
                break
            parts.append(character)
            used += cost
        return "".join(parts) + "..."


class DetailWindowSinglePassTests(unittest.TestCase):
    """T9：单遍扫描重构与既有语义等价（golden 事件序列）。

    既有 agent_detail_window 直测点（R1/预算/locale 系列）保持
    不动即为主等价证明；此处补跨区块语义的组合序列。"""

    def _window(self, events, *, stage="architect", lifecycle="RUNNING",
                ascii_only=False, locale="en", max_lines=10):
        return projection.agent_detail_window(
            template_slots(2), events, stage=stage,
            lifecycle=lifecycle, last_outcome=None, width=100,
            ascii_only=ascii_only, max_lines=max_lines, locale=locale)

    def test_full_lifecycle_event_sequence(self):
        events = (
            ev(ExecutionEventType.STAGE_STARTED, seq=0,
               stage="architect", runtime="rt-0"),
            ev(ExecutionEventType.INVOCATION_STARTED, seq=1,
               stage="architect", runtime="rt-0", status="STARTED"),
            ev(ExecutionEventType.HANDOFF, seq=2,
               stage="architect", runtime="rt-1", status="EMBEDDED",
               reason="EMBEDDED"),
            ev(ExecutionEventType.HANDOFF, seq=4,
               stage="coder", runtime="rt-0", status="EMBEDDED",
               reason="EMBEDDED"),
            ev(ExecutionEventType.INVOCATION_FINISHED, seq=3,
               stage="architect", runtime="rt-0", status="SUCCESS",
               duration_ms=42),
        )
        joined = "\n".join(self._window(events))
        self.assertIn("ARCHITECT", joined)
        self.assertIn("SUCCESS · 42ms", joined)
        self.assertIn("→ rt-1 (EMBEDDED)", joined)       # 出向 handoff
        self.assertIn("coder → here (EMBEDDED)", joined)  # 入向 handoff
        self.assertIn("[1] architect started", joined)    # 尾窗活动

    def test_inbound_handoff_from_other_stage_counts(self):
        events = (
            ev(ExecutionEventType.HANDOFF, seq=0, stage="coder",
               runtime="rt-0", status="EMBEDDED", reason="EMBEDDED"),
            ev(ExecutionEventType.STAGE_STARTED, seq=1,
               stage="architect", runtime="rt-0"),
        )
        joined = "\n".join(self._window(events))
        self.assertIn("coder → here (EMBEDDED)", joined)
        self.assertIn("WAITING", joined)   # STAGE_STARTED 在场 → 等待

    def test_lifecycle_overlay_axis(self):
        events = (
            ev(ExecutionEventType.INVOCATION_STARTED, seq=1,
               stage="architect", runtime="rt-0", status="STARTED"),
        )
        for lifecycle, expected in (
                ("RUNNING", "RUNNING"), ("PAUSED", "PAUSED"),
                ("PARKED", "PARKED"), ("ABORTED", "ABORTED")):
            joined = "\n".join(
                self._window(events, lifecycle=lifecycle))
            self.assertIn(expected, joined)

    def test_finished_failure_overrides_lifecycle(self):
        events = (
            ev(ExecutionEventType.INVOCATION_STARTED, seq=1,
               stage="architect", runtime="rt-0", status="STARTED"),
            ev(ExecutionEventType.INVOCATION_FINISHED, seq=2,
               stage="architect", runtime="rt-0", status="FAILURE",
               duration_ms=7),
        )
        joined = "\n".join(self._window(events, lifecycle="PARKED"))
        self.assertIn("FAILED", joined)
        self.assertIn("FAILURE · 7ms", joined)

    def test_locale_and_ascii_axes(self):
        events = (ev(ExecutionEventType.STAGE_STARTED, seq=0,
                     stage="architect", runtime="rt-0"),)
        zh = "\n".join(self._window(events, locale="zh"))
        self.assertIn("状态", zh)
        self.assertIn("等待中", zh)
        ascii_lines = "\n".join(self._window(events, ascii_only=True))
        self.assertIn("WAITING", ascii_lines)

    def test_tail_window_limit_and_overflow_marker(self):
        events = tuple(
            ev(ExecutionEventType.INVOCATION_STARTED, seq=i,
               stage="architect", runtime="rt-0", status="STARTED")
            for i in range(9))
        joined = "\n".join(self._window(events, max_lines=10))
        self.assertIn("(+3 more", joined)  # 9 行活动超尾 6 → 溢出

    def test_empty_events_honest_window(self):
        joined = "\n".join(self._window(()))
        self.assertIn("—", joined)
        self.assertIn("NOT_STARTED", joined)

if __name__ == "__main__":
    unittest.main()
