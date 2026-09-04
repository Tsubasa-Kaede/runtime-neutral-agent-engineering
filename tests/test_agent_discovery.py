"""V3.0-E: Agent binding liveness discovery tests.

Red-first：agent_discovery.py 尚不存在时本文件必然失败（ImportError）。
期望值是独立字面量，绝不从被测模块自身源码派生。

V3.0-E 只回答一个问题：
«一个已声明的 Agent，其 binding 指向的 Runtime 当前是否可被发现？»

锁定（Boundary Re-check + 实现授权）：
1. Agent 是逻辑身份（声明），Runtime 才能被环境发现——Discovery 是
   声明世界 × runtime 发现事实的 join 投影，不是扫描，不是枚举
2. AgentCandidate 字段封闭为四项：agent_id / runtime_identity /
   discovered / reason —— 不携带 capability、verified、trust、
   health、score、admitted、status 等任何后续层语义
3. declared ≠ discovered ≠ verified ≠ admitted：绝不合并成单一
   status（模块不存在 AgentStatus 之类的合并物）
4. 探测按完整 Runtime Identity 四元组去重，恰一次/distinct identity；
   同 runtime_id 不同 fingerprint 是两个不同探测键
5. agent 候选键是 agent_id，绝不退化成 runtime_id；同 runtime 多
   agent 各自独立成候选，绝不被 runtime 去重吞掉
6. 探测能力全部来自注入的 discover_runtime callable；异常按 V2
   Runtime Discovery 的收敛方式（受控 NOT_FOUND，绝不虚假成功）
7. registry 只读消费（list/get），factory 永不被 discovery 调用
   （V3.0-B/C 契约：composer 是唯一合法调用点）
8. runtime-neutral + agent-neutral：抽象 agent-a/b/c/d + 抽象 runtime
   identity；不固定任何协作组合
"""
import ast
import subprocess
import sys
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from agent_discovery import (
    AgentCandidate,
    candidate_field_names,
    discover_agent_bindings,
)
from agent_identity import AgentIdentity, AgentRuntimeBinding
from agent_manifest import AgentManifest, AgentRegistry

# 抽象 runtime identities（同 runtime_id 不同 fingerprint = 不同身份）。
RT_X = ("rt-x", "provider-1", None, "fp-x-1")
RT_X_ALT_FINGERPRINT = ("rt-x", "provider-1", None, "fp-x-2")
RT_Y = ("rt-y", "provider-2", "m-y", "fp-y-1")


class CountingFactory:
    """callable 替身：计数。discovery 绝不能调用它（V3.0-B/C 契约）。"""

    def __init__(self):
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return object()


class Fact:
    """V2 RuntimeCandidate 形状的发现事实替身（duck-typed）。"""

    def __init__(self, available, reason=None):
        self.available = available
        self.reason = reason


class RecordingDiscovery:
    """注入的 runtime 粒度探测 callable：记录每次调用与收到的 identity。

    未登记的 identity 返回 V2 同款 NOT_FOUND（no source registered）。"""

    def __init__(self, facts=None, error=None):
        self.calls = []
        self._facts = {tuple(k): v for k, v in (facts or {}).items()}
        self._error = error

    def __call__(self, identity):
        self.calls.append(tuple(identity))
        if self._error is not None:
            raise self._error
        return self._facts.get(
            tuple(identity),
            Fact(False, "NOT_FOUND: no source registered"))


def manifest_of(agent_id, runtime):
    factory = CountingFactory()
    manifest = AgentManifest(
        binding=AgentRuntimeBinding(agent=AgentIdentity(agent_id=agent_id),
                                    runtime_identity=runtime),
        declared_roles=("coder",),
        adapter_factory=factory)
    return manifest, factory


def registry_of(*manifests):
    registry = AgentRegistry()
    for manifest in manifests:
        registry.register(manifest)
    return registry


# ---------------------------------------------------------------------------
# 契约：字段封闭、身份语义、候选唯一
# ---------------------------------------------------------------------------


