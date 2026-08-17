"""RC-2B-R: the single missing product evidence point.

One sanctioned REAL qualification; the SAME pool then serves ONE
AUTO+SIMPLE run routed SINGLE through the host entry (real
VerifiedOrchestrator, host parsing seam, packet-contract seam). Asserts
the full invariant set; honest structured failure output if it fails.
"""
import json
import os
import sys
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from candidate_validation import CandidateRuntimeInstance, CandidateValidationStatus
from claude_code_adapter import ClaudeCodeAdapter
from cli import run_cli
from host import build_facade
from real_validation_executor import run_real_validation
from runtime_status import (
    HealthEvidence,
    ReasonCode,
    RuntimeState,
    RuntimeStatus,
)

IDENTITY = ("claude-cli", "anthropic", None, "fp-rc2br-final")
CAPS_ALL = ("architecture", "coding", "review", "testing")
SIMPLE_TASK = "fix one simple bug in one file"
SECRET_MARKERS = ("token", "secret", "api_key", "authorization", "bearer", "stdout", "stderr")


def health():
    return {IDENTITY[0]: RuntimeStatus(
        runtime_id=IDENTITY[0], executable="claude", version="2",
        status=RuntimeState.READY, provider="anthropic", model=None,
        auth_method=None, reason_code=ReasonCode.NONE,
        evidence=HealthEvidence("d", "a", "p", "m", "ok"),
        checked_at=0.0, expires_at=1.0)}


class RC2BRSingleRealTests(unittest.TestCase):
    def setUp(self):
        if os.environ.get("RUN_REAL_PROVIDER_TESTS", "") != "1":
            self.skipTest("RUN_REAL_PROVIDER_TESTS != 1")

    def test_single_real_success_closure(self):
        adapter = ClaudeCodeAdapter.from_environment()
        if adapter is None:
            self.skipTest("claude executable not found")
        count = {"n": 0}
        real_invoke = adapter.invoke

        def counting(request):
            count["n"] += 1
            return real_invoke(request)

        adapter.invoke = counting
        # -- ONE sanctioned qualification --------------------------------------
        instance = CandidateRuntimeInstance(
            runtime_id=IDENTITY[0], provider_id=IDENTITY[1], model_id=None,
            config_fingerprint=IDENTITY[3], capability_context=(), probe=adapter,
            invocation_spec={"timeout_seconds": 300})
        validation, _ = run_real_validation(
            instance, adapter, timeout_seconds=300.0,
            experiment_id="rc2br-final-qualification")
        print("QUALIFICATION:", validation.status.value, validation.provenance,
              validation.validated_capabilities, "| calls:", count["n"])
        if validation.status is not CandidateValidationStatus.VERIFIED:
            failure = next((g for g in validation.gate_results
                            if g.verdict.value != "PASS"), None)
            print("QUALIFICATION_FAILURE:", json.dumps({
                "gate": (validation.failure_point[0].name
                         if validation.failure_point else None),
                "category": (str(validation.failure_point[1])
                             if validation.failure_point else None),
                "role": (failure.evidence.get("failure_role")
                         if failure else None),
                "detail": (failure.evidence.get("failure_detail")
                           if failure else None),
                "exception_type": (failure.evidence.get("exception_type")
                                   if failure else None),
                "shape": (failure.evidence.get("shape") if failure else None),
                "invocations": count["n"],
            }, sort_keys=True, default=str))
            self.fail("qualification FAILED — SINGLE not attempted")
        self.assertEqual(validation.provenance, "REAL")
        self.assertEqual(validation.validated_capabilities, CAPS_ALL)
        qualification_calls = count["n"]

        # -- ONE SINGLE run via the user entry (AUTO + SIMPLE) ---------------
        facade = build_facade(adapter, validation, health())
        usage = facade._usage
        started = time.monotonic()
        summary = json.loads(run_cli(facade, ["run", SIMPLE_TASK]))
        elapsed = round(time.monotonic() - started, 1)
        print("SINGLE_REAL:", json.dumps(summary, sort_keys=True),
              "| elapsed:", elapsed)
        print("INVOCATIONS: qualification =", qualification_calls,
              "| single =", count["n"] - qualification_calls,
              "| total =", count["n"])

        # Routing invariants
        self.assertEqual(summary["mode"], "AUTO")
        self.assertEqual(summary["path"], "SINGLE")
        # Execution invariants: exactly ONE coder invocation.
        self.assertEqual(count["n"] - qualification_calls, 1)
        # Result invariants
        self.assertEqual(summary["status"], "SUCCESS")
        self.assertEqual(summary["provenance"], "REAL")
        self.assertEqual(summary["stages"], [])
        # Budget: 1 reserve / 1 consume / 1 invocation, no overflow.
        self.assertEqual(usage.total_agent_calls, 1)
        self.assertEqual(usage.coder_calls, 1)
        # No FOUR_STAGE ledger: single-path ExecutionResult carries no
        # collaboration envelopes; nothing was fabricated.
        # Safety
        lowered = json.dumps(summary).lower()
        for marker in SECRET_MARKERS:
            self.assertNotIn(marker, lowered)
        self.assertEqual(adapter._processes, {})


if __name__ == "__main__":
    unittest.main()
