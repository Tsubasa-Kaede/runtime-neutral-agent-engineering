"""CU-COCKPIT-1 P1: COMPOSE TUI stage — pre-run funnel stage Pilot tests.

键契约（授权 §十二 + P2 W6/W7）：↑↓ 池导航 · space 勾选 ·
←→ participant · r Role 循环 · Enter 两段（preview → start）·
esc 返回漏斗（selection/override/已知角色保留，W6）· q 无可失
状态单键退出、有可失状态两段守卫（W7）· l EN⇄ZH。COMPOSE 是
pre-run funnel 阶段——绝不触碰 C2 RUNNING 三态。全部离线
doubles；REAL=0。

2.8-C GROUP_AUTHORING（ERRATA 1-4）：g 进模式/建组（恒不退出——
仅 Enter/Esc 回 BASE）；space 成员资格环；x 解散；单调 id
（g1,g2,g3→删 g2→g4）；拒绝 = honest no-op（零 draft 变更 ⇒
零 invalidate）；剪除钩子；/new 双清（draft+seq）。
"""
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("textual")

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from execution_observation import ExecutionEvent, ExecutionEventType  # noqa: E402

import cockpit_tui  # noqa: E402


# ---------------------------------------------------------------- doubles


class _Gate:
    """阻塞门控 driver：保持 App 存活到测试断言完成。"""

    def __init__(self):
        self.release = threading.Event()

    def driver(self):
        self.release.wait(10)
        return "gated-outcome"


def _event(seq, kind, stage="architect", runtime="rt-0", status="SUCCESS",
           reason="R", duration_ms=None):
    return ExecutionEvent(
        event_type=kind, sequence=seq, task_id="t", correlation_id="e",
        stage=stage, runtime_id=runtime, status=status, reason=reason,
        duration_ms=duration_ms)


def static_events():
    return (
        _event(0, ExecutionEventType.STAGE_STARTED),
        _event(1, ExecutionEventType.INVOCATION_STARTED),
    )


class _SessionProjection:
    run_state = None
    terminal = None
    last_outcome = None


def funnel_composition(bindings=(("architect", "rt-a", "prov-a"),
                                 ("coder", "rt-b", "prov-b")),
                       blocked_reason=None, blocked_hint=None):
    return SimpleNamespace(
        roles=tuple(role for role, _, _ in bindings),
        bindings=tuple(
            SimpleNamespace(role=role, runtime_id=runtime,
                            provider_id=provider,
                            canonical_runtime_identity=(
                                runtime, provider, None, "f"))
            for role, runtime, provider in bindings),
        blocked_reason=blocked_reason, blocked_hint=blocked_hint)


def resolved_double(bindings=(("architect", "rt-a", "prov-a"),
                              ("coder", "rt-b", "prov-b"))):
    return SimpleNamespace(
        bindings=tuple(
            SimpleNamespace(role=role, runtime_id=runtime,
                            provider_id=provider,
                            canonical_runtime_identity=(
                                runtime, provider, None, "f"))
            for role, runtime, provider in bindings),
        member_ids=("member-1", "member-2"), groups=(),
        steps=tuple((role, runtime) for role, runtime, _ in bindings))


def composition_error_double(reason="INVALID_MEMBER_COUNT",
                             detail="composition needs 2-4 members "
                                    "(found 1)",
                             hint=None):
    return SimpleNamespace(reason=reason, detail=detail, hint=hint)


def composition_changed_double(reasons, composition):
    return SimpleNamespace(reasons=tuple(reasons), composition=composition)


def composed_run_double(task="compose task", gate=None):
    def drive():
        if gate is not None:
            gate.release.wait(10)
        return "compose-outcome"

    return SimpleNamespace(
        task=task,
        steps=(("architect", "rt-a"), ("coder", "rt-b")),
        plan=(("step-0-architect", "architect", "rt-a", "prov-a"),
              ("step-1-coder", "coder", "rt-b", "prov-b")),
        task_id="t", execution_id="e", emit=None,
        drive=drive,
        session=_SessionProjection(),
        dispatch_control=lambda *args, **kwargs: None,
        revision_pending=lambda: 0,
        events=static_events, facts=lambda: (), usage=lambda: ())


class _UserSurfaceRecorder:
    """user 面注入双件：listing 计数；preview/start 按脚本回放。"""

    def __init__(self, entries, preview_results=(), start_results=()):
        self.entries = tuple(entries)
        self.preview_results = list(preview_results)
        self.start_results = list(start_results)
        self.listing_calls = 0
        self.preview_calls = []
        self.start_calls = []

    def listing(self):
        self.listing_calls += 1
        return self.entries

    def preview(self, selection, groups=()):
        """镜像 user 面 2.8-C 契约：preview(selection, groups=())。"""
        self.preview_calls.append((tuple(selection), tuple(groups)))
        return self.preview_results.pop(0)

    def start(self, task_text, intent, expected_resolved):
        self.start_calls.append((task_text, intent, expected_resolved))
        return self.start_results.pop(0)


class _StartRecorder:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def __call__(self, task_text, expected_composition):
        self.calls.append((task_text, expected_composition))
        return self.results.pop(0)


def _entries(*runtime_ids):
    return tuple(SimpleNamespace(runtime_id=rt, provider_id=f"prov-{rt}")
                 for rt in runtime_ids)


def make_compose_app(composition, start, user_surface, task_token=None):
    return cockpit_tui.CockpitApp(
        composition_preview=lambda: composition,
        start_composition=start,
        user_composition_surface=user_surface,
        task_token=task_token)


def _compose_text(app):
    return app.compose_text


