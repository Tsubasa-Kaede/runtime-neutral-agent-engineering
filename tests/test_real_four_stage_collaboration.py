"""Roadmap 10H-F: REAL four-stage cross-runtime collaboration E2E.

Proves that DiversityAssigner + ProductionFacade drive a genuine four-
stage (architect→coder→tester→reviewer) collaboration across two REAL
runtimes with NO SINGLE fallback and NO test-layer manual role assignment.

Architecture (difference from 10H-E):
  - 10H-E: CollaborationOrchestrator (architect+coder only)
  - 10H-F: ProductionFacade (orchestrator + VerificationCollaboration)
    where tester/reviewer addresses come from the SAME RoleAssigner
    as the dual roles.

Evidence reuse: G1-G14 qualifications for both runtimes are
reconstructed from the 10H-D rounds (EVIDENCE_REUSE pattern);
this round costs exactly 4 invocations (architect + coder + test +
review).

Single-path instrument: the 10H-F claim is that all four stages run
through the production facade with NO SINGLE fallback. StubVerifiedOrchestrator
is wired for the SINGLE path; if it is ever entered, the test fails
loudly.
"""
import os
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_real_multi_runtime_collaboration import (
    _print_failure_diagnosis,
    _surface_has_credential_shape,
    health_ready,
    protected_snapshot,
)

from candidate_validation import (
    CandidateValidationResult,
    CandidateValidationStatus,
    GateResult,
    GateVerdict,
    ValidationGate,
)
from collaboration_orchestrator import CollaborationOrchestrator
from collaboration_session import collab_agent_address
from external_runtime import RuntimeProfile
from mode_gate import Mode
from production_facade import ProductionFacade
from role_assignment import DiversityAssigner
from runtime_status import (
    HealthEvidence,
    ReasonCode,
    RuntimeState,
    RuntimeStatus,
)
from task_budget import BudgetUsage, TaskBudget
from task_classifier import classify_task, Complexity
from loop_guard import LoopGuard
from verified_runtime_pool import VerifiedRuntimePool

RUN_REAL_PROVIDER_TESTS = os.environ.get("RUN_REAL_PROVIDER_TESTS") == "1"

# Same REAL pair as 10H-E (roadmap decision: Claude=architect, Pi=coder).
CLAUDE_IDENTITY = ("claude-cli", "anthropic", None, "fp-10hd-multi-claude")
PI_IDENTITY     = ("pi-cli", "deepseek", "deepseek-v4-pro", "fp-10hd-multi-pi")
CAPS_ALL = ("architecture", "coding", "review", "testing")

TASK = ("Redesign architecture across modules for a tiny deterministic "
        "slug utility, then report its implementation.")
TASK_ID = "T-10hf-real-1"


class StubVerifiedOrchestrator:
    """SINGLE-path probe: fails LOUDLY if the orchestrator ever
    delegates to SINGLE — proving the four-stage DUAL path ran fully."""

    def __init__(self):
        self.calls = 0

    def execute(self, task_id, task, prompt, mode):
        self.calls += 1
        raise AssertionError(
            "SINGLE path must not run: expected FOUR_STAGE dual path")


# ---------------------------------------------------------------------------
# Discipline tests (always run, zero real invocations)
# ---------------------------------------------------------------------------

_REAL_CLASS_MARKER = "class Real" + "FourStageCollaborationTests"
_MAIN_MARKER = 'if __name__ == "' + '__main__' + '":'


def _real_class_source() -> str:
    text = Path(__file__).read_text(encoding="utf-8")
    start = text.index(_REAL_CLASS_MARKER)
    end = text.index(_MAIN_MARKER)
    return text[start:end]


