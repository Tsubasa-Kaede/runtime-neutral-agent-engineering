"""ORCH-5 Integration（Architecture A：entry 直连）集成测试。

覆盖授权 §二十 三层矩阵：
- OFF regression：OFF == 既有行为（默认组合/sorted distinct 序/
  M2 等值/monkeypatch 证 OFF 永不触碰 Router）。
- ON integration：无证据 ≡ 旧行为；KNOWN 调用数可改变默认指派；
  UNKNOWN token 保持 UNKNOWN；UNSUPPORTED 货币被忽略；显式组合
  绕过 Router；角色保形；VERIFIED 池边界；既有门（INVALID_TASK/
  披露全等门）保持；桥转译；确定性重放；preview/start 一致性；
  会话二轮 usage 改变默认选择；畸形候选拒绝；无候选保持 BLOCKED。
- 静态边界：零 provider/adapter/Compiler/Memory 新依赖；零持久化
  词；激活位全仓唯一且显式（ORCH-5-ACT Option A2：use_route_
  default=True 恰一处、位于 _run_first_run_funnel；signature 缺省
  False 保留为桥级保守缺省）。

夹具沿 M2 池契约测试惯例：真 AdapterRegistry/AdapterDescriptor（池
路径零 factory 调用）+ 直接构造 evidence dict（离线数据对象）。
REAL=0：零 runtime 调用、零 subprocess、零网络、零 credentials；
runtime_id 全中性值（rt-*）。
"""
import re
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import cockpit_entry  # noqa: E402
from candidate_validation import (  # noqa: E402
    CandidateValidationResult,
    CandidateValidationStatus,
)
from cockpit_entry import (  # noqa: E402
    CompositionChanged,
    CompositionError,
    DefaultComposition,
    resolve_default_composition,
    routed_default_composition,
)
from cockpit_projection import DEFAULT_ROLE_TEMPLATES  # noqa: E402
from cockpit_route import (  # noqa: E402
    CostDimension,
    RoutingPolicy,
    RouteModelError,
)
from runtime_adapter_registry import (  # noqa: E402
    AdapterDescriptor,
    AdapterRegistry,
)

ENTRY_SOURCE = Path(
    cockpit_entry.__file__).read_text(encoding="utf-8")


def _descriptor(runtime_id, provider_id):
    """真注册单元（池路径零调用 factory——_verified_pool 不触碰）。"""
    return AdapterDescriptor(
        runtime_id=runtime_id,
        provider_id=provider_id,
        runtime_type="neutral",
        display_name=runtime_id,
        adapter_factory=lambda: None,
    )


def _evidence(descriptors):
    """VERIFIED evidence dict（离线数据对象；schema 标记非真实调用）。"""
    return {
        descriptor.identity: CandidateValidationResult(
            identity=descriptor.identity,
            status=CandidateValidationStatus.VERIFIED,
            gates_passed=frozenset(),
            gate_results=(),
            block_reason=None,
            failure_point=None,
            experiment_id="route-integration-fixture",
            executed_at=1.0,
            provenance="REAL",
        )
        for descriptor in descriptors
    }


def _registry(descriptors):
    registry = AdapterRegistry()
    for descriptor in descriptors:
        registry.register(descriptor)
    return registry


def _pool(descriptors):
    return cockpit_entry._verified_pool(
        _registry(descriptors), _evidence(descriptors))


def _rec(runtime_id, usage_status="KNOWN", duration_ms=100,
         input_tokens=10, output_tokens=5):
    return SimpleNamespace(
        runtime_id=runtime_id,
        duration_ms=duration_ms,
        usage_status=usage_status,
        input_tokens=input_tokens,
        output_tokens=output_tokens)


def _fake_run(*records):
    """composed_runs 假 run（usage 闭包形；零引擎对象）。"""
    return SimpleNamespace(usage=lambda: records)


def _runtime_ids(composition):
    return tuple(b.runtime_id for b in composition.bindings)


FIVE = tuple(_descriptor(f"rt-{name}", f"prov-{name}")
             for name in ("alpha", "beta", "gamma", "delta", "epsilon"))
THREE = FIVE[:3]


