"""V3.1-A: Remote Collaboration Contract — behavior tests.

The remote boundary envelope (RemoteEnvelope) wraps exactly one existing
CollaborationPacket as payload; it never redefines collaboration business
semantics. These tests lock the contract behavior only: construction,
deterministic canonical JSON, round-trip, malformed rejection, versioning,
Agent-Address (not Runtime-Identity) sender/recipient semantics, the
message_id/correlation_id distinction, the closed delivery vocabulary and
the closed structured error vocabulary. Transport specifics stay outside:
no socket/HTTP/TLS notion may appear in the contract vocabulary, and the
contract module must stay pickle-free and dependency-free (stdlib only).
"""
import json
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from agent_identity import AgentIdentity, AgentRuntimeBinding, agent_address
from collaboration_packet import (
    CollaborationPacket,
    CollaborationPayloadType,
    new_correlation_id,
)
from remote_contract import (
    PROTOCOL_VERSION,
    RemoteEnvelope,
    RemoteEnvelopeError,
    RemoteEnvelopeErrorCode,
    RemoteEnvelopeStatus,
    RemotePayloadType,
    deserialize_remote_envelope,
    new_message_id,
    serialize_remote_envelope,
)
from remote_transport import RemoteDeliveryStatus
from structured_packets import ArchitecturePacket


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
    # correlation override stays consistent with the payload packet unless
    # the test overrides the payload too (mismatch tests do exactly that).
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
    """Run callable_ and return the structured category it raises."""
    try:
        callable_()
    except RemoteEnvelopeError as exc:
        return exc.category
    raise AssertionError("expected RemoteEnvelopeError")


class RemoteEnvelopeConstructionTests(unittest.TestCase):
    def test_happy_path_carries_all_contract_fields(self):
        env = envelope()
        self.assertTrue(env.message_id)
        self.assertTrue(env.correlation_id)
        self.assertTrue(env.sender)
        self.assertTrue(env.recipient)
        self.assertEqual(env.role, "coder")
        self.assertEqual(env.payload_type, RemotePayloadType.COLLABORATION_PACKET)
        self.assertEqual(env.protocol_version, PROTOCOL_VERSION)
        self.assertEqual(type(env.payload), CollaborationPacket)

    def test_payload_type_defaults_to_collaboration_packet(self):
        cid = new_correlation_id()
        env = RemoteEnvelope(
            message_id=new_message_id(), correlation_id=cid,
            sender="agent:a:architect", recipient="agent:b:coder",
            role="coder", payload=packet(cid))
        self.assertEqual(env.payload_type, RemotePayloadType.COLLABORATION_PACKET)

    def test_envelope_is_frozen(self):
        env = envelope()
        with self.assertRaises(Exception):
            env.sender = "agent:x:coder"

    def test_sender_and_recipient_must_differ(self):
        code = raised_code(lambda: envelope(sender="agent:same:coder",
                                            recipient="agent:same:coder"))
        self.assertEqual(code, RemoteEnvelopeErrorCode.INVALID_ENVELOPE)

    def test_role_must_match_payload_target_role(self):
        code = raised_code(lambda: envelope(role="tester"))
        self.assertEqual(code, RemoteEnvelopeErrorCode.INVALID_ENVELOPE)

    def test_correlation_must_match_payload_correlation(self):
        code = raised_code(lambda: envelope(
            correlation_id=new_correlation_id(),
            payload=packet(new_correlation_id())))
        self.assertEqual(code, RemoteEnvelopeErrorCode.INVALID_ENVELOPE)

    def test_payload_must_be_a_collaboration_packet(self):
        for foreign in (object(), "wire text", {"x": 1}, 42,
                        ("claude-cli", "anthropic", None, "fp"),
                        (lambda: None)):
            with self.subTest(payload=type(foreign).__name__):
                code = raised_code(lambda f=foreign: envelope(payload=f))
                self.assertEqual(code, RemoteEnvelopeErrorCode.INVALID_ENVELOPE)

    def test_header_fields_reject_secret_shaped_content(self):
        for field, bad in (("sender", "agent:api_key:architect"),
                           ("recipient", "agent:token-holder:coder"),
                           ("message_id", "remote-with-bearer-in-it"),
                           ("role", "authorization")):
            with self.subTest(field=field):
                code = raised_code(lambda f=field, b=bad: envelope(**{f: b}))
                self.assertEqual(code, RemoteEnvelopeErrorCode.INVALID_ENVELOPE)
        # correlation_id is shared with the payload packet, so a dirty value
        # rides on a clean payload to reach the envelope's own header scan.
        code = raised_code(lambda: envelope(
            correlation_id="collab-secret",
            payload=packet(new_correlation_id())))
        self.assertEqual(code, RemoteEnvelopeErrorCode.INVALID_ENVELOPE)


