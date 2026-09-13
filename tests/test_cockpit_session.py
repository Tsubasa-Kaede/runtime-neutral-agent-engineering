"""CU-TUI-2 (V3.2): CockpitSession — control + RunState session seam.

All doubles are offline: no network, no REAL providers, no credentials,
no subprocess, no runtime installation. The session is the single
RunState owner and the control submission facade; every truth below it
stays in the frozen stack (boundary / journal / slots / pipeline).

RED proof (written before the session component exists): importing
cockpit_session fails outright — the seam itself is absent.

Locked semantics under the CU-TUI-2 authorization:
- accepted != paused: PAUSE accepted merely parks at the NEXT stage
  admission; the in-flight invocation always completes naturally.
- PARKED is a real RunOutcome carrying RunState; RESUME re-enters the
  pipeline with exactly that saved value (no restart at step 0, no
  rebuilt transcript).
- G3 terminal gate: after a real terminal outcome (COMPLETED / FAILED /
  ABORTED) every further control command is rejected locally with
  ALREADY_TERMINAL — zero new facts, zero boundary side effects.
- G2 SUBMISSION consumer: accepted SUBMISSION revisions are recorded at
  submit time and applied (steps rebuilt via the injected factory) only
  at the start of a FRESH segment; accepted != applied != honored.
- G1 protection: NEXT_INVOCATION revisions flow through the frozen
  boundary -> RevisionAdapter -> one-shot consume path; this session
  never reads the revision queue and never duplicates applied facts.
- Execution is serial: one run_segment call at a time; submit is
  facade-only. The session emits no events and keeps no observation
  truth of its own.
"""
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from cockpit_session import CockpitSession  # noqa: E402
from control_boundary import (  # noqa: E402
    ControlCommand,
    ControlCommandType,
    ControlLifecycle,
    ControlModelError,
    ControlReason,
    ControlStatus,
    RevisionPayload,
    RevisionTarget,
)
from control_journal import ControlFactType, ControlJournal  # noqa: E402
from execution_slots import (  # noqa: E402
    ExecutionSlotSpec,
    ExecutionSlots,
    build_execution_slots,
)
from external_runtime import (  # noqa: E402
    ExternalAgentRequest,
    InvocationResult,
    InvocationStatus,
)
from sequential_pipeline import (  # noqa: E402
    RunOutcome,
    RunStatus,
    StepSpec,
)
from usage_log import UsageLog  # noqa: E402

SESSION_PATH = SCRIPTS / "cockpit_session.py"

_CONTROL_FACT_VOCABULARY = {
    ControlFactType.PAUSE_REQUESTED,
    ControlFactType.PAUSE_CONFIRMED,
    ControlFactType.RESUME_REQUESTED,
    ControlFactType.REVISE_REQUESTED,
    ControlFactType.REVISION_APPLIED,
    ControlFactType.ABORT_REQUESTED,
    ControlFactType.ABORT_CONFIRMED,
    ControlFactType.ABORT_SUPERSEDED,
}


# ---------------------------------------------------------------- doubles


def _success(slot):
    return InvocationResult(status=InvocationStatus.SUCCESS,
                            output=f"out-{slot}", error=None, trace=None)


class _HookRaw:
    """raw 替身：记录入口/请求/返回值；首请求前可挂 in-flight 回调
    （委托进行中经 THE boundary 提交控制命令——唯一合法途径）。"""

    def __init__(self, slot, on_first=None, raises=None):
        self.slot = slot
        self.entries = []
        self.requests = []
        self.results = []
        self._on_first = on_first
        self._raises = raises

    def invoke(self, request):
        if not self.requests and self._on_first is not None:
            self._on_first()
        self.entries.append(request.task_id)
        self.requests.append(request)
        if self._raises is not None:
            raise self._raises
        result = _success(self.slot)
        self.results.append(result)
        return result


