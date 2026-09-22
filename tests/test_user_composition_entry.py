"""2.8.0 Composition Core M3: user composition entry contract tests.

M3 受控解冻的验收面：_user_composition_surface / start_user_composition
七步管线、ComposedRun.groups 尾部默认字段、default/user parity、
惰性接线（循环 import 守卫）、NO EXECUTION 硬门（assembly 即止）。

全部离线：scripted adapters + 注入 evidence；REAL=0（import-order
守卫的 subprocess 是测试 harness 机制，非 provider invocation）。
fixture 惯例镜像 test_cockpit_first_run.py（_OfflineAdapter/_factories/
_verified_evidence）。
"""
import os
import subprocess
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
from composition_core import (  # noqa: E402
    AgentSpec,
    CollaborationGroupSpec,
    CompositionIntent,
    ResolvedComposition,
    RuntimeBindingRequest,
    resolve_composition,
)
from control_boundary import ControlModelError  # noqa: E402
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


def _intent(triples, groups=()):
    members = tuple(AgentSpec(m, r) for m, r, _ in triples)
    requests = {m: RuntimeBindingRequest(rt) for m, _, rt in triples}
    return CompositionIntent(members=members, binding_requests=requests,
                             groups=tuple(groups))


def _group(group_id, member_ids):
    return CollaborationGroupSpec(group_id=group_id,
                                  member_ids=tuple(member_ids))


def _surface(adapters=None, evidence=None, verified_ids=None,
             boundary_hook=None):
    """构造 user 面 + 全部现场件（registry/evidence/hooks）。

    默认 evidence = 全部注册 runtime 皆 VERIFIED；verified_ids 传
    子集可制造"注册在场但未验证"的缺席场景。"""
    if adapters is None:
        adapters = (_OfflineAdapter("rt-a", "prov-a"),
                    _OfflineAdapter("rt-b", "prov-b"))
    registry, skipped = host_entry.environment_registry(
        _factories(*adapters))
    if evidence is None:
        if verified_ids is None:
            verified_ids = tuple(
                descriptor.runtime_id for descriptor in registry.list())
        evidence = _verified_evidence(registry, *verified_ids)
    hooks = []
    if boundary_hook is None:
        def boundary_hook(boundary, execution_id):
            hooks.append((boundary, execution_id))
    surface = cockpit_entry._user_composition_surface(
        registry, skipped, evidence, timeout_seconds=None,
        boundary_hook=boundary_hook, observation_sink=None,
        event_index=EventIndex())
    return surface, adapters, registry, skipped, evidence, hooks


def _four_adapters():
    return tuple(_OfflineAdapter(f"rt-{name}", f"prov-{name}")
                 for name in "abcd")


# ------------------------------------------------------------- happy path


