# Dual-Agent Development (V2)

A **runtime-neutral agent collaboration orchestrator**: given a task, it
classifies the work, routes it, and runs it through up to four structured
collaboration stages — **architect → coder → tester → reviewer** — with
structured packet handoffs, an append-only shared ledger, one task-lifecycle
budget, loop protection, and honest failure reporting.

This is **not a chatbot** and not a model provider. It is the orchestration
layer above whatever coding-agent CLIs you already have: it discovers runtimes,
checks their health, selects them by *verified capabilities* (never by name),
and coordinates their work through validated contracts.

## Architecture

```text
                     ┌────────────────────────────┐
 task + mode ──────► │      ProductionFacade      │  (the entrypoint)
                     └─────────────┬──────────────┘
                OFF/AUTO/ON + complexity routing
                     ┌─────────────┴──────────────┐
                     ▼                            ▼
          CollaborationOrchestrator     VerificationCollaboration
          (architect → coder)           (tester → reviewer)
                     │                            │
                     ▼                            ▼
        CollaborationPacket envelopes + Shared Collaboration State
        (append-only ledger: DECISION/REQUEST/REPLY/FAILURE records)
                     │                            │
                     ▼                            ▼
        shared TaskBudget/BudgetUsage + LoopGuard (one task lifecycle)
```

- **Packets**: `ArchitecturePacket`, `ImplementationPacket`, `TestPacket`,
  `ReviewPacket` (see `dual-agent-development/templates/`), wrapped in a
  `CollaborationPacket` envelope. A stage never sees another stage's raw
  output — only structured packets.
- **Ledger**: append-only, immutable, wire-at-append. `task_id` scopes the
  whole lifecycle, `correlation_id` links one request/reply hop, `sequence`
  is per-task, dense, and assigned by the ledger.
- **Selection**: runtimes are discovered and health-checked
  (READY / AUTH_REQUIRED / UNAVAILABLE / ERROR) and admitted to the verified
  pool by capability evidence — runtime-neutral, no hardcoded runtimes.

## Installation

Requires Python 3.10+ (standard library only):

```bash
git clone <repo-url>
cd dual-agent-development-repo
pip install -e .
```

This installs the `dual_agent` package (mapped from
`dual-agent-development/scripts/`), the `dual-agent` console script, and the
skill assets (`SKILL.md`, `references/`, `templates/`, `agents/`, `examples/`)
under the package data area.

## Quick Start (offline, no runtime needed)

The fastest way to see the four-stage chain is the offline example, which uses
mock adapters and the real `ProductionFacade`:

```bash
python examples/offline_mock_run.py
```

Expected output — a closed, secret-free JSON summary:

```json
{"path": "FOUR_STAGE", "status": "SUCCESS", "stages": ["architect","coder","tester","reviewer"], ...}
```

## CLI

```bash
dual-agent run --mode off  "实现 GitHub Webhook"
dual-agent run --mode auto "实现 GitHub Webhook"
dual-agent run --mode on   "实现 GitHub Webhook"
```

**Honest limitation**: the CLI parses arguments, calls an injected facade, and
prints the closed JSON summary (status, mode, path, stages, failure category,
stage counts). The `ProductionFacade` must be **configured and injected by a
host application** — the CLI never creates runtimes, adapters, credentials, or
a default facade by itself, and it will never auto-configure a provider or
read an API key. Without an injected facade it exits with a clear error. Host
applications inject like this:

```python
from dual_agent import cli
cli.main._facade = my_configured_facade   # built with your adapters/pool
```

See `examples/offline_mock_run.py` for how a facade is constructed from real
engine components.

## Modes

| Mode | Behavior |
|---|---|
| `off` | No orchestration; returns the delegated empty result |
| `auto` | Classifies the task; SIMPLE/MEDIUM/UNRESOLVED take the single-agent path, COMPLEX takes the dual-agent path |
| `on` | Forces the dual-agent path (architect+coder, then tester+reviewer when capable candidates exist) |

Dual-agent success without verified tester/reviewer candidates is reported as
`NO_VERIFICATION_CAPABILITY` — never a silent two-stage success, never a
fabricated four-stage one.

## Roles and packets

| Role | Reads | Produces |
|---|---|---|
| architect | the task | `ArchitecturePacket` |
| coder | the architecture packet wire | `ImplementationPacket` |
| tester | latest implementation packet | `TestPacket` |
| reviewer | architecture + implementation + test | `ReviewPacket` |

## Failure semantics

Failures are structured and terminal; downstream stages do not run after an
upstream failure. Nothing is success-wrapped and nothing falls back silently:

- `*_INVOKE_FAILED`, `*_PACKET_INVALID` — a stage failed at the runtime or
  against the packet contract
- `MISSING_HANDOFF` — a required upstream packet is absent from the ledger
- `BUDGET_EXHAUSTED`, `LOOP_GUARD_REJECTED` — task-lifecycle guards
- `NO_VERIFICATION_CAPABILITY` — no verified tester/reviewer candidate

Retry honestly with a new `task_id`; the loop guard rejects reruns of the same
stage on the same task.

## Runtime-neutral design

No runtime, provider, or model name is hardcoded anywhere in the engine.
Concrete adapters (for example the Claude Code CLI adapter) are individual
implementations of the adapter contract (`references/adapter-contract.md`) —
Claude Code is *an* adapter, not *the* runtime. Adding a runtime means
implementing the adapter protocol, not modifying the orchestrator.

## Provenance: OFFLINE vs REAL

Every validation result carries `provenance`:

- `OFFLINE` — produced by mocks/injected executors; useful for contract
  verification, and **not** evidence of real capability.
- `REAL` — produced only under the explicit real-validation gate with real
  invocation evidence.

Offline verification is not real verification. The engine structurally
prevents upgrading one to the other.

## Security boundary

- Raw stdout/stderr, secrets, and model reasoning never enter packets, the
  ledger, or public results; `content_safety` is the single scan authority.
- The engine never reads, stores, prints, or modifies credentials, never logs
  in or out, and never touches runtime configuration.
- Real runtime invocation is opt-in and off by default
  (`RUN_REAL_PROVIDER_TESTS=1` gates the real tests).
- The CLI output is a closed allow-list summary only.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `{"error": "no facade configured"}` | Expected when no host injected a facade; configure one (see CLI section) |
| Everything reports `NO_VERIFICATION_CAPABILITY` / no candidates | No runtime has verified capability evidence yet; runtimes must pass the gated validation chain before selection |
| `LOOP_GUARD_REJECTED` on retry | Same task + same stage was already run; use a new `task_id` |
| Tests show `skipped=7` | The 7 real-runtime test entries are opt-in only; set `RUN_REAL_PROVIDER_TESTS=1` to run them (they invoke a real runtime) |

## Development and tests

```bash
python -m unittest discover -s tests      # full offline suite
python -m compileall dual-agent-development/scripts tests
```

The engine is pure standard library. Every phase of V2 was built test-first;
the suite covers the contract, transport, ledger, orchestration, facade, CLI,
security hardening, and productization.
