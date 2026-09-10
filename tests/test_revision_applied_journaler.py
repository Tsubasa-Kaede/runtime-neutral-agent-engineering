"""OBS-4c tests: RevisionAppliedJournaler（handoff→REVISION_APPLIED 转译）。

链路（组合根接线，旁挂、不在委托路径上）：

    REV-2 UsageCapture handoff seam
        ↓ on_handoff(invocation_id, applied_revisions)
    RevisionAppliedJournaler（factual write authority，非裁决者）
        ↓ revision_writer capability
    账本 REVISION_APPLIED（per-revision、FIFO、append-only）

锁定语义：
- 唯一生产入口 = on_handoff（语义上对应真实 handoff observation）；
  零旁路 API（apply_revision / record_revision / mark_applied 等）。
- id 逐字、不排序、不去重、不转换；空携带 no-op。
- execution_id / execution_version 取触发时刻 boundary 只读快照
  ——版本真值归 boundary，本层只观察（V1 动态证明）。
- 单条 append 失败按条隔离（Exception 级）：成功项已记录、失败项
  诚实缺席，绝不改变执行 outcome；BaseException 政策不在本层扩张。
- 认识论上限：APPLIED = B 级委托已发起，绝不声称 runtime honored。
"""
import ast
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
import sys
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from revision_applied_journaler import RevisionAppliedJournaler  # noqa: E402
from control_boundary import (  # noqa: E402
    ControlBoundary,
    ControlCommand,
    ControlCommandType,
    ControlModelError,
    RevisionPayload,
    RevisionTarget,
)
from control_journal import ControlFactType, ControlJournal  # noqa: E402

MODULE_PATH = SCRIPTS / "revision_applied_journaler.py"


def make_boundary(execution_id="exec-1", initial_version=0):
    journal = ControlJournal()
    boundary = ControlBoundary(journal=journal, execution_id=execution_id,
                               initial_version=initial_version)
    return boundary, journal


def next_rev(command_id, text, execution_id="exec-1"):
    return ControlCommand(
        command_id=command_id, execution_id=execution_id,
        command=ControlCommandType.REVISE,
        payload=RevisionPayload(target=RevisionTarget.NEXT_INVOCATION,
                                text=text),
        expected_version=0)


class _FakeJournal:
    """结构化账本替身：可注入 per-revision append 失败。"""

    def __init__(self, fail_on=()):
        self._fail_on = set(fail_on)
        self.facts = []  # (fact_type, command_id, execution_version, payload)

    def revision_writer(self):
        journal = self

        class _Writer:
            def append(self, *, fact_type, execution_id, command_id,
                       execution_version, payload):
                if command_id in journal._fail_on:
                    raise RuntimeError("journal down for " + command_id)
                journal.facts.append(
                    (fact_type, command_id, execution_version, payload))

        return _Writer()


class ConstructionTests(unittest.TestCase):
    """J1：构造拒绝非法 boundary / journal / writer。"""

    def test_rejects_non_boundary(self):
        _, journal = make_boundary()
        with self.assertRaises(ControlModelError):
            RevisionAppliedJournaler(object(), journal)

    def test_rejects_journal_without_revision_writer(self):
        boundary, _ = make_boundary()
        with self.assertRaises(ControlModelError):
            RevisionAppliedJournaler(boundary, object())

    def test_rejects_writer_without_append(self):
        boundary, _ = make_boundary()

        class _BadFactoryJournal:
            def revision_writer(self):
                return object()  # 无 callable append

        with self.assertRaises(ControlModelError):
            RevisionAppliedJournaler(boundary, _BadFactoryJournal())


class FactProductionTests(unittest.TestCase):
    """J2-J5、J10：事实形状、FIFO、逐字、空 no-op、类型恒定。"""

    def test_single_revision_fact_shape(self):
        boundary, journal = make_boundary(execution_id="exec-9",
                                          initial_version=3)
        journaler = RevisionAppliedJournaler(boundary, journal)
        journaler.on_handoff("invocation-abc", ("rev-1",))
        facts = journal.snapshot()
        self.assertEqual(len(facts), 1)
        fact = facts[0]
        self.assertIs(fact.fact_type, ControlFactType.REVISION_APPLIED)
        self.assertEqual(fact.command_id, "rev-1")
        self.assertEqual(fact.execution_id, "exec-9")
        self.assertEqual(fact.execution_version, 3)  # boundary 只读快照
        self.assertEqual(fact.payload["invocation_id"], "invocation-abc")

    def test_multiple_revisions_fifo_order(self):
        boundary, journal = make_boundary()
        journaler = RevisionAppliedJournaler(boundary, journal)
        journaler.on_handoff("inv-x", ("rev-1", "rev-2", "rev-3"))
        facts = journal.snapshot()
        self.assertEqual([fact.command_id for fact in facts],
                         ["rev-1", "rev-2", "rev-3"])  # FIFO 逐字
        self.assertEqual([fact.seq for fact in facts], [0, 1, 2])
        for fact in facts:
            self.assertEqual(fact.payload["invocation_id"], "inv-x")

    def test_revision_id_verbatim(self):
        boundary, journal = make_boundary()
        journaler = RevisionAppliedJournaler(boundary, journal)
        journaler.on_handoff("inv-x", ("cmd-42",))
        self.assertEqual(journal.snapshot()[0].command_id, "cmd-42")

    def test_empty_tuple_is_noop(self):
        boundary, journal = make_boundary()
        journaler = RevisionAppliedJournaler(boundary, journal)
        journaler.on_handoff("inv-x", ())
        self.assertEqual(journal.snapshot(), ())

    def test_fact_type_always_revision_applied(self):
        boundary, journal = make_boundary()
        journaler = RevisionAppliedJournaler(boundary, journal)
        journaler.on_handoff("inv-x", ("r1", "r2"))
        for fact in journal.snapshot():
            self.assertIs(fact.fact_type, ControlFactType.REVISION_APPLIED)

    def test_list_carrier_iterated_verbatim(self):
        # seam 可能携带 list（REV-2 逐字透传）：按序逐条，零转换
        boundary, journal = make_boundary()
        journaler = RevisionAppliedJournaler(boundary, journal)
        journaler.on_handoff("inv-x", ["rev-b", "rev-a"])
        self.assertEqual([f.command_id for f in journal.snapshot()],
                         ["rev-b", "rev-a"])  # 原顺序，不排序


