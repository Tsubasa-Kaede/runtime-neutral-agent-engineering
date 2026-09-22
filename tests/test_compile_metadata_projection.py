"""CU-CONTEXT W3-P — COMPILER METADATA PROJECTION DISCLOSURE 测试。

W3-P 实施轮：把 Compiler 衍生元数据经呈现层 additive 链忠实披露到
既有 Context panel——本文件钉定该链的每一段与每条边界律。

披露链（W3-P 目标态）：

    compile_invocation_context（已完成编译产物）
      → cockpit_entry._compile_disclosure_record（只读转录，零重编译）
        → CompileDisclosure（呈现域记录，cockpit_projection 定义）
          → ProjectionInputs.compile_metadata（additive，缺省 None）
            → _context_lines COMPILE 段（现有 Context panel 内）

边界律（W3-D 批准设计 + 本轮授权面）：Compiler ≠ Observation
Truth；Metadata ≠ Usage Truth（COMPILE 段 ≠ SESSION 段——编译期
记账与 runtime 用量分节呈现、绝不混算）；UNKNOWN ≠ 0（计量状态词
照实，字符数绝不折算/表述/暗示为 token 数）；Projection ≠ Source
of Truth；TUI 绝不直读 CompiledInvocationContext（只见
CompileDisclosure）；缺省 compile_metadata=None 下投影输出与既有
渲染逐字节一致。

测试面：记录纯值性 / additive 字节等价矩阵 / COMPILE 段渲染 /
词汇律 / 接缝只读转录 / 真离线装配链（offline adapters）。
全部离线；REAL=0。
"""
import ast
import dataclasses
import inspect
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import cockpit_entry  # noqa: E402
import cockpit_projection  # noqa: E402
import cockpit_tui  # noqa: E402
import host_entry  # noqa: E402
from candidate_validation import CandidateValidationStatus  # noqa: E402
from cockpit_context_wire import compile_invocation_context  # noqa: E402
from cockpit_projection import (  # noqa: E402
    AgentSlotView,
    CompileDisclosure,
    ProjectionInputs,
    build_projection,
)
from event_index import EventIndex  # noqa: E402
from execution_observation import (  # noqa: E402
    ExecutionEvent,
    ExecutionEventType,
)
from external_runtime import (  # noqa: E402
    InvocationResult,
    InvocationStatus,
    InvocationTrace,
)
from usage_log import UsageObservation, UsageRecord  # noqa: E402

_ENTRY_SOURCE = Path(cockpit_entry.__file__).read_text(encoding="utf-8")
_TUI_SOURCE = Path(cockpit_tui.__file__).read_text(encoding="utf-8")

_COMPILE_DOMAIN_ROOTS = ("cockpit_compile", "cockpit_context",
                         "cockpit_context_wire")


def _prior(output="prior output text", invocation_id="w3p-inv-prior"):
    return SimpleNamespace(
        output=output,
        trace=SimpleNamespace(invocation_id=invocation_id,
                              started_at=None, finished_at=None))


def _disclosure(task="w3p task", prior=None, task_id="w3p-task",
                step_index=1):
    """经接缝真转录获得披露记录（唯一生产映射路径）。"""
    records = []
    builder = cockpit_entry._make_request_builder(
        task, task_id, "coder", "prov-w3p", 60.0,
        previous_role="architect", step_index=step_index,
        disclosure_sink=records.append)
    builder(prior)
    return records[0]


def _slots(count=1):
    return tuple(
        AgentSlotView(stage=f"step-{index}-coder", role="coder",
                      runtime_id=f"rt-{index}", provider=f"prov-{index}")
        for index in range(count))


def _known_usage(input_tokens=120, output_tokens=80):
    return UsageRecord(
        invocation_id="w3p-inv", task_id="w3p-task", agent_id="w3p-agent",
        role="coder", runtime_id="rt-0", status="SUCCESS",
        usage_status=UsageObservation.KNOWN,
        input_tokens=input_tokens, output_tokens=output_tokens)


def _started_event(sequence=1):
    return ExecutionEvent(
        event_type=ExecutionEventType.INVOCATION_STARTED,
        sequence=sequence, task_id="w3p-task", correlation_id="w3p-c",
        stage="step-0-coder", runtime_id="rt-0",
        status="STARTED", reason="STARTED")


# ------------------------------------------------------- 披露记录纯值性


