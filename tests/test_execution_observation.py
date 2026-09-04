"""R7-D1: execution observation contract — offline test matrix.

锁定授权矩阵的九类证据（全部离线：零 runtime 触碰、零子进程、零网络、
零环境读取、零 REAL）：

- Contract: 7 词封闭词表全部合法、非法 event type 拒绝（含授权点名的
  ROLE_ASSIGNED / BUDGET_RESERVED / RUNTIME_SELECTED / PROGRESS /
  UI_UPDATE 私自扩展词，以及任意字符串 / 非字符串）、字段完整性
  （恰好 9 字段、顺序固定、必需/可选分明）、frozen 不可变。
- Sequence: 0 合法、正整数合法、负数拒绝、bool 拒绝、非 int 拒绝。
- Required fields: 六个字符串字段空串/空白串/None/非字符串一律拒绝。
- Duration: None（默认与显式）合法、0 合法、正数合法、负数拒绝、
  bool 拒绝、float 拒绝。
- Security: 每字段 × 每 marker 的 secret-marker 拒绝（词表直接取自
  content_safety —— 证明单一 secret policy 无第二套）、裸 credential
  形状（marker 子串抓不到的 sk-… 形）拒绝、错误信息绝不回显被拒
  材料（只点名字段与规则）、ObservationError 是 ValueError 子类、
  house 封闭 reason 词表（POLICY_* / DUAL_*）不被校验误伤。
- Determinism: 相同输入相同事件（值 / hash / repr）、to_dict 二次
  调用与键序稳定、模块 AST 零 clock / uuid / random / env 依赖、零
  async / thread 构造、import 白名单（dataclasses / enum / typing /
  content_safety）、源文本零 runtime 名（claude / codex / pi-cli）、
  零模块级可变状态。
- Sink: 可调用 sink 收到事件本体、协议表面恰好一个 on_event 方法
  （签名 (self, event) -> None）、sink 只凭契约字段即可工作（不需要
  runtime / provider / UI 信息 —— 字段集中根本不存在这些可要）。
- Serialization: to_dict 字段集合固定且有序、event_type 序列化为
  词表字符串、禁止字段不出现、输出 secret-safe、无 JSON 框架依赖。
- Architecture invariants: ExecutionEvent != CollaborationRecord
  （ledger 审计真值）!= CollaborationPacket != InvocationResult !=
  UI state —— 类型不同、字段集不同、不共享持久 schema（模块零
  ledger/packet/runtime import）；模块表面恰好四个公开契约类；
  未接线任何生产调用链（scripts/ 其余模块零引用 —— R7-D2 之前
  的接线边界）。

既有测试文件零修改；生产代码零修改（本文件只读契约并断言边界）。
"""
import ast
import dataclasses
import inspect
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from execution_observation import (
    ExecutionEvent,
    ExecutionEventType,
    ObservationError,
    ObservationSink,
)
import execution_observation as observation_module

from collaboration_packet import CollaborationPacket
from collaboration_state import CollaborationRecord
from content_safety import SECRET_MARKERS, contains_unsafe_content
from external_runtime import InvocationResult

MODULE_PATH = SCRIPTS / "execution_observation.py"

# 授权字段集：恰好 9 个，顺序即定义顺序。
AUTHORIZED_FIELDS = [
    "event_type", "sequence", "task_id", "correlation_id", "stage",
    "runtime_id", "status", "reason", "duration_ms",
]
REQUIRED_STRING_FIELDS = [
    "task_id", "correlation_id", "stage", "runtime_id", "status", "reason",
]

# 授权词表：恰好 7 词（封闭，绝不静默扩展）。
AUTHORIZED_EVENT_TYPES = [
    "DECISION", "STAGE_STARTED", "INVOCATION_STARTED",
    "INVOCATION_FINISHED", "STAGE_FINISHED", "HANDOFF", "TERMINAL",
]

# 授权明令禁止出现的字段名（timestamp / prompt / payload / provider /
# model / credentials / environment / raw exception 等的各种别称）。
FORBIDDEN_FIELDS = {
    "timestamp", "started_at", "finished_at", "created_at", "wall_clock",
    "agent_address", "execution_id", "safe_summary", "prompt", "payload",
    "packet", "packet_payload", "provider", "model", "credentials",
    "environment", "exception", "error", "output", "wire", "provenance",
    "stdout", "stderr", "command",
}

