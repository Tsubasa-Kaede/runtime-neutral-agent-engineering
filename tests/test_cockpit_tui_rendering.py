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
from sequential_pipeline import RunOutcome, RunState, RunStatus

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
            await pilot.press("y")   # C2-R1：第二次 y 已回 command →
            await pilot.pause()      # auto-ingress 入 steering 缓冲
            self.assertEqual(recorded, [("ABORT", None, None)])
            self.assertEqual(app._cockpit_mode, "composer")
            self.assertEqual(app._cockpit_revise_text, "y")
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

    async def test_command_mode_ignores_modifier_combos(self):
        # C2-R1：可打印字符不再 no-op（ingress 由
        # PersistentComposerPilotTests 覆盖）——本测试收缩为修饰
        # 组合仍 no-op（character None，结构性排除）。
        gate = _Gate()
        recorded = []
        app = make_app(driver=gate.driver, control=make_control(recorded))
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.press("ctrl+a")
            await pilot.pause()
            self.assertEqual(recorded, [])
            self.assertEqual(app._cockpit_mode, "command")
            self.assertEqual(app._cockpit_revise_text, "")
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
            # C2-R1 W2：提交后留在 COMPOSER（stay-open）
            self.assertEqual(app._cockpit_mode, "composer")
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

    async def test_escape_ladder_clears_then_exits(self):
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
            # C2-R1 W3 阶梯首段：清稿、留在 COMPOSER
            self.assertEqual(app._cockpit_mode, "composer")
            self.assertEqual(app._cockpit_revise_text, "")
            await pilot.press("escape")   # 阶梯末段：空缓冲回 COMMAND
            await pilot.pause()
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
                             "✓ ACCEPTED PAUSE · ui-1 v3")
            gate.release.set()

    async def test_rejected_reason_verbatim(self):
        gate = _Gate()
        app = make_app(driver=gate.driver, control=make_control(
            [], receipt_result("REJECTED", reason="ALREADY_TERMINAL")))
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.press("p")
            await pilot.pause()
            self.assertEqual(app._cockpit_receipt,
                             "✗ REJECTED PAUSE · ui-1 v1 · ALREADY_TERMINAL")
            gate.release.set()

    async def test_no_op_reason_verbatim(self):
        gate = _Gate()
        app = make_app(driver=gate.driver, control=make_control(
            [], receipt_result("NO_OP", reason="NOT_PAUSED")))
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.press("r")
            await pilot.pause()
            self.assertEqual(app._cockpit_receipt,
                             "⊘ NO_OP RESUME · ui-1 v1 · NOT_PAUSED")
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
            # CU-TUI-INPUT A1：输入 dock 漏斗期在场（与 RUNNING 同位）
            self.assertTrue(app.query_one("#input-dock").display)
            text = _funnel_text(app)
            for expected in ("dual-agent cockpit",
                             "Describe the collaboration task",
                             "Collaboration plan (default)",
                             "architect  ← rt-a · prov-a",
                             "coder      ← rt-b · prov-b"):
                self.assertIn(expected, text)
            # 六要素之后两个（输入行/两键提示）迁入底部 dock
            self.assertEqual(app._dock_input_line(), "> |")
            self.assertEqual(app._dock_controls_line(),
                             "Enter start · q quit")
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
            # CU-TUI-INPUT A1：回显在底部 dock，不在顶部 body
            self.assertEqual(app._dock_input_line(), "> fix the bug|")
            self.assertNotIn("fix the bug", _funnel_text(app))

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


# ------------------- CU-TUI-6 Phase P: reliability + stateful interaction


class _LiveSession:
    """可变 session 双件：投影面字段随 scripted outcome 推进。"""

    def __init__(self):
        self.run_state = None
        self.terminal = None
        self.last_outcome = None


def _outcome(status, **kwargs):
    return RunOutcome(status=status, **kwargs)


def _park_resume_abort_driver(session):
    """scripted 驱动：PARKED → (唤醒后续驱) ABORTED。"""
    calls = []

    def drive():
        calls.append(len(calls))
        if len(calls) == 1:
            outcome = _outcome(RunStatus.PARKED, run_state=RunState(
                next_step_index=1))
        else:
            outcome = _outcome(RunStatus.ABORTED)
        session.last_outcome = outcome
        session.run_state = getattr(outcome, "run_state", None)
        session.terminal = (None if outcome.status is RunStatus.PARKED
                            else outcome.status)
        return outcome

    return drive, calls


class PhasePDriveLoopTests(unittest.IsolatedAsyncioTestCase):
    """P-01~P-05/P-11：驱动循环——PARKED 停驻、RESUME 续驱、终态保留。"""

    async def _await_condition(self, pilot, predicate, limit=400):
        for _ in range(limit):
            if predicate():
                return True
            await pilot.pause()
        return predicate()

    async def test_park_keeps_app_alive_and_resume_completes(self):
        session = _LiveSession()
        drive, calls = _park_resume_abort_driver(session)
        recorded = []
        app = make_app(driver=drive, session=session,
                       control=make_control(recorded,
                                            receipt_result("ACCEPTED")))
        async with app.run_test(size=(100, 24)) as pilot:
            ok = await self._await_condition(
                pilot, lambda: app.outcome is not None
                and getattr(app.outcome, "status", None) is RunStatus.PARKED)
            self.assertTrue(ok, "first segment never parked")
            # P-01：PARKED 后 App 存活（绝不因 outcome 到达而退出）
            self.assertTrue(app.is_running)
            self.assertEqual(len(calls), 1)
            # 停驻键位：R 在场、P 退场
            self.assertIn("[R]esume", app._dock_controls_line())
            self.assertNotIn("[P]ause", app._dock_controls_line())
            await pilot.press("r")                       # P-02
            ok = await self._await_condition(
                pilot, lambda: len(calls) >= 2)
            self.assertTrue(ok, "resume never re-drove the segment")
            self.assertEqual(recorded, [("RESUME", None, None)])
            self.assertTrue(app.is_running)              # P-03：终态保留
            self.assertIn("ABORTED", app.last_state.badge)
            # 终态键位：只剩 T/Q（R2 追加语言提示 [L]中文——approved
            # R2 test synchronization，行为语义不变）
            self.assertEqual(app._dock_controls_line(),
                             "[T]race [L]中文 [Q]uit")
            await pilot.press("t")                       # 终态 Trace 可开
            await pilot.pause()
            self.assertIsInstance(app.screen, cockpit_tui.TraceScreen)
            await pilot.press("escape")
            await pilot.pause()

    async def test_failed_outcome_keeps_app_alive_trace_accessible(self):
        session = _LiveSession()
        failed = _outcome(RunStatus.FAILED, error=RuntimeError("boom"))

        def drive():
            session.last_outcome = failed
            session.terminal = RunStatus.FAILED
            return failed

        app = make_app(driver=drive, session=session)
        async with app.run_test(size=(100, 24)) as pilot:
            ok = await self._await_condition(
                pilot, lambda: app.outcome is not None)
            self.assertTrue(ok)
            self.assertTrue(app.is_running)              # P-04/P-08
            self.assertIn("FAILED", app.last_state.badge)
            await pilot.press("t")                       # P-05/P-09
            await pilot.pause()
            self.assertIsInstance(app.screen, cockpit_tui.TraceScreen)
            await pilot.press("escape")
            await pilot.pause()
            self.assertTrue(app.is_running)

    async def test_worker_exception_visible_not_fatal(self):
        boom = RuntimeError("worker-honest-failure")

        def failing():
            raise boom

        app = make_app(driver=failing)
        async with app.run_test(size=(100, 24)) as pilot:
            ok = await self._await_condition(
                pilot, lambda: app.failure is not None)
            self.assertTrue(ok)
            await pilot.pause()                          # 让 call_from_thread 落地
            self.assertTrue(app.is_running)              # P-14：不闪退
            self.assertIs(app.failure, boom)             # 原因保留
            self.assertIn("WORKER ERROR", app.result_text)
            self.assertIn("worker-honest-failure", app.result_text)

    async def test_q_while_parked_follows_existing_ladder(self):
        session = _LiveSession()
        drive, calls = _park_resume_abort_driver(session)
        recorded = []
        app = make_app(driver=drive, session=session,
                       control=make_control(recorded,
                                            receipt_result("ACCEPTED")))
        async with app.run_test(size=(100, 24)) as pilot:
            ok = await self._await_condition(
                pilot, lambda: app.outcome is not None)
            self.assertTrue(ok)
            await pilot.press("q")                       # P-11：不直退
            await pilot.pause()
            self.assertEqual(app._cockpit_mode, "confirm")
            await pilot.press("y")                       # 确认 = ABORT
            ok = await self._await_condition(
                pilot, lambda: len(calls) >= 2)
            self.assertTrue(ok, "abort never re-drove to ABORTED")
            self.assertEqual(recorded, [("ABORT", None, None)])
            ok = await self._await_condition(
                pilot, lambda: app.last_state is not None
                and "ABORTED" in app.last_state.badge)
            self.assertTrue(ok)
            await pilot.press("q")                       # 终态 q 直退
            await pilot.pause()
            self.assertFalse(app.is_running)

    async def test_parked_thread_releases_on_exit_flag(self):
        session = _LiveSession()
        drive, calls = _park_resume_abort_driver(session)
        app = make_app(driver=drive, session=session)
        async with app.run_test(size=(100, 24)) as pilot:
            ok = await self._await_condition(
                pilot, lambda: app.outcome is not None)
            self.assertTrue(ok)
            app._cockpit_exit_requested = True
            app._cockpit_wake.set()
            await pilot.pause()
        app.wait_for_driver()      # join 不悬挂（若悬挂本测试超时失败）
        self.assertEqual(len(calls), 1)                  # 退出不再续驱

    async def test_failed_then_q_exits_with_outcome_retained(self):
        """P-12：FAILED 后按既有契约退出（终态直退、outcome 保留）。"""
        session = _LiveSession()
        failed = _outcome(RunStatus.FAILED, error=RuntimeError("boom"))

        def drive():
            session.last_outcome = failed
            session.terminal = RunStatus.FAILED
            return failed

        app = make_app(driver=drive, session=session)
        async with app.run_test(size=(100, 24)) as pilot:
            ok = await self._await_condition(
                pilot, lambda: app.outcome is not None)
            self.assertTrue(ok)
            await pilot.press("q")
            await pilot.pause()
            self.assertFalse(app.is_running)
        app.wait_for_driver()
        self.assertIs(app.outcome, failed)


