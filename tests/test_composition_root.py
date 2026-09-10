"""COMP-1 tests: CompositionRoot / ExecutionComposition（组合根）。

层级关系：

    caller（未来编排层/CLI/TUI）
        ↓ .compose_execution(execution_id, ...)
    CompositionRoot（object-graph 组合：恰一 journal + 唯一性闸）
        ↓ 复用 build_wrap_stack（OBS-4c 冻结，恰一次/每 execution）
    ExecutionComposition（不可变薄句柄：.boundary + .invoke）
        ↓ WrapStack.invoke → AbortGate → RevisionAdapter → UsageCapture → raw

锁定语义：
- one execution → one boundary → one journal → one revision pipeline
  （唯一性闸 = in-memory 已组集合 ∪ journal 历史事实扫描）。
- 组装零重写：build_wrap_stack 是唯一组装路径（恰一个调用点）。
- root 是装配者不是引擎：ControlAborted 原样穿透（零捕获/零
  ABORT_CONFIRMED/零终态事实）；invoke 路径零 root 锁（恰一把
  组合域 Lock 只保护 compose 的 check-register 临界区）。
- execution_id 一律 caller 供应（零生成）；构造失败不烧毁 id。
- facts() 仅代理 journal.snapshot()（detached；不外泄 writer
  capability 或 journal 引用）。
"""
import ast
import threading
import time
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
import sys
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from composition_root import (  # noqa: E402
    CompositionRoot,
    ExecutionComposition,
)
from control_boundary import (  # noqa: E402
    ControlBoundary,
    ControlCommand,
    ControlCommandType,
    ControlModelError,
    ControlStatus,
    RevisionPayload,
    RevisionTarget,
)
from control_gate import ControlAborted  # noqa: E402
from control_journal import ControlFactType, ControlJournal  # noqa: E402
from external_runtime import (  # noqa: E402
    ExternalAgentRequest,
    InvocationResult,
    InvocationStatus,
)
from usage_capture import UsageCaptureError  # noqa: E402
from usage_log import UsageLog  # noqa: E402

MODULE_PATH = SCRIPTS / "composition_root.py"


def _success():
    return InvocationResult(status=InvocationStatus.SUCCESS)


class _SpyRaw:
    """真实 spy：方法体首行自记入口与 request（委托证据）。"""

    def __init__(self, result=None, raises=None, release=None):
        self.entries = []
        self.requests = []
        self._result = result
        self._raises = raises
        self._release = release

    def invoke(self, request):
        self.entries.append(request.task_id)
        self.requests.append(request)
        if self._release is not None:
            self._release.wait(5.0)
        if self._raises is not None:
            raise self._raises
        return self._result


def next_rev(command_id, text, execution_id="exec-1"):
    return ControlCommand(
        command_id=command_id, execution_id=execution_id,
        command=ControlCommandType.REVISE,
        payload=RevisionPayload(target=RevisionTarget.NEXT_INVOCATION,
                                text=text),
        expected_version=0)


def abort_cmd(command_id="a1", execution_id="exec-1"):
    return ControlCommand(command_id=command_id,
                          execution_id=execution_id,
                          command=ControlCommandType.ABORT)


def make_request(task_id="task-1", prompt="base prompt"):
    return ExternalAgentRequest(task_id=task_id, prompt=prompt,
                                agent_id="agent-1", role="coder")


def make_composition(root=None, execution_id="exec-1", raw=None):
    if root is None:
        root = CompositionRoot()
    if raw is None:
        raw = _SpyRaw(result=_success())
    composition = root.compose_execution(
        execution_id, raw, UsageLog(),
        runtime_id="runtime-x", role="coder")
    return composition, root, raw


class SurfaceTests(unittest.TestCase):
    """公开面最小性 + 同对象图证明。"""

    def test_composition_public_surface(self):
        composition, _, _ = make_composition()
        self.assertIsInstance(composition, ExecutionComposition)
        self.assertEqual(
            sorted(name for name in dir(composition)
                   if not name.startswith("_")),
            ["boundary", "invoke"])  # 恰两面，无内部对象外泄

    def test_root_public_surface(self):
        root = CompositionRoot()
        self.assertEqual(
            sorted(name for name in dir(root)
                   if not name.startswith("_")),
            ["compose_execution", "facts"])

    def test_boundary_and_invoke_are_same_object_graph(self):
        # 经把手 submit + 经 invoke 执行 ⇒ overlay 到达 raw、双事实
        # 落账：证明 .boundary 与 .invoke 焊在同一 WrapStack 图上
        composition, root, raw = make_composition()
        composition.boundary.submit(next_rev("rev-1", "use X"))
        result = composition.invoke(make_request())
        self.assertIsInstance(result, InvocationResult)
        self.assertEqual(len(raw.entries), 1)
        self.assertIn("[USER REVISION rev-1]", raw.requests[0].prompt)
        self.assertEqual(
            [f.fact_type for f in root.facts()],
            [ControlFactType.REVISE_REQUESTED,
             ControlFactType.REVISION_APPLIED])