# 模块 import 白名单：契约模块只允许 dataclasses / enum / typing /
# content_safety（复用既有 secret policy）+ __future__。
ALLOWED_IMPORTS = {
    "__future__", "dataclasses", "enum", "typing", "content_safety",
}

# 零依赖面：clock / 随机 / UUID / 环境 / 并发 / 网络 / 子进程 / 序列化框架。
FORBIDDEN_IDENTIFIERS = {
    "time", "datetime", "uuid", "random", "environ", "getenv",
    "monotonic", "perf_counter", "clock",
    "subprocess", "socket", "http", "urllib", "requests",
    "asyncio", "threading", "queue", "multiprocessing",
    "json", "pickle", "yaml",
}

# 生命周期之外的私自扩展词（授权明令不得加入，构造期必须拒绝）。
UNAUTHORIZED_EXTENSIONS = [
    "ROLE_ASSIGNED", "BUDGET_RESERVED", "RUNTIME_SELECTED",
    "PROGRESS", "UI_UPDATE", "INVOCATION_RUNNING",
]


def make_event(**overrides):
    """授权形态的合法事件基线；overrides 覆盖单个字段做探测。"""
    base = dict(
        event_type=ExecutionEventType.DECISION,
        sequence=0,
        task_id="task-001",
        correlation_id="corr-7",
        stage="architect",
        runtime_id="rt-x",
        status="SUCCESS",
        reason="ROLE_ASSIGNMENT=POLICY_COUNT_UNSATISFIED",
    )
    base.update(overrides)
    return ExecutionEvent(**base)


def module_source():
    return MODULE_PATH.read_text(encoding="utf-8")


def module_tree():
    return ast.parse(module_source())


def imported_modules(tree):
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.add(node.module.split(".")[0])
    return names


def identifier_names(tree):
    """AST 内出现的全部标识符（Name.id + Attribute.attr）。"""
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
    return names


# ---------------------------------------------------------------------------
# Contract: 封闭词表 + 字段完整性 + frozen
# ---------------------------------------------------------------------------


class ContractTests(unittest.TestCase):

    def test_vocabulary_is_exactly_seven_authorized_values(self):
        self.assertEqual(len(ExecutionEventType), 7)
        self.assertEqual(
            [member.value for member in ExecutionEventType],
            AUTHORIZED_EVENT_TYPES)

    def test_every_authorized_event_type_constructs_legally(self):
        for index, name in enumerate(AUTHORIZED_EVENT_TYPES):
            with self.subTest(event_type=name):
                event = make_event(
                    event_type=ExecutionEventType[name], sequence=index)
                self.assertIs(event.event_type, ExecutionEventType[name])
                self.assertEqual(event.event_type.value, name)

    def test_string_member_value_converts_to_enum(self):
        event = make_event(event_type="DECISION")
        self.assertIs(event.event_type, ExecutionEventType.DECISION)

    def test_illegal_event_type_rejected_closed(self):
        # 任意字符串（含大小写变体、前后空白）与非字符串一律拒绝，
        # 无绕过路径。
        illegal = ["", "decision", "Decision", "NOT_A_TYPE", "DECISION ",
                   None, 3, 3.5, True, ["DECISION"], {"t": 1}, object()]
        for value in illegal:
            with self.subTest(value=repr(value)):
                with self.assertRaises(ObservationError):
                    make_event(event_type=value)

    def test_unauthorized_vocabulary_extensions_rejected(self):
        for word in UNAUTHORIZED_EXTENSIONS:
            with self.subTest(word=word):
                with self.assertRaises(ObservationError):
                    make_event(event_type=word)

    def test_field_set_is_exactly_authorized_nine_in_order(self):
        self.assertEqual(
            [field.name for field in dataclasses.fields(ExecutionEvent)],
            AUTHORIZED_FIELDS)

    def test_no_forbidden_field_exists(self):
        names = {field.name for field in dataclasses.fields(ExecutionEvent)}
        self.assertEqual(names & FORBIDDEN_FIELDS, set())

    def test_only_duration_ms_carries_a_default(self):
        defaults = {
            field.name for field in dataclasses.fields(ExecutionEvent)
            if field.default is not dataclasses.MISSING
            or field.default_factory is not dataclasses.MISSING
        }
        self.assertEqual(defaults, {"duration_ms"})

    def test_missing_required_field_rejected(self):
        with self.assertRaises(TypeError):
            ExecutionEvent(sequence=0, task_id="T", correlation_id="C",
                           stage="s", runtime_id="r", status="S", reason="R")

    def test_frozen_immutable(self):
        event = make_event()
        with self.assertRaises(dataclasses.FrozenInstanceError):
            event.status = "FAILED"

    def test_equal_inputs_produce_equal_and_hashable_events(self):
        first = make_event()
        second = make_event(event_type="DECISION")
        self.assertEqual(first, second)
        self.assertEqual(hash(first), hash(second))

    def test_realistic_house_event_is_legal(self):
        # house 封闭 reason 词表（POLICY_* / 角色名 / SUCCESS）不被
        # secret 校验误伤 —— 既有词形必须全部继续合法。
        event = ExecutionEvent(
            event_type=ExecutionEventType.HANDOFF, sequence=3,
            task_id="task-001", correlation_id="corr-7",
            stage="coder", runtime_id="rt-y", status="SUCCESS",
            reason="ROLE_ASSIGNMENT=POLICY_SPREAD", duration_ms=12)
        self.assertEqual(event.stage, "coder")
        self.assertEqual(event.duration_ms, 12)
        self.assertFalse(contains_unsafe_content(event.reason))


