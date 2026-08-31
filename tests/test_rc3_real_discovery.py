"""RC-3 Task D: REAL local runtime discovery through the registry chain.

Gated. Proves Registry -> DiscoverySource -> RuntimeCandidateDiscovery ->
the REAL machine, then the discovered candidate through the existing health
layer, evidence reuse (or exactly one qualification), and the Verified Pool
admission gate. Machine-honest: descriptors only cover runtimes actually
installed here, health is whatever the machine says, and every count is
derived from the run itself — no fabricated status objects anywhere.
Structured closed summaries only — no prompts, raw output, credentials or
environment secrets are ever printed.
"""
import json
import os
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from discovery_bootstrap import bootstrap_runtime_session
from runtime_adapter_registry import AdapterDescriptor, AdapterRegistry

CAPS_ALL = ("architecture", "coding", "review", "testing")
SECRET_MARKERS = ("token", "secret", "api_key", "authorization", "bearer", "stdout", "stderr")

# Same protected set as the committed REAL smoke: G13 must guard credentials
# and configuration across every real invocation this session performs.
HOME = Path.home()
PROTECTED = (
    HOME / ".claude" / ".credentials.json",
    HOME / ".claude.json",
    HOME / ".claude" / "settings.json",
    HOME / ".codex" / "auth.json",
    HOME / ".codex" / "config.toml",
)


def real_registry():
    """Registry over the machine's REAL installed runtimes only.

    Runtime-specific knowledge lives here (the adapter-layer boundary);
    no fake adapters, no fabricated results — descriptors whose runtime is
    absent simply never register. Registration means ONLY "the adapter is
    implemented and the production chain knows this runtime"; it never
    means READY, never means VERIFIED, and never fabricates qualification
    evidence (health, validation and admission stay owned by their own
    layers downstream of discovery)."""
    from claude_code_adapter import ClaudeCodeAdapter
    from codex_adapter import CodexAdapter
    from gemini_adapter import GeminiAdapter
    from pi_adapter import PiAdapter
    from tiny_agents_adapter import TinyAgentsAdapter

    registry = AdapterRegistry()

    claude = ClaudeCodeAdapter.from_environment()
    if claude is not None:
        registry.register(AdapterDescriptor(
            runtime_id="claude-cli", provider_id="anthropic", model_id=None,
            runtime_type="coding-agent", display_name="Claude Code",
            adapter_factory=lambda: claude,
            config_fingerprint="installed",
        ))

    pi = PiAdapter.from_environment()
    if pi is not None:
        registry.register(AdapterDescriptor(
            runtime_id="pi-cli", provider_id="deepseek", model_id=None,
            runtime_type="coding-agent", display_name="Pi",
            adapter_factory=lambda: pi,
            config_fingerprint="installed",
        ))

    codex = CodexAdapter.from_environment()
    if codex is not None:
        registry.register(AdapterDescriptor(
            runtime_id="codex-cli", provider_id="openai", model_id=None,
            runtime_type="coding-agent", display_name="Codex",
            adapter_factory=lambda: codex,
            config_fingerprint="installed",
        ))

    gemini = GeminiAdapter.from_environment()
    if gemini is not None:
        registry.register(AdapterDescriptor(
            runtime_id="gemini-cli", provider_id="google", model_id=None,
            runtime_type="coding-agent", display_name="Gemini CLI",
            adapter_factory=lambda: gemini,
            config_fingerprint="installed",
        ))

    tiny = TinyAgentsAdapter.from_environment()
    if tiny is not None:
        registry.register(AdapterDescriptor(
            runtime_id="tiny-agents", provider_id="tiny-agents", model_id=None,
            runtime_type="coding-agent", display_name="tiny-agents",
            adapter_factory=lambda: tiny,
            config_fingerprint="installed",
        ))
    return registry


def real_qualifier():
    """Sanctioned REAL qualification through the existing G1-G14 chain."""
    from real_validation_executor import run_real_validation

    def qualify(instance):
        validation, _executor = run_real_validation(
            instance, instance.probe, timeout_seconds=300.0,
            protected_paths=PROTECTED,
            experiment_id="rc3-taskd-qualification",
        )
        return validation
    return qualify


