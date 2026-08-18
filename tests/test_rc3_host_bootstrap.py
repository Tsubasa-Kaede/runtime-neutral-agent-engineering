"""RC-3 Task C: Host/CLI Discovery Bootstrap integration.

The manual injection path (build_facade(adapter, validation, health)) stays
untouched; a new automatic entry composes Registry -> bootstrap -> HostFacade
and the CLI keeps consuming whatever facade it is given. Routing stays in
ModeGate/CollaborationOrchestrator — bootstrap only answers WHO is usable.
"""
import json
import sys
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from candidate_validation import (
    CandidateRuntimeInstance,
    CandidateValidationResult,
    CandidateValidationStatus,
    GateResult,
    GateVerdict,
    ValidationGate,
)
from discovery_bootstrap import bootstrap_runtime_session
from external_runtime import RuntimeDiscovery
from host import build_facade, build_facade_from_bootstrap
from runtime_adapter_registry import AdapterDescriptor, AdapterRegistry
from runtime_status import (
    AuthenticationState,
    HealthEvidence,
    ReasonCode,
    RuntimeState,
    RuntimeStatus,
)
from mode_gate import Mode
from production_facade import ProductionFacade
from cli import run_cli

SECRET_MARKERS = ("token", "secret", "api_key", "authorization", "bearer", "stdout", "stderr")
CAPS_ALL = ("architecture", "coding", "review", "testing")


class OfflineAdapter:
    runtime_id = "rt-a"
    provider_id = "provider-a"

    def discover(self):
        return RuntimeDiscovery("rt-a", True, "1.0", None, frozenset())

    def check_authentication(self):
        from runtime_health import AuthenticationCheck
        return AuthenticationCheck(AuthenticationState.AUTHENTICATED, "oauth")

    def check_provider_model(self):
        from runtime_health import ProviderModelCheck
        return ProviderModelCheck("provider-a", None, True, ReasonCode.NONE)

    def minimal_health_check(self, timeout_seconds):
        from runtime_health import MinimalHealthCheck
        return MinimalHealthCheck(True, ReasonCode.NONE, output_class="exact_ok")

    def invoke(self, request):
        raise AssertionError("offline contract: no runtime invocation allowed")


ARCH_P = {"task_id": "t", "role": "architect", "goal": ["g"], "constraints": ["c"],
          "architecture": ["a"], "interfaces": [{}], "implementation_steps": [{}],
          "acceptance_criteria": ["ac"], "risks": [{}]}
IMPL_P = {"task_id": "t", "role": "coder", "changed_files": ["f"],
           "implementation_summary": "s", "implementation_details": ["d"],
           "assumptions": [], "unresolved_items": [], "test_requirements": ["tr"]}
TEST_P = {"task_id": "t", "role": "tester", "tests_run": ["x"], "tests_passed": ["x"],
         "tests_failed": [], "failures": [], "coverage_or_validation": [],
         "remaining_risks": []}
REVIEW_P = {"task_id": "t", "role": "reviewer", "status": "PASS", "findings": [],
            "severity": [], "affected_files": [], "required_changes": [],
            "acceptance_criteria_status": []}


class AnsweringAdapter(OfflineAdapter):
    """Offline adapter that answers every role with a valid packet.

    Routes by prompt semantics like a real adapter: the bare-identity
    agent id (SINGLE executor) is a coder invocation."""

    IDENTITY_AGENT_ID = '["rt-a","provider-a",null,"fp-a"]'

    def invoke(self, request):
        from external_runtime import InvocationResult, InvocationStatus, InvocationTrace
        if request.agent_id == self.IDENTITY_AGENT_ID:
            return self._packet(IMPL_P)
        for role, packet in (("architect", ARCH_P), ("coder", IMPL_P),
                              ("tester", TEST_P), ("reviewer", REVIEW_P)):
            if request.agent_id == role or request.agent_id.endswith(f',"{role}"]'):
                return self._packet(packet)
        from external_runtime import InvocationResult as IR, InvocationStatus as IS
        return IR(IS.SUCCESS, output="OK")

    @staticmethod
    def _packet(packet):
        from external_runtime import InvocationResult, InvocationStatus, InvocationTrace
        return InvocationResult(
            InvocationStatus.SUCCESS, output=json.dumps(packet),
            trace=InvocationTrace(
                invocation_id="inv-c", task_id="t", agent_id="a", runtime="rt-a",
                provider=None, model=None, role=None,
                status=InvocationStatus.SUCCESS,
                started_at=0.0, finished_at=0.0, duration_ms=1, exit_code=0,
                input_tokens="unknown", output_tokens="unknown", error=None))


