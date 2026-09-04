"""V3.0-C: Agent Composition Seam tests — first consumer of A/B layers.

Red-first：agent_composition.py 尚不存在时本文件必然失败（ImportError）。
期望值是独立字面量或既有模块调用，绝不从被测模块自身源码派生。

锁定（Boundary Review + 实现授权）：
1. 单/多 agent × 单/多 role 组合；字段逐项正确（恰好五字段封闭）
2. 确定性输出，排序键 (role, agent_id)
3. compat 地址来自 V3.0-A 投影；runtime_identity 逐字透传
4. adapter_factory 恰一次/agent；失败原样传播（不吞、不 retry）
5. registry 只读消费，内容不变
6. V2 地址投影保真度边界：同 runtime + 同 role + 不同 agent -> ValueError
7. 同 runtime 不同 role 合法；同 agent 多 role 合法且地址不同
8. 未知 agent / 未声明 role / 空输入 —— 诚实封闭错误
9. rebind 语义：identity 不变、runtime 变、地址随 binding 变
10. 源码纪律：零 runtime 名、零 discovery/admission/verification/
    capability 实现、零 V2 直接 import、import 面最小化
"""
import ast
import subprocess
import sys
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from agent_composition import (
    AgentSlotResolution,
    compose_agent_slots,
    resolution_field_names,
)
from agent_identity import (
    AgentIdentity,
    AgentRuntimeBinding,
    compat_collab_address,
)
from agent_manifest import AgentManifest, AgentRegistry

CLAUDE = ("claude-cli", "anthropic", "claude-sonnet-5", "fp-claude-a")
PI = ("pi-cli", "deepseek", "deepseek-v4-pro", "fp-pi-a")


class CountingFactory:
    """callable 替身：计数并返回稳定哨兵对象。"""

    def __init__(self):
        self.calls = 0
        self.product = object()

    def __call__(self):
        self.calls += 1
        return self.product


class ExplodingFactory:
    """失败替身：抛出可识别的哨兵异常（composer 必须原样传播）。"""

    def __call__(self):
        raise RuntimeError("factory-boom")


def make_manifest(agent_id="arch-001", runtime=CLAUDE, roles=("architect",),
                  factory=None):
    return AgentManifest(
        binding=AgentRuntimeBinding(agent=AgentIdentity(agent_id=agent_id),
                                    runtime_identity=runtime),
        declared_roles=tuple(roles),
        adapter_factory=factory if factory is not None else CountingFactory())


def make_registry(*manifests):
    registry = AgentRegistry()
    for manifest in manifests:
        registry.register(manifest)
    return registry


# ---------------------------------------------------------------------------
# 1-3: happy path 与字段封闭
# ---------------------------------------------------------------------------


class HappyPathTests(unittest.TestCase):

    def test_single_agent_single_role(self):
        factory = CountingFactory()
        registry = make_registry(make_manifest(factory=factory))
        slots = compose_agent_slots(registry, ("arch-001",), ("architect",))
        self.assertEqual(len(slots), 1)
        slot = slots[0]
        self.assertEqual(slot.agent_id, "arch-001")
        self.assertEqual(slot.role, "architect")
        self.assertEqual(
            slot.address,
            compat_collab_address(
                AgentRuntimeBinding(agent=AgentIdentity(agent_id="arch-001"),
                                    runtime_identity=CLAUDE), "architect"))
        self.assertEqual(slot.runtime_identity, CLAUDE)
        self.assertIs(slot.adapter, factory.product)

    def test_field_vocabulary_is_closed_to_five(self):
        self.assertEqual(
            resolution_field_names(),
            ("agent_id", "role", "address", "runtime_identity", "adapter"))

    def test_resolution_is_frozen(self):
        slot = compose_agent_slots(
            make_registry(make_manifest()), ("arch-001",),
            ("architect",))[0]
        with self.assertRaises(FrozenInstanceError):
            slot.role = "coder"

    def test_n_agents_multi_roles(self):
        registry = make_registry(
            make_manifest("arch-001", CLAUDE, ("architect", "coder")),
            make_manifest("test-001", PI, ("tester", "reviewer")))
        slots = compose_agent_slots(
            registry, ("test-001", "arch-001"),
            ("reviewer", "architect", "coder", "tester"))
        self.assertEqual(
            [(s.role, s.agent_id) for s in slots],
            [("architect", "arch-001"), ("coder", "arch-001"),
             ("reviewer", "test-001"), ("tester", "test-001")])

    def test_required_role_superset_is_legal(self):
        # 需求角色是声明的子集即可：只组合被请求的角色。
        registry = make_registry(make_manifest("a", CLAUDE, ("architect",)))
        slots = compose_agent_slots(registry, ("a",), ("architect",))
        self.assertEqual([s.role for s in slots], ["architect"])


