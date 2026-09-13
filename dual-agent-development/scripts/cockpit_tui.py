"""CU-TUI-3 (V3.2): Textual 协作驾驶舱 — 只读呈现 App。

G3/G4 hard rules（授权 §七/§九）：

- UI 框架的 import 只存在于本模块内部，且为惰性——模块级导入零
  框架依赖（App/TraceScreen 类经 __getattr__ 在首次访问时构建），
  cockpit_entry 只尝试导入本模块，绝不直接触碰框架；
- 本层只 READ → PROJECT → RENDER：执行驱动唯一来自入口注入的
  driver 回调（worker 线程内调用，App 退出后同步收尾）；本层
  绝不提交控制命令、绝不调用段执行、绝不直接驱动任何运行时
  组件、不持有第二份执行真相——lifecycle/usage/observation 全部
  经 cockpit_projection 从既有事实源纯投影；
- G16：依赖缺席的回退发生在入口层（textual_available）；
  本层真实异常诚实上抛/如实呈现失败，绝不伪装成功。

线程模型（单 Session）：UI 线程只做只读投影与渲染；worker 线程
唯一执行注入的 driver；刷新为数据驱动重投影（零动画、零 spinner、
零闪烁、零伪造活动）。
"""
from __future__ import annotations

import sys
import threading

from cockpit_projection import (
    AgentSlotView,
    ProjectionInputs,
    build_projection,
)
from event_index import EventIndex

__all__ = ("CockpitApp", "TraceScreen", "new_event_store",
           "run_cockpit_tui", "textual_available")


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


def _build_classes() -> None:
    """首次访问 CockpitApp / TraceScreen 时构建类（惰性框架 import）。

    类构建后写入模块全局，后续访问走正常属性查找；构建动作幂等。
    """
    global CockpitApp, TraceScreen
    if globals().get("CockpitApp") is not None:
        return

    from textual.app import App
    from textual.containers import Container
    from textual.screen import Screen
    from textual.widgets import Static, TabbedContent, TabPane

    class CockpitApp(App):  # type: ignore[misc]
        """主界面：六区 + 固定底 dock + （≥140 列）Context 面板。"""

        CSS = """
        #input-dock { dock: bottom; height: 2; }
        #dock-controls { height: 1; }
        #dock-input { height: 1; }
        #context-panel { dock: right; width: 28; display: none; }
        #header-zone { height: 1; }
        #result-zone { height: auto; }
        """

        BINDINGS = [
            ("t", "show_trace", "Trace"),
            ("c", "toggle_context", "Context"),
            ("q", "quit_ui", "Quit"),
        ]

        def __init__(self, *, driver, task, plan, events, facts, usage,
                     session):
            super().__init__()
            self._cockpit_drive = driver
            self._cockpit_task = task
            self._cockpit_plan = tuple(plan)
            self._cockpit_events = events
            self._cockpit_facts = facts
            self._cockpit_usage = usage
            self._cockpit_session = session
            self.outcome = None
            self.failure = None
            self._cockpit_thread = None
            self._cockpit_ascii = _ascii_preferred()
            self._cockpit_show_context = True

        # ------------------------------------------------ 布局

        def compose(self):
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
            # G9：控制键在 CU-TUI-3 仅呈现提示（未接线，TUI-4 激活）
            self.query_one("#dock-controls").update(
                "[P]ause [R]esume [E]dit [A]bort [T]race [Q]uit")
            self.query_one("#dock-input").update(">")
            panel = self.query_one("#context-panel")
            wide = self.size.width >= 140
            if state.context_lines and wide and self._cockpit_show_context:
                panel.update("\n".join(state.context_lines))
                panel.display = True
            else:
                panel.display = False

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
            self.call_from_thread(self._refresh)
            self.call_from_thread(self.exit)

        def wait_for_driver(self) -> None:
            """App 退出后同步收尾 worker（结果绝不丢失）。"""
            if self._cockpit_thread is not None:
                self._cockpit_thread.join()

        # ------------------------------------------------ 键位（呈现层导航）

        def action_show_trace(self) -> None:
            self.push_screen(TraceScreen())

        def action_toggle_context(self) -> None:
            self._cockpit_show_context = not self._cockpit_show_context
            self._refresh()

        def action_quit_ui(self) -> None:
            self.exit()

    class TraceScreen(Screen):  # type: ignore[misc]
        """全屏三 Tab：OBS / CTRL / USAGE（既有事实源原样投影）。"""

        BINDINGS = [("escape", "back", "Back")]

        def compose(self):
            with TabbedContent():
                with TabPane("OBS"):
                    yield Static("", id="trace-obs")
                with TabPane("CTRL"):
                    yield Static("", id="trace-ctrl")
                with TabPane("USAGE"):
                    yield Static("", id="trace-usage")

        def on_mount(self) -> None:
            state = build_projection(self.app._collect_inputs())
            self.observation_text = "\n".join(state.trace_obs)
            self.query_one("#trace-obs").update(self.observation_text)
            self.query_one("#trace-ctrl").update(
                "\n".join(state.trace_ctrl))
            self.query_one("#trace-usage").update(
                "\n".join(state.trace_usage))

        def action_back(self) -> None:
            self.app.pop_screen()


def __getattr__(name):
    """PEP 562：App 类惰性物化（首次访问才 import UI 框架）。"""
    if name in ("CockpitApp", "TraceScreen"):
        _build_classes()
        return globals()[name]
    raise AttributeError(f"module has no attribute {name!r}")


def run_cockpit_tui(*, driver, task, plan, events, facts, usage,
                    session):
    """同步外壳：驱动 App、收尾 worker、诚实返回/上抛（G16）。"""
    _build_classes()
    app = CockpitApp(driver=driver, task=task, plan=plan, events=events,
                     facts=facts, usage=usage, session=session)
    app.run()
    app.wait_for_driver()
    if app.failure is not None:
        raise app.failure
    return app.outcome
