"""R4: Production Registry Completion — offline registry boundary tests.

These tests run WITHOUT the REAL gate (RUN_REAL_PROVIDER_TESTS unset)
and prove the registry boundary only:

    adapter implemented -> registered descriptor -> discovery can see it

Registration means none of: READY, AUTHENTICATED, VERIFIED, REAL
provenance, pool admission, role assignment. Every test here is offline:
descriptors are built from the machine's actual PATH (runtimes may be
absent — that is an honest outcome, never a failure), and no adapter
invoke() is ever called.
"""
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from runtime_adapter_registry import AdapterDescriptor, AdapterRegistry
from runtime_discovery import DiscoverySource, RuntimeCandidateDiscovery

# Same registry construction the REAL-gated chain uses (single import).
from test_rc3_real_discovery import real_registry

CAPS_ALL = ("architecture", "coding", "review", "testing")


def offline_registry():
    """real_registry() with every executable forced PRESENT offline.

    The registration boundary is data-level: each adapter's
    from_environment is patched to return a real instance built against a
    fake executable path, so descriptors register WITHOUT the runtimes
    installed and WITHOUT any subprocess being spawned. This proves the
    registry can describe implemented adapters even when the machine has
    none of them — adapter implemented is not runtime installed.
    """
    from claude_code_adapter import ClaudeCodeAdapter
    from codex_adapter import CodexAdapter
    from external_runtime import RuntimeProfile
    from gemini_adapter import GeminiAdapter
    from pi_adapter import PiAdapter
    from tiny_agents_adapter import TinyAgentsAdapter

    with patch("claude_code_adapter.shutil.which", return_value="/fake/claude"), \
         patch("pi_adapter.shutil.which", return_value="/fake/pi"), \
         patch("codex_adapter.shutil.which", return_value="/fake/codex"), \
         patch("gemini_adapter.shutil.which", return_value="/fake/gemini"), \
         patch("tiny_agents_adapter.shutil.which", return_value="/fake/tiny-agents"), \
         patch.dict(os.environ, {"TINY_AGENTS_AGENT_PATH": "/fake/agent",
                                 "TINY_AGENTS_COMMAND": "/fake/command"}, clear=False):
        return real_registry()


class RegistryPresenceTests(unittest.TestCase):
    """All five implemented adapters are known to the production chain."""

    def test_all_five_adapters_register(self):
        registry = offline_registry()
        ids = {descriptor.runtime_id for descriptor in registry.list()}
        self.assertEqual(ids, {
            "claude-cli", "pi-cli", "codex-cli", "gemini-cli", "tiny-agents"})

    def test_registry_only_registers_implemented_adapters(self):
        # The declaration surface stays the adapter family itself: the
        # registry is built from from_environment, so an adapter that
        # does not exist cannot register (AttributeError-free absence).
        registry = offline_registry()
        for descriptor in registry.list():
            self.assertTrue(callable(descriptor.adapter_factory))

    def test_absent_runtime_is_honest_absence_not_registration(self):
        # Gemini is NOT installed on this machine: the unpatched
        # from_environment returns None and gemini must NOT register.
        with patch("gemini_adapter.shutil.which", return_value=None):
            registry = real_registry()
        ids = {descriptor.runtime_id for descriptor in registry.list()}
        # Only runtimes actually present on this machine register; the
        # invariant is "absent adapter -> not registered".
        from gemini_adapter import GeminiAdapter
        if GeminiAdapter.from_environment() is None:
            self.assertNotIn("gemini-cli", ids)


class DescriptorIdentityTests(unittest.TestCase):
    """Descriptor identity fields are exact and runtime-neutral."""

    EXPECTED = {
        "claude-cli": ("claude-cli", "anthropic", None, "installed"),
        "pi-cli": ("pi-cli", "deepseek", None, "installed"),
        "codex-cli": ("codex-cli", "openai", None, "installed"),
        "gemini-cli": ("gemini-cli", "google", None, "installed"),
        "tiny-agents": ("tiny-agents", "tiny-agents", None, "installed"),
    }

    def test_descriptor_identity_tuples_are_exact(self):
        registry = offline_registry()
        for descriptor in registry.list():
            expected = self.EXPECTED[descriptor.runtime_id]
            self.assertEqual(descriptor.identity, expected,
                             descriptor.runtime_id)

    def test_descriptor_factory_yields_the_registered_adapter(self):
        registry = offline_registry()
        by_id = {d.runtime_id: d for d in registry.list()}
        self.assertEqual(
            type(by_id["pi-cli"].adapter_factory()).__name__, "PiAdapter")
        self.assertEqual(
            type(by_id["codex-cli"].adapter_factory()).__name__, "CodexAdapter")
        self.assertEqual(
            type(by_id["gemini-cli"].adapter_factory()).__name__, "GeminiAdapter")

    def test_discovery_sources_bridge_every_descriptor(self):
        # The existing bridging covers the full registry unchanged — the
        # registry layer requires no new plumbing for new runtimes.
        from runtime_adapter_registry import discovery_sources
        registry = offline_registry()
        sources = discovery_sources(registry)
        self.assertEqual(len(sources), 5)
        self.assertEqual({s.runtime_id for s in sources},
                         set(self.EXPECTED))


