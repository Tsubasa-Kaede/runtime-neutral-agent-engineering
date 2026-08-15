"""Verified Runtime Pool unit tests — offline, mock-only.

Locks the approved Admission Contract: fixed five-step decision order,
REJECTED before DUPLICATE, no-overwrite duplicates, invalidate/re-admit,
parameter-injected health, set-only capability consumption.
"""
import sys
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import Mock

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
from verified_runtime_pool import (
    AdmissionKind,
    AdmissionOutcome,
    RejectionReason,
    VerifiedPoolEntry,
    VerifiedRuntimePool,
)
from runtime_status import RuntimeState


def make_result(status=CandidateValidationStatus.VERIFIED, caps=("coding", "architecture"),
                identity=("r", "p", "m", "f"), experiment_id="exp-1"):
    gate_results = tuple(
        GateResult(g, GateVerdict.PASS if status is CandidateValidationStatus.VERIFIED else GateVerdict.PASS)
        for g in ValidationGate
    )
    return CandidateValidationResult(
        identity=identity,
        status=status,
        gates_passed=frozenset(ValidationGate) if status is CandidateValidationStatus.VERIFIED else frozenset(),
        gate_results=gate_results,
        block_reason=None,
        failure_point=None,
        experiment_id=experiment_id,
        executed_at=1.0,
        validated_capabilities=tuple(caps) if status is CandidateValidationStatus.VERIFIED else (),
        evidence={},
    )


def fixed_clock():
    return lambda: 500.0


