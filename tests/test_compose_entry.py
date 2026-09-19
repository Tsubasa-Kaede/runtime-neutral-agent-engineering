"""CU-COCKPIT-1 P1: user composition surface listing/preview closures.

P1 验收面：UserCompositionSurface 新增只读闭包（listing = Verified 池
canonically-sorted 清单；preview = selection → (intent, resolve 结果)
纯装配+解析，零副作用零执行）。start 链 = M3 既有 start_user_composition
零改动（七步）。全部离线：scripted adapters + 注入 evidence；REAL=0。
fixture 惯例镜像 test_user_composition_entry.py。
"""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import cockpit_entry  # noqa: E402
import host_entry  # noqa: E402
from candidate_validation import CandidateValidationStatus  # noqa: E402
from event_index import EventIndex  # noqa: E402
from external_runtime import (  # noqa: E402
    InvocationResult,
    InvocationStatus,
    InvocationTrace,
)
from runtime_adapter_registry import (  # noqa: E402
    AdapterDescriptor,
    AdapterRegistry,
)


# ---------------------------------------------------------------- doubles


class _OfflineAdapter:
    """Offline double（first_run 同款）：记录全部 invoke = NO
    EXECUTION 硬门的证据源。"""

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


def _adapters():
    return tuple(_OfflineAdapter(f"rt-{name}", f"prov-{name}")
                 for name in "abc")


def _four_adapters():
    return _adapters() + (_OfflineAdapter("rt-d", "prov-d"),)


def _surface(adapters=None, verified_ids=None, evidence=None):
    """构造 user 面 + 全部现场件。默认全部注册 runtime 皆 VERIFIED。"""
    if adapters is None:
        adapters = _adapters()
    registry, skipped = host_entry.environment_registry(
        _factories(*adapters))
    if evidence is None:
        if verified_ids is None:
            verified_ids = tuple(
                descriptor.runtime_id for descriptor in registry.list())
        evidence = _verified_evidence(registry, *verified_ids)
    hooks = []

    def boundary_hook(boundary, execution_id):
        hooks.append((boundary, execution_id))

    surface = cockpit_entry._user_composition_surface(
        registry, skipped, evidence, timeout_seconds=None,
        boundary_hook=boundary_hook, observation_sink=None,
        event_index=EventIndex())
    return surface, adapters, registry, skipped, evidence, hooks


def _selection(surface, roles=("architect", "coder")):
    """从 listing 派生 P1 selection（（runtime_id, role）对，声明序）。"""
    return tuple(
        (entry.runtime_id, role)
        for entry, role in zip(surface.listing(), roles))


# ---------------------------------------------------------------- listing


class ListingTests(unittest.TestCase):
    def test_deterministic_canonical_order(self):
        surface, _, _, _, _, _ = _surface()
        first = surface.listing()
        second = surface.listing()
        self.assertEqual(
            tuple(entry.runtime_id for entry in first),
            ("rt-a", "rt-b", "rt-c"))
        self.assertEqual(first, second)
        self.assertTrue(all(
            entry.provider_id for entry in first))

    def test_verified_only(self):
        surface, _, _, _, _, _ = _surface(verified_ids=("rt-a", "rt-b"))
        self.assertEqual(
            tuple(entry.runtime_id for entry in surface.listing()),
            ("rt-a", "rt-b"))

    def test_empty_pool_listing(self):
        surface, _, _, _, _, _ = _surface(verified_ids=())
        self.assertEqual(surface.listing(), ())


# ---------------------------------------------------------------- preview


