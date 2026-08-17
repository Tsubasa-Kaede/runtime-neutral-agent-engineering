"""RC-2 REAL smokes: host entry → ProductionFacade, DUAL + SINGLE/AUTO.

One sanctioned qualification (gated) admits the runtime; the SAME pool then
serves both smokes (qualification-once). Smoke 1: CLI/host path drives a
FOUR_STAGE run (integration, not stability). Smoke 2: Mode.AUTO with a SIMPLE
task routes SINGLE through the REAL VerifiedOrchestrator — the first real
execution of the SINGLE path. Structured safe output only.
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

IDENTITY = ("claude-cli", "anthropic", None, "fp-rc2-host-4")
CAPS_ALL = ("architecture", "coding", "review", "testing")
DUAL_TASK = ("Produce a minimal implementation plan for adding a pure function "
             "that converts Celsius to Fahrenheit. Do not modify files.")
SIMPLE_TASK = "fix one simple bug in one file"
SECRET_MARKERS = ("token", "secret", "api_key", "authorization", "bearer", "stdout", "stderr")


def health():
    return {IDENTITY[0]: RuntimeStatus(
        runtime_id=IDENTITY[0], executable="claude", version="2",
        status=RuntimeState.READY, provider="anthropic", model=None,
        auth_method=None, reason_code=ReasonCode.NONE,
        evidence=HealthEvidence("d", "a", "p", "m", "ok"),
        checked_at=0.0, expires_at=1.0)}


class RC2RealSmokeTests(unittest.TestCase):
    def setUp(self):
        if os.environ.get("RUN_REAL_PROVIDER_TESTS", "") != "1":
            self.skipTest("RUN_REAL_PROVIDER_TESTS != 1")

    def test_host_dual_and_single_smokes(self):
        adapter = ClaudeCodeAdapter.from_environment()
        if adapter is None:
            self.skipTest("claude executable not found")
        count = {"n": 0}
        real_invoke = adapter.invoke

        def counting(request):
            count["n"] += 1
            return real_invoke(request)

        adapter.invoke = counting
        instance = CandidateRuntimeInstance(
            runtime_id=IDENTITY[0], provider_id=IDENTITY[1], model_id=None,
            config_fingerprint=IDENTITY[3], capability_context=(), probe=adapter,
            invocation_spec={"timeout_seconds": 300})
        validation, _ = run_real_validation(
            instance, adapter, timeout_seconds=300.0,
            experiment_id="rc2-host-qualification")
        print("QUALIFICATION:", validation.status.value, validation.provenance,
              validation.validated_capabilities, "| calls:", count["n"])
        self.assertEqual(validation.status.value, "VERIFIED")
        self.assertEqual(validation.provenance, "REAL")
        self.assertEqual(validation.validated_capabilities, CAPS_ALL)
        qualification_calls = count["n"]

        facade = build_facade(adapter, validation, health())

        # -- Smoke 1: DUAL via the CLI seam (user entry → facade) ----------
        started = time.monotonic()
        summary = json.loads(run_cli(facade, ["run", "--mode", "on", DUAL_TASK]))
        dual_elapsed = round(time.monotonic() - started, 1)
        print("DUAL_SMOKE:", json.dumps(summary, sort_keys=True),
              "| elapsed:", dual_elapsed)
        self.assertEqual(summary["status"], "SUCCESS")
        self.assertEqual(summary["path"], "FOUR_STAGE")
        self.assertEqual(summary["provenance"], "REAL")
        self.assertEqual(summary["stages"],
                         ["architect", "coder", "tester", "reviewer"])
        self.assertEqual(summary["failure_category"], "")
        self.assertEqual(summary["mode"], "ON")

        # -- Smoke 2: AUTO + SIMPLE → SINGLE via REAL VerifiedOrchestrator ---
        # A fresh facade = a fresh task lifecycle (budget/usage/guard): the
        # dual smoke already consumed the first lifecycle's 4-call budget,
        # and one facade instance is ONE task lifecycle by design.
        facade2 = build_facade(adapter, validation, health())
        started = time.monotonic()
        summary2 = json.loads(run_cli(facade2, ["run", SIMPLE_TASK]))
        single_elapsed = round(time.monotonic() - started, 1)
        print("SINGLE_SMOKE:", json.dumps(summary2, sort_keys=True),
              "| elapsed:", single_elapsed)
        self.assertEqual(summary2["mode"], "AUTO")
        self.assertEqual(summary2["path"], "SINGLE")
        self.assertNotEqual(summary2["path"], "FOUR_STAGE")

        # -- Accounting ------------------------------------------------------
        chain_calls = count["n"] - qualification_calls
        print("INVOCATIONS: qualification =", qualification_calls,
              "| chain =", chain_calls,
              "| total =", count["n"])
        # 4 (dual) + 1 (single coder) = 5 chain calls, 0 retry/fallback.
        self.assertEqual(chain_calls, 5)
        self.assertEqual(adapter._processes, {})
        for text in (json.dumps(summary), json.dumps(summary2)):
            lowered = text.lower()
            for marker in SECRET_MARKERS:
                self.assertNotIn(marker, lowered)


if __name__ == "__main__":
    unittest.main()
