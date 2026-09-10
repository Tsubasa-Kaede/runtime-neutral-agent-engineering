"""CU-OBS-3 tests: Adapter 观察 capability 契约（声明层，非事实层）。

覆盖冻结契约：
- capability 词表恰 4 值（INPUT_TOKENS/OUTPUT_TOKENS/CONTEXT_USED/
  CONTEXT_LIMIT）；deferred/未来项（percentage/cost/latency/total）不得混入
- 三态 SUPPORTED/UNSUPPORTED/UNKNOWN 语义；未声明 kind → UNKNOWN
  （绝不默认 SUPPORTED；UNKNOWN 不可被升级）
- 声明不可变（frozen + 只读映射；源 dict 变更不影响；无增删面）
- capability 属 adapter（由 adapter 对象携带声明；契约模块零 runtime 知识）
- capability ≠ usage：SUPPORTED 不生成 UsageLog record / 不等于 KNOWN /
  不产生 0；UNSUPPORTED 不阻止已获事实记录为 KNOWN
- capability ≠ trace：不创建/不修改 InvocationTrace / 不决定 status
- import boundary：仅标准库；零控制域/事件索引/用量 store/投影器/
  runtime adapter/observation 契约依赖
"""
import ast
import sys
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import observation_capability  # noqa: E402
from observation_capability import (  # noqa: E402
    ObservationCapabilities,
    ObservationCapabilityError,
    ObservationCapabilityState,
    ObservationKind,
)
from external_runtime import InvocationStatus, InvocationTrace  # noqa: E402
from usage_log import UsageLog, UsageObservation, UsageRecord  # noqa: E402


def declaration(**overrides):
    states = {
        ObservationKind.INPUT_TOKENS: ObservationCapabilityState.SUPPORTED,
        ObservationKind.OUTPUT_TOKENS: ObservationCapabilityState.SUPPORTED,
    }
    for name, value in overrides.items():
        states[ObservationKind[name]] = value
    drop = [k for k, v in states.items() if v is None]
    for key in drop:
        del states[key]
    return ObservationCapabilities(states)


class _DeclaringAdapter:
    """stand-in adapter：声明由 adapter 对象携带（挂载模式证明）。"""

    def __init__(self, capabilities):
        self.observation_capabilities = capabilities


class VocabularyTests(unittest.TestCase):
    def test_observation_kind_vocabulary_is_exact(self):
        self.assertEqual(
            {member.name for member in ObservationKind},
            {"INPUT_TOKENS", "OUTPUT_TOKENS", "CONTEXT_USED",
             "CONTEXT_LIMIT"})

    def test_future_kinds_are_not_members(self):
        # context percentage 明确 deferred；total/cost/latency 不在契约内
        for name in ("TOTAL_TOKENS", "CONTEXT_PERCENTAGE", "COST",
                     "LATENCY", "DURATION"):
            self.assertFalse(hasattr(ObservationKind, name), name)

    def test_state_vocabulary_is_exact(self):
        self.assertEqual(
            {member.name for member in ObservationCapabilityState},
            {"SUPPORTED", "UNSUPPORTED", "UNKNOWN"})


class DeclarationValidationTests(unittest.TestCase):
    def test_empty_declaration_is_valid(self):
        caps = ObservationCapabilities({})
        self.assertEqual(
            caps.state(ObservationKind.INPUT_TOKENS),
            ObservationCapabilityState.UNKNOWN)

    def test_raw_string_kind_rejected(self):
        with self.assertRaises(ObservationCapabilityError):
            ObservationCapabilities(
                {"INPUT_TOKENS": ObservationCapabilityState.SUPPORTED})

    def test_raw_string_state_rejected(self):
        with self.assertRaises(ObservationCapabilityError):
            ObservationCapabilities(
                {ObservationKind.INPUT_TOKENS: "SUPPORTED"})

    def test_cross_domain_vocabulary_rejected_as_state(self):
        # capability 三态（SUPPORTED/…）≠ usage 记录三态（KNOWN/…）
        with self.assertRaises(ObservationCapabilityError):
            ObservationCapabilities(
                {ObservationKind.INPUT_TOKENS: UsageObservation.KNOWN})
        with self.assertRaises(ObservationCapabilityError):
            ObservationCapabilities(
                {ObservationKind.INPUT_TOKENS: UsageObservation.UNSUPPORTED})

    def test_none_and_bool_states_rejected(self):
        with self.assertRaises(ObservationCapabilityError):
            ObservationCapabilities(
                {ObservationKind.CONTEXT_USED: None})
        with self.assertRaises(ObservationCapabilityError):
            ObservationCapabilities(
                {ObservationKind.CONTEXT_USED: True})

    def test_state_query_rejects_non_members(self):
        caps = declaration()
        with self.assertRaises(ObservationCapabilityError):
            caps.state("INPUT_TOKENS")
        with self.assertRaises(ObservationCapabilityError):
            caps.state(None)


