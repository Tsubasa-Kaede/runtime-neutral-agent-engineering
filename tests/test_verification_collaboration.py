"""Phase 10H-G2: VerificationCollaboration — ledger-backed tester->reviewer loop.

Composed AFTER CollaborationSession: reads upstream facts from the shared
ledger via handoff_input_for, drives tester then reviewer with the SAME
shared budget/usage/loop_guard, emits TestPacket/ReviewPacket and writes
REQUEST records back. Honest failure statuses, per-hop correlation, verbatim
provenance, and its own raw-output scan over open dict packet fields.
"""
import json
import sys
import unittest
from dataclasses import FrozenInstanceError, fields
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from collaboration_packet import CollaborationPacket, CollaborationPayloadType
from collaboration_handoff import handoff_input_for
from collaboration_session import collab_agent_address
from collaboration_state import CollaborationDirection, SharedCollaborationState
from external_runtime import InvocationResult, InvocationStatus, InvocationTrace
from handoff_context import HandoffError
from loop_guard import LoopGuard
from structured_packets import (
    ArchitecturePacket,
    ImplementationPacket,
    ReviewPacket,
    TestPacket,
    serialize_packet,
)
from task_budget import BudgetUsage, TaskBudget
from verified_selection_bridge import agent_id_for
from verification_collaboration import (
    VerificationCollaboration,
    VerificationOutcome,
    VerificationStatus,
)

IDENTITY = ("rt-x", "provider-x", "model-x", "fp-x")
ARCH_ADDR = collab_agent_address(IDENTITY, "architect")
CODER_ADDR = collab_agent_address(IDENTITY, "coder")
TESTER_ADDR = collab_agent_address(IDENTITY, "tester")
REVIEWER_ADDR = collab_agent_address(IDENTITY, "reviewer")

SECRET_MARKERS = ("token", "secret", "api_key", "authorization", "bearer", "stdout", "stderr")


def arch(task_id="T1"):
    return ArchitecturePacket.from_dict({
        "task_id": task_id, "role": "architect", "goal": ["g"], "constraints": ["c"],
        "architecture": ["a"], "interfaces": [{}], "implementation_steps": [{}],
        "acceptance_criteria": ["ac"], "risks": [{}],
    })


def impl(task_id="T1"):
    return ImplementationPacket.from_dict({
        "task_id": task_id, "role": "coder", "changed_files": ["f.py"],
        "implementation_summary": "s", "implementation_details": ["d"],
        "assumptions": [], "unresolved_items": [], "test_requirements": ["tr"],
    })


def test_pkt_dict(task_id="T1", failures=None):
    return {
        "task_id": task_id, "role": "tester", "tests_run": ["t"], "tests_passed": ["t"],
        "tests_failed": [], "failures": failures if failures is not None else [],
        "coverage_or_validation": [], "remaining_risks": [],
    }


def review_pkt_dict(task_id="T1", findings=None):
    return {
        "task_id": task_id, "role": "reviewer", "status": "PASS",
        "findings": findings if findings is not None else [], "severity": [],
        "affected_files": [], "required_changes": [], "acceptance_criteria_status": [],
    }


def trace(status=InvocationStatus.SUCCESS, exit_code=0):
    return InvocationTrace(
        invocation_id="inv-1", task_id="T1", agent_id="a", runtime="rt",
        provider=None, model=None, role=None, status=status,
        started_at=1.0, finished_at=2.0, duration_ms=10,
        exit_code=exit_code, input_tokens="unknown", output_tokens="unknown",
        error=None)


class FakeAgentAdapter:
    def __init__(self, result):
        self.result = result
        self.requests = []

    def invoke(self, request):
        self.requests.append(request)
        return self.result


def ok_result(payload_dict):
    return InvocationResult(InvocationStatus.SUCCESS,
                            output=json.dumps(payload_dict), trace=trace())


def prefilled_ledger(task_id="T1", corr="C1"):
    """arch(REQUEST) + impl(REPLY) as CollaborationSession would leave it."""
    state = SharedCollaborationState()
    state = state.append_envelope(
        task_id,
        CollaborationPacket(
            correlation_id=corr, task_id=task_id,
            source_agent=ARCH_ADDR, target_agent=CODER_ADDR,
            source_role="architect", target_role="coder",
            payload_type=CollaborationPayloadType.ARCHITECTURE, payload=arch(task_id)),
        "REQUEST", "DELIVERED")
    state = state.append_envelope(
        task_id,
        CollaborationPacket(
            correlation_id=corr, task_id=task_id,
            source_agent=CODER_ADDR, target_agent=ARCH_ADDR,
            source_role="coder", target_role="architect",
            payload_type=CollaborationPayloadType.IMPLEMENTATION, payload=impl(task_id)),
        "REPLY", "DELIVERED")
    return state


