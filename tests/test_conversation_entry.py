"""2.8-A Conversational Container：会话容器测试面。

覆盖矩阵（授权 §测试覆盖）：
- Entry：会话级 task_id 铸造（唯一性/首铸字节对等/撞号消解）；
  双 surface 共享注入件（mint/emitted）跨面唯一；
- Transition：终态 → BETWEEN_RUNS 再入（漏斗复武装/换屏/interval
  收止/横幅）→ 轮间输入启动第二轮（分节线/主屏复现）；首跑漏斗
  斜杠=任务文本冻结律保持；
- Result：四态语义——COMPLETED/FAILED/ABORTED 入轮间镜像逐词、
  PARKED ≠ terminal（停驻唤醒续驱仍在原 run，绝不进轮间）；
- Control：轮间控制意图仍经注入 dispatcher → 死 run 边界诚实
  REJECTED 回执（CockpitSession 终态门不改，延续=新 run）；
- Projection：run 镜像/分节线/摘要为纯呈现（不成为 truth source
  ——投影输入仍读 run 事实闭包，绝不读呈现镜像）；
- Regression：单轮路径契约等价（无分节线、outcome 逐词、首铸
  task_id 与 host 逐字节一致）。

全部离线（scripted doubles / offline adapters）；REAL=0。
"""
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import cockpit_entry  # noqa: E402
import cockpit_tui  # noqa: E402
import host_entry  # noqa: E402
from candidate_validation import CandidateValidationStatus  # noqa: E402
from event_index import EventIndex  # noqa: E402
from execution_observation import ExecutionEventType  # noqa: E402
from external_runtime import (  # noqa: E402
    InvocationResult,
    InvocationStatus,
    InvocationTrace,
)

# CI 只装 .[test]（无 textual）：TUI 路径类经仓库自有 G3 探针类级跳过
# （同 test_cockpit_tui_rendering/test_cockpit_compose_tui 的守卫语义，
# 但本模块 47 测试中 27 个纯 entry 测试不依赖 textual，故类级而非模块级）。
_TEXTUAL_AVAILABLE = cockpit_tui.textual_available()


# ---------------------------------------------------------------- doubles


class _OfflineAdapter:
    """Offline double（test_compose_entry 同款）：invoke 记录 + 成功。"""

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


def _factories(*adapters):
    return [lambda bound=adapter: bound for adapter in adapters]


def _verified_evidence(registry, *runtime_ids):
    return {registry.get(rt).identity: SimpleNamespace(
        status=CandidateValidationStatus.VERIFIED)
        for rt in runtime_ids}


def _offline_adapters():
    return tuple(_OfflineAdapter(f"rt-{name}", f"prov-{name}")
                 for name in "ab")


class _StartRecorder:
    """注入 start 闭包双件：记录调用、按脚本回放结果。"""

    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def __call__(self, task_text, expected_composition):
        self.calls.append((task_text, expected_composition))
        return self.results.pop(0)


class _MutableSession:
    """可变 session 投影 double：terminal/run_state/last_outcome 由
    drive 脚本推进（呈现面只读三属性——真实 CockpitSession 同形状）。"""

    def __init__(self):
        self.run_state = None
        self.terminal = None
        self.last_outcome = None


def _outcome(status):
    return SimpleNamespace(status=SimpleNamespace(value=status))


def _conversation_double(task, script, gate=None):
    """会话组合句柄 double：drive 按 script 逐段推进 session 投影。

    script 每项 = "PARKED"（停驻：run_state 在场、无 terminal）或
    终态词（COMPLETED/FAILED/ABORTED：terminal 置词、run_state 清空）。
    gate 在场时 drive 先阻塞于其上（_Gate 律：RUNNING 视觉态的
    可观测窗口由测试掌控，瞬时 double 的 RUNNING 窗口仅微秒宽、
    断言必竞态）。
    """
    session = _MutableSession()
    calls = []

    def drive():
        if gate is not None:
            gate.wait(10)
        calls.append(1)
        status = script[min(len(calls) - 1, len(script) - 1)]
        session.last_outcome = _outcome(status)
        if status == "PARKED":
            session.run_state = object()
        else:
            session.run_state = None
            session.terminal = status
        return session.last_outcome

    return SimpleNamespace(
        task=task,
        steps=(("architect", "rt-a"), ("coder", "rt-b")),
        plan=(("step-0-architect", "architect", "rt-a", "prov-a"),
              ("step-1-coder", "coder", "rt-b", "prov-b")),
        task_id=f"tid-{task[:8]}", execution_id=f"cockpit-tid-{task[:8]}",
        emit=None, drive=drive, session=session,
        dispatch_control=lambda *args, **kwargs: None,
        revision_pending=lambda: 0,
        events=lambda: (), facts=lambda: (), usage=lambda: ())


def _funnel_composition():
    return SimpleNamespace(
        roles=("architect", "coder"),
        bindings=tuple(
            SimpleNamespace(role=role, runtime_id=runtime,
                            provider_id=provider,
                            canonical_runtime_identity=(
                                runtime, provider, None, "f"))
            for role, runtime, provider in
            (("architect", "rt-a", "prov-a"),
             ("coder", "rt-b", "prov-b"))),
        blocked_reason=None, blocked_hint=None)


def make_conversation_app(start):
    return cockpit_tui.CockpitApp(
        composition_preview=lambda: _funnel_composition(),
        start_composition=start)


def receipt_result(status="ACCEPTED", reason=None, version=1,
                   command_id="ui-1"):
    return SimpleNamespace(status=status, reason=reason,
                           execution_version=version,
                           command_id=command_id, execution_id="e")


