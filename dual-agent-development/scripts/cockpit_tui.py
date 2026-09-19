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
- 交互模型恰三态（CU-TUI-4 C2）——UX2-R1（C2-R2）将三态降级为
  内部实现概念：常驻 Persistent Composer（唯一文本输入面、唯一
  focusable widget）从启动到退出在场，普通文字即文字，命令降权
  为空缓冲 hidden shortcuts；MODE_COMMAND/REVISION_COMPOSER/
  ABORT_CONFIRM 三词永不外露（MODE_CONFIRM 保持——确认条仅消费
  y/n/escape，其余键仍归 composer）；
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

from cockpit_input import (
    INTENT_COMMAND,
    INTENT_REVISION,
    PHASE_BETWEEN_RUNS,
    SLASH_REGISTRY,
    classify_submit,
    parse_slash,
    slash_candidates,
    slash_help_lines,
)
from cockpit_projection import (
    AgentSlotView,
    ProjectionInputs,
    agent_detail,
    apply_budget_narrowing,
    build_projection,
    COMPOSITION_ROLES,
    compose_keys_hint,
    compose_screen_lines,
    control_receipt_line,
    derive_lifecycle,
    event_detail_line,
    funnel_changed_lines,
    funnel_enter_lines,
    funnel_error_lines,
    funnel_first_screen,
    funnel_keys_hint,
    funnel_prefill_text,
    revision_status_lines,
    run_divider_line,
    runs_summary_lines,
    trace_control_lines,
    trace_observation_lines,
    trace_status_line,
    trace_usage_lines,
    ui_label,
    worker_failure_lines,
)
from event_index import EventIndex

__all__ = ("CockpitApp", "TraceScreen", "new_event_store",
           "run_cockpit_tui", "run_cockpit_funnel", "textual_available",
           "STAGE_NOT_STARTED", "STAGE_COMPOSING", "STAGE_RUNNING",
           "STAGE_TERMINAL")


# 交互三态（CU-TUI-4 C2 裁决：恰此三态，无第四模式）。UX2-R1
# （C2-R2）：三态全部降级为内部实现概念——常驻 composer 使
# MODE_COMMAND/MODE_COMPOSER 的区分结构性消失（composer 不是
# mode，是布局基座；常量保留为词汇与 guard 测试面），用户可见
# 面零 mode 名；MODE_CONFIRM 保持（确认条消费 y/n/escape，其余
# 键仍归 composer——DESIGN LOCK v1.1 §3）。
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

# UX2-R1（DESIGN LOCK v1.1 原则 5）：普通文字就是文字，命令降权为
# 空缓冲 hidden shortcuts——composer 缓冲为空时这些裸键保持既有
# 命令语义（六键契约降权不降义）；缓冲非空时全部归文本本体
# （"quick fix" 的 q、"parse" 的 p 均为文字）。UX2-R2：e 召回最近
# 提交供改写（REVISION 语义——gate=空缓冲 ∧ 存在历史提交），
# 不入裸命令键集（无历史提交时 e 是文字本体）。
_BARE_COMMAND_KEYS = frozenset(("p", "r", "a", "q", "t", "c", "l", "L",
                                "H"))

# 2.8-B H 显式横滚模式：←/→ 单次滚动的列步长（呈现参数——与
# tier/截断律完全正交）。
_H_SCROLL_STEP = 8

# RUNNING 观察区选择器（漏斗态整组隐藏，Start 后整组复现）。
# CU-TUI-INPUT A1/A2 + UX2-R1：#input-dock（composer）恒在场
# （漏斗与 RUNNING 同一底部 dock——Start 前后输入位置恒底，绝不
# 跳变），故不在隐藏组内；#collab-log 属主屏观察区（漏斗态隐藏）。
# 2.8-B：协作管线迁入 #collab-scroll（HorizontalScroll——H 显式
# 横滚模式的 viewport 容器；常规态内容已按宽截断恒无溢出 = 零
# 视觉差）。容器与内层 #collab-zone 成对入组整组隐藏/复现（Static
# 本体 display 随组翻转——既有测试断言面零迁移）。#collab-zone
# width:auto = Static 按内容定宽（默认填充容器宽会把超宽行软换行、
# 溢出永不成立——H 滚动将结构性失效；截断律下常规态内容宽 ≤
# 容器宽 = 零布局差）。
_MAIN_ZONE_SELECTORS = (
    "#header-zone", "#task-zone", "#collab-scroll", "#collab-zone",
    "#detail-zone", "#activity-zone", "#result-zone", "#progress-zone",
    "#collab-log", "#context-panel")

