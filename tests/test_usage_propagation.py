"""Phase 10H-I: usage propagation seams — trace -> BudgetUsage accounting.

The adapters now fill trace token fields when usage is genuinely observed.
This file locks the propagation contract at the three invoke seams that
own the shared BudgetUsage:

  collaboration_session      (architect + coder, DUAL path)
  verification_collaboration (tester + reviewer, FOUR_STAGE tail)
  execution_engine           (SINGLE path + fallback)

Each seam must call the EXISTING BudgetUsage.record_tokens with the
trace's observed values; "unknown" must flow through as "unknown"
(record_tokens already skips non-observed values). Capture changes no
decision: selection, assignment, guard, and result shapes are untouched.
"""
import json
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from collaboration_packet import CollaborationPacket, CollaborationPayloadType
from collaboration_state import SharedCollaborationState
from external_runtime import InvocationResult, InvocationStatus, InvocationTrace
from loop_guard import LoopGuard
from structured_packets import (
    ArchitecturePacket, ImplementationPacket, ReviewPacket, TestPacket,
)
from task_budget import BudgetUsage, TaskBudget


IDENTITY = ("rt-x", "provider-x", "model-x", "fp-x")


def collab_address(role):
    from collaboration_session import collab_agent_address
    return collab_agent_address(IDENTITY, role)


ARCH = collab_address("architect")
CODER = collab_address("coder")
TESTER = collab_address("tester")
REVIEWER = collab_address("reviewer")


def trace_with_tokens(input_tokens, output_tokens,
                      status=InvocationStatus.SUCCESS, exit_code=0):
    return InvocationTrace(
        invocation_id="inv-1", task_id="T1", agent_id="a", runtime="rt",
        provider=None, model=None, role=None, status=status,
        started_at=1.0, finished_at=2.0, duration_ms=10,
        exit_code=exit_code, input_tokens=input_tokens,
        output_tokens=output_tokens, error=None)


class RecordingAdapter:
    """Returns a canned InvocationResult per role, whatever the prompt."""

    def __init__(self, outputs):
        self.outputs = outputs

    def invoke(self, request):
        payload = self.outputs.get(request.role)
        if payload is None:
            raise AssertionError(f"unexpected role {request.role}")
        return payload


def arch_pkt(task_id="T1"):
    return ArchitecturePacket(task_id, "architect", ("g",), ("c",), ("a",), ({},),
                              ({},), ("ac",), ({},))


def impl_pkt(task_id="T1"):
    return ImplementationPacket(task_id, "coder", ("f.py",), "s", (), (), (), ())


def test_pkt(task_id="T1"):
    return TestPacket(task_id, "tester", (), (), (), (), (), ())


def review_pkt(task_id="T1"):
    return ReviewPacket(task_id, "reviewer", "PASS", (), (), (), (), ())


def as_json(packet):
    return json.dumps(packet.to_dict() if hasattr(packet, "to_dict") else packet.__dict__)


class SessionUsagePropagationTests(unittest.TestCase):
    """DUAL path: architect + coder usage reaches the shared usage."""

    def run_session(self, usage, arch_result, coder_result):
        from collaboration_session import CollaborationSession
        from remote_transport import LoopbackRemoteTransport
        adapters = {
            ARCH: RecordingAdapter({"architect": arch_result,
                                    "coder": coder_result}),
            CODER: RecordingAdapter({"architect": arch_result,
                                     "coder": coder_result}),
        }
        budget = TaskBudget(8, 8, timeout_seconds=30.0)
        session = CollaborationSession(
            LoopbackRemoteTransport(), adapters, budget, usage, LoopGuard())
        return session.run("T1", "redesign architecture across modules",
                           ARCH, CODER, provenance="OFFLINE")

    def test_observed_tokens_reach_usage(self):
        usage = BudgetUsage()
        arch = InvocationResult(InvocationStatus.SUCCESS,
                                output=json.dumps(arch_pkt().__dict__),
                                trace=trace_with_tokens(100, 40))
        coder = InvocationResult(InvocationStatus.SUCCESS,
                                 output=json.dumps(impl_pkt().__dict__),
                                 trace=trace_with_tokens(70, 30))
        outcome = self.run_session(usage, arch, coder)
        self.assertEqual(outcome.status.value, "SUCCESS", outcome.status)
        self.assertEqual(usage.total_input_tokens, 170)
        self.assertEqual(usage.total_output_tokens, 70)

    def test_unknown_tokens_stay_unknown(self):
        usage = BudgetUsage()
        arch = InvocationResult(InvocationStatus.SUCCESS,
                                output=json.dumps(arch_pkt().__dict__),
                                trace=trace_with_tokens("unknown", "unknown"))
        coder = InvocationResult(InvocationStatus.SUCCESS,
                                 output=json.dumps(impl_pkt().__dict__),
                                 trace=trace_with_tokens("unknown", "unknown"))
        outcome = self.run_session(usage, arch, coder)
        self.assertEqual(outcome.status.value, "SUCCESS", outcome.status)
        self.assertEqual(usage.total_input_tokens, "unknown")
        self.assertEqual(usage.total_output_tokens, "unknown")


