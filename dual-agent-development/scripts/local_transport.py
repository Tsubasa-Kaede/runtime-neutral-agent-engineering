"""In-process local collaboration transport (Phase 10H-B).

Carries CollaborationPacket values between agents inside one process:
send validates the full wire round-trip (the decoded value must equal the
sent value exactly), stores the canonical wire text per target mailbox,
and receive hands back an equal, freshly decoded packet. Delivery is by
value, first-in-first-out per target, strictly isolated between targets.
Rejections are receipt values, never exceptions, and never enqueue. This
layer knows nothing about execution backends: it performs no calls, reads
no configuration, mints no identifiers and never touches a clock. Queue
growth is unbounded by design — a local deterministic transport has no
capacity semantics.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from enum import Enum

from collaboration_packet import (
    CollaborationPacket,
    PacketValidationError,
    deserialize_collaboration_packet,
    serialize_collaboration_packet,
)

_SECRET_MARKERS = ("token", "secret", "api_key", "authorization", "bearer", "stdout", "stderr")


class DeliveryStatus(str, Enum):
    DELIVERED = "DELIVERED"
    REJECTED_NOT_A_PACKET = "REJECTED_NOT_A_PACKET"
    REJECTED_INVALID_PACKET = "REJECTED_INVALID_PACKET"


def _assert_receipt_field_clean(value, field_name: str) -> None:
    if not isinstance(value, str):
        raise PacketValidationError(f"{field_name} must be a string")
    lowered = value.lower()
    for marker in _SECRET_MARKERS:
        if marker in lowered:
            raise PacketValidationError(f"{field_name} must not contain secret-shaped content")


@dataclass(frozen=True)
class DeliveryReceipt:
    """Value-shaped delivery outcome; empty ids mean "not echoable"."""

    status: DeliveryStatus
    correlation_id: str = ""
    target_agent: str = ""

    def __post_init__(self) -> None:
        _assert_receipt_field_clean(self.correlation_id, "correlation_id")
        _assert_receipt_field_clean(self.target_agent, "target_agent")


class LocalTransport:
    """Deterministic in-memory mailbox transport over the wire contract."""

    def __init__(self) -> None:
        self._mailboxes: dict[str, deque] = {}

    def send(self, packet) -> DeliveryReceipt:
        if type(packet) is not CollaborationPacket:
            # Never read attributes off an untyped object: contents may be
            # unsafe to echo, or even unsafe to access.
            return DeliveryReceipt(DeliveryStatus.REJECTED_NOT_A_PACKET)
        try:
            wire = serialize_collaboration_packet(packet)
            decoded = deserialize_collaboration_packet(wire)
        except (TypeError, ValueError, RecursionError):
            return DeliveryReceipt(DeliveryStatus.REJECTED_INVALID_PACKET,
                                   packet.correlation_id, packet.target_agent)
        if decoded != packet:
            # The round-trip silently changed the value (stringified dict
            # keys, list-to-tuple drift, NaN): untransportable as-is.
            return DeliveryReceipt(DeliveryStatus.REJECTED_INVALID_PACKET,
                                   packet.correlation_id, packet.target_agent)
        self._mailboxes.setdefault(packet.target_agent, deque()).append(wire)
        return DeliveryReceipt(DeliveryStatus.DELIVERED,
                               packet.correlation_id, packet.target_agent)

    def receive(self, agent_id: str):
        mailbox = self._mailboxes.get(agent_id)
        if not mailbox:
            return None
        wire = mailbox.popleft()
        # A decode failure here is impossible for wires accepted by send();
        # if it ever happened it must surface, never masquerade as empty.
        return deserialize_collaboration_packet(wire)

    def pending(self, agent_id: str) -> int:
        return len(self._mailboxes.get(agent_id, ()))
