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
from collaboration_policy import PolicyConstrainedAssigner
from collaboration_session import (
    CollaborationOutcome,
    CollaborationStatus,
    collab_agent_address,
)
from collaboration_state import TraceSummary
from execution_observation import ExecutionEventType
from mode_gate import Mode, ModeGate
from role_assignment import ConvergingAssigner
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
                 usage, loop_guard, session_factory, state=None,
                 role_assigner=None):
        self._verified_orchestrator = verified_orchestrator
        self._pool = pool
        self._current_health = dict(current_health)
        self._budget = budget
        self._usage = usage
        self._loop_guard = loop_guard
        self._session_factory = session_factory
        # 10H-E: role assignment is an injectable policy; the default
        # (ConvergingAssigner) reproduces the historical candidates[0]
        # fold verbatim, so absent injection nothing changes.
        self._role_assigner = role_assigner if role_assigner is not None else ConvergingAssigner()
        from collaboration_state import SharedCollaborationState
        self._state = state if state is not None else SharedCollaborationState()

    @property
    def state(self):
        return self._state

    def run(self, task_id, task, prompt, mode=Mode.AUTO, provenance="OFFLINE",
            observation_emit=None, policy=None):
        decision = ModeGate().decide(mode, task)
        if decision.mode is Mode.OFF:
            self._state = self._state.append_decision(
                task_id, mode=decision.mode.value,
                complexity=decision.complexity.value, path="SINGLE",
                runtime_mode="", reason="MODE_OFF")
            return self._verified_orchestrator.execute(task_id, task, prompt, mode)
        forced = decision.mode is Mode.ON
        if forced or decision.complexity is Complexity.COMPLEX:
            return self._run_dual(task_id, task, prompt, mode, decision,
                                  provenance, policy, observation_emit)
        self._state = self._state.append_decision(
            task_id, mode=decision.mode.value,
            complexity=decision.complexity.value, path="SINGLE",
            runtime_mode="", reason=decision.reason)
        return self._verified_orchestrator.execute(task_id, task, prompt, mode)

    # -- dual path --------------------------------------------------------

    def _role_candidate_sets(self):
        """Per-role bridge candidate sets for the dual roles (10H-E joint
        selection input). Empty dict when there is no pool."""
        if self._pool is None:
            return {}
        bridge = VerifiedSelectionBridge()
        return {
            role: bridge.candidates_for(
                self._pool, self._current_health, role, _ROLE_REQUIREMENTS[role])
            for role in ("architect", "coder")
        }

    def _run_dual(self, task_id, task, prompt, mode, decision, provenance,
                  policy=None, observation_emit=None):
        # 10H-E: architect/coder joint choice goes through the injected
        # role-assignment policy (default = historical candidates[0]
        # fold). Candidates come only from the bridge sets. A per-run
        # policy (R7-A2) switches to a PolicyConstrainedAssigner for this
        # run only; policy=None keeps the historical assigner verbatim.
        candidate_sets = self._role_candidate_sets()
        assigner = self._role_assigner if policy is None else \
            PolicyConstrainedAssigner(policy)
        assignment = assigner.assign(candidate_sets, decision.complexity)
        architect = assignment.assignments.get("architect")
        coder = assignment.assignments.get("coder")
        if architect is None or coder is None:
            # R7-A3: preserve the assignment reason on the no-capable-agent
            # terminal — additive observability only. The DUAL_NO_CAPABLE_AGENT
            # status, the failure record and the fallback rules are verbatim;
            # without a per-run policy the reason stays the exact historical
            # string (zero drift).
            none_reason = DUAL_NO_CAPABLE_AGENT
            if policy is not None and assignment.reason:
                none_reason = f"{DUAL_NO_CAPABLE_AGENT}/ROLE_ASSIGNMENT={assignment.reason}"
            self._state = self._state.append_decision(
                task_id, mode=decision.mode.value,
                complexity=decision.complexity.value, path="DUAL",
                runtime_mode="", reason=none_reason)
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
            reason=f"{decision.reason}/COVERAGE=ARCHITECT_CODER"
                   f"/ROLE_ASSIGNMENT={assignment.reason}")
        # R7-D2: DECISION 事件在真实 dual 决策缝（correlation 已铸造、
        # 决策记录已 append）同步发射 —— 唯一权威发射者；sink 故障被
        # emit helper 隔离，绝不影响执行流（旁路观察，不是控制流）。
        if observation_emit is not None:
            observation_emit(
                ExecutionEventType.DECISION, stage="dual",
                runtime_id=architect.runtime_id, status=decision.mode.value,
                reason=f"{decision.reason}/COVERAGE=ARCHITECT_CODER"
                       f"/ROLE_ASSIGNMENT={assignment.reason}",
                correlation_id=correlation_id)

        usage_before = self._usage.total_agent_calls
        session = self._session_factory()
        outcome = session.run(
            task_id=task_id, task=task,
            architect_address=architect_address, coder_address=coder_address,
            correlation_id=correlation_id, provenance=provenance,
            runtime_mode=runtime_mode, observation_emit=observation_emit)
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
