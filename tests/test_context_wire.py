"""CU-CONTEXT W1 production wiring 测试。

覆盖：wire 铸造与 PRIOR 准入、邻接旁证推导（行为面）、编译集成
（恰限/超限/指纹/确定性）、兼容矩阵（production contract fixture
行 1-6 与 legacy oracle 逐字节平价；generic 边界行 7/8 DELTA 单独
钉定）、RevisionAdapter overlay 与编译产物共存不变、请求字段恒等、
wire 静态墙、shipped adapter SUCCESS→trace invariant 形状、真链
集成（prior 传播 / HANDOFF 计数 / NEXT_INVOCATION 修订 overlay
追加于编译基础 prompt 之后）。

全部离线；REAL=0。
"""
import re
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

TESTS = Path(__file__).resolve().parent
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))

import cockpit_context_wire  # noqa: E402
import cockpit_entry  # noqa: E402
import host_entry  # noqa: E402
from cockpit_compile import CompilePolicy, compile_context  # noqa: E402
from cockpit_context import (  # noqa: E402
    ContextItemKind,
    ContextSnapshot,
    ContextValidity,
    item_from_invocation_result,
    task_item,
)
from cockpit_context_wire import compile_invocation_context  # noqa: E402
from control_boundary import (  # noqa: E402
    ControlCommand,
    ControlCommandType,
    ControlStatus,
    RevisionPayload,
    RevisionTarget,
)
from external_runtime import ExternalAgentRequest  # noqa: E402
from revision_adapter import _overlay  # noqa: E402
from test_conversation_entry import (  # noqa: E402
    _OfflineAdapter,
    _factories,
    _verified_evidence,
)

_WIRE_SOURCE = Path(
    cockpit_context_wire.__file__).read_text(encoding="utf-8")
_ENTRY_SOURCE = Path(
    cockpit_entry.__file__).read_text(encoding="utf-8")

_STEPS_PER_RUN = 3


def _prior_result(output, invocation_id="inv-w1-prior"):
    """production contract 形 fixture：SUCCESS 结果带 trace+id。"""
    return SimpleNamespace(
        output=output,
        trace=SimpleNamespace(
            invocation_id=invocation_id,
            started_at=None, finished_at=None))


def _legacy_prompt(role, task, prior):
    """legacy oracle：接缝接线前的逐字节推导公式（常量单真源）。"""
    prompt = cockpit_entry._PROMPT_TEMPLATE.format(role=role, task=task)
    if prior:
        if len(prior) > cockpit_entry._EMBED_LIMIT:
            prior = prior[:cockpit_entry._EMBED_LIMIT]
        prompt = prompt + cockpit_entry._PROMPT_PREVIOUS_SECTION + prior
    return prompt


def _builder(task="w1 task", task_id="tid-w1", role="coder",
             provider="prov-x", previous_role="architect",
             step_index=1, emit=None):
    return cockpit_entry._make_request_builder(
        task, task_id, role, provider, 30.0,
        emit=emit, runtime_id="rt-x", previous_role=previous_role,
        step_index=step_index)


def _wire_funnel(*, boundary_hook=None, observation_sink=None):
    """真装配离线漏斗（boundary/sink 可注入）。"""
    adapters = tuple(
        _OfflineAdapter(f"rt-{name}", f"prov-{name}") for name in "abc")
    registry, skipped = host_entry.environment_registry(
        _factories(*adapters))
    evidence = _verified_evidence(registry, "rt-a", "rt-b", "rt-c")
    funnel = cockpit_entry._funnel_composition_closures(
        registry, skipped, evidence, timeout_seconds=None,
        boundary_hook=boundary_hook,
        observation_sink=observation_sink)
    return funnel, adapters


def _drive_to_terminal(composed, cap=12):
    outcome = None
    for _ in range(cap):
        outcome = composed.drive()
        status = getattr(getattr(outcome, "status", None), "value", None)
        if status != "PARKED":
            return outcome
    raise AssertionError("run did not reach terminal within cap")


