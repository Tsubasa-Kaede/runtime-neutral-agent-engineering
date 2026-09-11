# Post-Release Validation Protocol

Status: active · Current release: **2.3.0** (RELEASED) · Next release:
**2.4.0** (IN PREPARATION) · V3.2: **IMPLEMENTED — SHIPS IN 2.4.0**

## 1. Current state

- Release **2.3.0** (tag `v2.3.0`, commit `2dc0dda`) is published:
  PyPI, GitHub Release, CI 9/9 green, fresh-install verified.
- **2.4.0 is in preparation**: release metadata committed; the V3.2
  Release-Gate review passed (2026-09-11, ready with documented
  non-blocking concerns); push, tag, and PyPI publish each await their
  separate authorizations.
- **V3.2 is implemented and ships in 2.4.0**: control, observation,
  revision, composition, sequential orchestration, and the
  `dual-agent cockpit` CLI entry, completed through the authorized CU
  chain (2026-09-10/11). Sections 3–11 below record the observation
  protocol as it governed the 2.3.0 window that preceded that chain.

## 2. Purpose

Post-release validation does not exist to prove the project can keep
developing. It exists to answer one question:

> Have real users produced needs that the current architecture does
> not satisfy?

## 3. Observation signals (minimal, manual)

- **GitHub** — stars, forks, issues, discussions (if any),
  traffic/clones (if available). Interest signals only; not V3.2
  triggers.
- **PyPI** — download trend. An adoption signal; alone it proves
  nothing about V3.2 demand.
- **Real usage** — any real case where someone completed actual remote
  collaboration with 2.3.0; any multi-agent workflow need.
- **User feedback** — installation problems, Quick Start friction,
  REAL runtime problems, Remote Collaboration usage problems, API/UX
  friction, multi-agent needs, capability-selection needs,
  orchestration needs.

No automated tracking is built for any of this. GitHub/PyPI UIs and
issues are the tools.

## 4. Feedback classification

| Class | Meaning | Route |
|---|---|---|
| A — Bug | A documented capability does not work as documented | Maintenance CU |
| B — Product/UX friction | The capability works, but first use, docs, or API cause real friction | Record evidence; open a Maintenance/Product CU only if the impact is clear |
| C — V3.1 gap | Inside V3.1's declared scope, real use exposed an uncovered problem | Separate audit — never jump straight to V3.2 |
| D — V3.2 demand | Real use requires coordinating multiple agents (see §5) | V3.2 Discovery |
| E — Architecture problem | Feedback shows an existing seam cannot carry the need | Architectural design — no direct code |
| F — No action | Stars/downloads/general interest without a concrete need | Keep observing |

## 5. V3.2 trigger rules

Do **not** trigger V3.2 on any of these alone: star growth, fork
growth, download growth, "cool project", "hope you support more
agents", a maintainer's own new idea, the roadmap numbering, or the
desire to demonstrate an orchestrator.

Entering **V3.2 Discovery** requires at least one of:

1. A real user explicitly asks for multi-agent orchestration.
2. In real work, manually coordinating multiple agents has become
   clearly repetitive labor.
3. At least one real task reasonably requires multiple agents
   cooperating to complete.
4. A real user explicitly needs capability-based selection, task
   decomposition, or a deterministic workflow.
5. Multiple independent feedback items point at the same orchestration
   problem.

"Trigger" means entering V3.2 Discovery/Brainstorming — never direct
implementation.

## 6. V3.2 decision gate

```text
Evidence → Demand → Problem Definition → V3.2 Boundary →
Architecture/Brainstorming → Design Approval → Implementation
```

`Idea → Implementation` is forbidden.

## 7. V3.2 initial boundary (candidates only — not an approved design)

If V3.2 ever opens, the candidate core is a **first multi-agent
orchestrator** consuming existing seams: AgentIdentity,
AgentRuntimeBinding, AgentManifest/Registry, the capability join,
RemoteAgentComposition, RemoteAgentSession/Endpoint, and the V3.1
packet/transport contract.

Candidate capabilities: deterministic multi-agent workflow, role
assignment, capability-aware selection, task decomposition, dependency
ordering, result aggregation, team-level failure presentation.

This is a future candidate boundary, not a current implementation
plan.

## 8. V3.2 non-goals (still)

Not to be implemented ahead of real demand and separate design:
cross-machine networking, authentication/authorization,
retry/reconnect infrastructure, cancellation propagation, distributed
scheduler, dashboard, cloud, marketplace, telemetry platform, global
capability marketplace, autonomous planner, arbitrary DAG engine.

## 9. Observation cadence

Manual and lightweight: review once after the 2.3.0 release window,
then whenever a batch of real feedback arrives, or after a reasonable
gap with a light pass. A date on the calendar never creates a
development task by itself.

## 10. Evidence record (per real feedback item)

Date · Source · Observed behavior · User & task · Evidence ·
Classification (A–F) · Impact · Potential CU · V3.2 relevance ·
Decision.

Kept in Markdown or GitHub issues — no database, no telemetry, no
tracking system.

## 11. Review outcomes

Every review ends with exactly one of: CONTINUE OBSERVATION,
MAINTENANCE CU, PRODUCT/UX CU, V3.1 GAP AUDIT, V3.2 DISCOVERY,
ARCHITECTURAL RE-DESIGN. "V3.2 DISCOVERY" is not "V3.2
IMPLEMENTATION".

## 12. Existing debt (separately triaged)

The post-release audit's debt map is advisory and does not
automatically create implementation work. Known items, untouched by
this protocol: diagnostics public read surface, historical share
data-file behavior, the v2.2.1 GitHub Release gap, E/F spec/plan
tracking, PyPI metadata, stale dist artifacts.
