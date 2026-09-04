"""V3.0-A: Agent Identity tests — WHO vs WHERE, locked by contract.

Red-first: every assertion here was written against the design report
before agent_identity.py existed. Expected values are independent
literals or V2 production calls — never derived from this module's own
source. The adversarial section proves identity is NOT a runtime
projection (the exact mistake V2 made and V3.0-A exists to undo).
"""
import sys
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from agent_identity import (
    AgentIdentity,
    AgentRuntimeBinding,
    agent_address,
    compat_collab_address,
    identity_field_names,
)
from collaboration_session import collab_agent_address

CLAUDE = ("claude-cli", "anthropic", "claude-sonnet-5", "fp-claude-a")
PI = ("pi-cli", "deepseek", "deepseek-v4-pro", "fp-pi-a")
CLAUDE_OTHER_MODEL = ("claude-cli", "anthropic", "claude-haiku-4-5", "fp-claude-a")
CLAUDE_OTHER_CONFIG = ("claude-cli", "anthropic", "claude-sonnet-5", "fp-claude-b")


class IdentityTests(unittest.TestCase):
    def test_identity_can_be_created(self):
        agent = AgentIdentity(agent_id="arch-001")
        self.assertEqual(agent.agent_id, "arch-001")

    def test_agent_id_is_stable(self):
        agent = AgentIdentity(agent_id="arch-001")
        self.assertEqual(agent.agent_id, "arch-001")
        self.assertEqual(AgentIdentity(agent_id="arch-001"), agent)

    def test_two_agent_ids_are_two_agents(self):
        a = AgentIdentity(agent_id="arch-001")
        b = AgentIdentity(agent_id="arch-002")
        self.assertNotEqual(a, b)
        self.assertNotEqual(hash(a), hash(b))

    def test_identity_is_frozen(self):
        agent = AgentIdentity(agent_id="arch-001")
        with self.assertRaises(FrozenInstanceError):
            agent.agent_id = "mutated"

    def test_empty_agent_id_is_rejected(self):
        with self.assertRaises(ValueError):
            AgentIdentity(agent_id="")

    def test_secret_marker_agent_id_is_rejected(self):
        # 复用 V2/D1 同款单一 secret policy（content_safety 词表）：
        # marker 提及（大小写不敏感子串）与 credential 形状都在构造期
        # 被拒收 —— identity 要安全充当 log 标识符/协作地址/观察投影。
        for bad in ("my-api_key", "bearer-token-agent", "agent with secret",
                    "stdout-reader", "Authorization-Agent", "sk-abc123agent"):
            with self.assertRaises(ValueError, msg=bad):
                AgentIdentity(agent_id=bad)

    def test_identity_field_vocabulary_is_who_only(self):
        # 结构级断言：AgentIdentity 的字段词表恰好是 {agent_id}。
        # 任何未来把 WHERE/CAPABILITY/TRUST 塞进 Identity 的改动
        # 都会在 RED 时被发现。
        self.assertEqual(identity_field_names(), ("agent_id",))


class BindingTests(unittest.TestCase):
    def test_binding_attaches_agent_to_runtime_identity(self):
        binding = AgentRuntimeBinding(
            agent=AgentIdentity(agent_id="arch-001"), runtime_identity=CLAUDE)
        self.assertEqual(binding.agent_id, "arch-001")
        self.assertEqual(binding.runtime_identity, CLAUDE)

    def test_rebinding_preserves_agent_id(self):
        binding = AgentRuntimeBinding(
            agent=AgentIdentity(agent_id="arch-001"), runtime_identity=CLAUDE)
        rebound = binding.rebind(PI)
        self.assertEqual(rebound.agent_id, binding.agent_id)
        self.assertEqual(rebound.agent, binding.agent)
        self.assertEqual(rebound.runtime_identity, PI)
        self.assertNotEqual(rebound.runtime_identity, binding.runtime_identity)

    def test_two_agents_can_share_one_runtime(self):
        arch = AgentRuntimeBinding(
            agent=AgentIdentity(agent_id="arch-001"), runtime_identity=CLAUDE)
        tester = AgentRuntimeBinding(
            agent=AgentIdentity(agent_id="test-001"), runtime_identity=CLAUDE)
        self.assertEqual(arch.runtime_identity, tester.runtime_identity)
        self.assertNotEqual(arch.agent_id, tester.agent_id)

    def test_binding_rejects_malformed_runtime_identity(self):
        with self.assertRaises(ValueError):
            AgentRuntimeBinding(
                agent=AgentIdentity(agent_id="a"), runtime_identity=("only-one",))

    def test_binding_rejects_non_agent(self):
        with self.assertRaises(TypeError):
            AgentRuntimeBinding(agent="not-an-agent", runtime_identity=CLAUDE)


