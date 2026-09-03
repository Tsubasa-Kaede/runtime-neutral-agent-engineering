"""Minimal CLI entrypoint over the production facade (10H-K).

Parses args, calls an injected (pre-configured) facade, and emits a safe JSON
summary. The CLI understands no runtimes and mints no budget/guard/state/
adapter — it forwards mode + task and the parsed CollaborationPolicy
(R7-A2 entry surface), then renders the closed FacadeResult.

Policy construction fails here, honestly and before any facade access:
argparse exits non-zero and no adapter is ever invoked.
"""
from __future__ import annotations

import argparse
import json
import sys

from collaboration_policy import CollaborationPolicy
from host import DEFAULT_MIN_DISTINCT_RUNTIMES
from mode_gate import Mode

try:  # installed-package mode: dual_agent.cli -> package shim
    from . import __version__
except ImportError:  # source-tree flat-import mode (tests/examples)
    from __init__ import __version__

_MODE_MAP = {"off": Mode.OFF, "auto": Mode.AUTO, "on": Mode.ON}

# R7-A2 advisory precheck: the four-stage pipeline has exactly four role
# slots (architect/coder/tester/reviewer), so a min/max distinct-runtime
# bound above the slot count is a pure configuration error — rejected at
# the entry layer BEFORE any facade access. The engine never learns this
# rule (collaboration_policy.py stays slot-count-agnostic); the precheck
# reads no health, no auth, no pool, and touches no runtime.
MAX_ROLE_SLOTS = 4


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dual-agent", description="Dual-agent collaboration entrypoint")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="Run a collaboration task")
    run.add_argument("--mode", choices=("off", "auto", "on"), default="auto",
                     help="off: no orchestration; auto: route by complexity; on: force dual")
    run.add_argument("--runtimes", default=None, metavar="RUNTIME_ID[,RUNTIME_ID...]",
                     help="runtime allowlist; comma-separated, deduplicated, sorted deterministically")
    run.add_argument("--min-runtimes", type=int, default=None, metavar="N",
                     help="desired minimum distinct runtimes (default: host deployment constant)")
    run.add_argument("--max-runtimes", type=int, default=None, metavar="N",
                     help="maximum distinct runtimes")
    run.add_argument("--no-runtime-reuse", action="store_true", default=False,
                     help="forbid one runtime serving multiple roles (injective assignment)")
    run.add_argument("task", help="the task to collaborate on")
    return parser


def policy_from_args(args) -> CollaborationPolicy:
    """Build the run-level CollaborationPolicy from parsed CLI flags.

    The deployment-default minimum (host constant, single source — never a
    magic number copied here) applies only when the user set NEITHER bound:
    an explicit --max-runtimes alone is a pure upper bound (min stays None),
    so "max only" combinations are never rejected for clashing with the
    default. Every invalid combination the user DID express is rejected by
    CollaborationPolicy's constructor BEFORE any facade access.
    """
    allowlist = None
    if args.runtimes is not None:
        entries = [entry.strip() for entry in args.runtimes.split(",")]
        if any(not entry for entry in entries):
            raise SystemExit(f"dual-agent: error: --runtimes entries must be non-empty")
        allowlist = tuple(entries)
    if args.min_runtimes is not None or args.max_runtimes is not None:
        min_runtimes = args.min_runtimes
    else:
        min_runtimes = DEFAULT_MIN_DISTINCT_RUNTIMES
    for name, value in (("--min-runtimes", min_runtimes),
                        ("--max-runtimes", args.max_runtimes)):
        if value is not None and value > MAX_ROLE_SLOTS:
            raise SystemExit(
                f"dual-agent: error: {name} {value} exceeds the four "
                f"collaboration role slots (max {MAX_ROLE_SLOTS})")
    return CollaborationPolicy(
        runtime_allowlist=allowlist,
        min_distinct_runtimes=min_runtimes,
        max_distinct_runtimes=args.max_runtimes,
        allow_runtime_reuse=not args.no_runtime_reuse,
    )


def render_summary(result) -> str:
    """Emit ONLY the closed safe summary — never raw output, secrets, or traces."""
    return json.dumps({
        "status": result.status,
        "mode": result.mode,
        "path": result.path,
        "task_id": result.task_id,
        "provenance": result.provenance,
        "stages": list(result.stages),
        "failure_category": result.failure_category,
        "stage_counts": result.safe_summary.get("stage_counts", {}),
    }, sort_keys=True, separators=(",", ":"))


def run_cli(facade, argv=None) -> str:
    """Drive one run from argv; returns the safe JSON summary string."""
    args = build_parser().parse_args(argv)
    mode = _MODE_MAP[args.mode]
    try:
        policy = policy_from_args(args)
    except ValueError as error:  # construction-time rejection: honest exit,
        # non-zero, before any facade access and any adapter invocation.
        raise SystemExit(f"dual-agent: error: invalid policy: {error}") from error
    result = facade.run(task_id=args.task, task=args.task, prompt=args.task,
                        mode=mode, policy=policy)
    return render_summary(result)


def main(argv=None) -> int:
    # Parse up front so --help/--version work even without an injected
    # facade (argparse handles both before any facade access).
    build_parser().parse_args(argv)
    # The facade must be injected/configured by the embedding application; this
    # module never builds one. If none is provided, report clearly and exit.
    facade = getattr(main, "_facade", None)
    if facade is None:
        print(json.dumps({"error": "no facade configured"}), file=sys.stderr)
        return 2
    print(run_cli(facade, argv))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
