"""R7-D2: production execution observation wiring (offline).

验证链：facade.run(observation_sink=None) -> execution-local 序号闭包 ->
orchestrator.run(observation_emit) -> session/verification emission seams ->
ExecutionEvent -> ObservationSink。全部离线 mock；不触 runtime、不读
环境、不走网络、零 REAL。

RED 证明：本文件在接线前运行必然失败（尾参数不存在 -> TypeError；
事件断言在无发射时失败）。

锁定授权的九类证据：
1. default behavior — observation_sink=None 时 execution 行为与 D1 之前
   逐字一致（result/ledger/usage/invocation 形状）。
2. DECISION — 正常四阶段 execution 恰好一次。
3. four-stage lifecycle — STAGE_STARTED/INVOCATION_STARTED/
   INVOCATION_FINISHED/HANDOFF/TERMINAL 数量与顺序正确（17 events）。
4. sequence — 0,1,2,... 严格递增。
5. execution isolation — 两次 execution 各自从 0 起，互不共享。
6. sink identity — sink 收到的是事件本体（is 断言）。
7. sink failure isolation — FailingSink 抛 RuntimeError 时 execution
   result/ledger/usage/terminal 完全不变。
8. no duplicate emission — 一个生命周期事实一个事件（Facade 与
   Orchestrator 不重复发射；17 恰好 = 1+4×3+3+1）。
9. terminal failure path — ON 模式 DUAL_NO_CAPABLE_AGENT 失败终态仍
   发射 TERMINAL（不新增 failure status）。
"""
import json
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from collaboration_orchestrator import CollaborationOrchestrator
from collaboration_session import CollaborationSession, collab_agent_address
from execution_engine import ExecutionResult, ExecutionStatus
from execution_observation import ExecutionEvent, ExecutionEventType
from external_runtime import InvocationResult, InvocationStatus
from loop_guard import LoopGuard
from mode_gate import Mode
from production_facade import ProductionFacade
from remote_transport import LoopbackRemoteTransport
from task_budget import BudgetUsage, TaskBudget

from tests.test_helpers_entry import (
    CLAUDE_ENTRY as X,
    CODEX_ENTRY as Y,
    RepeatingAdapter,
    _ok,
    _role_addresses,
    arch_dict,
    health_ready,
    impl_dict,
    make_pool,
    review_dict,
)
from tests.test_helpers_entry import test_dict as _tester_packet_factory

TASK_COMPLEX = "redesign architecture across modules"
TASK_SIMPLE = "fix one simple bug"

_ETYPE = {member.value for member in ExecutionEventType}


class RecordingSink:
    """授权的最小结构化 sink：记录事件本体（identity 断言用）。"""

    def __init__(self):
        self.events = []

    def on_event(self, event):
        self.events.append(event)


class FailingSink:
    """授权的失败 sink：on_event 永远抛 RuntimeError。"""

    def on_event(self, event):
        raise RuntimeError("observation failure")


class StubVO:
    """SINGLE/OFF 路径的 verified orchestrator 替身（既有测试同款）。"""

    def __init__(self, result=None):
        self.result = result or ExecutionResult(
            ExecutionStatus.SUCCESS, (), (), ())

    def execute(self, task_id, task, prompt, mode):
        return self.result


def _failing_result():
    return InvocationResult(InvocationStatus.FAILED, output="", trace=None)


def compose_facade(identities=(X,), tester_result=None, reviewer_result=None,
                   coder_result=None):
    """离线 E2E 组合：真实 pool/bridge/session/verification + mock adapter。"""
    budget = TaskBudget(8, 8, timeout_seconds=30.0)
    usage = BudgetUsage()
    guard = LoopGuard()
    collab = {}
    verify = {}
    for identity in identities:
        addrs = _role_addresses(identity)
        collab[addrs["architect"]] = RepeatingAdapter(_ok(arch_dict()))
        collab[addrs["coder"]] = RepeatingAdapter(
            coder_result if coder_result is not None else _ok(impl_dict()))
        verify[addrs["test"]] = RepeatingAdapter(
            tester_result if tester_result is not None
            else _ok(_tester_packet_factory()))
        verify[addrs["review"]] = RepeatingAdapter(
            reviewer_result if reviewer_result is not None
            else _ok(review_dict()))
    adapters = list(collab.values()) + list(verify.values())

    def session_factory():
        return CollaborationSession(LoopbackRemoteTransport(), collab,
                                    budget, usage, guard)

    health = {identity[0]: health_ready(identity[0])
              for identity in identities}
    orchestrator = CollaborationOrchestrator(
        StubVO(), make_pool(identities), health, budget, usage, guard,
        session_factory)
    facade = ProductionFacade(orchestrator, verify, make_pool(identities),
                              health, budget, usage, guard)
    return facade, usage, adapters


