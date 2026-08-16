"""Phase G14-B: capability-evidence failure observability contract.

G14 failures must record role, category, exception type (never the message)
and a safe shape diagnosis — finite enums only, no raw output anywhere.
Qualification semantics: one validated result can serve as the pool entry
for many facade tasks; stability of the four-stage chain is measured
separately from capability-experiment stability.
"""
import json
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from candidate_validation import (
    CandidateRuntimeInstance,
    CandidateValidationRunner,
    CandidateValidationStatus,
    GateVerdict,
    ValidationGate,
)
from external_runtime import InvocationResult, InvocationStatus, InvocationTrace
from real_validation_executor import RealGateExecutor, run_real_validation

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
        invocation_id="inv-g14", task_id="capability-evidence", agent_id="a",
        runtime="rt", provider=None, model=None, role=None, status=status,
        started_at=1.0, finished_at=2.0, duration_ms=5, exit_code=exit_code,
        input_tokens="unknown", output_tokens="unknown", error=None)


class RoleAdapter:
    """Green by default; per-role faults injectable."""

    def __init__(self, bad_roles=(), raising_roles=(), timeout_roles=(),
                 unsafe_roles=(), raw_output_role=None):
        self.bad_roles = set(bad_roles)
        self.raising_roles = set(raising_roles)
        self.timeout_roles = set(timeout_roles)
        self.unsafe_roles = set(unsafe_roles)
        self.raw_output_role = raw_output_role

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
            return InvocationResult(InvocationStatus.SUCCESS, output="OK", trace=trace())
        role = request.agent_id
        if role in self.raising_roles:
            raise TimeoutError(f"secret in message {role} should never leak")
        if role in self.timeout_roles:
            return InvocationResult(InvocationStatus.TIMEOUT, error="t",
                                    trace=trace(InvocationStatus.TIMEOUT, None))
        if self.raw_output_role and role in self.raw_output_role:
            return InvocationResult(InvocationStatus.SUCCESS,
                                    output="definitely not json", trace=trace())
        payload = dict(ROLE_PACKETS[role])
        if role in self.bad_roles:
            payload.pop("role")
            payload["changed_files"] = 3  # number where array expected
        if role in self.unsafe_roles:
            payload["failures"] = [{"stdout": "raw"}]
            payload["implementation_summary"] = "stderr leak"
        return InvocationResult(InvocationStatus.SUCCESS,
                                output=json.dumps(payload), trace=trace())


def run_open(adapter):
    result, executor = run_real_validation(
        instance(), adapter, env=OPEN_ENV, timeout_seconds=30.0,
        experiment_id="exp-g14b")
    g14 = next(g for g in result.gate_results
               if g.gate is ValidationGate.G14_CAPABILITY_EVIDENCE)
    return result, executor, g14


