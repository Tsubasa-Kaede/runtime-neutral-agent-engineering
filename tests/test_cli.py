"""Phase 10H-K: CLI — minimal argparse entrypoint over the facade.

The CLI only parses args, calls an injected facade, and emits a safe JSON
summary. It never emits raw stdout/stderr, secrets, runtime names, addresses,
or reasoning. Offline only.

P1-U3 note: CLI 语义稳定契约（exit 映射 exit_code_for、语义失败
stdout JSON、--help/--version 预嗅探）住在 host_entry（产品边界组合根）
—— cli.py 是 V2 冻结件（五个 zero-diff 纪律测试钉定工作树零修改），
本文件只钉定其不变的渲染契约。
"""
import contextlib
import io
import json
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import cli
from cli import build_parser, render_summary, run_cli
from production_facade import FacadeResult


class StubFacade:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def run(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


def stub_result(status="SUCCESS", path="FOUR_STAGE", stages=("architect", "coder", "tester", "reviewer")):
    # 真实 facade 语义：成功 failure_category=""，失败 = 终态词本身。
    return FacadeResult(status=status, mode="AUTO", path=path, task_id="T1",
                        provenance="OFFLINE", stages=stages,
                        failure_category="" if status == "SUCCESS" else status,
                        safe_summary={"task_id": "T1", "provenance": "OFFLINE",
                                      "stage_counts": {"architect": 1, "coder": 1,
                                                       "tester": 1, "reviewer": 1}})


class ParserTests(unittest.TestCase):
    def test_mode_choices_and_task(self):
        parser = build_parser()
        args = parser.parse_args(["run", "--mode", "on", "do the thing"])
        self.assertEqual(args.mode, "on")
        self.assertEqual(args.task, "do the thing")

    def test_mode_default_is_auto(self):
        args = build_parser().parse_args(["run", "do the thing"])
        self.assertEqual(args.mode, "auto")

    def test_invalid_mode_is_rejected(self):
        with self.assertRaises(SystemExit):
            build_parser().parse_args(["run", "--mode", "bogus", "task"])


class VersionTests(unittest.TestCase):
    """RELEASE-2A: `--version` must use the package shim as its single source."""

    def test_cli_version_comes_from_package_init(self):
        import re as _re

        import cli

        init_text = (SCRIPTS / "__init__.py").read_text(encoding="utf-8")
        match = _re.search(r'__version__\s*=\s*"([^"]+)"', init_text)
        self.assertIsNotNone(match, "package shim must define __version__")
        self.assertEqual(cli.__version__, match.group(1))

    def test_version_flag_prints_and_exits_zero(self):
        import contextlib
        import io

        import cli

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            with self.assertRaises(SystemExit) as ctx:
                build_parser().parse_args(["--version"])
        self.assertEqual(ctx.exception.code, 0)
        self.assertEqual(out.getvalue().strip(), f"dual-agent {cli.__version__}")

    def test_main_version_and_help_work_without_facade(self):
        """--help/--version must not require an injected facade (RELEASE-2A)."""
        import contextlib
        import io

        import cli

        saved = cli.main.__dict__.get("_facade")
        cli.main.__dict__.pop("_facade", None)
        try:
            for flag in ("--version", "--help"):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    with self.assertRaises(SystemExit) as ctx:
                        cli.main([flag])
                self.assertEqual(ctx.exception.code, 0, flag)
                self.assertNotIn("no facade configured", out.getvalue(), flag)
            # without a facade a real run still fails honestly with exit 2
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(cli.main(["run", "x"]), 2)
            self.assertIn("no facade configured", err.getvalue())
        finally:
            if saved is not None:
                cli.main._facade = saved


class RenderTests(unittest.TestCase):
    def test_render_emits_closed_summary(self):
        summary = render_summary(stub_result())
        data = json.loads(summary)
        self.assertEqual(data["status"], "SUCCESS")
        self.assertEqual(data["path"], "FOUR_STAGE")
        self.assertIn("stages", data)
        self.assertIn("stage_counts", data)

    def test_render_never_leaks_secret_or_raw(self):
        summary = render_summary(stub_result()).lower()
        for marker in ("token", "secret", "api_key", "authorization", "bearer",
                       "stdout", "stderr", "claude", "codex", "deepseek", "gemini",
                       "anthropic", "openai", "runtime_id", "provider", "model",
                       "reasoning", "trace"):
            self.assertNotIn(marker, summary)

    def test_render_failure_category_is_emitted(self):
        result = stub_result(status="ARCHITECT_PACKET_INVALID", path="DUAL", stages=())
        data = json.loads(render_summary(result))
        self.assertEqual(data["status"], "ARCHITECT_PACKET_INVALID")
        self.assertEqual(data["path"], "DUAL")


class RunCliStabilityTests(unittest.TestCase):
    """P1-U3：run_cli 渲染契约的稳定面（cli.py 冻结原状下仍成立的部分）。"""

    def test_run_cli_output_is_canonical_single_machine_json_line(self):
        summary = run_cli(StubFacade(stub_result()), ["run", "x"])
        self.assertEqual(summary.count("\n"), 0)  # 单行（print 补换行）
        payload = json.loads(summary)
        self.assertEqual(payload["status"], "SUCCESS")
        # 键序确定性：渲染即规范形（sorted + 紧凑）。
        self.assertEqual(
            summary, json.dumps(payload, sort_keys=True,
                                separators=(",", ":")))

    def test_run_cli_failure_status_rendered_verbatim(self):
        summary = run_cli(
            StubFacade(stub_result(status="CODER_PACKET_INVALID",
                                   path="DUAL", stages=())), ["run", "x"])
        payload = json.loads(summary)
        self.assertEqual(payload["status"], "CODER_PACKET_INVALID")
        self.assertEqual(payload["failure_category"], "CODER_PACKET_INVALID")

    def test_main_no_facade_failure_shape_unchanged(self):
        # 嵌入面误用（无 facade）：既有 stderr JSON + exit 2 原样
        #（RELEASE-2A 钉定行为；产品入口 host_entry 不经过此路径）。
        saved = cli.main.__dict__.pop("_facade", None)
        try:
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(cli.main(["run", "x"]), 2)
        finally:
            if saved is not None:
                cli.main._facade = saved
        self.assertIn("no facade configured", err.getvalue())


if __name__ == "__main__":
    unittest.main()
