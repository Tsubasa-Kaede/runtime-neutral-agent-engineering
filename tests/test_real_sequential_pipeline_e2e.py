"""ORCH-4 (V3.2 COMP-3): gated REAL sequential pipeline E2E.

Proves the frozen sequential orchestration stack drives a genuine
four-stage collaboration across two REAL runtimes through the cockpit
composition surface, with NO single-runtime fallback and NO fabricated
evidence:

    Step 0  architect  (Claude)   REAL invocation #1
    Step 1  coder      (Pi)       REAL invocation #2
    Step 2  tester     (Pi)       REAL invocation #3
    Step 3  reviewer   (Claude)   REAL invocation #4

Assembly is 100% frozen production code (zero production changes):

    ControlJournal + UsageLog
      + build_execution_slots (4 slots, single boundary/journaler)
      + 4 StepSpec values (caller-supplied pure request builders)
      + build_sequential_pipeline -> SequentialPipeline.run()

Discipline locked by the offline layer below and enforced here:
- env gate: RUN_REAL_PROVIDER_TESTS=1 is the only switch; absent =
  honest SkipTest (never a new baseline failure);
- budget: exactly 4 REAL invocations (2 per runtime); any excess or
  repeat fails the counting assertions (no automatic second attempt);
- handoff: previous_result flows by object identity into the next
  request builder, and the next stage prompt verbatim embeds the
  prior stage output (truncated for budget, never parsed packets);
- identity honesty: StepRecord.invocation_id comes from the REAL
  adapter trace; UsageRecord.invocation_id comes from the frozen
  wrapper mint (two distinct real identity domains, never compared);
- usage honesty: KNOWN / UNKNOWN / UNSUPPORTED per frozen tri-state
  coupling; no token numbers are ever guessed;
- credential safety: protected files are snapshotted before/after and
  compared at the credential boundary — credential-bearing files must
  be byte-stable (mtime+size identical); the runtime-owned state file
  ~/.claude.json may drift in mtime from the runtime's own
  bookkeeping (a live CLI session writes it on its own schedule),
  but its size must stay stable (content growth still fails); nothing
  is printed on success; failure messages carry statuses and
  lengths only, never raw model output or credentials.

Pause/resume continuation is deliberately NOT exercised in this
round (offline-proven; authorized decision D2=iii): the chain runs
straight through step0 -> step3 with no control commands submitted.
"""
import os
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_real_multi_runtime_collaboration import (
    _surface_has_credential_shape,
    protected_snapshot,
)

from control_journal import ControlJournal
from execution_slots import ExecutionSlotSpec, build_execution_slots
from external_runtime import (
    ExternalAgentRequest,
    InvocationResult,
    InvocationStatus,
    InvocationTrace,
)
from sequential_pipeline import RunStatus, StepSpec, build_sequential_pipeline
from usage_log import UsageLog, UsageObservation

RUN_REAL_PROVIDER_TESTS = os.environ.get("RUN_REAL_PROVIDER_TESTS") == "1"

TASK_ID = "orch4-real-task-1"
EXECUTION_ID = "orch4-real-execution-1"
REQUEST_TIMEOUT_SECONDS = 300.0
MAX_REAL_INVOCATIONS = 4
EMBED_LIMIT = 4000

EXPECTED_SLOT_SEQUENCE = (
    "slot-architect", "slot-coder", "slot-tester", "slot-reviewer")
EXPECTED_ROLE_SEQUENCE = ("architect", "coder", "tester", "reviewer")
EXPECTED_RUNTIME_SEQUENCE = ("claude-cli", "pi-cli", "pi-cli", "claude-cli")

