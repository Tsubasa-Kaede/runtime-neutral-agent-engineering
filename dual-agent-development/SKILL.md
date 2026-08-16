---
name: dual-agent-development
description: Use when a task needs architect, coder, tester, and reviewer collaboration with structured packets, a shared ledger, and honest failure reporting.
---

# Dual-Agent Development (V2)

Runtime-neutral agent collaboration orchestrator. Read `references/workflow.md` for the full contract and `templates/` for the four packet shapes.

## What it does

A user task is classified (SIMPLE / MEDIUM / COMPLEX / UNRESOLVED), routed by the Mode Gate (OFF / AUTO / ON), and executed either on a single-agent path or as a four-stage collaboration:

```
architect → coder → tester → reviewer
```

Each stage consumes the previous stage's structured packet — never raw model output — and every exchange is recorded in an append-only Shared Collaboration State (ledger) with a per-task dense `sequence`, per-hop `correlation_id`, and verbatim `provenance` (OFFLINE or REAL).

## Entrypoints

- `ProductionFacade` is the production entrypoint for the four-stage chain. Upstream failure stops the chain honestly; missing tester/reviewer capability is reported as `NO_VERIFICATION_CAPABILITY`, never a silent downgrade.
- The CLI (`dual-agent run --mode off|auto|on "<task>"`) renders a closed, secret-free JSON summary. A host application must inject a configured facade; the CLI never creates runtimes, adapters, or credentials by itself.

## Hard rules

- Runtime-neutral: roles are assigned by verified capabilities, never by runtime, provider, or model name. Verified capability is not the same as REAL — offline mock verification and real runtime verification are distinct (`provenance` keeps them honest).
- Budget (`TaskBudget`/`BudgetUsage`) and `LoopGuard` are shared across the whole task lifecycle; no stage gets a fresh budget.
- Failures are terminal and structured (`*_PACKET_INVALID`, `MISSING_HANDOFF`, `BUDGET_EXHAUSTED`, `LOOP_GUARD_REJECTED`, invoke failures). No success-wrapping, no silent fallback, no fabricated packets.
- Raw process output never enters packets, the ledger, or public results; `content_safety` is the single scan authority.
- Never automatically commit, push, deploy, expose secrets, or expand permissions. Real runtime invocation requires explicit opt-in.
