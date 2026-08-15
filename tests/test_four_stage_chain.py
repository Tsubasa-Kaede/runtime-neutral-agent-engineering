"""Phase 10H-H: four-stage collaboration chain — composition verification.

Proves that CollaborationOrchestrator (architect+coder) and
VerificationCollaboration (tester+reviewer) compose into one architect ->
coder -> tester -> reviewer chain over a single shared ledger, budget, usage
and loop guard. OFFLINE structural verification only — mock adapters, no
runtime. The causal chain lives in the ledger: dense per-task sequence,
per-hop correlation, payload_type ARCHITECTURE -> IMPLEMENTATION -> TEST ->
REVIEW, verbatim provenance, honest failure.
"""
import json
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from collaboration_orchestrator import CollaborationOrchestrator
from collaboration_session import CollaborationSession, CollaborationStatus, collab_agent_address
from collaboration_state import CollaborationDirection, SharedCollaborationState
from candidate_validation import (
    CandidateValidationResult,
    CandidateValidationStatus,
    GateResult,
    GateVerdict,
    ValidationGate,
)
from external_runtime import InvocationResult, InvocationStatus, InvocationTrace
from loop_guard import LoopGuard
from mode_gate import Mode
from remote_transport import LoopbackRemoteTransport
from runtime_status import HealthEvidence, ReasonCode, RuntimeState, RuntimeStatus
from task_budget import BudgetUsage, TaskBudget
from verified_runtime_pool import VerifiedRuntimePool
from verification_collaboration import VerificationCollaboration, VerificationStatus

IDENTITY = ("rt-x", "provider-x", "model-x", "fp-x")
ALL_CAPS = ("architecture", "coding", "testing", "review")
ARCH_ADDR = collab_agent_address(IDENTITY, "architect")
CODER_ADDR = collab_agent_address(IDENTITY, "coder")
TESTER_ADDR = collab_agent_address(IDENTITY, "tester")
REVIEWER_ADDR = collab_agent_address(IDENTITY, "reviewer")

TASK = "redesign architecture across modules"
SECRET_MARKERS = ("token", "secret", "api_key", "authorization", "bearer", "stdout", "stderr")


def arch_dict(task_id="T1"):
    return {
        "task_id": task_id, "role": "architect", "goal": ["g"], "constraints": ["c"],
        "architecture": ["a"], "interfaces": [{}], "implementation_steps": [{}],
        "acceptance_criteria": ["ac"], "risks": [{}],
    }


def impl_dict(task_id="T1"):
    return {
        "task_id": task_id, "role": "coder", "changed_files": ["f.py"],
        "implementation_summary": "s", "implementation_details": ["d"],
        "assumptions": [], "unresolved_items": [], "test_requirements": ["tr"],
    }


def test_dict(task_id="T1"):
    return {
        "task_id": task_id, "role": "tester", "tests_run": ["t"], "tests_passed": ["t"],
        "tests_failed": [], "failures": [], "coverage_or_validation": [],
        "remaining_risks": [],
    }


def review_dict(task_id="T1"):
    return {
        "task_id": task_id, "role": "reviewer", "status": "PASS", "findings": [],
        "severity": [], "affected_files": [], "required_changes": [],
        "acceptance_criteria_status": [],
    }


def trace(status=InvocationStatus.SUCCESS, exit_code=0):
    return InvocationTrace(
        invocation_id="inv-1", task_id="T1", agent_id="a", runtime="rt",
        provider=None, model=None, role=None, status=status,
        started_at=1.0, finished_at=2.0, duration_ms=10,
        exit_code=exit_code, input_tokens="unknown", output_tokens="unknown", error=None)


class RepeatingAdapter:
    def __init__(self, result):
        self.result = result
        self.requests = []

    def invoke(self, request):
        self.requests.append(request)
        return self.result


def ok(payload_dict):
    return InvocationResult(InvocationStatus.SUCCESS,
                            output=json.dumps(payload_dict), trace=trace())


def health_ready(runtime_id):
    return RuntimeStatus(
        runtime_id=runtime_id, executable="exe", version="1",
        status=RuntimeState.READY, provider="p", model="m", auth_method=None,
        reason_code=ReasonCode.NONE,
        evidence=HealthEvidence("d", "a", "p", "m", "ok"),
        checked_at=1.0, expires_at=2.0)


def make_pool():
    pool = VerifiedRuntimePool(clock=lambda: 1.0)
    result = CandidateValidationResult(
        identity=IDENTITY, status=CandidateValidationStatus.VERIFIED,
        gates_passed=frozenset(ValidationGate),
        gate_results=tuple(GateResult(g, GateVerdict.PASS) for g in ValidationGate),
        block_reason=None, failure_point=None, experiment_id="exp-1", executed_at=1.0,
        validated_capabilities=ALL_CAPS, evidence={})
    pool.admit(result, ALL_CAPS, health_now="READY")
    return pool


def compose(task_id="T1", arch_result=None, coder_result=None):
    """Shared budget/usage/guard/state; arch+coder adapters + tester+reviewer."""
    budget = TaskBudget(4, 4, timeout_seconds=30.0)
    usage = BudgetUsage()
    guard = LoopGuard()
    shared_state = SharedCollaborationState()
    arch_adapters = {
        ARCH_ADDR: RepeatingAdapter(arch_result if arch_result is not None else ok(arch_dict(task_id))),
        CODER_ADDR: RepeatingAdapter(coder_result if coder_result is not None else ok(impl_dict(task_id))),
    }

    def session_factory():
        return CollaborationSession(LoopbackRemoteTransport(), arch_adapters,
                                    budget, usage, guard)

    orchestrator = CollaborationOrchestrator(
        object(), make_pool(), {IDENTITY[0]: health_ready(IDENTITY[0])},
        budget, usage, guard, session_factory, state=shared_state)
    verify_adapters = {
        TESTER_ADDR: RepeatingAdapter(ok(test_dict(task_id))),
        REVIEWER_ADDR: RepeatingAdapter(ok(review_dict(task_id))),
    }
    return orchestrator, verify_adapters, budget, usage, guard


