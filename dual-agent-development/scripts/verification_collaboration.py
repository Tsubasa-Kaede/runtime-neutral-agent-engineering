"""Verification collaboration: ledger-backed tester->reviewer loop (10H-G2).

Composes AFTER CollaborationSession, not inside it. Reads the tester's and
reviewer's upstream facts from the append-only ledger via the pure
handoff_input_for projection, drives each role with the SAME shared
budget/usage/loop_guard, emits TestPacket/ReviewPacket and appends their
REQUEST envelopes with a fresh correlation per hop so the ledger invariants
hold. It runs its own raw-output scan over the open dict packet fields
(failures/findings) before enveloping — the structured_packets cleaner does
not reject stdout/stderr there. Failures are honest terminal statuses, never
success-wrapped, and never silently downgrade to single-agent.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from collaboration_handoff import handoff_input_for
from collaboration_packet import (
    CollaborationPacket,
    CollaborationPayloadType,
    new_correlation_id,
)
from content_safety import packet_has_unsafe_content, sanitize_trace
from execution_observation import ExecutionEventType
from external_runtime import ExternalAgentRequest, InvocationStatus
from handoff_context import HandoffError
from structured_packets import ReviewPacket, TestPacket, serialize_packet
from task_budget import BudgetExceeded

TESTER_INSTRUCTION = (
    "You are the tester for one small, read-only task. "
    "The implementation packet below is your complete input contract. "
    "Return ONLY a JSON object with exactly these keys: "
    "task_id, role, tests_run, tests_passed, tests_failed, failures, "
    "coverage_or_validation, remaining_risks. "
    'task_id must equal the packet task_id and role must be "tester". '
    "No prose, no markdown fences. Do not modify files or run commands. "
    "Type rules: tests_run, tests_passed, tests_failed, failures, "
    "coverage_or_validation and remaining_risks must each be a JSON "
    "array (use [] when empty); never a number or a bare string. "
    "You may report zero tests honestly with empty arrays.\n\n"
    "Implementation packet:\n"
)

REVIEWER_INSTRUCTION = (
    "You are the reviewer for one small, read-only task. "
    "The implementation and test packets below are your complete input "
    "contract. Return ONLY a JSON object with exactly these keys: "
    "task_id, role, status, findings, severity, affected_files, "
    "required_changes, acceptance_criteria_status. "
    'task_id must equal the packet task_id and role must be "reviewer". '
    "No prose, no markdown fences. Do not modify files or run commands. "
    "Type rules: findings, severity, affected_files, "
    "required_changes and acceptance_criteria_status must each be a "
    "JSON array (use [] when empty); never a number or a bare string.\n\n"
    "Implementation packet:\n"
)


class VerificationStatus(str, Enum):
    SUCCESS = "SUCCESS"
    TESTER_INVOKE_FAILED = "TESTER_INVOKE_FAILED"
    TESTER_PACKET_INVALID = "TESTER_PACKET_INVALID"
    REVIEWER_INVOKE_FAILED = "REVIEWER_INVOKE_FAILED"
    REVIEWER_PACKET_INVALID = "REVIEWER_PACKET_INVALID"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    LOOP_GUARD_REJECTED = "LOOP_GUARD_REJECTED"
    MISSING_HANDOFF = "MISSING_HANDOFF"


@dataclass(frozen=True)
class VerificationOutcome:
    status: VerificationStatus
    task_id: str
    provenance: str
    test_envelope: CollaborationPacket | None = None
    review_envelope: CollaborationPacket | None = None
    traces: tuple = ()


class VerificationCollaboration:
    """Drives tester then reviewer off the ledger, reusing shared budget/guard."""

    def __init__(self, adapters, budget, usage, loop_guard, state=None):
        self.adapters = dict(adapters)
        self.budget = budget
        self.usage = usage
        self.loop_guard = loop_guard
        from collaboration_state import SharedCollaborationState
        self.state = state if state is not None else SharedCollaborationState()

    def run(self, task_id, tester_address, reviewer_address, architect_address,
            provenance="OFFLINE", correlation_id="", observation_emit=None):
        traces: list = []

        def outcome(status, test_envelope=None, review_envelope=None):
            return VerificationOutcome(
                status=status, task_id=task_id, provenance=provenance,
                test_envelope=test_envelope, review_envelope=review_envelope,
                traces=tuple(traces))

        def observe(event_type, stage, runtime_id, status, reason,
                    duration_ms=None):
            """R7-D2: verification 半场 STAGE/INVOCATION/HANDOFF 的唯一
            权威发射缝。correlation 用 dual 半场结果（执行级关联）。"""
            if observation_emit is None:
                return
            correlation = correlation_id or "UNCORRELATED"
            observation_emit(event_type, stage=stage, runtime_id=runtime_id,
                             status=status, reason=reason,
                             correlation_id=correlation,
                             duration_ms=duration_ms)

        def runtime_of(address):
            import json as _json
            try:
                parsed = _json.loads(address)
            except (TypeError, ValueError):
                return "UNKNOWN"
            if isinstance(parsed, list) and parsed and isinstance(parsed[0], str):
                return parsed[0]
            return "UNKNOWN"

        def invocation_finished(stage, runtime_id, result):
            trace = getattr(result, "trace", None)
            duration_ms = (getattr(trace, "duration_ms", None)
                           if trace is not None else None)
            status = getattr(result, "status", None)
            value = getattr(status, "value", "UNKNOWN") if status is not None else "UNKNOWN"
            observe(ExecutionEventType.INVOCATION_FINISHED, stage, runtime_id,
                    value, value, duration_ms=duration_ms)

        # -- tester stage -------------------------------------------------
        try:
            impl_packet = handoff_input_for(self.state, task_id, "tester")
        except HandoffError:
            self.state = self.state.append_failure(task_id, status="MISSING_HANDOFF")
            return outcome(VerificationStatus.MISSING_HANDOFF)
        if self.loop_guard.check(task_id, "test", tester_address) != "ALLOW":
            self.state = self.state.append_failure(task_id, status="LOOP_GUARD_REJECTED")
            return outcome(VerificationStatus.LOOP_GUARD_REJECTED)
        try:
            self.budget.reserve_call(self.usage, "test")
        except BudgetExceeded:
            self.state = self.state.append_failure(task_id, status="BUDGET_EXHAUSTED")
            return outcome(VerificationStatus.BUDGET_EXHAUSTED)
        self.loop_guard.record(task_id, "test", tester_address)
        observe(ExecutionEventType.STAGE_STARTED, "tester",
                runtime_of(tester_address), "STARTED", "ALLOW")
        observe(ExecutionEventType.INVOCATION_STARTED, "tester",
                runtime_of(tester_address), "STARTED", "ALLOW")
        tester_result = self.adapters[tester_address].invoke(ExternalAgentRequest(
            task_id=task_id, prompt=TESTER_INSTRUCTION + serialize_packet(impl_packet),
            agent_id=tester_address, role="tester",
            timeout_seconds=self.budget.timeout_seconds or 120))
        invocation_finished("tester", runtime_of(tester_address),
                            tester_result)
        if tester_result.trace is not None:
            traces.append(sanitize_trace(tester_result.trace))
            # 10H-I：观测到的 token 用量进入既有核算；"unknown"
            # 原样透传（record_tokens 只累计观测到的非负整数）。
            self.usage.record_tokens(
                tester_result.trace.input_tokens,
                tester_result.trace.output_tokens, self.budget)
        if tester_result.status is not InvocationStatus.SUCCESS:
            self.loop_guard.record_failure(task_id, "test", tester_address,
                                           "tester_invoke_failed")
            self.state = self.state.append_failure(task_id, status="TESTER_INVOKE_FAILED")
            return outcome(VerificationStatus.TESTER_INVOKE_FAILED)
        test_packet = _parse_packet(tester_result.output, TestPacket, task_id)
        if test_packet is None or packet_has_unsafe_content(test_packet):
            self.state = self.state.append_failure(task_id, status="TESTER_PACKET_INVALID")
            return outcome(VerificationStatus.TESTER_PACKET_INVALID)
        test_envelope = self._envelope(
            task_id, tester_address, reviewer_address,
            CollaborationPayloadType.TEST, test_packet, "tester", "reviewer", provenance)
        self.state = self.state.append_envelope(task_id, test_envelope, "REQUEST", "DELIVERED")
        # R7-D2: HANDOFF（tester->reviewer）在真实 envelope 发送缝发射；
        # 事件只携带生命周期事实，绝不复制 packet 内容。
        observe(ExecutionEventType.HANDOFF, "tester",
                runtime_of(reviewer_address), "DELIVERED", "DELIVERED")

        # -- reviewer stage ------------------------------------------------
        try:
            review_inputs = handoff_input_for(self.state, task_id, "reviewer")
        except HandoffError:
            self.state = self.state.append_failure(task_id, status="MISSING_HANDOFF")
            return outcome(VerificationStatus.MISSING_HANDOFF, test_envelope)
        if self.loop_guard.check(task_id, "review", reviewer_address) != "ALLOW":
            self.state = self.state.append_failure(task_id, status="LOOP_GUARD_REJECTED")
            return outcome(VerificationStatus.LOOP_GUARD_REJECTED, test_envelope)
        try:
            self.budget.reserve_call(self.usage, "review")
        except BudgetExceeded:
            self.state = self.state.append_failure(task_id, status="BUDGET_EXHAUSTED")
            return outcome(VerificationStatus.BUDGET_EXHAUSTED, test_envelope)
        self.loop_guard.record(task_id, "review", reviewer_address)
        observe(ExecutionEventType.STAGE_STARTED, "reviewer",
                runtime_of(reviewer_address), "STARTED", "ALLOW")
        observe(ExecutionEventType.INVOCATION_STARTED, "reviewer",
                runtime_of(reviewer_address), "STARTED", "ALLOW")
        reviewer_result = self.adapters[reviewer_address].invoke(ExternalAgentRequest(
            task_id=task_id,
            prompt=REVIEWER_INSTRUCTION + serialize_packet(review_inputs[1])
                  + "\n\nTest packet:\n" + serialize_packet(review_inputs[2]),
            agent_id=reviewer_address, role="reviewer",
            timeout_seconds=self.budget.timeout_seconds or 120))
        invocation_finished("reviewer", runtime_of(reviewer_address),
                             reviewer_result)
        if reviewer_result.trace is not None:
            traces.append(sanitize_trace(reviewer_result.trace))
            # 10H-I：同 tester 阶段的用量传播语义。
            self.usage.record_tokens(
                reviewer_result.trace.input_tokens,
                reviewer_result.trace.output_tokens, self.budget)
        if reviewer_result.status is not InvocationStatus.SUCCESS:
            self.loop_guard.record_failure(task_id, "review", reviewer_address,
                                           "reviewer_invoke_failed")
            self.state = self.state.append_failure(task_id, status="REVIEWER_INVOKE_FAILED")
            return outcome(VerificationStatus.REVIEWER_INVOKE_FAILED, test_envelope)
        review_packet = _parse_packet(reviewer_result.output, ReviewPacket, task_id)
        if review_packet is None or packet_has_unsafe_content(review_packet):
            self.state = self.state.append_failure(task_id, status="REVIEWER_PACKET_INVALID")
            return outcome(VerificationStatus.REVIEWER_PACKET_INVALID, test_envelope)
        review_envelope = self._envelope(
            task_id, reviewer_address, architect_address,
            CollaborationPayloadType.REVIEW, review_packet, "reviewer", "architect",
            provenance)
        self.state = self.state.append_envelope(task_id, review_envelope, "REQUEST", "DELIVERED")
        return outcome(VerificationStatus.SUCCESS, test_envelope, review_envelope)

    @staticmethod
    def _envelope(task_id, source, target, payload_type, payload, source_role,
                  target_role, provenance):
        return CollaborationPacket(
            correlation_id=new_correlation_id(), task_id=task_id,
            source_agent=source, target_agent=target,
            source_role=source_role, target_role=target_role,
            payload_type=payload_type, payload=payload, provenance=provenance)


def _parse_packet(output, packet_class, task_id):
    text = output if isinstance(output, str) else ""
    text = text.strip()
    if text.startswith("```"):
        first_newline = text.find("\n")
        text = text[first_newline + 1:] if first_newline != -1 else ""
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    import json
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    data["task_id"] = task_id  # orchestration owns task identity
    try:
        return packet_class.from_dict(data)
    except (ValueError, TypeError, KeyError):
        return None