class FactsTests(unittest.TestCase):
    """facts() 代理 + detached + 单账本。"""

    def test_facts_detached_snapshot(self):
        composition, root, _ = make_composition()
        composition.boundary.submit(next_rev("rev-1", "t"))
        before = root.facts()
        composition.invoke(make_request())  # 此后账本新增 APPLIED
        self.assertEqual(len(before), 1)  # 旧快照不被后续追加改变
        self.assertEqual(len(root.facts()), 2)

    def test_single_journal_all_fact_types(self):
        composition, root, _ = make_composition()
        composition.boundary.submit(next_rev("rev-1", "t"))
        composition.invoke(make_request())
        composition.boundary.submit(abort_cmd())
        types = {f.fact_type for f in root.facts()}
        self.assertEqual(types, {ControlFactType.REVISE_REQUESTED,
                                 ControlFactType.REVISION_APPLIED,
                                 ControlFactType.ABORT_REQUESTED})

    def test_explicit_journal_shared(self):
        journal = ControlJournal()
        root = CompositionRoot(journal=journal)
        composition, _, _ = make_composition(root=root)
        composition.boundary.submit(next_rev("rev-1", "t"))
        self.assertEqual(len(journal.snapshot()), 1)  # 事实入 caller 账本


class UniquenessTests(unittest.TestCase):
    """唯一性闸：同 root 重复 / journal 历史事实 / 构造失败不烧 id。"""

    def test_duplicate_execution_id_same_root_rejected(self):
        root = CompositionRoot()
        make_composition(root=root, execution_id="exec-1")
        with self.assertRaises(ControlModelError):
            root.compose_execution("exec-1", _SpyRaw(result=_success()),
                                   UsageLog(), runtime_id="r", role="coder")

    def test_duplicate_rejected_even_without_any_facts(self):
        # 零 submit 的空 boundary：in-memory 集合拦截（journal 无证据）
        root = CompositionRoot()
        make_composition(root=root, execution_id="exec-empty")
        with self.assertRaises(ControlModelError):
            root.compose_execution("exec-empty", _SpyRaw(result=_success()),
                                   UsageLog(), runtime_id="r", role="coder")

    def test_journal_historical_duplicate_rejected(self):
        # 另一 boundary 已在共享账本为该 execution 落过事实
        journal = ControlJournal()
        other = ControlBoundary(journal=journal, execution_id="exec-hist")
        other.submit(next_rev("rev-9", "t", "exec-hist"))
        root = CompositionRoot(journal=journal)
        with self.assertRaises(ControlModelError):
            root.compose_execution("exec-hist", _SpyRaw(result=_success()),
                                   UsageLog(), runtime_id="r", role="coder")

    def test_construction_failure_does_not_burn_execution_id(self):
        root = CompositionRoot()
        with self.assertRaises(UsageCaptureError):  # 坏 raw
            root.compose_execution("exec-f", object(), UsageLog(),
                                   runtime_id="r", role="coder")
        # 同 id 重来（合法 raw）必须成功——失败不登记
        composition, _, raw = make_composition(root=root,
                                               execution_id="exec-f")
        composition.boundary.submit(next_rev("rev-1", "t", "exec-f"))
        composition.invoke(make_request())
        self.assertEqual(len(raw.entries), 1)


