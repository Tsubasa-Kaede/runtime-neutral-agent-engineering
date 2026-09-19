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
from bisect import bisect_right

from console_observation import format_event_line
from content_safety import contains_unsafe_content

__all__ = (
    "AgentSlotView", "ProjectionInputs", "ProjectedState",
    "build_projection", "derive_lifecycle", "display_width",
    "format_tokens", "truncate_to_width", "ui_label",
    "agent_detail", "pipeline_lines", "activity_tail_lines",
    "agent_detail_window", "connection_observed",
    "apply_budget_narrowing",
    "control_receipt_line", "event_detail_line", "trace_status_line",
    "revision_status_lines", "worker_failure_lines",
    "trace_observation_lines", "trace_control_lines", "trace_usage_lines",
    "DEFAULT_ROLE_TEMPLATES",
    "funnel_preview_lines", "funnel_blocked_line", "funnel_enter_lines",
    "funnel_changed_lines", "funnel_error_lines", "funnel_first_screen",
    "funnel_input_line", "funnel_keys_hint",
    "COMPOSITION_ROLES", "compose_pool_lines",
    "compose_participant_lines", "compose_screen_lines",
    "compose_keys_hint",
    "run_divider_line", "runs_summary_lines",
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

# CU-COCKPIT-1：Role 呈现环（COMPOSE r 键循环面）。与 composition_
# core.ROLE_VOCABULARY 同源同式派生（同一张 DEFAULT_ROLE_TEMPLATES
# 冻结表）——本层不可反向 import core（core 顶层已 import 本层取
# 模板表），故以同式派生保持单一真源，零第二套 Role 词表。
COMPOSITION_ROLES = tuple(sorted(
    {role for template in DEFAULT_ROLE_TEMPLATES.values()
     for role in template}))

_SYMBOLS = {
    "COMPLETED": "✓", "FAILED": "✗", "ABORTED": "⊘",
    "PAUSED": "❚❚", "PARKED": "▫", "RUNNING": "●", "IDLE": "○",
}
_ASCII_SYMBOLS = {
    "✓": "[OK]", "●": "[RUN]", "○": "[PENDING]", "✗": "[FAIL]",
    "→": "->", "←": "<-", "‖": "[:]", "■": "[STOP]", "⊘": "[SKIP]",
    "❚❚": "||", "▫": "-", "◐": "~", "◉": "*", "»": ">", "▸": ">",
    # R1 管线符号（新增字符，零现存输出碰撞；· 的既有行为保持不变）
    "─": "-", "┄": ".", "↓": "|", "┆": ":", "↳": "\\",
    "▲": "^", "▶": ">", "▼": "v",
    # CU-COCKPIT-1：COMPOSE 键提示的 ↑（零现存输出碰撞）
    "↑": "^",
}

# Phase V §九：header 状态符号表（ASCII 降级不失可读）。
_LIFECYCLE_GLYPHS = dict(_SYMBOLS)
_LIFECYCLE_GLYPHS_ASCII = {
    "COMPLETED": "+", "FAILED": "x", "ABORTED": "!",
    "PAUSED": "||", "PARKED": "-", "RUNNING": "*", "IDLE": ".",
}

# Phase V §十一/十二：agent 面板状态词 → 符号（词来自真实事件流 +
# 既有 lifecycle 投影叠加；pulse 是纯呈现参数，只翻转 RUNNING 符号）。
_AGENT_GLYPHS = {
    "RUNNING": "●", "WAITING": "◐", "DONE": "✓", "FAILED": "✗",
    "PAUSED": "❚❚", "PARKED": "▫", "ABORTED": "⊘", "NOT_STARTED": "○",
}
_AGENT_GLYPHS_ASCII = {
    "RUNNING": "*", "WAITING": "~", "DONE": "+", "FAILED": "x",
    "PAUSED": "||", "PARKED": "-", "ABORTED": "!", "NOT_STARTED": ".",
}

# 回执状态前缀（封闭三态的视觉标记；词值仍逐字呈现）。
_RECEIPT_GLYPHS = {"ACCEPTED": "✓", "REJECTED": "✗", "NO_OP": "⊘"}

# R2 双语呈现词表（闭集纯数据；零 i18n 框架/零外部文件/零动态加载/
# 零注册表）。键 = 规范 EN 呈现词（en 列恒等于键本身）；两列恰
# en/zh。翻译只发生在渲染点——canonical EN 词恒为词表键与符号表键
# （_AGENT_GLYPHS["RUNNING"] 永不变）；事实面（task 原文/身份/
# runtime/provider/事件词与 status/reason/sequence/duration/usage
# 数值/回执与账本词/trace_obs 逐字节）永不经过本表。带 {n} 的条目
# 为模板，调用点 format。
_LOCALES = ("en", "zh")

_LABELS = {
    # lifecycle / agent 状态呈现词
    "RUNNING": ("RUNNING", "运行中"),
    "WAITING": ("WAITING", "等待中"),
    "DONE": ("DONE", "已完成"),
    "FAILED": ("FAILED", "失败"),
    "PAUSED": ("PAUSED", "已暂停"),
    "PARKED": ("PARKED", "已停驻"),
    "ABORTED": ("ABORTED", "已中止"),
    "NOT_STARTED": ("NOT_STARTED", "未开始"),
    "IDLE": ("IDLE", "空闲"),
    "COMPLETED": ("COMPLETED", "已完成"),
    # 分区 / 标签词
    "TASK": ("TASK", "任务"),
    "STAGE": ("STAGE", "阶段"),
    "prompt": ("prompt", "提示词"),
    "handoff": ("handoff", "交接"),
    "in": ("in", "入"),
    "out": ("out", "出"),
    "activity": ("activity", "活动"),
    "result": ("result", "结果"),
    "state": ("state", "状态"),
    # 活动动词短语（stage/status/duration 原文拼装）
    "stage started": ("stage started", "阶段已开始"),
    "started": ("started", "已开始"),
    "finished": ("finished", "已完成"),
    "stage finished": ("stage finished", "阶段已完成"),
    # 短句
    "No activity yet": ("No activity yet", "暂无活动"),
    "awaiting resume": ("awaiting resume", "等待继续"),
    "(no text output)": ("(no text output)", "(无文本输出)"),
    "(+{n} more lines)": ("(+{n} more lines)", "(+{n} 行未显示)"),
    "(+{n} more · T)": ("(+{n} more · T)", "(+{n} 条 · T)"),
    "pause pending": ("pause pending", "暂停待生效"),
    "pinned · {n} new events · g/end resumes tail": (
        "pinned · {n} new events · g/end resumes tail",
        "已钉住 · {n} 条新事件 · g/end 恢复跟随"),
    "[Q] quit re-raises the original error": (
        "[Q] quit re-raises the original error",
        "[Q] 退出将重新抛出原始错误"),
    "pending {n}": ("pending {n}", "待处理 {n}"),
    " · applies at next fresh segment": (
        " · applies at next fresh segment", " · 于下个全新执行段生效"),
    # dock 动词（TUI 消费；键字母恒 EN）
    "Pause": ("Pause", "暂停"),
    "Resume": ("Resume", "继续"),
    "Abort": ("Abort", "终止"),
    "Trace": ("Trace", "追踪"),
    "Quit": ("Quit", "退出"),
    "Context": ("Context", "上下文"),
    "abort? · y confirm · n/esc cancel": (
        "abort? · y confirm · n/esc cancel",
        "中止？· y 确认 · n/esc 取消"),
    # UX2-R1：常驻 composer hint（提交力学——键字母恒 EN 既有
    # mandates）与 Log echo 行词条（闭集纪律：Log 行全部经词表）
    "enter send · ctrl+j newline": ("enter send · ctrl+j newline",
                                    "回车发送 · ctrl+j 换行"),
    "you · steer": ("you · steer", "你 · 转向"),
    # UX2-R2：E 召回动词（R1 移除后随召回语义回归）、修订 echo、
    # slash 反馈词（unknown/无提交/未实现——闭集纪律：Log 行全经词表）
    "Edit": ("Edit", "编辑"),
    "you · revise": ("you · revise", "你 · 修订"),
    "unknown command · /help lists commands": (
        "unknown command · /help lists commands",
        "未知命令 · /help 查看命令表"),
    "no prior submission · type to steer": (
        "no prior submission · type to steer",
        "暂无可修订提交 · 直接键入即转向"),
    "/target <agent> is not implemented": (
        "/target <agent> is not implemented",
        "/target <agent> 尚未实现"),
    # CU-COCKPIT-1：COMPOSE 选择屏闭集词条（键字母恒 EN 既有惯例；
    # role/runtime 为 domain 词绝不入表）
    "compose collaboration": ("compose collaboration", "组合协作"),
    "runtimes": ("runtimes", "运行时"),
    "participants": ("participants", "参与者"),
    "collaboration plan": ("collaboration plan", "协作计划"),
    "no VERIFIED runtimes": ("no VERIFIED runtimes", "无已验证运行时"),
    "selected {n}/4": ("selected {n}/4", "已选择 {n}/4"),
    "roles (r): {roles}": ("roles (r): {roles}", "角色 (r): {roles}"),
    "press enter to preview": ("press enter to preview", "按 enter 预览"),
    "plan ready — enter to start": (
        "plan ready — enter to start", "计划已就绪 — 按 enter 启动"),
    "need ≥2 VERIFIED runtimes — qualify first": (
        "need ≥2 VERIFIED runtimes — qualify first",
        "至少需要 2 个 VERIFIED runtime — 请先 qualify"),
    "2-4 runtimes": ("2-4 runtimes", "需选择 2-4 个运行时"),
    "describe the task first": (
        "describe the task first", "先描述任务（esc 返回漏斗输入）"),
    # P2 W6/W7：重入失效披露 + q 两段守卫横幅（runtime id 为
    # domain 词经 {ids} 注入，绝不翻译）
    "removed from selection: {ids}": (
        "removed from selection: {ids}", "已从选择移除：{ids}"),
    "q again to quit · esc back": (
        "q again to quit · esc back", "再按 q 退出 · esc 返回"),
    "↑↓ move · space select · ←→ member · r role · "
    "enter preview/start · esc back · l lang · q quit": (
        "↑↓ move · space select · ←→ member · r role · "
        "enter preview/start · esc back · l lang · q quit",
        "↑↓ 移动 · space 勾选 · ←→ 成员 · r 角色 · "
        "enter 预览/启动 · esc 返回 · l 语言 · q 退出"),
    # 2.8-A 会话轮次面（Log 分节线/轮间横幅//runs 摘要——闭集
    # 纪律：Log 行全部经词表；{n}/{k}/{status}/{roles}/{task} 为
    # 事实面 domain 词 format 注入，绝不翻译）
    "── run {n} · {k} agents · {roles} ──": (
        "── run {n} · {k} agents · {roles} ──",
        "── 第 {n} 轮 · {k} 个成员 · {roles} ──"),
    "next collaboration · run {n} {status}": (
        "next collaboration · run {n} {status}",
        "下一轮协作 · 第 {n} 轮 {status}"),
    "runs": ("runs", "轮次"),
    "run {n} · {status} · {task}": (
        "run {n} · {status} · {task}",
        "第 {n} 轮 · {status} · {task}"),
    "no collaborations yet": ("no collaborations yet", "尚无协作轮次"),
    # 2.8-A 会话 slash 反馈词（轮间域限定提示与 /new 兑现回执——
    # 闭集纪律：Log 行全部经词表；命令名/键字母恒 EN）
    "only between runs · /again reloads the last task": (
        "only between runs · /again reloads the last task",
        "仅轮间可用 · /again 载入上轮任务"),
    "only between runs · /compose opens selection": (
        "only between runs · /compose opens selection",
        "仅轮间可用 · /compose 打开选择屏"),
    "only between runs · /new resets the session display": (
        "only between runs · /new resets the session display",
        "仅轮间可用 · /new 重置会话呈现史"),
    "session display cleared · run counter reset": (
        "session display cleared · run counter reset",
        "会话呈现史已清空 · 轮次计数已重置"),
}


def ui_label(key, locale="en"):
    """闭集词表唯一解析函数：en 列 = 键本身；非 zh 一律回退 en 列
    （未知 locale 安全回退、既有行为逐字节保持）；缺键原样返回
    （绝不 KeyError、绝不铸造新词）。"""
    columns = _LABELS.get(key)
    if columns is None:
        return key
    if locale == "zh":
        return columns[1]
    return columns[0]

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
                 "version", "width", "ascii_only",
                 "pulse", "reveal_seqs", "result_reveal",
                 "selected_index", "expanded_stage", "locale",
                 "detail_max_lines", "result_max_lines", "include_trace",
                 "groups", "member_ids", "scroll_mode")

    def __init__(self, *, task="", slots=(), events=(), facts=(),
                 usage_records=(), terminal=None, run_state=None,
                 last_outcome=None, capabilities=None, version="",
                 width=100, ascii_only=False, pulse=False, reveal_seqs=(),
                 result_reveal=False, selected_index=0,
                 expanded_stage=None, locale="en",
                 detail_max_lines=None, result_max_lines=None,
                 include_trace=True, groups=(), member_ids=(),
                 scroll_mode=False):
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
        # Phase V 动画呈现参数：只改变渲染输出，绝不参与 lifecycle
        # 推导、绝不写回事实源（Animation ≠ Execution State）。
        self.pulse = pulse
        self.reveal_seqs = tuple(reveal_seqs)
        self.result_reveal = result_reveal
        # R1 纯呈现参数：选中 cell / 展开 agent（marker、▲ 指针、
        # Detail 窗）；expanded 绑定稳定 slot 身份（stage），二者互不
        # 联动、绝不写回事实源。
        self.selected_index = selected_index
        self.expanded_stage = expanded_stage
        # R2 纯呈现参数：界面语言（闭集 en/zh，默认 en；additive 字段
        # ——既有构造点零迁移）。只改渲染词，绝不参与任何推导、绝不
        # 写回事实源；缺省 en 下全部输出与 R1 逐字节一致。
        self.locale = locale
        # CU-TUI-INPUT A4 纯呈现参数：高度预算收窄上限（None=既有
        # 行为；int=TUI 按终端剩余高度供应）。只影响有界窗行数，
        # 绝不参与任何推导、绝不写回事实源；缺省 None 下全部输出
        # 与既有投影逐字节一致。
        self.detail_max_lines = detail_max_lines
        self.result_max_lines = result_max_lines
        # CU-PERF-1 W2 纯呈现参数：trace 三字段是否随投影计算
        # （缺省 True = 既有构造点零迁移、输出逐字节一致；False 时
        # trace_obs/trace_ctrl/trace_usage 为 ()——trace 消费方改经
        # trace_*_lines 三函数直取，主屏不显示 trace 故可免算）。
        self.include_trace = include_trace
        # 2.8-B 纯呈现输入（additive——缺省下全部输出与既有投影
        # 逐字节一致）：groups/member_ids = run-local 组合快照只读
        # 供应（真相链 declaration → resolved → run-local snapshot
        # → 本输入；TUI/投影只读消费、绝不回写）；scroll_mode =
        # H 显式横滚呈现参数（True = 管线行集豁免宽度截断——
        # viewport 偏移由 TUI 容器持有，本层零状态零回写）。
        self.groups = tuple(groups)
        self.member_ids = tuple(member_ids)
        self.scroll_mode = bool(scroll_mode)