def make_group(slot_ids=("a", "b"), hook_commands=None, raising=None):
    """组装 slots 组：hook_commands = {slot_id: command_factory}；
    raising = {slot_id: exception}（该 raw 委托恒抛出）。

    工厂经 boundary_box 晚绑定（group 构建后才存在 boundary）；
    挂钩 raw 在自身首个请求发起前提交对应命令。"""
    box = {}
    hooks = {}
    for slot_id, command_factory in (hook_commands or {}).items():
        def hook(_factory=command_factory):
            box["boundary"].submit(_factory())
        hooks[slot_id] = hook
    journal = ControlJournal()
    raws = {}
    specs = []
    for slot_id in slot_ids:
        raw = _HookRaw(slot_id, on_first=hooks.get(slot_id),
                       raises=(raising or {}).get(slot_id))
        raws[slot_id] = raw
        specs.append(ExecutionSlotSpec(
            slot_id=slot_id, raw_adapter=raw, usage_log=UsageLog(),
            runtime_id=f"rt-{slot_id}", role=f"role-{slot_id}"))
    group = build_execution_slots(journal, tuple(specs),
                                  execution_id="exec-1")
    box["boundary"] = group.boundary
    return group, journal, raws


class StepsPlan:
    """steps_factory 替身：记录 submission 代际与 builder 调用。

    每次工厂调用记一代（submission 快照 + 新 builder 闭包集）；
    builder 调用与 previous_result 逐代隔离——旧代 builder 是否
    仍被调用即为"旧配置不再使用"的可执行证明。"""

    def __init__(self, slot_ids):
        self.slot_ids = tuple(slot_ids)
        self.submissions_seen = []
        self.generations = []

    def factory(self, submission):
        self.submissions_seen.append(submission)
        task_text = (submission.task if submission.task is not None
                     else submission.prompt)
        generation = {"task": task_text, "builder_calls": 0,
                      "called_slots": [], "previous": []}
        self.generations.append(generation)
        steps = []
        for slot_id in self.slot_ids:
            def builder(previous_result, _slot=slot_id, _task=task_text,
                        _gen=generation):
                _gen["builder_calls"] += 1
                _gen["called_slots"].append(_slot)
                _gen["previous"].append(previous_result)
                return ExternalAgentRequest(
                    task_id=f"task-{_slot}",
                    prompt=f"base:{_task}:{_slot}",
                    agent_id=f"agent-{_slot}", role=f"role-{_slot}")
            steps.append(StepSpec(slot_id=slot_id, request_builder=builder))
        return tuple(steps)


def make_session(slot_ids=("a", "b"), hook_commands=None,
                 task="initial task", raising=None):
    group, journal, raws = make_group(slot_ids, hook_commands, raising)
    plan = StepsPlan(slot_ids)
    session = CockpitSession(
        boundary=group.boundary, slots=group,
        submission=RevisionPayload(target=RevisionTarget.SUBMISSION,
                                   task=task),
        steps_factory=plan.factory)
    return session, plan, journal, raws, group


# ------------------------------------------------------------ commands


def pause_cmd(command_id="p1"):
    return ControlCommand(command_id=command_id, execution_id="exec-1",
                          command=ControlCommandType.PAUSE)


def resume_cmd(command_id="r1"):
    return ControlCommand(command_id=command_id, execution_id="exec-1",
                          command=ControlCommandType.RESUME)


def abort_cmd(command_id="a1"):
    return ControlCommand(command_id=command_id, execution_id="exec-1",
                          command=ControlCommandType.ABORT)


def next_inv_rev(command_id, text):
    return ControlCommand(
        command_id=command_id, execution_id="exec-1",
        command=ControlCommandType.REVISE,
        payload=RevisionPayload(target=RevisionTarget.NEXT_INVOCATION,
                                text=text),
        expected_version=0)


def submission_rev(command_id, task=None, prompt=None):
    return ControlCommand(
        command_id=command_id, execution_id="exec-1",
        command=ControlCommandType.REVISE,
        payload=RevisionPayload(target=RevisionTarget.SUBMISSION,
                                task=task, prompt=prompt),
        expected_version=0)


