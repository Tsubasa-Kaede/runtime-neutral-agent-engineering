# Runtime-Neutral Agent Engineering

> The engineering layer between Agents and the runtimes they depend on.

**Discover capabilities. Verify execution. Control collaboration.**

Coding-agent CLIs are powerful — but building a product on top of one leaves
you guessing. Is the runtime installed? Is it healthy *right now*? Has it
actually **proven** it can architect, code, test, and review — or only
claimed it? What happens when an invocation fails mid-task, the budget runs
out, or two agents need to hand work to each other without dumping raw
transcripts?

This project is the engineering layer that answers those questions. It sits
between your application (the host) and the coding-agent CLIs (the
runtimes): it discovers runtimes, checks their health, qualifies their
capabilities behind real gates, admits them to a verified pool, and
orchestrates their collaboration under budget and loop protection.

**It is not** a chatbot, a model provider, a single-runtime wrapper, a
remote agent network, or an A2A implementation.

- **Runtime-neutral** — no runtime, provider, or model name is hard-coded in
  the engine; runtimes plug in through an adapter contract.
- **Verification-first** — a runtime is selected by *verified capability*,
  never by name.
- **Controlled execution** — budget is reserved before every invoke, loop
  protection runs before any spend, and failures are structured and
  terminal; nothing is wrapped as success.
- **Structured agent collaboration** — architect → coder → tester →
  reviewer exchange validated packets over an append-only ledger, never raw
  output.

## Why This Exists

Agent systems are increasingly capable — but their reliability still
depends on the runtime layer beneath them. Different CLIs, model providers,
execution environments, and local configurations expose different
capabilities and different levels of reliability.

Depending directly on a specific runtime creates practical, unanswered
questions:

- **Existence and health.** A runtime that worked yesterday may be missing,
  logged out, or broken today. Hoping is not a deployment strategy.
- **Unverified capability.** "It usually writes good code" is not a
  contract. Without gated evidence you cannot know what a runtime can
  *prove* it does.
- **Runaway cost.** Retry loops and repeated failures burn invocations with
  nobody accounting for them.
- **Opaque collaboration.** Multi-stage work degrades into chat logs: raw
  output flows between stages with no validation, no contract, no audit
  trail.
- **Runtime lock-in.** Most wrappers hard-code one CLI; switching or adding
  a runtime means rewriting the orchestrator.

This project treats those questions as engineering problems. It answers
three of them directly:

- **What runtimes are actually available?**
- **What capabilities have they actually proven?**
- **Which runtime is safe to admit for execution?**

The goal is not another agent or model provider. The goal is to make agent
execution **discoverable, verifiable, controllable, and runtime-neutral**.

## What It Does

Given a task, the engine runs one controlled lifecycle:

```text
Classification (SIMPLE / MEDIUM / COMPLEX / UNRESOLVED)
  ↓
Discovery ──────── does the runtime exist?
  ↓
Health ─────────── is it READY right now?
  ↓
Capability ─────── what has it PROVEN it can do?
  ↓
Qualification ──── one sanctioned validation run (G1–G14)
  ↓
Verification ───── VERIFIED + REAL evidence
  ↓
Admission ──────── Verified Runtime Pool entry
  ↓
Execution ──────── architect → coder → tester → reviewer
  ↓
Collaboration ──── structured packets, shared ledger
```

In short:
**Discovery → Health → Capability → Qualification → Verification →
Admission → Execution → Collaboration.**

Per task, the engine classifies complexity, routes simple work to a single
agent and complex work through the four-stage path, enforces one
task-lifecycle budget and loop guard, and reports a closed, secret-free
summary — including honest failure categories when things go wrong.

## Architecture

### Runtime Lifecycle

Three vocabularies, three ownership layers — no layer answers for another:

| Layer | States | Question |
|---|---|---|
| Discovery | `DISCOVERED` / `NOT_FOUND` | does the runtime exist? |
| Health | `READY` / `AUTH_REQUIRED` / `UNAVAILABLE` / `ERROR` | is it usable right now? |
| Validation | `VERIFIED` / `BLOCKED` / `FAILED` / `NOT_VERIFIED` | did it pass the gates? |

