"""Provider-neutral external runtime adapter protocol.

The production adapter contract is the SIX-method surface below. The three
health methods are what the health pipeline (RuntimeHealthController /
GenericRuntimeHealth) and the G1-G14 qualification chain actually call, so an
adapter that only implements the core three cannot pass discovery_bootstrap
— it would fail with AttributeError at check_authentication. "Having the
methods" is not REAL VERIFIED: qualification evidence is granted only by a
gated real validation run. This Protocol states the fact surface; it adds
no runtime behavior of its own.
"""
from __future__ import annotations

from runtime_health import AuthenticationCheck, MinimalHealthCheck, ProviderModelCheck

from external_runtime import (
    ExternalAgentRequest,
    InvocationResult,
    RuntimeDiscovery,
)


class ExternalAgentAdapter(Protocol):
    # -- Core invocation surface ------------------------------------------

    def discover(self) -> RuntimeDiscovery: ...
    def invoke(self, request: ExternalAgentRequest) -> InvocationResult: ...
    def cancel(self, invocation_id: str) -> InvocationResult: ...

    # -- Health surface (required for health + qualification) -------------

    def check_authentication(self) -> AuthenticationCheck: ...
    def check_provider_model(self) -> ProviderModelCheck: ...
    def minimal_health_check(self, timeout_seconds: float) -> MinimalHealthCheck: ...