# ------------------------------------------------ wire 铸造 / PRIOR 准入


class WireMintTests(unittest.TestCase):
    """compile_invocation_context：铸造、准入单一规则、旁证推导。"""

    def test_task_only_when_no_previous_result(self):
        compiled = compile_invocation_context(
            "w1 task", "tid-w1", 0, None, None,
            prior_char_limit=cockpit_entry._EMBED_LIMIT)
        self.assertEqual(len(compiled.segments), 1)
        self.assertIs(compiled.segments[0].kind, ContextItemKind.TASK)
        self.assertEqual(compiled.segments[0].payload, "w1 task")
        self.assertEqual(compiled.step_index, 0)

    def test_prior_minted_with_provenance(self):
        result = _prior_result("prior body", invocation_id="inv-abc-9")
        compiled = compile_invocation_context(
            "w1 task", "tid-w1", 2, result, "architect",
            prior_char_limit=cockpit_entry._EMBED_LIMIT)
        self.assertEqual(len(compiled.segments), 2)
        task_segment, prior_segment = compiled.segments
        self.assertIs(task_segment.kind, ContextItemKind.TASK)
        self.assertIs(prior_segment.kind, ContextItemKind.PRIOR_STEP_OUTPUT)
        self.assertEqual(prior_segment.payload, "prior body")
        self.assertEqual(prior_segment.provenance.source_id, "inv-abc-9")
        self.assertEqual(prior_segment.provenance.producer_role, "architect")
        self.assertIs(prior_segment.provenance.task_id, "tid-w1")

    def test_adjacent_witness_hits_prior(self):
        """行为面：合格 prior → PRIOR 段入选（旁证恰一元组命中）。"""
        result = _prior_result("prior body", invocation_id="inv-hit-1")
        compiled = compile_invocation_context(
            "w1 task", "tid-w1", 1, result, None,
            prior_char_limit=cockpit_entry._EMBED_LIMIT)
        self.assertEqual(
            tuple(segment.kind for segment in compiled.segments),
            (ContextItemKind.TASK, ContextItemKind.PRIOR_STEP_OUTPUT))

    def test_missing_adjacent_witness_structurally_drops_prior(self):
        """compiler 行为钉定（wire 依赖）：无旁证 → PRIOR 静默排除。"""
        result = _prior_result("prior body", invocation_id="inv-miss-1")
        prior = item_from_invocation_result(
            result, task_id="tid-w1", producer_role=None)
        snapshot = ContextSnapshot(
            task_id="tid-w1", step_index=1,
            items=(task_item("w1 task", task_id="tid-w1"), prior),
            validity=(ContextValidity.VALID, ContextValidity.VALID))
        unwitnessed = compile_context(snapshot, CompilePolicy())
        self.assertEqual(len(unwitnessed.segments), 1)
        self.assertIs(unwitnessed.segments[0].kind, ContextItemKind.TASK)

    def test_whitespace_output_omits_prior_without_raise(self):
        # 矩阵行 7（generic 面 DOCUMENTED SEMANTIC DELTA 的 wire 级钉定）
        for output in (" ", "\n\t "):
            compiled = compile_invocation_context(
                "w1 task", "tid-w1", 1,
                _prior_result(output), None,
                prior_char_limit=cockpit_entry._EMBED_LIMIT)
            self.assertEqual(len(compiled.segments), 1)

    def test_empty_nonstr_and_missing_trace_omit_prior(self):
        # 空/非 str 输出（两侧同省略=平价）；trace 缺席/空白 id =
        # out-of-contract 防御性省略（矩阵行 8 generic 面）
        compiled = compile_invocation_context(
            "w1 task", "tid-w1", 1,
            SimpleNamespace(output="", trace=None), None,
            prior_char_limit=cockpit_entry._EMBED_LIMIT)
        self.assertEqual(len(compiled.segments), 1)
        compiled = compile_invocation_context(
            "w1 task", "tid-w1", 1,
            SimpleNamespace(output=123), None,
            prior_char_limit=cockpit_entry._EMBED_LIMIT)
        self.assertEqual(len(compiled.segments), 1)
        compiled = compile_invocation_context(
            "w1 task", "tid-w1", 1,
            SimpleNamespace(output="body", trace=None), None,
            prior_char_limit=cockpit_entry._EMBED_LIMIT)
        self.assertEqual(len(compiled.segments), 1)
        compiled = compile_invocation_context(
            "w1 task", "tid-w1", 1,
            SimpleNamespace(output="body",
                            trace=SimpleNamespace(invocation_id=" ")),
            None,
            prior_char_limit=cockpit_entry._EMBED_LIMIT)
        self.assertEqual(len(compiled.segments), 1)

    def test_validity_all_valid_at_fresh_mint(self):
        compiled = compile_invocation_context(
            "w1 task", "tid-w1", 1, _prior_result("prior body"), None,
            prior_char_limit=cockpit_entry._EMBED_LIMIT)
        for segment in compiled.segments:
            self.assertIs(segment.validity_at_compile,
                          ContextValidity.VALID)


