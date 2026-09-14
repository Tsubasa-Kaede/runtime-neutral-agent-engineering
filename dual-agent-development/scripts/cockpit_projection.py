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
    "agent_detail", "control_receipt_line", "event_detail_line",
    "revision_status_lines", "DEFAULT_ROLE_TEMPLATES",
    "funnel_preview_lines", "funnel_blocked_line", "funnel_enter_lines",
    "funnel_changed_lines", "funnel_error_lines", "funnel_first_screen",
)

# 呈现层 lifecycle 词表（P4 的 IDLE 仅为投影层视觉态，绝不进入
# 引擎状态/事件词表）。
_TERMINAL_LIFECYCLES = ("COMPLETED", "FAILED", "ABORTED")
_START_EVENT_TYPES = ("STAGE_STARTED", "INVOCATION_STARTED")
_PAUSE_ON_FACTS = ("PAUSE_REQUESTED", "PAUSE_CONFIRMED")
_PAUSE_OFF_FACTS = ("RESUME_REQUESTED", "ABORT_REQUESTED",
                    "ABORT_CONFIRMED", "ABORT_SUPERSEDED")
# CU-TUI-4：修订状态行只消费这两个既有账本词值（读取面，非铸造）。
_REVISION_FACT_TYPES = ("REVISE_REQUESTED", "REVISION_APPLIED")

# CU-TUI-5：默认协作角色模板（呈现层纯数据冻结映射）。组合绑定
# 真相唯一源在 cockpit_entry 的默认组合函数——本层只提供
# "N 个 VERIFIED runtime → 默认角色序列"的查表值，零绑定逻辑、
# 零 identity 计算、零 IO。
_TEMPLATES = {
    2: ("architect", "coder"),
    3: ("architect", "coder", "reviewer"),
    4: ("architect", "coder", "tester", "reviewer"),
}
DEFAULT_ROLE_TEMPLATES = _TEMPLATES

