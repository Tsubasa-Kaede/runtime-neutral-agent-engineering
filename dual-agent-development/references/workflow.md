# V2 Workflow Contract

This document describes what the V2 engine actually does. The source code is
the single source of truth; if this document and the code disagree, the code
wins.

## 1. Task and Mode Gate

A run starts with a `task_id`, a task text, and a mode:

- `OFF` — no orchestration; the run delegates and returns the empty result.
- `AUTO` — the task is classified (`SIMPLE`, `MEDIUM`, `COMPLEX`, `UNRESOLVED`)
  by keyword rules. SIMPLE/MEDIUM/UNRESOLVED take the single-agent path;
  COMPLEX takes the dual-agent path.
- `ON` — the dual-agent path is forced, regardless of classification.

## 2. Runtime Discovery, Health, and Selection

The engine is runtime-neutral. Runtimes are discovered, health-checked
(READY / AUTH_REQUIRED / UNAVAILABLE / ERROR), and selected by *verified
capabilities* — never by name. A runtime enters the verified pool only after
the gated validation chain; offline mock verification and real runtime
verification are distinguished by `provenance` (OFFLINE / REAL). REAL requires
explicit opt-in and real invocation evidence; it can never be asserted by a
caller string alone.

## 3. The Four Stages

| Stage | Produces | Consumes |
|---|---|---|
| architect | `ArchitecturePacket` | the task |
| coder | `ImplementationPacket` | the `ArchitecturePacket` wire |
| tester | `TestPacket` | the latest `ImplementationPacket` |
| reviewer | `ReviewPacket` | architecture + implementation + test packets |

Every stage output is a frozen, secret-scanned packet (`templates/` shows the
exact fields). A stage never sees another stage's raw output.

## 4. CollaborationPacket Envelope and the Ledger

Each stage handoff is wrapped in a `CollaborationPacket` envelope
(`correlation_id`, `task_id`, source/target agents and roles, `payload_type`,
`acceptance_criteria`, `protocol_version`, `provenance`) and recorded in the
append-only Shared Collaboration State:

- `task_id` identifies the whole task lifecycle (one budget, one guard).
- `correlation_id` links exactly one request/reply hop; each hop mints a new
  one (architect↔coder share one; tester and reviewer hops each get their own).
- `sequence` is per-task, dense, and assigned by the ledger — callers cannot
  inject it.
- Records are DECISION / REQUEST / REPLY / FAILURE; history is immutable and
  stored as canonical wire text, so later mutation of packet objects can never
  rewrite history.

Later stages read upstream facts from the ledger via the `handoff_input_for`
projection (keyed by `payload_type`, latest-by-sequence, fresh decoded copies).

## 5. Budget and LoopGuard

One `TaskBudget`/`BudgetUsage` and one `LoopGuard` are shared across the whole
task lifecycle. Each real stage invocation reserves exactly one call
(architect / coder / test / review buckets). Exhausted budget is terminal.
The guard keys on (task_id, stage, role-qualified agent address); a rerun of
the same stage on the same task is rejected — retry honestly with a new
task_id.

## 6. Failure Propagation

Failures are structured and terminal; downstream stages do not run after an
upstream failure:

- `*_INVOKE_FAILED`, `*_PACKET_INVALID` — runtime or contract failure of a
  stage; the chain stops.
- `MISSING_HANDOFF` — a required upstream packet is absent from the ledger.
- `BUDGET_EXHAUSTED`, `LOOP_GUARD_REJECTED` — lifecycle guards; terminal.
- `NO_VERIFICATION_CAPABILITY` — the dual path succeeded but no verified
  tester/reviewer candidate exists; reported honestly, never a silent
  two-stage success.

Nothing is success-wrapped and nothing falls back silently.

## 7. Provenance

`provenance` is OFFLINE or REAL, copied verbatim from envelopes, and can never
be upgraded by the transport, the ledger, or a caller. REAL exists only on
results produced under the explicit real-validation gate with real invocation
evidence.

## 8. ProductionFacade and CLI

`ProductionFacade` is the single production entrypoint for the four-stage
chain: it routes via the orchestrator (architect+coder) and, only after a dual
success, drives verification (tester+reviewer) with the same shared budget,
guard, and ledger. It returns one closed `FacadeResult` — raw outcomes,
traces, and envelope wire never reach the caller. Do not bypass it by wiring
the internal orchestrator/session/verification components yourself unless you
are building a new host integration.

The CLI (`dual-agent run --mode off|auto|on "<task>"`) parses arguments and
prints the closed JSON summary only. The facade must be injected by the host
application; the CLI never constructs runtimes, adapters, credentials, or a
default facade.
