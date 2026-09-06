"""V3.1-B1: Loopback envelope transport — the envelope delivery seam.

This seam moves RemoteEnvelope values (V3.1-A) across an in-process,
offline boundary. It owns exactly the delivery boundary and nothing else:

- Wire discipline: the mailbox stores canonical UTF-8 JSON wire text
  (the contract's own serializer — sort_keys, compact separators) and
  never an object reference; receive() decodes a fresh envelope from
  that text. Message/correlation identity is supplied upstream by the
  contract and echoed, never generated here.
- Status honesty: the loopback is synchronous and atomic — a send that
  returns has either sunk the wire (DELIVERED) or not. ACCEPTED (taken
  over the boundary, delivery unproven) describes a transport with
  deferred delivery and can never honestly be produced by this one.
- One mapping authority: expected far-side conditions surface as
  RemoteExchangeError categories (the V2 exchange vocabulary) and map
  onto receipt statuses through the single explicit mapping below —
  no scattered if/elif second rulebook. Session/execution-layer codes
  (UNKNOWN_AGENT, ROLE_UNAVAILABLE, REMOTE_EXECUTION_FAILED) are not
  delivery facts; this seam does not own them and never raises or
  maps them.
- Honest failure: corrupted stored wire surfaces on receive() as a
  structured RemoteEnvelopeError — never as an empty mailbox.

The recipient address is an opaque routing key (agent-address space);
the transport never parses agent ids, roles, runtimes, providers or
models out of it, and makes no business decision from the payload.
This module performs no network or process calls, reads no
configuration, holds no credentials, and imports stdlib only.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Mapping, Protocol, runtime_checkable

from remote_contract import (
    RemoteEnvelope,
    RemoteEnvelopeError,
    RemoteEnvelopeErrorCode,
    RemoteEnvelopeStatus,
    deserialize_remote_envelope,
    serialize_remote_envelope,
)
from remote_transport import RemoteExchangeError

# The single mapping authority (§ delivery boundary): every closed
# RemoteExchangeError category → (receipt status, contract error code).
# REJECTED-refusals carry code None because the contract's closed envelope
# vocabulary has no peer-refusal code; forcing one would invent semantics.
EXCHANGE_CATEGORY_TO_RECEIPT: Mapping[str, tuple] = {
    "REMOTE_UNAVAILABLE": (
        RemoteEnvelopeStatus.FAILED, RemoteEnvelopeErrorCode.DELIVERY_FAILED),
    "REMOTE_PROTOCOL_ERROR": (
        RemoteEnvelopeStatus.FAILED, RemoteEnvelopeErrorCode.DELIVERY_FAILED),
    "REMOTE_TIMEOUT": (
        RemoteEnvelopeStatus.TIMEOUT, RemoteEnvelopeErrorCode.DELIVERY_TIMEOUT),
    "REMOTE_REJECTED": (RemoteEnvelopeStatus.REJECTED, None),
    "AUTH_REQUIRED": (RemoteEnvelopeStatus.REJECTED, None),
}


@dataclass(frozen=True)
class RemoteEnvelopeReceipt:
    """Value-shaped outcome of one send(); empty ids mean "not echoable".

    Every field earns its place:
    - status — the only judgment the delivery boundary may make.
    - message_id / correlation_id — echo the wire identity of exactly one
      envelope and its interaction grouping when a readable envelope was
      handed in; empty when the input was not an envelope and nothing
      else is knowable.
    - recipient — the routing key the boundary addressed (agent-address
      space; opaque to the transport).
    - error_code — why a non-DELIVERED receipt happened, in the contract's
      closed vocabulary; None when there is nothing to name (DELIVERED,
      or a peer refusal the closed set has no code for).

    No runtime objects, no adapters, no credentials, no timestamps:
    deterministic and comparable. Header cleanliness is enforced once,
    upstream, by the contract's own header scan; the receipt never
    re-scans (single content-safety authority).
    """

    status: RemoteEnvelopeStatus
    message_id: str = ""
    correlation_id: str = ""
    recipient: str = ""
    error_code: "RemoteEnvelopeErrorCode | None" = None

    def __post_init__(self) -> None:
        # Closed-vocabulary discipline: a receipt can never carry an
        # arbitrary string in either judgment field.
        if not isinstance(self.status, RemoteEnvelopeStatus):
            raise ValueError("receipt status must be a RemoteEnvelopeStatus")
        if self.error_code is not None and not isinstance(
                self.error_code, RemoteEnvelopeErrorCode):
            raise ValueError(
                "receipt error_code must be a RemoteEnvelopeErrorCode "
                "or None")


@runtime_checkable
class RemoteEnvelopeTransport(Protocol):
    """Delivery-boundary contract: send one envelope, poll one mailbox."""

    def send(self, envelope) -> RemoteEnvelopeReceipt: ...

    def receive(self, recipient: str): ...


class LoopbackEnvelopeTransport:
    """In-process, offline envelope delivery seam.

    Declares first-in-first-out per recipient. The injected exchange
    models the far side: it may sink zero, one or many wires (drop,
    duplicate), sink damaged text, or raise a typed RemoteExchangeError.
    The default exchange simply sinks the validated wire once.
    """

    def __init__(self, exchange=None):
        self._mailboxes: dict = {}
        self._exchange = exchange or self._default_exchange

    @staticmethod
    def _default_exchange(recipient: str, wire: str, sink) -> None:
        sink(recipient, wire)

    def _enqueue(self, recipient: str, wire: str) -> None:
        self._mailboxes.setdefault(recipient, deque()).append(wire)

    def send(self, envelope) -> RemoteEnvelopeReceipt:
        if type(envelope) is not RemoteEnvelope:
            # Never read attributes off an untyped object.
            return RemoteEnvelopeReceipt(
                RemoteEnvelopeStatus.REJECTED,
                error_code=RemoteEnvelopeErrorCode.INVALID_ENVELOPE)
        echo = {
            "message_id": envelope.message_id,
            "correlation_id": envelope.correlation_id,
            "recipient": envelope.recipient,
        }
        try:
            wire = serialize_remote_envelope(envelope)
            decoded = deserialize_remote_envelope(wire)
        except RemoteEnvelopeError as exc:
            # Envelope-level refusals (invalid / unsupported protocol) are
            # boundary rejections by definition; the category rides along.
            return RemoteEnvelopeReceipt(
                RemoteEnvelopeStatus.REJECTED, error_code=exc.category, **echo)
        except (TypeError, ValueError, RecursionError):
            # Expected codec/validation failure on a constructed envelope
            # (unserializable payload, mixed dict keys, PacketValidationError
            # family): the boundary rejects the input structurally — these
            # are exactly the V2 loopback's codec-failure classes.
            return RemoteEnvelopeReceipt(
                RemoteEnvelopeStatus.REJECTED,
                error_code=RemoteEnvelopeErrorCode.INVALID_ENVELOPE, **echo)
        if decoded != envelope:
            return RemoteEnvelopeReceipt(
                RemoteEnvelopeStatus.REJECTED,
                error_code=RemoteEnvelopeErrorCode.INVALID_ENVELOPE, **echo)
        before = len(self._mailboxes.get(envelope.recipient, ()))
        try:
            self._exchange(envelope.recipient, wire, self._enqueue)
        except RemoteExchangeError as exc:
            status, code = EXCHANGE_CATEGORY_TO_RECEIPT[exc.category]
            return RemoteEnvelopeReceipt(status, error_code=code, **echo)
        if len(self._mailboxes.get(envelope.recipient, ())) == before:
            # The exchange returned without producing a delivery fact for
            # this recipient: the wire never entered the mailbox. Honesty
            # demands FAILED — never DELIVERED, never a fabricated ACCEPTED.
            return RemoteEnvelopeReceipt(
                RemoteEnvelopeStatus.FAILED,
                error_code=RemoteEnvelopeErrorCode.DELIVERY_FAILED, **echo)
        return RemoteEnvelopeReceipt(RemoteEnvelopeStatus.DELIVERED, **echo)

    def receive(self, recipient: str):
        mailbox = self._mailboxes.get(recipient)
        if not mailbox:
            return None
        wire = mailbox.popleft()
        decoded = deserialize_remote_envelope(wire)
        if decoded.recipient != recipient:
            # A mailbox only ever yields its own mail: a wire addressed
            # elsewhere is a corrupted routing fact and fails loudly —
            # no rerouting, no rewriting, never a silent swap.
            raise RemoteEnvelopeError(
                RemoteEnvelopeErrorCode.INVALID_ENVELOPE,
                "mailbox wire is addressed to another recipient")
        return decoded
