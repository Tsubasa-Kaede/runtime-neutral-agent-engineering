"""Phase 10F unit tests: VerifiedOrchestrator plan-level behaviour.

Offline and mock-only. Locks mode semantics, verified routing (no ReadyPool
fallback), plan-level budget/loop-guard gating without usage consumption,
agent_id mapping discipline and source-level boundary invariants.
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from candidate_validation import (
    CandidateValidationResult,
    CandidateValidationStatus,
    GateResult,
    GateVerdict,
    ValidationGate,
)
from invocation_plan import InvocationPlan
from loop_guard import LoopGuard
from mode_gate import Mode
from runtime_status import HealthEvidence, ReasonCode, RuntimeState, RuntimeStatus
from task_budget import BudgetUsage, TaskBudget
from verified_runtime_pool import VerifiedRuntimePool
from verified_selection_bridge import agent_id_for
from verified_orchestrator import VerifiedOrchestrator


def runtime_status(rid, state=RuntimeState.READY):
    return RuntimeStatus(rid, rid + ".exe", "1", state, "p", None, "managed",
                         ReasonCode.NONE, HealthEvidence("v", "v", "v", "v", "v"), 1, 100)


def verified_result(identity, caps, experiment="exp-1"):
    return CandidateValidationResult(
        identity=identity,
        status=CandidateValidationStatus.VERIFIED,
        gates_passed=frozenset(ValidationGate),
        gate_results=tuple(GateResult(g, GateVerdict.PASS) for g in ValidationGate),
        block_reason=None,
        failure_point=None,
        experiment_id=experiment,
        executed_at=1.0,
        validated_capabilities=tuple(sorted(caps)),
        evidence={},
    )


ALL_CAPS = ("architecture", "coding", "testing", "review")
IDENTITY_A = ("runtime-a", "provider-a", "model-a", "fp-a")
IDENTITY_B = ("runtime-b", "provider-b", "model-b", "fp-b")


def pool_with(*specs):
    """specs: (identity, caps) pairs admitted as VERIFIED."""
    pool = VerifiedRuntimePool(clock=lambda: 1.0)
    for identity, caps in specs:
        pool.admit(verified_result(identity, caps), frozenset(), RuntimeState.READY)
    return pool


def orchestrator(pool=None, health=None, calls=10, usage=None, guard=None):
    return VerifiedOrchestrator(
        pool=pool,
        current_health=health if health is not None else {},
        adapters={},
        budget=TaskBudget(calls, 4),
        usage=usage or BudgetUsage(),
        loop_guard=guard or LoopGuard(max_iterations=4),
    )


def health_map(state=RuntimeState.READY):
    return {
        "runtime-a": runtime_status("runtime-a", state),
        "runtime-b": runtime_status("runtime-b", state),
    }


class ModeTests(unittest.TestCase):
    def test_off_yields_mode_off_empty_plan(self):
        plan = orchestrator(pool_with((IDENTITY_A, ALL_CAPS)), health_map()).plan(
            "t", "redesign architecture across modules", mode=Mode.OFF)
        self.assertIsInstance(plan, InvocationPlan)
        self.assertEqual(plan.stages, ())
        self.assertIn("MODE_OFF", plan.reasons)

    def test_on_routes_verified_path(self):
        orch = orchestrator(pool_with((IDENTITY_A, ALL_CAPS)), health_map())
        plan = orch.plan("t", "fix one function", mode=Mode.ON)
        self.assertTrue(plan.stages)
        agent_ids = {s.agent_id for s in plan.stages}
        self.assertEqual(agent_ids, {agent_id_for(IDENTITY_A)})

    def test_auto_simple_single_coder_stage(self):
        plan = orchestrator(pool_with((IDENTITY_A, ALL_CAPS)), health_map()).plan(
            "t", "fix one function", mode=Mode.AUTO)
        self.assertEqual([s.stage for s in plan.stages], ["coder"])

    def test_auto_medium_coder_then_test(self):
        plan = orchestrator(pool_with((IDENTITY_A, ALL_CAPS)), health_map()).plan(
            "t", "change two related files and add tests", mode=Mode.AUTO)
        self.assertEqual([s.stage for s in plan.stages], ["coder", "test"])

    def test_auto_complex_four_stages(self):
        plan = orchestrator(pool_with((IDENTITY_A, ALL_CAPS)), health_map()).plan(
            "t", "redesign architecture across modules", mode=Mode.AUTO)
        self.assertEqual([s.stage for s in plan.stages],
                         ["architect", "coder", "test", "review"])


class VerifiedRoutingTests(unittest.TestCase):
    def test_enabled_pool_uses_verified_agent_ids(self):
        plan = orchestrator(pool_with((IDENTITY_A, ALL_CAPS)), health_map()).plan(
            "t", "redesign architecture across modules", mode=Mode.ON)
        for stage in plan.stages:
            self.assertEqual(stage.agent_id, agent_id_for(IDENTITY_A))
            self.assertEqual(stage.runtime_id, "runtime-a")

    def test_disabled_pool_does_not_route_verified(self):
        plan = orchestrator(pool=None, health={}).plan(
            "t", "redesign architecture across modules", mode=Mode.ON)
        self.assertEqual(plan.stages, ())
        self.assertIn("VERIFIED_POOL_NOT_ENABLED", plan.reasons)

    def test_empty_pool_reports_no_capable_agent(self):
        plan = orchestrator(pool_with(), health_map()).plan(
            "t", "redesign architecture across modules", mode=Mode.ON)
        self.assertEqual(plan.stages, ())
        self.assertIn("NO_CAPABLE_AGENT", plan.reasons)

    def test_no_ready_pool_fallback_when_candidates_missing(self):
        # identity validated only for coding: complex needs all four roles,
        # so the verified path must fail honestly, never borrow ReadyPool.
        plan = orchestrator(pool_with((IDENTITY_A, ("coding",))), health_map()).plan(
            "t", "redesign architecture across modules", mode=Mode.ON)
        self.assertEqual(plan.stages, ())
        self.assertIn("NO_CAPABLE_AGENT", plan.reasons)

    def test_non_ready_health_excludes_candidate(self):
        health = {"runtime-a": runtime_status("runtime-a", RuntimeState.AUTH_REQUIRED)}
        plan = orchestrator(pool_with((IDENTITY_A, ALL_CAPS)), health).plan(
            "t", "fix one function", mode=Mode.ON)
        self.assertEqual(plan.stages, ())
        self.assertIn("NO_CAPABLE_AGENT", plan.reasons)


class PlanGatingTests(unittest.TestCase):
    def test_plan_level_budget_exhausted(self):
        usage = BudgetUsage()
        usage.total_agent_calls = 10
        orch = orchestrator(pool_with((IDENTITY_A, ALL_CAPS)), health_map(),
                            calls=10, usage=usage)
        plan = orch.plan("t", "fix one function", mode=Mode.ON)
        self.assertEqual(plan.stages, ())
        self.assertIn("BUDGET_EXHAUSTED", plan.reasons)

    def test_plan_does_not_reserve_budget(self):
        usage = BudgetUsage()
        orchestrator(pool_with((IDENTITY_A, ALL_CAPS)), health_map(), usage=usage).plan(
            "t", "redesign architecture across modules", mode=Mode.ON)
        self.assertEqual(usage.total_agent_calls, 0)

    def test_plan_level_loop_guard_rejection(self):
        guard = LoopGuard(max_iterations=4)
        guard.record("t", "coder", agent_id_for(IDENTITY_A))
        orch = orchestrator(pool_with((IDENTITY_A, ALL_CAPS)), health_map(), guard=guard)
        plan = orch.plan("t", "fix one function", mode=Mode.ON)
        self.assertEqual(plan.stages, ())
        self.assertIn("LOOP_GUARD_REJECTED", plan.reasons)

    def test_plan_does_not_record_loop_guard(self):
        guard = LoopGuard(max_iterations=4)
        orchestrator(pool_with((IDENTITY_A, ALL_CAPS)), health_map(), guard=guard).plan(
            "t", "redesign architecture across modules", mode=Mode.ON)
        self.assertEqual(guard.check("t", "architect", agent_id_for(IDENTITY_A)), "ALLOW")


class IdentityAdapterTests(unittest.TestCase):
    def test_multi_runtime_multi_provider_model_mapping(self):
        # a validated for architecture/review, b for coding/testing -> MULTI
        pool = pool_with((IDENTITY_A, ("architecture", "review")),
                         (IDENTITY_B, ("coding", "testing")))
        plan = orchestrator(pool, health_map()).plan(
            "t", "redesign architecture across modules", mode=Mode.ON)
        mapping = {s.stage: s.runtime_id for s in plan.stages}
        self.assertEqual(mapping["architect"], "runtime-a")
        self.assertEqual(mapping["review"], "runtime-a")
        self.assertEqual(mapping["coder"], "runtime-b")
        self.assertEqual(mapping["test"], "runtime-b")
        for stage in plan.stages:
            identity = IDENTITY_A if stage.runtime_id == "runtime-a" else IDENTITY_B
            self.assertEqual(stage.agent_id, agent_id_for(identity))

    def test_runtime_id_never_used_as_agent_id(self):
        plan = orchestrator(pool_with((IDENTITY_A, ALL_CAPS)), health_map()).plan(
            "t", "fix one function", mode=Mode.ON)
        agent_ids = {s.agent_id for s in plan.stages}
        self.assertNotIn("runtime-a", agent_ids)
        self.assertEqual(agent_ids, {agent_id_for(IDENTITY_A)})


class BoundaryScanTests(unittest.TestCase):
    def test_source_invariants(self):
        import verified_orchestrator
        source = Path(verified_orchestrator.__file__).read_text(encoding="utf-8")
        lowered = source.lower()
        # no process launching, no registry scoring, no capability_context
        for forbidden in ("subprocess", "capability_registry", "capability_context",
                          "dualagentpair"):
            self.assertNotIn(forbidden, lowered)
        # no ReadyPool smuggling of any flavour
        for forbidden in ("readypool", "runtime_pool_construction",
                          "from verified_runtime_pool import"):
            self.assertNotIn(forbidden, lowered)
        # no runtime-name hardcoding (names assembled at runtime for the scan)
        names = tuple("".join(p) for p in (
            ("cl", "aude"), ("co", "dex"), ("deep", "seek"),
            ("tiny", "-agents"), ("tiny", "_agents"), ("gem", "ini"),
        ))
        for name in names:
            self.assertNotIn(name, lowered)
        # no health probing of its own
        self.assertNotIn("runtime_health", lowered)


if __name__ == "__main__":
    unittest.main()
