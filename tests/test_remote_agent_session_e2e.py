"""V3.1-D: REAL process-boundary E2E — the B2 + C1 seam over a real child.

These tests prove the existing, unchanged V3.1 seam completes a real OS
process-boundary interaction: a RemoteAgentSession drives the production
SubprocessStdioEnvelopeTransport, whose child is a REAL independent
process that speaks the existing canonical JSONL wire protocol and the
existing ACK. REAL_PROCESS = YES, REAL_TRANSPORT = YES, REAL_PROVIDER =
NO — the child fixtures below are test-only envelope-speaking endpoints,
not agent runtimes.

Fixture protocol obligations (from the B2 design): the response envelope
line is written BEFORE the acknowledgement line (DELIVERED must never
outrun the response that precedes it), and both lines go out through
sys.stdout.buffer with an explicit flush. The child's OS pid rides inside
the reply's business payload (task_id), so process distinctness is proven
by the data that crossed the boundary, not by the subprocess API merely
being present.

Architectural facts, one per test:
D1  real process round-trip (DELIVERED and the response are asserted
    separately — the ACK is never treated as the response)
D2a identity and correlation survive the real wire
D2b C1 inbound validation fires across the real boundary
D3  persistent child + FIFO (existing B2 transport behavior)
D3b close terminates the real child
"""
import ast
import os
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from collaboration_packet import (
    CollaborationPacket,
    CollaborationPayloadType,
)
from remote_contract import (
    RemoteEnvelope,
    RemoteEnvelopeError,
    RemoteEnvelopeErrorCode,
    RemoteEnvelopeStatus,
)
from remote_subprocess_transport import SubprocessStdioEnvelopeTransport
from remote_agent_session import RemoteAgentSession
from structured_packets import ArchitecturePacket

LOCAL = "agent:arch-e2e:architect"
REMOTE = "agent:coder-e2e:coder"

# The child receives only a whitelisted environment from B2, so the repo
# path is embedded in the source, never passed through the environment.
CHILD_PREAMBLE = (
    "import sys\n"
    "sys.path.insert(0, " + repr(str(SCRIPTS)) + ")\n"
)


def _endpoint_source(correlation_expr, task_expr):
    """Build the inline child source: a real, independent OS process that
    reads the existing envelope wire, builds a self-consistent reply with
    the project's own contract classes, and honours the response-before-
    acknowledgement ordering."""
    return CHILD_PREAMBLE + f"""
import json
import os
import sys

from collaboration_packet import (
    CollaborationPacket,
    CollaborationPayloadType,
    new_correlation_id,
)
from remote_contract import (
    RemoteEnvelope,
    RemotePayloadType,
    deserialize_remote_envelope,
    new_message_id,
    serialize_remote_envelope,
)
from structured_packets import ArchitecturePacket

for line in sys.stdin.buffer:
    if not line.strip():
        continue
    inbound = deserialize_remote_envelope(line.decode("utf-8"))
    correlation = {correlation_expr}
    marker = {task_expr}
    reply_packet = CollaborationPacket(
        correlation_id=correlation,
        task_id=marker,
        source_agent=inbound.recipient,
        target_agent=inbound.sender,
        source_role="coder",
        target_role="architect",
        payload_type=CollaborationPayloadType.ARCHITECTURE,
        payload=ArchitecturePacket(
            task_id=marker, role="architect", goal=("g",),
            constraints=("c",), architecture=("a",), interfaces=({{}},),
            implementation_steps=({{}},), acceptance_criteria=("ac",),
            risks=({{}},),
        ),
    )
    reply = RemoteEnvelope(
        message_id=new_message_id(),
        correlation_id=correlation,
        sender=inbound.recipient,
        recipient=inbound.sender,
        role=reply_packet.target_role,
        payload_type=RemotePayloadType.COLLABORATION_PACKET,
        payload=reply_packet,
    )
    wire = serialize_remote_envelope(reply).encode("utf-8") + b"\\n"
    ack = json.dumps(dict(delivered=inbound.message_id), sort_keys=True,
                     separators=(",", ":")).encode("utf-8") + b"\\n"
    out = sys.stdout.buffer
    # Response first, ACK second: DELIVERED may only authorise delivery
    # of an exchange whose response line is already on the wire.
    out.write(wire)
    out.write(ack)
    out.flush()
"""


# Well-behaved endpoint: correlation preserved, child pid as task marker.
ROUND_TRIP_CHILD = _endpoint_source("inbound.correlation_id", "str(os.getpid())")
# Self-consistent but foreign-correlation reply: valid enough to enter
# the B2 mailbox, wrong enough to be refused by C1 validation.
BAD_CORRELATION_CHILD = _endpoint_source("new_correlation_id()", "str(os.getpid())")
# Order marker: "pid:message_id" — same child proof plus FIFO proof.
FIFO_CHILD = _endpoint_source(
    "inbound.correlation_id",
    'str(os.getpid()) + ":" + inbound.message_id')