class PhasePEscapeTests(unittest.IsolatedAsyncioTestCase):
    """P-06~P-09：ESC 阶梯——全部接入既有语义，零新模式。"""

    async def test_escape_in_command_opens_abort_confirm(self):
        gate = _Gate()
        app = make_app(driver=gate.driver)
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.press("escape")
            await pilot.pause()
            self.assertEqual(app._cockpit_mode, "confirm")
            gate.release.set()

    async def test_escape_in_confirm_returns_to_command(self):
        gate = _Gate()
        app = make_app(driver=gate.driver)
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.press("escape")
            await pilot.press("escape")
            await pilot.pause()
            self.assertEqual(app._cockpit_mode, "command")
            gate.release.set()

    async def test_escape_in_composer_returns_to_command(self):
        gate = _Gate()
        app = make_app(driver=gate.driver)
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.press("e")
            await pilot.press("escape")
            await pilot.pause()
            self.assertEqual(app._cockpit_mode, "command")
            gate.release.set()

    async def test_escape_in_trace_returns_to_main(self):
        gate = _Gate()
        app = make_app(driver=gate.driver)
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.press("t")
            await pilot.pause()
            self.assertIsInstance(app.screen, cockpit_tui.TraceScreen)
            await pilot.press("escape")
            await pilot.pause()
            self.assertNotIsInstance(app.screen, cockpit_tui.TraceScreen)
            gate.release.set()


class PhasePReceiptTests(unittest.IsolatedAsyncioTestCase):
    """P-10：回执与键位提示分行独立在场。"""

    async def test_receipt_row_separate_from_controls(self):
        gate = _Gate()
        app = make_app(driver=gate.driver, control=make_control(
            [], receipt_result("ACCEPTED")))
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.press("p")
            await pilot.pause()
            self.assertEqual(app._dock_receipt_line(),
                             "✓ ACCEPTED PAUSE · ui-1 v1")
            self.assertIn("[P]ause", app._dock_controls_line())
            gate.release.set()

    async def test_receipt_row_empty_before_any_dispatch(self):
        gate = _Gate()
        app = make_app(driver=gate.driver)
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.pause()
            self.assertEqual(app._dock_receipt_line(), "")
            gate.release.set()


class PhasePResponsivenessTests(unittest.IsolatedAsyncioTestCase):
    """P-13：worker 占线期间键事件仍被 on_key 接收。"""

    async def test_key_events_processed_while_worker_runs(self):
        started = threading.Event()
        release = threading.Event()

        def slow_driver():
            started.set()
            release.wait(10)
            return "slow-outcome"

        recorded = []
        app = make_app(driver=slow_driver,
                       control=make_control(recorded,
                                            receipt_result("ACCEPTED")))
        async with app.run_test(size=(100, 24)) as pilot:
            for _ in range(200):
                if started.is_set():
                    break
                await pilot.pause()
            self.assertTrue(started.is_set())
            await pilot.press("p")     # invocation 在飞时按键
            await pilot.pause()
            self.assertEqual(recorded, [("PAUSE", None, None)])
            release.set()


class PhasePTruthGuardTests(unittest.TestCase):
    """P-15/P-16：观察真相唯一在事件索引、控制真相唯一在账本。"""

    def setUp(self):
        with open(cockpit_tui.__file__, "r", encoding="utf-8") as handle:
            self.source = handle.read()
        with open(cockpit_projection.__file__, "r", encoding="utf-8") as h:
            self.projection_source = h.read()

    def test_no_observation_surface_added(self):
        # new_event_store 是既有获准工厂；此处只禁写面
        for token in (".observe(", ".record("):
            self.assertNotIn(token, self.source)

    def test_no_control_history_surface_added(self):
        for token in ("JournalFact(", ".snapshot()", "revision_queue"):
            self.assertNotIn(token, self.source)

    def test_wake_primitive_stays_tui_private(self):
        # wake 只是本模块私有同步原语：绝不进投影层、绝不当真相
        self.assertNotIn("wake", self.projection_source)
        self.assertIn("_cockpit_wake", self.source)

    def test_drive_loop_never_exits_app_on_outcome(self):
        # 驱动循环体内不得出现 exit 调用（退出只经用户 q/Ctrl-C 阶梯）
        loop_start = self.source.index("def _drive_loop")
        loop_end = self.source.index("def wait_for_driver")
        loop_body = self.source[loop_start:loop_end]
        self.assertNotIn("self.exit", loop_body)
        self.assertNotIn(".exit()", loop_body)


class PhaseVMainScreenTests(unittest.IsolatedAsyncioTestCase):
    """Phase V §八-§十五：主屏产品化——header 状态、task 锚点、agent
    面板、活动尾窗、结果呈现（全部经 last_state/渲染文本属性断言）。"""

    async def test_header_carries_lifecycle_state(self):
        gate = _Gate()
        app = make_app(driver=gate.driver)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            self.assertIn("RUNNING", app.header_text)
            self.assertTrue(app.header_text.endswith("● RUNNING"))
            self.assertIn("dual-agent cockpit", app.header_text)
            gate.release.set()

    async def test_task_zone_is_pure_task_anchor(self):
        gate = _Gate()
        app = make_app(driver=gate.driver)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            self.assertIn("demo task", app.task_text)
            self.assertNotIn("RUNNING", app.task_text)  # 状态已归 header
            gate.release.set()

    async def test_four_agent_pipeline_renders(self):
        gate = _Gate()
        plan = tuple(
            (f"step-{index}", role, f"rt-{index}", "prov")
            for index, role in enumerate(
                ("architect", "coder", "tester", "reviewer")))
        app = make_app(driver=gate.driver, plan=plan)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            lines = app.agent_text.splitlines()
            # R1 管线（MAIN_WIDE 三列）：[A,B,C] 行块(3)+▲+↳[D] 续行块(3)
            self.assertEqual(len(lines), 7)
            joined = app.agent_text
            for role in ("ARCHITECT", "CODER", "TESTER", "REVIEWER"):
                self.assertIn(role, joined)
            for runtime in ("rt-0", "rt-1", "rt-2", "rt-3"):
                self.assertIn(runtime, joined)
            self.assertTrue(lines[4].startswith("↳"))
            gate.release.set()

    async def test_activity_zone_shows_recent_events(self):
        gate = _Gate()
        app = make_app(driver=gate.driver)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            self.assertIn("[0]", app.activity_text)
            self.assertIn("[1]", app.activity_text)
            self.assertIn("architect", app.activity_text)
            gate.release.set()

    async def test_activity_empty_honest_state(self):
        gate = _Gate()
        app = make_app(driver=gate.driver, events=lambda: ())
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            self.assertEqual(app.activity_text, "No activity yet")
            gate.release.set()

    async def test_activity_hidden_below_24_height_dock_stays(self):
        gate = _Gate()
        app = make_app(driver=gate.driver)
        async with app.run_test(size=(100, 20)) as pilot:
            await pilot.pause()
            self.assertFalse(app.query_one("#activity-zone").display)
            dock = app.query_one("#input-dock")
            self.assertTrue(dock.display)
            self.assertEqual(dock.region.y + dock.region.height, 20)
            gate.release.set()

    async def test_new_event_reveal_decays_after_refresh(self):
        gate = _Gate()
        store = []
        app = make_app(driver=gate.driver,
                       events=lambda: tuple(store))
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            self.assertNotIn("▸", app.activity_text)
            store.append(_event(7, ExecutionEventType.STAGE_STARTED,
                                stage="coder", runtime="rt-1"))
            app._refresh()
            self.assertIn("▸", app.activity_text)
            app._refresh()      # 无新事件 → 揭示标记衰减
            self.assertNotIn("▸", app.activity_text)
            gate.release.set()

    async def test_result_reveal_transient_on_terminal(self):
        session = _LiveSession()
        completed = _outcome(RunStatus.COMPLETED)

        def drive():
            session.last_outcome = completed
            session.terminal = RunStatus.COMPLETED
            return completed

        app = make_app(driver=drive, session=session)
        async with app.run_test(size=(100, 30)) as pilot:
            for _ in range(400):
                if app.outcome is not None:
                    break
                await pilot.pause()
            self.assertTrue(app.outcome is not None)
            self.assertTrue(app.result_text.startswith("» "))
            self.assertIn("✓ COMPLETED", app.result_text)
            for _ in range(4):
                app._refresh()
            self.assertFalse(app.result_text.startswith("» "))
            self.assertIn("✓ COMPLETED", app.result_text)


class PhaseVDockHintTests(unittest.IsolatedAsyncioTestCase):
    """Phase V §十七：键位提示按真实 lifecycle 派生——RUNNING 不提示
    R（受理必 NO_OP）、≥140 列追加 [C]ontext。"""

    async def test_running_hint_omits_resume(self):
        gate = _Gate()
        app = make_app(driver=gate.driver)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            line = app._dock_controls_line()
            self.assertIn("[P]ause", line)
            self.assertIn("[E]dit", line)
            self.assertIn("[A]bort", line)
            self.assertNotIn("[R]esume", line)
            gate.release.set()

    async def test_wide_adds_context_hint(self):
        gate = _Gate()
        app = make_app(driver=gate.driver)
        async with app.run_test(size=(150, 30)) as pilot:
            await pilot.pause()
            self.assertIn("[C]ontext", app._dock_controls_line())
            gate.release.set()

    async def test_narrow_omits_context_hint(self):
        gate = _Gate()
        app = make_app(driver=gate.driver)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            self.assertNotIn("[C]ontext", app._dock_controls_line())
            gate.release.set()


class PhaseVPulseTests(unittest.IsolatedAsyncioTestCase):
    """Phase V §二十四 A-1：活动脉冲 = 纯呈现（tick 派生、零时钟），
    只翻转 RUNNING agent 符号，绝不触碰事实源。"""

    async def test_pulse_alternates_glyph_truth_untouched(self):
        gate = _Gate()
        store = list(static_events())
        app = make_app(driver=gate.driver,
                       events=lambda: tuple(store))
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            before = tuple(store)
            seen = set()
            for tick in (0, 1, 2, 3, 4, 5):
                app._cockpit_tick = tick
                app._refresh()
                for glyph in ("●", "◉"):
                    if glyph in app.agent_text:
                        seen.add(glyph)
            self.assertEqual(seen, {"●", "◉"})    # 两相都出现过
            self.assertEqual(tuple(store), before)  # 事实源零变化
            self.assertEqual(app.last_state.lifecycle, "RUNNING")
            gate.release.set()