class UserCompositionHappyPathTests(unittest.TestCase):
    """1-3 / 12 / 18 / 22-23：happy 2/3/4 员 + expected=None + groups
    透传 + NO EXECUTION 硬门。"""

    def test_happy_two_member(self):
        surface, adapters, _registry, _skipped, _evidence, hooks = _surface()
        intent = _intent([("m1", "architect", "rt-a"),
                          ("m2", "coder", "rt-b")])
        composed = surface.start("user task", intent)
        self.assertIsInstance(composed, cockpit_entry.ComposedRun)
        self.assertEqual(composed.task, "user task")
        self.assertEqual(composed.steps,
                         (("architect", "rt-a"), ("coder", "rt-b")))
        self.assertEqual([slot[0] for slot in composed.plan],
                         ["step-0-architect", "step-1-coder"])
        self.assertEqual(composed.groups, ())
        self.assertEqual(len(surface.composed_runs), 1)
        self.assertEqual(len(hooks), 1)          # assembled exactly once
        self.assertTrue(callable(composed.drive))  # 未调用（见下）

    def test_happy_three_member(self):
        adapters = _four_adapters()[:3]
        surface, *_ = _surface(adapters=adapters)
        composed = surface.start(
            "user task", _intent([("m1", "architect", "rt-a"),
                                  ("m2", "coder", "rt-b"),
                                  ("m3", "reviewer", "rt-c")]))
        self.assertEqual(
            composed.steps,
            (("architect", "rt-a"), ("coder", "rt-b"),
             ("reviewer", "rt-c")))

    def test_happy_four_member(self):
        surface, *_ = _surface(adapters=_four_adapters())
        composed = surface.start(
            "user task", _intent([("m1", "architect", "rt-a"),
                                  ("m2", "coder", "rt-b"),
                                  ("m3", "tester", "rt-c"),
                                  ("m4", "reviewer", "rt-d")]))
        self.assertEqual(len(composed.steps), 4)
        self.assertEqual([slot[0] for slot in composed.plan],
                         ["step-0-architect", "step-1-coder",
                          "step-2-tester", "step-3-reviewer"])

    def test_expected_none_skips_gate_both_forms(self):
        surface, *_ = _surface()
        intent = _intent([("m1", "architect", "rt-a"),
                          ("m2", "coder", "rt-b")])
        explicit = surface.start("task one", intent, None)
        self.assertIsInstance(explicit, cockpit_entry.ComposedRun)
        omitted = surface.start("task two", intent)  # 默认参数同义
        self.assertIsInstance(omitted, cockpit_entry.ComposedRun)
        self.assertEqual(len(surface.composed_runs), 2)

    def test_user_path_groups_equal_live_groups(self):
        surface, _, registry, _, evidence, _hooks = _surface()
        spec = _group("pair", ("m1", "m2"))
        intent = _intent([("m1", "architect", "rt-a"),
                          ("m2", "coder", "rt-b")], groups=[spec])
        live = resolve_composition(
            intent, cockpit_entry._verified_pool(registry, evidence))
        composed = surface.start("grouped task", intent, live)
        self.assertEqual(composed.groups, live.groups)
        self.assertEqual(composed.groups, (spec,))

    def test_no_execution_after_assembly(self):
        # NO EXECUTION 硬门：装配即止——provider 零调用、事件/事实/
        # usage 零增长、drive 零触碰。
        surface, adapters, *_ = _surface()
        composed = surface.start(
            "quiet task", _intent([("m1", "architect", "rt-a"),
                                   ("m2", "coder", "rt-b")]))
        for adapter in adapters:
            self.assertEqual(adapter.requests, [])
        self.assertEqual(composed.events(), ())
        self.assertEqual(composed.facts(), ())
        self.assertEqual(composed.usage(), ())


# ------------------------------------------------------------- validation


class UserCompositionValidationTests(unittest.TestCase):
    """4-7 + STEP 1：结构/词表门经 core resolve 前置拦截。"""

    def _error(self, intent, task="user task"):
        surface, *_ = _surface()
        outcome = surface.start(task, intent)
        self.assertIsInstance(outcome, cockpit_entry.CompositionError)
        return outcome

    def test_step_one_invalid_task_matches_default_law(self):
        surface, *_ = _surface()
        outcome = surface.start("   ", _intent(
            [("m1", "architect", "rt-a"), ("m2", "coder", "rt-b")]))
        self.assertEqual(outcome.reason, "INVALID_TASK")

    def test_invalid_role_rejected(self):
        self.assertEqual(
            self._error(_intent([("m1", "hacker", "rt-a"),
                                 ("m2", "coder", "rt-b")])).reason,
            "UNKNOWN_ROLE")

    def test_invalid_member_count_rejected(self):
        for triples in (
                [("m1", "coder", "rt-a")],
                [(f"m{i}", "coder", "rt-a") for i in range(5)]):
            with self.subTest(count=len(triples)):
                self.assertEqual(
                    self._error(_intent(triples)).reason,
                    "INVALID_MEMBER_COUNT")

    def test_invalid_group_member_rejected(self):
        self.assertEqual(
            self._error(_intent(
                [("m1", "architect", "rt-a"), ("m2", "coder", "rt-b")],
                groups=[_group("g", ("m1", "ghost"))])).reason,
            "GROUP_MEMBER_UNKNOWN")

    def test_duplicate_group_membership_rejected(self):
        self.assertEqual(
            self._error(_intent(
                [("m1", "architect", "rt-a"), ("m2", "coder", "rt-b")],
                groups=[_group("g1", ("m1",)),
                        _group("g2", ("m1", "m2"))])).reason,
            "MEMBER_IN_MULTIPLE_GROUPS")


