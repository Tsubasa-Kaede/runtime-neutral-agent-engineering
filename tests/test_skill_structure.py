import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class SkillStructureTests(unittest.TestCase):
    def test_skill_entrypoint_has_trigger_only_frontmatter_and_references(self):
        path = ROOT / "dual-agent-development" / "SKILL.md"
        self.assertTrue(path.exists(), "Skill entrypoint must exist")
        text = path.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("---\nname: dual-agent-development\n"))
        self.assertIn("description: Use when", text)
        self.assertIn("references/workflow.md", text)
        self.assertTrue(
            (ROOT / "dual-agent-development" / "references" / "workflow.md").exists(),
            "referenced workflow guidance must exist",
        )
        self.assertNotIn("ANTHROPIC_AUTH_TOKEN", text)

    def test_role_files_define_distinct_boundaries(self):
        base = ROOT / "dual-agent-development" / "agents"
        for role in ("architect", "coder", "reviewer"):
            self.assertTrue((base / f"{role}.md").exists(), f"missing {role} role")
        architect = (base / "architect.md").read_text(encoding="utf-8")
        coder = (base / "coder.md").read_text(encoding="utf-8")
        reviewer = (base / "reviewer.md").read_text(encoding="utf-8")
        self.assertIn("read-only", architect.lower())
        self.assertIn("implement", coder.lower())
        self.assertIn("read-only", reviewer.lower())


class ProtocolTests(unittest.TestCase):
    def test_templates_are_versioned_and_do_not_authorize_commands(self):
        def forbidden_keys(value):
            if isinstance(value, dict):
                for key, nested in value.items():
                    if key in {"execute", "shellCommand"}:
                        yield key
                    yield from forbidden_keys(nested)
            elif isinstance(value, list):
                for nested in value:
                    yield from forbidden_keys(nested)

        base = ROOT / "dual-agent-development" / "templates"
        # V2 (10H-L): templates follow the V2 packet schema; the V1 protocol
        # fields (protocolVersion/packetId/packetVersion) are retired. Guard
        # that no template carries V1 residue and none authorizes commands.
        for name in ("architecture-packet.json", "implementation-packet.json",
                     "test-packet.json", "review-packet.json"):
            payload = json.loads((base / name).read_text(encoding="utf-8"))
            for v1_field in ("protocolVersion", "packetId", "packetVersion", "kind"):
                self.assertNotIn(v1_field, payload, name)
            self.assertEqual(list(forbidden_keys(payload)), [])

    def test_architecture_template_is_accepted(self):
        # V2 (10H-L): the architecture template follows the V2 packet schema,
        # so the legacy V1 protocol validator must reject it — asserting the
        # absence of V1 residue rather than the retired protocol's acceptance.
        from scripts.validate_skill import validate_packet

        path = (
            ROOT
            / "dual-agent-development"
            / "templates"
            / "architecture-packet.json"
        )
        packet = json.loads(path.read_text(encoding="utf-8"))
        self.assertNotEqual(validate_packet(packet, "architecture"), [])

    def test_validator_rejects_unknown_terminal_status(self):
        from scripts.validate_skill import validate_packet

        packet = {
            "protocolVersion": "1.0",
            "kind": "review",
            "status": "DONE_BY_AGENT",
            "provenance": {"source": "agent_proposal"},
        }
        errors = validate_packet(packet, "review")
        self.assertTrue(any("status" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
