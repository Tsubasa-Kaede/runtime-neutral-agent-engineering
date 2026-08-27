"""Offline, deterministic tests for examples/minimal_host_app.py.

Error boundary and output contract only. Every test runs without network,
without a runtime, without credentials, and without depending on the
developer machine's RUN_REAL_PROVIDER_TESTS setting. The one test that
sets the gate variable does so in a child process whose PATH is empty, so
no runtime can be discovered and no real invocation can ever happen.
"""
import io
import json
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "dual-agent-development" / "scripts"
EXAMPLES = ROOT / "examples"
EXAMPLE = EXAMPLES / "minimal_host_app.py"
SECRET_MARKERS = ("token", "secret", "api_key", "authorization", "bearer")


def _run_example(args, overrides=None):
    """Run the example in a child env from which the gate never leaks in."""
    env = {k: v for k, v in os.environ.items()
           if k != "RUN_REAL_PROVIDER_TESTS"}
    env.update(overrides or {})
    return subprocess.run(
        [sys.executable, str(EXAMPLE), *args],
        capture_output=True, text=True, timeout=120,
        cwd=str(ROOT), env=env, encoding="utf-8", errors="replace",
    )


class ErrorBoundaryTests(unittest.TestCase):
    def test_missing_task_argument_fails_with_usage(self):
        done = _run_example([])
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("usage", done.stderr.lower())

    def test_missing_gate_variable_fails_honestly(self):
        done = _run_example(["demo task"])
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("RUN_REAL_PROVIDER_TESTS=1", done.stderr)
        self.assertNotIn("SUCCESS", done.stdout)

    def test_absent_runtime_fails_without_invoking_anything(self):
        # Gate open in the child, but PATH is empty: the adapter cannot be
        # discovered, so the example must exit before any real invocation.
        done = _run_example(["demo task"],
                            {"RUN_REAL_PROVIDER_TESTS": "1", "PATH": ""})
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("Claude Code", done.stderr)
        self.assertNotIn("SUCCESS", done.stdout)

    def test_error_outputs_are_secret_free(self):
        cases = (
            _run_example([]),
            _run_example(["demo task"]),
            _run_example(["demo task"],
                         {"RUN_REAL_PROVIDER_TESTS": "1", "PATH": ""}),
        )
        for done in cases:
            lowered = (done.stdout + done.stderr).lower()
            for marker in SECRET_MARKERS:
                self.assertNotIn(marker, lowered)


class NoFallbackTests(unittest.TestCase):
    def test_source_composes_the_real_path_and_no_mock(self):
        source = EXAMPLE.read_text(encoding="utf-8")
        self.assertNotIn("offline_mock", source)
        self.assertNotIn("Mock", source)
        for api in ("from_environment", "run_real_validation",
                    "build_facade_from_bootstrap", "GenericRuntimeHealth"):
            self.assertIn(api, source)


class SuccessOutputTests(unittest.TestCase):
    def test_success_summary_is_closed_json_with_real_provenance(self):
        sys.path.insert(0, str(SCRIPTS))
        sys.path.insert(0, str(EXAMPLES))
        import minimal_host_app
        from production_facade import FacadeResult

        class StubFacade:
            def run(self, **kwargs):
                return FacadeResult(
                    status="SUCCESS", mode="on", path="FOUR_STAGE",
                    task_id="minimal-host-app", provenance="REAL",
                    stages=("architect", "coder", "tester", "reviewer"),
                    failure_category=None,
                    safe_summary={"task_id": "minimal-host-app",
                                  "provenance": "REAL",
                                  "stage_counts": {"architect": 1}})

        saved = (minimal_host_app.real_gate_open,
                 minimal_host_app.build_registry,
                 minimal_host_app.build_current_health,
                 minimal_host_app.build_facade_from_bootstrap)
        try:
            minimal_host_app.real_gate_open = lambda: True
            minimal_host_app.build_registry = lambda: object()
            minimal_host_app.build_current_health = lambda registry: {}
            minimal_host_app.build_facade_from_bootstrap = \
                lambda *a, **k: StubFacade()
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = minimal_host_app.main(["demo task"])
        finally:
            (minimal_host_app.real_gate_open,
             minimal_host_app.build_registry,
             minimal_host_app.build_current_health,
             minimal_host_app.build_facade_from_bootstrap) = saved

        self.assertEqual(code, 0)
        payload = json.loads(buffer.getvalue())
        self.assertEqual(payload["status"], "SUCCESS")
        self.assertEqual(payload["path"], "FOUR_STAGE")
        self.assertEqual(payload["provenance"], "REAL")
        lowered = buffer.getvalue().lower()
        for marker in SECRET_MARKERS:
            self.assertNotIn(marker, lowered)


if __name__ == "__main__":
    unittest.main()