class ProjectedState:
    """渲染就绪的只读视图模型（全部为纯字符串/字符串元组）。"""

    __slots__ = ("header_line", "task_line", "badge",
                 "collaboration_lines", "activity_lines", "detail_lines",
                 "progress_line", "tokens_line",
                 "result_lines", "context_lines", "trace_obs",
                 "trace_ctrl", "trace_usage", "lifecycle", "tier")

    def __init__(self, *, header_line, task_line, badge,
                 collaboration_lines, activity_lines, progress_line,
                 tokens_line, result_lines, context_lines, trace_obs,
                 trace_ctrl, trace_usage, lifecycle, tier,
                 detail_lines=()):
        self.header_line = header_line
        self.task_line = task_line
        self.badge = badge
        self.collaboration_lines = tuple(collaboration_lines)
        self.activity_lines = tuple(activity_lines)
        self.detail_lines = tuple(detail_lines)
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


# WIDE_RANGES 的二分查找形态（CU-PERF-1 W6）：区间端点排序副本 +
# 成员判断 O(log n)。与 _WIDE_RANGES 同一冻结数据源派生，非第二
# 词表——构造处断言两者覆盖一致。
_WIDE_STARTS = tuple(low for low, _high in _WIDE_RANGES)
_WIDE_ENDS = tuple(high for _low, high in _WIDE_RANGES)


def _char_width(code):
    """单码点呈现宽度（宽字符 2 列；bisect 区间查找）。"""
    index = bisect_right(_WIDE_STARTS, code) - 1
    if index >= 0 and code <= _WIDE_ENDS[index]:
        return 2
    return 1


