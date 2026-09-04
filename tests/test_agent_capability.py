"""V3.0-F: Agent capability view tests — declared × runtime-evidence join.

Red-first：agent_capability.py 尚不存在时本文件必然失败（ImportError）。
期望值是独立字面量或既有模块调用，绝不从被测模块自身源码派生。

V3.0-F 只回答一个问题：
«这个 Agent 声称/被证明具有什么能力？»——以纯 join 视图（不组合、
不执行、不产证据、不抛能力裁决错误）表达：

    AgentManifest.declared_roles（声明，V3.0-B）
      × evidence（runtime identity -> CandidateValidationResult，V2 资格）
      ↓ project_agent_capabilities（本模块被测物：纯函数投影）
    tuple[AgentCapabilityView]  <- 每 agent 的 per-能力 backing

锁定（Boundary Review + 实现授权）：
1. 必要非充分语义：RUNTIME_BACKED 表示「声明 ∧ runtime 证据在位」
   （必要条件满足），绝不表示「agent 已被验证」——backing 词表封闭
   为三项且不含 VERIFIED 字样（反射 + 值断言锁定）
2. Declared ≠ Verified：DECLARED_ONLY（声明在、证据缺）与
   RUNTIME_BACKED 严格分离；agent 级 VERIFIED 不存在（属未来验证
   阶段），视图绝不输出它
3. 词表诚实边界：声明角色无 V2 capability 投影 -> BEYOND_VOCABULARY
   （capability=None），绝不静默丢弃声明
4. 投影词表三重锁定：本模块 _ROLE_CAPABILITY == agent_host 的
   _ROLE_CAPABILITY == V2 _ROLE_REQUIREMENTS 值（防漂移）
5. 非 raising：能力状态缺失不抛错（与 V3.0-D 的 raising 裁决互补：
   同一不变量的两种消费形态）；输入契约错误（证据 identity 不符）
   仍诚实拒绝
6. registry/evidence 只读；adapter_factory 永不被调用（V3.0-B/C 契约）
7. 与 V3.0-D 一致性：单 agent 全角色时「全部 RUNTIME_BACKED 且无
   词表内多余证据」⟺ build_facade_from_agents 接受；团队级多余证据
   裁决权仍在 D（文档化边界）
8. runtime-neutral + agent-neutral：抽象 agent + 抽象 runtime identity
"""
import ast
import subprocess
import sys
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from agent_capability import (
    AgentCapabilityView,
    CapabilityBacking,
    DeclaredCapability,
    capability_field_names,
    project_agent_capabilities,
    view_field_names,
)
from agent_host import _ROLE_CAPABILITY as HOST_ROLE_CAPABILITY
from agent_identity import AgentIdentity, AgentRuntimeBinding
from agent_manifest import AgentManifest, AgentRegistry
from candidate_validation import (
    CandidateValidationResult,
    CandidateValidationStatus,
    GateResult,
    GateVerdict,
    ValidationGate,
)
from verified_stage_selector import _ROLE_REQUIREMENTS

RT_1 = ("rt-one", "provider-a", None, "fp-one")
RT_1_ALT_FINGERPRINT = ("rt-one", "provider-a", None, "fp-one-b")
RT_2 = ("rt-two", "provider-b", "m-two", "fp-two")
CAPS_ALL = ("architecture", "coding", "review", "testing")


class CountingFactory:
    """callable 替身：计数。视图绝不能调用它（V3.0-B/C 契约）。"""

    def __init__(self):
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return object()


def manifest_of(agent_id, runtime, roles):
    factory = CountingFactory()
    manifest = AgentManifest(
        binding=AgentRuntimeBinding(agent=AgentIdentity(agent_id=agent_id),
                                    runtime_identity=runtime),
        declared_roles=tuple(roles),
        adapter_factory=factory)
    return manifest, factory


def registry_of(*manifests):
    registry = AgentRegistry()
    for manifest in manifests:
        registry.register(manifest)
    return registry


