"""Collaboration session: the minimal real dual-agent seam (Phase 10H-D).

Runs one Architect -> Coder -> Architect collaboration over the remote
transport contract with injected agent adapters. Every stage passes the
shared budget and loop guard exactly like the execution layer; the coder
consumes the serialized ArchitecturePacket as its complete input contract
(never raw task text); outputs become structured packets or honest
failures — never fabricated packets, never success-wrapped failures.
Raw process output stays out of every packet and outcome.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum

from collaboration_packet import (
    CollaborationPacket,
    CollaborationPayloadType,
    PacketValidationError,
    new_correlation_id,
    serialize_packet,
)
from external_runtime import ExternalAgentRequest, InvocationStatus
from remote_transport import RemoteDeliveryStatus
from structured_packets import ArchitecturePacket, ImplementationPacket
from task_budget import BudgetExceeded
from verified_selection_bridge import agent_id_for

_SECRET_MARKERS = ("token", "secret", "api_key", "authorization", "bearer", "stdout", "stderr")

ARCHITECT_INSTRUCTION = (
    "You are the architect for one small, read-only design task. "
    "Return ONLY a JSON object with exactly these keys: "
    "task_id, role, goal, constraints, architecture, interfaces, "
    "implementation_steps, acceptance_criteria, risks. "
    'task_id and role are strings (role must be "architect"). '
    "goal, constraints, architecture, acceptance_criteria are arrays of "
    "strings. interfaces, implementation_steps, risks are arrays of "
    "objects. No prose, no markdown fences. "
    "Do not modify files, run commands, or touch any repository.\n\n"
    "Task: "
)

CODER_INSTRUCTION = (
    "You are the coder for one small, read-only implementation task. "
    "The architecture packet below is your complete input contract. "
    "Return ONLY a JSON object with exactly these keys: "
    "task_id, role, changed_files, implementation_summary, "
    "implementation_details, assumptions, unresolved_items, "
    "test_requirements. task_id must equal the packet task_id; "
    'role must be "coder". task_id, role and implementation_summary are '
    "strings. changed_files, implementation_details, assumptions, "
    "unresolved_items, test_requirements are arrays of strings. "
    "No prose, no markdown fences. Do not modify files or run commands.\n\n"
    "Architecture packet:\n"
)


class CollaborationStatus(str, Enum):
    SUCCESS = "SUCCESS"
    ARCHITECT_INVOKE_FAILED = "ARCHITECT_INVOKE_FAILED"
    ARCHITECT_PACKET_INVALID = "ARCHITECT_PACKET_INVALID"
    CODER_INVOKE_FAILED = "CODER_INVOKE_FAILED"
    CODER_PACKET_INVALID = "CODER_PACKET_INVALID"
    TRANSPORT_FAILED = "TRANSPORT_FAILED"
    CORRELATION_MISMATCH = "CORRELATION_MISMATCH"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    LOOP_GUARD_REJECTED = "LOOP_GUARD_REJECTED"


def collab_agent_address(identity, role: str) -> str:
    """Role-qualified projection: the sanctioned helper over an extended
    tuple — the same runtime identity serving one collaboration role."""
    return agent_id_for(tuple(identity) + (role,))


def _assert_outcome_text_clean(value, field_name: str) -> None:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    lowered = value.lower()
    for marker in _SECRET_MARKERS:
        if marker in lowered:
            raise ValueError(f"{field_name} must not contain secret-shaped content")


@dataclass(frozen=True)
class CollaborationOutcome:
    """Honest result of one collaboration run; envelopes only on progress."""

    status: CollaborationStatus
    task_id: str
    correlation_id: str
    runtime_mode: str = ""
    request_envelope: CollaborationPacket | None = None
    reply_envelope: CollaborationPacket | None = None
    receipts: tuple = ()
    traces: tuple = ()

    def __post_init__(self) -> None:
        for name in ("task_id", "correlation_id", "runtime_mode"):
            _assert_outcome_text_clean(getattr(self, name), name)


# Conservative scalar->single-item-list normalization for known packet
# list fields (a bare string is semantically equal to a one-item array);
# every other shape deviation stays rejected by from_dict.
_LIST_FIELDS = (
    "goal", "constraints", "architecture", "interfaces",
    "implementation_steps", "acceptance_criteria", "risks",
    "changed_files", "implementation_details", "assumptions",
    "unresolved_items", "test_requirements", "findings", "severity",
    "affected_files", "required_changes", "acceptance_criteria_status",
    "tests_run", "tests_passed", "tests_failed", "failures",
    "coverage_or_validation", "remaining_risks",
)


def _normalize(data: dict) -> dict:
    for key in _LIST_FIELDS:
        if key in data and isinstance(data[key], str):
            data[key] = [data[key]]
    return data


def _packet_from_output(output, packet_class, task_id):
    text = output if isinstance(output, str) else ""
    text = text.strip()
    if text.startswith("```"):
        first_newline = text.find("\n")
        text = text[first_newline + 1:] if first_newline != -1 else ""
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    # The orchestration owns task identity: whatever id the model echoes,
    # the packet belongs to this task by construction.
    data["task_id"] = task_id
    try:
        return packet_class.from_dict(_normalize(data))
    except (PacketValidationError, TypeError, KeyError, ValueError):
        return None


class CollaborationSession:
    """Orchestrates one A->B->A collaboration over the transport contract."""

    def __init__(self, transport, adapters, budget, usage, loop_guard):
        self.transport = transport
        self.adapters = dict(adapters)
        self.budget = budget
        self.usage = usage
        self.loop_guard = loop_guard

    def run(self, task_id, task, architect_address, coder_address,
            correlation_id=None, provenance="OFFLINE", runtime_mode=""):
        correlation = correlation_id or new_correlation_id()
        traces: list = []
        receipts: list = []

        def outcome(status, request_envelope=None, reply_envelope=None):
            return CollaborationOutcome(
                status=status, task_id=task_id, correlation_id=correlation,
                runtime_mode=runtime_mode, request_envelope=request_envelope,
                reply_envelope=reply_envelope,
                receipts=tuple(receipts), traces=tuple(traces))

        # --- architect stage ---
        if self.loop_guard.check(task_id, "architect", architect_address) != "ALLOW":
            return outcome(CollaborationStatus.LOOP_GUARD_REJECTED)
        try:
            self.budget.reserve_call(self.usage, "architect")
        except BudgetExceeded:
            return outcome(CollaborationStatus.BUDGET_EXHAUSTED)
        self.loop_guard.record(task_id, "architect", architect_address)
        architect_result = self.adapters[architect_address].invoke(ExternalAgentRequest(
            task_id=task_id,
            prompt=ARCHITECT_INSTRUCTION + f'task_id must be exactly "{task_id}".\n\nTask: ' + task,
            agent_id=architect_address, role="architect",
            timeout_seconds=self.budget.timeout_seconds or 120,
        ))
        if architect_result.trace is not None:
            traces.append(architect_result.trace)
        if architect_result.status is not InvocationStatus.SUCCESS:
            self.loop_guard.record_failure(task_id, "architect", architect_address,
                                            "architect_invoke_failed")
            return outcome(CollaborationStatus.ARCHITECT_INVOKE_FAILED)
        arch_packet = _packet_from_output(architect_result.output, ArchitecturePacket, task_id)
        if arch_packet is None:
            return outcome(CollaborationStatus.ARCHITECT_PACKET_INVALID)
        try:
            envelope = CollaborationPacket(
                correlation_id=correlation, task_id=task_id,
                source_agent=architect_address, target_agent=coder_address,
                source_role="architect", target_role="coder",
                payload_type=CollaborationPayloadType.ARCHITECTURE,
                payload=arch_packet,
                acceptance_criteria=arch_packet.acceptance_criteria,
                provenance=provenance,
            )
        except (PacketValidationError, ValueError):
            return outcome(CollaborationStatus.ARCHITECT_PACKET_INVALID)
        receipt = self.transport.send(envelope)
        receipts.append(receipt)
        if receipt.status is not RemoteDeliveryStatus.DELIVERED:
            return outcome(CollaborationStatus.TRANSPORT_FAILED, envelope)

        # --- coder stage ---
        coder_envelope = self.transport.receive(coder_address)
        if coder_envelope is None:
            return outcome(CollaborationStatus.TRANSPORT_FAILED, envelope)
        if coder_envelope.correlation_id != correlation:
            return outcome(CollaborationStatus.CORRELATION_MISMATCH, envelope)
        if self.loop_guard.check(task_id, "coder", coder_address) != "ALLOW":
            return outcome(CollaborationStatus.LOOP_GUARD_REJECTED, envelope)
        try:
            self.budget.reserve_call(self.usage, "coder")
        except BudgetExceeded:
            return outcome(CollaborationStatus.BUDGET_EXHAUSTED, envelope)
        self.loop_guard.record(task_id, "coder", coder_address)
        coder_result = self.adapters[coder_address].invoke(ExternalAgentRequest(
            task_id=task_id,
            prompt=CODER_INSTRUCTION + serialize_packet(coder_envelope.payload),
            agent_id=coder_address, role="coder",
            timeout_seconds=self.budget.timeout_seconds or 120,
        ))
        if coder_result.trace is not None:
            traces.append(coder_result.trace)
        if coder_result.status is not InvocationStatus.SUCCESS:
            self.loop_guard.record_failure(task_id, "coder", coder_address,
                                           "coder_invoke_failed")
            return outcome(CollaborationStatus.CODER_INVOKE_FAILED, envelope)
        impl_packet = _packet_from_output(coder_result.output, ImplementationPacket, task_id)
        if impl_packet is None:
            return outcome(CollaborationStatus.CODER_PACKET_INVALID, envelope)
        try:
            reply = CollaborationPacket(
                correlation_id=correlation, task_id=task_id,
                source_agent=coder_address, target_agent=architect_address,
                source_role="coder", target_role="architect",
                payload_type=CollaborationPayloadType.IMPLEMENTATION,
                payload=impl_packet,
                acceptance_criteria=impl_packet.test_requirements,
                provenance=provenance,
            )
        except (PacketValidationError, ValueError):
            return outcome(CollaborationStatus.CODER_PACKET_INVALID, envelope)
        reply_receipt = self.transport.send(reply)
        receipts.append(reply_receipt)
        if reply_receipt.status is not RemoteDeliveryStatus.DELIVERED:
            return outcome(CollaborationStatus.TRANSPORT_FAILED, envelope, reply)
        final = self.transport.receive(architect_address)
        if final is None:
            return outcome(CollaborationStatus.TRANSPORT_FAILED, envelope, reply)
        if final.correlation_id != correlation:
            return outcome(CollaborationStatus.CORRELATION_MISMATCH, envelope, reply)
        return outcome(CollaborationStatus.SUCCESS, envelope, reply)
