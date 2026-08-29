"""Phase 10H-B: LocalTransport — in-process collaboration transport contract.

Pure offline tests: value delivery through the wire round-trip, FIFO per
target, strict isolation, honest rejection (never queued, never raising),
no id minting, no env/subprocess/network, runtime-neutral by source scan.
"""
import sys
import unittest
from dataclasses import FrozenInstanceError, fields
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from collaboration_packet import (
    CollaborationPacket,
    CollaborationPayloadType,
    PacketValidationError,
    serialize_collaboration_packet,
)
from local_transport import DeliveryReceipt, DeliveryStatus, LocalTransport
from structured_packets import (
    ArchitecturePacket,
    ImplementationPacket,
    ReviewPacket,
    TestPacket,
)
from verified_selection_bridge import agent_id_for

SOURCE_AGENT = agent_id_for(("rt-a", "provider-a", "model-a", "fp-a"))
TARGET_AGENT = agent_id_for(("rt-b", "provider-b", "model-b", "fp-b"))

SECRET_MARKERS = ("token", "secret", "api_key", "authorization", "bearer", "stdout", "stderr")


def arch(task_id="T1", interfaces=({},), steps=({},), risks=({},)):
    return ArchitecturePacket(
        task_id=task_id, role="architect", goal=("g",), constraints=("c",),
        architecture=("a",), interfaces=interfaces, implementation_steps=steps,
        acceptance_criteria=("ac1",), risks=risks,
    )


def impl(task_id="T1"):
    return ImplementationPacket(
        task_id=task_id, role="coder", changed_files=("f.py",),
        implementation_summary="s", implementation_details=("d",),
        assumptions=(), unresolved_items=(), test_requirements=(),
    )


def test_pkt(task_id="T1"):
    return TestPacket(
        task_id=task_id, role="tester", tests_run=("t1",), tests_passed=("t1",),
        tests_failed=(), failures=(), coverage_or_validation=(), remaining_risks=(),
    )


def review_pkt(task_id="T1"):
    return ReviewPacket(
        task_id=task_id, role="reviewer", status="PASS", findings=(), severity=(),
        affected_files=(), required_changes=(), acceptance_criteria_status=(),
    )


def payload_for(payload_type):
    return {
        CollaborationPayloadType.ARCHITECTURE: arch,
        CollaborationPayloadType.IMPLEMENTATION: impl,
        CollaborationPayloadType.TEST: test_pkt,
        CollaborationPayloadType.REVIEW: review_pkt,
    }[payload_type]()


def envelope(**overrides):
    values = dict(
        correlation_id="C001",
        task_id="T1",
        source_agent=SOURCE_AGENT,
        target_agent=TARGET_AGENT,
        source_role="architect",
        target_role="coder",
        payload_type=CollaborationPayloadType.ARCHITECTURE,
        payload=arch(),
    )
    values.update(overrides)
    return CollaborationPacket(**values)


class BoobyTrapped:
    """Attribute access must never happen on non-packet inputs."""

    @property
    def correlation_id(self):
        raise AssertionError("attribute access leaked through send()")

    @property
    def target_agent(self):
        return "token=leak"


class ContractTests(unittest.TestCase):
    def test_delivery_status_members_and_values(self):
        self.assertEqual(
            {member.name for member in DeliveryStatus},
            {"DELIVERED", "REJECTED_NOT_A_PACKET", "REJECTED_INVALID_PACKET"},
        )
        for member in DeliveryStatus:
            self.assertEqual(member.value, member.name)

    def test_receipt_field_set_is_exact(self):
        self.assertEqual(
            {field.name for field in fields(DeliveryReceipt)},
            {"status", "correlation_id", "target_agent"},
        )

    def test_receipt_is_frozen(self):
        receipt = DeliveryReceipt(DeliveryStatus.DELIVERED, "C1", TARGET_AGENT)
        with self.assertRaises(FrozenInstanceError):
            receipt.status = DeliveryStatus.REJECTED_NOT_A_PACKET

    def test_receipt_rejects_secret_shaped_fields(self):
        for field_name in ("correlation_id", "target_agent"):
            with self.subTest(field=field_name):
                with self.assertRaises(PacketValidationError):
                    DeliveryReceipt(DeliveryStatus.DELIVERED, **{field_name: "api_key=1"})

    def test_receipt_repr_stays_clean(self):
        receipt = DeliveryReceipt(DeliveryStatus.DELIVERED, "C1", TARGET_AGENT)
        surface = (repr(receipt) + str(receipt)).lower()
        for marker in SECRET_MARKERS:
            self.assertNotIn(marker, surface)

    def test_identical_receipts_are_equal(self):
        first = DeliveryReceipt(DeliveryStatus.DELIVERED, "C1", TARGET_AGENT)
        second = DeliveryReceipt(DeliveryStatus.DELIVERED, "C1", TARGET_AGENT)
        self.assertEqual(first, second)


