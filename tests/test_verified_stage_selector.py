"""Phase 10E: Verified Stage Selector unit tests - offline, mock-only.

Locks the approved score-less integration contract: the verified selector
consumes only VerifiedRoleCandidateSet, picks the rank-1 candidate per
stage, never reads or computes scores, decides SINGLE/MULTI structurally
by runtime diversity, converges to the coder-best runtime when MULTI is
not allowed, and stays deterministic, immutable, secret-free and
runtime-neutral.
"""
import sys
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from loop_guard import LoopGuard
from stage_runtime_selection import (
    RuntimeSelectionResult,
    SelectionMode,
    SelectionReason,
    StageSelection,
)
from task_budget import BudgetUsage
from task_classifier import Complexity
from verified_selection_bridge import (
    VerifiedRoleCandidate,
    VerifiedRoleCandidateSet,
    agent_id_for,
)
from verified_stage_selector import VerifiedStageSelector, _ROLE_REQUIREMENTS


def candidate(role, runtime_id, rank=1, provider="provider-a", model="model-a", fingerprint="fp-a"):
    return VerifiedRoleCandidate(
        role=role,
        agent_id=agent_id_for((runtime_id, provider, model, fingerprint)),
        runtime_id=runtime_id,
        provider_id=provider,
        model_id=model,
        config_fingerprint=fingerprint,
        capabilities=(),
        required_capabilities=(),
        rank=rank,
    )


def role_set(role, *candidates):
    return VerifiedRoleCandidateSet(role, tuple(candidates))


