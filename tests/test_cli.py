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
