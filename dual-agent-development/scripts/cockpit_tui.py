"""CU-TUI-3/4/5 (V3.2): Textual 协作驾驶舱 — 呈现 + 用户控制面。

宪法（CU-TUI-3 G3/G4 + CU-TUI-4 C1/C2）：

- UI 框架的 import 只存在于本模块内部，且为惰性——模块级导入零
  框架依赖（App/TraceScreen 类经 __getattr__ 在首次访问时构建），
  cockpit_entry 只尝试导入本模块，绝不直接触碰框架；
- 本层 READ → PROJECT → RENDER + 单向意图外发（DISPATCH）：执行
  驱动唯一来自入口注入的 driver 回调；控制意图唯一经入口注入的
  dispatcher 回调外发——本层不 import 控制域、不触碰引擎对象、
  不构造命令值；revision pending 只经注入的只读读数（int 或缺席，
  缺席呈现 —，绝不推断）；
- 本层不持有第二份执行真相：lifecycle/usage/observation/修订状态
  全部经 cockpit_projection 从既有事实源纯投影；选择/跟随/回执等
  呈现态绝不写回任何事实源（READ 与 DISPATCH 两径严格分离）；
- 交互模型恰三态（CU-TUI-4 C2）：COMMAND 单键面 /
  REVISION_COMPOSER 修订编辑 / ABORT_CONFIRM 破坏性确认——此外
  无第四输入模式；
- G16：依赖缺席的回退发生在入口层（textual_available）；
  本层真实异常诚实上抛/如实呈现失败，绝不伪装成功；
- CU-TUI-5 首跑漏斗：单一 CockpitApp、单次 run()——漏斗是 App 的
  初始呈现阶段（组合句柄 late-bound，Start 前零引擎对象、零 driver
  线程）；组合真相只经注入的 preview/start 两闭包进出（零引擎
  import、零 identity 计算）；漏斗外层呈现态（STAGE_*）为 TUI
  私有，绝不进入事件/生命周期词表。

线程模型（单 Session）：UI 线程只做只读投影、渲染与意图外发；
worker 线程经驱动循环执行注入的 driver——PARKED 停驻等待注入回执
唤醒（wake 仅为私有同步原语，绝非真相）；terminal 呈现保留（App
退出只经用户 q/Ctrl-C 阶梯，绝不因 RunOutcome 到达而自动退出）。

刷新为数据驱动重投影；Phase V 授权的受限动画（活动脉冲 ●↔◉ /
新事件揭示 ▸ / 结果揭示 »）均为纯呈现参数——由刷新 tick 计数
派生（零时钟、零随机、零伪造事件），只改变渲染字符串，绝不参与
lifecycle 推导、绝不写回任何事实源（Animation ≠ Execution
State）。漏斗态无 interval——刷新为纯键事件驱动。
"""
from __future__ import annotations

import sys
import threading

from cockpit_projection import (
    AgentSlotView,
    ProjectionInputs,
    agent_detail,
    build_projection,
    control_receipt_line,
    derive_lifecycle,
    event_detail_line,
    funnel_changed_lines,
    funnel_enter_lines,
    funnel_error_lines,
    funnel_first_screen,
    funnel_input_line,
    funnel_keys_hint,
    revision_status_lines,
    trace_status_line,
    ui_label,
    worker_failure_lines,
)
from event_index import EventIndex

__all__ = ("CockpitApp", "TraceScreen", "new_event_store",
           "run_cockpit_tui", "run_cockpit_funnel", "textual_available",
           "STAGE_NOT_STARTED", "STAGE_COMPOSING", "STAGE_RUNNING",
           "STAGE_TERMINAL")


# 交互三态（CU-TUI-4 C2 裁决：恰此三态，无第四模式）。
MODE_COMMAND = "command"
MODE_COMPOSER = "composer"
MODE_CONFIRM = "confirm"

# 漏斗外层呈现态（CU-TUI-5 §十一：TUI presentation-private，绝不进入
# engine event/lifecycle/observation 词表、绝不持久化；READY 为派生态
# ——缓冲非空白 ∧ 预览非 BLOCKED，非独立模态，故无独立常量）。
STAGE_NOT_STARTED = "funnel_not_started"
STAGE_COMPOSING = "funnel_composing"
STAGE_RUNNING = "funnel_running"
STAGE_TERMINAL = "funnel_terminal"

# 修订 target 封闭二选一（引擎既有词值，呈现层零新词）。
_TARGETS = ("NEXT_INVOCATION", "SUBMISSION")

# RUNNING 观察区选择器（漏斗态整组隐藏，Start 后整组复现）。
# CU-TUI-INPUT A1/A2：#input-dock 恒在场（漏斗与 RUNNING 同一底部
# dock——Start 前后输入位置恒底，绝不跳变），故不在隐藏组内。
_MAIN_ZONE_SELECTORS = (
    "#header-zone", "#task-zone", "#collab-zone", "#detail-zone",
    "#activity-zone", "#result-zone", "#progress-zone",
    "#context-panel")

# 底部输入 dock 的保留行数（CU-TUI-INPUT A4 高度预算真源：CSS
# height 与内容预算共用此常量，绝不双写）。
_DOCK_ROWS = 3

# 回执有界寿命：按刷新次数衰减（零时钟，确定性）。
_RECEIPT_TICKS = 6

# 结果揭示有界寿命（Phase V A-3）：终态到达后按刷新次数衰减。
_RESULT_REVEAL_TICKS = 4

# 键位提示的终态集（与投影层终态词表同值的只读呈现面）。
_TERMINAL_LIFECYCLE_HINTS = ("COMPLETED", "FAILED", "ABORTED")


def new_event_store():
    """呈现层自有的只读事件索引工厂（CU-TUI-3 消费面）。

    组合根在呈现层在场而调用方未注入观察面时经此取得事件
    store；构造归消费方，默认路径零事件面（CU-TUI-1 零漂移
    语义保持）。"""
    return EventIndex()


def textual_available() -> bool:
    """惰性探测（G3）：框架是否可导入（缺席 → 入口层回退）。"""
    try:
        import textual  # noqa: F401  惰性：仅探测，零使用
    except ImportError:
        return False
    return True


def _ascii_preferred() -> bool:
    """G12 ASCII 降级偏好：输出编码非 UTF 系时启用符号降级。"""
    encoding = (getattr(sys.stdout, "encoding", "") or "").lower()
    return "utf" not in encoding