_SYMBOLS = {
    "COMPLETED": "✓", "FAILED": "✗", "ABORTED": "■",
    "PAUSED": "‖", "PARKED": "‖", "RUNNING": "●", "IDLE": "○",
    "DONE": "✓", "FAILED_STEP": "✗", "ACTIVE": "●", "WAITING": "○",
}
_ASCII_SYMBOLS = {
    "✓": "[OK]", "●": "[RUN]", "○": "[PENDING]", "✗": "[FAIL]",
    "→": "->", "←": "<-", "‖": "[:]", "■": "[STOP]",
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


# ------------------------------------------- CU-TUI-4：detail/status 投影


def control_receipt_line(result):
    """一次控制提交的同步回执单行（ControlResult duck 只读投影）。

    status / reason 逐字（封闭词值，零新 ControlStatus）；回执是
    瞬态 UI 呈现，不是控制历史——历史只来自账本事实。"""
    line = (f"receipt {getattr(result, 'command_id', '')}: "
            f"{_value_of(getattr(result, 'status', ''))}")
    version = getattr(result, "execution_version", None)
    if version is not None:
        line += f" v{version}"
    reason = getattr(result, "reason", None)
    if reason is not None:
        line += f" · {_value_of(reason)}"
    return line


def revision_status_lines(facts, pending_count):
    """修订状态行（四档呈现律，CU-TUI-4 §10）。

    pending 只来自注入读数（None → —，绝不推断）；accepted/applied
    只来自账本事实；SUBMISSION 附引擎契约说明（静态文字，非状态
    声称）；honored 永不呈现——无可观测事实面。"""
    pending = "—" if pending_count is None else str(pending_count)
    lines = [f"pending {pending}"]
    for entry in facts:
        kind = _value_of(getattr(entry, "fact_type", ""))
        if kind not in _REVISION_FACT_TYPES:
            continue
        seq = getattr(entry, "seq", "")
        if kind == "REVISION_APPLIED":
            lines.append(f"[{seq}] REVISION_APPLIED")
            continue
        payload = getattr(entry, "payload", None) or {}
        target = payload.get("target", "—")
        line = f"[{seq}] REVISE_REQUESTED target={target}"
        if str(target) == "SUBMISSION":
            line += " · applies at next fresh segment"
        lines.append(line)
    return tuple(lines)


def event_detail_line(event):
    """选中事件详情行（既有字段子集逐字；缺席字段零行）。"""
    lines = [f"seq {getattr(event, 'sequence', '')}",
             f"type {_event_type_of(event)}"]
    for label, field in (("stage", "stage"), ("runtime", "runtime_id"),
                         ("status", "status"), ("reason", "reason")):
        value = getattr(event, field, None)
        if value is None or value == "":
            continue
        lines.append(f"{label} {_value_of(value)}")
    duration = getattr(event, "duration_ms", None)
    if duration is not None:
        lines.append(f"duration {duration}ms")
    return tuple(lines)


# ------------------- CU-TUI-5 (V3.2): first-run funnel pure renderers
#
# 漏斗呈现词（COMPOSITION_CHANGED 横幅/首屏六要素）全部是 TUI 私有
# 呈现层词汇，绝不进入 engine observation/event vocabulary。组合值
# 对象（roles/bindings/blocked_reason/blocked_hint）以 duck 类型只读
# 进入；binding 对本层只有 role/runtime_id/provider 三字段呈现面——
# canonical 身份四元组是不透明比较载荷，本层零计算零消费。

_FUNNEL_PREVIEW_HEADER = "Collaboration plan (default)"
_FUNNEL_INSTRUCTION = "Describe the collaboration task"
_FUNNEL_KEYS_HINT = "Enter start · q quit"
_FUNNEL_CHANGED_BANNER = "collaboration plan changed:"
_FUNNEL_BLANK_TASK_HINT = "describe the task first"
_FUNNEL_REDACTED = "[redacted: unsafe content]"


def funnel_preview_lines(composition, *, ascii_only=False):
    """默认组合披露行（§十三预览块）：标题 + 逐槽位一行
    "role ← runtime · provider"（角色列对齐至最长角色名）。

    blocked 组合零绑定 → 预览块整块缺席（诚实原因行独立渲染，
    见 funnel_blocked_line）。确定性纯函数。"""
    if getattr(composition, "blocked_reason", None) is not None:
        return ()
    bindings = tuple(getattr(composition, "bindings", ()) or ())
    if not bindings:
        return ()
    width = max(len(bound.role) for bound in bindings)
    lines = [_FUNNEL_PREVIEW_HEADER]
    lines.extend(
        f"  {bound.role:<{width}}  ← {bound.runtime_id} · "
        f"{bound.provider_id}"
        for bound in bindings)
    if ascii_only:
        lines = [_to_ascii(line) for line in lines]
    return tuple(lines)


def funnel_blocked_line(composition):
    """BLOCKED 时恰一行诚实原因（原文）；hint 属 Enter 反馈行
    （funnel_enter_lines），不混入首屏原因行。"""
    return getattr(composition, "blocked_reason", None)


def funnel_enter_lines(composition, task_text):
    """Enter 键的状态行反馈（§十二顺序）：空白 → no-op 提示；
    预览 BLOCKED → 原因 + hint；就绪 → 零行（Start 判定与执行
    不在本层）。"""
    if not str(task_text).strip():
        return (_FUNNEL_BLANK_TASK_HINT,)
    blocked_reason = getattr(composition, "blocked_reason", None)
    if blocked_reason is None:
        return ()
    lines = [blocked_reason]
    hint = getattr(composition, "blocked_hint", None)
    if hint is not None:
        lines.append(hint)
    return tuple(lines)


def funnel_changed_lines(reasons, *, ascii_only=False):
    """COMPOSITION_CHANGED 横幅（TUI 私有组合结果词，非 engine
    event）+ 逐条诚实原因（entry 组合面已按类生成，本层零推断）。"""
    lines = [_FUNNEL_CHANGED_BANNER]
    lines.extend(f"  {reason}" for reason in reasons)
    if ascii_only:
        lines = [_to_ascii(line) for line in lines]
    return tuple(lines)


def funnel_error_lines(error):
    """CompositionError → 漏斗红行：reason/detail 原词汇一行 +
    hint 原文次行（缺席诚实省略）。不 exit——交互面内消化。"""
    lines = [f"{getattr(error, 'reason', '')}: "
             f"{getattr(error, 'detail', '')}"]
    hint = getattr(error, "hint", None)
    if hint is not None:
        lines.append(hint)
    return tuple(lines)


def funnel_first_screen(version_text, composition, task_buffer, *,
                        width=100, ascii_only=False):
    """首屏六要素组装（NOT_STARTED/COMPOSING 共用，§十三）：
    header（版本真源由调用方注入，本层零版本读取）/ 唯一指令行 /
    输入行（内容安全门 + 宽度截断）/ 预览块 / 恰两键提示 / BLOCKED
    原因行。零 task_id/execution_id/UUID/内部对象/debug metadata。"""
    buffer_text = str(task_buffer)
    if contains_unsafe_content(buffer_text):
        buffer_text = _FUNNEL_REDACTED
    header = (f"dual-agent cockpit · {version_text}" if version_text
              else "dual-agent cockpit")
    lines = [header,
             _FUNNEL_INSTRUCTION,
             truncate_to_width(f"> {buffer_text}", width)]
    lines.extend(funnel_preview_lines(composition))
    blocked_reason = getattr(composition, "blocked_reason", None)
    if blocked_reason is not None:
        lines.append(blocked_reason)
    lines.append(_FUNNEL_KEYS_HINT)
    if ascii_only:
        lines = [_to_ascii(line) for line in lines]
    return tuple(lines)


def _slot_status(slot_view, events):
    """槽位状态字：完成态逐字（事件 status 值），在途/等待为呈现词。"""
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
        symbol = "✓" if str(finished).upper() == "SUCCESS" else "✗"
        return symbol, str(finished)
    if started:
        return "●", "in-flight"
    return "○", "waiting"


def _slot_duration_text(slot_view, events):
    """时长只来自真实 INVOCATION_FINISHED.duration_ms（最后一次）。"""
    duration = None
    for event in events:
        if (getattr(event, "stage", None) != slot_view.role
                or getattr(event, "runtime_id", None)
                != slot_view.runtime_id):
            continue
        if _event_type_of(event) == "INVOCATION_FINISHED":
            value = getattr(event, "duration_ms", None)
            if value is not None:
                duration = value
    return "—" if duration is None else f"{duration}ms"


def _slot_handoff_text(slot_view, events):
    """交接只来自真实 HANDOFF 事件（stage=产出角色，runtime=接收方）。"""
    handoffs = []
    for event in events:
        if (_event_type_of(event) != "HANDOFF"
                or getattr(event, "stage", None) != slot_view.role):
            continue
        handoffs.append(
            f"→ {getattr(event, 'runtime_id', '')}"
            f" ({_value_of(getattr(event, 'status', ''))})")
    return "; ".join(handoffs) if handoffs else "—"


def _slot_usage_text(slot_view, usage_records):
    """用量行：最后一条匹配记录；非 KNOWN 一律 —（零折算）。"""
    shown = None
    for record in usage_records:
        if (getattr(record, "role", None) != slot_view.role
                or getattr(record, "runtime_id", None)
                != slot_view.runtime_id):
            continue
        shown = record
    if shown is None or _value_of(
            getattr(shown, "usage_status", "")) != "KNOWN":
        return "—"
    return f"in={shown.input_tokens} out={shown.output_tokens}"


def _slot_result_text(slot_view, last_outcome):
    """结果行：只来自既有 transcript 的 step status metadata
    （终态后才有；绝不伪装完整 agent output）。"""
    if last_outcome is None:
        return "—"
    for record in getattr(last_outcome, "transcript", None) or ():
        if getattr(record, "slot_id", None) == slot_view.stage:
            return _value_of(getattr(record, "status", ""))
    return "—"


def agent_detail(slots, events, usage_records, last_outcome, *,
                 expanded=False, width=100, ascii_only=False):
    """Agent Detail 行集（CU-TUI-4 §8）。

    Role ≠ Runtime（角色为节标题、runtime 单列一行）；Status/Duration/
    Handoff/Usage/Result 只来自既有事实源，缺席诚实呈现 —；零 agent
    registry、零身份铸造、零推断。expanded=True 时不按宽度截断
    （长结果展开）。"""
    lines = []
    for slot_view in slots:
        symbol, status_text = _slot_status(slot_view, events)
        provider = (f" · provider {slot_view.provider}"
                    if slot_view.provider else "")
        lines.extend((
            slot_view.role.upper(),
            f"  {'status':<8}  {symbol} {status_text}",
            f"  {'runtime':<8}  {slot_view.runtime_id}{provider}",
            f"  {'duration':<8}  {_slot_duration_text(slot_view, events)}",
            f"  {'handoff':<8}  {_slot_handoff_text(slot_view, events)}",
            f"  {'usage':<8}  {_slot_usage_text(slot_view, usage_records)}",
            f"  {'result':<8}  {_slot_result_text(slot_view, last_outcome)}",
        ))
    if not expanded:
        lines = [truncate_to_width(line, width) for line in lines]
    if ascii_only:
        lines = [_to_ascii(line) for line in lines]
    return tuple(lines)


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