class ContractTests(unittest.TestCase):

    def test_single_agent_single_runtime_fields(self):
        manifest, _ = manifest_of("agent-a", RT_X)
        discovery = RecordingDiscovery({RT_X: Fact(True)})
        candidates = discover_agent_bindings(registry_of(manifest), discovery)
        self.assertEqual(len(candidates), 1)
        candidate = candidates[0]
        self.assertEqual(candidate.agent_id, "agent-a")
        self.assertEqual(candidate.runtime_identity, RT_X)
        self.assertIs(candidate.discovered, True)
        self.assertIsNone(candidate.reason)

    def test_candidate_field_vocabulary_closed_to_four(self):
        self.assertEqual(
            candidate_field_names(),
            ("agent_id", "runtime_identity", "discovered", "reason"))

    def test_no_lifecycle_merge_symbol_exists(self):
        # declared/discovered/verified/admitted 绝不合并成单一 status。
        import agent_discovery as module
        self.assertFalse(hasattr(module, "AgentStatus"))
        self.assertFalse(hasattr(module, "AgentLifecycle"))

    def test_agent_id_is_not_runtime_id(self):
        # agent 候选键是逻辑 agent_id；runtime_id 只是 identity 的一部分。
        manifest, _ = manifest_of("agent-a", RT_X)
        candidates = discover_agent_bindings(
            registry_of(manifest), RecordingDiscovery({RT_X: Fact(True)}))
        self.assertEqual(candidates[0].agent_id, "agent-a")
        self.assertNotEqual(candidates[0].agent_id,
                            candidates[0].runtime_identity[0])
        self.assertNotIn(candidates[0].runtime_identity[0],
                         candidates[0].agent_id)

    def test_runtime_identity_passed_through_verbatim(self):
        manifest, _ = manifest_of("agent-a", RT_Y)
        candidates = discover_agent_bindings(
            registry_of(manifest), RecordingDiscovery({RT_Y: Fact(True)}))
        self.assertEqual(candidates[0].runtime_identity, RT_Y)
        self.assertEqual(tuple(candidates[0].runtime_identity), RT_Y)

    def test_one_candidate_per_agent(self):
        manifests = [manifest_of(name, RT_X)[0]
                     for name in ("agent-a", "agent-b", "agent-c")]
        candidates = discover_agent_bindings(
            registry_of(*manifests), RecordingDiscovery({RT_X: Fact(True)}))
        self.assertEqual([c.agent_id for c in candidates],
                         ["agent-a", "agent-b", "agent-c"])
        self.assertEqual(len(set(c.agent_id for c in candidates)), 3)

    def test_candidate_is_frozen(self):
        manifest, _ = manifest_of("agent-a", RT_X)
        candidate = discover_agent_bindings(
            registry_of(manifest), RecordingDiscovery({RT_X: Fact(True)}))[0]
        with self.assertRaises(FrozenInstanceError):
            candidate.discovered = False

    def test_discovered_true_rejects_reason(self):
        # discovered=True 携带 reason 是自相矛盾的事实，构造期拒绝。
        with self.assertRaises(ValueError):
            AgentCandidate(agent_id="agent-a", runtime_identity=RT_X,
                           discovered=True, reason="NOT_FOUND")

    def test_candidate_rejects_empty_agent_id(self):
        with self.assertRaises(ValueError):
            AgentCandidate(agent_id="", runtime_identity=RT_X,
                           discovered=True)


# ---------------------------------------------------------------------------
# 探测去重：distinct Runtime Identity 恰一次
# ---------------------------------------------------------------------------