def _version_text() -> str:
    """版本真源读取（cli.py 双模式 import 先例同型：安装态包相对 /
    源码树 flat `__init__`；两图皆缺席时诚实空串 → header 无版本段）。"""
    try:
        from . import __version__
        return __version__
    except ImportError:
        pass
    try:
        from __init__ import __version__
        return __version__
    except ImportError:
        return ""


def _fact_kind(entry):
    """账本条目词值（封闭词表只读读取面）。"""
    kind = getattr(entry, "fact_type", None)
    return getattr(kind, "value", kind)


def _build_classes() -> None:
    """首次访问 CockpitApp / TraceScreen 时构建类（惰性框架 import）。

    类构建后写入模块全局，后续访问走正常属性查找；构建动作幂等。
    """
    global CockpitApp, TraceScreen
    if globals().get("CockpitApp") is not None:
        return

    from textual.app import App
    from textual.binding import Binding
    from textual.containers import Container, VerticalScroll
    from textual.screen import Screen
    from textual.widgets import Static, TabbedContent, TabPane

    class CockpitApp(App):  # type: ignore[misc]
        """主界面：六区 + 固定底 dock + （≥140 列）Context 面板。"""

        CSS = f"""
        #funnel-screen {{ display: none; }}
        #input-dock {{ dock: bottom; height: {_DOCK_ROWS}; }}
        #dock-controls {{ height: 1; }}
        #dock-receipt {{ height: 1; }}
        #dock-input {{ height: 1; }}
        #context-panel {{ dock: right; width: 28; display: none; }}
        #header-zone {{ height: 1; }}
        #detail-zone {{ height: auto; }}
        #activity-zone {{ height: auto; }}
        #result-zone {{ height: auto; }}
        """

        # 字母键全部经 on_key 按态分发（避免框架级绑定绕过状态机）。
        # tab 是 Screen 默认焦点键会先期消费——priority 绑定改走
        # composer 的 target 切换；ctrl+c 与 q 同径（冻结决策）。
        BINDINGS = [
            Binding("tab", "cockpit_tab", "Target", priority=True),
            Binding("ctrl+c", "cockpit_quit", "Quit", priority=True),
        ]

        def __init__(self, *, driver=None, task="", plan=(), events=None,
                     facts=None, usage=None, session=None, control=None,
                     revision_pending=None, composition_preview=None,
                     start_composition=None, task_token=None,
                     timeout_seconds=None):
            super().__init__()
            self._cockpit_drive = driver
            self._cockpit_task = task
            self._cockpit_plan = tuple(plan)
            self._cockpit_events = events
            self._cockpit_facts = facts
            self._cockpit_usage = usage
            self._cockpit_session = session
            # CU-TUI-4 注入面（DISPATCH / READ 两径，均零引擎知识）
            self._cockpit_control = control
            self._cockpit_revision_pending = revision_pending
            # 呈现态（绝不入投影输入、绝不持久、绝不写回事实源）
            self._cockpit_mode = MODE_COMMAND
            self._cockpit_revise_text = ""
            self._cockpit_revise_target = _TARGETS[0]
            self._cockpit_receipt = None
            self._cockpit_receipt_ttl = 0
            self.outcome = None
            self.failure = None
            self._cockpit_thread = None
            # Phase P：驱动循环的私有同步原语——wake 只唤醒停驻等待
            # （RESUME/ABORT 受理时 set），exit 旗标只请求线程收尾；
            # 两者均为呈现层私语，绝不入投影输入、绝不写回事实源。
            self._cockpit_wake = threading.Event()
            self._cockpit_exit_requested = False
            # Phase V 呈现态（绝不入投影真相、绝不写回事实源）：
            # 动画 tick（刷新计数派生，零时钟）/ 活动揭示已见序列 /
            # 结果揭示剩余 tick / 上次 lifecycle（终态跃迁检测）。
            self._cockpit_tick = 0
            self._cockpit_seen_seq = None
            self._cockpit_result_reveal_ticks = 0
            self._cockpit_prev_lifecycle = None
            # R1 呈现态：选中 cell（index，refresh 时对组合长度 clamp）
            # 与展开 agent（绑定稳定 slot 身份 stage；None=全折叠）。
            # 二者互不联动、零 dispatch、绝不写回任何事实源。
            self._cockpit_selected_index = 0
            self._cockpit_expanded_stage = None
            # R2 呈现态：界面语言（封闭 en/zh，默认 en；零 OS/LANG/
            # env 探测、零落盘持久化）。session-local——App 退出即消
            # 失，重启回 en；绝不入 RunState/账本/EventIndex/UsageLog/
            # 执行请求，绝不写回任何事实源。
            self._cockpit_locale = "en"
            # 渲染文本快照（funnel_text 先例：属性镜像便于测试断言）
            self.header_text = ""
            self.task_text = ""
            self.agent_text = ""
            self.activity_text = ""
            self.detail_text = ""
            self._cockpit_ascii = _ascii_preferred()
            self._cockpit_show_context = True
            # CU-TUI-5 漏斗面：注入闭包 = 组合真相唯一通道；Start 前
            # _cockpit_composed 恒 None（late-bound，零引擎对象）。
            # timeout 已由入口闭包捕获（真值不在本层），参数仅为
            # 路由对齐；漏斗态无 interval（键事件驱动刷新）。
            self._cockpit_composition_preview = composition_preview
            self._cockpit_start_composition = start_composition
            self._cockpit_composed = None
            self._cockpit_funnel_timeout = timeout_seconds
            self._cockpit_funnel_buffer = task_token if task_token else ""
            self._cockpit_funnel_message = ()
            self._cockpit_stage = (
                STAGE_COMPOSING if self._cockpit_funnel_buffer.strip()
                else STAGE_NOT_STARTED)
            self._cockpit_disclosure = None
            self._cockpit_version_text = _version_text()
            if composition_preview is not None:
                self._cockpit_disclosure = composition_preview()

        # ------------------------------------------------ 布局

        def compose(self):
            # R1 主屏次序：Header / Task / Collaboration Pipeline /
            # Agent Detail（选中且展开时有界窗）/ Live Activity /
            # Result / Status / Bottom Dock（+ ≥140 Context）
            yield Static("", id="funnel-screen")
            yield Static("", id="header-zone")
            yield Static("", id="task-zone")
            yield Static("", id="collab-zone")
            yield Static("", id="detail-zone")
            yield Static("", id="activity-zone")
            yield Static("", id="result-zone")
            yield Static("", id="progress-zone")
            with Container(id="input-dock"):
                yield Static("", id="dock-controls")
                yield Static("", id="dock-receipt")
                yield Static("", id="dock-input")
            yield Static("", id="context-panel")

        def on_mount(self) -> None:
            if self._cockpit_composition_preview is not None:
                self._funnel_mount()
                return
            self._running_mount()

        def _funnel_mount(self) -> None:
            """漏斗初始呈现：主界面观察区隐藏，单一漏斗屏在场。
            CU-TUI-INPUT A1/A2：底部输入 dock 恒在场（不在隐藏组）——
            漏斗与 RUNNING 同一 dock，输入位置全程恒底。"""
            for selector in _MAIN_ZONE_SELECTORS:
                self.query_one(selector).display = False
            self.query_one("#funnel-screen").display = True
            self._funnel_refresh()

        def _running_mount(self) -> None:
            """RUNNING 呈现（legacy 直达与漏斗 Start 后共用同一面）。"""
            self.query_one("#funnel-screen").display = False
            for selector in _MAIN_ZONE_SELECTORS:
                self.query_one(selector).display = True
            self._refresh()
            # 数据驱动重投影：仅当事实源变化时内容才变化（零动画）
            self.set_interval(0.5, self._refresh)
            self._cockpit_thread = threading.Thread(
                target=self._drive_loop, daemon=True)
            self._cockpit_thread.start()

        # ------------------------------------------------ 数据

        def _collect_inputs(self):
            """入口供应的事实源只读快照 → 投影输入（零第二真相）。"""
            width = self.size.width or 100
            session = self._cockpit_session
            return ProjectionInputs(
                task=self._cockpit_task,
                slots=tuple(
                    AgentSlotView(stage=stage, role=role,
                                  runtime_id=runtime, provider=provider)
                    for stage, role, runtime, provider in self._cockpit_plan),
                events=tuple(self._cockpit_events()),
                facts=tuple(self._cockpit_facts()),
                usage_records=tuple(self._cockpit_usage()),
                terminal=None if session is None else session.terminal,
                run_state=(None if session is None
                           else session.run_state),
                last_outcome=(None if session is None
                              else session.last_outcome),
                width=width,
                ascii_only=self._cockpit_ascii)

        def _refresh(self, *, advance_tick: bool = True) -> None:
            """READ → PROJECT → RENDER（UI 线程执行）。

            Phase V 动画呈现参数在此装配：pulse 由 tick 计数派生
            （0.5s 刷新 × 2 = ~1Hz）；活动揭示 = 超出已见序列的事件
            （瞬态，下一轮无新事件即衰减）；结果揭示在 lifecycle 跃迁
            至终态时启动并按 tick 衰减。三者均为纯呈现输入。
            R2：advance_tick=False 为零推进渲染——语言切换重绘用
            （tick/揭示/回执寿命一概不动，脉冲相位保持）；缺省 True
            与既有调用逐字节同径。"""
            if not self.query("#header-zone"):
                # teardown 竞态：主屏节点已不在 DOM（App 关停换屏窗口）
                # ——跳过本轮渲染。纯呈现关切，零事实影响。
                return
            if advance_tick:
                self._cockpit_tick += 1
            values = self._collect_inputs()
            # R1 选中 clamp：组合快照变化（缩减/重排）绝不越界
            if values.slots:
                self._cockpit_selected_index = max(
                    0, min(self._cockpit_selected_index,
                           len(values.slots) - 1))
            values.selected_index = self._cockpit_selected_index
            values.expanded_stage = self._cockpit_expanded_stage
            # R2 界面语言（纯呈现；仅主屏渲染路径供给——TraceScreen
            # 自行 collect，事实面恒 en）
            values.locale = self._cockpit_locale
            # 活动揭示（A-2）：首帧建立基线不揭示，此后仅新序列揭示
            sequences = [getattr(event, "sequence", None)
                         for event in values.events]
            sequences = [seq for seq in sequences if seq is not None]
            if self._cockpit_seen_seq is None:
                self._cockpit_seen_seq = max(sequences) if sequences else -1
            fresh = tuple(seq for seq in sequences
                          if seq > self._cockpit_seen_seq)
            if fresh:
                self._cockpit_seen_seq = max(fresh)
            # 终态跃迁 → 结果揭示（A-3）有界寿命
            lifecycle = derive_lifecycle(
                values.terminal, values.run_state, values.events,
                values.facts)
            if lifecycle != self._cockpit_prev_lifecycle:
                if lifecycle in _TERMINAL_LIFECYCLE_HINTS:
                    self._cockpit_result_reveal_ticks = _RESULT_REVEAL_TICKS
                self._cockpit_prev_lifecycle = lifecycle
            if advance_tick and self._cockpit_result_reveal_ticks > 0:
                self._cockpit_result_reveal_ticks -= 1
            values.pulse = (self._cockpit_tick // 2) % 2 == 0
            values.reveal_seqs = fresh
            values.result_reveal = self._cockpit_result_reveal_ticks > 0
            state = build_projection(values)
            # CU-TUI-INPUT A4：内容高度预算——Σ可见区行数必须服从
            # dock 保留高度；超出时按让位次序收窄/隐藏（纯呈现，
            # 可能经呈现参数第二遍重建——其余字段逐字节不变）。
            failure_lines = (
                worker_failure_lines(
                    self.failure, ascii_only=self._cockpit_ascii,
                    locale=self._cockpit_locale)
                if self.failure is not None else None)
            result_rows = (len(failure_lines) if failure_lines is not None
                           else len(state.result_lines))
            state, detail_fits, activity_fits = (
                self._apply_content_budget(
                    values, state, result_rows=result_rows))
            self.last_state = state
            self.header_text = state.header_line
            self.query_one("#header-zone").update(state.header_line)
            self.task_text = state.task_line
            self.query_one("#task-zone").update(state.task_line)
            self.agent_text = "\n".join(state.collaboration_lines)
            self.query_one("#collab-zone").update(self.agent_text)
            # R1 Detail 有界窗：管线正下方；身份消失/未展开 = 诚实空；
            # 高度预算与活动尾窗同门规（<24 隐藏，Trace 仍是全量出口）
            self.detail_text = "\n".join(state.detail_lines)
            detail = self.query_one("#detail-zone")
            detail.update(self.detail_text)
            detail.display = bool(self.detail_text) \
                and self.size.height >= 24 and detail_fits
            self.activity_text = "\n".join(state.activity_lines)
            activity = self.query_one("#activity-zone")
            activity.update(self.activity_text)
            # 高度预算（§二十二）：<24 行优先保留 Header/Task/Agents/
            # Status/Dock，隐藏活动尾窗（Trace 页仍可看全量历史）；
            # CU-TUI-INPUT A4：≥24 但预算不足时同样让位（整区隐藏）
            activity.display = self.size.height >= 24 and activity_fits
            if failure_lines is not None:
                # Phase P：worker 残余异常诚实可见（预期内执行失败走
                # RunOutcome 投影的 result lines，不经此处）
                result_text = "\n".join(failure_lines)
            else:
                result_text = "\n".join(state.result_lines)
            self.result_text = result_text
            result = self.query_one("#result-zone")
            result.update(result_text)
            # 空态整区隐藏（零结果零占位；有结果恒在场）
            result.display = bool(result_text)
            self.query_one("#progress-zone").update(
                f"{state.progress_line}\n{state.tokens_line}")
            self.query_one("#dock-controls").update(
                self._dock_controls_line())
            self.query_one("#dock-receipt").update(
                self._dock_receipt_line())
            self.query_one("#dock-input").update(self._dock_input_line())
            panel = self.query_one("#context-panel")
            wide = self.size.width >= 140
            if state.context_lines and wide and self._cockpit_show_context:
                panel.update("\n".join(state.context_lines))
                panel.display = True
            else:
                panel.display = False
            # 回执有界衰减（按刷新次数，零时钟；零推进渲染不消耗寿命）
            if advance_tick and self._cockpit_receipt_ttl > 0:
                self._cockpit_receipt_ttl -= 1
                if self._cockpit_receipt_ttl == 0:
                    self._cockpit_receipt = None

        # ------------------------------------------------ 高度预算（A4）

        def _apply_content_budget(self, values, state, *, result_rows):
            """CU-TUI-INPUT A4：dock 保留高度预算（纯呈现）。

            Σ可见区行数 ≤ 终端高 - _DOCK_ROWS（dock 恒底部 3 行，
            内容绝不绘入其保留区）。优先集（§二十二）恒保留：Header
            (1) / Task (1) / Collaboration / Status(2)。让位次序：
            (1) activity 尾窗整区隐藏——Trace 仍是全量出口；
            (2) detail 有界窗按余量收窄（detail_max_lines，
            (+N more · T) 诚实溢出；零余量整区隐藏）；
            (3) result 交付物最后收窄（result_max_lines ≥2——头行
            status 事实永不折入溢出行；worker 失败行只计数不收窄，
            诚实错误面不裁剪）。
            超出经 ProjectionInputs 呈现参数第二遍重建投影——确定性
            纯函数、零事实触碰；未超出时零重建、零行为差。
            返回 (state, detail_fits, activity_fits)。"""
            avail = (self.size.height or 24) - _DOCK_ROWS
            fixed = 2 + len(state.collaboration_lines) + 2
            rem = avail - fixed
            # result 先占位（空态由调用方整区隐藏；非空保留至余量）
            if result_rows > rem:
                values.result_max_lines = max(rem, 2)
                result_rows = min(result_rows, values.result_max_lines)
            rem -= result_rows
            # detail 次之：余量内收窄，零/负余量整区隐藏
            detail_fits = rem >= 1
            detail_kept = 0
            if detail_fits:
                detail_kept = min(len(state.detail_lines), rem)
                if detail_kept < len(state.detail_lines):
                    values.detail_max_lines = detail_kept
            # activity 最后：让位即整区隐藏（§二十二 门规同型）
            rem -= detail_kept
            activity_fits = rem >= len(state.activity_lines)
            # 仅在发生收窄时重建（第二遍纯函数；其余字段逐字节不变）
            if (values.detail_max_lines is not None
                    or values.result_max_lines is not None):
                state = build_projection(values)
            return state, detail_fits, activity_fits

        # ------------------------------------------------ dock 呈现

        def _dock_controls_line(self) -> str:
            """Controls 行：键位提示（Lifecycle × 交互态派生）。

            回执已分离至独立 receipt 行；提示只呈现当前真实可用的
            键——RUNNING 不提示 R（受理必 NO_OP）、停驻态不提示 P、
            终态只剩 T/Q；≥140 列追加 [C]ontext（面板真实在场）。
            R2：COMMAND 三档均含语言提示（EN 态 [L]中文 / ZH 态
            [L] EN——指向对侧语言，可发现性不依赖先验知识）；键字母
            恒 EN，动词经投影层闭集词表（单一词表真源）。
            CU-TUI-INPUT A1：漏斗期本行 = 两键提示（EN 冻结，
            funnel_keys_hint 唯一真源——与回显同处底部 dock）。"""
            if self._funnel_pre_start():
                return funnel_keys_hint(
                    ascii_only=self._cockpit_ascii)
            locale = self._cockpit_locale

            def word(key):
                return ui_label(key, locale)

            def verb(key):
                # 键字母恒 EN（ mandates §十九/§三十三：[L]中文 而非
                # [中]…）；EN 态首字母入方括号（[R]esume 惯例），
                # ZH 态完整动词跟在 EN 键字母后（[R]继续）。
                label = word(key)
                if label == key:
                    label = label[1:]
                return f"[{key[0]}]{label}"

            if self._cockpit_mode == MODE_COMPOSER:
                return word("revise · enter submit · tab target · esc cancel")
            if self._cockpit_mode == MODE_CONFIRM:
                return word("abort? · y confirm · n/esc cancel")
            lifecycle = getattr(
                getattr(self, "last_state", None), "lifecycle", None)
            language = "[L] EN" if locale == "zh" else "[L]中文"
            if lifecycle in ("PAUSED", "PARKED"):
                line = (f"{verb('Resume')} {verb('Edit')} "
                        f"{verb('Abort')} {verb('Trace')} "
                        f"{language} {verb('Quit')}")
            elif lifecycle in _TERMINAL_LIFECYCLE_HINTS:
                line = f"{verb('Trace')} {language} {verb('Quit')}"
            else:
                line = (f"{verb('Pause')} {verb('Edit')} "
                        f"{verb('Abort')} {verb('Trace')} "
                        f"{language} {verb('Quit')}")
            if self.size.width >= 140:
                line += f" {verb('Context')}"
            return line

        def _dock_receipt_line(self) -> str:
            """Receipt 行：瞬态回执（TTL 内在场，否则空行）。

            回执是控制结果的只读投影；持久控制真相唯一在
            账本（Trace CTRL tab），本行绝不成为事实源。"""
            if self._cockpit_receipt and self._cockpit_receipt_ttl > 0:
                return self._cockpit_receipt
            return ""

        def _dock_input_line(self) -> str:
            """Input 行：漏斗回显 / 命令模式标签 / 修订编辑 / 确认问题。

            CU-TUI-INPUT A1：漏斗期 = 底部 dock 回显行（光标 | 由本层
            追加——TUI-4 composer 惯例；安全门/截断在投影层）。
            CU-TUI-INPUT A3：COMMAND 态不再呈现 ">"——那是 shell 提示
            符的视觉许诺，而本态是 CU-TUI-4 冻结的单键命令面（普通
            字符 no-op）；改呈模式标签（[COMMAND]/[命令]，经闭集词
            表），零路由语义变化。"""
            if self._funnel_pre_start():
                return funnel_input_line(
                    self._cockpit_funnel_buffer,
                    width=self.size.width or 100,
                    ascii_only=self._cockpit_ascii) + "|"
            if self._cockpit_mode == MODE_COMPOSER:
                return (f"revise [{self._cockpit_revise_target}] "
                        f"{self._cockpit_revise_text}|")
            if self._cockpit_mode == MODE_CONFIRM:
                return "abort? (y/n)"
            return f"[{ui_label('command', self._cockpit_locale).upper()}]"

        # ------------------------------------------------ 意图外发（唯一通道）

        def _dispatch(self, kind, text=None, target=None):
            """DISPATCH 边界：呈现层意图 → 注入回调外发意图 → 同步回执。

            RESUME/ABORT 受理（ACCEPTED）时唤醒驱动循环的停驻等待
            ——wake 仅为私有同步原语，绝非执行真相。dispatcher 缺席
            时诚实 no-op（零回执、零伪造状态）。"""
            if self._cockpit_control is None:
                return None
            result = self._cockpit_control(kind, text=text, target=target)
            self._cockpit_receipt = control_receipt_line(
                result, kind=kind, ascii_only=self._cockpit_ascii)
            self._cockpit_receipt_ttl = _RECEIPT_TICKS
            status = getattr(result, "status", None)
            if (kind in ("RESUME", "ABORT")
                    and getattr(status, "value", status) == "ACCEPTED"):
                self._cockpit_wake.set()
            return result

        # ------------------------------------------------ 首跑漏斗（CU-TUI-5）

        def _funnel_pre_start(self) -> bool:
            """漏斗前置态谓词：Start 前的键语义专用（RUNNING 后
            内层三态接管，本谓词恒 False）。"""
            return (self._cockpit_composition_preview is not None
                    and self._cockpit_composed is None)

        def _funnel_refresh(self) -> None:
            """漏斗屏渲染：投影层首屏组装 + 瞬态状态行（no-op 提示/
            BLOCKED 原因/红行/变更横幅）。CU-TUI-INPUT A1/A2：输入
            回显与两键提示迁入底部 dock（与 RUNNING 同位——Start 前后
            输入位置恒底，顶部 body 不再有输入行）；dock 三行经
            _dock_*_line 漏斗分支供给（单一真源/行）。"""
            lines = funnel_first_screen(
                self._cockpit_version_text, self._cockpit_disclosure,
                self._cockpit_funnel_buffer,
                width=self.size.width or 100,
                ascii_only=self._cockpit_ascii,
                include_input=False)
            if self._cockpit_funnel_message:
                lines = lines + tuple(self._cockpit_funnel_message)
            self.funnel_text = "\n".join(lines)
            self.query_one("#funnel-screen").update(self.funnel_text)
            self.query_one("#dock-controls").update(
                self._dock_controls_line())
            self.query_one("#dock-receipt").update(
                self._dock_receipt_line())
            self.query_one("#dock-input").update(
                self._dock_input_line())

        def _funnel_key(self, key: str, character) -> None:
            """漏斗键语义（§十二）：Enter/Esc/Backspace 专属处理；
            q 仅空缓冲时退出（COMPOSING 中 q 为可打印字符本体）；
            可打印字符经 event.character 入缓冲（修饰组合结构性排除）。"""
            if key == "enter":
                self._funnel_enter()
                return
            if key == "escape":
                # 清空缓冲（不退出）：draft 归零、回到 NOT_STARTED
                self._cockpit_funnel_buffer = ""
                self._cockpit_stage = STAGE_NOT_STARTED
                self._cockpit_funnel_message = ()
                self._funnel_refresh()
                return
            if key == "backspace":
                self._cockpit_funnel_buffer = \
                    self._cockpit_funnel_buffer[:-1]
                if not self._cockpit_funnel_buffer:
                    self._cockpit_stage = STAGE_NOT_STARTED
                self._funnel_refresh()
                return
            if key == "q" and not self._cockpit_funnel_buffer:
                # 前置态直接退出 exit 0（零执行、零事件、零确认）
                self.exit()
                return
            if character is not None and len(character) == 1:
                self._cockpit_funnel_buffer += character
                self._cockpit_stage = STAGE_COMPOSING
                self._cockpit_funnel_message = ()
                self._funnel_refresh()
                return
            # 其余（修饰组合/功能键）no-op

        def _funnel_enter(self) -> None:
            """Enter 判定（§十二顺序）：空白 no-op 提示 → 预览 BLOCKED
            原因+hint → 就绪才经注入闭包 Start（真相零进本层）。"""
            buffer = self._cockpit_funnel_buffer
            feedback = funnel_enter_lines(self._cockpit_disclosure, buffer)
            if feedback:
                self._cockpit_funnel_message = feedback
                self._funnel_refresh()
                return
            result = self._cockpit_start_composition(
                buffer, self._cockpit_disclosure)
            if hasattr(result, "drive"):
                # ComposedRun（duck 判别：注入闭包的三种结果值对象
                # 字段互斥，本层零 entry import）
                self._funnel_start_success(result)
                return
            if hasattr(result, "reasons"):
                # CompositionChanged：刷新披露为活组合、横幅呈现原因、
                # 停留漏斗——再 Enter 在新披露上重估（live == 披露才启动）
                self._cockpit_disclosure = result.composition
                self._cockpit_funnel_message = funnel_changed_lines(
                    result.reasons)
                self._funnel_refresh()
                return
            # CompositionError：红行（原词汇），停留漏斗不退出
            self._cockpit_funnel_message = funnel_error_lines(result)
            self._funnel_refresh()

        def _funnel_start_success(self, composed) -> None:
            """Start 成功：late-bind 组合句柄 → 换屏 RUNNING（同一
            App、同一次 run；内层三态/P/R/E/A/X/Trace 原样接管）。"""
            self._cockpit_composed = composed
            self._cockpit_stage = STAGE_RUNNING
            self._cockpit_drive = composed.drive
            self._cockpit_task = composed.task
            self._cockpit_plan = tuple(composed.plan)
            self._cockpit_events = composed.events
            self._cockpit_facts = composed.facts
            self._cockpit_usage = composed.usage
            self._cockpit_session = composed.session
            self._cockpit_control = composed.dispatch_control
            self._cockpit_revision_pending = composed.revision_pending
            self._running_mount()

        # ------------------------------------------------ 键位（状态机）

        def on_key(self, event) -> None:
            key = getattr(event, "key", "")
            character = getattr(event, "character", None)
            if self._funnel_pre_start():
                self._funnel_key(key, character)
                return
            if self._cockpit_mode == MODE_COMPOSER:
                self._composer_key(key, character)
                return
            if self._cockpit_mode == MODE_CONFIRM:
                self._confirm_key(key)
                return
            self._command_key(key)

        def _command_key(self, key: str) -> None:
            if key == "p":
                self._dispatch("PAUSE")
                self._refresh()
            elif key == "r":
                self._dispatch("RESUME")
                self._refresh()
            elif key == "a":
                self._cockpit_mode = MODE_CONFIRM
                self._refresh()
            elif key == "escape":
                # Phase P：ESC@COMMAND 与 A 同径进入中止确认——零新
                # 语义、零绕过（本项目无 runtime 取消契约，不发明）
                self._cockpit_mode = MODE_CONFIRM
                self._refresh()
            elif key == "e":
                self._open_composer()
            elif key == "q":
                self._quit_path()
            elif key == "t":
                self.push_screen(TraceScreen())
            elif key == "c":
                self._cockpit_show_context = not self._cockpit_show_context
                self._refresh()
            elif key in ("l", "L"):
                # R2 语言切换：仅 COMMAND 主屏。TraceScreen 未绑定键
                # 会冒泡至 App——显式拦截（no-op、不刷新）；切换走
                # 零推进渲染（动画 tick/揭示/回执寿命一概不动），
                # 零外发、零事实触碰。注意先判态再判屏：composer/
                # confirm 的 l 已在各自分支消费，永不至此。
                if (self._cockpit_mode == MODE_COMMAND
                        and not isinstance(self.screen, TraceScreen)):
                    self._cockpit_locale = (
                        "zh" if self._cockpit_locale == "en" else "en")
                    self._refresh(advance_tick=False)
            elif key in ("left", "right"):
                # R1 选中移动：纯呈现态（clamp、空组合 no-op、零外发）
                if self._cockpit_plan:
                    delta = -1 if key == "left" else 1
                    self._cockpit_selected_index = max(
                        0, min(self._cockpit_selected_index + delta,
                               len(self._cockpit_plan) - 1))
                self._refresh()
            elif key in ("enter", "space"):
                # R1 展开/折叠选中 agent：绑定稳定 stage 身份（Enter
                # 与 Space 同径）；切换即自动折叠前一个（至多一个展开）
                if self._cockpit_plan:
                    index = min(self._cockpit_selected_index,
                                len(self._cockpit_plan) - 1)
                    stage = self._cockpit_plan[index][0]
                    self._cockpit_expanded_stage = (
                        None if self._cockpit_expanded_stage == stage
                        else stage)
                self._refresh()
            # 其余按键 no-op（零副作用）

        def _open_composer(self) -> None:
            self._cockpit_revise_text = ""
            self._cockpit_revise_target = _TARGETS[0]
            self._cockpit_mode = MODE_COMPOSER
            self._refresh()

        def _composer_key(self, key: str, character) -> None:
            if key == "escape":
                # 取消：丢弃缓冲、零意图外发、零事实变化
                self._cockpit_revise_text = ""
                self._cockpit_mode = MODE_COMMAND
                self._refresh()
                return
            if key == "enter":
                if (self._cockpit_control is not None
                        and self._cockpit_revise_text.strip()):
                    result = self._dispatch(
                        "REVISE", text=self._cockpit_revise_text,
                        target=self._cockpit_revise_target)
                    if result is not None:
                        self._cockpit_revise_text = ""
                        self._cockpit_mode = MODE_COMMAND
                self._refresh()
                return
            if key == "backspace":
                self._cockpit_revise_text = self._cockpit_revise_text[:-1]
                self._refresh()
                return
            if character is not None and len(character) == 1:
                # 可打印字符本体（含标点/空格；修饰组合与非打印键为
                # None，结构性排除——零猜测映射）
                self._cockpit_revise_text += character
                self._refresh()
            # 其余（修饰组合/功能键）no-op

        def _confirm_key(self, key: str) -> None:
            if key == "y":
                self._dispatch("ABORT")
                self._cockpit_mode = MODE_COMMAND
                self._refresh()
            elif key in ("n", "escape"):
                self._cockpit_mode = MODE_COMMAND
                self._refresh()
            # 其余按键 no-op（零外发、零状态变化）

        def action_cockpit_tab(self) -> None:
            """composer 内 target 封闭二选一切换；其余态 no-op。"""
            if self._cockpit_mode == MODE_COMPOSER:
                other = [item for item in _TARGETS
                         if item != self._cockpit_revise_target]
                self._cockpit_revise_target = other[0]
                self._refresh()

        def action_cockpit_quit(self) -> None:
            """Ctrl-C 与 q 同径（冻结决策）。"""
            self._quit_path()

        def _quit_path(self) -> None:
            """q 阶梯：终态直退；ABORT 已受理待兑现 → 硬弃界面；
            其余非终态 → 破坏性确认。硬弃只放弃界面，进程收尾
            仍诚实等待 driver 完成。漏斗前置态（§十二）：直接退出
            exit 0——零执行在场，任务文本损失可接受，无确认。"""
            if self._funnel_pre_start():
                self.exit()
                return
            session = self._cockpit_session
            terminal = None if session is None else session.terminal
            if terminal is not None or self._abort_awaited():
                # Phase P：先请求驱动循环收尾再退 UI——停驻等待中的
                # worker 经旗标返回，join 不悬挂（进程仍诚实等待在飞
                # invocation 自然收尾，绝不 kill）
                self._cockpit_exit_requested = True
                self._cockpit_wake.set()
                self.exit()
                return
            self._cockpit_mode = MODE_CONFIRM
            self._refresh()

        def _abort_awaited(self) -> bool:
            """账本事实面：ABORT_REQUESTED 在场即等待真实兑现。"""
            for entry in self._cockpit_facts() or ():
                if _fact_kind(entry) == "ABORT_REQUESTED":
                    return True
            return False

        # ------------------------------------------------ 驱动（唯一执行点）

        def _drive_loop(self) -> None:
            """worker 线程：驱动循环（App lifetime ≠ 段执行 lifetime）。

            PARKED 停驻等待注入回执唤醒（RESUME/ABORT 受理 → 再驱动，
            续走真相全在注入闭包持有的续走值里）；terminal 呈现保留
            ——绝不因 RunOutcome 到达而退出 App（退出只经用户 q/Ctrl-C
            阶梯）。残余异常诚实捕获呈现（self.failure），App 存活；
            退出路径按既有外壳语义如实上抛，绝不吞掉。"""
            while True:
                try:
                    outcome = self._cockpit_drive()
                except BaseException as error:  # 诚实失败，绝不伪装成功
                    self.failure = error
                    self.call_from_thread(self._refresh)
                    return
                self.outcome = outcome
                status = getattr(outcome, "status", None)
                if getattr(status, "value", status) == "PARKED":
                    self.call_from_thread(self._refresh)
                    # 停驻等待：wake 唤醒续驱；退出旗标优先返回（携
                    # 最近真实 PARKED outcome，rc 契约不变）
                    while not self._cockpit_wake.wait(0.25):
                        if self._cockpit_exit_requested:
                            return
                    self._cockpit_wake.clear()
                    if self._cockpit_exit_requested:
                        return
                    continue
                if self._cockpit_composition_preview is not None:
                    # 漏斗外层呈现态收尾（TUI 私有，零引擎写入）
                    self._cockpit_stage = STAGE_TERMINAL
                self.call_from_thread(self._refresh)
                return

        def wait_for_driver(self) -> None:
            """App 退出后同步收尾 worker（结果绝不丢失）。"""
            if self._cockpit_thread is not None:
                self._cockpit_thread.join()

    class TraceScreen(Screen):  # type: ignore[misc]
        """全屏四 Tab：OBS / CTRL / USAGE / AGENTS（事实源原样投影）。

        选择/跟随/展开均为呈现态：selected 恒为事件自身 sequence
        （稳定标识，绝非列表下标）；follow 断开后新事件既不抢滚动
        也不抢选择，钉住状态行呈现 "<N> new events" 计数；刷新只
        消费只读快照。"""

        CSS = "#trace-status { height: 1; }"

        BINDINGS = [
            ("escape", "back", "Back"),
            ("up", "select_prev", "Prev"),
            ("down", "select_next", "Next"),
            ("g", "follow_tail", "Tail"),
            ("x", "toggle_expanded", "Expand"),
            ("enter", "toggle_expanded", "Detail"),
            ("pageup", "scroll_page_up", "PgUp"),
            ("pagedown", "scroll_page_down", "PgDn"),
            ("home", "scroll_top", "Top"),
            ("end", "scroll_bottom", "End"),
        ]

        _TAB_SCROLL = {"tab-obs": "scroll-obs",
                       "tab-ctrl": "scroll-ctrl",
                       "tab-usage": "scroll-usage",
                       "tab-agents": "scroll-agents"}

        # 呈现态默认值（实例赋值后各自独立）
        _trace_follow = True
        _trace_selected_seq = None
        _trace_expanded = False
        _trace_seen_base = None

        def compose(self):
            yield Static("", id="trace-status")
            with TabbedContent(id="trace-tabs"):
                with TabPane("OBS", id="tab-obs"):
                    yield Static("", id="obs-detail")
                    with VerticalScroll(id="scroll-obs", can_focus=False):
                        yield Static("", id="trace-obs")
                with TabPane("CTRL", id="tab-ctrl"):
                    with VerticalScroll(id="scroll-ctrl", can_focus=False):
                        yield Static("", id="trace-ctrl")
                with TabPane("USAGE", id="tab-usage"):
                    with VerticalScroll(id="scroll-usage", can_focus=False):
                        yield Static("", id="trace-usage")
                with TabPane("AGENTS", id="tab-agents"):
                    with VerticalScroll(id="scroll-agents", can_focus=False):
                        yield Static("", id="trace-agents")

        def on_mount(self) -> None:
            self._trace_refresh()
            # live 投影：与主界面同节拍的数据驱动刷新
            self.set_interval(0.5, self._trace_refresh)

        # ------------------------------------------------ 投影刷新

        def _events_now(self):
            return self.app._collect_inputs().events

        def _trace_refresh(self) -> None:
            app = self.app
            # follow 检测在内容更新前：用户已离开底部（滚轮/翻页）
            # 则停止钉底，绝不抢回滚动位置
            container = self._active_scroll()
            if container is not None and self._trace_follow:
                maximum = container.max_scroll_y
                if maximum and float(container.scroll_y) < (
                        float(maximum) - 0.5):
                    self._follow_off()
            values = app._collect_inputs()
            state = build_projection(values)
            events = values.events
            seqs = [getattr(event, "sequence", None) for event in events]
            selected = self._trace_selected_seq
            marker_index = (seqs.index(selected)
                            if selected in seqs else None)
            obs_lines = []
            for index, line in enumerate(state.trace_obs):
                obs_lines.append(
                    ("▸ " if index == marker_index else "  ") + line)
            self.observation_text = "\n".join(obs_lines)
            self._update_static("#trace-obs", self.observation_text)
            detail = ""
            if marker_index is not None:
                detail = "\n".join(event_detail_line(events[marker_index]))
            self.detail_text = detail
            self._update_static("#obs-detail", detail)
            pending = None
            if app._cockpit_revision_pending is not None:
                pending = app._cockpit_revision_pending()
            self.ctrl_text = "\n".join(
                state.trace_ctrl + ("",)
                + revision_status_lines(values.facts, pending))
            self._update_static("#trace-ctrl", self.ctrl_text)
            self.usage_text = "\n".join(state.trace_usage)
            self._update_static("#trace-usage", self.usage_text)
            self.agents_text = "\n".join(agent_detail(
                values.slots, values.events, values.usage_records,
                values.last_outcome, expanded=self._trace_expanded,
                width=values.width, ascii_only=values.ascii_only))
            self._update_static("#trace-agents", self.agents_text)
            # 钉住状态行（§二十）：跟随中空行；钉住时呈现新事件计数
            # （follow/seen_base 均为呈现态，绝非事实）
            top_seq = max(
                (seq for seq in seqs if seq is not None), default=-1)
            if self._trace_follow:
                self._trace_seen_base = top_seq
                new_count = 0
            else:
                if self._trace_seen_base is None:
                    self._trace_seen_base = top_seq
                new_count = max(0, top_seq - self._trace_seen_base)
            self.trace_status_text = trace_status_line(
                self._trace_follow, new_count,
                ascii_only=getattr(app, "_cockpit_ascii", False))
            self._update_static("#trace-status", self.trace_status_text)
            if self._trace_follow:
                target = self._active_scroll()
                if target is not None:
                    target.scroll_end(animate=False)

        def _update_static(self, selector: str, text: str) -> None:
            """仅内容变化时更新 DOM（数据驱动，零闪烁）。"""
            widget = self.query_one(selector)
            if getattr(widget, "_cockpit_last", None) != text:
                widget._cockpit_last = text
                widget.update(text)

        def _active_scroll(self):
            tabs = self.query_one("#trace-tabs")
            active = getattr(tabs, "active", None)
            selector = self._TAB_SCROLL.get(active, "scroll-obs")
            return self.query_one(f"#{selector}")

        # ------------------------------------------------ 选择（稳定 seq）

        def _top_sequence(self) -> int:
            """当前事件流最大序列（钉住基线的取数面）。"""
            seqs = [getattr(event, "sequence", None)
                    for event in self._events_now()]
            seqs = [seq for seq in seqs if seq is not None]
            return max(seqs) if seqs else -1

        def _follow_off(self) -> None:
            """跟随 → 钉住的一次性跃迁：此刻起记录新事件计数基线。"""
            if self._trace_follow:
                self._trace_follow = False
                self._trace_seen_base = self._top_sequence()

        def action_select_prev(self) -> None:
            seqs = [getattr(event, "sequence", None)
                    for event in self._events_now()]
            if not seqs:
                return
            if self._trace_selected_seq not in seqs:
                self._trace_selected_seq = seqs[-1]
            else:
                index = seqs.index(self._trace_selected_seq)
                if index > 0:
                    self._trace_selected_seq = seqs[index - 1]
            self._follow_off()
            self._trace_refresh()

        def action_select_next(self) -> None:
            seqs = [getattr(event, "sequence", None)
                    for event in self._events_now()]
            if not seqs:
                return
            if self._trace_selected_seq not in seqs:
                self._trace_selected_seq = seqs[-1]
            else:
                index = seqs.index(self._trace_selected_seq)
                if index < len(seqs) - 1:
                    self._trace_selected_seq = seqs[index + 1]
            self._trace_refresh()

        # ------------------------------------------------ 跟随 / 展开 / 滚动

        def action_follow_tail(self) -> None:
            self._trace_follow = True
            self._trace_refresh()

        def action_toggle_expanded(self) -> None:
            self._trace_expanded = not self._trace_expanded
            self._trace_refresh()

        def action_scroll_page_up(self) -> None:
            self._follow_off()
            self._active_scroll().scroll_page_up(animate=False)

        def action_scroll_page_down(self) -> None:
            self._active_scroll().scroll_page_down(animate=False)

        def action_scroll_top(self) -> None:
            self._follow_off()
            self._active_scroll().scroll_home(animate=False)

        def action_scroll_bottom(self) -> None:
            self._trace_follow = True
            self._active_scroll().scroll_end(animate=False)

        def action_back(self) -> None:
            self.app.pop_screen()


def __getattr__(name):
    """PEP 562：App 类惰性物化（首次访问才 import UI 框架）。"""
    if name in ("CockpitApp", "TraceScreen"):
        _build_classes()
        return globals()[name]
    raise AttributeError(f"module has no attribute {name!r}")


def run_cockpit_tui(*, driver, task, plan, events, facts, usage,
                    session, control=None, revision_pending=None):
    """同步外壳：驱动 App、收尾 worker、诚实返回/上抛（G16）。"""
    _build_classes()
    app = CockpitApp(driver=driver, task=task, plan=plan, events=events,
                     facts=facts, usage=usage, session=session,
                     control=control, revision_pending=revision_pending)
    app.run()
    app.wait_for_driver()
    if app.failure is not None:
        raise app.failure
    return app.outcome


def run_cockpit_funnel(*, composition_preview, start_composition,
                       task_token=None, timeout_seconds=None):
    """漏斗同步外壳（CU-TUI-5 §十六）：单一 App、单次 run()——
    漏斗为初始呈现阶段，Start 后同一 App 换屏 RUNNING。

    前置退出（q/Ctrl-C，未 Start）→ outcome None（零执行零交付）；
    timeout 真值已由入口闭包捕获，本参数仅路由对齐。组合真相只经
    注入的 preview/start 两闭包进出。"""
    _build_classes()
    app = CockpitApp(composition_preview=composition_preview,
                     start_composition=start_composition,
                     task_token=task_token,
                     timeout_seconds=timeout_seconds)
    app.run()
    app.wait_for_driver()
    if app.failure is not None:
        raise app.failure
    return app.outcome
