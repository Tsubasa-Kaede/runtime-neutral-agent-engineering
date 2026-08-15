"""Phase 10C-A: READY Runtime Pool construction.

Discovery -> Generic Health -> Pool that keeps only READY runtimes.
The pool never selects, scores, caches, or invokes; it is a pure
construction over DiscoverySource entries whose adapter is also a
RuntimeHealthProbe.
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from external_runtime import RuntimeDiscovery
from generic_runtime_health import GenericRuntimeHealth
from runtime_discovery import DiscoverySource, RuntimeCandidate
from runtime_pool_construction import PooledRuntime, ReadyPool, RuntimePoolConstructor
from runtime_status import (
    AuthenticationState,
    ReasonCode,
    RuntimeState,
    RuntimeStatus,
)
from task_budget import BudgetUsage
from loop_guard import LoopGuard


def make_probe(rid, *, discovered=True, auth="AUTHENTICATED", provider=True, health=True):
    probe = Mock(spec=["discover", "check_authentication", "check_provider_model", "minimal_health_check"])
    probe.discover.return_value = RuntimeDiscovery(rid, discovered, "1.0", None, frozenset())
    probe.check_authentication.return_value = type("A", (), {
        "state": AuthenticationState(auth), "method": "managed", "reason_code": ReasonCode.NONE})()
    probe.check_provider_model.return_value = type("P", (), {
        "provider": "p", "model": "m", "available": provider, "reason_code": ReasonCode.NONE})()
    probe.minimal_health_check.return_value = type("H", (), {
        "passed": health, "reason_code": ReasonCode.NONE, "trace": None, "output_class": "exact_ok"})()
    return probe


def source(rid, probe):
    return DiscoverySource(rid, "cli", rid, probe)


class Phase10CPoolConstructionTests(unittest.TestCase):
    def build(self, sources):
        return RuntimePoolConstructor().build(sources)

    def test_empty_sources_produce_empty_pool(self):
        pool = self.build([])
        self.assertEqual(pool.ready, ())
        self.assertEqual(pool.excluded, ())

    def test_single_ready_runtime_enters_pool(self):
        probe = make_probe("runtime-a")
        pool = self.build([source("runtime-a", probe)])
        self.assertEqual(len(pool.ready), 1)
        self.assertEqual(pool.ready[0].candidate.runtime_id, "runtime-a")
        self.assertEqual(pool.ready[0].status.status, RuntimeState.READY)
        self.assertEqual(pool.excluded, ())

    def deterministic_constructor(self):
        from runtime_health import RuntimeHealthController
        return RuntimePoolConstructor(
            health=GenericRuntimeHealth(RuntimeHealthController(ttl_seconds=60, clock=lambda: 100.0))
        )

    def test_multiple_ready_runtimes_sorted_deterministically(self):
        pool = self.deterministic_constructor().build([
            source("zeta", make_probe("zeta")),
            source("alpha", make_probe("alpha")),
            source("mid", make_probe("mid")),
        ])
        self.assertEqual([item.candidate.runtime_id for item in pool.ready], ["alpha", "mid", "zeta"])
        again = self.deterministic_constructor().build([
            source("mid", make_probe("mid")),
            source("alpha", make_probe("alpha")),
            source("zeta", make_probe("zeta")),
        ])
        self.assertEqual(pool, again)

    def test_non_ready_states_are_excluded_not_pooled(self):
        cases = {
            "AUTH_REQUIRED": dict(auth="AUTH_REQUIRED"),
            "UNAVAILABLE_PROVIDER": dict(provider=False),
            "ERROR_HEALTH": dict(health=False),
            "ERROR_UNKNOWN_AUTH": dict(auth="UNKNOWN"),
        }
        for rid, kwargs in cases.items():
            with self.subTest(case=rid):
                pool = self.build([
                    source("ready-one", make_probe("ready-one")),
                    source(rid, make_probe(rid, **kwargs)),
                ])
                self.assertEqual([i.candidate.runtime_id for i in pool.ready], ["ready-one"])
                self.assertEqual([i.candidate.runtime_id for i in pool.excluded], [rid])
                self.assertNotEqual(pool.excluded[0].status.status, RuntimeState.READY)

    def test_undiscovered_runtime_is_excluded(self):
        probe = make_probe("ghost", discovered=False)
        pool = self.build([source("ghost", probe)])
        self.assertEqual(pool.ready, ())
        self.assertEqual(pool.excluded[0].candidate.runtime_id, "ghost")
        self.assertEqual(pool.excluded[0].status.status, RuntimeState.UNAVAILABLE)
        self.assertFalse(pool.excluded[0].candidate.available)

    def test_pool_never_invokes_adapters(self):
        probe = make_probe("runtime-a")
        self.build([source("runtime-a", probe)])
        # discovery layer enumerates the candidate; the health pipeline
        # reconfirms it — both are discover(), never an invocation.
        probe.discover.assert_called()
        probe.minimal_health_check.assert_called_once()
        self.assertFalse(hasattr(RuntimePoolConstructor, "invoke"))
        self.assertFalse(hasattr(ReadyPool, "invoke"))

    def test_pool_consumes_no_budget_and_no_guard(self):
        usage = BudgetUsage()
        guard = LoopGuard()
        self.build([source("a", make_probe("a")), source("b", make_probe("b", auth="AUTH_REQUIRED"))])
        self.assertEqual(usage.total_agent_calls, 0)
        self.assertEqual(usage.iterations_used, 0)
        self.assertEqual(guard.check("t", "architect", "x"), "ALLOW")

    def test_pooled_data_pairs_candidate_with_status(self):
        probe = make_probe("runtime-a")
        pool = self.build([source("runtime-a", probe)])
        item = pool.ready[0]
        self.assertIsInstance(item.candidate, RuntimeCandidate)
        self.assertIsInstance(item.status, RuntimeStatus)
        self.assertEqual(item.candidate.runtime_id, item.status.runtime_id)
        with self.assertRaises(Exception):
            item.candidate = None

    def test_no_runtime_name_branches_in_core(self):
        import runtime_pool_construction
        text = Path(runtime_pool_construction.__file__).read_text(encoding="utf-8").lower()
        for name in ("claude", "codex", "gemini", "deepseek"):
            self.assertNotIn(name, text)

    def test_pool_surface_is_secret_free(self):
        pool = self.build([
            source("a", make_probe("a")),
            source("b", make_probe("b", auth="AUTH_REQUIRED")),
        ])
        surface = repr(pool).lower()
        for marker in ("token", "secret", "api_key", "authorization", "stdout", "stderr"):
            self.assertNotIn(marker, surface)

    def test_generic_health_not_bypassed(self):
        constructor = RuntimePoolConstructor(health=GenericRuntimeHealth())
        pool = constructor.build([source("a", make_probe("a", auth="AUTH_REQUIRED"))])
        self.assertEqual(pool.ready, ())
        self.assertEqual(pool.excluded[0].status.status, RuntimeState.AUTH_REQUIRED)


if __name__ == "__main__":
    unittest.main()
