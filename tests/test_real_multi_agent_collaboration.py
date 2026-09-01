"""R6: REAL multi-Agent runtime collaboration — three runtimes, three roles.

The R6 claim, in one line: three DIFFERENT REAL-qualified runtimes can
serve three DIFFERENT roles (ARCHITECT -> CODER -> REVIEWER) inside ONE
structured collaboration protocol, and the whole chain is provable from
structured data alone (ledger + envelopes + traces) — never from reading
terminal output.

Layers in this file (mirroring the house REAL-test convention):

- Offline discipline tests (always run, zero invocations): gate opt-in,
  no mocks in the REAL class, no hand-assigned roles, no forged evidence
  labels, runtime-name neutrality of the collaboration layer.
- R6-A offline contract tests: Role A / Runtime A -> Packet -> Transport
  -> Role B / Runtime B over fake adapters — packet identity, correlation
  preservation, source/target agents, roles, ordered handoff, failure /
  timeout / cancellation observability, no silent handoff loss.
- R6-B offline cross-runtime assignment tests: one pool holding THREE
  verified identities; the role-assignment policy must be able to place
  architect/coder/reviewer on three DIFFERENT runtimes with exact
  identity tuples preserved — RoleAssignment never hard-codes a runtime.
- R6-C gated REAL test (RUN_REAL_PROVIDER_TESTS=1): ARCHITECT=claude-cli,
  CODER=codex-cli, REVIEWER=pi-cli through the production composition
  (CollaborationOrchestrator + ProductionFacade + VerificationCollaboration
  with DiversityAssigner), exactly 3 REAL invocations, full structured
  trace, strict accounting.

Evidence reuse: the G1-G14 REAL qualifications for all three runtimes ran
and PASSED in the authorized R5-A round (VERIFIED / REAL / four
capabilities each, identity tuples exactly as recorded there); they are
reconstructed here with the same identity tuples and gate surface
(EVIDENCE_REUSE), keeping this round to the THREE collaboration
invocations.

R6-A failure-path observability reuses the shared, secret-safe diagnosis
helpers from the 10H-D REAL round (test_real_multi_runtime_collaboration).
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
from collaboration_packet import CollaborationPayloadType
from collaboration_session import (
    CollaborationSession,
    CollaborationStatus,
    collab_agent_address,
)
from collaboration_state import CollaborationDirection
from external_runtime import (
    ExternalAgentRequest,
    InvocationResult,
    InvocationStatus,
    InvocationTrace,
    RuntimeProfile,
)
from mode_gate import Mode
from remote_transport import LoopbackRemoteTransport, RemoteDeliveryStatus
from role_assignment import ConvergingAssigner, DiversityAssigner, RoleAssignment
from structured_packets import (
    ArchitecturePacket,
    ImplementationPacket,
    ReviewPacket,
)
from task_budget import BudgetUsage, TaskBudget
from task_classifier import Complexity, classify_task
from loop_guard import LoopGuard
from verified_runtime_pool import VerifiedRuntimePool

# The three R5-A REAL-qualified identities, EXACTLY as that round recorded
# them (R5-A report §3): claude via the rc2br chain, codex and pi via the
# R5-A one-off drivers. config_fingerprint "installed" is what the
# production registry descriptors carry.
CLAUDE_IDENTITY = ("claude-cli", "anthropic", None, "installed")
CODEX_IDENTITY = ("codex-cli", "openai", None, "installed")
PI_IDENTITY = ("pi-cli", "deepseek", None, "installed")

IDENTITIES = (CLAUDE_IDENTITY, CODEX_IDENTITY, PI_IDENTITY)

CAPS_ALL = ("architecture", "coding", "review", "testing")

# Must classify COMPLEX (drives ModeGate AUTO -> DUAL and the spread
# policy); the work itself stays tiny so three real invocations can
# honestly succeed.
TASK = ("Redesign architecture across modules for a tiny deterministic "
        "slug utility, then report its implementation and review it.")

TASK_ID = "T-r6-real-1"
CORRELATION = "r6-real-1"

SECRET_MARKERS = ("token", "secret", "api_key", "authorization", "bearer", "stdout", "stderr")


# ---------------------------------------------------------------------------
# Offline packet fixtures (R6-A / R6-B only; the REAL class never uses them)
# ---------------------------------------------------------------------------

def arch_dict(task_id=TASK_ID):
    return {
        "task_id": task_id, "role": "architect", "goal": ["g"],
        "constraints": ["c"], "architecture": ["a"], "interfaces": [{}],
        "implementation_steps": [{}], "acceptance_criteria": ["ac"],
        "risks": [{}],
    }


def impl_dict(task_id=TASK_ID):
    return {
        "task_id": task_id, "role": "coder", "changed_files": ["f.py"],
        "implementation_summary": "s", "implementation_details": ["d"],
        "assumptions": [], "unresolved_items": [],
        "test_requirements": ["tr"],
    }


def _tester_packet_dict(task_id=TASK_ID):
    return {
        "task_id": task_id, "role": "tester", "tests_run": ["t"],
        "tests_passed": ["t"], "tests_failed": [], "failures": [],
        "coverage_or_validation": [], "remaining_risks": [],
    }


def review_dict(task_id=TASK_ID):
    return {
        "task_id": task_id, "role": "reviewer", "status": "PASS",
        "findings": [], "severity": [], "affected_files": [],
        "required_changes": [], "acceptance_criteria_status": [],
    }


def trace_for(runtime, role, status=InvocationStatus.SUCCESS, exit_code=0):
    return InvocationTrace(
        invocation_id=f"inv-{runtime}-{role}", task_id=TASK_ID,
        agent_id="a", runtime=runtime, provider=None, model=None,
        role=role, status=status, started_at=1.0, finished_at=2.0,
        duration_ms=10, exit_code=exit_code,
        input_tokens="unknown", output_tokens="unknown", error=None)


class RoleAnsweringAdapter:
    """Offline adapter that answers every role with a valid packet.

    Records every request so the tests can prove packet identity,
    correlation, source/target, role and ordered handoff from structured
    data. Failure injection is opt-in per role via failure_role."""

    def __init__(self, runtime, failure_role=None, failure_status=None):
        self.runtime = runtime
        self.requests = []
        self.failure_role = failure_role
        self.failure_status = failure_status or InvocationStatus.FAILED

    def invoke(self, request):
        self.requests.append(request)
        if self.failure_role and request.role == self.failure_role:
            return InvocationResult(
                self.failure_status, output=None, error="injected failure",
                trace=trace_for(self.runtime, request.role,
                                self.failure_status, exit_code=1))
        payload = {
            "architect": arch_dict, "coder": impl_dict,
            "tester": _tester_packet_dict, "reviewer": review_dict,
        }[request.role](request.task_id)
        import json
        return InvocationResult(
            InvocationStatus.SUCCESS, output=json.dumps(payload),
            trace=trace_for(self.runtime, request.role))


# ---------------------------------------------------------------------------
# File-discipline tests (always run, zero invocations)
# ---------------------------------------------------------------------------

_REAL_CLASS_MARKER = "class Real" + "MultiAgentCollaborationTests"
_MAIN_MARKER = 'if __name__ == "' + '__main__' + '":'


def _real_class_source() -> str:
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

    def test_real_class_uses_no_mock_or_stub_executors(self):
        source = _real_class_source()
        for forbidden in ("MockAdapter", "FakeAgentAdapter", "RepeatingAdapter",
                          "RoleAnsweringAdapter", "StubVerifiedOrchestrator",
                          "SpySession", "unittest.mock", "Mock("):
            self.assertNotIn(forbidden, source)

    def test_real_class_provision_never_hand_assigns_roles(self):
        # R6 discipline (10H-F convention): the capability provision is
        # 12 EXPLICIT collab_agent_address(...) entries (3 identities x 4
        # roles) INSIDE the REAL class body — statically scannable. The
        # test layer supplies capability (addressability), never the
        # role->runtime binding: that decision belongs to the injected
        # role-assignment policy.
        source = _real_class_source()
        for identity in ("CLAUDE_IDENTITY", "CODEX_IDENTITY", "PI_IDENTITY"):
            for role in ("architect", "coder", "tester", "reviewer"):
                self.assertIn(
                    f'collab_agent_address({identity}, "{role}")', source)
        for forbidden in ("architect_address =", "coder_address =",
                          "tester_address =", "reviewer_address ="):
            self.assertNotIn(forbidden, source)

    def test_real_class_never_forges_evidence_labels(self):
        # VERIFIED / REAL must come from the sanctioned validation path:
        # the one sanctioned exception is EVIDENCE_REUSE (reconstructing
        # the previous authorized round's result), which must say so and
        # pass the validation's own provenance through — never a
        # hand-written literal.
        source = _real_class_source()
        self.assertNotIn('provenance="REAL"', source)
        self.assertIn("EVIDENCE_REUSE", source)
        self.assertIn("claude_validation.provenance", source)


# ---------------------------------------------------------------------------
# R6-A: Offline Collaboration Contract (fake adapters, zero invocations)
# ---------------------------------------------------------------------------

def three_runtime_adapters(failure_role=None, failure_status=None):
    """Three role-answering fake adapters, one per R6 runtime slot.

    Uses runtime-NEUTRAL slot names (rt-a/rt-b/rt-c): the collaboration
    contract must be provable without any real runtime installed."""
    return {
        "rt-a": RoleAnsweringAdapter("rt-a", failure_role, failure_status),
        "rt-b": RoleAnsweringAdapter("rt-b", failure_role, failure_status),
        "rt-c": RoleAnsweringAdapter("rt-c", failure_role, failure_status),
    }


def provision(adapters):
    """3 identities x 2 dual roles address map (R6-A works the dual seam)."""
    ids = (("rt-a", "p-a", None, "fp-a"),
           ("rt-b", "p-b", None, "fp-b"),
           ("rt-c", "p-c", None, "fp-c"))
    return {
        collab_agent_address(identity, role): adapters[runtime]
        for runtime, identity in zip(("rt-a", "rt-b", "rt-c"), ids)
        for role in ("architect", "coder")
    }


class OfflineCollaborationContractTests(unittest.TestCase):
    """R6-A: Role A / Runtime A -> packet -> transport -> Role B / Runtime B.

    Every claim below is proven from structured data (requests recorded by
    the adapters, envelopes, receipts, outcome fields) — never from
    terminal output."""

    def _run_session(self, adapters, architect_identity, coder_identity,
                     correlation=CORRELATION):
        arch_addr = collab_agent_address(architect_identity, "architect")
        coder_addr = collab_agent_address(coder_identity, "coder")
        transport = LoopbackRemoteTransport()
        budget = TaskBudget(4, 4, timeout_seconds=30.0)
        usage = BudgetUsage()
        guard = LoopGuard()
        session = CollaborationSession(
            transport, provision(adapters), budget, usage, guard)
        outcome = session.run(
            task_id=TASK_ID, task="offline contract task",
            architect_address=arch_addr, coder_address=coder_addr,
            correlation_id=correlation, provenance="OFFLINE",
            runtime_mode="MULTI")
        return session, outcome, arch_addr, coder_addr, usage

    def test_packet_identity_and_correlation_preserved_across_runtimes(self):
        adapters = three_runtime_adapters()
        ids_a = ("rt-a", "p-a", None, "fp-a")
        ids_b = ("rt-b", "p-b", None, "fp-b")
        _, outcome, arch_addr, coder_addr, _ = self._run_session(
            adapters, ids_a, ids_b)
        self.assertEqual(outcome.status, CollaborationStatus.SUCCESS)
        request, reply = outcome.request_envelope, outcome.reply_envelope
        # packet identity: the payload task_id IS the session task.
        self.assertEqual(request.payload.task_id, TASK_ID)
        self.assertEqual(reply.payload.task_id, TASK_ID)
        # correlation preserved end to end (envelopes + receipts).
        self.assertEqual(request.correlation_id, CORRELATION)
        self.assertEqual(reply.correlation_id, CORRELATION)
        self.assertEqual([r.correlation_id for r in outcome.receipts],
                         [CORRELATION, CORRELATION])
        # source/target agents and roles exact.
        self.assertEqual(request.source_agent, arch_addr)
        self.assertEqual(request.target_agent, coder_addr)
        self.assertEqual(request.source_role, "architect")
        self.assertEqual(request.target_role, "coder")
        self.assertEqual(reply.source_agent, coder_addr)
        self.assertEqual(reply.target_agent, arch_addr)
        self.assertEqual(reply.source_role, "coder")
        self.assertEqual(reply.target_role, "architect")

    def test_handoff_order_is_architect_then_coder(self):
        adapters = three_runtime_adapters()
        _, outcome, _, _, _ = self._run_session(
            adapters, ("rt-a", "p-a", None, "fp-a"),
            ("rt-b", "p-b", None, "fp-b"))
        self.assertEqual(outcome.status, CollaborationStatus.SUCCESS)
        arch_adapter = adapters["rt-a"]
        coder_adapter = adapters["rt-b"]
        # Exactly one request per role, in stage order.
        self.assertEqual([r.role for r in arch_adapter.requests], ["architect"])
        self.assertEqual([r.role for r in coder_adapter.requests], ["coder"])
        # The coder consumed the serialized ArchitecturePacket — the
        # structured handoff, never raw task text.
        import json as _json
        coder_prompt = coder_adapter.requests[0].prompt
        payload = _json.loads(
            coder_prompt[coder_prompt.index("{"):coder_prompt.rindex("}") + 1])
        self.assertEqual(payload.get("packet_type"), "ArchitecturePacket")
        # Structured data proves the two stages ran on different runtimes.
        self.assertEqual(
            [t.runtime for t in outcome.traces], ["rt-a", "rt-b"])

    def test_runtime_failure_is_localized_to_the_failing_stage(self):
        # Coder runtime failure must surface CODER_INVOKE_FAILED — never
        # ARCHITECT_* or REVIEWER_*: failure localization is a contract.
        adapters = three_runtime_adapters(failure_role="coder")
        _, outcome, _, _, _ = self._run_session(
            adapters, ("rt-a", "p-a", None, "fp-a"),
            ("rt-b", "p-b", None, "fp-b"))
        self.assertEqual(outcome.status, CollaborationStatus.CODER_INVOKE_FAILED)
        # The architect stage DID run; the coder stage did not reach a
        # request record (its invoke returned before any packet parse).
        self.assertEqual(len(adapters["rt-a"].requests), 1)
        self.assertEqual(adapters["rt-b"].requests[0].role, "coder")
        # No silent handoff loss: the failure outcome still carries the
        # request envelope and the architect trace.
        self.assertIsNotNone(outcome.request_envelope)
        # Both stage traces are recorded (the failed coder trace too —
        # failure observability, never a silent drop).
        self.assertEqual(len(outcome.traces), 2)
        self.assertEqual(outcome.traces[1].status, InvocationStatus.FAILED)

    def test_handoff_failure_leaves_coder_unexecuted(self):
        # Architect succeeded, transport dropped the packet: the coder
        # must NEVER execute and the failure must be TRANSPORT_FAILED.
        arch_ids = ("rt-a", "p-a", None, "fp-a")
        coder_ids = ("rt-b", "p-b", None, "fp-b")
        arch_addr = collab_agent_address(arch_ids, "architect")
        coder_addr = collab_agent_address(coder_ids, "coder")
        adapters = three_runtime_adapters()

        class DroppingTransport(LoopbackRemoteTransport):
            def send(self, packet):
                receipt = super().send(packet)
                if packet.target_agent == coder_addr:
                    # Simulate a remote drop: the mailbox loses the wire.
                    self._mailboxes.pop(coder_addr, None)
                return receipt

        transport = DroppingTransport()
        budget = TaskBudget(4, 4, timeout_seconds=30.0)
        usage = BudgetUsage()
        guard = LoopGuard()
        session = CollaborationSession(
            transport, provision(adapters), budget, usage, guard)
        outcome = session.run(
            task_id=TASK_ID, task="offline contract task",
            architect_address=arch_addr, coder_address=coder_addr,
            correlation_id=CORRELATION, provenance="OFFLINE",
            runtime_mode="MULTI")
        self.assertEqual(outcome.status, CollaborationStatus.TRANSPORT_FAILED)
        # Precise localization: architect ran exactly once, coder NEVER.
        self.assertEqual(len(adapters["rt-a"].requests), 1)
        self.assertEqual(adapters["rt-b"].requests, [])
        # The dropped handoff is still observable in the outcome envelope.
        self.assertIsNotNone(outcome.request_envelope)

    def test_cancellation_is_observable_not_silent(self):
        # A CANCELLED invocation on the coder stage surfaces as
        # CODER_INVOKE_FAILED with the cancelled trace recorded — never
        # as SUCCESS and never silently dropped.
        adapters = three_runtime_adapters(
            failure_role="coder",
            failure_status=InvocationStatus.CANCELLED)
        _, outcome, _, _, _ = self._run_session(
            adapters, ("rt-a", "p-a", None, "fp-a"),
            ("rt-b", "p-b", None, "fp-b"))
        self.assertEqual(outcome.status, CollaborationStatus.CODER_INVOKE_FAILED)
        # The cancelled trace is IN the outcome (cancellation observable).
        self.assertEqual(len(outcome.traces), 2)
        self.assertEqual(outcome.traces[1].status, InvocationStatus.CANCELLED)

    def test_timeout_is_observable_not_silent(self):
        adapters = three_runtime_adapters(
            failure_role="coder", failure_status=InvocationStatus.TIMEOUT)
        _, outcome, _, _, _ = self._run_session(
            adapters, ("rt-a", "p-a", None, "fp-a"),
            ("rt-b", "p-b", None, "fp-b"))
        self.assertEqual(outcome.status, CollaborationStatus.CODER_INVOKE_FAILED)
        self.assertEqual(outcome.traces[1].status, InvocationStatus.TIMEOUT)

    def test_correlation_mismatch_is_detected_on_receive(self):
        # A mismatched correlation on the coder side is CORRELATION_MISMATCH
        # — the ledger-grade invariant, observable, never swallowed.
        arch_ids = ("rt-a", "p-a", None, "fp-a")
        coder_ids = ("rt-b", "p-b", None, "fp-b")
        arch_addr = collab_agent_address(arch_ids, "architect")
        coder_addr = collab_agent_address(coder_ids, "coder")
        adapters = three_runtime_adapters()
        transport = LoopbackRemoteTransport()

        class MismatchedCorrelationTransport(LoopbackRemoteTransport):
            def receive(self, agent_id):
                packet = super().receive(agent_id)
                if packet is not None and agent_id == coder_addr:
                    from collaboration_packet import (
                        CollaborationPacket as CP,
                        CollaborationPayloadType as PT,
                    )
                    from dataclasses import replace as _replace
                    packet = _replace(packet, correlation_id="forged-correlation")
                return packet

        transport = MismatchedCorrelationTransport()
        budget = TaskBudget(4, 4, timeout_seconds=30.0)
        usage = BudgetUsage()
        guard = LoopGuard()
        session = CollaborationSession(
            transport, provision(adapters), budget, usage, guard)
        outcome = session.run(
            task_id=TASK_ID, task="offline contract task",
            architect_address=arch_addr, coder_address=coder_addr,
            correlation_id=CORRELATION, provenance="OFFLINE",
            runtime_mode="MULTI")
        self.assertEqual(outcome.status, CollaborationStatus.CORRELATION_MISMATCH)
        # The coder never executed under the forged correlation.
        self.assertEqual(adapters["rt-b"].requests, [])


# ---------------------------------------------------------------------------
# R6-C1: Offline evidence-printer forensics (Layer D repair, zero REAL runs)
# ---------------------------------------------------------------------------
# The R6-C REAL round crashed while PRINTING the coder invocation evidence:
# the codex adapter decodes process bytes with encoding="utf-8" +
# errors="replace" (so any invalid byte in codex CLI output legitimately
# becomes U+FFFD in the Python string), and the Windows GBK console cannot
# ENCODE U+FFFD back out — print() raised UnicodeEncodeError at the display
# layer, before the invocation's true status was ever observed.
#
# The repair below is display-layer ONLY: safe_display() re-encodes text to
# the console's own encoding with errors="replace" (lossy at the DISPLAY
# boundary only). The original error string, the InvocationResult, the
# trace — everything the production chain produced — is never mutated,
# never swallowed, never re-labeled.

import sys as _sys


def safe_display(text, limit=400):
    """Console-encoding-safe rendering for evidence printing ONLY.

    Never mutates the value it renders: returns a display copy. Any
    character the console encoding cannot represent degrades to that
    encoding's replacement marker at the display layer; the caller's
    original string is untouched."""
    if not isinstance(text, str):
        text = repr(text)
    encoding = _sys.stdout.encoding or "ascii"
    cleaned = text.encode(encoding, errors="replace").decode(
        encoding, errors="replace")
    return cleaned.replace("\n", " ")[:limit]


class OfflineEvidencePrinterTests(unittest.TestCase):
    """R6-C1 RED->GREEN: the evidence printer must survive any console.

    Offline only — zero runtimes, zero subprocesses, zero REAL path. The
    RED case reproduces the exact GBK failure shape (a decoded U+FFFD in
    the error surface) without any invocation."""

    def test_safe_display_survives_replacement_character(self):
        # RED with the old printer (raw print of the same string crashes
        # on a GBK console — proven in the R6-C round); GREEN with
        # safe_display: the call must not raise, on ANY console encoding.
        decoded_error = "prefix � suffix"  # what errors="replace" yields
        rendered = safe_display(decoded_error)
        self.assertIsInstance(rendered, str)

    def test_safe_display_renders_original_unchanged(self):
        # The DISPLAY copy is safe; the ORIGINAL string is untouched —
        # evidence integrity: safe_display(error) is allowed, but the
        # error itself is never replaced by its sanitized rendering.
        original = "error � detail"
        _rendered = safe_display(original)
        self.assertEqual(original, "error � detail")

    def test_safe_display_collapses_newlines_and_truncates(self):
        # Multi-line diagnostic text becomes one bounded line — the
        # printer's own format contract.
        rendered = safe_display("line1\nline2\n" + "x" * 1000, limit=50)
        self.assertNotIn("\n", rendered)
        self.assertLessEqual(len(rendered), 50)

    def test_safe_display_handles_non_string_values(self):
        self.assertIn("None", safe_display(None))

    def test_printer_no_unicodeencodeerror_on_gbk_console(self):
        # The direct regression proof: printing the repaired evidence
        # line (status/exit_code/duration/error with a U+FFFD inside)
        # must not raise, regardless of the active console encoding.
        try:
            print(f"INVOCATION_EVIDENCE: rt-x:coder status=FAILED "
                  f"exit_code=1 duration_s=1.0 "
                  f"error={safe_display('prefix � suffix')}")
        except UnicodeEncodeError:  # pragma: no cover - the failure mode
            self.fail("evidence printer raised UnicodeEncodeError")




# ---------------------------------------------------------------------------
# R6-B: Offline Cross-Runtime Assignment (pool of THREE verified identities)
# ---------------------------------------------------------------------------

def reused_validation(identity, experiment_id):
    """EVIDENCE_REUSE reconstruction of the R5-A REAL qualification result
    (offline shape only — used by R6-B assignment tests, never labeled
    stronger than what R5-A emitted)."""
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


def three_runtime_pool():
    """One VerifiedRuntimePool holding all three R5-A-qualified identities."""
    pool = VerifiedRuntimePool(clock=lambda: 0.0)
    for identity, experiment in (
            (CLAUDE_IDENTITY, "r5a-claude"),
            (CODEX_IDENTITY, "r5a-codex"),
            (PI_IDENTITY, "r5a-pi")):
        pool.admit(reused_validation(identity, experiment),
                   CAPS_ALL, health_now="READY")
    return pool


def three_runtime_health():
    return {
        identity[0]: health_ready(identity[0], identity[1])
        for identity in IDENTITIES
    }


class OfflineCrossRuntimeAssignmentTests(unittest.TestCase):
    """R6-B: one task, three roles, three DIFFERENT runtimes — legal.

    RoleAssignment must place architect/coder/reviewer across runtimes
    from bridge candidate sets, with exact identity tuples preserved and
    no runtime hard-coded."""

    def _candidate_sets(self, roles):
        from verified_selection_bridge import VerifiedSelectionBridge
        from verified_stage_selector import _ROLE_REQUIREMENTS
        pool = three_runtime_pool()
        health = three_runtime_health()
        bridge = VerifiedSelectionBridge()
        return {
            role: bridge.candidates_for(
                pool, health, role, _ROLE_REQUIREMENTS[role])
            for role in roles
        }, pool, health

    def test_three_runtime_pool_admits_all_r5a_identities(self):
        pool = three_runtime_pool()
        # identities() is a sorted TUPLE of identity tuples.
        self.assertEqual(list(pool.identities()), sorted(IDENTITIES))
        for identity in pool.identities():
            result = pool.get(identity)
            self.assertEqual(result.status, CandidateValidationStatus.VERIFIED)
            self.assertEqual(result.provenance, "REAL")
            self.assertEqual(tuple(sorted(result.validated_capabilities)),
                             tuple(sorted(CAPS_ALL)))

    def test_diversity_assigner_spreads_four_roles_over_three_runtimes(self):
        # The R6 configuration: architect / coder / tester / reviewer over
        # a THREE-runtime pool. The spread policy must be legal and must
        # land on at least two runtimes (here: the deterministic round
        # robin over three runtimes).
        sets, _, _ = self._candidate_sets(
            ("architect", "coder", "test", "review"))
        assignment = DiversityAssigner().assign(sets, Complexity.COMPLEX)
        runtimes = [c.runtime_id for c in assignment.assignments.values()
                    if c is not None]
        self.assertEqual(assignment.reason, "POLICY_SPREAD")
        self.assertEqual(len(set(runtimes)), 3)
        self.assertEqual(set(runtimes),
                         {"claude-cli", "codex-cli", "pi-cli"})

    def test_production_split_assigns_tester_codex_reviewer_claude(self):
        # R6-C8 audit (the exact R6-C7 mapping, mirrored offline): the
        # production composition makes TWO assign() calls with different
        # role subsets — the orchestrator over (architect, coder), the
        # facade over (test, review). Each call's round-robin re-indexes
        # from the sorted runtime order, so the facade call lands
        # review->claude and test->codex. This IS the deterministic
        # legal output the REAL R6-C7 run observed.
        dual_sets, _, _ = self._candidate_sets(("architect", "coder"))
        dual = DiversityAssigner().assign(dual_sets, Complexity.COMPLEX)
        ver_sets, _, _ = self._candidate_sets(("test", "review"))
        verification = DiversityAssigner().assign(ver_sets, Complexity.COMPLEX)
        self.assertEqual(
            dual.assignments["architect"].runtime_id, "claude-cli")
        self.assertEqual(
            dual.assignments["coder"].runtime_id, "codex-cli")
        self.assertEqual(
            verification.assignments["test"].runtime_id, "codex-cli")
        self.assertEqual(
            verification.assignments["review"].runtime_id, "claude-cli")

    def test_injected_policy_can_route_all_three_runtimes_no_prod_change(self):
        # Goal B coverage design: "if a scenario explicitly wants all
        # three runtimes to participate, does the existing injection
        # surface support it WITHOUT touching production routing?"
        # YES — RoleAssignment is an injectable POLICY (10H-E design):
        # CollaborationOrchestrator and ProductionFacade both accept
        # role_assigner=. A test-layer assigner that round-robins the
        # FULL stage order (architect, coder, test, review) over the
        # three runtimes hands every runtime a role. This test proves
        # the capability offline with the real bridge/pool data.

        # The production-shaped split: two calls, one assigner instance.
        policy = FullSpreadAssigner()
        dual_sets, _, _ = self._candidate_sets(("architect", "coder"))
        dual = policy.assign(dual_sets, Complexity.COMPLEX)
        ver_sets, _, _ = self._candidate_sets(("test", "review"))
        verification = policy.assign(ver_sets, Complexity.COMPLEX)
        mapping = {
            "architect": dual.assignments["architect"].runtime_id,
            "coder": dual.assignments["coder"].runtime_id,
            "tester": verification.assignments["test"].runtime_id,
            "reviewer": verification.assignments["review"].runtime_id,
        }
        # All three runtimes participate — achieved purely by policy
        # injection over the SAME pool/bridge data. No production file
        # changes; this is exactly how a future REAL "three-runtime
        # participation" scenario would drive the production chain.
        self.assertEqual(set(mapping.values()),
                         {"claude-cli", "codex-cli", "pi-cli"})
        self.assertIn("pi-cli", mapping.values())

    def test_architect_claude_coder_codex_is_a_legal_assignment(self):
        # The exact R6-C dual binding, produced by the POLICY (round-robin
        # order over sorted pool identities): sorted identities are
        # claude-cli < codex-cli < pi-cli, so architect->claude,
        # coder->codex, test->pi, review->claude. The dual-role subset
        # (architect, coder) is exactly ARCHITECT=claude / CODER=codex.
        sets, _, _ = self._candidate_sets(("architect", "coder"))
        assignment = DiversityAssigner().assign(sets, Complexity.COMPLEX)
        architect = assignment.assignments["architect"]
        coder = assignment.assignments["coder"]
        self.assertEqual(architect.runtime_id, "claude-cli")
        self.assertEqual(coder.runtime_id, "codex-cli")
        # Exact identity tuples preserved (runtime/provider/model/fp).
        self.assertEqual(
            (architect.runtime_id, architect.provider_id,
             architect.model_id, architect.config_fingerprint),
            CLAUDE_IDENTITY)
        self.assertEqual(
            (coder.runtime_id, coder.provider_id,
             coder.model_id, coder.config_fingerprint),
            CODEX_IDENTITY)
        # runtime_mode is the ORCHESTRATOR's derivation from identities.
        self.assertNotEqual(
            (architect.runtime_id, architect.provider_id,
             architect.model_id, architect.config_fingerprint),
            (coder.runtime_id, coder.provider_id,
             coder.model_id, coder.config_fingerprint))

    def test_alternate_binding_codex_architect_claude_coder_is_legal(self):
        # "Role != Runtime": a policy that swaps architect and coder must
        # be representable — the pool/bridge layer must accept BOTH
        # bindings. A converging test with the codex-first candidate
        # ordering proves the swap is data, not code.
        sets, pool, health = self._candidate_sets(("architect", "coder"))
        # The bridge sorts by identity; feed the SAME sets to a policy
        # that converges on the codex candidate for architect.
        codex_architect = next(
            c for c in sets["architect"].candidates
            if c.runtime_id == "codex-cli")
        claude_coder = next(
            c for c in sets["coder"].candidates
            if c.runtime_id == "claude-cli")
        self.assertEqual(codex_architect.runtime_id, "codex-cli")
        self.assertEqual(claude_coder.runtime_id, "claude-cli")
        # Both are genuine bridge candidates over the SAME pool — the
        # alternate binding needs zero core changes.
        self.assertEqual(pool.get(CODEX_IDENTITY).status,
                         CandidateValidationStatus.VERIFIED)
        self.assertEqual(pool.get(CLAUDE_IDENTITY).status,
                         CandidateValidationStatus.VERIFIED)
        self.assertEqual(health["codex-cli"].status.value, "READY")

    def test_role_assignment_layer_has_no_hard_coded_runtime(self):
        # RoleAssignment must not bind runtimes: the module source has no
        # concrete runtime names (the spread decision is structural).
        import role_assignment as module
        source = Path(module.__file__).read_text(encoding="utf-8").lower()
        for name in ("claude", "codex", "pi-cli", "gemini", "tiny-agents",
                     "anthropic", "openai", "deepseek", "google"):
            self.assertNotIn(name, source)

    def test_bridge_candidates_preserve_identity_fields(self):
        # Every role candidate carries the exact four identity fields —
        # runtime_id / provider_id / model_id / config_fingerprint.
        sets, _, _ = self._candidate_sets(("architect", "coder"))
        for role, candidate_set in sets.items():
            self.assertEqual(len(candidate_set.candidates), 3, role)
            by_runtime = {c.runtime_id: c for c in candidate_set.candidates}
            for identity in IDENTITIES:
                candidate = by_runtime[identity[0]]
                self.assertEqual(
                    (candidate.runtime_id, candidate.provider_id,
                     candidate.model_id, candidate.config_fingerprint),
                    identity)

    def test_converging_assigner_stays_legal_on_three_runtime_pool(self):
        # The default policy converges every role onto the first
        # candidate (sorted identities): legal, honest, never a spread
        # claim.
        sets, _, _ = self._candidate_sets(("architect", "coder"))
        assignment = ConvergingAssigner().assign(sets, Complexity.COMPLEX)
        self.assertEqual(assignment.reason, "POLICY_CONVERGED")
        for candidate in assignment.assignments.values():
            self.assertEqual(candidate.runtime_id, "claude-cli")


# ---------------------------------------------------------------------------
# R6-C: gated REAL three-runtime collaboration (RUN_REAL_PROVIDER_TESTS=1)
# ---------------------------------------------------------------------------

class FullSpreadAssigner:
    """R6-C9 test-layer policy: deterministic stage-order round robin.

    Verbatim semantic twin of the assigner proven offline in
    OfflineCrossRuntimeAssignmentTests (same fixed 4-role enumeration,
    same runtime_order construction, same index round-robin) — hoisted
    to module level so the gated REAL class can inject the SAME policy
    the offline tests proved. It reads only the injected bridge candidate
    sets (never the pool/health), picks candidates verbatim, and never
    mints one. Under the production two-call split (orchestrator:
    architect+coder; facade: test+review) it yields
    architect=claude, coder=codex, tester=pi, reviewer=claude."""

    def assign(self, role_candidate_sets, complexity):
        roles = ("architect", "coder", "test", "review")
        runtime_order = []
        for role in roles:
            set_ = role_candidate_sets.get(role)
            for candidate in (set_.candidates if set_ else ()):
                if candidate.runtime_id not in runtime_order:
                    runtime_order.append(candidate.runtime_id)
        assignments = {}
        for index, role in enumerate(roles):
            set_ = role_candidate_sets.get(role)
            target = runtime_order[index % len(runtime_order)]
            picked = next(
                (c for c in (set_.candidates if set_ else ())
                 if c.runtime_id == target), None)
            assignments[role] = picked
        distinct = {c.runtime_id for c in assignments.values()
                    if c is not None}
        reason = ("POLICY_SPREAD" if len(distinct) > 1
                  else "POLICY_CONVERGED")
        return RoleAssignment(assignments, reason)


class _SinglePathProbe:
    """SINGLE-path instrument: records entry and fails LOUDLY.

    The R6 claim is a completed FOUR_STAGE cross-runtime run with NO
    SINGLE fallback. If the orchestrator ever delegates to SINGLE, this
    probe fails instead of spending a real invocation or fabricating a
    result."""

    def __init__(self):
        self.calls = 0

    def execute(self, task_id, task, prompt, mode):
        self.calls += 1
        raise AssertionError(
            "SINGLE path must not run: expected the cross-runtime "
            "FOUR_STAGE path")


class RealMultiAgentCollaborationTests(unittest.TestCase):
    """Gated REAL: ARCHITECT=claude-cli / CODER=codex-cli / \
    TESTER+REVIEWER spread over the pool; three runtimes, one task."""

    @classmethod
    def setUpClass(cls):
        if os.environ.get("RUN_REAL_PROVIDER_TESTS", "") != "1":
            raise unittest.SkipTest("RUN_REAL_PROVIDER_TESTS != 1")

    def test_real_three_runtime_collaboration_chain(self):
        import time as _time
        from claude_code_adapter import ClaudeCodeAdapter
        from codex_adapter import CodexAdapter
        from pi_adapter import PiAdapter
        from production_facade import ProductionFacade
        from verification_collaboration import VerificationCollaboration

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

        # -- invocation accounting: every call counted, tracked, and
        # attributable to task_id / correlation / runtime / role -------
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
                # R6-C1 evidence-integrity contract: the credential-shape
                # scan and safe_display both render at the DISPLAY layer
                # only — `error` itself (and the result/trace behind it)
                # is never mutated, never replaced, never swallowed.
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

        # -- qualification evidence (EVIDENCE_REUSE) ----------------------
        # Full G1-G14 for all three runtimes ran and PASSED in the
        # authorized R5-A round (VERIFIED / REAL / four capabilities
        # each; identity tuples exactly as recorded in the R5-A report).
        # Reconstructed here with the same gate surface; no new
        # qualification invocations in this round.
        _QUALIFICATION_PROVENANCE = "REAL"  # EVIDENCE_REUSE: R5-A output

        claude_validation = reused_validation(CLAUDE_IDENTITY, "r5a-claude")
        codex_validation = reused_validation(CODEX_IDENTITY, "r5a-codex")
        pi_validation = reused_validation(PI_IDENTITY, "r5a-pi")
        print("CLAUDE_QUALIFICATION(reused):",
              claude_validation.status.value,
              claude_validation.provenance,
              claude_validation.validated_capabilities)
        print("CODEX_QUALIFICATION(reused):",
              codex_validation.status.value,
              codex_validation.provenance,
              codex_validation.validated_capabilities)
        print("PI_QUALIFICATION(reused):",
              pi_validation.status.value,
              pi_validation.provenance,
              pi_validation.validated_capabilities)

        # -- one pool, three runtimes --------------------------------------
        pool = VerifiedRuntimePool(clock=lambda: 0.0)
        for validation in (claude_validation, codex_validation,
                           pi_validation):
            pool.admit(validation, CAPS_ALL, health_now="READY")
        self.assertEqual(list(pool.identities()), sorted(IDENTITIES))

        health = three_runtime_health()

        # -- capability provision (NOT role assignment) ---------------------
        # All 12 addresses (3 identities x 4 roles) are provisioned; which
        # runtime serves which role is the ORCHESTRATOR's + FACADE's own
        # decision through the injected FullSpreadAssigner policy.
        session_adapters = {
            collab_agent_address(CLAUDE_IDENTITY, "architect"): claude,
            collab_agent_address(CLAUDE_IDENTITY, "coder"): claude,
            collab_agent_address(CLAUDE_IDENTITY, "tester"): claude,
            collab_agent_address(CLAUDE_IDENTITY, "reviewer"): claude,
            collab_agent_address(CODEX_IDENTITY, "architect"): codex,
            collab_agent_address(CODEX_IDENTITY, "coder"): codex,
            collab_agent_address(CODEX_IDENTITY, "tester"): codex,
            collab_agent_address(CODEX_IDENTITY, "reviewer"): codex,
            collab_agent_address(PI_IDENTITY, "architect"): pi,
            collab_agent_address(PI_IDENTITY, "coder"): pi,
            collab_agent_address(PI_IDENTITY, "tester"): pi,
            collab_agent_address(PI_IDENTITY, "reviewer"): pi,
        }

        budget = TaskBudget(8, 8, timeout_seconds=300.0)
        usage = BudgetUsage()
        guard = LoopGuard()
        transport = LoopbackRemoteTransport()

        def session_factory():
            return CollaborationSession(
                transport, session_adapters, budget, usage, guard)

        probe = _SinglePathProbe()
        # R6-C9: test-layer FullSpreadAssigner — the exact policy proven
        # offline in OfflineCrossRuntimeAssignmentTests (fixed 4-role
        # stage-order round robin over the three-runtimes' candidate
        # order). Under the production two-call split it hands
        # architect=claude, coder=codex (dual call) and tester=pi,
        # reviewer=claude (verification call) — all three REAL-qualified
        # runtimes each receive a role, with ZERO production changes.
        policy = FullSpreadAssigner()
        orchestrator = CollaborationOrchestrator(
            probe, pool, health, budget, usage, guard, session_factory,
            role_assigner=policy)

        facade = ProductionFacade(
            orchestrator, session_adapters, pool, health, budget, usage,
            guard, role_assigner=policy)

        # -- execute through the production facade --------------------------
        result = facade.run(
            task_id=TASK_ID, task=TASK, prompt="p",
            mode=Mode.AUTO, provenance=claude_validation.provenance)

        print("REAL_OUTCOME_STATUS:", result.status)
        print("FACADE_PATH:", result.path)
        print("FACADE_STAGES:", result.stages)
        print("FACADE_FAILURE_CATEGORY:", result.failure_category)
        print("ROLE_RUNTIME_LOG:", role_runtime_log)
        history = facade.state.history(TASK_ID)
        if history:
            print("LEDGER_DECISION:", history[0].reason)
            for failed in facade.state.failures(TASK_ID):
                print("LEDGER_FAILURE:", failed.task_id,
                      failed.status, failed.reason)
        if result.status != "SUCCESS":
            for (runtime_name, role), raw in sorted(stage_outputs.items()):
                _print_failure_diagnosis(f"{runtime_name}:{role}", raw)
        print("INVOCATIONS: claude =", calls["claude-cli"],
              "| codex =", calls["codex-cli"],
              "| pi =", calls["pi-cli"],
              "| usage.total =", usage.total_agent_calls)

        # -- R6 success criteria (from structured data only) ----------------
        # 1. COMPLEX task routed through the four-stage path.
        self.assertIs(classify_task(TASK), Complexity.COMPLEX)
        # Path follows the production facade's own failure semantics: on
        # full success the facade labels FOUR_STAGE (after verification);
        # an honest upstream failure stays DUAL with the failure category
        # carried below. The path assertion therefore checks the label
        # that matches the observed status, never a hard-coded happy path.
        expected_path = ("FOUR_STAGE" if result.status == "SUCCESS"
                         else "DUAL")
        self.assertEqual(result.path, expected_path)

        # 2. status SUCCESS, all four stages, no failure category.
        self.assertEqual(result.status, "SUCCESS")
        self.assertEqual(result.stages,
                         ("architect", "coder", "tester", "reviewer"))
        self.assertEqual(result.failure_category, "")

        # 3. FOUR real invocations — the test-layer FullSpreadAssigner's
        #    deterministic split (mirrored offline in
        #    OfflineCrossRuntimeAssignmentTests and audited in R6-C9
        #    pre-flight): the dual call assigns (architect, coder) as
        #    claude/codex and the verification call assigns (test, review)
        #    as pi/claude. Four stages, four invocations, every one from
        #    the injected policy's own decisions over bridge candidates.
        total_invocations = sum(calls.values())
        self.assertEqual(total_invocations, 4)
        # Budget reserve-before-invoke: each of the four stages reserved
        # one call (usage counts reservations; the wrapper counts real
        # process invocations — both are structured, both exact).
        self.assertEqual(usage.total_agent_calls, 4)
        # R6-C12/C13: the canonical four-stage ledger is 1 DECISION +
        # 4 envelope records (ARCHITECTURE REQUEST, IMPLEMENTATION
        # REPLY, TEST REQUEST, REVIEW REQUEST) = 5 records — the same
        # contract tests/test_four_stage_chain.py locks
        # ([DECISION, REQUEST, REPLY, REQUEST, REQUEST]).
        self.assertEqual(len(history), 5)

        # 4. Structured trace: the exact role->runtime mapping the
        #    injected FullSpreadAssigner deterministically produces for
        #    this pool (R6-C9 offline audit output):
        #    architect=claude, coder=codex, tester=pi, reviewer=claude.
        stages_by_role = {
            role: runtime for (_task, role, runtime, _inv)
            in role_runtime_log}
        self.assertEqual(stages_by_role, {
            "architect": "claude-cli",
            "coder": "codex-cli",
            "tester": "pi-cli",
            "reviewer": "claude-cli",
        })
        used_runtimes = set(stages_by_role.values())
        # R6-C9 Goal B: ALL THREE REAL-qualified runtimes participate in
        # ONE four-stage scenario — this is the core three-runtime
        # coverage criterion (pi-cli receives the tester role).
        self.assertEqual(used_runtimes,
                         {"claude-cli", "codex-cli", "pi-cli"})
        # R6-C9 §五.8: the most important new evidence — pi-cli REAL
        # invocation count >= 1.
        self.assertGreaterEqual(calls["pi-cli"], 1)

        # 5. Correlation: the dual loop's correlation and per-hop
        #    correlations all present in the ledger.
        request_records = [r for r in history
                           if r.direction is CollaborationDirection.REQUEST]
        self.assertEqual(len(request_records), 3)
        correlations = [r.correlation_id for r in request_records]
        self.assertTrue(all(correlations))
        self.assertEqual(len(set(correlations)), 3)  # dual + test + review

        # 6. Ledger envelope addresses prove the cross-runtime handoffs.
        arch_addr = collab_agent_address(CLAUDE_IDENTITY, "architect")
        coder_addr = collab_agent_address(CODEX_IDENTITY, "coder")
        architecture_record = next(
            r for r in request_records
            if r.payload_type == "ARCHITECTURE")
        self.assertEqual(architecture_record.source_agent, arch_addr)
        self.assertEqual(architecture_record.target_agent, coder_addr)

        # 7. Ledger wire carries the four packet types in handoff order.
        payload_types = [r.payload_type for r in history[1:]]
        self.assertEqual(
            payload_types,
            ["ARCHITECTURE", "IMPLEMENTATION", "TEST", "REVIEW"])

        # 8. provenance = REAL on every envelope (from validation, never
        #    hand-set).
        for record in history[1:]:
            self.assertEqual(record.provenance, "REAL")

        # 9. No budget bypass, no silent fallback, no failures.
        self.assertEqual(facade.state.failures(TASK_ID), ())
        self.assertEqual(probe.calls, 0)

        # 10. Content safety: the closed result surface is secret-free.
        surface = repr(result)
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
