"""V3.0-D: Agent composition root tests — Agent Composition first wired
into the frozen V2 execution stack.

Red-first：agent_host.py 尚不存在时本文件必然失败（ImportError）。
期望值是独立字面量或既有模块调用（V2 自身的 collab_agent_address /
_ROLE_REQUIREMENTS），绝不从被测模块自身源码派生。

锁定（Boundary Review + 实现授权）：
1. 单 agent 全角色组合；四阶段离线真实跑通（mock adapters，REAL=0）
2. 多 agent / 多 runtime 自由组合；跨 runtime 角色分工（arch/coder 与
   test/review 分属不同 runtime）
3. 同 runtime 多 agent（不同 role）共存；一个 agent 声明多 role
4. attribution = compat_address -> agent_id：与 V2 collab_agent_address
   字节一致、地址级一一对应；V3 agent_id 绝不进入 ExecutionEvent
5. factory 恰一次/agent（V3.0-C 调用点复用）；registry 只读
6. V3.0-C collapse error 原样透传（不重包装）
7. 未知 agent / 缺 manifest / 缺 qualification evidence / 缺
   current_health / 证据 identity 不符 / 混合 provenance —— 组合期
   封闭错误
8. 缺 role capability evidence：声明角色投影出的 capability 不在证据
   中时，由既有 V2 admission 诚实拒绝（CAPABILITY_INSUFFICIENT 原样
   上抛，组合根绝不代补）；反向——证据含未被声明角色的协作
   capability 时组合期拒绝（冻结 bridge 按 validation 选择，窄化
   admission 无法收窄选择宇宙——实现期真实发现，双向一致性）
9. min-distinct policy 与 V2 一致：满足时干净通过；不满足时 V2 照常
   返回最优指派并在 DECISION reason 如实标注 POLICY_COUNT_UNSATISFIED
10. SINGLE 执行与既有 V2 行为一致：SINGLE 由 coder 声明 agent 的
    adapter 承担（V2 SINGLE 指令契约即 coder 契约）
11. determinism：同输入同输出
12. 源码纪律：零 runtime/provider 名、import 面封闭、零角色选择逻辑、
    V2 模块不 import 任何 agent 层
13. V2 文件本轮零修改；两个受保护 untracked 文件保持原样
"""
import ast
import json
import subprocess
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from agent_host import _ROLE_CAPABILITY, build_facade_from_agents
from agent_identity import AgentIdentity, AgentRuntimeBinding
from agent_manifest import AgentManifest, AgentRegistry
from candidate_validation import (
    CandidateValidationResult,
    CandidateValidationStatus,
    GateResult,
    GateVerdict,
    ValidationGate,
)
from collaboration_policy import CollaborationPolicy
from collaboration_session import collab_agent_address
from external_runtime import InvocationResult, InvocationStatus, InvocationTrace
from mode_gate import Mode
from production_facade import ProductionFacade
from runtime_status import (
    HealthEvidence,
    ReasonCode,
    RuntimeState,
    RuntimeStatus,
)
from verified_selection_bridge import agent_id_for
from verified_stage_selector import _ROLE_REQUIREMENTS

RT_A = ("rt-alpha", "provider-x", None, "fp-alpha")
RT_B = ("rt-beta", "provider-y", None, "fp-beta")
CAPS_ALL = ("architecture", "coding", "review", "testing")
TASK_COMPLEX = "redesign architecture across modules"
TASK_SIMPLE = "fix one simple bug"

ARCH_P = {"task_id": "t", "role": "architect", "goal": ["g"], "constraints": ["c"],
          "architecture": ["a"], "interfaces": [{}], "implementation_steps": [{}],
          "acceptance_criteria": ["ac"], "risks": [{}]}
IMPL_P = {"task_id": "t", "role": "coder", "changed_files": ["f"],
          "implementation_summary": "s", "implementation_details": ["d"],
          "assumptions": [], "unresolved_items": [], "test_requirements": ["tr"]}
TEST_P = {"task_id": "t", "role": "tester", "tests_run": ["x"], "tests_passed": ["x"],
          "tests_failed": [], "failures": [], "coverage_or_validation": [],
          "remaining_risks": []}
REVIEW_P = {"task_id": "t", "role": "reviewer", "status": "PASS", "findings": [],
            "severity": [], "affected_files": [], "required_changes": [],
            "acceptance_criteria_status": []}


