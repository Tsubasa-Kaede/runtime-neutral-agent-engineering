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
from content_safety import (
    diagnose_packet_reject,
    last_validation_diagnostic as last_packet_diagnostic,
    packet_has_unsafe_content,
    reset_validation_diagnostic,
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

    def test_bare_marker_mention_in_criteria_is_accepted(self):
        # R6-C10 forensics (the G15 semantic split, applied to the one
        # remaining old-scan site): acceptance_criteria are MODEL PROSE
        # copied from the architect packet, whose own layer already
        # rejects credential SHAPES and whose whole-packet scan already
        # ran. The envelope's historical bare-substring scan rejected
        # legitimate technical prose ("no token input",
        # "the parser tokenizes input once") — the exact false-positive
        # class G15 removed everywhere else, and a live REAl-scenario
        # risk (a legal architect packet dies at envelope construction
        # and surfaces as ARCHITECT_PACKET_INVALID with a valid-looking
        # diagnosis). Criteria items must be SHAPE-scanned, not
        # word-banned.
        for legal in ("no clock, no random, no token input",
                      "the parser tokenizes input once",
                      "must not write to stdout during tests",
                      "logs contain no authorization header"):
            with self.subTest(legal=legal):
                packet = envelope(acceptance_criteria=(legal,))
                self.assertEqual(packet.acceptance_criteria, (legal,))

    def test_credential_shaped_criteria_are_still_rejected(self):
        # The fix must not weaken true positives: every credential
        # SHAPE stays fatal at the envelope, same vocabulary as the
        # packet layer (assignment / bearer / sk- material).
        for bad in ("api_key=1", "token: deadbeef", "bearer abc123",
                    "authorization: Bearer x", "sk-abcdefgh1234"):
            with self.subTest(bad=bad):
                with self.assertRaises(PacketValidationError):
                    envelope(acceptance_criteria=(bad,))


class PacketValidationDiagnosticTests(unittest.TestCase):
    """R6-C11: secret-safe, field-level, rule-level failure diagnostics.

    Contract (fixed fixtures, independently expected — never derived
    from production code): every validation REJECT carries a structured
    diagnostic naming LAYER / FIELD / INDEX (when the value lives in a
    list) / RULE — and NOTHING ELSE. The rejected raw value, the
    prompt, any credential material: never present, never substrings.
    The acceptance semantics are unchanged; only observability grew.

    E1-E5 (the R6-C10 failure-space enumeration):
      E1 packet _clean: credential shape in a string VALUE
      E2 packet _clean: credential shape in a dict KEY
      E3 whole-packet scan: credential shape in a value
      E4 whole-packet scan: marker substring in a dict key
      E5 envelope criteria scan: credential shape in an item
    """

    def setUp(self):
        # The diagnostic slot is one global "last REJECT" observation;
        # every test starts from a clean observation.
        reset_validation_diagnostic()

    def test_safe_criteria_has_no_diagnostic(self):
        # Test 1 (authorization §七): a safe criteria item ACCEPTS and
        # produces NO diagnostic (nothing to observe).
        packet = envelope(acceptance_criteria=("no token input is used",))
        self.assertEqual(packet.acceptance_criteria, ("no token input is used",))
        self.assertIsNone(last_packet_diagnostic())

    def test_shaped_criteria_diagnostic_names_field_index_rule(self):
        # Test 2: a credential-shaped criteria item REJECTS with the
        # full structured diagnostic — layer, field, index, rule.
        with self.assertRaises(PacketValidationError):
            envelope(acceptance_criteria=(
                "deterministic under varied PYTHONHASHSEED",
                "api_key=deadbeef"))
        diagnostic = last_packet_diagnostic()
        self.assertIsNotNone(diagnostic)
        self.assertEqual(diagnostic.layer, "envelope")
        self.assertEqual(diagnostic.field, "acceptance_criteria")
        self.assertEqual(diagnostic.index, 1)
        self.assertEqual(diagnostic.rule, "UNSAFE_SHAPE")

    def test_unsafe_key_rejects_with_diagnostic(self):
        # Test 3: a marker substring in a dict KEY (E2/E4 family). The
        # packet layer itself allows marker keys; the whole-packet scan
        # is the rejecting authority — its REJECT must be observable
        # with field + rule (the walker names the key path too).
        packet = ArchitecturePacket(
            task_id="T1", role="architect", goal=("g",),
            constraints=("c",), architecture=("a",),
            interfaces=({"stdout": "raw"},),
            implementation_steps=({},),
            acceptance_criteria=("ac1",), risks=({},))
        self.assertTrue(packet_has_unsafe_content(packet))
        diagnostic = diagnose_packet_reject(packet, "packet")
        self.assertIsNotNone(diagnostic)
        self.assertEqual(diagnostic.layer, "packet")
        self.assertEqual(diagnostic.field, "interfaces.stdout")
        self.assertEqual(diagnostic.rule, "UNSAFE_KEY")

    def test_unsafe_value_rejects_with_diagnostic(self):
        # Test 4: a credential shape in a string VALUE (E1/E3 family)
        # is still REJECTED, with the value's field/index/rule named.
        with self.assertRaises(PacketValidationError):
            ArchitecturePacket(
                task_id="T1", role="architect", goal=("g",),
                constraints=("token=leak",),
                architecture=("a",), interfaces=({},),
                implementation_steps=({},),
                acceptance_criteria=("ac1",), risks=({},))
        diagnostic = last_packet_diagnostic()
        self.assertIsNotNone(diagnostic)
        self.assertEqual(diagnostic.layer, "packet")
        self.assertEqual(diagnostic.field, "constraints[0]")
        self.assertEqual(diagnostic.rule, "UNSAFE_SHAPE")

    def test_diagnostic_carries_no_secret_material(self):
        # Test 5: the diagnostic surface (all fields + str/repr) must
        # not contain the rejected raw value or credential material.
        secret = "sk-supersecretmaterial99"
        with self.assertRaises(PacketValidationError):
            envelope(acceptance_criteria=(secret,))
        diagnostic = last_packet_diagnostic()
        surface = (str(diagnostic) + repr(diagnostic)).lower()
        self.assertNotIn(secret, surface)
        self.assertNotIn("deadbeef", surface)
        for fragment in ("supersecret", "sk-", "material"):
            self.assertNotIn(fragment, surface, surface)

    def test_e1_to_e5_diagnostics_do_not_collapse(self):
        # Test 6: the five exit families must remain DISTINGUISHABLE —
        # not one generic "invalid" code. Independently expected pairs.
        expected = {
            "E1_packet_value_shape": ("packet", "UNSAFE_SHAPE"),
            "E2_packet_key_shape": ("packet", "UNSAFE_KEY"),
            "E3_envelope_value_shape": ("envelope", "UNSAFE_SHAPE"),
            "E4_whole_packet_key": ("packet", "UNSAFE_KEY"),
            "E5_envelope_criteria_shape": ("envelope", "UNSAFE_SHAPE"),
        }
        observed = {}
        # E1: value shape at the packet layer (constraints list item).
        with self.assertRaises(PacketValidationError):
            ArchitecturePacket(
                task_id="T1", role="architect", goal=("g",),
                constraints=("password=hunter2",),
                architecture=("a",), interfaces=({},),
                implementation_steps=({},),
                acceptance_criteria=("ac1",), risks=({},))
        d = last_packet_diagnostic()
        observed["E1_packet_value_shape"] = (d.layer, d.rule)
        # E2: KEY carrying a credential SHAPE (api_key=x as a key).
        with self.assertRaises(PacketValidationError):
            ArchitecturePacket(
                task_id="T1", role="architect", goal=("g",),
                constraints=("c",), architecture=("a",),
                interfaces=({"api_key=x": "v"},),
                implementation_steps=({},),
                acceptance_criteria=("ac1",), risks=({},))
        d = last_packet_diagnostic()
        observed["E2_packet_key_shape"] = (d.layer, d.rule)
        # E3: value shape surfacing at the ENVELOPE criteria layer.
        with self.assertRaises(PacketValidationError):
            envelope(acceptance_criteria=("token: deadbeef",))
        d = last_packet_diagnostic()
        observed["E3_envelope_value_shape"] = (d.layer, d.rule)
        # E4: whole-packet key marker (stderr) in risks dicts — the
        # packet layer itself allows marker keys; the whole-packet scan
        # is the rejecting authority, diagnosed via the walker.
        packet = ArchitecturePacket(
            task_id="T1", role="architect", goal=("g",),
            constraints=("c",), architecture=("a",),
            interfaces=({},), implementation_steps=({},),
            acceptance_criteria=("ac1",),
            risks=({"stderr": "dump"},))
        self.assertTrue(packet_has_unsafe_content(packet))
        d = diagnose_packet_reject(packet, "packet")
        self.assertIsNotNone(d)
        observed["E4_whole_packet_key"] = (d.layer, d.rule)
        # E5: envelope criteria shape (already covered; distinct fixture).
        with self.assertRaises(PacketValidationError):
            envelope(acceptance_criteria=("bearer abc123",))
        d = last_packet_diagnostic()
        observed["E5_envelope_criteria_shape"] = (d.layer, d.rule)
        # The layer/rule pairs must match the independently fixed
        # fixtures, and the five observed codes must not all be equal.
        self.assertEqual(observed, expected)
        codes = {(layer, rule) for layer, rule in observed.values()}
        self.assertGreater(len(codes), 1)

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
