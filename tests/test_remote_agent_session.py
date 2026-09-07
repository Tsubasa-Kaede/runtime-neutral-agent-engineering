"""V3.1-C1: Remote agent session — behavior tests.

The session owns exactly one interaction context: local/remote agent
addresses, one correlation id, packet→envelope wrapping, receipt
passthrough, inbound validation (sender/recipient/correlation) and the
OPEN/CLOSED lifecycle delegating close to the transport. Everything
else — delivery judgment, business meaning, orchestration, retries —
is deliberately somewhere else. These tests lock that boundary.
"""
import ast
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from collaboration_packet import (
    CollaborationPacket,
    CollaborationPayloadType,
    new_correlation_id,
)
from remote_contract import (
    RemoteEnvelope,
    RemoteEnvelopeError,
    RemoteEnvelopeErrorCode,
    RemoteEnvelopeStatus,
    RemotePayloadType,
    serialize_remote_envelope,
)
from remote_envelope_transport import (
    RemoteEnvelopeReceipt,
    RemoteEnvelopeTransport,
)
from remote_agent_session import RemoteAgentSession
from structured_packets import ArchitecturePacket

LOCAL = "agent:arch-bot:architect"
REMOTE = "agent:coder-bot:coder"


def arch():
    return ArchitecturePacket(
        task_id="T1", role="architect", goal=("g",), constraints=("c",),
        architecture=("a",), interfaces=({},), implementation_steps=({},),
        acceptance_criteria=("ac1",), risks=({},),
    )


def packet(cid=None, target_role="coder", source=LOCAL, target=REMOTE):
    cid = cid or new_correlation_id()
    return CollaborationPacket(
        correlation_id=cid, task_id="T1",
        source_agent=source, target_agent=target,
        source_role="architect", target_role=target_role,
        payload_type=CollaborationPayloadType.ARCHITECTURE, payload=arch(),
    )


def reply_packet(cid):
    return packet(cid, target_role="architect", source=REMOTE, target=LOCAL)


def inbound(cid=None, sender=REMOTE, recipient=LOCAL, role="architect"):
    cid = cid or new_correlation_id()
    return RemoteEnvelope(
        message_id=f"remote-inbound-{cid[-6:]}",
        correlation_id=cid,
        sender=sender,
        recipient=recipient,
        role=role,
        payload_type=RemotePayloadType.COLLABORATION_PACKET,
        payload=reply_packet(cid),
    )


def raised_code(callable_):
    try:
        callable_()
    except RemoteEnvelopeError as exc:
        return exc.category
    raise AssertionError("expected RemoteEnvelopeError")


class RecordingTransport:
    """In-memory RemoteEnvelopeTransport double; records every call."""

    def __init__(self, receipt=None):
        self.sent = []
        self.polls = []
        self.close_calls = 0
        self.inbox = []
        self.receipt = receipt or RemoteEnvelopeReceipt(
            RemoteEnvelopeStatus.DELIVERED)

    def send(self, envelope):
        self.sent.append(envelope)
        return self.receipt

    def receive(self, recipient):
        self.polls.append(recipient)
        return self.inbox.pop(0) if self.inbox else None

    def close(self):
        self.close_calls += 1


def session_for(transport=None, correlation_id=None):
    return RemoteAgentSession(
        LOCAL, REMOTE, transport or RecordingTransport(),
        correlation_id=correlation_id)


def packet_for(session):
    return packet(cid=session.correlation_id)