def queue_ids(boundary):
    return [entry.revision_id for entry in boundary.snapshot(
        ControlLifecycle.RUNNING).revision_queue]


# ------------------------------------------------------------- surface


class SurfaceTests(unittest.TestCase):
    """构造校验 + 最小公开面 + G1 结构性守卫。"""

    def test_requires_control_boundary(self):
        _, plan, _, _, group = make_session()
        with self.assertRaises(ControlModelError):
            CockpitSession(boundary=object(), slots=group,
                           submission=RevisionPayload(target=RevisionTarget.SUBMISSION, task="t"),
                           steps_factory=plan.factory)

    def test_requires_execution_slots(self):
        _, plan, _, _, group = make_session()
        with self.assertRaises(ControlModelError):
            CockpitSession(boundary=group.boundary, slots=object(),
                           submission=RevisionPayload(target=RevisionTarget.SUBMISSION, task="t"),
                           steps_factory=plan.factory)

    def test_requires_revision_payload_submission(self):
        _, plan, _, _, group = make_session()
        with self.assertRaises(ControlModelError):
            CockpitSession(boundary=group.boundary, slots=group,
                           submission="task text",
                           steps_factory=plan.factory)

    def test_requires_callable_steps_factory(self):
        _, _, _, _, group = make_session()
        with self.assertRaises(ControlModelError):
            CockpitSession(boundary=group.boundary, slots=group,
                           submission=RevisionPayload(target=RevisionTarget.SUBMISSION, task="t"),
                           steps_factory=object())

    def test_empty_steps_rejected_at_construction(self):
        # 无空 slot：空 steps 在构造期即拒绝（fail-fast，零半成品）
        group, _, _ = make_group(("a",))
        with self.assertRaises(ControlModelError):
            CockpitSession(
                boundary=group.boundary, slots=group,
                submission=RevisionPayload(target=RevisionTarget.SUBMISSION, task="t"),
                steps_factory=lambda submission: ())

    def test_no_revision_queue_access_in_session_source(self):
        # G1 结构性守卫：session 绝不直接读 revision 队列、绝不
        # 复制 drain / applied / usage 语义
        source = SESSION_PATH.read_text(encoding="utf-8")
        for token in ("revision_queue", "consume_revisions",
                      "on_handoff", "applied_revisions"):
            self.assertNotIn(token, source)
        for token in ("retry", "fallback", "sleep("):
            self.assertNotIn(token, source)

    def test_no_observation_truth_in_session_source(self):
        # session 不成为观察真相源：零事件构造、零 sink/index 引用
        source = SESSION_PATH.read_text(encoding="utf-8")
        for token in ("ExecutionEvent", "ExecutionEventType",
                      "ObservationSink", "EventIndex", "event_index"):
            self.assertNotIn(token, source)


# ------------------------------------------------------- CONTROL 1-11


