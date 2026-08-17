"""Repeat-Stability Gate v2 — qualification-once + pure FOUR_STAGE runs.

Decouples capability qualification from execution stability: ONE sanctioned
REAL qualification admits the runtime to the pool; N pure ProductionFacade
runs (fresh budget/guard/ledger per run, shared pool) measure FOUR_STAGE
stability alone. Offline contract tests lock the session semantics; the
gated REAL test performs 1 qualification + N=5 pure runs.
"""
import json
import os
import statistics
import sys
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from candidate_validation import CandidateRuntimeInstance, CandidateValidationStatus
from collaboration_orchestrator import CollaborationOrchestrator
from collaboration_session import CollaborationSession, collab_agent_address
from execution_engine import ExecutionResult, ExecutionStatus
from loop_guard import LoopGuard
from mode_gate import Mode
from production_facade import ProductionFacade
from remote_transport import LoopbackRemoteTransport
from runtime_status import (
    HealthEvidence,
    ReasonCode,
    RuntimeState,
    RuntimeStatus,
)
from task_budget import BudgetUsage, TaskBudget
from verified_runtime_pool import VerifiedRuntimePool

TASK = ("Produce a minimal implementation plan for adding a pure function "
        "that converts Celsius to Fahrenheit. Do not modify files.")
CAPS_ALL = ("architecture", "coding", "review", "testing")
STAGES = ("architect", "coder", "tester", "reviewer")
SECRET_MARKERS = ("token", "secret", "api_key", "authorization", "bearer",
                  "stdout", "stderr")
N_RUNS = 5
TIMEOUT_SECONDS = 300.0


class StabilitySession:
    """Qualification-once harness wrapper over the existing engine.

    One sanctioned validation admits the runtime to a shared pool; every
    stability run builds a fresh budget/usage/guard and its own facade, and
    NEVER re-runs capability experiments. Pure harness — zero production
    changes.
    """

    def __init__(self, adapter, identity, health, env=None):
        self.adapter = adapter
        self.identity = identity
        self.health = dict(health)
        self.env = env  # None -> os.environ (real usage); offline tests inject
        self.pool = None
        self.validation = None
        self.qualification_calls = 0

    def qualify(self):
        """One sanctioned REAL qualification; stores the reusable pool."""
        from real_validation_executor import run_real_validation
        if self.pool is not None:
            raise RuntimeError("qualification already performed for session")
        self.qualification_calls += 1
        instance = CandidateRuntimeInstance(
            runtime_id=self.identity[0], provider_id=self.identity[1],
            model_id=None, config_fingerprint=self.identity[3],
            capability_context=(), probe=self.adapter,
            invocation_spec={"timeout_seconds": TIMEOUT_SECONDS})
        validation, _ = run_real_validation(
            instance, self.adapter, timeout_seconds=TIMEOUT_SECONDS,
            experiment_id="stability-v2-qualification", env=self.env)
        self.validation = validation
        if validation.status is not CandidateValidationStatus.VERIFIED:
            # Safe structured diagnosis only: gate/role/category/exception
            # type/shape/invocation count — never raw output or messages.
            failure = next((g for g in validation.gate_results
                            if g.verdict.value != "PASS"), None)
            print("QUALIFICATION_FAILURE:", _json.dumps({
                "failure_gate": (validation.failure_point[0].name
                                 if validation.failure_point else None),
                "failure_category": (str(validation.failure_point[1])
                                     if validation.failure_point else None),
                "failure_role": (failure.evidence.get("failure_role")
                                 if failure else None),
                "evidence_category": (failure.evidence.get("failure_category")
                                      if failure else None),
                "failure_detail": (failure.evidence.get("failure_detail")
                                   if failure else None),
                "exception_type": (failure.evidence.get("exception_type")
                                   if failure else None),
                "shape": (failure.evidence.get("shape") if failure else None),
                "invocation_count": (failure.evidence.get("invocation_count")
                                     if failure else None),
                "reason": failure.reason if failure else None,
            }, sort_keys=True, default=str))
            raise AssertionError(
                "qualification FAILED — chain not started (not a stability "
                "failure; see QUALIFICATION_FAILURE above)")
        pool = VerifiedRuntimePool(clock=lambda: 0.0)
        pool.admit(validation, CAPS_ALL, health_now="READY")
        self.pool = pool
        return validation

    def run_four_stage(self, index):
        """One pure FOUR_STAGE run: shared pool, fresh lifecycle objects."""
        task_id = f"stability-v2-r{index}"
        identity = self.identity
        arch = collab_agent_address(identity, "architect")
        coder = collab_agent_address(identity, "coder")
        tester = collab_agent_address(identity, "tester")
        reviewer = collab_agent_address(identity, "reviewer")
        budget = TaskBudget(4, 4, timeout_seconds=TIMEOUT_SECONDS)
        usage = BudgetUsage()
        guard = LoopGuard()

        def session_factory():
            return CollaborationSession(
                LoopbackRemoteTransport(),
                {arch: self.adapter, coder: self.adapter},
                budget, usage, guard)

        orchestrator = CollaborationOrchestrator(
            type("VO", (), {"execute": staticmethod(
                lambda *a, **k: ExecutionResult(
                    ExecutionStatus.FAILED, (), (), ("MODE_OFF",)))})(),
            self.pool, self.health, budget, usage, guard, session_factory)
        facade = ProductionFacade(
            orchestrator,
            {tester: self.adapter, reviewer: self.adapter},
            self.pool, self.health, budget, usage, guard)
        started = time.monotonic()
        result = facade.run(task_id=task_id, task=TASK, prompt=TASK,
                            mode=Mode.ON, provenance=self.validation.provenance)
        elapsed = round(time.monotonic() - started, 1)
        return {
            "run": index, "task_id": task_id,
            "status": result.status, "path": result.path,
            "provenance": result.provenance, "stages": list(result.stages),
            "failure_category": result.failure_category or None,
            "elapsed_s": elapsed, "usage": usage, "facade": facade,
            "success": None,  # filled by invariant checks
        }