def display_width(text):
    """终端呈现宽度（宽字符 2 列；CU-PERF-1 W6 快路径）。

    isascii 全串快路径（宽字符区与 ASCII 零交集）+ 逐字符
    bisect 区间查找；输出与逐区间 any() 线性扫描逐字节一致
    （边界端点等价由测试锁死）。"""
    if text.isascii():
        return len(text)
    total = 0
    for character in text:
        total += _char_width(ord(character))
    return total


def truncate_to_width(text, limit):
    """按呈现宽度截断，尾部以 ... 标注（绝不产生横滚）。

    CU-PERF-1 W6：与 display_width 共享 _char_width 单一实现
    （既有内联区间循环与 display_width 重复实现的历史形态消除）。"""
    if text.isascii() and len(text) <= limit:
        return text
    if display_width(text) <= limit:
        return text
    budget = max(0, limit - 3)
    parts = []
    used = 0
    for character in text:
        cost = _char_width(ord(character))
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


def _result_lines(outcome, content_width, *, reveal=False,
                  ascii_only=False, locale="en", max_lines=None):
    """结果区行集（Phase V §十五/十六）：终态带状态头行（✓/✗ + 原
    词——RunOutcome.status 是事实直显，永不翻译）；PARKED 不伪造结果
    （单行停驻说明——lifecycle 呈现词可译）；reveal 为瞬态揭示前缀
    （纯呈现参数，绝不参与状态推导）。CU-TUI-INPUT A4：max_lines 为
    高度预算收窄上限（None=既有行为；int 时超限行数诚实折入
    (+N more lines) 溢出标记——头行（status 事实）永不收窄）。"""
    if outcome is None:
        return ()
    status = _value_of(getattr(outcome, "status", ""))
    if status == "PARKED":
        return (f"{ui_label('PARKED', locale)} · "
                f"{ui_label('awaiting resume', locale)}",)
    glyphs = (_LIFECYCLE_GLYPHS_ASCII if ascii_only
              else _LIFECYCLE_GLYPHS)
    header = f"{glyphs.get(status, '·')} {status}"
    if reveal:
        header = f"{'> ' if ascii_only else '» '}{header}"
    lines = [header]
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
                lines.append(ui_label("(+{n} more lines)",
                                      locale).format(n=len(wrapped) - 3))
        else:
            lines.append(ui_label("(no text output)", locale))
    error = getattr(outcome, "error", None)
    if error is not None:
        lines.append(
            f"ERROR {truncate_to_width(str(error), max(10, content_width - 7))}")
    if max_lines is not None:
        cap = max(2, int(max_lines))
        if len(lines) > cap:
            dropped = len(lines) - (cap - 1)
            lines = lines[:cap - 1]
            lines.append(ui_label("(+{n} more lines)",
                                  locale).format(n=dropped))
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


def _slot_state_word(slot_view, events, lifecycle):
    """槽位状态词（Phase V §十二封闭八态）——只从真实事件流推导：

    INVOCATION_FINISHED（SUCCESS→DONE/否则 FAILED，终局事实不被
    lifecycle 叠加盖写）> 在途（PAUSED/PARKED/ABORTED 会话叠加）>
    STAGE_STARTED 未调用（WAITING）> 未开始（NOT_STARTED）。
    零时钟、零静默时长推断。"""
    started = False
    stage_started = False
    finished = None
    for event in events:
        if (getattr(event, "stage", None) != slot_view.role
                or getattr(event, "runtime_id", None)
                != slot_view.runtime_id):
            continue
        kind = _event_type_of(event)
        if kind == "STAGE_STARTED":
            stage_started = True
        elif kind == "INVOCATION_STARTED":
            started = True
        elif kind == "INVOCATION_FINISHED":
            finished = _value_of(getattr(event, "status", ""))
    if finished is not None:
        return "DONE" if str(finished).upper() == "SUCCESS" else "FAILED"
    if started:
        if lifecycle == "PAUSED":
            return "PAUSED"
        if lifecycle == "PARKED":
            return "PARKED"
        if lifecycle == "ABORTED":
            return "ABORTED"
        return "RUNNING"
    if stage_started:
        return "WAITING"
    return "NOT_STARTED"


def _pad_cell(text, width):
    """呈现宽度对齐填充（宽字符安全）。"""
    return text + " " * max(0, width - display_width(text))


def _chain_groups(slots):
    """组列表抽象的唯一今日生产者：当前组合事实恒为单条顺序链
    （声明组合序即 plan 序，真实事实）→ 恰一个协作组。

    多组只能来自未来的真实组合事实源；本层与调用方永不按宽度或
    事件猜测分组。"""
    return (tuple(slots),)


def connection_observed(producer, consumer, events):
    """连接真值谓词（R1 §九）：──→ 当且仅当存在真实 HANDOFF 事件且
    stage=产出角色 ∧ runtime_id=接收方 runtime（与 cockpit_entry
    request_builder 的发射语义逐字对齐）；相邻性绝不蕴含交接。"""
    for event in events:
        if (_event_type_of(event) != "HANDOFF"
                or getattr(event, "stage", None) != producer.role
                or getattr(event, "runtime_id", None)
                != consumer.runtime_id):
            continue
        return True
    return False


def _pipeline_cells(slots, events, *, lifecycle, pulse, ascii_only,
                    locale="en"):
    """每槽位 cell 投影（头/runtime/状态三件的原料）——状态词与符号
    全部来自 _slot_state_word 真实推导（Phase V 语义复用）；pulse 只
    翻转 RUNNING 符号（● ↔ ◉，纯呈现）。R2：状态词在渲染点经闭集
    词表（规范 EN 词恒为符号表键，glyph 查找永不经翻译）。"""
    cells = []
    for slot_view in slots:
        word = _slot_state_word(slot_view, events, lifecycle)
        if ascii_only:
            glyph = _AGENT_GLYPHS_ASCII[word]
        elif word == "RUNNING" and pulse:
            glyph = "◉"
        else:
            glyph = _AGENT_GLYPHS[word]
        cells.append({
            "slot": slot_view,
            "word": word,
            "head": f"{glyph} {slot_view.role.upper()}",
            "runtime": slot_view.runtime_id,
            "state": f"{glyph} {ui_label(word, locale)}",
        })
    return cells


def _pipeline_marker(index, selected, expanded_stage, slots):
    """marker 列取值：expanded（含 selected∧expanded）→ ▼；selected
    未 expanded → ▶；未选中 → None（预留两空格列，布局稳定）。"""
    if expanded_stage is not None and slots[index].stage == expanded_stage:
        return "▼"
    if index == selected:
        return "▶"
    return None


def derive_slot_chains(slots, member_ids, group_specs):
    """2.8-B 纯派生：声明组 specs + run-local member_ids 快照 →
    槽位链列表（隐式主链（未分组成员，声明序）先行；声明组按声明
    序、组内按成员声明位序）。位置对齐律：member_ids 与 slots 同序
    同长（成员声明序 = steps 序 = plan 序——entry 装配事实）。只读
    零回写；无法对齐（空 specs / 空 member_ids / 长度不匹配 / 引用
    未知成员）= 诚实退化为单链——绝不按宽度或事件猜测分组
    （_chain_groups 纪律保持）。"""
    specs = tuple(group_specs or ())
    ids = tuple(member_ids or ())
    if not specs or not ids or len(ids) != len(slots):
        return (tuple(slots),), (None,)
    position = {member_id: index for index, member_id in enumerate(ids)}
    used = set()
    declared = []
    for spec in specs:
        group_id = getattr(spec, "group_id", None)
        indexes = []
        for member_id in getattr(spec, "member_ids", ()):
            slot_index = position.get(member_id)
            if (slot_index is not None and slot_index not in used
                    and slot_index not in indexes):
                indexes.append(slot_index)
        used.update(indexes)
        declared.append((group_id, tuple(sorted(indexes))))
    chains = []
    labels = []
    implicit = tuple(index for index in range(len(slots))
                     if index not in used)
    if implicit:
        chains.append(tuple(slots[index] for index in implicit))
        labels.append(None)
    for group_id, indexes in declared:
        if not indexes:
            continue
        chains.append(tuple(slots[index] for index in indexes))
        labels.append(group_id)
    if len(chains) == 1 and labels[0] is None:
        return (tuple(slots),), (None,)
    return tuple(chains), tuple(labels)


