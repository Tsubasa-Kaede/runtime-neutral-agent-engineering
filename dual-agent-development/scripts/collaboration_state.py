"""Shared collaboration state: append-only immutable ledger (Phase 10H-E1).

Records the structured facts of one collaboration: routing decisions,
request/reply envelopes (stored as canonical wire text computed at append
so later mutation of shared packet objects can never rewrite history),
failures, and safe trace summaries. Sequences are per-task, dense and
assigned by the ledger itself — callers cannot inject them. Provenance is
derived exclusively from envelopes; there is no free field to assert it.
Raw output, internal reasoning and chat never enter the ledger. Every
append returns a new state; the old one stays untouched.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from collaboration_packet import (
    CollaborationPacket,
    deserialize_collaboration_packet,
    serialize_collaboration_packet,
)

_SECRET_MARKERS = ("token", "secret", "api_key", "authorization", "bearer", "stdout", "stderr")

_PROVENANCE_VALUES = ("", "OFFLINE", "REAL")


class CollaborationStateError(ValueError):
    pass


class CollaborationDirection(str, Enum):
    DECISION = "DECISION"
    REQUEST = "REQUEST"
    REPLY = "REPLY"
    FAILURE = "FAILURE"


def _assert_clean(value, field_name: str, required: bool = False) -> None:
    if not isinstance(value, str):
        raise CollaborationStateError(f"{field_name} must be a string")
    if required and not value.strip():
        raise CollaborationStateError(f"{field_name} must be a non-empty string")
    lowered = value.lower()
    for marker in _SECRET_MARKERS:
        if marker in lowered:
            raise CollaborationStateError(f"{field_name} must not contain secret-shaped content")


@dataclass(frozen=True)
class TraceSummary:
    """Closed, safe projection of one invocation record."""

    invocation_id: str
    status: str
    exit_code: int | None = None
    duration_ms: int | None = None

    def __post_init__(self) -> None:
        _assert_clean(self.invocation_id, "invocation_id", required=True)
        _assert_clean(self.status, "status", required=True)


@dataclass(frozen=True)
class CollaborationRecord:
    """One immutable ledger entry; the ledger assigns the sequence."""

    task_id: str
    correlation_id: str
    sequence: int
    direction: CollaborationDirection
    role: str = ""
    source_agent: str = ""
    target_agent: str = ""
    payload_type: str = ""
    wire: str = ""
    provenance: str = ""
    status: str = ""
    trace_summaries: tuple = ()
    mode: str = ""
    complexity: str = ""
    path: str = ""
    runtime_mode: str = ""
    reason: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.sequence, int) or isinstance(self.sequence, bool) \
                or self.sequence < 1:
            raise CollaborationStateError("sequence must be a positive integer")
        if not isinstance(self.direction, CollaborationDirection):
            raise CollaborationStateError("direction must be a CollaborationDirection")
        _assert_clean(self.task_id, "task_id", required=True)
        for name in ("correlation_id", "role", "source_agent", "target_agent",
                     "payload_type", "status", "mode", "complexity", "path",
                     "runtime_mode", "reason"):
            _assert_clean(getattr(self, name), name)
        _assert_clean(self.provenance, "provenance")
        if self.provenance not in _PROVENANCE_VALUES:
            raise CollaborationStateError("provenance must be OFFLINE or REAL")
        if not isinstance(self.wire, str):
            raise CollaborationStateError("wire must be a string")
        for summary in self.trace_summaries:
            if not isinstance(summary, TraceSummary):
                raise CollaborationStateError("trace summaries must be TraceSummary values")

    def envelope(self):
        """Fresh decode of the stored wire — a new value on every call."""
        if not self.wire:
            raise CollaborationStateError("record carries no envelope wire")
        return deserialize_collaboration_packet(self.wire)


class SharedCollaborationState:
    """Append-only ledger; every mutation returns a new instance."""

    def __init__(self, _records=None):
        self._records: dict[str, tuple] = dict(_records or {})

    def history(self, task_id: str) -> tuple:
        return self._records.get(task_id, ())

    def records_for(self, correlation_id: str) -> tuple:
        return tuple(
            record
            for records in self._records.values()
            for record in records
            if record.correlation_id == correlation_id
        )

    def failures(self, task_id: str) -> tuple:
        return tuple(
            record for record in self.history(task_id)
            if record.direction is CollaborationDirection.FAILURE
        )

    def append_envelope(self, task_id, envelope, direction, status, trace_summaries=()):
        if type(envelope) is not CollaborationPacket:
            # Never read attributes off an untyped object.
            raise CollaborationStateError("append requires a CollaborationPacket envelope")
        direction = CollaborationDirection(direction)
        if direction not in (CollaborationDirection.REQUEST, CollaborationDirection.REPLY):
            raise CollaborationStateError("envelope records must be REQUEST or REPLY")
        _assert_clean(task_id, "task_id", required=True)
        if envelope.task_id != task_id:
            raise CollaborationStateError("envelope task does not match the ledger task")
        self._check_invariants(task_id, envelope.correlation_id, direction)
        record = CollaborationRecord(
            task_id=task_id,
            correlation_id=envelope.correlation_id,
            sequence=len(self.history(task_id)) + 1,
            direction=direction,
            role=envelope.source_role,
            source_agent=envelope.source_agent,
            target_agent=envelope.target_agent,
            payload_type=envelope.payload_type.value,
            wire=serialize_collaboration_packet(envelope),
            provenance=envelope.provenance,
            status=status,
            trace_summaries=tuple(trace_summaries),
        )
        return self._with(task_id, record)

    def append_decision(self, task_id, mode, complexity, path, runtime_mode, reason):
        _assert_clean(task_id, "task_id", required=True)
        record = CollaborationRecord(
            task_id=task_id,
            correlation_id="",
            sequence=len(self.history(task_id)) + 1,
            direction=CollaborationDirection.DECISION,
            mode=mode,
            complexity=complexity,
            path=path,
            runtime_mode=runtime_mode,
            reason=reason,
        )
        return self._with(task_id, record)

    def append_failure(self, task_id, status, correlation_id="", envelope=None,
                       trace_summaries=()):
        _assert_clean(task_id, "task_id", required=True)
        _assert_clean(status, "status", required=True)
        derived = dict(role="", source_agent="", target_agent="",
                       payload_type="", wire="", provenance="")
        if envelope is not None:
            if type(envelope) is not CollaborationPacket:
                raise CollaborationStateError("append requires a CollaborationPacket envelope")
            if envelope.task_id != task_id:
                raise CollaborationStateError("envelope task does not match the ledger task")
            if correlation_id and envelope.correlation_id != correlation_id:
                raise CollaborationStateError("envelope correlation does not match")
            correlation_id = envelope.correlation_id
            derived = dict(
                role=envelope.source_role,
                source_agent=envelope.source_agent,
                target_agent=envelope.target_agent,
                payload_type=envelope.payload_type.value,
                wire=serialize_collaboration_packet(envelope),
                provenance=envelope.provenance,
            )
        if correlation_id:
            self._check_invariants(task_id, correlation_id, CollaborationDirection.FAILURE)
        record = CollaborationRecord(
            task_id=task_id,
            correlation_id=correlation_id,
            sequence=len(self.history(task_id)) + 1,
            direction=CollaborationDirection.FAILURE,
            status=status,
            trace_summaries=tuple(trace_summaries),
            **derived,
        )
        return self._with(task_id, record)

    def _check_invariants(self, task_id, correlation_id, direction) -> None:
        if not correlation_id:
            return
        existing = self.records_for(correlation_id)
        for record in existing:
            if record.task_id != task_id:
                raise CollaborationStateError("correlation is already bound to another task")
        if direction is CollaborationDirection.REQUEST:
            if any(record.direction is CollaborationDirection.REQUEST for record in existing):
                raise CollaborationStateError("duplicate REQUEST for correlation")
        if direction is CollaborationDirection.REPLY:
            if not any(record.direction is CollaborationDirection.REQUEST
                       for record in existing):
                raise CollaborationStateError("REPLY requires a prior REQUEST")
            if any(record.direction is CollaborationDirection.REPLY for record in existing):
                raise CollaborationStateError("duplicate REPLY for correlation")

    def _with(self, task_id, record):
        records = dict(self._records)
        records[task_id] = self.history(task_id) + (record,)
        return SharedCollaborationState(_records=records)