class ControlCommandTests(unittest.TestCase):
    """控制面：accepted ≠ paused、admission 停驻、终态门、幂等。"""

    def test_initial_pause_parks_before_first_step(self):
        # 1: run 前提交 PAUSE → ACCEPTED；首个 admission 即停驻，零 invocation
        session, plan, journal, raws, group = make_session()
        result = session.submit(pause_cmd())
        self.assertIs(result.status, ControlStatus.ACCEPTED)
        outcome = session.run_segment()
        self.assertIs(outcome.status, RunStatus.PARKED)
        self.assertEqual(raws["a"].entries, [])   # 零 invocation
        self.assertEqual(raws["b"].entries, [])
        self.assertEqual([f.fact_type for f in journal.snapshot()],
                         [ControlFactType.PAUSE_REQUESTED])

    def test_running_pause_completes_inflight_then_parks(self):
        # 2+3+4: 在途 invocation 自然完成（a 恰一次进入并返回）；
        # 下一步 admission 发现 pause intent ⇒ PARKED + RunState
        session, plan, journal, raws, group = make_session(
            hook_commands={"a": pause_cmd})
        outcome = session.run_segment()
        self.assertIs(outcome.status, RunStatus.PARKED)
        self.assertEqual(raws["a"].entries, ["task-a"])  # 在途完成
        self.assertEqual(raws["b"].entries, [])          # 未启动
        self.assertEqual(outcome.run_state.next_step_index, 1)
        self.assertEqual(len(outcome.transcript), 1)
        self.assertIs(session.run_state, outcome.run_state)  # 唯一持有者

    def test_resume_before_parked_is_no_op(self):
        # 5: 未 PARKED 时 RESUME = 既有 NO_OP/NOT_PAUSED，零 invocation
        session, plan, journal, raws, group = make_session()
        result = session.submit(resume_cmd())
        self.assertIs(result.status, ControlStatus.NO_OP)
        self.assertIs(result.reason, ControlReason.NOT_PAUSED)
        self.assertEqual(raws["a"].entries, [])  # 提交不启动执行
        outcome = session.run_segment()          # 正常首跑不受影响
        self.assertIs(outcome.status, RunStatus.COMPLETED)

    def test_resume_from_parked_continues_saved_run_state(self):
        # 6+13: 恢复用 PARKED 保存的 RunState——不回 step 0、不重建历史
        session, plan, journal, raws, group = make_session(
            hook_commands={"a": pause_cmd})
        parked = session.run_segment()
        self.assertIs(session.submit(resume_cmd()).status,
                      ControlStatus.ACCEPTED)
        resumed = session.run_segment()
        self.assertIs(resumed.status, RunStatus.COMPLETED)
        self.assertEqual(raws["a"].entries, ["task-a"])   # step0 不重复
        self.assertEqual(raws["b"].entries, ["task-b"])   # 续走恰一次
        self.assertEqual(len(resumed.transcript), 2)      # 跨续累积
        self.assertIs(resumed.transcript[0], parked.transcript[0])
        self.assertIsNone(session.run_state)              # 终态后清持有

    def test_abort_running_reaches_real_aborted(self):
        # 8: 在途完成 → admission 终止 → 真实 ABORTED
        session, plan, journal, raws, group = make_session(
            hook_commands={"a": abort_cmd})
        outcome = session.run_segment()
        self.assertIs(outcome.status, RunStatus.ABORTED)
        self.assertEqual(raws["a"].entries, ["task-a"])  # 在途自然完成
        self.assertEqual(raws["b"].entries, [])
        self.assertIs(session.terminal, RunStatus.ABORTED)

    def test_abort_parked_yields_aborted_without_new_invocation(self):
        # 9: 停驻后 ABORT → 恢复尝试在 admission 立即真实 ABORTED
        session, plan, journal, raws, group = make_session(
            hook_commands={"a": pause_cmd})
        session.run_segment()
        self.assertIs(session.submit(abort_cmd()).status,
                      ControlStatus.ACCEPTED)
        outcome = session.run_segment()
        self.assertIs(outcome.status, RunStatus.ABORTED)
        self.assertEqual(raws["a"].entries, ["task-a"])  # 零新 invocation
        self.assertEqual(raws["b"].entries, [])
        self.assertIsNone(outcome.run_state)

    def test_terminal_gate_rejects_every_command_with_zero_facts(self):
        # 10: 真实终态后四类命令一律本地 ALREADY_TERMINAL，零新事实
        for hook in (None, {"a": abort_cmd}):
            with self.subTest(hook=bool(hook)):
                session, plan, journal, raws, group = (
                    make_session(hook_commands=hook))
                session.run_segment()
                facts_before = journal.snapshot()
                for command in (pause_cmd("tp"), resume_cmd("tr"),
                                next_inv_rev("tv", "x"),
                                abort_cmd("ta")):
                    result = session.submit(command)
                    self.assertIs(result.status, ControlStatus.REJECTED)
                    self.assertIs(result.reason,
                                  ControlReason.ALREADY_TERMINAL)
                self.assertEqual(journal.snapshot(), facts_before)

    def test_command_idempotency_and_conflict(self):
        # 11: 精确重放纯查找（同结果对象）；同 id 不同载荷 = 冲突
        session, plan, journal, raws, group = make_session()
        first = session.submit(next_inv_rev("rev-1", "one"))
        self.assertIs(first.status, ControlStatus.ACCEPTED)
        replay = session.submit(next_inv_rev("rev-1", "one"))
        self.assertIs(replay, first)  # replay 返回首次结果对象
        conflict = session.submit(next_inv_rev("rev-1", "two"))
        self.assertIs(conflict.status, ControlStatus.REJECTED)
        self.assertIs(conflict.reason, ControlReason.COMMAND_ID_CONFLICT)
        # SUBMISSION 重放不重复登记（见 RevisionTests 的消费计数）
        accepted = session.submit(submission_rev("sub-1", task="S1"))
        self.assertIs(accepted.status, ControlStatus.ACCEPTED)
        replay_sub = session.submit(submission_rev("sub-1", task="S1"))
        self.assertIs(replay_sub, accepted)