def pipeline_lines(slots, events, *, lifecycle="RUNNING", pulse=False,
                   tier="MAIN", width=100, selected_index=0,
                   expanded_stage=None, groups=None, group_labels=(),
                   scroll_mode=False, ascii_only=False, locale="en"):
    """协作管线行集（R1 布局宪法 + 2.8-B 组横排律：横向=同一协作
    组内的链相邻关系，纵向=不同协作组的显式退让态）。

    cell 三行 "marker+符号 ROLE / runtime / 符号 状态词"；同行相邻
    cell 间连接符 ──→=观察到的真实 HANDOFF、┄┄→=计划相邻（§九真值
    契约）；超列换行 ↳ 前缀 = 同组续行（绝非第二组）；选中 cell 带
    ▶/▼ marker 与 ▲ 指针。cols=1（含 DEGRADED）退化为纵向链：块间
    ↓/┆ 同真值律。逐行按宽度截断（不溢出铁律；scroll_mode=True =
    H 显式横滚豁免——行集不截断，viewport 偏移由调用方容器持有）。
    R2：locale 只换状态呈现词——tier/容量/换行/续行/marker/指针列/
    连接符/分组与语言完全正交。

    2.8-B 布局律（ERRATA-3 纯宽度算法）：单链恒走既有渲染路径
    （groups=()/未分组 与既有输出逐字节一致）；多链经带宽算术——
    band(cols) = Σ 链宽 + 链间连接符列，≤ width → 组间横排；超出
    则 cols 自协商值递减重算（组内 wrap 收窄，只改变换行绝不丢
    成员）；cols=1 仍超宽 → 组间显式纵叠（组间空行分界、组内 wrap
    照旧）。布局判定绝不依组数分支——任何组数一律走同一宽度
    比较。组标签形态随 tier（宽度派生）：FULL/MAIN_WIDE/DEGRADED
    组头行 [id]；MAIN 行内标签折叠 "id·ROLE"（标签宽度计入
    cell_width——诚实宽度算术的一部分）。组 id = 声明事实面绝不
    翻译。链间连接符 ──→/┄┄→ 锚定前链末 cell 的 head 行，真值
    谓词与链内同源（connection_observed——跨组同样成立）。"""
    if not slots:
        return ()
    groups = _chain_groups(slots) if groups is None else tuple(groups)
    labels = (tuple(group_labels) if group_labels
              else (None,) * len(groups))
    cells = _pipeline_cells(slots, events, lifecycle=lifecycle,
                            pulse=pulse, ascii_only=ascii_only,
                            locale=locale)
    # 声明索引表：链内成员 → cells/marker 的全局索引（成员声明序，
    # 与链序解耦——隐式主链先行会重排链序，flat 索引绝不可按链序
    # 累计位移）。
    decl_index = {id(slot_view): index
                  for index, slot_view in enumerate(slots)}
    # MAIN 行内组标签：声明组首成员 head 折叠（组 id = 事实面）
    if tier == "MAIN":
        for chain_index in range(len(groups)):
            label = labels[chain_index]
            chain = groups[chain_index]
            if label is None or not chain:
                continue
            cell = cells[decl_index[id(chain[0])]]
            glyph = cell["head"].split(" ", 1)[0]
            cell["head"] = (
                f"{glyph} {label}·{cell['slot'].role.upper()}")
    selected = max(0, min(selected_index, len(cells) - 1))
    tier_caps = {"DEGRADED": 1, "MAIN": 2, "MAIN_WIDE": 3, "FULL": 4}
    cap = tier_caps.get(tier, 2)
    gap = 5  # " ──→ "
    cell_width = max(
        max(display_width(cell["head"]) for cell in cells),
        max(display_width(cell["runtime"]) for cell in cells),
        max(display_width(cell["state"]) for cell in cells))
    cols = 1
    while (cols + 1 <= cap
           and (cols + 1) * (cell_width + 2) + cols * gap <= width):
        cols += 1
    member_indexes = [[decl_index[id(member)] for member in group]
                      for group in groups]
    if len(groups) > 1:
        # 纯宽度布局律（ERRATA-3）：band 自协商 cols 递减搜索，
        # 命中即组间横排；cols=1 仍超宽 = 组间显式纵叠（落入下方
        # 既有分界渲染，cols=协商值——组内 wrap 照旧）。
        band_cols = None
        for candidate in range(cols, 0, -1):
            band = (len(groups) - 1) * gap
            for chain in groups:
                placed = min(candidate, len(chain))
                band += placed * (cell_width + 2) + (placed - 1) * gap
            if band <= width:
                band_cols = candidate
                break
        if band_cols is not None:
            return _finish_lines(
                _compose_band(groups, events, cells, cols=band_cols,
                              member_indexes=member_indexes,
                              cell_width=cell_width, gap=gap,
                              selected=selected,
                              expanded_stage=expanded_stage,
                              slots=slots, tier=tier, labels=labels),
                width=width, ascii_only=ascii_only,
                scroll_mode=scroll_mode)
    lines = []
    for group_index, group in enumerate(groups):
        if group_index:
            lines.append("")         # 组间空行 = 纵向分界
        block, _anchor = _chain_block_lines(
            group, events, cells, member_indexes[group_index],
            selected=selected, expanded_stage=expanded_stage,
            slots=slots, cols=cols, cell_width=cell_width, gap=gap,
            label_line=(f"[{labels[group_index]}]"
                        if labels[group_index] is not None
                        and tier != "MAIN" else None))
        lines.extend(block)
    return _finish_lines(lines, width=width, ascii_only=ascii_only,
                         scroll_mode=scroll_mode)


def _compose_band(groups, events, cells, *, cols, member_indexes,
                  cell_width, gap, selected, expanded_stage, slots,
                  tier, labels):
    """组间横排合成（2.8-B ERRATA-3）：块顶对齐逐行拼接；中间块按
    块宽右补齐、尾块不补、合成行 rstrip（多链合成面专用——单链恒
    走既有路径不经此处）。链间连接符列锚定前链末 cell 的 head 行
    （──→=真实跨组 HANDOFF、┄┄→=计划相邻——connection_observed
    真值谓词，绝不伪造连接）。组头行统一占据各自块的首行（无组头
    的块补空行对齐——标签恒在内容上方）。"""
    label_of = [None if (labels[index] is None or tier == "MAIN")
                else f"[{labels[index]}]"
                for index in range(len(groups))]
    pad_label = any(label is not None for label in label_of)
    blocks = []
    for group_index, group in enumerate(groups):
        block, anchor = _chain_block_lines(
            group, events, cells, member_indexes[group_index],
            selected=selected, expanded_stage=expanded_stage,
            slots=slots, cols=cols, cell_width=cell_width, gap=gap,
            label_line=label_of[group_index], pad_label=pad_label)
        blocks.append((block, anchor))
    block_widths = [
        max((display_width(line) for line in block), default=0)
        for block, _anchor in blocks]
    links = []
    for index in range(len(groups) - 1):
        producer = groups[index][-1] if groups[index] else None
        consumer = groups[index + 1][0] if groups[index + 1] else None
        links.append("──→" if (
            producer is not None and consumer is not None
            and connection_observed(producer, consumer, events))
            else "┄┄→")
    row_count = max(len(block) for block, _anchor in blocks)
    lines = []
    for row in range(row_count):
        parts = []
        for index, (block, anchor) in enumerate(blocks):
            text = block[row] if row < len(block) else ""
            part = _pad_cell(text, block_widths[index])
            if index + 1 < len(blocks):
                part += (f" {links[index]} " if anchor == row
                         else " " * gap)
            parts.append(part)
        lines.append("".join(parts).rstrip())
    return lines


def _finish_lines(lines, *, width, ascii_only, scroll_mode):
    """行集出口：ascii 投影恒应用；宽度截断铁律在 scroll_mode=True
    （H 显式横滚——viewport 由调用方容器持有）时豁免。"""
    if ascii_only:
        lines = [_to_ascii(line) for line in lines]
    if scroll_mode:
        return tuple(lines)
    return tuple(truncate_to_width(line, width) for line in lines)