def make_g2(tester_result=None, reviewer_result=None, state=None, budget=None,
            usage=None, guard=None):
    adapters = {
        TESTER_ADDR: FakeAgentAdapter(tester_result if tester_result is not None
                                      else ok_result(test_pkt_dict())),
        REVIEWER_ADDR: FakeAgentAdapter(reviewer_result if reviewer_result is not None
                                        else ok_result(review_pkt_dict())),
    }
    return (VerificationCollaboration(
        adapters, budget or TaskBudget(8, 8, timeout_seconds=30.0),
        usage or BudgetUsage(), guard or LoopGuard(),
        state=state if state is not None else prefilled_ledger()),
        adapters)


def run_g2(g2, **kwargs):
    values = dict(task_id="T1", tester_address=TESTER_ADDR,
                  reviewer_address=REVIEWER_ADDR, architect_address=ARCH_ADDR)
    values.update(kwargs)
    return g2.run(**values)


class ContractTests(unittest.TestCase):
    def test_status_members_and_values(self):
        self.assertEqual(
            {member.name for member in VerificationStatus},
            {"SUCCESS", "TESTER_INVOKE_FAILED", "TESTER_PACKET_INVALID",
             "REVIEWER_INVOKE_FAILED", "REVIEWER_PACKET_INVALID",
             "BUDGET_EXHAUSTED", "LOOP_GUARD_REJECTED", "MISSING_HANDOFF"},
        )
        for member in VerificationStatus:
            self.assertEqual(member.value, member.name)

    def test_outcome_field_set_and_frozen(self):
        self.assertEqual(
            {field.name for field in fields(VerificationOutcome)},
            {"status", "task_id", "provenance", "test_envelope",
             "review_envelope", "traces"},
        )
        outcome = VerificationOutcome(VerificationStatus.SUCCESS, "T1", "OFFLINE")
        with self.assertRaises(FrozenInstanceError):
            outcome.status = VerificationStatus.MISSING_HANDOFF


class SuccessPathTests(unittest.TestCase):
    def test_full_success_records_test_and_review(self):
        g2, _ = make_g2()
        outcome = run_g2(g2)
        self.assertEqual(outcome.status, VerificationStatus.SUCCESS)
        history = g2.state.history("T1")
        payload_types = [r.payload_type for r in history]
        self.assertIn("TEST", payload_types)
        self.assertIn("REVIEW", payload_types)
        self.assertIsNotNone(outcome.test_envelope)
        self.assertIsNotNone(outcome.review_envelope)
        self.assertEqual(outcome.test_envelope.payload_type,
                         CollaborationPayloadType.TEST)
        self.assertEqual(outcome.review_envelope.payload_type,
                         CollaborationPayloadType.REVIEW)
        self.assertEqual(len(outcome.traces), 2)

    def test_budget_records_test_and_review_buckets(self):
        usage = BudgetUsage()
        g2, _ = make_g2(usage=usage)
        run_g2(g2)
        self.assertEqual(usage.test_calls, 1)
        self.assertEqual(usage.review_calls, 1)
        self.assertEqual(usage.total_agent_calls, 2)

    def test_guard_records_test_and_review_stages(self):
        guard = LoopGuard()
        g2, _ = make_g2(guard=guard)
        run_g2(g2, task_id="T1")
        # rerun same task + same stages -> guard rejects
        outcome2 = run_g2(g2, task_id="T1")
        self.assertEqual(outcome2.status, VerificationStatus.LOOP_GUARD_REJECTED)

    def test_correlations_are_distinct_per_hop(self):
        g2, _ = make_g2()
        run_g2(g2)
        test_records = [r for r in g2.state.history("T1") if r.payload_type == "TEST"]
        review_records = [r for r in g2.state.history("T1") if r.payload_type == "REVIEW"]
        self.assertEqual(len(test_records), 1)
        self.assertEqual(len(review_records), 1)
        self.assertNotEqual(test_records[0].correlation_id,
                            review_records[0].correlation_id)

    def test_provenance_verbatim_offline_and_real(self):
        for provenance in ("OFFLINE", "REAL"):
            g2, _ = make_g2()
            outcome = run_g2(g2, provenance=provenance)
            self.assertEqual(outcome.provenance, provenance)
            test_record = [r for r in g2.state.history("T1")
                           if r.payload_type == "TEST"][0]
            self.assertEqual(test_record.provenance, provenance)

    def test_tester_receives_serialized_impl_not_raw(self):
        g2, adapters = make_g2()
        run_g2(g2)
        tester_prompt = adapters[TESTER_ADDR].requests[0].prompt
        self.assertIn('"packet_type":"ImplementationPacket"', tester_prompt)
        self.assertNotIn("raw stdout", tester_prompt)

    def test_reviewer_receives_impl_and_test_serialized(self):
        g2, adapters = make_g2()
        run_g2(g2)
        reviewer_prompt = adapters[REVIEWER_ADDR].requests[0].prompt
        self.assertIn('"packet_type":"ImplementationPacket"', reviewer_prompt)
        self.assertIn('"packet_type":"TestPacket"', reviewer_prompt)


