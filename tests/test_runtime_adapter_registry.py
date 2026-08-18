"""RC-3 Task B-1: Adapter Descriptor Registry contract.

The registry is pure registration + lookup + discovery delegation: it never
executes a runtime, never qualifies, never holds secrets, and carries no
orchestration logic. Runtime-specific knowledge stays inside the adapters
and their DiscoverySource metadata.
"""
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from runtime_adapter_registry import (
    AdapterDescriptor,
    AdapterRegistry,
    discovery_sources,
)

SECRET_MARKERS = ("token", "secret", "api_key", "authorization", "bearer", "stdout", "stderr")


def descriptor(runtime_id="rt-a", provider_id="provider-a", model_id=None,
               display_name="Runtime A", **overrides):
    values = dict(
        runtime_id=runtime_id, provider_id=provider_id, model_id=model_id,
        runtime_type="coding-agent", display_name=display_name,
        adapter_factory=lambda: FakeAdapter(), config_fingerprint="fp-a",
    )
    values.update(overrides)
    return AdapterDescriptor(**values)


class FakeAdapter:
    """Offline probe surface; no process is ever started."""

    def discover(self):
        from external_runtime import RuntimeDiscovery
        return RuntimeDiscovery("rt-a", True, "1.0", None, frozenset())

    def check_authentication(self):
        from runtime_health import AuthenticationCheck
        from runtime_status import AuthenticationState
        return AuthenticationCheck(AuthenticationState.AUTHENTICATED, "oauth")

    def check_provider_model(self):
        from runtime_health import ProviderModelCheck
        from runtime_status import ReasonCode
        return ProviderModelCheck("provider-a", None, True, ReasonCode.NONE)

    def minimal_health_check(self, timeout_seconds):
        from runtime_health import MinimalHealthCheck
        from runtime_status import ReasonCode
        return MinimalHealthCheck(True, ReasonCode.NONE, output_class="exact_ok")


class AdapterRegistryContractTests(unittest.TestCase):
    def test_register_and_get_descriptor(self):
        registry = AdapterRegistry()
        registry.register(descriptor())
        self.assertEqual(registry.get("rt-a").runtime_id, "rt-a")

    def test_list_is_sorted_and_complete(self):
        registry = AdapterRegistry()
        registry.register(descriptor(runtime_id="rt-b", provider_id="p-b"))
        registry.register(descriptor(runtime_id="rt-a", provider_id="p-a"))
        self.assertEqual([d.runtime_id for d in registry.list()], ["rt-a", "rt-b"])

    def test_duplicate_runtime_id_is_rejected(self):
        registry = AdapterRegistry()
        registry.register(descriptor())
        with self.assertRaises(ValueError):
            registry.register(descriptor())

    def test_unknown_runtime_id_is_explicit(self):
        registry = AdapterRegistry()
        with self.assertRaises(KeyError):
            registry.get("nobody")

    def test_registry_never_executes_runtime(self):
        import runtime_adapter_registry as module
        source = Path(module.__file__).read_text(encoding="utf-8")
        for forbidden in ("invoke(", "minimal_health_check(", "run_real_validation",
                          "CandidateValidationRunner", "subprocess"):
            self.assertNotIn(forbidden, source)
        # register() must not call the adapter factory.
        calls = {"n": 0}

        def counting_factory():
            calls["n"] += 1
            return FakeAdapter()

        AdapterRegistry().register(descriptor(adapter_factory=counting_factory))
        self.assertEqual(calls["n"], 0)

    def test_descriptor_rejects_secret_shaped_config(self):
        for field, bad in (("provider_id", "api_key=abc"), ("display_name", "token: x"),
                           ("config_fingerprint", "bearer secret")):
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    descriptor(**{field: bad})

    def test_discovery_sources_bridge_to_existing_discovery(self):
        from runtime_discovery import DiscoverySource, RuntimeCandidateDiscovery
        registry = AdapterRegistry()
        registry.register(descriptor())
        sources = discovery_sources(registry)
        self.assertEqual(len(sources), 1)
        self.assertIsInstance(sources[0], DiscoverySource)
        candidates = RuntimeCandidateDiscovery(sources).discover_all()
        self.assertEqual(len(candidates), 1)
        self.assertTrue(candidates[0].available)

    def test_registry_is_runtime_neutral(self):
        import runtime_adapter_registry as module
        text = Path(module.__file__).read_text(encoding="utf-8").lower()
        for name in ("claude", "codex", "deepseek", "openai", "anthropic",
                     "gemini", "tiny-agents", "tiny_agents"):
            self.assertNotIn(name, text)

    def test_descriptor_is_frozen_and_secret_free(self):
        from dataclasses import FrozenInstanceError
        item = descriptor()
        with self.assertRaises(FrozenInstanceError):
            item.runtime_id = "mutated"
        surface = repr(item).lower()
        for marker in SECRET_MARKERS:
            self.assertNotIn(marker, surface)


if __name__ == "__main__":
    unittest.main()