class ComposeStagePilotTests(unittest.IsolatedAsyncioTestCase):
    """COMPOSE = pre-run funnel 阶段：c 进入 / esc 返回 / q 退出。"""

    async def test_c_opens_compose_and_esc_returns(self):
        user = _UserSurfaceRecorder(_entries("rt-a", "rt-b"))
        start = _StartRecorder([])
        app = make_compose_app(funnel_composition(), start, user,
                               task_token="do work")
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.press("c")
            await pilot.pause()
            self.assertTrue(app.query_one("#compose-screen").display)
            self.assertFalse(app.query_one("#funnel-screen").display)
            text = _compose_text(app)
            self.assertIn("[ ] rt-a", text)
            self.assertIn("TASK: do work", text)
            # COMPOSE 是 pre-run 阶段：C2 mode 零触碰
            self.assertEqual(app._cockpit_mode, cockpit_tui.MODE_COMMAND)
            await pilot.press("escape")
            await pilot.pause()
            self.assertFalse(app.query_one("#compose-screen").display)
            self.assertTrue(app.query_one("#funnel-screen").display)
            self.assertEqual(app._composer_text(), "do work")
            self.assertEqual(start.calls, [])
            self.assertEqual(user.preview_calls, [])

    async def test_space_selects_and_fifth_blocked(self):
        user = _UserSurfaceRecorder(
            _entries("rt-a", "rt-b", "rt-c", "rt-d", "rt-e"))
        app = make_compose_app(funnel_composition(), _StartRecorder([]),
                               user, task_token="do work")
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.press("c")
            for _ in range(4):
                await pilot.press("space")
                await pilot.press("down")
            await pilot.pause()
            text = _compose_text(app)
            self.assertEqual(text.count("[x]"), 4)
            # 第 5 个勾选被阻止（2-4 门）且诚实提示
            await pilot.press("space")
            await pilot.pause()
            self.assertIn("2-4", _compose_text(app))
            self.assertEqual(_compose_text(app).count("[x]"), 4)

    async def test_role_cycling_and_duplicate_allowed(self):
        user = _UserSurfaceRecorder(
            _entries("rt-a", "rt-b"),
            preview_results=[
                (SimpleNamespace(members=()), resolved_double())])
        app = make_compose_app(funnel_composition(), _StartRecorder([]),
                               user, task_token="do work")
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.press("c")
            await pilot.press("space")
            await pilot.press("down")
            await pilot.press("space")
            await pilot.pause()
            # 预览前零指派 = 诚实 "—"（默认角色经 preview 披露回填）
            self.assertIn("member-1  —", _compose_text(app))
            await pilot.press("enter")
            await pilot.pause()
            # 披露镜像：member-1 ARCHITECT / member-2 CODER（模板默认）
            self.assertIn("member-1  ARCHITECT", _compose_text(app))
            self.assertIn("member-2  CODER", _compose_text(app))
            # r 循环 member-1：architect → coder（duplicate 合法）
            await pilot.press("r")
            await pilot.pause()
            self.assertIn("member-1  CODER", _compose_text(app))
            self.assertIn("member-2  CODER", _compose_text(app))

    async def test_enter_blank_task_is_honest_message(self):
        user = _UserSurfaceRecorder(_entries("rt-a", "rt-b"))
        app = make_compose_app(funnel_composition(), _StartRecorder([]),
                               user)
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.press("c")
            await pilot.press("enter")
            await pilot.pause()
            self.assertIn("describe the task first", _compose_text(app))
            self.assertEqual(user.preview_calls, [])

    async def test_enter_preview_then_enter_start_reaches_running(self):
        gate = _Gate()
        intent_double = SimpleNamespace(members=())
        resolved = resolved_double()
        user = _UserSurfaceRecorder(
            _entries("rt-a", "rt-b"),
            preview_results=[(intent_double, resolved)],
            start_results=[composed_run_double(gate=gate)])
        start = _StartRecorder([])
        app = make_compose_app(funnel_composition(), start, user,
                               task_token="compose task")
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.press("c")
            await pilot.press("space")
            await pilot.press("down")
            await pilot.press("space")
            await pilot.press("enter")
            await pilot.pause()
            # 阶段一：preview（计划块在场；start 未调用）
            self.assertEqual(len(user.preview_calls), 1)
            selection, preview_groups = user.preview_calls[0]
            self.assertEqual(
                tuple(rt for rt, _ in selection), ("rt-a", "rt-b"))
            self.assertEqual(preview_groups, ())
            self.assertIn("architect  ← rt-a", _compose_text(app))
            self.assertEqual(user.start_calls, [])
            await pilot.press("enter")
            for _ in range(100):
                if app._cockpit_composed is not None:
                    break
                await pilot.pause()
            # 阶段二：start（task + previewed intent + expected——同一性）
            self.assertEqual(len(user.start_calls), 1)
            task_text, intent, expected = user.start_calls[0]
            self.assertEqual(task_text, "compose task")
            self.assertIs(intent, intent_double)
            self.assertIs(expected, resolved)
            self.assertEqual(app._cockpit_stage, cockpit_tui.STAGE_RUNNING)
            self.assertFalse(app.query_one("#compose-screen").display)
            self.assertFalse(app.query_one("#funnel-screen").display)
            self.assertTrue(app.query_one("#header-zone").display)
            gate.release.set()
            for _ in range(200):
                if app.outcome is not None:
                    break
                await pilot.pause()
        self.assertEqual(app.outcome, "compose-outcome")

    async def test_preview_error_shows_reason_and_retries(self):
        user = _UserSurfaceRecorder(
            _entries("rt-a", "rt-b"),
            preview_results=[
                (None, composition_error_double()),
                (SimpleNamespace(members=()), resolved_double())])
        app = make_compose_app(funnel_composition(), _StartRecorder([]),
                               user, task_token="do work")
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.press("c")
            await pilot.press("space")
            await pilot.press("down")
            await pilot.press("space")
            await pilot.press("enter")
            await pilot.pause()
            self.assertIn("INVALID_MEMBER_COUNT", _compose_text(app))
            self.assertEqual(user.start_calls, [])
            # 错误后 Enter 重试 preview（不直接 start）
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(len(user.preview_calls), 2)
            self.assertEqual(user.start_calls, [])

    async def test_selection_change_after_preview_repreviews(self):
        user = _UserSurfaceRecorder(
            _entries("rt-a", "rt-b", "rt-c"),
            preview_results=[
                (SimpleNamespace(members=()), resolved_double()),
                (SimpleNamespace(members=()), resolved_double())])
        app = make_compose_app(funnel_composition(), _StartRecorder([]),
                               user, task_token="do work")
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.press("c")
            await pilot.press("space")
            await pilot.press("down")
            await pilot.press("space")
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(len(user.preview_calls), 1)
            # r 改角色 → 披露失效：Enter 重 preview，绝不带旧 expected start
            await pilot.press("r")
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(len(user.preview_calls), 2)
            self.assertEqual(user.start_calls, [])

    async def test_start_composition_changed_banner_and_refresh(self):
        user = _UserSurfaceRecorder(
            _entries("rt-a", "rt-b"),
            preview_results=[(SimpleNamespace(members=()), resolved_double()),
                             (SimpleNamespace(members=()), resolved_double())],
            start_results=[
                composition_changed_double(
                    ("runtime rt-b no longer VERIFIED",),
                    resolved_double())])
        app = make_compose_app(funnel_composition(), _StartRecorder([]),
                               user, task_token="do work")
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.press("c")
            await pilot.press("space")
            await pilot.press("down")
            await pilot.press("space")
            await pilot.press("enter")
            await pilot.press("enter")
            await pilot.pause()
            text = _compose_text(app)
            self.assertIn("collaboration plan changed", text)
            self.assertIn("rt-b no longer VERIFIED", text)
            self.assertTrue(app._cockpit_compose_active)
            self.assertEqual(len(user.start_calls), 1)
            # 池漂移后 listing 已刷新；preview 已失效（再 Enter 重 preview）
            self.assertEqual(user.listing_calls, 2)
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(len(user.preview_calls), 2)

    async def test_q_quits_from_compose(self):
        """W7 空态（零 selection + 零草稿）：单 q 即退（既有路径）。"""
        user = _UserSurfaceRecorder(_entries("rt-a", "rt-b"))
        app = make_compose_app(funnel_composition(), _StartRecorder([]),
                               user)
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.press("c")
            await pilot.press("q")
            for _ in range(20):
                if not app.is_running:
                    break
                await pilot.pause()
            self.assertFalse(app.is_running)
            self.assertEqual(user.start_calls, [])

    async def test_l_toggles_locale_labels(self):
        user = _UserSurfaceRecorder(_entries("rt-a", "rt-b"))
        app = make_compose_app(funnel_composition(), _StartRecorder([]),
                               user, task_token="do work")
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.press("c")
            await pilot.pause()
            self.assertIn("runtimes", _compose_text(app))
            await pilot.press("l")
            await pilot.pause()
            self.assertIn("运行时", _compose_text(app))
            self.assertIn("参与者", _compose_text(app))
            # domain 词不翻译
            self.assertIn("rt-a", _compose_text(app))

    async def test_ascii_fallback_and_narrow_width(self):
        user = _UserSurfaceRecorder(_entries("rt-a", "rt-b"))
        app = make_compose_app(funnel_composition(), _StartRecorder([]),
                               user, task_token="do work")
        async with app.run_test(size=(60, 24)) as pilot:
            await pilot.press("c")
            await pilot.pause()
            for line in _compose_text(app).splitlines():
                self.assertLessEqual(len(line), 60)
            app._cockpit_ascii = True
            app._compose_refresh()
            await pilot.pause()
            self.assertNotIn("←", _compose_text(app))
            self.assertNotIn("▶", _compose_text(app))

    async def test_default_enter_zero_migration(self):
        """无 c：漏斗 Enter 行为零迁移（default 启动；user 面零触碰）。"""
        user = _UserSurfaceRecorder(_entries("rt-a", "rt-b"))
        composition = funnel_composition()
        start = _StartRecorder([composed_run_double()])
        app = make_compose_app(composition, start, user,
                               task_token="plain task")
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.press("enter")
            for _ in range(100):
                if app._cockpit_composed is not None:
                    break
                await pilot.pause()
            self.assertEqual(start.calls, [("plain task", composition)])
            self.assertEqual(user.preview_calls, [])
            self.assertEqual(user.start_calls, [])
            self.assertFalse(app.query_one("#compose-screen").display)