def run_full_chain(task_id="T1", **compose_kwargs):
    orchestrator, verify_adapters, budget, usage, guard = compose(task_id, **compose_kwargs)
    orch_outcome = orchestrator.run(task_id=task_id, task=TASK, prompt="p", mode=Mode.ON)
    if orch_outcome.status is not CollaborationStatus.SUCCESS:
        return orchestrator, verify_adapters, budget, usage, guard, orch_outcome, None
    verification = VerificationCollaboration(
        verify_adapters, budget, usage, guard, state=orchestrator.state)
    voutcome = verification.run(task_id, TESTER_ADDR, REVIEWER_ADDR, ARCH_ADDR)
    return orchestrator, verify_adapters, budget, usage, guard, orch_outcome, (verification, voutcome)


class FourStageChainTests(unittest.TestCase):
    def test_composition_records_dense_five_entry_ledger(self):
        _, _, _, _, _, _, (verification, voutcome) = run_full_chain()
        self.assertEqual(voutcome.status, VerificationStatus.SUCCESS)
        history = verification.state.history("T1")
        self.assertEqual(len(history), 5)
        self.assertEqual([r.sequence for r in history], [1, 2, 3, 4, 5])
        self.assertEqual([r.direction for r in history],
                         [CollaborationDirection.DECISION,
                          CollaborationDirection.REQUEST,
                          CollaborationDirection.REPLY,
                          CollaborationDirection.REQUEST,
                          CollaborationDirection.REQUEST])
        self.assertEqual([r.payload_type for r in history],
                         ["", "ARCHITECTURE", "IMPLEMENTATION", "TEST", "REVIEW"])
        self.assertTrue(all(r.task_id == "T1" for r in history))
        # correlation: C1 = r1==r2 non-empty; C2 distinct; C3 distinct
        c1 = history[1].correlation_id
        self.assertEqual(history[0].correlation_id, "")
        self.assertEqual(history[2].correlation_id, c1)
        self.assertNotEqual(history[3].correlation_id, c1)
        self.assertNotEqual(history[4].correlation_id, c1)
        self.assertNotEqual(history[4].correlation_id, history[3].correlation_id)
        for r in history[1:]:
            self.assertEqual(r.provenance, "OFFLINE")

    def test_budget_and_guard_shared_across_four_stages(self):
        _, _, _, usage, guard, _, (verification, _) = run_full_chain()
        self.assertEqual(usage.architect_calls, 1)
        self.assertEqual(usage.coder_calls, 1)
        self.assertEqual(usage.test_calls, 1)
        self.assertEqual(usage.review_calls, 1)
        self.assertEqual(usage.total_agent_calls, 4)
        # rerun same task + same stages -> guard carries across components
        again = verification.run("T1", TESTER_ADDR, REVIEWER_ADDR, ARCH_ADDR)
        self.assertEqual(again.status, VerificationStatus.LOOP_GUARD_REJECTED)

    def test_handoff_threads_one_ledger_across_components(self):
        _, verify_adapters, _, _, _, _, _ = run_full_chain()
        tester_prompt = verify_adapters[TESTER_ADDR].requests[0].prompt
        self.assertIn('"packet_type":"ImplementationPacket"', tester_prompt)
        reviewer_prompt = verify_adapters[REVIEWER_ADDR].requests[0].prompt
        self.assertIn('"packet_type":"ImplementationPacket"', reviewer_prompt)
        self.assertIn('"packet_type":"TestPacket"', reviewer_prompt)

    def test_ledger_wire_is_free_of_raw_output_and_secret(self):
        _, _, _, _, _, _, (verification, _) = run_full_chain()
        for record in verification.state.history("T1"):
            wire = record.wire.lower()
            for marker in SECRET_MARKERS:
                self.assertNotIn(marker, wire)
        surface = repr(verification.state).lower()
        for marker in SECRET_MARKERS:
            self.assertNotIn(marker, surface)

    def test_architect_failure_is_honest_and_downstream_does_not_fake_success(self):
        bad = InvocationResult(InvocationStatus.SUCCESS, output="free text", trace=trace())
        _, _, _, _, _, orch_outcome, _ = run_full_chain(arch_result=bad)
        self.assertEqual(orch_outcome.status, CollaborationStatus.ARCHITECT_PACKET_INVALID)
        # the ledger has no IMPLEMENTATION, so tester read fails honestly
        orchestrator, verify_adapters, budget, usage, guard, _, _ = run_full_chain(arch_result=bad)
        verification = VerificationCollaboration(
            verify_adapters, budget, usage, guard, state=orchestrator.state)
        voutcome = verification.run("T1", TESTER_ADDR, REVIEWER_ADDR, ARCH_ADDR)
        self.assertEqual(voutcome.status, VerificationStatus.MISSING_HANDOFF)
        # no TEST/REVIEW was fabricated
        payload_types = [r.payload_type for r in verification.state.history("T1")]
        self.assertNotIn("TEST", payload_types)
        self.assertNotIn("REVIEW", payload_types)


if __name__ == "__main__":
    unittest.main()
