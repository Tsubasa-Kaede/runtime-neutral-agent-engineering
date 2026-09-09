"""R7-B (a): REAL user-controlled collaboration policy verification.

One gated REAL scenario (zero retries, zero fallbacks, exactly four real
invocations): the user's CLI policy — parsed by the REAL entry surface
(cli.policy_from_args over the authorized argv) — flows through the
production ProductionFacade / CollaborationOrchestrator into
PolicyConstrainedAssigner, and the pipeline answers HONESTLY.

Plan (a) semantics (accepted by this round's authorization):
  - The production pipeline makes TWO independent 2-role assign() calls
    (dual: architect+coder; verification: test+review). Each call can
    yield at most 2 distinct runtimes, so min_distinct_runtimes=3 is
    UNSATISFIABLE per call — and the system must say so
    (POLICY_COUNT_UNSATISFIED) instead of forcing a third runtime,
    retrying, reassigning, backfilling or falling back.
  - No FullSpreadAssigner (test-layer 4-role round robin) is used; the
    ONLY assigner in play is the user policy's PolicyConstrainedAssigner.
  - The honest expected evidence:
      architect=claude, coder=codex, tester=codex, reviewer=claude
      calls: claude=2, codex=2, pi=0; used_runtimes={claude-cli, codex-cli}
      ROLE_ASSIGNMENT=POLICY_COUNT_UNSATISFIED on the DUAL DECISION record
      (success path = 1 DECISION; the verification-half reason is recorded
      only on its None terminal by A3 design — its honesty is proven
      offline in OfflinePolicyEntryTests).

Evidence reuse: R5-A REAL qualifications for all three runtimes
(reconstructed offline with the exact identity tuples and gate surface);
this round spends invocations ONLY on the four collaboration stages.

Single-path instrument: _SinglePathProbe fails LOUDLY if the orchestrator
ever delegates to SINGLE — mode=ON must never fall back.
"""
import os
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_real_multi_agent_collaboration import (
    _print_failure_diagnosis,
    _surface_has_credential_shape,
    health_ready,
    protected_snapshot,
    safe_display,
)

from candidate_validation import (
    CandidateValidationResult,
    CandidateValidationStatus,
    GateResult,
    GateVerdict,
    ValidationGate,
)
from cli import build_parser, policy_from_args
from collaboration_session import collab_agent_address
from collaboration_state import CollaborationDirection
from external_runtime import RuntimeProfile
from mode_gate import Mode
from production_facade import ProductionFacade
from task_budget import BudgetUsage, TaskBudget
from task_classifier import Complexity, classify_task
from loop_guard import LoopGuard
from verified_runtime_pool import VerifiedRuntimePool

RUN_REAL_PROVIDER_TESTS = os.environ.get("RUN_REAL_PROVIDER_TESTS") == "1"

# The three R5-A REAL-qualified identities, exactly as recorded (R6-C12
# precedent; config_fingerprint "installed" matches the registry).
CLAUDE_IDENTITY = ("claude-cli", "anthropic", None, "installed")
CODEX_IDENTITY = ("codex-cli", "openai", None, "installed")
PI_IDENTITY = ("pi-cli", "deepseek", None, "installed")
IDENTITIES = (CLAUDE_IDENTITY, CODEX_IDENTITY, PI_IDENTITY)
CAPS_ALL = ("architecture", "coding", "review", "testing")

TASK = ("Redesign architecture across modules for a tiny deterministic "
        "slug utility, then report its implementation and review it.")
TASK_ID = "T-r7b-real-1"

# The authorized CLI argv — parsed by the REAL entry surface, never
# hand-constructed policy objects.
ARGV = ["run", "--mode", "on", "--runtimes", "claude-cli,codex-cli,pi-cli",
        "--min-runtimes", "3", TASK]

# Plan (a) expected evidence (production semantics, offline-audited):
EXPECTED_MAPPING = {
    "architect": "claude-cli",
    "coder": "codex-cli",
    "tester": "codex-cli",
    "reviewer": "claude-cli",
}
EXPECTED_USED = {"claude-cli", "codex-cli"}
EXPECTED_CALLS = {"claude-cli": 2, "codex-cli": 2, "pi-cli": 0}