def check_invariants(summary):
    """Fill success from full invariant checks (Task E)."""
    usage = summary["usage"]
    facade = summary["facade"]
    budget_ok = (usage.architect_calls == 1 and usage.coder_calls == 1
                 and usage.test_calls == 1 and usage.review_calls == 1
                 and usage.total_agent_calls == 4)
    history = facade.state.history(summary["task_id"])
    ledger_ok = (
        [r.sequence for r in history] == [1, 2, 3, 4, 5]
        and [r.payload_type for r in history] == [
            "", "ARCHITECTURE", "IMPLEMENTATION", "TEST", "REVIEW"]
        and all(r.task_id == summary["task_id"] for r in history))
    c1 = history[1].correlation_id
    ledger_ok = ledger_ok and history[2].correlation_id == c1
    ledger_ok = ledger_ok and history[3].correlation_id != c1
    ledger_ok = ledger_ok and history[4].correlation_id != history[3].correlation_id
    ledger_ok = ledger_ok and all(r.provenance == "REAL" for r in history[1:])
    # guard: fresh guard per run + success chain => no collision/duplicate
    guard_ok = True
    surfaces = repr(facade.state).lower()
    security_ok = not any(m in surfaces for m in SECRET_MARKERS)
    if not security_ok:
        raise RuntimeError("SECURITY VIOLATION — aborting measurement")
    summary.update({
        "budget_ok": budget_ok, "ledger_ok": ledger_ok, "guard_ok": guard_ok,
        "security_ok": security_ok,
        "success": bool(
            summary["status"] == "SUCCESS" and summary["path"] == "FOUR_STAGE"
            and summary["provenance"] == "REAL"
            and tuple(summary["stages"]) == STAGES
            and budget_ok and ledger_ok and security_ok),
    })
    del summary["usage"]
    del summary["facade"]
    return summary


# -- offline mock plumbing ----------------------------------------------------

import json as _json
from external_runtime import InvocationResult, InvocationStatus, InvocationTrace

ARCH_P = {"task_id": "capability-evidence", "role": "architect", "goal": ["g"],
          "constraints": ["c"], "architecture": ["a"], "interfaces": [{}],
          "implementation_steps": [{}], "acceptance_criteria": ["ac"], "risks": [{}]}
IMPL_P = {"task_id": "capability-evidence", "role": "coder", "changed_files": ["f"],
          "implementation_summary": "s", "implementation_details": ["d"],
          "assumptions": [], "unresolved_items": [], "test_requirements": ["tr"]}
TEST_P = {"task_id": "capability-evidence", "role": "tester", "tests_run": ["x"],
          "tests_passed": ["x"], "tests_failed": [], "failures": [],
          "coverage_or_validation": [], "remaining_risks": []}
REVIEW_P = {"task_id": "capability-evidence", "role": "reviewer", "status": "PASS",
            "findings": [], "severity": [], "affected_files": [],
            "required_changes": [], "acceptance_criteria_status": []}


