"""R7-D4: first observation consumer — stateless console projection (offline).

RED-first：ConsoleObservationSink 尚不存在时本文件必然失败（ImportError）。

锁定授权证据（R7-D4 Boundary Design 裁决 Option A）：
1. 七种 EventType 全部渲染为恰好一行
2. sequence 原值保留（0 基 execution-scoped，绝不重编号）
3. 同一事件 -> 逐字节相同输出（确定性，零时间/随机/UUID）
4. duration_ms=None 时不伪造 duration（省略字段，诚实未知）
5. reason 逐字透传（不解析、不截断、不建第二套 secret policy）
6. sink 零跨事件状态（无 buffer、无聚合、无 finalize）
7. writer 可注入（CLI 绑 sys.stderr，测试绑替身）
8. writer 抛 OSError 时异常绝不进入 execution（隔离唯一属 D2 emit 层）
9. --observe 默认 OFF（CLI 行为与当前版本逐字一致）
10. --observe 时 observation 只进 stderr
11. stdout 仍恰好一份原有 JSON（零 observation 污染、schema 不变）
12. 完整四阶段生命周期经 console sink 可观察（17 行）
13. 无重复事件（sequence 0..16 各恰一次）
14. 源码扫描：无 runtime 名分支、无 time/random/uuid、零引擎/ledger/
    packet/adapter import
15. 受保护 untracked 文件仍为 untracked（git 视角未被动过）

全部离线；REAL=0；不触 runtime、不读环境、不走网络。
"""
import contextlib
import io
import json
import re
import subprocess
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from console_observation import ConsoleObservationSink, format_event_line
from execution_observation import ExecutionEvent, ExecutionEventType
from mode_gate import Mode

from tests.test_execution_observation_wiring import compose_facade

TASK_COMPLEX = "redesign architecture across modules"

# 四阶段成功的 17 行投影（type, stage）逐位对齐 D2 已证明的事件顺序。
EXPECTED_LINES = [
    ("DECISION", "dual"),
    ("STAGE_STARTED", "architect"),
    ("INVOCATION_STARTED", "architect"),
    ("INVOCATION_FINISHED", "architect"),
    ("HANDOFF", "architect"),
    ("STAGE_STARTED", "coder"),
    ("INVOCATION_STARTED", "coder"),
    ("INVOCATION_FINISHED", "coder"),
    ("HANDOFF", "coder"),
    ("STAGE_STARTED", "tester"),
    ("INVOCATION_STARTED", "tester"),
    ("INVOCATION_FINISHED", "tester"),
    ("HANDOFF", "tester"),
    ("STAGE_STARTED", "reviewer"),
    ("INVOCATION_STARTED", "reviewer"),
    ("INVOCATION_FINISHED", "reviewer"),
    ("TERMINAL", "FOUR_STAGE"),
]

_LINE_SHAPE = re.compile(
    r"^\[(\d+)\] ([A-Z_]+) stage=(\S+) runtime=(\S+) status=(\S+)"
    r"(?: duration_ms=(\d+))? reason=(.+)$")


def make_event(**overrides):
    fields = dict(
        event_type=ExecutionEventType.DECISION, sequence=0, task_id="T1",
        correlation_id="corr-1", stage="dual", runtime_id="rt-x",
        status="AUTO", reason="MODE_AUTO/COVERAGE=ARCHITECT_CODER")
    fields.update(overrides)
    return ExecutionEvent(**fields)


class CaptureWriter:
    def __init__(self):
        self.lines = []

    def write(self, line):
        self.lines.append(line)


class RaisingWriter:
    def write(self, line):
        raise OSError("stream closed")


# ---------------------------------------------------------------------------
# 1-6: 单行格式契约（纯函数 + 无状态 sink）
# ---------------------------------------------------------------------------


