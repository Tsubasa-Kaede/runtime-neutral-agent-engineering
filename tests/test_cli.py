"""Phase 10H-K: CLI — minimal argparse entrypoint over the facade.

The CLI only parses args, calls an injected facade, and emits a safe JSON
summary. It never emits raw stdout/stderr, secrets, runtime names, addresses,
or reasoning. Offline only.
"""
import json
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from cli import build_parser, render_summary
from production_facade import FacadeResult


class StubFacade:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def run(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


def stub_result(status="SUCCESS", path="FOUR_STAGE", stages=("architect", "coder", "tester", "reviewer")):
    return FacadeResult(status=status, mode="AUTO", path=path, task_id="T1",
                        provenance="OFFLINE", stages=stages, failure_category="",
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


if __name__ == "__main__":
    unittest.main()