The distinctions the engine never blurs:

| Distinction | Meaning |
|---|---|
| Discovery ≠ Health | a discovered runtime may not be healthy; `DISCOVERED` is never treated as `READY` |
| Health ≠ Qualification | `READY` asserts nothing about validation evidence |
| Qualification ≠ Verification | a qualification run produces a result; `VERIFIED` is the outcome of one full gated pass |
| Verification ≠ Admission | `VERIFIED` alone does not enter the pool — admission also requires the required capability subset, `READY` health, and no duplicate |
| READY ≠ VERIFIED | health is a renewable state; verification is earned evidence — neither implies the other, in either direction |

### ReadyPool Path vs Verified Path

Two parallel paths; the entry point decides which one runs:

```text
ReadyPool path (classic engine)          Verified path (production stack)
──────────────────────────────           ───────────────────────────────
Runtime                                  Runtime
 → Health                                 → Discovery
 → Capability (registry evidence)         → Health
 → ReadyPool (runtime_pool)               → Capability (gate evidence)
 → CapabilityRegistry selection           → Qualification (G1–G14, gated)
 → ExecutionEngine                        → Verification (VERIFIED + REAL)
                                          → VerifiedRuntimePool admission
                                          → Verified selection (score-less)
                                          → Execution (never falls back)
```

The classic path admits on health and scores candidates from registry
evidence. The production path requires gated qualification, `VERIFIED` +
`REAL` evidence, and Verified Runtime Pool admission before execution — and
never falls back.

Load-bearing invariant: **the Verified path never silently borrows the
ReadyPool.** This is structural — an empty verified selection normalizes to
`NO_CAPABLE_AGENT` instead of consulting the ready-pool registry, and the
verified orchestrator executes with an empty fallback policy.

### Task Lifecycle

One `ProductionFacade` owns exactly one task lifecycle:

```text
Task 1 → Facade 1 → done          Task 2 → Facade 2 → done
```

- Budget, loop guard, and ledger are per-task and never reset between runs.
- SINGLE path: at most 1 real agent invocation (one coder call).
- FOUR_STAGE path: at most 4 (architect, coder, tester, reviewer — each
  exactly once). Beyond that: `BUDGET_EXHAUSTED`; a new task needs a new
  facade.
- Verified evidence is reused across tasks: one sanctioned REAL
  qualification admits a runtime to the pool for many facades — runtimes
  are never re-qualified per task.

## What Makes It Different

### Runtime-Neutral

The engine core contains no runtime, provider, or model names anywhere.
Concrete adapters (for example the Claude Code CLI adapter) are individual
implementations of the adapter contract
([`dual-agent-development/references/adapter-contract.md`](dual-agent-development/references/adapter-contract.md)):
Claude Code is *an* adapter, not *the* runtime. Adding a runtime means
implementing the adapter protocol — never modifying the orchestrator.

### Verification-First

Selection works on verified capability, never on names. Capabilities are
built only from structured gate evidence; a candidate's *declared*
capabilities are never promoted into verified ones. In the evidence
hierarchy, DECLARED never counts as VERIFIED.

### Controlled Execution

Every guard runs **before** money or invocations can be spent. The budget
reserves an invocation slot before the adapter is called
(reserve-before-invoke), and the loop guard pre-checks duplicates, repeated
failures, and cycles. Failures are structured and terminal — upstream
failure stops downstream stages, nothing is packaged as success, and the
verified path has no silent fallback.

### Structured Collaboration

Stages exchange validated packets over an append-only shared ledger — never
raw model output. A stage's input is always an upstream *packet*; raw
invocation output must pass the packet contract and content scanning before
it reaches the next stage.

## Core Capabilities