_STAGE_PROMPTS = {
    "architect": (
        "你是架构师。任务：为函数 is_palindrome(s: str) -> bool"
        "（忽略大小写与空格）给出 3-5 行实现计划。"
        "只输出计划文本，不超过 10 行。"
    ),
    "coder": (
        "你是程序员。根据以下架构师计划，写出 is_palindrome 的 "
        "Python 实现与一个最小测试断言。只输出代码与断言，"
        "不超过 20 行。\n\n=== 架构师计划 ===\n"
    ),
    "tester": (
        "你是测试员。审阅以下实现，列出 2-3 个测试断言，"
        "并给出结论 PASS 或 NEEDS_WORK。不超过 15 行。"
        "\n\n=== 程序员输出 ===\n"
    ),
    "reviewer": (
        "你是评审员。基于以下测试报告给出最终裁定 "
        "APPROVED 或 CHANGES_REQUESTED，并给出一行理由。"
        "不超过 6 行。\n\n=== 测试报告 ===\n"
    ),
}


def _stage_prompt(role, previous_result):
    """Caller-supplied pure hook output: a fresh prompt per stage.

    The prior stage output is truncated for budget and embedded as
    plain text inside a new stage template — it is never forwarded
    verbatim as the whole prompt and never parsed as a provider
    packet (the orchestrator stays packet-agnostic by design).
    """
    template = _STAGE_PROMPTS[role]
    if previous_result is None:
        return template
    prior = previous_result.output or ""
    if len(prior) > EMBED_LIMIT:
        prior = prior[:EMBED_LIMIT]
    return template + prior


def _make_request_builder(step_index, agent_id, role, provider, received):
    """Build one StepSpec request_builder closure (pure; records arg)."""

    def request_builder(previous_result):
        received.append((step_index, previous_result))
        return ExternalAgentRequest(
            task_id=TASK_ID,
            prompt=_stage_prompt(role, previous_result),
            agent_id=agent_id,
            role=role,
            provider=provider,
            model=None,
            timeout_seconds=REQUEST_TIMEOUT_SECONDS,
        )

    return request_builder


def _wrap_counting_recording(adapter, counter, captured):
    """Delegate-through wrapper: counts invocations, captures pairs.

    The wrapped call still executes the adapter's REAL invoke (no
    substitution, no interception of the result value).
    """
    real_invoke = adapter.invoke

    def wrapped(request):
        counter["n"] += 1
        result = real_invoke(request)
        captured.append((request, result))
        return result

    adapter.invoke = wrapped


def _assemble_four_stage(claude, pi):
    """Frozen-code assembly of the authorized 4-slot/4-step chain."""
    received = []
    claude_provider = claude.profile.provider
    pi_provider = pi.profile.provider
    journal = ControlJournal()
    usage_log = UsageLog()
    slot_specs = (
        ExecutionSlotSpec(slot_id="slot-architect", raw_adapter=claude,
                          usage_log=usage_log,
                          runtime_id=claude.profile.runtime,
                          role="architect"),
        ExecutionSlotSpec(slot_id="slot-coder", raw_adapter=pi,
                          usage_log=usage_log,
                          runtime_id=pi.profile.runtime,
                          role="coder"),
        ExecutionSlotSpec(slot_id="slot-tester", raw_adapter=pi,
                          usage_log=usage_log,
                          runtime_id=pi.profile.runtime,
                          role="tester"),
        ExecutionSlotSpec(slot_id="slot-reviewer", raw_adapter=claude,
                          usage_log=usage_log,
                          runtime_id=claude.profile.runtime,
                          role="reviewer"),
    )
    slots = build_execution_slots(journal, slot_specs,
                                  execution_id=EXECUTION_ID)
    steps = (
        StepSpec(slot_id="slot-architect",
                 request_builder=_make_request_builder(
                     0, "orch4-architect", "architect", claude_provider,
                     received)),
        StepSpec(slot_id="slot-coder",
                 request_builder=_make_request_builder(
                     1, "orch4-coder", "coder", pi_provider, received)),
        StepSpec(slot_id="slot-tester",
                 request_builder=_make_request_builder(
                     2, "orch4-tester", "tester", pi_provider, received)),
        StepSpec(slot_id="slot-reviewer",
                 request_builder=_make_request_builder(
                     3, "orch4-reviewer", "reviewer", claude_provider,
                     received)),
    )
    pipeline = build_sequential_pipeline(slots, steps)
    return {
        "journal": journal,
        "usage_log": usage_log,
        "received": received,
        "pipeline": pipeline,
    }