class SendTests(unittest.TestCase):
    def setUp(self):
        self.transport = LocalTransport()

    def test_valid_packet_is_delivered_with_verbatim_fields(self):
        receipt = self.transport.send(envelope())
        self.assertEqual(receipt.status, DeliveryStatus.DELIVERED)
        self.assertEqual(receipt.correlation_id, "C001")
        self.assertEqual(receipt.target_agent, TARGET_AGENT)

    def test_non_packet_inputs_are_rejected(self):
        for bad in (object(), {}, None, "x", 7, lambda: None, BoobyTrapped()):
            with self.subTest(bad=type(bad).__name__):
                receipt = self.transport.send(bad)
                self.assertEqual(receipt.status, DeliveryStatus.REJECTED_NOT_A_PACKET)
                self.assertEqual(receipt.correlation_id, "")
                self.assertEqual(receipt.target_agent, "")

    def test_booby_trapped_object_never_leaks_or_raises(self):
        receipt = self.transport.send(BoobyTrapped())
        self.assertEqual(receipt.status, DeliveryStatus.REJECTED_NOT_A_PACKET)
        surface = repr(receipt).lower()
        for marker in SECRET_MARKERS:
            self.assertNotIn(marker, surface)

    def test_subclass_is_rejected_as_not_a_packet(self):
        class SubPacket(CollaborationPacket):
            pass

        sub = SubPacket(**{
            "correlation_id": "C001", "task_id": "T1",
            "source_agent": SOURCE_AGENT, "target_agent": TARGET_AGENT,
            "source_role": "architect", "target_role": "coder",
            "payload_type": CollaborationPayloadType.ARCHITECTURE,
            "payload": arch(),
        })
        receipt = self.transport.send(sub)
        self.assertEqual(receipt.status, DeliveryStatus.REJECTED_NOT_A_PACKET)
        self.assertEqual(receipt.correlation_id, "")
        self.assertIsNone(self.transport.receive(TARGET_AGENT))

    def test_silently_mutating_payloads_are_rejected(self):
        mutations = {
            "int dict key": dict(interfaces=({1: "a"},)),
            "bool dict key": dict(interfaces=({True: "x"},)),
            "list in dict value": dict(steps=({"steps": [1, 2]},)),
            "nan float": dict(risks=({"score": float("nan")},)),
            "mixed dict keys": dict(interfaces=({"a": 1, 1: "b"},)),
        }
        for label, kwargs in mutations.items():
            with self.subTest(mutation=label):
                packet = envelope(payload=arch(**kwargs))
                receipt = self.transport.send(packet)
                self.assertEqual(receipt.status, DeliveryStatus.REJECTED_INVALID_PACKET)
                self.assertEqual(receipt.correlation_id, "C001")
                self.assertEqual(receipt.target_agent, TARGET_AGENT)

    def test_non_serializable_payload_is_rejected(self):
        packet = envelope(payload=arch(interfaces=({"cb": object()},)))
        receipt = self.transport.send(packet)
        self.assertEqual(receipt.status, DeliveryStatus.REJECTED_INVALID_PACKET)
        self.assertEqual(receipt.correlation_id, "C001")

    def test_rejections_never_enqueue(self):
        self.transport.send(envelope(payload=arch(interfaces=({1: "a"},))))
        self.transport.send(object())
        self.transport.send(None)
        self.assertEqual(self.transport.pending(TARGET_AGENT), 0)
        self.assertIsNone(self.transport.receive(TARGET_AGENT))

    def test_real_provenance_packet_is_delivered_verbatim(self):
        receipt = self.transport.send(envelope(provenance="REAL"))
        self.assertEqual(receipt.status, DeliveryStatus.DELIVERED)
        received = self.transport.receive(TARGET_AGENT)
        self.assertEqual(received.provenance, "REAL")