| Component | Responsibility |
|---|---|
| Runtime Adapter Registry | registers adapters satisfying the adapter contract |
| Runtime Discovery | existence: `DISCOVERED` / `NOT_FOUND` (controlled result, never an exception) |
| Runtime Health | moment-of-call state: auth, provider/model, minimal probe |
| Capability Registry | evidence-backed capability records |
| Capability Validation | `validated_capabilities` built only from gate evidence |
| Qualification G1–G14 | the one sanctioned validation run, double-gated |
| Verified Runtime Pool | admission for VERIFIED + REAL runtimes only |
| Verified Selection | score-less selection from the verified pool |
| Budget | per-task invocation accounting (reserve-before-invoke) |
| LoopGuard | duplicate / repeated-failure / cycle protection before spend |
| Collaboration Packet | the protocol contract between stages |
| Local Collaboration Transport | in-process delivery mechanism |
| Collaboration Ledger | append-only shared record of handoffs |
| Production Facade | per-task engine surface for host applications |
| Host / CLI Integration | facade injection plus the `dual-agent` CLI |
| REAL Runtime Validation | gated, evidence-carrying real-call validation |
| Cross-platform CI | Ubuntu / Windows / macOS × Python 3.10 / 3.11 / 3.12 |

## Collaboration

Four stages, four contracts:

```text
Architect
    ↓  ArchitecturePacket
Coder
    ↓  ImplementationPacket
Tester
    ↓  TestPacket
Reviewer
    ↓  ReviewPacket
```

| Role | Reads | Produces |
|---|---|---|
| architect | the task itself | `ArchitecturePacket` |
| coder | architecture packet wire text | `ImplementationPacket` |
| tester | latest implementation packet | `TestPacket` |
| reviewer | architecture + implementation + test | `ReviewPacket` |

Two layers that are easy to conflate but are not the same:

- **`CollaborationPacket` is the protocol contract** — who owes what work,
  on a frozen envelope schema.
- **Transport is the delivery mechanism** — an in-process mailbox today.

V2 contains **no** Remote Agent Network, no A2A protocol, no distributed
execution, and no multi-agent network. The remote transport module defines
the boundary contract with a loopback implementation only.

## Runtime Verification

### Capability Validation

`validated_capabilities` is built **only** from structured gate evidence
(the four role experiments of G14). A candidate's declared capability
context is never promoted into it: DECLARED never becomes VERIFIED. Pool
admission checks that the required capabilities are a subset of the
validated ones.

### Provenance

Every validation result carries a `provenance`:

- `OFFLINE` — produced by mock / injected executors; valid for contract
  verification, **not** evidence of real capability.
- `REAL` — produced only under the explicit real-validation gate
  (`RUN_REAL_PROVIDER_TESTS=1`) with real-call evidence.

**Offline validation is not REAL validation.** The runner refuses to grant
`REAL` without real-call evidence, so the two cannot be swapped.

### REAL Runtime Validation

Verified facts, not aspirations (measured 2026-08-25 on the reference
machine; gated test: `tests/test_rc3_real_discovery.py`):

- **Claude Code CLI** (2.1.227, first-party login): the full chain is
  REAL-proven — Discovery (`FOUND`) → Health (`READY`) → REAL qualification
  → `VERIFIED` + `REAL` with all four capabilities → Verified Runtime Pool
  admission.
- **Evidence reuse**: a second bootstrap session carrying the first
  session's evidence performs zero re-qualification
  (`qualification_count = 0`).
- **G13 protected paths**: all five declared protected files
  (credentials / config) showed `diff = 0` across every real call.
- **Codex CLI**: not installed on the reference machine — no claims made.
- **tiny-agents**: executable present but unconfigured
  (`TINY_AGENTS_AGENT_PATH` / `TINY_AGENTS_COMMAND` unset) → not
  registered; honest absence, not failure.

Offline baseline at commit `2fa1ab2`: 945 passed / 21 skipped (all skips
are opt-in REAL-gated tests) / 377 subtests. See the latest CI run for the
authoritative test result.

## Safety & Control

### Security Boundaries

- **No-secrets contract**: raw output, secrets, and model reasoning never
  enter packets, the ledger, traces, or public results; `content_safety`
  is the single scan authority.
- **Raw-output quarantine**: stage inputs are always upstream packets; raw
  output must pass the packet contract and content scan first.
- **Protected paths**: REAL validation snapshots caller-declared protected
  files (credentials / config); any change during the run fails G13.