def make_control(recorded, result=None):
    """注入 dispatcher double：记录 (kind, text, target) 意图元组。"""
    if result is None:
        result = receipt_result()

    def control(kind, text=None, target=None):
        recorded.append((kind, text, target))
        return result

    return control


async def _await_condition(pilot, app, predicate, budget=200):
    for _ in range(budget):
        if predicate():
            return True
        await pilot.pause()
    raise AssertionError("condition not met within pilot budget")


async def _start_first_run(pilot, app, task_text, start):
    """输入任务并提交，等待 start 闭包被调（确定性观测点——
    composed 置位后瞬时 double 的 RUNNING 窗口仅微秒宽，以其为
    等待条件必竞态；start 调用与 mount 在同一键处理栈内同步完成，
    观测到 calls 即可断言 mounted 事实）。"""
    for character in task_text:
        await pilot.press(character)
    await pilot.press("enter")
    await _await_condition(pilot, app, lambda: bool(start.calls))


async def _drive_to_between_runs(pilot, app):
    """推进到轮间态（终态经 refresh 观察后 composed 复空）。"""
    await _await_condition(
        pilot, app,
        lambda: (app._cockpit_composed is None
                 and bool(app._cockpit_runs)))


# ------------------------------------------------------------- Entry（mint）


class TaskIdMintTests(unittest.TestCase):
    """会话级 task_id 铸造：唯一性 / 首铸字节对等 / 撞号计数序标。"""

    def test_first_mint_is_byte_equal_to_host_opaque_id(self):
        mint = cockpit_entry._task_id_mint()
        task = "fix the login bug"
        self.assertEqual(mint(task),
                         host_entry._opaque_task_id(task))

    def test_same_task_mints_distinct_ids(self):
        mint = cockpit_entry._task_id_mint()
        first = mint("same task")
        second = mint("same task")
        third = mint("same task")
        self.assertEqual(first, host_entry._opaque_task_id("same task"))
        self.assertEqual(len({first, second, third}), 3)

    def test_distinct_tasks_keep_own_opaque_ids(self):
        mint = cockpit_entry._task_id_mint()
        one = mint("task one")
        two = mint("task two")
        self.assertEqual(one, host_entry._opaque_task_id("task one"))
        self.assertEqual(two, host_entry._opaque_task_id("task two"))
        self.assertNotEqual(one, two)

    def test_suffix_ladder_is_counted_and_stable(self):
        mint = cockpit_entry._task_id_mint()
        base = mint("repeat me")
        ids = [mint("repeat me") for _ in range(3)]
        self.assertEqual(ids, [f"{base}-2", f"{base}-3", f"{base}-4"])

    def test_source_has_no_while_loop(self):
        # ArchitectureGuard 同律：entry 层零第二编排循环（有界 for）。
        source = open(cockpit_entry.__file__, encoding="utf-8").read()
        self.assertNotIn("while ", source)


# -------------------------------------------------- Entry（surfaces 装配）


def _terminal_events(index, task_id):
    return tuple(
        event for event in index.snapshot(task_id)
        if getattr(getattr(event, "event_type", None), "value",
                   getattr(event, "event_type", None))
        == ExecutionEventType.TERMINAL)


class ConversationSurfaceTests(unittest.TestCase):
    """双 surface 会话装配：跨面共享 mint/emitted；同任务两 run 的
    task_id/execution_id 唯一；EventIndex 按 task_id 隔离；前轮
    TERMINAL 在 run N+1 装配时恰一次补发。全部离线 REAL=0。"""

    def _surfaces(self):
        registry, skipped = host_entry.environment_registry(
            _factories(*_offline_adapters()))
        evidence = _verified_evidence(registry, "rt-a", "rt-b")
        index = EventIndex()
        mint = cockpit_entry._task_id_mint()
        emitted = set()
        funnel = cockpit_entry._funnel_composition_closures(
            registry, skipped, evidence, timeout_seconds=None,
            event_index=index, task_id_mint=mint,
            terminal_emitted=emitted)
        user = cockpit_entry._user_composition_surface(
            registry, skipped, evidence, timeout_seconds=None,
            event_index=index, task_id_mint=mint,
            terminal_emitted=emitted)
        return funnel, user, index, emitted

    def test_two_runs_same_task_get_distinct_identity(self):
        funnel, _, _, _ = self._surfaces()
        first = funnel.start("repeat task", funnel.preview())
        second = funnel.start("repeat task", funnel.preview())
        self.assertNotEqual(first.task_id, second.task_id)
        self.assertNotEqual(first.execution_id, second.execution_id)
        self.assertEqual(len(funnel.composed_runs), 2)

    def test_event_index_groups_runs_by_task_id(self):
        funnel, _, index, _ = self._surfaces()
        first = funnel.start("task alpha", funnel.preview())
        second = funnel.start("task beta", funnel.preview())
        first.drive()
        second.drive()
        events_a = index.snapshot(first.task_id)
        events_b = index.snapshot(second.task_id)
        self.assertTrue(events_a)
        self.assertTrue(events_b)
        self.assertTrue(all(
            getattr(event, "task_id", None) == first.task_id
            for event in events_a))
        self.assertTrue(all(
            getattr(event, "task_id", None) == second.task_id
            for event in events_b))

    def test_prior_run_terminal_flushed_once_at_next_start(self):
        funnel, _, index, emitted = self._surfaces()
        first = funnel.start("flush task", funnel.preview())
        first.drive()   # 真实 offline 管线：SUCCESS → 终态 outcome
        self.assertNotIn(first.task_id, emitted)
        self.assertEqual(_terminal_events(index, first.task_id), ())
        funnel.start("flush task", funnel.preview())
        flushed = _terminal_events(index, first.task_id)
        self.assertEqual(len(flushed), 1)
        self.assertEqual(getattr(flushed[0], "status", None),
                         "COMPLETED")
        second = funnel.composed_runs[-1]
        self.assertEqual(_terminal_events(index, second.task_id), ())
        # 幂等：第三次启动不重复补发
        third = funnel.start("flush task", funnel.preview())
        self.assertEqual(
            len(_terminal_events(index, first.task_id)), 1)
        self.assertIn(first.execution_id, emitted)
        self.assertNotIn(second.execution_id, emitted)
        self.assertNotIn(third.execution_id, emitted)

    def test_single_run_never_flushes_before_exit(self):
        # 单轮路径字节等价核心：首 run 装配时 flush 为 no-op——
        # TERMINAL 仍由 post-App 兜底发射（2.7.0 位置不动）。
        funnel, _, index, emitted = self._surfaces()
        composed = funnel.start("solo task", funnel.preview())
        self.assertEqual(emitted, set())
        self.assertEqual(
            _terminal_events(index, composed.task_id), ())

    def test_cross_surface_mint_shared(self):
        funnel, user, _, _ = self._surfaces()
        first = funnel.start("shared task", funnel.preview())
        intent, resolved = user.preview(
            (("rt-a", "architect"), ("rt-b", "coder")))
        second = user.start("shared task", intent, resolved)
        self.assertNotEqual(first.task_id, second.task_id)


