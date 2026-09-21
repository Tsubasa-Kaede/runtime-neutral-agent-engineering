"""ORCH-5 Dispatch Router（cockpit_route）测试。

覆盖授权 §十四 的 28 点矩阵（每测试注明对应点号）：
(1) 空候选集 (2) 单候选 (3) 多候选 (4) 确定性选择（重放逐字节）
(5) 确定性平局决出（池 canonical 序=runtime_id 字典序兜底，乱序
输入钉输出序） (6) 用量证据消费 (7) 调用数维度 (8) 时长维度
(9) token UNKNOWN (10) 货币 UNSUPPORTED (11) UNKNOWN 候选保留
(12) REQUIRE_KNOWN 政策（披露不排除） (13) UNSUPPORTED 维度弃权
(14) 稳定降级 (15) 政策身份（指纹） (16) 组合指纹 (17) DispatchPlan
身份 (18) 不可变结果 (19) 零身份铸造 (20) 零 runtime 调用
(21) 零 provider 依赖 (22) 零语义编译层依赖 (23) 零 Memory 依赖
(24) 零持久化 (25) 零隐藏排序（封闭理由词表） (26) 零随机/时钟
依赖 (27) 既有角色域结果被消费而非重建 (28) 不复制既有执行计划
语义（零执行生命周期字段）。

设计负向：不调用 Runtime、不修改执行域、不写用量日志、不写事件
索引、不读 UI、不直接读 Memory Store（静态墙 + 形状拒绝双证）。

静态墙：import 面恰 {dataclasses,enum,hashlib,json}、json.dumps
恰一次、闭面 __all__、零身份铸造词、零随机/时钟词、零持久化 IO
词、零 provider 名词、零兄弟域模块字面量、零越域词表。

全部离线；REAL=0。
"""
import ast
import re
import subprocess
import sys
import unittest
from dataclasses import FrozenInstanceError, fields
from pathlib import Path
from types import SimpleNamespace

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import cockpit_route
from cockpit_route import (  # noqa: E402
    COST_ORDERED,
    COST_UNKNOWN_RETAINED,
    COST_UNSUPPORTED_DIMENSION_OFF,
    CostDimension,
    CostFactView,
    CostMeasure,
    CostStatus,
    DispatchPlan,
    DispatchSeat,
    DispatchSelection,
    EMPTY_CANDIDATE_POOL,
    NO_HEALTH_WITNESS,
    COST_EVIDENCE_REQUIRED_UNSATISFIED,
    RouteModelError,
    RoutingPolicy,
    RuntimeCostFacts,
    STABLE_ORDER,
    route_composition,
)

MODULE_SOURCE = Path(
    cockpit_route.__file__).read_text(encoding="utf-8")

COUNT = CostDimension.INVOCATION_COUNT
LATENCY = CostDimension.LATENCY
TOKENS = CostDimension.TOKEN_TOTAL
MONEY = CostDimension.MONETARY
K = CostStatus.KNOWN
U = CostStatus.UNKNOWN
S = CostStatus.UNSUPPORTED


def _pool(*specs):
    """池快照鸭（三属性协议形：runtime_id/provider_id/identity）。"""
    return tuple(
        SimpleNamespace(runtime_id=runtime_id, provider_id=provider,
                        identity=runtime_id)
        for runtime_id, provider in specs)


def _seat(member_id="m1", role="coder"):
    return DispatchSeat(member_id=member_id, role=role)


def _rec(runtime_id="rt-a", duration_ms=100, usage_status="KNOWN",
         input_tokens=10, output_tokens=5):
    return SimpleNamespace(
        runtime_id=runtime_id,
        duration_ms=duration_ms,
        usage_status=usage_status,
        input_tokens=input_tokens,
        output_tokens=output_tokens)


def _facts(runtime_id, count=None, latency=None, tokens=None, money=None):
    return RuntimeCostFacts(
        runtime_id=runtime_id,
        invocation_count=count or CostMeasure.unknown(),
        mean_latency_ms=latency or CostMeasure.unknown(),
        token_total=tokens or CostMeasure.unknown(),
        monetary=money or CostMeasure.unsupported())


class ReadyStatus(str):
    """健康见证鸭（枚举形：带 .value）。"""

    def __init__(self, value):
        self.value = value


READY = SimpleNamespace(status="READY")
BUSY = SimpleNamespace(status="BUSY")
ENUM_READY = SimpleNamespace(status=ReadyStatus("READY"))
ENUM_BUSY = SimpleNamespace(status=ReadyStatus("BUSY"))