class CompileDisclosureRecordTests(unittest.TestCase):
    """CompileDisclosure：呈现域纯值记录（frozen、字段封闭、轻验证）。"""

    def test_field_set_is_exact_and_closed(self):
        names = {field.name for field in dataclasses.fields(CompileDisclosure)}
        self.assertEqual(names, {
            "task_id", "step_index", "policy_fingerprint",
            "item_counts_by_kind", "embedded_chars_by_kind",
            "total_embedded_chars", "truncations", "selection_notes",
            "token_status"})

    def test_record_is_frozen(self):
        record = _disclosure()
        with self.assertRaises(dataclasses.FrozenInstanceError):
            record.task_id = "mutated"

    def test_validation_rejects_non_tuple_and_negative(self):
        with self.assertRaises(ValueError):
            CompileDisclosure(
                task_id="t", step_index=0, policy_fingerprint="policy_x",
                item_counts_by_kind="not-a-tuple",
                embedded_chars_by_kind=(), total_embedded_chars=0,
                truncations=(), selection_notes=(),
                token_status="UNKNOWN")
        with self.assertRaises(ValueError):
            CompileDisclosure(
                task_id="t", step_index=-1, policy_fingerprint="policy_x",
                item_counts_by_kind=(), embedded_chars_by_kind=(),
                total_embedded_chars=0, truncations=(),
                selection_notes=(), token_status="UNKNOWN")

    def test_projection_module_exports_disclosure_record(self):
        self.assertIn("CompileDisclosure", cockpit_projection.__all__)
        self.assertIs(cockpit_projection.CompileDisclosure, CompileDisclosure)


# -------------------------------------------------- ProjectionInputs additive


class ProjectionInputsAdditiveTests(unittest.TestCase):
    """additive 位：缺省 None = 逐字节一致；激活仅追加 COMPILE 段。"""

    def test_compile_metadata_slot_defaults_to_none(self):
        default = inspect.signature(
            ProjectionInputs.__init__).parameters[
                "compile_metadata"].default
        self.assertIsNone(default)
        values = ProjectionInputs()
        self.assertIsNone(values.compile_metadata)

    def test_explicit_none_is_byte_equivalent_to_legacy_construction(self):
        record = _disclosure()
        common = dict(task="t", slots=_slots(2), width=160,
                      compile_metadata=record)
        legacy = build_projection(
            ProjectionInputs(task="t", slots=_slots(2), width=160))
        explicit = build_projection(ProjectionInputs(
            task="t", slots=_slots(2), width=160, compile_metadata=None))
        for field in legacy.__slots__:
            self.assertEqual(getattr(legacy, field),
                             getattr(explicit, field), field)
        activated = build_projection(ProjectionInputs(**common))
        # 激活只追加 COMPILE 行——其余投影面逐字段不变
        for field in legacy.__slots__:
            if field == "context_lines":
                continue
            self.assertEqual(getattr(legacy, field),
                             getattr(activated, field), field)
        self.assertTrue(
            activated.context_lines[:len(legacy.context_lines)]
            == legacy.context_lines)

    def test_scenario_matrix_none_kwarg_never_changes_output(self):
        record = _disclosure()
        scenarios = (
            dict(task="", slots=()),                                  # 空
            dict(task="only task", slots=_slots(1)),                  # TASK 面
            dict(task="t", slots=_slots(1),
                 events=(_started_event(),)),                         # 事件面
            dict(task="t", slots=_slots(1),
                 usage_records=(_known_usage(),)),                    # KNOWN 用量
            dict(task="t", slots=_slots(4), width=139),               # 窄门
            dict(task="t", slots=_slots(1), width=200,
                 groups=(), member_ids=(), scroll_mode=True),         # 2.8-B 面
            dict(task="t", slots=_slots(1), include_trace=True,
                 events=(_started_event(),), facts=(),
                 usage_records=(_known_usage(),)),                    # trace 面
        )
        for scenario in scenarios:
            legacy = build_projection(ProjectionInputs(**scenario))
            explicit = build_projection(ProjectionInputs(
                compile_metadata=None, **scenario))
            for field in legacy.__slots__:
                self.assertEqual(getattr(legacy, field),
                                 getattr(explicit, field),
                                 f"{field} @ {scenario}")

    def test_narrow_width_short_circuit_unaffected_by_activation(self):
        # width<140 = 既有 context 空门——披露在场也不破例（空间不撒谎）
        lines = build_projection(ProjectionInputs(
            task="t", slots=_slots(1), width=139,
            compile_metadata=_disclosure())).context_lines
        self.assertEqual(lines, ())


# ------------------------------------------------------- COMPILE 段渲染