class VerifiedStageSelectorTests(unittest.TestCase):
    def selector(self):
        return VerifiedStageSelector()

    def test_role_requirements_mapping(self):
        self.assertEqual(_ROLE_REQUIREMENTS["architect"], ("architecture",))
        self.assertEqual(_ROLE_REQUIREMENTS["coder"], ("coding",))
        self.assertEqual(_ROLE_REQUIREMENTS["test"], ("testing",))
        self.assertEqual(_ROLE_REQUIREMENTS["review"], ("review",))
        self.assertEqual(set(_ROLE_REQUIREMENTS), {"architect", "coder", "test", "review"})

    def test_simple_selects_coder_single_simple_task(self):
        result = self.selector().select(
            {"coder": role_set("coder", candidate("coder", "runtime-a"))},
            Complexity.SIMPLE,
        )
        self.assertIsInstance(result, RuntimeSelectionResult)
        self.assertEqual(result.mode, SelectionMode.SINGLE)
        self.assertEqual(result.reason, SelectionReason.SIMPLE_TASK)
        self.assertEqual(len(result.stage_selections), 1)
        stage = result.stage_selections[0]
        self.assertEqual(stage.stage, "coder")
        self.assertEqual(stage.runtime_id, "runtime-a")
        self.assertEqual(stage.agent_id, agent_id_for(("runtime-a", "provider-a", "model-a", "fp-a")))
        self.assertIsNone(stage.score)

    def test_medium_selects_coder_and_test(self):
        result = self.selector().select(
            {
                "coder": role_set("coder", candidate("coder", "runtime-a")),
                "test": role_set("test", candidate("test", "runtime-a")),
            },
            Complexity.MEDIUM,
        )
        self.assertEqual([s.stage for s in result.stage_selections], ["coder", "test"])
        self.assertEqual(result.mode, SelectionMode.SINGLE)
        self.assertEqual(result.reason, SelectionReason.SINGLE_RUNTIME_POOL)

    def test_complex_same_runtime_single_runtime_pool(self):
        sets = {
            role: role_set(role, candidate(role, "runtime-a"))
            for role in ("architect", "coder", "test", "review")
        }
        result = self.selector().select(sets, Complexity.COMPLEX)
        self.assertEqual(
            [s.stage for s in result.stage_selections],
            ["architect", "coder", "test", "review"],
        )
        self.assertEqual(result.mode, SelectionMode.SINGLE)
        self.assertEqual(result.reason, SelectionReason.SINGLE_RUNTIME_POOL)
        self.assertEqual({s.runtime_id for s in result.stage_selections}, {"runtime-a"})

    def test_complex_cross_runtime_multi_clear_specialization(self):
        sets = {
            "architect": role_set("architect", candidate("architect", "runtime-arch")),
            "coder": role_set("coder", candidate("coder", "runtime-code")),
            "test": role_set("test", candidate("test", "runtime-code")),
            "review": role_set("review", candidate("review", "runtime-arch")),
        }
        result = self.selector().select(sets, Complexity.COMPLEX)
        self.assertEqual(result.mode, SelectionMode.MULTI)
        self.assertEqual(result.reason, SelectionReason.CLEAR_SPECIALIZATION)
        self.assertEqual(result.stage_selections[0].runtime_id, "runtime-arch")
        self.assertEqual(result.stage_selections[1].runtime_id, "runtime-code")

    def test_empty_candidates_no_capable_agent(self):
        result = self.selector().select({"coder": role_set("coder")}, Complexity.SIMPLE)
        self.assertEqual(result.mode, SelectionMode.SINGLE)
        self.assertEqual(result.stage_selections, ())
        self.assertEqual(result.reason, SelectionReason.NO_CAPABLE_AGENT)

    def test_missing_role_set_no_capable_agent(self):
        result = self.selector().select(
            {"coder": role_set("coder", candidate("coder", "runtime-a"))},
            Complexity.COMPLEX,
        )
        self.assertEqual(result.stage_selections, ())
        self.assertEqual(result.reason, SelectionReason.NO_CAPABLE_AGENT)

    def test_partial_candidates_no_capable_agent(self):
        sets = {
            "architect": role_set("architect", candidate("architect", "runtime-a")),
            "coder": role_set("coder", candidate("coder", "runtime-a")),
            "test": role_set("test"),
            "review": role_set("review", candidate("review", "runtime-a")),
        }
        result = self.selector().select(sets, Complexity.COMPLEX)
        self.assertEqual(result.stage_selections, ())
        self.assertEqual(result.reason, SelectionReason.NO_CAPABLE_AGENT)
    def test_single_convergence_to_coder_runtime(self):
        sets = {
            "coder": role_set("coder", candidate("coder", "runtime-c")),
            "test": role_set(
                "test",
                candidate("test", "runtime-t", rank=1),
                candidate("test", "runtime-c", rank=2),
            ),
        }
        result = self.selector().select(sets, Complexity.MEDIUM)
        self.assertEqual(result.mode, SelectionMode.SINGLE)
        self.assertEqual([s.runtime_id for s in result.stage_selections], ["runtime-c", "runtime-c"])
        self.assertEqual(result.reason, SelectionReason.SINGLE_RUNTIME_POOL)

    def test_convergence_falls_back_to_set_best_when_target_missing(self):
        sets = {
            "coder": role_set("coder", candidate("coder", "runtime-c")),
            "test": role_set("test", candidate("test", "runtime-t")),
        }
        result = self.selector().select(sets, Complexity.MEDIUM)
        self.assertEqual(result.mode, SelectionMode.SINGLE)
        self.assertEqual(result.stage_selections[0].runtime_id, "runtime-c")
        self.assertEqual(result.stage_selections[1].runtime_id, "runtime-t")

    def test_same_identity_used_for_multiple_stages(self):
        same = candidate("coder", "runtime-a")
        sets = {
            "coder": role_set("coder", same),
            "test": role_set("test", same),
        }
        result = self.selector().select(sets, Complexity.MEDIUM)
        self.assertEqual(result.mode, SelectionMode.SINGLE)
        self.assertEqual({s.agent_id for s in result.stage_selections}, {same.agent_id})
        self.assertEqual({s.runtime_id for s in result.stage_selections}, {"runtime-a"})

    def test_same_runtime_different_provider_model_is_single(self):
        arch = candidate("architect", "runtime-a", provider="provider-x", model="model-x")
        coder = candidate("coder", "runtime-a", provider="provider-y", model="model-y")
        sets = {
            "architect": role_set("architect", arch),
            "coder": role_set("coder", coder),
            "test": role_set("test", candidate("test", "runtime-a")),
            "review": role_set("review", candidate("review", "runtime-a")),
        }
        result = self.selector().select(sets, Complexity.COMPLEX)
        self.assertEqual(result.mode, SelectionMode.SINGLE)
        self.assertEqual({s.runtime_id for s in result.stage_selections}, {"runtime-a"})
        self.assertEqual(result.stage_selections[0].agent_id, arch.agent_id)
        self.assertEqual(result.stage_selections[1].agent_id, coder.agent_id)

    def test_score_always_none(self):
        sets = {
            "architect": role_set("architect", candidate("architect", "runtime-arch")),
            "coder": role_set("coder", candidate("coder", "runtime-code")),
            "test": role_set("test", candidate("test", "runtime-code")),
            "review": role_set("review", candidate("review", "runtime-arch")),
        }
        result = self.selector().select(sets, Complexity.COMPLEX)
        for stage in result.stage_selections:
            self.assertIsNone(stage.score)

    def test_deterministic_across_calls(self):
        sets = {
            "architect": role_set("architect", candidate("architect", "runtime-arch")),
            "coder": role_set("coder", candidate("coder", "runtime-code")),
            "test": role_set("test", candidate("test", "runtime-code")),
            "review": role_set("review", candidate("review", "runtime-arch")),
        }
        selector = self.selector()
        self.assertEqual(selector.select(sets, Complexity.COMPLEX), selector.select(sets, Complexity.COMPLEX))

    def test_output_immutable_and_secret_free(self):
        result = self.selector().select(
            {"coder": role_set("coder", candidate("coder", "runtime-a"))},
            Complexity.SIMPLE,
        )
        with self.assertRaises(FrozenInstanceError):
            result.stage_selections = ()
        with self.assertRaises(FrozenInstanceError):
            result.stage_selections[0].runtime_id = "other"
        surface = repr(result).lower()
        for marker in ("token", "secret", "api_key", "authorization", "stdout", "stderr"):
            self.assertNotIn(marker, surface)

    def test_no_runtime_names_or_provider_model_branches(self):
        import verified_stage_selector
        text = Path(verified_stage_selector.__file__).read_text(encoding="utf-8").lower()
        for name in ("claude", "codex", "gemini", "deepseek", "tiny-agents"):
            self.assertNotIn(name, text)
        for branch in ("provider_id ==", "model_id =="):
            self.assertNotIn(branch, text)

    def test_selector_never_reads_score_or_registry(self):
        import verified_stage_selector
        source = Path(verified_stage_selector.__file__).read_text(encoding="utf-8")
        self.assertNotIn(".score", source)
        for forbidden in ("capability_registry", "capability_context", "subprocess", "invoke",
                          "orchestrator", "execution_engine", "RuntimeHealthController",
                          "GenericRuntimeHealth", "runtime_health"):
            self.assertNotIn(forbidden, source)

    def test_stage_selection_score_contract_allows_none(self):
        hint = str(StageSelection.__dataclass_fields__["score"].type)
        self.assertIn("None", hint)
    def test_budget_and_loop_guard_untouched(self):
        usage, guard = BudgetUsage(), LoopGuard()
        before = (usage.total_agent_calls, usage.iterations_used, guard.check("t", "s", "a"))
        self.selector().select(
            {"coder": role_set("coder", candidate("coder", "runtime-a"))},
            Complexity.SIMPLE,
        )
        after = (usage.total_agent_calls, usage.iterations_used, guard.check("t", "s", "a"))
        self.assertEqual(before, after)
        self.assertFalse(hasattr(VerifiedStageSelector, "invoke"))
        self.assertFalse(hasattr(VerifiedStageSelector, "plan"))


if __name__ == "__main__":
    unittest.main()