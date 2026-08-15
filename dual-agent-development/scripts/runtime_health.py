"""Bounded, provider-neutral Runtime health pipeline."""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Protocol

from runtime_status import (
    AuthenticationState,
    HealthEvidence,
    ReasonCode,
    RuntimeState,
    RuntimeStatus,
)


@dataclass(frozen=True)
class AuthenticationCheck:
    state: AuthenticationState
    method: str | None = None
    reason_code: ReasonCode = ReasonCode.NONE


@dataclass(frozen=True)
class ProviderModelCheck:
    provider: str | None
    model: str | None
    available: bool
    reason_code: ReasonCode = ReasonCode.NONE


@dataclass(frozen=True)
class MinimalHealthCheck:
    passed: bool
    reason_code: ReasonCode = ReasonCode.NONE
    trace: object | None = None
    output_class: str | None = None


@dataclass(frozen=True)
class RuntimeHealthResult:
    status: RuntimeStatus
    trace: object | None = None


class RuntimeHealthAdapter(Protocol):
    def discover(self): ...
    def check_authentication(self) -> AuthenticationCheck: ...
    def check_provider_model(self) -> ProviderModelCheck: ...
    def minimal_health_check(self, timeout_seconds: float) -> MinimalHealthCheck: ...


class RuntimeHealthController:
    def __init__(self, ttl_seconds: float = 300.0, clock=time.time):
        self.ttl_seconds = ttl_seconds
        self.clock = clock

    def check(self, adapter: RuntimeHealthAdapter) -> RuntimeStatus:
        return self.check_with_trace(adapter).status

    def check_with_trace(self, adapter: RuntimeHealthAdapter) -> RuntimeHealthResult:
        checked_at = self.clock()
        discovery = adapter.discover()
        runtime_id = discovery.runtime
        if not discovery.available:
            return RuntimeHealthResult(self._status(
                runtime_id, None, None, None, RuntimeState.UNAVAILABLE,
                ReasonCode.EXECUTABLE_NOT_FOUND,
                HealthEvidence("failed", "not_checked", "not_checked", "not_checked", "not_checked"),
                checked_at,
            ))

        auth = adapter.check_authentication()
        provider_model = adapter.check_provider_model()
        auth_state = auth.state.value if isinstance(auth.state, AuthenticationState) else str(auth.state)
        if auth_state in {AuthenticationState.AUTH_REQUIRED.value, AuthenticationState.REJECTED.value}:
            reason = auth.reason_code if auth.reason_code != ReasonCode.NONE else ReasonCode.AUTH_REQUIRED
            return RuntimeHealthResult(self._status(
                runtime_id, discovery.version, provider_model.provider, provider_model.model,
                RuntimeState.AUTH_REQUIRED, reason,
                HealthEvidence("verified", auth_state, "not_checked", "not_checked", "not_checked"),
                checked_at,
                auth.method,
            ))
        if auth_state != AuthenticationState.AUTHENTICATED.value:
            return RuntimeHealthResult(self._status(
                runtime_id, discovery.version, provider_model.provider, provider_model.model,
                RuntimeState.ERROR, ReasonCode.PROTOCOL_ERROR,
                HealthEvidence("verified", auth_state, "not_checked", "not_checked", "not_checked"),
                checked_at,
                auth.method,
            ))
        if not provider_model.available:
            reason = provider_model.reason_code if provider_model.reason_code != ReasonCode.NONE else ReasonCode.PROVIDER_UNREACHABLE
            state = RuntimeState.UNAVAILABLE if reason != ReasonCode.AUTH_REQUIRED else RuntimeState.AUTH_REQUIRED
            return RuntimeHealthResult(self._status(
                runtime_id, discovery.version, provider_model.provider, provider_model.model,
                state, reason,
                HealthEvidence("verified", "authenticated", "failed", "failed", "not_checked"),
                checked_at,
                auth.method,
            ))

        health_timeout_seconds = min(30.0, max(1.0, self.ttl_seconds))
        health = adapter.minimal_health_check(health_timeout_seconds)
        trace = getattr(health, "trace", None)
        if not health.passed:
            reason = health.reason_code if health.reason_code != ReasonCode.NONE else ReasonCode.HEALTH_CHECK_FAILED
            return RuntimeHealthResult(self._status(
                runtime_id, discovery.version, provider_model.provider, provider_model.model,
                RuntimeState.ERROR, reason,
                HealthEvidence(
                    "verified", "authenticated", "verified", "verified", "failed",
                    exit_code=getattr(trace, "exit_code", None),
                    duration_ms=getattr(trace, "duration_ms", None),
                    output_class=getattr(health, "output_class", None),
                ),
                checked_at,
                auth.method,
            ), trace)
        return RuntimeHealthResult(self._status(
            runtime_id, discovery.version, provider_model.provider, provider_model.model,
            RuntimeState.READY, ReasonCode.NONE,
            HealthEvidence(
                "verified", "authenticated", "verified", "verified", "passed",
                exit_code=getattr(trace, "exit_code", None),
                duration_ms=getattr(trace, "duration_ms", None),
                output_class=getattr(health, "output_class", None),
            ),
            checked_at,
            auth.method,
        ), trace)

    def _status(self, runtime_id, version, provider, model, state, reason, evidence, checked_at, auth_method=None):
        return RuntimeStatus(
            runtime_id, None, version, state, provider, model, auth_method,
            reason, evidence, checked_at, checked_at + self.ttl_seconds,
        )