class LineFormatTests(unittest.TestCase):

    def test_all_seven_event_types_render_single_line(self):
        for index, event_type in enumerate(ExecutionEventType):
            writer = CaptureWriter()
            ConsoleObservationSink(writer).on_event(
                make_event(event_type=event_type, sequence=index))
            self.assertEqual(len(writer.lines), 1, event_type.value)
            line = writer.lines[0]
            self.assertTrue(line.endswith("\n"), event_type.value)
            self.assertIn(event_type.value, line, event_type.value)

    def test_line_shape_is_stable_key_order(self):
        line = format_event_line(make_event())
        match = _LINE_SHAPE.match(line.rstrip("\n"))
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), "0")
        self.assertEqual(match.group(2), "DECISION")
        self.assertEqual(match.group(3), "dual")
        self.assertEqual(match.group(4), "rt-x")
        self.assertEqual(match.group(5), "AUTO")
        self.assertIsNone(match.group(6))
        self.assertEqual(match.group(7), "MODE_AUTO/COVERAGE=ARCHITECT_CODER")

    def test_sequence_printed_verbatim_never_renumbered(self):
        self.assertTrue(format_event_line(make_event(sequence=0))
                        .startswith("[0] "))
        self.assertTrue(format_event_line(make_event(sequence=12))
                        .startswith("[12] "))

    def test_same_event_deterministic_identical_output(self):
        event = make_event(event_type=ExecutionEventType.INVOCATION_FINISHED,
                           sequence=7, stage="coder", status="SUCCESS",
                           duration_ms=10)
        first_writer, second_writer = CaptureWriter(), CaptureWriter()
        ConsoleObservationSink(first_writer).on_event(event)
        second_sink = ConsoleObservationSink(second_writer)
        second_sink.on_event(event)
        second_sink.on_event(event)
        self.assertEqual(first_writer.lines[0], second_writer.lines[0])
        self.assertEqual(second_writer.lines[0], second_writer.lines[1])
        self.assertEqual(format_event_line(event), first_writer.lines[0])

    def test_duration_none_omitted_not_fabricated(self):
        line = format_event_line(make_event(duration_ms=None))
        self.assertNotIn("duration_ms", line)

    def test_duration_int_included_verbatim(self):
        line = format_event_line(
            make_event(duration_ms=42)).rstrip("\n")
        self.assertIn(" duration_ms=42 ", line)
        self.assertNotIn("duration_ms=0", line)

    def test_reason_verbatim_passthrough(self):
        reason = "FALLBACK_AFTER_CODER_PACKET_INVALID/ROLE_ASSIGNMENT=POLICY_CONVERGED"
        line = format_event_line(make_event(reason=reason))
        self.assertIn(f"reason={reason}", line)

    def test_no_cross_event_state(self):
        writer = CaptureWriter()
        sink = ConsoleObservationSink(writer)
        first = make_event(sequence=0, reason="FIRST")
        second = make_event(sequence=1, reason="SECOND")
        sink.on_event(first)
        sink.on_event(second)
        alone = format_event_line(first)
        # 第一行不受后续事件影响；两行独立、无共享状态。
        self.assertEqual(writer.lines[0], alone)
        self.assertEqual(writer.lines[1], format_event_line(second))
        self.assertNotIn("SECOND", writer.lines[0])


# ---------------------------------------------------------------------------
# 7-8: writer 注入与故障边界（隔离责任唯一属 D2 emit 层）
# ---------------------------------------------------------------------------


class WriterInjectionTests(unittest.TestCase):

    def test_writer_is_injectable_and_independent(self):
        left, right = CaptureWriter(), CaptureWriter()
        event = make_event()
        ConsoleObservationSink(left).on_event(event)
        ConsoleObservationSink(right).on_event(event)
        self.assertEqual(left.lines, right.lines)
        self.assertEqual(len(left.lines), 1)

    def test_sink_does_not_swallow_writer_oserror(self):
        # sink 不吞、不重试 —— 隔离责任在 D2 的 emit 隔离层（下一个测试
        # 经真实 facade 证明执行不受影响）。
        sink = ConsoleObservationSink(RaisingWriter())
        with self.assertRaises(OSError):
            sink.on_event(make_event())


