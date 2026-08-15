"""Offline Single / Multi-Runtime Selection over ranked role candidates.

Consumes RoleCandidateSets (10C-B) and decides whether stages share one
runtime or spread across several, using the Phase-9C specialization
semantics (threshold reused verbatim; gains computed structurally the same
way from candidate scores: both sides must be strictly better on their own
turf). Pure decision — no health, no scoring, no plan, no invocation, and
no runtime-name branches.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Mapping

from dual_agent_selection import _SPECIALIZATION_THRESHOLD, _STAGE_REQUIREMENTS, Complexity
from role_candidates import RoleCandidateSet


class SelectionMode(str, Enum):
    SINGLE = "SINGLE"
    MULTI = "MULTI"


class SelectionReason(str, Enum):
    SINGLE_RUNTIME_POOL = "SINGLE_RUNTIME_POOL"
    SIMPLE_TASK = "SIMPLE_TASK"
    INSUFFICIENT_SPECIALIZATION = "INSUFFICIENT_SPECIALIZATION"
    CLEAR_SPECIALIZATION = "CLEAR_SPECIALIZATION"
    NO_CAPABLE_AGENT = "NO_CAPABLE_AGENT"


@dataclass(frozen=True)
class StageSelection:
    """Stage assignment; score is None on the verified score-less path."""

    stage: str
    runtime_id: str
    agent_id: str
    score: float | None
    rank: int

    def __post_init__(self) -> None:
        lowered = (self.stage + self.runtime_id + self.agent_id).lower()
        for marker in ("token", "secret", "api_key", "authorization"):
            if marker in lowered:
                raise ValueError("stage selection must not contain secret-shaped content")


@dataclass(frozen=True)
class RuntimeSelectionResult:
    mode: SelectionMode
    stage_selections: tuple[StageSelection, ...]
    reason: SelectionReason = SelectionReason.SIMPLE_TASK


def _pick(candidate_set: RoleCandidateSet, runtime_id: str | None = None):
    """Best candidate of a role set, optionally constrained to one runtime.
    Ordering follows the set's rank (already (-score, agent_id) stable)."""
    pool = candidate_set.candidates
    if runtime_id is not None:
        pool = tuple(item for item in pool if item.runtime_id == runtime_id)
    return pool[0] if pool else None


def _score_of(candidate_set: RoleCandidateSet, runtime_id: str) -> float | None:
    for item in candidate_set.candidates:
        if item.runtime_id == runtime_id:
            return item.score
    return None


def _specialization(architect_set: RoleCandidateSet, coder_set: RoleCandidateSet) -> float:
    """Same semantics as DualAgentSelection._specialization: the sum of gains
    only when each side is strictly better on its own role."""
    if not architect_set.candidates or not coder_set.candidates:
        return 0.0
    arch_best = architect_set.candidates[0]
    coder_best = coder_set.candidates[0]
    if arch_best.runtime_id == coder_best.runtime_id:
        return 0.0
    arch_gain = arch_best.score - (_score_of(architect_set, coder_best.runtime_id) or 0.0)
    coder_gain = coder_best.score - (_score_of(coder_set, arch_best.runtime_id) or 0.0)
    if arch_gain <= 0 or coder_gain <= 0:
        return 0.0
    return arch_gain + coder_gain


class StageRuntimeSelector:
    def select(
        self,
        role_candidate_sets: Mapping[str, RoleCandidateSet],
        complexity: Complexity | str,
    ) -> RuntimeSelectionResult:
        complexity = Complexity(complexity)
        stages = self._stages(complexity)
        required = [stage for stage in stages if stage in ("architect", "coder", "test", "review")]
        if any(not role_candidate_sets.get(stage) or not role_candidate_sets[stage].candidates for stage in required):
            return RuntimeSelectionResult(SelectionMode.SINGLE, (), SelectionReason.NO_CAPABLE_AGENT)

        architect_set = role_candidate_sets.get("architect") or RoleCandidateSet("architect", ())
        coder_set = role_candidate_sets["coder"]

        picks = {stage: _pick(role_candidate_sets[stage]) for stage in required}

        # Multi-runtime signal mirrors DualAgentSelection.decide(): the best
        # architect and best coder sit on different runtimes AND the
        # specialization gain clears the Phase-9C threshold.
        crosses = (
            bool(architect_set.candidates)
            and architect_set.candidates[0].runtime_id != coder_set.candidates[0].runtime_id
        )
        spec = _specialization(architect_set, coder_set)
        multi_allowed = (
            complexity is not Complexity.SIMPLE
            and crosses
            and spec >= _SPECIALIZATION_THRESHOLD
        )

        distinct = {pick.runtime_id for pick in picks.values()}
        if not multi_allowed and (crosses or len(distinct) > 1):
            target = coder_set.candidates[0].runtime_id
            converged = []
            for stage in required:
                pick = _pick(role_candidate_sets[stage], target) or picks[stage]
                converged.append(StageSelection(stage, pick.runtime_id, pick.agent_id, pick.score, pick.rank))
            reason = SelectionReason.SIMPLE_TASK if complexity is Complexity.SIMPLE else SelectionReason.INSUFFICIENT_SPECIALIZATION
            return RuntimeSelectionResult(SelectionMode.SINGLE, tuple(converged), reason)

        selections = tuple(
            StageSelection(stage, picks[stage].runtime_id, picks[stage].agent_id, picks[stage].score, picks[stage].rank)
            for stage in required
        )
        if multi_allowed:
            # Multi is the decision, even when this complexity's stage list
            # happens to land on one runtime (the signal came from the
            # architect/coder crossover, mirroring DualAgentSelection.decide).
            return RuntimeSelectionResult(SelectionMode.MULTI, selections, SelectionReason.CLEAR_SPECIALIZATION)
        reason = SelectionReason.SIMPLE_TASK if complexity is Complexity.SIMPLE else SelectionReason.SINGLE_RUNTIME_POOL
        return RuntimeSelectionResult(SelectionMode.SINGLE, selections, reason)

    @staticmethod
    def _stages(complexity: Complexity) -> tuple[str, ...]:
        # Same stage sets as DualAgentSelection._stages.
        if complexity is Complexity.MEDIUM:
            return ("coder", "test")
        if complexity is Complexity.COMPLEX:
            return ("architect", "coder", "test", "review")
        return ("coder",)


# keep the stage->capability mapping referenced so the coupling to 9C stays explicit
_STAGE_CAPABILITIES = _STAGE_REQUIREMENTS