class EmitRunTerminalTests(unittest.TestCase):
    """TERMINAL 发射面抽出的逐字节同形钉定。"""

    def test_none_status_emits_nothing(self):
        emitted = []

        def emit(*args, **kwargs):
            emitted.append((args, kwargs))

        composed = SimpleNamespace(emit=emit, execution_id="e1")
        cockpit_entry._emit_run_terminal(composed, None)
        self.assertEqual(emitted, [])

    def test_status_word_verbatim(self):
        captured = {}

        def emit(event_type, *, stage, runtime_id, status, reason):
            captured.update(event_type=event_type, stage=stage,
                            runtime_id=runtime_id, status=status,
                            reason=reason)

        composed = SimpleNamespace(emit=emit, execution_id="e1")
        cockpit_entry._emit_run_terminal(composed, "FAILED")
        self.assertEqual(
            captured["event_type"], ExecutionEventType.TERMINAL)
        self.assertEqual(captured["stage"], "SEQUENTIAL")
        self.assertIsNone(captured["runtime_id"])
        self.assertEqual(captured["status"], "FAILED")
        self.assertEqual(captured["reason"], "FAILED")


# ------------------------------------------------- Transition / Result（TUI）


@unittest.skipUnless(_TEXTUAL_AVAILABLE, "textual not installed (.[test] lacks [tui])")
class BetweenRunsTransitionTests(unittest.IsolatedAsyncioTestCase):
    """终态 → 轮间再入 → 第二轮启动：漏斗复武装/换屏/interval 收止/
    分节线/新驱动线程；首跑斜杠冻结律；PARKED ≠ terminal。"""

    async def test_terminal_enters_between_runs_and_rearms_funnel(self):
        start = _StartRecorder([
            _conversation_double("conversation task", ["COMPLETED"])])
        app = make_conversation_app(start)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await _start_first_run(pilot, app, "my task", start)
            await _drive_to_between_runs(pilot, app)
            # 轮间镜像：[(task, steps, 终态词)]
            self.assertEqual(len(app._cockpit_runs), 1)
            task_text, steps, status = app._cockpit_runs[0]
            self.assertEqual(task_text, "conversation task")
            self.assertEqual(
                steps, (("architect", "rt-a"), ("coder", "rt-b")))
            self.assertEqual(status, "COMPLETED")
            # 漏斗复武装：composed 置空 → _funnel_pre_start 复真
            self.assertIsNone(app._cockpit_composed)
            self.assertTrue(app._funnel_pre_start())
            # 换屏：主屏观察区隐藏、漏斗屏在场（composer 恒钉底）
            self.assertTrue(app.query_one("#funnel-screen").display)
            self.assertFalse(app.query_one("#header-zone").display)
            self.assertFalse(app.query_one("#collab-zone").display)
            # interval 收止（轮间键驱动刷新律）
            self.assertIsNone(app._cockpit_interval)
            # 轮间横幅（闭集词表 + 终态词 format 注入）
            self.assertIn("next collaboration · run 1 COMPLETED",
                          app.funnel_text)
            # outcome 保留（退出码=末轮律的呈现侧前提）
            self.assertEqual(
                getattr(app.outcome.status, "value", None), "COMPLETED")

    async def test_between_runs_typing_starts_second_run(self):
        second_gate = threading.Event()
        start = _StartRecorder([
            _conversation_double("first of two", ["COMPLETED"]),
            _conversation_double("second task", ["FAILED"],
                                 gate=second_gate)])
        app = make_conversation_app(start)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await _start_first_run(pilot, app, "my task", start)
            await _drive_to_between_runs(pilot, app)
            for character in "next one":
                await pilot.press(character)
            await pilot.press("enter")
            await _await_condition(
                pilot, app, lambda: len(start.calls) == 2)
            self.assertEqual(
                [call[0] for call in start.calls],
                ["my task", "next one"])
            # 第二轮 late-bind + 主屏复现 + RUNNING（门控 double 使
            # RUNNING 窗口可观测——断言后放行续走到终态）
            self.assertIsNotNone(app._cockpit_composed)
            self.assertEqual(app._cockpit_stage,
                             cockpit_tui.STAGE_RUNNING)
            self.assertFalse(app.query_one("#funnel-screen").display)
            self.assertTrue(app.query_one("#header-zone").display)
            # 分节线恰一条（run 2 启动入 Log；run 1 无分节）
            self.assertIn("── run 2 · 2 agents · architect→coder ──",
                          app.log_text)
            # interval 单活跃律：重建且恰一个句柄
            self.assertIsNotNone(app._cockpit_interval)
            second_gate.set()
            await _drive_to_between_runs(pilot, app)
            # FAILED 逐词入镜像（Result 面）
            self.assertEqual(app._cockpit_runs[1][2], "FAILED")
            self.assertIn("next collaboration · run 2 FAILED",
                          app.funnel_text)

    async def test_first_run_slash_stays_task_text_frozen(self):
        # 首跑漏斗斜杠=任务文本（冻结律）：start 收到原文，零命令路由
        start = _StartRecorder([
            _conversation_double("/help", ["COMPLETED"])])
        app = make_conversation_app(start)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            for character in "/help":
                await pilot.press(character)
            await pilot.press("enter")
            await _await_condition(pilot, app, lambda: bool(start.calls))
            self.assertEqual(start.calls[0][0], "/help")

    async def test_between_runs_slash_routes_to_commands(self):
        # /help 在轮间是命令（非任务文本）：13 行入 Log、零新 run
        start = _StartRecorder([
            _conversation_double("slash task", ["COMPLETED"])])
        app = make_conversation_app(start)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await _start_first_run(pilot, app, "slash task", start)
            await _drive_to_between_runs(pilot, app)
            for character in "/help":
                await pilot.press(character)
            await pilot.press("enter")
            await pilot.pause()
            lines = app.log_text.splitlines()
            self.assertEqual(len(app._cockpit_runs), 1)
            self.assertTrue(any(
                line.startswith("/pause — ") for line in lines))
            self.assertTrue(any(
                line.startswith("/runs — ") for line in lines))
            self.assertTrue(any(
                line.startswith("/again — ") for line in lines))

    async def test_between_runs_control_gets_honest_rejected_receipt(self):
        # Control 面：轮间对死 run 的意图 → 注入 dispatcher → 诚实
        # REJECTED 回执入 Log（终态门不改，延续=新 run）
        recorded = []
        composed = _conversation_double(
            "control task", ["COMPLETED"])
        composed.dispatch_control = make_control(
            recorded, receipt_result(
                "REJECTED", reason="ALREADY_TERMINAL"))
        start = _StartRecorder([composed])
        app = make_conversation_app(start)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await _start_first_run(pilot, app, "ctrl task", start)
            await _drive_to_between_runs(pilot, app)
            for character in "/pause":
                await pilot.press(character)
            await pilot.press("enter")
            await pilot.pause()
        self.assertEqual(recorded, [("PAUSE", None, None)])
        self.assertIn("REJECTED", app.log_text)

    async def test_parked_is_not_terminal_and_stays_in_run(self):
        # PARKED ≠ terminal：停驻不进轮间（composed 保持、无换屏）；
        # 唤醒续驱至终态才再入。
        start = _StartRecorder([
            _conversation_double("parked task", ["PARKED", "COMPLETED"])])
        app = make_conversation_app(start)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await _start_first_run(pilot, app, "park", start)
            await _await_condition(
                pilot, app, lambda: app.outcome is not None)
            self.assertEqual(
                getattr(app.outcome.status, "value", None), "PARKED")
            for _ in range(20):
                await pilot.pause()
            # 停驻中：仍在原 run、无轮间换屏、镜像终态诚实缺席
            self.assertIsNotNone(app._cockpit_composed)
            self.assertFalse(app._funnel_pre_start())
            self.assertTrue(app.query_one("#header-zone").display)
            self.assertEqual(len(app._cockpit_runs), 1)
            self.assertIsNone(app._cockpit_runs[0][2])
            # 唤醒（私有同步原语——P-0x 测试同径）→ 续驱 → 终态
            app._cockpit_wake.set()
            await _drive_to_between_runs(pilot, app)
            self.assertEqual(app._cockpit_runs[0][2], "COMPLETED")
            self.assertTrue(app._funnel_pre_start())

    async def test_aborted_word_recorded(self):
        start = _StartRecorder([
            _conversation_double("abort task", ["ABORTED"])])
        app = make_conversation_app(start)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await _start_first_run(pilot, app, "abort task", start)
            await _drive_to_between_runs(pilot, app)
            self.assertEqual(app._cockpit_runs[0][2], "ABORTED")
            self.assertIn("next collaboration · run 1 ABORTED",
                          app.funnel_text)

    async def test_between_runs_projection_reads_run_truth_not_mirror(self):
        # Projection 面：轮间投影输入仍读 run 事实闭包（events 等），
        # 绝不读 runs 呈现镜像（UI ≠ truth）。
        events_seen = []

        def events():
            events_seen.append("read")
            return ("event-sentinel",)

        composed = _conversation_double("truth task", ["COMPLETED"])
        composed.events = events
        start = _StartRecorder([composed])
        app = make_conversation_app(start)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await _start_first_run(pilot, app, "truth task", start)
            await _drive_to_between_runs(pilot, app)
            values = app._collect_inputs()
            self.assertEqual(values.events, ("event-sentinel",))
            self.assertTrue(events_seen)

    async def test_runs_slash_lists_session_mirror(self):
        start = _StartRecorder([
            _conversation_double("mirror task", ["COMPLETED"])])
        app = make_conversation_app(start)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await _start_first_run(pilot, app, "mirror task", start)
            await _drive_to_between_runs(pilot, app)
            for character in "/runs":
                await pilot.press(character)
            await pilot.press("enter")
            await pilot.pause()
            lines = app.log_text.splitlines()
            self.assertTrue(any(line == "runs" for line in lines))
            self.assertTrue(any(
                line.startswith("run 1 · COMPLETED · ")
                and "mirror task" in line
                for line in lines))

    async def test_new_command_clears_display_history_only(self):
        # /new：确认门 → y → 呈现史清空（镜像/召回源/Log 显示缓存）；
        # 引擎事实零触碰（session 真相仍可读）
        double = _conversation_double("reset task", ["COMPLETED"])
        start = _StartRecorder([double])
        app = make_conversation_app(start)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await _start_first_run(pilot, app, "reset task", start)
            await _drive_to_between_runs(pilot, app)
            for character in "/new":
                await pilot.press(character)
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(app._cockpit_mode,
                             cockpit_tui.MODE_CONFIRM)
            await pilot.press("y")
            await pilot.pause()
            self.assertEqual(app._cockpit_runs, [])
            self.assertEqual(app._cockpit_last_task, "")
            self.assertIn(
                "session display cleared · run counter reset",
                app.log_text)
            # 事实面未动：session 真相仍在
            self.assertEqual(double.session.terminal, "COMPLETED")

    async def test_between_runs_q_quits_directly(self):
        # 轮间直退（零执行在飞，同漏斗律）——q 空缓冲 → 直接退出。
        # 任务文本不取 q 开头词：漏斗冻结键律 q-空缓冲=直退（§十二）。
        start = _StartRecorder([
            _conversation_double("leave task", ["COMPLETED"])])
        app = make_conversation_app(start)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await _start_first_run(pilot, app, "leave task", start)
            await _drive_to_between_runs(pilot, app)
            await pilot.press("q")
            await pilot.pause()
        self.assertFalse(app.is_running)


