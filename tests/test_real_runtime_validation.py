"""Phase 10G-B: Real Runtime Validation — gate executor contract.

Offline contract tests run by default with mock adapters and the real gate
UNSET: the executor must BLOCK at G5, produce provenance="OFFLINE" and make
zero invocations. Gate-open failure paths are covered by calling the Runner
directly with provenance="OFFLINE" so no offline run can ever fabricate a
REAL label. The single real smoke test runs only under
RUN_REAL_PROVIDER_TESTS=1 with the real Claude Code adapter.
"""
import os
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
from external_runtime import (
    ExternalAgentRequest,
    InvocationResult,
    InvocationStatus,
    InvocationTrace,
)
from real_validation_executor import RealGateExecutor, run_real_validation
from runtime_health import AuthenticationCheck
from runtime_status import AuthenticationState, ReasonCode

MINIMAL_PROMPT = "Return exactly OK and nothing else."


class FakeAdapter:
    """Neutral fake with the full probe surface; counts invocations."""

    runtime_id = "rt-x"
    provider_id = "provider-x"
    model_id = None
    config_fingerprint = "fp-x"
    capability_context = ()
    probe = None
    invocation_spec = {"timeout_seconds": 60}

    def __init__(self, invocation_result=None, auth_state=AuthenticationState.AUTHENTICATED,
                 available=True):
        self.invocation_result = invocation_result or _ok_result()
        self.auth_state = auth_state
        self.available = available
        self.invoke_calls = []

    def discover(self):
        from external_runtime import RuntimeDiscovery
        return RuntimeDiscovery(self.runtime_id, self.available,
                                "1.0" if self.available else None,
                                None if self.available else "missing executable")

    def check_authentication(self):
        if self.auth_state is AuthenticationState.AUTHENTICATED:
            return AuthenticationCheck(self.auth_state, "oauth_token")
        return AuthenticationCheck(self.auth_state, reason_code=ReasonCode.AUTH_REQUIRED)

    def check_provider_model(self):
        from runtime_health import ProviderModelCheck
        return ProviderModelCheck(self.provider_id, self.model_id, True, ReasonCode.NONE)

    def invoke(self, request):
        self.invoke_calls.append(request)
        return self.invocation_result

    def cancel(self, invocation_id):
        return InvocationResult(InvocationStatus.CANCELLED)


def _trace(status=InvocationStatus.SUCCESS, exit_code=0, duration_ms=800):
    return InvocationTrace(
        invocation_id="inv-test-1", task_id="real-validation", agent_id="real-validation",
        runtime="rt-x", provider="provider-x", model=None, role=None, status=status,
        started_at=1.0, finished_at=2.0, duration_ms=duration_ms, exit_code=exit_code,
        input_tokens="unknown", output_tokens="unknown", error=None,
    )


def _ok_result():
    return InvocationResult(InvocationStatus.SUCCESS, output="OK", trace=_trace())


def _result(status=InvocationStatus.FAILED, output=None, error=None, exit_code=1,
            duration_ms=800):
    return InvocationResult(status, output=output, error=error, trace=_trace(status, exit_code, duration_ms))


def instance():
    return CandidateRuntimeInstance(
        runtime_id="rt-x", provider_id="provider-x", model_id=None,
        config_fingerprint="fp-x", capability_context=(), probe=None,
        invocation_spec={"timeout_seconds": 60},
    )


class Phase10GBGateOffTests(unittest.TestCase):
    """Default environment (gate UNSET): BLOCKED, OFFLINE, zero invocations."""

    def test_gate_off_blocks_at_g5_without_invocation(self):
        adapter = FakeAdapter()
        result, executor = run_real_validation(instance(), adapter, experiment_id="exp-off")
        self.assertEqual(result.status, CandidateValidationStatus.BLOCKED)
        self.assertIn("REAL_RUNTIME_GATE_NOT_ENABLED", result.block_reason)
        self.assertEqual(adapter.invoke_calls, [])
        self.assertEqual(executor.invocation_count, 0)

    def test_gate_off_result_is_offline_never_real(self):
        adapter = FakeAdapter()
        result, _ = run_real_validation(instance(), adapter)
        self.assertEqual(result.provenance, "OFFLINE")
        self.assertNotEqual(result.provenance, "REAL")

    def test_gate_off_still_probes_read_only_gates(self):
        adapter = FakeAdapter()
        result, _ = run_real_validation(instance(), adapter)
        self.assertIn(ValidationGate.G1_DISCOVERY, result.gates_passed)
        self.assertIn(ValidationGate.G4_MODEL, result.gates_passed)
        self.assertNotIn(ValidationGate.G5_MINIMAL_INVOCATION, result.gates_passed)