- **Minimal environment**: CLI adapters start subprocesses with a
  whitelist env (`PATH` / `HOME` / `USERPROFILE` / `SYSTEMROOT`) —
  credential-bearing variables are never forwarded.
- **Safe error normalization**: adapter error text is shape-scrubbed before
  reaching traces or reports.
- The engine never reads, stores, prints, or modifies credentials; never
  logs in or out; never touches runtime configuration.
- Real runtime calls are opt-in and off by default
  (`RUN_REAL_PROVIDER_TESTS=1` gates the real tests).
- CLI output is a closed allow-list summary.

### Budget & LoopGuard

Both guards run **before** any invocation — a rejected duplicate or an
exhausted budget must never consume money or calls:

- **TaskBudget** spans one task lifecycle. Reserve-before-invoke: the slot
  is reserved before the adapter call (exhaustion raises), so a call either
  happened-and-was-paid or never happened. Token counts default to an
  honest `"unknown"` — never guessed.
- **LoopGuard** spans one task. `check()` is the pre-check
  (DUPLICATE_TASK / REPEATED_FAILURE / CYCLE_DETECTED / caps); `record()`
  completes the pair after the call. Only hashed failure *categories* are
  remembered — never raw diagnostics.

## Quick Start

Requirements: Python 3.10+ — the engine is pure standard library with zero
runtime dependencies. Runtimes are optional and bring their own
prerequisites; for example the Claude Code CLI requires Node.js, which is a
runtime-level concern, not a dependency of this package.

```bash
git clone https://github.com/Tsubasa-Kaede/agent-development.git
cd agent-development
pip install -e .
python examples/offline_mock_run.py
```

Expected output — a closed, secret-free JSON summary:

```json
{"path": "FOUR_STAGE", "status": "SUCCESS", "stages": ["architect","coder","tester","reviewer"], ...}
```

## CLI

```bash
dual-agent run --mode off  "Implement a GitHub webhook"
dual-agent run --mode auto "Implement a GitHub webhook"
dual-agent run --mode on   "Implement a GitHub webhook"
```

| Mode | Behavior |
|---|---|
| `OFF` | no orchestration; returns the delegated empty result |
| `AUTO` | classify the task; SIMPLE / MEDIUM / UNRESOLVED take the single-agent path, COMPLEX takes the dual-agent path |
| `ON` | force the dual-agent path (architect + coder; tester + reviewer when qualified candidates exist) |

Without verified tester / reviewer candidates, dual-agent success is
reported as `NO_VERIFICATION_CAPABILITY` — never a silent two-stage
success, never a fabricated four-stage success.

Honest limitation: the CLI parses arguments, invokes the **host-injected**
facade, and prints a closed JSON summary. The `ProductionFacade` must be
configured and injected by the host application — the CLI never creates
runtimes, adapters, credentials, or a default facade, never configures
providers, and never reads API keys. Without an injected facade it exits
with a clear error. A host injects like this:

```python
from dual_agent import cli
cli.main._facade = my_configured_facade   # build with your adapters/pool
```

See `examples/offline_mock_run.py` for constructing the facade from real
engine components.

**Failure semantics** — structured and terminal; downstream stages do not
run after an upstream failure:

- `*_INVOKE_FAILED`, `*_PACKET_INVALID` — a stage failed on the runtime or
  the packet contract
- `MISSING_HANDOFF` — a required upstream packet is absent from the ledger
- `BUDGET_EXHAUSTED`, `LOOP_GUARD_REJECTED` — task-lifecycle guards
- `NO_VERIFICATION_CAPABILITY` — no verified tester / reviewer candidates

Honest retries require a new `task_id`; the loop guard rejects re-running
the same stage of the same task.

## Verification

```bash
python -m pytest tests/ -q                     # offline suite + gated skips
python -m unittest discover -s tests           # equivalent stdlib runner
python -m compileall dual-agent-development    # syntax gate
```

Every layer of V2 was built test-first. REAL-runtime tests are opt-in
(`RUN_REAL_PROVIDER_TESTS=1`; they invoke real runtimes) and skipped by
default. Cross-platform CI runs the offline matrix on Ubuntu, Windows, and
macOS across Python 3.10 / 3.11 / 3.12 — see the latest CI run for the
authoritative result.