def make_registry(adapter=None):
    adapter = adapter or OfflineAdapter()
    registry = AdapterRegistry()
    registry.register(AdapterDescriptor(
        runtime_id="rt-a", provider_id="provider-a", model_id=None,
        runtime_type="coding-agent", display_name="Runtime A",
        adapter_factory=lambda: adapter, config_fingerprint="fp-a",
    ))
    return registry


def evidence(status=CandidateValidationStatus.VERIFIED, caps=CAPS_ALL, provenance="REAL"):
    return CandidateValidationResult(
        identity=("rt-a", "provider-a", None, "fp-a"),
        status=status,
        gates_passed=frozenset(ValidationGate),
        gate_results=tuple(GateResult(g, GateVerdict.PASS) for g in ValidationGate),
        block_reason=None, failure_point=None, experiment_id="exp-c",
        executed_at=0.0,
        validated_capabilities=caps if status is CandidateValidationStatus.VERIFIED else (),
        evidence={}, provenance=provenance)


def fake_qualifier(result):
    calls = {"n": 0}

    def qualify(instance):
        calls["n"] += 1
        return result
    return qualify, calls


def health():
    return {"rt-a": RuntimeStatus(
        runtime_id="rt-a", executable="e", version="1",
        status=RuntimeState.READY, provider="provider-a", model=None,
        auth_method=None, reason_code=ReasonCode.NONE,
        evidence=HealthEvidence("d", "a", "p", "m", "ok"),
        checked_at=0.0, expires_at=1.0)}


