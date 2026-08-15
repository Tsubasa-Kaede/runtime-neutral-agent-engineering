"""Deterministic, provider-neutral fallback selection policy."""
from __future__ import annotations

from enum import Enum
from typing import Mapping, Iterable

from capability_registry import AgentProfile, CapabilityConfidence, CapabilityName
from runtime_status import RuntimeState, RuntimeStatus


class FallbackReason(str, Enum):
    SELECTED = "SELECTED"
    NO_FALLBACK_AGENT = "NO_FALLBACK_AGENT"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"


class FallbackPolicy:
    def __init__(self, agents: Iterable[AgentProfile]):
        self._agents = tuple(agents)

    def select(
        self,
        failed_agent_id: str,
        runtimes: Mapping[str, RuntimeStatus],
        required_capabilities: set[CapabilityName] | frozenset[CapabilityName],
        *,
        budget_available: bool = True,
    ):
        if not budget_available:
            return type("FallbackResult", (), {"agent_id": None, "reason": FallbackReason.BUDGET_EXHAUSTED})()
        candidates = []
        for agent in self._agents:
            if agent.agent_id == failed_agent_id:
                continue
            status = runtimes.get(agent.runtime_id)
            if status is None or status.status is not RuntimeState.READY:
                continue
            evidence = [agent.capabilities.get(capability) for capability in required_capabilities]
            if any(item is None or item.confidence is CapabilityConfidence.UNKNOWN for item in evidence):
                continue
            score = sum(item.score or 0.0 for item in evidence) / len(evidence) if evidence else 0.0
            score = score * 0.9 + (agent.historical_success_rate or 0.0) * 0.1
            candidates.append((score, agent.agent_id))
        if not candidates:
            return type("FallbackResult", (), {"agent_id": None, "reason": FallbackReason.NO_FALLBACK_AGENT})()
        selected = sorted(candidates, key=lambda item: (-item[0], item[1]))[0][1]
        return type("FallbackResult", (), {"agent_id": selected, "reason": FallbackReason.SELECTED})()
