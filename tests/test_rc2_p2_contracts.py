"""Phase RC-2-P2: SINGLE failure granularity + harness evidence contracts.

Locks what the SINGLE production path ACTUALLY surfaces today (the granular
reason lives in ExecutionResult.errors; the facade maps failure_category to
the coarse status) and locks the harness rule: REAL qualification failure
diagnostics must be read from production evidence, never re-derived from
reason strings, captured output, or raw runtime text.
"""
import json
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from collaboration_orchestrator import CollaborationOrchestrator
from execution_engine import ExecutionResult, ExecutionStatus
from external_runtime import InvocationResult, InvocationStatus, InvocationTrace
from host import build_facade
from loop_guard import LoopGuard
from mode_gate import Mode
from production_facade import FacadeResult
from task_budget import BudgetUsage, TaskBudget

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_host_integration import HostAdapter, health, validation_result

SECRET_MARKERS = ("token", "secret", "api_key", "authorization", "bearer", "stdout", "stderr")


def executed(result: ExecutionResult):
    """A stub SINGLE executor returning the given ExecutionResult."""
    return result


class SingleFailureGranularityTests(unittest.TestCase):
    """Task A: lock current SINGLE failure surfacing + document the gap."""

    def _facade_with(self, executor_result):
        adapter = HostAdapter()
        budget = TaskBudget(4, 4, timeout_seconds=30.0)
        usage = BudgetUsage()
        guard = LoopGuard()

        class VO:
            def plan(self, task_id, task, mode=Mode.AUTO):
                from invocation_plan import InvocationPlan
                return InvocationPlan(task_id, "AUTO", "SIMPLE", (), (), (),
                                      budget.to_dict(), ("REASON",))

            def execute(self, task_id, task, prompt, mode=Mode.AUTO):
                return executor_result

        from remote_transport import LoopbackRemoteTransport
        from collaboration_session import CollaborationSession
        orchestrator = CollaborationOrchestrator(
            VO(), None, {}, budget, usage, guard,
            lambda: CollaborationSession(LoopbackRemoteTransport(), {},
                                         budget, usage, guard))
        from production_facade import ProductionFacade
        return ProductionFacade(orchestrator, {}, None, {}, budget, usage, guard)

    def _single_failure(self, errors):
        result = ExecutionResult(ExecutionStatus.FAILED, (), (), tuple(errors))
        facade = self._facade_with(result)
        return facade.run(task_id="t", task="fix one simple bug",
                          prompt="fix one simple bug", mode=Mode.AUTO)

    def test_single_failure_surfaces_coarse_status_today(self):
        """CURRENT CONTRACT (AUDIT GAP RC2-P2-A): every SINGLE failure maps
        failure_category to the coarse ExecutionStatus ('FAILED'); the
        granular reason (errors tuple) is intentionally NOT surfaced by the
        protected facade. Granularity fix requires a facade change and is
        recorded as a P2 gap, not silently altered here."""
        for errors in (("BUDGET_EXHAUSTED",), ("MISSING_HANDOFF",),
                       ("INVOKE_FAILED",), ("RUNTIME_NOT_READY",),
                       ("LOOP_GUARD", "DUPLICATE")):
            with self.subTest(errors=errors):
                result = self._single_failure(errors)
                self.assertIsInstance(result, FacadeResult)
                self.assertEqual(result.status, "FAILED")
                self.assertEqual(result.path, "SINGLE")
                self.assertEqual(result.failure_category, "FAILED")

    def test_single_success_category_reflects_status(self):
        ok = ExecutionResult(ExecutionStatus.SUCCESS, (), (), ())
        result = self._facade_with(ok).run(
            task_id="t", task="fix one simple bug",
            prompt="fix one simple bug", mode=Mode.AUTO)
        self.assertEqual(result.status, "SUCCESS")
        # CURRENT mapping: failure_category mirrors the status on the
        # SINGLE path (success carries the status, failures 'FAILED').
        self.assertEqual(result.failure_category, "SUCCESS")

    def test_single_failure_summary_is_secret_free(self):
        result = self._single_failure(("INVOKE_FAILED", "token=abc"))
        surface = repr(result).lower()
        for marker in SECRET_MARKERS:
            self.assertNotIn(marker, surface)

    def test_budget_failure_contract_dual_side_is_granular(self):
        """The DUAL/collaboration path DOES surface granular categories —
        locked since 10H-K; the asymmetry is the documented gap."""
        adapter = HostAdapter()
        guard = LoopGuard()
        guard.record("t2", "architect",
                    __import__("collaboration_session").collab_agent_address(
                        ("rt-host", "provider-h", None, "fp-host"), "architect"))
        # A pre-recorded architect stage forces LOOP_GUARD on the dual path.
        from collaboration_session import collab_agent_address
        identity = ("rt-host", "provider-h", None, "fp-host")
        guard2 = LoopGuard()
        guard2.record("t2", "architect", collab_agent_address(identity, "architect"))
        facade = build_facade(adapter, validation_result(), health())
        # Use the fresh facade's own lifecycle for a guard rejection proof.
        guard3 = facade._loop_guard
        guard3.record("t2", "architect", collab_agent_address(identity, "architect"))
        result = facade.run(task_id="t2",
                            task="redesign architecture across modules",
                            prompt="redesign architecture across modules",
                            mode=Mode.ON)
        self.assertEqual(result.path, "DUAL")
        self.assertEqual(result.failure_category, "LOOP_GUARD_REJECTED")