class _SinglePathProbe:
    """SINGLE-path instrument: mode=ON must NEVER delegate to SINGLE.
    Fails loudly instead of spending an invocation or fabricating."""

    def __init__(self):
        self.calls = 0

    def execute(self, task_id, task, prompt, mode):
        self.calls += 1
        raise AssertionError(
            "SINGLE path must not run: mode=ON policy scenario")


_REAL_CLASS_MARKER = "class Real" + "CliPolicyCollaborationTests"
_MAIN_MARKER = 'if __name__ == "' + '__main__' + '":'


def _real_class_source() -> str:
    text = Path(__file__).read_text(encoding="utf-8")
    start = text.index(_REAL_CLASS_MARKER)
    end = text.index(_MAIN_MARKER)
    return text[start:end]


def reused_validation(identity, experiment_id):
    """EVIDENCE_REUSE reconstruction of the R5-A REAL qualification
    (offline shape only, never labeled stronger than R5-A emitted)."""
    return CandidateValidationResult(
        identity=identity,
        status=CandidateValidationStatus.VERIFIED,
        gates_passed=frozenset(ValidationGate),
        gate_results=tuple(
            GateResult(gate, GateVerdict.PASS) for gate in ValidationGate),
        block_reason=None, failure_point=None,
        experiment_id=experiment_id, executed_at=0.0,
        validated_capabilities=CAPS_ALL, evidence={},
        provenance="REAL")


# ---------------------------------------------------------------------------
# Offline discipline tests (always run, zero REAL invocations)
# ---------------------------------------------------------------------------


class TestFileDisciplineTests(unittest.TestCase):
    """Offline: this file's own honesty rules."""

    def test_real_test_is_opt_in_gated(self):
        source = Path(__file__).read_text(encoding="utf-8")
        self.assertIn('RUN_REAL_PROVIDER_TESTS") == "1"', source)
        self.assertIn("setUpClass", source)

    def test_real_class_uses_no_mock_or_test_layer_assigner(self):
        source = _real_class_source()
        for forbidden in ("FullSpreadAssigner", "MockAdapter",
                          "FakeAgentAdapter", "RepeatingAdapter",
                          "StubVerifiedOrchestrator", "SpySession",
                          "unittest.mock", "Mock("):
            self.assertNotIn(forbidden, source)

    def test_policy_comes_from_the_real_cli_entry(self):
        # The policy object MUST be minted by cli.policy_from_args over the
        # authorized argv — never hand-constructed in the test body.
        source = _real_class_source()
        self.assertIn("policy_from_args", source)
        self.assertNotIn("policy = CollaborationPolicy(", source)

    def test_no_manual_role_assignment(self):
        # Role choice belongs to the production assigner; the test only
        # records and asserts. Raw assignment statements are forbidden
        # (assertion-side EXPECTED_ constants are fine).
        source = _real_class_source()
        for forbidden in ("architect_address = collab",
                          "coder_address = collab",
                          "tester_address = collab",
                          "reviewer_address = collab",
                          "session.run("):
            self.assertNotIn(forbidden, source)

    def test_single_path_probe_fails_loudly(self):
        probe = _SinglePathProbe()
        with self.assertRaises(AssertionError):
            probe.execute("T", "task", "p", Mode.ON)
        self.assertEqual(probe.calls, 1)


