"""Provider-neutral capability evidence and deterministic agent selection."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Mapping

from runtime_status import RuntimeState, RuntimeStatus


class CapabilityName(str, Enum):
    ARCHITECTURE = "architecture"
    CODING = "coding"
    REVIEW = "review"
    TESTING = "testing"


class CapabilityConfidence(str, Enum):
    VERIFIED = "VERIFIED"
    DECLARED = "DECLARED"
    UNKNOWN = "UNKNOWN"


class SelectionReason(str, Enum):
    SELECTED = "SELECTED"
    NO_CAPABLE_AGENT = "NO_CAPABLE_AGENT"


@dataclass(frozen=True)
class CapabilityEvidence:
    capability: CapabilityName
    score: float | None
    confidence: CapabilityConfidence
    source: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.capability, CapabilityName):
            raise ValueError("invalid capability")
        if self.score is not None and not 0.0 <= self.score <= 1.0:
            raise ValueError("capability score must be between 0 and 1")
        if self.confidence is CapabilityConfidence.UNKNOWN and self.score is not None:
            raise ValueError("unknown capability cannot have a score")
        if self.confidence is not CapabilityConfidence.UNKNOWN and not self.source:
            raise ValueError("known capability requires evidence source")


@dataclass(frozen=True)
class AgentProfile:
    agent_id: str
    runtime_id: str
    provider: str | None
    model: str | None
    role: str | None
    capabilities: Mapping[CapabilityName, CapabilityEvidence]
    historical_success_rate: float | None = None

    def __post_init__(self) -> None:
        if not self.agent_id or not self.runtime_id:
            raise ValueError("agent and runtime identifiers are required")
        if self.historical_success_rate is not None and not 0.0 <= self.historical_success_rate <= 1.0:
            raise ValueError("historical success rate must be between 0 and 1")
        if any(not isinstance(key, CapabilityName) for key in self.capabilities):
            raise ValueError("invalid capability key")
        if any(value.capability is not key for key, value in self.capabilities.items()):
            raise ValueError("capability evidence key mismatch")


@dataclass(frozen=True)
class SelectionCandidate:
    agent_id: str
    runtime_id: str
    score: float | None
    eligible: bool
    failures: tuple[str, ...]


@dataclass(frozen=True)
class SelectionResult:
    agent_id: str | None
    reason: SelectionReason
    candidates: tuple[SelectionCandidate, ...]


class CapabilityRegistry:
    def __init__(self, agents: list[AgentProfile] | tuple[AgentProfile, ...] = ()):
        self._agents = tuple(agents)

    def select(
        self,
        required_capabilities: set[CapabilityName] | frozenset[CapabilityName],
        runtimes: Mapping[str, RuntimeStatus],
        role: str | None = None,
    ) -> SelectionResult:
        candidates = []
        for profile in sorted(self._agents, key=lambda item: item.agent_id):
            failures: list[str] = []
            status = runtimes.get(profile.runtime_id)
            if status is None or status.status is not RuntimeState.READY:
                failures.append("runtime_not_ready")
            if role is not None and profile.role != role:
                failures.append("role_mismatch")
            evidence = [profile.capabilities.get(capability) for capability in required_capabilities]
            if any(item is None for item in evidence):
                failures.append("capability_missing")
            elif any(item.confidence is CapabilityConfidence.UNKNOWN for item in evidence):
                failures.append("capability_unknown")
            score = None
            if not failures:
                capability_fit = sum(item.score or 0.0 for item in evidence) / len(evidence) if evidence else 0.0
                confidence_fit = sum(
                    1.0 if item.confidence is CapabilityConfidence.VERIFIED else 0.5
                    for item in evidence
                ) / len(evidence) if evidence else 0.0
                readiness = 1.0
                history = profile.historical_success_rate if profile.historical_success_rate is not None else 0.0
                score = capability_fit * 0.6 + confidence_fit * 0.25 + readiness * 0.1 + history * 0.05
            candidates.append(SelectionCandidate(profile.agent_id, profile.runtime_id, score, not failures, tuple(failures)))

        eligible = [candidate for candidate in candidates if candidate.eligible]
        if not eligible:
            return SelectionResult(None, SelectionReason.NO_CAPABLE_AGENT, tuple(candidates))
        selected = sorted(eligible, key=lambda item: (-item.score, item.agent_id))[0]
        return SelectionResult(selected.agent_id, SelectionReason.SELECTED, tuple(candidates))