# ------------------------------------------------------ RUNSTATE 12-17


class RunStateTests(unittest.TestCase):
    """RunState 所有权：PARKED 保存、resume 消费、不可变累积、终态清空。"""

    def test_parked_returns_run_state_held_by_session(self):
        # 12: outcome 携带的 RunState 即 session 持有值（单一持有者）
        session, plan, journal, raws, group = make_session(
            hook_commands={"a": pause_cmd})
        outcome = session.run_segment()
        self.assertIsNotNone(outcome.run_state)
        self.assertIs(session.run_state, outcome.run_state)
        self.assertEqual(outcome.run_state.next_step_index, 1)
        self.assertIs(outcome.run_state.previous_result,
                      raws["a"].results[0])  # 真实 handoff 值

    def test_next_step_index_progresses_across_parks(self):
        # 14: 连续停驻推进步序（1 → 2），不重不漏。
        # 两次停驻用互异 command_id——同 id 是精确重放（既有幂等：
        # 纯查找不重设 intent），不产生第二次停驻
        session, plan, journal, raws, group = make_session(
            slot_ids=("a", "b", "c"),
            hook_commands={"a": lambda: pause_cmd("p-a"),
                           "b": lambda: pause_cmd("p-b")})
        first = session.run_segment()
        self.assertIs(first.status, RunStatus.PARKED)
        self.assertEqual(first.run_state.next_step_index, 1)
        session.submit(resume_cmd("r-1"))
        second = session.run_segment()
        self.assertIs(second.status, RunStatus.PARKED)
        self.assertEqual(second.run_state.next_step_index, 2)
        session.submit(resume_cmd("r-2"))  # 互异 id：同 id 是重放不清 pending
        final = session.run_segment()
        self.assertIs(final.status, RunStatus.COMPLETED)
        self.assertEqual([len(r.entries) for r in
                          (raws["a"], raws["b"], raws["c"])], [1, 1, 1])

    def test_previous_result_is_real_handoff_value(self):
        # 15: 续走首步收到的 previous_result 恰为 step0 真实结果对象
        session, plan, journal, raws, group = make_session(
            hook_commands={"a": pause_cmd})
        session.run_segment()
        session.submit(resume_cmd())
        session.run_segment()
        generation = plan.generations[-1]
        self.assertIsNone(generation["previous"][0])   # 首步无前值
        self.assertIs(generation["previous"][1],       # 次步收前步真实结果
                      raws["a"].results[0])
        self.assertIsNone(plan.generations[0]["previous"][0])

    def test_transcript_accumulates_immutable_prefix(self):
        # 16: transcript 不可变累积——前缀同对象、长度递增
        session, plan, journal, raws, group = make_session(
            slot_ids=("a", "b", "c"),
            hook_commands={"a": lambda: pause_cmd("p-a"),
                           "b": lambda: pause_cmd("p-b")})
        first = session.run_segment()
        self.assertEqual(len(first.run_state.transcript), 1)
        session.submit(resume_cmd("r-1"))
        second = session.run_segment()
        self.assertEqual(len(second.run_state.transcript), 2)
        self.assertIs(second.run_state.transcript[0],
                      first.run_state.transcript[0])
        session.submit(resume_cmd("r-2"))  # 互异 id：同 id 是重放不清 pending
        final = session.run_segment()
        self.assertEqual(len(final.transcript), 3)
        self.assertIs(final.transcript[0], first.run_state.transcript[0])

    def test_terminal_clears_executable_run_state(self):
        # 17: 三类终态后 session 不再持有可执行 RunState；投影恒真实
        for hook, expected in (
                (None, RunStatus.COMPLETED),
                ({"a": abort_cmd}, RunStatus.ABORTED)):
            with self.subTest(status=expected.value):
                session, plan, journal, raws, group = make_session(
                    hook_commands=hook)
                outcome = session.run_segment()
                self.assertIs(outcome.status, expected)
                self.assertIsNone(outcome.run_state)  # 不变量 ⟺ PARKED
                self.assertIsNone(session.run_state)
                self.assertIs(session.terminal, expected)
                self.assertIs(session.last_outcome, outcome)
                self.assertIsInstance(outcome, RunOutcome)  # 真实对象

    def test_failed_terminal_also_clears_state(self):
        # 17（FAILED 分支）：异常终态同律清空可执行 RunState
        session, plan, journal, raws, group = make_session(
            raising={"b": RuntimeError("boom")})
        outcome = session.run_segment()
        self.assertIs(outcome.status, RunStatus.FAILED)
        self.assertIsNone(outcome.run_state)  # 不变量 ⟺ PARKED
        self.assertIsNone(session.run_state)
        self.assertIs(session.terminal, RunStatus.FAILED)
        self.assertIs(session.last_outcome, outcome)
        self.assertIsInstance(outcome, RunOutcome)  # 真实对象