class RemoteEnvelopeIdentityTests(unittest.TestCase):
    def test_sender_and_recipient_are_agent_addresses_not_runtime_identities(self):
        runtime_a = ("claude-cli", "anthropic", None, "fp-a")
        runtime_b = ("codex-cli", "openai", None, "fp-b")
        agent = AgentIdentity("arch-bot")
        AgentRuntimeBinding(agent, runtime_a)
        address_before = agent_address(agent, "architect")
        AgentRuntimeBinding(agent, runtime_b)
        address_after = agent_address(agent, "architect")
        # rebinding never moves the address: runtime is not the addressing truth
        self.assertEqual(address_before, address_after)
        self.assertNotIn("claude-cli", address_before)
        self.assertNotIn("codex-cli", address_before)
        other = agent_address(AgentIdentity("coder-bot"), "coder")
        self.assertNotEqual(address_before, other)
        env = envelope(sender=address_before, recipient=other, role="coder")
        self.assertEqual(env.sender, address_before)
        self.assertEqual(env.recipient, other)


class RemoteEnvelopeIdSemanticsTests(unittest.TestCase):
    def test_message_id_and_correlation_id_are_distinct_fields(self):
        self.assertIn("message_id", RemoteEnvelope.REQUIRED_FIELDS)
        self.assertIn("correlation_id", RemoteEnvelope.REQUIRED_FIELDS)
        env = envelope()
        self.assertNotEqual(env.message_id, env.correlation_id)
        self.assertTrue(env.message_id.startswith("remote-"))
        self.assertTrue(env.correlation_id.startswith("collab-"))

    def test_one_interaction_shares_correlation_never_message_id(self):
        cid = new_correlation_id()
        first = envelope(correlation_id=cid)
        second = envelope(correlation_id=cid)
        self.assertEqual(first.correlation_id, second.correlation_id)
        self.assertNotEqual(first.message_id, second.message_id)

    def test_message_id_factory_is_fresh_each_call(self):
        self.assertNotEqual(new_message_id(), new_message_id())


class RemoteEnvelopeSerializationTests(unittest.TestCase):
    def test_to_dict_is_deterministic(self):
        env = envelope()
        self.assertEqual(env.to_dict(), env.to_dict())

    def test_serialize_is_deterministic_and_canonical(self):
        env = envelope()
        first = serialize_remote_envelope(env)
        self.assertEqual(first, serialize_remote_envelope(env))
        self.assertEqual(
            first,
            json.dumps(env.to_dict(), sort_keys=True, separators=(",", ":")))

    def test_to_dict_returns_fresh_dict(self):
        env = envelope()
        mutated = env.to_dict()
        mutated["sender"] = "agent:tampered:coder"
        self.assertNotEqual(env.to_dict()["sender"], "agent:tampered:coder")

    def test_json_round_trip_preserves_semantics(self):
        env = envelope()
        restored = deserialize_remote_envelope(serialize_remote_envelope(env))
        self.assertEqual(restored, env)

    def test_round_trip_of_interaction_pair_keeps_correlation(self):
        cid = new_correlation_id()
        pair = [envelope(correlation_id=cid), envelope(correlation_id=cid)]
        restored = [deserialize_remote_envelope(serialize_remote_envelope(env))
                    for env in pair]
        self.assertEqual(restored[0].correlation_id, restored[1].correlation_id)
        self.assertNotEqual(restored[0].message_id, restored[1].message_id)


class RemoteEnvelopeValidationTests(unittest.TestCase):
    def base_dict(self):
        return envelope().to_dict()

    def test_missing_required_field_is_rejected(self):
        for name in RemoteEnvelope.REQUIRED_FIELDS:
            with self.subTest(missing=name):
                data = self.base_dict()
                del data[name]
                code = raised_code(
                    lambda d=data: RemoteEnvelope.from_dict(d))
                self.assertEqual(code, RemoteEnvelopeErrorCode.INVALID_ENVELOPE)

    def test_wrong_field_type_is_rejected(self):
        cases = {
            "sender": 7, "recipient": None, "message_id": 1.5,
            "correlation_id": [], "role": True, "protocol_version": 2,
            "payload": {"nested": "object"},
            "payload_type": "SESSION_CONTROL",
        }
        for name, bad in cases.items():
            with self.subTest(field=name):
                data = self.base_dict()
                data[name] = bad
                code = raised_code(
                    lambda d=data: RemoteEnvelope.from_dict(d))
                self.assertEqual(code, RemoteEnvelopeErrorCode.INVALID_ENVELOPE)

    def test_malformed_json_is_rejected(self):
        for bad in ("{not json", "", "[]", "null", 123):
            with self.subTest(text=bad):
                code = raised_code(
                    lambda b=bad: deserialize_remote_envelope(b))
                self.assertEqual(code, RemoteEnvelopeErrorCode.INVALID_ENVELOPE)

    def test_unsupported_protocol_version_is_rejected(self):
        for version in ("2.0", "1", "1.0.0"):
            with self.subTest(version=version):
                data = self.base_dict()
                data["protocol_version"] = version
                code = raised_code(lambda d=data: RemoteEnvelope.from_dict(d))
                self.assertEqual(code,
                                 RemoteEnvelopeErrorCode.UNSUPPORTED_PROTOCOL)
                code = raised_code(
                    lambda v=version: envelope(protocol_version=v))
                self.assertEqual(code,
                                 RemoteEnvelopeErrorCode.UNSUPPORTED_PROTOCOL)

    def test_payload_wire_damage_is_rejected_as_invalid_envelope(self):
        data = self.base_dict()
        data["payload"] = data["payload"][:-4]
        code = raised_code(lambda d=data: RemoteEnvelope.from_dict(d))
        self.assertEqual(code, RemoteEnvelopeErrorCode.INVALID_ENVELOPE)


