"""V3.1-A: Remote Collaboration Contract — the remote boundary envelope.

RemoteEnvelope is the remote boundary: it wraps exactly one existing
CollaborationPacket as its payload and adds only what crossing a remote
boundary requires — a message id, a recipient-addressed role, agent
addresses for sender/recipient, and a protocol version. It deliberately
does NOT redefine collaboration business semantics: the packet inside
keeps its own frozen schema, validation and provenance; the envelope
never reaches into packet internals beyond the two namespace-free facts
it must keep consistent (correlation_id, target role).

Identity semantics: sender/recipient are Agent Addresses (the V3
agent:{agent_id}:{role} space, stable across runtime rebinding) — never
runtime identities. The runtime four-tuple has no place in this
contract; two agents on two runtimes address each other by WHO, and
rebinding changes the binding, never the address.

message_id identifies exactly one envelope on the wire; correlation_id
groups the envelopes of one request/response/handoff interaction and is
kept consistent with the payload packet's correlation_id. They are
distinct fields with distinct factories and never merge.

Serialization is deterministic UTF-8 JSON only (sort_keys, compact
separators); the payload travels as the packet's canonical wire text, so
the envelope stays schema-agnostic about the packet it carries. No
pickle, no binary, no runtime objects: the payload slot accepts a
CollaborationPacket value and nothing else.

Delivery and error vocabularies are closed and contract-level only:
DELIVERED means the boundary reached the peer side — never that the
agent executed anything. Transport-specific conditions (connections,
TLS, HTTP status) belong to the future transport layer, not here.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import Any, ClassVar
from uuid import uuid4

from collaboration_packet import (
    CollaborationPacket,
    PacketValidationError,
    deserialize_collaboration_packet,
    serialize_collaboration_packet,
)
from content_safety import SECRET_MARKERS, contains_unsafe_content

# Repo version convention (collaboration_packet.PROTOCOL_VERSION): exact
# match, no downgrade, no silent acceptance of unknown versions.
PROTOCOL_VERSION = "1.0"


class RemotePayloadType(str, Enum):
    """Closed payload vocabulary; exactly one business protocol exists."""

    COLLABORATION_PACKET = "COLLABORATION_PACKET"


class RemoteEnvelopeStatus(str, Enum):
    """Contract-level delivery semantics (V3.1-B maps receipts onto these).

    ACCEPTED — taken over the boundary, delivery to the peer not proven.
    DELIVERED — the peer boundary received it; still not "executed".
    REJECTED — refused by the boundary (malformed, policy, unknown target).
    FAILED — the boundary attempted delivery and the attempt failed.
    TIMEOUT — delivery did not complete inside the declared window.
    """

    ACCEPTED = "ACCEPTED"
    DELIVERED = "DELIVERED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"
    TIMEOUT = "TIMEOUT"


class RemoteEnvelopeErrorCode(str, Enum):
    """Closed cross-boundary failure vocabulary; no transport specifics."""

    INVALID_ENVELOPE = "INVALID_ENVELOPE"
    UNSUPPORTED_PROTOCOL = "UNSUPPORTED_PROTOCOL"
    UNKNOWN_AGENT = "UNKNOWN_AGENT"
    ROLE_UNAVAILABLE = "ROLE_UNAVAILABLE"
    DELIVERY_FAILED = "DELIVERY_FAILED"
    DELIVERY_TIMEOUT = "DELIVERY_TIMEOUT"
    REMOTE_EXECUTION_FAILED = "REMOTE_EXECUTION_FAILED"


class RemoteEnvelopeError(Exception):
    """Structured contract failure; judge by .category, never by text."""

    def __init__(self, category: RemoteEnvelopeErrorCode, detail: str = ""):
        if not isinstance(category, RemoteEnvelopeErrorCode):
            raise ValueError("unknown remote envelope error category")
        self.category = category
        self.detail = detail
        message = f"remote envelope error: {category.value}"
        if detail:
            message = f"{message}: {detail}"
        super().__init__(message)


def new_message_id() -> str:
    """Fresh message id; mirrors the house id-factory convention."""
    return f"remote-{uuid4().hex}"


def _assert_clean_header(value: Any, field_name: str) -> None:
    """Header discipline mirrors collaboration_packet's header scan, with
    the single content-safety authority (markers + credential shapes)."""
    if not isinstance(value, str) or not value.strip():
        raise RemoteEnvelopeError(
            RemoteEnvelopeErrorCode.INVALID_ENVELOPE,
            f"{field_name} must be a non-empty string")
    lowered = value.lower()
    for marker in SECRET_MARKERS:
        if marker in lowered:
            raise RemoteEnvelopeError(
                RemoteEnvelopeErrorCode.INVALID_ENVELOPE,
                f"{field_name} must not contain secret-shaped content")
    if contains_unsafe_content(value):
        raise RemoteEnvelopeError(
            RemoteEnvelopeErrorCode.INVALID_ENVELOPE,
            f"{field_name} must not contain secret-shaped content")


@dataclass(frozen=True)
class RemoteEnvelope:
    """One remote-boundary message; payload is an existing CollaborationPacket.

    The envelope owns only boundary facts. Two consistency facts are
    enforced against the payload so the pair can never diverge in flight:
    the envelope correlation_id equals the payload packet's correlation_id,
    and the envelope role equals the payload packet's target_role. Sender
    and recipient live in the agent-address space and are deliberately NOT
    cross-checked against the packet's source/target (the frozen V2 packet
    projects identities through the runtime-keyed address space; the remote
    boundary speaks agent addresses).
    """

    message_id: str
    correlation_id: str
    sender: str
    recipient: str
    role: str
    payload: Any
    payload_type: RemotePayloadType = RemotePayloadType.COLLABORATION_PACKET
    protocol_version: str = PROTOCOL_VERSION

    REQUIRED_FIELDS: ClassVar[tuple[str, ...]] = (
        "protocol_version", "message_id", "correlation_id", "sender",
        "recipient", "role", "payload", "payload_type",
    )

    def __post_init__(self) -> None:
        if not isinstance(self.payload_type, RemotePayloadType):
            raise RemoteEnvelopeError(
                RemoteEnvelopeErrorCode.INVALID_ENVELOPE,
                "payload_type must be a RemotePayloadType")
        if type(self.payload) is not CollaborationPacket:
            raise RemoteEnvelopeError(
                RemoteEnvelopeErrorCode.INVALID_ENVELOPE,
                "payload must be a CollaborationPacket")
        for field_name in ("message_id", "correlation_id", "sender",
                           "recipient", "role"):
            _assert_clean_header(getattr(self, field_name), field_name)
        if self.protocol_version != PROTOCOL_VERSION:
            raise RemoteEnvelopeError(
                RemoteEnvelopeErrorCode.UNSUPPORTED_PROTOCOL,
                f"unsupported remote protocol version: {self.protocol_version}")
        if self.sender == self.recipient:
            raise RemoteEnvelopeError(
                RemoteEnvelopeErrorCode.INVALID_ENVELOPE,
                "sender and recipient must differ")
        if self.payload.correlation_id != self.correlation_id:
            raise RemoteEnvelopeError(
                RemoteEnvelopeErrorCode.INVALID_ENVELOPE,
                "correlation_id does not match the payload correlation_id")
        if self.payload.target_role != self.role:
            raise RemoteEnvelopeError(
                RemoteEnvelopeErrorCode.INVALID_ENVELOPE,
                "role does not match the payload target role")

    def to_dict(self) -> dict:
        """JSON-native dict; payload is the packet's canonical wire text."""
        return {
            "protocol_version": self.protocol_version,
            "message_id": self.message_id,
            "correlation_id": self.correlation_id,
            "sender": self.sender,
            "recipient": self.recipient,
            "role": self.role,
            "payload_type": self.payload_type.value,
            "payload": serialize_collaboration_packet(self.payload),
        }

    @classmethod
    def from_dict(cls, data: Any) -> "RemoteEnvelope":
        if not isinstance(data, dict):
            raise RemoteEnvelopeError(
                RemoteEnvelopeErrorCode.INVALID_ENVELOPE,
                "remote envelope must be an object")
        missing = [name for name in cls.REQUIRED_FIELDS if name not in data]
        if missing:
            raise RemoteEnvelopeError(
                RemoteEnvelopeErrorCode.INVALID_ENVELOPE,
                f"missing required fields: {', '.join(missing)}")
        for name in ("protocol_version", "message_id", "correlation_id",
                     "sender", "recipient", "role", "payload"):
            if not isinstance(data[name], str):
                raise RemoteEnvelopeError(
                    RemoteEnvelopeErrorCode.INVALID_ENVELOPE,
                    f"{name} must be a string")
        if data["protocol_version"] != PROTOCOL_VERSION:
            raise RemoteEnvelopeError(
                RemoteEnvelopeErrorCode.UNSUPPORTED_PROTOCOL,
                "unsupported remote protocol version: "
                f"{data['protocol_version']}")
        try:
            payload_type = RemotePayloadType(data["payload_type"])
        except ValueError as exc:
            raise RemoteEnvelopeError(
                RemoteEnvelopeErrorCode.INVALID_ENVELOPE,
                "unknown payload type") from exc
        try:
            payload = deserialize_collaboration_packet(data["payload"])
            return cls(
                message_id=data["message_id"],
                correlation_id=data["correlation_id"],
                sender=data["sender"],
                recipient=data["recipient"],
                role=data["role"],
                payload_type=payload_type,
                payload=payload,
                protocol_version=data["protocol_version"],
            )
        except RemoteEnvelopeError:
            raise
        except (PacketValidationError, TypeError, ValueError) as exc:
            raise RemoteEnvelopeError(
                RemoteEnvelopeErrorCode.INVALID_ENVELOPE,
                "payload is not a valid collaboration packet") from exc


def serialize_remote_envelope(envelope: Any) -> str:
    """Canonical UTF-8 JSON wire text; deterministic for equal envelopes."""
    if type(envelope) is not RemoteEnvelope:
        raise RemoteEnvelopeError(
            RemoteEnvelopeErrorCode.INVALID_ENVELOPE,
            "unsupported remote envelope type")
    return json.dumps(
        envelope.to_dict(),
        sort_keys=True,
        separators=(",", ":"),
    )


def deserialize_remote_envelope(text: Any) -> RemoteEnvelope:
    try:
        data = json.loads(text)
    except (TypeError, ValueError) as exc:
        raise RemoteEnvelopeError(
            RemoteEnvelopeErrorCode.INVALID_ENVELOPE,
            "malformed remote envelope JSON") from exc
    return RemoteEnvelope.from_dict(data)