class VerificationUsagePropagationTests(unittest.TestCase):
    """FOUR_STAGE tail: tester + reviewer usage reaches the same usage."""

    def run_verification(self, usage, tester_result, reviewer_result):
        from verification_collaboration import VerificationCollaboration
        state = SharedCollaborationState()
        for envelope, direction in (
            (CollaborationPacket(
                correlation_id="C1", task_id="T1", source_agent=ARCH,
                target_agent=CODER, source_role="architect",
                target_role="coder", payload_type=CollaborationPayloadType.ARCHITECTURE,
                payload=arch_pkt()), "REQUEST"),
            (CollaborationPacket(
                correlation_id="C1", task_id="T1", source_agent=CODER,
                target_agent=ARCH, source_role="coder",
                target_role="architect",
                payload_type=CollaborationPayloadType.IMPLEMENTATION,
                payload=impl_pkt()), "REPLY"),
        ):
            state = state.append_envelope("T1", envelope, direction, "DELIVERED")
        adapters = {
            TESTER: RecordingAdapter({"tester": tester_result}),
            REVIEWER: RecordingAdapter({"reviewer": reviewer_result}),
        }
        budget = TaskBudget(8, 8, timeout_seconds=30.0)
        verification = VerificationCollaboration(
            adapters, budget, usage, LoopGuard(), state=state)
        return verification.run("T1", TESTER, REVIEWER, ARCH, "OFFLINE")

    def test_observed_tokens_reach_usage(self):
        usage = BudgetUsage()
        tester = InvocationResult(InvocationStatus.SUCCESS,
                                  output=json.dumps(test_pkt().__dict__),
                                  trace=trace_with_tokens(50, 20))
        reviewer = InvocationResult(InvocationStatus.SUCCESS,
                                    output=json.dumps(review_pkt().__dict__),
                                    trace=trace_with_tokens(60, 25))
        outcome = self.run_verification(usage, tester, reviewer)
        self.assertEqual(outcome.status.value, "SUCCESS", outcome.status)
        self.assertEqual(usage.total_input_tokens, 110)
        self.assertEqual(usage.total_output_tokens, 45)

    def test_unknown_tokens_stay_unknown(self):
        usage = BudgetUsage()
        tester = InvocationResult(InvocationStatus.SUCCESS,
                                  output=json.dumps(test_pkt().__dict__),
                                  trace=trace_with_tokens("unknown", "unknown"))
        reviewer = InvocationResult(InvocationStatus.SUCCESS,
                                    output=json.dumps(review_pkt().__dict__),
                                    trace=trace_with_tokens("unknown", "unknown"))
        outcome = self.run_verification(usage, tester, reviewer)
        self.assertEqual(outcome.status.value, "SUCCESS", outcome.status)
        self.assertEqual(usage.total_input_tokens, "unknown")
        self.assertEqual(usage.total_output_tokens, "unknown")