# ------------------------------------------------------------- Regression


@unittest.skipUnless(_TEXTUAL_AVAILABLE, "textual not installed (.[test] lacks [tui])")
class SingleRunRegressionTests(unittest.IsolatedAsyncioTestCase):
    """单轮路径契约等价：终态即 q——outcome 逐词、零分节线、
    首跑键律不变（呈现面换漏斗为 2.8 设计内变化，契约面零漂移）。"""

    async def test_single_run_quit_contract(self):
        start = _StartRecorder([
            _conversation_double("solo contract", ["COMPLETED"])])
        app = make_conversation_app(start)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await _start_first_run(pilot, app, "solo", start)
            await _drive_to_between_runs(pilot, app)
            # 契约面：outcome 逐词保留、恰一 run、零分节线、Log 零源
            self.assertEqual(
                getattr(app.outcome.status, "value", None), "COMPLETED")
            self.assertEqual(len(app._cockpit_runs), 1)
            self.assertEqual(app.log_text, "")
            await pilot.press("q")
            await pilot.pause()
        self.assertFalse(app.is_running)


class ProjectionPurityTests(unittest.TestCase):
    """run 镜像呈现函数纯度（不成为 truth source）。"""

    def test_run_divider_line_is_deterministic_pure_data(self):
        from cockpit_projection import run_divider_line
        steps = (("architect", "rt-a"), ("coder", "rt-b"))
        first = run_divider_line(2, steps)
        second = run_divider_line(2, steps)
        self.assertEqual(first, second)
        self.assertIn("run 2", first)
        self.assertIn("architect→coder", first)
        self.assertIn("2 agents", first)

    def test_runs_summary_lines_honest_about_inflight(self):
        from cockpit_projection import runs_summary_lines
        runs = [("task one", (("architect", "rt-a"),), None),
                ("task two", (("coder", "rt-b"),), "FAILED")]
        lines = runs_summary_lines(runs)
        self.assertEqual(lines[0], "runs")
        self.assertTrue(lines[1].startswith("run 1 · RUNNING · "))
        self.assertTrue(lines[2].startswith("run 2 · FAILED · "))

    def test_runs_summary_empty_is_honest_single_line(self):
        from cockpit_projection import runs_summary_lines
        self.assertEqual(runs_summary_lines(()),
                         ("no collaborations yet",))

    def test_summary_inputs_not_mutated(self):
        from cockpit_projection import runs_summary_lines
        runs = [("task", (("architect", "rt-a"),), "COMPLETED")]
        runs_summary_lines(runs)
        self.assertEqual(
            runs, [("task", (("architect", "rt-a"),), "COMPLETED")])