class ConstructorTests(unittest.TestCase):
    def test_binds_addresses_and_mints_correlation(self):
        session = session_for()
        self.assertEqual(session.local_address, LOCAL)
        self.assertEqual(session.remote_address, REMOTE)
        self.assertIsInstance(session.correlation_id, str)
        self.assertTrue(session.correlation_id.startswith("collab-"))

    def test_explicit_correlation_is_preserved_verbatim(self):
        cid = new_correlation_id()
        self.assertIs(session_for(correlation_id=cid).correlation_id, cid)

    def test_rejects_non_string_addresses(self):
        for bad in (None, 7, b"agent:x:y", "", "   "):
            with self.subTest(bad=bad):
                self.assertEqual(
                    raised_code(lambda: RemoteAgentSession(
                        bad, REMOTE, RecordingTransport())),
                    RemoteEnvelopeErrorCode.INVALID_ENVELOPE)
                self.assertEqual(
                    raised_code(lambda: RemoteAgentSession(
                        LOCAL, bad, RecordingTransport())),
                    RemoteEnvelopeErrorCode.INVALID_ENVELOPE)

    def test_rejects_local_equal_to_remote(self):
        self.assertEqual(
            raised_code(lambda: RemoteAgentSession(
                LOCAL, LOCAL, RecordingTransport())),
            RemoteEnvelopeErrorCode.INVALID_ENVELOPE)

    def test_rejects_invalid_correlation_id(self):
        for bad in (7, "", "   "):
            with self.subTest(bad=bad):
                self.assertEqual(
                    raised_code(lambda: session_for(correlation_id=bad)),
                    RemoteEnvelopeErrorCode.INVALID_ENVELOPE)

    def test_rejects_transport_not_conforming_to_protocol(self):
        self.assertEqual(
            raised_code(lambda: RemoteAgentSession(
                LOCAL, REMOTE, object())),
            RemoteEnvelopeErrorCode.INVALID_ENVELOPE)

    def test_rejects_transport_without_close(self):
        # B1b made close() contractual: a send/receive-only seam is not a
        # RemoteEnvelopeTransport and must be refused at construction.
        class SendReceiveOnly:
            def send(self, envelope): ...
            def receive(self, recipient): ...
        self.assertEqual(
            raised_code(lambda: RemoteAgentSession(
                LOCAL, REMOTE, SendReceiveOnly())),
            RemoteEnvelopeErrorCode.INVALID_ENVELOPE)


class SendEnvelopeTests(unittest.TestCase):
    def test_send_wraps_packet_with_session_headers(self):
        transport = RecordingTransport()
        session = session_for(transport)
        pkt = packet_for(session)
        session.send(pkt)
        self.assertEqual(len(transport.sent), 1)
        sent = transport.sent[0]
        self.assertIsInstance(sent, RemoteEnvelope)
        self.assertIs(sent.payload, pkt)
        self.assertEqual(sent.sender, LOCAL)
        self.assertEqual(sent.recipient, REMOTE)

    def test_send_role_derives_from_packet_target_role(self):
        transport = RecordingTransport()
        session = session_for(transport)
        session.send(packet(cid=session.correlation_id, target_role="reviewer"))
        self.assertEqual(transport.sent[0].role, "reviewer")

    def test_send_correlation_constant_and_message_ids_unique(self):
        transport = RecordingTransport()
        session = session_for(transport)
        pkt = packet_for(session)
        first = session.send(pkt)
        second = session.send(pkt)
        self.assertIs(first, transport.receipt)
        self.assertIs(second, transport.receipt)
        ids = [envelope.message_id for envelope in transport.sent]
        self.assertEqual(len(ids), 2)
        self.assertNotEqual(ids[0], ids[1])
        for mid in ids:
            self.assertTrue(mid.startswith("remote-"))
        for envelope in transport.sent:
            self.assertEqual(envelope.correlation_id, session.correlation_id)

    def test_send_returns_receipt_object_unchanged(self):
        for status in RemoteEnvelopeStatus:
            with self.subTest(status=status):
                receipt = RemoteEnvelopeReceipt(status)
                transport = RecordingTransport(receipt=receipt)
                session = session_for(transport)
                self.assertIs(session.send(packet_for(session)), receipt)

    def test_send_leaves_packet_untouched(self):
        transport = RecordingTransport()
        session = session_for(transport)
        pkt = packet_for(session)
        before = serialize_remote_envelope(inbound(cid=pkt.correlation_id))
        session.send(pkt)
        self.assertEqual(
            serialize_remote_envelope(inbound(cid=pkt.correlation_id)), before)
        self.assertEqual(pkt.correlation_id, session.correlation_id)
        self.assertIs(transport.sent[0].payload, pkt)

    def test_send_rejects_non_packet_input(self):
        transport = RecordingTransport()
        session = session_for(transport)
        self.assertEqual(
            raised_code(lambda: session.send(object())),
            RemoteEnvelopeErrorCode.INVALID_ENVELOPE)
        self.assertEqual(transport.sent, [])

    def test_send_rejects_packet_from_foreign_interaction(self):
        # The envelope contract enforces correlation consistency with the
        # payload; a packet from another interaction cannot ride this one.
        transport = RecordingTransport()
        session = session_for(transport)
        foreign = packet(cid=new_correlation_id())
        self.assertEqual(
            raised_code(lambda: session.send(foreign)),
            RemoteEnvelopeErrorCode.INVALID_ENVELOPE)
        self.assertEqual(transport.sent, [])

    def test_send_calls_transport_exactly_once(self):
        transport = RecordingTransport()
        session = session_for(transport)
        session.send(packet_for(session))
        self.assertEqual(len(transport.sent), 1)


