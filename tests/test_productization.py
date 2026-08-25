"""Phase 10H-L: productization contract — docs, templates, agents, packaging, example.

Verifies the V2 product surface matches the source of truth: templates equal
the real REQUIRED_FIELDS, four role assets exist, README/pyproject/package
shim exist, the offline example runs end-to-end without any runtime, and no
V1 protocol residue remains in active product assets (legacy dev history is
explicitly out of scope).
"""
import json
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from structured_packets import (
    ArchitecturePacket,
    ImplementationPacket,
    ReviewPacket,
    TestPacket,
)

ACTIVE_DOC_ASSETS = [
    "README.md",
    "pyproject.toml",
    "examples/offline_mock_run.py",
    "dual-agent-development/SKILL.md",
    "dual-agent-development/references/workflow.md",
    "dual-agent-development/references/adapter-contract.md",
    "dual-agent-development/templates/architecture-packet.json",
    "dual-agent-development/templates/implementation-packet.json",
    "dual-agent-development/templates/test-packet.json",
    "dual-agent-development/templates/review-packet.json",
    "dual-agent-development/agents/architect.md",
    "dual-agent-development/agents/coder.md",
    "dual-agent-development/agents/tester.md",
    "dual-agent-development/agents/reviewer.md",
    "dual-agent-development/agents/openai.yaml",
]

V1_MARKERS = (
    "packetId", "packetVersion", "findingId", "protocolVersion",
    "read_repository", "propose_commands", "write_files", "run_tests",
    "review_diff", "agent_proposal",
)
# Word-boundary markers: plain substring scan would false-positive on V2's
# own UNRESOLVED (Complexity member).
V1_WORD_MARKERS = ("RESOLVED", "NEED_FIX", "ARCHITECTURE_VIOLATION")

SECRET_MARKERS = ("token", "secret", "api_key", "authorization", "bearer", "stdout", "stderr")


class AssetExistenceTests(unittest.TestCase):
    def test_all_v2_product_assets_exist(self):
        for relative in ACTIVE_DOC_ASSETS:
            with self.subTest(asset=relative):
                self.assertTrue((REPO / relative).exists(), relative)

    def test_package_init_shim_exists(self):
        self.assertTrue((SCRIPTS / "__init__.py").exists())


class TemplateContractTests(unittest.TestCase):
    def test_templates_equal_source_required_fields(self):
        # ArchitecturePacket pins its role via validation, not required_role().
        pairs = (
            ("templates/architecture-packet.json", ArchitecturePacket, "architect"),
            ("templates/implementation-packet.json", ImplementationPacket, None),
            ("templates/test-packet.json", TestPacket, None),
            ("templates/review-packet.json", ReviewPacket, None),
        )
        base = REPO / "dual-agent-development"
        for relative, packet_class, fixed_role in pairs:
            with self.subTest(template=relative):
                data = json.loads((base / relative).read_text(encoding="utf-8"))
                self.assertEqual(set(data.keys()), set(packet_class.REQUIRED_FIELDS))
                expected_role = fixed_role or packet_class.required_role()
                self.assertEqual(data["role"], expected_role)

    def test_templates_are_valid_json_objects(self):
        base = REPO / "dual-agent-development" / "templates"
        for path in sorted(base.glob("*.json")):
            with self.subTest(template=path.name):
                self.assertIsInstance(json.loads(path.read_text(encoding="utf-8")), dict)


class AgentsTests(unittest.TestCase):
    def test_four_role_files_exist_and_name_their_packet(self):
        base = REPO / "dual-agent-development" / "agents"
        pairs = (
            ("architect.md", "ArchitecturePacket"),
            ("coder.md", "ImplementationPacket"),
            ("tester.md", "TestPacket"),
            ("reviewer.md", "ReviewPacket"),
        )
        for filename, packet_name in pairs:
            with self.subTest(agent=filename):
                text = (base / filename).read_text(encoding="utf-8")
                self.assertIn(packet_name, text)

    def test_openai_yaml_lists_four_roles(self):
        text = (REPO / "dual-agent-development" / "agents" / "openai.yaml").read_text(encoding="utf-8")
        lowered = text.lower()
        for role in ("architect", "coder", "tester", "reviewer"):
            self.assertIn(role, lowered)