class FailureIsolationTests(unittest.TestCase):
    """J6：单条 append 失败按条隔离、部分持久化诚实。"""

    def test_append_failure_isolated_per_revision(self):
        boundary, _ = make_boundary()
        fake = _FakeJournal(fail_on={"rev-2"})
        journaler = RevisionAppliedJournaler(boundary, fake)
        journaler.on_handoff("inv-x", ("rev-1", "rev-2", "rev-3"))
        # 无异常传播；rev-1/rev-3 已记录，rev-2 诚实缺席；顺序保持
        self.assertEqual([fact[1] for fact in fake.facts],
                         ["rev-1", "rev-3"])

    def test_all_append_failures_swallowed(self):
        boundary, _ = make_boundary()
        fake = _FakeJournal(fail_on={"r1", "r2"})
        journaler = RevisionAppliedJournaler(boundary, fake)
        journaler.on_handoff("inv-x", ("r1", "r2"))
        self.assertEqual(fake.facts, [])


class BoundaryNeutralityTests(unittest.TestCase):
    """J7：journaler 对 boundary 零 mutation、零 submit。"""

    def test_boundary_untouched_after_firing(self):
        boundary, journal = make_boundary()
        boundary.submit(next_rev("rev-1", "text"))
        version_before = boundary.execution_version
        pending_before = boundary.pending_intent
        requested_before = journal.snapshot()
        journaler = RevisionAppliedJournaler(boundary, journal)
        journaler.on_handoff("inv-x", ("rev-1",))
        self.assertEqual(boundary.execution_version, version_before)
        self.assertEqual(boundary.pending_intent, pending_before)
        # REQUESTED 事实原封；恰新增一条 APPLIED
        facts = journal.snapshot()
        self.assertEqual(facts[:len(requested_before)], requested_before)
        self.assertEqual(len(facts), len(requested_before) + 1)
        self.assertIs(facts[-1].fact_type, ControlFactType.REVISION_APPLIED)

    def test_no_submit_or_snapshot_calls_in_source(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        self.assertNotIn("submit(", source)
        self.assertNotIn("snapshot(", source)


class VersionSnapshotTests(unittest.TestCase):
    """V1：execution_version 来自触发时刻 boundary 只读快照。"""

    def test_version_read_at_fire_time_not_cached(self):
        class _SteppingBoundary(ControlBoundary):
            """测试替身：版本在两次触发之间前进——若 journaler
            缓存构造期值或自维护计数即暴露。"""

            def __init__(self, journal):
                super().__init__(journal=journal, execution_id="exec-v",
                                 initial_version=5)
                self.extra = 0

            @property
            def execution_version(self):
                return 5 + self.extra

        journal = ControlJournal()
        boundary = _SteppingBoundary(journal)
        journaler = RevisionAppliedJournaler(boundary, journal)
        journaler.on_handoff("inv-a", ("rev-1",))
        boundary.extra = 1  # 版本前进（模拟未来 epoch 语义）
        journaler.on_handoff("inv-b", ("rev-2",))
        versions = [fact.execution_version for fact in journal.snapshot()]
        self.assertEqual(versions, [5, 6])  # 逐次读 boundary，非缓存


class UniqueEntryTests(unittest.TestCase):
    """V2：唯一生产入口 = on_handoff；零旁路 API。"""

    def test_no_bypass_api_in_class_surface(self):
        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        class_methods = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                for item in node.body:
                    if isinstance(item, ast.FunctionDef):
                        class_methods.add(item.name)
        module_functions = {node.name for node in tree.body
                            if isinstance(node, ast.FunctionDef)}
        # 类面恰两方法：构造 + 唯一 handoff 入口
        self.assertEqual(class_methods, {"__init__", "on_handoff"})
        self.assertEqual(module_functions, set())  # 无模块级生产函数

    def test_no_bypass_names_anywhere(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        for token in ("apply_revision", "record_revision", "mark_applied"):
            self.assertNotIn(token, source, token)


class ArchitectureTests(unittest.TestCase):
    """J8/J9：纯度源扫描 + AST import 锁。"""

    def test_import_roots_locked(self):
        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        roots = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots.update(alias.name.split(".")[0]
                             for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                roots.add(node.module.split(".")[0])
        self.assertEqual(roots, {"__future__", "control_boundary",
                                 "control_journal"})

    def test_source_purity(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        for token in ("uuid", "monotonic", "subprocess", "Popen",
                      "ExecutionEvent", "INVOCATION_STARTED",
                      "INVOCATION_FINISHED",
                      "claude", "gemini", "codex", "qwen", "opencode",
                      "cline", "deepseek"):
            self.assertNotIn(token, source, token)


if __name__ == "__main__":
    unittest.main()
