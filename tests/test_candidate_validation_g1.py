"""G1 fix: capability evidence carrier — offline, mock-only.

validated_capabilities on CandidateValidationResult is a positive carrier
for evidence produced during validation. It is collected ONLY from explicit
structured GateResult.capabilities, never promoted from the candidate's
declared capability_context, never inferred from plain evidence strings.
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from candidate_validation import (
    CandidateRuntimeInstance,
    CandidateValidationRunner,
    CandidateValidationStatus,
    GateResult,
    GateVerdict,
    ValidationGate,
)


def make_candidate(capabilities=("coding", "architecture")):
    return CandidateRuntimeInstance(
        runtime_id="runtime-a", provider_id="provider-a", model_id="model-a",
        config_fingerprint="fp-a", capability_context=capabilities,
        probe=Mock(spec=[]),
        invocation_spec={"timeout_seconds": 30},
    )


def pass_all(gate):
    return GateResult(gate, GateVerdict.PASS)


def executor_with_caps(caps_by_gate):
    def executor(gate):
        caps = caps_by_gate.get(int(gate), ())
        return GateResult(gate, GateVerdict.PASS, capabilities=caps)
    return executor


class G1CapabilityEvidenceTests(unittest.TestCase):
    def run_with(self, executor, candidate=None):
        return CandidateValidationRunner().run(
            candidate or make_candidate(), executor,
            clock=lambda: 10.0, experiment_id="g1",
        )

    # 1. 默认为空
    def test_default_validated_capabilities_is_empty(self):
        result = self.run_with(pass_all)
        self.assertEqual(result.validated_capabilities, ())

    # 2. 显式 capability evidence 进入 Result
    def test_explicit_gate_capabilities_reach_result(self):
        result = self.run_with(executor_with_caps({5: ("coding",)}))
        self.assertEqual(result.validated_capabilities, ("coding",))

    # 3. capability_context 不自动升格
    def test_declared_context_never_promoted(self):
        result = self.run_with(pass_all)  # candidate declares coding+architecture
        self.assertEqual(result.validated_capabilities, ())

    # 4. 普通字符串 evidence 不解释为 capability
    def test_plain_evidence_strings_are_not_capabilities(self):
        def executor(gate):
            return GateResult(gate, GateVerdict.PASS,
                              reason=None, evidence={"capability": "coding"})
        result = self.run_with(executor)
        self.assertEqual(result.validated_capabilities, ())

    # 5. capability 顺序不影响结果
    def test_capability_order_does_not_change_result(self):
        first = self.run_with(executor_with_caps({11: ("coding", "architecture")}))
        second = self.run_with(executor_with_caps({11: ("architecture", "coding")}))
        self.assertEqual(first, second)
        self.assertEqual(first.validated_capabilities, ("architecture", "coding"))

    # 6. duplicate capability 确定性去重
    def test_duplicates_are_deduplicated_deterministically(self):
        result = self.run_with(executor_with_caps({5: ("coding", "coding"), 11: ("coding",)}))
        self.assertEqual(result.validated_capabilities, ("coding",))

    # 7. secret capability 拒绝
    def test_secret_shaped_capability_is_rejected(self):
        with self.assertRaises(ValueError):
            GateResult(ValidationGate.G5_MINIMAL_INVOCATION, GateVerdict.PASS,
                       capabilities=("token=x",))
        with self.assertRaises(ValueError):
            GateResult(ValidationGate.G5_MINIMAL_INVOCATION, GateVerdict.PASS,
                       capabilities=("api_key",))

    # 8. 非 VERIFIED 终态不保留 capability evidence
    def test_non_verified_results_carry_no_capability_evidence(self):
        scripts = {
            CandidateValidationStatus.BLOCKED: {ValidationGate.G2_AUTHENTICATION: GateVerdict.BLOCKED},
            CandidateValidationStatus.FAILED: {ValidationGate.G7_TIMEOUT: GateVerdict.FAILED},
            CandidateValidationStatus.NOT_VERIFIED: {ValidationGate.G5_MINIMAL_INVOCATION: GateVerdict.NOT_RUN},
        }
        for status, mapping in scripts.items():
            with self.subTest(status=status.value):
                def executor(gate, _m=mapping):
                    verdict = _m.get(gate, GateVerdict.PASS)
                    reason = {"BLOCKED": "external", "FAILED": "defect"}.get(verdict.value)
                    return GateResult(gate, verdict, reason, capabilities=("coding",))
                result = self.run_with(executor)
                self.assertEqual(result.status, status)
                self.assertEqual(result.validated_capabilities, ())

    # 9. immutable
    def test_validated_capabilities_immutable(self):
        result = self.run_with(executor_with_caps({5: ("coding",)}))
        with self.assertRaises(Exception):
            result.validated_capabilities = ("architecture",)

    # 10. Runtime-neutral：无 Runtime-specific capability 分支
    def test_no_runtime_specific_capability_branches(self):
        import candidate_validation
        text = Path(candidate_validation.__file__).read_text(encoding="utf-8").lower()
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