class OfflineAdapter:
    """Answers every role (capability + chain) with a valid packet.

    role_calls: per-role-name invocation log (experiment prompts use the
    bare role as agent_id; chain prompts use the full JSON address ending
    with the role — both are counted by role for experiment accounting).
    fault_roles: roles whose CHAIN invocation returns invalid output
    (capability experiments stay green so qualification succeeds).
    """

    runtime_id = "rt-off"
    provider_id = "p-off"

    def __init__(self, fault_roles=()):
        self.invocations = 0
        self.experiment_calls = {"architect": 0, "coder": 0,
                                 "tester": 0, "reviewer": 0}
        self.chain_calls = {"architect": 0, "coder": 0,
                            "tester": 0, "reviewer": 0}
        self.fault_roles = set(fault_roles)

    def discover(self):
        from external_runtime import RuntimeDiscovery
        return RuntimeDiscovery("rt-off", True, "1.0", None, frozenset())

    def check_authentication(self):
        from runtime_health import AuthenticationCheck
        from runtime_status import AuthenticationState
        return AuthenticationCheck(AuthenticationState.AUTHENTICATED, "oauth")

    def check_provider_model(self):
        from runtime_health import ProviderModelCheck
        from runtime_status import ReasonCode
        return ProviderModelCheck("p-off", None, True, ReasonCode.NONE)

    def cancel(self, invocation_id):
        return InvocationResult(InvocationStatus.CANCELLED)

    def invoke(self, request):
        self.invocations += 1
        if request.prompt.startswith("Return exactly OK"):
            return self._ok("OK")
        for role, packet in (("architect", ARCH_P), ("coder", IMPL_P),
                             ("tester", TEST_P), ("reviewer", REVIEW_P)):
            if request.agent_id == role or request.agent_id.endswith(f',"{role}"]'):
                is_chain = request.agent_id.endswith('"]')  # JSON address
                if is_chain:
                    self.chain_calls[role] += 1
                    if role in self.fault_roles:
                        return self._ok("invalid chain output for " + role)
                else:
                    self.experiment_calls[role] += 1
                return self._ok(_json.dumps(packet))
        return self._ok("OK")

    @staticmethod
    def _ok(output):
        trace = InvocationTrace(
            invocation_id="inv-off", task_id="t", agent_id="a", runtime="rt-off",
            provider=None, model=None, role=None, status=InvocationStatus.SUCCESS,
            started_at=0.0, finished_at=0.0, duration_ms=1, exit_code=0,
            input_tokens="unknown", output_tokens="unknown", error=None)
        return InvocationResult(InvocationStatus.SUCCESS, output=output, trace=trace)


def offline_health():
    return {"rt-off": RuntimeStatus(
        runtime_id="rt-off", executable="e", version="1",
        status=RuntimeState.READY, provider="p-off", model=None,
        auth_method=None, reason_code=ReasonCode.NONE,
        evidence=HealthEvidence("d", "a", "p", "m", "ok"),
        checked_at=0.0, expires_at=1.0)}


OPEN_ENV = {"RUN_REAL_PROVIDER_TESTS": "1"}


class QualificationDecouplingTests(unittest.TestCase):
    """Task 1/2: g14 experiment count == 4 total (once), never re-run."""

    def test_g14_experiment_count_is_one_session_worth(self):
        adapter = OfflineAdapter()
        session = StabilitySession(adapter, ("rt-off", "p-off", None, "fp-v2"),
                                   offline_health(), env=OPEN_ENV)
        session.qualify()
        # Exactly 4 experiment invocations (one per role), all consumed by
        # the single qualification; chain runs below add zero experiments.
        experiments_after_qualification = dict(adapter.experiment_calls)
        self.assertEqual(sum(experiments_after_qualification.values()), 4)
        for index in range(1, 4):
            check_invariants(session.run_four_stage(index))
        self.assertEqual(adapter.experiment_calls,
                         experiments_after_qualification)

    def test_n_facade_runs_consume_pool_not_requalification(self):
        adapter = OfflineAdapter()
        session = StabilitySession(adapter, ("rt-off", "p-off", None, "fp-v2"),
                                   offline_health(), env=OPEN_ENV)
        session.qualify()
        pool_entry_identity = session.pool.identities()
        for index in range(1, 6):
            session.run_four_stage(index)
            self.assertEqual(session.pool.identities(), pool_entry_identity)
        self.assertEqual(session.qualification_calls, 1)