# -------------------------------------------------- 2.8-E record（呈现视图）


def _usage_record(invocation_id, usage_status, input_tokens=None,
                  output_tokens=None):
    from usage_log import UsageRecord
    return UsageRecord(
        invocation_id=invocation_id, task_id="tid-rec",
        agent_id=f"agent-{invocation_id}", role="coder",
        runtime_id="rt-b", status="SUCCESS",
        usage_status=usage_status,
        input_tokens=input_tokens, output_tokens=output_tokens)


class ConversationRecordBuilderTests(unittest.TestCase):
    """conversation_record 纯构建器：确定性/只读/status 回落/PARKED
    诚实缺席/usage 三态/截断。全部真类型或同形鸭（REAL=0）。"""

    def _composed(self, task="record task", usage=(), groups=(),
                  final=None, status="COMPLETED"):
        from usage_log import UsageObservation
        outcome = SimpleNamespace(
            status=SimpleNamespace(value=status), final_result=final)
        composed = SimpleNamespace(
            task=task, steps=(("architect", "rt-a"), ("coder", "rt-b")),
            plan=(("s0", "architect", "rt-a", "pa"),
                  ("s1", "coder", "rt-b", "pb")),
            groups=groups, member_ids=("m-1", "m-2"),
            task_id="tid-record", usage=lambda: tuple(usage))
        return composed, outcome

    def test_deterministic_and_readonly(self):
        from usage_log import UsageObservation
        usage = (_usage_record("i1", UsageObservation.KNOWN, 10, 4),)
        composed, outcome = self._composed(usage=usage,
                                           final="final text")
        first = cockpit_entry.conversation_record(
            composed, outcome, "DONE")
        second = cockpit_entry.conversation_record(
            composed, outcome, "DONE")
        self.assertEqual(first, second)
        # 只读：输入对象零改写
        self.assertEqual(composed.task, "record task")
        self.assertEqual(outcome.final_result, "final text")

    def test_fields_are_engine_snapshots(self):
        from usage_log import UsageObservation
        usage = (
            _usage_record("i1", UsageObservation.KNOWN, 100, 40),
            _usage_record("i2", UsageObservation.KNOWN, 23, 9),
            _usage_record("i3", UsageObservation.UNKNOWN),
            _usage_record("i4", UsageObservation.UNSUPPORTED),
        )
        composed, outcome = self._composed(
            usage=usage, final="the final answer", groups=())
        record = cockpit_entry.conversation_record(
            composed, outcome, "DONE")
        self.assertEqual(record.task, "record task")
        self.assertEqual(
            record.steps, (("architect", "rt-a"), ("coder", "rt-b")))
        self.assertEqual(record.status, "COMPLETED")
        self.assertEqual(record.plan, composed.plan)
        self.assertEqual(record.groups, ())
        self.assertEqual(record.member_ids, ("m-1", "m-2"))
        self.assertEqual(record.final_preview, "the final answer")
        self.assertEqual(
            record.usage, cockpit_entry.UsageSummary(123, 49, 1, 1))
        self.assertEqual(record.task_id, "tid-record")

    def test_status_falls_back_to_lifecycle(self):
        composed, _ = self._composed()
        outcome = SimpleNamespace(status=None, final_result=None)
        record = cockpit_entry.conversation_record(
            composed, outcome, "FAILED")
        self.assertEqual(record.status, "FAILED")

    def test_parked_outcome_yields_empty_preview(self):
        # RunOutcome 不变量：PARKED 强制 final_result None → 预览 ""
        composed, _ = self._composed()
        outcome = SimpleNamespace(
            status=SimpleNamespace(value="PARKED"), final_result=None)
        record = cockpit_entry.conversation_record(
            composed, outcome, "PARKED")
        self.assertEqual(record.status, "PARKED")
        self.assertEqual(record.final_preview, "")

    def test_final_preview_truncated_at_limit(self):
        composed, _ = self._composed()
        outcome = SimpleNamespace(
            status=SimpleNamespace(value="COMPLETED"),
            final_result="x" * (cockpit_entry._FINAL_PREVIEW_LIMIT + 50))
        record = cockpit_entry.conversation_record(
            composed, outcome, "DONE")
        self.assertEqual(
            len(record.final_preview), cockpit_entry._FINAL_PREVIEW_LIMIT)

    def test_groups_snapshot_passthrough(self):
        composed, _ = self._composed(groups=(("g1", ("m-1", "m-2")),))
        outcome = SimpleNamespace(
            status=SimpleNamespace(value="COMPLETED"), final_result=None)
        record = cockpit_entry.conversation_record(
            composed, outcome, "DONE")
        self.assertEqual(record.groups, (("g1", ("m-1", "m-2")),))