class PhaseVAnimationGuardTests(unittest.IsolatedAsyncioTestCase):
    """Phase V §四：动画参数绝不写回事实源——reveal/pulse/result_reveal
    路径下事件存储与账本零变化。"""

    async def test_animation_refreshes_leave_event_store_untouched(self):
        gate = _Gate()
        store = list(static_events())
        app = make_app(driver=gate.driver,
                       events=lambda: tuple(store))
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            before = tuple(store)
            snapshot = tuple(store)
            app._refresh()
            store.append(_event(5, ExecutionEventType.INVOCATION_FINISHED,
                                stage="architect", runtime="rt-0"))
            app._refresh()
            app._cockpit_tick = 3
            app._refresh()
            # 先前快照对象未被动过；新增事件是测试自身的注入
            self.assertEqual(snapshot, before)
            self.assertEqual(len(store), 3)
            gate.release.set()


class PhaseVTraceFollowTests(unittest.IsolatedAsyncioTestCase):
    """Phase V §二十：follow 断开计数 + g/end 恢复 + Enter 详情。"""

    def _mutable_events(self):
        events = [
            _event(0, ExecutionEventType.STAGE_STARTED),
            _event(1, ExecutionEventType.INVOCATION_STARTED),
        ]

        def reader():
            return tuple(events)

        return events, reader

    async def _open_trace(self, app, pilot):
        await pilot.press("t")
        await pilot.pause()
        return app.screen

    async def test_pinned_status_counts_new_events(self):
        gate = _Gate()
        events, reader = self._mutable_events()
        app = make_app(driver=gate.driver, events=reader)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            screen = await self._open_trace(app, pilot)
            self.assertEqual(screen.trace_status_text, "")
            await pilot.press("up")            # 选择即钉住
            screen._trace_refresh()
            events.append(_event(2, ExecutionEventType.INVOCATION_FINISHED,
                                 stage="architect", runtime="rt-0"))
            events.append(_event(3, ExecutionEventType.STAGE_FINISHED,
                                 stage="architect", runtime="rt-0"))
            screen._trace_refresh()
            self.assertIn("2 new events", screen.trace_status_text)
            self.assertFalse(screen._trace_follow)
            gate.release.set()

    async def test_g_resumes_tail_and_clears_status(self):
        gate = _Gate()
        events, reader = self._mutable_events()
        app = make_app(driver=gate.driver, events=reader)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            screen = await self._open_trace(app, pilot)
            await pilot.press("up")
            events.append(_event(2, ExecutionEventType.INVOCATION_FINISHED,
                                 stage="architect", runtime="rt-0"))
            screen._trace_refresh()
            self.assertFalse(screen._trace_follow)
            await pilot.press("g")
            await pilot.pause()
            self.assertTrue(screen._trace_follow)
            self.assertEqual(screen.trace_status_text, "")
            gate.release.set()

    async def test_end_resumes_tail(self):
        gate = _Gate()
        events, reader = self._mutable_events()
        app = make_app(driver=gate.driver, events=reader)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            screen = await self._open_trace(app, pilot)
            await pilot.press("up")
            screen._trace_refresh()
            self.assertFalse(screen._trace_follow)
            await pilot.press("end")
            await pilot.pause()
            self.assertTrue(screen._trace_follow)
            gate.release.set()

    async def test_enter_toggles_expanded_like_x(self):
        gate = _Gate()
        app = make_app(driver=gate.driver)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            screen = await self._open_trace(app, pilot)
            self.assertFalse(screen._trace_expanded)
            await pilot.press("enter")
            await pilot.pause()
            self.assertTrue(screen._trace_expanded)
            gate.release.set()


# ------------------------------------------- R1: selection / expansion


class R1SelectionPilotTests(unittest.IsolatedAsyncioTestCase):
    """R1 P5：←/→ 选中移动 = 纯呈现态——marker/▲ 跟随、两端 clamp、
    空 plan no-op、零 dispatch、facts/events 快照零变化。"""

    async def test_arrow_moves_selection_marker(self):
        gate = _Gate()
        recorded = []
        app = make_app(driver=gate.driver,
                       control=make_control(recorded,
                                            receipt_result("ACCEPTED")))
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            self.assertEqual(app._cockpit_selected_index, 0)
            # architect 在途：脉冲相位 ●/◉ 由 tick 派生，两种皆合法
            self.assertTrue("▶ ● ARCHITECT" in app.agent_text
                            or "▶ ◉ ARCHITECT" in app.agent_text)
            await pilot.press("right")
            await pilot.pause()
            self.assertEqual(app._cockpit_selected_index, 1)
            self.assertIn("▶ ○ CODER", app.agent_text)   # 静态事件：coder 未开始
            self.assertNotIn("▶ ● ARCHITECT", app.agent_text)
            gate.release.set()
        self.assertEqual(recorded, [])                   # 零外发

    async def test_selection_clamps_at_both_ends(self):
        gate = _Gate()
        app = make_app(driver=gate.driver)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await pilot.press("left")                     # 0 处向左
            await pilot.pause()
            self.assertEqual(app._cockpit_selected_index, 0)
            for _ in range(4):                            # 末位向右
                await pilot.press("right")
            await pilot.pause()
            self.assertEqual(app._cockpit_selected_index, 1)
            gate.release.set()

    async def test_selection_is_presentation_only(self):
        gate = _Gate()
        events_before = None
        app = make_app(driver=gate.driver)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            events_before = app._cockpit_events()
            # CU-PERF-1 W2：主屏 include_trace=False 后 trace_obs 恒
            # （）——数据驱动稳定性 witness 迁至 activity_lines 与
            # lifecycle（同一只读快照面，语义不变）。
            activity_before = app.last_state.activity_lines
            lifecycle_before = app.last_state.lifecycle
            await pilot.press("right")
            await pilot.pause()
            self.assertEqual(app._cockpit_events(), events_before)
            self.assertEqual(app.last_state.activity_lines,
                             activity_before)
            self.assertEqual(app.last_state.lifecycle, lifecycle_before)
            self.assertIsNone(app._cockpit_session.run_state)
            gate.release.set()

    async def test_empty_plan_selection_no_op(self):
        gate = _Gate()
        app = make_app(driver=gate.driver, plan=())
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await pilot.press("right")
            await pilot.pause()
            self.assertEqual(app._cockpit_selected_index, 0)
            self.assertEqual(app.agent_text, "")
            self.assertTrue(app.is_running)
            gate.release.set()


class R1ExpansionPilotTests(unittest.IsolatedAsyncioTestCase):
    """R1 P6：Enter/Space 切换选中 agent 展开——至多一个展开、展开 B
    自动折叠 A、Detail 窗出现于管线正下方。"""

    async def test_enter_expands_selected_agent(self):
        gate = _Gate()
        app = make_app(driver=gate.driver)
        async with app.run_test(size=(100, 36)) as pilot:
            await pilot.pause()
            self.assertIsNone(app._cockpit_expanded_stage)
            self.assertFalse(app.query_one("#detail-zone").display)
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(app._cockpit_expanded_stage,
                             "step-0-architect")
            self.assertIn("▼ ARCHITECT", app.detail_text)
            # 展开标记在 architect 行（脉冲相位 ●/◉ 由 tick 派生，两种皆合法）
            self.assertTrue("▼ ● ARCHITECT" in app.agent_text
                            or "▼ ◉ ARCHITECT" in app.agent_text)
            self.assertTrue(app.query_one("#detail-zone").display)
            gate.release.set()

    async def test_enter_again_collapses(self):
        gate = _Gate()
        app = make_app(driver=gate.driver)
        async with app.run_test(size=(100, 36)) as pilot:
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            self.assertIsNone(app._cockpit_expanded_stage)
            self.assertEqual(app.detail_text, "")
            self.assertFalse(app.query_one("#detail-zone").display)
            gate.release.set()

    async def test_space_expands_like_enter(self):
        gate = _Gate()
        app = make_app(driver=gate.driver)
        async with app.run_test(size=(100, 36)) as pilot:
            await pilot.pause()
            await pilot.press("space")
            await pilot.pause()
            self.assertEqual(app._cockpit_expanded_stage,
                             "step-0-architect")
            self.assertIn("▼ ARCHITECT", app.detail_text)
            gate.release.set()

    async def test_expanding_other_collapses_previous(self):
        gate = _Gate()
        app = make_app(driver=gate.driver)
        async with app.run_test(size=(100, 36)) as pilot:
            await pilot.pause()
            await pilot.press("enter")            # 展开 architect
            await pilot.pause()
            await pilot.press("right")
            await pilot.press("enter")            # 展开 coder → 折叠 architect
            await pilot.pause()
            self.assertEqual(app._cockpit_expanded_stage, "step-1-coder")
            self.assertEqual(app.agent_text.count("▼"), 1)  # 至多一个 ▼
            self.assertIn("▼ CODER", app.detail_text)
            self.assertNotIn("▼ ARCHITECT", app.detail_text)
            gate.release.set()


class R1SelectionExpansionIndependenceTests(unittest.IsolatedAsyncioTestCase):
    """R1 P7：selected ≠ expanded——移动选中时展开态不跟随。"""

    async def test_expansion_stays_when_selection_moves(self):
        gate = _Gate()
        app = make_app(driver=gate.driver)
        async with app.run_test(size=(100, 36)) as pilot:
            await pilot.pause()
            await pilot.press("enter")            # 展开 + 选中 architect
            await pilot.pause()
            await pilot.press("right")            # 选中 coder，展开不跟随
            await pilot.pause()
            self.assertEqual(app._cockpit_selected_index, 1)
            self.assertEqual(app._cockpit_expanded_stage,
                             "step-0-architect")
            # ▼ 留在原 cell（脉冲相位 ●/◉ 皆合法）
            self.assertTrue("▼ ● ARCHITECT" in app.agent_text
                            or "▼ ◉ ARCHITECT" in app.agent_text)
            self.assertIn("▶ ○ CODER", app.agent_text)      # 选中者 ▶
            self.assertIn("▼ ARCHITECT", app.detail_text)   # detail 仍是 A
            gate.release.set()