class ReceiveValidationTests(unittest.TestCase):
    def test_receive_returns_none_when_nothing_arrived(self):
        session = session_for()
        self.assertIsNone(session.receive())

    def test_receive_polls_the_local_address_only(self):
        transport = RecordingTransport()
        session = session_for(transport)
        session.receive()
        session.receive()
        self.assertEqual(transport.polls, [LOCAL, LOCAL])

    def test_receive_returns_the_inbound_envelope(self):
        transport = RecordingTransport()
        session = session_for(transport)
        reply = inbound(cid=session.correlation_id)
        transport.inbox.append(reply)
        self.assertIs(session.receive(), reply)
        self.assertEqual(reply.payload, reply_packet(session.correlation_id))

    def test_receive_rejects_sender_mismatch(self):
        transport = RecordingTransport()
        session = session_for(transport)
        transport.inbox.append(inbound(
            cid=session.correlation_id, sender="agent:evil:coder"))
        self.assertEqual(raised_code(session.receive),
                         RemoteEnvelopeErrorCode.INVALID_ENVELOPE)

    def test_receive_rejects_recipient_mismatch(self):
        transport = RecordingTransport()
        session = session_for(transport)
        transport.inbox.append(inbound(
            cid=session.correlation_id, recipient="agent:other:architect"))
        self.assertEqual(raised_code(session.receive),
                         RemoteEnvelopeErrorCode.INVALID_ENVELOPE)

    def test_receive_rejects_correlation_mismatch(self):
        transport = RecordingTransport()
        session = session_for(transport)
        transport.inbox.append(inbound(cid=new_correlation_id()))
        self.assertEqual(raised_code(session.receive),
                         RemoteEnvelopeErrorCode.INVALID_ENVELOPE)


class LifecycleTests(unittest.TestCase):
    def test_fresh_session_is_open(self):
        transport = RecordingTransport()
        session = session_for(transport)
        session.send(packet_for(session))
        self.assertEqual(len(transport.sent), 1)
        self.assertIsNone(session.receive())
        self.assertEqual(transport.polls, [LOCAL])

    def test_close_delegates_once_and_refuses_afterwards(self):
        transport = RecordingTransport()
        session = session_for(transport)
        session.close()
        self.assertEqual(transport.close_calls, 1)
        self.assertEqual(
            raised_code(lambda: session.send(packet_for(session))),
            RemoteEnvelopeErrorCode.INVALID_ENVELOPE)
        self.assertEqual(raised_code(session.receive),
                         RemoteEnvelopeErrorCode.INVALID_ENVELOPE)

    def test_close_is_idempotent(self):
        transport = RecordingTransport()
        session = session_for(transport)
        session.close()
        session.close()
        session.close()
        self.assertEqual(transport.close_calls, 1)

    def test_closed_send_refusal_is_typed_not_fake_receipt(self):
        transport = RecordingTransport()
        session = session_for(transport)
        session.close()
        self.assertEqual(
            raised_code(lambda: session.send(packet_for(session))),
            RemoteEnvelopeErrorCode.INVALID_ENVELOPE)
        self.assertEqual(transport.sent, [])

    def test_closed_receive_refusal_is_typed_not_silent_none(self):
        transport = RecordingTransport()
        session = session_for(transport)
        transport.inbox.append(inbound(cid=session.correlation_id))
        session.close()
        self.assertEqual(raised_code(session.receive),
                         RemoteEnvelopeErrorCode.INVALID_ENVELOPE)
        self.assertEqual(transport.polls, [])

    def test_failed_receipt_does_not_change_lifecycle(self):
        transport = RecordingTransport(receipt=RemoteEnvelopeReceipt(
            RemoteEnvelopeStatus.FAILED,
            error_code=RemoteEnvelopeErrorCode.DELIVERY_FAILED))
        session = session_for(transport)
        session.send(packet_for(session))
        # Still OPEN: the next send reaches the transport again.
        session.send(packet_for(session))
        self.assertEqual(len(transport.sent), 2)
        session.close()
        self.assertEqual(transport.close_calls, 1)

    def test_no_retry_after_unsuccessful_receipt(self):
        for status in (RemoteEnvelopeStatus.FAILED,
                       RemoteEnvelopeStatus.TIMEOUT,
                       RemoteEnvelopeStatus.REJECTED):
            with self.subTest(status=status):
                transport = RecordingTransport(
                    receipt=RemoteEnvelopeReceipt(status))
                session = session_for(transport)
                session.send(packet_for(session))
                self.assertEqual(len(transport.sent), 1)