def trace():
    return InvocationTrace(
        invocation_id="inv-v3d", task_id="t", agent_id="a", runtime="rt",
        provider=None, model=None, role=None, status=InvocationStatus.SUCCESS,
        started_at=0.0, finished_at=0.0, duration_ms=1, exit_code=0,
        input_tokens="unknown", output_tokens="unknown", error=None)


class RoleAdapter:
    """Offline adapter: routes by V2 agent_id shape (bare 4-tuple JSON =
    SINGLE executor; role-suffixed address = collaboration packet) —
    runtime-neutral, no identity constants. Counts invocations and records
    every address it was asked to serve."""

    def __init__(self):
        self.invocations = 0
        self.addresses = []

    @staticmethod
    def _ok(output):
        return InvocationResult(InvocationStatus.SUCCESS, output=output,
                                trace=trace())

    def discover(self):
        from external_runtime import RuntimeDiscovery
        return RuntimeDiscovery("rt", True, "1.0", None, frozenset())

    def check_authentication(self):
        from runtime_health import AuthenticationCheck
        from runtime_status import AuthenticationState
        return AuthenticationCheck(AuthenticationState.AUTHENTICATED, "oauth")

    def check_provider_model(self):
        from runtime_health import ProviderModelCheck
        from runtime_status import ReasonCode as _RC
        return ProviderModelCheck("p", None, True, _RC.NONE)

    def cancel(self, invocation_id):
        return InvocationResult(InvocationStatus.CANCELLED)

    def invoke(self, request):
        self.invocations += 1
        self.addresses.append(request.agent_id)
        try:
            parsed = json.loads(request.agent_id)
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, list) and len(parsed) == 4:
            # bare runtime identity — the SINGLE executor path. Without the
            # packet-contract instruction (which the V2 host seam injects)
            # a real adapter answers free text; mirror that honestly.
            if "Return ONLY a JSON object" not in (request.prompt or ""):
                return self._ok("I would fix the bug by editing the file.")
            return self._ok(json.dumps(IMPL_P))
        for role, packet in (("architect", ARCH_P), ("coder", IMPL_P),
                             ("tester", TEST_P), ("reviewer", REVIEW_P)):
            if request.agent_id == role or request.agent_id.endswith(f',"{role}"]'):
                return self._ok(json.dumps(packet))
        return self._ok("OK")


class CountingFactory:
    """callable 替身：计数并返回稳定 adapter 实例。"""

    def __init__(self):
        self.calls = 0
        self.product = RoleAdapter()

    def __call__(self):
        self.calls += 1
        return self.product


def agent_entry(agent_id, runtime, roles):
    factory = CountingFactory()
    manifest = AgentManifest(
        binding=AgentRuntimeBinding(agent=AgentIdentity(agent_id=agent_id),
                                    runtime_identity=runtime),
        declared_roles=tuple(roles),
        adapter_factory=factory)
    return manifest, factory


def registry_of(manifests):
    registry = AgentRegistry()
    for manifest in manifests:
        registry.register(manifest)
    return registry


def cross_team():
    """跨 runtime 角色分工队伍：arch/review 在 RT_A，coder/test 在 RT_B。"""
    entries = {
        "agent-arch": agent_entry("agent-arch", RT_A, ("architect",)),
        "agent-code": agent_entry("agent-code", RT_B, ("coder",)),
        "agent-test": agent_entry("agent-test", RT_B, ("tester",)),
        "agent-review": agent_entry("agent-review", RT_A, ("reviewer",)),
    }
    return registry_of([m for m, _f in entries.values()]), entries


CROSS_IDS = ("agent-review", "agent-arch", "agent-test", "agent-code")


def validation_for(identity, caps=CAPS_ALL, provenance="OFFLINE"):
    return CandidateValidationResult(
        identity=tuple(identity), status=CandidateValidationStatus.VERIFIED,
        gates_passed=frozenset(ValidationGate),
        gate_results=tuple(GateResult(g, GateVerdict.PASS)
                           for g in ValidationGate),
        block_reason=None, failure_point=None, experiment_id="v3d-exp",
        executed_at=0.0, validated_capabilities=tuple(caps), evidence={},
        provenance=provenance)


def evidence_for(*identities, caps=CAPS_ALL, provenance="OFFLINE"):
    return {tuple(identity): validation_for(identity, caps=caps,
                                            provenance=provenance)
            for identity in identities}


def cross_evidence():
    """与 cross_team 声明严格一致的角色界定证据（双向能力一致性）。"""
    return {RT_A: validation_for(RT_A, caps=("architecture", "review")),
            RT_B: validation_for(RT_B, caps=("coding", "testing"))}