# UX2-R1：composer 行数上限（内部滚动，绝不外撑破坏布局）与
# Collaboration Log ring 上限（D 裁决：ring 仅淘汰显示行——有损
# 派生缓存，真相在引擎三源 + TraceScreen）。
_COMPOSER_MAX_ROWS = 5
_LOG_RING_LINES = 1000

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
    from textual.containers import (Container, HorizontalScroll,
                                    VerticalScroll)
    from textual.screen import Screen
    from textual.widgets import Static, TabbedContent, TabPane, TextArea
    from textual.widgets import RichLog

    class CockpitComposer(TextArea):  # type: ignore[misc]
        """UX2-R1 持久转向 composer（DESIGN LOCK v1.1 §5）：唯一文本
        输入面、唯一 focusable widget——从漏斗 task 输入到终态恒在场、
        恒钉底、恒持焦点（真 caret → 终端 IME 候选窗锚定输入位）。

        键语义（全部先于 TextArea 原生处理，prevent_default+stop）：
        - Enter 恒提交（漏斗态=task 判定，Start 后=STEER 提交）；
          Ctrl+J / Shift+Enter = 换行（B 裁决：Ctrl+J 字节级 LF 恒
          可达为保底主键，Alt+Enter 终端可达处同义——多终端差异）；
        - 空缓冲裸键 = hidden shortcuts（_BARE_COMMAND_KEYS +
          ←/→ 选中 + space 展开——R1 契约降权不降义）；缓冲非空时
          一切归文本（含 p/q 字母本体）；
        - MODE_CONFIRM：y/n/escape 由确认条消费，其余仍归 composer。
        typing 全程零投影（TextArea 自渲染；AC：typing 不触发完整
        projection——CU-PERF-1 边界延续）。
        """

        async def _on_key(self, event) -> None:
            key = getattr(event, "key", "")
            app = self.app
            if app._funnel_pre_start():
                if app._cockpit_mode == MODE_CONFIRM:
                    # 2.8-A 轮间域 slash（如 /abort）的确认条消费——
                    # y/n/escape 归确认条，其余归 composer（首跑漏斗
                    # 无 slash 面此分支结构性不可达 = 零首跑行为差）
                    if key in ("y", "n", "escape"):
                        event.prevent_default()
                        event.stop()
                        app._confirm_key(key)
                        return
                    await super()._on_key(event)
                    return
                if app._cockpit_compose_active:
                    await self._compose_key(event, key)
                    return
                if key == "c":
                    # CU-COCKPIT-1：c 进入 COMPOSE（导航键、可逆——
                    # 与 q 的空缓冲门不同恒生效；残余=task 裸小写 c
                    # 需大写 C/粘贴/prefill，q 先例同类已接受）
                    event.prevent_default()
                    event.stop()
                    app._compose_enter_screen()
                    return
                await self._funnel_key(event, key)
                return
            if app._cockpit_mode == MODE_CONFIRM:
                if key in ("y", "n", "escape"):
                    event.prevent_default()
                    event.stop()
                    app._confirm_key(key)
                    return
                await super()._on_key(event)
                return
            if key == "enter":
                event.prevent_default()
                event.stop()
                app._composer_submit()
                return
            if key in ("ctrl+j", "shift+enter"):
                event.prevent_default()
                event.stop()
                self.insert("\n")
                return
            if key == "escape":
                # 清稿（单一层级——常驻 composer 无"退出"目标）；
                # 同时撤销 E 召回态（ESC = 放弃改写）
                event.prevent_default()
                event.stop()
                app._cockpit_revision_recall = False
                if self.text:
                    self.text = ""
                    app._refresh_dock()
                return
            if (not self.text and key == "e"
                    and app._cockpit_last_submit):
                # UX2-R2：E 召回（空缓冲且存在历史提交）——载入最近
                # 提交原文供改写，下次 Enter 分类 REVISION；无历史提交
                # 时 e 是文字本体（gate 保护）。终态/Trace 顶由调用
                # 面收敛（hint 不呈现 [E]dit；Trace 顶焦点不在 composer）。
                event.prevent_default()
                event.stop()
                app._revision_recall()
                return
            if (not self.text and key in _BARE_COMMAND_KEYS
                    and event.character is not None):
                event.prevent_default()
                event.stop()
                app._bare_key(key)
                return
            if not self.text and key in ("left", "right", "space"):
                # 空缓冲导航/展开（R1 契约：←/→ 选中、space 展开；
                # Enter 已归提交——空提交 no-op 既有语义）
                event.prevent_default()
                event.stop()
                app._pipeline_nav_key(key)
                return
            await super()._on_key(event)

        async def _funnel_key(self, event, key: str) -> None:
            """F-R1：漏斗 task 输入 = composer 本体（判定逻辑零改动，
            仅输入面迁移）。q 仅空缓冲退出、ESC 清稿不退出、可打印
            字符原生入缓冲——CU-TUI-5 §十二语义逐字保持。"""
            app = self.app
            if key == "enter":
                event.prevent_default()
                event.stop()
                app._funnel_enter()
                return
            if key == "escape":
                event.prevent_default()
                event.stop()
                app._funnel_composer_clear()
                return
            if key == "q" and not self.text:
                event.prevent_default()
                event.stop()
                app.exit()
                return
            if (key == "e" and not self.text
                    and app._cockpit_last_task):
                # 2.8-A 轮间 E 召回：上一轮任务文本镜像入 composer
                # 供改写（首跑 last_task 空 = e 仍为文字本体，漏斗
                # F-R1 键律逐字不变）
                event.prevent_default()
                event.stop()
                app._between_runs_recall()
                return
            if key in ("ctrl+j", "shift+enter"):
                event.prevent_default()
                event.stop()
                self.insert("\n")
                return
            await super()._on_key(event)

        async def _compose_key(self, event, key: str) -> None:
            """CU-COCKPIT-1：COMPOSE 键路由（授权 §十二）。授权键集
            外的可打印键 no-op 消费（task 编辑回漏斗完成——compose 态
            绝不隐式编辑被遮蔽的草稿）；ctrl+c 放行框架既有路径。"""
            if key == "ctrl+c":
                await super()._on_key(event)
                return
            event.prevent_default()
            event.stop()
            if key in ("up", "down", "left", "right", "space", "enter",
                       "escape", "q", "r", "l", "L"):
                self.app._compose_dispatch(key)

    class CockpitApp(App):  # type: ignore[misc]
        """主界面：五层结构 Header / Task / Pipeline / Collaboration
        Log / Persistent Composer（UX2-R1）+ （≥140 列）Context 面板
        + detail/activity/result 观察区（保留原位，R1 契约不动）。"""

        CSS = f"""
        #funnel-screen {{ display: none; }}
        #compose-screen {{ display: none; }}
        #input-dock {{ dock: bottom; height: auto; }}
        #dock-receipt {{ height: 1; }}
        #dock-controls {{ height: 1; }}
        #composer {{ height: auto; max-height: {_COMPOSER_MAX_ROWS}; }}
        #collab-log {{ height: 1fr; }}
        #context-panel {{ dock: right; width: 28; display: none; }}
        #header-zone {{ height: 1; }}
        #detail-zone {{ height: auto; }}
        #activity-zone {{ height: auto; }}
        #result-zone {{ height: auto; }}
        #collab-scroll {{ height: auto; }}
        #collab-zone {{ width: auto; }}
        """

        # tab 是 Screen 默认焦点键会先期消费——priority 绑定改走
        # target 切换（hidden shortcut）；ctrl+c 与 q 同径（冻结决策）。
        # 其余键全部经常驻 composer（唯一焦点）按态分发。
        BINDINGS = [
            Binding("tab", "cockpit_tab", "Target", priority=True),
            Binding("ctrl+c", "cockpit_quit", "Quit", priority=True),
        ]

        def __init__(self, *, driver=None, task="", plan=(), events=None,
                     facts=None, usage=None, session=None, control=None,
                     revision_pending=None, composition_preview=None,
                     start_composition=None, task_token=None,
                     timeout_seconds=None,
                     user_composition_surface=None):
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
            # 呈现态（绝不入投影输入、绝不持久、绝不写回事实源）。
            # UX2-R1：composer 常驻——_cockpit_revise_text 缓冲真源
            # 移入 #composer widget（单一真相），此处只余 mode
            # （CONFIRM 确认条）与 target（session-sticky）。
            self._cockpit_mode = MODE_COMMAND
            self._cockpit_confirm_action = None
            self._cockpit_revise_target = _TARGETS[0]
            self._cockpit_receipt = None
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
            # UX2-R2 呈现态：E 召回（最近提交原文镜像——echo 缓存
            # UI 自有，绝不触碰 pending/队列真相；改写提交仍走 REVISE
            # 铸造新 command_id）与 slash hint 上下文旗标（hint 行恰
            # 在进入/变化/离开 slash 输入时刷新 dock 行）。零事实写回。
            self._cockpit_last_submit = ""
            self._cockpit_revision_recall = False
            self._cockpit_slash_hint = False
            # 渲染文本快照（funnel_text 先例：属性镜像便于测试断言）
            self.header_text = ""
            self.task_text = ""
            self.agent_text = ""
            self.activity_text = ""
            self.detail_text = ""
            self._cockpit_ascii = _ascii_preferred()
            self._cockpit_show_context = True
            # 2.8-B 组呈现态（呈现层私有，绝不入投影真相回写）：
            # scroll_mode = H 显式横滚开关（ERRATA-2 契约——toggle、
            # H 下 ←/→ 仅滚动且 selected_index 冻结、非空缓冲恒文本
            # 域；与 selected_index/show_context 三件彼此独立）；
            # groups/member_ids = run-local 组合快照只读缓存（Start
            # 成功时自 ComposedRun 鸭取——本层零 entry import，真相
            # 链 declaration → resolved → run-local snapshot → 本
            # 缓存 → ProjectionInputs，单向只读）。legacy 直达路径
            # 无组合快照 = 恒 ()（groups=() 字节等价路径）。
            self._cockpit_scroll_mode = False
            self._cockpit_groups = ()
            self._cockpit_member_ids = ()
            # CU-TUI-5 漏斗面：注入闭包 = 组合真相唯一通道；Start 前
            # _cockpit_composed 恒 None（late-bound，零引擎对象）。
            # timeout 已由入口闭包捕获（真值不在本层），参数仅为
            # 路由对齐；漏斗态无 interval（键事件驱动刷新）。
            self._cockpit_composition_preview = composition_preview
            self._cockpit_start_composition = start_composition
            self._cockpit_composed = None
            self._cockpit_funnel_timeout = timeout_seconds
            # F-R1：task 草稿真源 = #composer widget；此 prefill 在
            # 漏斗挂载时装载（stage 派生同旧：非空白即 COMPOSING）。
            self._cockpit_funnel_prefill = task_token if task_token else ""
            self._cockpit_funnel_message = ()
            self._cockpit_stage = (
                STAGE_COMPOSING if self._cockpit_funnel_prefill.strip()
                else STAGE_NOT_STARTED)
            # 2.8-A 会话呈现镜像（呈现层私有，绝不入投影输入/事实源）：
            # runs = [(task, steps, status|None), ...]（Conversation-
            # Record 呈现原料；status None = 在飞诚实缺席）；last_task
            # = 轮间 E 召回源（任务文本 UI 镜像）；interval = RUNNING
            # 刷新计时器句柄（轮间收止、再 RUNNING 重建——单活跃律）；
            # reentry_pending = 轮间换屏延迟旗标（渲染尾执行，见
            # _refresh 尾注）。
            self._cockpit_runs = []
            self._cockpit_last_task = ""
            self._cockpit_interval = None
            self._cockpit_funnel_reentry_pending = False
            # CU-COCKPIT-1：COMPOSE 选择屏（pre-run funnel 阶段——绝不
            # 触碰 C2 RUNNING 三态/六键契约）。user 面注入闭包 =
            # 组合真相唯一通道（listing/preview/start）；全部选择/
            # 角色/光标均为呈现态快照，绝不写回任何事实源。intent/
            # expected 为 preview 闭包产物（entry 组装真相）——本层
            # 零 composition_core import、零引擎词汇。
            self._cockpit_user_surface = user_composition_surface
            self._cockpit_compose_active = False
            self._cockpit_compose_entries = ()
            self._cockpit_compose_cursor = 0
            self._cockpit_compose_selected = []
            self._cockpit_compose_roles = {}
            self._cockpit_compose_known = {}
            self._cockpit_compose_pcursor = 0
            self._cockpit_compose_intent = None
            self._cockpit_compose_expected = None
            self._cockpit_compose_fp = None
            self._cockpit_compose_message = ()
            # P2 W7：q 两段守卫（TUI ephemeral 交互态——绝不入
            # RunState/ControlBoundary/EventIndex/UsageLog/引擎真相）
            self._cockpit_compose_q_armed = False
            self.compose_text = ""
            # UX2-R1 Collaboration Log（D 裁决：有损派生缓存）——
            # R1 骨架恰两源：echo（用户提交原文）+ 回执（控制面
            # 同步投影）；ring 上限仅淘汰显示行，log_text 为测试镜像
            # （funnel_text 先例）。Task 永不进 Log（AC17）。
            self._cockpit_log_lines = []
            self.log_text = ""
            self._cockpit_disclosure = None
            self._cockpit_version_text = _version_text()
            if composition_preview is not None:
                self._cockpit_disclosure = composition_preview()

        # ------------------------------------------------ 布局

        def compose(self):
            # UX2-R1 五层结构：Header / Task / Collaboration Pipeline /
            # （detail/activity/result 观察区保留原位）/ Collaboration
            # Log（弹性 1fr）/ Persistent Composer dock（确认条 +
            # composer + hint）+ ≥140 Context
            yield Static("", id="funnel-screen")
            yield Static("", id="compose-screen")
            yield Static("", id="header-zone")
            yield Static("", id="task-zone")
            # 2.8-B：管线区入 HorizontalScroll（H 显式横滚 viewport；
            # 常规态截断行恒不溢出 = 零布局差）
            with HorizontalScroll(id="collab-scroll"):
                yield Static("", id="collab-zone")
            yield Static("", id="detail-zone")
            yield Static("", id="activity-zone")
            yield Static("", id="result-zone")
            yield Static("", id="progress-zone")
            yield RichLog(id="collab-log", max_lines=_LOG_RING_LINES,
                          wrap=True, auto_scroll=True)
            with Container(id="input-dock"):
                yield Static("", id="dock-receipt")
                yield CockpitComposer(
                    id="composer", compact=True, soft_wrap=True,
                    show_line_numbers=False)
                yield Static("", id="dock-controls")
            yield Static("", id="context-panel")

        def on_mount(self) -> None:
            if self._cockpit_composition_preview is not None:
                self._funnel_mount()
                return
            self._running_mount()

        def _funnel_mount(self) -> None:
            """漏斗初始呈现：主界面观察区隐藏，单一漏斗屏在场。
            F-R1：task 输入面 = 常驻 composer（prefill 装载 + 持焦）。"""
            for selector in _MAIN_ZONE_SELECTORS:
                self.query_one(selector).display = False
            self.query_one("#funnel-screen").display = True
            composer = self.query_one("#composer")
            if self._cockpit_funnel_prefill:
                # 内容安全门同律（funnel_input_line 先例）：unsafe
                # prefill 以占位符装载——秘密绝不进入 TUI 呈现态。
                composer.text = funnel_prefill_text(
                    self._cockpit_funnel_prefill)
                # 光标归位文档末尾（.text 赋值后光标停起点，键入会前插）
                composer.move_cursor(composer.document.end)
            composer.focus()
            self._funnel_refresh()

        def _running_mount(self) -> None:
            """RUNNING 呈现（legacy 直达与漏斗 Start 后共用同一面）。"""
            self.query_one("#funnel-screen").display = False
            self.query_one("#compose-screen").display = False
            for selector in _MAIN_ZONE_SELECTORS:
                self.query_one(selector).display = True
            self._focus_composer()
            self._refresh()
            # 数据驱动重投影：仅当事实源变化时内容才变化（零动画）。
            # 2.8-A 单活跃计时器律：句柄缺席才建（轮间已收止则此处
            # 重建；RUNNING 期内恰一个 interval——run 2 绝不叠加
            # 第二个 0.5s 计时器）。
            if self._cockpit_interval is None:
                self._cockpit_interval = self.set_interval(
                    0.5, self._refresh)
            self._cockpit_thread = threading.Thread(
                target=self._drive_loop, daemon=True)
            self._cockpit_thread.start()

        def _focus_composer(self) -> None:
            """UX2-R1：composer 唯一焦点（自愈式——TraceScreen pop 或
            任何焦点漂移后回到 composer；绝不抢 TraceScreen 在顶期）。"""
            if isinstance(self.screen, TraceScreen):
                return
            composer = self.query("#composer")
            if composer and self.focused is not composer[0]:
                composer[0].focus()

        def _composer_text(self) -> str:
            """composer 缓冲只读读取（teardown 竞态安全）。"""
            composer = self.query("#composer")
            return composer[0].text if composer else ""

        def _on_text_area_changed(self, event) -> None:
            # F-R1：任何编辑路径（键、BINDING 如 backspace、粘贴）统一
            # 经 Changed 消息同步漏斗 stage——显式调用时序上先于
            # BINDING 处理，覆盖不了删除/粘贴类编辑。
            if self._funnel_pre_start():
                self._funnel_after_edit()
                return
            # UX2-R2：slash 上下文提示——恰在进入/变化/离开 slash 输入
            # 时刷新一次 dock hint 行（局部 _refresh_dock，零投影——
            # AC3 边界延续；非 slash 普通键击保持零 dock 扰动）。
            in_slash = self._composer_text().startswith("/")
            if in_slash or self._cockpit_slash_hint:
                self._cockpit_slash_hint = in_slash
                self._refresh_dock()

        # ------------------------------------------------ 数据

        def _zone_update(self, selector: str, text: str) -> None:
            """仅内容变化时更新 DOM（CU-PERF-1 W1：主屏变更检测）。

            TraceScreen._update_static 同款写门镜像（_cockpit_last
            只存最后写入值——不命中即写即新，无失效语义）；快照
            属性由调用方无条件维护（测试消费面不变），display
            布尔亦由调用方独立赋值（布局与文本解耦）。"""
            widget = self.query_one(selector)
            if getattr(widget, "_cockpit_last", None) != text:
                widget._cockpit_last = text
                widget.update(text)

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
                ascii_only=self._cockpit_ascii,
                # 2.8-B 组呈现输入：run-local 快照只读缓存 + H 横滚
                # 呈现参数（投影层纯消费，零回写）
                groups=self._cockpit_groups,
                member_ids=self._cockpit_member_ids,
                scroll_mode=self._cockpit_scroll_mode,
                # CU-PERF-1 W2：主屏不显示 trace（唯一消费方是
                # TraceScreen，改经 trace_*_lines 三函数直取）——
                # 免除每 tick 的全事件格式化白算。
                include_trace=False)

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
            # UX2-R1：唯一焦点自愈（TraceScreen pop / 焦点漂移后归位）
            self._focus_composer()
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
                    # UX2-R1 终态塌缩：清 composer 草稿（对已终结执行
                    # steer 无意义，保留会伪造可提交假象）。composer
                    # 常驻——终态键入+Enter 仍走真实 dispatch → session
                    # 终态门诚实 REJECTED 回执入 Log（零特判分支）。
                    # 本块恒在 UI 线程执行（interval / call_from_thread）。
                    composer = self.query("#composer")
                    if composer and composer[0].text:
                        composer[0].text = ""
                    # 2.8-A 轮间再入（漏斗路径且已有 run 完结）：
                    # 镜像收录终态 + composed 置空再武装漏斗键路由 +
                    # 披露活取。PARKED 结构性不入此径（derive_
                    # lifecycle 仅真实终态词可跃迁至此）。换屏延迟至
                    # 渲染尾（本函数后段的 display 赋值会覆盖提前
                    # 的隐藏——_funnel_reentry_swap 尾注）。
                    if (self._cockpit_composition_preview is not None
                            and self._cockpit_runs):
                        self._between_runs_record(lifecycle)
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
            self._zone_update("#header-zone", state.header_line)
            self.task_text = state.task_line
            self._zone_update("#task-zone", state.task_line)
            self.agent_text = "\n".join(state.collaboration_lines)
            self._zone_update("#collab-zone", self.agent_text)
            # R1 Detail 有界窗：管线正下方；身份消失/未展开 = 诚实空；
            # 高度预算与活动尾窗同门规（<24 隐藏，Trace 仍是全量出口）
            self.detail_text = "\n".join(state.detail_lines)
            detail = self.query_one("#detail-zone")
            self._zone_update("#detail-zone", self.detail_text)
            detail.display = bool(self.detail_text) \
                and self.size.height >= 24 and detail_fits
            self.activity_text = "\n".join(state.activity_lines)
            activity = self.query_one("#activity-zone")
            self._zone_update("#activity-zone", self.activity_text)
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
            self._zone_update("#result-zone", result_text)
            # 空态整区隐藏（零结果零占位；有结果恒在场）
            result.display = bool(result_text)
            self._zone_update(
                "#progress-zone",
                f"{state.progress_line}\n{state.tokens_line}")
            # UX2-R1：dock 两行（hint + 确认条）；receipt 已入 Log
            self._refresh_dock()
            panel = self.query_one("#context-panel")
            wide = self.size.width >= 140
            if state.context_lines and wide and self._cockpit_show_context:
                self._zone_update("#context-panel",
                                  "\n".join(state.context_lines))
                panel.display = True
            else:
                panel.display = False
            # 2.8-A 轮间换屏（延迟至此：上面各 zone 的 display 赋值
            # 已全部落地，隐藏不再被回卷）
            if self._cockpit_funnel_reentry_pending:
                self._cockpit_funnel_reentry_pending = False
                self._funnel_reentry_swap()

        # ------------------------------------------------ 高度预算（A4）

        def _apply_content_budget(self, values, state, *, result_rows):
            """CU-TUI-INPUT A4：dock 保留高度预算（纯呈现）。

            Σ可见区行数 ≤ 终端高 - dock 动态保留高度
            （_dock_reserved_rows：composer 1..5 + hint 1 + 确认条 0/1，
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
            avail = (self.size.height or 24) - self._dock_reserved_rows()
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
            # CU-PERF-1 W3：收窄经窄域重算（仅 detail/result 两字段
            # 受 max_lines 影响——其唯一消费点在 build_projection 内
            # 恰两处）；与携带同参数的全量重建逐字段相等（golden
            # 锁死），消除 A4 每 tick 第二遍全量投影。
            if (values.detail_max_lines is not None
                    or values.result_max_lines is not None):
                state = apply_budget_narrowing(
                    state, values,
                    detail_max_lines=values.detail_max_lines,
                    result_max_lines=values.result_max_lines)
            return state, detail_fits, activity_fits

        # ------------------------------------------------ dock 呈现

        def _dock_reserved_rows(self) -> int:
            """UX2-R1：dock 动态保留高度 = composer 行数（1..上限，
            内滚不外撑）+ hint 1 + 回执条 1（#dock-receipt 恒 height:1
            ——空态占位行，MODE_CONFIRM 只换内容不换高）。composer
            缺席（teardown 竞态）回退 3。
            # ponytail: 行数按 \\n 计——软换行长行可能低估 1-2 行，
            余量由 dock auto 高度自然吸收；若实测裁剪再引入按显示宽计算。"""
            composer = self.query("#composer")
            if not composer:
                return 3
            rows = len(composer[0].text.splitlines()) or 1
            rows = min(max(rows, 1), _COMPOSER_MAX_ROWS)
            return rows + 2

        def _refresh_dock(self) -> None:
            """键击级局部渲染（CU-PERF-1 W1 延伸）：恰两行 dock
            （hint + 确认条）。composer 本体由 TextArea 自渲染——
            零 build_projection、零事件扫描、零主屏 zone 触碰。"""
            self._zone_update("#dock-controls",
                              self._dock_controls_line())
            self._zone_update("#dock-receipt",
                              self._dock_receipt_line())

        def _dock_controls_line(self) -> str:
            """Hint 行（composer 下方，恒一行）：提交力学恒在 +
            lifecycle 动词（空缓冲 hidden shortcuts 的唯一可发现面）
            + 语言/退出提示 + target 角标。R2：slash 命令面接管可发现
            性后本行收敛。键字母恒 EN，动词经投影层闭集词表（单一
            词表真源；语言提示键字母恒 EN mandates——[L]中文 而非
            [L]Chinese，旧惯例保持）。"""
            if self._cockpit_compose_active:
                # CU-COCKPIT-1：COMPOSE 键提示（闭集词表，l 可切换）
                return compose_keys_hint(
                    locale=self._cockpit_locale,
                    ascii_only=self._cockpit_ascii)
            if self._funnel_pre_start():
                return funnel_keys_hint(
                    ascii_only=self._cockpit_ascii)
            locale = self._cockpit_locale

            def word(key):
                return ui_label(key, locale)

            def verb(key):
                label = word(key)
                if label == key:
                    label = label[1:]
                return f"[{key[0]}]{label}"

            # UX2-R2：slash 输入上下文——hint 行临时呈现注册表候选
            # （autocomplete 最小实现，无补全键）；零候选=未知命令
            # 诚实提示。非 slash 输入保持既有动词行（下方）。
            composer_text = self._composer_text()
            if composer_text.startswith("/"):
                prefix = composer_text[1:].split(" ")[0]
                matches = slash_candidates(prefix)
                if not matches:
                    return word(
                        "unknown command · /help lists commands")
                return " · ".join(f"/{name}" for name in matches)

            submit = word("enter send · ctrl+j newline")
            language = "[L] EN" if locale == "zh" else "[L]中文"
            target = self._cockpit_revise_target
            lifecycle = getattr(
                getattr(self, "last_state", None), "lifecycle", None)
            # UX2-R2：[E]dit（召回最近提交改写）——真实可用性呈现：
            # 存在历史提交且非终态才在场（终态 steer 诚实塌缩）。
            edit = (verb("Edit")
                    if (self._cockpit_last_submit
                        and lifecycle not in
                        _TERMINAL_LIFECYCLE_HINTS)
                    else "")
            if self._cockpit_mode == MODE_CONFIRM:
                line = word("abort? · y confirm · n/esc cancel")
            elif lifecycle in ("PAUSED", "PARKED"):
                line = (f"{submit} {edit} {verb('Resume')} "
                        f"{verb('Abort')} {verb('Trace')} {language} "
                        f"{verb('Quit')} {word('→')} {target}")
            elif lifecycle in _TERMINAL_LIFECYCLE_HINTS:
                line = (f"{submit} {verb('Trace')} {language} "
                        f"{verb('Quit')} {word('→')} {target}")
            else:
                line = (f"{submit} {edit} {verb('Pause')} "
                        f"{verb('Abort')} {verb('Trace')} {language} "
                        f"{verb('Quit')} {word('→')} {target}")
            if self.size.width >= 140:
                line += f" {verb('Context')}"
            return line

        def _dock_receipt_line(self) -> str:
            """确认条（composer 上方）：仅 MODE_CONFIRM 在场；回执已
            迁入 Collaboration Log（AC4：回执不再瞬态闪没）。"""
            if self._cockpit_mode == MODE_CONFIRM:
                return "abort? (y/n)"
            return ""

        # ------------------------------------------------ 意图外发（唯一通道）

        def _dispatch(self, kind, text=None, target=None):
            """DISPATCH 边界：呈现层意图 → 注入回调外发意图 → 同步回执。

            UX2-R1（AC4）：回执行入 Collaboration Log（持久可见），
            不再是 TTL 瞬态；_cockpit_receipt 仅存最后回执镜像（测试
            消费面）。RESUME/ABORT 受理（ACCEPTED）时唤醒驱动循环的
            停驻等待——wake 仅为私有同步原语，绝非执行真相。dispatcher
            缺席时诚实 no-op（零回执、零伪造状态）。"""
            if self._cockpit_control is None:
                return None
            result = self._cockpit_control(kind, text=text, target=target)
            self._cockpit_receipt = control_receipt_line(
                result, kind=kind, ascii_only=self._cockpit_ascii)
            self._log_append(self._cockpit_receipt)
            status = getattr(result, "status", None)
            if (kind in ("RESUME", "ABORT")
                    and getattr(status, "value", status) == "ACCEPTED"):
                self._cockpit_wake.set()
            return result

        def _log_append(self, line: str) -> None:
            """Collaboration Log 追加（UX2-R1 骨架：echo + 回执两源）。

            Log 是有损派生缓存（D 裁决）：ring 上限仅淘汰显示行，
            真相在引擎三源 + TraceScreen——本层绝不从 Log 读回任何
            语义。log_text 快照仅供测试断言（funnel_text 先例）。"""
            self.query_one("#collab-log").write(line)
            self._cockpit_log_lines.append(line)
            del self._cockpit_log_lines[:-_LOG_RING_LINES]
            self.log_text = "\n".join(self._cockpit_log_lines)

        def _echo_line(self, text: str, intent: str = "STEER") -> str:
            """提交回显行（echo 源 = 用户提交原文，UI 自有呈现态）。

            R2：REVISION 召回改写走 "you · revise" 词条（Log 闭集
            纪律——行词全经投影层词表）。"""
            label_key = ("you · revise"
                         if intent == INTENT_REVISION else "you · steer")
            label = ui_label(label_key, self._cockpit_locale)
            return f"{label} [{self._cockpit_revise_target}] {text}"

        def _composer_submit(self) -> None:
            """Enter 提交（常驻 composer 唯一提交径）。

            空白 no-op（既有语义）。R2 意图路由：COMMAND → slash 执行
            （注册表封闭集，不依赖控制面在场——local 命令在任何态
            可用）；STEER/REVISION → 既有 REVISE 通道（同一 dispatch，
            分类只决定 echo 词条；dispatcher 缺席诚实 no-op、缓冲
            保持）。提交后镜像 last_submit（E 召回源）。accepted ≠
            applied ≠ honored——applied 只在 Trace CTRL 事实面。"""
            composer = self.query("#composer")
            text = composer[0].text if composer else ""
            if not text.strip():
                return
            intent = classify_submit(
                funnel_pre_start=self._funnel_pre_start(),
                text=text,
                revision_recall=self._cockpit_revision_recall)
            self._cockpit_revision_recall = False
            if intent == INTENT_COMMAND:
                name, arg = parse_slash(text)
                self._slash_execute(name, arg)
                if composer:
                    composer[0].text = ""
                self._refresh_dock()
                return
            if self._cockpit_control is None:
                return
            self._cockpit_last_submit = text
            self._log_append(self._echo_line(text, intent))
            result = self._dispatch(
                "REVISE", text=text,
                target=self._cockpit_revise_target)
            if result is not None and composer:
                composer[0].text = ""
            self._refresh_dock()

        def _revision_recall(self) -> None:
            """E（空缓冲）召回最近提交原文入 composer 供改写。

            召回源是 UI 自有 echo 镜像（非事实源读回）；改写后的
            提交仍是新的 REVISE（新 command_id），绝不触碰 pending/
            队列真相。设置 recall 态使下次 Enter 分类 REVISION。"""
            if not self._cockpit_last_submit:
                self._log_append(ui_label(
                    "no prior submission · type to steer",
                    self._cockpit_locale))
                self._refresh_dock()
                return
            composer = self.query("#composer")
            if composer:
                composer[0].text = self._cockpit_last_submit
                composer[0].move_cursor(composer[0].document.end)
            self._cockpit_revision_recall = True
            self._refresh_dock()

        def _slash_execute(self, name, arg) -> None:
            """Slash 命令执行（R2 封闭注册表，恰四 kind）。

            dispatch 类走既有 _dispatch（控制面唯一裁决、回执行
            Log）；confirm 类走既有 a 路径（确认条）；screen 类走既有
            t 路径（推屏）；local 类纯呈现切换（零外发）。未知命令
            诚实反馈、零外发。/target <agent> 明确不实现（Lock C）。"""
            spec = SLASH_REGISTRY.get(name)
            if spec is None:
                self._log_append(ui_label(
                    "unknown command · /help lists commands",
                    self._cockpit_locale))
                return
            kind = spec["kind"]
            if kind == "dispatch":
                self._dispatch(spec["dispatch_kind"])
                self._refresh()
            elif kind == "confirm":
                # 2.8-A：确认动作随命令名携带——abort→既有 ABORT 外发
                # （注入 dispatcher 唯一裁决）；new→会话呈现史清空（轮间域
                # 限定，RUNNING 中诚实拒绝——绝不在执行中丢显示史）
                if name == "new":
                    if not self._funnel_pre_start():
                        self._log_append(ui_label(
                            "only between runs · /new resets the "
                            "session display", self._cockpit_locale))
                        return
                    self._cockpit_confirm_action = "new"
                else:
                    self._cockpit_confirm_action = "abort"
                self._cockpit_mode = MODE_CONFIRM
                self._refresh()
            elif kind == "screen":
                self.push_screen(TraceScreen())
            elif name == "lang":
                # local：与 l/L 裸键同径（零推进切换）
                self._cockpit_locale = (
                    "zh" if self._cockpit_locale == "en" else "en")
                self._refresh(advance_tick=False)
            elif name == "context":
                self._cockpit_show_context = not self._cockpit_show_context
                self._refresh()
            elif name == "clear":
                # Log 是有损派生缓存（D 裁决）：清显示不触碰事实源；
                # last_submit 召回源独立保留。
                self.query_one("#collab-log").clear()
                self._cockpit_log_lines = []
                self.log_text = ""
            elif name == "help":
                # EN 冻结面（漏斗 R2 ERRATA 同律——命令名不译）
                for line in slash_help_lines():
                    self._log_append(line)
            elif name == "target":
                if arg:
                    self._log_append(ui_label(
                        "/target <agent> is not implemented",
                        self._cockpit_locale))
                else:
                    # 无参：queue ⇄ prompt 二选一（tab 同径）
                    other = [item for item in _TARGETS
                             if item != self._cockpit_revise_target]
                    self._cockpit_revise_target = other[0]
                    self._log_append(
                        f'{ui_label("→", self._cockpit_locale)} '
                        f'{self._cockpit_revise_target}')
            elif name == "again":
                # 2.8-A local：上一轮任务文本载入 composer（与轮间 E
                # 召回同源同径）——重跑须再经 Enter 两段律（池门+
                # 指纹在 start 闭包活读重估，绝不免检复活旧 run）
                if (self._funnel_pre_start() and self._cockpit_runs
                        and self._cockpit_last_task):
                    composer = self.query("#composer")
                    if composer:
                        composer[0].text = self._cockpit_last_task
                        composer[0].move_cursor(
                            composer[0].document.end)
                    self._funnel_after_edit()
                else:
                    self._log_append(ui_label(
                        "only between runs · /again reloads the "
                        "last task", self._cockpit_locale))
            elif name == "compose":
                # 2.8-A local：漏斗域进 COMPOSE 选择屏（c 键同径；
                # RUNNING 域诚实拒绝——单工作者律，执行中绝不重选）
                if (self._funnel_pre_start()
                        and self._cockpit_user_surface is not None):
                    self._compose_enter_screen()
                else:
                    self._log_append(ui_label(
                        "only between runs · /compose opens "
                        "selection", self._cockpit_locale))
            elif name == "runs":
                # 2.8-A local：会话 run 摘要（ConversationRecord 呈现
                # 视图；任何相位可用——纯呈现镜像，零事实触碰）
                for line in runs_summary_lines(
                        self._cockpit_runs,
                        width=self.size.width or 100,
                        locale=self._cockpit_locale,
                        ascii_only=self._cockpit_ascii):
                    self._log_append(line)

        def _new_session_clear(self) -> None:
            """/new 兑现（y 确认后）：会话呈现史清空——runs 镜像、
            轮间召回源、Log 显示缓存归零（分节计数随之重起）。
            纯呈现遗忘：引擎三源（EventIndex/journal/usage）与
            TraceScreen 全量事实不触碰（真相在引擎，显示史在 UI）。"""
            self._cockpit_runs = []
            self._cockpit_last_task = ""
            # 2.8-B：组快照缓存与横滚态一并归零（纯呈现遗忘——
            # 事实源零触碰同律）
            self._cockpit_scroll_mode = False
            self._cockpit_groups = ()
            self._cockpit_member_ids = ()
            self.query_one("#collab-log").clear()
            self._cockpit_log_lines = []
            self.log_text = ""
            self._log_append(ui_label(
                "session display cleared · run counter reset",
                self._cockpit_locale))

        # ------------------------------------------------ 首跑漏斗（CU-TUI-5）

        def _funnel_pre_start(self) -> bool:
            """漏斗前置态谓词：Start 前的键语义专用（RUNNING 后
            内层三态接管，本谓词恒 False）。"""
            return (self._cockpit_composition_preview is not None
                    and self._cockpit_composed is None)

        def _funnel_refresh(self) -> None:
            """漏斗屏渲染：投影层首屏组装 + 瞬态状态行（no-op 提示/
            BLOCKED 原因/红行/变更横幅）。F-R1（UX2-R1）：task 输入面
            = 常驻 composer（缓冲真源 #composer.text）；dock 两行 =
            两键提示 + 空 确认条。"""
            lines = funnel_first_screen(
                self._cockpit_version_text, self._cockpit_disclosure,
                self._composer_text(),
                width=self.size.width or 100,
                ascii_only=self._cockpit_ascii,
                include_input=False)
            if self._cockpit_funnel_message:
                lines = lines + tuple(self._cockpit_funnel_message)
            self.funnel_text = "\n".join(lines)
            self._zone_update("#funnel-screen", self.funnel_text)
            self._refresh_dock()

        def _funnel_after_edit(self) -> None:
            """F-R1：composer 原生编辑后的 stage 同步（派生态：
            非空白=COMPOSING，空白=NOT_STARTED；stage 跃迁清瞬态
            消息——CU-TUI-5 可打印分支语义等价）。"""
            stage = (STAGE_COMPOSING if self._composer_text().strip()
                     else STAGE_NOT_STARTED)
            if stage != self._cockpit_stage:
                self._cockpit_stage = stage
                self._cockpit_funnel_message = ()
            self._funnel_refresh()

        def _funnel_composer_clear(self) -> None:
            """F-R1：ESC 清稿（原 _funnel_key escape 分支语义——
            不退出、draft 归零、回 NOT_STARTED、清瞬态消息）。"""
            composer = self.query("#composer")
            if composer:
                composer[0].text = ""
            self._cockpit_stage = STAGE_NOT_STARTED
            self._cockpit_funnel_message = ()
            self._funnel_refresh()

        def _funnel_enter(self) -> None:
            """Enter 判定（§十二顺序，F-R1 缓冲真源改 composer）：
            空白 no-op 提示 → 预览 BLOCKED 原因+hint → 就绪才经注入
            闭包 Start（真相零进本层；判定逻辑零改动）。2.8-A：轮间
            再入（runs 非空）时斜杠前缀先经 classify_submit（唯一
            分类真源）路由 COMMAND——首跑漏斗斜杠=任务文本冻结律
            由 runs 空守卫逐字保持。"""
            buffer = self._composer_text()
            if (self._cockpit_runs
                    and classify_submit(
                        funnel_pre_start=False, text=buffer,
                        phase=PHASE_BETWEEN_RUNS) == INTENT_COMMAND):
                name, arg = parse_slash(buffer)
                self._slash_execute(name, arg)
                composer = self.query("#composer")
                if composer:
                    composer[0].text = ""
                self._funnel_after_edit()
                return
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
            App、同一次 run；常驻 composer 原位接管 STEER 输入——
            task 文本进 Task zone，绝不入 Log，AC17）。"""
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
            # 2.8-B run-local 快照只读缓存（getattr 鸭取——本层零
            # entry import 先例；新 run = 新呈现：横滚开关与 viewport
            # 偏移一并归零）
            self._cockpit_groups = tuple(
                getattr(composed, "groups", ()) or ())
            self._cockpit_member_ids = tuple(
                getattr(composed, "member_ids", ()) or ())
            self._cockpit_scroll_mode = False
            scroller = self.query("#collab-scroll")
            if scroller:
                scroller[0].scroll_x = 0
            # 2.8-A run 镜像收录 + 轮间召回源 + 分节线（run N ≥ 2
            # 启动时入 Log——呈现层纯数据连接线，事实面零触碰）
            self._cockpit_last_task = composed.task
            if self._cockpit_runs:
                self._log_append(run_divider_line(
                    len(self._cockpit_runs) + 1, composed.steps,
                    width=self.size.width or 100,
                    locale=self._cockpit_locale,
                    ascii_only=self._cockpit_ascii))
            self._cockpit_runs.append(
                (composed.task, tuple(composed.steps), None))
            composer = self.query("#composer")
            if composer:
                composer[0].text = ""
            self._running_mount()

        # ------------------------------------ 2.8-A 轮间态（BETWEEN_RUNS）

        def _between_runs_record(self, lifecycle: str) -> None:
            """终态时点的轮间记录（_refresh 生命周期跃迁块内调用，
            恒 UI 线程）：run 镜像收录终态（App outcome 权威、非终态
            词一律回落投影 lifecycle 词——interval 先于 worker 落
            outcome 观察到 terminal 的窄竞态下不误录 PARKED）、
            composed 置空（_funnel_pre_start 复真——漏斗键路由/直退
            阶梯/漏斗刷新全套既有机制原样再武装）、披露活取（池可能
            已变——两段 enter 门在新披露上重估）、interval 收止
            （漏斗态键驱动刷新律）。换屏旗标置位，真正换屏在渲染尾
            （_refresh 后段 display 赋值不回卷）。PARKED ≠ terminal：
            本函数只能经 _TERMINAL_LIFECYCLE_HINTS 跃迁到达——停驻
            唤醒续驱仍在原 run 内，绝不进入轮间态。"""
            if self._cockpit_runs:
                task, steps, _ = self._cockpit_runs[-1]
                status = getattr(
                    getattr(self.outcome, "status", None), "value", None)
                if status not in _TERMINAL_LIFECYCLE_HINTS:
                    status = lifecycle
                self._cockpit_runs[-1] = (task, steps, status)
            self._cockpit_composed = None
            # 2.8-B：run-local 组快照与横滚态随 run 归零（下一轮
            # 组合可能完全不同——快照真相在下一 Start 时重取）
            self._cockpit_scroll_mode = False
            self._cockpit_groups = ()
            self._cockpit_member_ids = ()
            # stage 直落 NOT_STARTED（而非 TERMINAL）：漏斗再入的
            # 输入前相位。mount 清 composer 排队的 Changed 消息可能
            # 在本函数之后才处理——彼时 pre_start 已复真，_funnel_
            # after_edit 以 composer 空白算出 NOT_STARTED；若此处
            # 留 TERMINAL，那次迟到同步将产生一次 stage 迁移并清空
            # 轮间横幅（消息序竞态）。直落同值 = 迟到同步成 no-op，
            # 横幅确定性保留；首个键入字才如常迁 COMPOSING 并让位。
            self._cockpit_stage = STAGE_NOT_STARTED
            if self._cockpit_composition_preview is not None:
                self._cockpit_disclosure = (
                    self._cockpit_composition_preview())
            status_word = (self._cockpit_runs[-1][2]
                           if self._cockpit_runs else lifecycle)
            self._cockpit_funnel_message = (
                ui_label("next collaboration · run {n} {status}",
                         self._cockpit_locale).format(
                    n=len(self._cockpit_runs), status=status_word),)
            if self._cockpit_interval is not None:
                self._cockpit_interval.stop()
                self._cockpit_interval = None
            self._cockpit_funnel_reentry_pending = True

        def _funnel_reentry_swap(self) -> None:
            """轮间换屏（渲染尾执行）：主屏观察区隐藏、漏斗屏在场
            （composer 恒钉底不动——输入位置零跳变；呈现层纯迁移，
            零引擎触碰、零事件构造）。"""
            for selector in _MAIN_ZONE_SELECTORS:
                self.query_one(selector).display = False
            self.query_one("#compose-screen").display = False
            self.query_one("#funnel-screen").display = True
            self._funnel_refresh()

        def _between_runs_recall(self) -> None:
            """轮间 E 召回：上一轮任务文本镜像载入 composer 供改写
            （手动 /again 前奏）。召回源是 UI 自有镜像（非事实读回）；
            改写后 Enter 经漏斗两段门装配全新 run——绝不复活旧 run。"""
            composer = self.query("#composer")
            if composer and self._cockpit_last_task:
                composer[0].text = self._cockpit_last_task
                composer[0].move_cursor(composer[0].document.end)
            self._funnel_after_edit()

        # ------------------------------------ CU-COCKPIT-1 COMPOSE 选择屏

        def _compose_enter_screen(self) -> None:
            """c 进入 COMPOSE（P2 W6 重入语义）：listing 重取快照 +
            selection 过滤失效 runtime（诚实披露，零静默修复）+
            光标 clamp + 披露失效。selection/role override/已知角色
            呈现跨 esc 保留；task 草稿原样在常驻 composer。"""
            if self._cockpit_user_surface is None:
                return
            entries = tuple(self._cockpit_user_surface.listing())
            live = {entry.runtime_id for entry in entries}
            removed = tuple(runtime_id
                            for runtime_id
                            in self._cockpit_compose_selected
                            if runtime_id not in live)
            if removed:
                self._cockpit_compose_selected = [
                    runtime_id for runtime_id
                    in self._cockpit_compose_selected
                    if runtime_id in live]
                for runtime_id in removed:
                    self._cockpit_compose_roles.pop(runtime_id, None)
                    self._cockpit_compose_known.pop(runtime_id, None)
                self._cockpit_compose_pcursor = min(
                    self._cockpit_compose_pcursor,
                    max(0, len(self._cockpit_compose_selected) - 1))
            self._cockpit_compose_active = True
            self._cockpit_compose_entries = entries
            self._cockpit_compose_cursor = min(
                self._cockpit_compose_cursor, max(0, len(entries) - 1))
            self._compose_invalidate_preview()
            self._cockpit_compose_message = (
                (ui_label("removed from selection: {ids}",
                          self._cockpit_locale).format(
                    ids=", ".join(removed)),)
                if removed else ())
            self.query_one("#funnel-screen").display = False
            self.query_one("#compose-screen").display = True
            self._compose_refresh()

        def _compose_exit_screen(self) -> None:
            """esc 返回漏斗（P2 W6）：selection/override/已知角色呈现
            保留，披露失效（再入先重 preview）；q 守卫随换屏解除。"""
            self._cockpit_compose_q_armed = False
            self._cockpit_compose_active = False
            self._compose_invalidate_preview()
            self.query_one("#compose-screen").display = False
            self.query_one("#funnel-screen").display = True
            self._funnel_refresh()

        def _compose_invalidate_preview(self) -> None:
            """披露失效（选择/角色变更、start 诚实拒绝）：intent/
            expected/指纹清空——再 Enter 先重 preview，绝不带旧
            expected 启动。已知角色镜像（_cockpit_compose_known）
            保留：仅呈现便利（最近一次披露的默认角色投影），不参与
            启动判定（stamp 门强制重 preview）。"""
            self._cockpit_compose_intent = None
            self._cockpit_compose_expected = None
            self._cockpit_compose_fp = None

        def _compose_display_roles(self):
            """呈现用角色解析（纯呈现派生，零事实写回）：显式 override
            （r 键产物）> 已知默认（最近一次 preview 披露的 steps
            镜像——entry 唯一默认指派真源的产物）> 诚实缺席
            （None → "—"）。"""
            merged = dict(self._cockpit_compose_known)
            merged.update(self._cockpit_compose_roles)
            return merged

        def _compose_refresh(self) -> None:
            """COMPOSE 屏渲染（纯呈现态投影；compose_text = 测试镜像，
            funnel_text 先例）。"""
            lines = compose_screen_lines(
                self._cockpit_version_text, self._composer_text(),
                self._cockpit_compose_entries,
                selected_ids=tuple(self._cockpit_compose_selected),
                cursor_index=self._cockpit_compose_cursor,
                roles=self._compose_display_roles(),
                participant_index=self._cockpit_compose_pcursor,
                preview_composition=self._cockpit_compose_expected,
                message_lines=tuple(self._cockpit_compose_message),
                width=self.size.width or 100,
                # P2 W8：TUI 供应可用高度（纯呈现参数；projection 只做
                # 纯算术窗口化，零新滚动子系统）
                height=self.size.height or None,
                ascii_only=self._cockpit_ascii,
                locale=self._cockpit_locale)
            self.compose_text = "\n".join(lines)
            self._zone_update("#compose-screen", self.compose_text)
            self._refresh_dock()

        def _compose_dispatch(self, key: str) -> None:
            """COMPOSE 键语义（唯一入口；呈现态快照，零 dispatch 零
            事实写回）。P2 W7：q 有可失状态（selection 或 task 草稿）
            时两段守卫——首 q 只武装 + 横幅，任意其它键/esc/换屏
            解除；仅 armed 态 q 走既有 self.exit()。"""
            if self._cockpit_compose_q_armed and key != "q":
                # disarm + 撤守卫横幅（armed 期间仅本路由可改横幅）
                self._cockpit_compose_q_armed = False
                if self._cockpit_compose_message == (
                        ui_label("q again to quit · esc back",
                                 self._cockpit_locale),):
                    self._cockpit_compose_message = ()
            if key == "q":
                losable = (self._cockpit_compose_selected
                           or self._composer_text().strip())
                if losable and not self._cockpit_compose_q_armed:
                    self._cockpit_compose_q_armed = True
                    self._cockpit_compose_message = (
                        ui_label("q again to quit · esc back",
                                 self._cockpit_locale),)
                    self._compose_refresh()
                    return
                self.exit()
                return
            if key == "escape":
                self._compose_exit_screen()
                return
            if key in ("l", "L"):
                self._cockpit_locale = (
                    "zh" if self._cockpit_locale == "en" else "en")
                self._compose_refresh()
                return
            if key == "up":
                if self._cockpit_compose_entries:
                    self._cockpit_compose_cursor = max(
                        0, self._cockpit_compose_cursor - 1)
                self._compose_refresh()
                return
            if key == "down":
                if self._cockpit_compose_entries:
                    self._cockpit_compose_cursor = min(
                        len(self._cockpit_compose_entries) - 1,
                        self._cockpit_compose_cursor + 1)
                self._compose_refresh()
                return
            if key == "space":
                self._compose_toggle()
                return
            if key == "left":
                if self._cockpit_compose_selected:
                    self._cockpit_compose_pcursor = max(
                        0, self._cockpit_compose_pcursor - 1)
                    self._compose_refresh()
                return
            if key == "right":
                if self._cockpit_compose_selected:
                    self._cockpit_compose_pcursor = min(
                        len(self._cockpit_compose_selected) - 1,
                        self._cockpit_compose_pcursor + 1)
                    self._compose_refresh()
                return
            if key == "r":
                self._compose_cycle_role()
                return
            if key == "enter":
                self._compose_enter()
                return

        def _compose_toggle(self) -> None:
            """space 勾选/取消（2-4 门：第 5 个诚实阻断；0/1 由 core
            在 preview 诚实拒——UI 结构上只产闭集 role + 唯一 runtime
            勾选）。"""
            entries = self._cockpit_compose_entries
            if not entries or self._cockpit_compose_cursor >= len(entries):
                return
            runtime_id = entries[self._cockpit_compose_cursor].runtime_id
            selected = self._cockpit_compose_selected
            if runtime_id in selected:
                selected.remove(runtime_id)
                self._cockpit_compose_roles.pop(runtime_id, None)
                self._cockpit_compose_known.pop(runtime_id, None)
                if self._cockpit_compose_pcursor >= len(selected):
                    self._cockpit_compose_pcursor = max(
                        0, len(selected) - 1)
                self._compose_invalidate_preview()
                self._cockpit_compose_message = ()
                self._compose_refresh()
                return
            if len(selected) >= 4:
                self._cockpit_compose_message = (
                    ui_label("2-4 runtimes", self._cockpit_locale),)
                self._compose_refresh()
                return
            selected.append(runtime_id)
            selected.sort()  # 声明序 = sorted runtime_id（default 同律）
            self._compose_invalidate_preview()
            self._cockpit_compose_message = ()
            self._compose_refresh()

        def _compose_cycle_role(self) -> None:
            """r 循环当前 participant 的 Role（COMPOSITION_ROLES 闭集
            环；duplicate Role 合法——M0 ERRATA-2）。起点 = 当前呈现
            角色（override > 披露默认；缺席从环首起）。"""
            selected = self._cockpit_compose_selected
            if not selected:
                return
            pcursor = min(self._cockpit_compose_pcursor,
                          len(selected) - 1)
            runtime_id = selected[pcursor]
            role = self._compose_display_roles().get(runtime_id)
            index = (COMPOSITION_ROLES.index(role)
                     if role in COMPOSITION_ROLES else -1)
            self._cockpit_compose_roles[runtime_id] = COMPOSITION_ROLES[
                (index + 1) % len(COMPOSITION_ROLES)]
            self._compose_invalidate_preview()
            self._cockpit_compose_message = ()
            self._compose_refresh()

        def _compose_enter(self) -> None:
            """Enter 两段（授权 §七/§九）：无有效披露 → preview（只读
            零执行）；有有效披露且指纹未变 → start（既有 start_user_
            composition 全链——同一 intent/expected，绝不重建）。"""
            task_text = self._composer_text()
            if not task_text.strip():
                self._cockpit_compose_message = (
                    ui_label("describe the task first",
                             self._cockpit_locale),)
                self._compose_refresh()
                return
            selection = tuple(
                (runtime_id, self._cockpit_compose_roles.get(runtime_id))
                for runtime_id in self._cockpit_compose_selected)
            stamp = (
                tuple(self._cockpit_compose_selected),
                tuple(sorted(self._cockpit_compose_roles.items())))
            if (self._cockpit_compose_expected is None
                    or self._cockpit_compose_fp != stamp):
                intent, result = self._cockpit_user_surface.preview(
                    selection)
                if hasattr(result, "reason"):
                    # CompositionError：原词红行，停留（无披露可启动）
                    self._compose_invalidate_preview()
                    self._cockpit_compose_message = funnel_error_lines(
                        result)
                    self._compose_refresh()
                    return
                self._cockpit_compose_intent = intent
                self._cockpit_compose_expected = result
                self._cockpit_compose_fp = stamp
                self._cockpit_compose_known = {
                    runtime_id: role
                    for role, runtime_id in getattr(result, "steps", ())}
                self._cockpit_compose_message = ()
                self._compose_refresh()
                return
            result = self._cockpit_user_surface.start(
                task_text, self._cockpit_compose_intent,
                self._cockpit_compose_expected)
            if hasattr(result, "drive"):
                self._compose_start_success(result)
                return
            if hasattr(result, "reasons"):
                # CompositionChanged：诚实横幅 + listing 刷新 + 披露
                # 失效（re-selection required——零静默重绑/重试）
                self._cockpit_compose_entries = (
                    self._cockpit_user_surface.listing())
                self._compose_invalidate_preview()
                self._cockpit_compose_message = funnel_changed_lines(
                    result.reasons)
                self._compose_refresh()
                return
            # CompositionError：原词红行 + listing 刷新 + 披露失效
            self._cockpit_compose_entries = (
                self._cockpit_user_surface.listing())
            self._compose_invalidate_preview()
            self._cockpit_compose_message = funnel_error_lines(result)
            self._compose_refresh()

        def _compose_start_success(self, composed) -> None:
            """COMPOSE 启动成功 → 同一换屏路径（_funnel_start_success
            复用——零第二 RUNNING 实现）；q 守卫随换屏解除。"""
            self._cockpit_compose_q_armed = False
            self._cockpit_compose_active = False
            self.query_one("#compose-screen").display = False
            self._funnel_start_success(composed)

        # ------------------------------------------------ 键位（常驻 composer）

        def on_key(self, event) -> None:
            """UX2-R1：常态输入全部经常驻 composer（唯一焦点）消费；
            到达此处的只有 TraceScreen 在顶时未被其绑定消费的冒泡键
            ——"冒泡命令照旧触发"契约保持（v1.0 §8）；语言切换仅
            主屏（冻结先例）；t 不再二次叠屏（Trace 在顶 no-op）。"""
            key = getattr(event, "key", "")
            if isinstance(self.screen, TraceScreen):
                if key in ("l", "L"):
                    return
                if key in ("p", "r", "a", "q", "c"):
                    self._bare_key(key)
                return

        def _bare_key(self, key: str) -> None:
            """空缓冲裸键 hidden shortcuts（六键契约降权不降义——
            语义逐字沿用 _command_key 旧分支）。e 不再是命令：
            "打开 composer" 结构性无意义（召回编辑是 R2 /revise）。
            仅两径可达：composer 空缓冲（composer._on_key 拦截）或
            TraceScreen 在顶冒泡——后者 l/L 已被 on_key 先行排除。"""
            if key == "p":
                self._dispatch("PAUSE")
                self._refresh()
            elif key == "r":
                self._dispatch("RESUME")
                self._refresh()
            elif key == "a":
                self._cockpit_confirm_action = "abort"
                self._cockpit_mode = MODE_CONFIRM
                self._refresh()
            elif key == "q":
                self._quit_path()
            elif key == "t":
                self.push_screen(TraceScreen())
            elif key == "c":
                self._cockpit_show_context = not self._cockpit_show_context
                self._refresh()
            elif key in ("l", "L"):
                # 零推进渲染（动画 tick/揭示一概不动）、零外发、
                # 零事实触碰（R2 冻结语义原样）
                self._cockpit_locale = (
                    "zh" if self._cockpit_locale == "en" else "en")
                self._refresh(advance_tick=False)
            elif key == "H":
                # 2.8-B H 显式横滚模式（ERRATA-2 契约）：仅主屏组合
                # 在场时 toggle（漏斗前置/COMPOSE 态不可达本分支——
                # 键各自归文本/选择屏路由 = 诚实 no-op；TraceScreen
                # 在顶由 App 键路由先行拦截）。零推进渲染、零外发、
                # 零事实触碰（c 键同构纯呈现开关）。
                if self._cockpit_plan:
                    self._cockpit_scroll_mode = (
                        not self._cockpit_scroll_mode)
                    self._refresh(advance_tick=False)

        def _pipeline_nav_key(self, key: str) -> None:
            """空缓冲导航/展开（R1 契约：←/→ 选中、space 展开；Enter
            已归提交——空提交 no-op 既有语义）。纯呈现态：clamp、
            空组合 no-op、零外发。2.8-B（ERRATA-2）：H 模式下 ←/→
            唯一语义 = collab-zone 水平滚动（selected_index 冻结——
            同状态下 navigation 与 scrolling 互斥，本入口分流）；
            normal 模式 ←/→ 既有语义逐字不变；space 两模式下恒为
            展开（与滚动无冲突面）。"""
            if not self._cockpit_plan:
                return
            if self._cockpit_scroll_mode and key in ("left", "right"):
                self._scroll_collab_zone(key)
                return
            if key in ("left", "right"):
                delta = -1 if key == "left" else 1
                self._cockpit_selected_index = max(
                    0, min(self._cockpit_selected_index + delta,
                           len(self._cockpit_plan) - 1))
                self._refresh()
            elif key == "space":
                index = min(self._cockpit_selected_index,
                            len(self._cockpit_plan) - 1)
                stage = self._cockpit_plan[index][0]
                self._cockpit_expanded_stage = (
                    None if self._cockpit_expanded_stage == stage
                    else stage)
                self._refresh()

        def _scroll_collab_zone(self, key: str) -> None:
            """H 模式下 ←/→ 的唯一语义：collab-zone viewport 水平
            滚动（ERRATA-2 契约第 4/7 点）。滚动偏移由容器持有
            （App 零镜像）；selected_index 冻结；零外发、零事实
            触碰——纯呈现态。"""
            scroller = self.query("#collab-scroll")
            if not scroller:
                return
            delta = (-_H_SCROLL_STEP if key == "left"
                     else _H_SCROLL_STEP)
            scroller[0].scroll_x = max(
                0.0, scroller[0].scroll_x + delta)

        # ------------------------------------------------ 键位（状态机）

        def _confirm_key(self, key: str) -> None:
            if key == "y":
                # 2.8-A：确认动作随命令名携带（abort=既有 ABORT 外发
                # ——注入 dispatcher 唯一裁决；new=会话呈现史清空——纯呈现）
                if self._cockpit_confirm_action == "new":
                    self._new_session_clear()
                else:
                    self._dispatch("ABORT")
                self._cockpit_mode = MODE_COMMAND
                self._cockpit_confirm_action = None
                self._refresh()
            elif key in ("n", "escape"):
                self._cockpit_mode = MODE_COMMAND
                self._cockpit_confirm_action = None
                self._refresh()
            # 其余按键 no-op（零外发、零状态变化）

        def action_cockpit_tab(self) -> None:
            """target 封闭二选一切换（hidden shortcut；仅 Start 后
            主屏——漏斗态/Trace 在顶 no-op，hint 角标即时更新）。"""
            if (self._funnel_pre_start()
                    or isinstance(self.screen, TraceScreen)):
                return
            other = [item for item in _TARGETS
                     if item != self._cockpit_revise_target]
            self._cockpit_revise_target = other[0]
            self._refresh_dock()

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
            # CU-PERF-1 W2：trace 所有权分离——本屏只消费三个 trace
            # 字段（全量 build_projection 的其余产出从不显示），改经
            # trace_*_lines 三函数直取（事实面恒 en——locale 与主屏
            # 呈现参数同律，trace 词表零翻译）；slots/usage/width 仍经
            # _collect_inputs 只读快照供给。主屏 include_trace=False
            # 后，Trace 开启期每 0.5s 恰一份事件格式化（本处）。
            values = app._collect_inputs()
            events = values.events
            seqs = [getattr(event, "sequence", None) for event in events]
            selected = self._trace_selected_seq
            marker_index = (seqs.index(selected)
                            if selected in seqs else None)
            obs_lines = []
            for index, line in enumerate(trace_observation_lines(
                    events, ascii_only=values.ascii_only)):
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
                trace_control_lines(values.facts,
                                    ascii_only=values.ascii_only) + ("",)
                + revision_status_lines(values.facts, pending))
            self._update_static("#trace-ctrl", self.ctrl_text)
            self.usage_text = "\n".join(
                trace_usage_lines(values.usage_records,
                                  ascii_only=values.ascii_only))
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
                       task_token=None, timeout_seconds=None,
                       user_composition_surface=None):
    """漏斗同步外壳（CU-TUI-5 §十六）：单一 App、单次 run()——
    漏斗为初始呈现阶段，Start 后同一 App 换屏 RUNNING。

    前置退出（q/Ctrl-C，未 Start）→ outcome None（零执行零交付）；
    timeout 真值已由入口闭包捕获，本参数仅路由对齐。组合真相只经
    注入的 preview/start 两闭包进出；CU-COCKPIT-1 增 user 面
    （listing/preview/start）——c 键 COMPOSE 选择屏的唯一数据源。"""
    _build_classes()
    app = CockpitApp(composition_preview=composition_preview,
                     start_composition=start_composition,
                     task_token=task_token,
                     timeout_seconds=timeout_seconds,
                     user_composition_surface=user_composition_surface)
    app.run()
    app.wait_for_driver()
    if app.failure is not None:
        raise app.failure
    return app.outcome