class R1IdentityBindingTests(unittest.IsolatedAsyncioTestCase):
    """R1 P8：expanded 绑稳定 stage 身份；组合快照重排/缩减后 detail
    仍指向同一 agent，selected 安全 clamp。"""

    async def test_reorder_keeps_expanded_identity(self):
        gate = _Gate()
        plan = (("step-0-architect", "architect", "rt-0", "prov-a"),
                ("step-1-coder", "coder", "rt-1", "prov-b"),
                ("step-2-reviewer", "reviewer", "rt-2", "prov-c"))
        app = make_app(driver=gate.driver, plan=plan)
        async with app.run_test(size=(120, 36)) as pilot:
            await pilot.pause()
            await pilot.press("enter")            # 展开 architect
            await pilot.pause()
            self.assertEqual(app._cockpit_expanded_stage,
                             "step-0-architect")
            # 组合重排（stage 身份不变，次序变化）
            app._cockpit_plan = (("step-1-coder", "coder", "rt-1", "prov-b"),
                                 ("step-0-architect", "architect", "rt-0",
                                  "prov-a"),
                                 ("step-2-reviewer", "reviewer", "rt-2",
                                  "prov-c"))
            app._refresh()
            self.assertEqual(app._cockpit_expanded_stage,
                             "step-0-architect")
            self.assertIn("▼ ARCHITECT", app.detail_text)  # 同一 agent
            gate.release.set()

    async def test_shrink_clamps_selection_and_drops_detail(self):
        gate = _Gate()
        plan = (("step-0-architect", "architect", "rt-0", "prov-a"),
                ("step-1-coder", "coder", "rt-1", "prov-b"),
                ("step-2-reviewer", "reviewer", "rt-2", "prov-c"))
        app = make_app(driver=gate.driver, plan=plan)
        async with app.run_test(size=(120, 36)) as pilot:
            await pilot.pause()
            await pilot.press("right")
            await pilot.press("right")            # 选中 reviewer（index 2）
            await pilot.press("enter")            # 展开 reviewer
            await pilot.pause()
            self.assertEqual(app._cockpit_expanded_stage, "step-2-reviewer")
            app._cockpit_plan = plan[:2]          # 组合缩减
            app._refresh()
            self.assertEqual(app._cockpit_selected_index, 1)   # clamp
            self.assertEqual(app.detail_text, "")  # 身份消失 → 诚实空
            self.assertFalse(app.query_one("#detail-zone").display)
            gate.release.set()


class R1DetailPilotTests(unittest.IsolatedAsyncioTestCase):
    """R1 §十：Detail 位于管线正下方、prompt 诚实 —、空态隐藏、窄高
    （<24）隐藏而 dock 恒在底部。"""

    async def test_detail_zone_sits_below_collab(self):
        gate = _Gate()
        app = make_app(driver=gate.driver)
        async with app.run_test(size=(100, 36)) as pilot:
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            collab = app.query_one("#collab-zone")
            detail = app.query_one("#detail-zone")
            self.assertTrue(detail.display)
            self.assertGreater(detail.region.y, collab.region.y)
            gate.release.set()

    async def test_detail_prompt_is_honest_dash(self):
        gate = _Gate()
        app = make_app(driver=gate.driver)
        async with app.run_test(size=(100, 36)) as pilot:
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            prompt_lines = [line for line in app.detail_text.splitlines()
                            if "prompt" in line]
            self.assertEqual(len(prompt_lines), 1)
            self.assertTrue(prompt_lines[0].rstrip().endswith("—"))
            gate.release.set()

    async def test_detail_hidden_below_24_height_dock_stays(self):
        gate = _Gate()
        app = make_app(driver=gate.driver)
        async with app.run_test(size=(100, 20)) as pilot:
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(app._cockpit_expanded_stage, "step-0-architect")
            self.assertFalse(app.query_one("#detail-zone").display)
            dock = app.query_one("#input-dock")
            self.assertTrue(dock.display)
            self.assertEqual(dock.region.y + dock.region.height, 20)
            gate.release.set()


class R1KeyIsolationTests(unittest.IsolatedAsyncioTestCase):
    """R1 §七：新四键仅在 COMMAND 态生效——COMPOSER 内 Space=文本、
    Enter=提交；CONFIRM 只认 y/n/esc；漏斗前置态走漏斗语义。"""

    async def test_composer_space_is_text_not_expand(self):
        gate = _Gate()
        app = make_app(driver=gate.driver)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await pilot.press("e")                 # 打开修订编辑
            await pilot.pause()
            for key in ("a", "space", "b"):
                await pilot.press(key)
            await pilot.pause()
            self.assertEqual(app._cockpit_revise_text, "a b")
            self.assertIsNone(app._cockpit_expanded_stage)  # 未误触发展开
            self.assertEqual(app._dock_input_line(), "revise [NEXT_INVOCATION] a b|")
            await pilot.press("escape")
            gate.release.set()

    async def test_confirm_enter_and_space_are_no_op(self):
        gate = _Gate()
        app = make_app(driver=gate.driver)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await pilot.press("a")                 # 进入中止确认
            await pilot.pause()
            self.assertEqual(app._cockpit_mode, cockpit_tui.MODE_CONFIRM)
            await pilot.press("enter")
            await pilot.press("space")
            await pilot.pause()
            self.assertEqual(app._cockpit_mode, cockpit_tui.MODE_CONFIRM)
            await pilot.press("n")                 # 取消回 COMMAND
            await pilot.pause()
            self.assertEqual(app._cockpit_mode, cockpit_tui.MODE_COMMAND)
            gate.release.set()

    async def test_funnel_pre_start_new_keys_are_funnel_scoped(self):
        composition = funnel_composition()
        start = _StartRecorder([composed_run_double()])
        app = make_funnel_app(composition, start)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            for key in ("left", "right", "space"):
                await pilot.press(key)
            await pilot.pause()
            self.assertEqual(start.calls, [])       # 零 Start
            self.assertEqual(app._cockpit_selected_index, 0)
            self.assertIn("Describe the collaboration task", _funnel_text(app))
            self.assertTrue(app.is_running)
            await pilot.press("escape")             # 清空缓冲不退出
            await pilot.pause()
            self.assertTrue(app.is_running)
            app.exit()


# ------------------- R2: bilingual locale switch (L key, EN ⇄ ZH)


class R2DefaultLocaleTests(unittest.IsolatedAsyncioTestCase):
    """R2-1：默认 en（零 OS/env 探测）；首帧英文呈现。"""

    def test_default_locale_is_en(self):
        app = make_app()
        self.assertEqual(app._cockpit_locale, "en")

    async def test_first_frame_renders_english(self):
        gate = _Gate()
        app = make_app(driver=gate.driver)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            self.assertIn("TASK", app.task_text)
            self.assertIn("dual-agent cockpit", app.header_text)
            self.assertIn("[T]race", app._dock_controls_line())
            self.assertNotIn("追踪", app._dock_controls_line())
            gate.release.set()


class R2LocaleToggleTests(unittest.IsolatedAsyncioTestCase):
    """R2-2：主屏 COMMAND 按 L 即时 EN⇄ZH；呈现词换装、事实原文、
    dock 提示互换。"""

    async def test_l_toggles_en_zh_en(self):
        gate = _Gate()
        app = make_app(driver=gate.driver)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            self.assertIn("TASK", app.task_text)
            await pilot.press("l")
            await pilot.pause()
            self.assertEqual(app._cockpit_locale, "zh")
            self.assertIn("任务", app.task_text)
            self.assertIn("demo task", app.task_text)   # task 原文
            self.assertIn("运行中", app.last_state.badge)
            self.assertIn("运行中", app.agent_text)     # architect 在途
            self.assertIn("未开始", app.agent_text)     # coder 静态
            self.assertIn("rt-0", app.agent_text)       # runtime 不译
            self.assertIn("ARCHITECT", app.agent_text)  # ROLE 不译
            self.assertIn("[L] EN", app._dock_controls_line())
            self.assertIn("[P]暂停", app._dock_controls_line())
            self.assertIn("[Q]退出", app._dock_controls_line())
            await pilot.press("l")
            await pilot.pause()
            self.assertEqual(app._cockpit_locale, "en")
            self.assertIn("TASK", app.task_text)
            gate.release.set()

    async def test_uppercase_l_toggles_too(self):
        gate = _Gate()
        app = make_app(driver=gate.driver)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await pilot.press("L")
            await pilot.pause()
            self.assertEqual(app._cockpit_locale, "zh")
            gate.release.set()


class R2LocalePurityTests(unittest.IsolatedAsyncioTestCase):
    """R2-3：切换零事实影响——events/trace 三面/journal 读数/run_state
    快照逐项相等。"""

    async def test_toggle_keeps_truth_identical(self):
        gate = _Gate()
        record = SimpleNamespace(usage_status="KNOWN", input_tokens=10,
                                 output_tokens=5, runtime_id="rt-0",
                                 role="architect")
        facts = (SimpleNamespace(seq=0, fact_type="PAUSE_REQUESTED",
                                 command_id="ui-1", execution_version=1,
                                 payload=None),)
        app = make_app(driver=gate.driver,
                       facts=lambda: facts,
                       usage=lambda: (record,))
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            events_before = app._cockpit_events()
            en_state = app.last_state
            await pilot.press("l")
            await pilot.pause()
            zh_state = app.last_state
            self.assertEqual(app._cockpit_events(), events_before)
            for field in ("trace_obs", "trace_ctrl", "trace_usage",
                          "lifecycle", "tier"):
                self.assertEqual(getattr(en_state, field),
                                 getattr(zh_state, field), field)
            self.assertIsNone(app._cockpit_session.run_state)
            self.assertIsNone(app._cockpit_session.terminal)
            gate.release.set()


class R2ComposerIsolationTests(unittest.IsolatedAsyncioTestCase):
    """R2-5：composer 内 l/L 是文本字符，绝不切换 locale。"""

    async def test_l_is_plain_text_in_composer(self):
        gate = _Gate()
        app = make_app(driver=gate.driver)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await pilot.press("e")
            await pilot.pause()
            self.assertEqual(app._cockpit_mode,
                             cockpit_tui.MODE_COMPOSER)
            for character in ("l", "o", "c", "a", "l"):
                await pilot.press(character)
            await pilot.pause()
            self.assertEqual(app._cockpit_revise_text, "local")
            self.assertEqual(app._cockpit_locale, "en")
            await pilot.press("L")                     # 大写同为文本
            await pilot.pause()
            self.assertEqual(app._cockpit_revise_text, "localL")
            self.assertEqual(app._cockpit_locale, "en")
            self.assertIn("localL", app._dock_input_line())
            gate.release.set()


class R2ConfirmIsolationTests(unittest.IsolatedAsyncioTestCase):
    """R2-6：ABORT_CONFIRM 中 L/l no-op（不换态、不切换）。"""

    async def test_l_no_op_in_confirm(self):
        gate = _Gate()
        app = make_app(driver=gate.driver)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await pilot.press("a")
            await pilot.pause()
            self.assertEqual(app._cockpit_mode,
                             cockpit_tui.MODE_CONFIRM)
            await pilot.press("l")
            await pilot.press("L")
            await pilot.pause()
            self.assertEqual(app._cockpit_mode,
                             cockpit_tui.MODE_CONFIRM)
            self.assertEqual(app._cockpit_locale, "en")
            await pilot.press("n")
            await pilot.pause()
            self.assertEqual(app._cockpit_mode,
                             cockpit_tui.MODE_COMMAND)
            gate.release.set()