class EligibilityTests(unittest.TestCase):
    """资格维（池成员 ∧ 健康就绪）——点 (1)(2)(3)。"""

    def test_empty_pool_honest_empty_selection(self):  # (1)
        plan = route_composition([_seat()], ())
        self.assertEqual(plan.selections, ())
        self.assertIn(EMPTY_CANDIDATE_POOL, plan.notes)

    def test_empty_pool_no_seats_no_empty_note(self):  # (1) 边界
        plan = route_composition([], ())
        self.assertEqual(plan.selections, ())
        self.assertNotIn(EMPTY_CANDIDATE_POOL, plan.notes)

    def test_single_candidate_selected(self):  # (2)
        plan = route_composition([_seat()], _pool(("rt-a", "prov-a")))
        self.assertEqual(len(plan.selections), 1)
        selection = plan.selections[0]
        self.assertEqual(selection.runtime_id, "rt-a")
        self.assertEqual(selection.provider_id, "prov-a")
        self.assertEqual(selection.reason, STABLE_ORDER)

    def test_multiple_candidates_stable_order_picks_canonical(self):  # (3)
        plan = route_composition(
            [_seat()], _pool(("rt-b", None), ("rt-a", None)))
        self.assertEqual(plan.selections[0].runtime_id, "rt-a")

    def test_no_health_witness_note_when_health_absent(self):
        plan = route_composition([_seat()], _pool(("rt-a", None)))
        self.assertIn(NO_HEALTH_WITNESS, plan.notes)

    def test_health_ready_object_passes(self):
        plan = route_composition(
            [_seat()], _pool(("rt-a", None)), health={"rt-a": READY})
        self.assertEqual(plan.selections[0].runtime_id, "rt-a")
        self.assertNotIn(NO_HEALTH_WITNESS, plan.notes)

    def test_health_enum_shaped_ready_passes(self):
        plan = route_composition(
            [_seat()], _pool(("rt-a", None)), health={"rt-a": ENUM_READY})
        self.assertEqual(len(plan.selections), 1)

    def test_health_non_ready_excluded(self):
        plan = route_composition(
            [_seat()], _pool(("rt-a", None), ("rt-b", None)),
            health={"rt-a": BUSY, "rt-b": READY})
        self.assertEqual(plan.selections[0].runtime_id, "rt-b")

    def test_health_enum_shaped_busy_excluded(self):
        plan = route_composition(
            [_seat()], _pool(("rt-a", None)),
            health={"rt-a": ENUM_BUSY})
        self.assertEqual(plan.selections, ())
        self.assertIn(EMPTY_CANDIDATE_POOL, plan.notes)

    def test_health_absent_entry_excluded(self):
        plan = route_composition(
            [_seat()], _pool(("rt-a", None), ("rt-b", None)),
            health={"rt-b": READY})
        self.assertEqual(plan.selections[0].runtime_id, "rt-b")

    def test_health_non_mapping_rejected(self):
        with self.assertRaises(RouteModelError):
            route_composition([_seat()], _pool(("rt-a", None)),
                              health=[("rt-a", READY)])

    def test_same_runtime_across_seats_legal(self):
        plan = route_composition(
            [_seat("m1", "architect"), _seat("m2", "coder")],
            _pool(("rt-a", None)))
        self.assertEqual(
            [s.runtime_id for s in plan.selections], ["rt-a", "rt-a"])


class DeterminismTests(unittest.TestCase):
    """确定性律——点 (4)(5)(17)。"""

    def test_replay_produces_identical_plan(self):  # (4)
        pool = _pool(("rt-a", "p-a"), ("rt-b", "p-b"))
        seats = [_seat("m1", "coder"), _seat("m2", "reviewer")]
        usage = CostFactView(entries=(
            _facts("rt-a", count=CostMeasure.known(3)),))
        policy = RoutingPolicy(cost_dimensions=(COUNT,))
        first = route_composition(seats, pool, usage=usage, policy=policy)
        second = route_composition(seats, pool, usage=usage, policy=policy)
        self.assertEqual(first, second)
        self.assertEqual(first.identity, second.identity)

    def test_shuffled_pool_input_pins_output(self):  # (5) 乱序输入钉输出序
        forward = route_composition(
            [_seat()], _pool(("rt-a", None), ("rt-b", None), ("rt-c", None)))
        reverse = route_composition(
            [_seat()], _pool(("rt-c", None), ("rt-b", None), ("rt-a", None)))
        self.assertEqual(
            forward.selections[0].runtime_id, "rt-a")
        self.assertEqual(
            reverse.selections[0].runtime_id, "rt-a")

    def test_tie_break_is_canonical_runtime_order(self):  # (5)
        usage = CostFactView(entries=(
            _facts("rt-b", count=CostMeasure.known(5)),
            _facts("rt-a", count=CostMeasure.known(5)),
            _facts("rt-c", count=CostMeasure.known(5))))
        plan = route_composition(
            [_seat()], _pool(("rt-b", None), ("rt-a", None), ("rt-c", None)),
            usage=usage, policy=RoutingPolicy(cost_dimensions=(COUNT,)))
        self.assertEqual(plan.selections[0].runtime_id, "rt-a")

    def test_usage_view_entry_order_irrelevant(self):
        view_a = CostFactView(entries=(
            _facts("rt-a", count=CostMeasure.known(9)),
            _facts("rt-b", count=CostMeasure.known(1))))
        view_b = CostFactView(entries=(
            _facts("rt-b", count=CostMeasure.known(1)),
            _facts("rt-a", count=CostMeasure.known(9))))
        pool = _pool(("rt-a", None), ("rt-b", None))
        plan_a = route_composition(
            [_seat()], pool, usage=view_a,
            policy=RoutingPolicy(cost_dimensions=(COUNT,)))
        plan_b = route_composition(
            [_seat()], pool, usage=view_b,
            policy=RoutingPolicy(cost_dimensions=(COUNT,)))
        self.assertEqual(plan_a, plan_b)


