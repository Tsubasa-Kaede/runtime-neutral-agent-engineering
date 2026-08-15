"""READY Runtime Pool construction: discovery -> generic health -> pool.

The pool is a pure construction layer: it keeps only runtimes whose
RuntimeStatus is READY and moves everything else (AUTH_REQUIRED,
UNAVAILABLE, ERROR, any non-READY outcome) to an excluded list with the
paired status. It never caches, scores, selects, or invokes.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from generic_runtime_health import GenericRuntimeHealth
from runtime_discovery import DiscoverySource, RuntimeCandidate, RuntimeCandidateDiscovery
from runtime_status import RuntimeState, RuntimeStatus


@dataclass(frozen=True)
class PooledRuntime:
    candidate: RuntimeCandidate
    status: RuntimeStatus


@dataclass(frozen=True)
class ReadyPool:
    ready: tuple[PooledRuntime, ...]
    excluded: tuple[PooledRuntime, ...]


class RuntimePoolConstructor:
    def __init__(self, health: GenericRuntimeHealth | None = None):
        self._health = health or GenericRuntimeHealth()

    def build(self, sources: Iterable[DiscoverySource]) -> ReadyPool:
        entries = tuple(sorted(sources, key=lambda item: item.runtime_id))
        candidates = RuntimeCandidateDiscovery(entries).discover_all()
        by_id = {source.runtime_id: source for source in entries}
        ready: list[PooledRuntime] = []
        excluded: list[PooledRuntime] = []
        for candidate in candidates:
            probe = by_id[candidate.runtime_id].adapter
            result = self._health.check(candidate, probe)
            pooled = PooledRuntime(candidate, result.status)
            if result.status.status is RuntimeState.READY:
                ready.append(pooled)
            else:
                excluded.append(pooled)
        return ReadyPool(tuple(ready), tuple(excluded))