class ComposeP2ReentryTests(unittest.IsolatedAsyncioTestCase):
    """W6：esc 保留 + 重入 listing 重取/失效过滤/光标 clamp/披露。"""

    async def test_esc_preserves_selection_and_reentry_restores(self):
        user = _UserSurfaceRecorder(_entries("rt-a", "rt-b", "rt-c"))
        app = make_compose_app(funnel_composition(), _StartRecorder([]),
                               user, task_token="do work")
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.press("c")
            await pilot.press("space")
            await pilot.press("down")
            await pilot.press("space")
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            self.assertFalse(app.query_one("#compose-screen").display)
            # 重入：listing 重取，selection/已知角色呈现保留
            await pilot.press("c")
            await pilot.pause()
            self.assertEqual(user.listing_calls, 2)
            text = _compose_text(app)
            self.assertIn("[x] rt-a", text)
            self.assertIn("[x] rt-b", text)
            self.assertIn("member-1", text)
            # esc 已使披露失效：重入 phase 回 preview（W3 经 TUI）
            self.assertIn("press enter to preview", text)
            self.assertEqual(user.preview_calls, [])

    async def test_reentry_filters_disappeared_and_discloses(self):
        user = _UserSurfaceRecorder(
            _entries("rt-a", "rt-b", "rt-c"),
            preview_results=[(SimpleNamespace(members=()),
                              resolved_double())])
        app = make_compose_app(funnel_composition(), _StartRecorder([]),
                               user, task_token="do work")
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.press("c")
            await pilot.press("space")
            await pilot.press("down")
            await pilot.press("space")
            await pilot.press("escape")
            # 池漂移：rt-a 消失
            user.entries = _entries("rt-b", "rt-c")
            await pilot.press("c")
            await pilot.pause()
            text = _compose_text(app)
            self.assertEqual(user.listing_calls, 2)
            # 失效 runtime 从 selection 丢弃 + 诚实披露（零静默修复）
            self.assertNotIn("[x] rt-a", text)
            self.assertNotIn("[ ] rt-a", text)
            self.assertIn("removed from selection: rt-a", text)
            self.assertIn("[x] rt-b", text)
            # 披露失效：Enter 重 preview，绝不带旧 expected start
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(len(user.preview_calls), 1)
            self.assertEqual(user.start_calls, [])

    async def test_reentry_clamps_cursor(self):
        user = _UserSurfaceRecorder(
            _entries(*[f"rt-{index}" for index in range(8)]))
        app = make_compose_app(funnel_composition(), _StartRecorder([]),
                               user, task_token="do work")
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.press("c")
            for _ in range(7):
                await pilot.press("down")
            await pilot.pause()
            self.assertIn("▶ [ ] rt-7", _compose_text(app))
            await pilot.press("escape")
            # 池收缩 8 → 3：cursor 7 必须钳到 2
            user.entries = _entries("rt-a", "rt-b", "rt-c")
            await pilot.press("c")
            await pilot.pause()
            self.assertIn("▶ [ ] rt-c", _compose_text(app))