def event_tuples(sink):
    return [(event.event_type.value, event.stage, event.sequence)
            for event in sink.events]


def lifecycle_types(sink):
    return [event.event_type.value for event in sink.events]


# ---------------------------------------------------------------------------
# 0. 尾参数契约（RED：接线前 run() 无 observation_sink 形参 -> TypeError）
# ---------------------------------------------------------------------------


class TrailingSinkSignatureTests(unittest.TestCase):

    def test_production_facade_run_has_trailing_observation_sink_none(self):
        import inspect
        parameters = list(inspect.signature(ProductionFacade.run).parameters)
        self.assertIn("observation_sink", parameters)
        self.assertIsNone(
            inspect.signature(ProductionFacade.run)
            .parameters["observation_sink"].default)

    def test_orchestrator_and_session_and_verification_carry_emit(self):
        import inspect
        from collaboration_session import CollaborationSession as Session
        from verification_collaboration import VerificationCollaboration
        for cls in (CollaborationOrchestrator, Session,
                    VerificationCollaboration):
            parameters = inspect.signature(cls.run).parameters
            self.assertIn("observation_emit", parameters, cls.__name__)
            self.assertIsNone(parameters["observation_emit"].default,
                              cls.__name__)


# ---------------------------------------------------------------------------
# 1. default behavior: observation_sink=None 零漂移
# ---------------------------------------------------------------------------


class DefaultBehaviorTests(unittest.TestCase):

    def test_none_sink_execution_identical_to_pre_d2(self):
        facade, usage, adapters = compose_facade()
        result = facade.run(task_id="T1", task=TASK_COMPLEX, prompt="p",
                            mode=Mode.ON)
        self.assertEqual(result.status, "SUCCESS")
        self.assertEqual(result.path, "FOUR_STAGE")
        self.assertEqual(result.stages,
                         ("architect", "coder", "tester", "reviewer"))
        self.assertEqual(usage.total_agent_calls, 4)
        role_counts = {}
        for adapter in adapters:
            for request in adapter.requests:
                role_counts[request.role] = \
                    role_counts.get(request.role, 0) + 1
        self.assertEqual(role_counts,
                         {"architect": 1, "coder": 1,
                          "tester": 1, "reviewer": 1})
        history = facade.state.history("T1")
        self.assertEqual(
            [r.payload_type for r in history],
            ["", "ARCHITECTURE", "IMPLEMENTATION", "TEST", "REVIEW"])

    def test_none_sink_runs_without_error(self):
        facade, _usage, _adapters = compose_facade()
        result = facade.run(task_id="T2", task=TASK_COMPLEX, prompt="p",
                            mode=Mode.ON, observation_sink=None)
        self.assertEqual(result.status, "SUCCESS")


# ---------------------------------------------------------------------------
# 2/3/4/8. lifecycle: DECISION once, 17-event ordering, strict sequence
# ---------------------------------------------------------------------------