class R2TraceIsolationTests(unittest.IsolatedAsyncioTestCase):
    """R2-7：Trace 推屏时 L no-op（键冒泡被显式拦截）；trace 三事实
    面不变；返回主屏后 L 恢复生效。"""

    async def test_l_no_op_inside_trace(self):
        gate = _Gate()
        app = make_app(driver=gate.driver)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await pilot.press("t")
            await pilot.pause()
            self.assertIsInstance(app.screen, cockpit_tui.TraceScreen)
            obs_before = app.screen.observation_text
            await pilot.press("l")
            await pilot.pause()
            self.assertEqual(app._cockpit_locale, "en")
            self.assertEqual(app.screen.observation_text, obs_before)
            self.assertIn("STAGE_STARTED", app.screen.observation_text)
            # escape 弹回主屏——Phase P 语义下该 escape 亦落入
            # ESC@COMMAND 进 confirm（既有冻结行为，与 R2 无关）；
            # n 退出 confirm 后回 COMMAND 主屏
            await pilot.press("escape")
            await pilot.pause()
            self.assertNotIsInstance(app.screen, cockpit_tui.TraceScreen)
            await pilot.press("n")
            await pilot.pause()
            self.assertEqual(app._cockpit_mode,
                             cockpit_tui.MODE_COMMAND)
            await pilot.press("l")
            await pilot.pause()
            self.assertEqual(app._cockpit_locale, "zh")
            gate.release.set()


class R2AnimationLocaleTests(unittest.IsolatedAsyncioTestCase):
    """R2-12：切换不重置动画态——seen/reveal/prev 不变、脉冲相位不
    翻转（切换走零推进渲染，tick 只可能被 0.5s 刷新周期自然推进）、
    EN→ZH→EN 渲染逐字节还原。"""

    async def test_toggle_preserves_animation_state(self):
        gate = _Gate()
        app = make_app(driver=gate.driver)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            seen = app._cockpit_seen_seq
            reveal = app._cockpit_result_reveal_ticks
            prev = app._cockpit_prev_lifecycle
            text_en = app.agent_text
            await pilot.press("l")
            await pilot.pause()
            self.assertEqual(app._cockpit_locale, "zh")
            self.assertEqual(app._cockpit_seen_seq, seen)
            self.assertEqual(app._cockpit_result_reveal_ticks, reveal)
            self.assertEqual(app._cockpit_prev_lifecycle, prev)
            self.assertIn("运行中", app.agent_text)
            # 脉冲相位只能被 0.5s 刷新周期成对推进（切换本身零推进
            # ——同相位往返）；EN→ZH→EN 渲染逐字节还原（●/◉ 归一）
            await pilot.press("l")
            await pilot.pause()
            self.assertEqual(app.agent_text.replace("◉", "●"),
                             text_en.replace("◉", "●"))
            self.assertEqual(app._cockpit_seen_seq, seen)
            self.assertEqual(app._cockpit_result_reveal_ticks, reveal)
            gate.release.set()


class R2DiscoverabilityTests(unittest.IsolatedAsyncioTestCase):
    """R2-13：COMMAND 三档 lifecycle dock 均含 [L] 提示且指向对侧
    语言（EN 态 [L]中文 / ZH 态 [L] EN）。"""

    async def test_running_dock_shows_language_hint(self):
        gate = _Gate()
        app = make_app(driver=gate.driver)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            self.assertIn("[L]中文", app._dock_controls_line())
            await pilot.press("l")
            await pilot.pause()
            self.assertIn("[L] EN", app._dock_controls_line())
            gate.release.set()

    async def test_parked_and_terminal_dock_show_language_hint(self):
        session = _LiveSession()
        drive, _calls = _park_resume_abort_driver(session)
        recorded = []
        app = make_app(driver=drive, session=session,
                       control=make_control(recorded,
                                            receipt_result("ACCEPTED")))
        async with app.run_test(size=(100, 24)) as pilot:
            parked = False
            for _ in range(400):
                state = getattr(app, "last_state", None)
                if state is not None and state.lifecycle == "PARKED":
                    parked = True
                    break
                await pilot.pause()
            self.assertTrue(parked, "first segment never parked")
            line = app._dock_controls_line()
            self.assertIn("[R]esume", line)
            self.assertIn("[L]中文", line)
            await pilot.press("l")                     # PARKED 下可切换
            await pilot.pause()
            self.assertIn("[L] EN", app._dock_controls_line())
            await pilot.press("l")
            await pilot.pause()
            await pilot.press("r")
            aborted = False
            for _ in range(400):
                outcome = getattr(app, "outcome", None)
                if outcome is not None and getattr(
                        outcome, "status", None) is RunStatus.ABORTED:
                    aborted = True
                    break
                await pilot.pause()
            self.assertTrue(aborted, "resume never re-drove the segment")
            terminal = app._dock_controls_line()
            self.assertIn("[T]race", terminal)
            self.assertIn("[L]中文", terminal)
            self.assertIn("[Q]uit", terminal)


class R2FunnelLocaleTests(unittest.IsolatedAsyncioTestCase):
    """R2-14：漏斗 EN 冻结——L 是任务文本字符，不切换 locale。"""

    async def test_l_is_task_text_in_funnel(self):
        start = _StartRecorder([])
        app = make_funnel_app(funnel_composition(), start)
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.pause()
            await pilot.press("l")
            await pilot.pause()
            self.assertEqual(app._cockpit_funnel_buffer, "l")
            self.assertEqual(app._cockpit_locale, "en")
            self.assertIn("Describe the collaboration task",
                          _funnel_text(app))


# ==================== CU-TUI-INPUT: dock budget + funnel dock + affordance


_PLAN4 = tuple(
    (f"step-{index}", role, f"rt-{index}", "prov")
    for index, role in enumerate(
        ("architect", "coder", "tester", "reviewer")))

_EVENTS8 = (
    _event(0, ExecutionEventType.STAGE_STARTED),
    _event(1, ExecutionEventType.INVOCATION_STARTED),
    _event(2, ExecutionEventType.INVOCATION_FINISHED),
    _event(3, ExecutionEventType.STAGE_FINISHED, stage="coder"),
    _event(4, ExecutionEventType.STAGE_STARTED, stage="coder"),
    _event(5, ExecutionEventType.INVOCATION_STARTED, stage="coder"),
    _event(6, ExecutionEventType.INVOCATION_FINISHED, stage="coder"),
    _event(7, ExecutionEventType.HANDOFF, stage="coder"),
)


def _terminal_session(output_text=" ".join("x" * 40 for _ in range(5))):
    """终态 session 双件：COMPLETED + 长输出 final_result（96 列内容
    宽下 5 词 → 3 包装行 → result 区恰 4 行）。"""
    session = _LiveSession()
    session.terminal = RunStatus.COMPLETED
    session.last_outcome = RunOutcome(
        status=RunStatus.COMPLETED,
        final_result=SimpleNamespace(output=output_text))
    return session


class CockpitInputDockBudgetTests(unittest.IsolatedAsyncioTestCase):
    """CU-TUI-INPUT A4：内容高度预算——内容行总和必须服从 dock 保留
    高度（终端高 - 3）；activity 尾窗最先整区让位（§二十二门规）、
    detail 有界窗按余量收窄、result 交付物保留头行。"""

    def _assert_no_dock_overlap(self, app, height):
        dock = app.query_one("#input-dock")
        self.assertEqual(dock.region.y + dock.region.height, height)
        for selector in ("#header-zone", "#task-zone", "#collab-zone",
                         "#detail-zone", "#activity-zone", "#result-zone",
                         "#progress-zone"):
            zone = app.query_one(selector)
            if zone.display and zone.region.height:
                self.assertLessEqual(
                    zone.region.y + zone.region.height, dock.region.y,
                    f"{selector} renders into dock rows")

    async def test_four_heights_expanded_detail_never_touch_dock(self):
        for height in (24, 26, 28, 32):
            gate = _Gate()
            app = make_app(driver=gate.driver, plan=_PLAN4,
                           events=lambda: _EVENTS8)
            async with app.run_test(size=(120, height)) as pilot:
                await pilot.pause()
                await pilot.press("enter")   # 展开选中 architect
                await pilot.pause()
                self.assertEqual(app._cockpit_expanded_stage, "step-0")
                self.assertTrue(app.query_one("#detail-zone").display)
                self.assertTrue(app.query_one("#progress-zone").display)
                self._assert_no_dock_overlap(app, height)
                gate.release.set()

    async def test_activity_yields_first_at_tight_heights(self):
        # 4-agent + 展开：24/26/28 activity 让位隐藏；32 容纳全显
        for height, shown in ((24, False), (26, False), (28, False),
                              (32, True)):
            gate = _Gate()
            app = make_app(driver=gate.driver, plan=_PLAN4,
                           events=lambda: _EVENTS8)
            async with app.run_test(size=(120, height)) as pilot:
                await pilot.pause()
                await pilot.press("enter")
                await pilot.pause()
                self.assertIs(app.query_one("#activity-zone").display,
                              shown, f"height={height}")
                gate.release.set()

    async def test_detail_window_shrinks_to_remaining_budget(self):
        # 终态（result 在场占行）+ 展开 @24：detail 收窄至余量并带
        # (+N more · T) 诚实溢出标记，绝不溢出 dock
        gate = _Gate()
        app = make_app(driver=gate.driver, plan=_PLAN4,
                       events=lambda: _EVENTS8,
                       session=_terminal_session())
        async with app.run_test(size=(120, 24)) as pilot:
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            lines = app.detail_text.splitlines()
            self.assertEqual(len(lines), 6)
            self.assertIn("more · T", lines[-1])
            self._assert_no_dock_overlap(app, 24)
            gate.release.set()

    async def test_result_hidden_when_empty_running(self):
        gate = _Gate()
        app = make_app(driver=gate.driver)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            self.assertFalse(app.query_one("#result-zone").display)
            gate.release.set()

    async def test_long_output_terminal_four_heights_result_stays(self):
        for height in (24, 26, 28, 32):
            gate = _Gate()
            app = make_app(driver=gate.driver, plan=_PLAN4,
                           events=lambda: _EVENTS8,
                           session=_terminal_session())
            async with app.run_test(size=(120, height)) as pilot:
                await pilot.pause()
                result = app.query_one("#result-zone")
                self.assertTrue(result.display)
                self.assertIn("COMPLETED", app.result_text)
                self._assert_no_dock_overlap(app, height)
                gate.release.set()

    async def test_progress_visible_at_all_four_heights(self):
        for height in (24, 26, 28, 32):
            gate = _Gate()
            app = make_app(driver=gate.driver, plan=_PLAN4,
                           events=lambda: _EVENTS8)
            async with app.run_test(size=(120, height)) as pilot:
                await pilot.pause()
                progress = app.query_one("#progress-zone")
                self.assertTrue(progress.display)
                self.assertIn("STAGE", app.last_state.progress_line)
                self.assertIn("TOKENS", app.last_state.tokens_line)
                gate.release.set()


