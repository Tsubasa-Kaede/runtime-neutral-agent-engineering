"""V3.0-G: Agent team bootstrap tests — E→F→D 的第一个真正消费者。

Red-first：agent_bootstrap.py 尚不存在时本文件必然失败（ImportError）。
期望值是独立字面量或既有模块调用（V2 的 collab_agent_address /
agent_id_for、V3.0-D 的 build_facade_from_agents、V3.0-E/F 的投影），
绝不从被测模块自身源码派生。

锁定（Boundary Review 方案 A + 实现授权）：
1. 成功：entries + (facade, attribution) 与 V3.0-D 直接组合等价；
   facade 四阶段与 SINGLE 离线真实跑通（mock adapters，REAL=0）
2. E liveness 预检：死绑定 → 结构化 entries（E 受控 reason 逐字携带）
   + facade/attribution 为 None，绝不 raising；探测恰一次/distinct
   队伍 runtime；非队伍声明的 runtime 永不被探测；发现失败短路
   能力预检（证据 identity 不符的畸形输入不再被触碰）
3. F 能力预检：词表内声明未获 runtime 背书（DECLARED_ONLY，含证据
   缺失与非 VERIFIED 的空 validated 集）→ 结构化逐 agent 诊断；
   BEYOND_VOCABULARY 是诚实边界不是失败——bootstrap 不得比 D 严
4. D raising 权威不变：预检全绿后 D 的组合期裁决（多余协作证据 /
   未知 agent / 缺 current_health / 混合 provenance / 覆盖缺口 /
   地址塌缩 / 空 agent_ids / 重复 agent_ids）与输入契约错误（非
   callable 探测、畸形发现事实、证据 identity 不符）原样上抛
5. factory 生命周期：预检失败零调用；成功恰一次/agent（V3.0-C 契约
   经 D 保持）；registry 只读
6. entries 引用 E/F 事实：backing 与 project_agent_capabilities 直接
   投影相等；字段词表封闭且无 verified/trusted/admitted/status 语义
7. determinism：同输入同输出
8. 源码纪律：零 runtime/provider 名、import 面封闭、零探测/选择/
   准入/admission 表面；下层模块不 import agent_bootstrap（无环）
9. V2 文件本轮零修改；两个受保护 untracked 文件保持原样
"""
import ast
import json
import subprocess
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from agent_bootstrap import (
    AgentTeamBootstrap,
    AgentTeamEntry,
    bootstrap_agent_team,
    bootstrap_field_names,
    entry_field_names,
)
from agent_capability import CapabilityBacking, project_agent_capabilities
from agent_host import build_facade_from_agents
from agent_identity import AgentIdentity, AgentRuntimeBinding
from agent_manifest import AgentManifest, AgentRegistry
from candidate_validation import (
    CandidateValidationResult,
    CandidateValidationStatus,
    GateResult,
    GateVerdict,
    ValidationGate,
)
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

RT_A = ("rt-alpha", "provider-x", None, "fp-alpha")
RT_B = ("rt-beta", "provider-y", None, "fp-beta")
RT_C = ("rt-gamma", "provider-z", None, "fp-gamma")
RT_A_ALT_FINGERPRINT = ("rt-alpha", "provider-x", None, "fp-beta")
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
        invocation_id="inv-v3g", task_id="t", agent_id="a", runtime="rt",
        provider=None, model=None, role=None, status=InvocationStatus.SUCCESS,
        started_at=0.0, finished_at=0.0, duration_ms=1, exit_code=0,
        input_tokens="unknown", output_tokens="unknown", error=None)


class RoleAdapter:
    """Offline adapter: routes by V2 agent_id shape (bare 4-tuple JSON =
    SINGLE executor; role-suffixed address = collaboration packet) —
    runtime-neutral, no identity constants."""

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