def _chain_block_lines(members, events, cells, member_indexes, *,
                       selected, expanded_stage, slots, cols,
                       cell_width, gap, label_line, pad_label=False):
    """单链块渲染（R1 既有算法原样抽取——行序/续行/marker/指针逐字
    保持；label_line 非空时块首加组头行）。member_indexes = 成员 →
    cells/marker 的全局声明索引（与链序解耦）。返回 (行列表,
    anchor)：anchor = 末 cell 所在 head 行的行号（2.8-B 链间连接符
    锚点；空链 anchor=None）。pad_label=True 且本块无组头行时块首
    补空行（横排合成下标签统一在内容上方）。"""
    lines = []
    if label_line is not None:
        lines.append(label_line)
    elif pad_label:
        lines.append("")
    anchor = None
    if cols == 1:
        for member_index in range(len(members)):
            if member_index:
                observed = connection_observed(
                    members[member_index - 1],
                    members[member_index], events)
                lines.append("  " + ("↓" if observed else "┆"))
            anchor = len(lines)
            _append_vertical_block(
                lines, cells[member_indexes[member_index]],
                flat=member_indexes[member_index], selected=selected,
                expanded_stage=expanded_stage, slots=slots,
                continued=member_index > 0)
        return lines, anchor
    rows = [members[start:start + cols]
            for start in range(0, len(members), cols)]
    for row_index, row in enumerate(rows):
        row_start = row_index * cols
        row_prefix = "↳ " if row_index else ""
        heads, runtimes, states = [], [], []
        pointer_column = None
        for position in range(len(row)):
            flat = member_indexes[row_start + position]
            marker = _pipeline_marker(
                flat, selected, expanded_stage, slots)
            prefix = f"{marker} " if marker else "  "
            heads.append(prefix
                         + _pad_cell(cells[flat]["head"], cell_width))
            runtimes.append(
                "  " + _pad_cell(cells[flat]["runtime"], cell_width))
            states.append(
                "  " + _pad_cell(cells[flat]["state"], cell_width))
            if flat == selected:
                pointer_column = position * (cell_width + 2 + gap)
            if position + 1 < len(row):
                observed = connection_observed(
                    members[row_start + position],
                    members[row_start + position + 1], events)
                link = "──→" if observed else "┄┄→"
                heads.append(f" {link} ")
                runtimes.append(" " + " " * 3 + " ")
                states.append(" " + " " * 3 + " ")
        anchor = len(lines)
        lines.append(row_prefix + "".join(heads))
        lines.append(row_prefix + "".join(runtimes))
        lines.append(row_prefix + "".join(states))
        if pointer_column is not None:
            lines.append(row_prefix + " " * pointer_column + "▲")
    return lines, anchor


def _append_vertical_block(lines, cell, *, flat, selected,
                           expanded_stage, slots, continued):
    """cols=1 纵向链的单槽块：↳ 续行前缀 + cell 三行 + 选中 ▲ 指针。"""
    prefix = "↳ " if continued else ""
    marker = _pipeline_marker(flat, selected, expanded_stage, slots)
    marker_prefix = f"{marker} " if marker else "  "
    lines.append(prefix + marker_prefix + cell["head"])
    lines.append(prefix + "  " + cell["runtime"])
    lines.append(prefix + "  " + cell["state"])
    if flat == selected:
        lines.append(prefix + "▲")


def _activity_line(event, locale="en"):
    """单事件活动短行——既有字段子集（无时间戳是冻结事实：以 sequence
    锚定，绝不铸造时刻）。R2：动词短语经闭集词表；stage/status/
    duration 原文拼装。"""
    kind = _event_type_of(event)
    stage = getattr(event, "stage", "") or ""
    seq = getattr(event, "sequence", "")
    if kind == "STAGE_STARTED":
        text = f"{stage} {ui_label('stage started', locale)}"
    elif kind == "INVOCATION_STARTED":
        text = f"{stage} {ui_label('started', locale)}"
    elif kind == "INVOCATION_FINISHED":
        text = (f"{stage} {ui_label('finished', locale)} "
                f"{_value_of(getattr(event, 'status', ''))}")
        duration = getattr(event, "duration_ms", None)
        if duration is not None:
            text += f" ({duration}ms)"
    elif kind == "STAGE_FINISHED":
        text = f"{stage} {ui_label('stage finished', locale)}"
    elif kind == "HANDOFF":
        text = f"{stage} → {getattr(event, 'runtime_id', '')}"
    else:
        text = f"{kind} {stage}".rstrip()
    return f"[{seq}] {text}"


def activity_tail_lines(events, *, limit=6, reveal_seqs=(),
                        ascii_only=False, width=100, locale="en"):
    """活动尾窗（Phase V §十三/十四）：EventIndex 同源只读尾 K 条——
    零第二套事件、零伪造；空态诚实一行；reveal_seqs 为新事件揭示
    标记（纯呈现，调用方瞬态供给）。"""
    if not events:
        return (ui_label("No activity yet", locale),)
    tail = tuple(events[-limit:]) if limit and limit > 0 else tuple(events)
    reveal = set(reveal_seqs)
    lines = []
    for event in tail:
        line = _activity_line(event, locale)
        if getattr(event, "sequence", None) in reveal:
            line = f"▸ {line}"
        lines.append(line)
    if ascii_only:
        lines = [_to_ascii(line) for line in lines]
    return tuple(truncate_to_width(line, width) for line in lines)


def _header_line(values, lifecycle):
    """header：identity 左置 + 会话状态右对齐（宽度感知）。"""
    identity = "dual-agent cockpit"
    if values.version:
        identity += f" · v{values.version}"
    glyphs = (_LIFECYCLE_GLYPHS_ASCII if values.ascii_only
              else _LIFECYCLE_GLYPHS)
    state_text = (f"{glyphs.get(lifecycle, '·')} "
                  f"{ui_label(lifecycle, values.locale)}")
    gap = (values.width - display_width(identity)
           - display_width(state_text))
    if gap >= 1:
        return identity + " " * gap + state_text
    identity = truncate_to_width(
        identity, max(10, values.width - display_width(state_text) - 2))
    return f"{identity}  {state_text}"


def trace_status_line(following, new_count, *, ascii_only=False,
                      locale="en"):
    """Trace 钉住状态行（Phase V §二十）：跟随中为空行；钉住时呈现
    新事件计数与恢复键（follow/unseen 均为呈现态，非事实）。R2：
    chrome 短句可译；g/end 键位字面与事件计数原样。"""
    if following:
        return ""
    line = ui_label(
        "pinned · {n} new events · g/end resumes tail",
        locale).format(n=new_count)
    if ascii_only:
        line = _to_ascii(line.replace("·", "-"))
    return line


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
    line = (f"{ui_label('STAGE', values.locale)} "
            f"{min(finished, total_slots)}/{total_slots}")
    if active is not None:
        line += f" · {active}"
    if (lifecycle == "RUNNING"
            and _pause_intent_active(values.facts)):
        line += f"  · {ui_label('pause pending', values.locale)}"
    return line


def build_projection(values):
    """七个事实源 → ProjectedState（确定性纯函数；动画呈现参数只改
    渲染字段，绝不参与 lifecycle 推导）。"""
    tier = _tier_for(values.width)
    content_width = (values.width - 4 if tier == "DEGRADED"
                     else min(values.width - 4, 100))
    lifecycle = derive_lifecycle(values.terminal, values.run_state,
                                 values.events, values.facts)
    glyphs = (_LIFECYCLE_GLYPHS_ASCII if values.ascii_only
              else _LIFECYCLE_GLYPHS)
    # 2.8-B 组消费：run-local 快照（groups+member_ids）→ 纯派生槽位
    # 链（隐式主链先行+声明组按声明序）→ pipeline_lines 多链布局
    # 律；无声明组（specs 空/对齐失败）= groups=None 既有单链路径，
    # 输出与既有投影逐字节一致。
    chains, chain_labels = derive_slot_chains(
        values.slots, values.member_ids, values.groups)
    grouped = any(label is not None for label in chain_labels)
    state = ProjectedState(
        header_line=_header_line(values, lifecycle),
        task_line=(ui_label("TASK", values.locale) + " "
                   + truncate_to_width(values.task,
                                       max(10, content_width - 5))),
        badge=f"{glyphs.get(lifecycle, '·')} "
              f"{ui_label(lifecycle, values.locale)}",
        collaboration_lines=pipeline_lines(
            values.slots, values.events, lifecycle=lifecycle,
            pulse=values.pulse, tier=tier, width=content_width,
            selected_index=values.selected_index,
            expanded_stage=values.expanded_stage,
            groups=chains if grouped else None,
            group_labels=chain_labels if grouped else (),
            scroll_mode=values.scroll_mode,
            ascii_only=values.ascii_only, locale=values.locale),
        activity_lines=activity_tail_lines(
            values.events, reveal_seqs=values.reveal_seqs,
            ascii_only=values.ascii_only, width=content_width,
            locale=values.locale),
        detail_lines=(agent_detail_window(
            values.slots, values.events, stage=values.expanded_stage,
            lifecycle=lifecycle, last_outcome=values.last_outcome,
            width=content_width, ascii_only=values.ascii_only,
            max_lines=(10 if values.detail_max_lines is None
                       else values.detail_max_lines),
            locale=values.locale)
            if values.expanded_stage is not None else ()),
        progress_line=_progress_line(values, lifecycle,
                                     len(values.slots)),
        tokens_line=("TOKENS · "
                     + format_tokens(
                         _known_token_total(values.usage_records))),
        result_lines=_result_lines(
            values.last_outcome, content_width,
            reveal=values.result_reveal,
            ascii_only=values.ascii_only, locale=values.locale,
            max_lines=values.result_max_lines),
        context_lines=_context_lines(values, lifecycle),
        trace_obs=(trace_observation_lines(values.events)
                   if values.include_trace else ()),
        trace_ctrl=(trace_control_lines(values.facts)
                    if values.include_trace else ()),
        trace_usage=(trace_usage_lines(values.usage_records)
                     if values.include_trace else ()),
        lifecycle=lifecycle,
        tier=tier)
    if values.ascii_only:
        return _ascii_state(state)
    return state


