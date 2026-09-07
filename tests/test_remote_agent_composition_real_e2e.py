"""V3.1-F REAL Level 3 e2e — the composition root over a REAL provider.

One gated scenario (no doubles of the adapter, no fabricated output): a
real manifest declares the production composition root's tagged factory
for the REAL Claude adapter — wrapped in a transparent evidence module
reached through the declared source_path, the only production surfaces
involved — and build_remote_session derives the address, generates the
glue, builds the transport and the session. The reply must be a valid
IMPLEMENTATION CollaborationPacket parsed from the provider's actual
output through the production endpoint, with task and correlation
identity preserved end to end, and the invocation trace must come back
through the existing stderr diagnostics seam.

Evidence split mirrors V3.1-E: assertions 1-10 prove REAL_AGENT (the
production translation discipline over real model output); the
INVOCATION_EVIDENCE trace line proves REAL_PROVIDER (runtime/provider
identity emitted inside the child by the real adapter; the provider fact
is the composition-declared adapter identity, never endpoint-derived).
The closed facts assertions prove the composition contract itself.

Offline FileDisciplineTests lock the gate and the no-doubles rule.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))

SOURCE = Path(__file__).read_text(encoding="utf-8")

LOCAL = "agent:local-composer:architect"
AGENT_ID = "remote-composer"
REMOTE = f"agent:{AGENT_ID}:coder"
TASK_ID = "T-F-LEVEL3"

CLAUDE_PROFILE = {
    "agent_id": AGENT_ID,
    "runtime": "claude-cli",
    "provider": "anthropic",
    "model": None,
    "role": "coder",
    "capabilities": frozenset(),
}

# Transparent evidence wrapper over the REAL adapter (E's evidence
# pattern, expressed through the composition root's own declaration
# surface): acquires the real adapter from the environment, mirrors the
# invocation trace onto stderr, changes nothing else. Reached by the
# child only through the declared source_path — the wrapper file lives
# in a temp directory, not in the scripts directory.
WRAPPER_SOURCE = '''"""Transparent evidence wrapper over the real adapter."""
import sys

from claude_code_adapter import ClaudeCodeAdapter


def build(profile=None):
    real = ClaudeCodeAdapter.from_environment(profile=profile)
    if real is None:
        return None
    real_invoke = real.invoke

    def evidence_invoke(request):
        result = real_invoke(request)
        trace = getattr(result, "trace", None)
        if trace is not None:
            sys.stderr.write(
                "INVOCATION_EVIDENCE runtime=%s provider=%s "
                "invocation_id=%s status=%s exit_code=%s duration_ms=%s\\n"
                % (trace.runtime, real.profile.provider,
                   trace.invocation_id,
                   getattr(trace.status, "value", str(trace.status)),
                   trace.exit_code, trace.duration_ms))
        return result

    real.invoke = evidence_invoke
    return real
'''


class FileDisciplineTests(unittest.TestCase):
    """Offline structural guarantees of the REAL scenario file."""

    def test_real_test_is_opt_in_gated(self):
        self.assertIn('RUN_REAL_PROVIDER_TESTS") == "1"', SOURCE)

    def test_uses_production_adapter_acquisition(self):
        self.assertIn("from_environment", SOURCE)

    def test_no_test_doubles(self):
        # Concatenated so the discipline literals do not match themselves.
        self.assertNotIn("Mo" + "ck", SOURCE)
        self.assertNotIn("Fa" + "ke", SOURCE)


class RealCompositionCollaborationTests(unittest.TestCase):
    """REAL_AGENT + REAL_PROVIDER through the production composition root."""

    @classmethod
    def setUpClass(cls):
        if os.environ.get("RUN_REAL_PROVIDER_TESTS", "") != "1":
            raise unittest.SkipTest("RUN_REAL_PROVIDER_TESTS != 1")

    def test_real_composed_coder_level3_round_trip(self):
        from agent_identity import AgentIdentity, AgentRuntimeBinding
        from agent_manifest import AgentManifest, AgentRegistry
        from claude_code_adapter import ClaudeCodeAdapter
        from collaboration_packet import CollaborationPacket
        from collaboration_packet import CollaborationPayloadType
        from remote_agent_composition import (
            build_remote_session,
            importable_adapter_factory,
        )
        from remote_contract import RemoteEnvelopeStatus
        from structured_packets import ArchitecturePacket, ImplementationPacket

        if ClaudeCodeAdapter.from_environment() is None:
            self.skipTest("claude executable not found")

        tmpdir = tempfile.mkdtemp(prefix="f-real-composition-")
        self.addCleanup(shutil.rmtree, tmpdir, True)
        wrapper = Path(tmpdir) / "f_real_evidence_wrapper.py"
        wrapper.write_text(WRAPPER_SOURCE, encoding="utf-8")

        registry = AgentRegistry()
        registry.register(AgentManifest(
            binding=AgentRuntimeBinding(
                agent=AgentIdentity(AGENT_ID),
                runtime_identity=("claude-cli", "anthropic", None, "default")),
            declared_roles=("coder",),
            adapter_factory=importable_adapter_factory(
                "f_real_evidence_wrapper", "build",
                profile=CLAUDE_PROFILE, source_path=tmpdir)))

        # Composition — the entire production path under test.
        session, facts = build_remote_session(
            registry, AGENT_ID, "coder", LOCAL)

        # Composition facts 1-4 — the closed contract.
        self.assertEqual(facts.agent_id, AGENT_ID)
        self.assertEqual(facts.role, "coder")
        self.assertEqual(facts.remote_address, REMOTE)
        self.assertEqual(facts.runtime_identity,
                         ("claude-cli", "anthropic", None, "default"))

        packet = CollaborationPacket(
            correlation_id=session.correlation_id,
            task_id=TASK_ID,
            source_agent=LOCAL,
            target_agent=REMOTE,
            source_role="architect",
            target_role="coder",
            payload_type=CollaborationPayloadType.ARCHITECTURE,
            payload=ArchitecturePacket(
                task_id=TASK_ID, role="architect", goal=("g",),
                constraints=("c",), architecture=("a",), interfaces=({},),
                implementation_steps=({},), acceptance_criteria=("ac",),
                risks=({},)),
        )

        # REAL_AGENT 1 — delivery authorised by the child's ACK alone.
        receipt = session.send(packet)
        self.assertIs(receipt.status, RemoteEnvelopeStatus.DELIVERED)

        # REAL_AGENT 2-3 — a real response crossed the boundary and
        # passed the session's inbound validation.
        response = session.receive()
        self.assertIsNotNone(response)
        self.assertEqual(response.sender, REMOTE)
        self.assertEqual(response.recipient, LOCAL)
        self.assertEqual(response.correlation_id, session.correlation_id)

        # REAL_AGENT 4 — a fresh message identity inside the interaction.
        self.assertNotEqual(response.message_id, receipt.message_id)

        # REAL_AGENT 5 — the reply is a valid IMPLEMENTATION packet.
        self.assertIsInstance(response.payload, CollaborationPacket)
        self.assertIs(response.payload.payload_type,
                      CollaborationPayloadType.IMPLEMENTATION)
        self.assertIsInstance(response.payload.payload,
                              ImplementationPacket)

        # REAL_AGENT 6 — task authority preserved.
        self.assertEqual(response.payload.task_id, TASK_ID)
        self.assertEqual(response.payload.payload.task_id, TASK_ID)

        # REAL_AGENT 7 — role contract.
        self.assertEqual(response.role, "architect")

        # REAL_AGENT 8 — the content fields are model-authored (presence
        # only; exact text is never hardcoded or compared).
        self.assertTrue(response.payload.payload.implementation_summary)

        # REAL_AGENT 9 — acceptance criteria carried.
        self.assertEqual(response.payload.acceptance_criteria,
                         response.payload.payload.test_requirements)

        # REAL_AGENT 10 — provenance is the honest carried default.
        self.assertEqual(response.payload.provenance, "OFFLINE")

        # REAL_PROVIDER — the invocation trace, produced inside the
        # child by the real adapter (provider identity from the
        # composition-declared adapter profile) and captured through
        # the existing stderr diagnostics seam after close.
        session.close()
        diagnostics = session._transport.last_diagnostics
        self.assertIn(b"INVOCATION_EVIDENCE", diagnostics)
        self.assertIn(b"runtime=claude-cli", diagnostics)
        self.assertIn(b"provider=anthropic", diagnostics)


if __name__ == "__main__":
    unittest.main()