def validation_for(identity, caps=CAPS_ALL,
                   status=CandidateValidationStatus.VERIFIED):
    return CandidateValidationResult(
        identity=tuple(identity), status=status,
        gates_passed=frozenset(ValidationGate),
        gate_results=tuple(GateResult(g, GateVerdict.PASS)
                           for g in ValidationGate),
        block_reason=None, failure_point=None, experiment_id="v3f-exp",
        executed_at=0.0, validated_capabilities=tuple(caps), evidence={},
        provenance="OFFLINE")


def evidence_of(*pairs):
    return {tuple(identity): validation for identity, validation in pairs}


# ---------------------------------------------------------------------------
# 契约：字段封闭、词表封闭、命名诚实
# ---------------------------------------------------------------------------


class ContractTests(unittest.TestCase):

    def test_single_agent_all_roles_fully_backed(self):
        manifest, _ = manifest_of(
            "agent-a", RT_1, ("architect", "coder", "tester", "reviewer"))
        views = project_agent_capabilities(
            registry_of(manifest),
            evidence_of((RT_1, validation_for(RT_1, caps=CAPS_ALL))))
        self.assertEqual(len(views), 1)
        view = views[0]
        self.assertEqual(view.agent_id, "agent-a")
        self.assertEqual(view.runtime_identity, RT_1)
        self.assertEqual(
            [(c.role, c.capability, c.backing) for c in view.capabilities],
            [("architect", "architecture", CapabilityBacking.RUNTIME_BACKED),
             ("coder", "coding", CapabilityBacking.RUNTIME_BACKED),
             ("reviewer", "review", CapabilityBacking.RUNTIME_BACKED),
             ("tester", "testing", CapabilityBacking.RUNTIME_BACKED)])

    def test_view_field_vocabulary_closed_to_three(self):
        self.assertEqual(
            view_field_names(),
            ("agent_id", "runtime_identity", "capabilities"))

    def test_capability_field_vocabulary_closed_to_three(self):
        self.assertEqual(
            capability_field_names(), ("role", "capability", "backing"))

    def test_backing_vocabulary_closed_to_three(self):
        self.assertEqual(
            sorted(item.value for item in CapabilityBacking),
            ["BEYOND_VOCABULARY", "DECLARED_ONLY", "RUNTIME_BACKED"])

    def test_backing_never_claims_agent_verification(self):
        # RUNTIME_BACKED 是必要条件背书，不是 agent 级验证 —— 词表值里
        # 绝不出现 VERIFIED/ADMITTED 等生命周期词。
        for backing in CapabilityBacking:
            lowered = backing.value.lower()
            for forbidden in ("verified", "admitted", "trusted"):
                self.assertNotIn(forbidden, lowered)

    def test_view_is_frozen(self):
        manifest, _ = manifest_of("agent-a", RT_1, ("coder",))
        view = project_agent_capabilities(
            registry_of(manifest),
            evidence_of((RT_1, validation_for(RT_1))))[0]
        with self.assertRaises(FrozenInstanceError):
            view.agent_id = "agent-x"
        with self.assertRaises(FrozenInstanceError):
            view.capabilities[0].backing = CapabilityBacking.DECLARED_ONLY

    def test_declared_capability_rejects_empty_role(self):
        with self.assertRaises(ValueError):
            DeclaredCapability(role="", capability="coding",
                               backing=CapabilityBacking.DECLARED_ONLY)

    def test_backing_and_capability_consistency_guards(self):
        # BEYOND_VOCABULARY ⟺ capability is None；其余状态必须有投影。
        with self.assertRaises(ValueError):
            DeclaredCapability(
                role="coder", capability=None,
                backing=CapabilityBacking.DECLARED_ONLY)
        with self.assertRaises(ValueError):
            DeclaredCapability(
                role="summarizer", capability="coding",
                backing=CapabilityBacking.BEYOND_VOCABULARY)


# ---------------------------------------------------------------------------
# backing 语义：必要非充分
# ---------------------------------------------------------------------------


