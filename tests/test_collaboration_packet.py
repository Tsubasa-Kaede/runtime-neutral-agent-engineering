"""Phase 10H-A: CollaborationPacket — agent-to-agent collaboration envelope.

Pure contract tests: the envelope wraps the four business packets without
reimplementing them, stays runtime-neutral, secret-free, immutable, never
upgrades provenance, and performs no invocation of any runtime.
"""
import json
import sys
import unittest
from dataclasses import FrozenInstanceError, fields
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from collaboration_packet import (
    PROTOCOL_VERSION,
    CollaborationPacket,
    CollaborationPayloadType,
    deserialize_collaboration_packet,
    new_correlation_id,
    serialize_collaboration_packet,
)
from structured_packets import (
    ArchitecturePacket,
    ImplementationPacket,
    PacketValidationError,
    ReviewPacket,
    TestPacket,
    serialize_packet,
)
from verified_selection_bridge import agent_id_for

SOURCE_AGENT = agent_id_for(("rt-a", "provider-a", "model-a", "fp-a"))
TARGET_AGENT = agent_id_for(("rt-b", "provider-b", "model-b", "fp-b"))

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


def wire_dict():
    """A valid business-packet dict (content legal, type wrong for the envelope)."""
    return {
        "task_id": "T1", "role": "architect", "goal": ["g"], "constraints": ["c"],
        "architecture": ["a"], "interfaces": [{}], "implementation_steps": [{}],
        "acceptance_criteria": ["ac1"], "risks": [{}],
    }


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


class ContractTests(unittest.TestCase):
    def test_defaults_are_protocol_version_offline_and_empty_criteria(self):
        packet = envelope()
        self.assertEqual(packet.protocol_version, "1.0")
        self.assertEqual(packet.protocol_version, PROTOCOL_VERSION)
        self.assertEqual(packet.provenance, "OFFLINE")
        self.assertEqual(packet.acceptance_criteria, ())

    def test_field_set_is_exactly_the_contract(self):
        names = {field.name for field in fields(CollaborationPacket)}
        self.assertEqual(names, {
            "correlation_id", "task_id", "source_agent", "target_agent",
            "source_role", "target_role", "payload_type", "payload",
            "acceptance_criteria", "protocol_version", "provenance",
        })

    def test_payload_type_members_and_wire_values(self):
        self.assertEqual(
            {member.name for member in CollaborationPayloadType},
            {"ARCHITECTURE", "IMPLEMENTATION", "TEST", "REVIEW"},
        )
        for member in CollaborationPayloadType:
            self.assertEqual(member.value, member.name)


class PayloadContractTests(unittest.TestCase):
    def test_four_payload_types_map_to_matching_packets(self):
        pairs = (
            (CollaborationPayloadType.ARCHITECTURE, arch()),
            (CollaborationPayloadType.IMPLEMENTATION, impl()),
            (CollaborationPayloadType.TEST, test_pkt()),
            (CollaborationPayloadType.REVIEW, review_pkt()),
        )
        for payload_type, payload in pairs:
            with self.subTest(payload_type=payload_type.name):
                packet = envelope(payload_type=payload_type, payload=payload)
                self.assertIs(packet.payload, payload)

    def test_payload_class_mismatch_is_rejected_for_all_combinations(self):
        packets = (arch(), impl(), test_pkt(), review_pkt())
        types_ = tuple(CollaborationPayloadType)
        for payload_type in types_:
            for packet in packets:
                expected = {
                    CollaborationPayloadType.ARCHITECTURE: ArchitecturePacket,
                    CollaborationPayloadType.IMPLEMENTATION: ImplementationPacket,
                    CollaborationPayloadType.TEST: TestPacket,
                    CollaborationPayloadType.REVIEW: ReviewPacket,
                }[payload_type]
                if type(packet) is expected:
                    continue
                with self.subTest(payload_type=payload_type.name, cls=type(packet).__name__):
                    with self.assertRaises(PacketValidationError):
                        envelope(payload_type=payload_type, payload=packet)

    def test_non_packet_payloads_are_rejected(self):
        for bad in (object(), b"x", lambda: None, wire_dict()):
            with self.subTest(bad=type(bad).__name__):
                with self.assertRaises(PacketValidationError):
                    envelope(payload=bad)

    def test_constructor_rejects_plain_string_payload_type(self):
        with self.assertRaises(PacketValidationError):
            envelope(payload_type="ARCHITECTURE")

    def test_payload_task_id_must_match_envelope(self):
        with self.assertRaises(PacketValidationError):
            envelope(task_id="T2")
        with self.assertRaises(PacketValidationError):
            envelope(payload=arch(task_id="T2"))