def validation_for(identity, caps=CAPS_ALL, provenance="OFFLINE",
                   status=CandidateValidationStatus.VERIFIED):
    return CandidateValidationResult(
        identity=tuple(identity), status=status,
        gates_passed=frozenset(ValidationGate),
        gate_results=tuple(GateResult(g, GateVerdict.PASS)
                           for g in ValidationGate),
        block_reason=None, failure_point=None, experiment_id="v3g-exp",
        executed_at=0.0, validated_capabilities=tuple(caps), evidence={},
        provenance=provenance)


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


class Fact:
    """V2 RuntimeCandidate 形状的发现事实替身（duck-typed）。"""

    def __init__(self, available, reason=None):
        self.available = available
        self.reason = reason


class TeamDiscovery:
    """注入的 runtime 粒度探测 callable 替身：默认全部可发现，可按
    runtime_id 关闭或整体抛错；记录每次调用收到的完整 identity。"""

    def __init__(self, dead=(), error=None):
        self.dead = set(dead)
        self.error = error
        self.calls = []

    def __call__(self, identity):
        self.calls.append(tuple(identity))
        if self.error is not None:
            raise self.error
        if identity[0] in self.dead:
            return Fact(False, "NOT_FOUND: no source registered")
        return Fact(True)


class _ConstDiscovery:
    """恒返回同一（畸形）事实的探测替身。"""

    def __init__(self, fact):
        self.fact = fact

    def __call__(self, identity):
        return self.fact


def bootstrap_cross(discovery=None, evidence=None, health=None):
    """跨 runtime 队伍的标准 bootstrap 调用（默认全绿输入）。"""
    registry, entries = cross_team()
    result = bootstrap_agent_team(
        registry, CROSS_IDS,
        discover_runtime=discovery or TeamDiscovery(),
        evidence=evidence if evidence is not None else cross_evidence(),
        current_health=health if health is not None
        else health_for(RT_A, RT_B))
    factories = {agent_id: factory
                 for agent_id, (_m, factory) in entries.items()}
    return result, registry, factories


def entry_by_agent(result):
    return {entry.agent_id: entry for entry in result.entries}


# ---------------------------------------------------------------------------
# 组队契约：返回物、字段词表、成功 entries
# ---------------------------------------------------------------------------