class ComposeP2GuardedQuitTests(unittest.IsolatedAsyncioTestCase):
    """W7：有可失状态（selection 或草稿）时 q 两段守卫 + disarm。"""

    async def test_q_guarded_by_draft_requires_second_press(self):
        user = _UserSurfaceRecorder(_entries("rt-a", "rt-b"))
        app = make_compose_app(funnel_composition(), _StartRecorder([]),
                               user, task_token="do work")
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.press("c")
            await pilot.press("q")
            await pilot.pause()
            self.assertTrue(app.is_running)
            self.assertIn("q again to quit", _compose_text(app))
            await pilot.press("q")
            for _ in range(20):
                if not app.is_running:
                    break
                await pilot.pause()
            self.assertFalse(app.is_running)

    async def test_q_guarded_by_selection_alone(self):
        user = _UserSurfaceRecorder(_entries("rt-a", "rt-b"))
        app = make_compose_app(funnel_composition(), _StartRecorder([]),
                               user)
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.press("c")
            await pilot.press("space")
            await pilot.press("q")
            await pilot.pause()
            self.assertTrue(app.is_running)
            self.assertIn("q again to quit", _compose_text(app))

    async def test_q_guard_disarmed_by_other_key(self):
        user = _UserSurfaceRecorder(_entries("rt-a", "rt-b"))
        app = make_compose_app(funnel_composition(), _StartRecorder([]),
                               user, task_token="do work")
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.press("c")
            await pilot.press("q")
            await pilot.pause()
            self.assertTrue(app._cockpit_compose_q_armed)
            # 任意其它键 disarm + 守卫横幅撤下
            await pilot.press("down")
            await pilot.pause()
            self.assertFalse(app._cockpit_compose_q_armed)
            self.assertNotIn("q again to quit", _compose_text(app))
            self.assertTrue(app.is_running)
            # disarm 后单 q 重新武装而非退出
            await pilot.press("q")
            await pilot.pause()
            self.assertTrue(app.is_running)
            self.assertTrue(app._cockpit_compose_q_armed)

    async def test_q_guard_disarmed_by_escape(self):
        user = _UserSurfaceRecorder(_entries("rt-a", "rt-b"))
        app = make_compose_app(funnel_composition(), _StartRecorder([]),
                               user, task_token="do work")
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.press("c")
            await pilot.press("q")
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            # 换屏 disarm：回漏斗
            self.assertTrue(app.query_one("#funnel-screen").display)
            self.assertFalse(app._cockpit_compose_q_armed)
            await pilot.press("c")
            await pilot.press("q")
            await pilot.pause()
            # 重入后单 q 仍守卫（armed 已被 esc 清零）
            self.assertTrue(app.is_running)
            self.assertIn("q again to quit", _compose_text(app))


class ComposeP2WiringTests(unittest.IsolatedAsyncioTestCase):
    """W8/W4 接线：TUI 向 projection 供 width/height（纯呈现参数）。"""

    async def test_height_wiring_windows_pool(self):
        user = _UserSurfaceRecorder(
            _entries(*[f"rt-{index}" for index in range(8)]))
        app = make_compose_app(funnel_composition(), _StartRecorder([]),
                               user, task_token="do work")
        async with app.run_test(size=(100, 12)) as pilot:
            await pilot.press("c")
            await pilot.pause()
            text = _compose_text(app)
            # 高度预算收紧：池被窗口化，cursor 行恒可见 + 溢出行
            self.assertIn("▶ [ ] rt-0", text)
            self.assertIn("(+3 more lines)", text)
            for _ in range(7):
                await pilot.press("down")
            await pilot.pause()
            self.assertIn("▶ [ ] rt-7", _compose_text(app))

    async def test_width_wiring_display_name(self):
        entry = SimpleNamespace(runtime_id="rt-a", provider_id="prov-a",
                                display_name="Extra Name")
        user = _UserSurfaceRecorder((entry,))
        app = make_compose_app(funnel_composition(), _StartRecorder([]),
                               user, task_token="do work")
        async with app.run_test(size=(120, 24)) as pilot:
            await pilot.press("c")
            await pilot.pause()
            self.assertIn("Extra Name", _compose_text(app))
        user = _UserSurfaceRecorder((entry,))
        app = make_compose_app(funnel_composition(), _StartRecorder([]),
                               user, task_token="do work")
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.press("c")
            await pilot.pause()
            self.assertNotIn("Extra Name", _compose_text(app))


