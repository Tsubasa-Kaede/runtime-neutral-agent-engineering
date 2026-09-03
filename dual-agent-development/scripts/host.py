"""Minimal host integration — the composition root (RC-2A).

Thin wiring between a user entrypoint and the ALREADY-VERIFIED engine: one
adapter + one sanctioned validation result are composed into a ProductionFacade
with a REAL VerifiedOrchestrator (SINGLE path), the collaboration stack
(FOUR_STAGE path), and the shared budget/usage/guard lifecycle. The host
implements no orchestration of its own: no agent selection, no scoring, no
packet routing, no qualification logic, no retry, no fallback.
"""
from __future__ import annotations

import json
from dataclasses import replace
from typing import Any, Mapping

from collaboration_orchestrator import CollaborationOrchestrator
from collaboration_session import CollaborationSession, collab_agent_address
from external_runtime import InvocationStatus
from mode_gate import Mode
from production_facade import ProductionFacade
from remote_transport import LoopbackRemoteTransport
from runtime_status import RuntimeStatus
from task_budget import BudgetUsage, TaskBudget
from loop_guard import LoopGuard
from verified_orchestrator import VerifiedOrchestrator
from verified_selection_bridge import agent_id_for
from verified_runtime_pool import VerifiedRuntimePool

_CAPS_ALL = ("architecture", "coding", "review", "testing")

# R7-A2 deployment default: the host-side desired minimum of distinct
# runtimes for CLI-driven runs. A deployment constant ONLY — it lives here
# (and is re-used by the CLI), never inside the runtime-neutral policy
# core, and it never expands the pool: with a single admitted runtime the
# policy honestly reports POLICY_COUNT_UNSATISFIED.
DEFAULT_MIN_DISTINCT_RUNTIMES = 2


_SINGLE_CODER_INSTRUCTION = (
    "You are the coder for one small, read-only task. "
    "Return ONLY a JSON object with exactly these keys: "
    "task_id, role, changed_files, implementation_summary, "
    "implementation_details, assumptions, unresolved_items, "
    "test_requirements. role must be \"coder\". changed_files, "
    "implementation_details, assumptions, unresolved_items and "
    "test_requirements must each be a JSON array (use [] when empty); "
    "never a number or a bare string. No prose, no markdown fences. "
    "Do not modify files or run commands.\n\nTask: "
)


class _ParsedPacketAdapter:
    """Composition seam for the SINGLE executor (phase-9 E2E precedent).

    The single-path engine forwards the caller's prompt verbatim and
    consumes dict packets directly, while the collaboration stack builds
    packet-contract prompts itself and parses JSON text — so this seam,
    applied ONLY to the single executor's adapter view, (a) embeds a
    packet-contract instruction around the raw task and (b) converts a
    successful JSON-string output into the parsed dict. Failures and
    non-string outputs pass through untouched; nothing is fabricated
    (unparseable text stays as-is and the engine reports it honestly)."""

    def __init__(self, inner: Any, task_id_getter=None):
        self._inner = inner

    def discover(self):
        return self._inner.discover()

    def check_authentication(self):
        return self._inner.check_authentication()

    def check_provider_model(self):
        return self._inner.check_provider_model()

    def cancel(self, invocation_id):
        return self._inner.cancel(invocation_id)

    def invoke(self, request):
        prompt = request.prompt
        if "Return ONLY a JSON object" not in prompt:
            request = replace(request, prompt=_SINGLE_CODER_INSTRUCTION + prompt)
        result = self._inner.invoke(request)
        output = getattr(result, "output", None)
        status = getattr(result, "status", None)
        if status is not InvocationStatus.SUCCESS or not isinstance(output, str):
            return result
        text = output.strip()
        if text.startswith("```"):
            first_newline = text.find("\n")
            text = text[first_newline + 1:] if first_newline != -1 else ""
            if text.rstrip().endswith("```"):
                text = text.rstrip()[:-3]
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError):
            return result
        if isinstance(parsed, dict):
            parsed = dict(parsed)
            parsed["task_id"] = request.task_id
            return replace(result, output=parsed)
        return result