class OffRegressionTests(unittest.TestCase):
    """OFF regression：OFF == 既有行为（授权 §二十）。"""

    def test_bridge_empty_usage_equals_legacy(self):
        """空证据 ⇒ 桥输出与既有默认组合全等（逐字段 NamedTuple ==）。"""
        for descriptors in (THREE, FIVE):
            pool = _pool(descriptors)
            with self.subTest(size=len(descriptors)):
                self.assertEqual(
                    routed_default_composition(pool),
                    resolve_default_composition(pool))

    def test_shuffled_pool_input_same_output(self):
        pool = _pool(FIVE)
        shuffled = tuple(reversed(pool))
        self.assertEqual(
            routed_default_composition(pool),
            routed_default_composition(shuffled))

    def test_legacy_sorted_distinct_semantics(self):
        """既有语义钉定：sorted(runtime_id) 前 min(4,n) 个 distinct
        （字典序 delta < epsilon < gamma）。"""
        composition = resolve_default_composition(_pool(FIVE))
        self.assertEqual(
            _runtime_ids(composition),
            ("rt-alpha", "rt-beta", "rt-delta", "rt-epsilon"))

    def test_bridge_pool_subset_and_distinct(self):
        composition = routed_default_composition(_pool(FIVE))
        ids = _runtime_ids(composition)
        pool_ids = {d.runtime_id for d in FIVE}
        self.assertEqual(len(ids), min(4, len(FIVE)))
        self.assertEqual(len(set(ids)), len(ids))
        self.assertTrue(set(ids) <= pool_ids)

    def test_blocked_pool_identical(self):
        """无候选 ⇒ 既有 BLOCKED 语义逐字（含 hint）。"""
        for descriptors in ((), FIVE[:1]):
            pool = _pool(descriptors)
            with self.subTest(size=len(descriptors)):
                self.assertEqual(
                    routed_default_composition(pool),
                    resolve_default_composition(pool))
                self.assertIsNotNone(
                    routed_default_composition(pool).blocked_reason)

    def test_unknown_usage_records_still_canonical(self):
        """全 UNKNOWN 记录（无 KNOWN 计数缺席非零）⇒ canonical 序。"""
        records = (_rec("rt-alpha", usage_status="UNKNOWN"),
                   _rec("rt-beta", usage_status="UNKNOWN"))
        self.assertEqual(
            _runtime_ids(routed_default_composition(
                _pool(THREE), records)),
            ("rt-alpha", "rt-beta", "rt-gamma"))

    def test_off_closure_never_calls_router(self):
        """OFF 缺省：Router 被替换为爆炸函数仍正常出默认组合。"""
        surfaces = cockpit_entry._funnel_composition_closures(
            _registry(THREE), None, _evidence(THREE), timeout_seconds=5)
        original = cockpit_entry.route_composition
        def _explode(*args, **kwargs):
            raise AssertionError("router must not be called when OFF")
        cockpit_entry.route_composition = _explode
        try:
            composition = surfaces.preview()
        finally:
            cockpit_entry.route_composition = original
        self.assertEqual(
            _runtime_ids(composition),
            ("rt-alpha", "rt-beta", "rt-gamma"))