## V2 Foundation

What V2 delivers today:

- The full chain, implemented and offline-tested: Discovery → Health →
  Capability → Qualification → Verification → Admission → Execution →
  Collaboration
- Four-stage structured collaboration with packets, ledger, and transport
- Two execution paths (ReadyPool classic engine; Verified production stack)
  with the no-silent-borrowing invariant
- Adapter contract plus a REAL-proven Claude Code CLI adapter
- Production Facade and host / CLI integration
- Cross-platform CI (3 OS × Python 3.10 / 3.11 / 3.12)
- MIT license

## What V2 Does Not Include

- No Remote Agent Network, A2A protocol, distributed execution, or
  multi-agent network — the remote transport module is a loopback boundary
  contract only
- No model provider or inference — it orchestrates your existing agent
  CLIs
- No automatic runtime login, logout, or configuration — credentials are
  yours, and the engine never touches them
- The task classifier is a closed keyword table, not a model; tasks with no
  keyword hit classify as UNRESOLVED and take the orchestration path
- Nothing from the V3 roadmap (below)

## V2 → V3 Roadmap

V2 asks *"which runtime can execute this task?"*. V3 asks *"which agent is
best suited for this task?"* — the runtime becomes one execution capability
of an agent. V3 is an **evolution of V2, not a replacement**:
contract-first, verification-first, and minimal-context-transfer principles
carry over.

| V2 | V3 |
|---|---|
| Runtime | Agent identity |
| Runtime Discovery | Agent discovery |
| Capability | Agent capability |
| Verification | Trust / admission |
| Local collaboration | Remote collaboration |

| Stage | Theme | Status |
|---|---|---|
| V3.0 | Agent Foundation — identity, manifest, discovery, capability, contract, verification, trust, admission | NOT IMPLEMENTED |
| V3.1 | Remote Collaboration — remote agents, artifact-based exchange, context isolation, authN/authZ | NOT IMPLEMENTED |
| V3.2 | Multi-Agent Orchestration — task decomposition, scheduling, workflows, failure recovery | NOT IMPLEMENTED |
| V3.5+ | Agent Network — pools, dynamic selection, reputation, marketplace | NOT IMPLEMENTED |

Full design goals and the ten inheritance principles:
[docs/roadmap/v2-to-v3.md](docs/roadmap/v2-to-v3.md).

## Documentation

```text
docs/
├── architecture/
│   ├── overview.md           # full architecture and module map
│   ├── runtime-lifecycle.md  # Discovery / Health / Qualification / Verification / Admission
│   ├── ready-vs-verified.md  # dual paths and the no-silent-borrowing invariant
│   ├── execution.md          # Health → Guard → Handoff → Budget → Reserve → Invoke gate chain
│   └── collaboration.md      # packets, contracts, transport, sessions, handoffs
├── development/
│   ├── getting-started.md    # structure, environment, install, first tests
│   ├── development-guide.md  # contract-first, boundary-first workflow
│   ├── testing.md            # unit / integration / E2E, OFFLINE vs REAL
│   └── real-runtime.md       # the gated Registry → … → Admission chain and RC-3 proof
└── roadmap/
    └── v2-to-v3.md           # V3 design goals — agent-centric evolution (not implemented)
```

Skill-facing assets: `dual-agent-development/SKILL.md`,
`dual-agent-development/references/`, `dual-agent-development/templates/`.

## Known Limitations

- On the reference machine only **one runtime** (Claude Code CLI) holds
  REAL-proven capability evidence; other adapters exist but are unproven
  there.
- Task classification is a closed keyword table — not a model.
- The dual-agent path covers architect + coder; tester + reviewer run as
  verification stages gated on dual-agent success (otherwise
  `NO_VERIFICATION_CAPABILITY`).
- Qualification is a point-in-time proof; stability over N runs is a
  separate measurement (sampled, not guaranteed).
- No remote collaboration in V2 — see the roadmap.

## License

MIT — see [LICENSE](LICENSE).
