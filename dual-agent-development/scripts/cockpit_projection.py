"""CU-TUI-3 (V3.2): cockpit projection — 纯只读呈现投影层。

职责（G2 hard rule）：既有事实源 → 确定性投影 → 可渲染只读视图模型。
本模块是纯函数层：零终端 UI 框架依赖、零 I/O、零子进程、零网络、
零时钟、零随机、零身份铸造、零事实构造、零执行面
（调用/提交/驱动一概不存在）、零线程。同一输入必然产生逐字节
一致的输出；本层不保存第二份执行状态——lifecycle 由四级冻结
优先级纯推导（P1 真实终态 > P2 停驻+有效 PAUSE > P2′ 停驻 >
P3 活跃事件 > P4 空闲呈现态），accepted 绝不直接投影为状态。

事实源（duck 类型只读，零冻结域 import）：
- events：执行观察事件（sequence/event_type/stage/runtime_id/
  status/reason/duration_ms 既有字段子集；OBS 行直接消费
  console_observation.format_event_line 的逐字节输出）；
- facts：控制事实账本条目（seq/fact_type/command_id/
  execution_version/payload；PAUSE_REQUESTED 等仅在裁决 accepted
  时落账——在场即 accepted 是账本侧冻结语义，非本层推断）；
- usage_records：用量记录（KNOWN 才计数；UNKNOWN/UNSUPPORTED
  呈现 —；absent 记录不产生行、绝不折算为 0）；
- session 投影面：terminal / run_state / last_outcome（真实
  RunOutcome 投影，本层零合成）；
- slots：组合真相（角色/运行时/provider 缺席诚实呈现 —）。
"""
from __future__ import annotations

import textwrap

from console_observation import format_event_line
from content_safety import contains_unsafe_content

__all__ = (
    "AgentSlotView", "ProjectionInputs", "ProjectedState",
    "build_projection", "derive_lifecycle", "display_width",
    "format_tokens", "truncate_to_width",
)

# 呈现层 lifecycle 词表（P4 的 IDLE 仅为投影层视觉态，绝不进入
# 引擎状态/事件词表）。
_TERMINAL_LIFECYCLES = ("COMPLETED", "FAILED", "ABORTED")
_START_EVENT_TYPES = ("STAGE_STARTED", "INVOCATION_STARTED")
_PAUSE_ON_FACTS = ("PAUSE_REQUESTED", "PAUSE_CONFIRMED")
_PAUSE_OFF_FACTS = ("RESUME_REQUESTED", "ABORT_REQUESTED",
                    "ABORT_CONFIRMED", "ABORT_SUPERSEDED")

_SYMBOLS = {
    "COMPLETED": "✓", "FAILED": "✗", "ABORTED": "■",
    "PAUSED": "‖", "PARKED": "‖", "RUNNING": "●", "IDLE": "○",
    "DONE": "✓", "FAILED_STEP": "✗", "ACTIVE": "●", "WAITING": "○",
}
_ASCII_SYMBOLS = {
    "✓": "[OK]", "●": "[RUN]", "○": "[PENDING]", "✗": "[FAIL]",
    "→": "->", "‖": "[:]", "■": "[STOP]",
}

# 呈现宽度：宽字符按 2 列计（CJK/全角区段的实用子集）。
_WIDE_RANGES = (
    (0x1100, 0x115F), (0x2E80, 0x303E), (0x3041, 0x33FF),
    (0x3400, 0x4DBF), (0x4E00, 0x9FFF), (0xA000, 0xA4CF),
    (0xAC00, 0xD7A3), (0xF900, 0xFAFF), (0xFE30, 0xFE4F),
    (0xFF00, 0xFF60), (0xFFE0, 0xFFE6),
)


class AgentSlotView:
    """组合真相的一个槽位投影值（纯值对象）。"""

    __slots__ = ("stage", "role", "runtime_id", "provider")

    def __init__(self, *, stage, role, runtime_id, provider=None):
        self.stage = stage
        self.role = role
        self.runtime_id = runtime_id
        self.provider = provider