class ComposeP2JourneyTests(unittest.IsolatedAsyncioTestCase):
    """North Star 冷启动旅程（授权 §五）：EN 与 ZH 双语各一条。"""

    async def test_cold_start_journey_en(self):
        gate = _Gate()
        intent_double = SimpleNamespace(members=())
        resolved = resolved_double()
        user = _UserSurfaceRecorder(
            _entries("rt-a", "rt-b"),
            preview_results=[(intent_double, resolved)],
            start_results=[composed_run_double(gate=gate)])
        app = make_compose_app(funnel_composition(), _StartRecorder([]),
                               user, task_token="compose task")
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.press("c")
            self.assertIn("press enter to preview", _compose_text(app))
            await pilot.press("space")
            await pilot.press("down")
            await pilot.press("space")
            await pilot.press("enter")
            await pilot.pause()
            self.assertIn("plan ready — enter to start",
                          _compose_text(app))
            await pilot.press("enter")
            for _ in range(100):
                if app._cockpit_composed is not None:
                    break
                await pilot.pause()
            self.assertEqual(app._cockpit_stage, cockpit_tui.STAGE_RUNNING)
            self.assertFalse(app.query_one("#compose-screen").display)
            gate.release.set()
            for _ in range(200):
                if app.outcome is not None:
                    break
                await pilot.pause()
        self.assertEqual(app.outcome, "compose-outcome")
        task_text, intent, expected = user.start_calls[0]
        self.assertEqual(task_text, "compose task")
        self.assertIs(intent, intent_double)
        self.assertIs(expected, resolved)

    async def test_cold_start_journey_zh(self):
        gate = _Gate()
        intent_double = SimpleNamespace(members=())
        resolved = resolved_double()
        user = _UserSurfaceRecorder(
            _entries("rt-a", "rt-b"),
            preview_results=[(intent_double, resolved)],
            start_results=[composed_run_double(gate=gate)])
        app = make_compose_app(funnel_composition(), _StartRecorder([]),
                               user, task_token="compose task")
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.press("c")
            await pilot.press("l")
            await pilot.pause()
            self.assertIn("按 enter 预览", _compose_text(app))
            await pilot.press("space")
            await pilot.press("down")
            await pilot.press("space")
            await pilot.press("enter")
            await pilot.pause()
            self.assertIn("计划已就绪 — 按 enter 启动", _compose_text(app))
            await pilot.press("enter")
            for _ in range(100):
                if app._cockpit_composed is not None:
                    break
                await pilot.pause()
            self.assertEqual(app._cockpit_stage, cockpit_tui.STAGE_RUNNING)
            gate.release.set()
            for _ in range(200):
                if app.outcome is not None:
                    break
                await pilot.pause()
        self.assertEqual(app.outcome, "compose-outcome")
        self.assertIs(user.start_calls[0][2], resolved)


# ----------------------------------------------- 2.8-C GROUP_AUTHORING


class GroupAuthoringModeTests(unittest.IsolatedAsyncioTestCase):
    """ERRATA-1：g 恒不退出模式；仅 Enter/Esc 回 BASE；六态链。"""

    async def _enter_compose_with_two(self, pilot):
        await pilot.press("c")
        await pilot.press("space")
        await pilot.press("down")
        await pilot.press("space")
        await pilot.pause()

    async def test_g_enters_mode_and_stays_after_create(self):
        """A1/A3 负向：g 创建后模式位仍 True（不存在 g→BASE 路径）。"""
        user = _UserSurfaceRecorder(_entries("rt-a", "rt-b", "rt-c"))
        app = make_compose_app(funnel_composition(), _StartRecorder([]),
                               user, task_token="do work")
        async with app.run_test(size=(100, 24)) as pilot:
            await self._enter_compose_with_two(pilot)
            await pilot.press("g")
            await pilot.pause()
            self.assertTrue(app._cockpit_compose_group_mode)
            self.assertIn("g new group", _compose_text(app))
            await pilot.press("g")  # 创建 g1 → 保持模式（负向 g→BASE）
            await pilot.pause()
            self.assertTrue(app._cockpit_compose_group_mode)
            self.assertEqual(
                app._cockpit_compose_groups, (("g1", ("rt-a",)),))
            self.assertEqual(app._cockpit_compose_group_seq, 2)

    async def test_enter_and_esc_return_base_with_draft_preserved(self):
        """A2：Enter/Esc 仅回 BASE（draft 保留 W6；不触发 preview）。"""
        user = _UserSurfaceRecorder(_entries("rt-a", "rt-b", "rt-c"))
        app = make_compose_app(funnel_composition(), _StartRecorder([]),
                               user, task_token="do work")
        async with app.run_test(size=(100, 24)) as pilot:
            await self._enter_compose_with_two(pilot)
            await pilot.press("g")
            await pilot.press("g")  # 创建 g1
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            self.assertFalse(app._cockpit_compose_group_mode)
            self.assertTrue(app.query_one("#compose-screen").display)
            self.assertEqual(
                app._cockpit_compose_groups, (("g1", ("rt-a",)),))
            self.assertEqual(user.preview_calls, [])
            # 再进模式 → esc 同律（draft 保留）
            await pilot.press("g")
            await pilot.pause()
            self.assertTrue(app._cockpit_compose_group_mode)
            await pilot.press("escape")
            await pilot.pause()
            self.assertFalse(app._cockpit_compose_group_mode)
            self.assertTrue(app.query_one("#compose-screen").display)
            self.assertFalse(app.query_one("#funnel-screen").display)
            self.assertEqual(
                app._cockpit_compose_groups, (("g1", ("rt-a",)),))

    async def test_esc_ladder_mode_base_funnel(self):
        """esc 阶梯：模式 → BASE → FUNNEL（两跳；draft 保留）。"""
        user = _UserSurfaceRecorder(_entries("rt-a", "rt-b"))
        app = make_compose_app(funnel_composition(), _StartRecorder([]),
                               user, task_token="do work")
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.press("c")
            await pilot.press("space")
            await pilot.press("g")
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            # 第一跳：模式 → BASE（仍在 COMPOSE）
            self.assertTrue(app.query_one("#compose-screen").display)
            await pilot.press("escape")
            await pilot.pause()
            # 第二跳：BASE → FUNNEL
            self.assertTrue(app.query_one("#funnel-screen").display)

    async def test_empty_selection_g_honest_reject(self):
        user = _UserSurfaceRecorder(_entries("rt-a", "rt-b"))
        app = make_compose_app(funnel_composition(), _StartRecorder([]),
                               user, task_token="do work")
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.press("c")
            await pilot.press("g")
            await pilot.pause()
            self.assertFalse(app._cockpit_compose_group_mode)
            self.assertIn("select a runtime first", _compose_text(app))

    async def test_q_guard_unchanged_in_mode(self):
        """A5：模式内 q = 既有 W7 两段守卫（非模式退出）。"""
        user = _UserSurfaceRecorder(_entries("rt-a", "rt-b"))
        app = make_compose_app(funnel_composition(), _StartRecorder([]),
                               user, task_token="do work")
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.press("c")
            await pilot.press("space")
            await pilot.press("g")
            await pilot.pause()
            await pilot.press("q")
            await pilot.pause()
            self.assertTrue(app.is_running)
            self.assertTrue(app._cockpit_compose_group_mode)
            self.assertIn("q again to quit", _compose_text(app))
            # 任意非 q 键 disarm（模式键同样生效）
            await pilot.press("g")
            await pilot.pause()
            self.assertFalse(app._cockpit_compose_q_armed)
            self.assertTrue(app._cockpit_compose_group_mode)


