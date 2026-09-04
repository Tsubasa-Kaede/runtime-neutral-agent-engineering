"""V3.0-B: Agent Manifest + Registry tests — declaration layer, locked.

Red-first：agent_manifest.py 尚不存在时本文件必然失败（ImportError）。
期望值是独立字面量或既有模块调用，绝不从被测模块自身源码派生。

锁定（Boundary Review Option A 裁决）：
1. manifest frozen / 字段词表封闭（binding, declared_roles, adapter_factory）
2. declared_roles：非空字符串、secret-marker 拒绝、重复拒绝、空元组合法
3. adapter_factory callable 校验；registry 存续期内零调用
4. registry CRUD / sorted list / duplicate ValueError / unknown get -> None
5. 同一 runtime 承载多个 agent；rebind 不改变 agent identity
6. 源码扫描：零 runtime 名分支、零执行/发现/准入依赖
"""
import ast
import subprocess
import sys
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from agent_identity import AgentIdentity, AgentRuntimeBinding
from agent_manifest import (
    AgentManifest,
    AgentRegistry,
    manifest_field_names,
)

CLAUDE = ("claude-cli", "anthropic", "claude-sonnet-5", "fp-claude-a")
PI = ("pi-cli", "deepseek", "deepseek-v4-pro", "fp-pi-a")


class CountingFactory:
    """callable 替身：计数而不执行任何真实动作。"""

    def __init__(self):
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return object()


def make_manifest(agent_id="arch-001", runtime=CLAUDE,
                  roles=("architect", "coder"), factory=None):
    return AgentManifest(
        binding=AgentRuntimeBinding(agent=AgentIdentity(agent_id=agent_id),
                                    runtime_identity=runtime),
        declared_roles=roles,
        adapter_factory=factory if factory is not None else CountingFactory())


# ---------------------------------------------------------------------------
# 1-3: Manifest 值契约
# ---------------------------------------------------------------------------


class ManifestTests(unittest.TestCase):

    def test_manifest_can_be_created(self):
        manifest = make_manifest()
        self.assertEqual(manifest.binding.agent_id, "arch-001")
        self.assertEqual(manifest.binding.runtime_identity, CLAUDE)
        self.assertEqual(manifest.declared_roles, ("architect", "coder"))
        self.assertEqual(manifest.agent_id, "arch-001")

    def test_manifest_is_frozen(self):
        manifest = make_manifest()
        with self.assertRaises(FrozenInstanceError):
            manifest.declared_roles = ("reviewer",)

    def test_field_vocabulary_is_closed_to_three(self):
        self.assertEqual(manifest_field_names(),
                         ("binding", "declared_roles", "adapter_factory"))

    def test_no_capability_trust_verified_health_status_fields(self):
        names = manifest_field_names()
        for banned in ("capability", "capabilities", "trust", "trusted",
                       "verified", "verification", "health", "status",
                       "provenance", "budget", "token", "score", "admitted"):
            self.assertNotIn(banned, names)

    def test_extra_fields_are_rejected(self):
        with self.assertRaises(TypeError):
            AgentManifest(binding=make_manifest().binding,
                          declared_roles=(), adapter_factory=lambda: None,
                          trust="verified")
        with self.assertRaises(TypeError):
            AgentManifest(binding=make_manifest().binding,
                          declared_roles=(), adapter_factory=lambda: None,
                          health="READY")

    def test_binding_must_be_agent_runtime_binding(self):
        with self.assertRaises(TypeError):
            AgentManifest(binding="not-a-binding", declared_roles=(),
                          adapter_factory=lambda: None)

    def test_declared_roles_normalized_to_tuple(self):
        manifest = AgentManifest(binding=make_manifest().binding,
                                 declared_roles=["architect"],
                                 adapter_factory=lambda: None)
        self.assertEqual(manifest.declared_roles, ("architect",))

    def test_declared_roles_must_be_non_empty_strings(self):
        for bad in (("architect", ""), ("architect", 7), (None,)):
            with self.assertRaises(ValueError, msg=repr(bad)):
                make_manifest(roles=bad)

    def test_declared_roles_secret_markers_rejected(self):
        # 复用单一 secret policy（content_safety 词表）——不建第二套。
        for bad in (("my-api_key",), ("bearer-role",), ("Stdout-Reader",),
                    ("sk-abc12345678",)):
            with self.assertRaises(ValueError, msg=repr(bad)):
                make_manifest(roles=bad)

    def test_declared_roles_duplicates_rejected(self):
        with self.assertRaises(ValueError):
            make_manifest(roles=("architect", "architect"))

    def test_empty_declared_roles_is_legal(self):
        # 诚实：可以一个角色都不声明（意图为空）。
        self.assertEqual(make_manifest(roles=()).declared_roles, ())

    def test_adapter_factory_must_be_callable(self):
        with self.assertRaises(ValueError):
            make_manifest(factory="not-callable")


