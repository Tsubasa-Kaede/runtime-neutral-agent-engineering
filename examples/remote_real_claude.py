"""Remote Collaboration, REAL — the same five-step flow as the offline
demo, with the remote side executing the real Claude Code CLI.

Purpose
    Take what the offline demo proved (declare -> compose -> send ->
    receive -> close across a real process boundary) and swap exactly one
    thing: the declared adapter is the real Claude adapter, so the result
    packet is real model output parsed through the production packet
    contract.

Prerequisite
    The Claude Code CLI installed on PATH and logged in through its own
    flow (see the README "Integration" section). A source checkout of
    this repository. No test gate is involved — this is not a test.

Usage
    python examples/remote_real_claude.py

    Success prints exactly one closed JSON line with
    "adapter": "claude-cli (REAL)". If the prerequisite is missing the
    example exits 2 with the reason and never falls back to the offline
    demo. If the exchange fails it exits 1 with the receipt status —
    a delivery receipt is never reported as execution success.
"""
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from agent_identity import AgentIdentity, AgentRuntimeBinding
from agent_manifest import AgentManifest, AgentRegistry
from claude_code_adapter import ClaudeCodeAdapter
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
AGENT_ID = "claude-coder"
TASK_ID = "T-REAL-1"

RUNTIME_IDENTITY = ("claude-cli", "anthropic", None, "default")
ADAPTER_PROFILE = {
    "agent_id": AGENT_ID,
    "runtime": "claude-cli",
    "provider": "anthropic",
    "model": None,
    "role": "coder",
    "capabilities": frozenset(),
}


def check_prerequisite() -> int:
    """Refuse honestly — never fall back — when the runtime is absent."""
    if ClaudeCodeAdapter.from_environment() is not None:
        return 0
    print("prerequisite missing: Claude Code CLI not found on PATH",
          file=sys.stderr)
    print("next step: install the Claude Code CLI and log in through its "
          "own flow, then retry; see the README 'Integration' section",
          file=sys.stderr)
    print("this example never falls back to the offline demo",
          file=sys.stderr)
    return 2


def declare_agent() -> AgentRegistry:
    """Same declaration shape as the offline demo — one swapped adapter."""
    registry = AgentRegistry()
    registry.register(AgentManifest(
        binding=AgentRuntimeBinding(
            agent=AgentIdentity(AGENT_ID),
            runtime_identity=RUNTIME_IDENTITY),
        declared_roles=("coder",),
        adapter_factory=importable_adapter_factory(
            "claude_code_adapter",
            "ClaudeCodeAdapter.from_environment",
            profile=ADAPTER_PROFILE,
        ),
    ))
    return registry


def build_task_packet(correlation_id: str, remote_address: str):
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
            goal=("Describe how to add a slug helper function",),
            constraints=("No new dependency", "No file changes"),
            architecture=("one short answer",),
            interfaces=({},),
            implementation_steps=({},),
            acceptance_criteria=("a valid implementation packet returns",),
            risks=({},),
        ),
    )


def main() -> int:
    failure = check_prerequisite()
    if failure:
        return failure
    session, facts = build_remote_session(
        declare_agent(), AGENT_ID, "coder", LOCAL_ADDRESS)
    try:
        receipt = session.send(
            build_task_packet(session.correlation_id, facts.remote_address))
        if receipt.status is not RemoteEnvelopeStatus.DELIVERED:
            # The remote process failed before completing the exchange
            # (bad construction, CLI crash, not logged in). Its stderr was
            # captured by the transport; first verify the CLI works in
            # this terminal (claude --version, then one trivial prompt).
            print(f"delivery failed: {receipt.status.value} — the remote "
                  "process failed; check that the Claude CLI works here "
                  "(claude --version, then a trivial prompt)",
                  file=sys.stderr)
            return 1
        reply = session.receive()
        if reply is None:
            print("no reply arrived", file=sys.stderr)
            return 1
        packet = reply.payload
        summary = {
            "status": "SUCCESS",
            "adapter": "claude-cli (REAL)",
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
        session.close()
    print(json.dumps(summary, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # one closed reason line, never a fake success
        print(f"real demo failed: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        raise SystemExit(1)