def trace_observation_lines(events, *, ascii_only=False):
    """Trace OBS 页行集（CU-PERF-1 W2 所有权分离）。

    逐事件 format_event_line 的唯一实现点；build_projection 与
    TraceScreen 均经此取数（零第二实现）。ascii_only 时逐行符号
    降级，与 _ascii_state 尾部转换同律。"""
    lines = tuple(format_event_line(event).rstrip("\n")
                  for event in events)
    if ascii_only:
        lines = tuple(_to_ascii(line) for line in lines)
    return lines


def trace_control_lines(facts, *, ascii_only=False):
    """Trace CTRL 页行集（CU-PERF-1 W2）：账本条目逐条控制行。"""
    lines = tuple(_control_line(entry) for entry in facts)
    if ascii_only:
        lines = tuple(_to_ascii(line) for line in lines)
    return lines


def trace_usage_lines(usage_records, *, ascii_only=False):
    """Trace USAGE 页行集（CU-PERF-1 W2）：用量记录逐条行。"""
    lines = tuple(_usage_line(record) for record in usage_records)
    if ascii_only:
        lines = tuple(_to_ascii(line) for line in lines)
    return lines


def apply_budget_narrowing(state, values, *, detail_max_lines=None,
                           result_max_lines=None):
    """A4 收窄的窄域重算（CU-PERF-1 W3）。

    只重算受 detail_max_lines / result_max_lines 影响的两个字段
    （其唯一消费点在 build_projection 内恰为 agent_detail_window
    与 _result_lines 两处调用）；其余字段原样引用返回新 state——
    与携带同参数的全量 build_projection 逐字段相等（确定性纯函数，
    golden 矩阵锁死）。纯呈现，零事实触碰。"""
    detail_lines = state.detail_lines
    result_lines = state.result_lines
    if (detail_max_lines is not None
            and values.expanded_stage is not None):
        detail_lines = agent_detail_window(
            values.slots, values.events, stage=values.expanded_stage,
            lifecycle=state.lifecycle, last_outcome=values.last_outcome,
            width=_content_width_for(values.width),
            ascii_only=values.ascii_only,
            max_lines=detail_max_lines,
            locale=values.locale)
    if result_max_lines is not None:
        result_lines = _result_lines(
            values.last_outcome, _content_width_for(values.width),
            reveal=values.result_reveal,
            ascii_only=values.ascii_only, locale=values.locale,
            max_lines=result_max_lines)
    if values.ascii_only:
        detail_lines = tuple(
            _to_ascii(line) for line in detail_lines)
        result_lines = tuple(
            _to_ascii(line) for line in result_lines)
    return ProjectedState(
        header_line=state.header_line,
        task_line=state.task_line,
        badge=state.badge,
        collaboration_lines=state.collaboration_lines,
        activity_lines=state.activity_lines,
        detail_lines=detail_lines,
        progress_line=state.progress_line,
        tokens_line=state.tokens_line,
        result_lines=result_lines,
        context_lines=state.context_lines,
        trace_obs=state.trace_obs,
        trace_ctrl=state.trace_ctrl,
        trace_usage=state.trace_usage,
        lifecycle=state.lifecycle,
        tier=state.tier)


def _content_width_for(width):
    """content_width 的单一再导出（build_projection 同式）。"""
    tier = _tier_for(width)
    return (width - 4 if tier == "DEGRADED"
            else min(width - 4, 100))


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


def control_receipt_line(result, *, kind=None, ascii_only=False):
    """一次控制提交的同步回执单行（ControlResult duck 只读投影）。

    状态前缀符号（✓/✗/⊘）只是封闭三态的视觉标记；status / reason
    逐字（零新 ControlStatus）；kind 为呈现层已知的外发意图词（零
    铸造）。回执是瞬态 UI 呈现，不是控制历史——历史只来自账本事实。"""
    status = _value_of(getattr(result, "status", ""))
    line = (f"{_RECEIPT_GLYPHS.get(status, '·')} {status}"
            if status else "receipt")
    if kind:
        line += f" {kind}"
    command_id = getattr(result, "command_id", "")
    if command_id:
        line += f" · {command_id}"
    version = getattr(result, "execution_version", None)
    if version is not None:
        line += f" v{version}"
    reason = getattr(result, "reason", None)
    if reason is not None:
        line += f" · {_value_of(reason)}"
    if ascii_only:
        line = _to_ascii(line)
    return line


def worker_failure_lines(error, *, ascii_only=False, locale="en"):
    """worker 残余异常的诚实呈现行（Phase P：捕获可见、退出再抛）。

    只读异常对象自身（类型名 + str 消息，长消息截断）；完整原因
    保留在异常对象上、由退出路径如实上抛——本呈现绝不替代它，
    也绝不吞掉它。预期内的执行失败走 RunOutcome 投影（result
    lines），不经过本函数。首行 = 异常事实原词；次行 = 呈现提示
    （R2 可译）。"""
    lines = (f"✗ WORKER ERROR {type(error).__name__}: "
             f"{truncate_to_width(str(error), 72)}",
             ui_label("[Q] quit re-raises the original error", locale))
    if ascii_only:
        lines = tuple(_to_ascii(line) for line in lines)
    return lines


def revision_status_lines(facts, pending_count, *, locale="en"):
    """修订状态行（四档呈现律，CU-TUI-4 §10）。

    pending 只来自注入读数（None → —，绝不推断）；accepted/applied
    只来自账本事实；SUBMISSION 附引擎契约说明（静态文字，非状态
    声称；R2 呈现标签/说明可译，fact_type 原词）；honored 永不
    呈现——无可观测事实面。"""
    pending = "—" if pending_count is None else str(pending_count)
    lines = [ui_label("pending {n}", locale).format(n=pending)]
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
            line += ui_label(" · applies at next fresh segment", locale)
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
_FUNNEL_KEYS_HINT = "Enter start · c compose · q quit"
_FUNNEL_CHANGED_BANNER = "collaboration plan changed:"
_FUNNEL_BLANK_TASK_HINT = "describe the task first"
_FUNNEL_REDACTED = "[redacted: unsafe content]"


def funnel_preview_lines(composition, *, ascii_only=False, header=None):
    """默认组合披露行（§十三预览块）：标题 + 逐槽位一行
    "role ← runtime · provider"（角色列对齐至最长角色名）。

    blocked 组合零绑定 → 预览块整块缺席（诚实原因行独立渲染，
    见 funnel_blocked_line）。确定性纯函数。header 缺省 = 既有
    常量（CU-COCKPIT-1：COMPOSE 屏复用本格式器换标题，漏斗面
    逐字节不变）。"""
    if getattr(composition, "blocked_reason", None) is not None:
        return ()
    bindings = tuple(getattr(composition, "bindings", ()) or ())
    if not bindings:
        return ()
    width = max(len(bound.role) for bound in bindings)
    lines = [_FUNNEL_PREVIEW_HEADER if header is None else header]
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


def funnel_input_line(task_buffer, *, width=100, ascii_only=False):
    """漏斗输入回显行（CU-TUI-INPUT A1：自顶部 Static 迁入底部 dock
    的唯一回显面）。内容安全门与宽度截断与既有首屏输入行同律；
    尾部光标 | 由调用方追加（TUI-4 composer 惯例，投影层不产光标）。
    确定性纯函数。"""
    buffer_text = str(task_buffer)
    if contains_unsafe_content(buffer_text):
        buffer_text = _FUNNEL_REDACTED
    line = truncate_to_width(f"> {buffer_text}", width)
    if ascii_only:
        line = _to_ascii(line)
    return line