class FunnelInputDockTests(unittest.IsolatedAsyncioTestCase):
    """CU-TUI-INPUT A1/A2：漏斗输入迁入底部 dock（与 RUNNING 同位）——
    回显/两键提示入 dock、body 零输入行、Start 前后输入位置恒底。"""

    async def test_funnel_dock_visible_and_pinned_bottom(self):
        start = _StartRecorder([])
        app = make_funnel_app(funnel_composition(), start)
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.pause()
            dock = app.query_one("#input-dock")
            self.assertTrue(dock.display)
            self.assertEqual(dock.region.y + dock.region.height, 24)
            self.assertTrue(app.query_one("#funnel-screen").display)
            self.assertFalse(app.query_one("#header-zone").display)
            self.assertFalse(app.query_one("#collab-zone").display)

    async def test_input_echo_lives_in_dock_not_body(self):
        start = _StartRecorder([])
        app = make_funnel_app(funnel_composition(), start)
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.pause()
            await pilot.press("a")
            await pilot.press("b")
            await pilot.pause()
            self.assertEqual(app._dock_input_line(), "> ab|")
            body_lines = _funnel_text(app).splitlines()
            self.assertFalse(any(line.startswith("> ")
                                 for line in body_lines))
            self.assertNotIn("> ab", _funnel_text(app))

    async def test_keys_hint_lives_in_dock_controls(self):
        start = _StartRecorder([])
        app = make_funnel_app(funnel_composition(), start)
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.pause()
            self.assertEqual(app._dock_controls_line(),
                             "Enter start · q quit")
            self.assertNotIn("Enter start", _funnel_text(app))

    async def test_receipt_row_empty_during_funnel(self):
        start = _StartRecorder([])
        app = make_funnel_app(funnel_composition(), start)
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.pause()
            self.assertEqual(app._dock_receipt_line(), "")

    async def test_prefill_echoes_in_dock(self):
        start = _StartRecorder([])
        app = make_funnel_app(funnel_composition(), start,
                              task_token="fix the bug")
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.pause()
            self.assertEqual(app._dock_input_line(), "> fix the bug|")

    async def test_unsafe_prefill_redacted_in_dock(self):
        secret = "sk-ant-api03-ABCDEFGHIJKLMNOPQRSTUVWXYZ1234"
        start = _StartRecorder([])
        app = make_funnel_app(funnel_composition(), start,
                              task_token=secret)
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.pause()
            line = app._dock_input_line()
            self.assertIn("[redacted: unsafe content]", line)
            self.assertNotIn(secret, line)
            self.assertNotIn(secret, _funnel_text(app))

    async def test_funnel_to_running_input_position_never_jumps(self):
        gate = _Gate()
        start = _StartRecorder([composed_run_double(gate=gate)])
        app = make_funnel_app(funnel_composition(), start)
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.pause()
            dock = app.query_one("#input-dock")
            funnel_y = dock.region.y
            self.assertEqual(funnel_y, 21)
            for key in ("m", "y", "space", "t", "a", "s", "k"):
                await pilot.press(key)
            await pilot.press("enter")
            for _ in range(100):
                if app._cockpit_composed is not None:
                    break
                await pilot.pause()
            self.assertEqual(app._cockpit_stage, cockpit_tui.STAGE_RUNNING)
            self.assertFalse(app.query_one("#funnel-screen").display)
            self.assertTrue(app.query_one("#header-zone").display)
            dock = app.query_one("#input-dock")
            self.assertEqual(dock.region.y, funnel_y)
            self.assertEqual(dock.region.y + dock.region.height, 24)
            gate.release.set()


class CommandAffordanceTests(unittest.IsolatedAsyncioTestCase):
    """CU-TUI-INPUT A3 → CU-INPUT-2 W8（C2-R1）：COMMAND 态输入行呈
    转向提示（普通字符从 no-op 反转为即输入）；P/R/E/A contract
    逐字不变。"""

    async def test_command_input_line_is_steering_hint_not_prompt(self):
        gate = _Gate()
        app = make_app(driver=gate.driver)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            self.assertEqual(app._dock_input_line(),
                             "type to steer · enter submits")
            self.assertNotEqual(app._dock_input_line(), ">")
            gate.release.set()

    async def test_command_affordance_bilingual_via_l(self):
        gate = _Gate()
        app = make_app(driver=gate.driver)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await pilot.press("l")
            await pilot.pause()
            self.assertEqual(app._dock_input_line(),
                             "输入即可转向 · enter 提交")
            await pilot.press("l")
            await pilot.pause()
            self.assertEqual(app._dock_input_line(),
                             "type to steer · enter submits")
            gate.release.set()

    async def test_composer_and_confirm_lines_unchanged(self):
        gate = _Gate()
        app = make_app(driver=gate.driver)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await pilot.press("e")
            await pilot.pause()
            self.assertEqual(app._dock_input_line(), "revise [NEXT_INVOCATION] |")
            await pilot.press("escape")
            await pilot.press("a")
            await pilot.pause()
            self.assertEqual(app._dock_input_line(), "abort? (y/n)")
            gate.release.set()

    async def test_ordinary_text_ingresses_steering_composer(self):
        gate = _Gate()
        recorded = []
        app = make_app(driver=gate.driver,
                       control=make_control(recorded))
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            for key in ("z", "x", "1", "!"):
                await pilot.press(key)
            await pilot.pause()
            # C2-R1 W1 语义反转（A3 witness 迁移）：普通字符即输入
            self.assertEqual(app._cockpit_mode, "composer")
            self.assertEqual(app._cockpit_revise_text, "zx1!")
            self.assertEqual(
                app._dock_input_line(), "revise [NEXT_INVOCATION] zx1!|")
            self.assertEqual(recorded, [])
            self.assertEqual(app._dock_receipt_line(), "")
            gate.release.set()

    async def test_command_keys_still_dispatch_with_receipt(self):
        gate = _Gate()
        recorded = []
        app = make_app(driver=gate.driver,
                       control=make_control(
                           recorded,
                           receipt_result("REJECTED",
                                          reason="ALREADY_TERMINAL")))
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await pilot.press("r")
            await pilot.pause()
            self.assertEqual(recorded, [("RESUME", None, None)])
            receipt = app._dock_receipt_line()
            self.assertIn("REJECTED", receipt)
            self.assertIn("ALREADY_TERMINAL", receipt)
            self.assertEqual(app._cockpit_mode, "command")
            gate.release.set()


# ------------------- CU-PERF-1: change detection / narrow pass / trace split


class ChangeDetectionPilotTests(unittest.IsolatedAsyncioTestCase):
    """T4/T10：主屏变更检测——同输入零 update、pulse 恰一区、零事件
    空闲多 tick 零 update。"""

    async def _drive(self, app, ticks):
        for _ in range(ticks):
            app._refresh()
            await asyncio.sleep(0)

    def _patched_static_update_counter(self, calls):
        """Static.update 为同步签名（返回 None）——包装器保持同步，
        计数后原样委托。"""
        from textual.widgets import Static as _Static
        original_update = _Static.update

        def counting(self, *args, **kwargs):
            calls.append(getattr(self, "id", None) or "?")
            return original_update(self, *args, **kwargs)
        return mock.patch.object(_Static, "update", counting)

    async def test_identical_refresh_zero_updates(self):
        gate = _Gate()
        app = make_app(driver=gate.driver)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            app._refresh()
            await pilot.pause()
            calls = []
            with self._patched_static_update_counter(calls):
                app._refresh(advance_tick=False)   # 同输入零推进
                app._refresh(advance_tick=False)
            self.assertEqual(calls, [])            # T4：0 次底层 update
            gate.release.set()

    async def test_pulse_flip_updates_exactly_collab_zone(self):
        gate = _Gate()

        def events():
            return (_event(0, ExecutionEventType.STAGE_STARTED),
                    _event(1, ExecutionEventType.INVOCATION_STARTED))

        app = make_app(driver=gate.driver, events=events)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            # 对齐相位边界：推进至偶数 tick（pulse 相位稳定）
            while app._cockpit_tick % 2:
                app._refresh()
            app._refresh()               # 基线写入完成
            await pilot.pause()
            calls = []
            with self._patched_static_update_counter(calls):
                # pulse 每 2 tick 翻转：恰 2 次推进跨恰 1 次相位翻转
                app._refresh()
                app._refresh()
            # 恰一次真实 DOM 写入，且目标是协作管线区（pulse 翻转
            # RUNNING 符号；其余区零事实变化零写入）
            self.assertEqual(calls.count("collab-zone"), 1)
            self.assertEqual(len(calls), 1)
            gate.release.set()

    async def test_zero_event_idle_multi_tick_zero_updates(self):
        gate = _Gate()
        app = make_app(driver=gate.driver,
                       events=lambda: (), plan=())
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            app._refresh()
            await pilot.pause()
            calls = []
            with self._patched_static_update_counter(calls):
                for _ in range(4):
                    app._refresh()      # 零事件零在途 → pulse 不入渲染
            self.assertEqual(calls, [])  # T10：空闲多 tick 0 update
            gate.release.set()


class NarrowSinglePassPilotTests(unittest.IsolatedAsyncioTestCase):
    """T5：A4 收窄触发场景 build_projection 每 tick 恰 1 次。"""

    async def test_a4_narrow_single_projection_per_tick(self):
        gate = _Gate()
        # 展开 + 矮终端 + 多行 result → 预算收窄必然触发
        session = _SessionProjection()
        session.last_outcome = RunOutcome(
            status=RunStatus.COMPLETED,
            final_result=SimpleNamespace(
                output="line one\nline two\nline three\nline four"))

        def events():
            return (_event(0, ExecutionEventType.STAGE_STARTED),
                    _event(1, ExecutionEventType.INVOCATION_STARTED),
                    _event(2, ExecutionEventType.INVOCATION_FINISHED,
                           duration_ms=5))

        app = make_app(driver=gate.driver, events=events,
                       session=session)
        async with app.run_test(size=(100, 24)) as pilot:   # 矮终端
            await pilot.pause()
            app._cockpit_expanded_stage = "step-0-architect"
            app._refresh()
            await pilot.pause()
            counter = []
            original = cockpit_tui.build_projection

            def counting(values):
                counter.append(len(counter))
                return original(values)

            with mock.patch.object(cockpit_tui, "build_projection",
                                  counting):
                app._refresh()
            self.assertEqual(len(counter), 1)   # T5：恰 1 次（改前 2）
            gate.release.set()


