"""Phase 10E: verified, score-less stage selection over verified projections.

Consumes VerifiedRoleCandidateSets (10D) and decides whether stages share
one runtime or spread across several, using only structural signals: the
best architect and best coder candidates live on different runtimes. No
scores, no registry scoring, no capability context, no health probing and
no process launching. It emits the same selection-result vocabulary as the
ReadyPool path so the existing selection plan bridge can translate it into
an InvocationPlan.
"""
from __future__ import annotations

from typing import Mapping

from invocation_plan import InvocationPlan
from runtime_status import RuntimeStatus
from selection_plan_bridge import bridge_selection
from stage_runtime_selection import (
    RuntimeSelectionResult,
    SelectionMode,
    SelectionReason,
    StageRuntimeSelector,
    StageSelection,
)
from task_classifier import Complexity
from verified_runtime_pool import VerifiedRuntimePool
from verified_selection_bridge import VerifiedRoleCandidateSet, VerifiedSelectionBridge


# Stage -> required capability names (CapabilityName values as strings).
# Static vocabulary only; the verified bridge validates them against
# validated_capabilities.
_ROLE_REQUIREMENTS = {
    "architect": ("architecture",),
    "coder": ("coding",),
    "test": ("testing",),
    "review": ("review",),
}


class VerifiedStageSelector:
    """Score-less SINGLE/MULTI decision over verified role candidate sets."""

    def select(
        self,
        role_candidate_sets: Mapping[str, VerifiedRoleCandidateSet],
        complexity: Complexity | str,
    ) -> RuntimeSelectionResult:
        complexity = Complexity(complexity)
        stages = StageRuntimeSelector._stages(complexity)
        required = [stage for stage in stages if stage in _ROLE_REQUIREMENTS]
        if any(
            not role_candidate_sets.get(stage) or not role_candidate_sets[stage].candidates
            for stage in required
        ):
            return RuntimeSelectionResult(SelectionMode.SINGLE, (), SelectionReason.NO_CAPABLE_AGENT)

        architect_set = role_candidate_sets.get("architect")
        coder_set = role_candidate_sets["coder"]
        picks = {stage: role_candidate_sets[stage].candidates[0] for stage in required}

        crosses = bool(
            architect_set
            and architect_set.candidates
            and coder_set.candidates
            and architect_set.candidates[0].runtime_id != coder_set.candidates[0].runtime_id
        )
        multi_allowed = complexity is not Complexity.SIMPLE and crosses

        distinct = {pick.runtime_id for pick in picks.values()}
        if not multi_allowed and (crosses or len(distinct) > 1):
            target = coder_set.candidates[0].runtime_id
            converged = tuple(
                self._stage_selection(stage, self._pick(role_candidate_sets[stage], target) or picks[stage])
                for stage in required
            )
            reason = SelectionReason.SIMPLE_TASK if complexity is Complexity.SIMPLE else SelectionReason.SINGLE_RUNTIME_POOL
            return RuntimeSelectionResult(SelectionMode.SINGLE, converged, reason)

        selections = tuple(self._stage_selection(stage, picks[stage]) for stage in required)
        if multi_allowed:
            return RuntimeSelectionResult(SelectionMode.MULTI, selections, SelectionReason.CLEAR_SPECIALIZATION)
        reason = SelectionReason.SIMPLE_TASK if complexity is Complexity.SIMPLE else SelectionReason.SINGLE_RUNTIME_POOL
        return RuntimeSelectionResult(SelectionMode.SINGLE, selections, reason)

    @staticmethod
    def _pick(candidate_set: VerifiedRoleCandidateSet, runtime_id: str):
        """Highest-ranked candidate of a set on one runtime (None if absent)."""
        for candidate in candidate_set.candidates:
            if candidate.runtime_id == runtime_id:
                return candidate
        return None

    @staticmethod
    def _stage_selection(stage: str, candidate) -> StageSelection:
        return StageSelection(stage, candidate.runtime_id, candidate.agent_id, score=None, rank=candidate.rank)


def verified_plan(
    pool: VerifiedRuntimePool,
    current_health: Mapping[str, RuntimeStatus],
    task_id: str,
    mode: str,
    complexity: Complexity | str,
    budget,
    usage,
) -> InvocationPlan:
    """Compose verified pool -> bridge -> selector -> plan. Pure and offline."""
    role_sets = {
        role: VerifiedSelectionBridge().candidates_for(pool, current_health, role, requirements)
        for role, requirements in _ROLE_REQUIREMENTS.items()
    }
    selection = VerifiedStageSelector().select(role_sets, complexity)
    return bridge_selection(selection, task_id, mode, complexity, budget, usage)