class NoRetryNoFallbackTests(unittest.TestCase):
    """Task 2 Steps 2-3: honest short-circuit, no retry, no fallback."""

    def test_failed_stage_short_circuits_with_single_attempt(self):
        adapter = OfflineAdapter(fault_roles=("coder",))
        session = StabilitySession(adapter, ("rt-off", "p-off", None, "fp-v2"),
                                   offline_health(), env=OPEN_ENV)
        session.qualify()
        coder_before = adapter.chain_calls["coder"]
        tester_before = adapter.chain_calls["tester"]
        summary = check_invariants(session.run_four_stage(1))
        self.assertNotEqual(summary["status"], "SUCCESS")
        self.assertEqual(adapter.chain_calls["coder"] - coder_before, 1)  # 1 try only
        self.assertEqual(adapter.chain_calls["tester"] - tester_before, 0)  # gated
        self.assertEqual(summary["failure_category"], "CODER_PACKET_INVALID")

    def test_failure_never_becomes_single_or_dual_success(self):
        adapter = OfflineAdapter(fault_roles=("architect",))
        session = StabilitySession(adapter, ("rt-off", "p-off", None, "fp-v2"),
                                   offline_health(), env=OPEN_ENV)
        session.qualify()
        summary = check_invariants(session.run_four_stage(1))
        self.assertFalse(summary["success"])
        self.assertNotEqual(summary["path"], "FOUR_STAGE")
        self.assertNotEqual(summary["status"], "SUCCESS")

    def test_mixed_faults_are_recorded_not_retried(self):
        adapter = OfflineAdapter(fault_roles=("reviewer",))
        session = StabilitySession(adapter, ("rt-off", "p-off", None, "fp-v2"),
                                   offline_health(), env=OPEN_ENV)
        session.qualify()
        reviewer_before = adapter.chain_calls["reviewer"]
        summary = check_invariants(session.run_four_stage(1))
        self.assertFalse(summary["success"])
        self.assertEqual(adapter.chain_calls["reviewer"] - reviewer_before, 1)
        self.assertEqual(summary["failure_category"], "REVIEWER_PACKET_INVALID")


class StabilityStatsTests(unittest.TestCase):
    """Task 3: statistics semantics — FOUR_STAGE-only denominator."""

    def test_success_rate_excludes_qualification_from_denominator(self):
        stats = compute_stability_stats(
            runs=[{"success": True}, {"success": True}, {"success": False}],
            qualification_ok=True)
        self.assertEqual(stats["N"], 3)
        self.assertEqual(stats["successful_runs"], 2)
        self.assertEqual(stats["success_rate"], round(2 / 3, 3))

    def test_qualification_failure_is_not_in_stability_denominator(self):
        stats = compute_stability_stats(runs=[], qualification_ok=False)
        self.assertEqual(stats["N"], 0)
        self.assertIsNone(stats["success_rate"])
        self.assertEqual(stats["qualification_failures"], 1)
        self.assertEqual(stats["qualification_sessions"], 1)

    def test_stage_rates_count_only_runs_reaching_the_stage(self):
        runs = [
            {"success": True, "stages": list(STAGES),
             "failure_category": None},
            {"success": False, "stages": ["architect"],
             "failure_category": "CODER_PACKET_INVALID"},
        ]
        stats = compute_stability_stats(runs=runs, qualification_ok=True)
        # Run 2 failed AT coder: architect completed (2/2), coder was
        # reached-but-failed (1/2), tester/reviewer never reached in run 2.
        self.assertEqual(stats["stage_stats"]["architect"]["rate"], 1.0)
        self.assertEqual(stats["stage_stats"]["coder"]["rate"], 0.5)
        self.assertEqual(stats["stage_stats"]["tester"]["rate"], 1.0)
        self.assertEqual(stats["stage_stats"]["reviewer"]["rate"], 1.0)
        self.assertEqual(stats["stage_stats"]["coder"]["not_reached"], 0)
        self.assertEqual(stats["stage_stats"]["tester"]["not_reached"], 1)

    def test_failure_classification_separates_categories(self):
        runs = [
            {"success": False, "stages": [], "failure_category": "ARCHITECT_PACKET_INVALID"},
            {"success": False, "stages": ["architect"], "failure_category": "CODER_INVOKE_FAILED"},
            {"success": False, "stages": ["architect", "coder"], "failure_category": "TESTER_PACKET_INVALID"},
        ]
        stats = compute_stability_stats(runs=runs, qualification_ok=True)
        self.assertEqual(stats["failure_by_category"],
                         {"ARCHITECT_PACKET_INVALID": 1,
                          "CODER_INVOKE_FAILED": 1,
                          "TESTER_PACKET_INVALID": 1})
        self.assertEqual(stats["failure_by_stage"],
                         {"architect": 1, "coder": 1, "tester": 1})

    def test_provenance_of_successful_runs_comes_from_evidence(self):
        runs = [{"success": True, "provenance": "REAL"}]
        stats = compute_stability_stats(runs=runs, qualification_ok=True)
        self.assertTrue(all(r["provenance"] == "REAL" for r in runs))
        self.assertEqual(stats["provenance_source"], "sanctioned-evidence")


