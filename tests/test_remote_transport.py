"""Phase 10H-C: RemoteTransport contract + LoopbackRemoteTransport seam.

Fully offline: remote conditions are simulated by injected exchanges that
raise closed-vocabulary categories or script sink behavior (drop,
duplicate, corrupt, selective failure). The contract is protocol-neutral
and execution-neutral; DELIVERED means accepted by the exchange, never
consumed by the peer; ordering is not part of the contract (the loopback
alone declares FIFO).
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
from remote_transport import (
    LoopbackRemoteTransport,
    RemoteDeliveryReceipt,
    RemoteDeliveryStatus,
    RemoteExchangeError,
    RemoteTransport,
)
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

REMOTE_CATEGORIES = (
    "REMOTE_UNAVAILABLE", "REMOTE_TIMEOUT", "REMOTE_REJECTED",
    "REMOTE_PROTOCOL_ERROR", "AUTH_REQUIRED",
)


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
    @property
    def correlation_id(self):
        raise AssertionError("attribute access leaked through send()")

    @property
    def target_agent(self):
        return "token=leak"


def raising_exchange(category):
    def exchange(target_agent, wire, sink):
        raise RemoteExchangeError(category)
    return exchange


def dropping_exchange(target_agent, wire, sink):
    return None  # accepted by the exchange, never sunk


def duplicating_exchange(target_agent, wire, sink):
    sink(target_agent, wire)
    sink(target_agent, wire)


def corrupting_exchange(target_agent, wire, sink):
    sink(target_agent, "not-a-valid-wire")


def selective_exchange(fail_for, category):
    def exchange(target_agent, wire, sink):
        if target_agent == fail_for:
            raise RemoteExchangeError(category)
        sink(target_agent, wire)
    return exchange


class ContractTests(unittest.TestCase):
    def test_status_members_and_values(self):
        self.assertEqual(
            {member.name for member in RemoteDeliveryStatus},
            {"DELIVERED", "REJECTED_NOT_A_PACKET", "REJECTED_INVALID_PACKET",
             "REMOTE_UNAVAILABLE", "REMOTE_TIMEOUT", "REMOTE_REJECTED",
             "REMOTE_PROTOCOL_ERROR", "AUTH_REQUIRED"},
        )
        for member in RemoteDeliveryStatus:
            self.assertEqual(member.value, member.name)

    def test_receipt_field_set_is_exact(self):
        self.assertEqual(
            {field.name for field in fields(RemoteDeliveryReceipt)},
            {"status", "correlation_id", "target_agent"},
        )

    def test_receipt_is_frozen(self):
        receipt = RemoteDeliveryReceipt(RemoteDeliveryStatus.DELIVERED, "C1", TARGET_AGENT)
        with self.assertRaises(FrozenInstanceError):
            receipt.status = RemoteDeliveryStatus.REJECTED_NOT_A_PACKET

    def test_receipt_rejects_secret_shaped_fields(self):
        for field_name in ("correlation_id", "target_agent"):
            with self.subTest(field=field_name):
                with self.assertRaises(PacketValidationError):
                    RemoteDeliveryReceipt(RemoteDeliveryStatus.DELIVERED,
                                          **{field_name: "api_key=1"})

    def test_receipt_repr_stays_clean(self):
        receipt = RemoteDeliveryReceipt(RemoteDeliveryStatus.DELIVERED, "C1", TARGET_AGENT)
        surface = (repr(receipt) + str(receipt)).lower()
        for marker in SECRET_MARKERS:
            self.assertNotIn(marker, surface)

    def test_identical_receipts_are_equal(self):
        self.assertEqual(
            RemoteDeliveryReceipt(RemoteDeliveryStatus.DELIVERED, "C1", TARGET_AGENT),
            RemoteDeliveryReceipt(RemoteDeliveryStatus.DELIVERED, "C1", TARGET_AGENT),
        )

    def test_exchange_error_vocabulary_is_closed(self):
        for category in REMOTE_CATEGORIES:
            with self.subTest(category=category):
                self.assertEqual(RemoteExchangeError(category).category, category)
        for bad in ("DELIVERED", "REJECTED_NOT_A_PACKET", "BOGUS", "", None):
            with self.subTest(bad=repr(bad)):
                with self.assertRaises(ValueError):
                    RemoteExchangeError(bad)

    def test_loopback_conforms_to_the_protocol(self):
        self.assertIsInstance(LoopbackRemoteTransport(), RemoteTransport)


class SendTests(unittest.TestCase):
    def test_valid_packet_is_delivered_with_verbatim_fields(self):
        receipt = LoopbackRemoteTransport().send(envelope())
        self.assertEqual(receipt.status, RemoteDeliveryStatus.DELIVERED)
        self.assertEqual(receipt.correlation_id, "C001")
        self.assertEqual(receipt.target_agent, TARGET_AGENT)

    def test_non_packet_inputs_are_rejected(self):
        transport = LoopbackRemoteTransport()
        for bad in (object(), {}, None, "x", 7, lambda: None, BoobyTrapped()):
            with self.subTest(bad=type(bad).__name__):
                receipt = transport.send(bad)
                self.assertEqual(receipt.status, RemoteDeliveryStatus.REJECTED_NOT_A_PACKET)
                self.assertEqual(receipt.correlation_id, "")
                self.assertEqual(receipt.target_agent, "")

    def test_booby_trapped_object_never_leaks_or_raises(self):
        receipt = LoopbackRemoteTransport().send(BoobyTrapped())
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
        receipt = LoopbackRemoteTransport().send(sub)
        self.assertEqual(receipt.status, RemoteDeliveryStatus.REJECTED_NOT_A_PACKET)
        self.assertEqual(receipt.correlation_id, "")

    def test_silently_mutating_payloads_are_rejected(self):
        mutations = {
            "int dict key": dict(interfaces=({1: "a"},)),
            "bool dict key": dict(interfaces=({True: "x"},)),
            "list in dict value": dict(steps=({"steps": [1, 2]},)),
            "nan float": dict(risks=({"score": float("nan")},)),
            "mixed dict keys": dict(interfaces=({"a": 1, 1: "b"},)),
        }
        transport = LoopbackRemoteTransport()
        for label, kwargs in mutations.items():
            with self.subTest(mutation=label):
                receipt = transport.send(envelope(payload=arch(**kwargs)))
                self.assertEqual(receipt.status, RemoteDeliveryStatus.REJECTED_INVALID_PACKET)
                self.assertEqual(receipt.correlation_id, "C001")
                self.assertEqual(receipt.target_agent, TARGET_AGENT)

    def test_non_serializable_payload_is_rejected(self):
        receipt = LoopbackRemoteTransport().send(
            envelope(payload=arch(interfaces=({"cb": object()},))))
        self.assertEqual(receipt.status, RemoteDeliveryStatus.REJECTED_INVALID_PACKET)
        self.assertEqual(receipt.correlation_id, "C001")

    def test_rejections_never_deliver(self):
        transport = LoopbackRemoteTransport()
        transport.send(envelope(payload=arch(interfaces=({1: "a"},))))
        transport.send(object())
        transport.send(None)
        self.assertIsNone(transport.receive(TARGET_AGENT))


class FaultInjectionTests(unittest.TestCase):
    def test_every_remote_category_maps_to_its_status(self):
        for category in REMOTE_CATEGORIES:
            with self.subTest(category=category):
                transport = LoopbackRemoteTransport(raising_exchange(category))
                receipt = transport.send(envelope())
                self.assertEqual(receipt.status.value, category)
                self.assertEqual(receipt.correlation_id, "C001")
                self.assertEqual(receipt.target_agent, TARGET_AGENT)
                self.assertIsNone(transport.receive(TARGET_AGENT))

    def test_failure_is_isolated_to_the_failing_target(self):
        other = agent_id_for(("rt-c", "provider-c", "model-c", "fp-c"))
        transport = LoopbackRemoteTransport(
            selective_exchange(TARGET_AGENT, "REMOTE_UNAVAILABLE"))
        failed = transport.send(envelope())
        delivered = transport.send(envelope(correlation_id="C2", target_agent=other,
                                            source_agent=TARGET_AGENT,
                                            source_role="coder", target_role="architect",
                                            payload_type=CollaborationPayloadType.IMPLEMENTATION,
                                            payload=impl()))
        self.assertEqual(failed.status, RemoteDeliveryStatus.REMOTE_UNAVAILABLE)
        self.assertEqual(delivered.status, RemoteDeliveryStatus.DELIVERED)
        self.assertEqual(transport.receive(other).correlation_id, "C2")
        self.assertIsNone(transport.receive(TARGET_AGENT))

    def test_delivered_does_not_mean_consumed(self):
        transport = LoopbackRemoteTransport(dropping_exchange)
        receipt = transport.send(envelope())
        self.assertEqual(receipt.status, RemoteDeliveryStatus.DELIVERED)
        self.assertIsNone(transport.receive(TARGET_AGENT))


class ReceiveTests(unittest.TestCase):
    def test_empty_returns_none(self):
        self.assertIsNone(LoopbackRemoteTransport().receive(TARGET_AGENT))

    def test_loopback_declares_fifo_exact_sequence(self):
        transport = LoopbackRemoteTransport()
        order = ["C1", "C2", "C3"]
        types_ = (CollaborationPayloadType.ARCHITECTURE,
                  CollaborationPayloadType.TEST,
                  CollaborationPayloadType.IMPLEMENTATION)
        for correlation_id, payload_type in zip(order, types_):
            transport.send(envelope(correlation_id=correlation_id,
                                    payload_type=payload_type,
                                    payload=payload_for(payload_type)))
        received_ids = [transport.receive(TARGET_AGENT).correlation_id for _ in order]
        self.assertEqual(received_ids, order)  # exact sequence, not a multiset
        self.assertIsNone(transport.receive(TARGET_AGENT))

    def test_delivery_is_by_value_not_reference(self):
        transport = LoopbackRemoteTransport()
        packet = envelope()
        transport.send(packet)
        received = transport.receive(TARGET_AGENT)
        self.assertEqual(received, packet)
        self.assertIsNot(received, packet)
        self.assertIsNot(received.payload, packet.payload)

    def test_full_field_fidelity(self):
        transport = LoopbackRemoteTransport()
        packet = envelope(acceptance_criteria=("c1", "c2"))
        transport.send(packet)
        received = transport.receive(TARGET_AGENT)
        for field_name in ("correlation_id", "task_id", "source_agent", "target_agent",
                           "source_role", "target_role", "payload_type", "payload",
                           "acceptance_criteria", "protocol_version", "provenance"):
            self.assertEqual(getattr(received, field_name), getattr(packet, field_name),
                             field_name)

    def test_provenance_offline_and_real_pass_through_untouched(self):
        for provenance in ("OFFLINE", "REAL"):
            with self.subTest(provenance=provenance):
                transport = LoopbackRemoteTransport()
                transport.send(envelope(provenance=provenance))
                self.assertEqual(transport.receive(TARGET_AGENT).provenance, provenance)

    def test_unknown_and_empty_agent_ids_return_none(self):
        transport = LoopbackRemoteTransport()
        transport.send(envelope())
        unknown = agent_id_for(("rt-z", "provider-z", "model-z", "fp-z"))
        self.assertIsNone(transport.receive(unknown))
        self.assertIsNone(transport.receive(""))

    def test_unhashable_agent_id_raises_honestly(self):
        with self.assertRaises(TypeError):
            LoopbackRemoteTransport().receive([])


class CorruptedWireTests(unittest.TestCase):
    def test_corrupted_wire_surfaces_honestly_on_receive(self):
        transport = LoopbackRemoteTransport(corrupting_exchange)
        receipt = transport.send(envelope())
        self.assertEqual(receipt.status, RemoteDeliveryStatus.DELIVERED)
        with self.assertRaises(PacketValidationError):
            transport.receive(TARGET_AGENT)

    def test_structured_but_invalid_wire_also_surfaces(self):
        def structured_garbage(target_agent, wire, sink):
            sink(target_agent, "{}")
        transport = LoopbackRemoteTransport(structured_garbage)
        transport.send(envelope())
        with self.assertRaises(PacketValidationError):
            transport.receive(TARGET_AGENT)


class DuplicateTests(unittest.TestCase):
    def test_duplicate_sink_delivers_twice_with_fresh_decodings(self):
        transport = LoopbackRemoteTransport(duplicating_exchange)
        packet = envelope()
        transport.send(packet)
        first = transport.receive(TARGET_AGENT)
        second = transport.receive(TARGET_AGENT)
        self.assertEqual(first, packet)
        self.assertEqual(second, packet)
        self.assertIsNot(first, second)
        self.assertIsNone(transport.receive(TARGET_AGENT))


class E2ETests(unittest.TestCase):
    def test_request_reply_loop_with_same_correlation(self):
        transport = LoopbackRemoteTransport()
        request = envelope(correlation_id="C001")
        self.assertEqual(transport.send(request).status, RemoteDeliveryStatus.DELIVERED)
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
        self.assertEqual(transport.send(reply).status, RemoteDeliveryStatus.DELIVERED)
        architect_got = transport.receive(SOURCE_AGENT)
        self.assertEqual(architect_got, reply)
        self.assertEqual(architect_got.correlation_id, coder_got.correlation_id)
        self.assertIsNone(transport.receive(SOURCE_AGENT))
        self.assertIsNone(transport.receive(TARGET_AGENT))

    def test_all_four_payload_types_round_trip(self):
        for payload_type in CollaborationPayloadType:
            with self.subTest(payload_type=payload_type.name):
                transport = LoopbackRemoteTransport()
                packet = envelope(payload_type=payload_type, payload=payload_for(payload_type))
                self.assertEqual(transport.send(packet).status, RemoteDeliveryStatus.DELIVERED)
                self.assertEqual(transport.receive(TARGET_AGENT), packet)


class DeterminismTests(unittest.TestCase):
    def test_same_sequence_same_results_on_fresh_instances(self):
        def scenario():
            transport = LoopbackRemoteTransport()
            receipts = [transport.send(envelope(correlation_id=f"C{index}"))
                        for index in range(3)]
            received = tuple(transport.receive(TARGET_AGENT) for _ in range(3))
            return receipts, received

        first_receipts, first_received = scenario()
        second_receipts, second_received = scenario()
        self.assertEqual(first_receipts, second_receipts)
        self.assertEqual(first_received, second_received)

    def test_wire_form_of_received_is_canonical(self):
        transport = LoopbackRemoteTransport()
        packet = envelope()
        transport.send(packet)
        received = transport.receive(TARGET_AGENT)
        self.assertEqual(
            serialize_collaboration_packet(received),
            serialize_collaboration_packet(packet),
        )


class SourceScanTests(unittest.TestCase):
    def test_no_runtime_names(self):
        import remote_transport as module
        text = Path(module.__file__).read_text(encoding="utf-8").lower()
        for name in ("claude", "codex", "deepseek", "openai", "anthropic",
                     "gemini", "tiny-agents", "tiny_agents"):
            self.assertNotIn(name, text)

    def test_no_forbidden_imports_or_channels(self):
        import remote_transport as module
        source = Path(module.__file__).read_text(encoding="utf-8")
        for forbidden in ("external_runtime", "execution_engine", "orchestrator",
                          "verified_", "runtime_health", "capability_registry",
                          "task_budget", "loop_guard", "fallback", "score",
                          "subprocess", "invoke", "os.environ", "getenv",
                          "RUN_REAL_PROVIDER_TESTS", "requests", "urllib",
                          "socket", "async", "threading", "http", "websocket",
                          "a2a"):
            self.assertNotIn(forbidden, source)

    def test_no_clock_or_id_minting_in_precise_forms(self):
        import remote_transport as module
        source = Path(module.__file__).read_text(encoding="utf-8")
        for forbidden in ("import time", "time.", "monotonic", "sleep",
                          "random", "uuid", "datetime", "new_correlation_id",
                          "clock"):
            self.assertNotIn(forbidden, source)

    def test_provenance_is_never_referenced(self):
        import remote_transport as module
        source = Path(module.__file__).read_text(encoding="utf-8")
        self.assertNotIn("provenance", source)


if __name__ == "__main__":
    unittest.main()