class PreviewTests(unittest.TestCase):
    def test_success_returns_intent_and_resolved(self):
        surface, adapters, _, _, _, _ = _surface()
        intent, resolved = surface.preview(
            (("rt-a", "architect"), ("rt-b", "coder")))
        self.assertEqual(
            tuple(spec.member_id for spec in intent.members),
            ("member-1", "member-2"))
        self.assertEqual(
            tuple(spec.role for spec in intent.members),
            ("architect", "coder"))
        self.assertEqual(
            tuple(request.runtime_id
                  for request in intent.binding_requests.values()),
            ("rt-a", "rt-b"))
        self.assertEqual(
            tuple(binding.runtime_id for binding in resolved.bindings),
            ("rt-a", "rt-b"))
        self.assertEqual(resolved.steps,
                         (("architect", "rt-a"), ("coder", "rt-b")))
        # 纯只读：零装配、零执行
        self.assertEqual(surface.composed_runs, [])
        self.assertTrue(all(adapter.requests == [] for adapter in adapters))

    def test_member_count_gates_zero_one_five(self):
        surface, _, _, _, _, _ = _surface()
        for selection in (
                (),
                (("rt-a", "architect"),),
                tuple((f"rt-{name}", "coder") for name in "abcde")):
            intent, result = surface.preview(selection)
            self.assertEqual(result.reason, "INVALID_MEMBER_COUNT")
            self.assertIsNone(intent)

    def test_two_three_four_all_resolvable(self):
        for count, roles in ((2, ("architect", "coder")),
                             (3, ("architect", "coder", "reviewer")),
                             (4, ("architect", "coder", "tester",
                                  "reviewer"))):
            surface, _, _, _, _, _ = _surface(adapters=_four_adapters())
            selection = tuple(
                (f"rt-{name}", role)
                for name, role in zip("abcd", roles))
            intent, resolved = surface.preview(selection)
            self.assertIsNone(getattr(resolved, "reason", None))
            self.assertEqual(len(resolved.bindings), count)

    def test_none_roles_prefilled_from_default_assignment(self):
        """role=None 对 → 子集池默认指派回填（唯一真源复用：
        _TEMPLATES[N] 同 sorted 序同位次）。"""
        surface, _, _, _, _, _ = _surface()
        intent, resolved = surface.preview(
            (("rt-a", None), ("rt-b", None), ("rt-c", None)))
        self.assertIsNone(getattr(resolved, "reason", None))
        self.assertEqual(resolved.steps,
                         (("architect", "rt-a"), ("coder", "rt-b"),
                          ("reviewer", "rt-c")))

    def test_partial_override_prefills_only_unassigned(self):
        surface, _, _, _, _, _ = _surface()
        intent, resolved = surface.preview(
            (("rt-a", "tester"), ("rt-b", None)))
        self.assertIsNone(getattr(resolved, "reason", None))
        # override 保留；未指派成员回填子集默认位次
        self.assertEqual(resolved.steps,
                         (("tester", "rt-a"), ("coder", "rt-b")))

    def test_duplicate_role_is_valid(self):
        surface, _, _, _, _, _ = _surface()
        intent, resolved = surface.preview(
            (("rt-a", "architect"), ("rt-b", "architect")))
        self.assertIsNone(getattr(resolved, "reason", None))
        self.assertEqual(resolved.steps,
                         (("architect", "rt-a"),
                          ("architect", "rt-b")))

    def test_unqualified_runtime_rejected_with_hint(self):
        surface, _, _, _, _, _ = _surface(verified_ids=("rt-a", "rt-b"))
        intent, result = surface.preview(
            (("rt-a", "architect"), ("rt-c", "coder")))
        self.assertEqual(result.reason, "RUNTIME_NOT_QUALIFIED")
        self.assertIn("qualify", result.hint)


# ------------------------------------------------------- start via preview