class HarnessEvidenceContracts(unittest.TestCase):
    """Task B: REAL diagnostics read production evidence, never re-derive."""

    def test_qualification_failure_diagnostic_reads_production_evidence(self):
        """The stability/rc2 harness prints gate/role/category/exception/
        shape straight from validation.gate_results evidence — locked by
        inspecting the printing source for re-derivation patterns."""
        import re
        harnesses = [
            "tests/test_repeat_stability_v2.py",
            "tests/test_rc2br_single_real.py",
            "tests/test_g14_diagnostic_n5.py",
        ]
        for name in harnesses:
            with self.subTest(harness=name):
                source = Path(name).read_text(encoding="utf-8")
                # Reads production evidence fields...
                self.assertIn("evidence.get", source)
                # ...and never re-derives from captured raw output.
                self.assertNotIn("reason in", source)
                self.assertNotIn("captured[-1][2]", source)

    def test_diagnostic_fields_are_the_structured_vocabulary(self):
        import test_rc2br_single_real as harness
        source = Path(harness.__file__).read_text(encoding="utf-8")
        # Semantic diagnostic fields (short key forms used by this harness).
        for field in ("\"gate\"", "\"category\"", "\"role\"",
                      "\"detail\"", "\"exception_type\"", "\"shape\"",
                      "\"invocations\""):
            self.assertIn(field, source)
        for forbidden in ("print(stdout", "print(stderr", "print(prompt"):
            self.assertNotIn(forbidden, source)


class FailureTaxonomyAuditTests(unittest.TestCase):
    """Task D: canonical taxonomy is sufficient — locked, not refactored."""

    def test_single_side_categories_exist_in_engine_errors(self):
        """ExecutionEngine error tokens (the SINGLE-side vocabulary).
        MISSING_HANDOFF surfaces via str(HandoffError('MISSING_HANDOFF'))
        — the literal lives in handoff_context; the engine propagates it."""
        import execution_engine as module
        source = Path(module.__file__).read_text(encoding="utf-8")
        for token in ("INVOKE_FAILED", "BUDGET_EXHAUSTED", "RUNTIME_NOT_READY",
                      "LOOP_GUARD"):
            self.assertIn(token, source)
        self.assertIn("except HandoffError", source.replace(
            "except HandoffError as exc", "except HandoffError"))
        import handoff_context
        self.assertIn("MISSING_HANDOFF",
                      Path(handoff_context.__file__).read_text(encoding="utf-8"))

    def test_dual_side_enum_is_closed_and_granular(self):
        from collaboration_session import CollaborationStatus
        self.assertEqual(
            {member.name for member in CollaborationStatus},
            {"SUCCESS", "ARCHITECT_INVOKE_FAILED", "ARCHITECT_PACKET_INVALID",
             "CODER_INVOKE_FAILED", "CODER_PACKET_INVALID", "TRANSPORT_FAILED",
             "CORRELATION_MISMATCH", "BUDGET_EXHAUSTED", "LOOP_GUARD_REJECTED"})

    def test_verification_side_enum_is_closed(self):
        from verification_collaboration import VerificationStatus
        self.assertEqual(
            {member.name for member in VerificationStatus},
            {"SUCCESS", "TESTER_INVOKE_FAILED", "TESTER_PACKET_INVALID",
             "REVIEWER_INVOKE_FAILED", "REVIEWER_PACKET_INVALID",
             "BUDGET_EXHAUSTED", "LOOP_GUARD_REJECTED", "MISSING_HANDOFF"})

    def test_no_fake_categories_were_invented(self):
        """The three vocabularies stay as-is: no new members added by this
        phase (locked by exact member sets above)."""
        import candidate_validation
        self.assertEqual(
            {member.name for member in candidate_validation.CandidateValidationStatus},
            {"VERIFIED", "BLOCKED", "FAILED", "NOT_VERIFIED"})


if __name__ == "__main__":
    unittest.main()
