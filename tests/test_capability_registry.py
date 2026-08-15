import sys
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from capability_registry import (
    AgentProfile,
    CapabilityEvidence,
    CapabilityRegistry,
    CapabilityConfidence,
    CapabilityName,
    SelectionReason,
)
from runtime_status import HealthEvidence, ReasonCode, RuntimeState, RuntimeStatus


def runtime(runtime_id, state=RuntimeState.READY):
    return RuntimeStatus(
        runtime_id=runtime_id,
        executable=f"{runtime_id}.exe",
        version="1.0",
        status=state,
        provider="provider",
        model="model",
        auth_method="managed",
        reason_code=ReasonCode.NONE if state is RuntimeState.READY else ReasonCode.AUTH_REQUIRED,
        evidence=HealthEvidence("verified", "authenticated", "verified", "verified", "passed"),
        checked_at=100,
        expires_at=200,
    )


def agent(agent_id, runtime_id, role="coder", capabilities=None, reliability=0.8):
    capabilities = capabilities or {
        CapabilityName.CODING: CapabilityEvidence(CapabilityName.CODING, 0.9, CapabilityConfidence.VERIFIED, "test")
    }
    return AgentProfile(
        agent_id=agent_id,
        runtime_id=runtime_id,
        provider="provider",
        model="model",
        role=role,
        capabilities=capabilities,
        historical_success_rate=reliability,
    )


class CapabilityRegistryTests(unittest.TestCase):
    def test_capability_evidence_has_confidence_and_is_immutable(self):
        evidence = CapabilityEvidence(
            CapabilityName.ARCHITECTURE,
            0.9,
            CapabilityConfidence.VERIFIED,
            "integration-test",
        )
        self.assertEqual(evidence.confidence, CapabilityConfidence.VERIFIED)
        with self.assertRaises(FrozenInstanceError):
            evidence.score = 0.1

    def test_unknown_capability_does_not_pass_hard_gate(self):
        registry = CapabilityRegistry([agent(
            "a", "runtime-a", capabilities={
                CapabilityName.CODING: CapabilityEvidence(
                    CapabilityName.CODING, None, CapabilityConfidence.UNKNOWN, None
                )
            }
        )])

        result = registry.select(
            required_capabilities={CapabilityName.CODING},
            runtimes={"runtime-a": runtime("runtime-a")},
        )

        self.assertIsNone(result.agent_id)
        self.assertEqual(result.reason, SelectionReason.NO_CAPABLE_AGENT)

    def test_non_ready_runtime_is_never_selected(self):
        registry = CapabilityRegistry([agent("a", "runtime-a")])

        result = registry.select(
            required_capabilities={CapabilityName.CODING},
            runtimes={"runtime-a": runtime("runtime-a", RuntimeState.AUTH_REQUIRED)},
        )

        self.assertIsNone(result.agent_id)
        self.assertEqual(result.reason, SelectionReason.NO_CAPABLE_AGENT)

    def test_same_capability_can_be_provided_by_multiple_runtimes(self):
        registry = CapabilityRegistry([
            agent("slow", "runtime-a", reliability=0.7),
            agent("reliable", "runtime-b", reliability=0.9),
        ])

        result = registry.select(
            required_capabilities={CapabilityName.CODING},
            runtimes={"runtime-a": runtime("runtime-a"), "runtime-b": runtime("runtime-b")},
        )

        self.assertEqual(result.agent_id, "reliable")

    def test_selection_is_deterministic_on_tie(self):
        registry = CapabilityRegistry([
            agent("z-agent", "runtime-z"),
            agent("a-agent", "runtime-a"),
        ])
        runtimes = {"runtime-z": runtime("runtime-z"), "runtime-a": runtime("runtime-a")}

        first = registry.select({CapabilityName.CODING}, runtimes)
        second = registry.select({CapabilityName.CODING}, runtimes)

        self.assertEqual(first, second)
        self.assertEqual(first.agent_id, "a-agent")

    def test_role_suitability_is_a_selection_requirement(self):
        registry = CapabilityRegistry([
            agent("coder", "runtime-a", role="coder"),
            agent("reviewer", "runtime-b", role="reviewer", capabilities={
                CapabilityName.REVIEW: CapabilityEvidence(
                    CapabilityName.REVIEW, 0.9, CapabilityConfidence.VERIFIED, "test"
                )
            }),
        ])
        result = registry.select(
            {CapabilityName.REVIEW},
            {"runtime-a": runtime("runtime-a"), "runtime-b": runtime("runtime-b")},
            role="reviewer",
        )
        self.assertEqual(result.agent_id, "reviewer")

    def test_registry_does_not_expose_secret_fields(self):
        profile = agent("a", "runtime-a")
        self.assertFalse(hasattr(profile, "token"))
        self.assertFalse(hasattr(profile, "secret"))
        self.assertFalse(hasattr(profile, "api_key"))


if __name__ == "__main__":
    unittest.main()
