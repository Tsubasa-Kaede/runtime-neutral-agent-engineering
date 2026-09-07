"""Repository example smoke — the offline remote demo runs clean.

Executes examples/remote_offline_demo.py as a real subprocess from a
clean-room standpoint (absolute paths, no test doubles, no gate
environment) and asserts the closed JSON summary contract. The REAL
example is verified statically only: it must use the production adapter
acquisition, never the test gate, and carry the honest missing-runtime
refusal — real provider execution is intentionally NOT part of CI.
"""
import json
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OFFLINE_DEMO = REPO / "examples" / "remote_offline_demo.py"
REAL_EXAMPLE = REPO / "examples" / "remote_real_claude.py"
SCRIPTED_MODULE = REPO / "examples" / "scripted_coder.py"


class OfflineRemoteDemoSmokeTests(unittest.TestCase):
    """The offline demo is the first-user entry — it must just run."""

    def test_offline_remote_demo_runs_clean_and_prints_closed_summary(self):
        result = subprocess.run(
            [sys.executable, str(OFFLINE_DEMO)],
            capture_output=True, text=True, cwd=str(REPO), timeout=120,
        )
        self.assertEqual(
            result.returncode, 0, msg=f"stderr: {result.stderr}")
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        self.assertEqual(
            len(lines), 1, msg="stdout must be exactly one closed JSON line")
        summary = json.loads(lines[0])
        self.assertEqual(summary["status"], "SUCCESS")
        self.assertEqual(summary["adapter"],
                         "scripted (offline demonstration)")
        self.assertEqual(summary["agent_id"], "scripted-coder")
        self.assertEqual(summary["role"], "coder")
        self.assertEqual(summary["remote_address"],
                         "agent:scripted-coder:coder")
        self.assertEqual(summary["task_id"], summary["reply_task_id"])
        self.assertTrue(summary["reply_summary"])
        self.assertEqual(summary["provenance"], "OFFLINE")

    def test_offline_demo_does_not_touch_gate_or_credentials(self):
        source = OFFLINE_DEMO.read_text(encoding="utf-8")
        self.assertNotIn("RUN_REAL_PROVIDER_TESTS", source)


class RealExampleDisciplineTests(unittest.TestCase):
    """Static discipline of the REAL example (no provider in CI)."""

    def test_uses_production_adapter_acquisition(self):
        source = REAL_EXAMPLE.read_text(encoding="utf-8")
        self.assertIn("from_environment", source)

    def test_never_uses_the_test_gate(self):
        source = REAL_EXAMPLE.read_text(encoding="utf-8")
        self.assertNotIn("RUN_REAL_PROVIDER_TESTS", source)

    def test_carries_the_honest_missing_runtime_refusal(self):
        source = REAL_EXAMPLE.read_text(encoding="utf-8")
        self.assertIn("return 2", source)
        self.assertIn("prerequisite", source.lower())

    def test_scripted_module_exposes_the_factory_convention(self):
        source = SCRIPTED_MODULE.read_text(encoding="utf-8")
        self.assertIn("def build(profile=None)", source)


if __name__ == "__main__":
    unittest.main()
