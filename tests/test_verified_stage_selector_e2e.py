"""Phase 10E: Verified Stage Selector offline E2E - mock-only.

Full verified chain: fake adapter -> VERIFIED validation -> pool admission
-> bridge (per role) -> verified selector -> selection plan bridge ->
InvocationPlan. No runtime is spawned and no real invocation happens.
"""
import json
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
from runtime_status import AuthenticationState
from stage_runtime_selection import SelectionMode, SelectionReason
from task_budget import BudgetUsage, TaskBudget
from task_classifier import Complexity
from verified_runtime_pool import VerifiedRuntimePool
from verified_selection_bridge import VerifiedSelectionBridge, agent_id_for
from verified_stage_selector import VerifiedStageSelector, _ROLE_REQUIREMENTS, verified_plan


def mock_probe(rid):
    probe = Mock(spec=["discover", "check_authentication", "check_provider_model", "minimal_health_check"])
    probe.discover.return_value = RuntimeDiscovery(rid, True, "1.0", None, frozenset())
    probe.check_authentication.return_value = type("A", (), {
        "state": AuthenticationState.AUTHENTICATED, "method": "managed", "reason_code": None})()
    probe.check_provider_model.return_value = type("P", (), {
        "provider": "p", "model": "m", "available": True, "reason_code": None})()
    probe.minimal_health_check.return_value = type("H", (), {
        "passed": True, "reason_code": None, "trace": None, "output_class": "exact_ok"})()
    return probe


class FakeAdapter:
    def __init__(self, rid, pid, mid, fp, probe):
        self.runtime_id, self.provider_id, self.model_id = rid, pid, mid
        self.config_fingerprint = fp
        self.capability_context = ("architecture", "coding", "testing", "review")
        self.probe = probe
        self.invocation_spec = {"timeout_seconds": 30}


def pass_executor(caps=("coding",)):
    def executor(gate):
        return GateResult(gate, GateVerdict.PASS, capabilities=caps)
    return executor


def validated(adapter, caps, experiment="e2e"):
    candidate = candidate_from_adapter(adapter)
    result = CandidateValidationRunner().run(
        candidate, pass_executor(caps), clock=lambda: 5.0, experiment_id=experiment,
    )
    health = RuntimeHealthController(ttl_seconds=60).check(adapter.probe)
    return result, health


