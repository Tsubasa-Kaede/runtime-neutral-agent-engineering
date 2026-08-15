"""Runtime-neutral candidate discovery: collect which runtimes exist.

Discovery answers only existence/findability. Health (READY / AUTH_REQUIRED /
UNAVAILABLE / ERROR) stays owned by the Runtime Health Pipeline — a
DISCOVERED candidate is never treated as READY here. New runtimes plug in
via DiscoverySource registration; this module never branches on runtime
names.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable


class DiscoveryState(str, Enum):
    DISCOVERED = "DISCOVERED"
    NOT_FOUND = "NOT_FOUND"


_SECRET_MARKERS = ("token", "secret", "api_key", "authorization")


def _assert_secret_free(value: str | None, field: str) -> None:
    if value is None:
        return
    lowered = value.lower()
    for marker in _SECRET_MARKERS:
        if marker in lowered and any(sep in lowered[max(0, lowered.index(marker)):lowered.index(marker) + len(marker) + 2] for sep in (":", "=")):
            raise ValueError(f"{field} must not contain secret-shaped content")


@dataclass(frozen=True)
class RuntimeCandidate:
    runtime_id: str
    runtime_type: str
    display_name: str
    available: bool
    reason: str | None = None
    executable: str | None = None
    version: str | None = None

    def __post_init__(self) -> None:
        if not self.runtime_id or not self.runtime_type:
            raise ValueError("runtime_id and runtime_type are required")
        _assert_secret_free(self.display_name, "display_name")
        _assert_secret_free(self.reason, "reason")


@dataclass(frozen=True)
class DiscoverySource:
    """Registration unit: an adapter exposing discover() plus neutral metadata."""
    runtime_id: str
    runtime_type: str
    display_name: str
    adapter: Any

    def __post_init__(self) -> None:
        if not callable(getattr(self.adapter, "discover", None)):
            raise ValueError("adapter must provide a callable discover()")


class RuntimeCandidateDiscovery:
    def __init__(self, sources: Iterable[DiscoverySource]):
        self._sources = tuple(sorted(sources, key=lambda item: item.runtime_id))

    def discover_all(self) -> tuple[RuntimeCandidate, ...]:
        candidates = []
        for source in self._sources:
            candidates.append(self._discover_one(source))
        return tuple(candidates)

    def discover(self, runtime_id: str) -> RuntimeCandidate:
        for source in self._sources:
            if source.runtime_id == runtime_id:
                return self._discover_one(source)
        return RuntimeCandidate(runtime_id, "unknown", runtime_id, False,
                                f"{DiscoveryState.NOT_FOUND.value}: no source registered")

    @staticmethod
    def _discover_one(source: DiscoverySource) -> RuntimeCandidate:
        try:
            result = source.adapter.discover()
        except Exception as exc:  # discovery failure is a controlled outcome
            return RuntimeCandidate(
                source.runtime_id, source.runtime_type, source.display_name, False,
                f"{DiscoveryState.NOT_FOUND.value}: discovery error ({type(exc).__name__})",
            )
        reason = None
        if not getattr(result, "available", False):
            raw = getattr(result, "reason", None) or "runtime not discoverable"
            reason = f"{DiscoveryState.NOT_FOUND.value}: {raw}"
        return RuntimeCandidate(
            source.runtime_id, source.runtime_type, source.display_name,
            bool(getattr(result, "available", False)),
            reason,
            version=getattr(result, "version", None),
        )