class BootstrapContractTests(unittest.TestCase):

    def test_success_returns_facade_attribution_and_entries(self):
        result, _registry, _factories = bootstrap_cross()
        self.assertIsInstance(result, AgentTeamBootstrap)
        self.assertIsInstance(result.facade, ProductionFacade)
        self.assertIsInstance(result.attribution, dict)

    def test_field_vocabularies_closed(self):
        self.assertEqual(
            bootstrap_field_names(), ("entries", "facade", "attribution"))
        self.assertEqual(entry_field_names(), (
            "agent_id", "runtime_identity", "discovered", "reason",
            "backing", "addresses"))

    def test_entry_fields_carry_no_lifecycle_claims(self):
        # 组队事实之外的生命周期语义（verified/trusted/admitted/status/
        # score/health）绝不进入 entry 字段词表——分层各归其 artifact。
        for name in entry_field_names():
            for forbidden in ("verified", "trusted", "admitted", "status",
                              "score", "health"):
                self.assertNotIn(forbidden, name)

    def test_success_entries_scoped_sorted_and_fully_backed(self):
        result, _registry, _factories = bootstrap_cross()
        self.assertEqual([entry.agent_id for entry in result.entries],
                         sorted(CROSS_IDS))
        by_agent = entry_by_agent(result)
        # 与 V2 collab_agent_address 独立推导的期望地址字节一致。
        self.assertEqual(by_agent["agent-arch"].addresses,
                         (collab_agent_address(RT_A, "architect"),))
        self.assertEqual(by_agent["agent-code"].addresses,
                         (collab_agent_address(RT_B, "coder"),))
        self.assertEqual(by_agent["agent-test"].addresses,
                         (collab_agent_address(RT_B, "tester"),))
        self.assertEqual(by_agent["agent-review"].addresses,
                         (collab_agent_address(RT_A, "reviewer"),))
        for entry in result.entries:
            self.assertTrue(entry.discovered)
            self.assertIsNone(entry.reason)
            self.assertTrue(all(
                fact.backing is CapabilityBacking.RUNTIME_BACKED
                for fact in entry.backing))

    def test_backing_references_f_projection_verbatim(self):
        # backing 必须是 F 直接投影的逐字引用事实（独立既有模块计算）。
        registry, _ = cross_team()
        result = bootstrap_agent_team(
            registry, CROSS_IDS, discover_runtime=TeamDiscovery(),
            evidence=cross_evidence(), current_health=health_for(RT_A, RT_B))
        expected = {view.agent_id: view.capabilities
                    for view in project_agent_capabilities(
                        registry, cross_evidence())}
        by_agent = entry_by_agent(result)
        for agent_id, capabilities in expected.items():
            self.assertEqual(by_agent[agent_id].backing, capabilities)

    def test_failure_returns_none_facade_and_attribution(self):
        result, _registry, factories = bootstrap_cross(
            discovery=TeamDiscovery(dead=("rt-alpha",)))
        self.assertIsNone(result.facade)
        self.assertIsNone(result.attribution)
        for entry in result.entries:
            self.assertEqual(entry.addresses, ())
        for factory in factories.values():
            self.assertEqual(factory.calls, 0)

    def test_facade_and_attribution_coherence_guard(self):
        # 全有或全无：facade 与 attribution 不得只出现一个。
        with self.assertRaises(ValueError):
            AgentTeamBootstrap(entries=(), facade=object(), attribution=None)
        with self.assertRaises(ValueError):
            AgentTeamBootstrap(entries=(), facade=None, attribution={})

    def test_composed_entry_reason_guard(self):
        manifest, _ = agent_entry(
            "agent-a", RT_A, ("architect", "coder", "tester", "reviewer"))
        with self.assertRaises(ValueError):
            AgentTeamEntry(agent_id="agent-a", runtime_identity=RT_A,
                           discovered=True, reason="LATE",
                           backing=(), addresses=("addr",))
        with self.assertRaises(ValueError):
            AgentTeamEntry(agent_id="agent-a", runtime_identity=RT_A,
                           discovered=False, reason=None, backing=(),
                           addresses=())


# ---------------------------------------------------------------------------
# E liveness 预检：结构化失败、探测范围、短路
# ---------------------------------------------------------------------------


