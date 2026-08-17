"""Phase G14-E: architect experiment output robustness contract.

Locks that the G14 architect experiment prompt carries an explicit JSON-only
+ array-type + marker-word-avoidance contract (mirroring the proven
tester/reviewer suffixes), that the base production instruction and Packet
schema stay untouched, that non-JSON still fails, and that G5 failures stay
independent of capability experiments. Measurement of a semantic gap in the
content scan is documented, not fixed here.
"""
import json
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from candidate_validation import (
    CandidateRuntimeInstance,
    CandidateValidationStatus,
    ValidationGate,
)
from collaboration_session import ARCHITECT_INSTRUCTION
from external_runtime import InvocationResult, InvocationStatus, InvocationTrace
from real_validation_executor import RealGateExecutor, run_real_validation
from structured_packets import ArchitecturePacket, ImplementationPacket, TestPacket, ReviewPacket

OPEN_ENV = {"RUN_REAL_PROVIDER_TESTS": "1"}
SECRET_MARKERS = ("token", "secret", "api_key", "authorization", "bearer", "stdout", "stderr")
CAPS_ALL = ("architecture", "coding", "review", "testing")

ARCH = {"task_id": "capability-evidence", "role": "architect", "goal": ["g"],
        "constraints": ["c"], "architecture": ["a"], "interfaces": [{}],
        "implementation_steps": [{}], "acceptance_criteria": ["ac"], "risks": [{}]}
IMPL = {"task_id": "capability-evidence", "role": "coder", "changed_files": ["f"],
        "implementation_summary": "s", "implementation_details": ["d"],
        "assumptions": [], "unresolved_items": [], "test_requirements": ["tr"]}
TESTP = {"task_id": "capability-evidence", "role": "tester", "tests_run": ["x"],
         "tests_passed": ["x"], "tests_failed": [], "failures": [],
         "coverage_or_validation": [], "remaining_risks": []}
REVIEW = {"task_id": "capability-evidence", "role": "reviewer", "status": "PASS",
          "findings": [], "severity": [], "affected_files": [],
          "required_changes": [], "acceptance_criteria_status": []}
ROLE_PACKETS = {"architect": ARCH, "coder": IMPL, "tester": TESTP, "reviewer": REVIEW}


def instance():
    return CandidateRuntimeInstance(
        runtime_id="rt-x", provider_id="p-x", model_id=None,
        config_fingerprint="fp-x", capability_context=(), probe=None,
        invocation_spec={"timeout_seconds": 60})


def trace(status=InvocationStatus.SUCCESS, exit_code=0):
    return InvocationTrace(
        invocation_id="inv-e", task_id="capability-evidence", agent_id="a",
        runtime="rt", provider=None, model=None, role=None, status=status,
        started_at=1.0, finished_at=2.0, duration_ms=5, exit_code=exit_code,
        input_tokens="unknown", output_tokens="unknown", error=None)


class RoleAdapter:
    def __init__(self, bad_roles=(), raw_output_roles=(), unsafe_roles=(),
                 minimal_failure=False):
        self.bad_roles = set(bad_roles)
        self.raw_output_roles = set(raw_output_roles)
        self.unsafe_roles = set(unsafe_roles)
        self.minimal_failure = minimal_failure

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
        return ProviderModelCheck("p-x", None, True, ReasonCode.NONE)

    def cancel(self, invocation_id):
        return InvocationResult(InvocationStatus.CANCELLED)

    def invoke(self, request):
        if request.agent_id == "real-validation":
            if self.minimal_failure:
                return InvocationResult(
                    InvocationStatus.FAILED, error="x",
                    trace=trace(InvocationStatus.FAILED, 1))
            return InvocationResult(InvocationStatus.SUCCESS, output="OK", trace=trace())
        role = request.agent_id
        if role in self.raw_output_roles:
            return InvocationResult(InvocationStatus.SUCCESS,
                                    output="Here is my design: ...", trace=trace())
        payload = dict(ROLE_PACKETS[role])
        if role in self.bad_roles:
            payload["goal"] = "not-an-array"
        if role in self.unsafe_roles:
            payload["constraints"] = ["leaked api_key=abc123 in logs"]
        return InvocationResult(InvocationStatus.SUCCESS,
                                output=json.dumps(payload), trace=trace())


def run_open(adapter):
    result, executor = run_real_validation(
        instance(), adapter, env=OPEN_ENV, timeout_seconds=30.0,
        experiment_id="exp-g14e")
    g14 = next((g for g in result.gate_results
                if g.gate is ValidationGate.G14_CAPABILITY_EVIDENCE), None)
    return result, g14


class ArchitectPromptContractTests(unittest.TestCase):
    def test_architect_experiment_prompt_has_json_only_contract(self):
        executor = RealGateExecutor(RoleAdapter(), env=OPEN_ENV)
        architect_prompt = executor._capability_prompts()[0][3]
        # Single-object JSON boundary + explicit array rule (mirror the
        # proven tester/reviewer wording).
        self.assertIn("single JSON object", architect_prompt)
        self.assertIn("starts with { and ends with }", architect_prompt)
        self.assertIn("JSON array", architect_prompt)
        self.assertIn("[]", architect_prompt)
        self.assertIn("never a number or a bare string", architect_prompt)

    def test_architect_experiment_prompt_avoids_marker_words(self):
        # The scan itself is NOT relaxed; the model is simply told not to use
        # the marker words in legitimate prose so technical descriptions do
        # not trip the strict scan.
        executor = RealGateAdapter = RealGateExecutor(RoleAdapter(), env=OPEN_ENV)
        architect_prompt = RealGateAdapter._capability_prompts()[0][3]
        self.assertIn("Do not use the words", architect_prompt)

    def test_base_architect_instruction_is_untouched(self):
        # The protected production instruction keeps its exact literal shape.
        self.assertTrue(ARCHITECT_INSTRUCTION.startswith(
            "You are the architect for one small, read-only design task. "))
        self.assertIn("No prose, no markdown fences. ", ARCHITECT_INSTRUCTION)

    def test_packet_schema_is_untouched(self):
        self.assertEqual(ArchitecturePacket.REQUIRED_FIELDS, (
            "task_id", "role", "goal", "constraints", "architecture",
            "interfaces", "implementation_steps", "acceptance_criteria", "risks"))
        for cls in (ArchitecturePacket, ImplementationPacket, TestPacket, ReviewPacket):
            path = Path(sys.modules["structured_packets"].__file__)
            self.assertIn("REQUIRED_FIELDS: ClassVar",
                          path.read_text(encoding="utf-8"))


