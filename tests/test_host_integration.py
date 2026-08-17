"""Phase RC-2A: minimal host integration — the real user entry seam.

Locks that a host layer exists, wires the ALREADY-VERIFIED engine (pool from
sanctioned qualification, REAL VerifiedOrchestrator for the SINGLE path, the
collaboration stack for FOUR_STAGE) without reimplementing any orchestration,
and that the CLI can actually reach ProductionFacade through it. Offline
tests use mock adapters; the gated REAL smoke proves the live path.
"""
import json
import sys
import unittest
from dataclasses import fields
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from candidate_validation import (
    CandidateRuntimeInstance,
    CandidateValidationResult,
    CandidateValidationStatus,
    GateResult,
    GateVerdict,
    ValidationGate,
)
from external_runtime import InvocationResult, InvocationStatus, InvocationTrace
from host import build_facade
from mode_gate import Mode
from production_facade import FacadeResult, ProductionFacade
from runtime_status import (
    HealthEvidence,
    ReasonCode,
    RuntimeState,
    RuntimeStatus,
)
from task_budget import BudgetUsage
from verified_selection_bridge import agent_id_for

IDENTITY = ("rt-host", "provider-h", None, "fp-host")
CAPS_ALL = ("architecture", "coding", "review", "testing")
SECRET_MARKERS = ("token", "secret", "api_key", "authorization", "bearer", "stdout", "stderr")

ARCH_P = {"task_id": "t", "role": "architect", "goal": ["g"], "constraints": ["c"],
          "architecture": ["a"], "interfaces": [{}], "implementation_steps": [{}],
          "acceptance_criteria": ["ac"], "risks": [{}]}
IMPL_P = {"task_id": "t", "role": "coder", "changed_files": ["f"],
           "implementation_summary": "s", "implementation_details": ["d"],
           "assumptions": [], "unresolved_items": [], "test_requirements": ["tr"]}
TEST_P = {"task_id": "t", "role": "tester", "tests_run": ["x"], "tests_passed": ["x"],
          "tests_failed": [], "failures": [], "coverage_or_validation": [],
          "remaining_risks": []}
REVIEW_P = {"task_id": "t", "role": "reviewer", "status": "PASS", "findings": [],
            "severity": [], "affected_files": [], "required_changes": [],
            "acceptance_criteria_status": []}


IDENTITY_BARE = agent_id_for(IDENTITY)


def trace():
    return InvocationTrace(
        invocation_id="inv-h", task_id="t", agent_id="a", runtime="rt-host",
        provider=None, model=None, role=None, status=InvocationStatus.SUCCESS,
        started_at=0.0, finished_at=0.0, duration_ms=1, exit_code=0,
        input_tokens="unknown", output_tokens="unknown", error=None)


class HostAdapter:
    """Offline adapter answering every role with a valid packet."""

    runtime_id = IDENTITY[0]
    provider_id = IDENTITY[1]

    def __init__(self):
        self.invocations = 0

    def discover(self):
        from external_runtime import RuntimeDiscovery
        return RuntimeDiscovery(IDENTITY[0], True, "1.0", None, frozenset())

    def check_authentication(self):
        from runtime_health import AuthenticationCheck
        from runtime_status import AuthenticationState
        return AuthenticationCheck(AuthenticationState.AUTHENTICATED, "oauth")

    def check_provider_model(self):
        from runtime_health import ProviderModelCheck
        from runtime_status import ReasonCode
        return ProviderModelCheck(IDENTITY[1], None, True, ReasonCode.NONE)

    def cancel(self, invocation_id):
        return InvocationResult(InvocationStatus.CANCELLED)

    def invoke(self, request):
        self.invocations += 1
        for role, packet in (("architect", ARCH_P), ("coder", IMPL_P),
                             ("tester", TEST_P), ("reviewer", REVIEW_P)):
            if request.agent_id == role or request.agent_id.endswith(f',"{role}"]'):
                return InvocationResult(InvocationStatus.SUCCESS,
                                        output=json.dumps(packet), trace=trace())
        return InvocationResult(InvocationStatus.SUCCESS, output="OK", trace=trace())


def validation_result():
    return CandidateValidationResult(
        identity=IDENTITY, status=CandidateValidationStatus.VERIFIED,
        gates_passed=frozenset(ValidationGate),
        gate_results=tuple(GateResult(g, GateVerdict.PASS) for g in ValidationGate),
        block_reason=None, failure_point=None, experiment_id="host-exp",
        executed_at=0.0, validated_capabilities=CAPS_ALL, evidence={},
        provenance="REAL")


