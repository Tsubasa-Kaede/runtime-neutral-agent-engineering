"""One-command bootstrap for a local, editable install of this project.

Usage:

    python scripts/bootstrap.py          # create/reuse .venv + install this project
    python scripts/bootstrap.py --check  # no-side-effect preflight

Hard boundaries (by design):

- Installs THIS PROJECT ONLY. It never installs, configures, upgrades, or
  logs into a third-party agent runtime (Claude Code, Codex CLI, Gemini
  CLI, tiny-agents, ...): runtime installation and authentication belong
  to the user and the runtime's own flow.
- Never reads or writes secrets, tokens, .env files, or credentials of
  any kind, and never logs in to or out of any service.
- Never modifies system-level configuration: no PATH edits, no shell
  profiles, no git configuration, no system Python. Everything it creates
  stays inside the repository's own .venv directory.
- --check performs a preflight only: no files are created and no network
  is used.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VENV_DIR = ROOT / ".venv"
MINIMUM_PYTHON = (3, 10)

# Repository layout the bootstrap relies on (and --check verifies).
EXPECTED_PATHS = (
    "pyproject.toml",
    "README.md",
    "examples/offline_mock_run.py",
    "dual-agent-development/scripts/cli.py",
    "tests",
)


def python_ok(version_info=None) -> bool:
    """True when the running (or given) interpreter is 3.10+."""
    version = version_info if version_info is not None else sys.version_info
    return (version[0], version[1]) >= MINIMUM_PYTHON


def layout_ok(root: Path = ROOT) -> bool:
    """True when the expected repository layout is present under root."""
    return all((root / part).exists() for part in EXPECTED_PATHS)


def venv_python(venv_dir: Path = VENV_DIR) -> Path:
    """Interpreter path inside the virtual environment, either platform."""
    windows = venv_dir / "Scripts" / "python.exe"
    posix = venv_dir / "bin" / "python"
    return windows if windows.exists() else posix


def check() -> int:
    """Preflight: python version + repository layout. No files, no network."""
    problems = []
    if not python_ok():
        problems.append(
            f"Python {MINIMUM_PYTHON[0]}.{MINIMUM_PYTHON[1]}+ required, "
            f"found {sys.version.split()[0]}")
    missing = [part for part in EXPECTED_PATHS if not (ROOT / part).exists()]
    if missing:
        problems.append(
            "repository layout mismatch, missing: " + ", ".join(missing))
    if problems:
        for line in problems:
            print(f"bootstrap-check: {line}", file=sys.stderr)
        return 2
    print("bootstrap-check: OK")
    print(f"bootstrap-check: Python {sys.version.split()[0]}")
    print("bootstrap-check: repository layout verified")
    print("bootstrap-check: no files created, no network used")
    return 0


def create_venv() -> int:
    """Create .venv when absent; an existing one is reused untouched."""
    if venv_python().exists():
        print(f"bootstrap: reusing existing virtual environment ({VENV_DIR})")
        return 0
    print(f"bootstrap: creating virtual environment at {VENV_DIR}")
    code = subprocess.run(
        [sys.executable, "-m", "venv", str(VENV_DIR)]).returncode
    if code != 0:
        print("bootstrap: venv creation failed", file=sys.stderr)
    return code


def install_project() -> int:
    """Editable-install this project from the repository root. Nothing else."""
    target = venv_python()
    if not target.exists():
        print("bootstrap: virtual environment python not found",
              file=sys.stderr)
        return 2
    print("bootstrap: installing this project "
          "(editable; no third-party agent runtime is installed)")
    return subprocess.run(
        [str(target), "-m", "pip", "install", "-e", "."],
        cwd=str(ROOT)).returncode


def print_next_steps() -> None:
    activate = (".venv\\Scripts\\activate" if os.name == "nt"
                else "source .venv/bin/activate")
    print("bootstrap: done. This project is installed; no agent runtime "
          "was installed, configured, or logged into.")
    print("bootstrap: next steps:")
    print(f"  {activate}")
    print("  dual-agent --version")
    print("  python examples/offline_mock_run.py")
    print("  (for REAL runs: install the Claude Code CLI yourself, then "
          "opt in with RUN_REAL_PROVIDER_TESTS=1)")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="bootstrap.py",
        description="Create/reuse .venv and install this project "
                    "(nothing else).")
    parser.add_argument(
        "--check", action="store_true",
        help="preflight only: verify Python and repository layout; "
             "create no files, use no network")
    args = parser.parse_args(argv)
    # Keep this script's own messages ordered against subprocess (pip)
    # output when stdout is redirected to a log or CI capture.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass
    if args.check:
        return check()
    code = check()
    if code != 0:
        return code
    code = create_venv()
    if code != 0:
        return code
    code = install_project()
    if code != 0:
        return code
    print_next_steps()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
