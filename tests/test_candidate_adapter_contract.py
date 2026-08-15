"""Candidate Adapter Contract Verification — offline, mock-only.

Answers one question: can any runtime adapter be described as a
CandidateRuntimeInstance so the validation layer never needs the runtime's
name? All adapters here are test doubles; nothing is spawned or invoked.
"""
import sys
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import Mock

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from candidate_adapter_contract import CandidateAdapter, candidate_from_adapter
from candidate_validation import (
    CandidateRuntimeInstance,
    CandidateValidationRunner,
    CandidateValidationStatus,
    GateResult,
    GateVerdict,
    ValidationGate,
)
from task_budget import BudgetUsage
from loop_guard import LoopGuard


def silent_probe():
    return Mock(spec=["discover", "check_authentication", "check_provider_model", "minimal_health_check"])


class MockAdapter:
    """Minimal test double satisfying the CandidateAdapter contract."""

    def __init__(self, runtime_id, provider_id, model_id, fingerprint,
                 capabilities=("coding",), probe=None, spec=None):
        self.runtime_id = runtime_id
        self.provider_id = provider_id
        self.model_id = model_id
        self.config_fingerprint = fingerprint
        self.capability_context = tuple(capabilities)
        self.probe = probe if probe is not None else silent_probe()
        self.invocation_spec = spec if spec is not None else {
            "prompt_channel": "stdin", "timeout_seconds": 30,
        }


def adapter_a():
    return MockAdapter("runtime-a", "provider-a", "model-a", "fp-a")


def pass_all(gate):
    return GateResult(gate, GateVerdict.PASS)