class Phase10GBFailurePathTests(unittest.TestCase):
    """Gate-open failure paths via direct Runner calls pinned to OFFLINE."""

    def run_with(self, adapter, **kwargs):
        executor = RealGateExecutor(adapter, env={"RUN_REAL_PROVIDER_TESTS": "1"})
        result = CandidateValidationRunner().run(
            instance(), executor, clock=lambda: 1.0,
            experiment_id="exp-fail", provenance="OFFLINE",
        )
        return result, executor

    def test_auth_required_blocks(self):
        result, _ = self.run_with(FakeAdapter(auth_state=AuthenticationState.AUTH_REQUIRED))
        self.assertEqual(result.status, CandidateValidationStatus.BLOCKED)
        self.assertIn("AUTH_REQUIRED", result.block_reason)

    def test_unavailable_discovery_blocks(self):
        result, _ = self.run_with(FakeAdapter(available=False))
        self.assertEqual(result.status, CandidateValidationStatus.BLOCKED)

    def test_failed_invocation_maps_to_invocation_failed(self):
        result, _ = self.run_with(FakeAdapter(invocation_result=_result(
            InvocationStatus.FAILED, error="runtime failed")))
        self.assertEqual(result.status, CandidateValidationStatus.FAILED)
        self.assertEqual(result.failure_point[0], ValidationGate.G5_MINIMAL_INVOCATION)
        self.assertIn("INVOCATION_FAILED", result.failure_point[1])

    def test_timeout_maps_to_timeout_category(self):
        result, _ = self.run_with(FakeAdapter(invocation_result=_result(
            InvocationStatus.TIMEOUT, error="external runtime timeout", exit_code=None,
            duration_ms=60000)))
        self.assertEqual(result.status, CandidateValidationStatus.FAILED)
        self.assertIn("TIMEOUT", result.failure_point[1])

    def test_unavailable_invocation_blocks_with_category(self):
        result, _ = self.run_with(FakeAdapter(invocation_result=_result(
            InvocationStatus.UNAVAILABLE, error="spawn failed")))
        self.assertEqual(result.status, CandidateValidationStatus.BLOCKED)
        self.assertIn("UNAVAILABLE", result.block_reason)

    def test_invalid_output_fails_at_structured_gate(self):
        bad = InvocationResult(InvocationStatus.SUCCESS, output="hello there", trace=_trace())
        result, _ = self.run_with(FakeAdapter(invocation_result=bad))
        self.assertEqual(result.status, CandidateValidationStatus.FAILED)
        self.assertEqual(result.failure_point[0], ValidationGate.G11_STRUCTURED_PACKET)
        self.assertIn("INVALID_OUTPUT", result.failure_point[1])

    def test_secret_shaped_error_is_detected_not_stored(self):
        # Success must reach G12, where the unsafe error surface is detected.
        leaky = InvocationResult(InvocationStatus.SUCCESS, output="OK",
                                 error="api_key=abc123 leaked", trace=_trace())
        result, _ = self.run_with(FakeAdapter(invocation_result=leaky))
        self.assertEqual(result.status, CandidateValidationStatus.FAILED)
        self.assertEqual(result.failure_point[0], ValidationGate.G12_SECURITY)
        # Marker-safe category inside guarded structures; equals the
        # SECRET_LEAK_DETECTED taxonomy entry at the reporting boundary.
        self.assertEqual(result.failure_point[1], "LEAK_DETECTED")
        surface = repr(result).lower()
        self.assertNotIn("abc123", surface)

    def test_config_mutation_fails_integrity_gate(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "config.toml"
            target.write_text("x = 1", encoding="utf-8")
            adapter = FakeAdapter()
            executor = RealGateExecutor(adapter, protected_paths=(target,),
                                        env={"RUN_REAL_PROVIDER_TESTS": "1"})
            # simulate an external mutation after the snapshot
            executor._snapshot_after = lambda: {target: (999, 999)}
            result = CandidateValidationRunner().run(
                instance(), executor, clock=lambda: 1.0,
                experiment_id="exp-mut", provenance="OFFLINE")
            self.assertEqual(result.status, CandidateValidationStatus.FAILED)
            self.assertEqual(result.failure_point[0], ValidationGate.G13_CONFIGURATION_INTEGRITY)


class Phase10GBOfflineVerifiedTests(unittest.TestCase):
    """Fake executor success stays OFFLINE — the §9 contract."""

    def test_fake_executor_verified_stays_offline(self):
        # Gate flag is injected into the executor only (no os.environ patch):
        # the helper's REAL branch is deliberately NOT exercised offline.
        executor = RealGateExecutor(FakeAdapter(), env={"RUN_REAL_PROVIDER_TESTS": "1"})
        result = CandidateValidationRunner().run(
            instance(), executor, clock=lambda: 1.0,
            experiment_id="exp-mock", provenance="OFFLINE")
        self.assertEqual(result.status, CandidateValidationStatus.VERIFIED)
        self.assertEqual(result.provenance, "OFFLINE")
        self.assertEqual(executor.invocation_count, 1)
        self.assertEqual(len(executor.adapter.invoke_calls), 1)
        self.assertEqual(executor.adapter.invoke_calls[0].prompt, MINIMAL_PROMPT)

    def test_real_provenance_literal_appears_only_in_gated_helper(self):
        import real_validation_executor as module
        source = Path(module.__file__).read_text(encoding="utf-8")
        self.assertEqual(source.count('provenance="REAL"'), 1)
        self.assertNotIn(".provenance =", source)

    def test_evidence_summary_is_secret_free_and_complete(self):
        adapter = FakeAdapter()
        _, executor = run_real_validation(instance(), adapter)
        report = executor.evidence_summary()
        for key in ("runtime_id", "provider_id", "model_id", "status", "exit_code",
                    "duration_ms", "success", "safe_output_summary", "failure_category",
                    "executed_at"):
            self.assertIn(key, report)
        surface = repr(report).lower()
        for marker in ("token", "secret", "api_key", "authorization", "stdout", "stderr"):
            self.assertNotIn(marker, surface)

    def test_module_is_runtime_neutral(self):
        import real_validation_executor as module
        text = Path(module.__file__).read_text(encoding="utf-8").lower()
        for name in ("claude", "codex", "gemini", "deepseek", "tiny-agents", "tiny_agents"):
            self.assertNotIn(name, text)

    def test_minimal_prompt_is_fixed_and_minimal(self):
        import real_validation_executor as module
        executor = RealGateExecutor(FakeAdapter(), env={"RUN_REAL_PROVIDER_TESTS": "1"})
        self.assertEqual(executor.minimal_prompt, MINIMAL_PROMPT)


class RealRuntimeSmokeTests(unittest.TestCase):
    """Single real runtime, single minimal invocation — opt-in only."""

    def setUp(self):
        if os.environ.get("RUN_REAL_PROVIDER_TESTS", "") != "1":
            self.skipTest("RUN_REAL_PROVIDER_TESTS != 1")

    def test_real_claude_validation_produces_real_provenance(self):
        from candidate_adapter_contract import candidate_from_adapter
        from claude_code_adapter import ClaudeCodeAdapter

        adapter = ClaudeCodeAdapter.from_environment()
        if adapter is None:
            self.skipTest("claude executable not found")

        class Candidate:
            runtime_id = "claude-cli"
            provider_id = "anthropic"
            model_id = None
            config_fingerprint = "claude-cli-real-smoke"
            capability_context = ()
            probe = adapter
            invocation_spec = {"timeout_seconds": 60}

        home = Path.home()
        protected = (
            home / ".claude" / ".credentials.json",
            home / ".claude.json",
            home / ".claude" / "settings.json",
            home / ".codex" / "auth.json",
            home / ".codex" / "config.toml",
        )
        result, executor = run_real_validation(
            candidate_from_adapter(Candidate()), adapter,
            agent_id="real-validation", timeout_seconds=60.0,
            protected_paths=protected, experiment_id="10g-b-real-smoke-1",
        )
        report = executor.evidence_summary()
        print("REAL_EVIDENCE:", report)
        self.assertEqual(result.status, CandidateValidationStatus.VERIFIED)
        self.assertEqual(result.provenance, "REAL")
        self.assertEqual(executor.invocation_count, 1)
        self.assertEqual(adapter._processes, {})
        self.assertEqual(report["exit_code"], 0)
        self.assertEqual(report["safe_output_summary"], "exact_ok")
        self.assertIs(report["success"], True)
        self.assertLess(report["duration_ms"], 60000)
        surface = repr(report).lower()
        for marker in ("token", "secret", "api_key", "authorization", "stdout", "stderr"):
            self.assertNotIn(marker, surface)


if __name__ == "__main__":
    unittest.main()