# ------------------------------------------------------------- resolution


class UserCompositionResolutionTests(unittest.TestCase):
    """8-10：池门 truthful REJECT + no fallback。"""

    def test_runtime_missing_rejected(self):
        surface, *_ = _surface()
        outcome = surface.start(
            "task", _intent([("m1", "architect", "rt-zz"),
                             ("m2", "coder", "rt-a")]))
        self.assertEqual(outcome.reason, "RUNTIME_NOT_QUALIFIED")
        self.assertIn("rt-zz", outcome.detail)

    def test_runtime_unqualified_rejected_with_hint(self):
        # registry 在场但无 VERIFIED evidence：池外即拒（无 silent
        # promotion）。
        adapters = (_OfflineAdapter("rt-a", "prov-a"),
                    _OfflineAdapter("rt-c", "prov-c"))
        surface, *_ = _surface(adapters=adapters, verified_ids=("rt-a",))
        outcome = surface.start(
            "task", _intent([("m1", "architect", "rt-c"),
                             ("m2", "coder", "rt-a")]))
        self.assertEqual(outcome.reason, "RUNTIME_NOT_QUALIFIED")
        self.assertEqual(outcome.hint, host_entry._HINT_QUALIFY)

    def test_no_fallback_intent_untouched_zero_assembly(self):
        # 点名 rt-zz 缺席（池={rt-a,rt-b}）：REJECT——零替换、intent
        # 原样、零装配。
        surface, adapters, registry, skipped, evidence, hooks = _surface()
        intent = _intent([("m1", "architect", "rt-zz"),
                          ("m2", "coder", "rt-b")])
        snapshot = _intent([("m1", "architect", "rt-zz"),
                            ("m2", "coder", "rt-b")])
        outcome = surface.start("task", intent)
        self.assertEqual(outcome.reason, "RUNTIME_NOT_QUALIFIED")
        self.assertEqual(intent, snapshot)      # 用户声明零改写
        self.assertEqual(surface.composed_runs, [])
        self.assertEqual(hooks, [])
        for adapter in adapters:
            self.assertEqual(adapter.requests, [])


# ------------------------------------------------------------------- gate


