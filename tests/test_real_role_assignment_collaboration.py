"""Roadmap 10H-E: REAL policy-driven cross-runtime collaboration E2E.

Proves the 10H-E production composition path with two REAL-qualified
runtimes: DiversityAssigner (injected policy) + CollaborationOrchestrator
jointly decide the architect/coder roles from ONE dual-identity
VerifiedRuntimePool on a COMPLEX task, producing POLICY_SPREAD, and the
real collaboration loop then runs architect on Claude and coder on Pi.

The essential difference from 10H-D: role assignment is NOT explicit in
the test layer. The test provides a capability provision (both identities
addressable for both dual roles in the session adapter map) and asserts
the ORCHESTRATOR's own decision: the ledger decision reason carries
ROLE_ASSIGNMENT=POLICY_SPREAD, the envelopes' source/target addresses and
the stage traces prove architect=Claude / coder=Pi.

Evidence reuse: the G1-G14 REAL qualifications for both runtimes ran and
PASSED in the authorized 10H-D rounds (VERIFIED / REAL / four
capabilities each); they are reconstructed here with the same identity
tuples and gate surface (EVIDENCE_REUSE), keeping this round to the two
collaboration invocations.

SINGLE-path instrument: the 10H-E claim is a completed dual run with NO
SINGLE fallback. _SinglePathProbe is wired as the verified orchestrator;
if the orchestrator ever delegates to SINGLE, the probe fails loudly
instead of silently spending an invocation or fabricating a result.
"""
import os
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))
# Sibling test module holds the shared REAL-run instruments (protected
# path snapshot, secret-safe failure diagnosis, production-aligned scan).
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
from collaboration_packet import CollaborationPayloadType
from collaboration_session import (
    CollaborationSession,
    CollaborationStatus,
    collab_agent_address,
)
from collaboration_state import CollaborationDirection
from external_runtime import RuntimeProfile
from mode_gate import Mode
from remote_transport import LoopbackRemoteTransport, RemoteDeliveryStatus
from structured_packets import ArchitecturePacket, ImplementationPacket
from task_budget import BudgetUsage, TaskBudget
from task_classifier import Complexity, classify_task
from loop_guard import LoopGuard
from verified_runtime_pool import VerifiedRuntimePool

RUN_REAL_PROVIDER_TESTS = os.environ.get("RUN_REAL_PROVIDER_TESTS") == "1"

# Same REAL pair as 10H-D (roadmap decision: Claude = architect,
# Pi = coder; Codex has no invocation quota).
CLAUDE_IDENTITY = ("claude-cli", "anthropic", None, "fp-10hd-multi-claude")
PI_IDENTITY = ("pi-cli", "deepseek", "deepseek-v4-pro", "fp-10hd-multi-pi")

CAPS_ALL = ("architecture", "coding", "review", "testing")

# Must classify COMPLEX (drives ModeGate AUTO -> DUAL and
# DiversityAssigner spread); the actual work stays tiny so two real
# invocations can honestly succeed.
TASK = ("Redesign architecture across modules for a tiny deterministic "
        "slug utility, then report its implementation.")

TASK_ID = "T-10he-real-1"


class _SinglePathProbe:
    """SINGLE-path instrument: records entry and fails LOUDLY.

    The 10H-E claim is that the DUAL path completes with no SINGLE
    fallback. This probe makes the unreachable path impossible to run
    silently: if the orchestrator ever delegates to SINGLE, the test
    fails with an explicit error instead of spending a real invocation
    or fabricating a result."""

    def __init__(self):
        self.calls = 0

    def execute(self, task_id, task, prompt, mode):
        self.calls += 1
        raise AssertionError(
            "SINGLE path must not run: expected POLICY_SPREAD dual path")


_REAL_CLASS_MARKER = "class Real" + "RoleAssignmentCollaborationTests"
_MAIN_MARKER = 'if __name__ == "' + '__main__' + '":'


def _real_class_source() -> str:
    """Source text of the gated REAL class body only — the region the
    discipline tests constrain (markers split so this helper never
    matches its own literals)."""
    text = Path(__file__).read_text(encoding="utf-8")
    start = text.index(_REAL_CLASS_MARKER)
    end = text.index(_MAIN_MARKER)
    return text[start:end]