class DiscoveryPreflightTests(unittest.TestCase):

    def test_dead_binding_blocks_team_structured(self):
        result, _registry, _factories = bootstrap_cross(
            discovery=TeamDiscovery(dead=("rt-alpha",)))
        by_agent = entry_by_agent(result)
        # E 的受控 reason 逐字携带，绝不改写、绝不 raising。
        self.assertEqual(by_agent["agent-arch"].discovered, False)
        self.assertEqual(by_agent["agent-arch"].reason,
                         "NOT_FOUND: no source registered")
        self.assertEqual(by_agent["agent-review"].discovered, False)
        # 同队其余 agent 的 per-agent 事实照常报告（引用不复制）。
        self.assertEqual(by_agent["agent-code"].discovered, True)
        self.assertIsNone(by_agent["agent-code"].reason)
        # 发现预检短路：能力预检未进行，backing 留空（未计算，非空声明）。
        self.assertEqual(by_agent["agent-code"].backing, ())

    def test_discovery_exception_converged_not_raised(self):
        result, _registry, _factories = bootstrap_cross(
            discovery=TeamDiscovery(error=RuntimeError("probe blew up")))
        by_agent = entry_by_agent(result)
        self.assertIsNone(result.facade)
        self.assertTrue(by_agent["agent-arch"].reason.startswith(
            "NOT_FOUND: discovery error"))

    def test_discovery_failure_short_circuits_capability_preflight(self):
        # 死绑定 + 畸形证据（identity 与键不符）：证据不再被触碰，
        # 绝不抛错——预检短路是队伍级事实。
        other = ("rt-other", "provider-w", None, "fp-other")
        mismatched = {RT_A: validation_for(RT_A, caps=("architecture", "review")),
                      RT_B: validation_for(other)}
        result, _registry, _factories = bootstrap_cross(
            discovery=TeamDiscovery(dead=("rt-alpha",)), evidence=mismatched)
        self.assertIsNone(result.facade)

    def test_non_callable_discovery_rejected(self):
        registry, _ = cross_team()
        with self.assertRaises(ValueError):
            bootstrap_agent_team(
                registry, CROSS_IDS, discover_runtime="not-callable",
                evidence=cross_evidence(),
                current_health=health_for(RT_A, RT_B))

    def test_malformed_discovery_fact_rejected(self):
        registry, _ = cross_team()
        with self.assertRaises(ValueError) as ctx:
            bootstrap_agent_team(
                registry, CROSS_IDS,
                discover_runtime=_ConstDiscovery(object()),
                evidence=cross_evidence(),
                current_health=health_for(RT_A, RT_B))
        self.assertIn("available", str(ctx.exception))

    def test_probes_once_per_distinct_team_runtime(self):
        # 同 runtime 两 agent（arch 归 a、其余归 b）：探测恰一次。
        arch, _ = agent_entry("agent-a", RT_A, ("architect",))
        rest, _ = agent_entry("agent-b", RT_A, ("coder", "tester", "reviewer"))
        registry = registry_of([arch, rest])
        discovery = TeamDiscovery()
        result = bootstrap_agent_team(
            registry, ("agent-b", "agent-a"), discover_runtime=discovery,
            evidence={RT_A: validation_for(RT_A)},
            current_health=health_for(RT_A))
        self.assertEqual(discovery.calls, [RT_A])
        self.assertIsInstance(result.facade, ProductionFacade)

    def test_same_runtime_id_different_fingerprint_two_probes(self):
        arch, _ = agent_entry("agent-a", RT_A, ("architect",))
        rest, _ = agent_entry(
            "agent-b", RT_A_ALT_FINGERPRINT, ("coder", "tester", "reviewer"))
        registry = registry_of([arch, rest])
        discovery = TeamDiscovery()
        bootstrap_agent_team(
            registry, ("agent-a", "agent-b"), discover_runtime=discovery,
            evidence={RT_A: validation_for(RT_A, caps=("architecture",)),
                      RT_A_ALT_FINGERPRINT: validation_for(
                          RT_A_ALT_FINGERPRINT,
                          caps=("coding", "review", "testing"))},
            current_health=health_for(RT_A, RT_A_ALT_FINGERPRINT))
        self.assertEqual(discovery.calls, [RT_A, RT_A_ALT_FINGERPRINT])

    def test_non_team_runtimes_never_probed(self):
        # registry 里有队伍之外的声明（RT_C）：预检范围 = 队伍范围，
        # RT_C 绝不被探测，其 entry 也不存在。
        extra, _ = agent_entry("agent-extra", RT_C, ("coder",))
        registry, _ = cross_team()
        registry.register(extra)
        discovery = TeamDiscovery()
        result = bootstrap_agent_team(
            registry, CROSS_IDS, discover_runtime=discovery,
            evidence=cross_evidence(), current_health=health_for(RT_A, RT_B))
        self.assertEqual(discovery.calls, [RT_A, RT_B])
        self.assertEqual(sorted(entry.agent_id for entry in result.entries),
                         sorted(CROSS_IDS))
        self.assertIsInstance(result.facade, ProductionFacade)


# ---------------------------------------------------------------------------
# F 能力预检：结构化诊断；不比 D 严
# ---------------------------------------------------------------------------


