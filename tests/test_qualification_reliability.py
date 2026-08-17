"""Phase G15: qualification reliability hardening contracts.

Three layers: (B) content-safety semantic split — credential-shape in prose
values, marker-substring in structural keys — fixing the PROVEN false
positive ("must not write to stdout" constraint) without weakening any
true positive; (C) parser format contracts (fenced/whitespace/prose);
(D) G5 exception_type observability; (E) qualification reliability
semantics (failure never yields evidence; qualification ≠ stability).
"""
import re
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from candidate_validation import (
    CandidateRuntimeInstance,
    CandidateValidationStatus,
    GateResult,
    GateVerdict,
    ValidationGate,
)
from content_safety import REDACTED_ERROR, contains_unsafe_content, sanitize_trace
from external_runtime import InvocationResult, InvocationStatus, InvocationTrace
from real_validation_executor import RealGateExecutor, run_real_validation

OPEN_ENV = {"RUN_REAL_PROVIDER_TESTS": "1"}
SECRET_MARKERS = ("token", "secret", "api_key", "authorization", "bearer", "stdout", "stderr")

ARCH = {"task_id": "capability-evidence", "role": "architect", "goal": ["g"],
        "constraints": ["must not write to stdout during tests"],
        "architecture": ["a"], "interfaces": [{}], "implementation_steps": [{}],
        "acceptance_criteria": ["the parser tokenizes input once"],
        "risks": [{"risk": "none", "mitigation": "no shell side effects"}]}


class ContentSafetySemanticsTests(unittest.TestCase):
    """Task B: the semantic split. Historical REAL evidence (G14-D run 2):
    a valid JSON architect packet was rejected SOLELY because a constraint
    value contained the word 'stdout' — bare substring scan over prose."""

    def test_legitimate_technical_prose_is_not_rejected(self):
        self.assertFalse(contains_unsafe_content(
            "must not write to stdout during tests"))
        self.assertFalse(contains_unsafe_content(
            "the parser tokenizes input once"))

    def test_credential_shaped_values_are_still_rejected(self):
        for bad in ("api_key=abc123", "token: deadbeef", "secret='x'",
                    "authorization: Bearer abc", "bearer abc123",
                    "password=hunter2", "sk-abc123token"):
            with self.subTest(bad=bad):
                self.assertTrue(contains_unsafe_content(bad), bad)

    def test_structural_keys_stay_substring_strict(self):
        # Raw process-output dumps arrive as dict keys: {"stdout": "<dump>"}.
        self.assertTrue(contains_unsafe_content({"stdout": "raw"}))
        self.assertTrue(contains_unsafe_content({"stderr": "anything"}))
        self.assertTrue(contains_unsafe_content({"api_key": "redacted"}))

    def test_nested_dump_keys_in_packets_are_rejected(self):
        # The tester failure-dict carrier: [ {"stdout": "raw"} ]
        self.assertTrue(contains_unsafe_content([{"stdout": "raw"}]))
        self.assertTrue(contains_unsafe_content(
            {"failures": ({"stderr": "dump"},)}))

    def test_secret_values_inside_structural_keys_still_rejected(self):
        self.assertTrue(contains_unsafe_content({"note": "token=abc"}))
        self.assertTrue(contains_unsafe_content(["api_key: 123"]))

    def test_valid_architect_packet_with_technical_prose_passes(self):
        # End-to-end: the exact false-positive shape from G14-D run 2.
        from collaboration_session import _packet_from_output
        from structured_packets import ArchitecturePacket
        import json
        packet = _packet_from_output(json.dumps(ARCH), ArchitecturePacket,
                                     "capability-evidence")
        self.assertIsNotNone(packet)
        self.assertIn("must not write to stdout during tests", packet.constraints)

    def test_no_marker_was_removed_from_the_scanner_vocabulary(self):
        import content_safety
        self.assertEqual(content_safety.SECRET_MARKERS, SECRET_MARKERS)

    def test_sanitize_trace_still_redacts_real_dumps(self):
        trace = InvocationTrace(
            invocation_id="i", task_id="t", agent_id="a", runtime="r",
            provider=None, model=None, role=None, status=InvocationStatus.FAILED,
            error="stdout: api_key=abc dump")
        sanitized = sanitize_trace(trace)
        self.assertEqual(sanitized.error, REDACTED_ERROR)


