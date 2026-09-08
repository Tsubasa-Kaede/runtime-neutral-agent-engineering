import json
import sys
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from content_safety import (
    last_validation_diagnostic,
    reset_validation_diagnostic,
)
from structured_packets import (
    ArchitecturePacket,
    ImplementationPacket,
    PacketValidationError,
    ReviewPacket,
    TestPacket,
    deserialize_packet,
    serialize_packet,
)


class StructuredPacketTests(unittest.TestCase):
    def architecture(self):
        return ArchitecturePacket(
            task_id="task-1",
            role="architect",
            goal=("Add health checks",),
            constraints=("No secrets",),
            architecture=("Use a status pipeline",),
            interfaces=({"name": "Health", "input": "Runtime"},),
            implementation_steps=({"id": "step-1", "description": "Add contracts"},),
            acceptance_criteria=("Offline tests pass",),
            risks=({"description": "Unknown provider", "mitigation": "Return unavailable"},),
        )

    def test_architecture_packet_has_required_fields_and_is_immutable(self):
        packet = self.architecture()
        self.assertEqual(packet.role, "architect")
        self.assertEqual(packet.task_id, "task-1")
        with self.assertRaises(FrozenInstanceError):
            packet.goal = ()

    def test_architecture_packet_rejects_missing_required_field(self):
        with self.assertRaises(PacketValidationError):
            ArchitecturePacket.from_dict({"task_id": "task-1", "role": "architect"})

    def test_all_packet_types_require_task_id_and_role(self):
        for packet_type in (ImplementationPacket, ReviewPacket, TestPacket):
            with self.subTest(packet_type=packet_type):
                with self.assertRaises(PacketValidationError):
                    packet_type.from_dict({})

    def test_implementation_packet_fields(self):
        packet = ImplementationPacket(
            task_id="task-1", role="coder", changed_files=("a.py",),
            implementation_summary="Added contracts", implementation_details=("Defined types",),
            assumptions=("Python 3",), unresolved_items=(), test_requirements=("Run unittest",),
        )
        self.assertEqual(packet.changed_files, ("a.py",))

    def test_review_packet_fields(self):
        packet = ReviewPacket(
            task_id="task-1", role="reviewer", status="PASS", findings=(),
            severity=(), affected_files=(), required_changes=(),
            acceptance_criteria_status=("passed",),
        )
        self.assertEqual(packet.status, "PASS")

    def test_test_packet_fields(self):
        packet = TestPacket(
            task_id="task-1", role="tester", tests_run=("unittest",),
            tests_passed=("test_a",), tests_failed=(), failures=(),
            coverage_or_validation=("compileall",), remaining_risks=(),
        )
        self.assertEqual(packet.tests_failed, ())

    def test_packets_round_trip_through_json(self):
        original = self.architecture()
        encoded = serialize_packet(original)
        decoded = deserialize_packet(encoded)
        self.assertEqual(decoded, original)
        self.assertEqual(json.loads(encoded)["packet_type"], "ArchitecturePacket")

    def test_unknown_packet_type_fails_explicitly(self):
        with self.assertRaises(PacketValidationError):
            deserialize_packet('{"packet_type":"UnknownPacket","task_id":"x"}')

    def test_secret_shaped_values_are_rejected(self):
        with self.assertRaises(PacketValidationError):
            ArchitecturePacket(
                task_id="task-1", role="architect", goal=("api_key=secret",),
                constraints=(), architecture=(), interfaces=(),
                implementation_steps=(), acceptance_criteria=(), risks=(),
            )

    def test_handoff_roles_are_explicit(self):
        self.assertEqual(self.architecture().role, "architect")
        self.assertEqual(ImplementationPacket.required_role(), "coder")
        self.assertEqual(TestPacket.required_role(), "tester")
        self.assertEqual(ReviewPacket.required_role(), "reviewer")