class TesterFailureTests(unittest.TestCase):
    def test_tester_invoke_failed_short_circuits(self):
        g2, adapters = make_g2(tester_result=InvocationResult(
            InvocationStatus.FAILED, error="x", trace=trace(InvocationStatus.FAILED, 1)))
        outcome = run_g2(g2)
        self.assertEqual(outcome.status, VerificationStatus.TESTER_INVOKE_FAILED)
        self.assertEqual(adapters[REVIEWER_ADDR].requests, [])
        self.assertEqual(g2.state.failures("T1")[-1].status, "TESTER_INVOKE_FAILED")

    def test_tester_packet_invalid_non_json(self):
        g2, _ = make_g2(tester_result=InvocationResult(
            InvocationStatus.SUCCESS, output="free text", trace=trace()))
        outcome = run_g2(g2)
        self.assertEqual(outcome.status, VerificationStatus.TESTER_PACKET_INVALID)

    def test_tester_packet_with_raw_output_is_rejected_and_not_written(self):
        bad = test_pkt_dict(failures=[{"stdout": "raw model output"}])
        g2, _ = make_g2(tester_result=ok_result(bad))
        outcome = run_g2(g2)
        self.assertEqual(outcome.status, VerificationStatus.TESTER_PACKET_INVALID)
        payload_types = [r.payload_type for r in g2.state.history("T1")]
        self.assertNotIn("TEST", payload_types)


class ReviewerFailureTests(unittest.TestCase):
    def test_reviewer_invoke_failed(self):
        g2, _ = make_g2(reviewer_result=InvocationResult(
            InvocationStatus.TIMEOUT, error="timeout", trace=trace(InvocationStatus.TIMEOUT, None)))
        outcome = run_g2(g2)
        self.assertEqual(outcome.status, VerificationStatus.REVIEWER_INVOKE_FAILED)

    def test_reviewer_packet_invalid(self):
        g2, _ = make_g2(reviewer_result=InvocationResult(
            InvocationStatus.SUCCESS, output={"not": "a packet"}, trace=trace()))
        outcome = run_g2(g2)
        self.assertEqual(outcome.status, VerificationStatus.REVIEWER_PACKET_INVALID)

    def test_reviewer_packet_with_raw_output_is_rejected(self):
        bad = review_pkt_dict(findings=[{"stderr": "raw"}])
        g2, _ = make_g2(reviewer_result=ok_result(bad))
        outcome = run_g2(g2)
        self.assertEqual(outcome.status, VerificationStatus.REVIEWER_PACKET_INVALID)
        payload_types = [r.payload_type for r in g2.state.history("T1")]
        self.assertNotIn("REVIEW", payload_types)


class SharedConstraintTests(unittest.TestCase):
    def test_missing_handoff_when_impl_absent(self):
        # empty ledger -> tester has no implementation
        g2, _ = make_g2(state=SharedCollaborationState())
        outcome = run_g2(g2)
        self.assertEqual(outcome.status, VerificationStatus.MISSING_HANDOFF)

    def test_budget_exhausted_is_terminal(self):
        usage = BudgetUsage()
        usage.total_agent_calls = 7  # budget 8: tester reserve ok, reviewer reserve fails
        g2, adapters = make_g2(budget=TaskBudget(8, 8, timeout_seconds=30.0), usage=usage)
        outcome = run_g2(g2)
        self.assertEqual(outcome.status, VerificationStatus.BUDGET_EXHAUSTED)
        self.assertEqual(len(adapters[TESTER_ADDR].requests), 1)
        self.assertEqual(adapters[REVIEWER_ADDR].requests, [])

    def test_loop_guard_rejected_is_terminal(self):
        guard = LoopGuard()
        guard.record("T1", "test", TESTER_ADDR)
        g2, adapters = make_g2(guard=guard)
        outcome = run_g2(g2)
        self.assertEqual(outcome.status, VerificationStatus.LOOP_GUARD_REJECTED)
        self.assertEqual(adapters[TESTER_ADDR].requests, [])

    def test_outcome_repr_stays_clean(self):
        g2, _ = make_g2()
        outcome = run_g2(g2)
        # Contract-owned surface (trace field names input_tokens/output_tokens
        # carry the marker substring; their error VALUES are checked below).
        surface = (repr(outcome.status) + repr(outcome.test_envelope)
                   + repr(outcome.review_envelope) + outcome.task_id
                   + outcome.provenance).lower()
        for marker in SECRET_MARKERS:
            self.assertNotIn(marker, surface)
        for trace_ in outcome.traces:
            error_text = (trace_.error or "").lower()
            for marker in SECRET_MARKERS:
                self.assertNotIn(marker, error_text)


class SourceScanTests(unittest.TestCase):
    def test_runtime_neutral_and_no_budget_guard_mint(self):
        import verification_collaboration as module
        source = Path(module.__file__).read_text(encoding="utf-8")
        lowered = source.lower()
        for name in ("claude", "codex", "deepseek", "openai", "anthropic",
                     "gemini", "tiny-agents", "tiny_agents"):
            self.assertNotIn(name, lowered)
        for forbidden in ("os.environ", "getenv", "subprocess", "requests",
                          "urllib", "socket", "http", "uuid", "random",
                          "datetime", "import time", "time.", "monotonic",
                          "sleep", "TaskBudget(", "BudgetUsage(", "LoopGuard("):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