class UserCompositionGateTests(unittest.TestCase):
    """11 / 13：expected/live 四字段全等门（identity 含内）。"""

    def test_identity_change_refuses_with_zero_assembly(self):
        # 池漂移：rt-a 重注册换 config_fingerprint，新旧 identity 均
        # 有 VERIFIED evidence → live bindings identity 变 → Changed。
        adapters = (_OfflineAdapter("rt-a", "prov-a"),
                    _OfflineAdapter("rt-b", "prov-b"))
        factories = _factories(*adapters)
        registry_old, skipped = host_entry.environment_registry(factories)
        fresh = AdapterRegistry()
        for descriptor in host_entry.environment_registry(factories)[0].list():
            kwargs = dict(
                runtime_id=descriptor.runtime_id,
                provider_id=descriptor.provider_id,
                runtime_type=descriptor.runtime_type,
                display_name=descriptor.display_name,
                adapter_factory=descriptor.adapter_factory,
                model_id=descriptor.model_id)
            if descriptor.runtime_id == "rt-a":
                kwargs["config_fingerprint"] = "fp-B"
            fresh.register(AdapterDescriptor(**kwargs))
        evidence = {}
        for registry in (registry_old, fresh):
            for runtime_id in ("rt-a", "rt-b"):
                evidence[registry.get(runtime_id).identity] = (
                    SimpleNamespace(
                        status=CandidateValidationStatus.VERIFIED))
        intent = _intent([("m1", "architect", "rt-a"),
                          ("m2", "coder", "rt-b")])
        expected = resolve_composition(
            intent, cockpit_entry._verified_pool(registry_old, evidence))
        self.assertIsInstance(expected, ResolvedComposition)
        hooks = []
        surface = cockpit_entry._user_composition_surface(
            fresh, skipped, evidence, timeout_seconds=None,
            boundary_hook=lambda boundary, execution_id:
                hooks.append((boundary, execution_id)),
            observation_sink=None, event_index=EventIndex())
        changed = surface.start("drift task", intent, expected)
        self.assertIsInstance(changed, cockpit_entry.CompositionChanged)
        self.assertIsInstance(changed.composition, ResolvedComposition)
        self.assertTrue(changed.reasons)
        self.assertEqual(surface.composed_runs, [])
        self.assertEqual(hooks, [])

    def test_expected_identity_mismatch_detected(self):
        # 四字段门：三字段相等、identity 不同 → 不放行（NamedTuple
        # 值等含 canonical_runtime_identity）。
        surface, _, registry, _, evidence, hooks = _surface()
        pool = cockpit_entry._verified_pool(registry, evidence)
        intent = _intent([("m1", "architect", "rt-a"),
                          ("m2", "coder", "rt-b")])
        expected = resolve_composition(intent, pool)
        tampered = expected._replace(bindings=(
            expected.bindings[0]._replace(
                canonical_runtime_identity=("rt-a", "prov-a", None, "fp-x")),
            *expected.bindings[1:]))
        changed = surface.start("tamper task", intent, tampered)
        self.assertIsInstance(changed, cockpit_entry.CompositionChanged)
        self.assertEqual(surface.composed_runs, [])


# ----------------------------------------------------------- invariance


class GroupPermutationTests(unittest.TestCase):
    """14：groups 置换 → steps/plan/slot_ids 逐字节不变。"""

    _TRIPLES = (("m1", "architect", "rt-a"),
                ("m2", "coder", "rt-b"),
                ("m3", "reviewer", "rt-c"))

    def _compose(self, groups):
        adapters = _four_adapters()[:3]
        surface, *_ = _surface(adapters=adapters)
        return surface.start(
            "perm task",
            _intent(list(self._TRIPLES), groups=groups))

    def test_permutation_leaves_execution_surfaces_unchanged(self):
        baseline = self._compose(())
        regrouped = self._compose(
            [_group("left", ("m1", "m2")), _group("right", ("m3",))])
        self.assertEqual(regrouped.steps, baseline.steps)
        self.assertEqual(regrouped.plan, baseline.plan)
        self.assertEqual(
            [slot[0] for slot in regrouped.plan],
            [slot[0] for slot in baseline.plan])
        self.assertEqual(regrouped.groups,
                         (_group("left", ("m1", "m2")),
                          _group("right", ("m3",))))
        # 2.8-B：member_ids = 声明成员快照（STEP 7 同点注入），
        # 组置换不改变它；两条路径（有组/无组）取值相同。
        self.assertEqual(baseline.member_ids, ("m1", "m2", "m3"))
        self.assertEqual(regrouped.member_ids, ("m1", "m2", "m3"))


# ----------------------------------------------------------------- parity