def _assert_four_stage_evidence(testcase, outcome, bundle, captured):
    """Shared evidence matrix (offline wiring + gated REAL)."""
    received = bundle["received"]
    usage_log = bundle["usage_log"]
    journal = bundle["journal"]

    # -- outcome shape --------------------------------------------------
    testcase.assertIs(outcome.status, RunStatus.COMPLETED)
    testcase.assertIsNone(outcome.error)
    testcase.assertIsNone(outcome.run_state)

    # -- budget: exactly 4 invocations, zero excess ----------------------
    testcase.assertEqual(len(captured), MAX_REAL_INVOCATIONS)

    # -- per-invocation REAL success + adapter trace provenance ----------
    trace_ids = []
    for i, (request, result) in enumerate(captured):
        testcase.assertIs(
            result.status, InvocationStatus.SUCCESS,
            f"step {i}: non-SUCCESS terminal status")
        testcase.assertIsNotNone(result.trace, f"step {i}: trace absent")
        trace_ids.append(result.trace.invocation_id)
    testcase.assertEqual(len(set(trace_ids)), MAX_REAL_INVOCATIONS,
                         "adapter trace invocation ids must be 4 "
                         "distinct REAL ids")

    # -- final result identity -------------------------------------------
    testcase.assertIs(outcome.final_result, captured[-1][1])

    # -- transcript: 4 real StepRecords -----------------------------------
    transcript = outcome.transcript
    testcase.assertEqual(len(transcript), 4)
    for i, record in enumerate(transcript):
        testcase.assertEqual(record.step_index, i)
        testcase.assertEqual(record.slot_id, EXPECTED_SLOT_SEQUENCE[i])
        testcase.assertIs(record.status, InvocationStatus.SUCCESS)
        testcase.assertEqual(record.invocation_id, trace_ids[i],
                             "StepRecord invocation_id must come from "
                             "the REAL adapter trace")

    # -- handoff: object-identity chain ------------------------------------
    testcase.assertEqual([idx for idx, _ in received], [0, 1, 2, 3])
    testcase.assertIsNone(received[0][1])
    for k in (1, 2, 3):
        testcase.assertIs(received[k][1], captured[k - 1][1],
                          f"step {k} builder did not receive step "
                          f"{k - 1} result object")

    # -- handoff: next prompt embeds prior output verbatim ------------------
    for k in (1, 2, 3):
        prior_output = captured[k - 1][1].output or ""
        testcase.assertTrue(
            prior_output,
            f"step {k - 1} output empty: handoff content unverifiable")
        testcase.assertIn(prior_output[:120], captured[k][0].prompt)

    # -- usage: honest tri-state + runtime/role identity ---------------------
    records = usage_log.snapshot()
    testcase.assertEqual(len(records), MAX_REAL_INVOCATIONS)
    testcase.assertEqual([r.runtime_id for r in records],
                         list(EXPECTED_RUNTIME_SEQUENCE))
    testcase.assertEqual([r.role for r in records],
                         list(EXPECTED_ROLE_SEQUENCE))
    for r in records:
        testcase.assertIn(r.usage_status,
                          (UsageObservation.KNOWN,
                           UsageObservation.UNKNOWN,
                           UsageObservation.UNSUPPORTED))
        if r.usage_status is UsageObservation.KNOWN:
            testcase.assertIsNotNone(r.input_tokens)
            testcase.assertIsNotNone(r.output_tokens)
    testcase.assertEqual(len({r.invocation_id for r in records}),
                         MAX_REAL_INVOCATIONS,
                         "usage invocation ids are the frozen wrapper "
                         "mint (a distinct REAL domain from the adapter "
                         "trace ids)")

    # -- control journal: normal path fabricates zero control facts ----------
    testcase.assertEqual(len(journal.snapshot()), 0,
                         "normal path must not fabricate control facts")


_TOLERATED_STATE_PATH = Path.home() / ".claude.json"


