"""Phase 10D-fix: Verified Selection Bridge unit tests - offline, mock-only.

Locks the approved bridge contract: candidates come exclusively from the
VerifiedRuntimePool, current health is injected and read-only, only
VERIFIED + READY entries become candidates, capability evidence is built
only from validated_capabilities (declared context is never promoted),
and the output is deterministic, immutable, secret-free and
runtime-neutral. agent_id is a deterministic projection of the full
identity, score stays None, and entries without an experiment_id are
skipped without failing the role.
"""
import sys
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from candidate_validation import (
    CandidateValidationResult,
    CandidateValidationStatus,
    GateResult,
    GateVerdict,
    ValidationGate,
)
from capability_registry import (
    CapabilityConfidence,
    CapabilityEvidence,
    CapabilityName,
)
from loop_guard import LoopGuard
from runtime_status import (
    HealthEvidence,
    ReasonCode,
    RuntimeState,
    RuntimeStatus,
)
from task_budget import BudgetUsage
from verified_runtime_pool import VerifiedPoolEntry, VerifiedRuntimePool
from verified_selection_bridge import (
    VerifiedRoleCandidate,
    VerifiedRoleCandidateSet,
    VerifiedSelectionBridge,
    agent_id_for,
)


def make_result(status=CandidateValidationStatus.VERIFIED, caps=("coding",),
                identity=("runtime-a", "provider-a", "model-a", "fp-a"),
                experiment_id="exp-1"):
    gates = tuple(GateResult(gate, GateVerdict.PASS) for gate in ValidationGate)
    return CandidateValidationResult(
        identity=identity,
        status=status,
        gates_passed=frozenset(ValidationGate) if status is CandidateValidationStatus.VERIFIED else frozenset(),
        gate_results=gates,
        block_reason=None,
        failure_point=None,
        experiment_id=experiment_id,
        executed_at=1.0,
        validated_capabilities=tuple(caps) if status is CandidateValidationStatus.VERIFIED else (),
        evidence={},
    )


def runtime_status(rid, state=RuntimeState.READY):
    return RuntimeStatus(
        rid, rid + ".exe", "1.0", state, "provider-a", "model-a", "managed",
        ReasonCode.NONE, HealthEvidence("v", "v", "v", "v", "v"), 1.0, 100.0,
    )


def ready_health(*runtime_ids):
    return {rid: runtime_status(rid) for rid in runtime_ids}


def admit(pool, result, required=("coding",)):
    return pool.admit(result, frozenset(required), RuntimeState.READY)