class RejectDiagnosticsTests(unittest.TestCase):
    """CU-R2（Packet Rejection Diagnostics）：from_dict 的既有拒绝点
    记录值安全的结构坐标诊断（rule/field/layer，绝不含被拒值）——
    拒绝语义零变化，只新增可观测性。诊断槽是全局 last-REJECT，
    每个用例先清零。"""

    def setUp(self):
        reset_validation_diagnostic()

    def _arch_dict(self, **overrides):
        data = {
            "task_id": "task-1", "role": "architect",
            "goal": ["g"], "constraints": ["c"], "architecture": ["a"],
            "interfaces": [], "implementation_steps": [],
            "acceptance_criteria": ["ac"], "risks": [],
        }
        data.update(overrides)
        return data

    def _coder_dict(self, **overrides):
        data = {
            "task_id": "task-1", "role": "coder",
            "changed_files": ["parser.py"],
            "implementation_summary": "s",
            "implementation_details": [], "assumptions": [],
            "unresolved_items": [], "test_requirements": [],
        }
        data.update(overrides)
        return data

    def test_missing_fields_diagnostic_names_fields(self):
        with self.assertRaises(PacketValidationError):
            ArchitecturePacket.from_dict(
                {"task_id": "task-1", "role": "architect"})
        diagnostic = last_validation_diagnostic()
        self.assertIsNotNone(diagnostic)
        self.assertEqual(diagnostic.rule, "MISSING_FIELDS")
        self.assertEqual(diagnostic.layer, "packet")
        self.assertIn("goal", diagnostic.field)
        self.assertIn("risks", diagnostic.field)

    def test_not_a_list_diagnostic_names_field(self):
        with self.assertRaises(PacketValidationError):
            ArchitecturePacket.from_dict(self._arch_dict(goal="single goal"))
        diagnostic = last_validation_diagnostic()
        self.assertEqual(diagnostic.rule, "NOT_A_LIST")
        self.assertEqual(diagnostic.field, "goal")
        self.assertEqual(diagnostic.layer, "packet")

    def test_identity_invalid_diagnostic_names_role(self):
        with self.assertRaises(PacketValidationError):
            ArchitecturePacket.from_dict(self._arch_dict(role="Architect"))
        diagnostic = last_validation_diagnostic()
        self.assertEqual(diagnostic.rule, "IDENTITY_INVALID")
        self.assertEqual(diagnostic.field, "role")

    def test_identity_invalid_blank_task_id(self):
        with self.assertRaises(PacketValidationError):
            ArchitecturePacket.from_dict(self._arch_dict(task_id="   "))
        self.assertEqual(last_validation_diagnostic().rule, "IDENTITY_INVALID")
        self.assertEqual(last_validation_diagnostic().field, "task_id")

    def test_coder_identity_invalid_diagnostic(self):
        with self.assertRaises(PacketValidationError):
            ImplementationPacket.from_dict(self._coder_dict(role="coderr"))
        self.assertEqual(last_validation_diagnostic().rule, "IDENTITY_INVALID")
        self.assertEqual(last_validation_diagnostic().field, "role")

    def test_valid_packet_records_no_diagnostic(self):
        packet = ArchitecturePacket.from_dict(self._arch_dict())
        self.assertEqual(packet.role, "architect")
        self.assertIsNone(last_validation_diagnostic())

    def test_unsafe_shape_still_records(self):
        with self.assertRaises(PacketValidationError):
            ArchitecturePacket.from_dict(
                self._arch_dict(goal=["api_key=deadbeef"]))
        self.assertEqual(last_validation_diagnostic().rule, "UNSAFE_SHAPE")

    def test_diagnostics_never_carry_rejected_values(self):
        with self.assertRaises(PacketValidationError):
            ArchitecturePacket.from_dict(
                self._arch_dict(goal=["api_key=deadbeef"], risks="oops"))
        joined = str(last_validation_diagnostic())
        self.assertNotIn("deadbeef", joined)
        self.assertNotIn("oops", joined)


if __name__ == "__main__":
    unittest.main()