# ------------------------------------------------ 编译集成（限额/确定性）


class WireCompileTests(unittest.TestCase):
    """政策单真源派生、截断语义、指纹、确定性重放。"""

    def test_exactly_limit_not_truncated(self):
        # 两侧同严格大于：恰 4000 不截（矩阵行 3）
        output = "B" * cockpit_entry._EMBED_LIMIT
        compiled = compile_invocation_context(
            "w1 task", "tid-w1", 1, _prior_result(output), None,
            prior_char_limit=cockpit_entry._EMBED_LIMIT)
        prior_segment = compiled.segments[1]
        self.assertEqual(len(prior_segment.payload),
                         cockpit_entry._EMBED_LIMIT)
        self.assertEqual(compiled.budget.truncations, ())

    def test_over_limit_head_truncated(self):
        # 矩阵行 4：codepoint 头截 + 截断记账
        output = "C" * (cockpit_entry._EMBED_LIMIT + 250)
        compiled = compile_invocation_context(
            "w1 task", "tid-w1", 1, _prior_result(output), None,
            prior_char_limit=cockpit_entry._EMBED_LIMIT)
        prior_segment = compiled.segments[1]
        self.assertEqual(
            prior_segment.payload, output[:cockpit_entry._EMBED_LIMIT])
        (fact,) = compiled.budget.truncations
        self.assertIs(fact.kind, ContextItemKind.PRIOR_STEP_OUTPUT)
        self.assertEqual(fact.original_chars,
                         cockpit_entry._EMBED_LIMIT + 250)
        self.assertEqual(fact.embedded_chars, cockpit_entry._EMBED_LIMIT)

    def test_policy_fingerprint_stability(self):
        kwargs = dict(prior_char_limit=cockpit_entry._EMBED_LIMIT)
        first = compile_invocation_context(
            "w1 task", "tid-w1", 1, _prior_result("prior"), None, **kwargs)
        second = compile_invocation_context(
            "w1 task", "tid-w1", 1, _prior_result("prior"), None, **kwargs)
        self.assertEqual(first.policy_fingerprint, second.policy_fingerprint)
        self.assertEqual(
            first.policy_fingerprint,
            CompilePolicy(
                prior_output_char_limit=cockpit_entry._EMBED_LIMIT
            ).fingerprint)
        other = compile_invocation_context(
            "w1 task", "tid-w1", 1, _prior_result("prior"), None,
            prior_char_limit=cockpit_entry._EMBED_LIMIT + 1)
        self.assertNotEqual(first.policy_fingerprint,
                            other.policy_fingerprint)

    def test_deterministic_replay(self):
        result = _prior_result("replay body", invocation_id="inv-rep-1")
        first = compile_invocation_context(
            "w1 task", "tid-w1", 3, result, "architect",
            prior_char_limit=cockpit_entry._EMBED_LIMIT)
        second = compile_invocation_context(
            "w1 task", "tid-w1", 3, result, "architect",
            prior_char_limit=cockpit_entry._EMBED_LIMIT)
        self.assertEqual(first, second)