class ProjectionInputs:
    """一次投影的全部输入（调用方按只读约定供应事实源）。"""

    __slots__ = ("task", "slots", "events", "facts", "usage_records",
                 "terminal", "run_state", "last_outcome", "capabilities",
                 "version", "width", "ascii_only")

    def __init__(self, *, task="", slots=(), events=(), facts=(),
                 usage_records=(), terminal=None, run_state=None,
                 last_outcome=None, capabilities=None, version="",
                 width=100, ascii_only=False):
        self.task = task
        self.slots = tuple(slots)
        self.events = tuple(events)
        self.facts = tuple(facts)
        self.usage_records = tuple(usage_records)
        self.terminal = terminal
        self.run_state = run_state
        self.last_outcome = last_outcome
        self.capabilities = capabilities
        self.version = version
        self.width = width
        self.ascii_only = ascii_only


class ProjectedState:
    """渲染就绪的只读视图模型（全部为纯字符串/字符串元组）。"""

    __slots__ = ("header_line", "task_line", "badge",
                 "collaboration_lines", "progress_line", "tokens_line",
                 "result_lines", "context_lines", "trace_obs",
                 "trace_ctrl", "trace_usage", "lifecycle", "tier")

    def __init__(self, *, header_line, task_line, badge,
                 collaboration_lines, progress_line, tokens_line,
                 result_lines, context_lines, trace_obs, trace_ctrl,
                 trace_usage, lifecycle, tier):
        self.header_line = header_line
        self.task_line = task_line
        self.badge = badge
        self.collaboration_lines = tuple(collaboration_lines)
        self.progress_line = progress_line
        self.tokens_line = tokens_line
        self.result_lines = tuple(result_lines)
        self.context_lines = tuple(context_lines)
        self.trace_obs = tuple(trace_obs)
        self.trace_ctrl = tuple(trace_ctrl)
        self.trace_usage = tuple(trace_usage)
        self.lifecycle = lifecycle
        self.tier = tier

    def __eq__(self, other):
        if not isinstance(other, ProjectedState):
            return NotImplemented
        return all(
            getattr(self, name) == getattr(other, name)
            for name in self.__slots__)

    def __repr__(self):
        return ("ProjectedState(lifecycle=%r, tier=%r)"
                % (self.lifecycle, self.tier))


# ------------------------------------------------------------ 纯工具函数


def _value_of(item):
    """枚举成员取封闭词值，普通字符串原样（零新词汇铸造）。"""
    return getattr(item, "value", item)


def _event_type_of(event):
    return _value_of(getattr(event, "event_type", ""))


def display_width(text):
    """终端呈现宽度（宽字符 2 列；零依赖实用实现）。"""
    total = 0
    for character in text:
        code = ord(character)
        wide = any(low <= code <= high for low, high in _WIDE_RANGES)
        total += 2 if wide else 1
    return total


def truncate_to_width(text, limit):
    """按呈现宽度截断，尾部以 ... 标注（绝不产生横滚）。"""
    if display_width(text) <= limit:
        return text
    budget = max(0, limit - 3)
    parts = []
    used = 0
    for character in text:
        code = ord(character)
        cost = 2 if any(low <= code <= high
                        for low, high in _WIDE_RANGES) else 1
        if used + cost > budget:
            break
        parts.append(character)
        used += cost
    return "".join(parts) + "..."


def format_tokens(total):
    """ΣKNOWN 的唯一呈现形式（零 KNOWN → —；k/M 单位一位小数）。"""
    if not total:
        return "—"
    if total < 1000:
        return str(total)
    if total < 1_000_000:
        return f"{total / 1000:.1f}k"
    return f"{total / 1_000_000:.1f}M"


def derive_lifecycle(terminal, run_state, events, facts):
    """四级冻结优先级（CU-TUI-3 授权 §五，逐字实现）。

    P1 真实终态（仅来自真实 RunOutcome 投影）> P2 停驻+有效 PAUSE
    > P2′ 停驻 > P3 活跃事件 > P4 空闲（呈现层视觉态）。"""
    if terminal is not None:
        value = _value_of(terminal)
        if value in _TERMINAL_LIFECYCLES:
            return value
    if run_state is not None:
        return "PAUSED" if _pause_intent_active(facts) else "PARKED"
    for event in events:
        if _event_type_of(event) in _START_EVENT_TYPES:
            return "RUNNING"
    return "IDLE"