def health():
    return {IDENTITY[0]: RuntimeStatus(
        runtime_id=IDENTITY[0], executable="e", version="1",
        status=RuntimeState.READY, provider=IDENTITY[1], model=None,
        auth_method=None, reason_code=ReasonCode.NONE,
        evidence=HealthEvidence("d", "a", "p", "m", "ok"),
        checked_at=0.0, expires_at=1.0)}


class HostContractTests(unittest.TestCase):
    def test_build_facade_returns_production_facade(self):
        adapter = HostAdapter()
        facade = build_facade(adapter, validation_result(), health())
        self.assertIsInstance(facade, ProductionFacade)

    def test_host_wires_real_verified_orchestrator(self):
        """The SINGLE path must run through a REAL VerifiedOrchestrator,
        not a stub — this was the RC-1 P1-② wiring gap."""
        from verified_orchestrator import VerifiedOrchestrator
        adapter = HostAdapter()
        facade = build_facade(adapter, validation_result(), health())
        self.assertIsInstance(facade._orchestrator._verified_orchestrator,
                              VerifiedOrchestrator)

    def test_host_offline_four_stage_via_on_mode(self):
        adapter = HostAdapter()
        facade = build_facade(adapter, validation_result(), health())
        result = facade.run(task_id="host-1", task="redesign architecture across modules",
                            prompt="redesign architecture across modules", mode=Mode.ON,
                            provenance="OFFLINE")
        self.assertEqual(result.status, "SUCCESS")
        self.assertEqual(result.path, "FOUR_STAGE")
        self.assertEqual(adapter.invocations, 4)  # build only wires; chain = 4

    def test_host_single_path_via_auto_simple(self):
        adapter = HostAdapter()
        facade = build_facade(adapter, validation_result(), health())
        result = facade.run(task_id="host-2", task="fix one simple bug",
                            prompt="fix one simple bug", mode=Mode.AUTO,
                            provenance="OFFLINE")
        self.assertEqual(result.path, "SINGLE")
        self.assertNotEqual(result.path, "FOUR_STAGE")
        self.assertEqual(adapter.invocations, 1)  # single coder invocation

    def test_host_off_mode_runs_nothing(self):
        adapter = HostAdapter()
        facade = build_facade(adapter, validation_result(), health())
        before = adapter.invocations
        result = facade.run(task_id="host-3", task="fix one simple bug",
                            prompt="fix one simple bug", mode=Mode.OFF)
        self.assertEqual(result.path, "OFF")
        self.assertEqual(adapter.invocations, before)

    def test_budget_usage_visible_through_host(self):
        adapter = HostAdapter()
        facade = build_facade(adapter, validation_result(), health())
        self.assertIsInstance(facade._usage, BudgetUsage)
        facade.run(task_id="host-4", task="fix one simple bug",
                   prompt="fix one simple bug", mode=Mode.AUTO)
        self.assertEqual(facade._usage.total_agent_calls, 1)

    def test_facade_result_is_closed_and_secret_free(self):
        adapter = HostAdapter()
        facade = build_facade(adapter, validation_result(), health())
        result = facade.run(task_id="host-5", task="redesign architecture across modules",
                            prompt="redesign architecture across modules", mode=Mode.ON)
        self.assertIsInstance(result, FacadeResult)
        surface = repr(result).lower()
        for marker in SECRET_MARKERS:
            self.assertNotIn(marker, surface)

    def test_host_does_not_reimplement_orchestration(self):
        """The host module must stay a thin composition root."""
        import host as module
        source = Path(module.__file__).read_text(encoding="utf-8")
        for forbidden in ("class .*Orchestrator", "def _select", "def _score",
                          "reserve_call", "loop_guard.check"):
            import re
            self.assertIsNone(re.search(forbidden, source), forbidden)