# ------------------------------------------------ 接缝兼容矩阵（行 1-6 平价）


class SeamParityTests(unittest.TestCase):
    """production contract fixture 上与 legacy oracle 逐字节平价。"""

    def test_matrix_rows_byte_identical(self):
        limit = cockpit_entry._EMBED_LIMIT
        cases = {
            "no-prior": None,
            "normal": _prior_result("short prior body"),
            "exactly-4000": _prior_result("D" * limit),
            "over-4000": _prior_result("E" * (limit + 17)),
        }
        for name, previous_result in cases.items():
            with self.subTest(case=name):
                request = _builder()(previous_result)
                prior = (None if previous_result is None
                         else previous_result.output)
                self.assertEqual(
                    request.prompt, _legacy_prompt("coder", "w1 task", prior))

    def test_handoff_emitted_iff_prior_embedded(self):
        emitted = []

        def emit(event_type, **fields):
            emitted.append((event_type, fields))

        builder = _builder(emit=emit)
        request = builder(None)
        self.assertNotIn("PREVIOUS STEP OUTPUT", request.prompt)
        self.assertEqual(emitted, [])
        request = builder(_prior_result("prior body"))
        self.assertIn("PREVIOUS STEP OUTPUT", request.prompt)
        self.assertEqual(len(emitted), 1)
        event_type, fields = emitted[0]
        self.assertEqual(getattr(event_type, "value", event_type), "HANDOFF")
        self.assertEqual(
            fields, {"stage": "architect", "runtime_id": "rt-x",
                     "status": "EMBEDDED", "reason": "EMBEDDED"})

    def test_request_fields_beyond_prompt_unchanged(self):
        request = _builder()(_prior_result("prior body"))
        self.assertEqual(request.task_id, "tid-w1")
        self.assertEqual(request.agent_id, "cockpit-coder")
        self.assertEqual(request.role, "coder")
        self.assertEqual(request.provider, "prov-x")
        self.assertIsNone(request.model)
        self.assertEqual(request.timeout_seconds, 30.0)
        self.assertEqual(request.handoff_packets, ())

    def test_resume_replay_identical(self):
        # 矩阵行 5：同一携带 previous_result 经独立重建的 builder
        # 推导 → 逐字节同（PARKED 续走确定性）
        carried = _prior_result("carried body", invocation_id="inv-carry-1")
        prompt_a = _builder(step_index=2)(carried).prompt
        prompt_b = _builder(step_index=2)(carried).prompt
        self.assertEqual(prompt_a, prompt_b)
        self.assertEqual(
            prompt_a,
            _legacy_prompt("coder", "w1 task", carried.output))

    def test_submission_rebuild_new_task_text(self):
        # 矩阵行 6：fresh segment 重建工厂以合并后文本铸 TASK 段
        merged = "w1 task revised by SUBMISSION merge"
        request = _builder(task=merged)(None)
        self.assertEqual(
            request.prompt, _legacy_prompt("coder", merged, None))

    def test_generic_delta_whitespace_no_section_no_handoff(self):
        # 矩阵行 7（seam 面）：无节、无 HANDOFF、运行不失败
        emitted = []

        def emit(event_type, **fields):
            emitted.append((event_type, fields))

        request = _builder(emit=emit)(
            SimpleNamespace(output=" ", trace=SimpleNamespace(
                invocation_id="inv-ws-1")))
        self.assertNotIn("PREVIOUS STEP OUTPUT", request.prompt)
        self.assertEqual(emitted, [])

    def test_generic_delta_trace_missing_no_section_no_handoff(self):
        # 矩阵行 8（generic 面）：out-of-contract 输入防御性省略
        emitted = []

        def emit(event_type, **fields):
            emitted.append((event_type, fields))

        request = _builder(emit=emit)(
            SimpleNamespace(output="orphan body", trace=None))
        self.assertNotIn("PREVIOUS STEP OUTPUT", request.prompt)
        self.assertEqual(emitted, [])

    def test_revision_overlay_coexistence_unchanged(self):
        # RevisionAdapter overlay 语义（frozen owner）与编译产物共存：
        # 同一条目下，wired 基础 prompt 与 legacy oracle 的 overlay
        # 结果逐字节同（overlay 独立于基础 prompt 的派生方式）
        entries = (SimpleNamespace(revision_id="rev-w1-1",
                                   text="W1 OVERLAY TEXT"),)
        request = _builder()(_prior_result("prior body"))
        overlaid_wired = _overlay(request.prompt, entries)
        overlaid_legacy = _overlay(
            _legacy_prompt("coder", "w1 task", "prior body"), entries)
        self.assertEqual(overlaid_wired, overlaid_legacy)
        self.assertIn("[USER REVISION rev-w1-1]", overlaid_wired)