def health_for(*identities):
    return {identity[0]: RuntimeStatus(
        runtime_id=identity[0], executable="e", version="1",
        status=RuntimeState.READY, provider=identity[1], model=None,
        auth_method=None, reason_code=ReasonCode.NONE,
        evidence=HealthEvidence("d", "a", "p", "m", "ok"),
        checked_at=0.0, expires_at=1.0) for identity in identities}


class RecordingSink:
    """Observation 消费者替身：只收集，不投影。"""

    def __init__(self):
        self.events = []

    def on_event(self, event):
        self.events.append(event)


def build_cross():
    """跨 runtime 角色分工队伍 + 与声明严格一致的角色界定证据。

    RT_A 声明 architect+reviewer（证据 caps=architecture+review），
    RT_B 声明 coder+tester（证据 caps=coding+testing）——能力一致性
    不变量的双向要求（证据含未声明协作能力会被组合期拒绝）。"""
    registry, entries = cross_team()
    facade, attribution = build_facade_from_agents(
        registry, CROSS_IDS, cross_evidence(), health_for(RT_A, RT_B))
    factories = {agent_id: factory
                 for agent_id, (_m, factory) in entries.items()}
    return facade, attribution, factories


# ---------------------------------------------------------------------------
# 组合契约：返回物、attribution、pool、词表锁定
# ---------------------------------------------------------------------------


class CompositionContractTests(unittest.TestCase):

    def test_returns_production_facade_and_attribution(self):
        facade, attribution = build_cross()[:2]
        self.assertIsInstance(facade, ProductionFacade)
        self.assertIsInstance(attribution, dict)

    def test_attribution_matches_v2_collab_address_byte_exact(self):
        # 期望值由 V2 自身的 collab_agent_address 独立计算 —— 字节级投影。
        _, attribution = build_cross()[:2]
        self.assertEqual(attribution, {
            collab_agent_address(RT_A, "architect"): "agent-arch",
            collab_agent_address(RT_B, "coder"): "agent-code",
            collab_agent_address(RT_B, "tester"): "agent-test",
            collab_agent_address(RT_A, "reviewer"): "agent-review",
        })

    def test_attribution_addresses_are_unique_per_runtime_role(self):
        # 地址级一一对应：四个不同 (runtime, role) 地址、各自恰一 agent。
        _, attribution = build_cross()[:2]
        self.assertEqual(len(attribution), 4)
        self.assertEqual(len(set(attribution)), 4)

    def test_single_agent_all_roles_maps_one_agent_to_four_addresses(self):
        manifest, _factory = agent_entry(
            "agent-solo", RT_A, ("architect", "coder", "tester", "reviewer"))
        facade, attribution = build_facade_from_agents(
            registry_of([manifest]), ("agent-solo",),
            evidence_for(RT_A), health_for(RT_A))
        self.assertEqual(sorted(attribution.values()),
                         ["agent-solo"] * 4)
        self.assertEqual(len(attribution), 4)
        self.assertIsInstance(facade, ProductionFacade)

    def test_pool_contains_exactly_the_composed_runtimes(self):
        facade, _ = build_cross()[:2]
        self.assertEqual(facade._pool.identities(), (RT_A, RT_B))

    def test_role_capability_projection_locked_to_v2_vocabulary(self):
        # 组合根的 role→capability 投影不得与 V2 _ROLE_REQUIREMENTS 漂移
        # （tester/reviewer 是地址拼写，stage 名拼写为 test/review）。
        stage_of = {"tester": "test", "reviewer": "review"}
        for role, capability in _ROLE_CAPABILITY.items():
            stage = stage_of.get(role, role)
            self.assertEqual(_ROLE_REQUIREMENTS[stage], (capability,))


# ---------------------------------------------------------------------------
# 执行链路 E2E：Agent -> composition -> pool -> orchestrator -> session /
# verification -> ProductionFacade（mock adapters，REAL=0）
# ---------------------------------------------------------------------------