class CompileSectionRenderingTests(unittest.TestCase):
    """COMPILE 段：现有 Context panel 内忠实转录（COMPILE ≠ SESSION）。"""

    @staticmethod
    def _lines(record, **extra):
        return build_projection(ProjectionInputs(
            task="t", slots=_slots(1), width=200,
            compile_metadata=record, **extra)).context_lines

    def test_compile_section_follows_session_section(self):
        lines = self._lines(_disclosure())
        self.assertIn("SESSION", lines)
        self.assertIn("COMPILE", lines)
        self.assertLess(lines.index("SESSION"), lines.index("COMPILE"))

    def test_identity_lines_carry_task_id_and_step(self):
        lines = self._lines(_disclosure(task_id="w3p-task", step_index=2))
        self.assertIn("  task-id  w3p-task", lines)
        self.assertIn("  step  2", lines)

    def test_policy_fingerprint_line_present(self):
        lines = self._lines(_disclosure())
        fingerprint = _disclosure().policy_fingerprint
        self.assertTrue(fingerprint.startswith("policy_"))
        self.assertTrue(
            any(fingerprint[:14] in line for line in lines), lines)

    def test_items_and_chars_lines_use_closed_kind_words(self):
        record = _disclosure(task="12345", prior=_prior())
        lines = self._lines(record)
        self.assertIn("  items  TASK 1", lines)
        self.assertIn("  items  PRIOR_STEP_OUTPUT 1", lines)
        # 5 task chars + 17 prior chars = 22（chars 记账，非 token）
        self.assertIn("  total-chars  22", lines)
        # chars 词根恒在（命名律）；char 行按 kind 秩呈现
        chars_lines = [line for line in lines
                       if line.startswith("  chars")]
        self.assertTrue(chars_lines, lines)

    def test_truncation_fact_line_records_original_and_embedded(self):
        record = _disclosure(
            task="t", prior=_prior(output="B" * 4500))
        self.assertEqual(record.truncations,
                         (("PRIOR_STEP_OUTPUT", 4500, 4000),))
        lines = self._lines(record)
        # 面板 28 列宽度律：截断行前缀在场（精确三数值由记录钉定）
        self.assertTrue(
            any(line.startswith("  truncated") for line in lines), lines)

    def test_token_line_is_exactly_unknown_and_singular(self):
        lines = self._lines(_disclosure())
        self.assertIn("  token  UNKNOWN", lines)
        # 单数 token（编译期计量状态）≠ 复数 tokens（SESSION runtime 用量）
        self.assertNotIn("  tokens  UNKNOWN", lines)


# ------------------------------------------------------- 披露真值律


class DisclosureTruthLawsTests(unittest.TestCase):
    """UNKNOWN ≠ 0；chars ≠ tokens；COMPILE ≠ SESSION；确定性重放。"""

    def test_context_lines_free_of_banned_measurement_vocabulary(self):
        record = _disclosure(prior=_prior(output="B" * 4500))
        lines = build_projection(ProjectionInputs(
            task="t", slots=_slots(1), width=200,
            compile_metadata=record,
            usage_records=(_known_usage(),))).context_lines
        joined = "\n".join(lines)
        for banned in ("estimated", "input token", "input_tokens",
                       "cost", "billing", "saved", "optimized"):
            self.assertNotIn(banned, joined, banned)

    def test_every_token_wording_line_is_either_session_or_compile(self):
        # 出现 "token" 词根的行只有两类：SESSION runtime 用量行与
        # COMPILE 计量状态行——字符数绝不被表述为 token 数
        lines = build_projection(ProjectionInputs(
            task="t", slots=_slots(1), width=200,
            compile_metadata=_disclosure(),
            usage_records=(_known_usage(120, 80),))).context_lines
        token_lines = [line for line in lines if "token" in line]
        self.assertEqual(token_lines,
                         ["  tokens  200", "  token  UNKNOWN"])

    def test_compile_token_stays_unknown_when_runtime_usage_is_known(self):
        # runtime 用量 KNOWN（SESSION tokens 有数）不反哺编译期计量：
        # COMPILE token 恒 UNKNOWN（两真值域分节、绝不混算）
        lines = build_projection(ProjectionInputs(
            task="t", slots=_slots(1), width=200,
            compile_metadata=_disclosure(),
            usage_records=(_known_usage(120, 80),))).context_lines
        self.assertIn("  tokens  200", lines)
        self.assertIn("  token  UNKNOWN", lines)

    def test_record_transcribes_compiled_product_faithfully(self):
        task, prior_text = "faithful task", "faithful prior"
        record = _disclosure(task=task, prior=_prior(output=prior_text))
        compiled = compile_invocation_context(
            task, record.task_id, 1, _prior(output=prior_text),
            "architect", prior_char_limit=4000)
        self.assertEqual(record.policy_fingerprint,
                         compiled.policy_fingerprint)
        self.assertEqual(record.item_counts_by_kind,
                         compiled.budget.item_counts_by_kind)
        self.assertEqual(record.embedded_chars_by_kind,
                         compiled.budget.embedded_chars_by_kind)
        self.assertEqual(record.total_embedded_chars,
                         compiled.budget.total_embedded_chars)
        self.assertEqual(record.token_status, "UNKNOWN")
        self.assertIsNone(compiled.token_measure.tokens)

    def test_deterministic_replay_yields_equal_records(self):
        first = _disclosure(prior=_prior(output="replay body"))
        second = _disclosure(prior=_prior(output="replay body"))
        self.assertEqual(first, second)