class BackingTests(unittest.TestCase):

    def test_missing_capability_evidence_is_declared_only(self):
        manifest, _ = manifest_of(
            "agent-a", RT_1, ("coder", "tester"))
        views = project_agent_capabilities(
            registry_of(manifest),
            evidence_of((RT_1, validation_for(RT_1, caps=("coding",)))))
        self.assertEqual(
            [(c.role, c.backing) for c in views[0].capabilities],
            [("coder", CapabilityBacking.RUNTIME_BACKED),
             ("tester", CapabilityBacking.DECLARED_ONLY)])

    def test_no_evidence_for_identity_is_declared_only(self):
        manifest, _ = manifest_of("agent-a", RT_1, ("coder",))
        views = project_agent_capabilities(registry_of(manifest), {})
        self.assertEqual(views[0].capabilities[0].backing,
                         CapabilityBacking.DECLARED_ONLY)

    def test_unverified_evidence_backs_nothing(self):
        # 非 VERIFIED 的资格结果其 validated_capabilities 为空（V2 规则：
        # 短路验证不完整，不算证据）——视图照实投影为 DECLARED_ONLY。
        manifest, _ = manifest_of("agent-a", RT_1, ("coder",))
        blocked = validation_for(
            RT_1, caps=(), status=CandidateValidationStatus.BLOCKED)
        views = project_agent_capabilities(
            registry_of(manifest), evidence_of((RT_1, blocked)))
        self.assertEqual(views[0].capabilities[0].backing,
                         CapabilityBacking.DECLARED_ONLY)

    def test_role_beyond_vocabulary_is_honest_not_dropped(self):
        # 词表外声明（V2 冻结词表无对应 capability）：诚实标注
        # BEYOND_VOCABULARY + capability=None，绝不静默丢弃。
        manifest, _ = manifest_of("agent-a", RT_1, ("coder", "summarizer"))
        views = project_agent_capabilities(
            registry_of(manifest),
            evidence_of((RT_1, validation_for(RT_1, caps=CAPS_ALL))))
        by_role = {c.role: c for c in views[0].capabilities}
        self.assertEqual(by_role["summarizer"].backing,
                         CapabilityBacking.BEYOND_VOCABULARY)
        self.assertIsNone(by_role["summarizer"].capability)
        self.assertEqual(by_role["coder"].backing,
                         CapabilityBacking.RUNTIME_BACKED)


# ---------------------------------------------------------------------------
# 多 Agent / 多 Runtime
# ---------------------------------------------------------------------------


class MultiAgentTests(unittest.TestCase):

    def test_two_agents_one_runtime_separate_views(self):
        # 同 runtime 双 agent：共享同一份 runtime 证据作为各自前提，
        # 声明侧各自独立 —— 绝不同化成一个视图。
        first, _ = manifest_of("agent-a", RT_1, ("coder",))
        second, _ = manifest_of("agent-b", RT_1, ("tester", "reviewer"))
        views = project_agent_capabilities(
            registry_of(first, second),
            evidence_of((RT_1, validation_for(
                RT_1, caps=("coding", "testing", "review")))))
        self.assertEqual([v.agent_id for v in views], ["agent-a", "agent-b"])
        self.assertEqual(
            [(c.role, c.backing) for c in views[0].capabilities],
            [("coder", CapabilityBacking.RUNTIME_BACKED)])
        self.assertEqual(
            [(c.role, c.backing) for c in views[1].capabilities],
            [("reviewer", CapabilityBacking.RUNTIME_BACKED),
             ("tester", CapabilityBacking.RUNTIME_BACKED)])

    def test_same_runtime_id_different_fingerprint_distinct(self):
        first, _ = manifest_of("agent-a", RT_1, ("coder",))
        second, _ = manifest_of("agent-b", RT_1_ALT_FINGERPRINT, ("coder",))
        views = project_agent_capabilities(
            registry_of(first, second),
            evidence_of((RT_1, validation_for(RT_1, caps=("coding",)))))
        by_agent = {v.agent_id: v for v in views}
        self.assertEqual(by_agent["agent-a"].runtime_identity, RT_1)
        self.assertEqual(by_agent["agent-b"].runtime_identity,
                         RT_1_ALT_FINGERPRINT)
        self.assertEqual(by_agent["agent-a"].capabilities[0].backing,
                         CapabilityBacking.RUNTIME_BACKED)
        self.assertEqual(by_agent["agent-b"].capabilities[0].backing,
                         CapabilityBacking.DECLARED_ONLY)

    def test_multi_runtime_independent_backing(self):
        first, _ = manifest_of("agent-a", RT_1, ("coder",))
        second, _ = manifest_of("agent-b", RT_2, ("coder",))
        views = project_agent_capabilities(
            registry_of(first, second),
            evidence_of((RT_2, validation_for(RT_2, caps=("coding",)))))
        by_agent = {v.agent_id: v for v in views}
        self.assertEqual(by_agent["agent-a"].capabilities[0].backing,
                         CapabilityBacking.DECLARED_ONLY)
        self.assertEqual(by_agent["agent-b"].capabilities[0].backing,
                         CapabilityBacking.RUNTIME_BACKED)