class ExecutionPathTests(unittest.TestCase):

    def test_cross_runtime_team_four_stage_success(self):
        facade, attribution, factories = build_cross()
        result = facade.run(task_id="v3d-1", task=TASK_COMPLEX,
                            prompt=TASK_COMPLEX, mode=Mode.ON)
        self.assertEqual(result.status, "SUCCESS")
        self.assertEqual(result.path, "FOUR_STAGE")
        self.assertEqual(result.stages,
                         ("architect", "coder", "tester", "reviewer"))
        # 每个角色恰好落在声明该角色的 agent 的 adapter 上，各一次。
        self.assertEqual(factories["agent-arch"].product.invocations, 1)
        self.assertEqual(factories["agent-code"].product.invocations, 1)
        self.assertEqual(factories["agent-test"].product.invocations, 1)
        self.assertEqual(factories["agent-review"].product.invocations, 1)
        # adapter 实际看到的 V2 地址与 attribution 键字节一致。
        self.assertEqual(factories["agent-arch"].product.addresses,
                         [collab_agent_address(RT_A, "architect")])
        self.assertEqual(factories["agent-code"].product.addresses,
                         [collab_agent_address(RT_B, "coder")])
        self.assertEqual(factories["agent-test"].product.addresses,
                         [collab_agent_address(RT_B, "tester")])
        self.assertEqual(factories["agent-review"].product.addresses,
                         [collab_agent_address(RT_A, "reviewer")])
        self.assertEqual(len(attribution), 4)

    def test_single_agent_all_roles_four_stage_success(self):
        manifest, factory = agent_entry(
            "agent-solo", RT_A, ("architect", "coder", "tester", "reviewer"))
        facade, _ = build_facade_from_agents(
            registry_of([manifest]), ("agent-solo",),
            evidence_for(RT_A), health_for(RT_A))
        result = facade.run(task_id="v3d-2", task=TASK_COMPLEX,
                            prompt=TASK_COMPLEX, mode=Mode.ON)
        self.assertEqual(result.status, "SUCCESS")
        self.assertEqual(result.path, "FOUR_STAGE")
        self.assertEqual(factory.product.invocations, 4)

    def test_one_runtime_multiple_agents_coexist(self):
        # 同 runtime 两 agent：architect 归 agent-a，其余三角色归 agent-b。
        arch, arch_factory = agent_entry("agent-a", RT_A, ("architect",))
        rest, rest_factory = agent_entry(
            "agent-b", RT_A, ("coder", "tester", "reviewer"))
        facade, attribution = build_facade_from_agents(
            registry_of([arch, rest]), ("agent-b", "agent-a"),
            evidence_for(RT_A), health_for(RT_A))
        self.assertEqual(facade._pool.identities(), (RT_A,))
        self.assertEqual(attribution[collab_agent_address(RT_A, "architect")],
                         "agent-a")
        self.assertEqual(attribution[collab_agent_address(RT_A, "coder")],
                         "agent-b")
        result = facade.run(task_id="v3d-3", task=TASK_COMPLEX,
                            prompt=TASK_COMPLEX, mode=Mode.ON)
        self.assertEqual(result.status, "SUCCESS")
        self.assertEqual(result.path, "FOUR_STAGE")
        self.assertEqual(arch_factory.product.invocations, 1)
        self.assertEqual(rest_factory.product.invocations, 3)

    def test_budget_usage_shared_through_facade(self):
        facade, _ = build_cross()[:2]
        facade.run(task_id="v3d-4", task=TASK_COMPLEX, prompt=TASK_COMPLEX,
                   mode=Mode.ON)
        self.assertEqual(facade._usage.total_agent_calls, 4)

    def test_provenance_defaults_to_evidence(self):
        facade, _ = build_cross()[:2]
        result = facade.run(task_id="v3d-5", task=TASK_COMPLEX,
                            prompt=TASK_COMPLEX, mode=Mode.ON)
        self.assertEqual(result.safe_summary["provenance"], "OFFLINE")


# ---------------------------------------------------------------------------
# SINGLE 语义：与既有 V2 行为一致，coder 声明者承担
# ---------------------------------------------------------------------------