class LifecycleTests(unittest.TestCase):
    """多 execution 隔离 + 句柄丢弃后事实长存。"""

    def test_multiple_executions_isolated(self):
        root = CompositionRoot()
        comp_a, _, raw_a = make_composition(root=root, execution_id="exec-a")
        comp_b, _, raw_b = make_composition(root=root, execution_id="exec-b")
        self.assertIsNot(comp_a.boundary, comp_b.boundary)
        comp_a.boundary.submit(next_rev("rev-a1", "alpha", "exec-a"))
        comp_b.boundary.submit(next_rev("rev-b1", "beta", "exec-b"))
        comp_a.invoke(make_request(task_id="task-a"))
        comp_b.invoke(make_request(task_id="task-b"))
        self.assertIn("alpha", raw_a.requests[0].prompt)
        self.assertNotIn("beta", raw_a.requests[0].prompt)  # 互不污染
        self.assertIn("beta", raw_b.requests[0].prompt)
        by_execution = {}
        for fact in root.facts():
            by_execution.setdefault(fact.execution_id, []).append(
                fact.fact_type)
        self.assertEqual(by_execution["exec-a"],
                         [ControlFactType.REVISE_REQUESTED,
                          ControlFactType.REVISION_APPLIED])
        self.assertEqual(by_execution["exec-b"],
                         [ControlFactType.REVISE_REQUESTED,
                          ControlFactType.REVISION_APPLIED])

    def test_facts_survive_handle_discard(self):
        root = CompositionRoot()
        composition, _, _ = make_composition(root=root)
        composition.boundary.submit(next_rev("rev-1", "t"))
        composition.invoke(make_request())
        del composition  # 句柄销毁
        self.assertEqual(len(root.facts()), 2)  # 事实超越句柄


class AbortTests(unittest.TestCase):
    """ControlAborted 穿透 + in-flight 不被 root 捕获。"""

    def test_control_aborted_identity_passthrough_no_confirm_fact(self):
        composition, root, raw = make_composition()
        composition.boundary.submit(next_rev("rev-1", "t"))
        composition.boundary.submit(abort_cmd())
        with self.assertRaises(ControlAborted):
            composition.invoke(make_request())
        self.assertEqual(raw.entries, [])  # raw 零进入
        types = {f.fact_type for f in root.facts()}
        self.assertEqual(types, {ControlFactType.REVISE_REQUESTED,
                                 ControlFactType.ABORT_REQUESTED})
        # 无 ABORT_CONFIRMED / 无终态事实 / 无 APPLIED：root 零捕获零裁决

    def test_in_flight_abort_not_caught_by_root(self):
        release = threading.Event()
        result = _success()
        raw = _SpyRaw(result=result, release=release)
        composition, root, _ = make_composition(raw=raw)
        composition.boundary.submit(next_rev("rev-1", "t"))
        outcome = []

        def worker():
            try:
                outcome.append(composition.invoke(make_request()))
            except BaseException as exc:  # noqa: BLE001  测试侧收集
                outcome.append(exc)

        thread = threading.Thread(target=worker)
        thread.start()
        self.addCleanup(thread.join, 10.0)
        for _ in range(2000):
            if raw.entries or not thread.is_alive():
                break
            time.sleep(0.005)
        composition.boundary.submit(abort_cmd())  # in-flight ABORT
        release.set()
        thread.join()
        self.assertEqual(outcome, [result])  # 不取消，结果原样
        types = {f.fact_type for f in root.facts()}
        self.assertEqual(types, {ControlFactType.REVISE_REQUESTED,
                                 ControlFactType.REVISION_APPLIED,
                                 ControlFactType.ABORT_REQUESTED})
        self.assertEqual(composition.boundary.pending_intent.kind.name,
                         "ABORT")


class RevisionFlowTests(unittest.TestCase):
    """REQUESTED→APPLIED correlation + replay 语义。"""

    def test_revision_correlation_join(self):
        composition, root, _ = make_composition()
        composition.boundary.submit(next_rev("rev-1", "t"))
        composition.invoke(make_request())
        facts = root.facts()
        requested, applied = facts[0], facts[1]
        self.assertEqual(applied.command_id, requested.command_id)  # join
        self.assertIsNotNone(applied.payload["invocation_id"])

    def test_replay_zero_new_facts_via_root_surface(self):
        composition, root, _ = make_composition()
        composition.boundary.submit(next_rev("rev-1", "t"))
        composition.invoke(make_request())
        facts_before = root.facts()
        replay = composition.boundary.submit(next_rev("rev-1", "t"))
        self.assertEqual(replay.status, ControlStatus.ACCEPTED)  # 纯查找
        self.assertEqual(root.facts(), facts_before)  # 零新事实