def funnel_prefill_text(task_buffer):
    """漏斗 prefill 的缓冲装载形态（UX2-R1 F-R1）：内容安全门与
    funnel_input_line 同律——unsafe 内容以占位符装载（秘密绝不
    进入 TUI 呈现态/缓冲）。确定性纯函数。"""
    buffer_text = str(task_buffer)
    if contains_unsafe_content(buffer_text):
        buffer_text = _FUNNEL_REDACTED
    return buffer_text


def funnel_keys_hint(*, ascii_only=False):
    """漏斗两键提示行（CU-TUI-INPUT A1：迁入 dock-controls 的唯一
    提示面；漏斗呈现 EN 冻结——R2 ERRATA 同律）。"""
    line = _FUNNEL_KEYS_HINT
    if ascii_only:
        line = _to_ascii(line)
    return line


def funnel_first_screen(version_text, composition, task_buffer, *,
                        width=100, ascii_only=False, include_input=True):
    """首屏六要素组装（NOT_STARTED/COMPOSING 共用，§十三）：
    header（版本真源由调用方注入，本层零版本读取）/ 唯一指令行 /
    输入行（内容安全门 + 宽度截断）/ 预览块 / 恰两键提示 / BLOCKED
    原因行。零 task_id/execution_id/UUID/内部对象/debug metadata。
    CU-TUI-INPUT A1：include_input=False 为输入迁入底部 dock 的组装
    形态——body 省略输入行与两键提示（回显经 funnel_input_line 入
    dock-input、提示经 funnel_keys_hint 入 dock-controls）；默认
    True 与既有输出逐字节一致。"""
    header = (f"dual-agent cockpit · {version_text}" if version_text
              else "dual-agent cockpit")
    lines = [header,
             _FUNNEL_INSTRUCTION]
    if include_input:
        lines.append(funnel_input_line(task_buffer, width=width))
    lines.extend(funnel_preview_lines(composition))
    blocked_reason = getattr(composition, "blocked_reason", None)
    if blocked_reason is not None:
        lines.append(blocked_reason)
    if include_input:
        lines.append(_FUNNEL_KEYS_HINT)
    if ascii_only:
        lines = [_to_ascii(line) for line in lines]
    return tuple(lines)


# ------------------------------------ 2.8-A 会话轮次呈现（Conversation 层）
# （确定性纯函数：runs 镜像 = TUI 自有呈现态快照（task 原文/steps
# 交付序/status 词），非引擎真相读回；status 缺席 = 在飞，绝不推断。
# 零 IO、零事件、零引擎词；Log 闭集纪律——行词全经 _LABELS，task/
# roles/status 为事实面 domain 词经 format 注入，绝不翻译。）

def run_divider_line(run_number, steps, *, width=100, locale="en",
                     ascii_only=False):
    """会话 Log 分节线（run N ≥ 2 启动时入 Log，§九）：呈现层连接
    线——只消费 TUI 传入的 run 序号与 steps 镜像，宽度经
    truncate_to_width、符号经 _to_ascii（CJK/降级铁律同全局）。"""
    roles = "→".join(str(role) for role, _ in steps)
    line = ui_label("── run {n} · {k} agents · {roles} ──",
                    locale).format(n=run_number, k=len(steps), roles=roles)
    line = truncate_to_width(line, width)
    if ascii_only:
        line = _to_ascii(line)
    return line


def runs_summary_lines(runs, *, width=100, locale="en",
                       ascii_only=False):
    """/runs 会话摘要（ConversationRecord 呈现视图原料，§十七）：
    每轮一行 run N · status · task 摘要；status None = 在飞
    （呈现 RUNNING 词，绝不伪造终态）。runs 空 = 恰一行诚实提示。"""
    if not runs:
        line = ui_label("no collaborations yet", locale)
        return ((line,) if not ascii_only
                else (_to_ascii(line),))
    lines = [ui_label("runs", locale)]
    for index, (task, steps, status) in enumerate(runs, start=1):
        status_word = (str(status) if status is not None
                       else ui_label("RUNNING", locale))
        task_text = str(task) if task is not None else ""
        if not task_text:
            task_text = ui_label("(no text output)", locale)
        line = ui_label("run {n} · {status} · {task}",
                        locale).format(n=index, status=status_word,
                                       task=task_text)
        lines.append(truncate_to_width(line, width))
    if ascii_only:
        lines = [_to_ascii(line) for line in lines]
    return tuple(lines)


# --------------------------------------- CU-COCKPIT-1 COMPOSE 选择屏投影
# （确定性纯函数：entries = Verified 池清单 duck（runtime_id/
# provider_id 二属性协议，cockpit_entry.listing 注入）；selected/
# roles/cursor 均为 TUI 呈现态快照。零 IO、零事件、零引擎词。）

def compose_pool_lines(entries, *, selected_ids=(), cursor_index=0,
                       locale="en", ascii_only=False, width=100):
    """池清单行：光标行 ▶ 前缀 + [x]/[ ] 勾选 + runtime_id · provider。

    只列 Verified 池成员（listing 已过滤——Installed≠Verified 的
    层次不绕行）；空池 = 恰一行诚实提示（词表闭集）。标准宽度
    （>=120）附加非重复 display_name；紧凑宽度保持既有字节面。"""
    selected = frozenset(selected_ids)
    lines = []
    for index, entry in enumerate(entries):
        marker = "▶ " if index == cursor_index else "  "
        box = "[x]" if entry.runtime_id in selected else "[ ]"
        line = f"{marker}{box} {entry.runtime_id} · {entry.provider_id}"
        display_name = getattr(entry, "display_name", None)
        if (width >= 120 and display_name
                and display_name != entry.runtime_id):
            line += f" · {display_name}"
        lines.append(line)
    if not lines:
        lines = [ui_label("no VERIFIED runtimes", locale)]
    if ascii_only:
        lines = [_to_ascii(line) for line in lines]
    return tuple(lines)


def compose_participant_lines(selected_ids, roles, *,
                              participant_index=0):
    """participant 行（声明序 = sorted runtime_id）：
    member-N  ROLE ← runtime_id。roles 值缺席（尚未指派/未预览）
    = 诚实 "—"（缺席呈现既有惯例）；duplicate Role 合法（按声明
    原样呈现）；零选择 = 零行（区块标题由组装器管理）。"""
    lines = []
    for index, runtime_id in enumerate(sorted(selected_ids)):
        marker = "▶ " if index == participant_index else "  "
        role = roles.get(runtime_id)
        role_text = role.upper() if role else "—"
        lines.append(f"{marker}member-{index + 1}  {role_text} "
                     f"← {runtime_id}")
    return tuple(lines)


def compose_keys_hint(*, locale="en", ascii_only=False):
    """COMPOSE 键提示（唯一提示面；键字母恒 EN 既有惯例，动词经
    闭集词表）。"""
    line = ui_label(
        "↑↓ move · space select · ←→ member · r role · "
        "enter preview/start · esc back · l lang · q quit", locale)
    if ascii_only:
        line = _to_ascii(line)
    return line


