"""Minimal CLI entrypoint over the production facade (10H-K).

Parses args, calls an injected (pre-configured) facade, and emits a safe JSON
summary. The CLI understands no runtimes and mints no budget/guard/state/
adapter — it only forwards mode + task and renders the closed FacadeResult.
"""
from __future__ import annotations

import argparse
import json
import sys

from mode_gate import Mode

_MODE_MAP = {"off": Mode.OFF, "auto": Mode.AUTO, "on": Mode.ON}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dual-agent", description="Dual-agent collaboration entrypoint")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="Run a collaboration task")
    run.add_argument("--mode", choices=("off", "auto", "on"), default="auto",
                     help="off: no orchestration; auto: route by complexity; on: force dual")
    run.add_argument("task", help="the task to collaborate on")
    return parser


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
    result = facade.run(task_id=args.task, task=args.task, prompt=args.task, mode=mode)
    return render_summary(result)


def main(argv=None) -> int:
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