class HostFacade(ProductionFacade):
    """ProductionFacade with the run-default provenance bound to the
    qualification evidence — a caller may still pass an explicit value,
    and the label always originates from the sanctioned validation."""

    _evidence_provenance = "OFFLINE"

    def run(self, task_id, task, prompt, mode=Mode.AUTO, provenance=None,
            observation_sink=None, policy=None):
        # R7-D2: 旁路观察 sink 原样透传（默认 None = 零行为漂移）。
        return super().run(
            task_id, task, prompt, mode=mode,
            provenance=self._evidence_provenance if provenance is None
            else provenance,
            observation_sink=observation_sink, policy=policy)


def build_facade(
    adapter: Any,
    validation,
    current_health: Mapping[str, RuntimeStatus],
    *,
    timeout_seconds: float = 300.0,
    budget: TaskBudget | None = None,
    usage: BudgetUsage | None = None,
    loop_guard: LoopGuard | None = None,
) -> ProductionFacade:
    """Compose the verified engine around one qualified runtime adapter.

    `validation` must come from the sanctioned qualification path; its
    provenance becomes the default label of every run through the host so
    the CLI seam cannot mislabel a real run as OFFLINE.
    """
    identity = tuple(validation.identity)
    arch = collab_agent_address(identity, "architect")
    coder = collab_agent_address(identity, "coder")
    tester = collab_agent_address(identity, "tester")
    reviewer = collab_agent_address(identity, "reviewer")
    budget = budget or TaskBudget(4, 4, timeout_seconds=timeout_seconds)
    usage = usage or BudgetUsage()
    guard = loop_guard or LoopGuard()

    pool = VerifiedRuntimePool(clock=lambda: 0.0)
    pool.admit(validation, _CAPS_ALL, health_now="READY")

    # SINGLE/OFF route: a REAL VerifiedOrchestrator over the same pool —
    # the host never stubs the engine it hands to the user. The executor's
    # adapter view goes through the parsing seam (dict packets); the
    # collaboration stack below keeps the raw wire-text adapter.
    verified_orchestrator = VerifiedOrchestrator(
        pool, current_health,
        {agent_id_for(identity): _ParsedPacketAdapter(adapter)},
        budget, usage, guard)

    def session_factory():
        return CollaborationSession(
            LoopbackRemoteTransport(),
            {arch: adapter, coder: adapter},
            budget, usage, guard)

    orchestrator = CollaborationOrchestrator(
        verified_orchestrator, pool, current_health,
        budget, usage, guard, session_factory)
    facade = HostFacade(
        orchestrator,
        {tester: adapter, reviewer: adapter},
        pool, dict(current_health), budget, usage, guard)
    facade._evidence_provenance = validation.provenance
    return facade


def build_facade_from_bootstrap(
    registry,
    *,
    evidence=None,
    qualifier=None,
    current_health,
    timeout_seconds: float = 300.0,
):
    """Automatic entry: Registry -> Discovery -> Health -> evidence
    reuse / one qualification -> Verified Pool -> HostFacade.

    Thin composition over the Task-B bootstrap and the existing manual
    build_facade — no routing knowledge lives here (who is usable, not how
    a task executes). Fails honestly when no runtime reaches admission.
    """
    from discovery_bootstrap import bootstrap_runtime_session

    session = bootstrap_runtime_session(
        registry, evidence=evidence, qualifier=qualifier,
        required_capabilities=_CAPS_ALL,
    )
    admitted = [entry for entry in session.entries if entry.admitted]
    if not admitted:
        reasons = "; ".join(
            f"{entry.runtime_id}:{entry.reason}" for entry in session.entries
        ) or "NO RUNTIMES REGISTERED"
        raise RuntimeError(f"no admitted verified runtime ({reasons})")
    entry = admitted[0]
    descriptor = registry.get(entry.runtime_id)
    validation = session.evidence[descriptor.identity]
    adapter = descriptor.adapter_factory()
    return build_facade(adapter, validation, current_health,
                       timeout_seconds=timeout_seconds)