def outbound_packet(session):
    return CollaborationPacket(
        correlation_id=session.correlation_id,
        task_id="T1",
        source_agent=LOCAL,
        target_agent=REMOTE,
        source_role="architect",
        target_role="coder",
        payload_type=CollaborationPayloadType.ARCHITECTURE,
        payload=ArchitecturePacket(
            task_id="T1", role="architect", goal=("g",), constraints=("c",),
            architecture=("a",), interfaces=({},), implementation_steps=({},),
            acceptance_criteria=("ac",), risks=({},),
        ),
    )


class RealProcessBoundaryE2ETests(unittest.TestCase):
    def _session(self, child):
        transport = SubprocessStdioEnvelopeTransport(
            [sys.executable, "-c", child], timeout_seconds=30)
        session = RemoteAgentSession(LOCAL, REMOTE, transport)
        self.addCleanup(session.close)
        return session, transport

    def test_d1_real_process_round_trip(self):
        session, _ = self._session(ROUND_TRIP_CHILD)
        receipt = session.send(outbound_packet(session))
        # Transport-level fact, asserted on its own: DELIVERED authorised
        # by the child's ACK — never substituted by the response below.
        self.assertIs(receipt.status, RemoteEnvelopeStatus.DELIVERED)
        # Remote-level fact: a real response envelope came back over the
        # real stdout boundary and was routed into the B2 mailbox.
        response = session.receive()
        self.assertIsNotNone(response)
        self.assertIsInstance(response, RemoteEnvelope)
        # Process distinctness proven by data that crossed the boundary:
        # the reply's business payload carries the child's OS pid.
        child_pid = int(response.payload.task_id)
        self.assertNotEqual(child_pid, os.getpid())

    def test_d2a_identity_and_correlation_survive_the_wire(self):
        session, _ = self._session(ROUND_TRIP_CHILD)
        receipt = session.send(outbound_packet(session))
        self.assertIs(receipt.status, RemoteEnvelopeStatus.DELIVERED)
        response = session.receive()
        self.assertIsNotNone(response)
        self.assertEqual(response.sender, REMOTE)
        self.assertEqual(response.recipient, LOCAL)
        self.assertEqual(response.correlation_id, session.correlation_id)
        # One interaction, two distinct wire messages: the response is a
        # fresh message (new message_id) inside the same correlation.
        self.assertNotEqual(response.message_id, receipt.message_id)

    def test_d2b_c1_validation_fires_across_the_boundary(self):
        session, _ = self._session(BAD_CORRELATION_CHILD)
        receipt = session.send(outbound_packet(session))
        # The child ACKed honestly, so delivery itself succeeded.
        self.assertIs(receipt.status, RemoteEnvelopeStatus.DELIVERED)
        # Its reply is a structurally valid envelope that entered the B2
        # mailbox — but it belongs to another interaction, and C1's
        # inbound triple validation must refuse it across the REAL
        # boundary: typed refusal, never a silent drop or a foreign
        # envelope handed back.
        with self.assertRaises(RemoteEnvelopeError) as caught:
            session.receive()
        self.assertIs(caught.exception.category,
                      RemoteEnvelopeErrorCode.INVALID_ENVELOPE)

    def test_d3_persistent_child_fifo(self):
        session, _ = self._session(FIFO_CHILD)
        receipts = [session.send(outbound_packet(session))
                    for _ in range(3)]
        for receipt in receipts:
            self.assertIs(receipt.status, RemoteEnvelopeStatus.DELIVERED)
        ids = [receipt.message_id for receipt in receipts]
        self.assertEqual(len(set(ids)), 3)
        replies = [session.receive() for _ in range(3)]
        # FIFO here is evidence of the EXISTING B2 transport behavior
        # (one persistent child, mailbox yields in write order) — it is
        # not a new C1 contract.
        pids = set()
        order = []
        for reply in replies:
            pid_part, message_part = reply.payload.task_id.split(":", 1)
            pids.add(pid_part)
            order.append(message_part)
        self.assertEqual(len(pids), 1)      # one persistent child served all
        self.assertNotEqual(int(pids.pop()), os.getpid())
        self.assertEqual(order, ids)        # replies arrived in send order

    def test_d3b_close_terminates_the_real_child(self):
        session, transport = self._session(ROUND_TRIP_CHILD)
        self.assertIs(session.send(outbound_packet(session)).status,
                      RemoteEnvelopeStatus.DELIVERED)
        child_pid = int(session.receive().payload.task_id)
        session.close()
        # OS-level fact (white-box, test-only): close delegated to the
        # transport's cleanup authority and the real child is reaped —
        # no orphan survives.
        self.assertIsNotNone(transport._process.poll())
        # Lifecycle fact: a closed session refuses work with a typed
        # refusal — never a fake receipt.
        with self.assertRaises(RemoteEnvelopeError):
            session.send(outbound_packet(session))
        self.assertNotEqual(child_pid, os.getpid())


if __name__ == "__main__":
    unittest.main()
