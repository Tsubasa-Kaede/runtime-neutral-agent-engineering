"""Provider-neutral external runtime adapter protocol."""
from __future__ import annotations

from typing import Protocol

from external_runtime import (
    ExternalAgentRequest,
    InvocationResult,
    RuntimeDiscovery,
)


class ExternalAgentAdapter(Protocol):
    def discover(self) -> RuntimeDiscovery: ...
    def invoke(self, request: ExternalAgentRequest) -> InvocationResult: ...
    def cancel(self, invocation_id: str) -> InvocationResult: ...