class SingleExecutionTests(unittest.TestCase):

    def _team(self):
        arch, arch_factory = agent_entry("agent-arch", RT_A, ("architect",))
        rest, rest_factory = agent_entry(
            "agent-code", RT_A, ("coder", "tester", "reviewer"))
        facade, _ = build_facade_from_agents(
            registry_of([arch, rest]), ("agent-code", "agent-arch"),
            evidence_for(RT_A), health_for(RT_A))
        return facade, arch_factory, rest_factory

    def test_single_routes_to_coder_declaring_agent(self):
        # 同 runtime 双 agent：SINGLE 只调用 coder 声明者的 adapter，
        # architect 声明者零调用 —— 归属由声明决定，不由顺序决定。
        facade, arch_factory, rest_factory = self._team()
        result = facade.run(task_id="v3d-s1", task=TASK_SIMPLE,
                            prompt=TASK_SIMPLE, mode=Mode.AUTO)
        self.assertEqual(result.path, "SINGLE")
        self.assertEqual(result.status, "SUCCESS", result.failure_category)
        self.assertEqual(arch_factory.product.invocations, 0)
        self.assertEqual(rest_factory.product.invocations, 1)
        # SINGLE 执行器键 = V2 的 agent_id_for(identity)。
        self.assertEqual(rest_factory.product.addresses,
                         [agent_id_for(RT_A)])

    def test_single_cross_runtime_team_uses_coder_runtime(self):
        # 跨 runtime 队伍：SINGLE 落在 coder 声明 agent 的 runtime 上。
        facade, _, factories = build_cross()
        result = facade.run(task_id="v3d-s2", task=TASK_SIMPLE,
                            prompt=TASK_SIMPLE, mode=Mode.AUTO)
        self.assertEqual(result.path, "SINGLE")
        self.assertEqual(result.status, "SUCCESS", result.failure_category)
        self.assertEqual(factories["agent-arch"].product.invocations, 0)
        self.assertEqual(factories["agent-code"].product.invocations, 1)
        self.assertEqual(factories["agent-test"].product.invocations, 0)
        self.assertEqual(factories["agent-review"].product.invocations, 0)


# ---------------------------------------------------------------------------
# min-distinct policy：与 V2 一致（照常指派 + reason 如实标注）
# ---------------------------------------------------------------------------


class PolicyConsistencyTests(unittest.TestCase):

    def test_min_distinct_satisfied_cross_runtime(self):
        facade, _, _ = build_cross()
        sink = RecordingSink()
        policy = CollaborationPolicy(min_distinct_runtimes=2)
        result = facade.run(task_id="v3d-p1", task=TASK_COMPLEX,
                            prompt=TASK_COMPLEX, mode=Mode.ON,
                            observation_sink=sink, policy=policy)
        self.assertEqual(result.status, "SUCCESS")
        self.assertEqual(result.path, "FOUR_STAGE")
        self.assertFalse(any("POLICY_COUNT_UNSATISFIED" in (e.reason or "")
                             for e in sink.events))

    def test_min_distinct_unsatisfied_reported_honestly_by_v2(self):
        # arch+coder 同 runtime：min=2 不可满足 —— V2 语义是照常返回最优
        # 指派并在 DECISION reason 如实标注，组合根不得改写该行为。
        lead, _ = agent_entry("agent-lead", RT_A, ("architect", "coder"))
        verify, _ = agent_entry("agent-verify", RT_B, ("tester", "reviewer"))
        facade, _ = build_facade_from_agents(
            registry_of([lead, verify]), ("agent-verify", "agent-lead"),
            {RT_A: validation_for(RT_A, caps=("architecture", "coding")),
             RT_B: validation_for(RT_B, caps=("testing", "review"))},
            health_for(RT_A, RT_B))
        sink = RecordingSink()
        policy = CollaborationPolicy(min_distinct_runtimes=2)
        result = facade.run(task_id="v3d-p2", task=TASK_COMPLEX,
                            prompt=TASK_COMPLEX, mode=Mode.ON,
                            observation_sink=sink, policy=policy)
        self.assertEqual(result.path, "FOUR_STAGE")
        self.assertTrue(any("POLICY_COUNT_UNSATISFIED" in (e.reason or "")
                            for e in sink.events),
                        [e.reason for e in sink.events])

    def test_same_runtime_dual_allowed_without_policy(self):
        # policy=None 保持历史 assigner：同 runtime dual 合法执行。
        lead, _ = agent_entry("agent-lead", RT_A, ("architect", "coder"))
        verify, _ = agent_entry("agent-verify", RT_B, ("tester", "reviewer"))
        facade, _ = build_facade_from_agents(
            registry_of([lead, verify]), ("agent-verify", "agent-lead"),
            {RT_A: validation_for(RT_A, caps=("architecture", "coding")),
             RT_B: validation_for(RT_B, caps=("testing", "review"))},
            health_for(RT_A, RT_B))
        result = facade.run(task_id="v3d-p3", task=TASK_COMPLEX,
                            prompt=TASK_COMPLEX, mode=Mode.ON)
        self.assertEqual(result.status, "SUCCESS")
        self.assertEqual(result.path, "FOUR_STAGE")


# ---------------------------------------------------------------------------
# Observation 边界：V2 事件永不携带 V3 agent_id
# ---------------------------------------------------------------------------