class DiscoveryHonestyTests(unittest.TestCase):
    """Discovery reports what the machine says — never upgrades a state."""

    def test_discovery_of_absent_runtime_is_not_found(self):
        # An adapter can be registered against a fake executable path,
        # but discovery must probe honestly: a missing binary yields
        # available=False, NOT a fabricated candidate.
        registry = offline_registry()
        sources = []
        for descriptor in registry.list():
            adapter = descriptor.adapter_factory()
            # Force every probe to fail like a missing binary would.
            with patch.object(type(adapter), "_probe",
                              return_value=(False, "not found")):
                sources.append(DiscoverySource(
                    runtime_id=descriptor.runtime_id,
                    runtime_type=descriptor.runtime_type,
                    display_name=descriptor.display_name,
                    adapter=adapter))
        candidates = RuntimeCandidateDiscovery(sources).discover_all()
        for candidate in candidates:
            self.assertFalse(candidate.available, candidate.runtime_id)
            self.assertTrue(candidate.reason)  # honest reason, always

    def test_codex_auth_failure_is_not_discovery_failure(self):
        # Codex historically reports auth problems; discovery answers
        # EXISTENCE only. A discoverable-but-unauthenticated Codex stays
        # a discovered candidate — health/auth classification belongs to
        # the health layer, not to registration or discovery.
        from codex_adapter import CodexAdapter
        from external_runtime import RuntimeProfile
        profile = RuntimeProfile("coding-agent", "codex-cli", "openai",
                                 None, "coder", frozenset())
        adapter = CodexAdapter(profile=profile, executable="/fake/codex")
        with patch.object(CodexAdapter, "_probe", return_value=(True, "0.1")):
            sources = [DiscoverySource("codex-cli", "coding-agent",
                                       "Codex", adapter)]
            candidate = RuntimeCandidateDiscovery(sources).discover("codex-cli")
        self.assertTrue(candidate.available)
        # And registration carries no health claim of any kind:
        # RuntimeCandidate's closed fields are existence-only (runtime
        # identity + availability + reason/version) — no capabilities,
        # no state, no provenance anywhere on the candidate.
        import dataclasses
        candidate_fields = {f.name for f in dataclasses.fields(
            type(candidate))}
        for banned in ("capabilities", "status", "health", "validation",
                       "provenance", "verified", "ready"):
            self.assertNotIn(banned, candidate_fields)

    def test_gemini_registered_but_absent_stays_not_found(self):
        # The precise R4 honesty case: the adapter EXISTS (implemented),
        # the descriptor registers when the executable exists, and with
        # the executable absent discovery reports NOT_FOUND — never
        # READY, never a candidate.
        from gemini_adapter import GeminiAdapter
        from external_runtime import RuntimeProfile
        profile = RuntimeProfile("coding-agent", "gemini-cli", "google",
                                 None, "coder", frozenset())
        adapter = GeminiAdapter(profile=profile, executable="/missing/gemini")
        with patch.object(GeminiAdapter, "_probe",
                          return_value=(False, "executable not found")):
            sources = [DiscoverySource("gemini-cli", "coding-agent",
                                       "Gemini CLI", adapter)]
            candidate = RuntimeCandidateDiscovery(sources).discover("gemini-cli")
        self.assertFalse(candidate.available)
        self.assertIn("NOT_FOUND", candidate.reason or "")
        # Existence-only candidate: nowhere to hide a state claim.
        import dataclasses
        candidate_fields = {f.name for f in dataclasses.fields(
            type(candidate))}
        for banned in ("capabilities", "status", "health", "validation",
                       "provenance", "verified", "ready"):
            self.assertNotIn(banned, candidate_fields)