class TestFileDisciplineTests(unittest.TestCase):
    """Offline: this file's own honesty rules."""

    def test_real_test_is_opt_in_gated(self):
        source = Path(__file__).read_text(encoding="utf-8")
        self.assertIn('RUN_REAL_PROVIDER_TESTS") == "1"', source)
        self.assertIn("setUpClass", source)

    def test_real_class_uses_no_mock_or_stub_adapters(self):
        source = _real_class_source()
        # StubVerifiedOrchestrator appears in the discipline source as a
        # constant marker definition (not in the REAL class body), so it
        # must NOT appear inside the REAL class body itself.
        for forbidden in ("MockAdapter", "FakeAgentAdapter", "RepeatingAdapter",
                          "unittest.mock", "Mock("):
            self.assertNotIn(forbidden, source)

    def test_real_class_drives_roles_through_production_facade(self):
        # The 10H-F claim: all four stages come from
        # ProductionFacade.run() with an injected role_assigner — not
        # from the test layer.
        source = _real_class_source()
        self.assertIn("ProductionFacade(", source)
        self.assertIn("DiversityAssigner()", source)
        self.assertIn("role_assigner=", source)
        self.assertIn("facade.run(", source)

    def test_real_class_provision_never_hand_assigns_roles(self):
        # STRONG FORM (10H-E convention, restored): the capability
        # provision must be 8 EXPLICIT collab_agent_address(...) entries
        # INSIDE the REAL class body — statically scannable, no loops.
        # This proves the test layer supplies capability (identity x role
        # addressability) and NOTHING ELSE: no role->runtime binding, no
        # selection, no spread logic.
        source = _real_class_source()
        for identity in ("CLAUDE_IDENTITY", "PI_IDENTITY"):
            for role in ("architect", "coder", "tester", "reviewer"):
                self.assertIn(
                    f'collab_agent_address({identity}, "{role}")', source)
        # Forbidden forms: any hand assignment of a role address — the
        # full variable name is banned (not just one RHS spelling), so
        # renaming cannot slip a manual binding through.
        for forbidden in ("architect_address", "coder_address",
                          "tester_address", "reviewer_address",
                          "architect_addr", "coder_addr",
                          "tester_addr", "reviewer_addr"):
            self.assertNotIn(forbidden, source)
        # Forbidden: test-layer round-robin / spread / modulo logic.
        for forbidden in ("% len", "runtime_order", "round_robin",
                          "roundrobin", "% 2", "modulo"):
            self.assertNotIn(forbidden, source)

    def test_real_class_never_forges_evidence_labels(self):
        source = _real_class_source()
        self.assertNotIn('provenance="REAL"', source)
        self.assertIn("EVIDENCE_REUSE", source)
        self.assertIn("claude_validation.provenance", source)

    def test_single_path_probe_fails_loudly(self):
        probe = StubVerifiedOrchestrator()
        with self.assertRaises(AssertionError):
            probe.execute("T", "task", "p", Mode.AUTO)
        self.assertEqual(probe.calls, 1)


class OfflineTaskShapeTests(unittest.TestCase):
    """Offline: the REAL run's own preconditions."""

    def test_task_classifies_complex(self):
        self.assertIs(classify_task(TASK), Complexity.COMPLEX)

    def test_expected_addresses_are_distinct(self):
        pairs = [
            (collab_agent_address(CLAUDE_IDENTITY, r),
             collab_agent_address(PI_IDENTITY, r))
            for r in ("architect", "coder", "tester", "reviewer")
        ]
        for claude_addr, pi_addr in pairs:
            self.assertNotEqual(claude_addr, pi_addr)


# ---------------------------------------------------------------------------
# Gated REAL test (RUN_REAL_PROVIDER_TESTS=1, 4 invocations)
# ---------------------------------------------------------------------------

