import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"))
from dual_agent import ReviewStateMachine, ReviewState, Finding, Evidence, EvidenceStatus

class StateMachineTests(unittest.TestCase):
    def test_need_fix_open_pass_and_blocking_rules(self):
        sm=ReviewStateMachine(); f=Finding("F1","bug", "sig")
        ev=Evidence("round", "review", EvidenceStatus.VERIFIED)
        self.assertEqual(sm.apply("NEED_FIX", [f], [ev]).state, ReviewState.NEED_FIX)
        self.assertEqual(sm.apply("OPEN", [Finding("F2","bug", "sig2")], [ev]).state, ReviewState.OPEN)
        self.assertEqual(sm.apply("PASS", [Finding("F1","bug", "sig", "RESOLVED", "controller"), Finding("F2","bug", "sig2", "RESOLVED", "controller")], [ev]).state, ReviewState.PASS)
    def test_third_round_repeated_missing_evidence_and_architecture(self):
        sm=ReviewStateMachine(); f=Finding("F1","bug","sig")
        for _ in range(2): sm.apply("NEED_FIX", [f])
        self.assertEqual(sm.apply("NEED_FIX", [f]).state, ReviewState.BLOCKED)
        self.assertEqual(ReviewStateMachine().apply("PASS", [f]).state, ReviewState.BLOCKED)
        e=Evidence("x","claim",EvidenceStatus.UNKNOWN)
        self.assertEqual(ReviewStateMachine().apply("OPEN", [], [e]).state, ReviewState.BLOCKED)
        self.assertEqual(ReviewStateMachine().apply("ARCHITECTURE_VIOLATION", []).state, ReviewState.ARCHITECTURE_VIOLATION)
    def test_finding_id_cannot_change_signature(self):
        ev=Evidence("e","review",EvidenceStatus.VERIFIED); sm=ReviewStateMachine()
        sm.apply("OPEN",[Finding("F1","x","sig-a")],[ev])
        result=sm.apply("OPEN",[Finding("F1","x","sig-b")],[ev])
        self.assertEqual(result.state,ReviewState.BLOCKED)
        self.assertEqual(result.findings[0].signature,"sig-a")
    def test_terminal_idempotent_and_controller_closure(self):
        sm=ReviewStateMachine(); ev=Evidence("round", "review", EvidenceStatus.VERIFIED); sm.apply("PASS",[],[ev]); self.assertEqual(sm.apply("PASS",[],[ev]).state,ReviewState.PASS)
        sm=ReviewStateMachine(); f=Finding("F","x","s","RESOLVED","controller"); self.assertEqual(sm.apply("PASS",[f],[ev]).state,ReviewState.PASS)
        self.assertEqual(ReviewStateMachine().apply("PASS",[Finding("F","x","s","RESOLVED","agent")],[ev]).state,ReviewState.BLOCKED)

if __name__ == '__main__': unittest.main()