# ---------------------------------------------------------------------------
# Sequence: 调用方提供的非负整数序号
# ---------------------------------------------------------------------------


class SequenceTests(unittest.TestCase):

    def test_zero_is_legal(self):
        self.assertEqual(make_event(sequence=0).sequence, 0)

    def test_positive_integer_is_legal(self):
        self.assertEqual(make_event(sequence=17).sequence, 17)

    def test_negative_rejected(self):
        with self.assertRaises(ObservationError):
            make_event(sequence=-1)

    def test_bool_rejected(self):
        for value in (True, False):
            with self.subTest(value=value):
                with self.assertRaises(ObservationError):
                    make_event(sequence=value)

    def test_non_integer_rejected(self):
        for value in (1.0, "1", None):
            with self.subTest(value=repr(value)):
                with self.assertRaises(ObservationError):
                    make_event(sequence=value)


# ---------------------------------------------------------------------------
# Required fields: 六个字符串字段非空
# ---------------------------------------------------------------------------


class RequiredFieldTests(unittest.TestCase):

    def test_empty_and_whitespace_rejected_for_every_field(self):
        for field in REQUIRED_STRING_FIELDS:
            for value in ("", "   "):
                with self.subTest(field=field, value=repr(value)):
                    with self.assertRaises(ObservationError):
                        make_event(**{field: value})

    def test_none_rejected_for_every_field(self):
        for field in REQUIRED_STRING_FIELDS:
            with self.subTest(field=field):
                with self.assertRaises(ObservationError):
                    make_event(**{field: None})

    def test_non_string_rejected_for_every_field(self):
        for field in REQUIRED_STRING_FIELDS:
            with self.subTest(field=field):
                with self.assertRaises(ObservationError):
                    make_event(**{field: 5})


# ---------------------------------------------------------------------------
# Duration: 可选相对时长（绝不是时间戳）
# ---------------------------------------------------------------------------


class DurationTests(unittest.TestCase):

    def test_default_is_none_and_legal(self):
        self.assertIsNone(make_event().duration_ms)

    def test_explicit_none_zero_positive_legal(self):
        for value in (None, 0, 1, 640):
            with self.subTest(value=value):
                event = make_event(duration_ms=value)
                self.assertEqual(event.duration_ms, value)

    def test_negative_rejected(self):
        with self.assertRaises(ObservationError):
            make_event(duration_ms=-1)

    def test_bool_and_float_rejected(self):
        for value in (True, False, 1.5):
            with self.subTest(value=repr(value)):
                with self.assertRaises(ObservationError):
                    make_event(duration_ms=value)


# ---------------------------------------------------------------------------
# Security: 单一 secret policy 复用 + 错误信息零泄漏
# ---------------------------------------------------------------------------