class GroupAuthoringKeysTests(unittest.IsolatedAsyncioTestCase):
    """§10 矩阵：space 环 / x 解散 / up/down/r/H 显式 no-op / ←→。"""

    async def _select_and_enter_mode(self, pilot, count):
        await pilot.press("c")
        for _ in range(count):
            await pilot.press("space")
            await pilot.press("down")
        await pilot.press("g")
        await pilot.pause()

    async def test_space_ring_joins_moves_and_leaves(self):
        user = _UserSurfaceRecorder(_entries("rt-a", "rt-b", "rt-c"))
        app = make_compose_app(funnel_composition(), _StartRecorder([]),
                               user, task_token="do work")
        async with app.run_test(size=(100, 24)) as pilot:
            await self._select_and_enter_mode(pilot, 3)
            # rt-b 建组 → space 出组（末成员移出 → 解散 + 披露）
            await pilot.press("right")   # rt-b
            await pilot.press("g")       # g1=(rt-b)
            await pilot.press("right")   # rt-c
            await pilot.press("g")       # g2=(rt-c)
            await pilot.pause()
            await pilot.press("space")   # rt-c 出组 → g2 解散
            await pilot.pause()
            self.assertEqual(
                app._cockpit_compose_groups, (("g1", ("rt-b",)),))
            self.assertIn("group g2 dissolved", _compose_text(app))
            await pilot.press("space")   # rt-c 入 g1（环下一站）
            await pilot.pause()
            self.assertEqual(
                app._cockpit_compose_groups,
                (("g1", ("rt-b", "rt-c")),))
            await pilot.press("left")    # 回 rt-b（组内声明位序第二）
            await pilot.press("space")   # rt-b 移组：g1 → 环下一 = ungrouped
            await pilot.pause()
            self.assertEqual(
                app._cockpit_compose_groups, (("g1", ("rt-c",)),))

    async def test_space_zero_groups_noop(self):
        user = _UserSurfaceRecorder(_entries("rt-a", "rt-b"))
        app = make_compose_app(funnel_composition(), _StartRecorder([]),
                               user, task_token="do work")
        async with app.run_test(size=(100, 24)) as pilot:
            await self._select_and_enter_mode(pilot, 2)
            await pilot.press("space")
            await pilot.pause()
            self.assertEqual(app._cockpit_compose_groups, ())
            self.assertTrue(app._cockpit_compose_group_mode)

    async def test_x_dissolves_and_ungrouped_rejects(self):
        user = _UserSurfaceRecorder(_entries("rt-a", "rt-b", "rt-c"))
        app = make_compose_app(funnel_composition(), _StartRecorder([]),
                               user, task_token="do work")
        async with app.run_test(size=(100, 24)) as pilot:
            await self._select_and_enter_mode(pilot, 3)
            await pilot.press("right")   # rt-b
            await pilot.press("g")       # g1=(rt-b)
            await pilot.press("right")   # rt-c
            await pilot.press("space")   # rt-c 入 g1 → 两成员
            await pilot.pause()
            await pilot.press("x")       # 解散 g1（2 名成员移出）
            await pilot.pause()
            self.assertEqual(app._cockpit_compose_groups, ())
            self.assertIn("group g1 dissolved · 2 member(s) ungrouped",
                          _compose_text(app))
            # ungrouped participant x = honest no-op
            await pilot.press("x")
            await pilot.pause()
            self.assertIn("no group to dissolve", _compose_text(app))
            self.assertTrue(app._cockpit_compose_group_mode)

    async def test_up_down_r_h_are_noops_in_mode(self):
        user = _UserSurfaceRecorder(
            _entries("rt-a", "rt-b", "rt-c", "rt-d"))
        app = make_compose_app(funnel_composition(), _StartRecorder([]),
                               user, task_token="do work")
        async with app.run_test(size=(100, 24)) as pilot:
            await self._select_and_enter_mode(pilot, 4)
            await pilot.press("g")       # g1=(rt-a)
            await pilot.press("r")       # 模式内 no-op
            await pilot.press("up")
            await pilot.press("down")
            await pilot.press("H")
            await pilot.pause()
            self.assertTrue(app._cockpit_compose_group_mode)
            self.assertEqual(app._cockpit_compose_cursor, 3)  # 4 行池钳位
            self.assertEqual(app._cockpit_compose_roles, {})
            self.assertEqual(
                app._cockpit_compose_groups, (("g1", ("rt-a",)),))

    async def test_arrows_move_participant_cursor_in_mode(self):
        user = _UserSurfaceRecorder(_entries("rt-a", "rt-b", "rt-c"))
        app = make_compose_app(funnel_composition(), _StartRecorder([]),
                               user, task_token="do work")
        async with app.run_test(size=(100, 24)) as pilot:
            await self._select_and_enter_mode(pilot, 3)
            await pilot.press("right")
            await pilot.press("right")
            await pilot.pause()
            self.assertEqual(app._cockpit_compose_pcursor, 2)
            await pilot.press("left")
            await pilot.pause()
            self.assertEqual(app._cockpit_compose_pcursor, 1)


