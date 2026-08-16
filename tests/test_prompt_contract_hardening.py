"""Prompt contract hardening (Retry #3): tester/reviewer array type rules.

Locks that TESTER_INSTRUCTION and REVIEWER_INSTRUCTION carry explicit JSON
array type rules (the G14-proven wording) and that no other prompt semantics
changed. Root cause: real tester returned "tests_run": 0 (number) under the
old key-list-only instruction.
"""
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from verification_collaboration import REVIEWER_INSTRUCTION, TESTER_INSTRUCTION

TESTER_ARRAY_FIELDS = (
    "tests_run", "tests_passed", "tests_failed", "failures",
    "coverage_or_validation", "remaining_risks",
)
REVIEWER_ARRAY_FIELDS = (
    "findings", "severity", "affected_files", "required_changes",
    "acceptance_criteria_status",
)


class PromptContractTests(unittest.TestCase):
    def test_tester_instruction_has_array_type_rules(self):
        for field in TESTER_ARRAY_FIELDS:
            self.assertIn(field, TESTER_INSTRUCTION)
        self.assertIn("JSON array", TESTER_INSTRUCTION)
        self.assertIn("[]", TESTER_INSTRUCTION)
        self.assertIn("never a number or a bare string", TESTER_INSTRUCTION)

    def test_reviewer_instruction_has_array_type_rules(self):
        for field in REVIEWER_ARRAY_FIELDS:
            self.assertIn(field, REVIEWER_INSTRUCTION)
        self.assertIn("JSON array", REVIEWER_INSTRUCTION)
        self.assertIn("[]", REVIEWER_INSTRUCTION)
        self.assertIn("never a number or a bare string", REVIEWER_INSTRUCTION)

    def test_core_prompt_semantics_unchanged(self):
        # The role contracts and key lists stay intact; only type rules were
        # appended.
        self.assertIn("You are the tester", TESTER_INSTRUCTION)
        self.assertIn("You are the reviewer", REVIEWER_INSTRUCTION)
        self.assertIn("complete input contract", TESTER_INSTRUCTION)
        self.assertIn("task_id must equal the packet task_id", TESTER_INSTRUCTION)
        self.assertIn("task_id must equal the packet task_id", REVIEWER_INSTRUCTION)
        self.assertIn("No prose, no markdown fences", TESTER_INSTRUCTION)
        self.assertIn("No prose, no markdown fences", REVIEWER_INSTRUCTION)


if __name__ == "__main__":
    unittest.main()