# --------------------------------------------------- 接缝只读转录


class EntrySeamDisclosureTests(unittest.TestCase):
    """builder 披露缝：只读、零 prompt 影响、零新增事件。"""

    def test_sink_present_leaves_request_bytes_identical(self):
        prior = _prior(output="seam prior")
        with_sink = []
        request_a = cockpit_entry._make_request_builder(
            "seam task", "w3p-task", "coder", "prov", 60.0,
            previous_role="architect", step_index=1,
            disclosure_sink=with_sink.append)(prior)
        request_b = cockpit_entry._make_request_builder(
            "seam task", "w3p-task", "coder", "prov", 60.0,
            previous_role="architect", step_index=1)(prior)
        self.assertEqual(request_a.prompt, request_b.prompt)
        self.assertEqual(request_a.task_id, request_b.task_id)
        self.assertEqual(request_a.agent_id, request_b.agent_id)
        self.assertEqual(len(with_sink), 1)

    def test_prompt_assembly_ignores_metadata(self):
        # prompt 字节 oracle：模板 + segments 直拼——披露词零渗入
        task, prior_text = "oracle task", "oracle prior"
        record_holder = []
        builder = cockpit_entry._make_request_builder(
            task, "w3p-task", "coder", "prov", 60.0,
            previous_role="architect", step_index=0,
            disclosure_sink=record_holder.append)
        request = builder(_prior(output=prior_text))
        expected = (
            cockpit_entry._PROMPT_TEMPLATE.format(role="coder", task=task)
            + cockpit_entry._PROMPT_PREVIOUS_SECTION + prior_text)
        self.assertEqual(request.prompt, expected)
        for token in ("policy_", "UNKNOWN", "embedded_chars"):
            self.assertNotIn(token, request.prompt)

    def test_disclosure_emits_no_extra_observation_events(self):
        events = []

        def recorder(event_type, **kwargs):
            events.append((event_type, kwargs))

        cockpit_entry._make_request_builder(
            "task", "w3p-task", "coder", "prov", 60.0,
            emit=recorder, runtime_id="rt-w3p",
            previous_role="architect", step_index=1,
            disclosure_sink=lambda record: None)(_prior())
        # 披露在场：事件面仍恰一 HANDOFF（零新增编译事件）
        self.assertEqual(len(events), 1)
        no_sink_events = []
        cockpit_entry._make_request_builder(
            "task", "w3p-task", "coder", "prov", 60.0,
            emit=lambda event_type, **kwargs: no_sink_events.append(
                (event_type, kwargs)),
            runtime_id="rt-w3p", previous_role="architect", step_index=1,
        )(_prior())
        self.assertEqual(events, no_sink_events)

    def test_truncation_scenario_transcribed(self):
        record = _disclosure(
            task="t", prior=_prior(output="C" * (4000 + 500)))
        self.assertEqual(record.truncations,
                         (("PRIOR_STEP_OUTPUT", 4500, 4000),))
        self.assertEqual(
            dict(record.embedded_chars_by_kind)["PRIOR_STEP_OUTPUT"], 4000)
        self.assertEqual(record.total_embedded_chars, 4001)

    def test_no_prior_scenario_records_task_only_compile(self):
        record = _disclosure(task="solo", prior=None, step_index=0)
        self.assertEqual(dict(record.item_counts_by_kind)["TASK"], 1)
        self.assertEqual(dict(record.item_counts_by_kind)
                         ["PRIOR_STEP_OUTPUT"], 0)
        self.assertEqual(record.truncations, ())
        self.assertEqual(record.total_embedded_chars, len("solo"))

    def test_entry_imports_disclosure_record_via_both_branches(self):
        self.assertEqual(
            _ENTRY_SOURCE.count(
                "from cockpit_projection import CompileDisclosure"), 1)
        self.assertEqual(
            _ENTRY_SOURCE.count(
                "from .cockpit_projection import CompileDisclosure"), 1)