class StartViaPreviewTests(unittest.TestCase):
    def test_start_with_previewed_intent_and_expected(self):
        surface, adapters, _, _, _, hooks = _surface()
        selection = (("rt-a", "architect"), ("rt-b", "coder"))
        intent, expected = surface.preview(selection)
        composed = surface.start("compose task", intent, expected)
        self.assertTrue(hasattr(composed, "drive"))
        self.assertEqual(composed.task, "compose task")
        self.assertEqual(composed.steps, expected.steps)
        self.assertEqual(len(surface.composed_runs), 1)
        # NO EXECUTION：装配即止
        self.assertTrue(all(adapter.requests == [] for adapter in adapters))
        self.assertEqual(len(hooks), 1)

    def test_default_parity_template_selection(self):
        """COMPOSE 不改任何勾选/角色（模板默认）≡ default 漏斗启动。"""
        from cockpit_projection import DEFAULT_ROLE_TEMPLATES
        adapters = _adapters()
        common = dict(timeout_seconds=None, observation_sink=None,
                      event_index=EventIndex())
        registry, skipped = host_entry.environment_registry(
            _factories(*adapters))
        evidence = _verified_evidence(registry, "rt-a", "rt-b", "rt-c")
        funnel = cockpit_entry._funnel_composition_closures(
            registry, skipped, evidence, **common)
        user = cockpit_entry._user_composition_surface(
            registry, skipped, evidence,
            boundary_hook=lambda boundary, execution_id: None, **common)
        roles = DEFAULT_ROLE_TEMPLATES[3]
        selection = tuple(
            (entry.runtime_id, role)
            for entry, role in zip(user.listing(), roles))
        intent, expected = user.preview(selection)
        default_run = funnel.start("parity task", funnel.preview())
        user_run = user.start("parity task", intent, expected)
        self.assertEqual(user_run.task, default_run.task)
        self.assertEqual(user_run.steps, default_run.steps)
        self.assertEqual(user_run.plan, default_run.plan)
        self.assertEqual(user_run.task_id, default_run.task_id)
        self.assertEqual(user_run.execution_id, default_run.execution_id)
        self.assertEqual(user_run.groups, ())
        self.assertEqual(default_run.groups, ())

    def test_start_pool_dropout_is_truthful_error(self):
        """预览后证据删除：start STEP 3 池门诚实拒（先于披露门）。"""
        surface, adapters, registry, _, evidence, _ = _surface()
        selection = (("rt-a", "architect"), ("rt-b", "coder"))
        intent, expected = surface.preview(selection)
        evidence.pop(registry.get("rt-b").identity, None)
        result = surface.start("dropout task", intent, expected)
        self.assertEqual(result.reason, "RUNTIME_NOT_QUALIFIED")
        self.assertEqual(surface.composed_runs, [])
        self.assertTrue(all(adapter.requests == [] for adapter in adapters))

    def test_composition_changed_on_identity_drift(self):
        """预览后 identity 漂移：STEP 4 披露全等门 → CompositionChanged
        （零装配零静默重绑——M3 identity-swap 同型）。"""
        adapters = _adapters()[:2]
        registry_old, skipped = host_entry.environment_registry(
            _factories(*adapters))
        fresh = AdapterRegistry()
        for descriptor in registry_old.list():
            kwargs = dict(
                runtime_id=descriptor.runtime_id,
                provider_id=descriptor.provider_id,
                runtime_type=descriptor.runtime_type,
                display_name=descriptor.display_name,
                adapter_factory=descriptor.adapter_factory,
                model_id=descriptor.model_id,
                config_fingerprint=descriptor.config_fingerprint)
            if descriptor.runtime_id == "rt-a":
                kwargs["config_fingerprint"] = "fp-B"
            fresh.register(AdapterDescriptor(**kwargs))
        evidence = _verified_evidence(registry_old, "rt-a", "rt-b")
        evidence.update(_verified_evidence(fresh, "rt-a", "rt-b"))

        def make_surface(registry):
            return cockpit_entry._user_composition_surface(
                registry, skipped, evidence, timeout_seconds=None,
                boundary_hook=lambda boundary, execution_id: None,
                observation_sink=None, event_index=EventIndex())

        selection = (("rt-a", "architect"), ("rt-b", "coder"))
        intent, expected = make_surface(registry_old).preview(selection)
        result = make_surface(fresh).start("drift task", intent, expected)
        self.assertTrue(hasattr(result, "reasons"))
        self.assertTrue(result.reasons)
        self.assertTrue(hasattr(result, "composition"))