class ObservationBoundaryTests(unittest.TestCase):

    def test_agent_ids_never_enter_observation_events(self):
        facade, _, _ = build_cross()
        sink = RecordingSink()
        facade.run(task_id="v3d-o1", task=TASK_COMPLEX, prompt=TASK_COMPLEX,
                   mode=Mode.ON, observation_sink=sink)
        self.assertTrue(sink.events)
        for event in sink.events:
            blob = "|".join(str(x) for x in (
                event.sequence, event.event_type, event.stage,
                event.runtime_id, event.status, event.duration_ms,
                event.reason, event.correlation_id))
            for agent_id in CROSS_IDS:
                self.assertNotIn(agent_id, blob)


# ---------------------------------------------------------------------------
# factory 生命周期与 registry 只读
# ---------------------------------------------------------------------------


class FactoryAndRegistryTests(unittest.TestCase):

    def test_factory_called_exactly_once_per_agent(self):
        registry, entries = cross_team()
        build_facade_from_agents(registry, CROSS_IDS, cross_evidence(),
                                 health_for(RT_A, RT_B))
        for _manifest, factory in entries.values():
            self.assertEqual(factory.calls, 1)

    def test_multi_role_agent_factory_still_once(self):
        manifest, factory = agent_entry(
            "agent-solo", RT_A, ("architect", "coder", "tester", "reviewer"))
        build_facade_from_agents(registry_of([manifest]), ("agent-solo",),
                                 evidence_for(RT_A), health_for(RT_A))
        self.assertEqual(factory.calls, 1)

    def test_registry_unchanged_by_build(self):
        registry, _ = cross_team()
        before = registry.list()
        build_facade_from_agents(registry, CROSS_IDS, cross_evidence(),
                                 health_for(RT_A, RT_B))
        self.assertEqual(registry.list(), before)
        self.assertIsNotNone(registry.get("agent-arch"))


# ---------------------------------------------------------------------------
# 诚实错误：组合期封闭拒绝 + V2 admission 裁决原样上抛
# ---------------------------------------------------------------------------