class CostDimensionTests(unittest.TestCase):
    """成本维（政策旋钮；仅 KNOWN 参与排序）——点 (6)-(14)。"""

    def test_usage_evidence_reorders_selection(self):  # (6)
        usage = CostFactView(entries=(
            _facts("rt-a", count=CostMeasure.known(7)),
            _facts("rt-b", count=CostMeasure.known(2))))
        plan = route_composition(
            [_seat()], _pool(("rt-a", None), ("rt-b", None)),
            usage=usage, policy=RoutingPolicy(cost_dimensions=(COUNT,)))
        self.assertEqual(plan.selections[0].runtime_id, "rt-b")
        self.assertEqual(plan.selections[0].reason, COST_ORDERED)

    def test_invocation_count_dimension(self):  # (7)
        usage = CostFactView.from_usage_records(
            [_rec("rt-a") for _ in range(3)] + [_rec("rt-b")])
        plan = route_composition(
            [_seat()], _pool(("rt-a", None), ("rt-b", None)),
            usage=usage, policy=RoutingPolicy(cost_dimensions=(COUNT,)))
        self.assertEqual(plan.selections[0].runtime_id, "rt-b")

    def test_latency_dimension_picks_lower_mean(self):  # (8)
        usage = CostFactView(entries=(
            _facts("rt-a", latency=CostMeasure.known(150)),
            _facts("rt-b", latency=CostMeasure.known(80))))
        plan = route_composition(
            [_seat()], _pool(("rt-a", None), ("rt-b", None)),
            usage=usage, policy=RoutingPolicy(cost_dimensions=(LATENCY,)))
        self.assertEqual(plan.selections[0].runtime_id, "rt-b")

    def test_token_dimension_orders_known_sums(self):  # (9) KNOWN 面
        usage = CostFactView(entries=(
            _facts("rt-a", tokens=CostMeasure.known(500)),
            _facts("rt-b", tokens=CostMeasure.known(90))))
        plan = route_composition(
            [_seat()], _pool(("rt-a", None), ("rt-b", None)),
            usage=usage, policy=RoutingPolicy(cost_dimensions=(TOKENS,)))
        self.assertEqual(plan.selections[0].runtime_id, "rt-b")

    def test_token_unknown_retained_not_excluded(self):  # (9)(11)
        usage = CostFactView(entries=(
            _facts("rt-b", tokens=CostMeasure.known(90)),))
        plan = route_composition(
            [_seat()], _pool(("rt-a", None), ("rt-b", None)),
            usage=usage, policy=RoutingPolicy(cost_dimensions=(TOKENS,)))
        self.assertEqual(plan.selections[0].runtime_id, "rt-b")
        plan_swapped = route_composition(
            [_seat()], _pool(("rt-b", None), ("rt-a", None)),
            usage=usage, policy=RoutingPolicy(cost_dimensions=(TOKENS,)))
        self.assertEqual(plan_swapped.selections[0].runtime_id, "rt-b")

    def test_unknown_only_candidate_still_selectable(self):  # (11)
        plan = route_composition(
            [_seat()], _pool(("rt-a", None)),
            usage=CostFactView(entries=()),
            policy=RoutingPolicy(cost_dimensions=(COUNT,)))
        self.assertEqual(plan.selections[0].runtime_id, "rt-a")
        self.assertEqual(
            plan.selections[0].reason, COST_UNKNOWN_RETAINED)

    def test_monetary_always_unsupported(self):  # (10)
        usage = CostFactView.from_usage_records(
            [_rec("rt-a"), _rec("rt-a")])
        self.assertEqual(
            usage.measure("rt-a", MONEY).status, S)
        self.assertIsNone(usage.measure("rt-a", MONEY).value)

    def test_monetary_dimension_waived_not_crashing(self):  # (10)(13)
        usage = CostFactView.from_usage_records(
            [_rec("rt-a"), _rec("rt-b")])
        plan = route_composition(
            [_seat()], _pool(("rt-a", None), ("rt-b", None)),
            usage=usage, policy=RoutingPolicy(cost_dimensions=(MONEY,)))
        self.assertEqual(plan.selections[0].runtime_id, "rt-a")
        self.assertEqual(
            plan.selections[0].reason, COST_UNSUPPORTED_DIMENSION_OFF)

    def test_unsupported_token_dimension_reason(self):  # (13)
        usage = CostFactView.from_usage_records(
            [_rec("rt-a", usage_status="UNSUPPORTED"),
             _rec("rt-b", usage_status="UNSUPPORTED")])
        self.assertEqual(usage.measure("rt-a", TOKENS).status, S)
        plan = route_composition(
            [_seat()], _pool(("rt-a", None), ("rt-b", None)),
            usage=usage, policy=RoutingPolicy(cost_dimensions=(TOKENS,)))
        self.assertEqual(
            plan.selections[0].reason, COST_UNSUPPORTED_DIMENSION_OFF)

    def test_all_unknown_degrades_to_stable_order(self):  # (14)
        plan = route_composition(
            [_seat()], _pool(("rt-b", None), ("rt-a", None)),
            usage=CostFactView(entries=()),
            policy=RoutingPolicy(cost_dimensions=(COUNT, LATENCY)))
        self.assertEqual(plan.selections[0].runtime_id, "rt-a")

    def test_dimensions_off_pure_stable_order(self):  # (14)
        plan = route_composition(
            [_seat()], _pool(("rt-b", None), ("rt-a", None)))
        self.assertEqual(plan.selections[0].runtime_id, "rt-a")
        self.assertEqual(plan.selections[0].reason, STABLE_ORDER)

    def test_lexicographic_first_dimension_decides(self):
        usage = CostFactView(entries=(
            _facts("rt-a", count=CostMeasure.known(1),
                   latency=CostMeasure.known(900)),
            _facts("rt-b", count=CostMeasure.known(2),
                   latency=CostMeasure.known(10))))
        plan = route_composition(
            [_seat()], _pool(("rt-a", None), ("rt-b", None)),
            usage=usage,
            policy=RoutingPolicy(cost_dimensions=(COUNT, LATENCY)))
        self.assertEqual(plan.selections[0].runtime_id, "rt-a")
        plan = route_composition(
            [_seat()], _pool(("rt-a", None), ("rt-b", None)),
            usage=usage,
            policy=RoutingPolicy(cost_dimensions=(LATENCY, COUNT)))
        self.assertEqual(plan.selections[0].runtime_id, "rt-b")

    def test_cost_report_lists_selected_measures(self):  # (6) 报告面
        usage = CostFactView(entries=(
            _facts("rt-a", count=CostMeasure.known(4)),
            _facts("rt-b", count=CostMeasure.known(1))))
        plan = route_composition(
            [_seat()], _pool(("rt-a", None), ("rt-b", None)),
            usage=usage, policy=RoutingPolicy(cost_dimensions=(COUNT,)))
        report = dict(plan.cost_report)
        self.assertEqual(set(report), {"rt-b"})
        dimension, measure = report["rt-b"][0]
        self.assertIs(dimension, COUNT)
        self.assertEqual(measure.status, K)
        self.assertEqual(measure.value, 1)

    def test_require_known_cost_discloses_without_excluding(self):  # (12)
        policy = RoutingPolicy(
            cost_dimensions=(COUNT,), require_known_cost=True)
        plan = route_composition(
            [_seat()], _pool(("rt-a", None), ("rt-b", None)),
            usage=CostFactView(entries=()), policy=policy)
        self.assertIn(COST_EVIDENCE_REQUIRED_UNSATISFIED, plan.notes)
        self.assertEqual(len(plan.selections), 1)
        self.assertEqual(plan.selections[0].runtime_id, "rt-a")

    def test_require_known_cost_satisfied_no_note(self):  # (12)
        usage = CostFactView(entries=(
            _facts("rt-a", count=CostMeasure.known(1)),))
        policy = RoutingPolicy(
            cost_dimensions=(COUNT,), require_known_cost=True)
        plan = route_composition(
            [_seat()], _pool(("rt-a", None)), usage=usage, policy=policy)
        self.assertNotIn(COST_EVIDENCE_REQUIRED_UNSATISFIED, plan.notes)