class ExecutionIsolationTests(unittest.TestCase):
    """writer 故障经真实生产链路 -> execution 全不变（D2 隔离兜底）。"""

    def test_writer_oserror_through_facade_execution_unaffected(self):
        control_facade, control_usage, _ = compose_facade()
        control = control_facade.run(task_id="T1", task=TASK_COMPLEX,
                                     prompt="p", mode=Mode.ON)
        failing_facade, failing_usage, _ = compose_facade()
        result = failing_facade.run(
            task_id="T1", task=TASK_COMPLEX, prompt="p", mode=Mode.ON,
            observation_sink=ConsoleObservationSink(RaisingWriter()))
        self.assertEqual(result.status, control.status)
        self.assertEqual(result.path, control.path)
        self.assertEqual(result.stages, control.stages)
        self.assertEqual(result.failure_category, control.failure_category)
        self.assertEqual(failing_usage.total_agent_calls,
                         control_usage.total_agent_calls)


# ---------------------------------------------------------------------------
# 12-13: 完整四阶段生命周期经 console sink 可观察、无重复
# ---------------------------------------------------------------------------


class ConsoleLifecycleTests(unittest.TestCase):

    @staticmethod
    def _run_with_console_sink():
        facade, _usage, _adapters = compose_facade()
        writer = CaptureWriter()
        facade.run(task_id="T1", task=TASK_COMPLEX, prompt="p", mode=Mode.ON,
                   observation_sink=ConsoleObservationSink(writer))
        return writer.lines

    def test_full_four_stage_lifecycle_seventeen_lines(self):
        lines = self._run_with_console_sink()
        self.assertEqual(len(lines), 17)
        parsed = []
        for line in lines:
            match = _LINE_SHAPE.match(line.rstrip("\n"))
            self.assertIsNotNone(match, line)
            parsed.append((int(match.group(1)), match.group(2),
                           match.group(3)))
        self.assertEqual([(seq, ) for seq, _t, _s in parsed],
                         [(seq, ) for seq in range(17)])
        self.assertEqual([(t, s) for _seq, t, s in parsed], EXPECTED_LINES)

    def test_no_duplicate_events(self):
        lines = self._run_with_console_sink()
        sequences = [int(_LINE_SHAPE.match(l.rstrip("\n")).group(1))
                     for l in lines]
        self.assertEqual(sorted(sequences), list(range(17)))
        self.assertEqual(len(set(sequences)), 17)


# ---------------------------------------------------------------------------
# 9-11: CLI --observe 开关与 stdout/stderr 边界
# ---------------------------------------------------------------------------


