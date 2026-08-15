import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from loop_guard import GuardDecision, LoopGuard


class LoopGuardTests(unittest.TestCase):
    def test_first_task_stage_is_allowed(self):
        guard = LoopGuard(max_iterations=3, max_escalations=2)
        decision = guard.check("task-a", "coder", "agent-a")
        self.assertEqual(decision, GuardDecision.ALLOW)
        guard.record("task-a", "coder", "agent-a")

    def test_duplicate_task_is_rejected(self):
        guard = LoopGuard()
        guard.record("task-a", "coder", "agent-a")
        result = guard.check("task-a", "coder", "agent-a")
        self.assertEqual(result, GuardDecision.DUPLICATE_TASK)

    def test_repeated_failure_is_rejected(self):
        guard = LoopGuard()
        guard.record_failure("task-a", "coder", "agent-a", "provider_timeout")
        result = guard.check("task-a", "coder", "agent-a", "provider_timeout")
        self.assertEqual(result, GuardDecision.REPEATED_FAILURE)

    def test_cycle_is_rejected(self):
        guard = LoopGuard(max_history=4)
        for stage, agent in (("a", "x"), ("b", "y"), ("a", "x"), ("b", "y")):
            guard.record("task-a", stage, agent)
        self.assertEqual(guard.check("task-a", "a", "x"), GuardDecision.CYCLE_DETECTED)

    def test_max_iterations_and_escalations(self):
        guard = LoopGuard(max_iterations=1, max_escalations=1)
        guard.record("task-a", "coder", "a")
        guard.record_iteration()
        self.assertEqual(guard.check("task-b", "coder", "a"), GuardDecision.MAX_ITERATIONS)
        guard.record_escalation()
        self.assertEqual(guard.check("task-c", "coder", "a"), GuardDecision.MAX_ESCALATIONS)

    def test_structured_signature_does_not_use_error_text(self):
        guard = LoopGuard()
        guard.record_failure("task-a", "coder", "agent-a", "timeout")
        guard.record_failure("task-a", "coder", "agent-a", "timeout: token=secret")
        self.assertEqual(len(guard.failure_signatures), 1)


if __name__ == "__main__":
    unittest.main()