class DefaultUserParityTests(unittest.TestCase):
    """15 / 17：default 漏斗与 user 路径产出同构 execution 语义面。"""

    def test_default_and_user_paths_produce_equal_semantics(self):
        adapters = (_OfflineAdapter("rt-a", "prov-a"),
                    _OfflineAdapter("rt-b", "prov-b"))
        registry, skipped = host_entry.environment_registry(
            _factories(*adapters))
        evidence = _verified_evidence(registry, "rt-a", "rt-b")
        # default 漏斗路径
        default_surfaces = cockpit_entry._funnel_composition_closures(
            registry, skipped, evidence, timeout_seconds=None,
            boundary_hook=None, observation_sink=None,
            event_index=EventIndex())
        default_composed = default_surfaces.start(
            "parity task", default_surfaces.preview())
        self.assertIsInstance(default_composed, cockpit_entry.ComposedRun)
        # user 路径：default → intent → resolve → start（零 drive）
        from composition_core import default_composition_to_intent
        pool = cockpit_entry._verified_pool(registry, evidence)
        intent = default_composition_to_intent(pool)
        expected = resolve_composition(intent, pool)
        user_surface = cockpit_entry._user_composition_surface(
            registry, skipped, evidence, timeout_seconds=None,
            boundary_hook=None, observation_sink=None,
            event_index=EventIndex())
        user_composed = user_surface.start("parity task", intent, expected)
        self.assertEqual(user_composed.task, default_composed.task)
        self.assertEqual(user_composed.steps, default_composed.steps)
        self.assertEqual(user_composed.plan, default_composed.plan)
        self.assertEqual(user_composed.task_id, default_composed.task_id)
        self.assertEqual(user_composed.execution_id,
                         default_composed.execution_id)
        self.assertEqual(user_composed.groups, ())   # 17：default=()
        self.assertEqual(default_composed.groups, ())
        # 2.8-B 双路径快照语义：default 漏斗直装配（不经 STEP 7）
        # 双缺省 ()；user 路径同点齐注——groups=()（未编排组）但
        # member_ids 恒随声明成员。
        self.assertEqual(default_composed.member_ids, ())
        self.assertEqual(user_composed.member_ids,
                         tuple(spec.member_id for spec in intent.members))
        # 零 execution：parity 两侧都只装配未驱动
        for adapter in adapters:
            self.assertEqual(adapter.requests, [])


# --------------------------------------------------- ComposedRun groups


class ComposedRunGroupsCompatTests(unittest.TestCase):
    """16 / 19：legacy 直装配 groups=()；字段序/构造器兼容。"""

    def test_legacy_direct_assembly_groups_default_empty(self):
        adapters = (_OfflineAdapter("rt-a", "prov-a"),
                    _OfflineAdapter("rt-b", "prov-b"))
        registry, skipped = host_entry.environment_registry(
            _factories(*adapters))
        evidence = _verified_evidence(registry, "rt-a", "rt-b")
        steps = (("coder", "rt-a"), ("reviewer", "rt-b"))
        resolved = cockpit_entry._resolve_runtimes(
            registry, skipped, evidence, steps)
        self.assertIsInstance(resolved, dict)
        composed = cockpit_entry._assemble_execution(
            resolved, "legacy task", steps, None, event_index=EventIndex())
        self.assertEqual(composed.groups, ())
        self.assertEqual(composed.member_ids, ())

    def test_field_order_and_constructor_compatibility(self):
        # W3-P 起尾随 additive 位两个变三个：groups / member_ids /
        # compile_disclosure（缺省 None——呈现披露闭包，零执行真值）
        self.assertEqual(
            cockpit_entry.ComposedRun._fields[:-3],
            ("task", "steps", "plan", "task_id", "execution_id",
             "emit", "drive", "session", "dispatch_control",
             "revision_pending", "events", "facts", "usage"))
        self.assertEqual(cockpit_entry.ComposedRun._fields[-3], "groups")
        self.assertEqual(cockpit_entry.ComposedRun._fields[-2],
                         "member_ids")
        self.assertEqual(cockpit_entry.ComposedRun._fields[-1],
                         "compile_disclosure")
        keyword = cockpit_entry.ComposedRun(
            task="t", steps=(), plan=(), task_id="t", execution_id="e",
            emit=None, drive=None, session=None, dispatch_control=None,
            revision_pending=None, events=None, facts=None, usage=None)
        self.assertEqual(keyword.groups, ())
        self.assertEqual(keyword.member_ids, ())
        self.assertIsNone(keyword.compile_disclosure)
        positional = cockpit_entry.ComposedRun(
            "t", (), (), "t", "e", None, None, None, None, None,
            None, None, None)
        self.assertEqual(positional.groups, ())
        self.assertEqual(positional.member_ids, ())
        self.assertIsNone(positional.compile_disclosure)
        with_ids = cockpit_entry.ComposedRun(
            task="t", steps=(), plan=(), task_id="t", execution_id="e",
            emit=None, drive=None, session=None, dispatch_control=None,
            revision_pending=None, events=None, facts=None, usage=None,
            member_ids=("m1", "m2"))
        self.assertEqual(with_ids.member_ids, ("m1", "m2"))
        self.assertEqual(with_ids.groups, ())


