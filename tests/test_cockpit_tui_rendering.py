"""CU-TUI-3 (V3.2): Textual cockpit rendering — minimal Pilot tests.

Mandate matrix (scenarios 6/27 render halves, 37/38 source guards, plus
the wrapper contract): the app is a READ -> PROJECT -> RENDER surface
only. Execution driving comes from the entry-injected driver callback;
the app never submits control commands, never calls the segment runner,
never invokes an adapter, and holds no second copy of execution truth.

Whole-module skip when textual is absent (G14/G15): the base install
carries no UI dependency and these tests must not fabricate one.
"""
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

pytest.importorskip("textual")

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from execution_observation import ExecutionEvent, ExecutionEventType
from sequential_pipeline import RunStatus

import cockpit_projection
import cockpit_tui


class _Gate:
    """阻塞门控 driver：保持 App 存活到测试断言完成。"""

    def __init__(self):
        self.release = threading.Event()

    def driver(self):
        self.release.wait(10)
        return "gated-outcome"


def _event(seq, kind, stage="architect", runtime="rt-0", status="SUCCESS",
           reason="R", duration_ms=None):
    return ExecutionEvent(
        event_type=kind, sequence=seq, task_id="t", correlation_id="e",
        stage=stage, runtime_id=runtime, status=status, reason=reason,
        duration_ms=duration_ms)


def static_events():
    return (
        _event(0, ExecutionEventType.STAGE_STARTED),
        _event(1, ExecutionEventType.INVOCATION_STARTED),
    )


class _SessionProjection:
    """Read-only session double: projection surface only."""

    run_state = None
    terminal = None
    last_outcome = None


def make_app(**overrides):
    values = dict(
        driver=lambda: "outcome-sentinel",
        task="demo task",
        plan=(("step-0-architect", "architect", "rt-0", "prov-a"),
              ("step-1-coder", "coder", "rt-1", "prov-b")),
        events=static_events,
        facts=lambda: (),
        usage=lambda: (),
        session=_SessionProjection())
    values.update(overrides)
    return cockpit_tui.CockpitApp(**values)


# ------------------------------------------------ scenarios 6/27 (render)


class ContextPanelPilotTests(unittest.IsolatedAsyncioTestCase):

    async def test_context_visible_at_160_columns(self):
        gate = _Gate()
        app = make_app(driver=gate.driver)
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            panel = app.query_one("#context-panel")
            self.assertTrue(panel.display is True)
            self.assertTrue(app.last_state.context_lines)
            gate.release.set()

    async def test_context_hidden_below_140_columns(self):
        gate = _Gate()
        app = make_app(driver=gate.driver)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            panel = app.query_one("#context-panel")
            self.assertTrue(panel.display is False)
            gate.release.set()


class InputDockPilotTests(unittest.IsolatedAsyncioTestCase):
    """G11: the input dock is always the bottom-most zone."""

    async def test_input_dock_is_bottom_zone(self):
        gate = _Gate()
        app = make_app(driver=gate.driver)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            dock = app.query_one("#input-dock")
            header = app.query_one("#header-zone")
            self.assertGreater(dock.region.y, header.region.y)
            self.assertEqual(dock.region.y + dock.region.height, 30)
            gate.release.set()


class TraceScreenPilotTests(unittest.IsolatedAsyncioTestCase):

    async def test_trace_toggle_shows_observation_lines(self):
        gate = _Gate()
        app = make_app(driver=gate.driver)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await pilot.press("t")
            await pilot.pause()
            self.assertIsInstance(app.screen, cockpit_tui.TraceScreen)
            self.assertIn("STAGE_STARTED",
                          app.screen.observation_text)
            gate.release.set()


class DriverExecutionPilotTests(unittest.IsolatedAsyncioTestCase):
    """G4: execution flows only through the injected driver callback."""

    async def test_driver_result_reaches_app_outcome(self):
        sentinel = object()
        app = make_app(driver=lambda: sentinel)
        async with app.run_test(size=(100, 24)) as pilot:
            for _ in range(200):
                if app.outcome is not None:
                    break
                await pilot.pause()
        self.assertIs(app.outcome, sentinel)

    async def test_driver_failure_is_stored_not_swallowed(self):
        boom = RuntimeError("driver-honest-failure")

        def failing_driver():
            raise boom

        app = make_app(driver=failing_driver)
        async with app.run_test(size=(100, 24)) as pilot:
            for _ in range(200):
                if app.failure is not None:
                    break
                await pilot.pause()
        self.assertIs(app.failure, boom)


# ------------------------------------------------ scenario 37/38 + G3/G16


class TuiSourceGuardTests(unittest.TestCase):

    def setUp(self):
        with open(cockpit_tui.__file__, "r", encoding="utf-8") as handle:
            self.source = handle.read()

    def test_no_control_submission_surface(self):        # scenario 37
        for token in (".submit(", "boundary"):
            self.assertNotIn(token, self.source)

    def test_no_execution_surface(self):                 # scenario 38
        for token in ("run_segment", ".invoke(", "adapter"):
            self.assertNotIn(token, self.source)

    def test_no_truth_minting(self):
        for token in ("ExecutionEvent(", "UsageRecord(",
                      "JournalFact(", "uuid4", "random"):
            self.assertNotIn(token, self.source)

    def test_no_clock(self):
        self.assertNotIn("time.time", self.source)
        self.assertNotIn("datetime", self.source)