def compose_screen_lines(version_text, task_text, entries, *,
                         selected_ids=(), cursor_index=0, roles=None,
                         participant_index=0, preview_composition=None,
                         message_lines=(), width=100, height=None,
                         ascii_only=False, locale="en"):
    """COMPOSE 屏组装（漏斗首屏同型）：header / task 回显 / 池清单 /
    participants / 计划块（preview 成功时）/ 瞬态反馈 / 键提示末行。

    preview_composition 带 reason（CompositionError duck）或零绑定时
    计划块缺席——错误词经 message_lines 原样呈现（本层零推断）。
    height 只约束池窗口且始终保留光标与至少三条真实池行；逐行宽度
    截断（不溢出铁律）。"""
    roles = roles or {}
    entries = tuple(entries)
    selected_ids = tuple(selected_ids)
    header = (f"dual-agent cockpit · {version_text}"
              if version_text else "dual-agent cockpit")
    lines = [f"{header} · {ui_label('compose collaboration', locale)}",
             f"{ui_label('TASK', locale)}: "
             f"{truncate_to_width(str(task_text), max(1, width - 8))}"]
    selected_label = ui_label("selected {n}/4", locale).format(
        n=len(selected_ids))
    lines.append(f"── {ui_label('runtimes', locale)} · {selected_label}")

    pool_entries = entries
    pool_cursor = cursor_index
    hidden = 0
    if height is not None and len(entries) > 3:
        fixed_count = (7 + len(selected_ids) + len(message_lines)
                       + (1 if len(entries) < 2 else 0))
        pool_limit = max(3, height - fixed_count)
        if pool_limit < len(entries):
            pool_limit = min(pool_limit, len(entries))
            cursor = min(max(0, cursor_index), len(entries) - 1)
            start = min(max(0, cursor - pool_limit // 2),
                        len(entries) - pool_limit)
            pool_entries = entries[start:start + pool_limit]
            pool_cursor = cursor - start
            hidden = len(entries) - pool_limit
    lines.extend(compose_pool_lines(
        pool_entries, selected_ids=selected_ids, cursor_index=pool_cursor,
        locale=locale, width=width))
    if hidden:
        lines.append(ui_label("(+{n} more lines)", locale).format(n=hidden))

    lines.append(f"── {ui_label('participants', locale)}")
    lines.extend(compose_participant_lines(
        selected_ids, roles, participant_index=participant_index))
    role_text = " · ".join(COMPOSITION_ROLES)
    lines.append(ui_label("roles (r): {roles}", locale).format(
        roles=role_text))
    if len(entries) < 2:
        lines.append(ui_label(
            "need ≥2 VERIFIED runtimes — qualify first", locale))

    bindings = tuple(getattr(preview_composition, "bindings", ())
                     or ()) if preview_composition is not None else ()
    if bindings:
        lines.extend(funnel_preview_lines(
            preview_composition, header=ui_label(
                "collaboration plan", locale)))
        lines.append(ui_label("plan ready — enter to start", locale))
    else:
        lines.append(ui_label("press enter to preview", locale))
    lines.extend(message_lines)
    lines.append(compose_keys_hint(locale=locale))
    lines = [truncate_to_width(line, width) for line in lines]
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
    """出向交接：只来自真实 HANDOFF 事件（stage=产出角色，runtime=接收方）。"""
    handoffs = []
    for event in events:
        if (_event_type_of(event) != "HANDOFF"
                or getattr(event, "stage", None) != slot_view.role):
            continue
        handoffs.append(
            f"→ {getattr(event, 'runtime_id', '')}"
            f" ({_value_of(getattr(event, 'status', ''))})")
    return "; ".join(handoffs) if handoffs else "—"


def _slot_handoff_inbound_text(slot_view, events):
    """入向交接：只来自真实 HANDOFF 事件（runtime=本槽 runtime，
    stage=产出方角色）——相邻顺序绝不产生交接行。"""
    handoffs = []
    for event in events:
        if (_event_type_of(event) != "HANDOFF"
                or getattr(event, "runtime_id", None)
                != slot_view.runtime_id):
            continue
        handoffs.append(
            f"{getattr(event, 'stage', '')} → here"
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


def agent_detail_window(slots, events, *, stage, lifecycle="RUNNING",
                        last_outcome=None, width=100, ascii_only=False,
                        max_lines=10, locale="en"):
    """Agent Detail 有界投影窗（R1 §十）：管线正下方、单 agent、
    ≤max_lines 的 bounded projection（非滚动视图）。

    逐区块只读投影：prompt=—（观察契约冻结——事件绝不携带 prompt，
    零合成、零推断）；handoff 双向只来自真实 HANDOFF 事件；activity
    为 EventIndex 按 stage==role 过滤的尾 6 条（与主屏活动尾窗同限、
    同一格式器，零第二套事件缓存）；result=最后一条
    INVOCATION_FINISHED 原词 + duration + 终态 transcript
    StepRecord.status（既有真源）；state 复用 _slot_state_word 八态
    （零新状态机）。超窗尾行 (+k more · T) 指向 Trace——完整历史
    唯一出口在 Trace，本窗绝不截断真相、只限投影窗。stage 缺席
    （组合刷新后身份消失）→ 诚实空行集。R2：标签列经闭集词表并
    _pad_cell 对齐（display-width 感知，CJK 安全）；区块结构与
    事实行原样。CU-PERF-1 W3b：事件扫描收敛为单遍——一遍同时
    收集过滤尾窗/在途/阶段启动/终局状态/时长/双向 handoff，
    后 O(1) 组合；各字段语义与多遍形态逐字节一致（golden 锁死）。"""
    target = None
    for slot_view in slots:
        if slot_view.stage == stage:
            target = slot_view
            break
    if target is None:
        return ()
    # 单遍收集（stage==role 过滤下的全部派生量）
    filtered_tail = []      # 过滤事件尾窗原料（全量保留，后取尾 6）
    started = False         # INVOCATION_STARTED 在场
    stage_started = False   # STAGE_STARTED 在场
    finished_status = None  # 最后一次 INVOCATION_FINISHED 状态
    last_duration = None    # 最后一次 FINISHED 携带的 duration_ms
    handoff_out = []        # HANDOFF(stage=产出角色 → 本槽出向)
    handoff_in = []         # HANDOFF(runtime=本槽 → 本槽入向)
    for event in events:
        if getattr(event, "stage", None) != target.role:
            # 入向 handoff 的 stage 是产出方角色——只在 runtime 匹配
            # 分支消费（下方独立判定），此处不归入任何 stage 过滤量
            if (_event_type_of(event) == "HANDOFF"
                    and getattr(event, "runtime_id", None)
                    == target.runtime_id):
                handoff_in.append(
                    f"{getattr(event, 'stage', '')} → here"
                    f" ({_value_of(getattr(event, 'status', ''))})")
            continue
        filtered_tail.append(event)
        kind = _event_type_of(event)
        if kind == "STAGE_STARTED":
            stage_started = True
        elif kind == "INVOCATION_STARTED":
            started = True
        elif kind == "INVOCATION_FINISHED":
            finished_status = _value_of(getattr(event, "status", ""))
            duration = getattr(event, "duration_ms", None)
            if duration is not None:
                last_duration = duration
        elif kind == "HANDOFF":
            handoff_out.append(
                f"→ {getattr(event, 'runtime_id', '')}"
                f" ({_value_of(getattr(event, 'status', ''))})")
    result_value = "—"
    if finished_status is not None:
        result_value = str(finished_status)
        if last_duration is not None:
            result_value += f" · {last_duration}ms"
        step_status = _slot_result_text(target, last_outcome)
        if step_status != "—":
            result_value += f" · step {step_status}"
    # 状态词：八态推导（_slot_state_word 同语义的 O(1) 组合——
    # 收集量即其全部输入）
    if finished_status is not None:
        word = ("DONE" if str(finished_status).upper() == "SUCCESS"
                else "FAILED")
    elif started:
        if lifecycle == "PAUSED":
            word = "PAUSED"
        elif lifecycle == "PARKED":
            word = "PARKED"
        elif lifecycle == "ABORTED":
            word = "ABORTED"
        else:
            word = "RUNNING"
    elif stage_started:
        word = "WAITING"
    else:
        word = "NOT_STARTED"
    glyph = (_AGENT_GLYPHS_ASCII[word] if ascii_only
             else _AGENT_GLYPHS[word])
    handoff_out_text = ("; ".join(handoff_out)
                        if handoff_out else "—")
    handoff_in_text = ("; ".join(handoff_in)
                       if handoff_in else "—")

    def label_pad(key):
        return _pad_cell(ui_label(key, locale), 8)

    lines = [f"▼ {target.role.upper()}",
             f"  {label_pad('prompt')}  —",
             f"  {label_pad('handoff')}  "
             f"{_pad_cell(ui_label('in', locale), 4)}{handoff_in_text}",
             f"  {label_pad('handoff')}  "
             f"{_pad_cell(ui_label('out', locale), 4)}{handoff_out_text}"]
    for index, event in enumerate(filtered_tail[-6:]):
        label = (label_pad("activity") if index == 0 else " " * 8)
        lines.append(f"  {label}  {_activity_line(event, locale)}")
    lines.extend((
        f"  {label_pad('result')}  {result_value}",
        f"  {label_pad('state')}  {glyph} {ui_label(word, locale)}",
    ))
    if len(lines) > max_lines:
        dropped = len(lines) - (max_lines - 1)
        lines = lines[:max_lines - 1]
        lines.append(
            ui_label("(+{n} more · T)", locale).format(n=dropped))
    if ascii_only:
        lines = [_to_ascii(line) for line in lines]
    return tuple(truncate_to_width(line, width) for line in lines)


def _ascii_state(state):
    """G12 ASCII 降级：逐符号映射，结构与层级不变。"""
    return ProjectedState(
        header_line=_to_ascii(state.header_line),
        task_line=_to_ascii(state.task_line),
        badge=_to_ascii(state.badge),
        collaboration_lines=tuple(
            _to_ascii(line) for line in state.collaboration_lines),
        activity_lines=tuple(
            _to_ascii(line) for line in state.activity_lines),
        detail_lines=tuple(
            _to_ascii(line) for line in state.detail_lines),
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
