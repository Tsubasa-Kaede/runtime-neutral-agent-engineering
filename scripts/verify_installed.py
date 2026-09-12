"""Install-state verification for the built wheel (CU 2.4.2-B).

Called by .github/workflows/ci.yml (install-smoke job) after building the
wheel and installing it into a fresh venv:

    python -m venv <venv>
    <venv>/python -m pip install --no-index --find-links dist dual_agent_development
    cd <neutral cwd> && <venv>/python scripts/verify_installed.py

Why a standalone script and not pytest: the offline suite runs against the
source tree, where flat imports form a single module graph by construction.
The defect this script pins (2.4.2-A) only exists in the INSTALLED package,
where the dual_agent shim makes flat and package-relative import spellings
both resolvable — a mixed spelling instantiates sibling modules twice and
breaks enum/class identity comparisons (cockpit's validation gate judged
every persisted VERIFIED+REAL evidence as "no persisted VERIFIED evidence").
pytest's rootdir/conftest machinery puts source-tree paths on sys.path,
which is exactly the shadowing this check must not have. Hence: fresh venv,
wheel install, neutral cwd, plain checks, exit 0/1.

What the smoke proves (no runtime CLI, no REAL provider calls, no
credentials — the fixture is constructed in-process and round-trips through
the installed evidence store):

    T1  the dual_agent package resolves from this venv's site-packages
    T2  the console entry point starts and reports the package version
    T3  a VERIFIED+REAL fixture round-trips save_evidence -> load_evidence
    T4  the production identity comparison in cockpit validation holds:
        loaded.status is cockpit_entry.CandidateValidationStatus.VERIFIED
    T5  cockpit's imported types resolve from the flat module graph (no
        second enum/class identity). host_entry-side relative imports are
        the P1-U4-ruled function/factory set: reported, never failed here.
    T6  evidence_store.SCHEMA_VERSION is unchanged (schema stability)

Failure contract: every failed check prints "FAIL <name>" and the script
exits 1 with a summary; success prints one PASS line per check, an
informational dual-graph listing, and exits 0.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import time
from pathlib import Path

import dual_agent
from dual_agent import cockpit_entry, evidence_store, host_entry

# cockpit_entry's import block, by module-graph discipline (2.4.2-A): every
# type cockpit compares or isinstance-checks must come from the flat module
# the rest of the engine shares — a package-relative twin would break the
# identity checks listed in the comment above the import block.
_COCKPIT_FLAT_TYPES = {
    "CandidateValidationStatus": "candidate_validation",
    "ExternalAgentRequest": "external_runtime",
    "ExecutionSlotSpec": "execution_slots",
    "ControlJournal": "control_journal",
    "UsageLog": "usage_log",
    "RunStatus": "sequential_pipeline",
    "StepSpec": "sequential_pipeline",
}


def _check(results: list, name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'} {name}{' ' + detail if detail else ''}")
    results.append(ok)


def _check_install_state(results: list) -> None:
    _check(results, "T1 dual_agent resolves from site-packages",
           "site-packages" in dual_agent.__file__.replace("\\", "/"),
           f"({dual_agent.__file__})")


def _check_console_entry(results: list) -> None:
    entries = sorted(Path(sys.executable).parent.glob("dual-agent*"))
    if not entries:
        _check(results, "T2 console entry starts and reports version",
               False, "(dual-agent console script not found next to python)")
        return
    proc = subprocess.run(
        [str(entries[0]), "--version"],
        capture_output=True, text=True, timeout=60,
        cwd=tempfile.gettempdir(),  # neutral cwd: no source-tree shadowing
    )
    expected = f"dual-agent {dual_agent.__version__}"
    _check(results, "T2 console entry starts and reports version",
           proc.returncode == 0 and proc.stdout.strip() == expected,
           f"(expected {expected!r}, got rc={proc.returncode} "
           f"stdout={proc.stdout.strip()!r})")


def _check_evidence_roundtrip_and_identity(results: list) -> None:
    # Flat import on purpose: the engine's own module graph. The dual_agent
    # shim guarantees this spelling resolves in installed mode too.
    from candidate_validation import (
        CandidateValidationResult,
        CandidateValidationStatus,
        GateResult,
        GateVerdict,
        ValidationGate,
    )

    gate_results = tuple(
        GateResult(gate=gate, verdict=GateVerdict.PASS,
                   evidence={"smoke": "install-smoke"})
        for gate in ValidationGate
    )
    fixture = CandidateValidationResult(
        identity=("install-smoke", "fixture", None, "default"),
        status=CandidateValidationStatus.VERIFIED,
        gates_passed=frozenset(ValidationGate),
        gate_results=gate_results,
        block_reason=None,
        failure_point=None,
        experiment_id="install-smoke",
        executed_at=time.time(),
        validated_capabilities=("coding",),
        provenance="REAL",
    )

    with tempfile.TemporaryDirectory() as tmp:
        try:
            evidence_store.save_evidence(Path(tmp), fixture)
            loaded, rejected = host_entry.load_evidence(Path(tmp))
        except (OSError, ValueError) as error:
            _check(results, "T3 VERIFIED+REAL fixture round-trips the store",
                   False, f"({type(error).__name__}: {error})")
            _check(results, "T4 cockpit identity comparison holds", False,
                   "(no loaded evidence to compare)")
            return

        entry = loaded.get(fixture.identity)
        _check(
            results, "T3 VERIFIED+REAL fixture round-trips the store",
            not rejected and len(loaded) == 1 and entry is not None
            and entry.provenance == "REAL",
            f"(loaded={len(loaded)} rejected={len(rejected)})")
        # The exact production comparison at cockpit_entry's validation
        # gate — the one the dual module graph made permanently False.
        _check(results, "T4 cockpit identity comparison holds",
               entry is not None and entry.status
               is cockpit_entry.CandidateValidationStatus.VERIFIED)


def _check_module_graph(results: list) -> None:
    off_graph = [
        f"{name}!={module}"
        for name, module in _COCKPIT_FLAT_TYPES.items()
        if getattr(cockpit_entry, name).__module__ != module
    ]
    _check(results, "T5 cockpit types resolve from the flat graph",
           not off_graph, f"({', '.join(off_graph)})" if off_graph else "")
    _check(results, "T5b no dual_agent.candidate_validation twin",
           "dual_agent.candidate_validation" not in sys.modules)

    # Informational only (P1-U4 ruling, out of 2.4.2-B scope): relative
    # imports inside host_entry instantiate function/factory twins. They
    # carry no identity comparisons, so they are listed, never failed.
    names_by_file: dict = {}
    for name, module in list(sys.modules.items()):
        file = getattr(module, "__file__", None)
        if file and "dual_agent" in file.replace("\\", "/"):
            names_by_file.setdefault(file, set()).add(name)
    twins = sorted(
        next(iter(names)) for names in names_by_file.values() if len(names) > 1
    )
    print(f"INFO host_entry-side dual identities (P1-U4-ruled): "
          f"{len(twins)} module files")


def _check_schema(results: list) -> None:
    _check(results, "T6 evidence schema_version unchanged",
           evidence_store.SCHEMA_VERSION == 1,
           f"(== {evidence_store.SCHEMA_VERSION})")


def main() -> int:
    print(f"verify_installed: dual_agent {dual_agent.__version__} "
          f"from {dual_agent.__file__}")
    results: list = []
    _check_install_state(results)
    _check_console_entry(results)
    _check_evidence_roundtrip_and_identity(results)
    _check_module_graph(results)
    _check_schema(results)
    failed = results.count(False)
    print(f"RESULT: {'GREEN' if not failed else f'RED ({failed} failed)'}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
