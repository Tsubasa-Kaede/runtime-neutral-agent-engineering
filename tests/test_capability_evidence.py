"""Runtime Capability Evidence Gate: G14 real capability experiments.

Capabilities may only be produced by real, successful, gated runtime
invocations that parse into valid role packets through the existing
normalization and content-safety boundaries. Offline runs never produce REAL
evidence; provenance carries the distinction.
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
    GateResult,
    GateVerdict,
    ValidationGate,
)
from external_runtime import InvocationResult, InvocationStatus, InvocationTrace
from real_validation_executor import RealGateExecutor, run_real_validation

OPEN_ENV = {"RUN_REAL_PROVIDER_TESTS": "1"}
CAPS_ALL = ("architecture", "coding", "review", "testing")  # runner sorts alphabetically
SECRET_MARKERS = ("token", "secret", "api_key", "authorization", "bearer", "stdout", "stderr")


def instance():
    return CandidateRuntimeInstance(
        runtime_id="rt-x", provider_id="p-x", model_id=None,
        config_fingerprint="fp-x", capability_context=(), probe=None,
        invocation_spec={"timeout_seconds": 60})


def trace(status=InvocationStatus.SUCCESS, exit_code=0):
    return InvocationTrace(
        invocation_id="inv-cap", task_id="t", agent_id="a", runtime="rt",
        provider=None, model=None, role=None, status=status,
        started_at=1.0, finished_at=2.0, duration_ms=5, exit_code=exit_code,
        input_tokens="unknown", output_tokens="unknown", error=None)


ARCH = {"task_id": "t", "role": "architect", "goal": ["g"], "constraints": ["c"],
        "architecture": ["a"], "interfaces": [{}], "implementation_steps": [{}],
        "acceptance_criteria": ["ac"], "risks": [{}]}
IMPL = {"task_id": "t", "role": "coder", "changed_files": ["f"],
        "implementation_summary": "s", "implementation_details": ["d"],
        "assumptions": [], "unresolved_items": [], "test_requirements": ["tr"]}
TESTP = {"task_id": "t", "role": "tester", "tests_run": ["x"], "tests_passed": ["x"],
         "tests_failed": [], "failures": [], "coverage_or_validation": [],
         "remaining_risks": []}
REVIEW = {"task_id": "t", "role": "reviewer", "status": "PASS", "findings": [],
          "severity": [], "affected_files": [], "required_changes": [],
          "acceptance_criteria_status": []}
ROLE_PACKETS = {"architect": ARCH, "coder": IMPL, "tester": TESTP, "reviewer": REVIEW}


class RoleAdapter:
    """Returns a valid packet for each role; records every request."""

    def __init__(self, overrides=None, failing_roles=(), unsafe_roles=()):
        self.overrides = overrides or {}
        self.failing_roles = set(failing_roles)
        self.unsafe_roles = set(unsafe_roles)
        self.requests = []

    def discover(self):
        from external_runtime import RuntimeDiscovery
        return RuntimeDiscovery("rt-x", True, "1.0", None, frozenset())

    def check_authentication(self):
        from runtime_health import AuthenticationCheck
        from runtime_status import AuthenticationState, ReasonCode
        return AuthenticationCheck(AuthenticationState.AUTHENTICATED, "oauth_token")

    def check_provider_model(self):
        from runtime_health import ProviderModelCheck
        from runtime_status import ReasonCode
        return ProviderModelCheck("p-x", None, True, ReasonCode.NONE)

    def cancel(self, invocation_id):
        return InvocationResult(InvocationStatus.CANCELLED)

    def invoke(self, request):
        self.requests.append(request)
        if request.agent_id == "real-validation":  # the G5 minimal invocation
            return InvocationResult(InvocationStatus.SUCCESS, output="OK", trace=trace())
        if request.agent_id in self.failing_roles:
            return InvocationResult(InvocationStatus.FAILED, error="boom",
                                    trace=trace(InvocationStatus.FAILED, 1))
        payload = dict(ROLE_PACKETS.get(request.agent_id, {"ok": True}))
        payload.update(self.overrides.get(request.agent_id, {}))
        if request.agent_id in self.unsafe_roles:
            payload["implementation_summary"] = "stderr leak" if request.agent_id == "coder" else payload.get("implementation_summary", "x")
            if request.agent_id == "tester":
                payload["failures"] = [{"stdout": "raw"}]
        return InvocationResult(InvocationStatus.SUCCESS,
                                output=json.dumps(payload), trace=trace())


def run_open(adapter):
    return run_real_validation(instance(), adapter, env=OPEN_ENV,
                               timeout_seconds=30.0, experiment_id="exp-caps")


class GateStructureTests(unittest.TestCase):
    def test_g14_exists_in_gate_enum(self):
        self.assertIn("G14_CAPABILITY_EVIDENCE", [g.name for g in ValidationGate])

    def test_runner_still_executes_all_gates_in_order(self):
        seen = []
        runner = CandidateValidationRunner()

        def executor(gate):
            seen.append(int(gate))
            return GateResult(gate, GateVerdict.PASS,
                              capabilities=CAPS_ALL if gate.value == 14 else ())

        result = runner.run(instance(), executor, clock=lambda: 1.0,
                            experiment_id="e", provenance="REAL",
                            real_invocation=True)
        self.assertEqual(seen, list(range(1, 15)))
        self.assertEqual(result.status, CandidateValidationStatus.VERIFIED)
        self.assertEqual(result.validated_capabilities, CAPS_ALL)


class CapabilityEvidenceTests(unittest.TestCase):
    def test_gate_closed_never_reaches_capability_gate(self):
        adapter = RoleAdapter()
        result, executor = run_real_validation(
            instance(), adapter, env={}, timeout_seconds=30.0)
        self.assertEqual(result.status, CandidateValidationStatus.BLOCKED)
        self.assertEqual(result.validated_capabilities, ())
        self.assertEqual(adapter.requests, [])  # zero invocations when closed

    def test_open_gate_with_all_role_packets_yields_four_capabilities(self):
        adapter = RoleAdapter()
        result, executor = run_open(adapter)
        self.assertEqual(result.status, CandidateValidationStatus.VERIFIED)
        self.assertEqual(result.provenance, "REAL")
        self.assertEqual(result.validated_capabilities, CAPS_ALL)
        # 1 minimal-invocation + 4 role experiments
        self.assertEqual(executor.invocation_count, 5)
        self.assertEqual(len(adapter.requests), 5)
        roles = [r.agent_id for r in adapter.requests[1:]]
        self.assertEqual(sorted(roles), ["architect", "coder", "reviewer", "tester"])

    def test_failing_role_experiment_blocks_verification(self):
        adapter = RoleAdapter(failing_roles=("tester",))
        result, executor = run_open(adapter)
        self.assertEqual(result.status, CandidateValidationStatus.FAILED)
        self.assertEqual(result.validated_capabilities, ())
        self.assertIn("CAPABILITY_EXPERIMENT_FAILED", (result.failure_point[1] if result.failure_point else ""))

    def test_invalid_role_output_blocks_verification(self):
        adapter = RoleAdapter(overrides={"reviewer": {"role": "wrong"}})
        result, _ = run_open(adapter)
        self.assertEqual(result.status, CandidateValidationStatus.FAILED)
        self.assertEqual(result.validated_capabilities, ())

    def test_unsafe_role_output_blocks_verification(self):
        adapter = RoleAdapter(unsafe_roles=("tester",))
        result, _ = run_open(adapter)
        self.assertEqual(result.status, CandidateValidationStatus.FAILED)
        self.assertEqual(result.validated_capabilities, ())

    def test_capability_evidence_is_secret_free(self):
        adapter = RoleAdapter()
        result, executor = run_open(adapter)
        surface = repr(result).lower()
        for marker in SECRET_MARKERS:
            self.assertNotIn(marker, surface)
        g14 = [g for g in result.gate_results if g.gate.value == 14][0]
        self.assertEqual(g14.capabilities, CAPS_ALL)
        self.assertIn("invocation_ids", g14.evidence)

    def test_capabilities_are_consumable_by_selection_bridge(self):
        from runtime_status import HealthEvidence, ReasonCode, RuntimeState, RuntimeStatus
        from verified_runtime_pool import VerifiedRuntimePool
        from verified_selection_bridge import VerifiedSelectionBridge

        adapter = RoleAdapter()
        result, _ = run_open(adapter)
        pool = VerifiedRuntimePool(clock=lambda: 1.0)
        outcome = pool.admit(result, CAPS_ALL, health_now="READY")
        self.assertEqual(outcome.kind.value, "ACCEPTED")
        health = {result.identity[0]: RuntimeStatus(
            runtime_id=result.identity[0], executable="e", version="1",
            status=RuntimeState.READY, provider="p", model=None, auth_method=None,
            reason_code=ReasonCode.NONE,
            evidence=HealthEvidence("d", "a", "p", "m", "ok"),
            checked_at=0.0, expires_at=1.0)}
        for role, required in (("architect", ("architecture",)),
                               ("coder", ("coding",)),
                               ("tester", ("testing",)),
                               ("reviewer", ("review",))):
            with self.subTest(role=role):
                candidates = VerifiedSelectionBridge().candidates_for(
                    pool, health, role, required).candidates
                self.assertEqual(len(candidates), 1)


class RealCapabilityExperimentTests(unittest.TestCase):
    """One sanctioned REAL validation (5 invocations) proving REAL caps."""

    def setUp(self):
        import os
        if os.environ.get("RUN_REAL_PROVIDER_TESTS", "") != "1":
            self.skipTest("RUN_REAL_PROVIDER_TESTS != 1")

    def test_real_claude_capability_evidence(self):
        from claude_code_adapter import ClaudeCodeAdapter
        adapter = ClaudeCodeAdapter.from_environment()
        if adapter is None:
            self.skipTest("claude executable not found")
        result, executor = run_real_validation(
            instance_real(), adapter, timeout_seconds=90.0)
        print("REAL_CAPS:", result.validated_capabilities,
              "PROVENANCE:", result.provenance,
              "STATUS:", result.status.value)
        self.assertEqual(result.status, CandidateValidationStatus.VERIFIED)
        self.assertEqual(result.provenance, "REAL")
        self.assertEqual(result.validated_capabilities, CAPS_ALL)
        self.assertEqual(executor.invocation_count, 5)


def instance_real():
    return CandidateRuntimeInstance(
        runtime_id="claude-cli", provider_id="anthropic", model_id=None,
        config_fingerprint="fp-real-caps", capability_context=(), probe=None,
        invocation_spec={"timeout_seconds": 90})


if __name__ == "__main__":
    unittest.main()
