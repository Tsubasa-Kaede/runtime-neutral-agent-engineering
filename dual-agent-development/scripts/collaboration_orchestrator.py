"""Collaboration orchestrator: composition routing facade (Phase 10H-E2).

Sits above the untouched single-agent stack: routing decisions come from
the existing ModeGate/classifier; SINGLE routes delegate verbatim to the
injected orchestrator, DUAL routes drive an injected collaboration
session factory over verified role candidates. The facade owns no budget,
guard or ledger of its own — it shares the injected instances and records
into the shared collaboration state. Fallback to SINGLE happens only for
dual failures with a zero usage delta (never for packet-invalid, mismatch,
budget-exhausted, or explicit ON). The dual path covers architect+coder
only; that coverage trade is recorded in every DUAL decision.
"""
from __future__ import annotations

from collaboration_packet import new_correlation_id
from collaboration_session import (
    CollaborationOutcome,
    CollaborationStatus,
    collab_agent_address,
)
from collaboration_state import TraceSummary
from mode_gate import Mode, ModeGate
from task_classifier import Complexity
from verified_selection_bridge import VerifiedSelectionBridge
from verified_stage_selector import _ROLE_REQUIREMENTS

# Facade-level outcome value for an explicit dual demand with no capable
# verified candidate. Not a session status: the session never ran.
DUAL_NO_CAPABLE_AGENT = "DUAL_NO_CAPABLE_AGENT"

# Never silently downgraded to SINGLE, regardless of call counts.
_NEVER_FALLBACK = (
    CollaborationStatus.ARCHITECT_PACKET_INVALID,
    CollaborationStatus.CODER_PACKET_INVALID,
    CollaborationStatus.CORRELATION_MISMATCH,
)


class CollaborationOrchestrator:
    """Routes SINGLE/DUAL by mode+complexity; records into shared state."""

    def __init__(self, verified_orchestrator, pool, current_health, budget,
                 usage, loop_guard, session_factory, state=None):
        self._verified_orchestrator = verified_orchestrator
        self._pool = pool
        self._current_health = dict(current_health)
        self._budget = budget
        self._usage = usage
        self._loop_guard = loop_guard
        self._session_factory = session_factory
        from collaboration_state import SharedCollaborationState
        self._state = state if state is not None else SharedCollaborationState()

    @property
    def state(self):
        return self._state

    def run(self, task_id, task, prompt, mode=Mode.AUTO, provenance="OFFLINE"):
        decision = ModeGate().decide(mode, task)
        if decision.mode is Mode.OFF:
            self._state = self._state.append_decision(
                task_id, mode=decision.mode.value,
                complexity=decision.complexity.value, path="SINGLE",
                runtime_mode="", reason="MODE_OFF")
            return self._verified_orchestrator.execute(task_id, task, prompt, mode)
        forced = decision.mode is Mode.ON
        if forced or decision.complexity is Complexity.COMPLEX:
            return self._run_dual(task_id, task, prompt, mode, decision, provenance)
        self._state = self._state.append_decision(
            task_id, mode=decision.mode.value,
            complexity=decision.complexity.value, path="SINGLE",
            runtime_mode="", reason=decision.reason)
        return self._verified_orchestrator.execute(task_id, task, prompt, mode)

    # -- dual path --------------------------------------------------------

    def _role_candidate(self, role):
        if self._pool is None:
            return None
        candidate_set = VerifiedSelectionBridge().candidates_for(
            self._pool, self._current_health, role, _ROLE_REQUIREMENTS[role])
        candidates = candidate_set.candidates
        return candidates[0] if candidates else None

    def _run_dual(self, task_id, task, prompt, mode, decision, provenance):
        architect = self._role_candidate("architect")
        coder = self._role_candidate("coder")
        if architect is None or coder is None:
            self._state = self._state.append_decision(
                task_id, mode=decision.mode.value,
                complexity=decision.complexity.value, path="DUAL",
                runtime_mode="", reason=DUAL_NO_CAPABLE_AGENT)
            self._state = self._state.append_failure(
                task_id, status=DUAL_NO_CAPABLE_AGENT)
            if decision.mode is Mode.ON:
                return CollaborationOutcome(
                    status=DUAL_NO_CAPABLE_AGENT, task_id=task_id,
                    correlation_id="", runtime_mode="")
            self._state = self._state.append_decision(
                task_id, mode=decision.mode.value,
                complexity=decision.complexity.value, path="SINGLE",
                runtime_mode="",
                reason=f"FALLBACK_AFTER_{DUAL_NO_CAPABLE_AGENT}")
            return self._verified_orchestrator.execute(task_id, task, prompt, mode)

        architect_identity = (architect.runtime_id, architect.provider_id,
                              architect.model_id, architect.config_fingerprint)
        coder_identity = (coder.runtime_id, coder.provider_id,
                         coder.model_id, coder.config_fingerprint)
        runtime_mode = "SINGLE_RUNTIME" if architect_identity == coder_identity else "MULTI"
        architect_address = collab_agent_address(architect_identity, "architect")
        coder_address = collab_agent_address(coder_identity, "coder")
        correlation_id = new_correlation_id()
        self._state = self._state.append_decision(
            task_id, mode=decision.mode.value,
            complexity=decision.complexity.value, path="DUAL",
            runtime_mode=runtime_mode,
            reason=f"{decision.reason}/COVERAGE=ARCHITECT_CODER")

        usage_before = self._usage.total_agent_calls
        session = self._session_factory()
        outcome = session.run(
            task_id=task_id, task=task,
            architect_address=architect_address, coder_address=coder_address,
            correlation_id=correlation_id, provenance=provenance,
            runtime_mode=runtime_mode)
        calls_made = self._usage.total_agent_calls - usage_before
        self._record_outcome(task_id, correlation_id, outcome)

        if outcome.status is CollaborationStatus.SUCCESS:
            return outcome
        if self._may_fallback(decision, outcome, calls_made):
            self._state = self._state.append_decision(
                task_id, mode=decision.mode.value,
                complexity=decision.complexity.value, path="SINGLE",
                runtime_mode="",
                reason=f"FALLBACK_AFTER_{outcome.status.value}")
            return self._verified_orchestrator.execute(task_id, task, prompt, mode)
        return outcome

    def _may_fallback(self, decision, outcome, calls_made):
        if decision.mode is Mode.ON:
            return False
        if outcome.status in _NEVER_FALLBACK:
            return False
        if outcome.status is CollaborationStatus.BUDGET_EXHAUSTED:
            return False
        return calls_made == 0

    def _record_outcome(self, task_id, correlation_id, outcome):
        summaries = tuple(
            TraceSummary(item.invocation_id, item.status.value,
                         item.exit_code, item.duration_ms)
            for item in outcome.traces
        )
        if outcome.status is CollaborationStatus.SUCCESS:
            self._state = self._state.append_envelope(
                task_id, outcome.request_envelope, "REQUEST", "DELIVERED",
                trace_summaries=summaries[:1])
            self._state = self._state.append_envelope(
                task_id, outcome.reply_envelope, "REPLY", "DELIVERED",
                trace_summaries=summaries[1:])
            return
        self._state = self._state.append_failure(
            task_id, status=outcome.status.value,
            correlation_id=correlation_id,
            envelope=outcome.request_envelope,
            trace_summaries=summaries)