def _assert_protected_unchanged(testcase, before, after):
    """Credential-safety boundary comparison (asset-local).

    Strict set (credential-bearing files — everything the shared
    protected_snapshot watches EXCEPT the runtime-owned state file):
    mtime and size must be identical across the REAL run.

    Tolerated set (~/.claude.json — the claude CLI's own top-level
    state file): a live CLI session and the adapter subprocesses write
    their own bookkeeping there on their own schedule, so an mtime-only
    drift is expected runtime behavior, NOT tampering. The SIZE must
    stay stable (content growth still fails) and the file must not
    appear or vanish mid-run.
    """
    strict_before = {p: s for p, s in before.items()
                     if p != _TOLERATED_STATE_PATH}
    strict_after = {p: s for p, s in after.items()
                    if p != _TOLERATED_STATE_PATH}
    testcase.assertEqual(
        strict_after, strict_before,
        "credential-bearing protected files changed during the REAL run")
    if (_TOLERATED_STATE_PATH in before
            or _TOLERATED_STATE_PATH in after):
        testcase.assertIn(
            _TOLERATED_STATE_PATH, before,
            "runtime state file appeared during the REAL run")
        testcase.assertIn(
            _TOLERATED_STATE_PATH, after,
            "runtime state file vanished during the REAL run")
        testcase.assertEqual(
            after[_TOLERATED_STATE_PATH][1],
            before[_TOLERATED_STATE_PATH][1],
            "runtime state file size changed during the REAL run "
            "(mtime-only drift is tolerated; content growth is not)")


class _SpyProfile:
    """Minimal profile namespace for the offline wiring double."""

    def __init__(self, runtime, provider):
        self.runtime = runtime
        self.provider = provider


class _SpyRaw:
    """Offline wiring double: records requests, returns SUCCESS."""

    def __init__(self, runtime_label):
        self.profile = _SpyProfile(runtime_label, None)
        self.runtime_label = runtime_label
        self.requests = []

    def invoke(self, request):
        self.requests.append(request)
        n = len(self.requests)
        trace = InvocationTrace(
            invocation_id=f"spy-{self.runtime_label}-{n}",
            task_id=request.task_id,
            agent_id=request.agent_id,
            runtime=self.runtime_label,
            provider=None,
            model=None,
            role=request.role,
            status=InvocationStatus.SUCCESS,
        )
        return InvocationResult(
            status=InvocationStatus.SUCCESS,
            output=f"spy output {self.runtime_label} #{n}",
            error=None,
            trace=trace,
        )


class FileDisciplineTests(unittest.TestCase):
    """Layer 1: this file's own gate/safety discipline locks."""

    def setUp(self):
        with open(__file__, "r", encoding="utf-8") as handle:
            self.source = handle.read()

    def test_env_gate_is_the_only_real_switch(self):
        self.assertIn("RUN_REAL_PROVIDER_TESTS", self.source)
        self.assertIn("SkipTest", self.source)

    def test_no_substitution_machinery_in_real_path(self):
        banned = ("unittest.mock", "Mock(", "sleep(", "while ")
        # Exclude this discipline test's own assertion lines from the
        # scan (they legitimately contain the banned tokens).
        scanned = "\n".join(
            line for line in self.source.splitlines()
            if "assertNotIn(" not in line and "banned" not in line)
        for token in banned:
            self.assertNotIn(token, scanned)

    def test_budget_and_chain_constants_locked(self):
        self.assertEqual(MAX_REAL_INVOCATIONS, 4)
        self.assertEqual(EXPECTED_RUNTIME_SEQUENCE,
                         ("claude-cli", "pi-cli", "pi-cli", "claude-cli"))
        self.assertEqual(EXPECTED_ROLE_SEQUENCE,
                         ("architect", "coder", "tester", "reviewer"))
        self.assertEqual(len(set(EXPECTED_RUNTIME_SEQUENCE)), 2)

    def test_stage_templates_carry_no_credential_shape(self):
        for text in (TASK_ID, EXECUTION_ID):
            self.assertFalse(_surface_has_credential_shape(text))
        for text in _STAGE_PROMPTS.values():
            self.assertFalse(_surface_has_credential_shape(text))

    def test_protected_snapshot_helper_wired(self):
        self.assertIn("protected_snapshot", self.source)
        self.assertIn("before = protected_snapshot()", self.source)
        self.assertIn("after = protected_snapshot()", self.source)
        self.assertIn(
            "_assert_protected_unchanged(self, before, after)",
            self.source)