class ReceiveTests(unittest.TestCase):
    def setUp(self):
        self.transport = LocalTransport()

    def test_empty_queue_returns_none(self):
        self.assertIsNone(self.transport.receive(TARGET_AGENT))

    def test_fifo_order_is_preserved(self):
        first = envelope(correlation_id="C1")
        second = envelope(correlation_id="C2", payload_type=CollaborationPayloadType.TEST,
                          payload=test_pkt())
        third = envelope(correlation_id="C3", payload_type=CollaborationPayloadType.IMPLEMENTATION,
                         payload=impl())
        for packet in (first, second, third):
            self.transport.send(packet)
        self.assertEqual(self.transport.receive(TARGET_AGENT).correlation_id, "C1")
        self.assertEqual(self.transport.receive(TARGET_AGENT).correlation_id, "C2")
        self.assertEqual(self.transport.receive(TARGET_AGENT).correlation_id, "C3")
        self.assertIsNone(self.transport.receive(TARGET_AGENT))

    def test_delivery_is_by_value_not_reference(self):
        packet = envelope()
        self.transport.send(packet)
        received = self.transport.receive(TARGET_AGENT)
        self.assertEqual(received, packet)
        self.assertIsNot(received, packet)
        self.assertIsNot(received.payload, packet.payload)

    def test_full_field_fidelity(self):
        packet = envelope(acceptance_criteria=("c1", "c2"), provenance="REAL")
        self.transport.send(packet)
        received = self.transport.receive(TARGET_AGENT)
        for field_name in ("correlation_id", "task_id", "source_agent", "target_agent",
                           "source_role", "target_role", "payload_type", "payload",
                           "acceptance_criteria", "protocol_version", "provenance"):
            self.assertEqual(getattr(received, field_name), getattr(packet, field_name),
                             field_name)

    def test_provenance_offline_and_real_both_preserved(self):
        for provenance in ("OFFLINE", "REAL"):
            with self.subTest(provenance=provenance):
                self.transport.send(envelope(provenance=provenance))
                self.assertEqual(self.transport.receive(TARGET_AGENT).provenance, provenance)

    def test_unknown_agent_returns_none_and_zero(self):
        unknown = agent_id_for(("rt-z", "provider-z", "model-z", "fp-z"))
        self.assertIsNone(self.transport.receive(unknown))
        self.assertEqual(self.transport.pending(unknown), 0)

    def test_empty_string_agent_id_returns_none(self):
        self.transport.send(envelope())
        self.assertIsNone(self.transport.receive(""))

    def test_unhashable_agent_id_raises_honestly(self):
        with self.assertRaises(TypeError):
            self.transport.receive([])


class IsolationTests(unittest.TestCase):
    def test_targets_are_strictly_isolated(self):
        other = agent_id_for(("rt-c", "provider-c", "model-c", "fp-c"))
        transport = LocalTransport()
        for index in range(3):
            transport.send(envelope(correlation_id=f"A{index}"))
            transport.send(envelope(correlation_id=f"B{index}", target_agent=other,
                                    source_agent=TARGET_AGENT, source_role="coder",
                                    target_role="architect",
                                    payload_type=CollaborationPayloadType.IMPLEMENTATION,
                                    payload=impl()))
        self.assertEqual(transport.pending(TARGET_AGENT), 3)
        self.assertEqual(transport.pending(other), 3)
        for index in range(3):
            self.assertEqual(transport.receive(TARGET_AGENT).correlation_id, f"A{index}")
            self.assertEqual(transport.receive(other).correlation_id, f"B{index}")


class DuplicateTests(unittest.TestCase):
    def test_same_packet_twice_queues_twice(self):
        transport = LocalTransport()
        packet = envelope()
        transport.send(packet)
        transport.send(packet)
        self.assertEqual(transport.pending(TARGET_AGENT), 2)
        self.assertEqual(transport.receive(TARGET_AGENT), packet)
        self.assertEqual(transport.receive(TARGET_AGENT), packet)
        self.assertIsNone(transport.receive(TARGET_AGENT))