# ---------------------------------------------------- 2.8-C group authoring


class GroupAuthoringEntryTests(unittest.TestCase):
    """2.8-C entry 闭包：groups 通道（透传/映射/缺省/置换不变/backstop）。

    设计 §21 entry 面：preview(selection, groups) 换算 member-N 域；
    组零参与 steps 派生链（Q7/AC-C-SEM-02）；缺省 () 后向兼容；
    core 四组错误词 backstop（空组 = GAP-1 登记文档测试——预期通过
    validate 的诚实面）。"""

    def test_groups_passed_through_as_member_domain(self):
        """UI 域组草稿 → intent.groups member-N 域（组内=声明位序）。"""
        surface, _, _, _, _, _ = _surface(adapters=_four_adapters())
        selection = (("rt-a", "architect"), ("rt-b", "coder"),
                     ("rt-c", "tester"), ("rt-d", "reviewer"))
        groups = (("g1", ("rt-b", "rt-a")), ("g2", ("rt-d",)))
        intent, resolved = surface.preview(selection, groups)
        self.assertIsNone(getattr(resolved, "reason", None))
        # 组内成员序 = selection 声明位序归一（草稿输入序不保留）
        self.assertEqual(
            tuple((spec.group_id, spec.member_ids) for spec in intent.groups),
            (("g1", ("member-1", "member-2")), ("g2", ("member-4",))))
        # resolved 透传（结构身份全链同值——AC-C-SEM-01）
        self.assertEqual(resolved.groups, intent.groups)

    def test_group_member_order_follows_selection_declaration(self):
        """组内成员序 = selection 声明位序过滤（乱序组内输入归一）。"""
        surface, _, _, _, _, _ = _surface()
        selection = (("rt-b", "coder"), ("rt-a", "architect"))
        # 草稿组内输入逆声明序——换算后仍按声明位序
        intent, resolved = surface.preview(
            selection, (("g1", ("rt-a", "rt-b")),))
        self.assertEqual(intent.groups[0].member_ids,
                         ("member-1", "member-2"))

    def test_partial_grouping_implicit_members_untouched(self):
        """部分分组：未入组成员不出现于任何组（隐式主链域）。"""
        surface, _, _, _, _, _ = _surface(adapters=_four_adapters())
        selection = tuple(
            (f"rt-{name}", "coder") for name in "abcd")
        intent, resolved = surface.preview(
            selection, (("g9", ("rt-b",)),))
        grouped = {member for spec in intent.groups
                   for member in spec.member_ids}
        self.assertEqual(grouped, {"member-2"})

    def test_default_groups_empty_backward_compatible(self):
        """缺省 groups=()：与 P1 平面协作逐字节同构（groups 恒 ()）。"""
        surface, _, _, _, _, _ = _surface()
        selection = (("rt-a", "architect"), ("rt-b", "coder"))
        intent_legacy, resolved_legacy = surface.preview(selection)
        intent_default, resolved_default = surface.preview(selection, ())
        self.assertEqual(intent_legacy, intent_default)
        self.assertEqual(resolved_legacy, resolved_default)
        self.assertEqual(intent_default.groups, ())

    def test_group_order_permutation_steps_identical(self):
        """组声明序置换 + 同 members 序 → steps/bindings 逐字节不变
        （Group ≠ execution ordering primitive；AC-C-SEM-02）。"""
        surface, _, _, _, _, _ = _surface(adapters=_four_adapters())
        selection = tuple(
            (f"rt-{name}", role) for name, role in zip(
                "abcd", ("architect", "coder", "tester", "reviewer")))
        first_groups = (("g1", ("rt-a", "rt-b")), ("g2", ("rt-c",)))
        second_groups = (("g2", ("rt-c",)), ("g1", ("rt-a", "rt-b")))
        _, first = surface.preview(selection, first_groups)
        _, second = surface.preview(selection, second_groups)
        _, bare = surface.preview(selection)
        self.assertEqual(first.steps, second.steps)
        self.assertEqual(first.steps, bare.steps)
        self.assertEqual(first.bindings, second.bindings)
        self.assertEqual(first.member_ids, second.member_ids)
        # 组本体随声明序保真（id 数值序 ≠ 声明序分叉用例）
        self.assertEqual(
            tuple(spec.group_id for spec in first.groups), ("g1", "g2"))
        self.assertEqual(
            tuple(spec.group_id for spec in second.groups), ("g2", "g1"))

    def test_skipped_id_numbers_preserved_verbatim(self):
        """跳号 id（g1,g3）：声明序+id 字符串原样透传（零重排/零重编）。"""
        surface, _, _, _, _, _ = _surface(adapters=_four_adapters())
        selection = tuple((f"rt-{name}", "coder") for name in "abcd")
        groups = (("g1", ("rt-a",)), ("g3", ("rt-b", "rt-c")))
        intent, resolved = surface.preview(selection, groups)
        self.assertEqual(
            tuple(spec.group_id for spec in intent.groups), ("g1", "g3"))
        self.assertEqual(
            tuple(spec.group_id for spec in resolved.groups), ("g1", "g3"))

    def test_unknown_runtime_group_member_rejected(self):
        """组引用池外 runtime → core GROUP_MEMBER_UNKNOWN 原词回传。"""
        surface, _, _, _, _, _ = _surface()
        selection = (("rt-a", "architect"), ("rt-b", "coder"))
        intent, result = surface.preview(
            selection, (("g1", ("rt-zzz",)),))
        self.assertIsNone(intent)
        self.assertEqual(result.reason, "GROUP_MEMBER_UNKNOWN")

    def test_member_in_multiple_groups_rejected(self):
        """双归属构造（绕过 UI 直调）→ core MEMBER_IN_MULTIPLE_GROUPS。"""
        surface, _, _, _, _, _ = _surface(adapters=_four_adapters())
        selection = tuple((f"rt-{name}", "coder") for name in "abcd")
        intent, result = surface.preview(
            selection, (("g1", ("rt-a",)), ("g2", ("rt-a", "rt-b"))))
        self.assertIsNone(intent)
        self.assertEqual(result.reason, "MEMBER_IN_MULTIPLE_GROUPS")

    def test_empty_group_passes_validation_gap1_documented(self):
        """CORE GAP-1 文档测试：空组经 validate 不拒（:182-195 空环零
        迭代）——设计登记不修 core；TUI draft 结构性不可空（AC-04），
        此处钉定 backstop 缺口真实存在（诚实面，非期望行为）。"""
        surface, _, _, _, _, _ = _surface(adapters=_four_adapters())
        selection = tuple((f"rt-{name}", "coder") for name in "abcd")
        intent, resolved = surface.preview(selection, (("g1", ()),))
        # GAP-1：core 不拒——resolved 透传空组（真实行为钉定）
        self.assertIsNone(getattr(resolved, "reason", None))
        self.assertEqual(resolved.groups[0].member_ids, ())

    def test_invalid_group_id_rejected(self):
        """group_id 非非空字符串 → core INVALID_GROUP_ID 原词。"""
        surface, _, _, _, _, _ = _surface()
        selection = (("rt-a", "architect"), ("rt-b", "coder"))
        intent, result = surface.preview(selection, (("", ("rt-a",)),))
        self.assertIsNone(intent)
        self.assertEqual(result.reason, "INVALID_GROUP_ID")

    def test_duplicate_group_id_rejected(self):
        """重复 group_id → core DUPLICATE_GROUP_ID 原词。"""
        surface, _, _, _, _, _ = _surface(adapters=_four_adapters())
        selection = tuple((f"rt-{name}", "coder") for name in "abcd")
        intent, result = surface.preview(
            selection, (("g1", ("rt-a",)), ("g1", ("rt-b",))))
        self.assertIsNone(intent)
        self.assertEqual(result.reason, "DUPLICATE_GROUP_ID")


if __name__ == "__main__":
    unittest.main()