# ---------------------------------------------------------------------------
# 与 V3.0-D 的一致性（同一不变量的两种消费形态）
# ---------------------------------------------------------------------------


class ConsistencyWithHostTests(unittest.TestCase):

    @staticmethod
    def _health_for(*identities):
        from runtime_status import (
            HealthEvidence, ReasonCode, RuntimeState, RuntimeStatus)
        return {identity[0]: RuntimeStatus(
            runtime_id=identity[0], executable="e", version="1",
            status=RuntimeState.READY, provider=identity[1], model=None,
            auth_method=None, reason_code=ReasonCode.NONE,
            evidence=HealthEvidence("d", "a", "p", "m", "ok"),
            checked_at=0.0, expires_at=1.0) for identity in identities}

    def test_fully_backed_implies_host_accepts(self):
        from agent_host import build_facade_from_agents
        manifest, _ = manifest_of(
            "agent-a", RT_1, ("architect", "coder", "tester", "reviewer"))
        registry = registry_of(manifest)
        evidence = evidence_of((RT_1, validation_for(RT_1, caps=CAPS_ALL)))
        views = project_agent_capabilities(registry, evidence)
        self.assertTrue(all(
            c.backing is CapabilityBacking.RUNTIME_BACKED
            for c in views[0].capabilities))
        facade, attribution = build_facade_from_agents(
            registry, ("agent-a",), evidence, self._health_for(RT_1))
        self.assertEqual(len(attribution), 4)

    def test_declared_only_implies_host_rejects(self):
        from agent_host import build_facade_from_agents
        manifest, _ = manifest_of(
            "agent-a", RT_1, ("architect", "coder", "tester", "reviewer"))
        registry = registry_of(manifest)
        partial = evidence_of((RT_1, validation_for(
            RT_1, caps=("architecture", "coding", "review"))))
        views = project_agent_capabilities(registry, partial)
        missing = {c.role for c in views[0].capabilities
                   if c.backing is CapabilityBacking.DECLARED_ONLY}
        self.assertEqual(missing, {"tester"})
        with self.assertRaises(RuntimeError) as ctx:
            build_facade_from_agents(
                registry, ("agent-a",), partial, self._health_for(RT_1))
        self.assertIn("CAPABILITY_INSUFFICIENT", str(ctx.exception))

    def test_extra_evidence_is_team_level_and_stays_with_host(self):
        # 双 runtime 团队（四角色全覆盖）：RT_1 证据带词表内多余能力。
        # 视图只报告各 agent 自己的声明（皆 RUNTIME_BACKED）；词表内
        # 多余证据的裁决权在 V3.0-D（团队级双向一致性），视图不重复
        # 也不改写它。
        from agent_host import build_facade_from_agents
        lead, _ = manifest_of("agent-a", RT_1, ("architect", "coder"))
        verify, _ = manifest_of("agent-b", RT_2, ("tester", "reviewer"))
        registry = registry_of(lead, verify)
        evidence = {
            RT_1: validation_for(RT_1, caps=CAPS_ALL),
            RT_2: validation_for(RT_2, caps=("testing", "review")),
        }
        views = project_agent_capabilities(registry, evidence)
        by_agent = {view.agent_id: view for view in views}
        self.assertEqual(
            sorted(c.role for c in by_agent["agent-a"].capabilities),
            ["architect", "coder"])
        self.assertTrue(all(
            c.backing is CapabilityBacking.RUNTIME_BACKED
            for view in views for c in view.capabilities))
        with self.assertRaises(ValueError) as ctx:
            build_facade_from_agents(
                registry, ("agent-a", "agent-b"), evidence,
                self._health_for(RT_1, RT_2))
        self.assertIn("declared by no selected agent", str(ctx.exception))