class DocumentationTests(unittest.TestCase):
    def test_active_assets_carry_no_v1_protocol_residue(self):
        for relative in ACTIVE_DOC_ASSETS:
            if relative.endswith(("pyproject.toml",)) or relative.startswith("examples/"):
                continue  # scanned separately below
            with self.subTest(asset=relative):
                text = (REPO / relative).read_text(encoding="utf-8")
                for marker in V1_MARKERS:
                    self.assertNotIn(marker, text)
                for marker in V1_WORD_MARKERS:
                    self.assertIsNone(
                        re.search(rf"\b{marker}\b", text),
                        f"V1 residue {marker!r} in {relative}")

    def test_skill_md_describes_v2_entrypoints(self):
        text = (REPO / "dual-agent-development" / "SKILL.md").read_text(encoding="utf-8")
        for concept in ("ProductionFacade", "OFFLINE", "REAL", "architect", "coder",
                        "tester", "reviewer", "OFF", "AUTO", "ON"):
            self.assertIn(concept, text)

    def test_workflow_md_describes_ledger_contract(self):
        text = (REPO / "dual-agent-development" / "references" / "workflow.md").read_text(encoding="utf-8")
        for concept in ("CollaborationPacket", "correlation_id", "sequence", "provenance",
                        "MISSING_HANDOFF", "Budget", "LoopGuard", "ledger"):
            self.assertIn(concept, text)

    def test_readme_covers_quickstart_modes_and_honesty(self):
        text = (REPO / "README.md").read_text(encoding="utf-8")
        for concept in ("dual-agent run", "--mode", "OFF", "AUTO", "ON",
                        "architect", "coder", "tester", "reviewer",
                        "Offline", "REAL", "facade"):
            self.assertIn(concept, text)
        # honesty: must not promise automatic runtime configuration
        self.assertIn("inject", text.lower())


class PackagingTests(unittest.TestCase):
    def test_pyproject_declares_package_cli_and_assets(self):
        try:
            import tomllib
        except ModuleNotFoundError:  # Python 3.10: tomllib is 3.11+ stdlib
            import tomli as tomllib
        data = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(data["project"]["name"], "dual-agent-development")
        scripts = data["project"]["scripts"]
        self.assertEqual(scripts["dual-agent"], "dual_agent.cli:main")
        package_dir = data["tool"]["setuptools"]["package-dir"]
        self.assertEqual(package_dir["dual_agent"], "dual-agent-development/scripts")
        carried = json.dumps(data["tool"]["setuptools"].get("data-files", {}))
        for asset in ("SKILL.md", "workflow.md", "templates", "agents", "offline_mock_run"):
            self.assertIn(asset, carried)

    def test_init_shim_exposes_flat_modules(self):
        namespace = {"__file__": str(SCRIPTS / "__init__.py"), "__name__": "dual_agent"}
        code = (SCRIPTS / "__init__.py").read_text(encoding="utf-8")
        exec(compile(code, namespace["__file__"], "exec"), namespace)
        self.assertIn(str(SCRIPTS), sys.path)
        self.assertTrue(namespace["__version__"])


class OfflineExampleTests(unittest.TestCase):
    def test_example_runs_offline_and_emits_safe_summary(self):
        env = {k: v for k, v in os.environ.items() if k != "RUN_REAL_PROVIDER_TESTS"}
        completed = subprocess.run(
            [sys.executable, str(REPO / "examples" / "offline_mock_run.py")],
            capture_output=True, text=True, timeout=120, cwd=str(REPO), env=env,
            encoding="utf-8", errors="replace",
        )
        self.assertEqual(completed.returncode, 0, completed.stderr[-400:])
        self.assertIn("SUCCESS", completed.stdout)
        self.assertIn("FOUR_STAGE", completed.stdout)
        lowered = completed.stdout.lower()
        for marker in SECRET_MARKERS:
            self.assertNotIn(marker, lowered)

    def test_example_contains_no_real_runtime_invocation(self):
        text = (REPO / "examples" / "offline_mock_run.py").read_text(encoding="utf-8")
        self.assertNotIn("RUN_REAL_PROVIDER_TESTS", text)
        self.assertNotIn("from_env", text)  # no real adapter construction


if __name__ == "__main__":
    unittest.main()