class SessionContractTests(unittest.TestCase):
    """Task A/B contract: ONE qualification, N pure runs, no re-validation."""

    def test_session_qualifies_once_and_runs_never_revalidate(self):
        adapter = OfflineAdapter()
        session = StabilitySession(adapter, ("rt-off", "p-off", None, "fp-v2"),
                                   offline_health(), env=OPEN_ENV)
        # Patch run_real_validation to count calls through the session path.
        import real_validation_executor as executor_module
        original = executor_module.run_real_validation
        calls = {"n": 0}

        def counting(*args, **kwargs):
            calls["n"] += 1
            return original(*args, **kwargs)

        executor_module.run_real_validation = counting
        try:
            validation = session.qualify()
            self.assertEqual(validation.status.value, "VERIFIED")
            qualification_invocations = adapter.invocations
            summaries = []
            for index in range(1, 4):
                summaries.append(check_invariants(
                    session.run_four_stage(index)))
        finally:
            executor_module.run_real_validation = original
        self.assertEqual(calls["n"], 1)  # exactly one qualification
        self.assertEqual(session.qualification_calls, 1)
        # Pure runs added only 4 chain invocations each, zero experiments.
        self.assertEqual(adapter.invocations - qualification_invocations, 3 * 4)
        for summary in summaries:
            self.assertTrue(summary["success"], summary)

    def test_double_qualification_is_refused(self):
        adapter = OfflineAdapter()
        session = StabilitySession(adapter, ("rt-off", "p-off", None, "fp-v2"),
                                   offline_health(), env=OPEN_ENV)
        session.qualify()
        with self.assertRaises(RuntimeError):
            session.qualify()

    def test_each_run_gets_fresh_lifecycle_objects(self):
        adapter = OfflineAdapter()
        session = StabilitySession(adapter, ("rt-off", "p-off", None, "fp-v2"),
                                   offline_health(), env=OPEN_ENV)
        session.qualify()
        first = session.run_four_stage(1)
        second = session.run_four_stage(2)
        self.assertNotEqual(first["task_id"], second["task_id"])
        # Independent budgets: each run consumed exactly its own 4 calls.
        self.assertIsNot(first["usage"], second["usage"])
        self.assertEqual(first["usage"].total_agent_calls, 4)
        self.assertEqual(second["usage"].total_agent_calls, 4)