class SessionIsolationTests(unittest.TestCase):
    def test_two_sessions_never_share_wire(self):
        first_transport = RecordingTransport()
        second_transport = RecordingTransport()
        first = session_for(first_transport)
        second = session_for(second_transport)
        first.send(packet_for(first))
        second.send(packet_for(second))
        self.assertEqual(len(first_transport.sent), 1)
        self.assertEqual(len(second_transport.sent), 1)
        self.assertNotEqual(first.correlation_id, second.correlation_id)
        self.assertNotEqual(first_transport.sent[0].message_id,
                            second_transport.sent[0].message_id)

    def test_foreign_interaction_envelope_is_refused(self):
        transport = RecordingTransport()
        session = session_for(transport)
        other = session_for(RecordingTransport())
        transport.inbox.append(inbound(cid=other.correlation_id))
        self.assertEqual(raised_code(session.receive),
                         RemoteEnvelopeErrorCode.INVALID_ENVELOPE)


class SourceScanTests(unittest.TestCase):
    SOURCE = (SCRIPTS / "remote_agent_session.py").read_text(encoding="utf-8")

    def test_import_surface_is_exactly_the_allowed_modules(self):
        tree = ast.parse(self.SOURCE)
        modules = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(name.split(".")[0] for name in
                               (alias.name for alias in node.names))
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    modules.add(node.module.split(".")[0])
        self.assertEqual(
            modules,
            {"__future__", "collaboration_packet", "remote_contract",
             "remote_envelope_transport"})

    def test_no_forbidden_vocabulary_in_source(self):
        for forbidden in (
                "requests", "httpx", "aiohttp", "socket", "grpc", "ssl",
                "urllib", "websocket", "subprocess", "threading",
                "multiprocessing", "asyncio", "concurrent", "pickle",
                "marshal", "cloudpickle", "a2a", "http", "async",
                "uuid", "random", "secrets", "monotonic", "sleep",
                "datetime", "clock", "time.",
                "agent_identity", "runtime", "adapter", "orchestrat",
                "discovery", "selection", "admission", "qualification",
                "schedul", "fallback", "provenance",
                "retry", "reconnect", "restart", "cancel",
                "execution", "executor", "execute",
                "RemoteRequest", "RemoteResponse", "SessionEnvelope",
                "SessionMessage", "RemoteSessionPacket"):
            self.assertNotIn(forbidden, self.SOURCE, forbidden)

    def test_no_new_id_generators(self):
        # The only identity factories are the contract's own; the session
        # mints nothing beyond delegating to them.
        self.assertNotIn("uuid", self.SOURCE)
        self.assertNotIn("random", self.SOURCE)
        self.assertIn("new_message_id", self.SOURCE)
        self.assertIn("new_correlation_id", self.SOURCE)


if __name__ == "__main__":
    unittest.main()