class AddressTests(unittest.TestCase):
    def test_address_is_stable_for_same_agent_and_role(self):
        agent = AgentIdentity(agent_id="arch-001")
        self.assertEqual(agent_address(agent, "architect"),
                         agent_address(agent, "architect"))

    def test_addresses_differ_across_agents(self):
        a = agent_address(AgentIdentity(agent_id="arch-001"), "architect")
        b = agent_address(AgentIdentity(agent_id="arch-002"), "architect")
        self.assertNotEqual(a, b)

    def test_addresses_differ_across_roles(self):
        agent = AgentIdentity(agent_id="arch-001")
        self.assertNotEqual(agent_address(agent, "architect"),
                            agent_address(agent, "coder"))

    def test_address_is_runtime_neutral(self):
        # 同一 agent 换绑前后（Claude → Pi），地址逐字节不变。
        agent = AgentIdentity(agent_id="arch-001")
        binding = AgentRuntimeBinding(agent=agent, runtime_identity=CLAUDE)
        before = agent_address(binding.agent, "architect")
        after = agent_address(binding.rebind(PI).agent, "architect")
        self.assertEqual(before, after)

    def test_address_contains_no_runtime_facts(self):
        agent = AgentIdentity(agent_id="arch-001")
        for role in ("architect", "coder", "tester", "reviewer"):
            address = agent_address(agent, role).lower()
            for forbidden in ("claude", "anthropic", "pi-cli", "deepseek",
                              "sonnet", "haiku", "fp-", "provider", "model",
                              "config"):
                self.assertNotIn(forbidden, address)

    def test_address_shape_is_agent_colon_id_colon_role(self):
        self.assertEqual(agent_address(AgentIdentity(agent_id="arch-001"), "coder"),
                         "agent:arch-001:coder")

    def test_empty_role_is_rejected(self):
        with self.assertRaises(ValueError):
            agent_address(AgentIdentity(agent_id="a"), "")


class CompatProjectionTests(unittest.TestCase):
    def test_projection_matches_v2_collab_address_exactly(self):
        for identity in (CLAUDE, PI, CLAUDE_OTHER_MODEL, CLAUDE_OTHER_CONFIG):
            binding = AgentRuntimeBinding(
                agent=AgentIdentity(agent_id="some-agent"),
                runtime_identity=identity)
            for role in ("architect", "coder", "tester", "reviewer"):
                self.assertEqual(
                    compat_collab_address(binding, role),
                    collab_agent_address(identity, role))

    def test_projection_follows_rebinding(self):
        # 同一 agent，不同绑定 → 投影地址跟随 runtime（V2 语义），
        # 而 agent_address 保持稳定（V3 语义）：两层地址各司其职。
        agent = AgentIdentity(agent_id="arch-001")
        on_claude = AgentRuntimeBinding(agent=agent, runtime_identity=CLAUDE)
        on_pi = on_claude.rebind(PI)
        self.assertNotEqual(
            compat_collab_address(on_claude, "architect"),
            compat_collab_address(on_pi, "architect"))
        self.assertEqual(
            agent_address(on_claude.agent, "architect"),
            agent_address(on_pi.agent, "architect"))

    def test_v3_and_v2_address_spaces_are_disjoint(self):
        agent = AgentIdentity(agent_id="arch-001")
        binding = AgentRuntimeBinding(agent=agent, runtime_identity=CLAUDE)
        self.assertNotEqual(agent_address(agent, "architect"),
                            compat_collab_address(binding, "architect"))