class StringOutputAdapter(HostAdapter):
    """Mimics the REAL adapter: (a) output is a JSON *string*; (b) routing
    by prompt semantics — bare identity (SINGLE) is a coder invocation;
    (c) PROMPT-SENSITIVE: without a packet-contract instruction in the
    prompt it answers free text (like a real model asked nothing about
    JSON), so the host seam must supply the contract."""

    @staticmethod
    def _ok(output):
        return InvocationResult(InvocationStatus.SUCCESS, output=output, trace=trace())

    def invoke(self, request):
        self.invocations += 1
        if request.prompt.startswith("Return exactly OK"):
            return self._ok("OK")
        has_contract = "Return ONLY a JSON object" in request.prompt
        if request.agent_id == IDENTITY_BARE:
            if not has_contract:
                return self._ok("I would fix the bug by editing the file.")
            return self._ok(json.dumps(IMPL_P))
        for role, packet in (("architect", ARCH_P), ("coder", IMPL_P),
                             ("tester", TEST_P), ("reviewer", REVIEW_P)):
            if request.agent_id == role or request.agent_id.endswith(f',"{role}"]'):
                return self._ok(json.dumps(packet))
        return self._ok(json.dumps(IMPL_P))


class SingleRealFormatTests(unittest.TestCase):
    """RED for RC-2B: the SINGLE executor consumes dict packets while the
    collaboration stack parses JSON text — the host seam must convert the
    real adapter's string output for the SINGLE path only (phase-9 E2E
    precedent: controller-side parsing), without touching the dual path."""

    def test_single_path_succeeds_with_string_output_adapter(self):
        adapter = StringOutputAdapter()
        facade = build_facade(adapter, validation_result(), health())
        result = facade.run(task_id="fmt-1", task="fix one simple bug",
                            prompt="fix one simple bug", mode=Mode.AUTO)
        self.assertEqual(result.path, "SINGLE")
        self.assertEqual(result.status, "SUCCESS", result.failure_category)
        self.assertEqual(adapter.invocations, 1)

    def test_single_prompt_gets_packet_contract_at_host_seam(self):
        """RED for the REAL finding: the single engine forwards the raw
        prompt; without a packet contract the real model answers free text
        and the packet parse fails. The host seam must embed the contract."""
        adapter = StringOutputAdapter()
        facade = build_facade(adapter, validation_result(), health())
        result = facade.run(task_id="fmt-3", task="fix one simple bug",
                            prompt="fix one simple bug", mode=Mode.AUTO)
        self.assertEqual(result.status, "SUCCESS", result.failure_category)

    def test_dual_path_still_receives_wire_text(self):
        adapter = StringOutputAdapter()
        facade = build_facade(adapter, validation_result(), health())
        result = facade.run(task_id="fmt-2",
                            task="redesign architecture across modules",
                            prompt="redesign architecture across modules",
                            mode=Mode.ON)
        self.assertEqual(result.path, "FOUR_STAGE")
        self.assertEqual(result.status, "SUCCESS")


class CliHostIntegrationTests(unittest.TestCase):
    def test_cli_reaches_facade_through_host(self):
        import cli
        adapter = HostAdapter()
        facade = build_facade(adapter, validation_result(), health())
        summary = json.loads(cli.run_cli(facade, ["run", "--mode", "on",
                                                  "redesign architecture across modules"]))
        self.assertEqual(summary["status"], "SUCCESS")
        self.assertEqual(summary["path"], "FOUR_STAGE")

    def test_cli_auto_simple_routes_single(self):
        import cli
        adapter = HostAdapter()
        facade = build_facade(adapter, validation_result(), health())
        summary = json.loads(cli.run_cli(facade, ["run", "fix one simple bug"]))
        self.assertEqual(summary["path"], "SINGLE")
        self.assertEqual(summary["mode"], "AUTO")

    def test_cli_seam_cannot_mislabel_real_evidence(self):
        """Integration defect found by the RC-2 REAL smoke: run_cli never
        forwarded provenance, so real runs through the CLI were labeled
        OFFLINE. The host binds the evidence provenance as the run default."""
        import cli
        adapter = HostAdapter()
        facade = build_facade(adapter, validation_result(), health())
        summary = json.loads(cli.run_cli(facade, ["run", "fix one simple bug"]))
        self.assertEqual(summary["provenance"], "REAL")  # from evidence

    def test_explicit_provenance_override_still_wins(self):
        adapter = HostAdapter()
        facade = build_facade(adapter, validation_result(), health())
        result = facade.run(task_id="host-p", task="fix one simple bug",
                            prompt="fix one simple bug", provenance="OFFLINE")
        self.assertEqual(result.provenance, "OFFLINE")


if __name__ == "__main__":
    unittest.main()