# ---------------------------------------------------------------------------
# 生命周期：只读、factory 零调用、rebind、输入契约
# ---------------------------------------------------------------------------


class LifecycleTests(unittest.TestCase):

    def test_registry_unchanged(self):
        manifest, _ = manifest_of("agent-a", RT_1, ("coder",))
        registry = registry_of(manifest)
        before = registry.list()
        project_agent_capabilities(
            registry, evidence_of((RT_1, validation_for(RT_1))))
        self.assertEqual(registry.list(), before)
        self.assertIs(registry.get("agent-a"), manifest)

    def test_evidence_mapping_unchanged(self):
        manifest, _ = manifest_of("agent-a", RT_1, ("coder",))
        evidence = evidence_of((RT_1, validation_for(RT_1)))
        before = dict(evidence)
        project_agent_capabilities(registry_of(manifest), evidence)
        self.assertEqual(evidence, before)

    def test_adapter_factory_never_called(self):
        first, first_factory = manifest_of("agent-a", RT_1, ("coder",))
        second, second_factory = manifest_of("agent-b", RT_2, ("tester",))
        project_agent_capabilities(
            registry_of(first, second),
            evidence_of((RT_1, validation_for(RT_1)),
                        (RT_2, validation_for(RT_2))))
        self.assertEqual(first_factory.calls, 0)
        self.assertEqual(second_factory.calls, 0)

    def test_rebind_follows_new_binding(self):
        binding = AgentRuntimeBinding(
            agent=AgentIdentity(agent_id="agent-a"), runtime_identity=RT_1)
        original = AgentManifest(binding=binding, declared_roles=("coder",),
                                 adapter_factory=CountingFactory())
        rebound = AgentManifest(binding=binding.rebind(RT_2),
                                declared_roles=("coder",),
                                adapter_factory=CountingFactory())
        views = project_agent_capabilities(
            registry_of(rebound),
            evidence_of((RT_2, validation_for(RT_2, caps=("coding",)))))
        self.assertEqual(views[0].runtime_identity, RT_2)
        self.assertEqual(views[0].capabilities[0].backing,
                         CapabilityBacking.RUNTIME_BACKED)
        self.assertEqual(original.binding.runtime_identity, RT_1)

    def test_evidence_identity_mismatch_rejected(self):
        manifest, _ = manifest_of("agent-a", RT_1, ("coder",))
        other = ("rt-other", "provider-z", None, "fp-other")
        with self.assertRaises(ValueError) as ctx:
            project_agent_capabilities(
                registry_of(manifest),
                evidence_of((RT_1, validation_for(other))))
        self.assertIn("mismatch", str(ctx.exception))


# ---------------------------------------------------------------------------
# determinism
# ---------------------------------------------------------------------------


class DeterminismTests(unittest.TestCase):

    def test_identical_inputs_identical_outputs(self):
        manifests = [manifest_of(name, runtime, roles)[0]
                     for name, runtime, roles in
                     (("agent-d", RT_2, ("reviewer",)),
                      ("agent-b", RT_1, ("coder",)),
                      ("agent-a", RT_1, ("architect",)),
                      ("agent-c", RT_2, ("tester",)))]
        evidence = evidence_of(
            (RT_1, validation_for(RT_1, caps=("architecture", "coding"))),
            (RT_2, validation_for(RT_2, caps=("review",))))
        first = project_agent_capabilities(registry_of(*manifests), evidence)
        second = project_agent_capabilities(registry_of(*manifests), evidence)
        self.assertEqual(first, second)
        self.assertEqual(first, tuple(sorted(
            first, key=lambda view: view.agent_id)))

    def test_capabilities_sorted_by_role_within_view(self):
        manifest, _ = manifest_of(
            "agent-a", RT_1, ("reviewer", "architect", "coder", "tester"))
        views = project_agent_capabilities(
            registry_of(manifest),
            evidence_of((RT_1, validation_for(RT_1))))
        self.assertEqual([c.role for c in views[0].capabilities],
                         ["architect", "coder", "reviewer", "tester"])

    def test_empty_registry_yields_empty_view(self):
        self.assertEqual(project_agent_capabilities(AgentRegistry(), {}), ())