class AdversarialIdentityTests(unittest.TestCase):
    """Negative: identity is NOT a runtime projection (V2's mistake)."""

    def test_runtime_change_does_not_change_agent_id(self):
        agent = AgentIdentity(agent_id="arch-001")
        binding = AgentRuntimeBinding(agent=agent, runtime_identity=CLAUDE)
        rebound = binding.rebind(PI)
        self.assertEqual(rebound.agent_id, agent.agent_id)
        self.assertEqual(rebound.agent, agent)

    def test_model_change_does_not_change_agent_id(self):
        agent = AgentIdentity(agent_id="arch-001")
        binding = AgentRuntimeBinding(agent=agent, runtime_identity=CLAUDE)
        self.assertEqual(
            binding.rebind(CLAUDE_OTHER_MODEL).agent_id, agent.agent_id)

    def test_config_change_does_not_change_agent_id(self):
        agent = AgentIdentity(agent_id="arch-001")
        binding = AgentRuntimeBinding(agent=agent, runtime_identity=CLAUDE)
        self.assertEqual(
            binding.rebind(CLAUDE_OTHER_CONFIG).agent_id, agent.agent_id)

    def test_agent_id_never_derives_from_runtime_identity(self):
        # 结构性证明：V2 的 agent_id_for(runtime_identity) 与任何
        # AgentIdentity.agent_id 都是不同字符串 —— 不存在派生关系
        # 可以被构造出来。
        from verified_selection_bridge import agent_id_for

        agent = AgentIdentity(agent_id="arch-001")
        for identity in (CLAUDE, PI, CLAUDE_OTHER_MODEL, CLAUDE_OTHER_CONFIG):
            for role in ("", "architect", "coder"):
                projection = agent_id_for(identity + ((role,) if role else ()))
                self.assertNotEqual(agent.agent_id, projection)
                self.assertNotIn(agent.agent_id, projection)
        # 且 V3 地址同样不与任何 V2 投影重合。
        for identity in (CLAUDE, PI):
            self.assertNotIn(
                agent_address(agent, "architect"),
                agent_id_for(identity + ("architect",)))

    def test_structured_agent_id_is_rejected(self):
        # 编码 runtime 四元组的结构化 id（V2 投影的典型形态）被拒收：
        # agent_id 必须是逻辑名，不是 runtime 数据的载体。
        with self.assertRaises(ValueError):
            AgentIdentity(agent_id='["claude-cli", "anthropic", null, "fp"]')
        with self.assertRaises(ValueError):
            AgentIdentity(agent_id='{"runtime_id": "claude-cli"}')

    def test_identity_has_no_runtime_capability_or_trust_fields(self):
        # 反射级负面断言：字段词表封闭为 {agent_id}。
        names = identity_field_names()
        for banned in ("runtime", "runtime_id", "provider", "provider_id",
                       "model", "model_id", "config", "config_fingerprint",
                       "capability", "capabilities", "trust", "trusted",
                       "verified", "verification", "provenance", "budget",
                       "token", "status"):
            self.assertNotIn(banned, names)

    def test_identity_construction_ignores_extra_fields(self):
        # dataclass 语义：多传字段直接 TypeError —— 无法把 runtime /
        # capability / trust 夹带进 Identity。
        with self.assertRaises(TypeError):
            AgentIdentity(agent_id="a", runtime_id="claude-cli")
        with self.assertRaises(TypeError):
            AgentIdentity(agent_id="a", trust="verified")
        with self.assertRaises(TypeError):
            AgentIdentity(agent_id="a", provenance="REAL")


class SourceScanTests(unittest.TestCase):
    def test_module_stays_value_only_and_v2_free(self):
        import ast
        import agent_identity as module
        source = Path(module.__file__).read_text(encoding="utf-8")
        # 扫描 CODE 而非 prose：docstring 诚实声明"未实现 registry/
        # manifest"是文档，不是实现。用 AST 剥离 docstring 与注释，
        # 断言强度不降 —— 可执行代码里这些词零出现。
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef)):
                if (node.body and isinstance(node.body[0], ast.Expr)
                        and isinstance(node.body[0].value, ast.Constant)
                        and isinstance(node.body[0].value.value, str)):
                    node.body = node.body[1:]
        code = ast.unparse(tree)
        lowered = code.lower()
        # 无 runtime 名：身份层与任何执行基底无关。
        for name in ("claude", "codex", "deepseek", "openai", "anthropic",
                     "gemini", "pi-cli", "tiny-agents", "tiny_agents"):
            self.assertNotIn(name, lowered)
        # 无状态/无通道/无治理：纯值对象模块。
        for forbidden in ("os.environ", "getenv", "subprocess", "requests",
                          "urllib", "socket", "http", "websocket", "a2a",
                          "uuid", "random", "datetime", "threading",
                          "registry", "manifest", "trust_score", "budget"):
            self.assertNotIn(forbidden, code)


if __name__ == "__main__":
    unittest.main()
