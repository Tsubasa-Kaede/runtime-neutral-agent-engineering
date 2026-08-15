import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from task_classifier import Complexity, classify_task


class TaskClassifierTests(unittest.TestCase):
    def test_simple_rules(self):
        self.assertEqual(classify_task("fix one function"), Complexity.SIMPLE)
        self.assertEqual(classify_task("update one config value"), Complexity.SIMPLE)

    def test_medium_rules(self):
        self.assertEqual(classify_task("change two related files and add tests"), Complexity.MEDIUM)

    def test_complex_rules(self):
        self.assertEqual(classify_task("redesign architecture across modules"), Complexity.COMPLEX)
        self.assertEqual(classify_task("perform a complex migration"), Complexity.COMPLEX)

    def test_unresolved_is_explicit(self):
        self.assertEqual(classify_task("please help"), Complexity.UNRESOLVED)


if __name__ == "__main__":
    unittest.main()