class IdentityTests(unittest.TestCase):
    """结果 identity / 空队列 request identity。"""

    def test_return_identity_passthrough(self):
        sentinel = _success()
        raw = _SpyRaw(result=sentinel)
        composition, _, _ = make_composition(raw=raw)
        self.assertIs(composition.invoke(make_request()), sentinel)

    def test_empty_queue_request_identity(self):
        raw = _SpyRaw(result=_success())
        composition, _, _ = make_composition(raw=raw)
        request = make_request()
        composition.invoke(request)
        self.assertIs(raw.requests[0], request)  # 原对象等价下传


class ConstructionErrorTests(unittest.TestCase):
    """构造失败：既有异常类型原样传播。"""

    def test_bad_raw_rejected(self):
        root = CompositionRoot()
        with self.assertRaises(UsageCaptureError):
            root.compose_execution("exec-x", object(), UsageLog(),
                                   runtime_id="r", role="coder")

    def test_bad_usage_log_rejected(self):
        root = CompositionRoot()
        with self.assertRaises(UsageCaptureError):
            root.compose_execution("exec-x", _SpyRaw(result=_success()),
                                   object(), runtime_id="r", role="coder")

    def test_empty_execution_id_rejected_by_boundary(self):
        root = CompositionRoot()
        with self.assertRaises(ControlModelError):  # 既有校验透传
            root.compose_execution("", _SpyRaw(result=_success()),
                                   UsageLog(), runtime_id="r", role="coder")

    def test_bad_journal_fails_at_compose(self):
        root = CompositionRoot(journal=object())  # root 不校验账本
        with self.assertRaises(AttributeError):  # 组件语义：用时失败
            root.compose_execution("exec-x", _SpyRaw(result=_success()),
                                   UsageLog(), runtime_id="r", role="coder")


class ConcurrencyTests(unittest.TestCase):
    """compose 原子性 + 栈级并发 invoke 补证。"""

    def test_concurrent_compose_same_id_exactly_one_wins(self):
        root = CompositionRoot()
        count = 8
        barrier = threading.Barrier(count)
        outcomes = []  # list.append 原子，规避 dict += 竞态

        def worker():
            try:
                barrier.wait()
                root.compose_execution(
                    "exec-race", _SpyRaw(result=_success()), UsageLog(),
                    runtime_id="r", role="coder")
                outcomes.append("ok")
            except ControlModelError:
                outcomes.append("rejected")

        threads = [threading.Thread(target=worker) for _ in range(count)]
        for thread in threads:
            thread.start()
            self.addCleanup(thread.join, 5.0)
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(outcomes),
                         ["ok"] + ["rejected"] * (count - 1))  # 恰一成功

    def test_concurrent_invoke_empty_queue_no_applied(self):
        # 对照面：空队列下 N 并发 invoke，零 APPLIED、全部到达
        raw = _SpyRaw(result=_success())
        root = CompositionRoot()
        composition, _, _ = make_composition(root=root, raw=raw)
        count = 8
        barrier = threading.Barrier(count)
        errors = []

        def worker(n):
            try:
                barrier.wait()
                composition.invoke(make_request(task_id=f"task-{n}"))
            except BaseException as exc:  # noqa: BLE001  测试侧收集
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(n,))
                   for n in range(count)]
        for thread in threads:
            thread.start()
            self.addCleanup(thread.join, 5.0)
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(raw.entries), count)
        applied = [f for f in root.facts()
                   if f.fact_type is ControlFactType.REVISION_APPLIED]
        self.assertEqual(applied, [])  # 空队列 ⇒ 零 APPLIED

    def test_concurrent_lanes_independent_revisions_no_crosstalk(self):
        # N 条 execution lane（各自独立 revision）并发 invoke：
        # 每 lane 恰一组 [REQUESTED, APPLIED]、command_id 归属正确、
        # invocation_id 两两互异——零串线
        count = 8
        root = CompositionRoot()
        lanes = []
        for n in range(count):
            composition, _, raw = make_composition(
                root=root, execution_id=f"exec-{n}")
            composition.boundary.submit(
                next_rev(f"rev-{n}", f"text-{n}", f"exec-{n}"))
            lanes.append((composition, raw))
        barrier = threading.Barrier(count)
        errors = []

        def worker(n):
            try:
                barrier.wait()
                lanes[n][0].invoke(make_request(task_id=f"task-{n}"))
            except BaseException as exc:  # noqa: BLE001  测试侧收集
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(n,))
                   for n in range(count)]
        for thread in threads:
            thread.start()
            self.addCleanup(thread.join, 5.0)
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        for n, (_, raw) in enumerate(lanes):  # overlay 只进自家 lane
            self.assertEqual(len(raw.requests), 1)
            self.assertIn(f"[USER REVISION rev-{n}]", raw.requests[0].prompt)
        by_execution = {}
        for fact in root.facts():
            by_execution.setdefault(fact.execution_id, []).append(
                fact.fact_type)
        for n in range(count):
            self.assertEqual(
                by_execution[f"exec-{n}"],
                [ControlFactType.REVISE_REQUESTED,
                 ControlFactType.REVISION_APPLIED])
        applied = [f for f in root.facts()
                   if f.fact_type is ControlFactType.REVISION_APPLIED]
        self.assertEqual(len(applied), count)
        ids = [f.payload["invocation_id"] for f in applied]
        self.assertEqual(len(set(ids)), count)  # invocation_id 互异
        for n, fact in enumerate(sorted(
                applied, key=lambda f: f.execution_id)):
            self.assertEqual(fact.command_id,
                             f"rev-{fact.execution_id.split('-')[1]}")

    def test_concurrent_invoke_same_lane_revision_fan_out(self):
        # 同一 lane 内 1 条 revision × N 并发 invoke（队列不排空）：
        # 每次真实 handoff 恰一条 APPLIED ⇒ N 条、invocation_id 互异
        raw = _SpyRaw(result=_success())
        root = CompositionRoot()
        composition, _, _ = make_composition(root=root, raw=raw)
        composition.boundary.submit(next_rev("rev-1", "t"))
        count = 8
        barrier = threading.Barrier(count)
        errors = []

        def worker(n):
            try:
                barrier.wait()
                composition.invoke(make_request(task_id=f"task-{n}"))
            except BaseException as exc:  # noqa: BLE001  测试侧收集
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(n,))
                   for n in range(count)]
        for thread in threads:
            thread.start()
            self.addCleanup(thread.join, 5.0)
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(raw.entries), count)
        applied = [f for f in root.facts()
                   if f.fact_type is ControlFactType.REVISION_APPLIED]
        self.assertEqual(len(applied), count)  # 每 invoke 恰一事实
        ids = [f.payload["invocation_id"] for f in applied]
        self.assertEqual(len(set(ids)), count)  # invocation_id 互异
        for fact in applied:
            self.assertEqual(fact.command_id, "rev-1")  # 无 id 串线