# --------------------------------------------------- 真离线装配链


class _OfflineAdapter:
    """Offline double（user_composition_entry 同款）：REAL=0。"""

    def __init__(self, runtime, provider):
        self.profile = SimpleNamespace(
            runtime=runtime, provider=provider, model=None,
            agent_id=f"{runtime}-agent")
        self.requests = []

    def invoke(self, request):
        self.requests.append(request)
        count = len(self.requests)
        return InvocationResult(
            status=InvocationStatus.SUCCESS,
            output=f"output-{self.profile.runtime}-{count}",
            error=None,
            trace=InvocationTrace(
                invocation_id=f"inv-{self.profile.runtime}-{count}",
                task_id=request.task_id, agent_id=request.agent_id,
                runtime=self.profile.runtime,
                provider=self.profile.provider, model=None,
                role=request.role, status=InvocationStatus.SUCCESS))


class AssembledChainTests(unittest.TestCase):
    """_assemble_execution → ComposedRun.compile_disclosure 只读闭包。"""

    @staticmethod
    def _composed():
        adapters = (_OfflineAdapter("rt-a", "prov-a"),
                    _OfflineAdapter("rt-b", "prov-b"))
        registry, skipped = host_entry.environment_registry(
            [lambda bound=adapter: bound for adapter in adapters])
        evidence = {registry.get(rt).identity: SimpleNamespace(
            status=CandidateValidationStatus.VERIFIED)
            for rt in ("rt-a", "rt-b")}
        steps = (("coder", "rt-a"), ("reviewer", "rt-b"))
        resolved = cockpit_entry._resolve_runtimes(
            registry, skipped, evidence, steps)
        return cockpit_entry._assemble_execution(
            resolved, "w3p chain task", steps, None,
            event_index=EventIndex()), adapters

    def test_composed_run_complosure_default_and_precall_none(self):
        composed, adapters = self._composed()
        self.assertTrue(callable(composed.compile_disclosure))
        # 首次编译前：诚实 None（零伪造编译前记录）
        self.assertIsNone(composed.compile_disclosure())
        self.assertEqual(adapters[0].requests, [])

    def test_drive_populates_last_step_disclosure(self):
        composed, adapters = self._composed()
        outcome = composed.drive()
        self.assertIsNotNone(outcome)
        record = composed.compile_disclosure()
        self.assertIsInstance(record, CompileDisclosure)
        self.assertEqual(record.task_id, composed.task_id)
        # 末步（reviewer，step 1）在位：TASK + PRIOR 各一
        self.assertEqual(record.step_index, 1)
        self.assertEqual(dict(record.item_counts_by_kind)["TASK"], 1)
        self.assertEqual(dict(record.item_counts_by_kind)
                         ["PRIOR_STEP_OUTPUT"], 1)
        self.assertEqual(
            dict(record.embedded_chars_by_kind)["PRIOR_STEP_OUTPUT"],
            len("output-rt-a-1"))
        self.assertEqual(record.token_status, "UNKNOWN")

    def test_tui_consumes_disclosure_via_injection_only(self):
        # TUI 呈现面：构造参数 + 漏斗 getattr 接线 + _collect_inputs
        # 只读传递；TUI/投影 import 图零编译域（W3-Q 钉定的延续）
        for token in ("compile_disclosure=None",
                      "compile_metadata=",
                      'getattr(\n                composed, '
                      '"compile_disclosure"'):
            normalized = " ".join(token.split())
            self.assertIn(
                normalized,
                " ".join(_TUI_SOURCE.split()),
                f"TUI wiring missing: {normalized}")
        for filename in ("cockpit_tui.py", "cockpit_projection.py"):
            tree = ast.parse((SCRIPTS / filename).read_text(
                encoding="utf-8"))
            roots = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        roots.add(alias.name.split(".")[0])
                elif isinstance(node, ast.ImportFrom):
                    if node.module:
                        roots.add(node.module.split(".")[0])
                    else:
                        for alias in node.names:
                            roots.add(alias.name.split(".")[0])
            self.assertFalse(
                set(_COMPILE_DOMAIN_ROOTS) & roots,
                f"{filename} imports compile domain: "
                f"{sorted(set(_COMPILE_DOMAIN_ROOTS) & roots)}")
        # run_cockpit_tui 外壳同 additive 形
        parameters = inspect.signature(
            cockpit_tui.run_cockpit_tui).parameters
        self.assertIn("compile_disclosure", parameters)
        self.assertIsNone(parameters["compile_disclosure"].default)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
