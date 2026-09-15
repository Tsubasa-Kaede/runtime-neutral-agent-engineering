"""CU-PERF-1: performance probe (opt-in, NOT a CI gate).

Environment variable COCKPIT_PERF_PROBE=1 enables the timed probes
below; without it every test is skipped (CI stays deterministic —
timing is recorded, not asserted; equivalence/correctness gates live
in the projection/rendering suites).

Before/After protocol: run with the env var on the baseline commit
and again after the change; record both outputs in the CU report.
"""
import os
import sys
import time
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