class OnRoutingTests(unittest.TestCase):
    """ON integration：Router 的真实默认指派能力（授权 §二十）。"""

    def test_known_invocation_count_reorders_selection(self):
        """KNOWN 调用数 ⇒ 默认指派可不同于 sorted(runtime_id)。"""
        records = (_rec("rt-alpha"), _rec("rt-alpha"), _rec("rt-alpha"),
                   _rec("rt-beta"), _rec("rt-beta"),
                   _rec("rt-gamma"))
        composition = routed_default_composition(_pool(THREE), records)
        self.assertEqual(
            _runtime_ids(composition),
            ("rt-gamma", "rt-beta", "rt-alpha"))

    def test_unknown_runtimes_rank_after_known(self):
        """无记录 runtime（UNKNOWN）排于 KNOWN 之后，保留不排除。"""
        records = (_rec("rt-beta"),)
        composition = routed_default_composition(_pool(THREE), records)
        self.assertEqual(
            _runtime_ids(composition),
            ("rt-beta", "rt-alpha", "rt-gamma"))

    def test_token_policy_unknown_canonical(self):
        """token 维 + 全 UNKNOWN ⇒ canonical（UNKNOWN ≠ 0 不折算）。"""
        records = (_rec("rt-alpha", usage_status="UNKNOWN"),
                   _rec("rt-beta", usage_status="UNKNOWN"))
        policy = RoutingPolicy(cost_dimensions=(CostDimension.TOKEN_TOTAL,))
        self.assertEqual(
            _runtime_ids(routed_default_composition(
                _pool(THREE), records, policy)),
            ("rt-alpha", "rt-beta", "rt-gamma"))

    def test_token_policy_known_reorders(self):
        records = (_rec("rt-gamma", input_tokens=1, output_tokens=1),
                   _rec("rt-alpha", input_tokens=900, output_tokens=90))
        policy = RoutingPolicy(cost_dimensions=(CostDimension.TOKEN_TOTAL,))
        self.assertEqual(
            _runtime_ids(routed_default_composition(
                _pool(THREE), records, policy))[0],
            "rt-gamma")

    def test_monetary_policy_unsupported_ignored(self):
        """货币维结构性 UNSUPPORTED ⇒ 维度弃权，canonical 序。"""
        policy = RoutingPolicy(cost_dimensions=(CostDimension.MONETARY,))
        self.assertEqual(
            routed_default_composition(_pool(THREE), (), policy),
            resolve_default_composition(_pool(THREE)))

    def test_role_template_position_preserved(self):
        """角色保形：恒模板位次（零推断/零改写/零 cost 改角）。"""
        records = (_rec("rt-alpha"), _rec("rt-alpha"), _rec("rt-beta"))
        composition = routed_default_composition(_pool(THREE), records)
        self.assertEqual(
            composition.roles,
            DEFAULT_ROLE_TEMPLATES[3])
        self.assertEqual(
            tuple(b.role for b in composition.bindings),
            DEFAULT_ROLE_TEMPLATES[3])

    def test_identity_passthrough(self):
        composition = routed_default_composition(_pool(THREE))
        by_runtime = {d.runtime_id: d for d in THREE}
        for binding in composition.bindings:
            self.assertEqual(
                binding.canonical_runtime_identity,
                by_runtime[binding.runtime_id].identity)
            self.assertEqual(
                binding.provider_id,
                by_runtime[binding.runtime_id].provider_id)

    def test_bridge_returns_existing_type(self):
        self.assertIsInstance(
            routed_default_composition(_pool(THREE)), DefaultComposition)

    def test_deterministic_replay(self):
        pool = _pool(FIVE)
        records = (_rec("rt-beta"), _rec("rt-gamma"), _rec("rt-gamma"))
        first = routed_default_composition(pool, records)
        second = routed_default_composition(pool, records)
        self.assertEqual(first, second)
        self.assertEqual(first, routed_default_composition(
            tuple(reversed(pool)), tuple(reversed(records))))

    def test_malformed_pool_entry_rejected(self):
        """畸形候选 ⇒ 与 legacy 同型失败（同一失败面零新增吞没）：
        重构后 base=resolve_default_composition 先消费池，坏形状在
        与 legacy 完全相同的表达式处 AttributeError；Router 自身的
        RouteModelError 形状门由其 92 测试钉定（桥路径下 base 先行
        =对池项结构性不可达——比先前直传 Router 更贴单一真源）。"""
        malformed_shapes = (
            SimpleNamespace(),                                     # 无 runtime_id
            SimpleNamespace(runtime_id="rt-bad", provider_id=3),   # 坏 provider
            SimpleNamespace(runtime_id="rt-bad"),                  # 无 identity
        )
        for bad in malformed_shapes:
            pool = _pool(THREE) + (bad,)
            with self.subTest(shape=sorted(vars(bad))):
                with self.assertRaises(AttributeError):
                    routed_default_composition(pool)
                with self.assertRaises(AttributeError):
                    resolve_default_composition(pool)

    def test_explicit_policy_respected(self):
        records = (_rec("rt-beta", duration_ms=900),
                   _rec("rt-gamma", duration_ms=10))
        latency_policy = RoutingPolicy(
            cost_dimensions=(CostDimension.LATENCY,))
        self.assertEqual(
            _runtime_ids(routed_default_composition(
                _pool(THREE), records, latency_policy))[0],
            "rt-gamma")