# ------------------------------------------------ wire 静态墙


class WireStaticWallTests(unittest.TestCase):
    """wire 分层纪律：单面、单向依赖、零调用/零持久化。"""

    def test_single_public_face(self):
        self.assertEqual(cockpit_context_wire.__all__,
                         ("compile_invocation_context",))

    def test_import_surface_exactly_semantic_pair(self):
        # 双分支恰各一（与 entry 单一 module graph 纪律同构）
        self.assertEqual(_WIRE_SOURCE.count(
            "from cockpit_compile import"), 1)
        self.assertEqual(_WIRE_SOURCE.count(
            "from .cockpit_compile import"), 1)
        self.assertEqual(_WIRE_SOURCE.count(
            "from cockpit_context import"), 1)
        self.assertEqual(_WIRE_SOURCE.count(
            "from .cockpit_context import"), 1)

    def test_no_runtime_route_memory_tui_persistence(self):
        for banned in ("cockpit_route", "cockpit_memory", "cockpit_tui",
                       "cockpit_entry", "sequential_pipeline",
                       "execution_slots", "EventIndex", "ControlJournal",
                       ".invoke(", "emit(", "open(", "sqlite",
                       "subprocess", "socket", "requests", "pathlib",
                       "tempfile", "pickle", "shelve", "while "):
            self.assertNotIn(banned, _WIRE_SOURCE)

    def test_seam_reads_prior_facts_only_via_wire(self):
        # 接缝 closure 不再直读 previous_result 的事实面（单通道：
        # 事实读取全部居住 wire）
        start = _ENTRY_SOURCE.index("def _make_request_builder")
        end = _ENTRY_SOURCE.index(
            "# ---------------------------------------------------------- observation")
        seam = _ENTRY_SOURCE[start:end]
        self.assertNotIn("getattr(previous_result", seam)
        self.assertNotIn("previous_result.output", seam)


# ------------------------------------------------ shipped adapter invariant


class AdapterInvariantShapeTests(unittest.TestCase):
    """W1 production contract 形状钉定：8 家 shipped adapter 的每处
    SUCCESS 结果构造都在构造窗内携带 trace（finish helper）。"""

    _ADAPTERS = (
        "claude_code_adapter.py", "codex_adapter.py",
        "gemini_adapter.py", "qwen_adapter.py",
        "opencode_adapter.py", "pi_adapter.py",
        "cline_adapter.py", "tiny_agents_adapter.py",
    )

    def test_success_constructions_carry_trace(self):
        pattern = re.compile(
            r"InvocationResult\(\s*InvocationStatus\.SUCCESS,(.{0,400})",
            re.S)
        for name in self._ADAPTERS:
            source = (SCRIPTS / name).read_text(encoding="utf-8")
            matches = pattern.findall(source)
            self.assertTrue(
                matches, f"{name} has no SUCCESS construction to pin")
            for window in matches:
                self.assertIn(
                    "trace=self._finish_trace", window,
                    f"{name}: SUCCESS construction without trace")