class DefaultSemanticsTests(unittest.TestCase):
    def test_undeclared_kind_is_unknown_never_supported(self):
        caps = declaration()  # 只声明 token 两项
        self.assertEqual(
            caps.state(ObservationKind.CONTEXT_USED),
            ObservationCapabilityState.UNKNOWN)
        self.assertEqual(
            caps.state(ObservationKind.CONTEXT_LIMIT),
            ObservationCapabilityState.UNKNOWN)

    def test_declared_kinds_return_declared_state(self):
        caps = declaration()
        self.assertEqual(
            caps.state(ObservationKind.INPUT_TOKENS),
            ObservationCapabilityState.SUPPORTED)

    def test_declared_unknown_stays_unknown(self):
        caps = declaration(
            CONTEXT_USED=ObservationCapabilityState.UNKNOWN)
        self.assertEqual(
            caps.state(ObservationKind.CONTEXT_USED),
            ObservationCapabilityState.UNKNOWN)

    def test_declared_unsupported_returns_unsupported(self):
        caps = declaration(
            CONTEXT_USED=ObservationCapabilityState.UNSUPPORTED)
        self.assertEqual(
            caps.state(ObservationKind.CONTEXT_USED),
            ObservationCapabilityState.UNSUPPORTED)


class ImmutabilityTests(unittest.TestCase):
    def test_source_dict_mutation_does_not_leak(self):
        source = {
            ObservationKind.INPUT_TOKENS:
                ObservationCapabilityState.SUPPORTED,
        }
        caps = ObservationCapabilities(source)
        source[ObservationKind.OUTPUT_TOKENS] = (
            ObservationCapabilityState.UNSUPPORTED)
        self.assertEqual(
            caps.state(ObservationKind.OUTPUT_TOKENS),
            ObservationCapabilityState.UNKNOWN)

    def test_mapping_view_is_read_only(self):
        caps = declaration()
        with self.assertRaises(TypeError):
            caps.by_kind[ObservationKind.CONTEXT_USED] = (
                ObservationCapabilityState.SUPPORTED)

    def test_declaration_fields_are_frozen(self):
        caps = declaration()
        with self.assertRaises(FrozenInstanceError):
            caps.by_kind = {}

    def test_no_mutator_api(self):
        caps = declaration()
        for name in ("add", "remove", "discard", "update", "clear",
                     "set_state", "declare"):
            self.assertFalse(hasattr(caps, name), name)

    def test_equal_declarations_are_equal(self):
        first = declaration()
        second = ObservationCapabilities({
            ObservationKind.INPUT_TOKENS:
                ObservationCapabilityState.SUPPORTED,
            ObservationKind.OUTPUT_TOKENS:
                ObservationCapabilityState.SUPPORTED,
        })
        self.assertEqual(first, second)


class AdapterDeclarationTests(unittest.TestCase):
    def test_adapter_can_carry_a_declaration(self):
        caps = declaration()
        adapter = _DeclaringAdapter(caps)
        self.assertIs(adapter.observation_capabilities, caps)

    def test_different_adapters_may_declare_differently(self):
        # 同一通用代码路径，零 runtime 分支 —— 差异只来自声明本身
        rich = _DeclaringAdapter(declaration())
        bare = _DeclaringAdapter(ObservationCapabilities(
            {ObservationKind.INPUT_TOKENS:
             ObservationCapabilityState.UNSUPPORTED}))
        self.assertEqual(
            rich.observation_capabilities.state(
                ObservationKind.INPUT_TOKENS),
            ObservationCapabilityState.SUPPORTED)
        self.assertEqual(
            bare.observation_capabilities.state(
                ObservationKind.INPUT_TOKENS),
            ObservationCapabilityState.UNSUPPORTED)
        self.assertEqual(
            bare.observation_capabilities.state(
                ObservationKind.OUTPUT_TOKENS),
            ObservationCapabilityState.UNKNOWN)

    def test_bare_adapter_declaring_nothing_means_all_unknown(self):
        adapter = _DeclaringAdapter(ObservationCapabilities({}))
        for kind in ObservationKind:
            self.assertEqual(
                adapter.observation_capabilities.state(kind),
                ObservationCapabilityState.UNKNOWN)