class SecurityTests(unittest.TestCase):

    def test_secret_marker_rejected_for_every_field_every_marker(self):
        # 6 字段 × 全部 marker 的矩阵；marker 直接取自 content_safety，
        # 证明拒绝词表就是既有单一 secret policy 的词表。
        for field in REQUIRED_STRING_FIELDS:
            for marker in SECRET_MARKERS:
                with self.subTest(field=field, marker=marker):
                    with self.assertRaises(ObservationError):
                        make_event(**{field: f"x-{marker}-probe"})

    def test_bare_credential_shape_rejected_for_every_field(self):
        # marker 子串抓不到的裸 credential 形状（contains_unsafe_content
        # 的形状规则面）同样拒绝 —— 复用既有 helper，不建第二套。
        shape = "sk-D1SHAPEPROBE99"
        for field in REQUIRED_STRING_FIELDS:
            with self.subTest(field=field):
                with self.assertRaises(ObservationError):
                    make_event(**{field: shape})

    def test_single_secret_policy_source(self):
        # 模块不自建 marker 词表：源内无第二套定义，直接引用既有常量。
        source = module_source()
        self.assertNotIn("_SECRET_MARKERS", source)
        self.assertNotIn('("token"', source)
        self.assertIn("content_safety", source)

    def test_error_messages_never_leak_rejected_material(self):
        secret = "api_key=sk-D1LEAKPROBE99"
        for field in REQUIRED_STRING_FIELDS:
            with self.subTest(field=field):
                with self.assertRaises(ObservationError) as ctx:
                    make_event(**{field: secret})
                message = str(ctx.exception)
                self.assertNotIn(secret, message)
                self.assertNotIn("sk-D1LEAKPROBE99", message)
                self.assertIn(field, message)

    def test_bare_shape_error_does_not_leak_material(self):
        with self.assertRaises(ObservationError) as ctx:
            make_event(reason="sk-D1LEAKPROBE99")
        message = str(ctx.exception)
        self.assertNotIn("sk-D1LEAKPROBE99", message)
        self.assertIn("reason", message)

    def test_event_type_error_never_echoes_value(self):
        with self.assertRaises(ObservationError) as ctx:
            make_event(event_type="PROGRESS")
        self.assertNotIn("PROGRESS", str(ctx.exception))

    def test_error_is_value_error_subclass(self):
        self.assertTrue(issubclass(ObservationError, ValueError))

    def test_rejection_surface_secret_safe(self):
        try:
            make_event(reason="api_key=sk-D1SURFACE99")
        except ObservationError as err:
            # 拒绝面本身（错误文本）也不含 credential 形状。
            self.assertFalse(contains_unsafe_content(str(err)))


# ---------------------------------------------------------------------------
# Determinism: 无 clock / 无随机 / 无环境 / 相同输入相同结果
# ---------------------------------------------------------------------------


class DeterminismTests(unittest.TestCase):

    def test_same_input_same_event_and_hash(self):
        kwargs = dict(event_type="STAGE_STARTED", sequence=4,
                      task_id="task-001", correlation_id="corr-7",
                      stage="coder", runtime_id="rt-y", status="RUNNING",
                      reason="STAGE_ENTRY", duration_ms=7)
        first = ExecutionEvent(**kwargs)
        second = ExecutionEvent(**kwargs)
        self.assertEqual(first, second)
        self.assertEqual(hash(first), hash(second))
        self.assertEqual(repr(first), repr(second))

    def test_to_dict_deterministic_and_order_stable(self):
        event = make_event(duration_ms=5)
        first = event.to_dict()
        second = event.to_dict()
        self.assertEqual(first, second)
        self.assertEqual(list(first.keys()), list(second.keys()))

    def test_no_clock_random_uuid_or_env_dependency(self):
        names = identifier_names(module_tree())
        self.assertEqual(names & FORBIDDEN_IDENTIFIERS, set())

    def test_imports_limited_to_allowed_set(self):
        self.assertEqual(imported_modules(module_tree()), ALLOWED_IMPORTS)

    def test_no_async_or_threading_constructs(self):
        for node in ast.walk(module_tree()):
            self.assertNotIsInstance(
                node, (ast.AsyncFunctionDef, ast.AsyncFor, ast.AsyncWith,
                       ast.Await))

    def test_no_runtime_names_in_source(self):
        source = module_source().lower()
        for name in ("claude", "codex", "pi-cli"):
            self.assertNotIn(name, source)

    def test_no_module_level_assignments_or_state(self):
        # 顶层只有 docstring / import / class / 私有 helper —— 零全局
        # 可变状态（事件无计数器、无缓存）。
        for node in module_tree().body:
            self.assertIsInstance(
                node, (ast.Expr, ast.Import, ast.ImportFrom,
                       ast.ClassDef, ast.FunctionDef))

    def test_no_timestamp_field_anywhere(self):
        names = {field.name for field in dataclasses.fields(ExecutionEvent)}
        for token in ("timestamp", "started_at", "finished_at", "created_at"):
            self.assertNotIn(token, names)
        self.assertNotIn("timestamp", make_event().to_dict())