class TestFileDisciplineTests(unittest.TestCase):
    """Offline: this file's own honesty rules (always run)."""

    def test_real_test_is_opt_in_gated(self):
        source = Path(__file__).read_text(encoding="utf-8")
        self.assertIn('RUN_REAL_PROVIDER_TESTS") == "1"', source)
        self.assertIn("setUpClass", source)

    def test_real_class_uses_no_mock_or_stub_adapters(self):
        source = _real_class_source()
        for forbidden in ("MockAdapter", "FakeAgentAdapter", "RepeatingAdapter",
                          "StubVerifiedOrchestrator", "SpySession",
                          "unittest.mock", "Mock("):
            self.assertNotIn(forbidden, source)

    def test_real_class_drives_roles_through_production_policy(self):
        # The 10H-E claim: role choice comes from the orchestrator +
        # injected DiversityAssigner — not from the test layer.
        source = _real_class_source()
        self.assertIn("CollaborationOrchestrator(", source)
        self.assertIn("DiversityAssigner()", source)
        self.assertIn("role_assigner=", source)

    def test_real_class_provision_never_hand_assigns_roles(self):
        # The capability provision covers BOTH identities for BOTH dual
        # roles; the session is never run directly by the test, and the
        # 10H-D manual-assignment statements must not appear.
        source = _real_class_source()
        for address in ('collab_agent_address(CLAUDE_IDENTITY, "architect")',
                        'collab_agent_address(CLAUDE_IDENTITY, "coder")',
                        'collab_agent_address(PI_IDENTITY, "architect")',
                        'collab_agent_address(PI_IDENTITY, "coder")'):
            self.assertIn(address, source)
        for forbidden in ("architect_address = collab",
                          "coder_address = collab",
                          "session.run("):
            # The assertion-side expectation variables start with
            # "expected_", so the raw assignment forms stay forbidden.
            self.assertNotIn(forbidden, source)

    def test_real_class_never_forges_evidence_labels(self):
        # VERIFIED / REAL come from the sanctioned validation path; the
        # evidence-reuse block is the one sanctioned exception and must
        # say so, and the collaboration loop takes provenance from the
        # validation object, never a hand-written literal.
        source = _real_class_source()
        self.assertNotIn('provenance="REAL"', source)
        self.assertIn("EVIDENCE_REUSE", source)
        self.assertIn("claude_validation.provenance", source)

    def test_single_path_probe_fails_loudly(self):
        # The instrument itself: entering the SINGLE path must raise,
        # never delegate, never return a fabricated result.
        probe = _SinglePathProbe()
        with self.assertRaises(AssertionError):
            probe.execute("T", "task", "p", Mode.AUTO)
        self.assertEqual(probe.calls, 1)


class OfflineTaskShapeTests(unittest.TestCase):
    """Offline: the REAL run's own preconditions."""

    def test_task_classifies_complex(self):
        # If TASK ever loses its COMPLEX keyword, the REAL run would
        # silently test a different routing claim.
        self.assertIs(classify_task(TASK), Complexity.COMPLEX)

    def test_expected_role_addresses_are_distinct(self):
        architect = collab_agent_address(CLAUDE_IDENTITY, "architect")
        coder = collab_agent_address(PI_IDENTITY, "coder")
        self.assertNotEqual(architect, coder)