class HonestErrorTests(unittest.TestCase):

    def test_unknown_agent_rejected(self):
        registry, _ = cross_team()
        with self.assertRaises(ValueError) as ctx:
            build_facade_from_agents(
                registry, ("agent-ghost",),
                evidence_for(RT_A, RT_B), health_for(RT_A, RT_B))
        self.assertIn("agent-ghost", str(ctx.exception))

    def test_missing_manifest_rejected_on_empty_registry(self):
        manifest, _ = agent_entry("agent-solo", RT_A,
                                  ("architect", "coder", "tester", "reviewer"))
        with self.assertRaises(ValueError):
            build_facade_from_agents(
                AgentRegistry(), ("agent-solo",),
                evidence_for(RT_A), health_for(RT_A))

    def test_missing_qualification_evidence_rejected(self):
        registry, _ = cross_team()
        with self.assertRaises(ValueError) as ctx:
            build_facade_from_agents(
                registry, CROSS_IDS,
                {RT_A: validation_for(RT_A, caps=("architecture", "review"))},
                health_for(RT_A, RT_B))
        self.assertIn("rt-beta", str(ctx.exception))

    def test_capability_gap_rejected_by_v2_admission(self):
        # RT_A 声明 architect+reviewer（需 architecture+review），但证据
        # 只验证了 architecture —— 组合根不得代补，V2 admission 拒绝并
        # 原样上抛 CAPABILITY_INSUFFICIENT。
        arch, _ = agent_entry("agent-arch", RT_A, ("architect", "reviewer"))
        rest, _ = agent_entry("agent-rest", RT_B,
                              ("coder", "tester"))
        with self.assertRaises(RuntimeError) as ctx:
            build_facade_from_agents(
                registry_of([arch, rest]), ("agent-arch", "agent-rest"),
                {RT_A: validation_for(RT_A, caps=("architecture",)),
                 RT_B: validation_for(RT_B)},
                health_for(RT_A, RT_B))
        self.assertIn("rt-alpha", str(ctx.exception))
        self.assertIn("CAPABILITY_INSUFFICIENT", str(ctx.exception))

    def test_missing_current_health_rejected(self):
        registry, _ = cross_team()
        with self.assertRaises(ValueError) as ctx:
            build_facade_from_agents(
                registry, CROSS_IDS, cross_evidence(), health_for(RT_A))
        self.assertIn("rt-beta", str(ctx.exception))

    def test_v3c_collapse_error_passthrough(self):
        # 同 (runtime, role) 双 agent：V3.0-C 的封闭 message 原样透传。
        first, _ = agent_entry("agent-x", RT_A, ("coder",))
        second, _ = agent_entry("agent-y", RT_A, ("coder",))
        rest, _ = agent_entry("agent-rest", RT_B,
                              ("architect", "tester", "reviewer"))
        with self.assertRaises(ValueError) as ctx:
            build_facade_from_agents(
                registry_of([first, second, rest]),
                ("agent-x", "agent-y", "agent-rest"),
                evidence_for(RT_A, RT_B), health_for(RT_A, RT_B))
        self.assertIn("ambiguous composition", str(ctx.exception))

    def test_evidence_identity_mismatch_rejected(self):
        arch, _ = agent_entry("agent-arch", RT_A, ("architect", "reviewer"))
        rest, _ = agent_entry("agent-rest", RT_B, ("coder", "tester"))
        other = ("rt-other", "provider-z", None, "fp-other")
        with self.assertRaises(ValueError) as ctx:
            build_facade_from_agents(
                registry_of([arch, rest]), ("agent-arch", "agent-rest"),
                {RT_A: validation_for(RT_A, caps=("architecture", "review")),
                 RT_B: validation_for(other)},
                health_for(RT_A, RT_B))
        self.assertIn("rt-beta", str(ctx.exception))

    def test_mixed_provenance_rejected(self):
        arch, _ = agent_entry("agent-arch", RT_A, ("architect", "reviewer"))
        rest, _ = agent_entry("agent-rest", RT_B, ("coder", "tester"))
        with self.assertRaises(ValueError) as ctx:
            build_facade_from_agents(
                registry_of([arch, rest]), ("agent-arch", "agent-rest"),
                {RT_A: validation_for(RT_A, caps=("architecture", "review"),
                                      provenance="OFFLINE"),
                 RT_B: validation_for(RT_B, caps=("coding", "testing"),
                                      provenance="REAL")},
                health_for(RT_A, RT_B))
        self.assertIn("provenance", str(ctx.exception))

    def test_extra_validated_capability_rejected(self):
        # GREEN 阶段的真实发现：冻结 bridge 按 validation.validated_
        # capabilities 选择候选（admission 传入的能力集不进入选择），
        # 因此证据含未声明的协作 capability 时，该 runtime 会被选中扮
        # 演无声明者的角色而命中未注册地址 —— 组合期诚实拒绝。
        registry, _ = cross_team()
        with self.assertRaises(ValueError) as ctx:
            build_facade_from_agents(
                registry, CROSS_IDS, evidence_for(RT_A, RT_B),
                health_for(RT_A, RT_B))
        self.assertIn("rt-alpha", str(ctx.exception))
        self.assertIn("declared by no selected agent", str(ctx.exception))


# ---------------------------------------------------------------------------
# determinism
# ---------------------------------------------------------------------------


class DeterminismTests(unittest.TestCase):

    def test_identical_inputs_identical_outputs(self):
        first_registry, _ = cross_team()
        second_registry, _ = cross_team()
        first = build_facade_from_agents(
            first_registry, CROSS_IDS, cross_evidence(),
            health_for(RT_A, RT_B))
        second = build_facade_from_agents(
            second_registry, CROSS_IDS, cross_evidence(),
            health_for(RT_A, RT_B))
        self.assertEqual(first[1], second[1])
        self.assertEqual(first[0]._pool.identities(),
                         second[0]._pool.identities())
        self.assertEqual(sorted(first[0]._verification_adapters),
                         sorted(second[0]._verification_adapters))


# ---------------------------------------------------------------------------
# 源码纪律扫描
# ---------------------------------------------------------------------------