class ProbeDedupTests(unittest.TestCase):

    def test_same_runtime_two_agents_probed_once(self):
        # Agent A ─┐
        #          ├─ Runtime X → discover_runtime(X) 恰一次
        # Agent B ─┘
        first, _ = manifest_of("agent-a", RT_X)
        second, _ = manifest_of("agent-b", RT_X)
        discovery = RecordingDiscovery({RT_X: Fact(True)})
        candidates = discover_agent_bindings(
            registry_of(first, second), discovery)
        self.assertEqual(discovery.calls, [RT_X])
        self.assertEqual(
            [(c.agent_id, c.discovered) for c in candidates],
            [("agent-a", True), ("agent-b", True)])

    def test_multi_runtime_probed_once_each(self):
        manifests = [manifest_of("agent-a", RT_X)[0],
                     manifest_of("agent-b", RT_X)[0],
                     manifest_of("agent-c", RT_Y)[0]]
        discovery = RecordingDiscovery({RT_X: Fact(True), RT_Y: Fact(True)})
        candidates = discover_agent_bindings(
            registry_of(*manifests), discovery)
        self.assertEqual(sorted(discovery.calls), [RT_X, RT_Y])
        self.assertEqual(len(discovery.calls), 2)
        self.assertEqual([c.discovered for c in candidates],
                         [True, True, True])

    def test_same_runtime_id_different_fingerprint_two_identities(self):
        # Runtime X(fp-x-1) 与 X(fp-x-2) 是两个不同 Runtime Identity：
        # 两次探测、两个候选各自携带完整四元组。
        first, _ = manifest_of("agent-a", RT_X)
        second, _ = manifest_of("agent-b", RT_X_ALT_FINGERPRINT)
        discovery = RecordingDiscovery(
            {RT_X: Fact(True), RT_X_ALT_FINGERPRINT: Fact(True)})
        candidates = discover_agent_bindings(
            registry_of(first, second), discovery)
        self.assertEqual(sorted(discovery.calls),
                         [RT_X, RT_X_ALT_FINGERPRINT])
        self.assertEqual(candidates[0].runtime_identity, RT_X)
        self.assertEqual(candidates[1].runtime_identity,
                         RT_X_ALT_FINGERPRINT)

    def test_empty_registry_probes_nothing(self):
        discovery = RecordingDiscovery()
        candidates = discover_agent_bindings(AgentRegistry(), discovery)
        self.assertEqual(candidates, ())
        self.assertEqual(discovery.calls, [])


# ---------------------------------------------------------------------------
# 结果语义：discovered / reason / 异常收敛
# ---------------------------------------------------------------------------


class OutcomeTests(unittest.TestCase):

    def test_undiscovered_runtime_carries_honest_reason(self):
        manifest, _ = manifest_of("agent-a", RT_X)
        discovery = RecordingDiscovery(
            {RT_X: Fact(False, "NOT_FOUND: runtime not discoverable")})
        candidates = discover_agent_bindings(registry_of(manifest), discovery)
        self.assertIs(candidates[0].discovered, False)
        self.assertEqual(candidates[0].reason,
                         "NOT_FOUND: runtime not discoverable")

    def test_unknown_runtime_defaults_to_not_found(self):
        # registry 声明了 binding，但注入的发现世界不认识该 runtime
        # —— V2 同款「no source registered」语义。
        manifest, _ = manifest_of("agent-a", RT_X)
        candidates = discover_agent_bindings(
            registry_of(manifest), RecordingDiscovery())
        self.assertIs(candidates[0].discovered, False)
        self.assertIn("no source registered", candidates[0].reason)

    def test_discovery_callable_exception_converges_to_not_found(self):
        # V2 Runtime Discovery 的收敛方式：探测异常 -> 受控 NOT_FOUND，
        # 绝不向上抛、绝不虚假成功。
        manifest, _ = manifest_of("agent-a", RT_X)
        discovery = RecordingDiscovery(error=RuntimeError("probe-boom"))
        candidates = discover_agent_bindings(registry_of(manifest), discovery)
        self.assertIs(candidates[0].discovered, False)
        self.assertEqual(candidates[0].reason,
                         "NOT_FOUND: discovery error (RuntimeError)")

    def test_malformed_discovery_fact_rejected(self):
        # 返回值不暴露 available 是调用方契约错误（不是 runtime 事实）
        # —— 封闭 ValueError，而非吞掉或伪造。
        manifest, _ = manifest_of("agent-a", RT_X)
        discovery = RecordingDiscovery({RT_X: object()})
        with self.assertRaises(ValueError) as ctx:
            discover_agent_bindings(registry_of(manifest), discovery)
        self.assertIn("available", str(ctx.exception))

    def test_non_callable_discovery_rejected(self):
        manifest, _ = manifest_of("agent-a", RT_X)
        with self.assertRaises(ValueError):
            discover_agent_bindings(registry_of(manifest), "not-callable")


# ---------------------------------------------------------------------------
# 生命周期：registry 只读、factory 零调用、rebind
# ---------------------------------------------------------------------------