# ---------------------------------------------------------------------------
# 4-5: 确定性与排序
# ---------------------------------------------------------------------------


class DeterminismTests(unittest.TestCase):

    def test_identical_inputs_identical_outputs(self):
        registry = make_registry(
            make_manifest("b", CLAUDE, ("coder",)),
            make_manifest("a", PI, ("architect",)))
        first = compose_agent_slots(registry, ("b", "a"),
                                    ("coder", "architect"))
        second = compose_agent_slots(registry, ("b", "a"),
                                     ("coder", "architect"))
        self.assertEqual(first, second)

    def test_output_sorted_by_role_then_agent_id(self):
        # z-9 与 a-1 分属不同 runtime，同为 coder 合法（同 runtime 同
        # role 双 agent 是塌缩组合，另测拒绝）。
        registry = make_registry(
            make_manifest("z-9", PI, ("coder",)),
            make_manifest("a-1", CLAUDE, ("coder", "architect")))
        slots = compose_agent_slots(registry, ("z-9", "a-1"),
                                    ("architect", "coder"))
        self.assertEqual(
            [(s.role, s.agent_id) for s in slots],
            [("architect", "a-1"), ("coder", "a-1"), ("coder", "z-9")])


# ---------------------------------------------------------------------------
# 6-9: factory 生命周期与 registry 只读
# ---------------------------------------------------------------------------


class FactoryLifecycleTests(unittest.TestCase):

    def test_factory_called_exactly_once_per_agent(self):
        factory = CountingFactory()
        registry = make_registry(
            make_manifest("a", CLAUDE, ("architect", "coder"),
                          factory=factory))
        slots = compose_agent_slots(registry, ("a",), ("architect", "coder"))
        self.assertEqual(len(slots), 2)
        self.assertEqual(factory.calls, 1)
        # 同一 agent 的多个槽位共享同一 adapter 实例。
        self.assertIs(slots[0].adapter, slots[1].adapter)

    def test_factory_failure_propagates_unchanged(self):
        registry = make_registry(
            make_manifest("a", CLAUDE, ("architect",),
                          factory=ExplodingFactory()))
        with self.assertRaises(RuntimeError) as ctx:
            compose_agent_slots(registry, ("a",), ("architect",))
        self.assertEqual(str(ctx.exception), "factory-boom")

    def test_non_callable_factory_rejected_at_manifest_layer(self):
        # 校验属于 AgentManifest 构造期（单一校验点）；composer 不重建。
        with self.assertRaises(ValueError):
            make_manifest(factory="not-callable")

    def test_registry_content_unchanged_by_composition(self):
        manifest = make_manifest()
        registry = make_registry(manifest)
        before = registry.list()
        compose_agent_slots(registry, ("arch-001",), ("architect",))
        self.assertEqual(registry.list(), before)
        self.assertIs(registry.get("arch-001"), manifest)

    def test_runtime_identity_passed_through_verbatim(self):
        registry = make_registry(make_manifest(runtime=PI))
        slot = compose_agent_slots(registry, ("arch-001",),
                                   ("architect",))[0]
        self.assertEqual(slot.runtime_identity, PI)
        self.assertEqual(tuple(slot.runtime_identity), PI)


# ---------------------------------------------------------------------------
# 10-12: V2 地址投影保真度边界
# ---------------------------------------------------------------------------


class SlotCollisionTests(unittest.TestCase):

    def test_same_runtime_different_role_is_legal(self):
        registry = make_registry(
            make_manifest("a", CLAUDE, ("architect",)),
            make_manifest("b", CLAUDE, ("coder",)))
        slots = compose_agent_slots(registry, ("a", "b"),
                                    ("architect", "coder"))
        self.assertEqual([(s.role, s.agent_id) for s in slots],
                         [("architect", "a"), ("coder", "b")])

    def test_same_runtime_same_role_two_agents_is_valueerror(self):
        # V2 compat address 由 runtime+role 决定 —— 两个 agent 会塌缩为
        # 同一参与者；组合层必须诚实拒绝（保真度边界，非准入判断）。
        registry = make_registry(
            make_manifest("a", CLAUDE, ("coder",)),
            make_manifest("b", CLAUDE, ("coder",)))
        with self.assertRaises(ValueError):
            compose_agent_slots(registry, ("a", "b"), ("coder",))

    def test_same_agent_multi_role_yields_distinct_addresses(self):
        registry = make_registry(
            make_manifest("a", CLAUDE, ("architect", "coder")))
        slots = compose_agent_slots(registry, ("a",),
                                    ("architect", "coder"))
        self.assertEqual(len(slots), 2)
        self.assertNotEqual(slots[0].address, slots[1].address)
        self.assertEqual({s.agent_id for s in slots}, {"a"})

    def test_same_agent_requested_twice_is_rejected(self):
        registry = make_registry(make_manifest("a", CLAUDE, ("coder",)))
        with self.assertRaises(ValueError):
            compose_agent_slots(registry, ("a", "a"), ("coder",))


