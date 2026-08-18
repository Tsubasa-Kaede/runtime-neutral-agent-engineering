"""Discovery Bootstrap — the RC-3 composition layer (offline core).

Registry -> Discovery -> Health -> Evidence lookup -> (reuse | explicit
qualification) -> VerifiedRuntimePool. Pure composition over the existing
verified layers: discovery and health come from the 10A/10B modules, the
evidence asset is the existing CandidateValidationResult, and admission
reuses VerifiedRuntimePool.admit. Only VERIFIED + REAL evidence is
admitted — READY never impersonates VERIFIED, invalid evidence is refused
without a re-run (no retry), and provenance is copied verbatim, never
fabricated. No process is started here.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

from candidate_validation import CandidateValidationStatus
from generic_runtime_health import GenericRuntimeHealth
from runtime_adapter_registry import AdapterRegistry, discovery_sources
from runtime_discovery import RuntimeCandidateDiscovery
from runtime_status import RuntimeState
from verified_runtime_pool import VerifiedRuntimePool

_SECRET_MARKERS = ("token", "secret", "api_key", "authorization", "bearer", "stdout", "stderr")

NOT_QUALIFIED = "NOT_QUALIFIED"


@dataclass(frozen=True)
class RuntimeBootstrapEntry:
    """Per-runtime structured outcome; references, never duplicates data."""

    runtime_id: str
    discovery_available: bool
    health_status: str | None
    validation_status: str | None
    provenance: str | None
    capabilities: tuple
    admitted: bool
    reason: str

    def __post_init__(self) -> None:
        for name in ("runtime_id", "health_status", "validation_status",
                     "provenance", "reason"):
            _assert_clean(getattr(self, name), name)

    @staticmethod
    def _clean_placeholder():
        raise NotImplementedError


def _assert_clean(value: str | None, name: str) -> None:
    if value is None:
        return
    lowered = value.lower()
    for marker in _SECRET_MARKERS:
        if marker in lowered:
            raise ValueError(f"{name} must not contain secret-shaped content")


@dataclass(frozen=True)
class RuntimeBootstrapSession:
    """Whole-session outcome: entries + the admitted VerifiedRuntimePool."""

    entries: tuple
    pool: VerifiedRuntimePool
    qualification_count: int


def bootstrap_runtime_session(
    registry: AdapterRegistry,
    evidence: Mapping[tuple, Any] | None = None,
    qualifier: Callable[[Any], Any] | None = None,
    required_capabilities: tuple = ("architecture", "coding", "review", "testing"),
    clock: Callable[[], float] = lambda: 0.0,
) -> RuntimeBootstrapSession:
    """Compose discovery -> health -> evidence reuse/qualification -> pool."""
    store = dict(evidence or {})
    pool = VerifiedRuntimePool(clock=clock)
    entries: list[RuntimeBootstrapEntry] = []
    qualification_count = 0

    discovery = RuntimeCandidateDiscovery(discovery_sources(registry))
    for candidate in discovery.discover_all():
        descriptor = registry.get(candidate.runtime_id)
        identity = descriptor.identity

        if not candidate.available:
            entries.append(RuntimeBootstrapEntry(
                runtime_id=candidate.runtime_id, discovery_available=False,
                health_status=None, validation_status=None, provenance=None,
                capabilities=(), admitted=False,
                reason=candidate.reason or "NOT_FOUND",
            ))
            continue

        adapter = descriptor.adapter_factory()
        health_result = GenericRuntimeHealth().check(candidate, adapter)
        health_status = health_result.status.status.value
        if health_result.status.status is not RuntimeState.READY:
            entries.append(RuntimeBootstrapEntry(
                runtime_id=candidate.runtime_id, discovery_available=True,
                health_status=health_status, validation_status=None,
                provenance=None, capabilities=(), admitted=False,
                reason=f"HEALTH_{health_status}",
            ))
            continue

        existing = store.get(identity)
        if existing is not None:
            # Reuse only genuinely valid evidence; invalid evidence is
            # refused without a re-run (no retry semantics).
            validation = existing
        elif qualifier is not None:
            from candidate_validation import CandidateRuntimeInstance
            instance = CandidateRuntimeInstance(
                runtime_id=descriptor.runtime_id,
                provider_id=descriptor.provider_id,
                model_id=descriptor.model_id,
                config_fingerprint=descriptor.config_fingerprint,
                capability_context=(), probe=adapter,
                invocation_spec={},
            )
            validation = qualifier(instance)
            qualification_count += 1
            store[identity] = validation
        else:
            validation = None

        if validation is None:
            entries.append(RuntimeBootstrapEntry(
                runtime_id=candidate.runtime_id, discovery_available=True,
                health_status=health_status, validation_status=NOT_QUALIFIED,
                provenance=None, capabilities=(), admitted=False,
                reason="NO_EVIDENCE_NO_QUALIFIER",
            ))
            continue

        status_value = validation.status.value
        provenance = validation.provenance
        caps = tuple(validation.validated_capabilities)
        admissible = (
            validation.status is CandidateValidationStatus.VERIFIED
            and provenance == "REAL"
            and set(required_capabilities).issubset(set(caps))
        )
        if not admissible:
            reason = (
                "CAPABILITY_INSUFFICIENT"
                if validation.status is CandidateValidationStatus.VERIFIED
                and provenance == "REAL"
                else f"NOT ADMITTED status={status_value} provenance={provenance}"
            )
            entries.append(RuntimeBootstrapEntry(
                runtime_id=candidate.runtime_id, discovery_available=True,
                health_status=health_status, validation_status=status_value,
                provenance=provenance, capabilities=caps, admitted=False,
                reason=reason,
            ))
            continue

        pool.admit(validation, tuple(required_capabilities), health_now="READY")
        entries.append(RuntimeBootstrapEntry(
            runtime_id=candidate.runtime_id, discovery_available=True,
            health_status=health_status, validation_status=status_value,
            provenance=provenance, capabilities=caps, admitted=True,
            reason="ADMITTED",
        ))

    return RuntimeBootstrapSession(
        entries=tuple(entries), pool=pool,
        qualification_count=qualification_count,
    )
