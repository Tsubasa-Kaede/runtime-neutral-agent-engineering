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
from collaboration_policy import PolicyConstrainedAssigner
from content_safety import contains_unsafe_content
from execution_engine import ExecutionResult
from execution_observation import (
    ExecutionEvent,
    ExecutionEventType,
    ObservationError,
)
from mode_gate import Mode
from role_assignment import ConvergingAssigner
from verified_selection_bridge import VerifiedSelectionBridge
from verified_stage_selector import _ROLE_REQUIREMENTS
from verification_collaboration import VerificationCollaboration

# Role-address suffix vocabulary (collab_agent_address uses tester/reviewer).
_ADDRESS_ROLE = {"test": "tester", "review": "reviewer"}

# R7-D2: 非协作路径（OFF/SINGLE）没有 correlation —— 事件 correlation_id
# 的诚实占位（封闭常量，非空、无信息泄漏）。
_UNCORRELATED = "UNCORRELATED"

# 终态事件的 runtime 作用域占位：无协作发生（OFF/SINGLE 路径）时没有
# 任何 runtime 拥有该终态事实 —— 封闭常量如实声明这一点。
_ORCHESTRATION_SCOPE = "ORCHESTRATION"


def _assert_clean(value: Any, field_name: str) -> None:
    if isinstance(value, str) and contains_unsafe_content(value):
        raise ValueError(f"{field_name} must not contain unsafe content")