class IdentityTests(unittest.TestCase):
    def test_agent_identity_is_opaque_and_not_runtime_id(self):
        packet = envelope()
        self.assertIsInstance(packet.source_agent, str)
        self.assertNotEqual(packet.source_agent, "rt-a")
        self.assertNotEqual(packet.target_agent, "rt-b")

    def test_same_agent_for_source_and_target_is_rejected(self):
        with self.assertRaises(PacketValidationError):
            envelope(target_agent=SOURCE_AGENT)

    def test_same_role_different_agents_is_allowed(self):
        packet = envelope(source_role="coder", target_role="coder")
        self.assertEqual(packet.source_role, packet.target_role)

    def test_blank_or_non_string_identity_fields_are_rejected(self):
        for field_name in ("correlation_id", "task_id", "source_agent",
                           "target_agent", "source_role", "target_role"):
            for bad in ("", "   ", None, 7):
                with self.subTest(field=field_name, bad=repr(bad)):
                    with self.assertRaises(PacketValidationError):
                        envelope(**{field_name: bad})


class ImmutabilityTests(unittest.TestCase):
    def test_field_assignment_raises(self):
        packet = envelope()
        with self.assertRaises(FrozenInstanceError):
            packet.correlation_id = "C999"
        with self.assertRaises(FrozenInstanceError):
            packet.payload.goal = ()

    def test_missing_required_argument_raises_type_error(self):
        with self.assertRaises(TypeError):
            CollaborationPacket(correlation_id="C001", task_id="T1")


class ProvenanceTests(unittest.TestCase):
    def test_real_is_expressible_by_caller_only(self):
        packet = envelope(provenance="REAL")
        self.assertEqual(packet.provenance, "REAL")

    def test_invalid_provenance_is_rejected(self):
        for bad in ("real", "offline", "", None, "MAYBE"):
            with self.subTest(bad=repr(bad)):
                with self.assertRaises(PacketValidationError):
                    envelope(provenance=bad)

    def test_module_never_upgrades_or_reads_the_real_gate(self):
        import collaboration_packet as module
        source = Path(module.__file__).read_text(encoding="utf-8")
        self.assertNotIn('provenance="REAL"', source)
        self.assertNotIn("RUN_REAL_PROVIDER_TESTS", source)
        self.assertNotIn("os.environ", source)
        self.assertNotIn("getenv", source)


class SecretSafetyTests(unittest.TestCase):
    def test_secret_shaped_fields_are_rejected(self):
        for field_name in ("correlation_id", "task_id", "source_agent",
                           "target_agent", "source_role", "target_role"):
            for bad in ("api_key=1", "Token=x", "bearer cred", "stdout dump"):
                with self.subTest(field=field_name, bad=bad):
                    with self.assertRaises(PacketValidationError):
                        envelope(**{field_name: bad})

    def test_secret_shaped_acceptance_criteria_is_rejected(self):
        with self.assertRaises(PacketValidationError):
            envelope(acceptance_criteria=("api_key=1",))

    def test_repr_and_exception_messages_stay_clean(self):
        packet = envelope()
        surface = (repr(packet) + str(packet)).lower()
        for marker in SECRET_MARKERS:
            self.assertNotIn(marker, surface)
        with self.assertRaises(PacketValidationError) as caught:
            envelope(task_id="token=abc")
        message = str(caught.exception).lower()
        for marker in SECRET_MARKERS:
            self.assertNotIn(marker, message)
        self.assertNotIn("token=abc", message)


class RuntimeNeutralTests(unittest.TestCase):
    def test_source_has_no_runtime_names(self):
        import collaboration_packet as module
        text = Path(module.__file__).read_text(encoding="utf-8").lower()
        for name in ("claude", "codex", "deepseek", "openai", "anthropic",
                     "gemini", "tiny-agents", "tiny_agents"):
            self.assertNotIn(name, text)

    def test_source_has_no_forbidden_imports_or_channels(self):
        import collaboration_packet as module
        source = Path(module.__file__).read_text(encoding="utf-8")
        for forbidden in ("external_runtime", "orchestrator", "execution_engine",
                          "runtime_pool", "task_budget", "loop_guard",
                          "invocation_plan", "handoff_context", "subprocess",
                          "invoke", "requests", "urllib", "socket"):
            self.assertNotIn(forbidden, source)


class DeterminismTests(unittest.TestCase):
    def test_equal_inputs_produce_equal_packets(self):
        self.assertEqual(envelope(), envelope())
        # Note: hashing is not part of the contract — business packets carry
        # dict-valued fields (e.g. interfaces), same as the house packets.

    def test_different_correlation_id_is_unequal(self):
        self.assertNotEqual(envelope(), envelope(correlation_id="C002"))