class VerifiedRuntimePoolTests(unittest.TestCase):
    def pool(self):
        return VerifiedRuntimePool(clock=fixed_clock())

    # A1 ACCEPTED when all three branches hold
    def test_accepted_when_all_conditions_hold(self):
        pool = self.pool()
        result = make_result()
        outcome = pool.admit(result, frozenset({"coding"}), RuntimeState.READY)
        self.assertEqual(outcome.kind, AdmissionKind.ACCEPTED)
        self.assertIsNone(outcome.reason)
        self.assertIsNone(outcome.existing_identity)
        self.assertIs(pool.get(result.identity), result)
        self.assertIn(result.identity, pool.identities())

    # A2 REJECTED(NOT_VERIFIED) for every non-verified status, pool untouched
    def test_not_verified_rejected_for_all_non_verified_statuses(self):
        for status in (CandidateValidationStatus.BLOCKED, CandidateValidationStatus.FAILED,
                       CandidateValidationStatus.NOT_VERIFIED):
            with self.subTest(status=status.value):
                pool = self.pool()
                outcome = pool.admit(make_result(status=status), frozenset(), RuntimeState.READY)
                self.assertEqual(outcome.kind, AdmissionKind.REJECTED)
                self.assertEqual(outcome.reason, RejectionReason.NOT_VERIFIED)
                self.assertEqual(pool.identities(), ())

    # A3 REJECTED(CAPABILITY_INSUFFICIENT)
    def test_capability_insufficient_rejected(self):
        pool = self.pool()
        outcome = pool.admit(make_result(caps=("coding",)), frozenset({"coding", "architecture"}),
                             RuntimeState.READY)
        self.assertEqual(outcome.kind, AdmissionKind.REJECTED)
        self.assertEqual(outcome.reason, RejectionReason.CAPABILITY_INSUFFICIENT)
        self.assertEqual(pool.identities(), ())

    # A4 REJECTED(HEALTH_NOT_READY) for every non-ready health
    def test_health_not_ready_rejected_for_all_non_ready_states(self):
        for state in (RuntimeState.AUTH_REQUIRED, RuntimeState.UNAVAILABLE, RuntimeState.ERROR):
            with self.subTest(state=state.value):
                pool = self.pool()
                outcome = pool.admit(make_result(), frozenset(), state)
                self.assertEqual(outcome.kind, AdmissionKind.REJECTED)
                self.assertEqual(outcome.reason, RejectionReason.HEALTH_NOT_READY)
                self.assertEqual(pool.identities(), ())

    # A5 DUPLICATE: same identity re-admitted, existing entry not overwritten
    def test_duplicate_identity_is_reported_and_not_overwritten(self):
        pool = self.pool()
        first = make_result(experiment_id="exp-1")
        second = make_result(experiment_id="exp-2")  # same identity, newer experiment
        pool.admit(first, frozenset(), RuntimeState.READY)
        outcome = pool.admit(second, frozenset(), RuntimeState.READY)
        self.assertEqual(outcome.kind, AdmissionKind.DUPLICATE)
        self.assertEqual(outcome.existing_identity, first.identity)
        self.assertIs(pool.get(first.identity), first)  # first stays authoritative

    # A6 REJECTED takes precedence over DUPLICATE
    def test_rejected_takes_precedence_over_duplicate(self):
        pool = self.pool()
        original = make_result()
        pool.admit(original, frozenset(), RuntimeState.READY)
        failing = make_result(status=CandidateValidationStatus.FAILED)
        outcome = pool.admit(failing, frozenset(), RuntimeState.READY)
        self.assertEqual(outcome.kind, AdmissionKind.REJECTED)
        self.assertEqual(outcome.reason, RejectionReason.NOT_VERIFIED)

    # A7 empty validated + empty required is acceptable
    def test_empty_capabilities_with_empty_requirement_accepted(self):
        pool = self.pool()
        outcome = pool.admit(make_result(caps=()), frozenset(), RuntimeState.READY)
        self.assertEqual(outcome.kind, AdmissionKind.ACCEPTED)

    # A8 required capabilities may arrive as any collection form
    def test_required_capability_container_forms(self):
        for required in (frozenset({"coding"}), {"coding"}, ["coding"], ("coding",)):
            with self.subTest(form=type(required).__name__):
                pool = self.pool()
                outcome = pool.admit(make_result(), required, RuntimeState.READY)
                self.assertEqual(outcome.kind, AdmissionKind.ACCEPTED)

    # B1/B2/B3 invalidate lifecycle
    def test_invalidate_returns_original_and_removes_entry(self):
        pool = self.pool()
        result = make_result()
        pool.admit(result, frozenset(), RuntimeState.READY)
        removed = pool.invalidate(result.identity)
        self.assertIs(removed, result)
        self.assertNotIn(result.identity, pool.identities())

    def test_invalidate_missing_identity_returns_none(self):
        self.assertIsNone(self.pool().invalidate(("x", "y", "z", "w")))

    def test_readmit_after_invalidate_is_accepted(self):
        pool = self.pool()
        result = make_result()
        pool.admit(result, frozenset(), RuntimeState.READY)
        pool.invalidate(result.identity)
        outcome = pool.admit(result, frozenset(), RuntimeState.READY)
        self.assertEqual(outcome.kind, AdmissionKind.ACCEPTED)

    # C1 identities sorted deterministically
    def test_identities_sorted_regardless_of_insertion_order(self):
        pool = self.pool()
        for identity in (("z", "p", "m", "f"), ("a", "p", "m", "f"), ("m", "p", "m", "f")):
            pool.admit(make_result(identity=identity), frozenset(), RuntimeState.READY)
        self.assertEqual(pool.identities(), tuple(sorted((
            ("z", "p", "m", "f"), ("a", "p", "m", "f"), ("m", "p", "m", "f"),
        ))))

    # C2 deterministic outcome and entry with injected clock
    def test_deterministic_outcome_and_entry(self):
        first_pool, second_pool = self.pool(), self.pool()
        result = make_result()
        first = first_pool.admit(result, frozenset({"coding"}), RuntimeState.READY)
        second = second_pool.admit(result, frozenset({"coding"}), RuntimeState.READY)
        self.assertEqual(first, second)
        entry = first_pool._entries[result.identity]
        self.assertEqual(entry.admitted_at, 500.0)
        self.assertEqual(entry.health_state_at_admission, "READY")
        self.assertIs(entry.result, result)

    # C3 immutability
    def test_outcome_and_entry_are_immutable(self):
        pool = self.pool()
        outcome = pool.admit(make_result(), frozenset(), RuntimeState.READY)
        entry = pool._entries[make_result().identity]
        with self.assertRaises(FrozenInstanceError):
            outcome.kind = AdmissionKind.REJECTED
        with self.assertRaises(FrozenInstanceError):
            entry.admitted_at = 0.0

    # D1 secret-shaped content rejected
    def test_secret_shaped_content_rejected(self):
        with self.assertRaises(ValueError):
            AdmissionOutcome(AdmissionKind.REJECTED, identity=("r", "p", "m", "token=x"),
                             reason=RejectionReason.NOT_VERIFIED)
        with self.assertRaises(ValueError):
            VerifiedPoolEntry(make_result(), 1.0, "api_key")

    # D2 import whitelist: only candidate_validation + stdlib
    def test_pool_imports_only_candidate_validation(self):
        import verified_runtime_pool
        source = Path(verified_runtime_pool.__file__).read_text(encoding="utf-8")
        self.assertIn("from candidate_validation import", source)
        for forbidden in ("runtime_health", "runtime_pool", "capability_registry", "role_candidates",
                          "stage_runtime_selection", "selection_plan_bridge", "orchestrator",
                          "execution_engine", "invocation_plan", "DualAgentPair",
                          "subprocess", "invoke"):
            self.assertNotIn(forbidden, source)

    # D3 runtime-neutral: no names, no value branches
    def test_no_runtime_names_or_value_branches(self):
        import verified_runtime_pool
        text = Path(verified_runtime_pool.__file__).read_text(encoding="utf-8").lower()
        names = tuple("".join(p) for p in (
            ("cl", "aude"), ("co", "dex"), ("deep", "seek"),
            ("tiny", "-agents"), ("tiny", "_agents"), ("gem", "ini"),
        ))
        for name in names:
            self.assertNotIn(name, text)
        for branch in ("runtime_id ==", "provider_id ==", "model_id =="):
            self.assertNotIn(branch, text)


if __name__ == "__main__":
    unittest.main()