class PendingTests(unittest.TestCase):
    def test_counts_and_decrement(self):
        transport = LocalTransport()
        transport.send(envelope(correlation_id="C1"))
        transport.send(envelope(correlation_id="C2"))
        self.assertEqual(transport.pending(TARGET_AGENT), 2)
        transport.receive(TARGET_AGENT)
        self.assertEqual(transport.pending(TARGET_AGENT), 1)
        transport.receive(TARGET_AGENT)
        self.assertEqual(transport.pending(TARGET_AGENT), 0)

    def test_send_to_one_target_leaves_others_at_zero(self):
        transport = LocalTransport()
        transport.send(envelope())
        other = agent_id_for(("rt-d", "provider-d", "model-d", "fp-d"))
        self.assertEqual(transport.pending(other), 0)


class E2ETests(unittest.TestCase):
    def test_architect_coder_request_reply_loop(self):
        transport = LocalTransport()
        request = envelope(correlation_id="C001")
        self.assertEqual(transport.send(request).status, DeliveryStatus.DELIVERED)
        coder_got = transport.receive(TARGET_AGENT)
        self.assertEqual(coder_got, request)
        reply = CollaborationPacket(
            correlation_id="C001",
            task_id="T1",
            source_agent=TARGET_AGENT,
            target_agent=SOURCE_AGENT,
            source_role="coder",
            target_role="architect",
            payload_type=CollaborationPayloadType.IMPLEMENTATION,
            payload=impl(),
        )
        self.assertEqual(transport.send(reply).status, DeliveryStatus.DELIVERED)
        architect_got = transport.receive(SOURCE_AGENT)
        self.assertEqual(architect_got, reply)
        self.assertEqual(architect_got.correlation_id, coder_got.correlation_id)
        self.assertEqual(transport.pending(SOURCE_AGENT), 0)
        self.assertEqual(transport.pending(TARGET_AGENT), 0)

    def test_all_four_payload_types_round_trip(self):
        for payload_type in CollaborationPayloadType:
            with self.subTest(payload_type=payload_type.name):
                transport = LocalTransport()
                packet = envelope(payload_type=payload_type, payload=payload_for(payload_type))
                self.assertEqual(transport.send(packet).status, DeliveryStatus.DELIVERED)
                self.assertEqual(transport.receive(TARGET_AGENT), packet)


class DeterminismTests(unittest.TestCase):
    def test_same_sequence_same_results_on_fresh_instances(self):
        def scenario():
            transport = LocalTransport()
            receipts = [
                transport.send(envelope(correlation_id=f"C{index}")) for index in range(3)
            ]
            received = tuple(
                transport.receive(TARGET_AGENT) for _ in range(3)
            )
            return receipts, received

        first_receipts, first_received = scenario()
        second_receipts, second_received = scenario()
        self.assertEqual(first_receipts, second_receipts)
        self.assertEqual(first_received, second_received)

    def test_wire_form_of_received_is_canonical(self):
        transport = LocalTransport()
        packet = envelope()
        transport.send(packet)
        received = transport.receive(TARGET_AGENT)
        self.assertEqual(
            serialize_collaboration_packet(received),
            serialize_collaboration_packet(packet),
        )