# ------------------------------------------------ 真链集成


class _EventSink:
    def __init__(self):
        self.events = []

    def on_event(self, event):
        self.events.append(event)


class ChainIntegrationTests(unittest.TestCase):
    """真装配离线链：prior 传播、HANDOFF 计数、修订 overlay 时序。"""

    def test_chain_prior_propagation_and_handoff_count(self):
        sink = _EventSink()
        funnel, adapters = _wire_funnel(observation_sink=sink)
        composed = funnel.start("w1 chain probe", funnel.preview())
        outcome = _drive_to_terminal(composed)
        self.assertEqual(outcome.status.value, "COMPLETED")
        prompts = [request.prompt for adapter in adapters
                   for request in adapter.requests
                   if request.task_id == composed.task_id]
        self.assertEqual(len(prompts), _STEPS_PER_RUN)
        # 步 2/3 嵌紧邻前步输出（编译段 → PREVIOUS 节）
        self.assertIn("PREVIOUS STEP OUTPUT", prompts[1])
        self.assertIn("PREVIOUS STEP OUTPUT", prompts[2])
        self.assertIn("output-rt-a-1", prompts[1])
        self.assertIn("output-rt-b-1", prompts[2])
        # HANDOFF 恰嵌入次（步 2/3），字段=组合事实
        handoffs = [event for event in sink.events
                    if getattr(event.event_type, "value", None) == "HANDOFF"]
        self.assertEqual(len(handoffs), _STEPS_PER_RUN - 1)

    def test_chain_revision_overlay_appends_to_compiled_base(self):
        holder = {}

        def hook(boundary, execution_id):
            holder["boundary"] = boundary
            holder["execution_id"] = execution_id

        funnel, adapters = _wire_funnel(boundary_hook=hook)
        composed = funnel.start("w1 overlay probe", funnel.preview())
        boundary = holder["boundary"]
        result = boundary.submit(ControlCommand(
            command_id="w1-rev-1",
            execution_id=holder["execution_id"],
            command=ControlCommandType.REVISE,
            payload=RevisionPayload(
                target=RevisionTarget.NEXT_INVOCATION,
                text="W1 OVERLAY SENTINEL 41C7"),
            expected_version=boundary.execution_version))
        self.assertIs(result.status, ControlStatus.ACCEPTED)
        outcome = _drive_to_terminal(composed)
        self.assertEqual(outcome.status.value, "COMPLETED")
        prompts = [request.prompt for adapter in adapters
                   for request in adapter.requests
                   if request.task_id == composed.task_id]
        self.assertEqual(len(prompts), _STEPS_PER_RUN)
        # overlay 恰落步 1（首次调用后一次性消费），追加于编译基础之后
        self.assertIn("[USER REVISION w1-rev-1]", prompts[0])
        self.assertIn("W1 OVERLAY SENTINEL 41C7", prompts[0])
        self.assertTrue(prompts[0].startswith(
            cockpit_entry._PROMPT_TEMPLATE.format(
                role="architect", task="w1 overlay probe")))
        # 步 2/3 无 overlay、有编译 PREVIOUS 节（共存且互不干扰）
        self.assertNotIn("[USER REVISION", prompts[1])
        self.assertNotIn("[USER REVISION", prompts[2])
        self.assertIn("PREVIOUS STEP OUTPUT", prompts[1])


if __name__ == "__main__":
    unittest.main()