class ParserFormatContractTests(unittest.TestCase):
    """Task C: does the G14 parser have a real format defect?"""

    def _parse(self, output):
        import json
        from collaboration_session import _packet_from_output
        from structured_packets import ImplementationPacket
        base = {"task_id": "capability-evidence", "role": "coder",
                "changed_files": ["f"], "implementation_summary": "s",
                "implementation_details": ["d"], "assumptions": [],
                "unresolved_items": [], "test_requirements": ["t"]}
        text = output if isinstance(output, str) else json.dumps(base)
        return _packet_from_output(text, ImplementationPacket,
                                  "capability-evidence")

    def test_plain_json_is_accepted(self):
        import json
        payload = {"task_id": "x", "role": "coder", "changed_files": ["f"],
                   "implementation_summary": "s", "implementation_details": ["d"],
                   "assumptions": [], "unresolved_items": [],
                   "test_requirements": ["t"]}
        self.assertIsNotNone(self._parse(json.dumps(payload)))

    def test_fenced_json_is_accepted(self):
        import json
        payload = {"task_id": "x", "role": "coder", "changed_files": ["f"],
                   "implementation_summary": "s", "implementation_details": ["d"],
                   "assumptions": [], "unresolved_items": [],
                   "test_requirements": ["t"]}
        self.assertIsNotNone(self._parse(
            "```json\n" + json.dumps(payload) + "\n```"))

    def test_whitespace_variation_is_accepted(self):
        import json
        payload = {"task_id": "x", "role": "coder", "changed_files": ["f"],
                   "implementation_summary": "s", "implementation_details": ["d"],
                   "assumptions": [], "unresolved_items": [],
                   "test_requirements": ["t"]}
        self.assertIsNotNone(self._parse("   \n" + json.dumps(payload) + "\n  "))

    def test_prose_is_rejected(self):
        self.assertIsNone(self._parse("Here is my plan: do the thing."))

    def test_malformed_json_is_rejected(self):
        self.assertIsNone(self._parse('{"role": "coder", '))

    def test_non_dict_json_is_rejected(self):
        self.assertIsNone(self._parse("[1, 2, 3]"))


class MinimalAdapter:
    def discover(self):
        from external_runtime import RuntimeDiscovery
        return RuntimeDiscovery("rt-x", True, "1.0", None, frozenset())

    def check_authentication(self):
        from runtime_health import AuthenticationCheck
        from runtime_status import AuthenticationState
        return AuthenticationCheck(AuthenticationState.AUTHENTICATED, "oauth")

    def check_provider_model(self):
        from runtime_health import ProviderModelCheck
        from runtime_status import ReasonCode
        return ProviderModelCheck("p", None, True, ReasonCode.NONE)

    def cancel(self, invocation_id):
        return InvocationResult(InvocationStatus.CANCELLED)


class G5ObservabilityTests(unittest.TestCase):
    """Task D: G5's executor-raise path must keep the exception TYPE (never
    the message) — mirroring the G14 observability contract."""

    def test_g5_adapter_exception_records_type_not_message(self):
        class RaisingAdapter(MinimalAdapter):
            def invoke(self, request):
                raise RuntimeError("secret in message must never leak")

        executor = RealGateExecutor(RaisingAdapter(), env=OPEN_ENV)
        g5 = executor._gate_minimal_invocation(ValidationGate.G5_MINIMAL_INVOCATION)
        self.assertEqual(g5.verdict.value, "FAILED")
        self.assertEqual(g5.evidence.get("exception_type"), "RuntimeError")
        self.assertNotIn("secret in message", repr(g5.evidence).lower())
        self.assertNotIn("message", g5.evidence)


class QualificationReliabilityContractTests(unittest.TestCase):
    """Task E: failure never fabricates evidence; qualification reliability
    is a separate axis from execution stability."""

    def test_failure_result_never_carries_capabilities(self):
        # Structurally: the runner only assigns validated_capabilities when
        # status is VERIFIED; lock it.
        import inspect
        import candidate_validation
        source = inspect.getsource(candidate_validation)
        self.assertIn(
            "if status is CandidateValidationStatus.VERIFIED else ()", source)

    def test_facade_and_stability_modules_never_run_qualification(self):
        import production_facade, collaboration_orchestrator, host
        for module in (production_facade, collaboration_orchestrator, host):
            source = Path(module.__file__).read_text(encoding="utf-8")
            self.assertNotIn("run_real_validation", source)
            self.assertNotIn("RealGateExecutor", source)


if __name__ == "__main__":
    unittest.main()