class RecordingFacade:
    """Stub facade：记录 run() 全部 kwargs，绝不 invoke adapter。"""

    def __init__(self, result):
        self.result = result
        self.calls = []

    def run(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


def _stub_result():
    from production_facade import FacadeResult
    return FacadeResult(
        status="SUCCESS", mode="AUTO", path="FOUR_STAGE", task_id="T1",
        provenance="OFFLINE", stages=("architect", "coder", "tester",
                                      "reviewer"), failure_category="",
        safe_summary={"task_id": "T1", "provenance": "OFFLINE",
                      "stage_counts": {"architect": 1, "coder": 1,
                                       "tester": 1, "reviewer": 1}})


class CliObserveFlagTests(unittest.TestCase):

    def test_observe_default_is_off(self):
        from cli import build_parser
        args = build_parser().parse_args(["run", "x"])
        self.assertFalse(args.observe)

    def test_observe_flag_parses_true(self):
        from cli import build_parser
        args = build_parser().parse_args(["run", "--observe", "x"])
        self.assertTrue(args.observe)

    def test_run_cli_default_passes_none_sink(self):
        from cli import run_cli
        facade = RecordingFacade(_stub_result())
        run_cli(facade, ["run", "x"])
        self.assertIsNone(facade.calls[0].get("observation_sink"))

    def test_run_cli_observe_passes_console_sink_instance(self):
        import cli
        facade = RecordingFacade(_stub_result())
        cli.run_cli(facade, ["run", "--observe", "x"])
        sink = facade.calls[0].get("observation_sink")
        self.assertIsInstance(sink, ConsoleObservationSink)
        # stub facade 不发射事件 —— stderr 实流由下方端到端测试证明。

    def test_cli_observe_streams_stderr_stdout_single_json(self):
        import cli
        facade, _usage, _adapters = compose_facade()
        out, err = io.StringIO(), io.StringIO()
        cli.main._facade = facade
        try:
            with contextlib.redirect_stdout(out), \
                    contextlib.redirect_stderr(err):
                code = cli.main(["run", "--mode", "on", "--observe",
                                 TASK_COMPLEX])
        finally:
            del cli.main._facade
        self.assertEqual(code, 0)
        # stdout：恰好一行、合法 JSON、schema 键集不变。
        stdout_text = out.getvalue()
        self.assertEqual(stdout_text.count("\n"), 1)
        summary = json.loads(stdout_text)
        for key in ("status", "mode", "path", "task_id", "provenance",
                    "stages", "failure_category", "stage_counts"):
            self.assertIn(key, summary)
        # stderr：17 行 observation 投影，首行 DECISION、末行 TERMINAL。
        lines = [l for l in err.getvalue().splitlines() if l]
        self.assertEqual(len(lines), 17)
        self.assertTrue(lines[0].startswith("[0] DECISION"), lines[0])
        self.assertTrue(lines[16].startswith("[16] TERMINAL"), lines[16])
        # 零 observation 内容进入 stdout。
        self.assertNotIn("DECISION", stdout_text)

    def test_cli_default_off_stderr_empty_stdout_unchanged(self):
        import cli
        facade, _usage, _adapters = compose_facade()
        out, err = io.StringIO(), io.StringIO()
        cli.main._facade = facade
        try:
            with contextlib.redirect_stdout(out), \
                    contextlib.redirect_stderr(err):
                code = cli.main(["run", "--mode", "on", TASK_COMPLEX])
        finally:
            del cli.main._facade
        self.assertEqual(code, 0)
        self.assertEqual(err.getvalue(), "")
        self.assertEqual(out.getvalue().count("\n"), 1)
        json.loads(out.getvalue())


# ---------------------------------------------------------------------------
# 14: 源码纪律扫描
# ---------------------------------------------------------------------------


class SourceDisciplineTests(unittest.TestCase):

    def test_console_observation_zero_imports_beyond_future(self):
        text = (SCRIPTS / "console_observation.py").read_text(
            encoding="utf-8")
        modules = re.findall(r"^\s*(?:from|import)\s+([.\w]+)", text,
                             re.MULTILINE)
        self.assertTrue(
            set(modules) <= {"__future__"},
            f"unexpected imports: {set(modules) - {'__future__'}}")

    def test_console_observation_no_runtime_names(self):
        text = (SCRIPTS / "console_observation.py").read_text(
            encoding="utf-8").lower()
        for name in ("claude", "codex", "gemini"):
            self.assertNotIn(name, text)
        self.assertIsNone(re.search(r"\bpi\b", text))

    def test_console_observation_no_time_random_uuid(self):
        text = (SCRIPTS / "console_observation.py").read_text(
            encoding="utf-8")
        self.assertIsNone(re.search(r"\b(time|random|uuid)\b", text))

    def test_cli_has_no_projection_formatting(self):
        # projection 格式化住在 console_observation；CLI 只做组合。
        text = (SCRIPTS / "cli.py").read_text(encoding="utf-8")
        for token in ("format_event_line", "event_type", "duration_ms",
                      "event.sequence"):
            self.assertNotIn(token, text)


# ---------------------------------------------------------------------------
# 15: 受保护 untracked 文件（git 视角原样）
# ---------------------------------------------------------------------------


_PROTECTED = (
    "dual-agent-development/scripts/agent_identity.py",
    "tests/test_agent_identity.py",
    "tests/test_policy_boundary_qualification.py",
    "tests/test_real_cli_policy_collaboration.py",
)


class ProtectedUntrackedTests(unittest.TestCase):

    def test_protected_untracked_files_still_untracked(self):
        import shutil
        if shutil.which("git") is None:
            self.skipTest("git not available")
        for relpath in _PROTECTED:
            if not (Path(__file__).resolve().parents[1] / relpath).exists():
                self.skipTest(f"missing protected file: {relpath}")
            proc = subprocess.run(
                ["git", "status", "--porcelain", "--", relpath],
                cwd=str(Path(__file__).resolve().parents[1]),
                capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, relpath)
            self.assertTrue(
                proc.stdout.startswith("?? "),
                f"{relpath} expected untracked, got: {proc.stdout!r}")


if __name__ == "__main__":
    unittest.main()