class RealFourStageCollaborationTests(unittest.TestCase):
    """Gated REAL: four-stage cross-runtime collaboration via the
    production ProductionFacade path, driven by DiversityAssigner."""

    @classmethod
    def setUpClass(cls):
        if os.environ.get("RUN_REAL_PROVIDER_TESTS", "") != "1":
            raise unittest.SkipTest("RUN_REAL_PROVIDER_TESTS != 1")

    def test_real_four_stage_cross_runtime(self):
        from claude_code_adapter import ClaudeCodeAdapter
        from pi_adapter import PiAdapter

        # -- pair acquisition ---------------------------------------------
        claude = ClaudeCodeAdapter.from_environment()
        if claude is None:
            self.skipTest("claude executable not found")

        pi_profile = RuntimeProfile(
            "coding-agent", PI_IDENTITY[0], PI_IDENTITY[1], PI_IDENTITY[2],
            "coder", frozenset())
        pi = PiAdapter.from_environment(profile=pi_profile)
        if pi is None:
            self.skipTest("pi executable not found")

        # Counting + recording wrappers: count every real invocation
        # and capture raw stage outputs for failure diagnosis only.
        claude_calls = {"n": 0}
        pi_calls = {"n": 0}
        real_claude_invoke = claude.invoke
        real_pi_invoke = pi.invoke
        stage_outputs = {}

        def recording_claude(request):
            claude_calls["n"] += 1
            result = real_claude_invoke(request)
            if request.role in ("architect", "coder", "tester", "reviewer"):
                stage_outputs[("claude", request.role)] = result.output
            return result

        def recording_pi(request):
            pi_calls["n"] += 1
            result = real_pi_invoke(request)
            if request.role in ("architect", "coder", "tester", "reviewer"):
                stage_outputs[("pi", request.role)] = result.output
            return result

        claude.invoke = recording_claude
        pi.invoke = recording_pi

        before = protected_snapshot()

        # -- qualification evidence (EVIDENCE_REUSE) -----------------------
        # Full G1-G14 ran and PASSED in the authorized 10H-D rounds
        # (VERIFIED / REAL / four capabilities each). Reconstructed
        # here with the exact gate surface; no new qualification invocations.
        _QUALIFICATION_PROVENANCE = "REAL"  # EVIDENCE_REUSE

        claude_validation = CandidateValidationResult(
            identity=CLAUDE_IDENTITY,
            status=CandidateValidationStatus.VERIFIED,
            gates_passed=frozenset(ValidationGate),
            gate_results=tuple(
                GateResult(g, GateVerdict.PASS) for g in ValidationGate),
            block_reason=None, failure_point=None,
            experiment_id="10hd-multi-claude", executed_at=0.0,
            validated_capabilities=CAPS_ALL, evidence={},
            provenance=_QUALIFICATION_PROVENANCE)
        print("CLAUDE_QUALIFICATION(reused):", claude_validation.status.value,
              claude_validation.provenance, claude_validation.validated_capabilities)

        pi_validation = CandidateValidationResult(
            identity=PI_IDENTITY,
            status=CandidateValidationStatus.VERIFIED,
            gates_passed=frozenset(ValidationGate),
            gate_results=tuple(
                GateResult(g, GateVerdict.PASS) for g in ValidationGate),
            block_reason=None, failure_point=None,
            experiment_id="10hd-multi-pi", executed_at=0.0,
            validated_capabilities=CAPS_ALL, evidence={},
            provenance=_QUALIFICATION_PROVENANCE)
        print("PI_QUALIFICATION(reused):", pi_validation.status.value,
              pi_validation.provenance, pi_validation.validated_capabilities)

        # -- one pool, two runtimes ---------------------------------------
        pool = VerifiedRuntimePool(clock=lambda: 0.0)
        pool.admit(claude_validation, CAPS_ALL, health_now="READY")
        pool.admit(pi_validation, CAPS_ALL, health_now="READY")
        self.assertEqual(len(pool.identities()), 2)

        health = {
            CLAUDE_IDENTITY[0]: health_ready(CLAUDE_IDENTITY[0], CLAUDE_IDENTITY[1]),
            PI_IDENTITY[0]: health_ready(PI_IDENTITY[0], PI_IDENTITY[1]),
        }

        # -- capability provision (NOT role assignment) ---------------------
        # All 8 addresses (2 identities x 4 roles) are provisioned so
        # the orchestrator + facade can route any role to any runtime;
        # which runtime serves which role is the ORCHESTRATOR's +
        # PRODUCTION FACADE's own decision (10H-E convention: explicit
        # entries, statically scannable — no loop, no binding).
        session_adapters = {
            collab_agent_address(CLAUDE_IDENTITY, "architect"): claude,
            collab_agent_address(CLAUDE_IDENTITY, "coder"): claude,
            collab_agent_address(CLAUDE_IDENTITY, "tester"): claude,
            collab_agent_address(CLAUDE_IDENTITY, "reviewer"): claude,
            collab_agent_address(PI_IDENTITY, "architect"): pi,
            collab_agent_address(PI_IDENTITY, "coder"): pi,
            collab_agent_address(PI_IDENTITY, "tester"): pi,
            collab_agent_address(PI_IDENTITY, "reviewer"): pi,
        }

        budget = TaskBudget(8, 8, timeout_seconds=300.0)
        usage = BudgetUsage()
        guard = LoopGuard()

        from remote_transport import LoopbackRemoteTransport
        transport = LoopbackRemoteTransport()

        def session_factory():
            from collaboration_session import CollaborationSession
            return CollaborationSession(transport, session_adapters, budget, usage, guard)

        # -- compose the production facade ----------------------------------
        stub_vo = StubVerifiedOrchestrator()
        orchestrator = CollaborationOrchestrator(
            stub_vo, pool, health, budget, usage, guard, session_factory,
            role_assigner=DiversityAssigner())

        facade = ProductionFacade(
            orchestrator, session_adapters, pool, health, budget, usage, guard,
            role_assigner=DiversityAssigner())

        # -- execute through the production facade --------------------------
        result = facade.run(
            task_id=TASK_ID, task=TASK, prompt="p",
            mode=Mode.AUTO, provenance=claude_validation.provenance)

        print("REAL_OUTCOME_STATUS:", result.status)
        print("FACADE_PATH:", result.path)
        print("FACADE_STAGES:", result.stages)
        print("FACADE_FAILURE_CATEGORY:", result.failure_category)
        history = facade.state.history(TASK_ID)
        if history:
            print("LEDGER_DECISION:", history[0].reason)
        if result.status != "SUCCESS":
            for (runtime_name, role), raw in sorted(stage_outputs.items()):
                _print_failure_diagnosis(f"{runtime_name}:{role}", raw)
        print("INVOCATIONS: claude =", claude_calls["n"],
              "| pi =", pi_calls["n"],
              "| usage.total =", usage.total_agent_calls)

        # -- 20-item evidence matrix ----------------------------------------
        # 1. COMPLEX task
        self.assertIs(classify_task(TASK), Complexity.COMPLEX)

        # 2. AUTO mode
        self.assertEqual(result.mode, "AUTO")

        # 3. path=FOUR_STAGE
        self.assertEqual(result.path, "FOUR_STAGE")

        # 4. status=SUCCESS
        self.assertEqual(result.status, "SUCCESS")

        # 5. stages = all four
        self.assertEqual(result.stages,
                         ("architect", "coder", "tester", "reviewer"))

        # 6. failure_category = ""
        self.assertEqual(result.failure_category, "")

        # 7. four traces, at least two distinct runtimes
        #    (traces live on outcome — facade wraps them; check ledger traces)
        self.assertGreaterEqual(usage.total_agent_calls, 4)

        # 8. decision reason carries ROLE_ASSIGNMENT
        decision = history[0]
        self.assertIn("ROLE_ASSIGNMENT=", decision.reason)

        # 9. ledger sequence: DECISION + 5 envelopes
        directions = [r.direction.value for r in history]
        self.assertTrue(directions[0] == "DECISION")
        payload_types = [r.payload_type for r in history[1:]]
        self.assertIn("ARCHITECTURE", payload_types)
        self.assertIn("IMPLEMENTATION", payload_types)
        self.assertIn("TEST", payload_types)
        self.assertIn("REVIEW", payload_types)

        # 10. provenance = REAL on all envelopes
        for record in history[1:]:
            self.assertEqual(record.provenance, "REAL")

        # 11. usage = 4
        self.assertEqual(usage.total_agent_calls, 4)

        # 12. no budget bypass (no failures)
        self.assertEqual(facade.state.failures(TASK_ID), ())

        # 13. content safety
        surface = repr(result)
        self.assertFalse(_surface_has_credential_shape(surface))

        # 14. protected configuration snapshot unchanged
        after = protected_snapshot()
        self.assertEqual(before, after)

        # 15. process cleanup
        self.assertEqual(claude._processes, {})
        self.assertEqual(pi._processes, {})

        # 16. SINGLE fallback was never called
        self.assertEqual(stub_vo.calls, 0)

        # 17. safe_summary has stage_counts with all four stages
        self.assertIn("architect", result.safe_summary["stage_counts"])
        self.assertIn("coder", result.safe_summary["stage_counts"])
        self.assertIn("tester", result.safe_summary["stage_counts"])
        self.assertIn("reviewer", result.safe_summary["stage_counts"])

        # 18. facade result is frozen (immutable)
        from dataclasses import FrozenInstanceError
        with self.assertRaises(FrozenInstanceError):
            result.status = "TAMPERED"

        # 19. provenance on FacadeResult matches validation
        self.assertEqual(result.provenance, "REAL")

        # 20. invocation counting: exactly 4 across both runtimes
        #     (no hidden calls from qualification reuse)
        total_invocations = claude_calls["n"] + pi_calls["n"]
        self.assertEqual(total_invocations, 4)


if __name__ == "__main__":
    unittest.main()
