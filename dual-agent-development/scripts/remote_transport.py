"""Remote collaboration transport contract with a loopback implementation.

Protocol-neutral, execution-neutral boundary: a RemoteTransport moves
CollaborationPacket values across a remote boundary as canonical wire
text. DELIVERED means accepted by the exchange — never that the peer
consumed the message. Ordering is not part of this contract; the
loopback below happens to deliver first-in-first-out per target and
declares so itself. Expected remote conditions become receipt values;
corrupted stored text surfaces as an honest decode error; programmer
mistakes raise. This module performs no calls, reads no configuration,
holds no credentials and never mints identifiers.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable

from collaboration_packet import (
    CollaborationPacket,
    PacketValidationError,
    deserialize_collaboration_packet,
    serialize_collaboration_packet,
)

_SECRET_MARKERS = ("token", "secret", "api_key", "authorization", "bearer", "stdout", "stderr")

_REMOTE_CATEGORIES = (
    "REMOTE_UNAVAILABLE",
    "REMOTE_TIMEOUT",
    "REMOTE_REJECTED",
    "REMOTE_PROTOCOL_ERROR",
    "AUTH_REQUIRED",
)


class RemoteDeliveryStatus(str, Enum):
    DELIVERED = "DELIVERED"
    REJECTED_NOT_A_PACKET = "REJECTED_NOT_A_PACKET"
    REJECTED_INVALID_PACKET = "REJECTED_INVALID_PACKET"
    REMOTE_UNAVAILABLE = "REMOTE_UNAVAILABLE"
    REMOTE_TIMEOUT = "REMOTE_TIMEOUT"
    REMOTE_REJECTED = "REMOTE_REJECTED"
    REMOTE_PROTOCOL_ERROR = "REMOTE_PROTOCOL_ERROR"
    AUTH_REQUIRED = "AUTH_REQUIRED"


class RemoteExchangeError(Exception):
    """Closed-vocabulary failure raised by an exchange (peer/protocol side)."""

    def __init__(self, category: str):
        if category not in _REMOTE_CATEGORIES:
            raise ValueError("unknown exchange category")
        self.category = category
        super().__init__(f"exchange failed: {category}")


def _assert_receipt_field_clean(value, field_name: str) -> None:
    if not isinstance(value, str):
        raise PacketValidationError(f"{field_name} must be a string")
    lowered = value.lower()
    for marker in _SECRET_MARKERS:
        if marker in lowered:
            raise PacketValidationError(f"{field_name} must not contain secret-shaped content")


@dataclass(frozen=True)
class RemoteDeliveryReceipt:
    """Value-shaped delivery outcome; empty ids mean "not echoable"."""

    status: RemoteDeliveryStatus
    correlation_id: str = ""
    target_agent: str = ""

    def __post_init__(self) -> None:
        _assert_receipt_field_clean(self.correlation_id, "correlation_id")
        _assert_receipt_field_clean(self.target_agent, "target_agent")


@runtime_checkable
class RemoteTransport(Protocol):
    """Contract: acceptance semantics only — no ordering, no queue depth."""

    def send(self, packet) -> RemoteDeliveryReceipt: ...

    def receive(self, agent_id: str): ...


class LoopbackRemoteTransport:
    """In-process production seam implementing the remote contract.

    Declares first-in-first-out per target. The injected exchange models
    the far side: it may sink zero, one or many wires (drop, duplicate),
    sink damaged text, or raise a typed category error. The default
    exchange simply sinks the validated wire once.
    """

    def __init__(self, exchange=None):
        self._mailboxes: dict[str, deque] = {}
        self._exchange = exchange or self._default_exchange

    @staticmethod
    def _default_exchange(target_agent: str, wire: str, sink) -> None:
        sink(target_agent, wire)

    def _enqueue(self, target_agent: str, wire: str) -> None:
        self._mailboxes.setdefault(target_agent, deque()).append(wire)

    def send(self, packet) -> RemoteDeliveryReceipt:
        if type(packet) is not CollaborationPacket:
            # Never read attributes off an untyped object.
            return RemoteDeliveryReceipt(RemoteDeliveryStatus.REJECTED_NOT_A_PACKET)
        try:
            wire = serialize_collaboration_packet(packet)
            decoded = deserialize_collaboration_packet(wire)
        except (TypeError, ValueError, RecursionError):
            return RemoteDeliveryReceipt(RemoteDeliveryStatus.REJECTED_INVALID_PACKET,
                                         packet.correlation_id, packet.target_agent)
        if decoded != packet:
            return RemoteDeliveryReceipt(RemoteDeliveryStatus.REJECTED_INVALID_PACKET,
                                         packet.correlation_id, packet.target_agent)
        try:
            self._exchange(packet.target_agent, wire, self._enqueue)
        except RemoteExchangeError as exc:
            return RemoteDeliveryReceipt(RemoteDeliveryStatus(exc.category),
                                         packet.correlation_id, packet.target_agent)
        return RemoteDeliveryReceipt(RemoteDeliveryStatus.DELIVERED,
                                     packet.correlation_id, packet.target_agent)

    def receive(self, agent_id: str):
        mailbox = self._mailboxes.get(agent_id)
        if not mailbox:
            return None
        wire = mailbox.popleft()
        # Corrupted stored text must surface, never masquerade as empty.
        return deserialize_collaboration_packet(wire)