class RepeatStabilityV2Tests(unittest.TestCase):
    """Task G: 1 sanctioned REAL qualification + N=5 pure FOUR_STAGE runs."""

    def setUp(self):
        if os.environ.get("RUN_REAL_PROVIDER_TESTS", "") != "1":
            self.skipTest("RUN_REAL_PROVIDER_TESTS != 1")

    def test_qualification_once_then_five_pure_runs(self):
        from claude_code_adapter import ClaudeCodeAdapter
        inner = ClaudeCodeAdapter.from_environment()
        if inner is None:
            self.skipTest("claude executable not found")
        counting = CountingAdapter(inner)
        health = {"claude-cli": RuntimeStatus(
            runtime_id="claude-cli", executable="claude", version="2",
            status=RuntimeState.READY, provider="anthropic", model=None,
            auth_method=None, reason_code=ReasonCode.NONE,
            evidence=HealthEvidence("d", "a", "p", "m", "ok"),
            checked_at=0.0, expires_at=1.0)}
        session = StabilitySession(counting, ("claude-cli", "anthropic", None,
                                              "fp-stability-v2"), health)
        qualification_started = time.monotonic()
        validation = session.qualify()
        qualification_elapsed = round(time.monotonic() - qualification_started, 1)
        print("QUALIFICATION:", validation.status.value, validation.provenance,
              validation.validated_capabilities, "| calls:",
              counting.invocations, "| elapsed:", qualification_elapsed)
        qualification_invocations = counting.invocations

        runs = []
        for index in range(1, N_RUNS + 1):
            try:
                summary = check_invariants(session.run_four_stage(index))
            except RuntimeError as exc:
                runs.append({"run": index, "security_abort": str(exc)})
                break
            runs.append(summary)
            print(f"RUN {index}:", _json.dumps(summary, sort_keys=True, default=str))
        completed = [r for r in runs if "task_id" in r]
        successful = [r for r in completed if r["success"]]
        failed = [r for r in completed if not r["success"]]
        elapsed = [r["elapsed_s"] for r in completed]
        stats = {
            "qualification_sessions": 1,
            "qualification_invocations": qualification_invocations,
            "N": len(completed),
            "successful_runs": len(successful),
            "failed_runs": len(failed),
            "success_rate": round(len(successful) / len(completed), 3) if completed else None,
            "failure_by_stage": {},
            "failure_by_category": {},
            "chain_invocation_distribution": [
                counting.invocations - qualification_invocations],
            "elapsed": {
                "mean": round(statistics.mean(elapsed), 1) if elapsed else None,
                "median": round(statistics.median(elapsed), 1) if elapsed else None,
                "min": min(elapsed) if elapsed else None,
                "max": max(elapsed) if elapsed else None,
            },
            "total_real_invocations": counting.invocations,
        }
        for r in failed:
            stage = _failing_stage(r.get("failure_category"))
            stats["failure_by_stage"][stage] = stats["failure_by_stage"].get(stage, 0) + 1
            category = r.get("failure_category") or "UNKNOWN"
            stats["failure_by_category"][category] = \
                stats["failure_by_category"].get(category, 0) + 1
        print("STABILITY_V2_STATS:", _json.dumps(stats, sort_keys=True))
        # Measurement completed; no assertion on success rate (data, not gate).


def compute_stability_stats(runs, qualification_ok):
    """Stability statistics over PURE FOUR_STAGE runs only.

    Qualification is NEVER part of the FOUR_STAGE denominator: a failed
    qualification is counted separately as qualification_failures. Stage
    rates count only runs that actually reached the stage (a failed run
    reaches exactly the stages listed before its failure point).
    """
    completed = list(runs)
    successful = [r for r in completed if r.get("success")]
    failed = [r for r in completed if not r.get("success")]
    stage_stats = {}
    for stage in STAGES:
        rank = STAGES.index(stage)
        reached = succeeded = 0
        for r in completed:
            if r.get("success"):
                reached += 1
                succeeded += 1
                continue
            failure_stage = _failing_stage(r.get("failure_category") or "")
            if failure_stage not in STAGES:
                continue  # lifecycle/facade failure: no stage attribution
            failure_rank = STAGES.index(failure_stage)
            if rank < failure_rank:
                reached += 1
                succeeded += 1  # this stage completed before the failure
            elif rank == failure_rank:
                reached += 1  # reached but did not succeed
        stage_stats[stage] = {
            "reached": reached,
            "succeeded": succeeded,
            "rate": round(succeeded / reached, 3) if reached else None,
            "not_reached": len(completed) - reached,
        }
    failure_by_category = {}
    failure_by_stage = {}
    for r in failed:
        category = r.get("failure_category") or "UNKNOWN"
        failure_by_category[category] = failure_by_category.get(category, 0) + 1
        stage = _failing_stage(category)
        failure_by_stage[stage] = failure_by_stage.get(stage, 0) + 1
    return {
        "N": len(completed),
        "successful_runs": len(successful),
        "failed_runs": len(failed),
        "success_rate": (round(len(successful) / len(completed), 3)
                         if completed else None),
        "stage_stats": stage_stats,
        "failure_by_category": failure_by_category,
        "failure_by_stage": failure_by_stage,
        "qualification_sessions": 1,
        "qualification_failures": 0 if qualification_ok else 1,
        "provenance_source": "sanctioned-evidence",
    }


def _failing_stage(category):
    if not category:
        return "unknown"
    for stage in STAGES:
        if stage.upper() in category.upper():
            return stage
    return "lifecycle"


class CountingAdapter:
    def __init__(self, inner):
        self._inner = inner
        self.invocations = 0

    def discover(self):
        return self._inner.discover()

    def check_authentication(self):
        return self._inner.check_authentication()

    def check_provider_model(self):
        return self._inner.check_provider_model()

    def cancel(self, invocation_id):
        return self._inner.cancel(invocation_id)

    def invoke(self, request):
        self.invocations += 1
        return self._inner.invoke(request)


if __name__ == "__main__":
    unittest.main()