def _pause_intent_active(facts):
    """最近一次有效 PAUSE accepted（账本只记 accepted 裁决）。

    顺序扫描，后者胜：PAUSE_REQUESTED/PAUSE_CONFIRMED 置位，
    RESUME/ABORT 系事实清除（沿 boundary 裁决表的取代语义，
    纯只读推导，非第二真相）。"""
    active = False
    for entry in facts:
        kind = _value_of(getattr(entry, "fact_type", ""))
        if kind in _PAUSE_ON_FACTS:
            active = True
        elif kind in _PAUSE_OFF_FACTS:
            active = False
    return active


def _known_token_total(records):
    total = 0
    for record in records:
        if _value_of(getattr(record, "usage_status", "")) == "KNOWN":
            total += (record.input_tokens or 0)
            total += (record.output_tokens or 0)
    return total


def _slot_symbol(slot_view, events):
    """槽位四态符号：事件流推导，零时钟零猜测。

    ✓ 完成（SUCCESS）· ✗ 完成（失败）· ● 进行中 · ○ 未开始。"""
    started = False
    finished = None
    for event in events:
        if (getattr(event, "stage", None) != slot_view.role
                or getattr(event, "runtime_id", None)
                != slot_view.runtime_id):
            continue
        kind = _event_type_of(event)
        if kind == "INVOCATION_STARTED":
            started = True
        elif kind == "INVOCATION_FINISHED":
            finished = _value_of(getattr(event, "status", ""))
    if finished is not None:
        return "✓" if str(finished).upper() == "SUCCESS" else "✗"
    return "●" if started else "○"


def _tier_for(width):
    if width < 80:
        return "DEGRADED"
    if width < 100:
        return "MAIN"
    if width < 140:
        return "MAIN_WIDE"
    return "FULL"


def _to_ascii(text):
    for symbol, replacement in _ASCII_SYMBOLS.items():
        text = text.replace(symbol, replacement)
    return text


def _result_lines(outcome, content_width):
    if outcome is None:
        return ()
    status = _value_of(getattr(outcome, "status", ""))
    lines = []
    if status == "PARKED":
        return ("PARKED · awaiting resume",)
    final_result = getattr(outcome, "final_result", None)
    if final_result is not None:
        output = getattr(final_result, "output", None)
        if isinstance(output, str) and output:
            if contains_unsafe_content(output):
                text = "[redacted: unsafe content]"
            else:
                text = output
            wrapped = textwrap.wrap(text, width=max(20, content_width))
            if not wrapped:
                wrapped = [""]
            lines.extend(wrapped[:3])
            if len(wrapped) > 3:
                lines.append(f"(+{len(wrapped) - 3} more lines)")
        else:
            lines.append("(no text output)")
    error = getattr(outcome, "error", None)
    if error is not None:
        lines.append(
            f"ERROR {truncate_to_width(str(error), max(10, content_width - 7))}")
    return tuple(lines)


def _context_lines(values, lifecycle):
    if values.width < 140:
        return ()
    lines = ["CURRENT",
             f"  task  {truncate_to_width(values.task, 18)}",
             f"  agents  {len(values.slots)}",
             f"  state  {lifecycle}",
             "RUNTIME"]
    for slot_view in values.slots:
        provider = slot_view.provider if slot_view.provider else "—"
        lines.append(
            f"  {slot_view.runtime_id}  provider {provider}")
    lines.append("CAPABILITIES")
    for slot_view in values.slots:
        names = None
        if values.capabilities is not None:
            names = values.capabilities.get(slot_view.runtime_id)
        shown = "、".join(names) if names else "—"
        lines.append(f"  {slot_view.runtime_id}  {shown}")
    lines.append("SESSION")
    lines.append(f"  tokens  {format_tokens(_known_token_total(values.usage_records))}")
    return tuple(
        truncate_to_width(line, 28) for line in lines)


def _collaboration_lines(values, tier):
    if tier == "DEGRADED":
        lines = []
        for slot_view in values.slots:
            symbol = _slot_symbol(slot_view, values.events)
            lines.append(f"{symbol} {slot_view.role.upper()}")
        return tuple(lines)
    role_cells = []
    status_cells = []
    widths = []
    for slot_view in values.slots:
        symbol = _slot_symbol(slot_view, values.events)
        role_cell = slot_view.role.upper()
        status_cell = f"{symbol} {slot_view.runtime_id}"
        width = max(display_width(role_cell),
                    display_width(status_cell))
        role_cells.append(role_cell)
        status_cells.append(status_cell)
        widths.append(width)
    line_one = " → ".join(
        cell + " " * (widths[index] - display_width(cell))
        for index, cell in enumerate(role_cells))
    line_two = "   ".join(
        cell + " " * (widths[index] - display_width(cell))
        for index, cell in enumerate(status_cells))
    return (line_one, line_two)