class RawParseContractTests(unittest.TestCase):
    def test_non_json_architect_output_still_fails(self):
        result, g14 = run_open(RoleAdapter(raw_output_roles=("architect",)))
        self.assertEqual(result.status, CandidateValidationStatus.FAILED)
        self.assertEqual(g14.evidence.get("failure_role"), "architect")
        self.assertEqual(g14.evidence.get("failure_category"), "PACKET_INVALID")
        self.assertEqual(g14.evidence.get("failure_detail"), "RAW_PARSE")
        self.assertEqual(result.validated_capabilities, ())

    def test_legal_json_reaches_verdict_through_existing_parser(self):
        result, g14 = run_open(RoleAdapter())
        self.assertEqual(result.status, CandidateValidationStatus.VERIFIED)
        self.assertEqual(result.validated_capabilities, CAPS_ALL)
        self.assertEqual(g14.verdict.value, "PASS")

    def test_raw_parse_failure_yields_no_capabilities(self):
        result, _ = run_open(RoleAdapter(raw_output_roles=("architect",)))
        self.assertEqual(result.validated_capabilities, ())
        self.assertFalse(result.status is CandidateValidationStatus.VERIFIED
                         and result.validated_capabilities)


class ContentSafetyContractTests(unittest.TestCase):
    def test_unsafe_architect_output_fails_with_safety_detail(self):
        result, g14 = run_open(RoleAdapter(unsafe_roles=("architect",)))
        self.assertEqual(result.status, CandidateValidationStatus.FAILED)
        self.assertEqual(g14.evidence.get("failure_role"), "architect")
        self.assertEqual(g14.evidence.get("failure_category"), "PACKET_INVALID")
        # A credential shape may be intercepted by the schema layer
        # (SCHEMA, structured_packets assignment pattern) or the
        # collaboration safety layer (CONTENT_SAFETY) — both rejections
        # are correct; the category must be PACKET_INVALID either way.
        self.assertIn(g14.evidence.get("failure_detail"),
                      ("SCHEMA", "CONTENT_SAFETY"))
        self.assertTrue((g14.evidence.get("shape") or {}).get("content_safety_hit")
                        or g14.evidence.get("failure_detail") == "SCHEMA")
        self.assertEqual(result.validated_capabilities, ())

    def test_safety_failure_evidence_is_structured_only(self):
        _, g14 = run_open(RoleAdapter(unsafe_roles=("architect",)))
        surface = repr(g14.evidence).lower()
        self.assertNotIn("must not write", surface)  # raw text never surfaces
        allowed = {"failure_role", "failure_category", "failure_detail",
                   "exception_type", "shape", "invocation_count", "roles",
                   "invocation_ids"}
        self.assertTrue(set(g14.evidence.keys()) <= allowed)

    def test_safety_scan_strictness_is_not_reduced(self):
        # The marker tuple in content_safety keeps every original marker.
        import content_safety
        self.assertEqual(content_safety.SECRET_MARKERS,
                         ("token", "secret", "api_key", "authorization",
                          "bearer", "stdout", "stderr"))

    def test_semantic_gap_is_resolved_not_documented(self):
        # G15 RESOLVED the CONTENT_SAFETY_SEMANTIC_GAP with a two-tier
        # split: credential SHAPES in prose values and marker words in
        # structural keys are rejected; legitimate technical prose that
        # merely MENTIONS a marker word ("must not write to stdout") is
        # accepted. This test pins the fixed semantics; the true-positive
        # side is covered by test_unsafe_architect_output_fails_with_safety_detail.
        import json as _json
        from collaboration_session import _packet_from_output
        from structured_packets import ArchitecturePacket
        payload = dict(ARCH)
        payload["constraints"] = ["must not write to stdout during tests"]
        packet = _packet_from_output(_json.dumps(payload), ArchitecturePacket,
                                     "capability-evidence")
        self.assertIsNotNone(packet)
        self.assertIn("must not write to stdout during tests", packet.constraints)


class G5IndependenceTests(unittest.TestCase):
    def test_g5_failure_short_circuits_before_any_experiment(self):
        result, g14 = run_open(RoleAdapter(minimal_failure=True))
        self.assertEqual(result.status, CandidateValidationStatus.FAILED)
        self.assertEqual(result.failure_point[0],
                         ValidationGate.G5_MINIMAL_INVOCATION)
        self.assertIsNone(g14)  # G14 never ran
        self.assertEqual(result.validated_capabilities, ())

    def test_g5_failure_is_not_a_packet_failure(self):
        result, _ = run_open(RoleAdapter(minimal_failure=True))
        self.assertIn("INVOCATION_FAILED", str(result.failure_point[1]))
        self.assertNotIn("PACKET", str(result.failure_point[1]))


if __name__ == "__main__":
    unittest.main()