class CapabilityPreflightTests(unittest.TestCase):

    def test_missing_evidence_structured_declared_only(self):
        # 缺一支 runtime 的证据：D 直接组合会 raising（输入完整性），
        # bootstrap 预检把同一事实前置成结构化诊断——非 raising。
        evidence = {RT_A: validation_for(RT_A, caps=("architecture", "review"))}
        result, _registry, factories = bootstrap_cross(evidence=evidence)
        self.assertIsNone(result.facade)
        by_agent = entry_by_agent(result)
        self.assertIsNone(by_agent["agent-arch"].reason)
        self.assertEqual(by_agent["agent-code"].reason,
                         "CAPABILITY_DECLARED_ONLY")
        self.assertEqual(by_agent["agent-test"].reason,
                         "CAPABILITY_DECLARED_ONLY")
        coder_facts = {fact.role: fact
                       for fact in by_agent["agent-code"].backing}
        self.assertIs(coder_facts["coder"].backing,
                      CapabilityBacking.DECLARED_ONLY)
        for factory in factories.values():
            self.assertEqual(factory.calls, 0)

    def test_unverified_evidence_structured_declared_only(self):
        # 非 VERIFIED 的资格结果其 validated_capabilities 为空（V2 规则）
        # ——预检照实投影为未背书，结构化拒绝。
        evidence = {
            RT_A: validation_for(RT_A, caps=("architecture", "review")),
            RT_B: validation_for(RT_B, caps=(),
                                 status=CandidateValidationStatus.BLOCKED),
        }
        result, _registry, _factories = bootstrap_cross(evidence=evidence)
        self.assertIsNone(result.facade)
        by_agent = entry_by_agent(result)
        self.assertEqual(by_agent["agent-code"].reason,
                         "CAPABILITY_DECLARED_ONLY")

    def test_capability_gap_equivalence_with_host(self):
        # 同一能力缺口输入：D 直接组合 raising CAPABILITY_INSUFFICIENT；
        # bootstrap 结构化返回。两种消费形态、同一事实，不矛盾。
        lead, _ = agent_entry("agent-lead", RT_A, ("architect", "coder"))
        verify, _ = agent_entry("agent-verify", RT_B, ("tester", "reviewer"))
        registry = registry_of([lead, verify])
        evidence = {
            RT_A: validation_for(RT_A, caps=("architecture",)),
            RT_B: validation_for(RT_B, caps=("testing", "review")),
        }
        with self.assertRaises(RuntimeError) as ctx:
            build_facade_from_agents(registry, ("agent-lead", "agent-verify"),
                                     evidence, health_for(RT_A, RT_B))
        self.assertIn("CAPABILITY_INSUFFICIENT", str(ctx.exception))
        result = bootstrap_agent_team(
            registry, ("agent-lead", "agent-verify"),
            discover_runtime=TeamDiscovery(), evidence=evidence,
            current_health=health_for(RT_A, RT_B))
        self.assertIsNone(result.facade)
        self.assertEqual(entry_by_agent(result)["agent-lead"].reason,
                         "CAPABILITY_DECLARED_ONLY")

    def test_beyond_vocabulary_declaration_still_composes(self):
        # 词表外声明（summarizer）：F 标注 BEYOND_VOCABULARY 是诚实边界，
        # 不是失败——bootstrap 不得比 D 严，组合照常成功，entry 如实记录。
        solo, _ = agent_entry(
            "agent-solo", RT_A,
            ("architect", "coder", "summarizer", "tester", "reviewer"))
        registry = registry_of([solo])
        result = bootstrap_agent_team(
            registry, ("agent-solo",), discover_runtime=TeamDiscovery(),
            evidence={RT_A: validation_for(RT_A)},
            current_health=health_for(RT_A))
        self.assertIsInstance(result.facade, ProductionFacade)
        entry = entry_by_agent(result)["agent-solo"]
        self.assertIsNone(entry.reason)
        self.assertEqual(len(entry.addresses), 4)
        beyond = [fact for fact in entry.backing
                  if fact.role == "summarizer"]
        self.assertEqual(len(beyond), 1)
        self.assertIs(beyond[0].backing, CapabilityBacking.BEYOND_VOCABULARY)
        self.assertIsNone(beyond[0].capability)


# ---------------------------------------------------------------------------
# D raising 权威不变：预检全绿后的组合期裁决原样上抛
# ---------------------------------------------------------------------------