class MainTraceTransitionTests(unittest.IsolatedAsyncioTestCase):
    """T6：MAIN↔TRACE 往返——变更检测镜像不卡死后继事实更新。"""

    async def test_round_trip_then_fact_change_still_renders(self):
        gate = _Gate()
        holder = {"events": ()}

        def events():
            return holder["events"]

        app = make_app(driver=gate.driver, events=events)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            await pilot.press("t")
            await pilot.pause()
            self.assertIsInstance(app.screen, cockpit_tui.TraceScreen)
            await pilot.press("escape")
            await pilot.pause()
            self.assertNotIsInstance(app.screen, cockpit_tui.TraceScreen)
            # escape 同时经 App on_key 进入 confirm（既有语义：先回
            # command 再开 Trace——R2 坑清单同款）
            await pilot.press("n")
            await pilot.pause()
            self.assertEqual(app._cockpit_mode, "command")
            # 往返后事实源变化 → 主屏如实更新（镜像无 stale）
            holder["events"] = (
                _event(0, ExecutionEventType.STAGE_STARTED),)
            app._refresh()
            self.assertIn("stage started",
                          app.activity_text)
            await pilot.press("t")
            await pilot.pause()
            self.assertIsInstance(app.screen, cockpit_tui.TraceScreen)
            self.assertIn("[0]", app.screen.observation_text)
            gate.release.set()

    async def test_trace_screen_projection_count_zero(self):
        gate = _Gate()

        def events():
            return (_event(0, ExecutionEventType.STAGE_STARTED),)

        app = make_app(driver=gate.driver, events=events)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            await pilot.press("t")
            await pilot.pause()
            counter = []
            with mock.patch.object(cockpit_tui, "build_projection",
                                  side_effect=lambda v: (
                                      counter.append(1),
                                      cockpit_tui.__dict__ and None,
                                  )[1] or None) as _patched:
                # _trace_refresh 自身不得触发任何 build_projection
                app.screen._trace_refresh()
            self.assertEqual(counter, [])   # T7：Trace 屏 0 次全量投影
            gate.release.set()


class TraceOwnershipPilotTests(unittest.IsolatedAsyncioTestCase):
    """T7：TraceScreen 三函数直取——文本与三函数输出 golden 相等。"""

    async def test_obs_text_matches_trace_observation_lines(self):
        gate = _Gate()
        events = (_event(0, ExecutionEventType.STAGE_STARTED),
                  _event(1, ExecutionEventType.INVOCATION_STARTED))
        app = make_app(driver=gate.driver, events=lambda: events)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            await pilot.press("t")
            await pilot.pause()
            expected_lines = [
                "  " + cockpit_projection.format_event_line(e).rstrip("\n")
                for e in events]
            self.assertEqual(
                app.screen.observation_text.split("\n"), expected_lines)
            gate.release.set()

    async def test_main_projection_skips_trace_fields(self):
        gate = _Gate()
        events = (_event(0, ExecutionEventType.STAGE_STARTED),)
        app = make_app(driver=gate.driver, events=lambda: events)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            # 主屏投影 include_trace=False（W2 免算断言）
            values = app._collect_inputs()
            self.assertFalse(values.include_trace)
            self.assertEqual(app.last_state.trace_obs, ())
            gate.release.set()


# ------------------- CU-INPUT-2 (C2-R1): persistent steering composer


class PersistentComposerPilotTests(unittest.IsolatedAsyncioTestCase):
    """T1/T3/T4/T5/T9/T10：COMMAND 态 auto-ingress、stay-open 提交、
    命令键豁免、q/a 文本所有权、target 粘滞。"""

    async def test_typing_in_command_ingresses_composer(self):
        gate = _Gate()
        recorded = []
        app = make_app(driver=gate.driver, control=make_control(recorded))
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await pilot.press("h")
            await pilot.pause()
            self.assertEqual(app._cockpit_mode, "composer")
            self.assertEqual(app._cockpit_revise_text, "h")
            self.assertEqual(
                app._dock_input_line(), "revise [NEXT_INVOCATION] h|")
            self.assertEqual(recorded, [])
            gate.release.set()

    async def test_ingress_then_enter_submits_and_stays_open(self):
        gate = _Gate()
        recorded = []
        app = make_app(driver=gate.driver, control=make_control(recorded))
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            for character in "hello":
                await pilot.press(character)
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(
                recorded, [("REVISE", "hello", "NEXT_INVOCATION")])
            self.assertIn("ACCEPTED", app._dock_receipt_line())  # T11
            self.assertEqual(app._cockpit_revise_text, "")
            self.assertEqual(app._cockpit_mode, "composer")  # W2 stay-open
            gate.release.set()

    async def test_consecutive_submissions_dispatch_in_order(self):
        gate = _Gate()
        recorded = []
        app = make_app(driver=gate.driver, control=make_control(recorded))
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            for word in ("A", "B", "C"):
                await pilot.press(word)
                await pilot.press("enter")
                await pilot.pause()
                self.assertEqual(app._cockpit_revise_text, "")  # 每条间清零
                self.assertEqual(app._cockpit_mode, "composer")
            self.assertEqual(  # T5：派发序 = 提交序（队列 FIFO 属冻结面）
                recorded,
                [("REVISE", "A", "NEXT_INVOCATION"),
                 ("REVISE", "B", "NEXT_INVOCATION"),
                 ("REVISE", "C", "NEXT_INVOCATION")])
            gate.release.set()

    async def test_command_keys_are_exempt_from_ingress(self):
        gate = _Gate()
        recorded = []
        app = make_app(driver=gate.driver, control=make_control(recorded))
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await pilot.press("p")       # PAUSE 派发，非 ingress
            await pilot.pause()
            self.assertEqual(recorded, [("PAUSE", None, None)])
            self.assertEqual(app._cockpit_mode, "command")
            await pilot.press("r")       # RESUME 派发
            await pilot.pause()
            self.assertEqual(recorded[1], ("RESUME", None, None))
            await pilot.press("c")       # Context 切换（呈现态）
            await pilot.pause()
            self.assertEqual(app._cockpit_mode, "command")
            await pilot.press("l")       # 语言切换（l/L 豁免）
            await pilot.pause()
            self.assertEqual(app._cockpit_locale, "zh")
            await pilot.press("a")       # 中止确认
            await pilot.pause()
            self.assertEqual(app._cockpit_mode, "confirm")
            await pilot.press("n")
            await pilot.pause()
            await pilot.press("q")       # 非终态 quit 阶梯 → confirm
            await pilot.pause()
            self.assertEqual(app._cockpit_mode, "confirm")
            await pilot.press("n")
            await pilot.pause()
            self.assertEqual(app._cockpit_mode, "command")
            self.assertEqual(app._cockpit_revise_text, "")  # 全程零入缓冲
            gate.release.set()

    async def test_uppercase_command_letter_variant_ingresses(self):
        gate = _Gate()
        recorded = []
        app = make_app(driver=gate.driver, control=make_control(recorded))
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await pilot.press("P")   # 大写变体非命令键（漏斗 Q 先例）
            await pilot.pause()
            self.assertEqual(app._cockpit_mode, "composer")
            self.assertEqual(app._cockpit_revise_text, "P")
            self.assertEqual(recorded, [])
            gate.release.set()

    async def test_q_typed_inside_composer_is_text_never_quits(self):
        gate = _Gate()
        recorded = []
        app = make_app(driver=gate.driver, control=make_control(recorded))
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await pilot.press("e")
            for character in "quick":
                await pilot.press(character)
            await pilot.pause()
            self.assertEqual(app._cockpit_revise_text, "quick")  # T10
            self.assertEqual(app._cockpit_mode, "composer")
            self.assertTrue(app.is_running)   # 未退出、未入确认
            gate.release.set()

    async def test_a_typed_inside_composer_is_text_not_confirm(self):
        gate = _Gate()
        recorded = []
        app = make_app(driver=gate.driver, control=make_control(recorded))
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await pilot.press("e")
            for character in "abort it":
                await pilot.press(character)
            await pilot.pause()
            self.assertEqual(app._cockpit_mode, "composer")  # T9
            self.assertEqual(recorded, [])
            gate.release.set()

    async def test_target_sticky_across_reopen(self):
        gate = _Gate()
        app = make_app(driver=gate.driver)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await pilot.press("e")
            await pilot.press("tab")          # → SUBMISSION
            await pilot.pause()
            self.assertEqual(app._cockpit_revise_target, "SUBMISSION")
            await pilot.press("escape")       # 空缓冲 → command（阶梯末段）
            await pilot.pause()
            self.assertEqual(app._cockpit_mode, "command")
            await pilot.press("e")            # 重开：target 粘滞（W7）
            await pilot.pause()
            self.assertEqual(app._cockpit_revise_target, "SUBMISSION")
            gate.release.set()


