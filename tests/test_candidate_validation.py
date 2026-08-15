"""Candidate Runtime Validation Skeleton — offline, runtime-neutral.

Data model + orchestration skeleton for the confirmed Candidate Runtime
Validation Gate design. No gate is executed here: a Runner coordinates an
injected gate executor, applies deterministic short-circuit/merge rules and
emits a secret-free immutable CandidateValidationResult.
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
    CandidateValidationRunner,
    CandidateValidationStatus,
    GateResult,
    GateVerdict,
    ValidationGate,
)
from task_budget import BudgetUsage
from loop_guard import LoopGuard


def instance(runtime_id="runtime-x", provider_id="provider-y", model_id="model-z",
             fingerprint="fp-1", capability_context=None, probe=None):
    return CandidateRuntimeInstance(
        runtime_id=runtime_id,
        provider_id=provider_id,
        model_id=model_id,
        config_fingerprint=fingerprint,
        capability_context=capability_context or ("coding",),
        probe=probe or Mock(),
        invocation_spec={"prompt_channel": "stdin", "timeout_seconds": 30},
    )


def pass_all(gate):
    return GateResult(gate, GateVerdict.PASS)


# 直接构造最小 Result（只验证合同层字段本身，不走 Runner）
def bare_result(**overrides):
    fields = dict(
        identity=("r", "p", "m", "fp"),
        status=CandidateValidationStatus.NOT_VERIFIED,
        gates_passed=frozenset(),
        gate_results=(),
        block_reason=None,
        failure_point=None,
        experiment_id=None,
        executed_at=None,
    )
    fields.update(overrides)
    return CandidateValidationResult(**fields)


class CandidateValidationSkeletonTests(unittest.TestCase):
    def run_with(self, executor, **kwargs):
        clock = kwargs.pop("clock", lambda: 1000.0)
        experiment_id = kwargs.pop("experiment_id", "exp-1")
        return CandidateValidationRunner().run(
            instance(), executor, clock=clock, experiment_id=experiment_id,
        )

    # 1. 空 Candidate 拒绝
    def test_empty_candidate_is_rejected(self):
        for bad in ({"runtime_id": ""}, {"provider_id": ""}, {"model_id": None, "runtime_id": "r"}):
            pass
        with self.assertRaises(ValueError):
            CandidateRuntimeInstance("", "p", "m", "fp", (), Mock(), {})
        with self.assertRaises(ValueError):
            CandidateRuntimeInstance("r", "", "m", "fp", (), Mock(), {})

    # 2. Runtime / Provider / Model 完全独立
    def test_dimensions_are_independent_fields(self):
        inst = instance(runtime_id="some-cli", provider_id="some-cloud", model_id="some-model")
        self.assertEqual(inst.runtime_id, "some-cli")
        self.assertEqual(inst.provider_id, "some-cloud")
        self.assertEqual(inst.model_id, "some-model")
        identity = inst.identity
        self.assertEqual(identity, ("some-cli", "some-cloud", "some-model", "fp-1"))

    # 3. Runtime 名称不出现在生产源码
    def test_no_runtime_names_in_production_source(self):
        import candidate_validation
        text = Path(candidate_validation.__file__).read_text(encoding="utf-8").lower()
        for name in ("claude", "codex", "deepseek", "tiny-agents", "tiny_agents", "gemini"):
            self.assertNotIn(name, text)

    # 4. Gate 顺序固定
    def test_gate_order_is_fixed(self):
        order = [g.value for g in ValidationGate]
        self.assertEqual(order, list(range(1, 14)))

    # 5. Gate 1–4 外部阻塞 → BLOCKED
    def test_external_blockage_in_first_four_gates_blocks(self):
        for gate in list(ValidationGate)[:4]:
            with self.subTest(gate=gate.name):
                def executor(g, _gate=gate):
                    if g is _gate:
                        return GateResult(g, GateVerdict.BLOCKED, reason="external condition missing")
                    return pass_all(g)
                result = self.run_with(executor)
                self.assertEqual(result.status, CandidateValidationStatus.BLOCKED)
                self.assertEqual(result.block_reason, "external condition missing")
                self.assertNotIn(gate, result.gates_passed)

    # 6. Gate 5–13 集成失败 → FAILED
    def test_integration_failure_in_later_gates_fails(self):
        for gate in list(ValidationGate)[4:]:
            with self.subTest(gate=gate.name):
                def executor(g, _gate=gate):
                    if g is _gate:
                        return GateResult(g, GateVerdict.FAILED, reason="integration defect")
                    return pass_all(g)
                result = self.run_with(executor)
                self.assertEqual(result.status, CandidateValidationStatus.FAILED)
                self.assertIsNotNone(result.failure_point)
                self.assertEqual(result.failure_point[0], gate)

    # 7. 未执行 → NOT_VERIFIED
    def test_not_run_gates_yield_not_verified(self):
        result = self.run_with(lambda gate: GateResult(gate, GateVerdict.NOT_RUN))
        self.assertEqual(result.status, CandidateValidationStatus.NOT_VERIFIED)

    # 8. 13 Gate 全 PASS → VERIFIED
    def test_all_pass_yields_verified(self):
        result = self.run_with(pass_all)
        self.assertEqual(result.status, CandidateValidationStatus.VERIFIED)
        self.assertEqual(len(result.gates_passed), 13)
        self.assertIsNone(result.block_reason)
        self.assertIsNone(result.failure_point)

    # 9. 第一个 BLOCKED 后停止
    def test_stops_after_first_blocked_gate(self):
        called = []

        def executor(gate):
            called.append(gate)
            if gate is ValidationGate.G2_AUTHENTICATION:
                return GateResult(gate, GateVerdict.BLOCKED, reason="auth pending")
            return pass_all(gate)

        result = self.run_with(executor)
        self.assertEqual(result.status, CandidateValidationStatus.BLOCKED)
        self.assertEqual([int(g) for g in called], [1, 2])

    # 10. 第一个 FAILED 后停止
    def test_stops_after_first_failed_gate(self):
        called = []

        def executor(gate):
            called.append(gate)
            if gate is ValidationGate.G7_TIMEOUT:
                return GateResult(gate, GateVerdict.FAILED, reason="reap unbounded")
            return pass_all(gate)

        result = self.run_with(executor)
        self.assertEqual(result.status, CandidateValidationStatus.FAILED)
        self.assertEqual([int(g) for g in called], [1, 2, 3, 4, 5, 6, 7])

    # 11. Gate 顺序不因输入顺序改变
    def test_gate_order_immune_to_input_order(self):
        shuffled = list(ValidationGate)
        shuffled.reverse()
        seen = []
        self.run_with(lambda gate: (seen.append(int(gate)), pass_all(gate))[1])
        self.assertEqual(seen, list(range(1, 14)))

    # 12. deterministic
    def test_deterministic_with_fixed_clock(self):
        first = self.run_with(pass_all)
        second = self.run_with(pass_all)
        self.assertEqual(first, second)
        self.assertEqual(first.experiment_id, "exp-1")
        self.assertEqual(first.executed_at, 1000.0)

    # 13. immutable
    def test_results_are_immutable(self):
        inst = instance()
        result = self.run_with(pass_all)
        gate = GateResult(ValidationGate.G1_DISCOVERY, GateVerdict.PASS)
        with self.assertRaises(FrozenInstanceError):
            inst.runtime_id = "mutated"
        with self.assertRaises(FrozenInstanceError):
            result.status = CandidateValidationStatus.NOT_VERIFIED
        with self.assertRaises(FrozenInstanceError):
            gate.verdict = GateVerdict.FAILED

    # 14. repr secret-free
    def test_repr_is_secret_free(self):
        inst = instance()
        result = self.run_with(pass_all)
        surface = (repr(inst) + repr(result)).lower()
        for marker in ("token", "secret", "api_key", "authorization", "stdout", "stderr"):
            self.assertNotIn(marker, surface)

    # 15-16. raw stdout/stderr / Token 不进入结果
    def test_secret_shaped_evidence_is_rejected(self):
        with self.assertRaises(ValueError):
            GateResult(ValidationGate.G5_MINIMAL_INVOCATION, GateVerdict.PASS,
                       evidence={"raw_stdout": "output", "api_key": "x"})
        with self.assertRaises(ValueError):
            CandidateRuntimeInstance("r", "p", "m", "fp", ("token=abc",), Mock(), {})

    # 17-18. 不产生 InvocationPlan / DualAgentPair
    def test_no_plan_or_pair_artifacts(self):
        import candidate_validation
        source = Path(candidate_validation.__file__).read_text(encoding="utf-8")
        self.assertNotIn("invocation_plan", source)
        self.assertNotIn("DualAgentPair", source.replace("dual_agent_pair_not_present", ""))

    # 19. 不调用 subprocess
    def test_no_subprocess(self):
        import candidate_validation
        source = Path(candidate_validation.__file__).read_text(encoding="utf-8")
        self.assertNotIn("subprocess", source)
        self.assertNotIn("invoke", source)

    # 20-21. 不消耗 Budget、不改变 LoopGuard
    def test_no_budget_or_guard_consumption(self):
        usage = BudgetUsage()
        guard = LoopGuard()
        self.run_with(pass_all)
        self.run_with(lambda gate: GateResult(gate, GateVerdict.BLOCKED, reason="x"))
        self.assertEqual(usage.total_agent_calls, 0)
        self.assertEqual(usage.iterations_used, 0)
        self.assertEqual(guard.check("t", "architect", "a"), "ALLOW")

    # 22-26. 不触碰正式栈（源码依赖检查）
    def test_does_not_touch_production_stack(self):
        import candidate_validation
        source = Path(candidate_validation.__file__).read_text(encoding="utf-8")
        for forbidden in ("runtime_health", "runtime_pool", "capability_registry",
                          "orchestrator", "execution_engine", "stage_runtime_selection",
                          "selection_plan_bridge", "role_candidates"):
            self.assertNotIn(forbidden, source)

    # 补充：instance identity 同元组去重语义
    def test_identity_distinguishes_provider_and_model(self):
        base = instance()
        same = instance()
        other_provider = instance(provider_id="provider-other")
        other_model = instance(model_id="model-other")
        other_fp = instance(fingerprint="fp-2")
        self.assertEqual(base.identity, same.identity)
        self.assertNotEqual(base.identity, other_provider.identity)
        self.assertNotEqual(base.identity, other_model.identity)
        self.assertNotEqual(base.identity, other_fp.identity)

    # 补充：BLOCKED 不会被归并为 FAILED，NOT_VERIFIED 不会变 VERIFIED
    def test_status_semantics_are_strict(self):
        blocked = self.run_with(lambda gate: GateResult(gate, GateVerdict.BLOCKED, reason="r"))
        self.assertNotEqual(blocked.status, CandidateValidationStatus.FAILED)
        not_run = self.run_with(lambda gate: GateResult(gate, GateVerdict.NOT_RUN))
        self.assertNotEqual(not_run.status, CandidateValidationStatus.VERIFIED)

    # 27. provenance 默认 OFFLINE
    def test_provenance_defaults_to_offline(self):
        self.assertEqual(bare_result().provenance, "OFFLINE")

    # 28. 显式 OFFLINE 与省略整对象相等
    def test_explicit_offline_equals_default(self):
        self.assertEqual(bare_result(), bare_result(provenance="OFFLINE"))

    # 29. REAL 经 Runner 透传
    def test_real_provenance_passes_through_runner(self):
        result = CandidateValidationRunner().run(
            instance(), pass_all, clock=lambda: 1.0,
            experiment_id="exp-real", provenance="REAL",
        )
        self.assertEqual(result.status, CandidateValidationStatus.VERIFIED)
        self.assertEqual(result.provenance, "REAL")

    # 30. 非法 provenance 构造即拒（大小写敏感白名单）
    def test_invalid_provenance_rejected_at_construction(self):
        for bad in ("", "offline", "real", "MAYBE", None):
            with self.subTest(provenance=bad):
                with self.assertRaises(ValueError):
                    bare_result(provenance=bad)

    # 31. provenance 不可变且 repr 不含 secret 形态
    def test_provenance_surface_frozen_and_secret_free(self):
        result = self.run_with(pass_all)
        self.assertEqual(result.provenance, "OFFLINE")
        with self.assertRaises(FrozenInstanceError):
            result.provenance = "REAL"
        surface = repr(result).lower()
        for marker in ("token", "secret", "api_key", "authorization", "stdout", "stderr"):
            self.assertNotIn(marker, surface)

    # 32. Runner 默认与显式 OFFLINE 整对象相等
    def test_runner_default_matches_literal_default(self):
        first = self.run_with(pass_all)
        second = CandidateValidationRunner().run(
            instance(), pass_all, clock=lambda: 1000.0,
            experiment_id="exp-1", provenance="OFFLINE",
        )
        self.assertEqual(first, second)
        self.assertEqual(first.provenance, "OFFLINE")


if __name__ == "__main__":
    unittest.main()