class HostVerdictPassthroughTests(unittest.TestCase):

    def test_extra_validated_capability_still_raises(self):
        # 证据含未被任何选定 agent 声明的协作 capability：团队级裁决权
        # 在 D（F 视图刻意不重复），原样上抛。
        result = None
        with self.assertRaises(ValueError) as ctx:
            result, _registry, _factories = bootstrap_cross(
                evidence={RT_A: validation_for(RT_A),
                          RT_B: validation_for(RT_B, caps=("coding", "testing"))})
        self.assertIsNone(result)
        self.assertIn("declared by no selected agent", str(ctx.exception))

    def test_unknown_agent_raises_from_composition(self):
        registry, _ = cross_team()
        with self.assertRaises(ValueError) as ctx:
            bootstrap_agent_team(
                registry, CROSS_IDS + ("agent-ghost",),
                discover_runtime=TeamDiscovery(),
                evidence=cross_evidence(),
                current_health=health_for(RT_A, RT_B))
        self.assertIn("agent-ghost", str(ctx.exception))

    def test_missing_current_health_raises_from_host(self):
        # 健康完整性属 D 的输入契约（预检不含健康语义），原样上抛。
        with self.assertRaises(ValueError) as ctx:
            bootstrap_cross(health=health_for(RT_A))
        self.assertIn("no current health for runtime: rt-beta",
                      str(ctx.exception))

    def test_collapse_error_passthrough(self):
        first, _ = agent_entry("agent-x", RT_A, ("coder",))
        second, _ = agent_entry("agent-y", RT_A, ("coder",))
        rest, _ = agent_entry("agent-rest", RT_B,
                              ("architect", "tester", "reviewer"))
        registry = registry_of([first, second, rest])
        with self.assertRaises(ValueError) as ctx:
            bootstrap_agent_team(
                registry, ("agent-x", "agent-y", "agent-rest"),
                discover_runtime=TeamDiscovery(),
                evidence={RT_A: validation_for(RT_A, caps=("coding",)),
                          RT_B: validation_for(
                              RT_B, caps=("architecture", "testing", "review"))},
                current_health=health_for(RT_A, RT_B))
        self.assertIn("ambiguous composition", str(ctx.exception))

    def test_coverage_gap_raises_from_composition(self):
        lead, _ = agent_entry("agent-lead", RT_A, ("architect", "coder"))
        registry = registry_of([lead])
        with self.assertRaises(ValueError) as ctx:
            bootstrap_agent_team(
                registry, ("agent-lead",), discover_runtime=TeamDiscovery(),
                evidence={RT_A: validation_for(RT_A, caps=("architecture",
                                                           "coding"))},
                current_health=health_for(RT_A))
        self.assertIn("required role declared by no requested agent",
                      str(ctx.exception))

    def test_mixed_provenance_raises_from_host(self):
        evidence = {
            RT_A: validation_for(RT_A, caps=("architecture", "review"),
                                 provenance="OFFLINE"),
            RT_B: validation_for(RT_B, caps=("coding", "testing"),
                                 provenance="REAL"),
        }
        with self.assertRaises(ValueError) as ctx:
            bootstrap_cross(evidence=evidence)
        self.assertIn("provenance", str(ctx.exception))

    def test_empty_agent_ids_rejected(self):
        registry, _ = cross_team()
        with self.assertRaises(ValueError) as ctx:
            bootstrap_agent_team(
                registry, (), discover_runtime=TeamDiscovery(),
                evidence=cross_evidence(),
                current_health=health_for(RT_A, RT_B))
        self.assertIn("must not be empty", str(ctx.exception))

    def test_duplicate_agent_ids_rejected_by_composition(self):
        # 组队范围去重仅用于探测范围；重复项的裁决权在 C/D。
        solo, _ = agent_entry(
            "agent-solo", RT_A, ("architect", "coder", "tester", "reviewer"))
        registry = registry_of([solo])
        with self.assertRaises(ValueError) as ctx:
            bootstrap_agent_team(
                registry, ("agent-solo", "agent-solo"),
                discover_runtime=TeamDiscovery(),
                evidence={RT_A: validation_for(RT_A)},
                current_health=health_for(RT_A))
        self.assertIn("must not contain duplicates", str(ctx.exception))

    def test_evidence_identity_mismatch_raises(self):
        other = ("rt-other", "provider-w", None, "fp-other")
        evidence = {RT_A: validation_for(RT_A, caps=("architecture", "review")),
                    RT_B: validation_for(other)}
        with self.assertRaises(ValueError) as ctx:
            bootstrap_cross(evidence=evidence)
        self.assertIn("evidence identity mismatch for runtime: rt-beta",
                      str(ctx.exception))


