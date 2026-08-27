"""Phase 10H-B end-to-end verification: Architect -> CollaborationPacket
-> LocalTransport -> Coder, plus the reply leg back to the architect.

Verification over the already-shipped 10H-B implementation
(local_transport.py + collaboration_packet.py): no production code is
exercised beyond the sanctioned chain, no runtimes, no network, no
clocks. Everything is deterministic — fixed ids, sanctioned agent-id
projection, value delivery only.
"""
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from collaboration_packet import (
    CollaborationPacket,
    CollaborationPayloadType,
    serialize_collaboration_packet,
)
from local_transport import DeliveryStatus, LocalTransport
from structured_packets import ArchitecturePacket, ImplementationPacket
from verified_selection_bridge import agent_id_for

ARCHITECT = agent_id_for(("rt-arch", "provider-a", "model-a", "fp-a"))
CODER = agent_id_for(("rt-code", "provider-b", "model-b", "fp-b"))
CODER_2 = agent_id_for(("rt-code2", "provider-c", "model-c", "fp-c"))

SECRET_MARKERS = ("token", "secret", "api_key", "authorization", "bearer", "stdout", "stderr")


def arch(task_id="T1"):
    return ArchitecturePacket(
        task_id=task_id, role="architect", goal=("g",), constraints=("c",),
        architecture=("a",), interfaces=({},), implementation_steps=({},),
        acceptance_criteria=("ac1",), risks=({},),
    )


def impl(task_id="T1"):
    return ImplementationPacket(
        task_id=task_id, role="coder", changed_files=("f.py",),
        implementation_summary="s", implementation_details=("d",),
        assumptions=(), unresolved_items=(), test_requirements=(),
    )


def request_pkt(correlation_id="C001", task_id="T1"):
    """Architect -> Coder: the architecture handoff."""
    return CollaborationPacket(
        correlation_id=correlation_id,
        task_id=task_id,
        source_agent=ARCHITECT,
        target_agent=CODER,
        source_role="architect",
        target_role="coder",
        payload_type=CollaborationPayloadType.ARCHITECTURE,
        payload=arch(task_id),
        acceptance_criteria=("ac1",),
    )


def reply_pkt(correlation_id, task_id="T1"):
    """Coder -> Architect: the implementation answer, same correlation."""
    return CollaborationPacket(
        correlation_id=correlation_id,
        task_id=task_id,
        source_agent=CODER,
        target_agent=ARCHITECT,
        source_role="coder",
        target_role="architect",
        payload_type=CollaborationPayloadType.IMPLEMENTATION,
        payload=impl(task_id),
        acceptance_criteria=(),
    )


class ArchitectToCoderChainTests(unittest.TestCase):
    def test_architect_to_coder_chain_delivers_verbatim_packet(self):
        transport = LocalTransport()
        sent = request_pkt()
        receipt = transport.send(sent)
        received = transport.receive(CODER)

        self.assertEqual(receipt.status, DeliveryStatus.DELIVERED)
        self.assertEqual(received, sent)
        self.assertIsNot(received, sent)  # delivery is by value
        # Field-by-field verbatim fidelity at the receiving end.
        self.assertEqual(received.correlation_id, "C001")
        self.assertEqual(received.task_id, "T1")
        self.assertEqual(received.source_agent, ARCHITECT)
        self.assertEqual(received.target_agent, CODER)
        self.assertEqual(received.source_role, "architect")
        self.assertEqual(received.target_role, "coder")
        self.assertEqual(received.payload_type, CollaborationPayloadType.ARCHITECTURE)
        self.assertEqual(received.payload, arch())
        self.assertEqual(received.acceptance_criteria, ("ac1",))
        self.assertEqual(received.protocol_version, sent.protocol_version)
        self.assertEqual(received.provenance, "OFFLINE")

    def test_wire_text_identical_at_both_ends(self):
        transport = LocalTransport()
        sent = request_pkt()
        transport.send(sent)
        received = transport.receive(CODER)

        self.assertEqual(
            serialize_collaboration_packet(received),
            serialize_collaboration_packet(sent))

    def test_correlation_id_links_request_and_reply(self):
        transport = LocalTransport()
        request = request_pkt(correlation_id="C-e2e")
        self.assertEqual(transport.send(request).status, DeliveryStatus.DELIVERED)

        # Coder consumes the request and answers under the SAME correlation.
        at_coder = transport.receive(CODER)
        self.assertEqual(at_coder.correlation_id, "C-e2e")
        answer = reply_pkt(at_coder.correlation_id, at_coder.task_id)
        self.assertEqual(transport.send(answer).status, DeliveryStatus.DELIVERED)

        at_architect = transport.receive(ARCHITECT)
        # End-to-end correlation: both legs share one correlation_id and
        # swap the agent/role direction exactly.
        self.assertEqual(at_architect.correlation_id, at_coder.correlation_id)
        self.assertEqual(at_architect.source_agent, request.target_agent)
        self.assertEqual(at_architect.target_agent, request.source_agent)
        self.assertEqual(at_architect.source_role, "coder")
        self.assertEqual(at_architect.target_role, "architect")
        self.assertEqual(at_architect.payload_type,
                         CollaborationPayloadType.IMPLEMENTATION)
        self.assertEqual(at_architect, answer)

    def test_fifo_and_isolation_across_chain(self):
        transport = LocalTransport()
        first = request_pkt(correlation_id="C-1")
        second = request_pkt(correlation_id="C-2")
        other_target = CollaborationPacket(
            correlation_id="C-3", task_id="T1",
            source_agent=ARCHITECT, target_agent=CODER_2,
            source_role="architect", target_role="coder",
            payload_type=CollaborationPayloadType.ARCHITECTURE,
            payload=arch(),
        )
        for packet in (first, second, other_target):
            self.assertEqual(
                transport.send(packet).status, DeliveryStatus.DELIVERED)

        # Coder sees exactly its own traffic, in FIFO order.
        self.assertEqual(transport.receive(CODER).correlation_id, "C-1")
        self.assertEqual(transport.receive(CODER).correlation_id, "C-2")
        self.assertIsNone(transport.receive(CODER))
        # The other mailbox is untouched by coder's receives.
        self.assertEqual(transport.pending(CODER_2), 1)
        self.assertEqual(transport.receive(CODER_2).correlation_id, "C-3")

    def test_pending_bookkeeping_across_the_chain(self):
        transport = LocalTransport()
        self.assertEqual(transport.pending(CODER), 0)
        transport.send(request_pkt())
        self.assertEqual(transport.pending(CODER), 1)
        transport.receive(CODER)
        self.assertEqual(transport.pending(CODER), 0)

    def test_rejected_packet_never_reaches_the_receiver(self):
        transport = LocalTransport()
        receipt = transport.send(object())  # not a CollaborationPacket
        self.assertEqual(receipt.status, DeliveryStatus.REJECTED_NOT_A_PACKET)
        self.assertEqual(transport.pending(CODER), 0)
        self.assertIsNone(transport.receive(CODER))
        # The channel stays clean: the next valid packet still delivers.
        self.assertEqual(
            transport.send(request_pkt()).status, DeliveryStatus.DELIVERED)
        self.assertEqual(transport.receive(CODER).correlation_id, "C001")

    def test_wire_text_is_secret_free_end_to_end(self):
        for packet in (request_pkt(), reply_pkt("C-reply")):
            wire = serialize_collaboration_packet(packet).lower()
            for marker in SECRET_MARKERS:
                self.assertNotIn(marker, wire)


if __name__ == "__main__":
    unittest.main()