class SerializationTests(unittest.TestCase):
    def test_roundtrip_all_four_payload_types(self):
        pairs = (
            (CollaborationPayloadType.ARCHITECTURE, arch()),
            (CollaborationPayloadType.IMPLEMENTATION, impl()),
            (CollaborationPayloadType.TEST, test_pkt()),
            (CollaborationPayloadType.REVIEW, review_pkt()),
        )
        classes = (ArchitecturePacket, ImplementationPacket, TestPacket, ReviewPacket)
        for (payload_type, payload), cls in zip(pairs, classes):
            with self.subTest(payload_type=payload_type.name):
                original = envelope(payload_type=payload_type, payload=payload)
                decoded = deserialize_collaboration_packet(
                    serialize_collaboration_packet(original))
                self.assertEqual(original, decoded)
                self.assertIsInstance(decoded.payload, cls)
                self.assertIsInstance(decoded.payload_type, CollaborationPayloadType)
                self.assertIn("CollaborationPayloadType." + payload_type.name, repr(decoded))

    def test_serialized_payload_reuses_the_business_wire_format(self):
        original = envelope()
        encoded = serialize_collaboration_packet(original)
        wire = json.loads(encoded)
        self.assertIsInstance(wire["payload"], str)
        self.assertEqual(wire["payload"], serialize_packet(original.payload))
        canonical = json.dumps(wire, sort_keys=True, separators=(",", ":"))
        self.assertEqual(encoded, canonical)

    def test_non_default_fields_survive_roundtrip(self):
        original = envelope(provenance="REAL", acceptance_criteria=("c1", "c2"))
        decoded = deserialize_collaboration_packet(
            serialize_collaboration_packet(original))
        self.assertEqual(decoded, original)
        self.assertEqual(decoded.provenance, "REAL")
        self.assertEqual(decoded.acceptance_criteria, ("c1", "c2"))

    def test_serialize_rejects_non_collaboration_objects(self):
        for bad in (arch(), None, {}):
            with self.subTest(bad=type(bad).__name__):
                with self.assertRaises(PacketValidationError):
                    serialize_collaboration_packet(bad)

    def test_deserialize_rejects_invalid_json(self):
        with self.assertRaises(PacketValidationError):
            deserialize_collaboration_packet("not json")

    def test_deserialize_rejects_non_object_json(self):
        with self.assertRaises(PacketValidationError):
            deserialize_collaboration_packet(json.dumps([1, 2]))

    def test_deserialize_rejects_missing_required_fields(self):
        for field_name in CollaborationPacket.REQUIRED_FIELDS:
            with self.subTest(field=field_name):
                wire = json.loads(serialize_collaboration_packet(envelope()))
                wire.pop(field_name)
                with self.assertRaises(PacketValidationError):
                    deserialize_collaboration_packet(json.dumps(wire))

    def test_deserialize_rejects_payload_class_mismatch(self):
        wire = json.loads(serialize_collaboration_packet(envelope()))
        wire["payload"] = serialize_packet(impl())  # same task_id, wrong class
        with self.assertRaises(PacketValidationError):
            deserialize_collaboration_packet(json.dumps(wire))

    def test_deserialize_rejects_unknown_payload_type(self):
        wire = json.loads(serialize_collaboration_packet(envelope()))
        wire["payload_type"] = "BOGUS"
        with self.assertRaises(PacketValidationError):
            deserialize_collaboration_packet(json.dumps(wire))

    def test_deserialize_revalidates_wire_only_secrets(self):
        wire = json.loads(serialize_collaboration_packet(envelope()))
        wire["task_id"] = "token=abc"
        with self.assertRaises(PacketValidationError):
            deserialize_collaboration_packet(json.dumps(wire))

    def test_from_dict_fills_defaults_when_optional_keys_omitted(self):
        wire = json.loads(serialize_collaboration_packet(envelope()))
        for key in ("acceptance_criteria", "protocol_version", "provenance"):
            wire.pop(key)
        decoded = deserialize_collaboration_packet(json.dumps(wire))
        self.assertEqual(decoded, envelope())

    def test_from_dict_coerces_criteria_list_to_tuple(self):
        wire = json.loads(serialize_collaboration_packet(envelope()))
        wire["acceptance_criteria"] = ["a", "b"]
        decoded = deserialize_collaboration_packet(json.dumps(wire))
        self.assertEqual(decoded.acceptance_criteria, ("a", "b"))

    def test_constructor_rejects_non_tuple_criteria(self):
        for bad in (["a"], "a", ("a", 1)):
            with self.subTest(bad=repr(bad)):
                with self.assertRaises(PacketValidationError):
                    envelope(acceptance_criteria=bad)


class ProtocolVersionTests(unittest.TestCase):
    def test_non_constant_versions_are_rejected(self):
        for bad in ("0.9", "2.0", "", None):
            with self.subTest(bad=repr(bad)):
                with self.assertRaises(PacketValidationError):
                    envelope(protocol_version=bad)


class CorrelationIdTests(unittest.TestCase):
    def test_new_correlation_id_shape_and_uniqueness(self):
        first = new_correlation_id()
        second = new_correlation_id()
        self.assertTrue(first.startswith("collab-"))
        self.assertEqual(len(first), len("collab-") + 32)
        self.assertNotEqual(first, second)
        packet = envelope(correlation_id=first)
        self.assertEqual(packet.correlation_id, first)

    def test_any_non_empty_secret_free_id_is_accepted(self):
        self.assertEqual(envelope(correlation_id="x").correlation_id, "x")


if __name__ == "__main__":
    unittest.main()