# ---------------------------------------------------------------------------
# 4: Registry 存储契约（零执行面）
# ---------------------------------------------------------------------------


class RegistryTests(unittest.TestCase):

    def test_register_and_get_roundtrip(self):
        registry = AgentRegistry()
        manifest = make_manifest()
        registry.register(manifest)
        self.assertIs(registry.get("arch-001"), manifest)

    def test_get_unknown_returns_none(self):
        self.assertIsNone(AgentRegistry().get("nobody"))

    def test_list_is_sorted_by_agent_id(self):
        registry = AgentRegistry()
        for agent_id in ("coder-009", "arch-001", "tester-005"):
            registry.register(make_manifest(agent_id=agent_id))
        self.assertEqual(
            [m.agent_id for m in registry.list()],
            ["arch-001", "coder-009", "tester-005"])

    def test_duplicate_agent_id_is_rejected(self):
        registry = AgentRegistry()
        registry.register(make_manifest(agent_id="arch-001"))
        with self.assertRaises(ValueError):
            registry.register(make_manifest(agent_id="arch-001",
                                            runtime=PI))

    def test_register_requires_manifest(self):
        with self.assertRaises(TypeError):
            AgentRegistry().register("not-a-manifest")

    def test_adapter_factory_never_called_by_registry(self):
        factory = CountingFactory()
        registry = AgentRegistry()
        registry.register(make_manifest(factory=factory))
        registry.get("arch-001")
        registry.list()
        self.assertEqual(factory.calls, 0)

    def test_two_agents_share_one_runtime(self):
        registry = AgentRegistry()
        registry.register(make_manifest(agent_id="arch-001", runtime=CLAUDE))
        registry.register(make_manifest(agent_id="test-001", runtime=CLAUDE))
        self.assertEqual(len(registry.list()), 2)
        self.assertEqual(registry.get("arch-001").binding.runtime_identity,
                         registry.get("test-001").binding.runtime_identity)


# ---------------------------------------------------------------------------
# 5: rebind 语义（identity 不变）
# ---------------------------------------------------------------------------


class RebindSemanticsTests(unittest.TestCase):

    def test_rebind_preserves_agent_identity(self):
        binding = AgentRuntimeBinding(
            agent=AgentIdentity(agent_id="arch-001"), runtime_identity=CLAUDE)
        manifest = AgentManifest(binding=binding,
                                 declared_roles=("architect",),
                                 adapter_factory=lambda: None)
        rebound = binding.rebind(PI)
        self.assertEqual(rebound.agent_id, manifest.binding.agent_id)
        self.assertEqual(rebound.agent, manifest.binding.agent)
        self.assertNotEqual(rebound.runtime_identity,
                            manifest.binding.runtime_identity)

    def test_manifest_is_a_snapshot_of_its_binding(self):
        # manifest frozen：binding 的 rebind 产生新值，不渗透进已声明快照。
        binding = AgentRuntimeBinding(
            agent=AgentIdentity(agent_id="arch-001"), runtime_identity=CLAUDE)
        manifest = AgentManifest(binding=binding,
                                 declared_roles=("architect",),
                                 adapter_factory=lambda: None)
        _rebound = binding.rebind(PI)
        self.assertEqual(manifest.binding.runtime_identity, CLAUDE)


# ---------------------------------------------------------------------------
# 6: 源码纪律扫描
# ---------------------------------------------------------------------------


class SourceScanTests(unittest.TestCase):

    @staticmethod
    def _code_without_docstrings():
        source = (SCRIPTS / "agent_manifest.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef)):
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

    def test_imports_are_exactly_the_authorized_set(self):
        source = (SCRIPTS / "agent_manifest.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        modules = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    modules.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules.add(node.module.split(".")[0])
        # 依赖封闭：__future__ / 标准库 dataclasses+typing / V3.0-A
        # agent_identity / 单一 secret policy content_safety —— 仅此。
        self.assertEqual(
            sorted(modules),
            ["__future__", "agent_identity", "content_safety",
             "dataclasses", "typing"])

    def test_no_execution_discovery_admission_surface(self):
        code = self._code_without_docstrings()
        for forbidden in ("os.environ", "getenv", "subprocess", "requests",
                          "urllib", "socket", "websocket", "a2a", "uuid",
                          "random", "datetime", "threading",
                          "verified_runtime_pool", "runtime_discovery",
                          "runtime_adapter_registry", "execution_engine",
                          "production_facade", "collaboration_session"):
            self.assertNotIn(forbidden, code)


# ---------------------------------------------------------------------------
# 7: 受保护 untracked 文件（git 视角原样）
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