class FingerprintTests(unittest.TestCase):
    """身份与指纹（内容寻址 derived）——点 (15)(16)(17)(19)。"""

    FINGERPRINT = re.compile(r"^[0-9a-z]+_[0-9a-f]{12}$")

    def test_policy_fingerprint_format(self):  # (15)
        self.assertRegex(
            RoutingPolicy().fingerprint, r"^route_[0-9a-f]{12}$")
        self.assertRegex(
            RoutingPolicy(cost_dimensions=(COUNT,)).fingerprint,
            r"^route_[0-9a-f]{12}$")

    def test_policy_fingerprint_deterministic_and_sensitive(self):  # (15)
        self.assertEqual(
            RoutingPolicy(cost_dimensions=(COUNT,)).fingerprint,
            RoutingPolicy(cost_dimensions=(COUNT,)).fingerprint)
        self.assertNotEqual(
            RoutingPolicy(cost_dimensions=(COUNT,)).fingerprint,
            RoutingPolicy(cost_dimensions=(LATENCY,)).fingerprint)
        self.assertNotEqual(
            RoutingPolicy(cost_dimensions=(COUNT, LATENCY)).fingerprint,
            RoutingPolicy(cost_dimensions=(LATENCY, COUNT)).fingerprint)
        self.assertNotEqual(
            RoutingPolicy(require_known_cost=False).fingerprint,
            RoutingPolicy(require_known_cost=True).fingerprint)

    def test_composition_fingerprint_format(self):  # (16)
        plan = route_composition(
            [_seat("m1", "coder")], _pool(("rt-a", None)))
        self.assertRegex(
            plan.composition_fingerprint, r"^composition_[0-9a-f]{12}$")

    def test_composition_fingerprint_sensitive_to_shape(self):  # (16)
        pool = _pool(("rt-a", None))
        base = route_composition([_seat("m1", "coder")], pool)
        changed_role = route_composition([_seat("m1", "reviewer")], pool)
        changed_member = route_composition([_seat("m2", "coder")], pool)
        added_seat = route_composition(
            [_seat("m1", "coder"), _seat("m2", "coder")], pool)
        self.assertNotEqual(
            base.composition_fingerprint, changed_role.composition_fingerprint)
        self.assertNotEqual(
            base.composition_fingerprint,
            changed_member.composition_fingerprint)
        self.assertNotEqual(
            base.composition_fingerprint, added_seat.composition_fingerprint)

    def test_composition_fingerprint_pool_insensitive(self):
        """组合指纹=输入组合形状（席位），与池/证据无关（职责正交）。"""
        plan_a = route_composition(
            [_seat()], _pool(("rt-a", None)))
        plan_b = route_composition(
            [_seat()], _pool(("rt-b", None), ("rt-c", None)))
        self.assertEqual(
            plan_a.composition_fingerprint,
            plan_b.composition_fingerprint)

    def test_plan_identity_shape(self):  # (17)
        plan = route_composition(
            [_seat()], _pool(("rt-a", None)),
            policy=RoutingPolicy(cost_dimensions=(COUNT,)))
        self.assertEqual(plan.identity, (
            plan.composition_fingerprint, plan.policy_fingerprint))
        self.assertEqual(
            plan.policy_fingerprint,
            RoutingPolicy(cost_dimensions=(COUNT,)).fingerprint)

    def test_plan_identity_tracks_policy_and_composition(self):  # (17)
        pool = _pool(("rt-a", None))
        seats = [_seat()]
        base = route_composition(seats, pool)
        other_policy = route_composition(
            seats, pool, policy=RoutingPolicy(cost_dimensions=(COUNT,)))
        other_seats = route_composition([_seat("m9")], pool)
        self.assertNotEqual(base.identity, other_policy.identity)
        self.assertNotEqual(base.identity, other_seats.identity)

    def test_no_identity_minting_on_plan(self):  # (19)
        """计划零身份铸造：唯一身份=双指纹（derived 散列）。"""
        plan = route_composition([_seat()], _pool(("rt-a", None)))
        names = {f.name for f in fields(plan)}
        self.assertEqual(names, {
            "composition_fingerprint", "policy_fingerprint",
            "selections", "cost_report", "notes"})
        for value in plan.identity:
            self.assertRegex(value, self.FINGERPRINT)


