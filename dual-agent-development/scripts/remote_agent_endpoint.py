"""V3.1-E: Remote agent endpoint — the far side of the remote seam.

A RemoteAgentEndpoint is the production translation boundary that lives
inside the child process of the V3.1 collaboration seam: it receives one
inbound RemoteEnvelope, turns its CollaborationPacket payload into one
ExternalAgentRequest for the adapter injected at construction, invokes
that adapter exactly once, parses the returned output through the
production packet discipline (fence strip, JSON parse, task-id override,
list normalization, from_dict, whole-packet safety scan), and answers
with a fresh reply RemoteEnvelope addressed back to the sender.

What it deliberately is not: a lifecycle owner (no open/close — the
transport owns the child process), a runtime chooser (the adapter
arrives already chosen, at construction), a parser of agent addresses
(they stay opaque strings end to end), or a multi-message coordinator
(each envelope is handled independently, with no memory between
handles, no clock, and no bookkeeping). Error mapping fires only on
typed facts: the closed envelope vocabulary for refusals, and the
invocation status vocabulary for execution failures. Raw model output
never escapes through an error detail.
"""
from __future__ import annotations

import json
import sys

from collaboration_packet import (
    CollaborationPacket,
    CollaborationPayloadType,
    PacketValidationError,
    serialize_collaboration_packet,
)
from content_safety import packet_has_unsafe_content
from external_runtime import ExternalAgentRequest, InvocationStatus
from remote_contract import (
    PROTOCOL_VERSION,
    RemoteEnvelope,
    RemoteEnvelopeError,
    RemoteEnvelopeErrorCode,
    RemotePayloadType,
    deserialize_remote_envelope,
    new_message_id,
    serialize_remote_envelope,
)
from structured_packets import ImplementationPacket

IMPLEMENTATION_INSTRUCTION = (
    "You are the coder for one small, read-only implementation task. "
    "The collaboration packet below is your complete input contract. "
    "Return ONLY a JSON object with exactly these keys: "
    "task_id, role, changed_files, implementation_summary, "
    "implementation_details, assumptions, unresolved_items, "
    "test_requirements. task_id must equal the packet task_id; "
    'role must be "coder". task_id, role and implementation_summary are '
    "strings. changed_files, implementation_details, assumptions, "
    "unresolved_items, test_requirements are arrays of strings. "
    "No prose, no markdown fences. Do not modify files or run commands."
    "\n\nCollaboration packet:\n"
)

# The closed per-role translation table: role -> (instruction, output
# payload type, output packet class). One row today; roles outside the
# table are refused with the structured ROLE_UNAVAILABLE fact.
_ROLE_CONTRACTS = {
    "coder": (
        IMPLEMENTATION_INSTRUCTION,
        CollaborationPayloadType.IMPLEMENTATION,
        ImplementationPacket,
    ),
}

# List fields normalized before from_dict — the V2 vocabulary verbatim.
_LIST_FIELDS = (
    "goal", "constraints", "architecture", "interfaces",
    "implementation_steps", "acceptance_criteria", "risks",
    "changed_files", "implementation_details", "assumptions",
    "unresolved_items", "test_requirements", "findings", "severity",
    "affected_files", "required_changes", "acceptance_criteria_status",
    "tests_run", "tests_passed", "tests_failed", "failures",
    "coverage_or_validation", "remaining_risks",
)

_ERROR_OUTPUT_INVALID = (
    "invocation output did not satisfy the role packet contract")
_ERROR_NO_TRACE = "invocation failed without trace"