# ------------------------------------------------------ REVISION 18-22


class RevisionTests(unittest.TestCase):
    """NEXT_INVOCATION 走 G1 冻结链；SUBMISSION 由 session 消费重建。"""

    def test_next_invocation_one_shot_through_frozen_path(self):
        # 18+22(G1 面): 停驻窗提交 → 恢复后恰首个委托携带并消费
        session, plan, journal, raws, group = make_session(
            hook_commands={"a": pause_cmd})
        session.run_segment()
        self.assertIs(session.submit(
            next_inv_rev("rev-1", "REV-TEXT")).status, ControlStatus.ACCEPTED)
        session.submit(resume_cmd())
        outcome = session.run_segment()
        self.assertIs(outcome.status, RunStatus.COMPLETED)
        prompt = raws["b"].requests[0].prompt
        self.assertIn("[USER REVISION rev-1]", prompt)
        self.assertIn("REV-TEXT", prompt)
        self.assertTrue(prompt.startswith("base:initial task:b"))
        self.assertEqual(queue_ids(group.boundary), [])  # one-shot 终空
        applied = [f for f in journal.snapshot()
                   if f.fact_type is ControlFactType.REVISION_APPLIED]
        self.assertEqual(len(applied), 1)  # 零重复 applied 事实

    def test_submission_revision_consumed_at_fresh_segment(self):
        # 19+20: 接受后仅在 fresh segment 起点消费重建；prompt 携新任务
        session, plan, journal, raws, group = make_session()
        self.assertIs(session.submit(
            submission_rev("sub-1", task="NEW-TASK")).status,
            ControlStatus.ACCEPTED)
        self.assertEqual(len(plan.submissions_seen), 1)  # 仅初始工厂调用
        outcome = session.run_segment()
        self.assertIs(outcome.status, RunStatus.COMPLETED)
        self.assertEqual(len(plan.submissions_seen), 2)  # 消费即重建
        self.assertEqual(plan.submissions_seen[1].task, "NEW-TASK")
        for raw in raws.values():
            self.assertIn("NEW-TASK", raw.requests[0].prompt)
            self.assertNotIn("initial task", raw.requests[0].prompt)

    def test_old_generation_builders_not_used_after_rebuild(self):
        # 21: 旧代 builder 零调用；新代承担全部委托
        session, plan, journal, raws, group = make_session()
        session.submit(submission_rev("sub-1", task="NEW-TASK"))
        session.run_segment()
        old, new = plan.generations[0], plan.generations[1]
        self.assertEqual(old["builder_calls"], 0)   # 旧配置不再使用
        self.assertEqual(new["builder_calls"], 2)   # 每步恰一次
        self.assertEqual(new["called_slots"], ["a", "b"])

    def test_accepted_not_applied_not_honored(self):
        # 22: accepted（事实落账）≠ applied（工厂重建）≠ honored（委托 prompt）
        session, plan, journal, raws, group = make_session()
        result = session.submit(submission_rev("sub-1", task="NEW-TASK"))
        self.assertIs(result.status, ControlStatus.ACCEPTED)
        fact_types = [f.fact_type for f in journal.snapshot()]
        self.assertEqual(fact_types, [ControlFactType.REVISE_REQUESTED])
        self.assertEqual(len(plan.submissions_seen), 1)  # 未 applied
        self.assertEqual(raws["a"].entries, [])          # 未 honored
        session.run_segment()                            # applied 边界
        self.assertEqual(plan.submissions_seen[-1].task, "NEW-TASK")
        self.assertIn("NEW-TASK", raws["a"].requests[0].prompt)  # honored

    def test_submission_merge_field_wise_fifo(self):
        # 多条 SUBMISSION：FIFO 应用、逐字段后者胜
        session, plan, journal, raws, group = make_session()
        session.submit(submission_rev("sub-1", task="T1"))
        session.submit(submission_rev("sub-2", prompt="P2"))
        session.run_segment()
        merged = plan.submissions_seen[-1]
        self.assertEqual(merged.task, "T1")       # 修订一改任务
        self.assertEqual(merged.prompt, "P2")     # 修订二改 prompt
        self.assertIn("T1", raws["a"].requests[0].prompt)

    def test_submission_pending_while_parked_applies_to_next_fresh(self):
        # 停驻窗内的 SUBMISSION 不作用于本段恢复——留给下一段
        session, plan, journal, raws, group = make_session(
            hook_commands={"a": pause_cmd})
        session.run_segment()
        session.submit(submission_rev("sub-1", task="NEW-TASK"))
        session.submit(resume_cmd())
        outcome = session.run_segment()
        self.assertIs(outcome.status, RunStatus.COMPLETED)
        self.assertEqual(len(plan.submissions_seen), 1)  # 本段未重建
        self.assertIn("initial task", raws["b"].requests[0].prompt)


