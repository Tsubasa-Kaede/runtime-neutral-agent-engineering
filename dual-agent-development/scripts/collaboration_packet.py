"""Immutable, secret-free, runtime-neutral agent-to-agent collaboration envelope.

CollaborationPacket wraps exactly one of the four business packets
(Architecture/Implementation/Test/Review) as the uniform communication
contract between agents. It expresses "which role owes what work to which
role" — never "which runtime or model must be used". Runtime selection,
health and validation stay in their own layers; this module performs no
invocation, reads no environment, and never upgrades provenance.

Identity: source_agent/target_agent are opaque strings by design; callers
must pass the sanctioned agent-id projection of the full identity (the
house agent_id_for helper). The envelope deliberately carries no
runtime/provider/model fields, so the projection string is the only
identity representation on the wire. correlation_id links one request to
its eventual response; task_id names the whole user task. Transport,
retries and asynchronous delivery are future phases — this is the
contract only.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import Any, ClassVar
from uuid import uuid4

from content_safety import (
    ValidationDiagnostic,
    contains_unsafe_content,
    record_validation_diagnostic,
)
from structured_packets import (
    ArchitecturePacket,
    ImplementationPacket,
    PacketValidationError,
    ReviewPacket,
    TestPacket,
    deserialize_packet,
    serialize_packet,
)

PROTOCOL_VERSION = "1.0"

_SECRET_MARKERS = ("token", "secret", "api_key", "authorization", "bearer", "stdout", "stderr")


class CollaborationPayloadType(str, Enum):
    ARCHITECTURE = "ARCHITECTURE"
    IMPLEMENTATION = "IMPLEMENTATION"
    TEST = "TEST"
    REVIEW = "REVIEW"


_PAYLOAD_CLASSES = {
    CollaborationPayloadType.ARCHITECTURE: ArchitecturePacket,
    CollaborationPayloadType.IMPLEMENTATION: ImplementationPacket,
    CollaborationPayloadType.TEST: TestPacket,
    CollaborationPayloadType.REVIEW: ReviewPacket,
}


def new_correlation_id() -> str:
    """Fresh correlation id; mirrors the house id-factory convention."""
    return f"collab-{uuid4().hex}"


def _assert_clean_text(value: Any, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise PacketValidationError(f"{field_name} must be a non-empty string")
    lowered = value.lower()
    for marker in _SECRET_MARKERS:
        if marker in lowered:
            raise PacketValidationError(f"{field_name} must not contain secret-shaped content")


@dataclass(frozen=True)
class CollaborationPacket:
    """One agent-to-agent handoff envelope; payload is a business packet."""

    correlation_id: str
    task_id: str
    source_agent: str
    target_agent: str
    source_role: str
    target_role: str
    payload_type: CollaborationPayloadType
    payload: Any
    acceptance_criteria: tuple[str, ...] = ()
    protocol_version: str = PROTOCOL_VERSION
    provenance: str = "OFFLINE"

    REQUIRED_FIELDS: ClassVar[tuple[str, ...]] = (
        "correlation_id", "task_id", "source_agent", "target_agent",
        "source_role", "target_role", "payload_type", "payload",
    )

    def __post_init__(self) -> None:
        if not isinstance(self.payload_type, CollaborationPayloadType):
            raise PacketValidationError("payload_type must be a CollaborationPayloadType")
        expected = _PAYLOAD_CLASSES[self.payload_type]
        if type(self.payload) is not expected:
            raise PacketValidationError("payload does not match payload_type")
        if getattr(self.payload, "task_id", None) != self.task_id:
            raise PacketValidationError("payload task_id does not match the envelope task_id")
        if self.source_agent == self.target_agent:
            raise PacketValidationError("source_agent and target_agent must differ")
        for field_name in ("correlation_id", "task_id", "source_agent",
                           "target_agent", "source_role", "target_role"):
            _assert_clean_text(getattr(self, field_name), field_name)
        if not isinstance(self.acceptance_criteria, tuple):
            raise PacketValidationError("acceptance_criteria must be a tuple")
        for index, item in enumerate(self.acceptance_criteria):
            if not isinstance(item, str):
                raise PacketValidationError("acceptance_criteria items must be strings")
            # R6-C10: criteria items are MODEL PROSE copied from the
            # architect/coder packet, which already rejected credential
            # shapes and already ran the whole-packet scan. They follow
            # the shared G15 two-tier authority (shape in values), not
            # the historical bare-substring word ban — otherwise legal
            # technical prose ("no token input") dies here and surfaces
            # as an unexplained ARCHITECT_PACKET_INVALID. The header
            # fields above keep the strict scan: they are
            # protocol-generated, never model prose.
            if contains_unsafe_content(item):
                # R6-C11: the REJECT stays a REJECT; it is now observable
                # as layer/field/index/rule — the rejected text itself
                # never leaves the scanner.
                record_validation_diagnostic(
                    ValidationDiagnostic(
                        "envelope", "acceptance_criteria", index,
                        "UNSAFE_SHAPE"))
                raise PacketValidationError(
                    "acceptance_criteria must not contain secret-shaped content")
        if self.protocol_version != PROTOCOL_VERSION:
            raise PacketValidationError("unsupported protocol version")
        if self.provenance not in ("OFFLINE", "REAL"):
            raise PacketValidationError("provenance must be OFFLINE or REAL")

    @classmethod
    def from_dict(cls, data: Any) -> "CollaborationPacket":
        if not isinstance(data, dict):
            raise PacketValidationError("collaboration packet must be an object")
        missing = [name for name in cls.REQUIRED_FIELDS if name not in data]
        if missing:
            raise PacketValidationError(f"missing required fields: {', '.join(missing)}")
        criteria = data.get("acceptance_criteria", ())
        if not isinstance(criteria, (list, tuple)):
            raise PacketValidationError("acceptance_criteria must be a list")
        try:
            payload_type = CollaborationPayloadType(data["payload_type"])
        except ValueError as exc:
            raise PacketValidationError("unknown payload type") from exc
        return cls(
            correlation_id=data["correlation_id"],
            task_id=data["task_id"],
            source_agent=data["source_agent"],
            target_agent=data["target_agent"],
            source_role=data["source_role"],
            target_role=data["target_role"],
            payload_type=payload_type,
            payload=deserialize_packet(data["payload"]),
            acceptance_criteria=tuple(criteria),
            protocol_version=data.get("protocol_version", PROTOCOL_VERSION),
            provenance=data.get("provenance", "OFFLINE"),
        )


def serialize_collaboration_packet(packet: Any) -> str:
    if type(packet) is not CollaborationPacket:
        raise PacketValidationError("unsupported packet type")
    return json.dumps(
        {
            "protocol_version": packet.protocol_version,
            "correlation_id": packet.correlation_id,
            "task_id": packet.task_id,
            "source_agent": packet.source_agent,
            "target_agent": packet.target_agent,
            "source_role": packet.source_role,
            "target_role": packet.target_role,
            "payload_type": packet.payload_type.value,
            "payload": serialize_packet(packet.payload),
            "acceptance_criteria": list(packet.acceptance_criteria),
            "provenance": packet.provenance,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def deserialize_collaboration_packet(payload: str) -> CollaborationPacket:
    try:
        data = json.loads(payload)
    except (TypeError, json.JSONDecodeError) as exc:
        raise PacketValidationError("invalid collaboration packet JSON") from exc
    return CollaborationPacket.from_dict(data)
