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
worker 线程唯一执行注入的 driver；刷新为数据驱动重投影（零动画、
零 spinner、零伪造活动）。漏斗态无 interval——刷新为纯键事件驱动。
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
    event_detail_line,
    funnel_changed_lines,
    funnel_enter_lines,
    funnel_error_lines,
    funnel_first_screen,
    revision_status_lines,
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

# RUNNING 主界面区选择器（漏斗态整组隐藏，Start 后整组复现）。
_MAIN_ZONE_SELECTORS = (
    "#header-zone", "#task-zone", "#collab-zone", "#progress-zone",
    "#result-zone", "#input-dock", "#context-panel")

# 回执有界寿命：按刷新次数衰减（零时钟，确定性）。
_RECEIPT_TICKS = 6


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

        CSS = """
        #funnel-screen { display: none; }
        #input-dock { dock: bottom; height: 2; }
        #dock-controls { height: 1; }
        #dock-input { height: 1; }
        #context-panel { dock: right; width: 28; display: none; }
        #header-zone { height: 1; }
        #result-zone { height: auto; }
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
            yield Static("", id="funnel-screen")
            yield Static("", id="header-zone")
            yield Static("", id="task-zone")
            yield Static("", id="collab-zone")
            yield Static("", id="progress-zone")
            yield Static("", id="result-zone")
            with Container(id="input-dock"):
                yield Static("", id="dock-controls")
                yield Static("", id="dock-input")
            yield Static("", id="context-panel")

        def on_mount(self) -> None:
            if self._cockpit_composition_preview is not None:
                self._funnel_mount()
                return
            self._running_mount()

        def _funnel_mount(self) -> None:
            """漏斗初始呈现：主界面六区隐藏，单一漏斗屏在场。"""
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
                target=self._drive, daemon=True)
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

        def _refresh(self) -> None:
            """READ → PROJECT → RENDER（UI 线程执行）。"""
            state = build_projection(self._collect_inputs())
            self.last_state = state
            self.query_one("#header-zone").update(state.header_line)
            self.query_one("#task-zone").update(
                f"{state.task_line}   {state.badge}")
            self.query_one("#collab-zone").update(
                "\n".join(state.collaboration_lines))
            self.query_one("#progress-zone").update(
                f"{state.progress_line}\n{state.tokens_line}")
            self.query_one("#result-zone").update(
                "\n".join(state.result_lines))
            self.query_one("#dock-controls").update(
                self._dock_controls_line())
            self.query_one("#dock-input").update(self._dock_input_line())
            panel = self.query_one("#context-panel")
            wide = self.size.width >= 140
            if state.context_lines and wide and self._cockpit_show_context:
                panel.update("\n".join(state.context_lines))
                panel.display = True
            else:
                panel.display = False
            # 回执有界衰减（按刷新次数，零时钟）
            if self._cockpit_receipt_ttl > 0:
                self._cockpit_receipt_ttl -= 1
                if self._cockpit_receipt_ttl == 0:
                    self._cockpit_receipt = None

        # ------------------------------------------------ dock 呈现

        def _dock_controls_line(self) -> str:
            """Controls 行双职责：回执在场时回显，否则键位提示。"""
            if self._cockpit_receipt:
                return self._cockpit_receipt
            return "[P]ause [R]esume [E]dit [A]bort [T]race [Q]uit"

        def _dock_input_line(self) -> str:
            """Input 行三态：命令提示 / 修订编辑 / 确认问题。"""
            if self._cockpit_mode == MODE_COMPOSER:
                return (f"revise [{self._cockpit_revise_target}] "
                        f"{self._cockpit_revise_text}|")
            if self._cockpit_mode == MODE_CONFIRM:
                return "abort? (y/n)"
            return ">"

        # ------------------------------------------------ 意图外发（唯一通道）

        def _dispatch(self, kind, text=None, target=None):
            """DISPATCH 边界：注入回调外发意图 → 同步回执原样呈现。

            dispatcher 缺席时诚实 no-op（零回执、零伪造状态）。"""
            if self._cockpit_control is None:
                return None
            result = self._cockpit_control(kind, text=text, target=target)
            self._cockpit_receipt = control_receipt_line(result)
            self._cockpit_receipt_ttl = _RECEIPT_TICKS
            return result

        # ------------------------------------------------ 首跑漏斗（CU-TUI-5）

        def _funnel_pre_start(self) -> bool:
            """漏斗前置态谓词：Start 前的键语义专用（RUNNING 后
            内层三态接管，本谓词恒 False）。"""
            return (self._cockpit_composition_preview is not None
                    and self._cockpit_composed is None)

        def _funnel_refresh(self) -> None:
            """漏斗屏渲染：投影层首屏组装 + 瞬态状态行（no-op 提示/
            BLOCKED 原因/红行/变更横幅）。输入行尾 | 为光标呈现
            （TUI-4 composer 同款惯例）。"""
            lines = funnel_first_screen(
                self._cockpit_version_text, self._cockpit_disclosure,
                self._cockpit_funnel_buffer + "|",
                width=self.size.width or 100,
                ascii_only=self._cockpit_ascii)
            if self._cockpit_funnel_message:
                lines = lines + tuple(self._cockpit_funnel_message)
            self.funnel_text = "\n".join(lines)
            self.query_one("#funnel-screen").update(self.funnel_text)

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
            elif key == "e":
                self._open_composer()
            elif key == "q":
                self._quit_path()
            elif key == "t":
                self.push_screen(TraceScreen())
            elif key == "c":
                self._cockpit_show_context = not self._cockpit_show_context
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

        def _drive(self) -> None:
            """worker 线程：调用入口注入的 driver，如实记录结果。"""
            try:
                outcome = self._cockpit_drive()
            except BaseException as error:  # 诚实失败，绝不伪装成功
                self.failure = error
                self.call_from_thread(self.exit)
                return
            self.outcome = outcome
            if self._cockpit_composition_preview is not None:
                # 漏斗外层呈现态收尾（TUI 私有，零引擎写入）
                self._cockpit_stage = STAGE_TERMINAL
            self.call_from_thread(self._refresh)
            self.call_from_thread(self.exit)

        def wait_for_driver(self) -> None:
            """App 退出后同步收尾 worker（结果绝不丢失）。"""
            if self._cockpit_thread is not None:
                self._cockpit_thread.join()

    class TraceScreen(Screen):  # type: ignore[misc]
        """全屏四 Tab：OBS / CTRL / USAGE / AGENTS（事实源原样投影）。

        选择/跟随/展开均为呈现态：selected 恒为事件自身 sequence
        （稳定标识，绝非列表下标）；follow 断开后新事件既不抢滚动
        也不抢选择；刷新只消费只读快照。"""

        BINDINGS = [
            ("escape", "back", "Back"),
            ("up", "select_prev", "Prev"),
            ("down", "select_next", "Next"),
            ("g", "follow_tail", "Tail"),
            ("x", "toggle_expanded", "Expand"),
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

        def compose(self):
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
                    self._trace_follow = False
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
            self._trace_follow = False
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
            self._trace_follow = False
            self._active_scroll().scroll_page_up(animate=False)

        def action_scroll_page_down(self) -> None:
            self._active_scroll().scroll_page_down(animate=False)

        def action_scroll_top(self) -> None:
            self._trace_follow = False
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