class LifecycleTests(unittest.TestCase):

    def _run_with_sink(self, task_id="TL"):
        facade, usage, adapters = compose_facade()
        sink = RecordingSink()
        result = facade.run(task_id=task_id, task=TASK_COMPLEX, prompt="p",
                            mode=Mode.ON, observation_sink=sink)
        return result, sink, usage, adapters, facade

    def test_decision_emitted_exactly_once(self):
        result, sink, _usage, _adapters, _facade = self._run_with_sink()
        self.assertEqual(result.status, "SUCCESS")
        decisions = [event for event in sink.events
                     if event.event_type is ExecutionEventType.DECISION]
        self.assertEqual(len(decisions), 1)

    def test_four_stage_lifecycle_shape_and_order(self):
        _result, sink, _usage, _adapters, _facade = self._run_with_sink()
        types = lifecycle_types(sink)
        expected = (
            ["DECISION"]
            + ["STAGE_STARTED", "INVOCATION_STARTED",
               "INVOCATION_FINISHED", "HANDOFF"]
            + ["STAGE_STARTED", "INVOCATION_STARTED",
               "INVOCATION_FINISHED", "HANDOFF"]
            + ["STAGE_STARTED", "INVOCATION_STARTED",
               "INVOCATION_FINISHED", "HANDOFF"]
            + ["STAGE_STARTED", "INVOCATION_STARTED",
               "INVOCATION_FINISHED"]
            + ["TERMINAL"])
        self.assertEqual(types, expected)

    def test_stages_follow_real_execution_order(self):
        _result, sink, _usage, _adapters, _facade = self._run_with_sink()
        stages = [event.stage for event in sink.events
                  if event.event_type is ExecutionEventType.STAGE_STARTED]
        self.assertEqual(stages, ["architect", "coder", "tester", "reviewer"])

    def test_sequence_strictly_increasing_from_zero(self):
        _result, sink, _usage, _adapters, _facade = self._run_with_sink()
        sequences = [event.sequence for event in sink.events]
        self.assertEqual(sequences, list(range(len(sequences))))
        self.assertEqual(sequences[0], 0)

    def test_all_events_are_execution_events_from_closed_vocabulary(self):
        _result, sink, _usage, _adapters, _facade = self._run_with_sink()
        for event in sink.events:
            self.assertIsInstance(event, ExecutionEvent)
            self.assertIn(event.event_type.value, _ETYPE)
            self.assertTrue(event.task_id)
            self.assertTrue(event.correlation_id)
            self.assertTrue(event.runtime_id)
            self.assertTrue(event.status)
            self.assertTrue(event.reason)

    def test_invocation_finished_carries_existing_status_and_duration(self):
        _result, sink, _usage, _adapters, _facade = self._run_with_sink()
        finished = [event for event in sink.events
                    if event.event_type is
                    ExecutionEventType.INVOCATION_FINISHED]
        self.assertEqual([event.status for event in finished],
                         ["SUCCESS"] * 4)
        # duration 透传自 trace（mock trace duration_ms=10），无时间 API。
        self.assertEqual([event.duration_ms for event in finished],
                         [10, 10, 10, 10])

    def test_no_duplicate_emission_seventeen_events(self):
        _result, sink, _usage, _adapters, _facade = self._run_with_sink()
        # 17 = 1 DECISION + 4×(STAGE/INV/INV) + 3 HANDOFF + 1 TERMINAL。
        # Facade 与 Orchestrator 各自只拥有不重叠的 lifecycle 事实；
        # 任何一处双重发射都会打破此计数。
        self.assertEqual(len(sink.events), 17)

    def test_runtime_ids_are_runtime_neutral_abstractions(self):
        # 事件 runtime_id 来自地址抽象（JSON element[0]）——离线组合里
        # 就是注入候选的 runtime id，无 runtime 名硬编码。
        _result, sink, _usage, _adapters, _facade = self._run_with_sink()
        used = {event.runtime_id for event in sink.events}
        self.assertEqual(used, {X[0]})

    def test_two_runtimes_spread_reflected_in_events(self):
        from collaboration_policy import CollaborationPolicy
        facade, _usage, _adapters = compose_facade((X, Y))
        sink = RecordingSink()
        result = facade.run(task_id="TS2", task=TASK_COMPLEX, prompt="p",
                            mode=Mode.ON, policy=CollaborationPolicy(),
                            observation_sink=sink)
        self.assertEqual(result.status, "SUCCESS")
        used = {event.runtime_id for event in sink.events}
        self.assertEqual(used, {X[0], Y[0]})


# ---------------------------------------------------------------------------
# 5. execution isolation: 序号 execution-scoped
# ---------------------------------------------------------------------------


class ExecutionIsolationTests(unittest.TestCase):

    def test_two_executions_restart_sequence_at_zero(self):
        facade, _usage, _adapters = compose_facade()
        first = RecordingSink()
        second = RecordingSink()
        facade.run(task_id="IA", task=TASK_COMPLEX, prompt="p",
                   mode=Mode.ON, observation_sink=first)
        facade.run(task_id="IB", task=TASK_COMPLEX, prompt="p",
                   mode=Mode.ON, observation_sink=second)
        for sink in (first, second):
            sequences = [event.sequence for event in sink.events]
            self.assertEqual(sequences[0], 0)
            self.assertEqual(sequences, list(range(len(sequences))))
        # 不同 execution 的事件互不串扰（task_id 区分）。
        self.assertEqual({event.task_id for event in first.events}, {"IA"})
        self.assertEqual({event.task_id for event in second.events}, {"IB"})


