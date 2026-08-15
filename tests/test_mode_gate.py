import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from mode_gate import Mode, ModeGate


class ModeGateTests(unittest.TestCase):
    def test_auto_simple_uses_fast_path(self):
        decision = ModeGate().decide(Mode.AUTO, "fix one function")
        self.assertFalse(decision.use_orchestrator)
        self.assertEqual(decision.mode, Mode.AUTO)

    def test_auto_complex_uses_orchestrator(self):
        decision = ModeGate().decide(Mode.AUTO, "redesign architecture across modules")
        self.assertTrue(decision.use_orchestrator)

    def test_on_forces_orchestrator_without_forcing_all_stages(self):
        decision = ModeGate().decide(Mode.ON, "fix one function")
        self.assertTrue(decision.use_orchestrator)

    def test_off_bypasses_orchestrator(self):
        decision = ModeGate().decide(Mode.OFF, "redesign architecture")
        self.assertFalse(decision.use_orchestrator)

    def test_mode_decision_is_deterministic(self):
        gate = ModeGate()
        self.assertEqual(gate.decide(Mode.AUTO, "same task"), gate.decide(Mode.AUTO, "same task"))


if __name__ == "__main__":
    unittest.main()