class GroupCreationPreconditionTests(unittest.IsolatedAsyncioTestCase):
    """ERRATA-2：拒绝 = honest no-op（零 draft 变更 ⇒ 零 invalidate）。"""

    async def test_grouped_member_g_is_zero_mutation_noop(self):
        """grouped+g：draft 逐字节不变 + 披露不被 invalidate + 消息。"""
        user = _UserSurfaceRecorder(
            _entries("rt-a", "rt-b", "rt-c"),
            preview_results=[
                (SimpleNamespace(members=()), resolved_double()),
                (SimpleNamespace(members=()), resolved_double())])
        app = make_compose_app(funnel_composition(), _StartRecorder([]),
                               user, task_token="do work")
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.press("c")
            await pilot.press("space")
            await pilot.press("down")
            await pilot.press("space")
            await pilot.press("enter")   # preview #1（无组）
            await pilot.pause()
            fingerprint = app._cockpit_compose_fp
            self.assertIsNotNone(fingerprint)
            await pilot.press("g")       # 进模式
            await pilot.press("g")       # 创建 g1 → invalidate → fp None
            await pilot.pause()
            self.assertIsNone(app._cockpit_compose_fp)
            await pilot.press("escape")  # 回 BASE → preview #2（带组）
            await pilot.press("enter")
            await pilot.pause()
            fingerprint2 = app._cockpit_compose_fp
            self.assertIsNotNone(fingerprint2)
            before = app._cockpit_compose_groups
            await pilot.press("g")       # 再进模式（当前 rt-a 已分组）
            await pilot.press("g")       # 拒绝：honest no-op
            await pilot.pause()
            self.assertEqual(app._cockpit_compose_groups, before)
            # 零 invalidate：披露仍有效（fp 未被清）+ 诚实消息
            self.assertEqual(app._cockpit_compose_fp, fingerprint2)
            self.assertIn("member already grouped", _compose_text(app))

    async def test_group_limit_reached_is_zero_mutation_noop(self):
        """上限分支（AC-C-GROUP-03）：注入 4 组防御态——数学上 UI
        结构性不可达（4 组 ⇒ 全员已分组，先命中 already-grouped
        分支）；此处直注 draft 覆盖防御分支行为契约本身。"""
        user = _UserSurfaceRecorder(_entries("rt-a", "rt-b", "rt-c"))
        app = make_compose_app(funnel_composition(), _StartRecorder([]),
                               user, task_token="do work")
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.press("c")
            await pilot.press("space")
            await pilot.press("down")
            await pilot.press("space")
            await pilot.press("down")
            await pilot.press("space")
            await pilot.pause()
            # 直注：4 组在场（含跨组引用的非法防御态——仅测分支契约）
            app._cockpit_compose_groups = (
                ("g1", ("rt-b",)), ("g2", ("rt-c",)),
                ("g3", ("rt-b",)), ("g4", ("rt-c",)))
            await pilot.press("g")   # 进模式；当前 rt-a ungrouped
            await pilot.pause()
            await pilot.press("g")   # 组数=4 → 拒绝
            await pilot.pause()
            self.assertEqual(len(app._cockpit_compose_groups), 4)
            self.assertEqual(app._cockpit_compose_group_seq, 1)
            self.assertIn("group limit reached", _compose_text(app))
            self.assertTrue(app._cockpit_compose_group_mode)


class GroupIdLifecycleTests(unittest.IsolatedAsyncioTestCase):
    """ERRATA-3：g1,g2,g3 → 删 g2 → g1,g3 → create → g4（无复用/
    无重编号；声明序 ≠ id 数值序）；/new 双清 draft+seq。"""

    async def test_canonical_lifecycle_g4_after_deletion(self):
        user = _UserSurfaceRecorder(_entries("rt-a", "rt-b", "rt-c",
                                             "rt-d"))
        app = make_compose_app(funnel_composition(), _StartRecorder([]),
                               user, task_token="do work")
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.press("c")
            for _ in range(4):
                await pilot.press("space")
                await pilot.press("down")
            await pilot.pause()
            await pilot.press("g")
            await pilot.press("g")                     # g1=(rt-a)
            await pilot.press("right")
            await pilot.press("g")                     # g2=(rt-b)
            await pilot.press("right")
            await pilot.press("g")                     # g3=(rt-c)
            await pilot.pause()
            self.assertEqual(
                tuple(gid for gid, _ in app._cockpit_compose_groups),
                ("g1", "g2", "g3"))
            await pilot.press("left")                  # rt-b
            await pilot.press("x")                     # 解散 g2
            await pilot.pause()
            self.assertEqual(
                app._cockpit_compose_groups,
                (("g1", ("rt-a",)), ("g3", ("rt-c",))))
            await pilot.press("right")
            await pilot.press("right")                 # rt-d ungrouped
            await pilot.press("g")                     # → g4（非 g2 复用）
            await pilot.pause()
            self.assertEqual(
                app._cockpit_compose_groups,
                (("g1", ("rt-a",)), ("g3", ("rt-c",)),
                 ("g4", ("rt-d",))))
            self.assertEqual(app._cockpit_compose_group_seq, 5)
            # 声明序 ≠ 数值连续：draft 序 g1,g3,g4（无重排/重编号）
            self.assertIn("g1: ", _compose_text(app))
            self.assertIn("g3: ", _compose_text(app))
            self.assertIn("g4: ", _compose_text(app))

    async def test_new_session_clear_resets_draft_and_sequence(self):
        """/new 双清：draft 与 seq 同时归零（绝不只清其一）。"""
        user = _UserSurfaceRecorder(_entries("rt-a", "rt-b"))
        app = make_compose_app(funnel_composition(), _StartRecorder([]),
                               user, task_token="do work")
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.press("c")
            await pilot.press("space")
            await pilot.press("g")
            await pilot.press("g")
            await pilot.pause()
            self.assertEqual(
                app._cockpit_compose_groups, (("g1", ("rt-a",)),))
            self.assertEqual(app._cockpit_compose_group_seq, 2)
            # esc 阶梯出 COMPOSE（模式 → BASE → FUNNEL 两跳）再 /new
            await pilot.press("escape")
            await pilot.press("escape")
            await pilot.pause()
            # /new 兑现（y 确认后调用的纯呈现遗忘点）
            app._new_session_clear()
            self.assertEqual(app._cockpit_compose_groups, ())
            self.assertEqual(app._cockpit_compose_group_seq, 1)
            self.assertFalse(app._cockpit_compose_group_mode)
            # 归零后再创建 → g1 重新从 1 起（全新草稿域）。
            # 重入后 selection 保留 [rt-a]（W6）——直接进模式建组。
            await pilot.press("c")
            await pilot.press("g")
            await pilot.press("g")
            await pilot.pause()
            self.assertEqual(
                app._cockpit_compose_groups, (("g1", ("rt-a",)),))
            self.assertEqual(app._cockpit_compose_group_seq, 2)


