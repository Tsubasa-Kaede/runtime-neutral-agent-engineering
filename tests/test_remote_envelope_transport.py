"""V3.1-B1: Loopback envelope transport — behavior tests.

The transport seam moves RemoteEnvelope values across an in-process,
offline loopback boundary as canonical wire text only: send validates and
serializes, the mailbox stores wire, receive decodes a fresh envelope.
DELIVERED is the honest success status for a synchronous atomic loopback;
ACCEPTED (taken but not yet delivered) can never honestly appear here.
Expected far-side conditions become receipt values through one explicit
mapping authority; corrupted stored wire surfaces as a structured
contract error, never as an empty mailbox; programmer mistakes raise.
Fully offline: remote conditions are simulated by injected exchanges.
"""
import ast
import json
import dataclasses
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from agent_identity import AgentIdentity, agent_address
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
    deserialize_remote_envelope,
    new_message_id,
    serialize_remote_envelope,
)
from remote_envelope_transport import (
    EXCHANGE_CATEGORY_TO_RECEIPT,
    LoopbackEnvelopeTransport,
    RemoteEnvelopeReceipt,
    RemoteEnvelopeTransport,
)
from remote_transport import RemoteExchangeError, _REMOTE_CATEGORIES
from structured_packets import (
    ArchitecturePacket,
    ImplementationPacket,
    ReviewPacket,
    TestPacket,
)


def arch(task_id="T1"):
    return ArchitecturePacket(
        task_id=task_id, role="architect", goal=("g",), constraints=("c",),
        architecture=("a",), interfaces=({},), implementation_steps=({},),
        acceptance_criteria=("ac1",), risks=({},),
    )


def packet(cid=None, target_role="coder"):
    cid = cid or new_correlation_id()
    return CollaborationPacket(
        correlation_id=cid, task_id="T1",
        source_agent="arch-bot", target_agent="coder-bot",
        source_role="architect", target_role=target_role,
        payload_type=CollaborationPayloadType.ARCHITECTURE, payload=arch(),
    )


def envelope(**overrides):
    cid = overrides.get("correlation_id") or new_correlation_id()
    values = dict(
        message_id=new_message_id(),
        correlation_id=cid,
        sender=agent_address(AgentIdentity("arch-bot"), "architect"),
        recipient=agent_address(AgentIdentity("coder-bot"), "coder"),
        role="coder",
        payload_type=RemotePayloadType.COLLABORATION_PACKET,
    )
    if "payload" not in overrides:
        values["payload"] = packet(cid)
    values.update(overrides)
    return RemoteEnvelope(**values)


def raised_code(callable_):
    try:
        callable_()
    except RemoteEnvelopeError as exc:
        return exc.category
    raise AssertionError("expected RemoteEnvelopeError")


class BoobyTrapped:
    """Any attribute access or comparison explodes; send() must not care."""

    def __getattr__(self, name):
        raise RuntimeError(f"booby trap sprung: {name}")

    def __eq__(self, other):
        raise RuntimeError("booby trap sprung: __eq__")

    def __hash__(self):
        raise RuntimeError("booby trap sprung: __hash__")


def raising_exchange(category):
    def exchange(recipient, wire, sink):
        raise RemoteExchangeError(category)
    return exchange


def dropping_exchange(recipient, wire, sink):
    return None  # accepted by the exchange, never sunk


def duplicating_exchange(recipient, wire, sink):
    sink(recipient, wire)
    sink(recipient, wire)


