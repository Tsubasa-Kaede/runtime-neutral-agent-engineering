"""RC-3 Task B-2/B-3: Discovery Bootstrap contract (RED-first).

Registry -> Discovery -> Health -> Evidence lookup -> (reuse | explicit
qualification) -> VerifiedRuntimePool.admit. Pure composition over the
existing verified layers; offline fakes only — no runtime is ever started,
no retry, no fallback, no fabricated provenance.
"""
import sys
import unittest
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
from discovery_bootstrap import RuntimeBootstrapEntry, bootstrap_runtime_session
from external_runtime import RuntimeDiscovery
from runtime_adapter_registry import AdapterDescriptor, AdapterRegistry
from runtime_status import (
    AuthenticationState,
    HealthEvidence,
    ReasonCode,
    RuntimeState,
)
from verified_runtime_pool import VerifiedRuntimePool

SECRET_MARKERS = ("token", "secret", "api_key", "authorization", "bearer", "stdout", "stderr")
CAPS_ALL = ("architecture", "coding", "review", "testing")


class OfflineAdapter:
    """Fake probe surface; no process, no network, no credentials."""

    def __init__(self, runtime_id="rt-a", provider_id="provider-a",
                 auth_state=AuthenticationState.AUTHENTICATED):
        self._runtime_id = runtime_id
        self._provider_id = provider_id
        self._auth_state = auth_state
        self.discover_calls = 0

    def discover(self):
        self.discover_calls += 1
        return RuntimeDiscovery(self._runtime_id, True, "1.0", None, frozenset())

    def check_authentication(self):
        from runtime_health import AuthenticationCheck
        return_value = None
        if self._auth_state is AuthenticationState.AUTHENTICATED:
            return_value = AuthenticationCheck(self._auth_state, "oauth")
        else:
            return_value = AuthenticationCheck(self._auth_state,
                                               reason_code=ReasonCode.AUTH_REQUIRED)
        return return_value

    def check_provider_model(self):
        from runtime_health import ProviderModelCheck
        return ProviderModelCheck(self._provider_id, None, True, ReasonCode.NONE)

    def minimal_health_check(self, timeout_seconds):
        from runtime_health import MinimalHealthCheck
        return MinimalHealthCheck(True, ReasonCode.NONE, output_class="exact_ok")


def make_registry(*adapters):
    registry = AdapterRegistry()
    for index, adapter in enumerate(adapters):
        registry.register(AdapterDescriptor(
            runtime_id=adapter._runtime_id,
            provider_id=adapter._provider_id,
            model_id=None,
            runtime_type="coding-agent",
            display_name=f"Runtime {index}",
            adapter_factory=lambda a=adapter: a,
            config_fingerprint=f"fp-{adapter._runtime_id}",
        ))
    return registry


def evidence(runtime_id="rt-a", provider_id="provider-a", status=CandidateValidationStatus.VERIFIED,
             caps=CAPS_ALL, provenance="REAL"):
    return CandidateValidationResult(
        identity=(runtime_id, provider_id, None, f"fp-{runtime_id}"),
        status=status,
        gates_passed=frozenset(ValidationGate),
        gate_results=tuple(GateResult(g, GateVerdict.PASS) for g in ValidationGate),
        block_reason=None, failure_point=None, experiment_id="exp-1", executed_at=0.0,
        validated_capabilities=caps if status is CandidateValidationStatus.VERIFIED else (),
        evidence={}, provenance=provenance)


def fake_qualifier(result):
    calls = {"n": 0}

    def qualify(instance):
        calls["n"] += 1
        return result
    return qualify, calls


def bootstrap(registry, evidence_store=None, qualifier=None):
    return bootstrap_runtime_session(
        registry, evidence=evidence_store or {}, qualifier=qualifier,
        required_capabilities=CAPS_ALL,
    )