class ImmutabilityTests(unittest.TestCase):
    """不可变产物——点 (18)。"""

    def test_plan_frozen(self):  # (18)
        plan = route_composition([_seat()], _pool(("rt-a", None)))
        with self.assertRaises(FrozenInstanceError):
            plan.selections = ()

    def test_selection_frozen(self):
        plan = route_composition([_seat()], _pool(("rt-a", None)))
        with self.assertRaises(FrozenInstanceError):
            plan.selections[0].runtime_id = "rt-z"

    def test_value_objects_frozen(self):
        with self.assertRaises(FrozenInstanceError):
            CostMeasure.known(1).value = 2
        with self.assertRaises(FrozenInstanceError):
            DispatchSeat("m1", "coder").role = "x"
        with self.assertRaises(FrozenInstanceError):
            RoutingPolicy().require_known_cost = True

    def test_plan_hashable_and_dedupable(self):
        pool = _pool(("rt-a", None))
        plan = route_composition([_seat()], pool)
        twin = route_composition([_seat()], pool)
        self.assertEqual(len({plan, twin}), 1)


class RoleConsumptionTests(unittest.TestCase):
    """角色域结果被消费而非重建——点 (27)。"""

    def test_role_passed_through_verbatim(self):  # (27)
        for role in ("coder", "ARCHITECT-PRIME", "审阅者", "a b c"):
            plan = route_composition(
                [_seat("m1", role)], _pool(("rt-a", None)))
            self.assertEqual(plan.selections[0].role, role)

    def test_seat_order_preserved_in_selections(self):  # (27)
        seats = [_seat("m1", "architect"), _seat("m2", "coder"),
                 _seat("m3", "reviewer")]
        plan = route_composition(seats, _pool(("rt-a", None)))
        self.assertEqual(
            [s.member_id for s in plan.selections], ["m1", "m2", "m3"])
        self.assertEqual(
            [s.role for s in plan.selections],
            ["architect", "coder", "reviewer"])

    def test_no_role_vocabulary_judgment(self):  # (27)
        """角色词表零裁决：任意非空串皆可（构造期只查非空）。"""
        plan = route_composition(
            [_seat("m1", "not-a-known-role-word")], _pool(("rt-a", None)))
        self.assertEqual(
            plan.selections[0].role, "not-a-known-role-word")