# ---------------------------------------------------------------------------
# 与 V3.0-D 直接组合的等价性 + facade 离线执行（mock adapters，REAL=0）
# ---------------------------------------------------------------------------


class EquivalenceAndExecutionTests(unittest.TestCase):

    def test_bootstrap_equals_direct_host_composition(self):
        registry, _ = cross_team()
        result = bootstrap_agent_team(
            registry, CROSS_IDS, discover_runtime=TeamDiscovery(),
            evidence=cross_evidence(), current_health=health_for(RT_A, RT_B))
        # 同输入的 D 直接组合在 bootstrap 之后照常成立（registry 只读）。
        direct_facade, direct_attribution = build_facade_from_agents(
            registry, CROSS_IDS, cross_evidence(), health_for(RT_A, RT_B))
        self.assertEqual(result.attribution, direct_attribution)
        self.assertEqual(result.facade._pool.identities(),
                         direct_facade._pool.identities())
        self.assertEqual(sorted(result.facade._verification_adapters),
                         sorted(direct_facade._verification_adapters))

    def test_bootstrapped_facade_runs_four_stage(self):
        result, _registry, factories = bootstrap_cross()
        run = result.facade.run(task_id="v3g-1", task=TASK_COMPLEX,
                                prompt=TASK_COMPLEX, mode=Mode.ON)
        self.assertEqual(run.status, "SUCCESS")
        self.assertEqual(run.path, "FOUR_STAGE")
        self.assertEqual(run.stages,
                         ("architect", "coder", "tester", "reviewer"))
        for agent_id in CROSS_IDS:
            self.assertEqual(factories[agent_id].product.invocations, 1,
                             agent_id)
        # adapter 实际看到的 V2 地址与 entry.addresses 字节一致。
        self.assertEqual(factories["agent-arch"].product.addresses,
                         [collab_agent_address(RT_A, "architect")])
        self.assertEqual(factories["agent-code"].product.addresses,
                         [collab_agent_address(RT_B, "coder")])
        self.assertEqual(factories["agent-test"].product.addresses,
                         [collab_agent_address(RT_B, "tester")])
        self.assertEqual(factories["agent-review"].product.addresses,
                         [collab_agent_address(RT_A, "reviewer")])

    def test_bootstrapped_facade_single_routes_to_coder_agent(self):
        result, _registry, factories = bootstrap_cross()
        run = result.facade.run(task_id="v3g-2", task=TASK_SIMPLE,
                                prompt=TASK_SIMPLE, mode=Mode.AUTO)
        self.assertEqual(run.path, "SINGLE")
        self.assertEqual(run.status, "SUCCESS", run.failure_category)
        self.assertEqual(factories["agent-arch"].product.invocations, 0)
        self.assertEqual(factories["agent-code"].product.invocations, 1)
        self.assertEqual(factories["agent-code"].product.addresses,
                         [agent_id_for(RT_B)])
        self.assertEqual(factories["agent-test"].product.invocations, 0)
        self.assertEqual(factories["agent-review"].product.invocations, 0)


# ---------------------------------------------------------------------------
# factory 生命周期与 registry 只读
# ---------------------------------------------------------------------------