class OfflineWiringTests(unittest.TestCase):
    """Layer 1: prove assembly + evidence matrix before any REAL cost."""

    def test_offline_four_stage_wiring_and_evidence(self):
        claude_spy = _SpyRaw("claude-cli")
        pi_spy = _SpyRaw("pi-cli")
        captured = []
        _wrap_counting_recording(claude_spy, {"n": 0}, captured)
        _wrap_counting_recording(pi_spy, {"n": 0}, captured)

        bundle = _assemble_four_stage(claude_spy, pi_spy)
        outcome = bundle["pipeline"].run()
        _assert_four_stage_evidence(self, outcome, bundle, captured)

        self.assertEqual(len(claude_spy.requests), 2)
        self.assertEqual(len(pi_spy.requests), 2)


class ProtectedBoundaryTests(unittest.TestCase):
    """Layer 1: credential-safety boundary semantics (offline, pure
    dict logic — no file IO, no REAL)."""

    def _sample_paths(self):
        return (Path.home() / ".codex" / "auth.json",
                _TOLERATED_STATE_PATH)

    def test_strict_file_drift_is_flagged(self):
        strict_path, tolerated_path = self._sample_paths()
        before = {strict_path: (100, 10), tolerated_path: (100, 42)}
        after = {strict_path: (999, 10), tolerated_path: (100, 42)}
        with self.assertRaises(AssertionError):
            _assert_protected_unchanged(self, before, after)

    def test_state_file_mtime_drift_with_stable_size_passes(self):
        strict_path, tolerated_path = self._sample_paths()
        before = {strict_path: (100, 10), tolerated_path: (100, 42)}
        after = {strict_path: (100, 10), tolerated_path: (999, 42)}
        _assert_protected_unchanged(self, before, after)

    def test_state_file_size_growth_is_flagged(self):
        _, tolerated_path = self._sample_paths()
        before = {tolerated_path: (100, 42)}
        after = {tolerated_path: (999, 43)}
        with self.assertRaises(AssertionError):
            _assert_protected_unchanged(self, before, after)


class RealSequentialPipelineE2ETests(unittest.TestCase):
    """Layer 2 (gated): the authorized four-stage REAL chain."""

    @classmethod
    def setUpClass(cls):
        if not RUN_REAL_PROVIDER_TESTS:
            raise unittest.SkipTest("RUN_REAL_PROVIDER_TESTS != 1")

    def test_real_four_stage_sequential_pipeline(self):
        from claude_code_adapter import ClaudeCodeAdapter
        from pi_adapter import PiAdapter

        claude = ClaudeCodeAdapter.from_environment()
        if claude is None:
            self.skipTest("claude executable not found")
        pi = PiAdapter.from_environment()
        if pi is None:
            self.skipTest("pi executable not found")

        claude_calls = {"n": 0}
        pi_calls = {"n": 0}
        captured = []
        _wrap_counting_recording(claude, claude_calls, captured)
        _wrap_counting_recording(pi, pi_calls, captured)

        before = protected_snapshot()
        try:
            bundle = _assemble_four_stage(claude, pi)
            outcome = bundle["pipeline"].run()
            _assert_four_stage_evidence(self, outcome, bundle, captured)
        finally:
            after = protected_snapshot()
            _assert_protected_unchanged(self, before, after)

        # budget split per runtime: exactly 2 + 2 (any repeat = FAIL)
        self.assertEqual(claude_calls["n"], 2)
        self.assertEqual(pi_calls["n"], 2)


if __name__ == "__main__":
    unittest.main()