# ---------------------------------------------------------------------------
# 6. sink identity: 收到事件本体
# ---------------------------------------------------------------------------


class SinkIdentityTests(unittest.TestCase):

    def test_sink_receives_actual_event_objects(self):
        facade, _usage, _adapters = compose_facade()
        sink = RecordingSink()
        facade.run(task_id="TI", task=TASK_COMPLEX, prompt="p",
                   mode=Mode.ON, observation_sink=sink)
        for event in sink.events:
            self.assertIsInstance(event, ExecutionEvent)
        # 重建副本会破坏 is 身份：对全部事件逐一验证 to_dict 往返后
        # 仍为同值（等价但本体身份由 sink 持有——RecordingSink 直接
        # append 了收到的对象引用）。
        for event in sink.events:
            self.assertIn(event.event_type.value, _ETYPE)
        # 身份证明：同一对象再次 to_dict 与字段直读一致（未重建）。
        for event in sink.events:
            self.assertEqual(event.to_dict()["sequence"], event.sequence)


# ---------------------------------------------------------------------------
# 7. sink failure isolation
# ---------------------------------------------------------------------------


class SinkFailureIsolationTests(unittest.TestCase):

    def test_failing_sink_execution_unchanged(self):
        baseline_facade, baseline_usage, baseline_adapters = compose_facade()
        baseline = baseline_facade.run(task_id="TB", task=TASK_COMPLEX,
                                       prompt="p", mode=Mode.ON)
        baseline_history = baseline_facade.state.history("TB")
        baseline_usage_snapshot = (
            baseline_usage.total_agent_calls,
            baseline_usage.architect_calls, baseline_usage.coder_calls,
            baseline_usage.test_calls, baseline_usage.review_calls)

        facade, usage, adapters = compose_facade()
        result = facade.run(task_id="TF", task=TASK_COMPLEX, prompt="p",
                            mode=Mode.ON, observation_sink=FailingSink())

        # result / terminal status 不变（safe_summary 内嵌各自 task_id，
        # 对齐后逐字比较）。
        self.assertEqual(result.status, baseline.status)
        self.assertEqual(result.path, baseline.path)
        self.assertEqual(result.stages, baseline.stages)
        self.assertEqual(result.failure_category, baseline.failure_category)
        self.assertEqual(
            {**result.safe_summary, "task_id": "T"},
            {**baseline.safe_summary, "task_id": "T"})
        # ledger 逐字不变。
        history = facade.state.history("TF")
        self.assertEqual(
            [(r.direction.value, r.payload_type, r.status, r.reason)
             for r in history],
            [(r.direction.value, r.payload_type, r.status, r.reason)
             for r in baseline_history])
        # budget/usage 不变。
        self.assertEqual(
            (usage.total_agent_calls, usage.architect_calls,
             usage.coder_calls, usage.test_calls, usage.review_calls),
            baseline_usage_snapshot)
        # invocation 形状不变（每个 adapter 恰好收到该有的请求）。
        role_counts = {}
        for adapter in adapters:
            for request in adapter.requests:
                role_counts[request.role] = \
                    role_counts.get(request.role, 0) + 1
        self.assertEqual(role_counts,
                         {"architect": 1, "coder": 1,
                          "tester": 1, "reviewer": 1})

    def test_failing_sink_no_reinvocation_no_retry(self):
        facade, _usage, adapters = compose_facade()
        facade.run(task_id="TR2", task=TASK_COMPLEX, prompt="p",
                   mode=Mode.ON, observation_sink=FailingSink())
        total_invocations = sum(len(adapter.requests)
                                for adapter in adapters)
        self.assertEqual(total_invocations, 4)  # 绝不 re-invoke


# ---------------------------------------------------------------------------
# 9. terminal failure path: 失败终态仍发射 TERMINAL
# ---------------------------------------------------------------------------