def _progress_line(values, lifecycle, total_slots):
    finished = 0
    started_stages = []
    finished_stages = set()
    for event in values.events:
        kind = _event_type_of(event)
        if kind == "STAGE_FINISHED":
            finished += 1
            finished_stages.add(getattr(event, "stage", ""))
        elif kind == "STAGE_STARTED":
            started_stages.append(getattr(event, "stage", ""))
    active = None
    for stage in reversed(started_stages):
        if stage not in finished_stages:
            active = stage
            break
    line = f"STAGE {min(finished, total_slots)}/{total_slots}"
    if active is not None:
        line += f" · {active}"
    if (lifecycle == "RUNNING"
            and _pause_intent_active(values.facts)):
        line += "  · pause pending"
    return line


def build_projection(values):
    """七个事实源 → ProjectedState（确定性纯函数）。"""
    tier = _tier_for(values.width)
    content_width = (values.width - 4 if tier == "DEGRADED"
                     else min(values.width - 4, 100))
    lifecycle = derive_lifecycle(values.terminal, values.run_state,
                                 values.events, values.facts)
    header = "dual-agent cockpit"
    if values.version:
        header += f" · v{values.version}"
    state = ProjectedState(
        header_line=header,
        task_line=("TASK "
                   + truncate_to_width(values.task,
                                       max(10, content_width - 5))),
        badge=f"{_SYMBOLS[lifecycle]} {lifecycle}",
        collaboration_lines=_collaboration_lines(values, tier),
        progress_line=_progress_line(values, lifecycle,
                                     len(values.slots)),
        tokens_line=("TOKENS · "
                     + format_tokens(
                         _known_token_total(values.usage_records))),
        result_lines=_result_lines(values.last_outcome, content_width),
        context_lines=_context_lines(values, lifecycle),
        trace_obs=tuple(format_event_line(event).rstrip("\n")
                        for event in values.events),
        trace_ctrl=tuple(_control_line(entry) for entry in values.facts),
        trace_usage=tuple(_usage_line(record)
                          for record in values.usage_records),
        lifecycle=lifecycle,
        tier=tier)
    if values.ascii_only:
        return _ascii_state(state)
    return state


def _control_line(entry):
    kind = _value_of(getattr(entry, "fact_type", ""))
    line = (f"[{entry.seq}] {kind} command={entry.command_id} "
            f"version={entry.execution_version}")
    payload = getattr(entry, "payload", None)
    if payload:
        for key in sorted(payload):
            line += f" {key}={payload[key]}"
    return line


def _usage_line(record):
    modality = _value_of(getattr(record, "usage_status", ""))
    if modality == "KNOWN":
        amounts = f"in={record.input_tokens} out={record.output_tokens}"
    else:
        amounts = "in=— out=—"
    return f"{record.runtime_id} {record.role} {modality} {amounts}"


def _ascii_state(state):
    """G12 ASCII 降级：逐符号映射，结构与层级不变。"""
    return ProjectedState(
        header_line=_to_ascii(state.header_line),
        task_line=_to_ascii(state.task_line),
        badge=_to_ascii(state.badge),
        collaboration_lines=tuple(
            _to_ascii(line) for line in state.collaboration_lines),
        progress_line=_to_ascii(state.progress_line),
        tokens_line=_to_ascii(state.tokens_line),
        result_lines=tuple(
            _to_ascii(line) for line in state.result_lines),
        context_lines=tuple(
            _to_ascii(line) for line in state.context_lines),
        trace_obs=tuple(_to_ascii(line) for line in state.trace_obs),
        trace_ctrl=tuple(_to_ascii(line) for line in state.trace_ctrl),
        trace_usage=tuple(
            _to_ascii(line) for line in state.trace_usage),
        lifecycle=state.lifecycle,
        tier=state.tier)
