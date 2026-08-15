"""Phase 10D-fix: Verified Selection Bridge offline E2E - mock-only.

Combines the verified chain end to end: adapter bridge -> validation
runner -> health controller (read-only) -> pool admission -> bridge with
the injected current-health snapshot. agent_id is derived from the full
identity, score stays None, and entries without an experiment_id are
skipped. No runtime is spawned and no formal selection layer is involved.
"""
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from candidate_adapter_contract import candidate_from_adapter
from candidate_validation import (
    CandidateValidationRunner,
    CandidateValidationStatus,
    GateResult,
    GateVerdict,
)
from external_runtime import RuntimeDiscovery
from loop_guard import LoopGuard
from runtime_health import RuntimeHealthController
from runtime_status import AuthenticationState, RuntimeState
from task_budget import BudgetUsage
from verified_runtime_pool import AdmissionKind, VerifiedRuntimePool
from verified_selection_bridge import VerifiedSelectionBridge, agent_id_for


def mock_probe(rid, auth_state=AuthenticationState.AUTHENTICATED):
    probe = Mock(spec=["discover", "check_authentication", "check_provider_model", "minimal_health_check"])
    probe.discover.return_value = RuntimeDiscovery(rid, True, "1.0", None, frozenset())
    probe.check_authentication.return_value = type("A", (), {
        "state": auth_state, "method": "managed", "reason_code": None})()
    probe.check_provider_model.return_value = type("P", (), {
        "provider": "p", "model": "m", "available": True, "reason_code": None})()
    probe.minimal_health_check.return_value = type("H", (), {
        "passed": True, "reason_code": None, "trace": None, "output_class": "exact_ok"})()
    return probe


class FakeAdapter:
    def __init__(self, rid, pid, mid, fp, probe, declared=("coding",)):
        self.runtime_id, self.provider_id, self.model_id = rid, pid, mid
        self.config_fingerprint = fp
        self.capability_context = tuple(declared)
        self.probe = probe
        self.invocation_spec = {"timeout_seconds": 30}


def pass_executor(caps=("coding",)):
    def executor(gate):
        return GateResult(gate, GateVerdict.PASS, capabilities=caps)
    return executor


def full_chain(adapter, executor, clock=lambda: 5.0, experiment="bridge-e2e"):
    candidate = candidate_from_adapter(adapter)
    result = CandidateValidationRunner().run(candidate, executor, clock=clock, experiment_id=experiment)
    health = RuntimeHealthController(ttl_seconds=60).check(adapter.probe)
    return result, health