class CapabilityNotUsageTests(unittest.TestCase):
    def test_supported_capability_generates_no_usage_record(self):
        log = UsageLog()
        caps = declaration()  # INPUT_TOKENS = SUPPORTED
        self.assertEqual(
            caps.state(ObservationKind.INPUT_TOKENS),
            ObservationCapabilityState.SUPPORTED)
        # 能力声明不产生任何事实观察
        self.assertEqual(log.snapshot(), ())

    def test_supported_does_not_become_known_or_zero(self):
        # SUPPORTED ≠ KNOWN；capability 不产生 input_tokens=0
        self.assertNotEqual(
            ObservationCapabilityState.SUPPORTED, UsageObservation.KNOWN)
        # 两域仅共享 UNKNOWN/UNSUPPORTED 词形（语义域不同：声明态 vs
        # 记录态）；SUPPORTED 只存在于声明域，KNOWN 只存在于事实域
        self.assertEqual(
            {member.name for member in ObservationCapabilityState}
            & {member.name for member in UsageObservation},
            {"UNKNOWN", "UNSUPPORTED"})

    def test_unsupported_does_not_block_real_observation(self):
        # Capability 是能力声明，UsageLog 是事实观察：
        # UNSUPPORTED 不阻止一条已获得的真实观察被记录为 KNOWN
        log = UsageLog()
        caps = declaration(
            INPUT_TOKENS=ObservationCapabilityState.UNSUPPORTED)
        self.assertEqual(
            caps.state(ObservationKind.INPUT_TOKENS),
            ObservationCapabilityState.UNSUPPORTED)
        record = UsageRecord(
            invocation_id="inv-1", task_id="task-1",
            agent_id="agent-architect", role="architect",
            runtime_id="rt-1", status="SUCCESS",
            usage_status=UsageObservation.KNOWN,
            duration_ms=100, input_tokens=10, output_tokens=20)
        log.append(record)
        self.assertEqual(len(log.snapshot()), 1)

    def test_unsupported_is_not_invocation_failure(self):
        # UNSUPPORTED ≠ Invocation failed —— 与 status 无因果关系
        self.assertEqual(
            declaration(
                INPUT_TOKENS=ObservationCapabilityState.UNSUPPORTED
            ).state(ObservationKind.INPUT_TOKENS),
            ObservationCapabilityState.UNSUPPORTED)
        self.assertEqual(InvocationStatus.SUCCESS.value, "SUCCESS")


class TraceBoundaryTests(unittest.TestCase):
    def test_declaration_neither_creates_nor_mutates_trace(self):
        trace = InvocationTrace(
            invocation_id="inv-1", task_id="task-1",
            agent_id="agent-a", runtime="rt-1", provider=None, model=None,
            role="architect", status=InvocationStatus.SUCCESS)
        before = (trace.invocation_id, trace.input_tokens,
                  trace.output_tokens, trace.status)
        declaration()  # 声明动作本身不触碰 trace
        self.assertEqual(
            (trace.invocation_id, trace.input_tokens,
             trace.output_tokens, trace.status), before)

    def test_module_does_not_reference_trace_contract(self):
        source = (SCRIPTS / "observation_capability.py").read_text(
            encoding="utf-8")
        for token in ("InvocationTrace", "external_runtime",
                      "InvocationStatus"):
            self.assertNotIn(token, source)


class NoRuntimeKnowledgeTests(unittest.TestCase):
    def test_module_has_no_vendor_names(self):
        source = (SCRIPTS / "observation_capability.py").read_text(
            encoding="utf-8")
        lowered = source.lower()
        for token in ("claude", "codex", "deepseek", "openai", "anthropic",
                      "gemini", "qwen", "tiny-agents", "tiny_agents"):
            self.assertNotIn(token, lowered)

    def test_module_has_no_runtime_identity_branching(self):
        source = (SCRIPTS / "observation_capability.py").read_text(
            encoding="utf-8")
        self.assertNotIn("runtime_id", source)
        self.assertNotIn("adapter_id", source)


class ImportBoundaryTests(unittest.TestCase):
    def test_module_import_roots_are_stdlib_only(self):
        source = (SCRIPTS / "observation_capability.py").read_text(
            encoding="utf-8")
        roots = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.ImportFrom):
                roots.add(node.module.split(".")[0])
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    roots.add(alias.name.split(".")[0])
        self.assertEqual(
            roots,
            {"__future__", "collections", "dataclasses", "enum", "types"})

    def test_module_does_not_reference_forbidden_domains(self):
        source = (SCRIPTS / "observation_capability.py").read_text(
            encoding="utf-8")
        for token in ("control_boundary", "control_journal", "event_index",
                      "usage_log", "trace_projector", "cockpit",
                      "execution_observation", "subprocess"):
            self.assertNotIn(token, source)

    def test_public_surface_is_minimal(self):
        self.assertEqual(
            observation_capability.__all__,
            ("ObservationCapabilities", "ObservationCapabilityError",
             "ObservationCapabilityState", "ObservationKind"))


if __name__ == "__main__":
    unittest.main()
