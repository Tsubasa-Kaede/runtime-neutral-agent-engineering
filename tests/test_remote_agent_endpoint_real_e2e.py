"""V3.1-E REAL Level 3 e2e — the endpoint over a REAL provider.

One gated scenario (no doubles, no fabricated output): a
RemoteAgentSession drives the production SubprocessStdioEnvelopeTransport
whose child runs the production run_endpoint loop over the production
ClaudeCodeAdapter, which spawns the real Claude CLI and therefore a real
provider. The reply must be a valid IMPLEMENTATION CollaborationPacket
parsed from the provider's actual output, with task and correlation
identity preserved end to end, and the invocation trace must come back
through the existing stderr diagnostics seam (B2 constructed with
stderr=PIPE captures it into last_diagnostics at close — zero B2
changes).

Evidence split: assertions 1-10 prove REAL_AGENT (production translation
discipline over real model output); assertion 11 proves REAL_PROVIDER
(trace identity emitted inside the child by the real adapter). PID/pipe/
ACK facts alone would prove nothing here — D already proved those with a
fixture — so they are not counted as agent or provider evidence.

Offline FileDisciplineTests lock the gate and the no-doubles rule.
"""
import os
import subprocess
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))

SOURCE = Path(__file__).read_text(encoding="utf-8")

LOCAL = "agent:arch-level3:architect"
REMOTE = "agent:coder-level3:coder"
TASK_ID = "T-LEVEL3"

# The composition glue (this test IS the composition root for E): it
# wires only production parts — the real adapter acquired from the
# environment, the production endpoint loop — and adds one evidence
# wrapper that mirrors the invocation trace onto stderr. The repo path
# is embedded in the source because the transport passes only a minimal
# environment whitelist to the child.
GLUE = (
    "import sys\n"
    "sys.path.insert(0, " + repr(str(SCRIPTS)) + ")\n"
    "from claude_code_adapter import ClaudeCodeAdapter\n"
    "from external_runtime import RuntimeProfile\n"
    "from remote_agent_endpoint import run_endpoint\n"
    "adapter = ClaudeCodeAdapter.from_environment(\n"
    "    profile=RuntimeProfile('coding-agent', 'claude-cli',\n"
    "                           'anthropic', None, 'coder', frozenset()))\n"
    "if adapter is None:\n"
    "    raise SystemExit(3)\n"
    "real_invoke = adapter.invoke\n"
    "def evidence_invoke(request):\n"
    "    result = real_invoke(request)\n"
    "    trace = getattr(result, 'trace', None)\n"
    "    if trace is not None:\n"
    "        sys.stderr.write(\n"
    "            'INVOCATION_EVIDENCE runtime=%s provider=%s "
    "invocation_id=%s status=%s exit_code=%s duration_ms=%s\\n'\n"
    "            % (trace.runtime, adapter.profile.provider,\n"
    "               trace.invocation_id,\n"
    "               getattr(trace.status, 'value', str(trace.status)),\n"
    "               trace.exit_code, trace.duration_ms))\n"
    "    return result\n"
    "adapter.invoke = evidence_invoke\n"
    "run_endpoint(adapter, " + repr(REMOTE) +
    ", sys.stdin.buffer, sys.stdout.buffer)\n"
)


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


class RealEndpointCollaborationTests(unittest.TestCase):
    """REAL_AGENT + REAL_PROVIDER over the full production chain."""

    @classmethod
    def setUpClass(cls):
        if os.environ.get("RUN_REAL_PROVIDER_TESTS", "") != "1":
            raise unittest.SkipTest("RUN_REAL_PROVIDER_TESTS != 1")

    def test_real_coder_endpoint_level3_round_trip(self):
        from claude_code_adapter import ClaudeCodeAdapter
        from collaboration_packet import (
            CollaborationPacket,
            CollaborationPayloadType,
        )
        from remote_agent_session import RemoteAgentSession
        from remote_subprocess_transport import (
            SubprocessStdioEnvelopeTransport,
        )
        from remote_contract import RemoteEnvelope, RemoteEnvelopeStatus
        from structured_packets import ArchitecturePacket, ImplementationPacket

        if ClaudeCodeAdapter.from_environment() is None:
            self.skipTest("claude executable not found")

        transport = SubprocessStdioEnvelopeTransport(
            [sys.executable, "-c", GLUE], timeout_seconds=300,
            stderr=subprocess.PIPE)
        session = RemoteAgentSession(LOCAL, REMOTE, transport)
        self.addCleanup(session.close)

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

        # REAL_AGENT 1 — delivery authorised by the child's ACK, asserted
        # as its own transport-level fact, never substituted below.
        receipt = session.send(packet)
        self.assertIs(receipt.status, RemoteEnvelopeStatus.DELIVERED)

        # REAL_AGENT 2-3 — a real response crossed the boundary and
        # passed the session's inbound validation.
        response = session.receive()
        self.assertIsNotNone(response)
        self.assertIsInstance(response, RemoteEnvelope)
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

        # REAL_AGENT 6 — task authority preserved (the override
        # discipline, not model cooperation).
        self.assertEqual(response.payload.task_id, TASK_ID)
        self.assertEqual(response.payload.payload.task_id, TASK_ID)

        # REAL_AGENT 7 — role contract: reply role is the requester's
        # declared source role.
        self.assertEqual(response.role, "architect")

        # REAL_AGENT 8 — the content fields are model-authored (presence
        # only; exact text is never hardcoded or compared).
        self.assertTrue(response.payload.payload.implementation_summary)

        # REAL_AGENT 9 — acceptance criteria carried from the parsed
        # packet's test requirements.
        self.assertEqual(response.payload.acceptance_criteria,
                         response.payload.payload.test_requirements)

        # REAL_AGENT 10 — provenance is the honest carried default: a
        # real call under an undeclared composition stays OFFLINE.
        self.assertEqual(response.payload.provenance, "OFFLINE")

        # REAL_PROVIDER — the invocation trace, produced inside the
        # child by the real adapter and captured through the existing
        # stderr diagnostics seam after close.
        session.close()
        diagnostics = transport.last_diagnostics
        self.assertIn(b"INVOCATION_EVIDENCE", diagnostics)
        self.assertIn(b"runtime=claude-cli", diagnostics)
        self.assertIn(b"provider=anthropic", diagnostics)


if __name__ == "__main__":
    unittest.main()