def corrupting_exchange(recipient, wire, sink):
    # Deterministic damage: half-truncated JSON can never decode.
    sink(recipient, wire[: len(wire) // 2])


def selective_exchange(fail_for, category):
    def exchange(recipient, wire, sink):
        if recipient == fail_for:
            raise RemoteExchangeError(category)
        sink(recipient, wire)
    return exchange


class TransportContractTests(unittest.TestCase):
    def test_loopback_conforms_to_the_protocol(self):
        self.assertIsInstance(LoopbackEnvelopeTransport(),
                              RemoteEnvelopeTransport)

    def test_receipt_field_set_is_exact(self):
        self.assertEqual(
            {field.name for field in dataclasses.fields(RemoteEnvelopeReceipt)},
            {"status", "message_id", "correlation_id", "recipient",
             "error_code"})

    def test_receipt_is_frozen(self):
        receipt = RemoteEnvelopeReceipt(RemoteEnvelopeStatus.DELIVERED)
        with self.assertRaises(Exception):
            receipt.status = RemoteEnvelopeStatus.REJECTED

    def test_identical_receipts_are_equal(self):
        first = RemoteEnvelopeReceipt(
            RemoteEnvelopeStatus.REJECTED, "m", "c", "r",
            RemoteEnvelopeErrorCode.INVALID_ENVELOPE)
        second = RemoteEnvelopeReceipt(
            RemoteEnvelopeStatus.REJECTED, "m", "c", "r",
            RemoteEnvelopeErrorCode.INVALID_ENVELOPE)
        self.assertEqual(first, second)
        self.assertEqual(hash(first), hash(second))

    def test_mapping_authority_covers_exactly_the_exchange_categories(self):
        self.assertEqual(set(EXCHANGE_CATEGORY_TO_RECEIPT),
                         set(_REMOTE_CATEGORIES))

    def test_mapping_authority_maps_to_honest_delivery_statuses(self):
        expected = {
            "REMOTE_UNAVAILABLE": (RemoteEnvelopeStatus.FAILED,
                                   RemoteEnvelopeErrorCode.DELIVERY_FAILED),
            "REMOTE_PROTOCOL_ERROR": (RemoteEnvelopeStatus.FAILED,
                                      RemoteEnvelopeErrorCode.DELIVERY_FAILED),
            "REMOTE_TIMEOUT": (RemoteEnvelopeStatus.TIMEOUT,
                               RemoteEnvelopeErrorCode.DELIVERY_TIMEOUT),
            "REMOTE_REJECTED": (RemoteEnvelopeStatus.REJECTED, None),
            "AUTH_REQUIRED": (RemoteEnvelopeStatus.REJECTED, None),
        }
        self.assertEqual(dict(EXCHANGE_CATEGORY_TO_RECEIPT), expected)

    def test_mapping_authority_never_produces_accepted(self):
        statuses = {status for status, _ in
                    EXCHANGE_CATEGORY_TO_RECEIPT.values()}
        self.assertNotIn(RemoteEnvelopeStatus.ACCEPTED, statuses)

    def test_session_layer_codes_are_not_delivery_facts(self):
        # UNKNOWN_AGENT / ROLE_UNAVAILABLE / REMOTE_EXECUTION_FAILED belong
        # to the session/remote-execution layer; the transport must not own
        # them, so they cannot appear in the delivery mapping.
        joined = " ".join(EXCHANGE_CATEGORY_TO_RECEIPT)
        for code in ("UNKNOWN_AGENT", "ROLE_UNAVAILABLE",
                     "REMOTE_EXECUTION_FAILED"):
            self.assertNotIn(code, joined)


class LoopbackDeliveryTests(unittest.TestCase):
    def test_valid_envelope_is_delivered(self):
        transport = LoopbackEnvelopeTransport()
        env = envelope()
        receipt = transport.send(env)
        self.assertEqual(receipt.status, RemoteEnvelopeStatus.DELIVERED)
        self.assertIsNone(receipt.error_code)

    def test_receipt_echoes_the_wire_identity(self):
        transport = LoopbackEnvelopeTransport()
        env = envelope()
        receipt = transport.send(env)
        self.assertEqual(receipt.message_id, env.message_id)
        self.assertEqual(receipt.correlation_id, env.correlation_id)
        self.assertEqual(receipt.recipient, env.recipient)

    def test_receive_returns_a_fresh_decoded_envelope(self):
        transport = LoopbackEnvelopeTransport()
        env = envelope()
        transport.send(env)
        received = transport.receive(env.recipient)
        self.assertIsNot(received, env)
        self.assertIsNot(received.payload, env.payload)
        self.assertEqual(received, env)

    def test_receive_preserves_every_envelope_field(self):
        transport = LoopbackEnvelopeTransport()
        env = envelope()
        transport.send(env)
        received = transport.receive(env.recipient)
        self.assertEqual(received.message_id, env.message_id)
        self.assertEqual(received.correlation_id, env.correlation_id)
        self.assertEqual(received.sender, env.sender)
        self.assertEqual(received.recipient, env.recipient)
        self.assertEqual(received.role, env.role)
        self.assertEqual(received.payload, env.payload)

    def test_transport_never_rewrites_sender_or_recipient(self):
        transport = LoopbackEnvelopeTransport()
        env = envelope()
        transport.send(env)
        received = transport.receive(env.recipient)
        self.assertEqual(received.sender, agent_address(
            AgentIdentity("arch-bot"), "architect"))
        self.assertEqual(received.recipient, agent_address(
            AgentIdentity("coder-bot"), "coder"))

    def test_mailbox_stores_canonical_wire_text(self):
        captured = []

        def capture(recipient, wire, sink):
            captured.append(wire)
            sink(recipient, wire)

        transport = LoopbackEnvelopeTransport(exchange=capture)
        env = envelope()
        transport.send(env)
        self.assertEqual(captured, [serialize_remote_envelope(env)])

    def test_serialization_is_deterministic_across_sends(self):
        captured = []

        def capture(recipient, wire, sink):
            captured.append(wire)
            sink(recipient, wire)

        transport = LoopbackEnvelopeTransport(exchange=capture)
        env = envelope()
        transport.send(env)
        transport.send(env)
        self.assertEqual(len(captured), 2)
        self.assertEqual(captured[0], captured[1])
        self.assertEqual(captured[0], serialize_remote_envelope(env))

    def test_fifo_order_per_recipient(self):
        transport = LoopbackEnvelopeTransport()
        cid = new_correlation_id()
        first = envelope(correlation_id=cid, message_id="remote-m1")
        second = envelope(correlation_id=cid, message_id="remote-m2")
        third = envelope(correlation_id=cid, message_id="remote-m3")
        for env in (first, second, third):
            transport.send(env)
        recipient = first.recipient
        self.assertEqual(transport.receive(recipient).message_id, "remote-m1")
        self.assertEqual(transport.receive(recipient).message_id, "remote-m2")
        self.assertEqual(transport.receive(recipient).message_id, "remote-m3")

    def test_recipient_isolation(self):
        transport = LoopbackEnvelopeTransport()
        env = envelope()
        transport.send(env)
        bystander = agent_address(AgentIdentity("tester-bot"), "tester")
        self.assertIsNotNone(transport.receive(env.recipient))
        self.assertIsNone(transport.receive(bystander))

    def test_empty_mailbox_returns_none(self):
        transport = LoopbackEnvelopeTransport()
        self.assertIsNone(transport.receive("agent:nobody:coder"))

    def test_duplicate_delivery_is_allowed_without_dedup(self):
        transport = LoopbackEnvelopeTransport(exchange=duplicating_exchange)
        env = envelope()
        receipt = transport.send(env)
        self.assertEqual(receipt.status, RemoteEnvelopeStatus.DELIVERED)
        first = transport.receive(env.recipient)
        second = transport.receive(env.recipient)
        self.assertEqual(first, env)
        self.assertEqual(second, env)
        self.assertIsNot(first, second)

    def test_accepted_is_never_fabricated(self):
        observed = {RemoteEnvelopeStatus.DELIVERED}
        transport = LoopbackEnvelopeTransport()
        observed.add(transport.send(envelope()).status)
        for category in _REMOTE_CATEGORIES:
            failing = LoopbackEnvelopeTransport(
                exchange=raising_exchange(category))
            observed.add(failing.send(envelope()).status)
        observed.add(transport.send("not an envelope").status)
        self.assertNotIn(RemoteEnvelopeStatus.ACCEPTED, observed)


class LoopbackRejectionTests(unittest.TestCase):
    def test_non_envelope_inputs_are_rejected(self):
        transport = LoopbackEnvelopeTransport()
        for foreign in (object(), "wire text", {"x": 1}, 42,
                        ("claude-cli", "anthropic", None, "fp"),
                        (lambda: None)):
            with self.subTest(payload=type(foreign).__name__):
                receipt = transport.send(foreign)
                self.assertEqual(receipt.status,
                                 RemoteEnvelopeStatus.REJECTED)
                self.assertEqual(receipt.error_code,
                                 RemoteEnvelopeErrorCode.INVALID_ENVELOPE)

    def test_booby_trapped_input_never_leaks_or_raises(self):
        transport = LoopbackEnvelopeTransport()
        receipt = transport.send(BoobyTrapped())
        self.assertEqual(receipt.status, RemoteEnvelopeStatus.REJECTED)
        self.assertEqual(receipt.message_id, "")
        self.assertEqual(receipt.correlation_id, "")
        self.assertEqual(receipt.recipient, "")

    def test_subclass_is_rejected_as_not_an_envelope(self):
        class SubEnvelope(RemoteEnvelope):
            pass

        cid = new_correlation_id()
        sub = SubEnvelope(
            message_id=new_message_id(), correlation_id=cid,
            sender="agent:a:architect", recipient="agent:b:coder",
            role="coder", payload=packet(cid))
        transport = LoopbackEnvelopeTransport()
        receipt = transport.send(sub)
        self.assertEqual(receipt.status, RemoteEnvelopeStatus.REJECTED)
        self.assertEqual(receipt.error_code,
                         RemoteEnvelopeErrorCode.INVALID_ENVELOPE)

    def test_rejections_never_deliver(self):
        transport = LoopbackEnvelopeTransport()
        transport.send("not an envelope")
        self.assertIsNone(transport.receive("agent:anyone:coder"))

    def test_unsupported_protocol_stops_at_the_contract_not_the_transport(self):
        # An off-version envelope cannot even be constructed, so the
        # transport needs no second protocol rule of its own.
        code = raised_code(lambda: envelope(protocol_version="2.0"))
        self.assertEqual(code, RemoteEnvelopeErrorCode.UNSUPPORTED_PROTOCOL)


class LoopbackFaultInjectionTests(unittest.TestCase):
    def test_every_exchange_category_maps_to_its_receipt(self):
        for category in _REMOTE_CATEGORIES:
            with self.subTest(category=category):
                transport = LoopbackEnvelopeTransport(
                    exchange=raising_exchange(category))
                env = envelope()
                receipt = transport.send(env)
                expected = EXCHANGE_CATEGORY_TO_RECEIPT[category]
                self.assertEqual(
                    (receipt.status, receipt.error_code), expected)
                # failure receipts still echo the wire identity
                self.assertEqual(receipt.message_id, env.message_id)
                self.assertEqual(receipt.correlation_id, env.correlation_id)
                self.assertEqual(receipt.recipient, env.recipient)

    def test_failure_is_isolated_to_the_failing_recipient(self):
        target = agent_address(AgentIdentity("coder-bot"), "coder")
        other = agent_address(AgentIdentity("tester-bot"), "tester")
        transport = LoopbackEnvelopeTransport(
            exchange=selective_exchange(target, "REMOTE_UNAVAILABLE"))
        blocked = envelope(recipient=target)
        through = envelope(recipient=other)
        self.assertEqual(transport.send(blocked).status,
                         RemoteEnvelopeStatus.FAILED)
        self.assertEqual(transport.send(through).status,
                         RemoteEnvelopeStatus.DELIVERED)
        self.assertIsNone(transport.receive(target))
        self.assertEqual(transport.receive(other), through)

    def test_corrupted_stored_wire_fails_loudly_on_receive(self):
        transport = LoopbackEnvelopeTransport(exchange=corrupting_exchange)
        env = envelope()
        receipt = transport.send(env)
        self.assertEqual(receipt.status, RemoteEnvelopeStatus.DELIVERED)
        code = raised_code(lambda: transport.receive(env.recipient))
        self.assertEqual(code, RemoteEnvelopeErrorCode.INVALID_ENVELOPE)

    def test_corrupted_wire_never_masquerades_as_empty_mailbox(self):
        transport = LoopbackEnvelopeTransport(exchange=corrupting_exchange)
        transport.send(envelope())
        with self.assertRaises(RemoteEnvelopeError):
            transport.receive(envelope().recipient)
        # The corrupted wire was consumed loudly (V2 loopback philosophy);
        # the mailbox is then genuinely empty and says so honestly.
        self.assertIsNone(transport.receive(
            agent_address(AgentIdentity("coder-bot"), "coder")))


class DeliveryFactTests(unittest.TestCase):
    """B1-01: DELIVERED requires an actual delivery fact (the wire entered
    this recipient's mailbox). An exchange that returns without sinking
    produced no delivery fact — honesty demands FAILED, never DELIVERED
    and never a fabricated ACCEPTED."""

    def test_drop_without_sink_is_failed_not_delivered(self):
        transport = LoopbackEnvelopeTransport(exchange=dropping_exchange)
        env = envelope()
        receipt = transport.send(env)
        self.assertEqual(receipt.status, RemoteEnvelopeStatus.FAILED)
        self.assertIs(receipt.error_code,
                      RemoteEnvelopeErrorCode.DELIVERY_FAILED)
        self.assertEqual(receipt.message_id, env.message_id)
        self.assertEqual(receipt.correlation_id, env.correlation_id)
        self.assertEqual(receipt.recipient, env.recipient)
        self.assertIsNone(transport.receive(env.recipient))

    def test_exchange_programming_error_propagates(self):
        def buggy(recipient, wire, sink):
            raise RuntimeError("exchange bug")

        transport = LoopbackEnvelopeTransport(exchange=buggy)
        env = envelope()
        with self.assertRaises(RuntimeError):
            transport.send(env)
        self.assertIsNone(transport.receive(env.recipient))

    def test_delivered_while_still_queued_means_delivery_not_execution(self):
        # DELIVERED is correct exactly when the wire truly entered the
        # mailbox — it says nothing about whether the peer agent executed.
        transport = LoopbackEnvelopeTransport()
        env = envelope()
        self.assertEqual(transport.send(env).status,
                         RemoteEnvelopeStatus.DELIVERED)
        self.assertEqual(transport.receive(env.recipient), env)

    def test_rejection_leaves_prior_success_untouched(self):
        transport = LoopbackEnvelopeTransport()
        env = envelope()
        self.assertEqual(transport.send(env).status,
                         RemoteEnvelopeStatus.DELIVERED)
        rejected = transport.send("not an envelope")
        self.assertEqual(rejected.status, RemoteEnvelopeStatus.REJECTED)
        self.assertEqual(transport.receive(env.recipient), env)
        self.assertIsNone(transport.receive(env.recipient))


class CodecFailureTests(unittest.TestCase):
    """B1-02: expected codec/validation failures on a constructed envelope
    become structured rejections — they never escape send(). The exchange's
    own programming errors still propagate (see DeliveryFactTests)."""

    def wrapped(self, interfaces):
        inner = ArchitecturePacket(
            task_id="T1", role="architect", goal=("g",), constraints=("c",),
            architecture=("a",), interfaces=interfaces,
            implementation_steps=({},), acceptance_criteria=("ac1",),
            risks=({},),
        )
        cid = new_correlation_id()
        payload = CollaborationPacket(
            correlation_id=cid, task_id="T1",
            source_agent="arch-bot", target_agent="coder-bot",
            source_role="architect", target_role="coder",
            payload_type=CollaborationPayloadType.ARCHITECTURE,
            payload=inner,
        )
        return envelope(correlation_id=cid, payload=payload)

    def test_unserializable_payload_is_rejected_not_raised(self):
        env = self.wrapped(({"cb": object()},))
        receipt = LoopbackEnvelopeTransport().send(env)
        self.assertEqual(receipt.status, RemoteEnvelopeStatus.REJECTED)
        self.assertIs(receipt.error_code,
                      RemoteEnvelopeErrorCode.INVALID_ENVELOPE)
        self.assertEqual(receipt.message_id, env.message_id)
        self.assertIsNone(LoopbackEnvelopeTransport().receive(env.recipient))

    def test_mixed_dict_keys_are_rejected_not_raised(self):
        env = self.wrapped(({"a": 1, 1: "b"},))
        receipt = LoopbackEnvelopeTransport().send(env)
        self.assertEqual(receipt.status, RemoteEnvelopeStatus.REJECTED)
        self.assertIs(receipt.error_code,
                      RemoteEnvelopeErrorCode.INVALID_ENVELOPE)


class RoutingVerificationTests(unittest.TestCase):
    """B1-03: a mailbox only ever yields its own mail. receive() verifies
    decoded.recipient against the mailbox key and fails loudly on a
    mismatch — no rerouting, no rewriting, no silent swap."""

    def env_for(self, seq, sender_id, sender_role, recipient_id,
                recipient_role):
        cid = new_correlation_id()
        payload = CollaborationPacket(
            correlation_id=cid, task_id="T1",
            source_agent=sender_id, target_agent=recipient_id,
            source_role=sender_role, target_role=recipient_role,
            payload_type=CollaborationPayloadType.ARCHITECTURE,
            payload=arch(),
        )
        return envelope(
            message_id=f"remote-{seq}",
            correlation_id=cid,
            sender=agent_address(AgentIdentity(sender_id), sender_role),
            recipient=agent_address(AgentIdentity(recipient_id),
                                    recipient_role),
            role=recipient_role,
            payload=payload,
        )

    def test_mailbox_wire_addressed_elsewhere_fails_loudly(self):
        to_coder = self.env_for("m1", "arch-bot", "architect",
                                "coder-bot", "coder")
        to_reviewer = self.env_for("m2", "arch-bot", "architect",
                                   "reviewer-bot", "reviewer")
        transport = LoopbackEnvelopeTransport()
        transport.send(to_coder)
        # Poison: mailbox A now holds a perfectly valid wire addressed to B.
        transport._mailboxes[to_coder.recipient][0] = (
            serialize_remote_envelope(to_reviewer))
        code = raised_code(lambda: transport.receive(to_coder.recipient))
        self.assertEqual(code, RemoteEnvelopeErrorCode.INVALID_ENVELOPE)

    def test_interleaved_dual_recipient_fifo(self):
        a1 = self.env_for("m1", "arch-bot", "architect", "coder-bot", "coder")
        b1 = self.env_for("m2", "arch-bot", "architect",
                          "reviewer-bot", "reviewer")
        a2 = self.env_for("m3", "arch-bot-2", "architect",
                          "coder-bot", "coder")
        b2 = self.env_for("m4", "arch-bot-2", "architect",
                          "reviewer-bot", "reviewer")
        transport = LoopbackEnvelopeTransport()
        for env in (a1, b1, a2, b2):
            transport.send(env)
        self.assertEqual(transport.receive(a1.recipient), a1)
        self.assertEqual(transport.receive(b1.recipient), b1)
        self.assertEqual(transport.receive(a1.recipient), a2)
        self.assertEqual(transport.receive(b1.recipient), b2)
        self.assertIsNone(transport.receive(a1.recipient))
        self.assertIsNone(transport.receive(b1.recipient))

    def test_both_mailboxes_consume_only_their_own(self):
        to_coder = self.env_for("m1", "arch-bot", "architect",
                                "coder-bot", "coder")
        to_reviewer = self.env_for("m2", "arch-bot", "architect",
                                   "reviewer-bot", "reviewer")
        transport = LoopbackEnvelopeTransport()
        transport.send(to_coder)
        transport.send(to_reviewer)
        self.assertEqual(transport.receive(to_coder.recipient).recipient,
                         to_coder.recipient)
        self.assertEqual(transport.receive(to_reviewer.recipient).recipient,
                         to_reviewer.recipient)


class RoundTripPayloadTests(unittest.TestCase):
    """Formal probes: deep object isolation across duplicate receives, and
    full four-payload-type roundtrips through the canonical wire."""

    def test_all_four_payload_types_round_trip(self):
        cases = (
            (CollaborationPayloadType.ARCHITECTURE, "architect", arch(),
             "coder", "arch-bot", "coder-bot"),
            (CollaborationPayloadType.IMPLEMENTATION, "coder", self.impl(),
             "tester", "coder-bot", "tester-bot"),
            (CollaborationPayloadType.TEST, "tester", self.test_pkt(),
             "reviewer", "tester-bot", "reviewer-bot"),
            (CollaborationPayloadType.REVIEW, "reviewer", self.review_pkt(),
             "architect", "reviewer-bot", "arch-bot"),
        )
        for (payload_type, source_role, payload, target_role,
             source_id, target_id) in cases:
            with self.subTest(payload_type=payload_type.name):
                cid = new_correlation_id()
                packet = CollaborationPacket(
                    correlation_id=cid, task_id="T1",
                    source_agent=source_id, target_agent=target_id,
                    source_role=source_role, target_role=target_role,
                    payload_type=payload_type, payload=payload,
                )
                env = envelope(
                    message_id=f"remote-{payload_type.value}",
                    correlation_id=cid,
                    sender=agent_address(AgentIdentity(source_id),
                                         source_role),
                    recipient=agent_address(AgentIdentity(target_id),
                                            target_role),
                    role=target_role, payload=packet,
                )
                transport = LoopbackEnvelopeTransport()
                self.assertEqual(transport.send(env).status,
                                 RemoteEnvelopeStatus.DELIVERED)
                self.assertEqual(transport.receive(env.recipient), env)

    def test_deep_object_isolation_across_duplicate_receives(self):
        transport = LoopbackEnvelopeTransport(exchange=duplicating_exchange)
        env = envelope()
        transport.send(env)
        first = transport.receive(env.recipient)
        second = transport.receive(env.recipient)
        self.assertIsNot(first, second)
        self.assertIsNot(first.payload, second.payload)
        self.assertIsNot(first.payload.payload, second.payload.payload)

    def impl(self):
        return ImplementationPacket(
            task_id="T1", role="coder", changed_files=("f.py",),
            implementation_summary="s", implementation_details=("d",),
            assumptions=(), unresolved_items=(), test_requirements=(),
        )

    def test_pkt(self):
        return TestPacket(
            task_id="T1", role="tester", tests_run=("t1",),
            tests_passed=("t1",), tests_failed=(), failures=(),
            coverage_or_validation=(), remaining_risks=(),
        )

    def review_pkt(self):
        return ReviewPacket(
            task_id="T1", role="reviewer", status="PASS", findings=(),
            severity=(), affected_files=(), required_changes=(),
            acceptance_criteria_status=(),
        )


class TransportBoundaryTests(unittest.TestCase):
    """AST-precise boundary scans: import modules and code identifiers,
    so prose in docstrings (e.g. naming what this seam must NOT own)
    cannot false-positive a text scan."""
    SOURCE = (SCRIPTS / "remote_envelope_transport.py").read_text(
        encoding="utf-8")
    TREE = ast.parse(SOURCE)

    def imported_modules(self):
        modules = set()
        for node in ast.walk(self.TREE):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    modules.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    modules.add(node.module.split(".")[0])
        return modules

    def imported_modules_and_names(self):
        names = set(self.imported_modules())
        for node in ast.walk(self.TREE):
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    names.add(alias.name)
        return names

    def code_identifiers(self):
        names = set()
        for node in ast.walk(self.TREE):
            if isinstance(node, ast.Name):
                names.add(node.id)
            elif isinstance(node, ast.Attribute):
                names.add(node.attr)
        return names

    def test_no_network_or_process_imports(self):
        banned = {"requests", "httpx", "aiohttp", "websocket", "socket",
                  "grpc", "ssl", "urllib", "asyncio", "subprocess",
                  "multiprocessing", "threading", "concurrent"}
        self.assertFalse(banned & self.imported_modules_and_names(), banned)

    def test_no_identity_or_time_generation(self):
        banned = {"uuid", "random", "time", "datetime", "secrets"}
        self.assertFalse(banned & self.imported_modules_and_names(), banned)
        # The transport echoes ids; it never mints them.
        minters = {"uuid4", "new_message_id", "new_correlation_id",
                   "time", "monotonic"}
        self.assertFalse(minters & self.code_identifiers(), minters)

    def test_no_pickle_family_serialization(self):
        banned = {"pickle", "marshal", "cloudpickle", "shelve", "dill"}
        self.assertFalse(banned & self.imported_modules_and_names(), banned)

    def test_no_runtime_identity_or_business_layer_references(self):
        banned = {"AgentIdentity", "agent_identity", "RuntimeIdentity",
                  "runtime_identity", "RuntimeProfile", "ExternalAgentAdapter",
                  "adapter", "selection", "admission", "qualification",
                  "execution", "runtime_id", "provider", "model"}
        self.assertFalse(banned & self.code_identifiers(), banned)

    def test_external_imports_are_only_the_two_vocabularies(self):
        stdlib_allowed = {"__future__", "collections", "dataclasses",
                          "typing"}
        self.assertEqual(self.imported_modules() - stdlib_allowed,
                         {"remote_contract", "remote_transport"})


class WireModelTests(unittest.TestCase):
    """Wire canonicality, UTF-8 safety, and by-wire storage: the mailbox
    must hold text, so later mutation of the original cannot leak in."""

    def test_wire_is_canonical_json(self):
        captured = []

        def capture(recipient, wire, sink):
            captured.append(wire)
            sink(recipient, wire)

        env = envelope()
        LoopbackEnvelopeTransport(exchange=capture).send(env)
        self.assertEqual(
            captured,
            [json.dumps(env.to_dict(), sort_keys=True, separators=(",", ":"))])

    def test_wire_survives_non_ascii_payload_as_utf8(self):
        non_ascii = ArchitecturePacket(
            task_id="T1", role="architect", goal=("目标",),
            constraints=("约束",), architecture=("架构",), interfaces=({},),
            implementation_steps=({},), acceptance_criteria=("验收通过",),
            risks=({},),
        )
        cid = new_correlation_id()
        wrapped = CollaborationPacket(
            correlation_id=cid, task_id="T1",
            source_agent="arch-bot", target_agent="coder-bot",
            source_role="architect", target_role="coder",
            payload_type=CollaborationPayloadType.ARCHITECTURE,
            payload=non_ascii,
        )
        transport = LoopbackEnvelopeTransport()
        env = envelope(correlation_id=cid, payload=wrapped)
        self.assertEqual(transport.send(env).status,
                         RemoteEnvelopeStatus.DELIVERED)
        received = transport.receive(env.recipient)
        self.assertEqual(received, env)
        wire = serialize_remote_envelope(received)
        self.assertEqual(wire.encode("utf-8").decode("utf-8"), wire)

    def test_mutating_original_after_send_cannot_change_the_mailbox(self):
        transport = LoopbackEnvelopeTransport()
        mutable = ArchitecturePacket(
            task_id="T1", role="architect", goal=("g",), constraints=("c",),
            architecture=("a",), interfaces=({"note": "original"},),
            implementation_steps=({},), acceptance_criteria=("ac1",),
            risks=({},),
        )
        cid = new_correlation_id()
        wrapped = CollaborationPacket(
            correlation_id=cid, task_id="T1",
            source_agent="arch-bot", target_agent="coder-bot",
            source_role="architect", target_role="coder",
            payload_type=CollaborationPayloadType.ARCHITECTURE,
            payload=mutable,
        )
        env = envelope(correlation_id=cid, payload=wrapped)
        transport.send(env)
        mutable.interfaces[0]["note"] = "mutated"
        received = transport.receive(env.recipient)
        self.assertEqual(received.payload.payload.interfaces[0]["note"],
                         "original")

    def test_structured_garbage_wire_surfaces_as_invalid_envelope(self):
        def garbage(recipient, wire, sink):
            sink(recipient, "{}")

        transport = LoopbackEnvelopeTransport(exchange=garbage)
        env = envelope()
        self.assertEqual(transport.send(env).status,
                         RemoteEnvelopeStatus.DELIVERED)
        code = raised_code(lambda: transport.receive(env.recipient))
        self.assertEqual(code, RemoteEnvelopeErrorCode.INVALID_ENVELOPE)

    def test_unsupported_version_wire_surfaces_as_unsupported_protocol(self):
        def tamper(recipient, wire, sink):
            data = json.loads(wire)
            data["protocol_version"] = "2.0"
            sink(recipient, json.dumps(data, sort_keys=True,
                                       separators=(",", ":")))

        transport = LoopbackEnvelopeTransport(exchange=tamper)
        env = envelope()
        self.assertEqual(transport.send(env).status,
                         RemoteEnvelopeStatus.DELIVERED)
        code = raised_code(lambda: transport.receive(env.recipient))
        self.assertEqual(code, RemoteEnvelopeErrorCode.UNSUPPORTED_PROTOCOL)


class AddressAndFidelityTests(unittest.TestCase):
    def test_recipient_address_is_an_opaque_routing_key(self):
        # A V2-shaped address string routes by exact match only: the
        # transport never parses its parts and never demands the V3 shape.
        opaque = '["rt-b","provider-b","model-b","fp-b","coder"]'
        transport = LoopbackEnvelopeTransport()
        receipt = transport.send(envelope(recipient=opaque))
        self.assertEqual(receipt.status, RemoteEnvelopeStatus.DELIVERED)
        self.assertEqual(receipt.recipient, opaque)
        received = transport.receive(opaque)
        self.assertEqual(received.recipient, opaque)
        self.assertIsNone(transport.receive("agent:nobody:coder"))

    def test_payload_type_and_protocol_version_are_preserved(self):
        transport = LoopbackEnvelopeTransport()
        env = envelope()
        transport.send(env)
        received = transport.receive(env.recipient)
        self.assertIs(received.payload_type, env.payload_type)
        self.assertEqual(received.protocol_version, env.protocol_version)

    def test_different_correlations_do_not_crosstalk(self):
        transport = LoopbackEnvelopeTransport()
        one = envelope(message_id="remote-m1",
                       correlation_id=new_correlation_id())
        two = envelope(message_id="remote-m2",
                       correlation_id=new_correlation_id())
        transport.send(one)
        transport.send(two)
        got_one = transport.receive(one.recipient)
        got_two = transport.receive(one.recipient)
        self.assertEqual(got_one, one)
        self.assertEqual(got_two, two)


class ReceiptValidationTests(unittest.TestCase):
    """The receipt speaks the closed contract vocabularies only: neither
    its status nor its error code can carry an arbitrary string."""

    def test_receipt_status_must_be_a_contract_member(self):
        for bad in ("DELIVERED", "EXECUTED", "BOGUS", 7, None):
            with self.subTest(status=repr(bad)):
                with self.assertRaises(ValueError):
                    RemoteEnvelopeReceipt(bad)

    def test_receipt_error_code_must_be_closed_vocabulary(self):
        with self.assertRaises(ValueError):
            RemoteEnvelopeReceipt(RemoteEnvelopeStatus.FAILED,
                                  error_code="HTTP_503")
        with self.assertRaises(ValueError):
            RemoteEnvelopeReceipt(RemoteEnvelopeStatus.FAILED,
                                  error_code=42)
        legal = (None, RemoteEnvelopeErrorCode.DELIVERY_FAILED,
                 RemoteEnvelopeErrorCode.DELIVERY_TIMEOUT,
                 RemoteEnvelopeErrorCode.INVALID_ENVELOPE,
                 RemoteEnvelopeErrorCode.UNSUPPORTED_PROTOCOL)
        for code in legal:
            with self.subTest(error_code=code):
                receipt = RemoteEnvelopeReceipt(
                    RemoteEnvelopeStatus.REJECTED, error_code=code)
                self.assertIs(receipt.error_code, code)


class DeliveryVocabularyTests(unittest.TestCase):
    def test_delivered_is_not_executed(self):
        # Execution is an agent-side outcome of a later unit; the delivery
        # vocabulary must not contain it and DELIVERED must not imply it.
        values = {status.value for status in RemoteEnvelopeStatus}
        self.assertNotIn("EXECUTED", values)

    def test_transport_never_sees_header_inconsistent_envelopes(self):
        # The V3.1-A contract refuses these at construction, so the send
        # path can only ever receive valid envelopes; these rejections are
        # contract facts the transport relies on, not transport logic.
        with self.assertRaises(RemoteEnvelopeError):
            envelope(sender="agent:same:coder", recipient="agent:same:coder")
        with self.assertRaises(RemoteEnvelopeError):
            envelope(role="tester")
        with self.assertRaises(RemoteEnvelopeError):
            envelope(correlation_id=new_correlation_id(),
                     payload=packet(new_correlation_id()))


class FreshInstanceDeterminismTests(unittest.TestCase):
    def fixed_envelope(self, index):
        cid = f"collab-fixed-{index}"
        return envelope(message_id=f"remote-fixed-{index}",
                        correlation_id=cid, payload=packet(cid))

    def test_same_sequence_same_results_on_fresh_instances(self):
        def scenario():
            transport = LoopbackEnvelopeTransport()
            recipient = self.fixed_envelope(0).recipient
            receipts = [transport.send(self.fixed_envelope(index))
                        for index in range(3)]
            received = tuple(transport.receive(recipient)
                             for _ in range(3))
            return receipts, received

        first_receipts, first_received = scenario()
        second_receipts, second_received = scenario()
        self.assertEqual(first_receipts, second_receipts)
        self.assertEqual(first_received, second_received)


class TransportCloseContractTests(unittest.TestCase):
    """V3.1-B1b: the transport lifecycle seam — close() is contract, not
    per-implementation luck. The Protocol must require it, the loopback must
    provide it as an idempotent no-op, and the resource-bearing B2 transport
    must keep satisfying the widened contract with its existing authority."""

    def test_protocol_requires_close(self):
        class SendReceiveOnly:
            def send(self, envelope): ...
            def receive(self, recipient): ...
        # A transport without close() is NOT a RemoteEnvelopeTransport.
        self.assertNotIsInstance(
            SendReceiveOnly(), RemoteEnvelopeTransport)

    def test_protocol_accepts_send_receive_close(self):
        class FullTransport:
            def send(self, envelope): ...
            def receive(self, recipient): ...
            def close(self): ...
        self.assertIsInstance(FullTransport(), RemoteEnvelopeTransport)

    def test_loopback_satisfies_widened_protocol(self):
        self.assertIsInstance(
            LoopbackEnvelopeTransport(), RemoteEnvelopeTransport)

    def test_loopback_provides_callable_close(self):
        transport = LoopbackEnvelopeTransport()
        self.assertTrue(callable(getattr(transport, "close", None)))

    def test_loopback_close_is_idempotent(self):
        transport = LoopbackEnvelopeTransport()
        transport.close()
        transport.close()  # a second close must never raise

    def test_loopback_close_changes_no_behavior(self):
        # No-op semantics: no lifecycle state exists, so send/receive must
        # behave exactly as the contract promises even after close calls.
        transport = LoopbackEnvelopeTransport()
        first = envelope()
        transport.close()
        receipt = transport.send(first)
        self.assertIs(receipt.status, RemoteEnvelopeStatus.DELIVERED)
        transport.close()
        self.assertEqual(transport.receive(first.recipient), first)

    def test_b2_subprocess_keeps_satisfying_protocol(self):
        # B2 owns the real close() authority (child reaping); construction
        # is lazy, so proving conformance spawns nothing. The widened
        # contract must not break the existing implementation.
        from remote_subprocess_transport import (
            SubprocessStdioEnvelopeTransport)
        transport = SubprocessStdioEnvelopeTransport(
            [sys.executable, "-c", "pass"])
        self.assertIsInstance(transport, RemoteEnvelopeTransport)


class SourceScanTests(unittest.TestCase):
    """V2-style whole-source channel scan (10H-C discipline). The AST
    scans above prove what the code does; this scan proves the source
    text carries none of the forbidden vocabulary anywhere — including
    prose — so the seam cannot even describe owning it."""

    SOURCE = (SCRIPTS / "remote_envelope_transport.py").read_text(
        encoding="utf-8")

    def test_no_network_process_or_serialization_channels(self):
        for forbidden in ("requests", "httpx", "aiohttp", "websocket",
                          "socket", "grpc", "ssl", "urllib", "subprocess",
                          "threading", "multiprocessing", "asyncio",
                          "pickle", "marshal", "cloudpickle", "a2a",
                          "http", "async"):
            self.assertNotIn(forbidden, self.SOURCE, forbidden)

    def test_no_clock_or_id_minting_vocabulary(self):
        for forbidden in ("import time", "time.", "monotonic", "sleep",
                          "random", "uuid", "datetime", "clock",
                          "new_message_id", "new_correlation_id"):
            self.assertNotIn(forbidden, self.SOURCE, forbidden)


if __name__ == "__main__":
    unittest.main()