class ClosureIntegrationTests(unittest.TestCase):
    """闭包级 ON/OFF（默认组合唯一决策点经 _funnel closures）。"""

    @staticmethod
    def _surfaces(descriptors, use_route_default):
        return cockpit_entry._funnel_composition_closures(
            _registry(descriptors), None, _evidence(descriptors),
            timeout_seconds=5, use_route_default=use_route_default)

    def test_on_no_evidence_equals_off(self):
        off = self._surfaces(THREE, False).preview()
        on = self._surfaces(THREE, True).preview()
        self.assertEqual(off, on)
        self.assertEqual(
            _runtime_ids(on), ("rt-alpha", "rt-beta", "rt-gamma"))

    def test_second_run_usage_changes_default_selection(self):
        """会话二轮：前轮 usage 闭包改变默认选择（独占产品价值）。"""
        surfaces = self._surfaces(THREE, True)
        self.assertEqual(
            _runtime_ids(surfaces.preview()),
            ("rt-alpha", "rt-beta", "rt-gamma"))
        surfaces.composed_runs.append(
            _fake_run(_rec("rt-alpha"), _rec("rt-alpha"),
                      _rec("rt-alpha"), _rec("rt-beta"),
                      _rec("rt-beta"), _rec("rt-gamma")))
        self.assertEqual(
            _runtime_ids(surfaces.preview()),
            ("rt-gamma", "rt-beta", "rt-alpha"))

    def test_off_ignores_usage_evidence(self):
        surfaces = self._surfaces(THREE, False)
        surfaces.composed_runs.append(
            _fake_run(_rec("rt-alpha"), _rec("rt-alpha"),
                      _rec("rt-beta")))
        self.assertEqual(
            _runtime_ids(surfaces.preview()),
            ("rt-alpha", "rt-beta", "rt-gamma"))

    def test_preview_start_consistency_gate(self):
        """证据变化 ⇒ 披露全等门诚实执法（CompositionChanged）。"""
        surfaces = self._surfaces(THREE, True)
        expected = surfaces.preview()
        surfaces.composed_runs.append(
            _fake_run(_rec("rt-alpha"), _rec("rt-alpha"),
                      _rec("rt-beta"), _rec("rt-gamma")))
        result = surfaces.start("fresh task", expected)
        self.assertIsInstance(result, CompositionChanged)
        # alpha=2 居首出局；beta=1 与 gamma=1 平局按 runtime_id 决出。
        self.assertEqual(
            _runtime_ids(result.composition),
            ("rt-beta", "rt-gamma", "rt-alpha"))

    def test_on_start_invalid_task_typed_error(self):
        """ON 路径既有 INVALID_TASK 门原样（typed error boundary）。"""
        surfaces = self._surfaces(THREE, True)
        result = surfaces.start("", None)
        self.assertIsInstance(result, CompositionError)
        self.assertEqual(result.reason, "INVALID_TASK")

    def test_preview_consistent_across_empty_evidence_runs(self):
        """空记录 run（零证据变化）⇒ preview 稳定不误触全等门基准。"""
        surfaces = self._surfaces(THREE, True)
        expected = surfaces.preview()
        surfaces.composed_runs.append(_fake_run())
        self.assertEqual(surfaces.preview(), expected)

    def test_no_flag_construction_ignores_usage(self):
        """A2 极性证明：不传旗标构造（embedder/测试形态）= 桥级
        保守缺省 OFF——KNOWN 用量在场仍 canonical 序（激活仅经
        生产调用点显式发生）。"""
        surfaces = cockpit_entry._funnel_composition_closures(
            _registry(THREE), None, _evidence(THREE), timeout_seconds=5)
        surfaces.composed_runs.append(
            _fake_run(_rec("rt-alpha"), _rec("rt-alpha"),
                      _rec("rt-beta")))
        self.assertEqual(
            _runtime_ids(surfaces.preview()),
            ("rt-alpha", "rt-beta", "rt-gamma"))


class ActivationWiringTests(unittest.TestCase):
    """ORCH-5-ACT 激活接线（真实生产调用路径；REAL=0 零渲染零执行）。"""

    def test_funnel_passes_activation_flag(self):
        """生产激活位真实生效：_run_first_run_funnel 构造漏斗闭包时
        显式激活（use_route_default=True）。录制式包装真构造器证
        kwarg 传递；fake TUI 零渲染，仅走装配路径（outcome=None ⇒
        exit 0 前置退出——零事件零执行零交付）。"""
        captured = {}
        real_closures = cockpit_entry._funnel_composition_closures
        real_host = cockpit_entry._host_entry

        def _recording_closures(*args, **kwargs):
            captured.update(kwargs)
            return real_closures(*args, **kwargs)

        def _fake_host():
            return SimpleNamespace(
                environment_registry=lambda factories: (
                    _registry(THREE), ()))

        tui = SimpleNamespace(
            new_event_store=lambda: None,
            run_cockpit_funnel=lambda **kwargs: None)
        intent = SimpleNamespace(task_token="demo", timeout_seconds=None)
        cockpit_entry._funnel_composition_closures = _recording_closures
        cockpit_entry._host_entry = _fake_host
        try:
            exit_code = cockpit_entry._run_first_run_funnel(
                intent, tui, factories=(), evidence=_evidence(THREE),
                base_dir=None, timeout_seconds=5,
                boundary_hook=None, observation_sink=None,
                event_index=object())
        finally:
            cockpit_entry._funnel_composition_closures = real_closures
            cockpit_entry._host_entry = real_host
        self.assertEqual(exit_code, 0)
        self.assertIs(captured.get("use_route_default"), True)


