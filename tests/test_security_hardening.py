"""Phase 10H-J: V2 security hardening — regression tests for three gaps.

GAP-1: CollaborationSession must whole-packet scan architect/coder output.
GAP-4: raw stderr in InvocationTrace.error must not reach the public outcome.
GAP-5: provenance REAL must require real-invocation evidence, never a bare
caller string. All offline; no runtime invocation.
"""
import json
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from candidate_validation import (
    CandidateRuntimeInstance,
    CandidateValidationResult,
    CandidateValidationRunner,
    CandidateValidationStatus,
    GateResult,
    GateVerdict,
    ValidationGate,
)
from collaboration_session import CollaborationSession, CollaborationStatus, collab_agent_address
from external_runtime import InvocationResult, InvocationStatus, InvocationTrace
from loop_guard import LoopGuard
from remote_transport import LoopbackRemoteTransport
from task_budget import BudgetUsage, TaskBudget
from verified_selection_bridge import agent_id_for

IDENTITY = ("rt-x", "provider-x", "model-x", "fp-x")
ARCH_ADDR = collab_agent_address(IDENTITY, "architect")
CODER_ADDR = collab_agent_address(IDENTITY, "coder")

SECRET_MARKERS = ("token", "secret", "api_key", "authorization", "bearer", "stdout", "stderr")


def arch_dict(goal=("g",)):
    return {
        "task_id": "T1", "role": "architect", "goal": list(goal), "constraints": ["c"],
        "architecture": ["a"], "interfaces": [{}], "implementation_steps": [{}],
        "acceptance_criteria": ["ac"], "risks": [{}],
    }


def impl_dict():
    return {
        "task_id": "T1", "role": "coder", "changed_files": ["f.py"],
        "implementation_summary": "s", "implementation_details": ["d"],
        "assumptions": [], "unresolved_items": [], "test_requirements": ["tr"],
    }


def trace(status=InvocationStatus.SUCCESS, exit_code=0, error=None):
    return InvocationTrace(
        invocation_id="inv-1", task_id="T1", agent_id="a", runtime="rt",
        provider=None, model=None, role=None, status=status,
        started_at=1.0, finished_at=2.0, duration_ms=10,
        exit_code=exit_code, input_tokens="unknown", output_tokens="unknown",
        error=error)


class FakeAgentAdapter:
    def __init__(self, result):
        self.result = result
        self.requests = []

    def invoke(self, request):
        self.requests.append(request)
        return self.result


def ok(payload_dict):
    return InvocationResult(InvocationStatus.SUCCESS,
                            output=json.dumps(payload_dict), trace=trace())


def make_session(arch_result, coder_result=None):
    budget = TaskBudget(4, 4, timeout_seconds=30.0)
    usage = BudgetUsage()
    guard = LoopGuard()
    adapters = {
        ARCH_ADDR: FakeAgentAdapter(arch_result),
        CODER_ADDR: FakeAgentAdapter(coder_result if coder_result is not None else ok(impl_dict())),
    }
    return CollaborationSession(LoopbackRemoteTransport(), adapters, budget, usage, guard)


def run_session(arch_result, coder_result=None):
    session = make_session(arch_result, coder_result)
    outcome = session.run(task_id="T1", task="do the thing",
                          architect_address=ARCH_ADDR, coder_address=CODER_ADDR,
                          correlation_id="C1")
    return session, outcome


