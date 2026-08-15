"""Verified Selection Bridge: read-only, runtime-neutral role candidate sets.

Phase 10D contract: candidates come only from a VerifiedRuntimePool; a
current-health snapshot is injected by the caller and never probed here;
only VERIFIED, READY entries enter the candidate set; capability evidence
is built only from validated_capabilities (declared context is never
promoted) with score=None and source=experiment_id. Entries without a
non-empty experiment_id are skipped, never fabricated. The projection is
score-less by design: candidate.score stays None. This module performs no
scoring, no health probing, no process launching and never creates plans
or agent pairs.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Iterable, Mapping

from candidate_validation import CandidateValidationStatus
from capability_registry import (
    CapabilityConfidence,
    CapabilityEvidence,
    CapabilityName,
)
from runtime_status import RuntimeState, RuntimeStatus
from verified_runtime_pool import VerifiedRuntimePool


_SECRET_MARKERS = ("token", "secret", "api_key", "authorization")


def _assert_secret_free(value, where: str) -> None:
    if isinstance(value, str):
        lowered = value.lower()
        for marker in _SECRET_MARKERS:
            if marker in lowered:
                raise ValueError(f"{where} must not contain secret-shaped content")
    elif isinstance(value, (tuple, list, frozenset, set)):
        for item in value:
            _assert_secret_free(item, where)


def _as_capability_name(value) -> CapabilityName:
    if isinstance(value, CapabilityName):
        return value
    return CapabilityName(value)


def agent_id_for(identity: tuple) -> str:
    """Deterministic, injective agent id derived from the full identity."""
    return json.dumps(tuple(identity), ensure_ascii=True, separators=(",", ":"))


@dataclass(frozen=True)
class VerifiedRoleCandidate:
    """Immutable, secret-free role candidate from one verified identity.

    Runtime / provider / model / config fingerprint stay independent
    fields; agent_id is a deterministic projection of the full identity
    (an additional field, never a replacement); score is None by design.
    """

    role: str
    agent_id: str
    runtime_id: str
    provider_id: str
    model_id: str
    config_fingerprint: str
    capabilities: tuple[CapabilityEvidence, ...]
    required_capabilities: tuple[str, ...]
    rank: int
    score: float | None = None

    def __post_init__(self) -> None:
        if self.score is not None:
            raise ValueError("verified candidates must not carry a score")
        for field, value in (
            ("role", self.role),
            ("agent_id", self.agent_id),
            ("runtime_id", self.runtime_id),
            ("provider_id", self.provider_id),
            ("model_id", self.model_id),
            ("config_fingerprint", self.config_fingerprint),
        ):
            _assert_secret_free(value, field)
        _assert_secret_free(self.required_capabilities, "required_capabilities")
        for item in self.capabilities:
            _assert_secret_free(item.source, "capability source")


@dataclass(frozen=True)
class VerifiedRoleCandidateSet:
    """Immutable per-role candidate set (read-only, runtime-neutral)."""

    role: str
    candidates: tuple[VerifiedRoleCandidate, ...]

    def __post_init__(self) -> None:
        _assert_secret_free(self.role, "role")


class VerifiedSelectionBridge:
    """Pure bridge: VerifiedRuntimePool + injected current health -> set."""

    def candidates_for(
        self,
        pool: VerifiedRuntimePool,
        current_health: Mapping[str, RuntimeStatus],
        role: str,
        required_capabilities: Iterable[CapabilityName | str],
    ) -> VerifiedRoleCandidateSet:
        required = tuple(sorted(
            {_as_capability_name(item) for item in required_capabilities},
            key=lambda item: item.value,
        ))
        required_values = frozenset(item.value for item in required)
        candidates = []
        for identity in sorted(pool.identities()):
            result = pool.get(identity)
            if result is None or result.status is not CandidateValidationStatus.VERIFIED:
                continue
            health = current_health.get(identity[0])
            if health is None or health.status is not RuntimeState.READY:
                continue
            if not result.experiment_id:
                continue
            if not required_values.issubset(frozenset(result.validated_capabilities)):
                continue
            capabilities = tuple(
                CapabilityEvidence(
                    capability=item,
                    score=None,
                    confidence=CapabilityConfidence.VERIFIED,
                    source=result.experiment_id,
                )
                for item in required
            )
            candidates.append(VerifiedRoleCandidate(
                role=role,
                agent_id=agent_id_for(identity),
                runtime_id=identity[0],
                provider_id=identity[1],
                model_id=identity[2],
                config_fingerprint=identity[3],
                capabilities=capabilities,
                required_capabilities=tuple(item.value for item in required),
                rank=len(candidates) + 1,
            ))
        return VerifiedRoleCandidateSet(role, tuple(candidates))