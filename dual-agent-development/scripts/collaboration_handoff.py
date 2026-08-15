"""Read-only handoff projection over the append-only collaboration ledger.

Reconstructs a role's upstream structured input from a SharedCollaborationState
by payload_type only — never by direction (the coder's implementation may be
recorded as a REPLY), never by target_role (not a top-level record field) and
never by correlation_id. The task_id is the primary key; the latest matching
record by ledger-assigned sequence wins; each read returns a fresh decode of
the stored wire, never a shared packet instance. The role vocabulary is
closed; a missing upstream fact is a MISSING_HANDOFF error, never a silent
None, and an unknown role is UNKNOWN_STAGE. No raw output or reasoning ever
leaves the ledger through this projection — only structured packet payloads.
"""
from __future__ import annotations

from handoff_context import HandoffError

# role -> ordered payload_type requirements; empty means "no upstream".
_ROLE_PAYLOAD_TYPES = {
    "architect": (),
    "coder": ("ARCHITECTURE",),
    "tester": ("IMPLEMENTATION",),
    "reviewer": ("ARCHITECTURE", "IMPLEMENTATION", "TEST"),
}


def handoff_input_for(state, task_id: str, role: str):
    """Return the role's upstream packet(s), or raise HandoffError honestly.

    architect -> None; coder -> ArchitecturePacket; tester ->
    ImplementationPacket; reviewer -> (ArchitecturePacket,
    ImplementationPacket, TestPacket). Raises UNKNOWN_STAGE for any role
    outside the closed vocabulary and MISSING_HANDOFF when a required
    upstream fact is absent.
    """
    required = _ROLE_PAYLOAD_TYPES.get(role)
    if required is None:
        raise HandoffError("UNKNOWN_STAGE")
    if not required:
        return None  # architect: no upstream handoff
    latest: dict = {}
    for record in state.history(task_id):
        if record.payload_type in required:
            latest[record.payload_type] = record  # history is sequence-ascending
    if any(payload_type not in latest for payload_type in required):
        raise HandoffError("MISSING_HANDOFF")
    packets = [latest[payload_type].envelope().payload for payload_type in required]
    if len(packets) == 1:
        return packets[0]
    return tuple(packets)