def _observation_channel(task_id, sink):
    """R7-D2: execution-local 观察通道 —— 一次 facade.run 一个闭包。

    序号 execution-scoped（从 0 严格递增，跨 execution 零共享、零模块
    级状态）；sink 故障被 try/except 隔离（观察失败绝不影响执行流，
    绝不 retry/reinvoke）。返回 emit(event_type, stage=..., runtime_id=...,
    status=..., reason=..., correlation_id=..., duration_ms=None) callable；
    事件构造期的 ObservationError 同样被隔离（旁路契约问题不是执行
    问题）。sink=None 时返回 None（零发射、零行为漂移）。

    runtime_id=None 语义：协作级事件（TERMINAL）不点名单一 runtime ——
    通道回填本次 execution 的主 runtime（DECISION 记录的 architect
    runtime）；没有协作发生时用 _ORCHESTRATION_SCOPE 占位。"""
    counter = [0]
    primary_runtime = [None]

    def emit(event_type, *, stage, runtime_id, status, reason,
             correlation_id, duration_ms=None):
        if event_type is ExecutionEventType.DECISION:
            primary_runtime[0] = runtime_id
        resolved_runtime = (runtime_id if runtime_id is not None
                            else (primary_runtime[0] or _ORCHESTRATION_SCOPE))
        sequence = counter[0]
        counter[0] += 1
        try:
            event = ExecutionEvent(
                event_type=event_type, sequence=sequence,
                task_id=task_id, correlation_id=correlation_id,
                stage=stage, runtime_id=resolved_runtime, status=status,
                reason=reason, duration_ms=duration_ms)
        except ObservationError:
            return  # 契约拒绝：旁路观察放弃该事件，执行流不受影响
        try:
            sink.on_event(event)
        except Exception:
            return  # sink 故障：隔离，绝不传播进执行控制流
    return emit


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
                 budget, usage, loop_guard, role_assigner=None):
        self._orchestrator = orchestrator
        self._verification_adapters = dict(verification_adapters)
        self._pool = pool
        self._current_health = dict(current_health)
        self._budget = budget
        self._usage = usage
        self._loop_guard = loop_guard
        # 10H-F: tester/reviewer assignment joins the policy layer; the
        # default (ConvergingAssigner) reproduces the historical
        # candidates[0] fold verbatim, so absent injection nothing changes.
        self._role_assigner = role_assigner if role_assigner is not None else ConvergingAssigner()
        self._final_state = orchestrator.state

    @property
    def state(self):
        """The latest shared ledger (four-stage after a successful run)."""
        return self._final_state

    def run(self, task_id, task, prompt, mode=Mode.AUTO, provenance="OFFLINE",
            observation_sink=None, policy=None):
        # R7-D2: observation sink 是旁路通道。sink=None（默认）时 emit 为
        # None，全部发射点短路 —— 执行行为与 D1 之前逐字一致。sink 给定时
        # 也绝不改变返回值/异常/重试/回退/预算/ledger/终态（emit 内部隔离
        # sink 故障与契约拒绝）。
        emit = _observation_channel(task_id, observation_sink) \
            if observation_sink is not None else None
        mode = Mode(mode)
        outcome = self._orchestrator.run(task_id, task, prompt, mode,
                                         provenance,
                                         observation_emit=emit, policy=policy)
        self._final_state = self._orchestrator.state

        if isinstance(outcome, ExecutionResult):
            path = "OFF" if mode is Mode.OFF else "SINGLE"
            status = outcome.status.value
            if emit is not None:
                emit(ExecutionEventType.TERMINAL, stage=path, runtime_id=None,
                     status=status, reason=status,
                     correlation_id=_UNCORRELATED)
            return FacadeResult(
                status=status, mode=mode.value, path=path, task_id=task_id,
                provenance=provenance, stages=(), failure_category=status,
                safe_summary={"task_id": task_id, "provenance": provenance,
                              "stage_counts": {}})

        # outcome is a CollaborationOutcome (DUAL architect+coder)
        # R7-A3: the ON-mode no-capable-agent terminal returns a bare
        # string status (DUAL_NO_CAPABLE_AGENT is deliberately not a
        # session status); normalize it for the closed FacadeResult —
        # the status value and every downstream field are unchanged.
        outcome_status = getattr(outcome.status, "value", outcome.status)
        if outcome_status != "SUCCESS":
            # correlation：session outcome 携带（no-capable 路径为空串，
            # 用诚实占位常量）。
            failure_correlation = outcome.correlation_id or _UNCORRELATED
            if emit is not None:
                emit(ExecutionEventType.TERMINAL, stage="DUAL",
                     runtime_id=None, status=outcome_status,
                     reason=outcome_status,
                     correlation_id=failure_correlation)
            return FacadeResult(
                status=outcome_status, mode=mode.value, path="DUAL",
                task_id=task_id, provenance=provenance, stages=(),
                failure_category=outcome_status,
                safe_summary={"task_id": task_id, "provenance": provenance,
                              "stage_counts": {}})

        # 10H-F: tester/reviewer are chosen JOINTLY through the injected
        # role-assignment policy (default = historical candidates[0]
        # fold), mirroring the orchestrator's dual-role assignment. A
        # per-run policy (R7-A2) switches to a PolicyConstrainedAssigner
        # for this run only; policy=None keeps the historical assigner.
        candidate_sets = {
            role: VerifiedSelectionBridge().candidates_for(
                self._pool, self._current_health, role, _ROLE_REQUIREMENTS[role])
            for role in ("test", "review")
        }
        assigner = self._role_assigner if policy is None else \
            PolicyConstrainedAssigner(policy)
        assignment = assigner.assign(candidate_sets, "COMPLEX")
        tester = assignment.assignments.get("test")
        reviewer = assignment.assignments.get("review")
        # R7-A3: the verification-half assignment reason surfaces through the
        # existing DECISION channel (no new record type, additive only). The
        # path/mode/complexity mirror the orchestrator's DUAL decision so the
        # ledger stays a homogeneous decision sequence; without a per-run
        # policy the record is emitted with the assignment reason verbatim,
        # which keeps the DUAL/FOUR_STAGE outcomes identical — only the
        # ledger gains one DECISION record.
        if tester is None or reviewer is None:
            self._orchestrator._state = self._orchestrator.state.append_decision(
                task_id, mode=mode.value, complexity="COMPLEX", path="DUAL",
                runtime_mode="",
                reason=f"NO_VERIFICATION_CAPABILITY/ROLE_ASSIGNMENT={assignment.reason}")
            self._final_state = self._orchestrator._state
            if emit is not None:
                emit(ExecutionEventType.TERMINAL, stage="DUAL",
                     runtime_id=None,
                     status="NO_VERIFICATION_CAPABILITY",
                     reason="NO_VERIFICATION_CAPABILITY",
                     correlation_id=outcome.correlation_id or _UNCORRELATED)
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
                                    architect_address, provenance,
                                    correlation_id=outcome.correlation_id,
                                    observation_emit=emit)
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
        if emit is not None:
            emit(ExecutionEventType.TERMINAL, stage="FOUR_STAGE",
                 runtime_id=None, status=voutcome.status.value,
                 reason=voutcome.status.value,
                 correlation_id=outcome.correlation_id or _UNCORRELATED)
        return FacadeResult(
            status=voutcome.status.value, mode=mode.value, path="FOUR_STAGE",
            task_id=task_id, provenance=provenance, stages=stages,
            failure_category=failure,
            safe_summary={"task_id": task_id, "provenance": provenance,
                          "stage_counts": stage_counts})

    @staticmethod
    def _identity(candidate):
        return (candidate.runtime_id, candidate.provider_id,
                candidate.model_id, candidate.config_fingerprint)
