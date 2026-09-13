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
from unittest import mock

import pytest

pytest.importorskip("textual")

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from execution_observation import ExecutionEvent, ExecutionEventType

import cockpit_projection
import cockpit_tui


class _Gate:
    """阻塞门控 driver：保持 App 存活到测试断言完成。"""

    def __init__(self):
        self.release = threading.Event()

    def driver(self):
        self.release.wait(10)
        return "gated-outcome"


def _event(seq, kind, stage="architect", runtime="rt-0"):
    return ExecutionEvent(
        event_type=kind, sequence=seq, task_id="t", correlation_id="e",
        stage=stage, runtime_id=runtime, status="SUCCESS", reason="R")


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


if __name__ == "__main__":
    unittest.main()