class AutoBootstrapTests(unittest.TestCase):
    def test_c1_auto_bootstrap_builds_host_facade(self):
        store = {("rt-a", "provider-a", None, "fp-a"): evidence()}
        facade = build_facade_from_bootstrap(
            make_registry(), evidence=store, current_health=health())
        self.assertIsInstance(facade, ProductionFacade)

    def test_c2_manual_path_stays_compatible(self):
        adapter = OfflineAdapter()
        facade = build_facade(adapter, evidence(), health())
        self.assertIsInstance(facade, ProductionFacade)

    def test_c3_auto_path_needs_no_manual_validation(self):
        qualifier, calls = fake_qualifier(evidence())
        facade = build_facade_from_bootstrap(
            make_registry(), evidence={}, qualifier=qualifier,
            current_health=health())
        self.assertIsInstance(facade, ProductionFacade)
        self.assertEqual(calls["n"], 1)

    def test_c4_host_uses_bootstrap_pool(self):
        store = {("rt-a", "provider-a", None, "fp-a"): evidence()}
        registry = make_registry()
        session = bootstrap_runtime_session(registry, evidence=store)
        facade = build_facade_from_bootstrap(
            registry, evidence=store, current_health=health())
        self.assertEqual(facade._orchestrator._pool.identities(),
                         session.pool.identities())
        self.assertEqual(len(facade._orchestrator._pool.identities()), 1)

    def test_c5_evidence_reuse_skips_qualification(self):
        store = {("rt-a", "provider-a", None, "fp-a"): evidence()}
        qualifier, calls = fake_qualifier(evidence())
        facade = build_facade_from_bootstrap(
            make_registry(), evidence=store, qualifier=qualifier,
            current_health=health())
        self.assertEqual(calls["n"], 0)
        self.assertIsInstance(facade, ProductionFacade)

    def test_c6_missing_evidence_qualifies_once(self):
        qualifier, calls = fake_qualifier(evidence())
        facade = build_facade_from_bootstrap(
            make_registry(), evidence={}, qualifier=qualifier,
            current_health=health())
        self.assertEqual(calls["n"], 1)
        self.assertIsInstance(facade, ProductionFacade)

    def test_c7_qualification_failure_fails_honestly(self):
        failed = evidence(status=CandidateValidationStatus.FAILED)
        qualifier, calls = fake_qualifier(failed)
        with self.assertRaises(RuntimeError) as caught:
            build_facade_from_bootstrap(
                make_registry(), evidence={}, qualifier=qualifier,
                current_health=health())
        self.assertIn("NOT ADMITTED", str(caught.exception))
        self.assertEqual(calls["n"], 1)

    def test_c7b_no_evidence_no_qualifier_fails_honestly(self):
        with self.assertRaises(RuntimeError) as caught:
            build_facade_from_bootstrap(
                make_registry(), evidence={}, qualifier=None, current_health=health())
        self.assertIn("NO_EVIDENCE_NO_QUALIFIER", str(caught.exception))

    def test_c7c_auth_required_runtime_fails_honestly(self):
        class AuthRequiredAdapter(OfflineAdapter):
            def check_authentication(self):
                from runtime_health import AuthenticationCheck
                return AuthenticationCheck(
                    AuthenticationState.AUTH_REQUIRED,
                    reason_code=ReasonCode.AUTH_REQUIRED)

        with self.assertRaises(RuntimeError) as caught:
            build_facade_from_bootstrap(
                make_registry(AuthRequiredAdapter()), evidence={},
                qualifier=None, current_health=health())
        self.assertIn("AUTH_REQUIRED", str(caught.exception))


class CliSeamTests(unittest.TestCase):
    def _answering_facade(self):
        adapter = AnsweringAdapter()
        store = {("rt-a", "provider-a", None, "fp-a"): evidence()}
        return build_facade_from_bootstrap(
            make_registry(adapter), evidence=store, current_health=health())

    def test_c8_cli_consumes_bootstrapped_facade(self):
        summary = json.loads(run_cli(
            self._answering_facade(), ["run", "fix one simple bug"]))
        self.assertEqual(summary["path"], "SINGLE")
        self.assertEqual(summary["status"], "SUCCESS")
        self.assertEqual(summary["provenance"], "REAL")

    def test_c9_auto_simple_routes_single(self):
        summary = json.loads(run_cli(
            self._answering_facade(), ["run", "fix one simple bug"]))
        self.assertEqual(summary["path"], "SINGLE")

    def test_c9b_auto_complex_routes_four_stage(self):
        summary = json.loads(run_cli(
            self._answering_facade(),
            ["run", "redesign architecture across modules"]))
        self.assertEqual(summary["path"], "FOUR_STAGE")

    def test_c9c_off_runs_nothing(self):
        facade = self._answering_facade()
        summary = json.loads(run_cli(facade, ["run", "--mode", "off", "any task"]))
        self.assertEqual(summary["path"], "OFF")
        self.assertEqual(summary["status"], "FAILED")

    def test_c10_integration_is_runtime_neutral(self):
        import host as module
        text = Path(module.__file__).read_text(encoding="utf-8").lower()
        for name in ("claude", "codex", "deepseek", "openai", "anthropic",
                     "gemini", "tiny-agents", "tiny_agents"):
            self.assertNotIn(name, text)

    def test_bootstrap_does_not_reimplement_routing(self):
        import host as module
        source = Path(module.__file__).read_text(encoding="utf-8")
        # Routing vocabulary stays in the orchestrator layer, not the host.
        self.assertNotIn("SIMPLE", source.replace("SIMPLE", "SIMPLE", 0) if False else source)
        self.assertNotIn("_stages", source)


if __name__ == "__main__":
    unittest.main()