class VerifiedSelectionBridgeTests(unittest.TestCase):
    def bridge(self):
        return VerifiedSelectionBridge()

    def test_verified_and_ready_entry_becomes_candidate(self):
        pool = VerifiedRuntimePool(clock=lambda: 1.0)
        result = make_result(caps=("coding",), experiment_id="exp-1")
        admit(pool, result)
        candidate_set = self.bridge().candidates_for(
            pool, ready_health("runtime-a"), "coder", (CapabilityName.CODING,),
        )
        self.assertIsInstance(candidate_set, VerifiedRoleCandidateSet)
        self.assertEqual(candidate_set.role, "coder")
        self.assertEqual(len(candidate_set.candidates), 1)
        candidate = candidate_set.candidates[0]
        self.assertIsInstance(candidate, VerifiedRoleCandidate)
        self.assertEqual(candidate.rank, 1)
        self.assertEqual(candidate.agent_id, agent_id_for(result.identity))
        self.assertIsNone(candidate.score)
        self.assertEqual(candidate.runtime_id, "runtime-a")
        self.assertEqual(candidate.provider_id, "provider-a")
        self.assertEqual(candidate.model_id, "model-a")
        self.assertEqual(candidate.config_fingerprint, "fp-a")
        self.assertEqual(candidate.required_capabilities, ("coding",))

    def test_agent_id_for_same_identity_is_stable(self):
        identity = ("runtime-a", "provider-a", "model-a", "fp-a")
        self.assertEqual(agent_id_for(identity), agent_id_for(identity))
        pool = VerifiedRuntimePool(clock=lambda: 1.0)
        admit(pool, make_result(identity=identity, caps=("coding",)))
        candidate = self.bridge().candidates_for(
            pool, ready_health("runtime-a"), "coder", ("coding",),
        ).candidates[0]
        self.assertEqual(candidate.agent_id, agent_id_for(identity))

    def test_agent_id_changes_with_each_identity_dimension(self):
        base = ("runtime-a", "provider-a", "model-a", "fp-a")
        variants = [
            ("runtime-b", "provider-a", "model-a", "fp-a"),
            ("runtime-a", "provider-b", "model-a", "fp-a"),
            ("runtime-a", "provider-a", "model-b", "fp-a"),
            ("runtime-a", "provider-a", "model-a", "fp-b"),
        ]
        base_id = agent_id_for(base)
        for variant in variants:
            with self.subTest(variant=variant):
                self.assertNotEqual(agent_id_for(variant), base_id)

    def test_agent_id_never_uses_runtime_id_alone(self):
        first = ("runtime-a", "provider-a", "model-a", "fp-a")
        second = ("runtime-a", "provider-b", "model-b", "fp-b")
        self.assertNotEqual(agent_id_for(first), agent_id_for(second))
        pool = VerifiedRuntimePool(clock=lambda: 1.0)
        for identity in (first, second):
            admit(pool, make_result(identity=identity, caps=("coding",)),
                  required=("coding",))
        candidate_set = self.bridge().candidates_for(
            pool, ready_health("runtime-a"), "coder", ("coding",),
        )
        self.assertEqual(len(candidate_set.candidates), 2)
        self.assertEqual(
            {c.agent_id for c in candidate_set.candidates},
            {agent_id_for(first), agent_id_for(second)},
        )

    def test_agent_id_independent_of_clock_and_order(self):
        pool_a = VerifiedRuntimePool(clock=lambda: 1.0)
        pool_b = VerifiedRuntimePool(clock=lambda: 999.0)
        admit(pool_a, make_result(caps=("coding",)))
        admit(pool_b, make_result(caps=("coding",)))
        first = self.bridge().candidates_for(
            pool_a, ready_health("runtime-a"), "coder", ("coding",),
        ).candidates[0]
        second = self.bridge().candidates_for(
            pool_b, ready_health("runtime-a"), "coder", ("coding",),
        ).candidates[0]
        self.assertEqual(first.agent_id, second.agent_id)
    def test_capability_evidence_from_validated_capabilities_only(self):
        pool = VerifiedRuntimePool(clock=lambda: 1.0)
        result = make_result(caps=("coding", "architecture"), experiment_id="exp-9")
        admit(pool, result)
        candidate = self.bridge().candidates_for(
            pool, ready_health("runtime-a"), "architect", (CapabilityName.ARCHITECTURE,),
        ).candidates[0]
        evidence = candidate.capabilities
        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0].capability, CapabilityName.ARCHITECTURE)
        self.assertIsNone(evidence[0].score)
        self.assertEqual(evidence[0].confidence, CapabilityConfidence.VERIFIED)
        self.assertEqual(evidence[0].source, "exp-9")

    def test_capability_evidence_deterministic_order(self):
        pool = VerifiedRuntimePool(clock=lambda: 1.0)
        admit(pool, make_result(
            caps=("coding", "architecture", "testing", "review"), experiment_id="exp-7",
        ))
        candidate = self.bridge().candidates_for(
            pool, ready_health("runtime-a"), "review",
            (CapabilityName.TESTING, CapabilityName.REVIEW, CapabilityName.ARCHITECTURE),
        ).candidates[0]
        self.assertEqual(
            [item.capability.value for item in candidate.capabilities],
            ["architecture", "review", "testing"],
        )
        for item in candidate.capabilities:
            self.assertIsNone(item.score)
            self.assertEqual(item.confidence, CapabilityConfidence.VERIFIED)
            self.assertEqual(item.source, "exp-7")

    def test_verified_candidate_score_is_none(self):
        pool = VerifiedRuntimePool(clock=lambda: 1.0)
        admit(pool, make_result(caps=("coding", "architecture"), experiment_id="exp-1"))
        candidate = self.bridge().candidates_for(
            pool, ready_health("runtime-a"), "coder", ("coding",),
        ).candidates[0]
        self.assertIsNone(candidate.score)
        for item in candidate.capabilities:
            self.assertIsNone(item.score)

    def test_capability_count_never_produces_score(self):
        pool = VerifiedRuntimePool(clock=lambda: 1.0)
        admit(pool, make_result(caps=("coding", "architecture"), experiment_id="exp-1"),
              required=("coding", "architecture"))
        single = self.bridge().candidates_for(
            pool, ready_health("runtime-a"), "coder", ("coding",),
        ).candidates[0]
        multi = self.bridge().candidates_for(
            pool, ready_health("runtime-a"), "review", ("coding", "architecture"),
        ).candidates[0]
        self.assertEqual(len(single.capabilities), 1)
        self.assertEqual(len(multi.capabilities), 2)
        self.assertIsNone(single.score)
        self.assertIsNone(multi.score)

    def test_score_cannot_be_assigned_by_contract(self):
        pool = VerifiedRuntimePool(clock=lambda: 1.0)
        admit(pool, make_result(caps=("coding",)))
        candidate = self.bridge().candidates_for(
            pool, ready_health("runtime-a"), "coder", ("coding",),
        ).candidates[0]
        with self.assertRaises(Exception):
            candidate.score = 0.0
        with self.assertRaises(ValueError):
            VerifiedRoleCandidate(
                role="coder",
                agent_id=agent_id_for(("r", "p", "m", "f")),
                runtime_id="r", provider_id="p", model_id="m", config_fingerprint="f",
                capabilities=(), required_capabilities=(), rank=1, score=0.0,
            )

    def test_identity_dimensions_preserved_with_agent_id(self):
        pool = VerifiedRuntimePool(clock=lambda: 1.0)
        admit(pool, make_result(caps=("coding",)))
        candidate = self.bridge().candidates_for(
            pool, ready_health("runtime-a"), "coder", ("coding",),
        ).candidates[0]
        self.assertEqual(candidate.runtime_id, "runtime-a")
        self.assertEqual(candidate.provider_id, "provider-a")
        self.assertEqual(candidate.model_id, "model-a")
        self.assertEqual(candidate.config_fingerprint, "fp-a")
        self.assertEqual(candidate.agent_id, agent_id_for(("runtime-a", "provider-a", "model-a", "fp-a")))

    def test_declared_capability_is_never_promoted(self):
        # validated_capabilities carries only coding; a requirement on
        # architecture cannot be satisfied by any declared context because
        # the bridge only ever reads validated evidence.
        pool = VerifiedRuntimePool(clock=lambda: 1.0)
        admit(pool, make_result(caps=("coding",)))
        candidate_set = self.bridge().candidates_for(
            pool, ready_health("runtime-a"), "coder", (CapabilityName.ARCHITECTURE,),
        )
        self.assertEqual(candidate_set.candidates, ())
        import verified_selection_bridge
        self.assertNotIn(
            "capability_context",
            Path(verified_selection_bridge.__file__).read_text(encoding="utf-8"),
        )

    def test_candidate_requires_validated_capability(self):
        pool = VerifiedRuntimePool(clock=lambda: 1.0)
        admit(pool, make_result(caps=("coding",)))
        candidate_set = self.bridge().candidates_for(
            pool, ready_health("runtime-a"), "coder", ("coding", "architecture"),
        )
        self.assertEqual(candidate_set.candidates, ())

    def test_all_non_ready_states_excluded(self):
        for state in (RuntimeState.AUTH_REQUIRED, RuntimeState.UNAVAILABLE, RuntimeState.ERROR):
            with self.subTest(state=state.value):
                pool = VerifiedRuntimePool(clock=lambda: 1.0)
                admit(pool, make_result())
                candidate_set = self.bridge().candidates_for(
                    pool, {"runtime-a": runtime_status("runtime-a", state)},
                    "coder", ("coding",),
                )
                self.assertEqual(candidate_set.candidates, ())

    def test_missing_health_snapshot_excluded(self):
        pool = VerifiedRuntimePool(clock=lambda: 1.0)
        admit(pool, make_result())
        candidate_set = self.bridge().candidates_for(pool, {}, "coder", ("coding",))
        self.assertEqual(candidate_set.candidates, ())

    def test_non_verified_entries_never_become_candidates(self):
        for status in (CandidateValidationStatus.BLOCKED, CandidateValidationStatus.FAILED,
                       CandidateValidationStatus.NOT_VERIFIED):
            with self.subTest(status=status.value):
                pool = VerifiedRuntimePool(clock=lambda: 1.0)
                result = make_result(status=status, caps=())
                # bypass admission so the bridge gate is proven on its own
                pool._entries[result.identity] = VerifiedPoolEntry(result, 1.0, "READY")
                candidate_set = self.bridge().candidates_for(
                    pool, ready_health("runtime-a"), "coder", ("coding",),
                )
                self.assertEqual(candidate_set.candidates, ())
    def test_experiment_id_none_skipped(self):
        pool = VerifiedRuntimePool(clock=lambda: 1.0)
        admit(pool, make_result(caps=("coding",), experiment_id=None), required=("coding",))
        candidate_set = self.bridge().candidates_for(
            pool, ready_health("runtime-a"), "coder", ("coding",),
        )
        self.assertEqual(candidate_set.candidates, ())

    def test_experiment_id_empty_skipped(self):
        pool = VerifiedRuntimePool(clock=lambda: 1.0)
        admit(pool, make_result(caps=("coding",), experiment_id=""), required=("coding",))
        candidate_set = self.bridge().candidates_for(
            pool, ready_health("runtime-a"), "coder", ("coding",),
        )
        self.assertEqual(candidate_set.candidates, ())

    def test_missing_experiment_id_does_not_fail_role_and_valid_remains(self):
        pool = VerifiedRuntimePool(clock=lambda: 1.0)
        admit(pool, make_result(identity=("r-bad", "p", "m", "f"), caps=("coding",),
                                experiment_id=None), required=("coding",))
        admit(pool, make_result(identity=("r-good", "p", "m", "f"), caps=("coding",),
                                experiment_id="exp-good"), required=("coding",))
        candidate_set = self.bridge().candidates_for(
            pool, ready_health("r-bad", "r-good"), "coder", ("coding",),
        )
        self.assertEqual(len(candidate_set.candidates), 1)
        self.assertEqual(candidate_set.candidates[0].agent_id,
                         agent_id_for(("r-good", "p", "m", "f")))
        self.assertEqual(candidate_set.candidates[0].capabilities[0].source, "exp-good")

    def test_multiple_runtimes_providers_models_coexist(self):
        pool = VerifiedRuntimePool(clock=lambda: 1.0)
        identities = [
            ("runtime-a", "provider-b", "model-a", "fp-b"),
            ("runtime-b", "provider-a", "model-b", "fp-c"),
            ("runtime-a", "provider-a", "model-c", "fp-d"),
        ]
        for identity in identities:
            admit(pool, make_result(identity=identity, caps=("coding",)),
                  required=("coding",))
        candidate_set = self.bridge().candidates_for(
            pool, ready_health("runtime-a", "runtime-b"), "coder", ("coding",),
        )
        self.assertEqual(len(candidate_set.candidates), 3)
        seen = {
            (c.runtime_id, c.provider_id, c.model_id, c.config_fingerprint)
            for c in candidate_set.candidates
        }
        self.assertEqual(seen, set(identities))
        self.assertEqual(
            {c.agent_id for c in candidate_set.candidates},
            {agent_id_for(identity) for identity in identities},
        )

    def test_identity_deterministic_regardless_of_insertion_order(self):
        def build(insertion):
            pool = VerifiedRuntimePool(clock=lambda: 1.0)
            for identity in insertion:
                admit(pool, make_result(identity=identity, caps=("coding",)),
                      required=("coding",))
            return self.bridge().candidates_for(
                pool, ready_health("r1", "r2", "r3"), "coder", ("coding",),
            )

        first_order = [("r2", "p", "m", "f"), ("r1", "p", "m", "f"), ("r3", "p", "m", "f")]
        second_order = [("r3", "p", "m", "f"), ("r1", "p", "m", "f"), ("r2", "p", "m", "f")]
        self.assertEqual(build(first_order), build(second_order))
        self.assertEqual(
            [c.runtime_id for c in build(first_order).candidates],
            ["r1", "r2", "r3"],
        )

    def test_deterministic_across_calls(self):
        pool = VerifiedRuntimePool(clock=lambda: 1.0)
        admit(pool, make_result(caps=("coding", "architecture")))
        bridge = self.bridge()
        first = bridge.candidates_for(
            pool, ready_health("runtime-a"), "coder", ("coding",),
        )
        second = bridge.candidates_for(
            pool, ready_health("runtime-a"), "coder", ("coding",),
        )
        self.assertEqual(first, second)

    def test_required_capability_container_forms(self):
        for required in (("coding",), ["coding"], frozenset({"coding"}), {"coding"},
                         (CapabilityName.CODING,)):
            with self.subTest(form=type(required).__name__):
                pool = VerifiedRuntimePool(clock=lambda: 1.0)
                admit(pool, make_result(caps=("coding",)))
                candidate_set = self.bridge().candidates_for(
                    pool, ready_health("runtime-a"), "coder", required,
                )
                self.assertEqual(len(candidate_set.candidates), 1)

    def test_output_immutable_and_secret_free(self):
        pool = VerifiedRuntimePool(clock=lambda: 1.0)
        admit(pool, make_result())
        candidate_set = self.bridge().candidates_for(
            pool, ready_health("runtime-a"), "coder", ("coding",),
        )
        with self.assertRaises(FrozenInstanceError):
            candidate_set.candidates = ()
        with self.assertRaises(FrozenInstanceError):
            candidate_set.candidates[0].runtime_id = "other"
        with self.assertRaises(FrozenInstanceError):
            candidate_set.candidates[0].agent_id = "other"
        surface = repr(candidate_set).lower()
        for marker in ("token", "secret", "api_key", "authorization", "stdout", "stderr"):
            self.assertNotIn(marker, surface)
        with self.assertRaises(ValueError):
            VerifiedRoleCandidate(
                role="coder",
                agent_id=agent_id_for(("r", "p", "m", "token=fp")),
                runtime_id="r", provider_id="p", model_id="m",
                config_fingerprint="token=fp",
                capabilities=(), required_capabilities=(), rank=1,
            )

    def test_bridge_is_read_only_and_never_probes(self):
        pool = VerifiedRuntimePool(clock=lambda: 1.0)
        result = make_result()
        admit(pool, result)
        health = ready_health("runtime-a")
        self.bridge().candidates_for(pool, health, "coder", ("coding",))
        self.assertEqual(pool.identities(), (result.identity,))
        self.assertEqual(health["runtime-a"].status, RuntimeState.READY)
        import verified_selection_bridge
        source = Path(verified_selection_bridge.__file__).read_text(encoding="utf-8")
        for forbidden in ("subprocess", "invoke", "RuntimeHealthController",
                          "GenericRuntimeHealth"):
            self.assertNotIn(forbidden, source)

    def test_no_runtime_names_or_value_branches(self):
        import verified_selection_bridge
        text = Path(verified_selection_bridge.__file__).read_text(encoding="utf-8").lower()
        for name in ("claude", "codex", "gemini", "deepseek", "tiny-agents"):
            self.assertNotIn(name, text)
        for branch in ("runtime_id ==", "provider_id ==", "model_id =="):
            self.assertNotIn(branch, text)

    def test_no_formal_selection_stack_dependency(self):
        import verified_selection_bridge
        source = Path(verified_selection_bridge.__file__).read_text(encoding="utf-8")
        for forbidden in ("role_candidates", "stage_runtime_selection",
                          "selection_plan_bridge", "dual_agent_selection",
                          "orchestrator", "execution_engine", "invocation_plan",
                          "DualAgentPair", "capability_router"):
            self.assertNotIn(forbidden, source)

    def test_imports_are_whitelisted(self):
        import verified_selection_bridge
        source = Path(verified_selection_bridge.__file__).read_text(encoding="utf-8")
        self.assertIn("from candidate_validation import", source)
        self.assertIn("from capability_registry import", source)
        self.assertIn("from runtime_status import", source)
        self.assertIn("from verified_runtime_pool import", source)

    def test_no_invocation_plan_or_pair(self):
        bridge = self.bridge()
        self.assertFalse(hasattr(bridge, "plan"))
        self.assertFalse(hasattr(bridge, "decide"))
        self.assertFalse(hasattr(bridge, "pair"))
        import verified_selection_bridge
        source = Path(verified_selection_bridge.__file__).read_text(encoding="utf-8")
        self.assertNotIn("invocation_plan", source)
        self.assertNotIn("DualAgentPair", source)

    def test_budget_and_loop_guard_untouched(self):
        usage = BudgetUsage()
        guard = LoopGuard()
        before = (usage.total_agent_calls, usage.iterations_used, guard.check("t", "s", "a"))
        pool = VerifiedRuntimePool(clock=lambda: 1.0)
        admit(pool, make_result())
        self.bridge().candidates_for(pool, ready_health("runtime-a"), "coder", ("coding",))
        after = (usage.total_agent_calls, usage.iterations_used, guard.check("t", "s", "a"))
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()