class ExecutionEngineUsagePropagationTests(unittest.TestCase):
    """SINGLE path: stage usage reaches the engine's shared usage."""

    def test_observed_tokens_reach_usage(self):
        from execution_engine import ExecutionEngine, ExecutionStatus
        from invocation_plan import InvocationPlan, StagePlan
        from runtime_status import (
            HealthEvidence, ReasonCode, RuntimeState, RuntimeStatus,
        )
        from fallback_policy import FallbackPolicy
        from capability_registry import (
            AgentProfile, CapabilityConfidence, CapabilityEvidence, CapabilityName,
        )
        from structured_packets import ImplementationPacket as Impl
        from unittest.mock import Mock

        runtime = RuntimeStatus(
            "a", "a.exe", "1", RuntimeState.READY, "provider", "model",
            "managed", ReasonCode.NONE,
            HealthEvidence("v", "v", "v", "v", "v"), 1, 100)
        profile = AgentProfile(
            "a", "a", "provider", "model", "coder",
            {CapabilityName.CODING: CapabilityEvidence(
                CapabilityName.CODING, .9, CapabilityConfidence.VERIFIED, "t")},
            .8)
        adapter = Mock()
        adapter.invoke.return_value = InvocationResult(
            InvocationStatus.SUCCESS,
            output=Impl("task-1", "coder", ("file.py",), "summary", (), (), (), ()),
            trace=trace_with_tokens(90, 55))
        usage = BudgetUsage()
        engine = ExecutionEngine(
            adapters={"a": adapter}, runtimes={"a": runtime},
            budget=TaskBudget(2, 2), usage=usage, loop_guard=LoopGuard(),
            fallback=FallbackPolicy([profile]))
        plan = InvocationPlan("task-1", "ON", "SIMPLE",
                              (StagePlan("coder", "coder", "a", ("coding",), "selected"),),
                              ("a",), (), {}, ())
        result = engine.execute(plan, prompt="Return OK")
        self.assertEqual(result.status, ExecutionStatus.SUCCESS)
        self.assertEqual(usage.total_input_tokens, 90)
        self.assertEqual(usage.total_output_tokens, 55)

    def test_unknown_tokens_stay_unknown(self):
        from execution_engine import ExecutionEngine
        from invocation_plan import InvocationPlan, StagePlan
        from runtime_status import (
            HealthEvidence, ReasonCode, RuntimeState, RuntimeStatus,
        )
        from fallback_policy import FallbackPolicy
        from capability_registry import (
            AgentProfile, CapabilityConfidence, CapabilityEvidence, CapabilityName,
        )
        from structured_packets import ImplementationPacket as Impl
        from unittest.mock import Mock

        runtime = RuntimeStatus(
            "a", "a.exe", "1", RuntimeState.READY, "provider", "model",
            "managed", ReasonCode.NONE,
            HealthEvidence("v", "v", "v", "v", "v"), 1, 100)
        profile = AgentProfile(
            "a", "a", "provider", "model", "coder",
            {CapabilityName.CODING: CapabilityEvidence(
                CapabilityName.CODING, .9, CapabilityConfidence.VERIFIED, "t")},
            .8)
        adapter = Mock()
        adapter.invoke.return_value = InvocationResult(
            InvocationStatus.SUCCESS,
            output=Impl("task-1", "coder", ("file.py",), "summary", (), (), (), ()),
            trace=trace_with_tokens("unknown", "unknown"))
        usage = BudgetUsage()
        engine = ExecutionEngine(
            adapters={"a": adapter}, runtimes={"a": runtime},
            budget=TaskBudget(2, 2), usage=usage, loop_guard=LoopGuard(),
            fallback=FallbackPolicy([profile]))
        plan = InvocationPlan("task-1", "ON", "SIMPLE",
                              (StagePlan("coder", "coder", "a", ("coding",), "selected"),),
                              ("a",), (), {}, ())
        result = engine.execute(plan, prompt="Return OK")
        self.assertEqual(usage.total_input_tokens, "unknown")
        self.assertEqual(usage.total_output_tokens, "unknown")


if __name__ == "__main__":
    unittest.main()
