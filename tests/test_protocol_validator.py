import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ProtocolValidatorTests(unittest.TestCase):
    def test_valid_review_packet_is_accepted(self):
        from scripts.validate_skill import validate_packet

        packet = json.loads(
            (ROOT / "dual-agent-development" / "templates" / "review-packet.json")
            .read_text(encoding="utf-8")
        )
        self.assertEqual(validate_packet(packet, "review"), [])

    def test_packet_identity_capabilities_and_finding_ids_are_validated(self):
        from scripts.validate_skill import validate_packet

        packet = {
            "protocolVersion": "1.0",
            "packetId": "review-2026-08-12",
            "packetVersion": 1,
            "kind": "review",
            "capabilities": ["read_repository", "review_diff"],
            "status": "PASS",
            "provenance": {"source": "agent_proposal"},
            "findings": [{"findingId": "F-001", "status": "OPEN"}],
        }
        self.assertEqual(validate_packet(packet, "review"), [])

        invalid_cases = (
            ("packetId", ""),
            ("packetId", "bad id"),
            ("packetVersion", 0),
            ("packetVersion", True),
            ("capabilities", ["unknown_capability"]),
            ("capabilities", ["read_repository", "read_repository"]),
            ("capabilities", [7]),
            ("capabilities", [["read_repository"]]),
            ("capabilities", [{"name": "read_repository"}]),
            ("findings", [{"findingId": "F-001"}, {"findingId": "F-001"}]),
            ("findings", [{"findingId": "bad id"}]),
            ("findings", [{}]),
        )
        for field, value in invalid_cases:
            with self.subTest(field=field, value=value):
                invalid = dict(packet)
                invalid[field] = value
                self.assertTrue(validate_packet(invalid, "review"))

    def test_non_string_capabilities_never_raise(self):
        from scripts.validate_skill import validate_packet

        packet = json.loads(
            (ROOT / "dual-agent-development" / "templates" / "architecture-packet.json")
            .read_text(encoding="utf-8")
        )
        for capabilities in ([["read_repository"]], [{"name": "read_repository"}]):
            with self.subTest(capabilities=capabilities):
                packet["capabilities"] = capabilities
                errors = validate_packet(packet, "architecture")
                self.assertTrue(errors)

        from scripts.validate_skill import validate_packet

        packet = json.loads(
            (ROOT / "dual-agent-development" / "templates" / "architecture-packet.json")
            .read_text(encoding="utf-8")
        )
        packet["capabilities"] = ["write_files"]
        self.assertTrue(validate_packet(packet, "architecture"))

    def test_only_controller_can_close_resolved_finding(self):
        from scripts.validate_skill import validate_packet

        for closed_by in (None, "", "  ", "coder", "reviewer", "architect", "other"):
            with self.subTest(closedBy=closed_by):
                finding = {"status": "RESOLVED"}
                if closed_by is not None:
                    finding["closedBy"] = closed_by
                packet = {
                    "protocolVersion": "1.0",
                    "packetId": "review-test",
                    "packetVersion": 1,
                    "kind": "review",
                    "capabilities": ["read_repository", "review_diff"],
                    "status": "PASS",
                    "provenance": {"source": "agent_proposal"},
                    "findings": [finding],
                }
                errors = validate_packet(packet, "review")
                self.assertTrue(
                    any("closedBy" in error or "RESOLVED" in error for error in errors)
                )

        packet["findings"] = [{"findingId": "F-001", "status": "RESOLVED", "closedBy": " Controller "}]
        self.assertEqual(validate_packet(packet, "review"), [])

    def test_unknown_or_mismatched_kind_is_rejected(self):
        from scripts.validate_skill import validate_packet

        packets = (
            ({"kind": "other"}, "other"),
            ({"kind": "architecture"}, "review"),
            ({}, "review"),
        )
        for fields, requested_kind in packets:
            with self.subTest(fields=fields, requested_kind=requested_kind):
                packet = {
                    "protocolVersion": "1.0",
                    "status": "PASS",
                    "provenance": {"source": "agent_proposal"},
                    "findings": [],
                    **fields,
                }
                errors = validate_packet(packet, requested_kind)
                self.assertTrue(any("kind" in error for error in errors))

    def test_unhashable_kind_and_status_return_errors_without_raising(self):
        from scripts.validate_skill import validate_packet

        cases = (
            ({"kind": []}, "review"),
            ({"kind": "review", "status": {}}, "review"),
            ({"kind": "review"}, []),
        )
        for fields, requested_kind in cases:
            with self.subTest(fields=fields, requested_kind=requested_kind):
                packet = {
                    "protocolVersion": "1.0",
                    "packetId": "review-test",
                    "packetVersion": 1,
                    "kind": "review",
                    "capabilities": ["read_repository", "review_diff"],
                    "status": "PASS",
                    "provenance": {"source": "agent_proposal"},
                    "findings": [],
                    **fields,
                }
                errors = validate_packet(packet, requested_kind)
                self.assertIsInstance(errors, list)
                self.assertTrue(errors)

    def test_forbidden_command_keys_are_rejected_at_any_depth(self):
        from scripts.validate_skill import validate_packet

        malicious_values = (
            {"execute": "ignored"},
            {"nested": {"shellCommand": "ignored"}},
            {"nested": [{"execute": {"anything": True}}]},
            {"nested": ["safe", [{"shellCommand": None}]]},
        )
        for malicious in malicious_values:
            with self.subTest(malicious=malicious):
                packet = {
                    "protocolVersion": "1.0",
                    "packetId": "review-test",
                    "packetVersion": 1,
                    "kind": "review",
                    "capabilities": ["read_repository", "review_diff"],
                    "status": "PASS",
                    "provenance": {"source": "agent_proposal"},
                    "findings": [],
                    **malicious,
                }
                errors = validate_packet(packet, "review")
                self.assertTrue(any("execute" in error or "shellCommand" in error for error in errors))

    def test_missing_malformed_or_unknown_provenance_is_rejected(self):
        from scripts.validate_skill import validate_packet

        for provenance in (None, [], {}, {"source": ""}, {"source": 7}, {"source": "reviewer"}):
            with self.subTest(provenance=provenance):
                packet = {
                    "protocolVersion": "1.0",
                    "packetId": "review-test",
                    "packetVersion": 1,
                    "kind": "review",
                    "capabilities": ["read_repository", "review_diff"],
                    "status": "PASS",
                    "findings": [],
                }
                if provenance is not None:
                    packet["provenance"] = provenance
                errors = validate_packet(packet, "review")
                self.assertTrue(any("provenance" in error for error in errors))

    def test_cyclic_and_oversized_packets_return_errors_without_raising(self):
        from unittest.mock import patch

        from scripts.validate_skill import validate_packet

        def valid_packet():
            return {
                "protocolVersion": "1.0",
                "kind": "review",
                "status": "PASS",
                "provenance": {"source": "agent_proposal"},
                "findings": [],
            }

        cyclic_dict = valid_packet()
        cyclic_dict["cycle"] = cyclic_dict

        cyclic_list = []
        cyclic_list.append(cyclic_list)
        packet_with_cyclic_list = valid_packet()
        packet_with_cyclic_list["cycle"] = cyclic_list

        too_deep = valid_packet()
        nested = too_deep
        for _ in range(66):
            child = {}
            nested["nested"] = child
            nested = child

        for packet in (cyclic_dict, packet_with_cyclic_list, too_deep):
            errors = validate_packet(packet, "review")
            self.assertIsInstance(errors, list)
            self.assertTrue(errors)

        over_node_limit = valid_packet()
        over_node_limit["values"] = [{"value": index} for index in range(5)]
        with patch("scripts.validate_skill.MAX_PACKET_NODES", 4):
            errors = validate_packet(over_node_limit, "review")
        self.assertTrue(any("node" in error.lower() for error in errors))

    def test_wide_containers_stop_at_node_budget_before_later_fields(self):
        from unittest.mock import patch

        from scripts.validate_skill import validate_packet

        packet = {
            "protocolVersion": "1.0",
            "kind": "review",
            "status": "PASS",
            "provenance": {"source": "agent_proposal"},
            "wide": [{"value": index} for index in range(100)],
            "findings": [{"status": "RESOLVED"}],
        }
        with patch("scripts.validate_skill.MAX_PACKET_NODES", 8):
            errors = validate_packet(packet, "review")
        self.assertTrue(any("node" in error.lower() for error in errors))
        self.assertFalse(any("RESOLVED" in error for error in errors))

    def test_wide_dict_stops_at_node_budget(self):
        from unittest.mock import patch

        from scripts.validate_skill import validate_packet

        packet = {
            "protocolVersion": "1.0",
            "kind": "review",
            "status": "PASS",
            "provenance": {"source": "agent_proposal"},
            "wide": {f"field-{index}": index for index in range(100)},
        }
        with patch("scripts.validate_skill.MAX_PACKET_NODES", 8):
            errors = validate_packet(packet, "review")
        self.assertTrue(any("node" in error.lower() for error in errors))

    def test_repeated_container_reference_is_not_an_error(self):
        from scripts.validate_skill import validate_packet

        shared = {"safe": True}
        packet = {
            "protocolVersion": "1.0",
            "packetId": "review-test",
            "packetVersion": 1,
            "kind": "review",
            "capabilities": ["read_repository", "review_diff"],
            "status": "PASS",
            "provenance": {"source": "agent_proposal"},
            "findings": [],
            "first": shared,
            "second": shared,
        }
        self.assertEqual(validate_packet(packet, "review"), [])

    def test_invalid_or_missing_protocol_version_is_rejected(self):
        from scripts.validate_skill import validate_packet

        for version in (None, "2.0", 1.0):
            with self.subTest(protocolVersion=version):
                packet = {
                    "kind": "review",
                    "status": "PASS",
                    "provenance": {"source": "agent_proposal"},
                    "findings": [],
                }
                if version is not None:
                    packet["protocolVersion"] = version
                errors = validate_packet(packet, "review")
                self.assertTrue(any("protocolVersion" in error for error in errors))

    def test_malformed_packet_shapes_return_errors_without_raising(self):
        from scripts.validate_skill import validate_packet

        malformed_packets = (
            None,
            [],
            "packet",
            {"protocolVersion": "1.0", "status": "PASS", "findings": {}},
            {"protocolVersion": "1.0", "status": "PASS", "findings": [None]},
            {
                "protocolVersion": "1.0",
                "status": "PASS",
                "findings": [{"status": "RESOLVED", "closedBy": 7}],
            },
        )
        for packet in malformed_packets:
            with self.subTest(packet=packet):
                errors = validate_packet(packet, "review")
                self.assertIsInstance(errors, list)
                self.assertTrue(errors)


if __name__ == "__main__":
    unittest.main()
