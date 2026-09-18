"""CU-PERF-1: performance probe (opt-in, NOT a CI gate).

Environment variable COCKPIT_PERF_PROBE=1 enables the timed probes
below; without it every test is skipped (CI stays deterministic —
timing is recorded, not asserted; equivalence/correctness gates live
in the projection/rendering suites).

Before/After protocol: run with the env var on the baseline commit
and again after the change; record both outputs in the CU report.

UX2-R3 addition: UX2R3PerfObservationTests (P1-P8) — observational
measurements of the interactive paths (typing/submit/STEER/log/resize/
trace/multiline/slash-hint). Same protocol: printed, never asserted;
numbers are only comparable against the same workload + same probe +
same measurement method (they are NOT comparable with the pure
projection probes above, and never with CU-PERF-1's figures).
"""
import os
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from cockpit_projection import (  # noqa: E402
    AgentSlotView,
    ProjectionInputs,
    build_projection,
    display_width,
)
from console_observation import format_event_line  # noqa: E402
from execution_observation import (  # noqa: E402
    ExecutionEvent,
    ExecutionEventType,
)

PROBE_ENABLED = os.environ.get("COCKPIT_PERF_PROBE") == "1"
SKIP_REASON = "set COCKPIT_PERF_PROBE=1 to run performance probes"


def _event(i, stage, runtime):
    kind = (ExecutionEventType.STAGE_STARTED if i % 4 == 0
            else ExecutionEventType.INVOCATION_STARTED if i % 4 == 1
            else ExecutionEventType.INVOCATION_FINISHED if i % 4 == 2
            else ExecutionEventType.HANDOFF)
    return ExecutionEvent(
        event_type=kind, sequence=i, task_id="t", correlation_id="e",
        stage=stage, runtime_id=runtime,
        status="SUCCESS", reason="R", duration_ms=100 + i)


def _events(n):
    return tuple(_event(i, "architect" if i % 2 else "coder",
                        "rt-0" if i % 2 else "rt-1")
                 for i in range(n))


_SLOTS = (
    AgentSlotView(stage="step-0-architect", role="architect",
                  runtime_id="rt-0", provider="p"),
    AgentSlotView(stage="step-1-coder", role="coder",
                  runtime_id="rt-1", provider="p"),
)


def _values(events, *, expanded=None, detail_max=None,
            result_max=None, include_trace=True):
    return ProjectionInputs(
        task="demo task for probe with CJK 任务文本运行中",
        slots=_SLOTS, events=events, facts=(), usage_records=(),
        terminal=None, run_state=None, last_outcome=None,
        width=100, ascii_only=False, pulse=True, reveal_seqs=(),
        locale="zh", expanded_stage=expanded,
        detail_max_lines=detail_max, result_max_lines=result_max,
        include_trace=include_trace)


def _ms_per_call(fn, count):
    fn()  # warm-up
    start = time.perf_counter()
    for _ in range(count):
        fn()
    return (time.perf_counter() - start) / count * 1000.0


def _median_ms(samples):
    ordered = sorted(samples)
    n = len(ordered)
    mid = n // 2
    if n % 2:
        return ordered[mid] * 1000.0
    return (ordered[mid - 1] + ordered[mid]) / 2 * 1000.0


class PerfProbeTests(unittest.TestCase):
    """Timed probes — printed, never asserted (recording protocol)."""

    def setUp(self):
        if not PROBE_ENABLED:
            self.skipTest(SKIP_REASON)

    def _report(self, label, ms):
        print(f"[probe] {label}: {ms:.3f} ms")

    def test_projection_sweep(self):
        # 真实量级（4-agent 单任务全量 ≈ 12 events）
        self._report("build_projection 12ev no-expand",
                     _ms_per_call(lambda: build_projection(
                         _values(_events(12), include_trace=False)), 200))
        self._report("build_projection 12ev expanded",
                     _ms_per_call(lambda: build_projection(
                         _values(_events(12), expanded="step-0-architect",
                                 include_trace=False)), 200))
        self._report("build_projection 12ev expanded include_trace",
                     _ms_per_call(lambda: build_projection(
                         _values(_events(12), expanded="step-0-architect")), 200))
        # 病理量级
        for n in (500, 2000):
            self._report(
                f"build_projection {n}ev no-expand (no-trace)",
                _ms_per_call(lambda: build_projection(
                    _values(_events(n), include_trace=False)), 20))
            self._report(
                f"build_projection {n}ev expanded (no-trace)",
                _ms_per_call(lambda: build_projection(
                    _values(_events(n), expanded="step-0-architect",
                            include_trace=False)), 20))
            self._report(
                f"build_projection {n}ev expanded full (legacy parity)",
                _ms_per_call(lambda: build_projection(
                    _values(_events(n), expanded="step-0-architect")), 20))
            self._report(
                f"a4-narrowed rebuild {n}ev (detail=5 result=3)",
                _ms_per_call(lambda: build_projection(
                    _values(_events(n), expanded="step-0-architect",
                            detail_max=5, result_max=3)), 20))

    def test_trace_split(self):
        events = _events(2000)
        self._report(
            "trace_observation_lines 2000ev",
            _ms_per_call(lambda: tuple(
                format_event_line(e).rstrip("\n") for e in events), 20))
        from cockpit_projection import (  # noqa: F401
            trace_observation_lines,
        )
        self._report(
            "trace_observation_lines() 2000ev",
            _ms_per_call(lambda: trace_observation_lines(events), 20))

    def test_display_width(self):
        ascii_line = ("[123] INVOCATION_FINISHED stage=architect "
                      "runtime=rt-0 status=SUCCESS duration_ms=1234 "
                      "reason=R")
        cjk_line = "任务 运行中 已完成 等待中 暂无活动 阶段已开始 交接"
        ms_ascii = _ms_per_call(lambda: display_width(ascii_line), 2000)
        ms_cjk = _ms_per_call(lambda: display_width(cjk_line), 2000)
        print(f"[probe] display_width ASCII {len(ascii_line)}ch: "
              f"{ms_ascii:.3f} ms/call = "
              f"{ms_ascii * 1e6 / len(ascii_line):.0f} ns/char")
        print(f"[probe] display_width CJK {len(cjk_line)}ch: "
              f"{ms_cjk:.3f} ms/call = "
              f"{ms_cjk * 1e6 / len(cjk_line):.0f} ns/char")