class AggregationTests(unittest.TestCase):
    """用量记录鸭 → 事实视图聚合（确定性；防御性降级）。"""

    def test_counts_known(self):
        view = CostFactView.from_usage_records(
            [_rec("rt-a"), _rec("rt-a"), _rec("rt-b")])
        self.assertEqual(view.measure("rt-a", COUNT).status, K)
        self.assertEqual(view.measure("rt-a", COUNT).value, 2)
        self.assertEqual(view.measure("rt-b", COUNT).value, 1)

    def test_latency_integer_mean(self):
        view = CostFactView.from_usage_records(
            [_rec("rt-a", duration_ms=100), _rec("rt-a", duration_ms=201)])
        self.assertEqual(view.measure("rt-a", LATENCY).value, 150)

    def test_latency_unknown_without_measurements(self):
        view = CostFactView.from_usage_records(
            [_rec("rt-a", duration_ms=None)])
        self.assertEqual(view.measure("rt-a", LATENCY).status, U)

    def test_token_known_sums_in_and_out(self):
        view = CostFactView.from_usage_records(
            [_rec("rt-a", input_tokens=10, output_tokens=5),
             _rec("rt-a", input_tokens=1, output_tokens=2)])
        self.assertEqual(view.measure("rt-a", TOKENS).value, 18)

    def test_token_unknown_when_no_known_status(self):
        view = CostFactView.from_usage_records(
            [_rec("rt-a", usage_status="UNKNOWN")])
        self.assertEqual(view.measure("rt-a", TOKENS).status, U)

    def test_token_unsupported_when_declared(self):
        view = CostFactView.from_usage_records(
            [_rec("rt-a", usage_status="UNSUPPORTED")])
        self.assertEqual(view.measure("rt-a", TOKENS).status, S)

    def test_token_known_wins_over_unsupported_mix(self):
        view = CostFactView.from_usage_records(
            [_rec("rt-a", usage_status="UNSUPPORTED"),
             _rec("rt-a", usage_status="KNOWN", input_tokens=7,
                  output_tokens=3)])
        self.assertEqual(view.measure("rt-a", TOKENS).status, K)
        self.assertEqual(view.measure("rt-a", TOKENS).value, 10)

    def test_absent_runtime_measure_unknown_never_zero(self):
        view = CostFactView.from_usage_records([_rec("rt-a")])
        self.assertEqual(view.measure("rt-zzz", COUNT).status, U)
        self.assertIsNone(view.measure("rt-zzz", COUNT).value)

    def test_entries_first_appearance_order(self):
        view = CostFactView.from_usage_records(
            [_rec("rt-b"), _rec("rt-a"), _rec("rt-b")])
        self.assertEqual(
            tuple(e.runtime_id for e in view.entries), ("rt-b", "rt-a"))

    def test_record_without_honest_identity_skipped(self):
        view = CostFactView.from_usage_records(
            [SimpleNamespace(runtime_id=None), _rec("rt-a")])
        self.assertEqual(len(view.entries), 1)

    def test_empty_records_empty_view(self):
        self.assertEqual(CostFactView.from_usage_records(()).entries, ())

    def test_manual_construction_is_future_provider_surface(self):
        view = CostFactView(entries=(
            _facts("rt-a", money=CostMeasure.unsupported()),))
        self.assertEqual(view.measure("rt-a", MONEY).status, S)