class QualificationBoundaryTests(unittest.TestCase):
    """Registration is not qualification — structurally and by value."""

    def test_registry_construction_spawns_no_processes(self):
        # Offline registry construction must not invoke any runtime:
        # every adapter method that could reach a subprocess is guarded
        # by an AssertionError spy. Building the registry is data-only.
        from claude_code_adapter import ClaudeCodeAdapter
        from codex_adapter import CodexAdapter
        from gemini_adapter import GeminiAdapter
        from pi_adapter import PiAdapter

        def _no_spawn(name):
            def fail(*args, **kwargs):
                raise AssertionError(f"{name} spawned a subprocess")
            return fail

        with patch("claude_code_adapter.shutil.which", return_value="/fake/claude"), \
             patch("pi_adapter.shutil.which", return_value="/fake/pi"), \
             patch("codex_adapter.shutil.which", return_value="/fake/codex"), \
             patch("gemini_adapter.shutil.which", return_value="/fake/gemini"), \
             patch("tiny_agents_adapter.shutil.which", return_value="/fake/tiny-agents"), \
             patch.dict(os.environ, {"TINY_AGENTS_AGENT_PATH": "/fake/agent",
                                     "TINY_AGENTS_COMMAND": "/fake/command"}), \
             patch("claude_code_adapter.subprocess.run", side_effect=_no_spawn("claude")), \
             patch("claude_code_adapter.subprocess.Popen", side_effect=_no_spawn("claude")), \
             patch("pi_adapter.subprocess.run", side_effect=_no_spawn("pi")), \
             patch("pi_adapter.subprocess.Popen", side_effect=_no_spawn("pi")), \
             patch("codex_adapter.subprocess.run", side_effect=_no_spawn("codex")), \
             patch("codex_adapter.subprocess.Popen", side_effect=_no_spawn("codex")), \
             patch("gemini_adapter.subprocess.run", side_effect=_no_spawn("gemini")), \
             patch("gemini_adapter.subprocess.Popen", side_effect=_no_spawn("gemini")):
            registry = real_registry()
        self.assertEqual(len(registry.list()), 5)

    def test_registration_carries_no_validation_fields(self):
        # The descriptor schema has NO health/validation/provenance
        # fields at all — there is structurally nowhere to hide a
        # VERIFIED or REAL claim in a registration.
        import dataclasses
        registry = offline_registry()
        field_names = {f.name for f in dataclasses.fields(AdapterDescriptor)}
        for banned in ("status", "health", "validation", "provenance",
                       "verified", "capabilities", "ready"):
            self.assertNotIn(banned, field_names)
        for descriptor in registry.list():
            # And no descriptor smuggles a state through its text fields.
            for text in (descriptor.display_name, descriptor.config_fingerprint):
                lowered = (text or "").lower()
                for banned in ("verified", "ready", "real", "authenticated"):
                    self.assertNotIn(banned, lowered)

    def test_offline_registry_does_not_touch_the_pool(self):
        # Building a registry and running discovery never admits anything:
        # VerifiedRuntimePool appears only inside bootstrap_runtime_session
        # (gated, REAL-authorized paths). Offline, the pool stays empty.
        from verified_runtime_pool import VerifiedRuntimePool
        pool = VerifiedRuntimePool(clock=lambda: 0.0)
        registry = offline_registry()
        sources = []
        for descriptor in registry.list():
            adapter = descriptor.adapter_factory()
            with patch.object(type(adapter), "_probe",
                              return_value=(False, "offline")):
                sources.append(DiscoverySource(
                    descriptor.runtime_id, descriptor.runtime_type,
                    descriptor.display_name, adapter))
        candidates = RuntimeCandidateDiscovery(sources).discover_all()
        self.assertEqual(len(candidates), 5)
        self.assertEqual(pool.identities(), ())

    def test_pi_registration_is_not_qualification(self):
        # Pi carries REAL four-stage collaboration evidence from 10H-F,
        # but that is collaboration evidence, NOT an independent G1-G14
        # qualification. Registration must not promote it: the registry
        # layer has no evidence surface, so nothing can leak through.
        import dataclasses
        registry = offline_registry()
        pi = next(d for d in registry.list() if d.runtime_id == "pi-cli")
        values = {f.name: getattr(pi, f.name) for f in dataclasses.fields(pi)}
        # The only callable is the factory; nothing carries gate results,
        # validated capabilities, or provenance.
        self.assertEqual(
            {k for k in values if k not in
             ("runtime_id", "provider_id", "model_id", "config_fingerprint",
              "runtime_type", "display_name", "adapter_factory")},
            set())


class RegistryNeutralityTests(unittest.TestCase):
    """The registry layer stays runtime-neutral: no name branches."""

    def test_registry_layer_has_no_runtime_name_branching(self):
        # runtime_adapter_registry.py must not mention any concrete
        # runtime: new runtimes plug in by data, not by code changes.
        import runtime_adapter_registry as module
        source = Path(module.__file__).read_text(encoding="utf-8").lower()
        for name in ("claude", "codex", "pi-cli", "gemini", "tiny-agents",
                     "anthropic", "openai", "deepseek", "google"):
            self.assertNotIn(name, source)

    def test_core_engine_has_no_new_runtime_knowledge(self):
        # The V2 core stays frozen: registration added zero runtime
        # knowledge to any engine module.
        core = ("execution_engine.py", "collaboration_orchestrator.py",
                "collaboration_session.py", "verification_collaboration.py",
                "verified_runtime_pool.py", "verified_selection_bridge.py",
                "verified_stage_selector.py", "role_assignment.py",
                "production_facade.py", "task_budget.py", "loop_guard.py",
                "real_validation_executor.py", "candidate_validation.py",
                "runtime_health.py", "generic_runtime_health.py",
                "host.py", "discovery_bootstrap.py")
        for name in core:
            path = SCRIPTS / name
            source = path.read_text(encoding="utf-8").lower()
            for runtime in ("gemini", "pi-cli", "codex-cli"):
                self.assertNotIn(runtime, source, name)


if __name__ == "__main__":
    unittest.main()