# ---------------------------------------------------------------------------
# Sink: 最小同步协议
# ---------------------------------------------------------------------------


class _RecordingSink:
    """最小结构化 sink：只凭契约字段工作，不 import 任何 runtime/UI。"""

    def __init__(self):
        self.events = []
        self.tokens = []

    def on_event(self, event):
        self.events.append(event)
        # 消费端自己的投影（如 Agent A/B/C 中性编号）只由契约字段派生。
        self.tokens.append(
            f"{event.event_type.value}#{event.sequence}@{event.stage}")


class SinkContractTests(unittest.TestCase):

    def test_callable_sink_receives_event(self):
        sink = _RecordingSink()
        event = make_event(event_type="INVOCATION_STARTED", sequence=2,
                           stage="coder", status="RUNNING")
        result = sink.on_event(event)
        self.assertIsNone(result)
        self.assertIs(sink.events[0], event)
        self.assertEqual(sink.tokens, ["INVOCATION_STARTED#2@coder"])

    def test_protocol_surface_is_exactly_one_method(self):
        public = {name for name in vars(ObservationSink)
                  if not name.startswith("_")}
        self.assertEqual(public, {"on_event"})
        signature = inspect.signature(ObservationSink.on_event)
        self.assertEqual(list(signature.parameters), ["self", "event"])
        self.assertEqual(
            ObservationSink.on_event.__annotations__.get("return"), "None")

    def test_sink_needs_nothing_beyond_contract(self):
        # 事件字段集就是 sink 的全部输入：无 provider / model /
        # credentials / payload / prompt 可要（字段根本不存在）。
        names = {field.name for field in dataclasses.fields(ExecutionEvent)}
        for absent in ("provider", "model", "credentials", "payload",
                       "prompt", "timestamp"):
            self.assertNotIn(absent, names)
        sink = _RecordingSink()
        sink.on_event(make_event())
        self.assertEqual(len(sink.tokens), 1)


# ---------------------------------------------------------------------------
# Serialization: 最小 deterministic dict 投影
# ---------------------------------------------------------------------------


class SerializationTests(unittest.TestCase):

    def test_to_dict_field_set_fixed_and_ordered(self):
        projection = make_event(duration_ms=9).to_dict()
        self.assertEqual(list(projection.keys()), AUTHORIZED_FIELDS)

    def test_event_type_serialized_as_vocabulary_string(self):
        projection = make_event(event_type="HANDOFF").to_dict()
        self.assertEqual(projection["event_type"], "HANDOFF")
        self.assertIsInstance(projection["event_type"], str)

    def test_to_dict_contains_no_forbidden_fields(self):
        projection = make_event().to_dict()
        self.assertEqual(set(projection) & FORBIDDEN_FIELDS, set())

    def test_to_dict_matches_field_values(self):
        event = make_event(event_type="TERMINAL", sequence=6,
                           status="FAILED",
                           reason="DUAL_NO_CAPABLE_AGENT", duration_ms=0)
        self.assertEqual(event.to_dict(), {
            "event_type": "TERMINAL", "sequence": 6,
            "task_id": "task-001", "correlation_id": "corr-7",
            "stage": "architect", "runtime_id": "rt-x",
            "status": "FAILED", "reason": "DUAL_NO_CAPABLE_AGENT",
            "duration_ms": 0,
        })

    def test_to_dict_output_secret_safe(self):
        event = make_event(reason="ROLE_ASSIGNMENT=POLICY_COUNT_UNSATISFIED")
        projection = event.to_dict()
        surface = " ".join(str(value) for value in projection.values())
        self.assertFalse(contains_unsafe_content(surface))

    def test_no_json_framework_dependency(self):
        # 序列化是纯 dict 投影；不引入 JSON / 序列化框架依赖。
        self.assertNotIn("json", imported_modules(module_tree()))


