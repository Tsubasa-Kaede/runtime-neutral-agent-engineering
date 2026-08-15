"""Verified Runtime Pool offline E2E — mock-only.

Combines the pool with the candidate chain end to end:
adapter bridge -> runner -> health controller (read-only) -> admit,
plus lifecycle, isolation and neutrality checks. No runtime is spawned.
"""
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
from runtime_health import RuntimeHealthController
from runtime_status import AuthenticationState, RuntimeState
from task_budget import BudgetUsage
from loop_guard import LoopGuard
from verified_runtime_pool import AdmissionKind, RejectionReason, VerifiedRuntimePool


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
    def __init__(self, rid, pid, mid, fp, probe):
        self.runtime_id, self.provider_id, self.model_id = rid, pid, mid
        self.config_fingerprint = fp
        self.capability_context = ("coding",)
        self.probe = probe
        self.invocation_spec = {"timeout_seconds": 30}


def pass_executor(caps=("coding",)):
    def executor(gate):
        return GateResult(gate, GateVerdict.PASS, capabilities=caps)
    return executor


def blocked_executor():
    def executor(gate):
        if int(gate) == 2:
            return GateResult(gate, GateVerdict.BLOCKED, "auth pending")
        return GateResult(gate, GateVerdict.PASS)
    return executor


def full_chain(adapter, executor, clock=lambda: 5.0, experiment="e2e"):
    candidate = candidate_from_adapter(adapter)
    result = CandidateValidationRunner().run(candidate, executor, clock=clock, experiment_id=experiment)
    health = RuntimeHealthController(ttl_seconds=60).check(adapter.probe)
    return result, health


class VerifiedPoolE2ETests(unittest.TestCase):
    def pool(self):
        return VerifiedRuntimePool(clock=lambda: 100.0)

    def adapter(self, rid="runtime-a", pid="provider-a", mid="model-a", fp="fp-a",
                auth_state=AuthenticationState.AUTHENTICATED):
        return FakeAdapter(rid, pid, mid, fp, mock_probe(rid, auth_state))

    # E1 full chain with READY health -> ACCEPTED
    def test_full_chain_ready_health_accepted(self):
        adapter = self.adapter()
        result, health = full_chain(adapter, pass_executor())
        self.assertEqual(result.status, CandidateValidationStatus.VERIFIED)
        self.assertEqual(health.status, RuntimeState.READY)
        outcome = self.pool().admit(result, frozenset({"coding"}), health.status)
        self.assertEqual(outcome.kind, AdmissionKind.ACCEPTED)

    # E2 health not ready -> REJECTED and health status object untouched
    def test_auth_required_health_rejects_and_health_readonly(self):
        adapter = self.adapter(auth_state=AuthenticationState.AUTH_REQUIRED)
        result, health = full_chain(adapter, pass_executor())
        self.assertEqual(health.status, RuntimeState.AUTH_REQUIRED)
        status_before = (health.status, health.reason_code)
        outcome = self.pool().admit(result, frozenset(), health.status)
        self.assertEqual(outcome.kind, AdmissionKind.REJECTED)
        self.assertEqual(outcome.reason, RejectionReason.HEALTH_NOT_READY)
        self.assertEqual((health.status, health.reason_code), status_before)

    # E3 three distinct identities coexist in one pool
    def test_three_candidates_coexist(self):
        pool = self.pool()
        specs = [
            ("runtime-a", "provider-b", "model-a", "fp-b"),
            ("runtime-b", "provider-a", "model-b", "fp-c"),
            ("runtime-a", "provider-a", "model-c", "fp-d"),
        ]
        for rid, pid, mid, fp in specs:
            adapter = self.adapter(rid, pid, mid, fp)
            result, health = full_chain(adapter, pass_executor())
            outcome = pool.admit(result, frozenset({"coding"}), health.status)
            self.assertEqual(outcome.kind, AdmissionKind.ACCEPTED)
        self.assertEqual(len(pool.identities()), 3)

    # E4 identical identity, different probe objects -> second is DUPLICATE
    def test_identical_identity_duplicate(self):
        pool = self.pool()
        first_adapter = self.adapter()
        second_adapter = self.adapter()  # fresh probe, same identity
        first_result, first_health = full_chain(first_adapter, pass_executor(), experiment="e1")
        second_result, second_health = full_chain(second_adapter, pass_executor(), experiment="e2")
        pool.admit(first_result, frozenset(), first_health.status)
        outcome = pool.admit(second_result, frozenset(), second_health.status)
        self.assertEqual(outcome.kind, AdmissionKind.DUPLICATE)
        self.assertEqual(outcome.existing_identity, first_result.identity)

    # E5 validation blocked -> REJECTED even with empty requirement
    def test_blocked_validation_rejected_even_with_empty_requirement(self):
        adapter = self.adapter()
        result, health = full_chain(adapter, blocked_executor())
        self.assertEqual(result.status, CandidateValidationStatus.BLOCKED)
        self.assertEqual(result.validated_capabilities, ())
        outcome = self.pool().admit(result, frozenset(), health.status)
        self.assertEqual(outcome.kind, AdmissionKind.REJECTED)
        self.assertEqual(outcome.reason, RejectionReason.NOT_VERIFIED)

    # E6 invalidate -> re-validate -> re-admit
    def test_invalidate_revalidate_readmit(self):
        pool = self.pool()
        adapter = self.adapter()
        result, health = full_chain(adapter, pass_executor())
        pool.admit(result, frozenset({"coding"}), health.status)
        removed = pool.invalidate(result.identity)
        self.assertIs(removed, result)
        fresh_result, fresh_health = full_chain(adapter, pass_executor(), experiment="re-run")
        outcome = pool.admit(fresh_result, frozenset({"coding"}), fresh_health.status)
        self.assertEqual(outcome.kind, AdmissionKind.ACCEPTED)

    # E7 health controller stays pure across repeated checks
    def test_health_controller_repeatable(self):
        adapter = self.adapter()
        controller = RuntimeHealthController(ttl_seconds=60)
        first = controller.check(adapter.probe)
        second = controller.check(adapter.probe)
        self.assertEqual(first.status, second.status)
        self.assertEqual(first.status, RuntimeState.READY)

    # E8 budget and loop guard untouched by the whole chain
    def test_budget_and_guard_untouched(self):
        usage, guard = BudgetUsage(), LoopGuard()
        before = (usage.total_agent_calls, usage.iterations_used, guard.check("t", "s", "a"))
        adapter = self.adapter()
        result, health = full_chain(adapter, pass_executor())
        self.pool().admit(result, frozenset(), health.status)
        after = (usage.total_agent_calls, usage.iterations_used, guard.check("t", "s", "a"))
        self.assertEqual(before, after)

    # boundary review: pool source has no production-stack imports
    def test_pool_isolated_from_production_stack(self):
        import verified_runtime_pool
        source = Path(verified_runtime_pool.__file__).read_text(encoding="utf-8")
        self.assertIn("from candidate_validation import", source)
        for forbidden in ("runtime_health", "runtime_pool", "capability_registry", "role_candidates",
                          "stage_runtime_selection", "selection_plan_bridge", "orchestrator",
                          "execution_engine", "invocation_plan", "DualAgentPair", "subprocess"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