class ConversationRecordProjectionTests(unittest.TestCase):
    """conversation_record_lines 纯渲染：闭集标签/诚实占位/组行
    条件在场/CJK·ASCII·窄宽铁律。"""

    def _record(self):
        from usage_log import UsageObservation
        composed = SimpleNamespace(
            task="detail task", steps=(("architect", "rt-a"),
                                       ("coder", "rt-b")),
            plan=(), groups=(("alpha", ("m-1",)),), member_ids=("m-1",),
            task_id="tid-detail",
            usage=lambda: (
                _usage_record("i1", UsageObservation.KNOWN, 100, 40),
                _usage_record("i2", UsageObservation.UNKNOWN),))
        outcome = SimpleNamespace(
            status=SimpleNamespace(value="COMPLETED"),
            final_result="final snippet")
        return cockpit_entry.conversation_record(composed, outcome,
                                                 "DONE")

    def test_detail_rows_render_with_labels(self):
        from cockpit_projection import conversation_record_lines
        lines = conversation_record_lines(self._record(), index=1,
                                          width=100)
        self.assertEqual(lines[0], "── run 1 · detail ──")
        self.assertTrue(lines[1].startswith("  task"))
        self.assertIn("detail task", lines[1])
        self.assertIn("architect → rt-a · coder → rt-b", lines[2])
        self.assertIn("[alpha: m-1]", lines[3])
        self.assertIn("COMPLETED", lines[4])
        self.assertIn("final snippet", lines[5])
        self.assertIn("100 in / 40 out · 1 unknown", lines[6])
        self.assertIn("0 unsupported", lines[6])

    def test_no_groups_row_when_groups_empty(self):
        from cockpit_projection import conversation_record_lines
        record = self._record()._replace(groups=())
        lines = conversation_record_lines(record, index=1, width=100)
        self.assertFalse(any("[" in line for line in lines))

    def test_zero_usage_and_no_final_are_honest_dashes(self):
        from cockpit_projection import conversation_record_lines
        record = self._record()._replace(
            final_preview="", usage=cockpit_entry.UsageSummary(0, 0, 0, 0))
        lines = conversation_record_lines(record, index=1, width=100)
        self.assertIn("  result    —", lines)
        self.assertIn("  usage     —", lines)

    def test_zh_labels_and_ascii_and_narrow(self):
        from cockpit_projection import (conversation_record_lines,
                                        display_width)
        record = self._record()
        zh = conversation_record_lines(record, index=2, width=100,
                                       locale="zh")
        self.assertEqual(zh[0], "── 第 2 轮 · 详情 ──")
        self.assertTrue(zh[1].startswith("  任务"))
        # 窄宽 CJK 铁律（无 ascii：截断即封顶——与 runs_summary_
        # lines 同律；ascii 转换的 →→-> 单列膨胀是全库既有顺序律，
        # 不在宽度断言面内）
        narrow = conversation_record_lines(record, index=1, width=26,
                                           locale="zh")
        for line in narrow:
            self.assertLessEqual(display_width(line), 26)
        # ASCII 降级：box-drawing/箭头降级（CJK 无 ASCII 等价——按
        # _to_ascii 既有词表）
        ascii_lines = conversation_record_lines(
            record, index=1, width=100, locale="zh", ascii_only=True)
        self.assertTrue(ascii_lines[0].startswith("-- 第 1 轮"))
        self.assertTrue(any("->" in line for line in ascii_lines))

    def test_record_not_mutated(self):
        from cockpit_projection import conversation_record_lines
        record = self._record()
        before = tuple(record)
        conversation_record_lines(record, index=1, width=40)
        self.assertEqual(tuple(record), before)


