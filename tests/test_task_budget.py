import json
import sys
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from task_budget import BudgetExceeded, BudgetUsage, TaskBudget


class TaskBudgetTests(unittest.TestCase):
    def budget(self):
        return TaskBudget(
            max_agent_calls=4,
            max_iterations=2,
            max_total_input_tokens=100,
            max_total_output_tokens=80,
            max_context_tokens_per_call=60,
            timeout_seconds=30,
        )

    def test_budget_is_immutable_and_serializable(self):
        budget = self.budget()
        self.assertIsInstance(budget.to_dict(), dict)
        self.assertEqual(TaskBudget.from_dict(budget.to_dict()), budget)
        with self.assertRaises(FrozenInstanceError):
            budget.max_agent_calls = 9
        self.assertNotIn("secret", json.dumps(budget.to_dict()).lower())

    def test_every_role_shares_one_total_call_limit(self):
        usage = BudgetUsage()
        budget = self.budget()
        for role in ("classification", "architect", "coder", "fix"):
            budget.reserve_call(usage, role)
        self.assertEqual(usage.total_agent_calls, 4)
        self.assertEqual(usage.classification_calls, 1)
        self.assertEqual(usage.architect_calls, 1)
        self.assertEqual(usage.coder_calls, 1)
        self.assertEqual(usage.fix_calls, 1)

    def test_exceeding_shared_call_limit_is_rejected(self):
        usage = BudgetUsage()
        budget = self.budget()
        for role in ("architect", "coder", "test", "review"):
            budget.reserve_call(usage, role)
        with self.assertRaises(BudgetExceeded):
            budget.reserve_call(usage, "fix")
        self.assertEqual(usage.total_agent_calls, 4)

    def test_token_usage_is_unknown_without_runtime_evidence(self):
        usage = BudgetUsage()
        self.assertEqual(usage.total_input_tokens, "unknown")
        self.assertEqual(usage.total_output_tokens, "unknown")
        usage.record_tokens("unknown", "unknown")
        self.assertEqual(usage.total_input_tokens, "unknown")
        self.assertEqual(usage.total_output_tokens, "unknown")

    def test_known_tokens_are_accumulated_and_limited(self):
        usage = BudgetUsage()
        budget = self.budget()
        budget.reserve_call(usage, "coder")
        usage.record_tokens(40, 30)
        self.assertEqual(usage.total_input_tokens, 40)
        self.assertEqual(usage.total_output_tokens, 30)
        with self.assertRaises(BudgetExceeded):
            usage.record_tokens(70, 1, budget=budget)

    def test_iteration_and_escalation_limits_are_shared(self):
        usage = BudgetUsage()
        budget = self.budget()
        budget.reserve_iteration(usage)
        budget.reserve_iteration(usage)
        with self.assertRaises(BudgetExceeded):
            budget.reserve_iteration(usage)
        usage.record_escalation()
        usage.record_escalation()
        with self.assertRaises(BudgetExceeded):
            usage.record_escalation(max_escalations=2)


if __name__ == "__main__":
    unittest.main()
