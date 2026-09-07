"""V3.1-C1: Remote agent session — one interaction context above delivery.

A RemoteAgentSession owns exactly one interaction between a local and a
remote agent address over one injected envelope transport: it binds the
two addresses, holds the interaction's single correlation id, wraps
caller-built CollaborationPacket values into RemoteEnvelope values
(minting a fresh message id per send through the contract's own
factory), hands each envelope to the transport and returns the receipt
untouched, polls the local mailbox, and refuses any inbound envelope
that is not from this remote address, for this recipient, inside this
interaction. It manages exactly the OPEN/CLOSED lifecycle of that
context and delegates close to the transport's own cleanup authority.

What it deliberately is not: a transport (delivery facts belong to the
injected seam), a driver of the far side (nothing here invokes it), or
a stage planner (no budgets, no guards, no ordering, and no business
reading of packet payloads). It holds no history and no pending-message
bookkeeping — the envelopes and the transport's mailboxes are the only
record. Receipts never mutate the session; only close() changes its
state. A resent envelope is the caller's choice under a fresh message
id; this module never re-sends, re-judges or rewrites a receipt.

Correlation discipline: the interaction's id is minted once at
construction (or accepted verbatim when provided) and stamped on every
outbound envelope; a packet whose own correlation id belongs to another
interaction is refused by the envelope contract itself and that error
surfaces as-is. Inbound envelopes carrying a foreign correlation are
refused with the contract's structured error — never silently dropped,
never re-routed, never repaired. Use of a closed session is likewise a
typed refusal in the same structured vocabulary, never a fabricated
receipt and never a silent empty poll.

Addresses are opaque strings in the agent-address space; this module
never parses them and never touches identity, binding or capability
concepts. It performs no network operations of any kind, holds no
credentials, and imports nothing beyond the packet factory, the remote
contract and the transport seam.
"""
from __future__ import annotations

from collaboration_packet import CollaborationPacket, new_correlation_id
from remote_contract import (
    RemoteEnvelope,
    RemoteEnvelopeError,
    RemoteEnvelopeErrorCode,
    new_message_id,
)
from remote_envelope_transport import (
    RemoteEnvelopeReceipt,
    RemoteEnvelopeTransport,
)


class RemoteAgentSession:
    """One interaction context between two agent addresses."""

    def __init__(self, local_address: str, remote_address: str,
                 transport: RemoteEnvelopeTransport,
                 correlation_id: str | None = None):
        for name, value in (("local_address", local_address),
                            ("remote_address", remote_address)):
            if not isinstance(value, str) or not value.strip():
                raise RemoteEnvelopeError(
                    RemoteEnvelopeErrorCode.INVALID_ENVELOPE,
                    f"{name} must be a non-empty string")
        if local_address == remote_address:
            raise RemoteEnvelopeError(
                RemoteEnvelopeErrorCode.INVALID_ENVELOPE,
                "local_address and remote_address must differ")
        if not isinstance(transport, RemoteEnvelopeTransport):
            raise RemoteEnvelopeError(
                RemoteEnvelopeErrorCode.INVALID_ENVELOPE,
                "transport must conform to RemoteEnvelopeTransport "
                "(send, receive, close)")
        if correlation_id is not None and (
                not isinstance(correlation_id, str)
                or not correlation_id.strip()):
            raise RemoteEnvelopeError(
                RemoteEnvelopeErrorCode.INVALID_ENVELOPE,
                "correlation_id must be a non-empty string when provided")
        self.local_address = local_address
        self.remote_address = remote_address
        self.correlation_id = (correlation_id if correlation_id is not None
                               else new_correlation_id())
        self._transport = transport
        self._closed = False

    def send(self, packet: CollaborationPacket) -> RemoteEnvelopeReceipt:
        """Wrap one packet and hand it to the transport; return its receipt."""
        if self._closed:
            raise RemoteEnvelopeError(
                RemoteEnvelopeErrorCode.INVALID_ENVELOPE, "session is closed")
        if type(packet) is not CollaborationPacket:
            # Never read attributes off an untyped object; the refusal
            # uses the boundary's own structured vocabulary.
            raise RemoteEnvelopeError(
                RemoteEnvelopeErrorCode.INVALID_ENVELOPE,
                "send requires a CollaborationPacket")
        envelope = RemoteEnvelope(
            message_id=new_message_id(),
            correlation_id=self.correlation_id,
            sender=self.local_address,
            recipient=self.remote_address,
            role=packet.target_role,
            payload=packet,
        )
        return self._transport.send(envelope)

    def receive(self) -> RemoteEnvelope | None:
        """Poll the local mailbox; refuse anything not from this interaction."""
        if self._closed:
            raise RemoteEnvelopeError(
                RemoteEnvelopeErrorCode.INVALID_ENVELOPE, "session is closed")
        envelope = self._transport.receive(self.local_address)
        if envelope is None:
            return None
        if envelope.recipient != self.local_address:
            raise RemoteEnvelopeError(
                RemoteEnvelopeErrorCode.INVALID_ENVELOPE,
                "inbound envelope is addressed to another recipient")
        if envelope.sender != self.remote_address:
            raise RemoteEnvelopeError(
                RemoteEnvelopeErrorCode.INVALID_ENVELOPE,
                "inbound envelope is not from this session's remote address")
        if envelope.correlation_id != self.correlation_id:
            raise RemoteEnvelopeError(
                RemoteEnvelopeErrorCode.INVALID_ENVELOPE,
                "inbound envelope belongs to another interaction")
        return envelope

    def close(self) -> None:
        """End the interaction; the transport close is the cleanup authority."""
        if self._closed:
            return
        self._closed = True
        self._transport.close()