class VerifiedStageSelectorE2ETests(unittest.TestCase):
    def adapter(self, rid, pid="provider-a", mid="model-a", fp="fp-a"):
        return FakeAdapter(rid, pid, mid, fp, mock_probe(rid))

    def admit_all(self, adapters, experiments, caps_list):
        pool = VerifiedRuntimePool(clock=lambda: 10.0)
        health = {}
        for adapter, experiment, caps in zip(adapters, experiments, caps_list):
            result, status = validated(adapter, caps, experiment=experiment)
            self.assertEqual(result.status, CandidateValidationStatus.VERIFIED)
            pool.admit(result, frozenset(caps), status.status)
            health[adapter.runtime_id] = status
        return pool, health

    def role_sets(self, pool, health):
        return {
            role: VerifiedSelectionBridge().candidates_for(pool, health, role, requirements)
            for role, requirements in _ROLE_REQUIREMENTS.items()
        }

    def test_simple_pipeline_produces_invocation_plan(self):
        adapter = self.adapter("runtime-code")
        pool, health = self.admit_all([adapter], ["e2e-simple"], [("coding",)])
        selection = VerifiedStageSelector().select(self.role_sets(pool, health), Complexity.SIMPLE)
        self.assertEqual(selection.mode, SelectionMode.SINGLE)
        self.assertEqual(selection.reason, SelectionReason.SIMPLE_TASK)
        plan = verified_plan(pool, health, "task-1", "AUTO", "SIMPLE", TaskBudget(4, 4), BudgetUsage())
        self.assertEqual([s.stage for s in plan.stages], ["coder"])
        expected_agent = agent_id_for(("runtime-code", "provider-a", "model-a", "fp-a"))
        self.assertEqual(plan.stages[0].agent_id, expected_agent)
        self.assertEqual(plan.stages[0].runtime_id, "runtime-code")
        self.assertEqual(plan.selected_agents, (expected_agent,))
        self.assertEqual(plan.complexity, "SIMPLE")
        self.assertEqual(plan.reasons, ())

    def test_complex_multi_pipeline_produces_invocation_plan(self):
        arch = self.adapter("runtime-arch", pid="provider-arch", fp="fp-arch")
        code = self.adapter("runtime-code", pid="provider-code", fp="fp-code")
        pool, health = self.admit_all(
            [arch, code],
            ["exp-arch", "exp-code"],
            [("architecture",), ("coding", "testing", "review")],
        )
        selection = VerifiedStageSelector().select(self.role_sets(pool, health), Complexity.COMPLEX)
        self.assertEqual(selection.mode, SelectionMode.MULTI)
        self.assertEqual(selection.reason, SelectionReason.CLEAR_SPECIALIZATION)
        plan = verified_plan(pool, health, "task-2", "AUTO", "COMPLEX", TaskBudget(4, 4), BudgetUsage())
        self.assertEqual(
            [s.stage for s in plan.stages],
            ["architect", "coder", "test", "review"],
        )
        self.assertEqual(plan.stages[0].runtime_id, "runtime-arch")
        self.assertEqual(plan.stages[1].runtime_id, "runtime-code")
        self.assertEqual(plan.stages[0].agent_id, agent_id_for(("runtime-arch", "provider-arch", "model-a", "fp-arch")))
        self.assertEqual(plan.stages[1].agent_id, agent_id_for(("runtime-code", "provider-code", "model-a", "fp-code")))
    def test_missing_experiment_id_skipped_in_plan(self):
        good = self.adapter("runtime-good")
        missing = self.adapter("runtime-missing")
        pool, health = self.admit_all(
            [good, missing], ["good-exp", None], [("coding",), ("coding",)],
        )
        plan = verified_plan(pool, health, "task-3", "AUTO", "SIMPLE", TaskBudget(4, 4), BudgetUsage())
        self.assertEqual([s.stage for s in plan.stages], ["coder"])
        self.assertEqual(plan.stages[0].agent_id, agent_id_for(("runtime-good", "provider-a", "model-a", "fp-a")))
        self.assertEqual(plan.stages[0].runtime_id, "runtime-good")

    def test_plan_is_score_free_and_budget_guard_untouched(self):
        adapter = self.adapter("runtime-code")
        pool, health = self.admit_all([adapter], ["e2e-invariants"], [("coding",)])
        usage, guard = BudgetUsage(), LoopGuard()
        plan = verified_plan(pool, health, "task-4", "AUTO", "SIMPLE", TaskBudget(4, 4), usage)
        self.assertNotIn("score", json.dumps(plan.to_dict()))
        self.assertEqual(plan.budget_snapshot.get("max_agent_calls"), 4)
        self.assertEqual(usage.total_agent_calls, 0)
        self.assertEqual(usage.iterations_used, 0)
        self.assertEqual(guard.check("t", "s", "a"), "ALLOW")

    def test_bridge_never_probes_after_plan(self):
        adapter = self.adapter("runtime-code")
        pool, health = self.admit_all([adapter], ["e2e-probes"], [("coding",)])
        probe = adapter.probe
        probe.reset_mock()
        verified_plan(pool, health, "task-5", "AUTO", "SIMPLE", TaskBudget(4, 4), BudgetUsage())
        self.assertEqual(probe.method_calls, [])
        import verified_stage_selector
        source = Path(verified_stage_selector.__file__).read_text(encoding="utf-8")
        self.assertNotIn("subprocess", source)
        self.assertNotIn("invoke", source)

    def test_real_provider_invocation_stays_opt_in(self):
        self.assertNotEqual(os.environ.get("RUN_REAL_PROVIDER_TESTS", ""), "1")


if __name__ == "__main__":
    unittest.main()