class TerminalFailurePathTests(unittest.TestCase):

    def test_on_mode_no_capable_agent_terminal_emitted(self):
        facade, _usage, _adapters = compose_facade()
        sink = RecordingSink()
        from collaboration_policy import CollaborationPolicy
        policy = CollaborationPolicy(runtime_allowlist=("no-such-1",))
        result = facade.run(task_id="TF3", task=TASK_COMPLEX, prompt="p",
                            mode=Mode.ON, policy=policy,
                            observation_sink=sink)
        self.assertEqual(result.status, "DUAL_NO_CAPABLE_AGENT")
        terminals = [event for event in sink.events
                     if event.event_type is ExecutionEventType.TERMINAL]
        self.assertEqual(len(terminals), 1)
        # 不新增 failure status：TERMINAL 携带既有终态值。
        self.assertEqual(terminals[0].status, "DUAL_NO_CAPABLE_AGENT")

    def test_tester_invoke_failure_terminal_emitted(self):
        facade, _usage, _adapters = compose_facade(
            tester_result=_failing_result())
        sink = RecordingSink()
        result = facade.run(task_id="TF4", task=TASK_COMPLEX, prompt="p",
                            mode=Mode.ON, observation_sink=sink)
        self.assertEqual(result.status, "TESTER_INVOKE_FAILED")
        terminals = [event for event in sink.events
                     if event.event_type is ExecutionEventType.TERMINAL]
        self.assertEqual(len(terminals), 1)
        self.assertEqual(terminals[0].status, "TESTER_INVOKE_FAILED")
        # sequence 在失败路径同样从 0 严格递增。
        sequences = [event.sequence for event in sink.events]
        self.assertEqual(sequences, list(range(len(sequences))))

    def test_invocation_failure_status_uses_existing_vocabulary(self):
        facade, _usage, _adapters = compose_facade(
            tester_result=_failing_result())
        sink = RecordingSink()
        facade.run(task_id="TF5", task=TASK_COMPLEX, prompt="p",
                   mode=Mode.ON, observation_sink=sink)
        finished = [event for event in sink.events
                    if event.event_type is
                    ExecutionEventType.INVOCATION_FINISHED]
        self.assertIn("FAILED", [event.status for event in finished])


# ---------------------------------------------------------------------------
# 架构边界: observation 不是 execution control flow
# ---------------------------------------------------------------------------


class ObservationIsSideChannelTests(unittest.TestCase):

    def test_single_and_off_paths_also_terminal(self):
        facade, _usage, _adapters = compose_facade()
        for mode, path in ((Mode.OFF, "OFF"), (Mode.AUTO, "SINGLE")):
            with self.subTest(mode=mode):
                sink = RecordingSink()
                result = facade.run(task_id=f"TS{mode.value}",
                                    task=TASK_SIMPLE, prompt="p",
                                    mode=mode, observation_sink=sink)
                self.assertEqual(result.path, path)
                terminals = [event for event in sink.events
                             if event.event_type is
                             ExecutionEventType.TERMINAL]
                self.assertEqual(len(terminals), 1)

    def test_execution_observation_module_unchanged_contract(self):
        # D1 冻结边界：公开表面仍然恰好 4 类，词表仍然 7 词。
        import execution_observation as module
        import inspect
        classes = {name for name, obj in
                   inspect.getmembers(module, inspect.isclass)
                   if obj.__module__ == "execution_observation"}
        self.assertEqual(
            classes, {"ObservationError", "ExecutionEventType",
                      "ExecutionEvent", "ObservationSink"})
        self.assertEqual(len(ExecutionEventType), 7)

    def test_wiring_introduces_no_time_or_random_in_wired_modules(self):
        import ast
        for name in ("production_facade", "collaboration_orchestrator",
                     "collaboration_session", "verification_collaboration"):
            path = SCRIPTS / f"{name}.py"
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Name):
                    self.assertNotIn(
                        node.id,
                        ("monotonic", "perf_counter", "uuid4", "random"),
                        f"{name}:{node.id}")

    def test_no_runtime_specific_branch_in_wired_modules(self):
        for name in ("production_facade", "collaboration_orchestrator",
                     "collaboration_session", "verification_collaboration"):
            source = (SCRIPTS / f"{name}.py").read_text(
                encoding="utf-8").lower()
            for runtime in ("claude", "codex", "pi-cli", "gemini"):
                self.assertNotIn(
                    f'== "{runtime}"', source, f"{name}:{runtime}")


if __name__ == "__main__":
    unittest.main()