# --------------------------------------------------- OBSERVATION 23-26


class ObservationTruthTests(unittest.TestCase):
    """控制后果可见、零合成事件、终态观察恒真实。"""

    def test_parked_consequence_observable_via_real_truth(self):
        # 23: PARKED = 真实 RunOutcome + PAUSE_REQUESTED 事实
        session, plan, journal, raws, group = make_session(
            hook_commands={"a": pause_cmd})
        outcome = session.run_segment()
        self.assertIsInstance(outcome, RunOutcome)
        self.assertIs(outcome.status, RunStatus.PARKED)
        self.assertIn(ControlFactType.PAUSE_REQUESTED,
                      [f.fact_type for f in journal.snapshot()])

    def test_terminal_consequence_observable_via_real_truth(self):
        # 24: ABORTED = 真实 RunOutcome + ABORT_REQUESTED 事实
        session, plan, journal, raws, group = make_session(
            hook_commands={"a": abort_cmd})
        outcome = session.run_segment()
        self.assertIs(outcome.status, RunStatus.ABORTED)
        self.assertIn(ControlFactType.ABORT_REQUESTED,
                      [f.fact_type for f in journal.snapshot()])
        self.assertIs(session.last_outcome, outcome)

    def test_no_synthetic_control_events_ever(self):
        # 25: 账本事实恒属冻结控制词表——session 零自写事实
        session, plan, journal, raws, group = make_session(
            hook_commands={"a": pause_cmd})
        session.run_segment()
        session.submit(next_inv_rev("rev-1", "x"))
        session.submit(resume_cmd())
        session.run_segment()
        for fact in journal.snapshot():
            self.assertIn(fact.fact_type, _CONTROL_FACT_VOCABULARY)

    def test_terminal_projection_is_the_real_outcome_object(self):
        # 26: 终态投影零合成——last_outcome 即管线返回对象本身；
        # 终态后再次 run_segment 幂等返回同一对象（零再执行）
        session, plan, journal, raws, group = make_session(
            hook_commands={"a": abort_cmd})
        first = session.run_segment()
        second = session.run_segment()
        self.assertIs(second, first)
        self.assertEqual(raws["a"].entries, ["task-a"])  # 零再执行