class AdapterContractTests(unittest.TestCase):
    # 1. Adapter Contract: all seven fields flow through the bridge
    def test_adapter_provides_full_candidate_contract(self):
        adapter = adapter_a()
        candidate = candidate_from_adapter(adapter)
        self.assertIsInstance(candidate, CandidateRuntimeInstance)
        self.assertEqual(candidate.runtime_id, "runtime-a")
        self.assertEqual(candidate.provider_id, "provider-a")
        self.assertEqual(candidate.model_id, "model-a")
        self.assertEqual(candidate.config_fingerprint, "fp-a")
        self.assertEqual(candidate.capability_context, ("coding",))
        self.assertIs(candidate.probe, adapter.probe)
        self.assertEqual(candidate.invocation_spec["prompt_channel"], "stdin")

    def test_unevidenced_model_stays_none_without_guessing(self):
        adapter = MockAdapter("runtime-x", "unknown", None, "fp-x")
        candidate = candidate_from_adapter(adapter)
        self.assertIsNone(candidate.model_id)
        self.assertEqual(candidate.provider_id, "unknown")

    # 2. Adapter -> Candidate for three distinct adapters
    def test_three_adapters_become_three_candidates(self):
        candidates = [candidate_from_adapter(a()) for a in (adapter_a,)]
        candidates += [
            candidate_from_adapter(MockAdapter("runtime-b", "provider-a", "model-b", "fp-b")),
            candidate_from_adapter(MockAdapter("runtime-c", "provider-c", "model-c", "fp-c")),
        ]
        self.assertEqual(len({c.identity for c in candidates}), 3)

    # 3. Runtime neutrality: no name/value branches in production sources
    def test_production_sources_have_no_runtime_or_dimension_branches(self):
        import candidate_adapter_contract
        import candidate_validation
        # forbidden names built at runtime so this file stays literal-free
        names = tuple("".join(p) for p in (
            ("cl", "aude"), ("co", "dex"), ("deep", "seek"),
            ("tiny", "-agents"), ("tiny", "_agents"), ("gem", "ini"),
        ))
        for module in (candidate_adapter_contract, candidate_validation):
            text = Path(module.__file__).read_text(encoding="utf-8").lower()
            for name in names:
                self.assertNotIn(name, text)
            for branch in ("runtime_id ==", "provider_id ==", "model_id =="):
                self.assertNotIn(branch, text)

    # 4. Identity preservation across all four identity quadrants
    def test_identity_quadrants_preserved_through_bridge(self):
        base = candidate_from_adapter(MockAdapter("r", "p", "m", "f1")).identity
        same = candidate_from_adapter(MockAdapter("r", "p", "m", "f1")).identity
        other_provider = candidate_from_adapter(MockAdapter("r", "p2", "m", "f2")).identity
        other_model = candidate_from_adapter(MockAdapter("r", "p", "m2", "f3")).identity
        other_config = candidate_from_adapter(MockAdapter("r", "p", "m", "f4")).identity
        self.assertEqual(base, same)
        for other in (other_provider, other_model, other_config):
            self.assertNotEqual(base, other)

    # 5. Capability context: declared passes through, never promoted
    def test_declared_capabilities_are_passed_but_never_promoted(self):
        adapter = MockAdapter("runtime-a", "provider-a", "model-a", "fp-a",
                              capabilities=("architecture", "coding"))
        candidate = candidate_from_adapter(adapter)
        self.assertEqual(candidate.capability_context, ("architecture", "coding"))
        result = CandidateValidationRunner().run(
            candidate, pass_all, clock=lambda: 1.0, experiment_id="cap",
        )
        # validated evidence lives only in gate results; declarations are not auto-promoted
        self.assertTrue(all(isinstance(g, GateResult) for g in result.gate_results))
        self.assertNotIn("capabilities", result.evidence)

    def test_empty_capability_context_is_allowed(self):
        candidate = candidate_from_adapter(MockAdapter("r", "p", "m", "f", capabilities=()))
        self.assertEqual(candidate.capability_context, ())

    # 6. Probe contract: injected only, never called by this layer
    def test_probe_is_injected_dependency_never_invoked(self):
        probe = Mock(spec=[])
        candidate = candidate_from_adapter(MockAdapter("r", "p", "m", "f", probe=probe))
        CandidateValidationRunner().run(candidate, pass_all, clock=lambda: 1.0, experiment_id="probe")
        self.assertEqual(probe.mock_calls, [])
        surface = repr(candidate)
        self.assertNotIn("Mock", surface)  # probe excluded from repr

    # 7. Invocation spec: inspectable, secret-free, immutable
    def test_invocation_spec_is_safe_and_immutable(self):
        candidate = candidate_from_adapter(MockAdapter("r", "p", "m", "f"))
        spec = candidate.invocation_spec
        self.assertEqual(spec["timeout_seconds"], 30)
        with self.assertRaises((TypeError, AttributeError)):
            spec["timeout_seconds"] = 999  # mapping is frozen

    def test_invocation_spec_rejects_secret_and_raw_io(self):
        for bad in ({"token": "x"}, {"api_key": "x"}, {"authorization": "x"},
                    {"raw_stdout": "x"}, {"raw_stderr": "x"}):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    candidate_from_adapter(MockAdapter("r", "p", "m", "f", spec=bad))

    # 8. Adapter factory: minimal protocol, double-based (no real factory built)
    def test_candidate_adapter_protocol_exists_as_contract_only(self):
        import candidate_adapter_contract as module
        for attr in ("runtime_id", "provider_id", "model_id", "config_fingerprint",
                     "capability_context", "probe", "invocation_spec"):
            self.assertIn(attr, CandidateAdapter.__annotations__)
        source = Path(module.__file__).read_text(encoding="utf-8")
        self.assertNotIn("subprocess", source)
        self.assertNotIn("invoke", source)

    # 9. Runner integration: four terminal states independent of adapter type
    def test_four_terminal_states_are_adapter_agnostic(self):
        def script(status):
            mapping = {
                CandidateValidationStatus.VERIFIED: {},
                CandidateValidationStatus.BLOCKED: {ValidationGate.G2_AUTHENTICATION: GateVerdict.BLOCKED},
                CandidateValidationStatus.FAILED: {ValidationGate.G7_TIMEOUT: GateVerdict.FAILED},
                CandidateValidationStatus.NOT_VERIFIED: {ValidationGate.G1_DISCOVERY: GateVerdict.NOT_RUN},
            }[status]

            def executor(gate):
                verdict = mapping.get(gate, GateVerdict.PASS)
                reason = {
                    GateVerdict.BLOCKED: "external condition missing",
                    GateVerdict.FAILED: "integration defect",
                }.get(verdict)
                return GateResult(gate, verdict, reason)

            return executor

        adapters = [
            MockAdapter("runtime-a", "provider-a", "model-a", "fp-a"),
            MockAdapter("runtime-b", "provider-a", "model-b", "fp-b"),
            MockAdapter("runtime-a", "provider-c", "model-c", "fp-c"),
        ]
        for status in CandidateValidationStatus:
            with self.subTest(status=status.value):
                runner = CandidateValidationRunner()
                outcomes = {
                    runner.run(candidate_from_adapter(a), script(status),
                               clock=lambda: 5.0, experiment_id="agg").status
                    for a in adapters
                }
                self.assertEqual(outcomes, {status})

    # 10. Cross-runtime independence: behavior depends only on instance + executor
    def test_behavior_depends_only_on_instance_and_executor(self):
        runner = CandidateValidationRunner()
        first = runner.run(candidate_from_adapter(adapter_a()), pass_all,
                           clock=lambda: 7.0, experiment_id="cross")
        second = runner.run(
            candidate_from_adapter(MockAdapter("runtime-b", "provider-a", "model-b", "fp-b")),
            pass_all, clock=lambda: 7.0, experiment_id="cross",
        )
        self.assertEqual(first.status, second.status)
        self.assertEqual(first.gates_passed, second.gates_passed)
        self.assertNotEqual(first.identity, second.identity)

    # 11. No formal stack coupling in the contract layer
    def test_contract_layer_has_no_formal_stack_dependency(self):
        import candidate_adapter_contract
        source = Path(candidate_adapter_contract.__file__).read_text(encoding="utf-8")
        for module in ("runtime_health", "runtime_pool", "capability_registry", "role_candidates",
                       "stage_runtime_selection", "selection_plan_bridge", "orchestrator",
                       "execution_engine"):
            self.assertNotIn(module, source)

    # 12. Security inherited: secret markers rejected at construction
    def test_secret_markers_rejected_across_all_surfaces(self):
        with self.assertRaises(ValueError):
            candidate_from_adapter(MockAdapter("r", "p", "m", "f", capabilities=("token=x",)))
        with self.assertRaises(ValueError):
            candidate_from_adapter(MockAdapter("r", "p", "m", "f", spec={"bearer": "y"}))

    # 13. Determinism: 10 identical runs
    def test_ten_runs_are_identical(self):
        adapter = adapter_a()
        runner = CandidateValidationRunner()
        results = [
            runner.run(candidate_from_adapter(adapter), pass_all,
                       clock=lambda: 3.0, experiment_id="det")
            for _ in range(10)
        ]
        for result in results[1:]:
            self.assertEqual(result, results[0])

    # immutability of the bridged candidate
    def test_bridged_candidate_is_immutable(self):
        candidate = candidate_from_adapter(adapter_a())
        with self.assertRaises(FrozenInstanceError):
            candidate.runtime_id = "mutated"

    # Budget / LoopGuard untouched
    def test_budget_and_guard_unchanged(self):
        usage, guard = BudgetUsage(), LoopGuard()
        before = (usage.total_agent_calls, usage.iterations_used, guard.check("t", "s", "a"))
        candidate_from_adapter(adapter_a())
        CandidateValidationRunner().run(
            candidate_from_adapter(adapter_a()), pass_all, clock=lambda: 1.0, experiment_id="bg")
        after = (usage.total_agent_calls, usage.iterations_used, guard.check("t", "s", "a"))
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