class SourceScanTests(unittest.TestCase):

    @staticmethod
    def _code_without_docstrings():
        source = (SCRIPTS / "agent_host.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef,
                                 ast.FunctionDef)):
                if (node.body and isinstance(node.body[0], ast.Expr)
                        and isinstance(node.body[0].value, ast.Constant)
                        and isinstance(node.body[0].value.value, str)):
                    node.body = node.body[1:]
        return ast.unparse(tree)

    def test_no_runtime_or_provider_names(self):
        lowered = self._code_without_docstrings().lower()
        for name in ("claude", "codex", "deepseek", "openai", "anthropic",
                     "gemini", "pi-cli", "tiny-agents", "tiny_agents"):
            self.assertNotIn(name, lowered)

    def test_import_surface_closed(self):
        source = (SCRIPTS / "agent_host.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        modules = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    modules.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules.add(node.module.split(".")[0])
        self.assertEqual(sorted(modules), sorted([
            "__future__", "agent_composition", "collaboration_orchestrator",
            "collaboration_session", "host", "loop_guard", "remote_transport",
            "runtime_status", "task_budget", "typing", "verified_orchestrator",
            "verified_runtime_pool", "verified_selection_bridge",
        ]))

    def test_no_agent_identity_or_manifest_import(self):
        # 翻译只经 V3.0-C 槽位：组合根不直接触碰 identity/manifest 模块。
        source = (SCRIPTS / "agent_host.py").read_text(encoding="utf-8")
        for forbidden in ("agent_identity", "agent_manifest"):
            self.assertNotIn(forbidden, source)

    def test_no_selection_or_probe_logic(self):
        # 组合根绝不选择角色、绝不探测 runtime、绝不调用 adapter。
        code = self._code_without_docstrings()
        for forbidden in (
                "candidates_for", "VerifiedRoleCandidate", "ConvergingAssigner",
                "PolicyConstrainedAssigner", "role_assignment", "assigner",
                ".assign(", "check_authentication", "check_provider_model",
                ".invoke(", "discover(", "subprocess", "os.environ", "uuid",
                "random", "datetime", "socket", "requests", "threading"):
            self.assertNotIn(forbidden, code)

    def test_v2_modules_do_not_import_agent_layers(self):
        v2_files = ("host.py", "production_facade.py",
                    "collaboration_orchestrator.py", "collaboration_session.py",
                    "verified_stage_selector.py", "verified_runtime_pool.py",
                    "role_assignment.py", "verified_selection_bridge.py",
                    "verification_collaboration.py", "verified_orchestrator.py",
                    "cli.py")
        for name in v2_files:
            source = (SCRIPTS / name).read_text(encoding="utf-8")
            for forbidden in ("agent_host", "agent_composition",
                              "agent_manifest", "agent_identity"):
                self.assertNotIn(forbidden, source, name)


# ---------------------------------------------------------------------------
# V2 文件本轮零修改（工作树视角）
# ---------------------------------------------------------------------------


_V2_FILES = (
    "dual-agent-development/scripts/host.py",
    "dual-agent-development/scripts/production_facade.py",
    "dual-agent-development/scripts/collaboration_orchestrator.py",
    "dual-agent-development/scripts/collaboration_session.py",
    "dual-agent-development/scripts/verified_stage_selector.py",
    "dual-agent-development/scripts/verified_runtime_pool.py",
    "dual-agent-development/scripts/role_assignment.py",
    "dual-agent-development/scripts/verified_selection_bridge.py",
    "dual-agent-development/scripts/verification_collaboration.py",
    "dual-agent-development/scripts/verified_orchestrator.py",
    "dual-agent-development/scripts/execution_observation.py",
    "dual-agent-development/scripts/console_observation.py",
    "dual-agent-development/scripts/cli.py",
)


class V2ZeroDiffTests(unittest.TestCase):

    def test_v2_files_unmodified_in_working_tree(self):
        import shutil
        if shutil.which("git") is None:
            self.skipTest("git not available")
        repo = Path(__file__).resolve().parents[1]
        for relpath in _V2_FILES:
            proc = subprocess.run(
                ["git", "status", "--porcelain", "--", relpath],
                cwd=str(repo), capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, relpath)
            self.assertEqual(proc.stdout, "",
                             f"{relpath} modified: {proc.stdout!r}")


# ---------------------------------------------------------------------------
# 受保护 untracked 文件（git 视角原样）
# ---------------------------------------------------------------------------


_PROTECTED = (
    "tests/test_policy_boundary_qualification.py",
    "tests/test_real_cli_policy_collaboration.py",
)


class ProtectedUntrackedTests(unittest.TestCase):

    def test_protected_untracked_files_still_untracked(self):
        import shutil
        if shutil.which("git") is None:
            self.skipTest("git not available")
        for relpath in _PROTECTED:
            if not (Path(__file__).resolve().parents[1] / relpath).exists():
                self.skipTest(f"missing protected file: {relpath}")
            proc = subprocess.run(
                ["git", "status", "--porcelain", "--", relpath],
                cwd=str(Path(__file__).resolve().parents[1]),
                capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, relpath)
            self.assertTrue(
                proc.stdout.startswith("?? "),
                f"{relpath} expected untracked, got: {proc.stdout!r}")


if __name__ == "__main__":
    unittest.main()