# -------------------------------------------------- AGENT COUNT 27-30


_TEMPLATES = {
    2: ("architect", "coder"),
    3: ("architect", "coder", "reviewer"),
    4: ("architect", "coder", "tester", "reviewer"),
}


class AgentCountTests(unittest.TestCase):
    """2/3/4 固定模板：步数=角色数、顺序固定、零空 slot、组合期定。"""

    def test_two_agent_template(self):
        self._run_template(2)

    def test_three_agent_template(self):
        self._run_template(3)

    def test_four_agent_template(self):
        self._run_template(4)

    def _run_template(self, count):
        roles = _TEMPLATES[count]
        session, plan, journal, raws, group = make_session(slot_ids=roles)
        outcome = session.run_segment()
        self.assertIs(outcome.status, RunStatus.COMPLETED)
        self.assertEqual(len(outcome.transcript), count)  # 步数=角色数
        self.assertEqual(plan.generations[-1]["called_slots"],
                         list(roles))                      # 顺序固定
        for role in roles:                                # 零空 slot
            self.assertEqual(len(raws[role].entries), 1)

    def test_composition_is_immutable_after_start(self):
        # 组合期定后不可变：terminal 后无任何途径改变步集
        session, plan, journal, raws, group = make_session(
            slot_ids=_TEMPLATES[2])
        session.run_segment()
        before = len(plan.generations)
        session.submit(pause_cmd("px"))  # 终态门拒绝
        self.assertEqual(len(plan.generations), before)

    def test_no_empty_slot_rejected_at_group_construction(self):
        with self.assertRaises(ControlModelError):
            make_group(())

    def test_same_runtime_reuse_across_agents(self):
        # Agent Count ≠ Runtime Count：三角色复用同 runtime 合法
        journal = ControlJournal()
        shared = _HookRaw("shared")
        specs = tuple(
            ExecutionSlotSpec(
                slot_id=role, raw_adapter=shared, usage_log=UsageLog(),
                runtime_id="rt-single", role=role)
            for role in _TEMPLATES[3])
        group = build_execution_slots(journal, specs,
                                      execution_id="exec-1")
        plan = StepsPlan(_TEMPLATES[3])
        session = CockpitSession(
            boundary=group.boundary, slots=group,
            submission=RevisionPayload(target=RevisionTarget.SUBMISSION, task="t"),
            steps_factory=plan.factory)
        outcome = session.run_segment()
        self.assertIs(outcome.status, RunStatus.COMPLETED)
        self.assertEqual(len(shared.entries), 3)  # 同 raw 三次进入


if __name__ == "__main__":
    unittest.main()
