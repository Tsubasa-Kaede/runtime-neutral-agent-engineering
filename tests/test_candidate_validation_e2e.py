"""Candidate Validation Offline E2E — Mock only.

Exercises the completed Candidate Validation Skeleton end-to-end across
scenarios A-L. No runtime, provider, model, subprocess or credential is
ever touched; every gate outcome comes from mock executors.
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
    ValidationGate,
)
from task_budget import BudgetUsage
from loop_guard import LoopGuard


def make_candidate(runtime_id="runtime-a", provider_id="provider-a",
                   model_id="model-a", fingerprint="fingerprint-a", probe=None):
    return CandidateRuntimeInstance(
        runtime_id=runtime_id,
        provider_id=provider_id,
        model_id=model_id,
        config_fingerprint=fingerprint,
        capability_context=("coding",),
        probe=probe if probe is not None else Mock(spec=[]),  # spec=[]: any attribute access explodes
        invocation_spec={"prompt_channel": "stdin", "timeout_seconds": 30},
    )


def scripted_executor(script, called):
    """script: {ValidationGate: GateVerdict}; unlisted gates default to PASS."""

    def executor(gate):
        if not isinstance(gate, ValidationGate):
            raise AssertionError(f"executor received non-gate input: {gate!r}")
        called.append(int(gate))
        verdict = script.get(gate, GateVerdict.PASS)
        reason = None
        if verdict is GateVerdict.BLOCKED:
            reason = "external condition missing"
        elif verdict is GateVerdict.FAILED:
            reason = "reap unbounded"
        return GateResult(gate, verdict, reason)

    return executor


class ScenarioAVerified(unittest.TestCase):
    def test_all_pass_produces_verified_in_order(self):
        called = []
        result = CandidateValidationRunner().run(
            make_candidate(), scripted_executor({}, called),
            clock=lambda: 100.0, experiment_id="e2e-a",
        )
        self.assertIsInstance(result, CandidateValidationResult)
        self.assertEqual(result.status, CandidateValidationStatus.VERIFIED)
        self.assertEqual({int(g) for g in result.gates_passed}, set(range(1, 15)))
        self.assertIsNone(result.block_reason)
        self.assertIsNone(result.failure_point)
        self.assertEqual(called, list(range(1, 15)))
        self.assertEqual(result.identity,
                         ("runtime-a", "provider-a", "model-a", "fingerprint-a"))


class ScenarioBBlockedAtGate2(unittest.TestCase):
    def test_blocked_at_gate_two_short_circuits(self):
        called = []
        result = CandidateValidationRunner().run(
            make_candidate(),
            scripted_executor({ValidationGate.G2_AUTHENTICATION: GateVerdict.BLOCKED}, called),
            clock=lambda: 100.0, experiment_id="e2e-b",
        )
        self.assertEqual(result.status, CandidateValidationStatus.BLOCKED)
        self.assertTrue(result.block_reason)
        self.assertIsNone(result.failure_point)
        self.assertNotEqual(result.status, CandidateValidationStatus.FAILED)
        self.assertNotEqual(result.status, CandidateValidationStatus.VERIFIED)
        self.assertEqual(called, [1, 2])  # gates 3-13 never executed
        self.assertNotIn(ValidationGate.G3_PROVIDER, result.gates_passed)


class ScenarioCFailedAtGate7(unittest.TestCase):
    def test_failed_at_gate_seven_short_circuits(self):
        called = []
        result = CandidateValidationRunner().run(
            make_candidate(),
            scripted_executor({ValidationGate.G7_TIMEOUT: GateVerdict.FAILED}, called),
            clock=lambda: 100.0, experiment_id="e2e-c",
        )
        self.assertEqual(result.status, CandidateValidationStatus.FAILED)
        self.assertEqual(result.failure_point[0], ValidationGate.G7_TIMEOUT)
        self.assertTrue(result.failure_point[1])  # error_class non-empty
        self.assertIsNone(result.block_reason)
        self.assertNotEqual(result.status, CandidateValidationStatus.BLOCKED)
        self.assertNotEqual(result.status, CandidateValidationStatus.VERIFIED)
        self.assertEqual(called, [1, 2, 3, 4, 5, 6, 7])  # gates 8-13 never executed


class ScenarioDNotVerified(unittest.TestCase):
    def test_not_run_yields_not_verified(self):
        called = []
        result = CandidateValidationRunner().run(
            make_candidate(),
            scripted_executor({ValidationGate.G5_MINIMAL_INVOCATION: GateVerdict.NOT_RUN}, called),
            clock=lambda: 100.0, experiment_id="e2e-d",
        )
        self.assertEqual(result.status, CandidateValidationStatus.NOT_VERIFIED)
        self.assertNotEqual(result.status, CandidateValidationStatus.VERIFIED)
        self.assertNotIn(ValidationGate.G5_MINIMAL_INVOCATION, result.gates_passed)


class ScenarioEIdentityIsolation(unittest.TestCase):
    def test_identity_distinguishes_every_dimension(self):
        a = make_candidate("runtime-a", "provider-a", "model-a", "fingerprint-a")
        b = make_candidate("runtime-a", "provider-b", "model-a", "fingerprint-b")
        c = make_candidate("runtime-b", "provider-a", "model-a", "fingerprint-c")
        d = make_candidate("runtime-a", "provider-a", "model-b", "fingerprint-d")
        identities = [a.identity, b.identity, c.identity, d.identity]
        self.assertEqual(len(set(identities)), 4)
        # shared runtime / shared provider / shared model must not collapse identity
        self.assertNotEqual(a.identity, b.identity)
        self.assertNotEqual(a.identity, c.identity)
        self.assertNotEqual(a.identity, d.identity)


class ScenarioFSameIdentity(unittest.TestCase):
    def test_identical_tuple_means_identical_identity(self):
        first = make_candidate("runtime-x", "provider-y", "model-z", "fp-1")
        second = make_candidate("runtime-x", "provider-y", "model-z", "fp-1")
        self.assertEqual(first.identity, second.identity)
        self.assertEqual(hash(first.identity), hash(second.identity))


class ScenarioGDeterminism(unittest.TestCase):
    def test_ten_runs_are_identical(self):
        candidate = make_candidate()
        executor = scripted_executor({}, [])
        runner = CandidateValidationRunner()
        results = [
            runner.run(candidate, executor, clock=lambda: 42.0, experiment_id="e2e-g")
            for _ in range(10)
        ]
        for result in results[1:]:
            self.assertEqual(result, results[0])
        self.assertEqual(results[0].executed_at, 42.0)


class ScenarioHSecretSafety(unittest.TestCase):
    def test_secret_evidence_is_rejected(self):
        gate = ValidationGate.G5_MINIMAL_INVOCATION
        payloads = [
            {"raw_stdout": "output"},
            {"raw_stderr": "error"},
            {"token": "abc"},
            {"api_key": "abc"},
            {"authorization": "Bearer x"},
            {"bearer": "x"},
        ]
        for evidence in payloads:
            with self.subTest(evidence=evidence):
                with self.assertRaises(ValueError):
                    GateResult(gate, GateVerdict.PASS, evidence=evidence)
        with self.assertRaises(ValueError):
            CandidateValidationResult(
                identity=("r", "p", "m", "f"),
                status=CandidateValidationStatus.VERIFIED,
                gates_passed=frozenset(),
                gate_results=(),
                block_reason=None,
                failure_point=None,
                experiment_id="x",
                executed_at=1.0,
                evidence={"token": "abc"},
            )

    def test_clean_result_surface_has_no_secrets(self):
        result = CandidateValidationRunner().run(
            make_candidate(), scripted_executor({}, []),
            clock=lambda: 1.0, experiment_id="e2e-h",
        )
        surface = repr(result).lower()
        for marker in ("token", "secret", "api_key", "authorization", "stdout", "stderr"):
            self.assertNotIn(marker, surface)


class ScenarioIRuntimeNeutrality(unittest.TestCase):
    # Built at runtime so the scanned sources never contain the full literals.
    FORBIDDEN_NAMES = tuple("".join(parts) for parts in (
        ("cl", "aude"), ("co", "dex"), ("deep", "seek"),
        ("tiny", "-agents"), ("tiny", "_agents"), ("gem", "ini"),
    ))

    def test_no_runtime_names_in_production_or_test_source(self):
        import candidate_validation
        for path in (Path(candidate_validation.__file__), Path(__file__)):
            text = path.read_text(encoding="utf-8").lower()
            for name in self.FORBIDDEN_NAMES:
                self.assertNotIn(name, text, msg=f"runtime name leaked into {path.name}")


class ScenarioJFormalStackIsolation(unittest.TestCase):
    FORBIDDEN = ("runtime_health", "runtime_pool", "capability_registry", "role_candidates",
                 "stage_runtime_selection", "selection_plan_bridge", "orchestrator",
                 "execution_engine")

    def test_production_module_pulls_in_no_formal_stack(self):
        import subprocess
        import json
        script = (
            "import sys, json;"
            f"sys.path.insert(0, r'{SCRIPTS}');"
            "import candidate_validation;"
            "print(json.dumps([m for m in "
            f"{self.FORBIDDEN!r}"
            " if m in sys.modules]))"
        )
        completed = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(completed.returncode, 0, "isolation probe subprocess failed")
        self.assertEqual(json.loads(completed.stdout), [])

    def test_e2e_test_imports_stay_outside_formal_stack(self):
        lines = Path(__file__).read_text(encoding="utf-8").splitlines()
        import_lines = [line for line in lines if line.startswith(("import ", "from "))]
        for line in import_lines:
            for module in self.FORBIDDEN:
                self.assertNotIn(module, line, msg=f"formal-stack import leaked: {line}")


class ScenarioKNoRuntimeInvocation(unittest.TestCase):
    def test_probe_is_never_touched(self):
        probe = Mock(spec=[])  # no methods at all: any call raises
        called = []
        result = CandidateValidationRunner().run(
            make_candidate(probe=probe), scripted_executor({}, called),
            clock=lambda: 1.0, experiment_id="e2e-k",
        )
        self.assertEqual(result.status, CandidateValidationStatus.VERIFIED)
        self.assertEqual(probe.mock_calls, [])  # runtime invocation count == 0
        self.assertEqual(called, list(range(1, 15)))


class ScenarioLNoBudgetOrGuardMutation(unittest.TestCase):
    def test_budget_and_guard_unchanged(self):
        usage = BudgetUsage()
        guard = LoopGuard()
        before = (usage.total_agent_calls, usage.iterations_used,
                  guard.check("t", "architect", "a"))
        for status in ({}, {ValidationGate.G2_AUTHENTICATION: GateVerdict.BLOCKED},
                       {ValidationGate.G7_TIMEOUT: GateVerdict.FAILED},
                       {ValidationGate.G5_MINIMAL_INVOCATION: GateVerdict.NOT_RUN}):
            CandidateValidationRunner().run(
                make_candidate(), scripted_executor(status, []),
                clock=lambda: 1.0, experiment_id="e2e-l",
            )
        after = (usage.total_agent_calls, usage.iterations_used,
                 guard.check("t", "architect", "a"))
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
