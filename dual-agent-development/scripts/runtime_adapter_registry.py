"""Runtime Adapter Registry — pure registration, lookup, discovery wiring.

Descriptors declare neutral identity metadata plus an adapter factory; the
registry only stores and enumerates them and bridges them to the existing
RuntimeCandidateDiscovery via DiscoverySource. It never executes a runtime,
never qualifies, never reads the environment, and holds no secrets —
runtime-specific knowledge stays inside each adapter.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable

from runtime_discovery import DiscoverySource

_SECRET_MARKERS = ("token", "secret", "api_key", "authorization", "bearer", "stdout", "stderr")


def _assert_secret_free(value: str | None, field: str) -> None:
    if value is None:
        return
    lowered = value.lower()
    for marker in _SECRET_MARKERS:
        if marker in lowered:
            raise ValueError(f"{field} must not contain secret-shaped content")


@dataclass(frozen=True)
class AdapterDescriptor:
    """Neutral registration unit; identity fields mirror the runtime identity."""

    runtime_id: str
    provider_id: str
    runtime_type: str
    display_name: str
    adapter_factory: Callable[[], Any]
    model_id: str | None = None
    config_fingerprint: str = "default"

    def __post_init__(self) -> None:
        for name in ("runtime_id", "provider_id", "runtime_type",
                     "display_name", "config_fingerprint"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
            _assert_secret_free(value, name)
        if self.model_id is not None:
            _assert_secret_free(self.model_id, "model_id")
        if not callable(self.adapter_factory):
            raise ValueError("adapter_factory must be callable")

    @property
    def identity(self) -> tuple:
        return (self.runtime_id, self.provider_id, self.model_id, self.config_fingerprint)


class AdapterRegistry:
    """Sorted, duplicate-free descriptor store; no execution surface."""

    def __init__(self) -> None:
        self._descriptors: dict[str, AdapterDescriptor] = {}

    def register(self, descriptor: AdapterDescriptor) -> None:
        if descriptor.runtime_id in self._descriptors:
            raise ValueError(f"duplicate runtime_id: {descriptor.runtime_id}")
        self._descriptors[descriptor.runtime_id] = descriptor

    def get(self, runtime_id: str) -> AdapterDescriptor:
        return self._descriptors[runtime_id]

    def list(self) -> tuple[AdapterDescriptor, ...]:
        return tuple(self._descriptors[key] for key in sorted(self._descriptors))


def discovery_sources(registry: AdapterRegistry) -> tuple[DiscoverySource, ...]:
    """Bridge descriptors into the existing discovery layer (no probing)."""
    return tuple(
        DiscoverySource(
            runtime_id=item.runtime_id,
            runtime_type=item.runtime_type,
            display_name=item.display_name,
            adapter=item.adapter_factory(),
        )
        for item in registry.list()
    )