class GroupPruneHookTests(unittest.IsolatedAsyncioTestCase):
    """§11 剪除钩子：BASE 勾选剔除 / 重入失效 → 组剪除+空组解散+
    披露行（draft ⊆ selection 不变量维持）。"""

    async def test_toggle_off_grouped_member_prunes_group(self):
        user = _UserSurfaceRecorder(
            _entries("rt-a", "rt-b", "rt-c"),
            preview_results=[])
        app = make_compose_app(funnel_composition(), _StartRecorder([]),
                               user, task_token="do work")
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.press("c")
            await pilot.press("space")          # rt-a
            await pilot.press("down")
            await pilot.press("space")          # rt-b
            await pilot.press("g")
            await pilot.press("g")              # g1=(rt-a)
            await pilot.press("escape")         # 回 BASE（draft 保留）
            await pilot.pause()
            # 池光标回 rt-a（勾选序后光标在 rt-b——up 一步）
            await pilot.press("up")
            await pilot.press("space")          # 剔除 rt-a → g1 空解散
            await pilot.pause()
            self.assertEqual(app._cockpit_compose_groups, ())
            self.assertIn("group g1 dissolved", _compose_text(app))
            self.assertNotIn("rt-a  [g1]", _compose_text(app))

    async def test_reentry_prunes_stale_grouped_member(self):
        user = _UserSurfaceRecorder(_entries("rt-a", "rt-b", "rt-c"))
        app = make_compose_app(funnel_composition(), _StartRecorder([]),
                               user, task_token="do work")
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.press("c")
            await pilot.press("space")
            await pilot.press("g")
            await pilot.press("g")              # g1=(rt-a)
            # esc 阶梯两跳出 COMPOSE（模式 → BASE → FUNNEL）
            await pilot.press("escape")
            await pilot.press("escape")
            # 池漂移：rt-a 失效
            user.entries = _entries("rt-b", "rt-c")
            await pilot.press("c")
            await pilot.pause()
            self.assertEqual(app._cockpit_compose_groups, ())
            text = _compose_text(app)
            self.assertIn("removed from selection: rt-a", text)
            self.assertIn("group g1 dissolved", text)


class GroupPreviewIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """组随 Enter 两段入链（draft → preview 铸 intent）；stamp 含组
    维度（组变更必重 preview）；拒绝态不 invalidate（披露有效直达
    start）；六态链 FUNNEL→BASE→MODE→BASE→PREVIEW→RUNNING。"""

    async def test_full_journey_passes_groups_to_preview_and_start(self):
        gate = _Gate()
        intent_double = SimpleNamespace(members=())
        resolved = resolved_double()
        user = _UserSurfaceRecorder(
            _entries("rt-a", "rt-b", "rt-c"),
            preview_results=[
                (intent_double, resolved), (intent_double, resolved)],
            start_results=[composed_run_double(gate=gate)])
        app = make_compose_app(funnel_composition(), _StartRecorder([]),
                               user, task_token="compose task")
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.press("c")
            await pilot.press("space")
            await pilot.press("down")
            await pilot.press("space")
            await pilot.press("down")
            await pilot.press("space")
            # MODE：建组 g1=(rt-a)（六态链第三态）
            await pilot.press("g")
            await pilot.press("g")
            await pilot.press("right")
            await pilot.press("space")          # rt-b 入 g1
            await pilot.pause()
            await pilot.press("enter")          # 回 BASE
            await pilot.press("enter")          # preview #1（带组）
            await pilot.pause()
            self.assertEqual(len(user.preview_calls), 1)
            _selection, preview_groups = user.preview_calls[0]
            self.assertEqual(preview_groups,
                             (("g1", ("rt-a", "rt-b")),))
            # 组变更（再入模式 rt-c 入组）→ invalidate → 必重 preview
            await pilot.press("g")
            await pilot.press("right")
            await pilot.press("right")
            await pilot.press("space")          # rt-c 入 g1
            await pilot.press("enter")
            await pilot.press("enter")          # preview #2（新组态）
            await pilot.pause()
            self.assertEqual(len(user.preview_calls), 2)
            _selection, preview_groups2 = user.preview_calls[1]
            self.assertEqual(preview_groups2,
                             (("g1", ("rt-a", "rt-b", "rt-c")),))
            # PREVIEW → RUNNING：start 用同一 intent/expected
            await pilot.press("enter")
            for _ in range(100):
                if app._cockpit_composed is not None:
                    break
                await pilot.pause()
            self.assertEqual(app._cockpit_stage, cockpit_tui.STAGE_RUNNING)
            _task, intent, expected = user.start_calls[0]
            self.assertIs(intent, intent_double)
            self.assertIs(expected, resolved)
            # 换屏归零模式位（draft 本体保留 W6）
            self.assertFalse(app._cockpit_compose_group_mode)
            self.assertEqual(app._cockpit_compose_groups,
                             (("g1", ("rt-a", "rt-b", "rt-c")),))
            gate.release.set()
            for _ in range(200):
                if app.outcome is not None:
                    break
                await pilot.pause()

    async def test_rejected_noop_keeps_disclosure_valid_to_start(self):
        """拒绝态零 invalidate 的最强形式：grouped+g 后 Enter 直接
        start（披露未被污染——stamp 匹配）。"""
        gate = _Gate()
        intent_double = SimpleNamespace(members=())
        resolved = resolved_double()
        user = _UserSurfaceRecorder(
            _entries("rt-a", "rt-b", "rt-c"),
            preview_results=[(intent_double, resolved)],
            start_results=[composed_run_double(gate=gate)])
        app = make_compose_app(funnel_composition(), _StartRecorder([]),
                               user, task_token="compose task")
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.press("c")
            await pilot.press("space")
            await pilot.press("down")
            await pilot.press("space")
            await pilot.press("g")
            await pilot.press("g")              # g1=(rt-a)
            await pilot.press("enter")          # 回 BASE
            await pilot.press("enter")          # preview（带组）
            await pilot.pause()
            self.assertEqual(len(user.preview_calls), 1)
            await pilot.press("g")              # 进模式
            await pilot.press("g")              # rt-a 已分组 → 拒绝
            await pilot.pause()
            self.assertEqual(len(user.preview_calls), 1)
            await pilot.press("escape")         # 回 BASE
            await pilot.press("enter")          # 披露仍有效 → start
            for _ in range(100):
                if app._cockpit_composed is not None:
                    break
                await pilot.pause()
            self.assertEqual(len(user.start_calls), 1)
            self.assertEqual(app._cockpit_stage, cockpit_tui.STAGE_RUNNING)
            gate.release.set()
            for _ in range(200):
                if app.outcome is not None:
                    break
                await pilot.pause()


if __name__ == "__main__":
    unittest.main()
