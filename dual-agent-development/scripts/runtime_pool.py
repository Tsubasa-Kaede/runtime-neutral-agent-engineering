"""Non-secret RuntimeStatus TTL cache and READY pool."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable
import time

from runtime_status import ReasonCode, RuntimeState, RuntimeStatus


@dataclass
class _Entry:
    fingerprint: str
    status: RuntimeStatus


class RuntimeHealthCache:
    def __init__(self, clock=time.time):
        self.clock = clock
        self._entries: dict[str, _Entry] = {}

    def store(self, runtime_id: str, fingerprint: str, status: RuntimeStatus) -> None:
        self._entries[runtime_id] = _Entry(fingerprint, status)

    def get_or_refresh(
        self,
        runtime_id: str,
        fingerprint: str,
        refresh: Callable[[], RuntimeStatus],
    ) -> RuntimeStatus:
        entry = self._entries.get(runtime_id)
        if entry and entry.fingerprint == fingerprint and entry.status.expires_at > self.clock():
            return entry.status
        status = refresh()
        self.store(runtime_id, fingerprint, status)
        return status

    def invalidate(self, runtime_id: str, reason: ReasonCode | None = None) -> None:
        self._entries.pop(runtime_id, None)

    def ready_statuses(self) -> tuple[RuntimeStatus, ...]:
        now = self.clock()
        return tuple(
            entry.status
            for entry in self._entries.values()
            if entry.status.status is RuntimeState.READY and entry.status.expires_at > now
        )