# --------------------------------------------------------- import guard


class LazyImportGuardTests(unittest.TestCase):
    """20：cockpit_entry ↔ composition_core 双向 import 零循环
    （干净解释器双序；subprocess 为 harness 机制，REAL=0）。"""

    def test_import_orders_do_not_cycle(self):
        env = dict(os.environ, PYTHONPATH=str(SCRIPTS), PYTHONUTF8="1")
        for statement in (
                "import cockpit_entry, composition_core",
                "import composition_core, cockpit_entry"):
            with self.subTest(statement=statement):
                result = subprocess.run(
                    [sys.executable, "-c", statement],
                    env=env, capture_output=True)
                self.assertEqual(
                    result.returncode, 0,
                    result.stderr.decode("utf-8", "replace"))


# ----------------------------------------------------- assembly failure


class AssemblyFailureTests(unittest.TestCase):
    """21：装配失败保持既有 fail-fast 语义（M3 零改动）。"""

    def test_empty_steps_fail_fast_is_existing_control_error(self):
        with self.assertRaises(ControlModelError) as raised:
            cockpit_entry._assemble_execution({}, "t", (), None)
        self.assertIn("at least one slot spec", str(raised.exception))


class GroupAuthoringChainTests(unittest.TestCase):
    """2.8-C：preview→start 组全链（draft 经 preview 铸 intent →
    STEP 7 run-local 快照落 run；快照对后续 authoring 免疫）。

    设计 §20 M3 扩面：组快照落 run + 后续组编辑零回写。"""

    def _select(self, surface):
        return tuple(
            (entry.runtime_id, role)
            for entry, role in zip(
                surface.listing(),
                ("architect", "coder", "tester", "reviewer")))

    def test_preview_to_start_snapshots_groups_into_run(self):
        surface, adapters, _, _, _, _ = _surface(adapters=_four_adapters())
        selection = self._select(surface)
        draft = (("g1", ("rt-a", "rt-b")), ("g2", ("rt-c",)))
        intent, expected = surface.preview(selection, draft)
        composed = surface.start("chain task", intent, expected)
        # STEP 7 快照 = resolved 透传（结构身份全链同值）
        self.assertEqual(composed.groups, intent.groups)
        self.assertEqual(
            tuple(spec.group_id for spec in composed.groups), ("g1", "g2"))
        self.assertEqual(composed.member_ids,
                         ("member-1", "member-2", "member-3", "member-4"))
        # NO EXECUTION 硬门
        self.assertTrue(all(adapter.requests == [] for adapter in adapters))

    def test_completed_run_snapshot_immune_to_later_authoring(self):
        """已完成 run 快照 immutable：后续新 preview（不同组）零回写。"""
        surface, _, _, _, _, _ = _surface(adapters=_four_adapters())
        selection = self._select(surface)
        intent, expected = surface.preview(
            selection, (("g1", ("rt-a", "rt-b")),))
        composed = surface.start("first task", intent, expected)
        frozen_groups = composed.groups
        frozen_members = composed.member_ids
        # 后续 authoring：重入 draft 再编排（同 selection 不同组）
        second_intent, second_expected = surface.preview(
            selection, (("g9", ("rt-c", "rt-d")),))
        self.assertNotEqual(second_intent.groups, frozen_groups)
        self.assertEqual(composed.groups, frozen_groups)
        self.assertEqual(composed.member_ids, frozen_members)
        # 快照随 run 存量不动（列表唯一项仍持原组）
        self.assertEqual(surface.composed_runs[0].groups, frozen_groups)


if __name__ == "__main__":
    unittest.main()
