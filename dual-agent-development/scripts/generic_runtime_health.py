"""Runtime-neutral generic health contract.

Bridges Phase 10A candidates into the health pipeline:
RuntimeCandidate -> RuntimeHealthProbe -> checks -> RuntimeHealthResult.
DISCOVERED is never treated as READY here; the probe answers auth,
provider/model and minimal health, and the pipeline stays free of
runtime-name branches.
"""
from __future__ import annotations

from typing import Protocol

from runtime_discovery import RuntimeCandidate
from runtime_health import (
    AuthenticationCheck,
    MinimalHealthCheck,
    ProviderModelCheck,
    RuntimeHealthController,
    RuntimeHealthResult,
)
from runtime_pool import RuntimeHealthCache
from runtime_status import HealthEvidence, ReasonCode, RuntimeState, RuntimeStatus


class RuntimeHealthProbe(Protocol):
    """Unified probe contract: any runtime adapter implementing these four
    methods plugs into the generic health pipeline unchanged."""

    def discover(self): ...
    def check_authentication(self) -> AuthenticationCheck: ...
    def check_provider_model(self) -> ProviderModelCheck: ...
    def minimal_health_check(self, timeout_seconds: float) -> MinimalHealthCheck: ...


class GenericRuntimeHealth:
    def __init__(self, controller: RuntimeHealthController | None = None):
        self._controller = controller or RuntimeHealthController()

    def check(self, candidate: RuntimeCandidate, probe: RuntimeHealthProbe) -> RuntimeHealthResult:
        if not candidate.available:
            checked_at = self._controller.clock()
            status = RuntimeStatus(
                candidate.runtime_id, None, None, RuntimeState.UNAVAILABLE,
                None, None, None, ReasonCode.EXECUTABLE_NOT_FOUND,
                HealthEvidence("failed", "not_checked", "not_checked", "not_checked", "not_checked"),
                checked_at, checked_at + self._controller.ttl_seconds,
            )
            return RuntimeHealthResult(status, None)
        return self._controller.check_with_trace(probe)

    def check_cached(
        self,
        candidate: RuntimeCandidate,
        probe: RuntimeHealthProbe,
        cache: RuntimeHealthCache,
        fingerprint: str,
    ) -> RuntimeHealthResult:
        """TTL/fingerprint-cached variant. Cache stores only the structured
        RuntimeStatus (never raw output or secrets); a cache hit skips the
        full probe pass, and the returned trace is unavailable on hits."""
        status = cache.get_or_refresh(
            candidate.runtime_id, fingerprint,
            lambda: self.check(candidate, probe).status,
        )
        return RuntimeHealthResult(status, None)