class ValidationTests(unittest.TestCase):
    """封闭契约拒绝（INVALID 惯用法；message 不含被拒值）。"""

    def test_cost_measure_known_requires_value(self):
        with self.assertRaises(RouteModelError):
            CostMeasure(status=K)
        with self.assertRaises(RouteModelError):
            CostMeasure(status=K, value=-1)
        with self.assertRaises(RouteModelError):
            CostMeasure(status=K, value=True)
        with self.assertRaises(RouteModelError):
            CostMeasure(status=K, value="3")

    def test_cost_measure_non_known_forbids_value(self):
        with self.assertRaises(RouteModelError):
            CostMeasure(status=U, value=0)
        with self.assertRaises(RouteModelError):
            CostMeasure(status=S, value=1)

    def test_cost_measure_bad_status(self):
        with self.assertRaises(RouteModelError):
            CostMeasure(status="KNOWN")

    def test_runtime_cost_facts_bad_shapes(self):
        with self.assertRaises(RouteModelError):
            RuntimeCostFacts(
                runtime_id="", invocation_count=CostMeasure.unknown(),
                mean_latency_ms=CostMeasure.unknown(),
                token_total=CostMeasure.unknown(),
                monetary=CostMeasure.unsupported())
        with self.assertRaises(RouteModelError):
            RuntimeCostFacts(
                runtime_id="rt-a", invocation_count=None,
                mean_latency_ms=CostMeasure.unknown(),
                token_total=CostMeasure.unknown(),
                monetary=CostMeasure.unsupported())

    def test_cost_fact_view_entries_must_be_tuple(self):
        with self.assertRaises(RouteModelError):
            CostFactView(entries=[])

    def test_routing_policy_bad_shapes(self):
        with self.assertRaises(RouteModelError):
            RoutingPolicy(cost_dimensions=[COUNT])
        with self.assertRaises(RouteModelError):
            RoutingPolicy(cost_dimensions=("COUNT",))
        with self.assertRaises(RouteModelError):
            RoutingPolicy(require_known_cost="yes")

    def test_dispatch_seat_requires_non_empty(self):
        with self.assertRaises(RouteModelError):
            DispatchSeat(member_id="", role="coder")
        with self.assertRaises(RouteModelError):
            DispatchSeat(member_id="m1", role=" ")

    def test_dispatch_selection_closed_reason(self):
        with self.assertRaises(RouteModelError):
            DispatchSelection(
                member_id="m1", role="coder", runtime_id="rt-a",
                provider_id=None, reason="BEST_AGENT")

    def test_route_rejects_non_seat(self):
        with self.assertRaises(RouteModelError):
            route_composition([SimpleNamespace(member_id="m")], ())

    def test_route_rejects_duplicate_member(self):
        with self.assertRaises(RouteModelError):
            route_composition(
                [_seat("m1"), _seat("m1")], _pool(("rt-a", None)))

    def test_route_rejects_bad_policy(self):
        with self.assertRaises(RouteModelError):
            route_composition([_seat()], (), policy=SimpleNamespace())

    def test_route_rejects_bad_usage_shape(self):  # (23) Memory 域形状拒绝
        with self.assertRaises(RouteModelError):
            route_composition(
                [_seat()], _pool(("rt-a", None)),
                usage=SimpleNamespace(entries=()))

    def test_route_rejects_pool_entry_without_runtime(self):  # (22) 快照形状拒绝
        with self.assertRaises(RouteModelError):
            route_composition(
                [_seat()],
                (SimpleNamespace(task_id="t", step_index=0, items=()),))

    def test_route_rejects_bad_provider_shape(self):
        with self.assertRaises(RouteModelError):
            route_composition(
                [_seat()],
                (SimpleNamespace(runtime_id="rt-a", provider_id=3),))

    def test_memory_record_shape_rejected(self):  # (23)
        record = SimpleNamespace(
            record_id="mem-1", scope="USER", content="past lesson")
        with self.assertRaises(RouteModelError):
            route_composition([_seat()], (record,))


class BoundaryContractTests(unittest.TestCase):
    """域边界行为面（零接线证明的行为半边）。"""

    def test_no_execution_lifecycle_fields(self):  # (28)
        """不复制既有执行计划语义：DispatchPlan 零执行身份/重试/回退/
        会话/编译/传输字段（字段封闭于五值）。"""
        plan = route_composition([_seat()], _pool(("rt-a", None)))
        names = {f.name for f in fields(plan)}
        for banned in ("execution_id", "invocation_id", "slot_id",
                       "retry", "retries", "fallback", "session_id",
                       "segments", "prompt", "request"):
            self.assertNotIn(banned, names)

    def test_selection_fields_closed(self):  # (28)
        selection = DispatchSelection(
            member_id="m", role="coder", runtime_id="rt-a",
            provider_id=None, reason=STABLE_ORDER)
        self.assertEqual(
            {f.name for f in fields(selection)},
            {"member_id", "role", "runtime_id", "provider_id", "reason"})

    def test_closed_selection_vocabulary(self):  # (25)
        observed = {
            STABLE_ORDER, COST_ORDERED, COST_UNKNOWN_RETAINED,
            COST_UNSUPPORTED_DIMENSION_OFF}
        plan_pool = _pool(("rt-a", None), ("rt-b", None))
        plans = (
            route_composition([_seat()], plan_pool),
            route_composition(
                [_seat()], plan_pool,
                policy=RoutingPolicy(cost_dimensions=(COUNT,))),
            route_composition(
                [_seat()], plan_pool,
                usage=CostFactView(entries=(
                    _facts("rt-a", tokens=CostMeasure.unsupported()),)),
                policy=RoutingPolicy(cost_dimensions=(TOKENS,))))
        reasons = {
            s.reason for plan in plans for s in plan.selections}
        self.assertTrue(reasons)
        self.assertTrue(reasons <= observed)

    def test_closed_plan_note_vocabulary(self):  # (25)
        observed = {
            EMPTY_CANDIDATE_POOL, NO_HEALTH_WITNESS,
            COST_EVIDENCE_REQUIRED_UNSATISFIED}
        plans = (
            route_composition([_seat()], ()),
            route_composition([_seat()], _pool(("rt-a", None))),
            route_composition(
                [_seat()], _pool(("rt-a", None)),
                policy=RoutingPolicy(
                    cost_dimensions=(COUNT,), require_known_cost=True)))
        notes = {n for plan in plans for n in plan.notes}
        self.assertTrue(notes)
        self.assertTrue(notes <= observed)