class TextualConfinementTests(unittest.TestCase):
    """G3: the UI framework import exists only inside this module, and
    the module itself imports cleanly when the framework is absent."""

    def test_module_imports_without_framework(self):
        saved_tui = sys.modules.pop("cockpit_tui", None)
        saved_framework = sys.modules.pop("textual", None)
        sys.modules["textual"] = None  # forces ImportError on import
        try:
            import importlib
            module = importlib.import_module("cockpit_tui")
            self.assertFalse(module.textual_available())
        finally:
            sys.modules.pop("cockpit_tui", None)
            sys.modules.pop("textual", None)
            if saved_framework is not None:
                sys.modules["textual"] = saved_framework
            if saved_tui is not None:
                sys.modules["cockpit_tui"] = saved_tui
            else:
                sys.modules["cockpit_tui"] = cockpit_tui

    def test_framework_import_lives_only_in_this_module(self):
        with open(cockpit_tui.__file__, "r", encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn("import textual", source)  # lazy, inside functions


class RunCockpitTuiWrapperTests(unittest.TestCase):
    """G16: dependency absence falls back at the entry layer; a real
    driver failure propagates honestly instead."""

    def _fake_app_class(self, *, fail=False):
        class _FakeApp:
            def __init__(self, **kwargs):
                self._driver = kwargs["driver"]
                self.outcome = None
                self.failure = None

            def run(self):
                try:
                    self.outcome = self._driver()
                except BaseException as error:  # honest failure path
                    self.failure = error

            def wait_for_driver(self):
                return None

        return _FakeApp

    def test_wrapper_returns_driver_result(self):
        sentinel = object()
        fake = self._fake_app_class()
        with mock.patch.object(cockpit_tui, "CockpitApp", fake):
            result = cockpit_tui.run_cockpit_tui(
                driver=lambda: sentinel, task="t", plan=(),
                events=lambda: (), facts=lambda: (), usage=lambda: (),
                session=None)
        self.assertIs(result, sentinel)

    def test_wrapper_propagates_driver_failure(self):
        class _Failing:
            def __init__(self, **kwargs):
                self._driver = kwargs["driver"]
                self.outcome = None
                self.failure = None

            def run(self):
                try:
                    self.outcome = self._driver()
                except BaseException as error:
                    self.failure = error

            def wait_for_driver(self):
                return None

        boom = RuntimeError("honest")
        with mock.patch.object(cockpit_tui, "CockpitApp", _Failing):
            with self.assertRaises(RuntimeError):
                cockpit_tui.run_cockpit_tui(
                    driver=_raise(boom), task="t", plan=(),
                    events=lambda: (), facts=lambda: (), usage=lambda: (),
                    session=None)


def _raise(error):
    def raiser():
        raise error
    return raiser


# ============================== CU-TUI-4: control + trace interaction


def receipt_result(status="ACCEPTED", reason=None, version=1,
                   command_id="ui-1"):
    """ControlResult duck：与冻结值同字段的只读回执输入。"""
    return SimpleNamespace(status=status, reason=reason,
                           execution_version=version,
                           command_id=command_id, execution_id="e")


def make_control(recorded, result=None):
    """注入 dispatcher double：记录 (kind, text, target) 意图元组。"""
    if result is None:
        result = receipt_result()

    def control(kind, text=None, target=None):
        recorded.append((kind, text, target))
        return result
    return control


def abort_requested_facts():
    entry = SimpleNamespace(seq=0, fact_type="ABORT_REQUESTED",
                            command_id="ui-1", execution_version=1,
                            payload=None)
    return (entry,)


class ControlModePilotTests(unittest.IsolatedAsyncioTestCase):
    """C2 状态机：COMMAND 单键直发；P/R 不换态；确认 y 恰一次。"""

    async def test_p_dispatches_pause_without_mode_change(self):
        gate = _Gate()
        recorded = []
        app = make_app(driver=gate.driver, control=make_control(recorded))
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.press("p")
            await pilot.pause()
            self.assertEqual(app._cockpit_mode, "command")
            self.assertEqual(recorded, [("PAUSE", None, None)])
            gate.release.set()

    async def test_r_dispatches_resume_without_mode_change(self):
        gate = _Gate()
        recorded = []
        app = make_app(driver=gate.driver, control=make_control(recorded))
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.press("r")
            await pilot.pause()
            self.assertEqual(app._cockpit_mode, "command")
            self.assertEqual(recorded, [("RESUME", None, None)])
            gate.release.set()

    async def test_a_confirm_y_dispatches_abort_exactly_once(self):
        gate = _Gate()
        recorded = []
        app = make_app(driver=gate.driver, control=make_control(recorded))
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.press("a")
            await pilot.pause()
            self.assertEqual(app._cockpit_mode, "confirm")
            await pilot.press("y")
            await pilot.press("y")   # 第二次 y 已回 command → no-op
            await pilot.pause()
            self.assertEqual(recorded, [("ABORT", None, None)])
            self.assertEqual(app._cockpit_mode, "command")
            gate.release.set()

    async def test_confirm_n_returns_without_dispatch(self):
        gate = _Gate()
        recorded = []
        app = make_app(driver=gate.driver, control=make_control(recorded))
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.press("a")
            await pilot.press("n")
            await pilot.pause()
            self.assertEqual(recorded, [])
            self.assertEqual(app._cockpit_mode, "command")
            gate.release.set()

    async def test_confirm_escape_returns_without_dispatch(self):
        gate = _Gate()
        recorded = []
        app = make_app(driver=gate.driver, control=make_control(recorded))
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.press("a")
            await pilot.press("escape")
            await pilot.pause()
            self.assertEqual(recorded, [])
            self.assertEqual(app._cockpit_mode, "command")
            gate.release.set()

    async def test_confirm_rejects_everything_else(self):
        gate = _Gate()
        recorded = []
        app = make_app(driver=gate.driver, control=make_control(recorded))
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.press("a")
            await pilot.press("z")
            await pilot.press("p")
            await pilot.pause()
            self.assertEqual(app._cockpit_mode, "confirm")
            self.assertEqual(recorded, [])
            gate.release.set()

    async def test_command_mode_ignores_unmapped_keys(self):
        gate = _Gate()
        recorded = []
        app = make_app(driver=gate.driver, control=make_control(recorded))
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.press("z")
            await pilot.press("ctrl+a")
            await pilot.pause()
            self.assertEqual(recorded, [])
            self.assertEqual(app._cockpit_mode, "command")
            gate.release.set()

    async def test_control_absent_keys_are_honest_noop(self):
        gate = _Gate()
        recorded = []
        app = make_app(driver=gate.driver)   # control 未注入
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.press("p")
            await pilot.press("a")
            await pilot.press("y")
            await pilot.pause()
            self.assertEqual(recorded, [])
            self.assertEqual(app._cockpit_mode, "command")
            self.assertIsNone(app._cockpit_receipt)
            gate.release.set()


class ComposerPilotTests(unittest.IsolatedAsyncioTestCase):
    """C2 REVISION_COMPOSER：字符/退格/target/Enter/Esc 语义。"""

    async def test_e_opens_composer_and_types_characters(self):
        gate = _Gate()
        recorded = []
        app = make_app(driver=gate.driver, control=make_control(recorded))
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.press("e")
            await pilot.pause()
            self.assertEqual(app._cockpit_mode, "composer")
            self.assertIn("NEXT_INVOCATION", app._dock_input_line())
            for character in "fix":
                await pilot.press(character)
            await pilot.pause()
            self.assertEqual(app._cockpit_revise_text, "fix")
            self.assertEqual(recorded, [])
            gate.release.set()

    async def test_space_and_backspace_edit_buffer(self):
        gate = _Gate()
        app = make_app(driver=gate.driver, control=make_control([]))
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.press("e")
            for character in "a b":
                await pilot.press(character)
            await pilot.press("backspace")
            await pilot.pause()
            self.assertEqual(app._cockpit_revise_text, "a ")
            gate.release.set()

    async def test_tab_toggles_target_both_ways(self):
        gate = _Gate()
        app = make_app(driver=gate.driver, control=make_control([]))
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.press("e")
            await pilot.press("tab")
            await pilot.pause()
            self.assertEqual(app._cockpit_revise_target, "SUBMISSION")
            await pilot.press("tab")
            await pilot.pause()
            self.assertEqual(app._cockpit_revise_target, "NEXT_INVOCATION")
            gate.release.set()

    async def test_enter_dispatches_revise_with_text_and_target(self):
        gate = _Gate()
        recorded = []
        app = make_app(driver=gate.driver, control=make_control(recorded))
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.press("e")
            for character in "fix":
                await pilot.press(character)
            await pilot.press("tab")
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(
                recorded, [("REVISE", "fix", "SUBMISSION")])
            self.assertEqual(app._cockpit_mode, "command")
            self.assertEqual(app._cockpit_revise_text, "")
            gate.release.set()

    async def test_enter_with_empty_buffer_is_noop(self):
        gate = _Gate()
        recorded = []
        app = make_app(driver=gate.driver, control=make_control(recorded))
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.press("e")
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(recorded, [])
            self.assertEqual(app._cockpit_mode, "composer")
            gate.release.set()

    async def test_escape_cancels_and_clears_buffer(self):
        gate = _Gate()
        recorded = []
        app = make_app(driver=gate.driver, control=make_control(recorded))
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.press("e")
            for character in "abc":
                await pilot.press(character)
            await pilot.press("escape")
            await pilot.pause()
            self.assertEqual(recorded, [])
            self.assertEqual(app._cockpit_mode, "command")
            await pilot.press("e")   # 重开：缓冲已清空
            await pilot.pause()
            self.assertEqual(app._cockpit_revise_text, "")
            gate.release.set()

    async def test_command_letters_type_inside_composer(self):
        gate = _Gate()
        recorded = []
        app = make_app(driver=gate.driver, control=make_control(recorded))
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.press("e")
            for key in ("p", "r", "a", "q", "t", "c"):
                await pilot.press(key)
            await pilot.pause()
            self.assertEqual(recorded, [])
            self.assertEqual(app._cockpit_revise_text, "praqtc")
            gate.release.set()

    async def test_modifier_keys_do_not_type(self):
        gate = _Gate()
        app = make_app(driver=gate.driver, control=make_control([]))
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.press("e")
            await pilot.press("ctrl+a")
            await pilot.press("up")
            await pilot.pause()
            self.assertEqual(app._cockpit_revise_text, "")
            self.assertEqual(app._cockpit_mode, "composer")
            gate.release.set()


class ModeTransitionMatrixTests(unittest.IsolatedAsyncioTestCase):
    """C2 三态转移表（有向序列走查）。"""

    async def test_full_transition_walk(self):
        gate = _Gate()
        recorded = []
        app = make_app(driver=gate.driver, control=make_control(recorded))
        async with app.run_test(size=(100, 24)) as pilot:
            # command → confirm → command（n）
            await pilot.press("a")
            self.assertEqual(app._cockpit_mode, "confirm")
            await pilot.press("n")
            self.assertEqual(app._cockpit_mode, "command")
            # command → composer → command（escape）
            await pilot.press("e")
            self.assertEqual(app._cockpit_mode, "composer")
            await pilot.press("escape")
            self.assertEqual(app._cockpit_mode, "command")
            # command → composer → enter 空缓冲（停留）
            await pilot.press("e")
            await pilot.press("enter")
            self.assertEqual(app._cockpit_mode, "composer")
            await pilot.press("escape")
            # composer 内 escape 后 command 面恢复工作
            await pilot.press("p")
            await pilot.pause()
            self.assertEqual(app._cockpit_mode, "command")
            self.assertEqual(recorded, [("PAUSE", None, None)])
            gate.release.set()


class ReceiptDisplayTests(unittest.IsolatedAsyncioTestCase):
    """§五：ControlResult 原样进回执，三态 + reason 逐字；有界衰减。"""

    async def test_accepted_receipt_line(self):
        gate = _Gate()
        app = make_app(driver=gate.driver, control=make_control(
            [], receipt_result("ACCEPTED", version=3, command_id="ui-1")))
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.press("p")
            await pilot.pause()
            self.assertEqual(app._cockpit_receipt,
                             "receipt ui-1: ACCEPTED v3")
            gate.release.set()

    async def test_rejected_reason_verbatim(self):
        gate = _Gate()
        app = make_app(driver=gate.driver, control=make_control(
            [], receipt_result("REJECTED", reason="ALREADY_TERMINAL")))
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.press("p")
            await pilot.pause()
            self.assertEqual(app._cockpit_receipt,
                             "receipt ui-1: REJECTED v1 · ALREADY_TERMINAL")
            gate.release.set()

    async def test_no_op_reason_verbatim(self):
        gate = _Gate()
        app = make_app(driver=gate.driver, control=make_control(
            [], receipt_result("NO_OP", reason="NOT_PAUSED")))
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.press("r")
            await pilot.pause()
            self.assertEqual(app._cockpit_receipt,
                             "receipt ui-1: NO_OP v1 · NOT_PAUSED")
            gate.release.set()

    async def test_receipt_decays_after_bounded_refreshes(self):
        gate = _Gate()
        app = make_app(driver=gate.driver, control=make_control([]))
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.press("p")
            await pilot.pause()
            self.assertIsNotNone(app._cockpit_receipt)
            for _ in range(cockpit_tui._RECEIPT_TICKS + 1):
                app._refresh()
            self.assertIsNone(app._cockpit_receipt)
            gate.release.set()


class QuitLadderTests(unittest.IsolatedAsyncioTestCase):
    """q 阶梯：终态直退；非终态进确认；ABORT 已受理 → 硬弃界面。"""

    async def test_q_at_terminal_exits_directly(self):
        gate = _Gate()
        session = SimpleNamespace(run_state=None,
                                  terminal=RunStatus.COMPLETED,
                                  last_outcome=None)
        app = make_app(driver=gate.driver, session=session)
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.press("q")
            await pilot.pause()
            self.assertFalse(app.is_running)
            gate.release.set()

    async def test_q_non_terminal_enters_abort_confirm(self):
        gate = _Gate()
        app = make_app(driver=gate.driver)
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.press("q")
            await pilot.pause()
            self.assertEqual(app._cockpit_mode, "confirm")
            gate.release.set()

    async def test_q_after_abort_accepted_hard_exits(self):
        gate = _Gate()
        app = make_app(driver=gate.driver,
                       facts=abort_requested_facts)
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.press("q")
            await pilot.pause()
            self.assertFalse(app.is_running)
            gate.release.set()


class TraceInteractionPilotTests(unittest.IsolatedAsyncioTestCase):
    """§六/§七：follow-tail/选择（稳定 seq）/详情/AGENTS/X/pending。"""

    def _mutable_events(self):
        holder = type("Holder", (), {})()
        holder.events = list(static_events())
        holder.events.append(_event(2, ExecutionEventType.INVOCATION_FINISHED,
                                    status="SUCCESS"))
        holder.events.append(_event(3, ExecutionEventType.STAGE_FINISHED,
                                    stage="coder", runtime="rt-1"))
        return holder

    async def test_agents_tab_renders_agent_detail(self):
        gate = _Gate()
        app = make_app(driver=gate.driver)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.press("t")
            await pilot.pause()
            screen = app.screen
            self.assertIn("ARCHITECT", screen.agents_text)
            self.assertIn("runtime", screen.agents_text)
            gate.release.set()

    async def test_selection_moves_on_stable_sequence(self):
        gate = _Gate()
        holder = self._mutable_events()
        app = make_app(driver=gate.driver, events=lambda: tuple(holder.events))
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.press("t")
            await pilot.pause()
            screen = app.screen
            await pilot.press("down")
            self.assertEqual(screen._trace_selected_seq, 3)
            await pilot.press("up")
            self.assertEqual(screen._trace_selected_seq, 2)
            await pilot.press("up")
            await pilot.press("up")
            self.assertEqual(screen._trace_selected_seq, 0)
            gate.release.set()

    async def test_selection_detail_renders_event_fields(self):
        gate = _Gate()
        app = make_app(driver=gate.driver)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.press("t")
            await pilot.pause()
            await pilot.press("down")
            screen = app.screen
            self.assertTrue(screen.detail_text.startswith("seq "))
            self.assertIn("type ", screen.detail_text)
            gate.release.set()

    async def test_new_events_do_not_steal_selection(self):
        gate = _Gate()
        holder = self._mutable_events()
        app = make_app(driver=gate.driver, events=lambda: tuple(holder.events))
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.press("t")
            await pilot.pause()
            screen = app.screen
            for _ in range(4):
                await pilot.press("up")
            self.assertEqual(screen._trace_selected_seq, 0)
            holder.events.append(_event(
                4, ExecutionEventType.STAGE_FINISHED,
                stage="coder", runtime="rt-1"))
            screen._trace_refresh()
            self.assertEqual(screen._trace_selected_seq, 0)
            gate.release.set()

    async def test_follow_tail_flag_transitions(self):
        gate = _Gate()
        holder = self._mutable_events()
        app = make_app(driver=gate.driver, events=lambda: tuple(holder.events))
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.press("t")
            await pilot.pause()
            screen = app.screen
            self.assertTrue(screen._trace_follow)
            await pilot.press("up")
            self.assertFalse(screen._trace_follow)
            await pilot.press("g")
            self.assertTrue(screen._trace_follow)
            gate.release.set()

    async def test_follow_off_preserves_selection_across_refresh(self):
        gate = _Gate()
        holder = self._mutable_events()
        app = make_app(driver=gate.driver, events=lambda: tuple(holder.events))
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.press("t")
            await pilot.pause()
            screen = app.screen
            for _ in range(4):
                await pilot.press("up")
            screen._trace_follow = False
            holder.events.append(_event(
                4, ExecutionEventType.STAGE_FINISHED,
                stage="coder", runtime="rt-1"))
            screen._trace_refresh()
            self.assertEqual(screen._trace_selected_seq, 0)
            self.assertFalse(screen._trace_follow)
            gate.release.set()

    async def test_x_toggles_expanded(self):
        gate = _Gate()
        app = make_app(driver=gate.driver)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.press("t")
            await pilot.pause()
            screen = app.screen
            self.assertFalse(screen._trace_expanded)
            await pilot.press("x")
            self.assertTrue(screen._trace_expanded)
            gate.release.set()

    async def test_ctrl_tab_renders_pending_and_dash_when_absent(self):
        gate = _Gate()
        app = make_app(driver=gate.driver, revision_pending=lambda: 2)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.press("t")
            await pilot.pause()
            self.assertIn("pending 2", app.screen.ctrl_text)
            gate.release.set()
        gate_two = _Gate()
        app_two = make_app(driver=gate_two.driver)   # provider 未注入
        async with app_two.run_test(size=(100, 30)) as pilot:
            await pilot.press("t")
            await pilot.pause()
            self.assertIn("pending —", app_two.screen.ctrl_text)
            gate_two.release.set()


class UiOnlyIsolationTests(unittest.IsolatedAsyncioTestCase):
    """§六.3：UI-only 交互绝不写回事实源；派发只经注入通道。"""

    async def test_key_sequences_leave_event_store_untouched(self):
        store = cockpit_tui.new_event_store()
        for event in static_events():
            store.observe(event)
        before = store.snapshot("t")
        gate = _Gate()
        recorded = []
        app = make_app(driver=gate.driver, events=lambda: store.snapshot("t"),
                       control=make_control(recorded))
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.press("t")
            await pilot.press("down")
            await pilot.press("up")
            await pilot.press("x")
            await pilot.press("escape")
            await pilot.press("e")
            for character in "abc":
                await pilot.press(character)
            await pilot.press("escape")
            await pilot.press("a")
            await pilot.press("n")
            await pilot.pause()
            gate.release.set()
        self.assertEqual(store.snapshot("t"), before)
        self.assertEqual(recorded, [])   # 零控制意图外发

    async def test_composer_only_dispatches_on_enter(self):
        gate = _Gate()
        recorded = []
        app = make_app(driver=gate.driver, control=make_control(recorded))
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.press("e")
            for character in "revise-me":
                await pilot.press(character)
            await pilot.pause()
            self.assertEqual(recorded, [])
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(
                recorded, [("REVISE", "revise-me", "NEXT_INVOCATION")])
            gate.release.set()


class ModeMachineGuardTests(unittest.TestCase):
    """C2 源码守卫：恰三态、无第四模式、零引擎词汇。"""

    def setUp(self):
        with open(cockpit_tui.__file__, "r", encoding="utf-8") as handle:
            self.source = handle.read()

    def test_no_fourth_input_mode(self):
        self.assertNotIn("INSERT", self.source)

    def test_engine_control_vocabulary_absent(self):
        for token in ("control_boundary", "revision_queue",
                      "ControlCommand", "ControlResult"):
            self.assertNotIn(token, self.source)

    def test_mode_vocabulary_is_exactly_three(self):
        for token in ('"command"', '"composer"', '"confirm"'):
            self.assertIn(token, self.source)


# ------------------------------- CU-TUI-5: first-run funnel (single App)


def funnel_composition(bindings=(("architect", "rt-a", "prov-a"),
                                 ("coder", "rt-b", "prov-b")),
                       blocked_reason=None, blocked_hint=None):
    """DefaultComposition duck（呈现面形状；canonical 载荷不透明）。"""
    return SimpleNamespace(
        roles=tuple(role for role, _, _ in bindings),
        bindings=tuple(
            SimpleNamespace(role=role, runtime_id=runtime,
                            provider_id=provider,
                            canonical_runtime_identity=(
                                runtime, provider, None, "f"))
            for role, runtime, provider in bindings),
        blocked_reason=blocked_reason, blocked_hint=blocked_hint)


def composition_changed_double(reasons, composition):
    return SimpleNamespace(reasons=tuple(reasons), composition=composition)


def composition_error_double(reason="RUNTIME_NOT_QUALIFIED",
                             detail="default collaboration needs at least 2 "
                                    "VERIFIED runtimes (found 1)",
                             hint="no persisted qualification evidence: "
                                  "run `dual-agent qualify` first"):
    return SimpleNamespace(reason=reason, detail=detail, hint=hint)


def composed_run_double(task="funnel task", gate=None):
    """ComposedRun duck：离线脚本组合句柄（Start 后的 late-bound 真相）。"""
    def drive():
        if gate is not None:
            gate.release.wait(10)
        return "funnel-outcome"

    return SimpleNamespace(
        task=task,
        steps=(("architect", "rt-a"), ("coder", "rt-b")),
        plan=(("step-0-architect", "architect", "rt-a", "prov-a"),
              ("step-1-coder", "coder", "rt-b", "prov-b")),
        task_id="t", execution_id="e", emit=None,
        drive=drive,
        session=_SessionProjection(),
        dispatch_control=lambda *args, **kwargs: None,
        revision_pending=lambda: 0,
        events=static_events, facts=lambda: (), usage=lambda: ())


def make_funnel_app(composition, start, task_token=None):
    return cockpit_tui.CockpitApp(
        composition_preview=lambda: composition,
        start_composition=start,
        task_token=task_token)


class _StartRecorder:
    """注入 start 闭包双件：记录 (task, expected) 调用、按脚本回放结果。"""

    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def __call__(self, task_text, expected_composition):
        self.calls.append((task_text, expected_composition))
        return self.results.pop(0)


def _funnel_text(app):
    return app.funnel_text


class FunnelScreenPilotTests(unittest.IsolatedAsyncioTestCase):
    """CU-TUI-5 §十三/§十六：漏斗 = 同一 App 的初始呈现阶段；Start 前
    零引擎对象、零 driver 线程、首屏恰六要素。"""

    async def test_initial_screen_six_elements_with_zones_hidden(self):
        start = _StartRecorder([])
        app = make_funnel_app(funnel_composition(), start)
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.pause()
            self.assertTrue(app.query_one("#funnel-screen").display)
            self.assertFalse(app.query_one("#header-zone").display)
            self.assertFalse(app.query_one("#input-dock").display)
            text = _funnel_text(app)
            for expected in ("dual-agent cockpit",
                             "Describe the collaboration task",
                             "Collaboration plan (default)",
                             "architect  ← rt-a · prov-a",
                             "coder      ← rt-b · prov-b",
                             "Enter start · q quit"):
                self.assertIn(expected, text)
            self.assertIsNone(app._cockpit_composed)
            self.assertIsNone(app._cockpit_thread)
            self.assertEqual(start.calls, [])

    async def test_task_token_prefills_buffer_as_composing(self):
        start = _StartRecorder([])
        app = make_funnel_app(funnel_composition(), start,
                              task_token="fix the bug")
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.pause()
            self.assertEqual(app._cockpit_funnel_buffer, "fix the bug")
            self.assertEqual(app._cockpit_stage,
                             cockpit_tui.STAGE_COMPOSING)
            self.assertIn("fix the bug", _funnel_text(app))

    async def test_typing_appends_and_backspace_empties(self):
        start = _StartRecorder([])
        app = make_funnel_app(funnel_composition(), start)
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.press("a")
            await pilot.press("b")
            await pilot.pause()
            self.assertEqual(app._cockpit_funnel_buffer, "ab")
            self.assertEqual(app._cockpit_stage,
                             cockpit_tui.STAGE_COMPOSING)
            await pilot.press("backspace")
            await pilot.press("backspace")
            await pilot.pause()
            self.assertEqual(app._cockpit_funnel_buffer, "")
            self.assertEqual(app._cockpit_stage,
                             cockpit_tui.STAGE_NOT_STARTED)

    async def test_enter_blank_shows_no_op_hint_without_start(self):
        start = _StartRecorder([])
        app = make_funnel_app(funnel_composition(), start)
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.press("enter")
            await pilot.pause()
            self.assertIn("describe the task first", _funnel_text(app))
            self.assertEqual(start.calls, [])
            self.assertTrue(app.is_running)

    async def test_enter_on_blocked_shows_reason_and_hint(self):
        start = _StartRecorder([])
        blocked = funnel_composition(
            blocked_reason="default collaboration needs at least 2 "
                           "VERIFIED runtimes (found 1)",
            blocked_hint="no persisted qualification evidence: run "
                         "`dual-agent qualify` first")
        app = make_funnel_app(blocked, start, task_token="my task")
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.press("enter")
            await pilot.pause()
            text = _funnel_text(app)
            self.assertIn(
                "default collaboration needs at least 2 VERIFIED runtimes",
                text)
            self.assertIn("dual-agent qualify", text)
            self.assertEqual(start.calls, [])
            self.assertTrue(app.is_running)

    async def test_enter_ready_starts_and_swaps_to_running(self):
        gate = _Gate()
        start = _StartRecorder([composed_run_double(gate=gate)])
        composition = funnel_composition()
        app = make_funnel_app(composition, start)
        async with app.run_test(size=(100, 24)) as pilot:
            for key in ("m", "y", "space", "t", "a", "s", "k"):
                await pilot.press(key)
            await pilot.press("enter")
            for _ in range(100):
                if app._cockpit_composed is not None:
                    break
                await pilot.pause()
            self.assertEqual(start.calls, [("my task", composition)])
            self.assertEqual(app._cockpit_stage, cockpit_tui.STAGE_RUNNING)
            self.assertFalse(app.query_one("#funnel-screen").display)
            self.assertTrue(app.query_one("#header-zone").display)
            gate.release.set()
            for _ in range(200):
                if app.outcome is not None:
                    break
                await pilot.pause()
        self.assertEqual(app.outcome, "funnel-outcome")
        self.assertEqual(app._cockpit_stage, cockpit_tui.STAGE_TERMINAL)

    async def test_composition_error_keeps_funnel_with_red_lines(self):
        start = _StartRecorder([composition_error_double(),
                                composed_run_double()])
        app = make_funnel_app(funnel_composition(), start,
                              task_token="my task")
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.press("enter")
            for _ in range(100):
                if start.calls:
                    break
                await pilot.pause()
            text = _funnel_text(app)
            self.assertIn("RUNTIME_NOT_QUALIFIED: ", text)
            self.assertIn("dual-agent qualify", text)
            self.assertTrue(app.query_one("#funnel-screen").display)
            self.assertIsNone(app._cockpit_composed)
            self.assertTrue(app.is_running)

    async def test_changed_refreshes_disclosure_then_reenter_starts(self):
        gate = _Gate()
        updated = funnel_composition(
            bindings=(("architect", "rt-a", "prov-a"),
                      ("coder", "rt-c", "prov-c")))
        start = _StartRecorder([
            composition_changed_double(
                ("runtime rt-b no longer VERIFIED",), updated),
            composed_run_double(gate=gate)])
        initial = funnel_composition()
        app = make_funnel_app(initial, start, task_token="my task")
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.press("enter")          # first Enter: changed
            for _ in range(100):
                if start.calls:
                    break
                await pilot.pause()
            text = _funnel_text(app)
            self.assertIn("collaboration plan changed:", text)
            self.assertIn("runtime rt-b no longer VERIFIED", text)
            self.assertIn("rt-c", text)          # 预览已刷新为新披露
            self.assertIs(app._cockpit_disclosure, updated)
            self.assertTrue(app.query_one("#funnel-screen").display)
            await pilot.press("enter")          # re-Enter: matches → start
            for _ in range(100):
                if app._cockpit_composed is not None:
                    break
                await pilot.pause()
            self.assertEqual(start.calls,
                             [("my task", initial), ("my task", updated)])
            self.assertFalse(app.query_one("#funnel-screen").display)
            gate.release.set()
            for _ in range(200):
                if app.outcome is not None:
                    break
                await pilot.pause()
        self.assertEqual(app.outcome, "funnel-outcome")

    async def test_escape_clears_buffer_to_not_started(self):
        start = _StartRecorder([])
        app = make_funnel_app(funnel_composition(), start,
                              task_token="draft")
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.press("escape")
            await pilot.pause()
            self.assertEqual(app._cockpit_funnel_buffer, "")
            self.assertEqual(app._cockpit_stage,
                             cockpit_tui.STAGE_NOT_STARTED)
            self.assertTrue(app.is_running)

    async def test_q_on_empty_buffer_exits_with_zero_start(self):
        start = _StartRecorder([])
        app = make_funnel_app(funnel_composition(), start)
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.press("q")
            await pilot.pause()
            self.assertFalse(app.is_running)
        app.wait_for_driver()
        self.assertIsNone(app.outcome)
        self.assertEqual(start.calls, [])

    async def test_ctrl_c_mid_composing_exits_text_loss_accepted(self):
        start = _StartRecorder([])
        app = make_funnel_app(funnel_composition(), start,
                              task_token="partial task")
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.press("ctrl+c")
            await pilot.pause()
            self.assertFalse(app.is_running)
        self.assertEqual(start.calls, [])

    async def test_q_while_composing_is_text_not_quit(self):
        start = _StartRecorder([])
        app = make_funnel_app(funnel_composition(), start,
                              task_token="star")
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.press("q")
            await pilot.pause()
            self.assertEqual(app._cockpit_funnel_buffer, "starq")
            self.assertTrue(app.is_running)


class FunnelSourceGuardTests(unittest.TestCase):
    """CU-TUI-5 守卫：TUI 零引擎 import、零 identity 计算、漏斗词
    不进 engine observation 词表。"""

    def setUp(self):
        with open(cockpit_tui.__file__, "r", encoding="utf-8") as handle:
            self.source = handle.read()

    def test_no_engine_composition_imports(self):
        for token in ("host_entry", "environment_registry",
                      "candidate_validation", "AdapterRegistry",
                      "load_evidence"):
            self.assertNotIn(token, self.source)

    def test_no_identity_computation(self):
        for token in ("canonical_runtime_identity", "config_fingerprint",
                      "fingerprint"):
            self.assertNotIn(token, self.source)

    def test_no_binding_composition_logic(self):
        # §二十-5/H-2：默认指派 sorted+zip 组合逻辑零出现（真相唯一
        # 源在 entry 的 resolve 函数；本层只消费注入闭包的结果值）
        for token in ("resolve_default_composition", "DEFAULT_ROLE_TEMPLATES",
                      "verified_pool", "zip("):
            self.assertNotIn(token, self.source)

    def test_funnel_words_absent_from_engine_vocab(self):
        import console_observation
        import execution_observation
        for module in (console_observation, execution_observation):
            with open(module.__file__, "r", encoding="utf-8") as handle:
                engine_source = handle.read()
            for token in ("COMPOSITION_CHANGED", "NOT_STARTED", "COMPOSING",
                          "STAGE_NOT_STARTED"):
                self.assertNotIn(token, engine_source)

    def test_funnel_stage_constants_exported(self):
        for name in ("STAGE_NOT_STARTED", "STAGE_COMPOSING",
                     "STAGE_RUNNING", "STAGE_TERMINAL"):
            self.assertTrue(hasattr(cockpit_tui, name))
        self.assertNotIn(cockpit_tui.STAGE_NOT_STARTED,
                         (cockpit_tui.MODE_COMMAND,
                          cockpit_tui.MODE_COMPOSER,
                          cockpit_tui.MODE_CONFIRM))


class RunCockpitFunnelWrapperTests(unittest.TestCase):
    """G16 同款外壳契约：单一 App 单次 run()；前置退出 outcome None；
    真实失败诚实上抛。"""

    def _fake_app_class(self, *, outcome="sentinel", failure=None):
        class _FakeApp:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
                self.outcome = None
                self.failure = None
                self._outcome = outcome
                self._failure = failure

            def run(self):
                self.outcome = self._outcome
                if self._failure is not None:
                    self.failure = self._failure

            def wait_for_driver(self):
                return None

        return _FakeApp

    def test_wrapper_passes_funnel_kwargs_and_returns_outcome(self):
        preview = lambda: funnel_composition()  # noqa: E731
        start = _StartRecorder([])
        fake = self._fake_app_class()
        with mock.patch.object(cockpit_tui, "CockpitApp", fake):
            result = cockpit_tui.run_cockpit_funnel(
                composition_preview=preview, start_composition=start,
                task_token="some task", timeout_seconds=45)
        self.assertEqual(result, "sentinel")

    def test_wrapper_kwargs_shape(self):
        preview = lambda: funnel_composition()  # noqa: E731
        holder = {}

        class _Capture:
            def __init__(self, **kwargs):
                holder.update(kwargs)
                self.outcome = "ok"
                self.failure = None

            def run(self):
                return None

            def wait_for_driver(self):
                return None

        with mock.patch.object(cockpit_tui, "CockpitApp", _Capture):
            cockpit_tui.run_cockpit_funnel(
                composition_preview=preview, start_composition=lambda t, e: None,
                task_token="t", timeout_seconds=45)
        self.assertEqual(
            sorted(holder),
            ["composition_preview", "start_composition", "task_token",
             "timeout_seconds"])
        self.assertEqual(holder["task_token"], "t")
        self.assertEqual(holder["timeout_seconds"], 45)

    def test_wrapper_returns_none_on_pre_start_quit(self):
        fake = self._fake_app_class(outcome=None)
        with mock.patch.object(cockpit_tui, "CockpitApp", fake):
            result = cockpit_tui.run_cockpit_funnel(
                composition_preview=lambda: funnel_composition(),
                start_composition=lambda task, expected: None)
        self.assertIsNone(result)

    def test_wrapper_propagates_failure(self):
        boom = RuntimeError("funnel-honest-failure")
        fake = self._fake_app_class(failure=boom)
        with mock.patch.object(cockpit_tui, "CockpitApp", fake):
            with self.assertRaises(RuntimeError):
                cockpit_tui.run_cockpit_funnel(
                    composition_preview=lambda: funnel_composition(),
                    start_composition=lambda task, expected: None)


if __name__ == "__main__":
    unittest.main()