class BootstrapChainTests(unittest.TestCase):
    def test_registry_discovery_health_chain(self):
        adapter = OfflineAdapter()
        session = bootstrap(make_registry(adapter))
        entry = session.entries[0]
        self.assertEqual(entry.runtime_id, "rt-a")
        self.assertTrue(entry.discovery_available)
        self.assertEqual(entry.health_status, "READY")

    def test_unavailable_discovery_is_reported_not_admitted(self):
        adapter = OfflineAdapter()
        adapter.discover = lambda: RuntimeDiscovery("rt-a", False, None, "missing", frozenset())
        session = bootstrap(make_registry(adapter))
        self.assertFalse(session.entries[0].discovery_available)
        self.assertEqual(session.pool.identities(), ())

    def test_auth_required_health_blocks_admission(self):
        adapter = OfflineAdapter(auth_state=AuthenticationState.AUTH_REQUIRED)
        session = bootstrap(make_registry(adapter))
        self.assertEqual(session.entries[0].health_status, "AUTH_REQUIRED")
        self.assertEqual(session.pool.identities(), ())

    def test_existing_verified_evidence_is_reused_without_qualification(self):
        adapter = OfflineAdapter()
        store = {("rt-a", "provider-a", None, "fp-rt-a"): evidence()}
        qualifier, calls = fake_qualifier(evidence())
        session = bootstrap(make_registry(adapter), store, qualifier)
        self.assertEqual(calls["n"], 0)          # reuse, no qualification
        self.assertEqual(session.qualification_count, 0)
        self.assertEqual(session.pool.identities(),
                         (("rt-a", "provider-a", None, "fp-rt-a"),))

    def test_missing_evidence_triggers_exactly_one_qualification(self):
        adapter = OfflineAdapter()
        qualifier, calls = fake_qualifier(evidence())
        session = bootstrap(make_registry(adapter), {}, qualifier)
        self.assertEqual(calls["n"], 1)
        self.assertEqual(session.qualification_count, 1)
        self.assertEqual(len(session.pool.identities()), 1)

    def test_qualification_failure_is_not_admitted(self):
        adapter = OfflineAdapter()
        failed = CandidateValidationResult(
            identity=("rt-a", "provider-a", None, "fp-rt-a"),
            status=CandidateValidationStatus.FAILED,
            gates_passed=frozenset(), gate_results=(),
            block_reason=None,
            failure_point=(ValidationGate.G5_MINIMAL_INVOCATION, "INVOCATION_FAILED"),
            experiment_id="exp-f", executed_at=0.0,
            validated_capabilities=(), evidence={},
            provenance="REAL")
        qualifier, calls = fake_qualifier(failed)
        session = bootstrap(make_registry(adapter), {}, qualifier)
        self.assertEqual(session.entries[0].validation_status, "FAILED")
        self.assertEqual(session.entries[0].capabilities, ())
        self.assertEqual(session.pool.identities(), ())
        self.assertEqual(calls["n"], 1)  # no retry after failure

    def test_non_verified_evidence_cannot_enter_pool(self):
        adapter = OfflineAdapter()
        store = {("rt-a", "provider-a", None, "fp-rt-a"): evidence(
            status=CandidateValidationStatus.NOT_VERIFIED)}
        qualifier, calls = fake_qualifier(evidence())
        session = bootstrap(make_registry(adapter), store, qualifier)
        self.assertEqual(session.pool.identities(), ())
        self.assertEqual(calls["n"], 0)  # invalid evidence is refused, not re-run

    def test_ready_health_never_impersonates_verified(self):
        # Health READY without any evidence and without a qualifier: not admitted.
        adapter = OfflineAdapter()  # health will be READY
        session = bootstrap(make_registry(adapter), {}, None)
        self.assertEqual(session.entries[0].health_status, "READY")
        self.assertEqual(session.entries[0].validation_status, "NOT_QUALIFIED")
        self.assertEqual(session.pool.identities(), ())

    def test_offline_provenance_evidence_is_not_admitted_as_real(self):
        adapter = OfflineAdapter()
        store = {("rt-a", "provider-a", None, "fp-rt-a"): evidence(provenance="OFFLINE")}
        session = bootstrap(make_registry(adapter), store, None)
        self.assertEqual(session.entries[0].provenance, "OFFLINE")
        self.assertEqual(session.pool.identities(), ())

    def test_bootstrap_never_mutates_provenance(self):
        adapter = OfflineAdapter()
        store = {("rt-a", "provider-a", None, "fp-rt-a"): evidence(provenance="OFFLINE")}
        session = bootstrap(make_registry(adapter), store, None)
        self.assertEqual(session.entries[0].provenance, "OFFLINE")

    def test_offline_bootstrap_performs_no_runtime_invocation(self):
        import discovery_bootstrap as module
        source = Path(module.__file__).read_text(encoding="utf-8")
        for forbidden in ("subprocess", "invoke(", "minimal_health_check(request",
                          "run_real_validation", "os.environ"):
            self.assertNotIn(forbidden, source)

    def test_bootstrap_is_runtime_neutral(self):
        import discovery_bootstrap as module
        text = Path(module.__file__).read_text(encoding="utf-8").lower()
        for name in ("claude", "codex", "deepseek", "openai", "anthropic",
                     "gemini", "tiny-agents", "tiny_agents"):
            self.assertNotIn(name, text)

    def test_result_entries_are_frozen_and_secret_free(self):
        from dataclasses import FrozenInstanceError
        adapter = OfflineAdapter()
        store = {("rt-a", "provider-a", None, "fp-rt-a"): evidence()}
        session = bootstrap(make_registry(adapter), store, None)
        entry = session.entries[0]
        with self.assertRaises(FrozenInstanceError):
            entry.runtime_id = "mutated"
        surface = repr(session).lower()
        for marker in SECRET_MARKERS:
            self.assertNotIn(marker, surface)

    def test_multi_runtime_pool_contains_only_verified_real(self):
        good = OfflineAdapter(runtime_id="rt-good", provider_id="p-good")
        blocked = OfflineAdapter(runtime_id="rt-blocked", provider_id="p-blocked",
                                auth_state=AuthenticationState.AUTH_REQUIRED)
        store = {("rt-good", "p-good", None, "fp-rt-good"): evidence(
            runtime_id="rt-good", provider_id="p-good")}
        session = bootstrap(make_registry(good, blocked), store, None)
        self.assertEqual(session.pool.identities(),
                         (("rt-good", "p-good", None, "fp-rt-good"),))
        by_id = {entry.runtime_id: entry for entry in session.entries}
        self.assertEqual(by_id["rt-blocked"].health_status, "AUTH_REQUIRED")
        self.assertEqual(by_id["rt-blocked"].admitted, False)

    def test_no_fallback_path_exists(self):
        import discovery_bootstrap as module
        source = Path(module.__file__).read_text(encoding="utf-8")
        self.assertNotIn("FallbackPolicy", source)
        self.assertNotIn("fallback", source.lower().replace("no fallback", ""))


if __name__ == "__main__":
    unittest.main()
