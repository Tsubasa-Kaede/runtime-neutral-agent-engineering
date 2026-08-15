"""Production facade: the single four-stage collaboration entrypoint (10H-K).

Wraps an already-configured CollaborationOrchestrator (which owns SINGLE/DUAL
routing) and gates VerificationCollaboration (tester+reviewer) strictly on a
DUAL success. The facade re-injects the SAME shared budget/usage/loop_guard
instances and reads the shared ledger from the orchestrator; it never mints
its own. It returns exactly one closed FacadeResult — never the raw
orchestrator/session/verification outcomes — so a CLI caller cannot leak
envelopes, traces, or open-dict packet fields. The isinstance/status branch is
a convenience that keeps verification from running after an upstream DUAL
failure; the append-only ledger's MISSING_HANDOFF is the invariant that keeps
downstream success from ever being fabricated even if a caller bypasses the
facade. Missing tester/reviewer capability is an honest terminal, never a
silent two-stage success.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from collaboration_session import CollaborationOutcome, CollaborationStatus, collab_agent_address
from content_safety import contains_unsafe_content
from execution_engine import ExecutionResult
from mode_gate import Mode
from verified_selection_bridge import VerifiedSelectionBridge
from verified_stage_selector import _ROLE_REQUIREMENTS
from verification_collaboration import VerificationCollaboration

# Role-address suffix vocabulary (collab_agent_address uses tester/reviewer).
_ADDRESS_ROLE = {"test": "tester", "review": "reviewer"}


def _assert_clean(value: Any, field_name: str) -> None:
    if isinstance(value, str) and contains_unsafe_content(value):
        raise ValueError(f"{field_name} must not contain unsafe content")


@dataclass(frozen=True)
class FacadeResult:
    """Closed, secret-free projection of one facade run. Safe to serialize."""

    status: str
    mode: str
    path: str
    task_id: str
    provenance: str
    stages: tuple
    failure_category: str
    safe_summary: dict

    def __post_init__(self) -> None:
        for name in ("status", "mode", "path", "task_id", "provenance", "failure_category"):
            _assert_clean(getattr(self, name), name)
        for stage in self.stages:
            _assert_clean(stage, "stage")
        for key, value in self.safe_summary.items():
            _assert_clean(str(key), "safe_summary key")
            if isinstance(value, (str, int)):
                _assert_clean(str(value), "safe_summary value")


class ProductionFacade:
    """Composes orchestrator (architect+coder) -> verification (tester+reviewer)."""

    def __init__(self, orchestrator, verification_adapters, pool, current_health,
                 budget, usage, loop_guard):
        self._orchestrator = orchestrator
        self._verification_adapters = dict(verification_adapters)
        self._pool = pool
        self._current_health = dict(current_health)
        self._budget = budget
        self._usage = usage
        self._loop_guard = loop_guard
        self._final_state = orchestrator.state

    @property
    def state(self):
        """The latest shared ledger (four-stage after a successful run)."""
        return self._final_state

    def run(self, task_id, task, prompt, mode=Mode.AUTO, provenance="OFFLINE"):
        mode = Mode(mode)
        outcome = self._orchestrator.run(task_id, task, prompt, mode, provenance)
        self._final_state = self._orchestrator.state

        if isinstance(outcome, ExecutionResult):
            path = "OFF" if mode is Mode.OFF else "SINGLE"
            status = outcome.status.value
            return FacadeResult(
                status=status, mode=mode.value, path=path, task_id=task_id,
                provenance=provenance, stages=(), failure_category=status,
                safe_summary={"task_id": task_id, "provenance": provenance,
                              "stage_counts": {}})

        # outcome is a CollaborationOutcome (DUAL architect+coder)
        if outcome.status is not CollaborationStatus.SUCCESS:
            return FacadeResult(
                status=outcome.status.value, mode=mode.value, path="DUAL",
                task_id=task_id, provenance=provenance, stages=(),
                failure_category=outcome.status.value,
                safe_summary={"task_id": task_id, "provenance": provenance,
                              "stage_counts": {}})

        tester = self._role_candidate("test")
        reviewer = self._role_candidate("review")
        if tester is None or reviewer is None:
            return FacadeResult(
                status="NO_VERIFICATION_CAPABILITY", mode=mode.value, path="DUAL",
                task_id=task_id, provenance=provenance,
                stages=("architect", "coder"),
                failure_category="NO_VERIFICATION_CAPABILITY",
                safe_summary={"task_id": task_id, "provenance": provenance,
                              "stage_counts": {"architect": 1, "coder": 1}})

        architect_address = outcome.request_envelope.source_agent
        tester_address = collab_agent_address(self._identity(tester), _ADDRESS_ROLE["test"])
        reviewer_address = collab_agent_address(self._identity(reviewer), _ADDRESS_ROLE["review"])

        verification = VerificationCollaboration(
            self._verification_adapters, self._budget, self._usage, self._loop_guard,
            state=self._orchestrator.state)
        voutcome = verification.run(task_id, tester_address, reviewer_address,
                                    architect_address, provenance)
        self._final_state = verification.state

        if voutcome.status.value == "SUCCESS":
            stages = ("architect", "coder", "tester", "reviewer")
            stage_counts = {"architect": 1, "coder": 1, "tester": 1, "reviewer": 1}
            failure = ""
        else:
            stages = ("architect", "coder", "tester") if "TESTER" in voutcome.status.value else (
                ("architect", "coder") if "REVIEWER" in voutcome.status.value else ("architect", "coder"))
            stage_counts = {"architect": 1, "coder": 1}
            failure = voutcome.status.value
        return FacadeResult(
            status=voutcome.status.value, mode=mode.value, path="FOUR_STAGE",
            task_id=task_id, provenance=provenance, stages=stages,
            failure_category=failure,
            safe_summary={"task_id": task_id, "provenance": provenance,
                          "stage_counts": stage_counts})

    def _role_candidate(self, role):
        candidate_set = VerifiedSelectionBridge().candidates_for(
            self._pool, self._current_health, role, _ROLE_REQUIREMENTS[role])
        candidates = candidate_set.candidates
        return candidates[0] if candidates else None

    @staticmethod
    def _identity(candidate):
        return (candidate.runtime_id, candidate.provider_id,
                candidate.model_id, candidate.config_fingerprint)