# ---------------------------------------------------------------------------
# 13-14: 诚实输入错误
# ---------------------------------------------------------------------------


class HonestErrorTests(unittest.TestCase):

    def test_unknown_agent_is_rejected(self):
        registry = make_registry(make_manifest("a", CLAUDE, ("coder",)))
        with self.assertRaises(ValueError):
            compose_agent_slots(registry, ("ghost",), ("coder",))

    def test_role_declared_by_no_requested_agent_is_rejected(self):
        registry = make_registry(make_manifest("a", CLAUDE, ("coder",)))
        with self.assertRaises(ValueError):
            compose_agent_slots(registry, ("a",), ("reviewer",))

    def test_agent_with_no_relevant_role_is_rejected(self):
        registry = make_registry(make_manifest("a", CLAUDE, ("reviewer",)))
        with self.assertRaises(ValueError):
            compose_agent_slots(registry, ("a",), ("coder",))

    def test_empty_agent_ids_rejected(self):
        with self.assertRaises(ValueError):
            compose_agent_slots(make_registry(), (), ("coder",))

    def test_empty_required_roles_rejected(self):
        registry = make_registry(make_manifest())
        with self.assertRaises(ValueError):
            compose_agent_slots(registry, ("arch-001",), ())


# ---------------------------------------------------------------------------
# 17: rebind 语义（identity 不变，地址随 binding 变）
# ---------------------------------------------------------------------------


class RebindSemanticsTests(unittest.TestCase):

    def test_rebind_preserves_identity_changes_address(self):
        binding = AgentRuntimeBinding(
            agent=AgentIdentity(agent_id="a"), runtime_identity=CLAUDE)
        first = make_registry(
            AgentManifest(binding=binding, declared_roles=("architect",),
                          adapter_factory=CountingFactory()))
        rebound_binding = binding.rebind(PI)
        second = make_registry(
            AgentManifest(binding=rebound_binding,
                          declared_roles=("architect",),
                          adapter_factory=CountingFactory()))
        slot_a = compose_agent_slots(first, ("a",), ("architect",))[0]
        slot_b = compose_agent_slots(second, ("a",), ("architect",))[0]
        # identity 不变；runtime 与 compat 地址随 binding 改变。
        self.assertEqual(slot_a.agent_id, slot_b.agent_id)
        self.assertEqual(rebound_binding.agent, binding.agent)
        self.assertNotEqual(slot_a.runtime_identity, slot_b.runtime_identity)
        self.assertNotEqual(slot_a.address, slot_b.address)


# ---------------------------------------------------------------------------
# 19: 源码纪律扫描
# ---------------------------------------------------------------------------


class SourceScanTests(unittest.TestCase):

    @staticmethod
    def _code_without_docstrings():
        source = (SCRIPTS / "agent_composition.py").read_text(
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

    def test_no_runtime_names_in_code(self):
        lowered = self._code_without_docstrings().lower()
        for name in ("claude", "codex", "deepseek", "openai", "anthropic",
                     "gemini", "pi-cli", "tiny-agents", "tiny_agents"):
            self.assertNotIn(name, lowered)

    def test_imports_are_minimal_and_v3_only(self):
        source = (SCRIPTS / "agent_composition.py").read_text(
            encoding="utf-8")
        tree = ast.parse(source)
        modules = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    modules.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules.add(node.module.split(".")[0])
        # 依赖方向：V3.0-C -> V3.0-A（compat 投影）+ 标准库 —— 仅此。
        self.assertEqual(sorted(modules),
                         ["__future__", "agent_identity", "dataclasses"])

    def test_no_v2_discovery_admission_verification_surface(self):
        code = self._code_without_docstrings()
        for forbidden in (
                "runtime_discovery", "runtime_adapter_registry",
                "verified_runtime_pool", "verified_selection_bridge",
                "collaboration_session", "collaboration_orchestrator",
                "production_facade", "execution_engine", "host",
                "candidate_validation", "discovery_bootstrap",
                "admission", "admit", "capability", "verification",
                "verified", "trust", "health", "probe", "invoke",
                "os.environ", "subprocess", "uuid", "random", "datetime",
                "threading", "socket", "requests"):
            self.assertNotIn(forbidden, code)


# ---------------------------------------------------------------------------
# 20: 受保护 untracked 文件（git 视角原样）
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