# ---------------------------------------------------------------------------
# Architecture invariants: != ledger / packet / result / UI state
# ---------------------------------------------------------------------------


class ArchitectureInvariantTests(unittest.TestCase):

    def _field_names(self, cls):
        return {field.name for field in dataclasses.fields(cls)}

    def test_event_is_not_ledger_record(self):
        # ledger（CollaborationRecord）是审计真值；事件只是通知旁路。
        self.assertFalse(issubclass(ExecutionEvent, CollaborationRecord))
        self.assertNotEqual(self._field_names(ExecutionEvent),
                            self._field_names(CollaborationRecord))
        # 不共享持久 schema：契约模块不 import ledger。
        self.assertNotIn("collaboration_state",
                         imported_modules(module_tree()))

    def test_event_is_not_collaboration_packet(self):
        self.assertFalse(issubclass(ExecutionEvent, CollaborationPacket))
        self.assertNotEqual(self._field_names(ExecutionEvent),
                            self._field_names(CollaborationPacket))
        self.assertNotIn("collaboration_packet",
                         imported_modules(module_tree()))

    def test_event_is_not_invocation_result(self):
        self.assertFalse(issubclass(ExecutionEvent, InvocationResult))
        self.assertNotEqual(self._field_names(ExecutionEvent),
                            self._field_names(InvocationResult))
        self.assertNotIn("external_runtime",
                         imported_modules(module_tree()))

    def test_event_is_not_ui_state(self):
        # 无 UI 呈现字段、无 UI 框架 import —— 呈现映射是消费端投影。
        names = self._field_names(ExecutionEvent)
        ui_tokens = {"ui", "widget", "render", "display", "label", "color",
                     "icon", "view", "screen", "canvas", "column", "row",
                     "width", "height"}
        self.assertEqual(names & ui_tokens, set())
        self.assertEqual(imported_modules(module_tree()) & {
            "tkinter", "curses", "rich", "textual", "flask", "fastapi",
            "streamlit"}, set())

    def test_module_surface_is_exactly_four_contract_classes(self):
        classes = {
            name for name, obj in inspect.getmembers(
                observation_module, inspect.isclass)
            if obj.__module__ == "execution_observation"}
        self.assertEqual(
            classes, {"ObservationError", "ExecutionEventType",
                      "ExecutionEvent", "ObservationSink"})
        functions = {
            name for name, obj in inspect.getmembers(
                observation_module, inspect.isfunction)
            if obj.__module__ == "execution_observation"}
        self.assertTrue(all(name.startswith("_") for name in functions))

    def test_wiring_limited_to_authorized_d2_seams(self):
        # R7-D2 之后：观察契约只被授权接线缝引用（orchestrator/facade/
        # session/verification —— 各自独占的生命周期事实的唯一权威发射
        # 者）。其余 scripts 模块零引用（零 import、零提及），防止接线
        # 蠕变到 adapter/pool/health/ledger 等禁改组件。
        # R7-D4 扩展：第一个授权消费者（console projection 本体 + CLI
        # 组合点）加入授权集 —— 消费端只引用契约，绝不反向影响执行。
        authorized = {
            "execution_observation.py",  # 契约本体
            "collaboration_orchestrator.py",   # DECISION
            "collaboration_session.py",        # STAGE/INVOCATION/HANDOFF(dual)
            "verification_collaboration.py",   # STAGE/INVOCATION/HANDOFF(verify)
            "production_facade.py",            # TERMINAL + execution 通道
            "console_observation.py",          # R7-D4 消费者本体
            "cli.py",                          # R7-D4 CLI 组合点
        }
        for path in sorted(SCRIPTS.glob("*.py")):
            if path.name in authorized:
                continue
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("execution_observation", text, path.name)
            self.assertNotIn("ObservationSink", text, path.name)


if __name__ == "__main__":
    unittest.main()
