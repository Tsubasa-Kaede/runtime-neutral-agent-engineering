"""Minimal REAL-path host application example.

Composes the production verified path with public APIs only:

    Application -> AdapterRegistry -> ClaudeCodeAdapter
        -> Runtime Discovery -> Runtime Health -> G1-G14 Qualification
        -> VERIFIED + REAL evidence -> Verified Runtime Pool admission
        -> HostFacade -> facade.run()

No engine internals are reimplemented and nothing is stubbed: there is no
mock or offline fallback. When any prerequisite is missing the example
fails honestly with a non-zero exit and a one-line reason.

Prerequisites (see the README "Integration" section):

1. A source checkout and Python 3.10+.
2. Claude Code CLI installed on PATH and logged in through its own flow.
3. RUN_REAL_PROVIDER_TESTS=1 - the opt-in gate for real runtime calls.

Usage:

    python examples/minimal_host_app.py "Add a slug helper and its test"

This example never reads, stores, prints, or modifies credentials, never
logs in to or out of any runtime, and never touches runtime configuration;
authentication belongs to the runtime itself.
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "dual-agent-development" / "scripts"))

from claude_code_adapter import ClaudeCodeAdapter
from cli import render_summary
from generic_runtime_health import GenericRuntimeHealth
from host import build_facade_from_bootstrap
from mode_gate import Mode
from real_validation_executor import run_real_validation
from runtime_adapter_registry import (
    AdapterDescriptor,
    AdapterRegistry,
    discovery_sources,
)
from runtime_discovery import RuntimeCandidateDiscovery

# G13 snapshots these files before and after every real invocation and fails
# qualification if anything mutates them. Paths only - file contents are
# never read or printed. The placeholders below cover the common Claude Code
# configuration locations; declare whatever your own runtime touches.
HOME = Path.home()
PROTECTED_PATHS = (
    HOME / ".claude" / ".credentials.json",
    HOME / ".claude.json",
    HOME / ".claude" / "settings.json",
)


def _fail(message: str) -> int:
    """One closed, secret-free error line; exit code 2 means setup refused."""
    print(f"minimal-host-app: {message}", file=sys.stderr)
    return 2


def real_gate_open() -> bool:
    return os.environ.get("RUN_REAL_PROVIDER_TESTS", "") == "1"


def build_registry() -> AdapterRegistry:
    """Register Claude Code when present. An absent runtime is honest
    absence (None from the adapter), never a fake registration."""
    adapter = ClaudeCodeAdapter.from_environment()
    if adapter is None:
        raise RuntimeError(
            "Claude Code CLI not found on PATH - install it and log in "
            "through its own flow first (this example has no offline "
            "fallback)")
    registry = AdapterRegistry()
    registry.register(AdapterDescriptor(
        runtime_id="claude-cli", provider_id="anthropic", model_id=None,
        runtime_type="coding-agent", display_name="Claude Code",
        adapter_factory=lambda: adapter,
        config_fingerprint="installed",
    ))
    return registry


def build_current_health(registry):
    """Real health snapshots for orchestration-time gating, built with the
    same public discovery and health chain the bootstrap itself uses.
    Undiscoverable runtimes are skipped, never fabricated."""
    health = {}
    for candidate in RuntimeCandidateDiscovery(
            discovery_sources(registry)).discover_all():
        if not candidate.available:
            continue
        adapter = registry.get(candidate.runtime_id).adapter_factory()
        result = GenericRuntimeHealth().check(candidate, adapter)
        health[candidate.runtime_id] = result.status
    if not health:
        raise RuntimeError("no runtime passed discovery")
    return health


def make_qualifier():
    """Sanctioned G1-G14 REAL qualification through the public entry point;
    the gate variable was already checked, so provenance will be REAL."""
    def qualify(instance):
        validation, _executor = run_real_validation(
            instance, instance.probe, timeout_seconds=300.0,
            protected_paths=PROTECTED_PATHS,
            experiment_id="minimal-host-app-qualification",
        )
        return validation
    return qualify


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        return _fail('usage: python examples/minimal_host_app.py "your task"')
    if not real_gate_open():
        return _fail(
            "REAL runtime path requires RUN_REAL_PROVIDER_TESTS=1 "
            "(opt-in gate; this example never falls back to mock/offline)")
    task = " ".join(argv)
    try:
        registry = build_registry()
        facade = build_facade_from_bootstrap(
            registry,
            qualifier=make_qualifier(),
            current_health=build_current_health(registry),
        )
    except RuntimeError as exc:
        return _fail(str(exc))
    # provenance is omitted on purpose: HostFacade labels the run from the
    # qualification evidence, so this seam cannot mislabel a REAL run.
    result = facade.run(task_id="minimal-host-app", task=task, prompt=task,
                        mode=Mode.ON)
    print(render_summary(result))
    return 0 if result.status == "SUCCESS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