def safe_summary(session):
    rows = []
    for entry in session.entries:
        rows.append({
            "runtime_id": entry.runtime_id,
            "discovery_available": entry.discovery_available,
            "health": entry.health_status,
            "validation": entry.validation_status,
            "provenance": entry.provenance,
            "capabilities": len(entry.capabilities),
            "admitted": entry.admitted,
            "reason": entry.reason,
        })
    return {
        "entries": rows,
        "pool": [list(identity) for identity in session.pool.identities()],
        "qualification_count": session.qualification_count,
    }


class RC3TaskDRealDiscoveryTests(unittest.TestCase):
    def setUp(self):
        if os.environ.get("RUN_REAL_PROVIDER_TESTS", "") != "1":
            self.skipTest("RUN_REAL_PROVIDER_TESTS != 1")

    def test_real_discovery_health_qualification_pool_chain(self):
        registry = real_registry()
        session = bootstrap_runtime_session(
            registry, evidence={}, qualifier=real_qualifier(),
            required_capabilities=CAPS_ALL,
        )
        summary = safe_summary(session)
        print("REAL_DISCOVERY:", json.dumps(summary, sort_keys=True))

        by_id = {row["runtime_id"]: row for row in summary["entries"]}
        # Case 1 — claude: FOUND on the real machine.
        self.assertIn("claude-cli", by_id)
        claude = by_id["claude-cli"]
        self.assertTrue(claude["discovery_available"])
        # Health is whatever the real machine says — no fabrication.
        self.assertIn(claude["health"], ("READY", "AUTH_REQUIRED", "UNAVAILABLE", "ERROR"))
        if claude["health"] != "READY":
            self.assertFalse(claude["admitted"])
            return
        # FOUND -> READY -> one qualification per READY runtime -> VERIFIED+REAL -> pool.
        ready_rows = [row for row in summary["entries"]
                      if row["discovery_available"] and row["health"] == "READY"]
        self.assertEqual(summary["qualification_count"], len(ready_rows))
        self.assertEqual(claude["validation"], "VERIFIED")
        self.assertEqual(claude["provenance"], "REAL")
        self.assertEqual(claude["capabilities"], len(CAPS_ALL))
        self.assertTrue(claude["admitted"])
        # Pool membership is exactly the admitted rows; claude's identity is exact.
        self.assertEqual(len(summary["pool"]),
                         sum(1 for row in summary["entries"] if row["admitted"]))
        self.assertIn(["claude-cli", "anthropic", None, "installed"], summary["pool"])

        # Case 2/3 — other runtimes report their real state; never admitted
        # unless genuinely VERIFIED+REAL on this machine.
        for runtime_id, row in by_id.items():
            if runtime_id == "claude-cli":
                continue
            if not row["discovery_available"] or row["health"] != "READY":
                self.assertFalse(row["admitted"], runtime_id)

        # Safety: the whole summary is closed and secret-free.
        surface = json.dumps(summary).lower()
        for marker in SECRET_MARKERS:
            self.assertNotIn(marker, surface)

    def test_evidence_reuse_skips_qualification(self):
        # Second session over the SAME machine with the evidence produced by
        # the first: qualification count must drop to zero (reuse).
        registry = real_registry()
        first = bootstrap_runtime_session(
            registry, evidence={}, qualifier=real_qualifier(),
            required_capabilities=CAPS_ALL)
        admitted = {identity: first.evidence[identity]
                    for identity in first.pool.identities()}
        if not admitted:
            self.skipTest("no admitted runtime on this machine")
        second = bootstrap_runtime_session(
            registry, evidence=dict(admitted), qualifier=real_qualifier(),
            required_capabilities=CAPS_ALL)
        summary = safe_summary(second)
        print("REAL_REUSE:", json.dumps(summary, sort_keys=True))
        self.assertEqual(summary["qualification_count"], 0)
        self.assertEqual(len(summary["pool"]), len(admitted))
        self.assertEqual({tuple(item) for item in summary["pool"]},
                         set(admitted))
        surface = json.dumps(summary).lower()
        for marker in SECRET_MARKERS:
            self.assertNotIn(marker, surface)


if __name__ == "__main__":
    unittest.main()