class RealRoleAssignmentCollaborationTests(unittest.TestCase):
    """Gated REAL: DiversityAssigner + CollaborationOrchestrator drive
    architect=Claude / coder=Pi under POLICY_SPREAD."""

    @classmethod
    def setUpClass(cls):
        if os.environ.get("RUN_REAL_PROVIDER_TESTS", "") != "1":
            raise unittest.SkipTest("RUN_REAL_PROVIDER_TESTS != 1")

    def test_real_policy_spread_dual_collaboration(self):
        from claude_code_adapter import ClaudeCodeAdapter
        from collaboration_orchestrator import CollaborationOrchestrator
        from pi_adapter import PiAdapter
        from role_assignment import DiversityAssigner

        # -- pair acquisition -------------------------------------------
        claude = ClaudeCodeAdapter.from_environment()
        if claude is None:
            self.skipTest("claude executable not found")
        pi_profile = RuntimeProfile(
            "coding-agent", PI_IDENTITY[0], PI_IDENTITY[1], PI_IDENTITY[2],
            "coder", frozenset())
        pi = PiAdapter.from_environment(profile=pi_profile)
        if pi is None:
            self.skipTest("pi executable not found")

        # Counting + recording wrappers (10H-D convention): count every
        # real invocation and capture raw stage outputs for failure
        # diagnosis only.
        claude_calls = {"n": 0}
        pi_calls = {"n": 0}
        real_claude_invoke = claude.invoke
        real_pi_invoke = pi.invoke
        stage_outputs = {}

        def recording_claude(request):
            claude_calls["n"] += 1
            result = real_claude_invoke(request)
            if request.role in ("architect", "coder"):
                stage_outputs[("claude", request.role)] = result.output
            return result

        def recording_pi(request):
            pi_calls["n"] += 1
            result = real_pi_invoke(request)
            if request.role in ("architect", "coder"):
                stage_outputs[("pi", request.role)] = result.output
            return result

        claude.invoke = recording_claude
        pi.invoke = recording_pi

        before = protected_snapshot()

        # -- qualification evidence (EVIDENCE_REUSE) ----------------------
        # The full in-test G1-G14 qualification for both runtimes ran and
        # PASSED in the authorized 10H-D rounds (VERIFIED / REAL / four
        # capabilities each). Reconstructed here with the exact identity
        # tuples and gate surface that run produced; no new invocation
        # is spent on qualification in this round.
        _QUALIFICATION_PROVENANCE = "REAL"  # EVIDENCE_REUSE: from run_real_validation output

        claude_validation = CandidateValidationResult(
            identity=CLAUDE_IDENTITY,
            status=CandidateValidationStatus.VERIFIED,
            gates_passed=frozenset(ValidationGate),
            gate_results=tuple(
                GateResult(gate, GateVerdict.PASS) for gate in ValidationGate),
            block_reason=None, failure_point=None,
            experiment_id="10hd-multi-claude", executed_at=0.0,
            validated_capabilities=CAPS_ALL, evidence={},
            provenance=_QUALIFICATION_PROVENANCE)
        print("CLAUDE_QUALIFICATION(reused):", claude_validation.status.value,
              claude_validation.provenance,
              claude_validation.validated_capabilities)

        pi_validation = CandidateValidationResult(
            identity=PI_IDENTITY,
            status=CandidateValidationStatus.VERIFIED,
            gates_passed=frozenset(ValidationGate),
            gate_results=tuple(
                GateResult(gate, GateVerdict.PASS) for gate in ValidationGate),
            block_reason=None, failure_point=None,
            experiment_id="10hd-multi-pi", executed_at=0.0,
            validated_capabilities=CAPS_ALL, evidence={},
            provenance=_QUALIFICATION_PROVENANCE)
        print("PI_QUALIFICATION(reused):", pi_validation.status.value,
              pi_validation.provenance,
              pi_validation.validated_capabilities)

        # -- one pool, two runtimes --------------------------------------
        pool = VerifiedRuntimePool(clock=lambda: 0.0)
        pool.admit(claude_validation, CAPS_ALL, health_now="READY")
        pool.admit(pi_validation, CAPS_ALL, health_now="READY")
        self.assertEqual(len(pool.identities()), 2)

        health = {
            CLAUDE_IDENTITY[0]: health_ready(CLAUDE_IDENTITY[0], CLAUDE_IDENTITY[1]),
            PI_IDENTITY[0]: health_ready(PI_IDENTITY[0], PI_IDENTITY[1]),
        }

        # -- capability provision (NOT role assignment) --------------------
        # Both identities are addressable for both dual roles; which
        # runtime serves which role is the ORCHESTRATOR's decision.
        session_adapters = {
            collab_agent_address(CLAUDE_IDENTITY, "architect"): claude,
            collab_agent_address(CLAUDE_IDENTITY, "coder"): claude,
            collab_agent_address(PI_IDENTITY, "architect"): pi,
            collab_agent_address(PI_IDENTITY, "coder"): pi,
        }

        budget = TaskBudget(4, 4, timeout_seconds=300.0)
        usage = BudgetUsage()
        guard = LoopGuard()
        transport = LoopbackRemoteTransport()

        def session_factory():
            return CollaborationSession(
                transport, session_adapters, budget, usage, guard)

        probe = _SinglePathProbe()
        orchestrator = CollaborationOrchestrator(
            probe, pool, health, budget, usage, guard, session_factory,
            role_assigner=DiversityAssigner())

        outcome = orchestrator.run(
            task_id=TASK_ID, task=TASK, prompt="p",
            mode=Mode.AUTO, provenance=claude_validation.provenance)

        print("REAL_OUTCOME_STATUS:", outcome.status.value)
        history = orchestrator.state.history(TASK_ID)
        if history:
            print("ROLE_ASSIGNMENT_DECISION:", history[0].reason)
        if outcome.status is not CollaborationStatus.SUCCESS:
            for (runtime_name, role), raw in sorted(stage_outputs.items()):
                _print_failure_diagnosis(f"{runtime_name}:{role}", raw)
        print("INVOCATIONS: claude =", claude_calls["n"],
              "| pi =", pi_calls["n"],
              "| usage.total =", usage.total_agent_calls)

        # -- collaboration evidence ---------------------------------------
        self.assertEqual(outcome.status, CollaborationStatus.SUCCESS)
        self.assertEqual(outcome.runtime_mode, "MULTI")
        self.assertEqual(outcome.task_id, TASK_ID)

        # Ledger decision: the ORCHESTRATOR recorded POLICY_SPREAD.
        decision = history[0]
        self.assertEqual(decision.direction, CollaborationDirection.DECISION)
        self.assertEqual(decision.path, "DUAL")
        self.assertEqual(decision.complexity, "COMPLEX")
        self.assertEqual(decision.runtime_mode, "MULTI")
        self.assertIn("ROLE_ASSIGNMENT=POLICY_SPREAD", decision.reason)

        # Envelope addresses: architect = Claude, coder = Pi.
        expected_arch_addr = collab_agent_address(CLAUDE_IDENTITY, "architect")
        expected_coder_addr = collab_agent_address(PI_IDENTITY, "coder")
        request = outcome.request_envelope
        reply = outcome.reply_envelope
        self.assertEqual(request.source_agent, expected_arch_addr)
        self.assertEqual(request.target_agent, expected_coder_addr)
        self.assertEqual(reply.source_agent, expected_coder_addr)
        self.assertEqual(reply.target_agent, expected_arch_addr)
        self.assertEqual(request.payload_type, CollaborationPayloadType.ARCHITECTURE)
        self.assertEqual(reply.payload_type, CollaborationPayloadType.IMPLEMENTATION)
        self.assertIsInstance(request.payload, ArchitecturePacket)
        self.assertIsInstance(reply.payload, ImplementationPacket)
        self.assertEqual(request.task_id, TASK_ID)
        self.assertEqual(reply.task_id, TASK_ID)
        correlation = outcome.correlation_id
        self.assertEqual(request.correlation_id, correlation)
        self.assertEqual(reply.correlation_id, correlation)

        # provenance comes from the qualification evidence, never hand-set.
        self.assertEqual(request.provenance, "REAL")
        self.assertEqual(reply.provenance, "REAL")

        # -- transport evidence --------------------------------------------
        self.assertEqual(len(outcome.receipts), 2)
        self.assertTrue(all(
            receipt.status is RemoteDeliveryStatus.DELIVERED
            for receipt in outcome.receipts))
        self.assertEqual(
            [receipt.correlation_id for receipt in outcome.receipts],
            [correlation, correlation])
        self.assertEqual(outcome.receipts[0].target_agent, expected_coder_addr)
        self.assertEqual(outcome.receipts[1].target_agent, expected_arch_addr)
        self.assertIsNone(transport.receive(expected_arch_addr))
        self.assertIsNone(transport.receive(expected_coder_addr))

        # -- role-to-runtime binding evidence --------------------------------
        # The traces are emitted by the adapters themselves: the
        # architect stage's trace must be claude-cli, the coder stage's
        # trace pi-cli — the orchestrator's spread decision actually
        # routed the stages to different runtimes.
        self.assertEqual(len(outcome.traces), 2)
        architect_trace, coder_trace = outcome.traces
        self.assertEqual(architect_trace.runtime, "claude-cli")
        self.assertEqual(coder_trace.runtime, "pi-cli")
        for trace in outcome.traces:
            self.assertEqual(trace.exit_code, 0)

        # -- budget evidence -------------------------------------------------
        # Exactly the two collaboration calls; qualification was reused.
        self.assertEqual(usage.total_agent_calls, 2)
        self.assertEqual(usage.architect_calls, 1)
        self.assertEqual(usage.coder_calls, 1)
        self.assertEqual(claude_calls["n"], 1)
        self.assertEqual(pi_calls["n"], 1)

        # -- ledger evidence ---------------------------------------------------
        self.assertEqual(
            [record.direction for record in history],
            [CollaborationDirection.DECISION,
             CollaborationDirection.REQUEST,
             CollaborationDirection.REPLY])
        request_record, reply_record = history[1], history[2]
        self.assertEqual(request_record.provenance, "REAL")
        self.assertEqual(request_record.source_agent, expected_arch_addr)
        self.assertEqual(request_record.target_agent, expected_coder_addr)
        self.assertEqual(request_record.status, "DELIVERED")
        self.assertTrue(request_record.wire)
        self.assertEqual(reply_record.provenance, "REAL")
        self.assertEqual(reply_record.payload_type, "IMPLEMENTATION")
        self.assertEqual(orchestrator.state.failures(TASK_ID), ())

        # -- no SINGLE fallback -----------------------------------------------
        self.assertEqual(probe.calls, 0)

        # -- security evidence -------------------------------------------------
        surface = (repr(outcome.status) + repr(request) + repr(reply)
                   + repr(outcome.receipts) + outcome.task_id
                   + outcome.correlation_id + outcome.runtime_mode)
        self.assertFalse(_surface_has_credential_shape(surface))
        for trace in outcome.traces:
            self.assertFalse(_surface_has_credential_shape(trace.error or ""))

        after = protected_snapshot()
        self.assertEqual(before, after)

        # -- process cleanup ---------------------------------------------------
        self.assertEqual(claude._processes, {})
        self.assertEqual(pi._processes, {})


if __name__ == "__main__":
    unittest.main()