# ---------------------------------------------------------------------------
# 投影词表三重锁定（防漂移）
# ---------------------------------------------------------------------------


class VocabularyLockTests(unittest.TestCase):

    def test_projection_identical_to_agent_host(self):
        from agent_capability import _ROLE_CAPABILITY
        self.assertEqual(_ROLE_CAPABILITY, HOST_ROLE_CAPABILITY)

    def test_projection_matches_v2_role_requirements(self):
        from agent_capability import _ROLE_CAPABILITY
        stage_of = {"tester": "test", "reviewer": "review"}
        for role, capability in _ROLE_CAPABILITY.items():
            stage = stage_of.get(role, role)
            self.assertEqual(_ROLE_REQUIREMENTS[stage], (capability,))


# ---------------------------------------------------------------------------
# 源码纪律扫描
# ---------------------------------------------------------------------------


class SourceScanTests(unittest.TestCase):

    @staticmethod
    def _code_without_docstrings():
        source = (SCRIPTS / "agent_capability.py").read_text(
            encoding="utf-8")
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

    def test_no_verification_claims_in_code(self):
        # 代码（非文档）绝不出现 verified/score/select —— 视图不是验证、
        # 不是打分（打分公式属 V1 ReadyPool 谱系，永不进入 V3 视图）。
        lowered = self._code_without_docstrings().lower()
        for forbidden in ("verified", "score", "select(", "trusted",
                          "admitted", "admit"):
            self.assertNotIn(forbidden, lowered)

    def test_import_surface_closed(self):
        source = (SCRIPTS / "agent_capability.py").read_text(
            encoding="utf-8")
        tree = ast.parse(source)
        modules = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    modules.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules.add(node.module.split(".")[0])
        # 纯标准库 —— 零 V2 import、零 V3 层 import（evidence 值注入）。
        self.assertEqual(sorted(modules),
                         ["__future__", "dataclasses", "enum", "typing"])

    def test_no_v2_or_lifecycle_surface(self):
        code = self._code_without_docstrings()
        for forbidden in (
                "capability_registry", "AgentProfile", "CapabilityRegistry",
                "CapabilityEvidence", "CapabilityName", "RuntimeCandidate",
                "DiscoverySource", "verified_runtime_pool", "register",
                "invalidate", ".pop(", ".clear(", "subprocess", "os.environ",
                "uuid", "random", "datetime", "socket", "requests",
                "threading"):
            self.assertNotIn(forbidden, code)


# ---------------------------------------------------------------------------
# V2 文件本轮零修改（工作树视角）
# ---------------------------------------------------------------------------


_V2_FILES = (
    "dual-agent-development/scripts/host.py",
    "dual-agent-development/scripts/runtime_discovery.py",
    "dual-agent-development/scripts/runtime_adapter_registry.py",
    "dual-agent-development/scripts/discovery_bootstrap.py",
    "dual-agent-development/scripts/verified_runtime_pool.py",
    "dual-agent-development/scripts/role_assignment.py",
    "dual-agent-development/scripts/verified_selection_bridge.py",
    "dual-agent-development/scripts/production_facade.py",
    "dual-agent-development/scripts/collaboration_orchestrator.py",
    "dual-agent-development/scripts/verification_collaboration.py",
    "dual-agent-development/scripts/collaboration_session.py",
    "dual-agent-development/scripts/verified_orchestrator.py",
    "dual-agent-development/scripts/verified_stage_selector.py",
    "dual-agent-development/scripts/capability_registry.py",
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
# 受保护 untracked 文件（git 视角原样，不读取其内容）
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
