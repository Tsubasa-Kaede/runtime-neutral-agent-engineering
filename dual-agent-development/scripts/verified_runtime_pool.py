"""Verified Runtime Pool — offline admission boundary.

Implements the approved Admission Contract over CandidateValidationResult
values. The pool decides admission only from the result's status, its
validated capabilities (set containment), an injected health state and its
own identity index — nothing else. It never imports the production health,
pool, capability or orchestration stack, never spawns processes, and never
touches credentials or configuration.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum

from candidate_validation import CandidateValidationResult, CandidateValidationStatus


_SECRET_MARKERS = ("token", "secret", "api_key", "authorization", "bearer", "stdout", "stderr")


def _assert_secret_free(value, where: str) -> None:
    if isinstance(value, str):
        lowered = value.lower()
        for marker in _SECRET_MARKERS:
            if marker in lowered:
                raise ValueError(f"{where} must not contain secret-shaped content")
    elif isinstance(value, (tuple, list, frozenset, set)):
        for item in value:
            _assert_secret_free(item, where)


class AdmissionKind(str, Enum):
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    DUPLICATE = "DUPLICATE"


class RejectionReason(str, Enum):
    NOT_VERIFIED = "NOT_VERIFIED"
    CAPABILITY_INSUFFICIENT = "CAPABILITY_INSUFFICIENT"
    HEALTH_NOT_READY = "HEALTH_NOT_READY"


@dataclass(frozen=True)
class AdmissionOutcome:
    kind: AdmissionKind
    identity: tuple
    reason: RejectionReason | None = None
    existing_identity: tuple | None = None

    def __post_init__(self) -> None:
        _assert_secret_free(self.identity, "outcome identity")
        _assert_secret_free(self.existing_identity or (), "existing identity")


@dataclass(frozen=True)
class VerifiedPoolEntry:
    result: CandidateValidationResult
    admitted_at: float
    health_state_at_admission: str

    def __post_init__(self) -> None:
        _assert_secret_free(self.health_state_at_admission, "health snapshot")


class VerifiedRuntimePool:
    def __init__(self, clock=time.time):
        self._clock = clock
        self._entries: dict[tuple, VerifiedPoolEntry] = {}

    def admit(self, result: CandidateValidationResult, required_capabilities, health_now) -> AdmissionOutcome:
        # Fixed decision order per the approved contract:
        # VERIFIED -> capability -> health -> duplicate -> ACCEPTED.
        if result.status is not CandidateValidationStatus.VERIFIED:
            return self._rejected(result.identity, RejectionReason.NOT_VERIFIED)
        if not frozenset(required_capabilities).issubset(frozenset(result.validated_capabilities)):
            return self._rejected(result.identity, RejectionReason.CAPABILITY_INSUFFICIENT)
        if getattr(health_now, "value", health_now) != "READY":
            return self._rejected(result.identity, RejectionReason.HEALTH_NOT_READY)
        existing = self._entries.get(result.identity)
        if existing is not None:
            return AdmissionOutcome(AdmissionKind.DUPLICATE, result.identity,
                                    existing_identity=existing.result.identity)
        self._entries[result.identity] = VerifiedPoolEntry(
            result=result,
            admitted_at=self._clock(),
            health_state_at_admission="READY",
        )
        return AdmissionOutcome(AdmissionKind.ACCEPTED, result.identity)

    def get(self, identity: tuple) -> CandidateValidationResult | None:
        entry = self._entries.get(identity)
        return entry.result if entry is not None else None

    def identities(self) -> tuple:
        return tuple(sorted(self._entries))

    def invalidate(self, identity: tuple) -> CandidateValidationResult | None:
        entry = self._entries.pop(identity, None)
        return entry.result if entry is not None else None

    @staticmethod
    def _rejected(identity: tuple, reason: RejectionReason) -> AdmissionOutcome:
        return AdmissionOutcome(AdmissionKind.REJECTED, identity, reason=reason)