class TerminalCollapsePilotTests(unittest.IsolatedAsyncioTestCase):
    """T1 终态迁移 + T7：终态塌缩清缓冲；终态后 E/Enter 诚实
    REJECTED 回执（session 终态门语义，UI 零特判）。"""

    async def _await_condition(self, pilot, predicate, limit=400):
        for _ in range(limit):
            if predicate():
                return True
            await pilot.pause()
        return predicate()

    async def test_terminal_collapses_composer_and_clears_buffer(self):
        gate = _Gate()
        session = _LiveSession()

        def driver():
            gate.release.wait(10)
            outcome = _outcome(RunStatus.COMPLETED)
            session.last_outcome = outcome
            session.terminal = RunStatus.COMPLETED
            return outcome

        app = make_app(driver=driver, session=session,
                       control=make_control([]))
        async with app.run_test(size=(100, 30)) as pilot:
            for character in "hel":
                await pilot.press(character)
            await pilot.pause()
            self.assertEqual(app._cockpit_mode, "composer")
            gate.release.set()
            ok = await self._await_condition(
                pilot, lambda: app._cockpit_mode == "command")
            self.assertTrue(ok, "terminal collapse never happened")
            self.assertEqual(app._cockpit_revise_text, "")
            self.assertEqual(app._dock_input_line(),
                             "type to steer · enter submits")
            gate.release.set()

    async def test_post_terminal_submit_is_honest_rejection(self):
        gate = _Gate()
        session = _LiveSession()
        session.terminal = RunStatus.COMPLETED
        session.last_outcome = _outcome(RunStatus.COMPLETED)
        recorded = []
        app = make_app(
            driver=gate.driver, session=session,
            control=make_control(
                recorded,
                receipt_result("REJECTED", reason="ALREADY_TERMINAL")))
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await pilot.press("e")            # 终态后 E 仍可开
            await pilot.pause()
            self.assertEqual(app._cockpit_mode, "composer")
            for character in "late":
                await pilot.press(character)
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(
                recorded, [("REVISE", "late", "NEXT_INVOCATION")])
            receipt = app._dock_receipt_line()  # T11：REJECTED 含 reason
            self.assertIn("REJECTED", receipt)
            self.assertIn("ALREADY_TERMINAL", receipt)
            self.assertEqual(app._cockpit_revise_text, "")
            self.assertEqual(app._cockpit_mode, "composer")  # uniform 规则
            gate.release.set()


class ParkedSteeringPilotTests(unittest.IsolatedAsyncioTestCase):
    """T6：PARKED 停驻下 pause-and-steer——REVISE 排队提交 + R 直达。"""

    async def _await_condition(self, pilot, predicate, limit=400):
        for _ in range(limit):
            if predicate():
                return True
            await pilot.pause()
        return predicate()

    async def test_parked_typing_revise_then_r_resumes(self):
        session = _LiveSession()
        drive, calls = _park_resume_abort_driver(session)
        recorded = []
        app = make_app(driver=drive, session=session,
                       control=make_control(recorded))
        async with app.run_test(size=(100, 30)) as pilot:
            ok = await self._await_condition(
                pilot,
                lambda: getattr(app.outcome, "status", None)
                is RunStatus.PARKED)
            self.assertTrue(ok, "first segment never parked")
            self.assertEqual(app._cockpit_mode, "command")
            for character in "fix":       # PARKED 下 auto-ingress 同构
                await pilot.press(character)
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(
                recorded, [("REVISE", "fix", "NEXT_INVOCATION")])
            self.assertEqual(app._cockpit_mode, "composer")
            await pilot.press("escape")   # 空缓冲 → command（阶梯末段）
            await pilot.pause()
            self.assertEqual(app._cockpit_mode, "command")
            await pilot.press("r")        # R 直达恢复（缓冲空时）
            await pilot.pause()
            self.assertEqual(recorded[1], ("RESUME", None, None))
            ok = await self._await_condition(
                pilot, lambda: session.terminal is RunStatus.ABORTED)
            self.assertTrue(ok, "second segment never terminal")


class TraceTopGuardPilotTests(unittest.IsolatedAsyncioTestCase):
    """T8/T13：Trace 在顶输入抑制、e no-op（W6）、冒泡命令照旧、
    composer 呈现态跨往返保持。"""

    async def test_typing_suppressed_while_trace_on_top(self):
        gate = _Gate()
        recorded = []
        app = make_app(driver=gate.driver, control=make_control(recorded))
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await pilot.press("t")
            await pilot.pause()
            self.assertIsInstance(app.screen, cockpit_tui.TraceScreen)
            for key in ("z", "x", "1"):
                await pilot.press(key)
            await pilot.pause()
            # W6：绝不向被遮蔽的 dock 打字
            self.assertEqual(app._cockpit_mode, "command")
            self.assertEqual(app._cockpit_revise_text, "")
            self.assertEqual(recorded, [])
            # 既有 R2 陷阱：trace escape 同时被 App.on_key 消费落
            # confirm——n 出（既有测试同款处理）
            await pilot.press("escape")
            await pilot.press("n")
            await pilot.pause()
            await pilot.press("z")        # pop 回主屏后输入面恢复
            await pilot.pause()
            self.assertEqual(app._cockpit_mode, "composer")
            self.assertEqual(app._cockpit_revise_text, "z")
            gate.release.set()

    async def test_e_noop_while_trace_on_top(self):
        gate = _Gate()
        app = make_app(driver=gate.driver)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await pilot.press("t")
            await pilot.pause()
            await pilot.press("e")
            await pilot.pause()
            self.assertEqual(app._cockpit_mode, "command")  # 不开在屏后
            await pilot.press("escape")
            await pilot.press("n")      # R2 陷阱：escape 双消费落 confirm
            await pilot.pause()
            await pilot.press("e")
            await pilot.pause()
            self.assertEqual(app._cockpit_mode, "composer")
            gate.release.set()

    async def test_bubbled_p_still_dispatches_at_trace_top(self):
        gate = _Gate()
        recorded = []
        app = make_app(driver=gate.driver, control=make_control(recorded))
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await pilot.press("t")
            await pilot.pause()
            await pilot.press("p")       # 既有冒泡行为（冻结保持）
            await pilot.pause()
            self.assertEqual(recorded, [("PAUSE", None, None)])
            await pilot.press("escape")
            gate.release.set()

    async def test_input_face_intact_after_trace_roundtrip(self):
        # T13（可达路径版）：COMMAND 开 Trace（composer 内 t 是文本、
        # 结构性无法开屏）→ 往返 → 输入面完整恢复。composer 呈现态
        # 的跨屏保持无键盘可达路径，不构成契约。
        gate = _Gate()
        recorded = []
        app = make_app(driver=gate.driver, control=make_control(recorded))
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await pilot.press("t")
            await pilot.pause()
            self.assertIsInstance(app.screen, cockpit_tui.TraceScreen)
            await pilot.press("escape")
            await pilot.press("n")      # R2 陷阱：escape 双消费落 confirm
            await pilot.pause()
            self.assertEqual(app._cockpit_mode, "command")
            # 往返后输入面完整：ingress + target 默认值 + dock 三行
            await pilot.press("z")
            await pilot.pause()
            self.assertEqual(app._cockpit_mode, "composer")
            self.assertEqual(app._cockpit_revise_text, "z")
            self.assertEqual(app._cockpit_revise_target,
                             "NEXT_INVOCATION")
            self.assertEqual(
                app._dock_input_line(), "revise [NEXT_INVOCATION] z|")
            gate.release.set()


class SteeringTruthIsolationTests(unittest.IsolatedAsyncioTestCase):
    """T12/T17：六阶区分 + 零合成执行事实——真 ControlJournal +
    真 ControlBoundary 见证：键击零事实零事件；Enter 恰 1 条
    REVISE_REQUESTED；receipt 只呈现 accepted（无 applied 宣称；
    applied/honored 只经事实面，本测试零 REVISION_APPLIED 落账）。"""

    async def test_typing_and_submit_never_fabricate_truth(self):
        from control_boundary import (
            ControlBoundary, ControlCommand, ControlCommandType,
            RevisionPayload, RevisionTarget)
        from control_journal import ControlJournal

        gate = _Gate()
        journal = ControlJournal()
        boundary = ControlBoundary(journal, "exec-t12")
        events_seen = []
        counter = [0]

        def control(kind, text=None, target=None):
            counter[0] += 1
            payload = None
            expected = None
            if kind == "REVISE":
                payload = RevisionPayload(
                    target=RevisionTarget.NEXT_INVOCATION, text=text)
                expected = boundary.execution_version
            return boundary.submit(ControlCommand(
                command_id=f"ui-{counter[0]}", execution_id="exec-t12",
                command=ControlCommandType(kind), payload=payload,
                expected_version=expected))

        app = make_app(driver=gate.driver,
                       events=lambda: tuple(events_seen),
                       facts=journal.snapshot,
                       control=control)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            base_facts = len(journal.snapshot())
            for character in "hello":   # typed：零事实、零事件
                await pilot.press(character)
            await pilot.pause()
            self.assertEqual(len(journal.snapshot()), base_facts)
            self.assertEqual(events_seen, [])
            await pilot.press("enter")  # submitted + accepted
            await pilot.pause()
            facts = journal.snapshot()
            self.assertEqual(len(facts), base_facts + 1)
            kind = getattr(facts[-1].fact_type, "value",
                           facts[-1].fact_type)
            self.assertEqual(kind, "REVISE_REQUESTED")
            self.assertEqual(events_seen, [])  # T17：零合成观察
            receipt = app._dock_receipt_line()
            self.assertIn("ACCEPTED", receipt)
            self.assertNotIn("APPLIED", receipt.upper())  # 主屏零 applied
            gate.release.set()


class TypingProjectionGuardTests(unittest.IsolatedAsyncioTestCase):
    """T16（W4）：键击/Enter 路径零 build_projection（CU-PERF-1
    延伸——typing 路径此前每键击全量 _refresh）。interval 被
    no-op 化以保证确定性（不依赖计时窗口）。"""

    async def test_typing_and_enter_never_full_projection(self):
        gate = _Gate()
        app = make_app(driver=gate.driver, control=make_control([]))
        counter = []
        original = cockpit_tui.build_projection

        def counting(values):
            counter.append(len(counter))
            return original(values)

        with mock.patch.object(cockpit_tui.CockpitApp, "set_interval",
                               lambda self, *args, **kwargs: None):
            async with app.run_test(size=(100, 30)) as pilot:
                with mock.patch.object(cockpit_tui, "build_projection",
                                       counting):
                    await pilot.pause()          # mount 初始投影
                    app._refresh()               # settle
                    settled = len(counter)
                    for character in "hello":
                        await pilot.press(character)
                    await pilot.press("enter")
                    await pilot.pause()
                    self.assertEqual(len(counter), settled)
        gate.release.set()


class ComposerTruncationPilotTests(unittest.IsolatedAsyncioTestCase):
    """W9：composer 行超宽截断——尾标 ... 诚实溢出、尾随光标 |
    恒在场、行宽恒 ≤ 终端宽（绝不横滚）。"""

    async def test_long_buffer_truncates_to_width(self):
        gate = _Gate()
        app = make_app(driver=gate.driver, control=make_control([]))
        async with app.run_test(size=(40, 30)) as pilot:
            await pilot.pause()
            await pilot.press("e")
            for _ in range(60):
                await pilot.press("x")
            await pilot.pause()
            line = app._dock_input_line()
            self.assertLessEqual(len(line), 40)
            self.assertTrue(line.endswith("|"))
            self.assertIn("...", line)
            gate.release.set()


if __name__ == "__main__":
    unittest.main()