class RemoteAgentEndpoint:
    """One envelope-to-invocation translation context over one adapter."""

    def __init__(self, adapter, address: str, *,
                 timeout_seconds: float = 120.0,
                 provenance: str = "OFFLINE"):
        if not isinstance(address, str) or not address:
            raise RemoteEnvelopeError(
                RemoteEnvelopeErrorCode.INVALID_ENVELOPE,
                "address must be a non-empty string")
        if not isinstance(timeout_seconds, (int, float)) \
                or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be a positive number")
        if provenance not in ("OFFLINE", "REAL"):
            raise ValueError("provenance must be OFFLINE or REAL")
        self._adapter = adapter
        self._address = address
        self._timeout_seconds = float(timeout_seconds)
        self._provenance = provenance

    @property
    def address(self) -> str:
        return self._address

    def handle(self, envelope: RemoteEnvelope) -> RemoteEnvelope:
        # Gate 1 — the value really is an envelope.
        if type(envelope) is not RemoteEnvelope:
            raise RemoteEnvelopeError(
                RemoteEnvelopeErrorCode.INVALID_ENVELOPE,
                "envelope must be a RemoteEnvelope")
        # Gate 2 — this message is addressed to this endpoint.
        if envelope.recipient != self._address:
            raise RemoteEnvelopeError(
                RemoteEnvelopeErrorCode.INVALID_ENVELOPE,
                "envelope is addressed to another recipient")
        # Gate 3 — this endpoint serves a contract for that role.
        instruction, output_type, packet_class = _ROLE_CONTRACTS.get(
            envelope.role, (None, None, None))
        if instruction is None:
            raise RemoteEnvelopeError(
                RemoteEnvelopeErrorCode.ROLE_UNAVAILABLE,
                f"no role contract for role: {envelope.role}")
        request = ExternalAgentRequest(
            task_id=envelope.payload.task_id,
            prompt=instruction
            + serialize_collaboration_packet(envelope.payload),
            agent_id=self._address,
            role=envelope.role,
            provider=None,
            model=None,
            timeout_seconds=self._timeout_seconds,
            handoff_packets=(),
        )
        result = self._adapter.invoke(request)
        if result.status is not InvocationStatus.SUCCESS:
            detail = _ERROR_NO_TRACE
            if result.trace is not None and result.trace.error:
                detail = result.trace.error
            raise RemoteEnvelopeError(
                RemoteEnvelopeErrorCode.REMOTE_EXECUTION_FAILED, detail)
        parsed = _output_to_packet(result.output, packet_class,
                                   envelope.payload.task_id)
        reply_packet = CollaborationPacket(
            correlation_id=envelope.correlation_id,
            task_id=envelope.payload.task_id,
            source_agent=self._address,
            target_agent=envelope.sender,
            source_role=envelope.role,
            target_role=envelope.payload.source_role,
            payload_type=output_type,
            payload=parsed,
            acceptance_criteria=parsed.test_requirements,
            provenance=self._provenance,
        )
        return RemoteEnvelope(
            message_id=new_message_id(),
            correlation_id=envelope.correlation_id,
            sender=self._address,
            recipient=envelope.sender,
            role=reply_packet.target_role,
            payload=reply_packet,
            payload_type=RemotePayloadType.COLLABORATION_PACKET,
            protocol_version=PROTOCOL_VERSION,
        )


def _normalize(data: dict) -> dict:
    """V2-equivalent list-field normalization (string -> one element)."""
    for key in _LIST_FIELDS:
        if key in data and isinstance(data[key], str):
            data[key] = [data[key]]
    return data


def _output_to_packet(output, packet_class, task_id):
    """The production parsing discipline over one invocation output.

    Any failure raises the typed execution error with a closed detail —
    the raw output never appears in the message."""
    text = output.strip() if isinstance(output, str) else ""
    if text.startswith("```"):
        first_newline = text.find("\n")
        text = text[first_newline + 1:] if first_newline != -1 else ""
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        raise RemoteEnvelopeError(
            RemoteEnvelopeErrorCode.REMOTE_EXECUTION_FAILED,
            _ERROR_OUTPUT_INVALID) from None
    if not isinstance(data, dict):
        raise RemoteEnvelopeError(
            RemoteEnvelopeErrorCode.REMOTE_EXECUTION_FAILED,
            _ERROR_OUTPUT_INVALID)
    # Task authority: whatever the model echoed, the packet belongs to
    # the inbound task by construction.
    data["task_id"] = task_id
    try:
        packet = packet_class.from_dict(_normalize(data))
    except (PacketValidationError, TypeError, KeyError, ValueError):
        raise RemoteEnvelopeError(
            RemoteEnvelopeErrorCode.REMOTE_EXECUTION_FAILED,
            _ERROR_OUTPUT_INVALID) from None
    if packet_has_unsafe_content(packet):
        raise RemoteEnvelopeError(
            RemoteEnvelopeErrorCode.REMOTE_EXECUTION_FAILED,
            _ERROR_OUTPUT_INVALID)
    return packet


def run_endpoint(adapter, address, input_stream, output_stream) -> None:
    """The child-side wire loop over one endpoint.

    Reads canonical JSONL envelopes from the binary input stream; for
    each, writes the reply envelope line (flushed) and then the
    acknowledgement line (flushed) — the response is always on the wire
    before its ACK. A typed endpoint error writes no ACK, emits one
    sanitized stderr diagnostic, and exits non-zero; end of input ends
    the loop normally.
    """
    endpoint = RemoteAgentEndpoint(adapter, address)
    for raw in input_stream:
        if not raw.strip():
            continue
        try:
            inbound = deserialize_remote_envelope(raw.decode("utf-8"))
            reply = endpoint.handle(inbound)
        except RemoteEnvelopeError as exc:
            sys.stderr.write(f"endpoint error: {exc.category.value}"
                             f": {exc.detail}\n")
            raise SystemExit(1) from None
        output_stream.write(
            serialize_remote_envelope(reply).encode("utf-8") + b"\n")
        output_stream.flush()
        output_stream.write(
            json.dumps({"delivered": inbound.message_id},
                       sort_keys=True,
                       separators=(",", ":")).encode("utf-8") + b"\n")
        output_stream.flush()
