import sys
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"))
from dual_agent import (
    AdapterProfile, Evidence, EvidenceStatus, Metric, RoutePolicy, Router,
    RolePolicy, Task, TaskKind, DiscoveryStatus, InvokeRequest, InvokeStatus, Result,
)
from mock_adapter import MockAdapter

class RouterTests(unittest.TestCase):
    def profile(self, aid="a", caps=("edit",), task_kinds=("code",), cancel=True, reliability=90, evidence=None):
        if evidence is None: evidence=[Evidence(f"ev-{c}", c, EvidenceStatus.VERIFIED) for c in caps]
        return AdapterProfile(aid, frozenset(caps), frozenset(task_kinds), cancel, reliability, tuple(evidence))
    def test_role_swap_and_task_specific(self):
        profiles=[self.profile("z", ("edit",), ("code",)), self.profile("a", ("review",), ("review",))]
        router=Router(profiles, {"code": RoutePolicy(frozenset({"edit"}), {"reliability":2}, 0), "review": RoutePolicy(frozenset({"review"}), {"reliability":1}, 0)})
        self.assertEqual(router.route(Task("code", "Coder")).adapter_id, "z")
        roles=RolePolicy({("Architect","code"):"review", ("Coder","code"):"code"})
        self.assertEqual(router.route(Task("code", "Architect"), roles).adapter_id, "a")
    def test_unknown_evidence_is_hard_gate(self):
        p=self.profile(evidence=[Evidence("cap-edit", "edit", EvidenceStatus.UNKNOWN)])
        result=Router([p], {"code":RoutePolicy(frozenset({"edit"}), {"reliability":1}, 0)}).route(Task("code"))
        self.assertIsNone(result.adapter_id); self.assertEqual(result.reason,"NO_ROUTE")
    def test_integer_weighted_score_and_tie(self):
        ps=[self.profile("b", reliability=80), self.profile("a", reliability=80)]
        policy=RoutePolicy(frozenset({"edit"}), {"reliability":2,"capability":1}, 0)
        out=Router(ps,{"code":policy}).route(Task("code")); self.assertEqual(out.adapter_id,"a"); self.assertEqual(out.candidates[0].score,86)
    def test_discovery_failure_isolation(self):
        bad=MockAdapter("bad", discovery_status=DiscoveryStatus.UNAVAILABLE)
        good=MockAdapter("good", profile=self.profile("good")); out=Router.from_adapters([bad,good], {"code":RoutePolicy(frozenset({"edit"}),{},0)}).route(Task("code")); self.assertEqual(out.adapter_id,"good")
    def test_prepare_cancel_before_run(self):
        adapter=MockAdapter("a"); router=Router.from_adapters([adapter], {})
        handle=router.prepare_invoke("a", InvokeRequest(Task("code")))
        self.assertEqual(router.cancel("a", handle).status, InvokeStatus.CANCELED)
        self.assertEqual(router.run_invoke(handle, InvokeRequest(Task("code"))).status, InvokeStatus.CANCELED)
        self.assertEqual(adapter.invocations, {})
    def test_immutability_and_malformed(self):
        with self.assertRaises(FrozenInstanceError): self.profile().adapter_id="x"
        self.assertEqual(Router([], {}).route(None).reason,"INVALID_INPUT")

if __name__ == '__main__': unittest.main()
