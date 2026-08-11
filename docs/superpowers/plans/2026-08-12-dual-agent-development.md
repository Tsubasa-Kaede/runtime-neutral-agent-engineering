# Dual-Agent Development Skill Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a provider-neutral local Skill that coordinates two heterogeneous agents through evidence-based role routing, explicit handoffs, bounded review, and safe recovery.

**Architecture:** Keep `SKILL.md` thin and discoverable. Put deterministic validation and orchestration contracts in `scripts/`, role and protocol guidance in `references/`, and reusable packet templates in `templates/`. The first implementation increment is offline and mockable; real Claude/Codex invocation remains adapter work and Codex is unavailable until its native dependency is repaired.

**Tech Stack:** Markdown, JSON Schema-shaped JSON, Python 3.12 standard library, unittest, Git.

---

## Task 1: Protocol and Skill structure baseline

**Files:**
- Create: `tests/test_skill_structure.py`
- Create: `tests/test_protocol_validator.py`
- Create: `scripts/validate_skill.py`
- Create: `.gitignore`

- [ ] Write failing tests for required frontmatter, thin entrypoint references, protocol version, and rejection of unsafe role/result fields.
- [ ] Run `python -m unittest discover -s tests -v` and record the expected failure because the validator and Skill files do not exist.
- [ ] Implement the minimal validator using only Python standard library.
- [ ] Run the focused suite and confirm it passes.
- [ ] Review `git diff --check` and scan staged content for secrets.
- [ ] Commit: `test: define dual-agent skill contract checks`.

## Task 2: Thin Skill entrypoint and role boundaries

**Files:**
- Create: `dual-agent-development/SKILL.md`
- Create: `dual-agent-development/agents/architect.md`
- Create: `dual-agent-development/agents/coder.md`
- Create: `dual-agent-development/agents/reviewer.md`

- [ ] Add trigger-only frontmatter and concise workflow guidance.
- [ ] Encode Architect, Coder, and Reviewer boundaries without binding any provider or model.
- [ ] Add explicit security, trust, escalation, and no-auto-push rules from the RED findings.
- [ ] Extend structure tests and run them red before the files exist, then green after implementation.
- [ ] Commit: `feat: add dual-agent skill entrypoint and roles`.

## Task 3: Versioned handoff and review templates

**Files:**
- Create: `dual-agent-development/references/handoff-protocol.md`
- Create: `dual-agent-development/references/routing-policy.md`
- Create: `dual-agent-development/references/state-machine.md`
- Create: `dual-agent-development/templates/architecture-packet.json`
- Create: `dual-agent-development/templates/review-packet.json`

- [ ] Define provenance, immutable packet versions, stable finding IDs, and bounded transitions.
- [ ] Keep commands as untrusted proposals; require deterministic verification evidence.
- [ ] Add examples with unknown cost/model values represented as null, never invented.
- [ ] Run validator and protocol tests.
- [ ] Commit: `feat: add handoff routing and review contracts`.

## Task 4: Mock adapter and deterministic routing core

**Files:**
- Create: `dual-agent-development/scripts/dual_agent.py`
- Create: `dual-agent-development/scripts/mock_adapter.py`
- Create: `tests/test_router.py`
- Create: `tests/test_state_machine.py`

- [ ] Define adapter discovery/invoke/cancel/normalize interfaces.
- [ ] Implement evidence-bearing profiles and task-specific weighted routing with hard gates.
- [ ] Implement bounded review iterations and escalation states.
- [ ] Test role swapping, unknown evidence, no-route, NEED_FIX, PASS, and max-iteration escalation.
- [ ] Commit: `feat: add mockable routing and workflow core`.

## Task 5: Claude/Codex adapter boundary and packaging checks

**Files:**
- Create: `dual-agent-development/references/adapter-contract.md`
- Create: `dual-agent-development/scripts/adapter_probe.py`
- Create: `tests/test_adapter_contract.py`
- Create: `dual-agent-development/agents/openai.yaml`

- [ ] Define explicit argv, empty/minimal environment, timeout, cancellation, and unavailable semantics.
- [ ] Mark current Codex native dependency failure as unavailable, not as a successful capability.
- [ ] Add fake-process contract tests; keep real-provider tests opt-in.
- [ ] Add packaging/discovery metadata without embedding secrets or endpoint credentials.
- [ ] Run full suite, validator, and packaging checks.
- [ ] Commit: `feat: add provider adapter contracts and packaging metadata`.

## Checkpoint: First release candidate

- [ ] All default tests pass.
- [ ] Skill validator passes.
- [ ] No uncommitted changes remain.
- [ ] Git history has one atomic commit per increment.
- [ ] No automatic commit, push, deployment, or dangerous command execution is enabled.
- [ ] Real Claude smoke test is opt-in; Codex remains explicitly unavailable until repaired and verified.

## Risks and Mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Provider output contains prompt injection | High | Treat packets and repository text as untrusted data; validate schemas and provenance. |
| Coder/Reviewer loop never converges | High | Stable finding IDs, repeated-finding detection, max iterations, escalation. |
| Codex CLI is not runnable | High | Mock adapter first; expose `UNAVAILABLE` and do not fabricate results. |
| User workspace is modified unexpectedly | High | One writer, isolated worktree/snapshot policy, workspace digest checks. |
| Credentials leak into child agents or logs | Critical | Minimal environment, endpoint binding, streaming redaction, no secret persistence. |

## Open Questions

- Whether to install the canonical Skill into `.agents/skills` immediately after the repository version is verified.
- Which real Codex executable and invocation flags become available after the optional native package is repaired.