@unittest.skipUnless(_TEXTUAL_AVAILABLE, "textual not installed (.[test] lacks [tui])")
class RunRecordCollectionTests(unittest.IsolatedAsyncioTestCase):
    """终态收录/PARKED 不铸//new 双清/第二轮追加/双镜像同步
    （TUI 呈现态三站点——builder 经注入，TUI 零 entry import）。"""

    def _app(self, start):
        return cockpit_tui.CockpitApp(
            composition_preview=lambda: _funnel_composition(),
            start_composition=start,
            record_builder=cockpit_entry.conversation_record)

    async def test_terminal_collects_record_synced_with_mirror(self):
        from usage_log import UsageObservation
        usage = (_usage_record("i1", UsageObservation.KNOWN, 7, 3),)
        composed = _conversation_double("collect task", ["COMPLETED"])
        composed.usage = lambda: usage
        composed.groups = ()
        composed.member_ids = ("m-1", "m-2")
        start = _StartRecorder([composed])
        app = self._app(start)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await _start_first_run(pilot, app, "collect", start)
            await _drive_to_between_runs(pilot, app)
            # 双镜像同步收录（同站点同刻）
            self.assertEqual(len(app._cockpit_runs), 1)
            self.assertEqual(len(app._cockpit_run_records), 1)
            record = app._cockpit_run_records[0]
            self.assertEqual(record.task, "collect task")
            self.assertEqual(
                record.steps,
                (("architect", "rt-a"), ("coder", "rt-b")))
            self.assertEqual(record.status, "COMPLETED")
            self.assertEqual(record.final_preview, "")
            self.assertEqual(record.usage,
                             cockpit_entry.UsageSummary(7, 3, 0, 0))
            self.assertEqual(record.task_id, composed.task_id)

    async def test_parked_does_not_mint_record(self):
        start = _StartRecorder([
            _conversation_double("parked record", ["PARKED",
                                                   "COMPLETED"])])
        app = self._app(start)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await _start_first_run(pilot, app, "park", start)
            await _await_condition(
                pilot, app, lambda: app.outcome is not None)
            # 停驻中：结构上无 record（在飞轮无详情——诚实缺席）
            self.assertEqual(app._cockpit_run_records, [])
            app._cockpit_wake.set()
            await _drive_to_between_runs(pilot, app)
            self.assertEqual(len(app._cockpit_run_records), 1)
            self.assertEqual(
                app._cockpit_run_records[0].status, "COMPLETED")

    async def test_new_clears_both_mirrors_together(self):
        start = _StartRecorder([
            _conversation_double("reset record", ["COMPLETED"])])
        app = self._app(start)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await _start_first_run(pilot, app, "reset", start)
            await _drive_to_between_runs(pilot, app)
            self.assertEqual(len(app._cockpit_run_records), 1)
            for character in "/new":
                await pilot.press(character)
            await pilot.press("enter")
            await pilot.pause()
            await pilot.press("y")
            await pilot.pause()
            self.assertEqual(app._cockpit_runs, [])
            self.assertEqual(app._cockpit_run_records, [])

    async def test_second_run_appends_second_record(self):
        start = _StartRecorder([
            _conversation_double("first record", ["COMPLETED"]),
            _conversation_double("second record", ["FAILED"])])
        app = self._app(start)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await _start_first_run(pilot, app, "one", start)
            await _drive_to_between_runs(pilot, app)
            for character in "two":
                await pilot.press(character)
            await pilot.press("enter")
            await _await_condition(
                pilot, app, lambda: len(start.calls) == 2)
            await _drive_to_between_runs(pilot, app)
            self.assertEqual(len(app._cockpit_runs), 2)
            self.assertEqual(len(app._cockpit_run_records), 2)
            self.assertEqual(
                [record.task for record in app._cockpit_run_records],
                ["first record", "second record"])
            self.assertEqual(
                [record.status
                 for record in app._cockpit_run_records],
                ["COMPLETED", "FAILED"])
            self.assertNotEqual(app._cockpit_run_records[0].task_id,
                                app._cockpit_run_records[1].task_id)

    async def test_no_builder_means_no_collection(self):
        # 缺省路径（record_builder=None）：零收录、既有行为不受影响
        start = _StartRecorder([
            _conversation_double("legacy task", ["COMPLETED"])])
        app = cockpit_tui.CockpitApp(
            composition_preview=lambda: _funnel_composition(),
            start_composition=start)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await _start_first_run(pilot, app, "legacy", start)
            await _drive_to_between_runs(pilot, app)
            self.assertEqual(app._cockpit_run_records, [])
            self.assertEqual(len(app._cockpit_runs), 1)