class Gap1SecurityAsymmetryTests(unittest.TestCase):
    def test_architect_packet_with_credential_shape_is_rejected(self):
        # G15 two-tier semantics: prose mentioning a marker word is fine, a
        # credential assignment in a value is what the whole-packet scan
        # exists to reject (the structured_packets cleaner pattern requires
        # an assignment shape for exactly these words).
        bad = arch_dict(goal=("leaked api_key=abc123 here",))
        _, outcome = run_session(ok(bad))
        self.assertEqual(outcome.status, CollaborationStatus.ARCHITECT_PACKET_INVALID)
        self.assertIsNone(outcome.request_envelope)

    def test_architect_packet_with_structural_dump_key_is_rejected(self):
        # A raw-dump carrier as an interface object key is still rejected.
        bad = arch_dict()
        bad["interfaces"] = [{"stdout": "raw process dump"}]
        _, outcome = run_session(ok(bad))
        self.assertEqual(outcome.status, CollaborationStatus.ARCHITECT_PACKET_INVALID)

    def test_architect_prose_mentioning_marker_word_is_accepted(self):
        # The historical false positive (G14-D run 2): a constraint that
        # merely says "must not write to stdout" is legitimate prose.
        clean = arch_dict(goal=("must not write to stdout during tests",))
        _, outcome = run_session(ok(clean))
        self.assertEqual(outcome.status, CollaborationStatus.SUCCESS)

    def test_coder_packet_with_credential_shape_is_rejected(self):
        bad_impl = impl_dict()
        bad_impl["implementation_summary"] = "leaked token=deadbeef to logs"
        _, outcome = run_session(ok(arch_dict()), ok(bad_impl))
        self.assertEqual(outcome.status, CollaborationStatus.CODER_PACKET_INVALID)
        self.assertIsNone(outcome.reply_envelope)

    def test_clean_packet_still_succeeds(self):
        _, outcome = run_session(ok(arch_dict()))
        self.assertEqual(outcome.status, CollaborationStatus.SUCCESS)


class Gap4RawStderrInTraceTests(unittest.TestCase):
    def test_failed_architect_trace_error_is_sanitized(self):
        fail = InvocationResult(
            InvocationStatus.FAILED, error="api_key=secret123 raw stderr",
            trace=trace(InvocationStatus.FAILED, exit_code=1, error="api_key=secret123 raw stderr"))
        _, outcome = run_session(fail)
        self.assertEqual(outcome.status, CollaborationStatus.ARCHITECT_INVOKE_FAILED)
        for trace_ in outcome.traces:
            error_text = (trace_.error or "").lower()
            self.assertNotIn("secret123", error_text)
            for marker in SECRET_MARKERS:
                self.assertNotIn(marker, error_text)

    def test_clean_trace_error_is_preserved(self):
        fail = InvocationResult(
            InvocationStatus.FAILED, error="external runtime failed",
            trace=trace(InvocationStatus.FAILED, exit_code=1, error="external runtime failed"))
        _, outcome = run_session(fail)
        self.assertTrue(any((t.error or "") == "external runtime failed" for t in outcome.traces))


class Gap5ProvenanceTrustHoleTests(unittest.TestCase):
    def setUp(self):
        self.instance = CandidateRuntimeInstance(
            runtime_id="rt-x", provider_id="provider-x", model_id=None,
            config_fingerprint="fp-x", capability_context=(), probe=None,
            invocation_spec={"timeout_seconds": 60})

    def pass_all(self, gate):
        return GateResult(gate, GateVerdict.PASS)

    def test_real_provenance_without_evidence_is_rejected(self):
        with self.assertRaises(ValueError):
            CandidateValidationRunner().run(
                self.instance, self.pass_all, clock=lambda: 1.0,
                experiment_id="exp", provenance="REAL")

    def test_real_provenance_with_evidence_is_allowed(self):
        result = CandidateValidationRunner().run(
            self.instance, self.pass_all, clock=lambda: 1.0,
            experiment_id="exp", provenance="REAL", real_invocation=True)
        self.assertEqual(result.provenance, "REAL")
        self.assertEqual(result.status, CandidateValidationStatus.VERIFIED)

    def test_offline_provenance_default_still_works(self):
        result = CandidateValidationRunner().run(
            self.instance, self.pass_all, clock=lambda: 1.0, experiment_id="exp")
        self.assertEqual(result.provenance, "OFFLINE")


class SourceScanTests(unittest.TestCase):
    def test_shared_scanner_is_runtime_neutral(self):
        import content_safety as module
        source = Path(module.__file__).read_text(encoding="utf-8")
        lowered = source.lower()
        for name in ("claude", "codex", "deepseek", "openai", "anthropic",
                     "gemini", "tiny-agents", "tiny_agents"):
            self.assertNotIn(name, lowered)
        for forbidden in ("os.environ", "getenv", "subprocess", "requests",
                          "urllib", "socket", "uuid", "random", "datetime",
                          "import time", "time.", "monotonic"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