class G14ObservabilityTests(unittest.TestCase):
    def g14_of(self, adapter):
        result, _, g14 = run_open(adapter)
        return result, g14

    def test_green_pass_carries_capabilities_and_real(self):
        result, g14 = self.g14_of(RoleAdapter())
        self.assertEqual(result.status, CandidateValidationStatus.VERIFIED)
        self.assertEqual(result.provenance, "REAL")
        self.assertEqual(result.validated_capabilities, CAPS_ALL)
        self.assertEqual(g14.verdict, GateVerdict.PASS)

    def test_invalid_packet_failure_records_role_and_category(self):
        result, g14 = self.g14_of(RoleAdapter(bad_roles=("coder",)))
        self.assertEqual(result.status, CandidateValidationStatus.FAILED)
        self.assertEqual(g14.verdict, GateVerdict.FAILED)
        self.assertIn("coder", g14.reason)
        self.assertEqual(g14.evidence.get("failure_role"), "coder")
        self.assertEqual(g14.evidence.get("failure_category"), "PACKET_INVALID")
        self.assertEqual(result.validated_capabilities, ())

    def test_timeout_failure_records_category(self):
        result, g14 = self.g14_of(RoleAdapter(timeout_roles=("tester",)))
        self.assertEqual(result.status, CandidateValidationStatus.FAILED)
        self.assertEqual(g14.evidence.get("failure_role"), "tester")
        self.assertEqual(g14.evidence.get("failure_category"), "INVOCATION_FAILED")
        self.assertIn("TIMEOUT", g14.evidence.get("failure_detail", ""))

    def test_raising_adapter_records_exception_type_not_message(self):
        result, g14 = self.g14_of(RoleAdapter(raising_roles=("reviewer",)))
        self.assertEqual(result.status, CandidateValidationStatus.FAILED)
        self.assertEqual(g14.evidence.get("failure_role"), "reviewer")
        self.assertEqual(g14.evidence.get("failure_category"), "ADAPTER_EXCEPTION")
        self.assertEqual(g14.evidence.get("exception_type"), "TimeoutError")
        surface = repr(g14).lower()
        self.assertNotIn("secret in message", surface)  # message never leaks

    def test_unsafe_output_records_content_safety_category(self):
        result, g14 = self.g14_of(RoleAdapter(unsafe_roles=("tester",)))
        self.assertEqual(result.status, CandidateValidationStatus.FAILED)
        self.assertEqual(g14.evidence.get("failure_category"), "PACKET_INVALID")
        self.assertEqual(g14.evidence.get("failure_detail"), "CONTENT_SAFETY")

    def test_raw_parse_failure_records_shape_category(self):
        adapter = RoleAdapter()
        adapter.raw_output_role = ("coder",)
        result, g14 = self.g14_of(adapter)
        self.assertEqual(result.status, CandidateValidationStatus.FAILED)
        self.assertEqual(g14.evidence.get("failure_role"), "coder")
        self.assertEqual(g14.evidence.get("failure_category"), "PACKET_INVALID")
        self.assertEqual(g14.evidence.get("failure_detail"), "RAW_PARSE")

    def test_evidence_is_finite_enum_and_secret_free(self):
        _, g14 = self.g14_of(RoleAdapter(bad_roles=("architect",)))
        allowed = {"failure_role", "failure_category", "failure_detail",
                   "exception_type", "shape", "invocation_count", "roles",
                   "invocation_ids"}
        self.assertTrue(set(g14.evidence.keys()) <= allowed,
                        g14.evidence.keys())
        surface = repr(g14.evidence).lower()
        for marker in SECRET_MARKERS:
            self.assertNotIn(marker, surface)

    def test_failure_role_always_in_reason(self):
        for roles in (("architect",), ("coder",), ("tester",), ("reviewer",)):
            with self.subTest(roles=roles):
                _, g14 = self.g14_of(RoleAdapter(bad_roles=roles))
                self.assertIn(roles[0], g14.reason)
                self.assertEqual(g14.evidence.get("failure_role"), roles[0])


class QualificationSemanticsTests(unittest.TestCase):
    def test_one_validation_result_serves_many_bridge_queries(self):
        """Qualification: a single validated result admits the runtime to the
        pool; every role query reuses it without re-running experiments."""
        from runtime_status import (
            HealthEvidence, ReasonCode, RuntimeState, RuntimeStatus,
        )
        from verified_runtime_pool import VerifiedRuntimePool
        from verified_selection_bridge import VerifiedSelectionBridge

        adapter = RoleAdapter()
        calls = {"n": 0}
        original = adapter.invoke

        def counting(request):
            calls["n"] += 1
            return original(request)

        adapter.invoke = counting
        result, _, _ = run_open(adapter)
        self.assertEqual(result.status, CandidateValidationStatus.VERIFIED)
        experiments_after_validation = calls["n"]

        pool = VerifiedRuntimePool(clock=lambda: 0.0)
        pool.admit(result, CAPS_ALL, health_now="READY")
        health = {"rt-x": RuntimeStatus(
            runtime_id="rt-x", executable="e", version="1",
            status=RuntimeState.READY, provider="p", model=None,
            auth_method=None, reason_code=ReasonCode.NONE,
            evidence=HealthEvidence("d", "a", "p", "m", "ok"),
            checked_at=0.0, expires_at=1.0)}
        # Query the pool many times: zero additional experiments.
        for _ in range(5):
            for role, required in (("architect", ("architecture",)),
                                   ("coder", ("coding",)),
                                   ("tester", ("testing",)),
                                   ("reviewer", ("review",))):
                candidates = VerifiedSelectionBridge().candidates_for(
                    pool, health, role, required).candidates
                self.assertEqual(len(candidates), 1)
        self.assertEqual(calls["n"], experiments_after_validation)

    def test_facade_chain_does_not_rerun_g14_within_one_pool(self):
        """The facade consumes the already-admitted pool entry; capability
        experiments are a qualification step, not a per-task step."""
        # Structural contract: the orchestrator/facade only ever read the
        # pool (no run_real_validation import anywhere in their modules).
        import collaboration_orchestrator
        import production_facade
        for module in (collaboration_orchestrator, production_facade):
            source = Path(module.__file__).read_text(encoding="utf-8")
            self.assertNotIn("run_real_validation", source)
            self.assertNotIn("RealGateExecutor", source)


if __name__ == "__main__":
    unittest.main()