class VerifiedSelectionBridgeE2ETests(unittest.TestCase):
    def bridge(self):
        return VerifiedSelectionBridge()

    def adapter(self, rid="runtime-a", pid="provider-a", mid="model-a", fp="fp-a",
                auth_state=AuthenticationState.AUTHENTICATED, declared=("coding",)):
        return FakeAdapter(rid, pid, mid, fp, mock_probe(rid, auth_state), declared)

    def test_full_chain_produces_verified_candidate(self):
        adapter = self.adapter(declared=("coding", "architecture"))
        result, health = full_chain(adapter, pass_executor(("coding", "architecture")),
                                    experiment="chain-1")
        pool = VerifiedRuntimePool(clock=lambda: 10.0)
        outcome = pool.admit(result, frozenset({"coding"}), health.status)
        self.assertEqual(outcome.kind, AdmissionKind.ACCEPTED)
        candidate = self.bridge().candidates_for(
            pool, {adapter.runtime_id: health}, "coder", ("coding",),
        ).candidates[0]
        self.assertEqual(candidate.agent_id, agent_id_for(result.identity))
        self.assertIsNone(candidate.score)
        self.assertEqual(candidate.runtime_id, "runtime-a")
        self.assertEqual(candidate.provider_id, "provider-a")
        self.assertEqual(candidate.model_id, "model-a")
        self.assertEqual([item.capability.value for item in candidate.capabilities], ["coding"])
        self.assertIsNone(candidate.capabilities[0].score)
        self.assertEqual(candidate.capabilities[0].source, "chain-1")

    def test_missing_experiment_id_skipped_while_valid_remains(self):
        pool = VerifiedRuntimePool(clock=lambda: 10.0)
        valid_adapter = self.adapter("runtime-good", "provider-a", "model-a", "fp-good")
        missing_adapter = self.adapter("runtime-missing", "provider-a", "model-a", "fp-missing")
        valid_result, valid_health = full_chain(valid_adapter, pass_executor(), experiment="valid-exp")
        missing_result, missing_health = full_chain(missing_adapter, pass_executor(), experiment=None)
        pool.admit(valid_result, frozenset({"coding"}), valid_health.status)
        pool.admit(missing_result, frozenset({"coding"}), missing_health.status)
        candidate_set = self.bridge().candidates_for(
            pool,
            {valid_adapter.runtime_id: valid_health, missing_adapter.runtime_id: missing_health},
            "coder", ("coding",),
        )
        self.assertEqual(len(candidate_set.candidates), 1)
        self.assertEqual(candidate_set.candidates[0].agent_id,
                         agent_id_for(valid_result.identity))
        self.assertEqual(candidate_set.candidates[0].capabilities[0].source, "valid-exp")
    def test_declared_context_does_not_fulfill_requirement(self):
        adapter = self.adapter(declared=("architecture",))
        result, health = full_chain(adapter, pass_executor(("coding",)))
        pool = VerifiedRuntimePool(clock=lambda: 10.0)
        pool.admit(result, frozenset(), health.status)
        candidate_set = self.bridge().candidates_for(
            pool, {adapter.runtime_id: health}, "architect", ("architecture",),
        )
        self.assertEqual(candidate_set.candidates, ())

    def test_current_health_snapshot_gates_selection(self):
        adapter = self.adapter()
        result, health = full_chain(adapter, pass_executor(), experiment="snap")
        pool = VerifiedRuntimePool(clock=lambda: 10.0)
        pool.admit(result, frozenset({"coding"}), health.status)  # READY at admission
        stale_probe = mock_probe(adapter.runtime_id, AuthenticationState.AUTH_REQUIRED)
        stale_health = RuntimeHealthController(ttl_seconds=60).check(stale_probe)
        self.assertEqual(stale_health.status, RuntimeState.AUTH_REQUIRED)
        candidate_set = self.bridge().candidates_for(
            pool, {adapter.runtime_id: stale_health}, "coder", ("coding",),
        )
        self.assertEqual(candidate_set.candidates, ())
        self.assertEqual(pool.identities(), (result.identity,))

    def test_multiple_runtimes_providers_models_coexist(self):
        pool = VerifiedRuntimePool(clock=lambda: 10.0)
        adapters = [
            self.adapter("runtime-a", "provider-b", "model-a", "fp-b"),
            self.adapter("runtime-b", "provider-a", "model-b", "fp-c"),
            self.adapter("runtime-a", "provider-a", "model-c", "fp-d"),
        ]
        health_snapshot = {}
        for adapter in adapters:
            result, health = full_chain(adapter, pass_executor(("coding",)),
                                        experiment=adapter.config_fingerprint)
            pool.admit(result, frozenset({"coding"}), health.status)
            health_snapshot[adapter.runtime_id] = health
        candidate_set = self.bridge().candidates_for(
            pool, health_snapshot, "coder", ("coding",),
        )
        self.assertEqual(len(candidate_set.candidates), 3)
        seen = {(c.runtime_id, c.provider_id, c.model_id) for c in candidate_set.candidates}
        self.assertEqual(
            seen, {(a.runtime_id, a.provider_id, a.model_id) for a in adapters},
        )

    def test_bridge_never_probes_after_chain(self):
        adapter = self.adapter()
        result, health = full_chain(adapter, pass_executor())
        pool = VerifiedRuntimePool(clock=lambda: 10.0)
        pool.admit(result, frozenset({"coding"}), health.status)
        probe = adapter.probe
        probe.reset_mock()
        self.bridge().candidates_for(
            pool, {adapter.runtime_id: health}, "coder", ("coding",),
        )
        self.assertEqual(probe.method_calls, [])
        import verified_selection_bridge
        source = Path(verified_selection_bridge.__file__).read_text(encoding="utf-8")
        self.assertNotIn("subprocess", source)
        self.assertNotIn("invoke", source)

    def test_budget_and_guard_untouched(self):
        usage, guard = BudgetUsage(), LoopGuard()
        before = (usage.total_agent_calls, usage.iterations_used, guard.check("t", "s", "a"))
        adapter = self.adapter()
        result, health = full_chain(adapter, pass_executor())
        pool = VerifiedRuntimePool(clock=lambda: 10.0)
        pool.admit(result, frozenset({"coding"}), health.status)
        self.bridge().candidates_for(
            pool, {adapter.runtime_id: health}, "coder", ("coding",),
        )
        after = (usage.total_agent_calls, usage.iterations_used, guard.check("t", "s", "a"))
        self.assertEqual(before, after)

    def test_no_invocation_plan_or_pair(self):
        import verified_selection_bridge
        source = Path(verified_selection_bridge.__file__).read_text(encoding="utf-8")
        for forbidden in ("invocation_plan", "DualAgentPair", "role_candidates",
                          "stage_runtime_selection", "selection_plan_bridge",
                          "dual_agent_selection", "orchestrator", "execution_engine"):
            self.assertNotIn(forbidden, source)
        self.assertFalse(hasattr(VerifiedSelectionBridge(), "plan"))
        self.assertFalse(hasattr(VerifiedSelectionBridge(), "decide"))

    def test_real_provider_invocation_stays_opt_in(self):
        self.assertNotEqual(os.environ.get("RUN_REAL_PROVIDER_TESTS", ""), "1")


if __name__ == "__main__":
    unittest.main()