class OfflinePolicyEntryTests(unittest.TestCase):
    """Offline: the authorized argv parses to the authorized policy, and
    the production assigner's expected mapping is deterministic."""

    def test_authorized_argv_parses_to_authorized_policy(self):
        args = build_parser().parse_args(ARGV)
        policy = policy_from_args(args)
        self.assertEqual(policy.runtime_allowlist,
                         ("claude-cli", "codex-cli", "pi-cli"))
        self.assertEqual(policy.min_distinct_runtimes, 3)
        self.assertIsNone(policy.max_distinct_runtimes)
        self.assertTrue(policy.allow_runtime_reuse)

    def test_task_classifies_complex(self):
        self.assertIs(classify_task(TASK), Complexity.COMPLEX)

    def test_expected_mapping_is_deterministic_offline(self):
        # The exact assignment the production assigner will produce for
        # this pool + policy (bridge order = sorted identities), proven
        # offline with the same shapes — the REAL run asserts the same.
        from collaboration_policy import PolicyConstrainedAssigner
        from verified_selection_bridge import VerifiedSelectionBridge
        pool = VerifiedRuntimePool(clock=lambda: 0.0)
        for identity in IDENTITIES:
            pool.admit(reused_validation(identity, "r5a"), CAPS_ALL,
                       health_now="READY")
        health = {identity[0]: health_ready(identity[0], identity[1])
                  for identity in IDENTITIES}
        bridge = VerifiedSelectionBridge()
        requirements = {"architect": ("architecture",), "coder": ("coding",),
                        "test": ("testing",), "review": ("review",)}
        dual_sets = {role: bridge.candidates_for(pool, health, role,
                                                 requirements[role])
                     for role in ("architect", "coder")}
        verify_sets = {role: bridge.candidates_for(pool, health, role,
                        requirements[role])
                       for role in ("test", "review")}
        args = build_parser().parse_args(ARGV)
        policy = policy_from_args(args)
        assigner = PolicyConstrainedAssigner(policy)
        dual = assigner.assign(dual_sets, Complexity.COMPLEX)
        verify = assigner.assign(verify_sets, Complexity.COMPLEX)
        mapping = {
            "architect": dual.assignments["architect"].runtime_id,
            "coder": dual.assignments["coder"].runtime_id,
            "tester": (verify.assignments.get("test")
                       and verify.assignments["test"].runtime_id),
            "reviewer": (verify.assignments.get("review")
                         and verify.assignments["review"].runtime_id),
        }
        self.assertEqual(mapping, EXPECTED_MAPPING)
        self.assertEqual(dual.reason, "POLICY_COUNT_UNSATISFIED")
        self.assertEqual(verify.reason, "POLICY_COUNT_UNSATISFIED")


# ---------------------------------------------------------------------------
# Gated REAL: one scenario, zero retries
# ---------------------------------------------------------------------------