class RemoteContractDeliverySemanticsTests(unittest.TestCase):
    def test_delivery_vocabulary_is_exactly_the_contract_set(self):
        self.assertEqual(
            [status.value for status in RemoteEnvelopeStatus],
            ["ACCEPTED", "DELIVERED", "REJECTED", "FAILED", "TIMEOUT"])

    def test_accepted_is_not_delivered(self):
        self.assertNotEqual(RemoteEnvelopeStatus.ACCEPTED,
                            RemoteEnvelopeStatus.DELIVERED)

    def test_delivered_is_not_executed(self):
        # DELIVERED means the boundary reached the peer side; execution is an
        # agent-side outcome and must not exist in the delivery vocabulary.
        values = {status.value for status in RemoteEnvelopeStatus}
        self.assertNotIn("EXECUTED", values)

    def test_delivered_aligns_with_existing_transport_receipt_value(self):
        self.assertEqual(RemoteEnvelopeStatus.DELIVERED.value,
                         RemoteDeliveryStatus.DELIVERED.value)


class RemoteContractErrorSemanticsTests(unittest.TestCase):
    def test_error_vocabulary_is_exactly_the_contract_set(self):
        self.assertEqual(
            [code.value for code in RemoteEnvelopeErrorCode],
            ["INVALID_ENVELOPE", "UNSUPPORTED_PROTOCOL", "UNKNOWN_AGENT",
             "ROLE_UNAVAILABLE", "DELIVERY_FAILED", "DELIVERY_TIMEOUT",
             "REMOTE_EXECUTION_FAILED"])

    def test_errors_are_structured_and_judgeable(self):
        exc = RemoteEnvelopeError(RemoteEnvelopeErrorCode.UNKNOWN_AGENT)
        self.assertIs(exc.category, RemoteEnvelopeErrorCode.UNKNOWN_AGENT)
        self.assertIn("UNKNOWN_AGENT", str(exc))

    def test_unknown_category_is_refused(self):
        with self.assertRaises(ValueError):
            RemoteEnvelopeError("HTTP_503")

    def test_transport_specific_vocabulary_stays_outside_the_contract(self):
        values = [code.value for code in RemoteEnvelopeErrorCode]
        for word in ("http", "socket", "tls", "connection", "503", "grpc"):
            self.assertFalse(any(word in value.lower() for value in values),
                             word)


class RemoteContractBoundaryTests(unittest.TestCase):
    SOURCE = (SCRIPTS / "remote_contract.py").read_text(encoding="utf-8")

    def import_lines(self):
        for line in self.SOURCE.splitlines():
            stripped = line.strip()
            if stripped.startswith(("import ", "from ")):
                yield stripped

    def test_no_pickle_family_imports(self):
        for banned in ("pickle", "marshal", "cloudpickle"):
            self.assertFalse(
                any(banned in line for line in self.import_lines()),
                banned)

    def test_no_transport_or_third_party_imports(self):
        for banned in ("requests", "httpx", "aiohttp", "websocket",
                       "socket", "grpc", "urllib", "ssl", "asyncio"):
            self.assertFalse(
                any(banned in line for line in self.import_lines()),
                banned)

    def test_no_second_business_packet_protocol(self):
        self.assertNotIn("RemoteCollaborationPacket", self.SOURCE)

    def test_serialization_needs_no_runtime_or_adapter_object(self):
        # The whole chain is values-only: identity address + offline packet.
        env = envelope()
        wire = serialize_remote_envelope(env)
        restored = deserialize_remote_envelope(wire)
        self.assertEqual(restored, env)


if __name__ == "__main__":
    unittest.main()