class UX2R3PerfObservationTests(unittest.IsolatedAsyncioTestCase):
    """UX2-R3 P1-P8 observational probes（print-only，零断言门槛）。

    方法学：pilot.press/pause 计时含事件泵开销（wall-clock）；中位数
    由 N 样本排序取中；interval 一律 no-op 化保证确定性；build_
    projection 计数为 CU-PERF-1 同款差分法。对照基线=同 probe 复跑
    （首轮即基线记录）；不与本文件纯投影探针及 CU-PERF-1 数字比较。
    """

    def setUp(self):
        if not PROBE_ENABLED:
            self.skipTest(SKIP_REASON)

    @staticmethod
    def _report(label, value, unit="ms"):
        print(f"[probe-R3] {label}: {value:.3f} {unit}")

    def _make_app(self):
        import cockpit_tui

        recorded = []
        return cockpit_tui.CockpitApp(
            driver=lambda: "probe-outcome",
            task="probe task",
            plan=(("step-0-architect", "architect", "rt-0", "prov-a"),
                  ("step-1-coder", "coder", "rt-1", "prov-b")),
            events=lambda: (), facts=lambda: (), usage=lambda: (),
            session=SimpleNamespace(run_state=None, terminal=None,
                                    last_outcome=None),
            control=lambda kind, text=None, target=None: (
                recorded.append((kind, text, target)),
                SimpleNamespace(
                    status=SimpleNamespace(value="ACCEPTED"),
                    reason=None, execution_version=1,
                    command_id=f"ui-{len(recorded)}",
                    execution_id="e"))[-1]), recorded

    def _counting(self):
        import cockpit_tui
        counter = []
        original = cockpit_tui.build_projection

        def counting(values):
            counter.append(len(counter))
            return original(values)
        patcher = mock.patch.object(cockpit_tui, "build_projection",
                                    counting)
        return counter, patcher

    async def _mounted(self, size=(100, 30)):
        app, recorded = self._make_app()
        interval_patch = mock.patch.object(
            type(app), "set_interval",
            lambda self, *args, **kwargs: None)
        return app, recorded, interval_patch

    async def test_p1_composer_typing(self):
        # P1：键击（含事件泵）中位 + 完整投影增量（预期 0——AC3 面
        # 已由渲染套件断言，此处仅记录）
        counter, patcher = self._counting()
        app, _recorded, interval_patch = await self._mounted()
        with interval_patch, patcher:
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause()
                app._refresh()
                settled = len(counter)
                samples = []
                for _ in range(50):
                    t0 = time.perf_counter()
                    await pilot.press("x")
                    t1 = time.perf_counter()
                    samples.append(t1 - t0)
                await pilot.pause()
                self._report("P1 typing press median (50x 'x', "
                             "incl. pilot pump)", _median_ms(samples))
                self._report("P1 build_projection delta",
                             len(counter) - settled, "count")

    async def test_p2_composer_submit(self):
        # P2：提交（enter+pause 至回执落 Log）中位 ×10
        app, _recorded, interval_patch = await self._mounted()
        with interval_patch:
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause()
                composer = app.query_one("#composer")
                composer.text = "hello"
                await pilot.pause()
                samples = []
                for i in range(10):
                    composer.text = f"msg-{i}"
                    await pilot.pause()
                    t0 = time.perf_counter()
                    await pilot.press("enter")
                    await pilot.pause()
                    t1 = time.perf_counter()
                    samples.append(t1 - t0)
                self._report("P2 submit (enter+pause→receipt) median "
                             "(10x)", _median_ms(samples))

    async def test_p3_continuous_steer(self):
        # P3：10 连 STEER 提交——总时长/单条中位/Log 行数
        app, recorded, interval_patch = await self._mounted()
        with interval_patch:
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause()
                composer = app.query_one("#composer")
                samples = []
                t_all = time.perf_counter()
                for i in range(10):
                    composer.text = f"steer-{i}"
                    await pilot.pause()
                    t0 = time.perf_counter()
                    await pilot.press("enter")
                    await pilot.pause()
                    samples.append(time.perf_counter() - t0)
                total = time.perf_counter() - t_all
                self.assertEqual(len(recorded), 10)
                self._report("P3 continuous STEER total (10x)",
                             total * 1000.0)
                self._report("P3 per-submit median",
                             _median_ms(samples))
                self._report("P3 log lines",
                             len(app.log_text.splitlines()), "lines")

    async def test_p4_log_growth(self):
        # P4：Log append 至 ring 上限 1000——首/末 100 条中位对比
        # （O(N²) 观察面：log_text join 每次全量，仅 observational）
        app, _recorded, interval_patch = await self._mounted()
        with interval_patch:
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause()
                samples = []
                t0 = time.perf_counter()
                for i in range(1000):
                    s = time.perf_counter()
                    app._log_append(f"probe line {i} " + "x" * 20)
                    samples.append(time.perf_counter() - s)
                total = time.perf_counter() - t0
                self._report("P4 log append 1000 total", total * 1000.0)
                self._report("P4 append median (first 100)",
                             _median_ms(samples[:100]))
                self._report("P4 append median (last 100)",
                             _median_ms(samples[-100:]))
                self._report("P4 ring cap",
                             len(app._cockpit_log_lines), "lines")

    async def test_p5_resize(self):
        # P5：宽度交替 40↔160 驱动重投影 ×50——单次中位 + 投影计数
        counter, patcher = self._counting()
        app, _recorded, interval_patch = await self._mounted()
        with interval_patch, patcher:
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause()
                app._refresh()
                settled = len(counter)
                samples = []
                for i in range(50):
                    width = 40 if i % 2 else 160
                    with mock.patch.object(
                            type(app), "size",
                            new_callable=mock.PropertyMock) as size:
                        size.return_value = SimpleNamespace(
                            width=width, height=30)
                        s = time.perf_counter()
                        app._refresh()
                        samples.append(time.perf_counter() - s)
                self._report("P5 resize-driven refresh median (50x)",
                             _median_ms(samples))
                self._report("P5 projection count",
                             len(counter) - settled, "count")

    async def test_p6_trace_enter_exit(self):
        # P6：Trace 往返 ×10（t→pause→escape→pause）中位 + 主屏投影
        # 增量（TraceScreen 直取 trace_*——预期 0）
        counter, patcher = self._counting()
        app, _recorded, interval_patch = await self._mounted()
        with interval_patch, patcher:
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause()
                app._refresh()
                settled = len(counter)
                samples = []
                for _ in range(10):
                    t0 = time.perf_counter()
                    await pilot.press("t")
                    await pilot.pause()
                    await pilot.press("escape")
                    await pilot.pause()
                    samples.append(time.perf_counter() - t0)
                self._report("P6 trace enter/exit round median (10x)",
                             _median_ms(samples))
                self._report("P6 main-screen projection delta",
                             len(counter) - settled, "count")

    async def test_p7_multiline_composer(self):
        # P7：composer 1/5/50 行三档——_refresh 中位（高度 clamp 成本）
        app, _recorded, interval_patch = await self._mounted()
        with interval_patch:
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause()
                composer = app.query_one("#composer")
                for rows in (1, 5, 50):
                    composer.text = "\n".join(f"row {i}"
                                              for i in range(rows))
                    await pilot.pause()
                    samples = []
                    for _ in range(20):
                        s = time.perf_counter()
                        app._refresh()
                        samples.append(time.perf_counter() - s)
                    self._report(
                        f"P7 refresh median composer={rows} rows",
                        _median_ms(samples))

    async def test_p8_slash_hint(self):
        # P8：slash 键击（/、p、a、u——含收窄至 unknown hint 路径）
        # 中位 + 投影增量（局部 dock 刷新，预期 0）
        counter, patcher = self._counting()
        app, _recorded, interval_patch = await self._mounted()
        with interval_patch, patcher:
            async with app.run_test(size=(120, 30)) as pilot:
                await pilot.pause()
                app._refresh()
                settled = len(counter)
                samples = []
                for key in ("/", "p", "a", "u"):
                    t0 = time.perf_counter()
                    await pilot.press(key)
                    t1 = time.perf_counter()
                    samples.append(t1 - t0)
                    await pilot.pause()
                self._report("P8 slash keypress median (/pau, "
                             "incl. pump)", _median_ms(samples))
                self._report("P8 build_projection delta",
                             len(counter) - settled, "count")


if __name__ == "__main__":
    unittest.main()