class FactoryAndRegistryTests(unittest.TestCase):

    def test_factory_once_per_agent_on_success(self):
        result, _registry, factories = bootstrap_cross()
        self.assertIsInstance(result.facade, ProductionFacade)
        for factory in factories.values():
            self.assertEqual(factory.calls, 1)

    def test_registry_unchanged_by_bootstrap(self):
        registry, _ = cross_team()
        before = registry.list()
        bootstrap_agent_team(
            registry, CROSS_IDS, discover_runtime=TeamDiscovery(),
            evidence=cross_evidence(), current_health=health_for(RT_A, RT_B))
        self.assertEqual(registry.list(), before)
        self.assertIsNotNone(registry.get("agent-arch"))

    def test_capability_preflight_failure_never_calls_factories(self):
        evidence = {RT_A: validation_for(RT_A, caps=("architecture",)),
                    RT_B: validation_for(RT_B, caps=("coding", "testing"))}
        result, _registry, factories = bootstrap_cross(evidence=evidence)
        self.assertIsNone(result.facade)
        for factory in factories.values():
            self.assertEqual(factory.calls, 0)


# ---------------------------------------------------------------------------
# determinism
# ---------------------------------------------------------------------------


class DeterminismTests(unittest.TestCase):

    def test_identical_inputs_identical_outputs(self):
        first_registry, _ = cross_team()
        second_registry, _ = cross_team()
        first = bootstrap_agent_team(
            first_registry, CROSS_IDS, discover_runtime=TeamDiscovery(),
            evidence=cross_evidence(), current_health=health_for(RT_A, RT_B))
        second = bootstrap_agent_team(
            second_registry, CROSS_IDS, discover_runtime=TeamDiscovery(),
            evidence=cross_evidence(), current_health=health_for(RT_A, RT_B))
        self.assertEqual(first.entries, second.entries)
        self.assertEqual(first.attribution, second.attribution)
        self.assertEqual(first.facade._pool.identities(),
                         second.facade._pool.identities())


# ---------------------------------------------------------------------------
# 源码纪律扫描
# ---------------------------------------------------------------------------


class SourceScanTests(unittest.TestCase):

    @staticmethod
    def _code_without_docstrings():
        source = (SCRIPTS / "agent_bootstrap.py").read_text(encoding="utf-8")
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
        source = (SCRIPTS / "agent_bootstrap.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        modules = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    modules.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules.add(node.module.split(".")[0])
        self.assertEqual(sorted(modules), sorted([
            "__future__", "agent_capability", "agent_discovery",
            "agent_host", "agent_manifest", "dataclasses", "typing",
        ]))

    def test_no_direct_composition_or_admission_surface(self):
        # 纯编排：不经 C 直接组合、不经手 admission、绝不触碰 factory /
        # 探测 / 选择 / 执行表面——这些分别锁在 D、V2 pool 与 C。
        code = self._code_without_docstrings()
        for forbidden in (
                "compose_agent_slots", "VerifiedRuntimePool", ".admit(",
                "adapter_factory", "candidates_for", "assigner", ".assign(",
                "check_authentication", "check_provider_model", ".invoke(",
                "RuntimeCandidate", "DiscoverySource", "runtime_discovery",
                "discovery_bootstrap", "runtime_adapter_registry",
                "subprocess", "os.environ", "uuid", "random", "datetime",
                "socket", "requests", "threading"):
            self.assertNotIn(forbidden, code)

    def test_lower_layers_do_not_import_bootstrap(self):
        # 无环：E/F/D/C/B/A 不得 import agent_bootstrap。
        for name in ("agent_identity.py", "agent_manifest.py",
                     "agent_composition.py", "agent_host.py",
                     "agent_discovery.py", "agent_capability.py"):
            source = (SCRIPTS / name).read_text(encoding="utf-8")
            self.assertNotIn("agent_bootstrap", source, name)

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
                              "agent_manifest", "agent_identity",
                              "agent_bootstrap", "agent_discovery",
                              "agent_capability"):
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
