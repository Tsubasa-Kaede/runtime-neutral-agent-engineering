"""Offline tests for scripts/bootstrap.py.

The full bootstrap (venv creation + editable install) is intentionally
never executed here: it would create files and use the network. These
tests cover the preflight contract (--check), the pure check functions,
and the safety boundary of the script source.
"""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "bootstrap.py"

sys.path.insert(0, str(ROOT / "scripts"))
import bootstrap  # noqa: E402


def _run_script(*args):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True, text=True, timeout=60,
        cwd=str(ROOT), encoding="utf-8", errors="replace",
    )


class CheckModeTests(unittest.TestCase):
    def test_check_exits_zero_and_creates_nothing(self):
        venv_existed = bootstrap.VENV_DIR.exists()
        done = _run_script("--check")
        self.assertEqual(done.returncode, 0)
        self.assertIn("bootstrap-check: OK", done.stdout)
        self.assertIn("no files created, no network used", done.stdout)
        # The preflight must not have created (or removed) a venv.
        self.assertEqual(bootstrap.VENV_DIR.exists(), venv_existed)

    def test_help_exits_zero_and_documents_check(self):
        done = _run_script("--help")
        self.assertEqual(done.returncode, 0)
        self.assertIn("--check", done.stdout)


class CheckFunctionTests(unittest.TestCase):
    def test_python_version_gate(self):
        self.assertTrue(bootstrap.python_ok())
        self.assertTrue(bootstrap.python_ok((3, 10, 0)))
        self.assertTrue(bootstrap.python_ok((3, 12, 9)))
        self.assertFalse(bootstrap.python_ok((3, 9, 18)))
        self.assertFalse(bootstrap.python_ok((2, 7, 18)))

    def test_layout_gate(self):
        self.assertTrue(bootstrap.layout_ok(ROOT))
        with tempfile.TemporaryDirectory() as empty:
            self.assertFalse(bootstrap.layout_ok(Path(empty)))


class BoundaryTests(unittest.TestCase):
    def test_installs_only_this_project(self):
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertEqual(source.count('"pip"'), 1)
        self.assertIn('"-m", "pip", "install", "-e", "."', source)
        self.assertNotIn("npm", source.lower())

    def test_no_login_and_no_environment_or_secret_access(self):
        source = SCRIPT.read_text(encoding="utf-8")
        lowered = source.lower()
        for banned in ("login", "logout", "os.environ", "getenv",
                       "credentials.json"):
            self.assertNotIn(banned, lowered)


if __name__ == "__main__":
    unittest.main()