class ExplicitBypassTests(unittest.TestCase):
    """显式用户组合结构性绕过 Router（授权 §十二）。"""

    def test_user_surface_preview_never_routes(self):
        surface = cockpit_entry._user_composition_surface(
            _registry(THREE), None, _evidence(THREE), timeout_seconds=5)
        original = cockpit_entry.route_composition
        def _explode(*args, **kwargs):
            raise AssertionError("explicit path must never route")
        cockpit_entry.route_composition = _explode
        try:
            intent, resolved = surface.preview(
                (("rt-beta", "coder"), ("rt-alpha", "architect")))
        finally:
            cockpit_entry.route_composition = original
        self.assertIsNotNone(intent)
        self.assertEqual(
            tuple(b.runtime_id for b in resolved.bindings),
            ("rt-beta", "rt-alpha"))

    def test_router_references_confined_to_default_path(self):
        """静态：routed_default_composition 的调用面仅漏斗闭包。"""
        entry_source = ENTRY_SOURCE
        body_start = entry_source.index(
            "def _user_composition_surface")
        body_end = entry_source.index("class UserCompositionSurface")
        user_surface_body = entry_source[body_start:body_end]
        self.assertNotIn("routed_default_composition", user_surface_body)
        self.assertNotIn("route_composition", user_surface_body)


class StaticBoundaryTests(unittest.TestCase):
    """静态边界（授权 §二十一）。"""

    def test_no_new_domain_imports(self):
        """entry 新增 import 恰 cockpit_route 双分支各一；零 Compiler/
        Memory/语义层新依赖。"""
        self.assertEqual(ENTRY_SOURCE.count("from cockpit_route import"), 1)
        self.assertEqual(
            ENTRY_SOURCE.count("from .cockpit_route import"), 1)
        for banned in ("cockpit_compile", "cockpit_memory",
                       "cockpit_context"):
            self.assertNotIn(f"import {banned}", ENTRY_SOURCE)

    def test_bridge_segment_no_persistence_or_provider(self):
        start = ENTRY_SOURCE.index("def routed_default_composition")
        end = ENTRY_SOURCE.index("def _is_verified")
        bridge = ENTRY_SOURCE[start:end]
        for banned in ("open(", "sqlite", "tempfile", "pickle", "shelve",
                       "write_text", "subprocess", "socket", "requests"):
            self.assertNotIn(banned, bridge)
        for provider in ("claude", "codex", "gemini", "qwen"):
            self.assertNotIn(provider, bridge)

    def test_single_explicit_activation_site(self):
        """ORCH-5-ACT（Option A2）：激活位全仓唯一且显式——
        use_route_default=True 恰一处并位于 _run_first_run_funnel
        的 source window 内；其他 scripts 文件零旗标（无第二激活
        面）；signature 缺省 False 保留（桥级保守缺省不动）。"""
        repo_root = Path(__file__).resolve().parents[1]
        hits = []
        for path in repo_root.glob("dual-agent-development/scripts/*.py"):
            text = path.read_text(encoding="utf-8")
            if "use_route_default" in text and path.name != "cockpit_entry.py":
                hits.append(path.name)
        self.assertEqual(hits, [])
        self.assertEqual(ENTRY_SOURCE.count("use_route_default=True"), 1)
        window_start = ENTRY_SOURCE.index("def _run_first_run_funnel")
        window_end = ENTRY_SOURCE.index("\ndef ", window_start)
        self.assertIn(
            "use_route_default=True",
            ENTRY_SOURCE[window_start:window_end])
        self.assertIn("use_route_default=False", ENTRY_SOURCE)

    def test_flag_defaults_off_in_signature(self):
        self.assertIn(
            "use_route_default=False", ENTRY_SOURCE)


if __name__ == "__main__":
    unittest.main()