@unittest.skipUnless(_TEXTUAL_AVAILABLE, "textual not installed (.[test] lacks [tui])")
class RunsDetailPilotTests(unittest.IsolatedAsyncioTestCase):
    """/runs 详情块：additive（摘要行逐字节兼容）+ 详情渲染 +
    在飞轮无详情块 + zh/窄宽形态。"""

    async def _run_to_between_runs(self, pilot, app, start, text):
        await _start_first_run(pilot, app, text, start)
        await _drive_to_between_runs(pilot, app)

    async def test_runs_appends_detail_block_after_summary(self):
        from cockpit_projection import runs_summary_lines
        composed = _conversation_double("detail pilot", ["COMPLETED"])
        composed.groups = ()
        start = _StartRecorder([composed])
        app = cockpit_tui.CockpitApp(
            composition_preview=lambda: _funnel_composition(),
            start_composition=start,
            record_builder=cockpit_entry.conversation_record)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await self._run_to_between_runs(pilot, app, start, "dp")
            for character in "/runs":
                await pilot.press(character)
            await pilot.press("enter")
            await pilot.pause()
            lines = app.log_text.splitlines()
            # 摘要行集逐字节 = runs_summary_lines（additive 证明）
            expected = runs_summary_lines(
                app._cockpit_runs, width=app.size.width or 100,
                locale=app._cockpit_locale,
                ascii_only=app._cockpit_ascii)
            for line in expected:
                self.assertIn(line, lines)
            # 详情块在场（分节线 + 标签行）
            self.assertIn("── run 1 · detail ──", lines)
            self.assertTrue(any(
                line.startswith("  task") and "detail pilot" in line
                for line in lines))
            self.assertTrue(any(
                line.startswith("  outcome") and "COMPLETED" in line
                for line in lines))
            # 摘要先于详情（块序：runs → run 1 · … → detail）
            self.assertLess(lines.index("runs"),
                            lines.index("── run 1 · detail ──"))

    async def test_inflight_run_has_no_detail_block(self):
        second_gate = threading.Event()
        start = _StartRecorder([
            _conversation_double("first inflight", ["COMPLETED"]),
            _conversation_double("second inflight", ["FAILED"],
                                 gate=second_gate)])
        app = cockpit_tui.CockpitApp(
            composition_preview=lambda: _funnel_composition(),
            start_composition=start,
            record_builder=cockpit_entry.conversation_record)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await self._run_to_between_runs(pilot, app, start, "one")
            for character in "two":
                await pilot.press(character)
            await pilot.press("enter")
            await _await_condition(
                pilot, app, lambda: len(start.calls) == 2)
            # RUNNING 中 /runs：run 2 只有摘要行（RUNNING 词），
            # 无第二块详情（在飞 final/usage 未定，绝不预呈）
            for character in "/runs":
                await pilot.press(character)
            await pilot.press("enter")
            await pilot.pause()
            lines = app.log_text.splitlines()
            self.assertTrue(any(
                "run 2 · RUNNING · " in line for line in lines))
            self.assertEqual(lines.count("── run 1 · detail ──"), 1)
            self.assertFalse(any(
                "── run 2 · detail ──" in line for line in lines))
            second_gate.set()

    async def test_runs_detail_zh_and_narrow(self):
        composed = _conversation_double("zh 详情轮次", ["COMPLETED"])
        start = _StartRecorder([composed])
        app = cockpit_tui.CockpitApp(
            composition_preview=lambda: _funnel_composition(),
            start_composition=start,
            record_builder=cockpit_entry.conversation_record)
        async with app.run_test(size=(48, 30)) as pilot:
            await pilot.pause()
            # 漏斗域 l 是文本（R2 切换仅主屏/COMPOSE 裸键）——locale
            # 为本测试的呈现参数，直接置位（R2 键律已有专属测试面）
            app._cockpit_locale = "zh"
            await self._run_to_between_runs(pilot, app, start, "zh")
            for character in "/runs":
                await pilot.press(character)
            await pilot.press("enter")
            await pilot.pause()
            lines = app.log_text.splitlines()
            self.assertIn("── 第 1 轮 · 详情 ──", lines)
            from cockpit_projection import display_width
            for line in lines:
                self.assertLessEqual(display_width(line), 48)


if __name__ == "__main__":
    unittest.main()