class ArchitectureTests(unittest.TestCase):
    """import/source 边界 + 无 invoke 路径锁。"""

    def _roots(self):
        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        roots = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots.update(alias.name.split(".")[0]
                             for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                roots.add(node.module.split(".")[0])
        return roots, tree

    def test_import_roots_locked(self):
        roots, _ = self._roots()
        self.assertEqual(roots, {"__future__", "control_boundary",
                                 "control_journal", "threading",
                                 "wrap_stack"})

    def test_no_forbidden_tokens(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        for token in ("cockpit", "control_gate", "external_runtime",
                      "subprocess", "Popen", "kill", "terminate",
                      "ExecutionEvent", "INVOCATION_STARTED",
                      "INVOCATION_FINISHED",
                      "event_index", "trace_projector", "revision_adapter",
                      "abort_gate", "usage_capture", "revision_applied_"
                      "journaler", "ABORT_CONFIRMED",
                      "claude", "gemini", "codex", "qwen", "opencode",
                      "cline", "deepseek"):
            self.assertNotIn(token, source, token)

    def test_single_build_wrap_stack_call_site(self):
        # 组装零重写：恰一个 build_wrap_stack( 调用点（import 不计）
        source = MODULE_PATH.read_text(encoding="utf-8")
        self.assertEqual(source.count("build_wrap_stack("), 1)

    def test_single_composition_domain_lock_not_in_invoke_path(self):
        # 恰一把 Lock；ExecutionComposition 类体零锁零 acquire
        _, tree = self._roots()
        locks = [node for node in ast.walk(tree)
                 if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Name)
                 and node.func.id == "Lock"]
        self.assertEqual(len(locks), 1)
        for node in ast.walk(tree):
            if (isinstance(node, ast.ClassDef)
                    and node.name == "ExecutionComposition"):
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Attribute):
                        self.assertNotIn(sub.attr, ("acquire", "release"))
                    if isinstance(sub, ast.Name):
                        self.assertNotEqual(sub.id, "Lock")


if __name__ == "__main__":
    unittest.main()