class RealCliPolicyCollaborationTests(unittest.TestCase):
    """Gated REAL: the user's CLI policy drives the production pipeline
    honestly — POLICY_COUNT_UNSATISFIED observed, never forged."""

    @classmethod
    def setUpClass(cls):
        if os.environ.get("RUN_REAL_PROVIDER_TESTS", "") != "1":
            raise unittest.SkipTest("RUN_REAL_PROVIDER_TESTS != 1")

    def test_real_cli_policy_honest_unsatisfied(self):
        import time as _time
        from claude_code_adapter import ClaudeCodeAdapter
        from codex_adapter import CodexAdapter
        from collaboration_orchestrator import CollaborationOrchestrator
        from collaboration_session import CollaborationSession
        from pi_adapter import PiAdapter
        from remote_transport import LoopbackRemoteTransport

        # -- runtime acquisition (honest absence, never fabrication) ----
        claude = ClaudeCodeAdapter.from_environment()
        if claude is None:
            self.skipTest("claude executable not found")
        codex = CodexAdapter.from_environment()
        if codex is None:
            self.skipTest("codex executable not found")
        pi = PiAdapter.from_environment(
            profile=RuntimeProfile(
                "coding-agent", PI_IDENTITY[0], PI_IDENTITY[1],
                PI_IDENTITY[2], "coder", frozenset()))
        if pi is None:
            self.skipTest("pi executable not found")

        # -- invocation accounting ------------------------------------------
        calls = {"claude-cli": 0, "codex-cli": 0, "pi-cli": 0}
        stage_outputs = {}
        role_runtime_log = []

        def _wrap(adapter, runtime_id, real_invoke):
            def wrapped(request):
                calls[runtime_id] += 1
                started = _time.monotonic()
                result = real_invoke(request)
                duration = round(_time.monotonic() - started, 1)
                status = getattr(result.status, "value", str(result.status))
                trace = getattr(result, "trace", None)
                exit_code = getattr(trace, "exit_code", None) if trace else None
                error = getattr(trace, "error", None) if trace else None
                display_error = ("<redacted>" if error and
                                 _surface_has_credential_shape(error)
                                 else (error or ""))
                print(f"INVOCATION_EVIDENCE: {runtime_id}:{request.role} "
                      f"status={status} exit_code={exit_code} "
                      f"duration_s={duration} "
                      f"error={safe_display(display_error)}")
                role_runtime_log.append(
                    (request.task_id, request.role, runtime_id,
                     getattr(trace, "invocation_id", None)))
                if request.role in ("architect", "coder", "tester",
                                    "reviewer"):
                    stage_outputs[(runtime_id, request.role)] = result.output
                return result
            return wrapped

        claude.invoke = _wrap(claude, "claude-cli", claude.invoke)
        codex.invoke = _wrap(codex, "codex-cli", codex.invoke)
        pi.invoke = _wrap(pi, "pi-cli", pi.invoke)

        before = protected_snapshot()

        # -- qualification evidence (EVIDENCE_REUSE: R5-A) ----------------
        claude_validation = reused_validation(CLAUDE_IDENTITY, "r5a-claude")
        codex_validation = reused_validation(CODEX_IDENTITY, "r5a-codex")
        pi_validation = reused_validation(PI_IDENTITY, "r5a-pi")
        print("CLAUDE_QUALIFICATION(reused):",
              claude_validation.status.value, claude_validation.provenance)
        print("CODEX_QUALIFICATION(reused):",
              codex_validation.status.value, codex_validation.provenance)
        print("PI_QUALIFICATION(reused):",
              pi_validation.status.value, pi_validation.provenance)

        # -- one pool, three runtimes --------------------------------------
        pool = VerifiedRuntimePool(clock=lambda: 0.0)
        for validation in (claude_validation, codex_validation,
                           pi_validation):
            pool.admit(validation, CAPS_ALL, health_now="READY")
        self.assertEqual(list(pool.identities()), sorted(IDENTITIES))

        health = {identity[0]: health_ready(identity[0], identity[1])
                  for identity in IDENTITIES}

        # -- capability provision (NOT role assignment): all 12 addresses --
        session_adapters = {}
        for identity, adapter in ((CLAUDE_IDENTITY, claude),
                                  (CODEX_IDENTITY, codex),
                                  (PI_IDENTITY, pi)):
            for role in ("architect", "coder", "tester", "reviewer"):
                session_adapters[
                    collab_agent_address(identity, role)] = adapter

        budget = TaskBudget(8, 8, timeout_seconds=300.0)
        usage = BudgetUsage()
        guard = LoopGuard()
        transport = LoopbackRemoteTransport()

        def session_factory():
            return CollaborationSession(
                transport, session_adapters, budget, usage, guard)

        probe = _SinglePathProbe()

        # -- THE USER POLICY, minted by the real CLI entry -----------------
        args = build_parser().parse_args(ARGV)
        policy = policy_from_args(args)
        print("USER_POLICY:", policy)

        # -- production composition (no test-layer assigner anywhere) -----
        orchestrator = CollaborationOrchestrator(
            probe, pool, health, budget, usage, guard, session_factory)
        facade = ProductionFacade(
            orchestrator, session_adapters, pool, health, budget, usage,
            guard)

        # mode=ON (authorized); provenance from the reused evidence.
        result = facade.run(
            task_id=TASK_ID, task=TASK, prompt="p",
            mode=Mode.ON, provenance=claude_validation.provenance,
            policy=policy)

        print("REAL_OUTCOME_STATUS:", result.status)
        print("FACADE_PATH:", result.path)
        print("FACADE_STAGES:", result.stages)
        print("FACADE_FAILURE_CATEGORY:", result.failure_category)
        print("ROLE_RUNTIME_LOG:", role_runtime_log)
        history = facade.state.history(TASK_ID)
        for record in history:
            if record.direction is CollaborationDirection.DECISION:
                print("LEDGER_DECISION:", record.reason)
        for failed in facade.state.failures(TASK_ID):
            print("LEDGER_FAILURE:", failed.task_id, failed.status)
        if result.status != "SUCCESS":
            for (runtime_id, role), raw in sorted(stage_outputs.items()):
                _print_failure_diagnosis(f"{runtime_id}:{role}", raw)
        print("INVOCATIONS: claude =", calls["claude-cli"],
              "| codex =", calls["codex-cli"],
              "| pi =", calls["pi-cli"],
              "| usage.total =", usage.total_agent_calls)

        # -- plan (a) evidence ----------------------------------------------
        # 1. COMPLEX + mode ON -> FOUR_STAGE, all four stages, success.
        self.assertIs(classify_task(TASK), Complexity.COMPLEX)
        self.assertEqual(result.status, "SUCCESS")
        self.assertEqual(result.path, "FOUR_STAGE")
        self.assertEqual(result.stages,
                         ("architect", "coder", "tester", "reviewer"))
        self.assertEqual(result.failure_category, "")

        # 2. The honest mapping (production 2-role round robin), NOT a
        #    forged third runtime: pi-cli stays at ZERO invocations.
        stages_by_role = {
            role: runtime for (_task, role, runtime, _inv)
            in role_runtime_log}
        self.assertEqual(stages_by_role, EXPECTED_MAPPING)
        self.assertEqual(calls, EXPECTED_CALLS)
        used_runtimes = set(stages_by_role.values())
        self.assertEqual(used_runtimes, EXPECTED_USED)
        # The core plan (a) boundary: min=3 was ASKED; 2 distinct runtimes
        # were USED; nothing forced the third.
        self.assertEqual(len(used_runtimes), 2)
        self.assertLess(len(used_runtimes),
                        policy.min_distinct_runtimes)

        # 3. Every invocation belongs to the allowlist (no runtime 越权).
        for runtime_id in used_runtimes:
            self.assertIn(runtime_id, policy.runtime_allowlist)

        # 4. POLICY_COUNT_UNSATISFIED is real, observable, and honest:
        #    the DUAL-half DECISION (the only record on the success path)
        #    carries it via the A3 ROLE_ASSIGNMENT channel. The
        #    verification-half assign() call reasons COUNT_UNSATISFIED too
        #    — proven offline in OfflinePolicyEntryTests (same assigner,
        #    same sets, same reason) — but on a successful verification
        #    the facade emits no extra DECISION (A3 adds that record only
        #    on the None terminal, by design).
        decisions = [record for record in history
                     if record.direction is CollaborationDirection.DECISION]
        self.assertEqual(len(decisions), 1)  # success path: 1 DECISION
        self.assertIn("ROLE_ASSIGNMENT=POLICY_COUNT_UNSATISFIED",
                      decisions[0].reason)

        # 5. Canonical four-stage ledger: 1 DECISION + 4 envelopes = 5.
        self.assertEqual(len(history), 5)
        self.assertEqual(
            [record.direction for record in history],
            [CollaborationDirection.DECISION,
             CollaborationDirection.REQUEST,
             CollaborationDirection.REPLY,
             CollaborationDirection.REQUEST,
             CollaborationDirection.REQUEST])
        payload_types = [record.payload_type for record in history[1:]]
        self.assertEqual(payload_types,
                         ["ARCHITECTURE", "IMPLEMENTATION", "TEST", "REVIEW"])
        request_records = [r for r in history
                           if r.direction is CollaborationDirection.REQUEST]
        correlations = [r.correlation_id for r in request_records]
        self.assertTrue(all(correlations))
        self.assertEqual(len(set(correlations)), 3)

        # 6. Cross-runtime handoff chain (envelope addresses).
        arch_addr = collab_agent_address(CLAUDE_IDENTITY, "architect")
        coder_addr = collab_agent_address(CODEX_IDENTITY, "coder")
        architecture_record = next(
            r for r in request_records if r.payload_type == "ARCHITECTURE")
        self.assertEqual(architecture_record.source_agent, arch_addr)
        self.assertEqual(architecture_record.target_agent, coder_addr)

        # 7. provenance = REAL on every envelope (from the reused
        #    qualification evidence, never hand-set).
        for record in history[1:]:
            self.assertEqual(record.provenance, "REAL")

        # 8. Budget: four reservations for four stages — the pipeline's
        #    shape decides, not min_distinct_runtimes.
        self.assertEqual(usage.total_agent_calls, 4)
        self.assertEqual(sum(calls.values()), 4)

        # 9. No failures, no SINGLE fallback, no retry.
        self.assertEqual(facade.state.failures(TASK_ID), ())
        self.assertEqual(probe.calls, 0)

        # 10. Content safety: closed surface is secret-free.
        surface = repr(result) + decisions[0].reason
        self.assertFalse(_surface_has_credential_shape(surface))

        # 11. Protected configuration snapshot unchanged.
        after = protected_snapshot()
        self.assertEqual(before, after)

        # 12. Process cleanup: no leaked runtime processes.
        self.assertEqual(claude._processes, {})
        self.assertEqual(codex._processes, {})
        self.assertEqual(pi._processes, {})


if __name__ == "__main__":
    unittest.main()
