"""Minimal host integration — the composition root (RC-2A).

Thin wiring between a user entrypoint and the ALREADY-VERIFIED engine: one
adapter + one sanctioned validation result are composed into a ProductionFacade
with a REAL VerifiedOrchestrator (SINGLE path), the collaboration stack
(FOUR_STAGE path), and the shared budget/usage/guard lifecycle. The host
implements no orchestration of its own: no agent selection, no scoring, no
packet routing, no qualification logic, no retry, no fallback.
"""
from __future__ import annotations

from typing import Any, Mapping

from collaboration_orchestrator import CollaborationOrchestrator
from collaboration_session import CollaborationSession, collab_agent_address
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


class HostFacade(ProductionFacade):
    """ProductionFacade with the run-default provenance bound to the
    qualification evidence — a caller may still pass an explicit value,
    and the label always originates from the sanctioned validation."""

    _evidence_provenance = "OFFLINE"

    def run(self, task_id, task, prompt, mode=Mode.AUTO, provenance=None):
        return super().run(
            task_id, task, prompt, mode=mode,
            provenance=self._evidence_provenance if provenance is None
            else provenance)


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
    # the host never stubs the engine it hands to the user.
    verified_orchestrator = VerifiedOrchestrator(
        pool, current_health,
        {agent_id_for(identity): adapter},
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