class LifecycleTests(unittest.TestCase):

    def test_registry_content_unchanged(self):
        manifest, _ = manifest_of("agent-a", RT_X)
        registry = registry_of(manifest)
        before = registry.list()
        discover_agent_bindings(registry,
                                RecordingDiscovery({RT_X: Fact(True)}))
        self.assertEqual(registry.list(), before)
        self.assertIs(registry.get("agent-a"), manifest)

    def test_adapter_factory_never_called(self):
        # factory 生命周期归 V3.0-C composer（恰一次/agent）；
        # discovery 偷调用 factory 是契约违约。
        first, first_factory = manifest_of("agent-a", RT_X)
        second, second_factory = manifest_of("agent-b", RT_X)
        discover_agent_bindings(
            registry_of(first, second),
            RecordingDiscovery({RT_X: Fact(True)}))
        self.assertEqual(first_factory.calls, 0)
        self.assertEqual(second_factory.calls, 0)

    def test_rebind_projects_new_runtime_identity(self):
        # 同一 agent 身份 rebind 到新 runtime：候选携带新 identity，
        # 探测落在新 identity 上（旧 runtime 不再被探测）。
        binding = AgentRuntimeBinding(
            agent=AgentIdentity(agent_id="agent-a"), runtime_identity=RT_X)
        manifest = AgentManifest(binding=binding, declared_roles=("coder",),
                                 adapter_factory=CountingFactory())
        rebound = AgentManifest(binding=binding.rebind(RT_Y),
                                declared_roles=("coder",),
                                adapter_factory=CountingFactory())
        discovery = RecordingDiscovery({RT_Y: Fact(True)})
        candidates = discover_agent_bindings(
            registry_of(rebound), discovery)
        self.assertEqual(discovery.calls, [RT_Y])
        self.assertEqual(candidates[0].runtime_identity, RT_Y)
        self.assertEqual(candidates[0].agent_id, "agent-a")
        # 原 binding 的 manifest 未被触碰（声明不变，探测跟着新声明走）。
        self.assertEqual(manifest.binding.runtime_identity, RT_X)


# ---------------------------------------------------------------------------
# determinism
# ---------------------------------------------------------------------------


class DeterminismTests(unittest.TestCase):

    def test_identical_inputs_identical_outputs(self):
        manifests = [manifest_of(name, runtime)[0] for name, runtime in
                     (("agent-d", RT_Y), ("agent-b", RT_X),
                      ("agent-c", RT_X), ("agent-a", RT_Y))]
        facts = {RT_X: Fact(True), RT_Y: Fact(False, "NOT_FOUND: down")}
        first = discover_agent_bindings(
            registry_of(*manifests), RecordingDiscovery(facts))
        second = discover_agent_bindings(
            registry_of(*manifests), RecordingDiscovery(facts))
        self.assertEqual(first, second)
        self.assertEqual(first, tuple(sorted(first,
                                             key=lambda c: c.agent_id)))

    def test_output_sorted_by_agent_id(self):
        manifests = [manifest_of(name, RT_X)[0]
                     for name in ("agent-zeta", "agent-alpha", "agent-mid")]
        candidates = discover_agent_bindings(
            registry_of(*manifests), RecordingDiscovery({RT_X: Fact(True)}))
        self.assertEqual([c.agent_id for c in candidates],
                         ["agent-alpha", "agent-mid", "agent-zeta"])


# ---------------------------------------------------------------------------
# 源码纪律扫描
# ---------------------------------------------------------------------------


class SourceScanTests(unittest.TestCase):

    @staticmethod
    def _code_without_docstrings():
        source = (SCRIPTS / "agent_discovery.py").read_text(
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

    def test_import_surface_closed(self):
        source = (SCRIPTS / "agent_discovery.py").read_text(
            encoding="utf-8")
        tree = ast.parse(source)
        modules = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    modules.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules.add(node.module.split(".")[0])
        # 依赖方向：纯标准库 —— 零 V2 import、零 V3 层 import（注入式）。
        self.assertEqual(sorted(modules),
                         ["__future__", "dataclasses", "typing"])

    def test_no_v2_discovery_or_lifecycle_surface(self):
        # 不桥接 DiscoverySource、不消费 RuntimeCandidate 类型、不回写
        # 任何 V2/registry 结构、不触执行/验证/准入表面。
        code = self._code_without_docstrings()
        for forbidden in (
                "RuntimeCandidate", "DiscoverySource", "runtime_discovery",
                "discovery_bootstrap", "runtime_adapter_registry",
                "verified", "production_facade", "collaboration",
                "candidate_validation", "admit", "register", "invalidate",
                "unregister", ".pop(", ".clear(", "check_authentication",
                "check_provider_model", ".invoke(", "subprocess",
                "os.environ", "uuid", "random", "datetime", "socket",
                "requests", "threading"):
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
