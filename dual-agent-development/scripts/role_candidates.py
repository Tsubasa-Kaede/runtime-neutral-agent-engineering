"""Role Candidate Selection: rank suitable READY runtimes per role.

Pure bridge over ReadyPool + CapabilityRegistry: the registry's scoring,
hard gates and tie-breaks are reused verbatim (no algorithm is duplicated
or altered here). This layer produces ranked candidate sets only — no
pairing, no single/dual decision, no InvocationPlan.
"""
from __future__ import annotations

from dataclasses import dataclass

from capability_registry import CapabilityRegistry, CapabilityName
from dual_agent_selection import _STAGE_REQUIREMENTS
from runtime_pool_construction import ReadyPool


@dataclass(frozen=True)
class RoleCandidate:
    role: str
    runtime_id: str
    agent_id: str
    score: float
    rank: int
    evidence: str
    required_capabilities: tuple[str, ...]

    def __post_init__(self) -> None:
        lowered = (self.role + self.evidence).lower()
        for marker in ("token", "secret", "api_key", "authorization"):
            if marker in lowered:
                raise ValueError("role candidate must not contain secret-shaped content")


@dataclass(frozen=True)
class RoleCandidateSet:
    role: str
    candidates: tuple[RoleCandidate, ...]


class RoleCandidateSelector:
    def candidates_for(self, pool: ReadyPool, registry: CapabilityRegistry, role: str) -> RoleCandidateSet:
        required = _STAGE_REQUIREMENTS[role]
        statuses = {item.candidate.runtime_id: item.status for item in pool.ready}
        selection = registry.select(set(required), statuses)
        ranked = sorted(
            (item for item in selection.candidates if item.eligible and item.score is not None),
            key=lambda item: (-item.score, item.agent_id),
        )
        candidates = tuple(
            RoleCandidate(
                role=role,
                runtime_id=item.runtime_id,
                agent_id=item.agent_id,
                score=item.score,
                rank=index + 1,
                evidence=",".join(sorted(cap.value for cap in required)),
                required_capabilities=tuple(sorted(cap.value for cap in required)),
            )
            for index, item in enumerate(ranked)
        )
        return RoleCandidateSet(role, candidates)
