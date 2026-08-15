"""Provider-neutral task loop and escalation guard."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json


class GuardDecision(str):
    ALLOW = "ALLOW"
    DUPLICATE_TASK = "DUPLICATE_TASK"
    REPEATED_FAILURE = "REPEATED_FAILURE"
    CYCLE_DETECTED = "CYCLE_DETECTED"
    MAX_ITERATIONS = "MAX_ITERATIONS"
    MAX_ESCALATIONS = "MAX_ESCALATIONS"


@dataclass(frozen=True)
class GuardEvent:
    task_id: str
    stage: str
    agent_id: str


class LoopGuard:
    def __init__(self, max_iterations: int = 3, max_escalations: int = 2, max_history: int = 8):
        self.max_iterations = max_iterations
        self.max_escalations = max_escalations
        self.max_history = max_history
        self._visited: set[tuple[str, str, str]] = set()
        self._history: list[GuardEvent] = []
        self._failures: set[str] = set()
        self._iterations = 0
        self._escalations = 0

    @property
    def failure_signatures(self) -> frozenset[str]:
        return frozenset(self._failures)

    def check(self, task_id: str, stage: str, agent_id: str, failure_signature: str | None = None) -> str:
        if self._escalations >= self.max_escalations:
            return GuardDecision.MAX_ESCALATIONS
        if self._iterations >= self.max_iterations:
            return GuardDecision.MAX_ITERATIONS
        key = (task_id, stage, agent_id)
        if failure_signature is not None and self._signature(task_id, stage, agent_id, failure_signature) in self._failures:
            return GuardDecision.REPEATED_FAILURE
        if len(self._history) >= 4 and self._history[-4:] == [
            GuardEvent(task_id, stage, agent_id),
            self._history[-3],
            self._history[-2],
            self._history[-1],
        ]:
            return GuardDecision.CYCLE_DETECTED
        if key in self._visited:
            return GuardDecision.DUPLICATE_TASK
        return GuardDecision.ALLOW

    def record(self, task_id: str, stage: str, agent_id: str) -> None:
        key = (task_id, stage, agent_id)
        self._visited.add(key)
        self._history.append(GuardEvent(task_id, stage, agent_id))
        self._history = self._history[-self.max_history:]

    def record_failure(self, task_id: str, stage: str, agent_id: str, failure_signature: str) -> None:
        self._failures.add(self._signature(task_id, stage, agent_id, failure_signature))

    def record_iteration(self) -> None:
        self._iterations += 1

    def record_escalation(self) -> None:
        self._escalations += 1

    @staticmethod
    def _signature(task_id: str, stage: str, agent_id: str, failure_signature: str) -> str:
        # Hash only structured fields; raw diagnostic text is never retained.
        payload = json.dumps([task_id, stage, agent_id, failure_signature.split(":", 1)[0]], separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()
