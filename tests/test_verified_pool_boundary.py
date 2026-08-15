"""Verified Runtime Pool Boundary — offline admission-semantics check.

Verifies the full VERIFIED admission contract is now expressible:
  status == VERIFIED  AND  validated_capabilities satisfy requirements
  AND  current Health == READY (checked at admission time by the pool side).
The admit() function below lives ONLY in this test as a semantic probe —
no production pool, health, selection or orchestration code is involved.
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from candidate_validation import (
    CandidateRuntimeInstance,
    CandidateValidationResult,
    CandidateValidationRunner,
    CandidateValidationStatus,
    GateResult,
    GateVerdict,
)


def make_candidate(capabilities=("coding",)):
    return CandidateRuntimeInstance(
        runtime_id="runtime-a", provider_id="provider-a", model_id="model-a",
        config_fingerprint="fp-a", capability_context=capabilities,
        probe=Mock(spec=[]), invocation_spec={"timeout_seconds": 30},
    )


def run(executor, candidate=None):
    return CandidateValidationRunner().run(
        candidate or make_candidate(), executor,
        clock=lambda: 1.0, experiment_id="boundary",
    )


def pass_with_caps(caps):
    def executor(gate):
        return GateResult(gate, GateVerdict.PASS, capabilities=caps)
    return executor


def blocked_with_caps(caps):
    def executor(gate):
        if gate.number if hasattr(gate, "number") else int(gate) == 2:
            return GateResult(gate, GateVerdict.BLOCKED, "external", capabilities=caps)
        return GateResult(gate, GateVerdict.PASS, capabilities=caps)
    return executor


def admit(result: CandidateValidationResult, required: frozenset, health_ready: bool) -> bool:
    """Semantic probe (test-only): the full admission contract."""
    return (
        result.status is CandidateValidationStatus.VERIFIED
        and required.issubset(frozenset(result.validated_capabilities))
        and health_ready
    )


class VerifiedPoolBoundaryTests(unittest.TestCase):
    def test_full_contract_verifies_when_all_three_conditions_hold(self):
        result = run(pass_with_caps(("coding", "architecture")))
        self.assertTrue(admit(result, frozenset({"coding"}), health_ready=True))
        self.assertTrue(admit(result, frozenset({"coding", "architecture"}), health_ready=True))

    def test_health_not_ready_blocks_admission(self):
        result = run(pass_with_caps(("coding",)))
        self.assertFalse(admit(result, frozenset({"coding"}), health_ready=False))

    def test_missing_capability_blocks_admission(self):
        result = run(pass_with_caps(("coding",)))
        self.assertFalse(admit(result, frozenset({"coding", "architecture"}), health_ready=True))

    def test_non_verified_status_blocks_admission_even_with_capabilities(self):
        # executor handed capabilities on every gate, but the run short-circuited:
        # the result must carry no capability evidence at all (double guard).
        def executor(gate):
            if int(gate) == 2:
                return GateResult(gate, GateVerdict.BLOCKED, "external", capabilities=("coding",))
            return GateResult(gate, GateVerdict.PASS, capabilities=("coding",))

        result = run(executor)
        self.assertEqual(result.status, CandidateValidationStatus.BLOCKED)
        self.assertEqual(result.validated_capabilities, ())
        self.assertFalse(admit(result, frozenset(), health_ready=True))

    def test_declared_context_cannot_fulfill_requirement(self):
        # candidate declares architecture; no gate evidence produced.
        result = run(pass_with_caps(()), candidate=make_candidate(("architecture",)))
        self.assertFalse(admit(result, frozenset({"architecture"}), health_ready=True))

    def test_identity_keeps_four_dimensions_decoupled(self):
        result = run(pass_with_caps(("coding",)))
        self.assertEqual(result.identity,
                         ("runtime-a", "provider-a", "model-a", "fp-a"))
        other = CandidateValidationRunner().run(
            make_candidate(), pass_with_caps(("coding",)),
            clock=lambda: 1.0, experiment_id="boundary",
        )
        self.assertEqual(result.identity, other.identity)

    def test_validation_layer_stays_outside_formal_stack(self):
        import candidate_validation
        source = Path(candidate_validation.__file__).read_text(encoding="utf-8")
        for module in ("runtime_health", "runtime_pool", "capability_registry", "role_candidates",
                       "stage_runtime_selection", "selection_plan_bridge", "orchestrator",
                       "execution_engine", "invocation_plan", "DualAgentPair"):
            self.assertNotIn(module, source)


if __name__ == "__main__":
    unittest.main()
