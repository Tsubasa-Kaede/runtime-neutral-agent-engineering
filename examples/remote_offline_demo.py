"""Remote Collaboration, offline — one declared agent, one real process
boundary, one task packet round trip. No runtime, no credentials, no
network.

Purpose
    The 30-second first experience of Remote Collaboration: declare an
    agent (identity + role + runtime binding) whose remote side is the
    scripted adapter module ``scripted_coder.py``, compose a remote
    session with one call, send one task packet, receive one result
    packet, close. The remote side runs in a real child process and the
    exchange goes through the same production path as a real run — only
    the adapter is scripted, and the summary says so.

Prerequisite
    Python 3.10+ and a source checkout of this repository (examples are
    repository examples — they are not part of the installed wheel).

Usage
    python examples/remote_offline_demo.py

    On success stdout is exactly one closed JSON line; diagnostics go to
    stderr. Any failure exits non-zero with a one-line reason and never
    fabricates a success summary.
"""
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from agent_identity import AgentIdentity, AgentRuntimeBinding
from agent_manifest import AgentManifest, AgentRegistry
from collaboration_packet import (
    CollaborationPacket,
    CollaborationPayloadType,
)
from remote_agent_composition import (
    build_remote_session,
    importable_adapter_factory,
)
from remote_contract import RemoteEnvelopeStatus
from structured_packets import ArchitecturePacket

LOCAL_ADDRESS = "agent:you:architect"
AGENT_ID = "scripted-coder"
TASK_ID = "T-DEMO-1"

# The adapter declaration must not contradict the runtime binding: the
# runtime/provider/model facts have to be identical in both places.
RUNTIME_IDENTITY = ("scripted-runtime", None, None, "default")
ADAPTER_PROFILE = {
    "agent_id": AGENT_ID,
    "runtime": "scripted-runtime",
    "provider": None,
    "model": None,
    "role": "coder",
    "capabilities": frozenset(),
}


def declare_agent() -> AgentRegistry:
    """Step 1 — declare: one agent, one role, one scripted runtime."""
    registry = AgentRegistry()
    registry.register(AgentManifest(
        binding=AgentRuntimeBinding(
            agent=AgentIdentity(AGENT_ID),
            runtime_identity=RUNTIME_IDENTITY),
        declared_roles=("coder",),
        adapter_factory=importable_adapter_factory(
            "scripted_coder",
            "build",
            profile=ADAPTER_PROFILE,
            # source_path: scripted_coder.py lives here in the examples
            # directory, not in the package directory the remote process
            # already knows — declare this directory so the child process
            # can import the module.
            source_path=str(Path(__file__).resolve().parent),
        ),
    ))
    return registry


def build_task_packet(correlation_id: str, remote_address: str):
    """The task packet: the remote side's complete input contract."""
    return CollaborationPacket(
        correlation_id=correlation_id,
        task_id=TASK_ID,
        source_agent=LOCAL_ADDRESS,
        target_agent=remote_address,
        source_role="architect",
        target_role="coder",
        payload_type=CollaborationPayloadType.ARCHITECTURE,
        payload=ArchitecturePacket(
            task_id=TASK_ID,
            role="architect",
            goal=("Add a slug helper function",),
            constraints=("No new dependency",),
            architecture=("one module, one function",),
            interfaces=({},),
            implementation_steps=({},),
            acceptance_criteria=("offline round trip completes",),
            risks=({},),
        ),
    )


def main() -> int:
    # Step 2 — compose: declaration to a usable remote session.
    session, facts = build_remote_session(
        declare_agent(), AGENT_ID, "coder", LOCAL_ADDRESS)
    try:
        # Step 3 — send: one task packet across the process boundary.
        receipt = session.send(
            build_task_packet(session.correlation_id, facts.remote_address))
        if receipt.status is not RemoteEnvelopeStatus.DELIVERED:
            # DELIVERED means the remote process received the packet —
            # anything less is an honest failure, never a success.
            print(f"delivery failed: {receipt.status.value}",
                  file=sys.stderr)
            return 1
        # Step 4 — receive: one result packet under the same contract.
        reply = session.receive()
        if reply is None:
            print("no reply arrived", file=sys.stderr)
            return 1
        packet = reply.payload
        summary = {
            "status": "SUCCESS",
            "adapter": "scripted (offline demonstration)",
            "agent_id": facts.agent_id,
            "role": facts.role,
            "remote_address": facts.remote_address,
            "runtime_identity": list(facts.runtime_identity),
            "flow": ["declare", "compose", "send", "receive", "close"],
            "task_id": TASK_ID,
            "reply_task_id": packet.payload.task_id,
            "reply_summary": packet.payload.implementation_summary,
            "provenance": packet.provenance,
        }
        if summary["reply_task_id"] != TASK_ID:
            print("reply task id does not match the task", file=sys.stderr)
            return 1
    finally:
        # Step 5 — close: end the interaction; the remote process is
        # reaped by the session's close.
        session.close()
    print(json.dumps(summary, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # one closed reason line, never a fake success
        print(f"offline demo failed: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        raise SystemExit(1)