class StaticWallTests(unittest.TestCase):
    """静态墙（模块源面）——点 (19)-(26) 与设计负向。"""

    def _assert_not_regex(self, pattern, label):
        self.assertIsNone(
            re.search(pattern, MODULE_SOURCE), f"banned token: {label}")

    def test_import_face_closed(self):  # §十五
        tree = ast.parse(MODULE_SOURCE)
        faces = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                faces.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                faces.add(node.module)
        self.assertEqual(faces, {"dataclasses", "enum", "hashlib", "json"})

    def test_all_face_closed(self):
        self.assertEqual(set(cockpit_route.__all__), {
            "RouteModelError", "CostStatus", "CostDimension", "CostMeasure",
            "RuntimeCostFacts", "CostFactView", "RoutingPolicy",
            "DispatchSeat", "DispatchSelection", "DispatchPlan",
            "route_composition"})
        for name in cockpit_route.__all__:
            self.assertTrue(hasattr(cockpit_route, name))

    def test_no_identity_minting_tokens(self):  # (19)
        self._assert_not_regex(r"\buuid", "uuid")
        self._assert_not_regex(r"\buuid4\b", "uuid4")

    def test_no_random_or_clock_tokens(self):  # (26)
        self._assert_not_regex(r"\brandom\b", "random")
        self._assert_not_regex(r"\btime\b", "bare time")
        self._assert_not_regex(r"\bmonotonic\b", "monotonic")
        self._assert_not_regex(r"\bdatetime\b", "datetime")
        self._assert_not_regex(r"\bperf_counter\b", "perf_counter")

    def test_no_runtime_invocation_tokens(self):  # (20)
        self._assert_not_regex(r"\.invoke\(", ".invoke(")
        self._assert_not_regex(r"\bsubprocess\b", "subprocess")
        self._assert_not_regex(r"\bsocket\b", "socket")
        self._assert_not_regex(r"\brequests\b", "requests")
        self._assert_not_regex(r"\burllib\b", "urllib")

    def test_no_provider_names(self):  # (21)
        for word in ("claude", "codex", "gemini", "qwen", "opencode",
                     "cline", "tiny", "anthropic", "openai"):
            self._assert_not_regex(
                rf"\b{word}\b", word)

    def test_no_compiler_or_memory_literals(self):  # (22)(23)
        for literal in ("cockpit_compile", "cockpit_memory",
                        "cockpit_context", "compile_context"):
            self.assertNotIn(literal, MODULE_SOURCE)

    def test_no_sibling_engine_literals(self):  # 设计负向
        for literal in ("invocation_plan", "role_assignment", "usage_log",
                        "event_index", "execution_slots",
                        "sequential_pipeline", "cockpit_tui",
                        "cockpit_entry", "EventIndex", "ControlJournal",
                        "ExecutionEvent"):
            self.assertNotIn(literal, MODULE_SOURCE)

    def test_no_persistence_tokens(self):  # (24)
        self._assert_not_regex(r"\bopen\(", "open(")
        for literal in ("sqlite", "pathlib", "tempfile", "pickle",
                        "shelve", "write_text", "write_bytes"):
            self.assertNotIn(literal, MODULE_SOURCE)

    def test_no_hidden_ranking_tokens(self):  # (25)
        self._assert_not_regex(r"\bscore\b", "score")
        self._assert_not_regex(r"\branking\b", "ranking")
        self._assert_not_regex(r"\bweight\b", "weight")

    def test_no_llm_token(self):  # §八
        self.assertNotIn("llm", MODULE_SOURCE)

    def test_no_ui_token(self):  # §十五
        self._assert_not_regex(r"\btextual\b", "textual")
        self._assert_not_regex(r"\bwidget\b", "widget")

    def test_no_append_calls(self):
        self.assertNotIn(".append(", MODULE_SOURCE)

    def test_json_dumps_exactly_once(self):
        self.assertEqual(MODULE_SOURCE.count("json.dumps"), 1)

    def test_fresh_import_loads_no_sibling_modules(self):  # (22)(23)
        """全新解释器只 import cockpit_route：零兄弟域模块、零 UI 框架
        被装载（import 面的行为证明）。"""
        code = (
            "import sys; import cockpit_route; "
            "mods = sorted(m for m in sys.modules if m.startswith('cockpit')); "
            "print(mods); "
            "print('textual' in sys.modules, "
            "'cockpit_compile' in sys.modules, "
            "'cockpit_memory' in sys.modules, "
            "'cockpit_context' in sys.modules)")
        proc = subprocess.run(
            [sys.executable, "-c", code], cwd=str(SCRIPTS),
            capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        lines = proc.stdout.strip().splitlines()
        self.assertEqual(lines[0], "['cockpit_route']")
        self.assertEqual(lines[1], "False False False False")


if __name__ == "__main__":
    unittest.main()
