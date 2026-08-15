import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from capability_registry import (
    AgentProfile, CapabilityConfidence, CapabilityEvidence, CapabilityName,
)
from fallback_policy import FallbackReason, FallbackPolicy
from runtime_status import HealthEvidence, ReasonCode, RuntimeState, RuntimeStatus


def runtime(runtime_id, state=RuntimeState.READY):
    return RuntimeStatus(runtime_id, runtime_id + ".exe", "1", state, "provider", "model", "managed", ReasonCode.NONE, HealthEvidence("verified", "authenticated", "verified", "verified", "passed"), 1, 100)


def agent(agent_id, runtime_id, score=.9):
    return AgentProfile(agent_id, runtime_id, "provider", "model", "coder", {CapabilityName.CODING: CapabilityEvidence(CapabilityName.CODING, score, CapabilityConfidence.VERIFIED, "test")}, .8)


class FallbackPolicyTests(unittest.TestCase):
    def test_selects_best_alternative_ready_capable_agent(self):
        policy = FallbackPolicy([agent("primary", "r1", .9), agent("backup", "r2", .8)])
        result = policy.select("primary", {"r1": runtime("r1"), "r2": runtime("r2")}, {CapabilityName.CODING})
        self.assertEqual(result.agent_id, "backup")
        self.assertEqual(result.reason, FallbackReason.SELECTED)

    def test_never_selects_non_ready_runtime(self):
        policy = FallbackPolicy([agent("primary", "r1"), agent("auth", "r2")])
        result = policy.select("primary", {"r1": runtime("r1"), "r2": runtime("r2", RuntimeState.AUTH_REQUIRED)}, {CapabilityName.CODING})
        self.assertEqual(result.reason, FallbackReason.NO_FALLBACK_AGENT)

    def test_excludes_primary_and_requires_capability(self):
        policy = FallbackPolicy([agent("primary", "r1"), AgentProfile(
            "reviewer", "r2", "provider", "model", "reviewer", {
                CapabilityName.REVIEW: CapabilityEvidence(
                    CapabilityName.REVIEW, .9, CapabilityConfidence.VERIFIED, "test"
                )
            }, .8,
        )])
        result = policy.select("primary", {"r1": runtime("r1"), "r2": runtime("r2")}, {CapabilityName.CODING})
        self.assertEqual(result.reason, FallbackReason.NO_FALLBACK_AGENT)

    def test_deterministic_tie_break(self):
        policy = FallbackPolicy([agent("z", "rz"), agent("a", "ra")])
        result = policy.select("primary", {"rz": runtime("rz"), "ra": runtime("ra")}, {CapabilityName.CODING})
        self.assertEqual(result.agent_id, "a")

    def test_requires_budget_and_guard_admission(self):
        policy = FallbackPolicy([agent("primary", "r1"), agent("backup", "r2")])
        result = policy.select("primary", {"r1": runtime("r1"), "r2": runtime("r2")}, {CapabilityName.CODING}, budget_available=False)
        self.assertEqual(result.reason, FallbackReason.BUDGET_EXHAUSTED)


if __name__ == "__main__":
    unittest.main()