class SynchronousContractTests(unittest.TestCase):
    """Timeout / cancellation contract of the LOCAL (synchronous) transport.

    A synchronous in-process transport has no in-flight state and no
    waiting surface: send() is atomic (validate-and-enqueue or reject,
    nothing in between) and receive() is non-blocking (empty means None
    immediately). Therefore timeout and cancellation are EMPTY by
    contract here — the module docstring must declare that these
    semantics belong to the remote transport contract, and the tests
    below pin both the declaration and the observable behavior.
    """

    def test_docstring_declares_timeout_and_cancellation_delegation(self):
        import local_transport as module
        doc = (module.__doc__ or "").lower()
        self.assertIn("timeout", doc)
        self.assertIn("cancellation", doc)
        self.assertIn("remote", doc)

    def test_send_is_atomic_no_intermediate_state(self):
        # Rejected send leaves the transport exactly as it was: the
        # mailbox gained nothing, and a subsequent valid send is the
        # next state. There is no "in flight" to time out or cancel.
        transport = LocalTransport()
        before_pending = transport.pending(TARGET_AGENT)
        receipt = transport.send(object())  # reject
        self.assertEqual(receipt.status, DeliveryStatus.REJECTED_NOT_A_PACKET)
        self.assertEqual(transport.pending(TARGET_AGENT), before_pending)
        receipt2 = transport.send(envelope())  # accept
        self.assertEqual(receipt2.status, DeliveryStatus.DELIVERED)
        self.assertEqual(transport.pending(TARGET_AGENT), before_pending + 1)

    def test_invalid_packet_rejection_is_terminal(self):
        # A REJECTED receipt is final: the same packet object can be
        # re-sent (and rejected again) with no retry-in-progress state,
        # and no partial residue ever appears in the mailbox.
        transport = LocalTransport()
        bad = envelope(payload=arch(interfaces=({1: "a"},)))
        first = transport.send(bad)
        second = transport.send(bad)
        self.assertEqual(first, second)
        self.assertEqual(first.status, DeliveryStatus.REJECTED_INVALID_PACKET)
        self.assertEqual(transport.pending(TARGET_AGENT), 0)

    def test_receive_never_blocks_and_empty_is_immediate_none(self):
        # No waiting surface: an empty mailbox answers None on the very
        # first call; there is no "pending receive" that could time out
        # or be cancelled later.
        transport = LocalTransport()
        self.assertIsNone(transport.receive(TARGET_AGENT))
        self.assertIsNone(transport.receive(TARGET_AGENT))
        transport.send(envelope())
        self.assertIsNotNone(transport.receive(TARGET_AGENT))
        self.assertIsNone(transport.receive(TARGET_AGENT))

    def test_delivery_has_no_cancel_window(self):
        # Once send() returns DELIVERED the packet is fully enqueued;
        # there is no in-flight phase during which a cancel could
        # arrive between acceptance and availability.
        transport = LocalTransport()
        receipt = transport.send(envelope(correlation_id="C-now"))
        self.assertEqual(receipt.status, DeliveryStatus.DELIVERED)
        # Immediately available — no intermediate async state.
        received = transport.receive(TARGET_AGENT)
        self.assertEqual(received.correlation_id, "C-now")

    def test_receive_drains_exactly_what_send_accepted(self):
        # The observable universe is exactly {accepted packets}: every
        # DELIVERED send is receivable exactly once, every rejection is
        # receivable zero times. Nothing else exists to time out.
        transport = LocalTransport()
        accepted = [envelope(correlation_id=f"C{i}") for i in range(3)]
        for packet in accepted:
            self.assertEqual(transport.send(packet).status, DeliveryStatus.DELIVERED)
        transport.send(object())  # rejected, invisible downstream
        transport.send(envelope(payload=arch(interfaces=({1: "a"},))))  # rejected
        drained = [transport.receive(TARGET_AGENT) for _ in range(3)]
        self.assertEqual(drained, accepted)
        self.assertIsNone(transport.receive(TARGET_AGENT))
        self.assertEqual(transport.pending(TARGET_AGENT), 0)


class SourceScanTests(unittest.TestCase):
    def test_no_runtime_names(self):
        import local_transport as module
        text = Path(module.__file__).read_text(encoding="utf-8").lower()
        for name in ("claude", "codex", "deepseek", "openai", "anthropic",
                     "gemini", "tiny-agents", "tiny_agents"):
            self.assertNotIn(name, text)

    def test_no_forbidden_imports_or_channels(self):
        import local_transport as module
        source = Path(module.__file__).read_text(encoding="utf-8")
        for forbidden in ("external_runtime", "execution_engine", "orchestrator",
                          "verified_", "runtime_health", "capability_registry",
                          "task_budget", "loop_guard", "fallback", "score",
                          "subprocess", "invoke", "os.environ", "getenv",
                          "RUN_REAL_PROVIDER_TESTS", "requests", "urllib",
                          "socket", "async"):
            self.assertNotIn(forbidden, source)

    def test_transport_mints_no_ids_and_touches_no_clock(self):
        import local_transport as module
        source = Path(module.__file__).read_text(encoding="utf-8")
        for forbidden in ("uuid", "random", "time", "datetime", "monotonic",
                          "new_correlation_id"):
            self.assertNotIn(forbidden, source)

    def test_provenance_is_never_produced_or_referenced(self):
        import local_transport as module
        source = Path(module.__file__).read_text(encoding="utf-8")
        self.assertNotIn("provenance", source)


if __name__ == "__main__":
    unittest.main()
