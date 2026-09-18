"""2.8.0 Composition Core (M1) unit tests.

M0 ARCHITECTURE LOCK 钉死的 M1 面：validate/resolve 纯域、Group
六不等式、Identity 三元不混同、池门零替换、zero execution
authority、纯性（零 filesystem/零 runtime 调用）、runtime-neutral。

池快照 fixture 遵循 cockpit_entry._verified_pool 快照项协议
（runtime_id/provider_id/identity 三属性）；与真实
resolve_default_composition 的对拍等值环归 M2 契约测试，此处只
钉 core 侧的 default 预填正确性。
"""
import re
import sys
import unittest
from pathlib import Path
from typing import NamedTuple
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from composition_core import (  # noqa: E402
    COMPOSITION_ERROR_REASONS,
    ROLE_VOCABULARY,
    AgentSpec,
    CollaborationGroupSpec,
    CompositionIntent,
    ResolvedComposition,
    RuntimeBindingRequest,
    default_composition_to_intent,
    resolve_composition,
    validate_composition,
)
from cockpit_entry import (  # noqa: E402
    CompositionBinding,
    CompositionError,
)
from cockpit_projection import DEFAULT_ROLE_TEMPLATES  # noqa: E402

CORE_SOURCE = Path(sys.modules["composition_core"].__file__).read_text(
    encoding="utf-8")


class _PoolEntry(NamedTuple):
    """池快照项协议 fixture（与 registry evidence 池项同形）。"""

    runtime_id: str
    provider_id: str
    identity: tuple


_POOL = (
    _PoolEntry("rt-alpha", "prov-a", ("rt-alpha", "prov-a", "key-a")),
    _PoolEntry("rt-beta", "prov-b", ("rt-beta", "prov-b", "key-b")),
    _PoolEntry("rt-gamma", "prov-c", ("rt-gamma", "prov-c", "key-c")),
    _PoolEntry("rt-delta", "prov-d", ("rt-delta", "prov-d", "key-d")),
)


def _intent(triples, groups=()):
    """triples = [(member_id, role, runtime_id), ...]（声明序即执行序）。"""
    members = tuple(AgentSpec(m, r) for m, r, _ in triples)
    requests = {m: RuntimeBindingRequest(rt) for m, _, rt in triples}
    return CompositionIntent(members=members, binding_requests=requests,
                             groups=tuple(groups))


def _group(group_id, member_ids):
    return CollaborationGroupSpec(group_id=group_id,
                                  member_ids=tuple(member_ids))


class DomainConstructionTests(unittest.TestCase):
    """A：域构造 + happy path resolve + default 预填。"""

    def test_two_member_resolve(self):
        resolved = resolve_composition(
            _intent([("m1", "architect", "rt-beta"),
                     ("m2", "coder", "rt-alpha")]),
            _POOL)
        self.assertIsInstance(resolved, ResolvedComposition)
        self.assertEqual(resolved.member_ids, ("m1", "m2"))
        self.assertEqual(
            resolved.bindings,
            (CompositionBinding("architect", "rt-beta", "prov-b",
                                ("rt-beta", "prov-b", "key-b")),
             CompositionBinding("coder", "rt-alpha", "prov-a",
                                ("rt-alpha", "prov-a", "key-a"))))
        self.assertEqual(resolved.groups, ())
        self.assertEqual(
            resolved.steps,
            (("architect", "rt-beta"), ("coder", "rt-alpha")))

    def test_identity_verbatim_passthrough(self):
        # identity 原值透传：runtime_id/provider_id 相同绝不掩盖
        # identity 差异（CompositionBinding 契约）。
        exotic = (_PoolEntry("rt-alpha", "prov-a", ("x", 4, None)),)
        resolved = resolve_composition(
            _intent([("m1", "architect", "rt-alpha"),
                     ("m2", "coder", "rt-alpha")]),
            exotic)
        self.assertEqual(
            resolved.bindings[0].canonical_runtime_identity, ("x", 4, None))

    def test_counts_two_to_four_accepted(self):
        roles = ("architect", "coder", "tester", "reviewer")
        runtimes = ("rt-alpha", "rt-beta", "rt-gamma", "rt-alpha")
        for count in (2, 3, 4):
            with self.subTest(count=count):
                triples = [(f"m{i}", roles[i], runtimes[i])
                           for i in range(count)]
                self.assertIsNone(
                    validate_composition(_intent(triples)))
                self.assertIsInstance(
                    resolve_composition(_intent(triples), _POOL),
                    ResolvedComposition)

    def test_default_prefill_matches_canonical_zip(self):
        # core 侧等值环：预填 → validate → resolve 的 bindings ==
        # sorted(runtime_id) + 冻结模板 zip 的期望（对真实入口函数的
        # 对拍归 M2 契约测试）。
        shuffled = tuple(reversed(_POOL))
        for pool_size in (2, 3, 4):
            pool = shuffled[:pool_size]
            expected_runtimes = sorted(entry.runtime_id
                                       for entry in pool)
            expected_roles = DEFAULT_ROLE_TEMPLATES[pool_size]
            intent = default_composition_to_intent(pool)
            self.assertIsNone(validate_composition(intent))
            resolved = resolve_composition(intent, pool)
            self.assertIsInstance(resolved, ResolvedComposition)
            self.assertEqual(
                tuple(binding.role for binding in resolved.bindings),
                expected_roles)
            self.assertEqual(
                tuple(binding.runtime_id for binding in resolved.bindings),
                tuple(expected_runtimes))
            self.assertEqual(intent.groups, ())

    def test_default_prefill_honest_below_two(self):
        # 池 <2：预填产出的 intent 过不了 validate（与 default 路径
        # BLOCKED 同向诚实，零重复 BLOCKED 逻辑）。
        intent = default_composition_to_intent(_POOL[:1])
        error = validate_composition(intent)
        self.assertIsInstance(error, CompositionError)
        self.assertEqual(error.reason, "INVALID_MEMBER_COUNT")


class ImmutabilityTests(unittest.TestCase):
    """B：NamedTuple 不可变 + resolve 零输入突变。"""

    def test_fields_are_immutable(self):
        intent = _intent([("m1", "architect", "rt-alpha"),
                          ("m2", "coder", "rt-beta")])
        with self.assertRaises(AttributeError):
            intent.members = ()
        resolved = resolve_composition(intent, _POOL)
        with self.assertRaises(AttributeError):
            resolved.steps = ()

    def test_resolve_does_not_mutate_inputs(self):
        requests = {"m1": RuntimeBindingRequest("rt-alpha"),
                    "m2": RuntimeBindingRequest("rt-beta")}
        intent = CompositionIntent(
            members=(AgentSpec("m1", "architect"),
                     AgentSpec("m2", "coder")),
            binding_requests=requests, groups=())
        resolve_composition(intent, _POOL)
        self.assertEqual(
            requests,
            {"m1": RuntimeBindingRequest("rt-alpha"),
             "m2": RuntimeBindingRequest("rt-beta")})
        self.assertEqual(len(intent.members), 2)


class VocabularyTests(unittest.TestCase):
    """C：Role 封闭词表 + reason 封闭集。"""

    def test_role_vocabulary_closed_set(self):
        self.assertEqual(ROLE_VOCABULARY,
                         frozenset({"architect", "coder",
                                    "tester", "reviewer"}))

    def test_vocabulary_derived_from_frozen_templates(self):
        derived = frozenset(
            role for template in DEFAULT_ROLE_TEMPLATES.values()
            for role in template)
        self.assertEqual(ROLE_VOCABULARY, derived)

    def test_reasons_form_closed_set_with_entry_word(self):
        # RUNTIME_NOT_QUALIFIED 与 cockpit_entry 既有协议同词。
        for reason in ("UNKNOWN_ROLE", "INVALID_MEMBER_COUNT",
                       "INVALID_MEMBER_ID", "DUPLICATE_MEMBER_ID",
                       "BINDING_MEMBER_UNKNOWN", "MISSING_BINDING",
                       "INVALID_GROUP_ID", "DUPLICATE_GROUP_ID",
                       "GROUP_MEMBER_UNKNOWN",
                       "MEMBER_IN_MULTIPLE_GROUPS",
                       "RUNTIME_NOT_QUALIFIED"):
            self.assertIn(reason, COMPOSITION_ERROR_REASONS)


class ValidationMatrixTests(unittest.TestCase):
    """D-J：validate 全矩阵（结构门，零池参与）。"""

    def _reason(self, triples, groups=()):
        error = validate_composition(_intent(triples, groups))
        self.assertIsInstance(error, CompositionError)
        return error.reason

    def test_member_count_below_two_rejected(self):
        self.assertEqual(
            self._reason([("m1", "coder", "rt-alpha")]),
            "INVALID_MEMBER_COUNT")

    def test_member_count_above_four_rejected(self):
        triples = [(f"m{i}", "coder", "rt-alpha") for i in range(5)]
        self.assertEqual(self._reason(triples),
                         "INVALID_MEMBER_COUNT")

    def test_invalid_member_id_rejected(self):
        for bad in ("", "   "):
            with self.subTest(bad=bad):
                self.assertEqual(
                    self._reason([(bad, "coder", "rt-alpha"),
                                  ("m2", "coder", "rt-beta")]),
                    "INVALID_MEMBER_ID")

    def test_duplicate_member_id_rejected(self):
        self.assertEqual(
            self._reason([("m1", "architect", "rt-alpha"),
                          ("m1", "coder", "rt-beta")]),
            "DUPLICATE_MEMBER_ID")

    def test_unknown_role_rejected(self):
        self.assertEqual(
            self._reason([("m1", "hacker", "rt-alpha"),
                          ("m2", "coder", "rt-beta")]),
            "UNKNOWN_ROLE")

    def test_duplicate_role_valid(self):
        # ERRATA-2：duplicate Role = VALID。
        triples = [("m1", "coder", "rt-alpha"),
                   ("m2", "coder", "rt-beta")]
        self.assertIsNone(validate_composition(_intent(triples)))
        resolved = resolve_composition(_intent(triples), _POOL)
        self.assertEqual(tuple(b.role for b in resolved.bindings),
                         ("coder", "coder"))

    def test_binding_for_unknown_member_rejected(self):
        intent = CompositionIntent(
            members=(AgentSpec("m1", "architect"),
                     AgentSpec("m2", "coder")),
            binding_requests={"m1": RuntimeBindingRequest("rt-alpha"),
                              "ghost": RuntimeBindingRequest("rt-beta")},
            groups=())
        error = validate_composition(intent)
        self.assertEqual(error.reason, "BINDING_MEMBER_UNKNOWN")

    def test_missing_binding_rejected(self):
        intent = CompositionIntent(
            members=(AgentSpec("m1", "architect"),
                     AgentSpec("m2", "coder")),
            binding_requests={"m1": RuntimeBindingRequest("rt-alpha")},
            groups=())
        error = validate_composition(intent)
        self.assertEqual(error.reason, "MISSING_BINDING")

    def test_invalid_group_id_rejected(self):
        self.assertEqual(
            self._reason([("m1", "architect", "rt-alpha"),
                          ("m2", "coder", "rt-beta")],
                         groups=[_group("  ", ("m1",))]),
            "INVALID_GROUP_ID")

    def test_duplicate_group_id_rejected(self):
        self.assertEqual(
            self._reason([("m1", "architect", "rt-alpha"),
                          ("m2", "coder", "rt-beta")],
                         groups=[_group("g1", ("m1",)),
                                 _group("g1", ("m2",))]),
            "DUPLICATE_GROUP_ID")

    def test_group_unknown_member_rejected(self):
        self.assertEqual(
            self._reason([("m1", "architect", "rt-alpha"),
                          ("m2", "coder", "rt-beta")],
                         groups=[_group("g1", ("m1", "ghost"))]),
            "GROUP_MEMBER_UNKNOWN")

    def test_member_in_multiple_groups_rejected(self):
        self.assertEqual(
            self._reason([("m1", "architect", "rt-alpha"),
                          ("m2", "coder", "rt-beta")],
                         groups=[_group("g1", ("m1",)),
                                 _group("g2", ("m1", "m2"))]),
            "MEMBER_IN_MULTIPLE_GROUPS")

    def test_single_group_membership_valid(self):
        intent = _intent([("m1", "architect", "rt-alpha"),
                          ("m2", "coder", "rt-beta")],
                         groups=[_group("g1", ("m1", "m2"))])
        self.assertIsNone(validate_composition(intent))
        self.assertEqual(resolve_composition(intent, _POOL).groups,
                         (_group("g1", ("m1", "m2")),))


class ResolutionMatrixTests(unittest.TestCase):
    """K-N + G：池门、零替换、声明序。"""

    def test_unknown_runtime_rejected_with_verified_list(self):
        error = resolve_composition(
            _intent([("m1", "architect", "rt-omega"),
                     ("m2", "coder", "rt-beta")]),
            _POOL)
        self.assertIsInstance(error, CompositionError)
        self.assertEqual(error.reason, "RUNTIME_NOT_QUALIFIED")
        self.assertIn("rt-omega", error.detail)
        self.assertIn("rt-alpha", error.detail)  # verified 列表披露

    def test_unqualified_runtime_rejected_with_hint(self):
        # 池快照 = VERIFIED-only：在 registry 而未验证的 runtime 同样
        # ∉ 池——core 只见池，统一 RUNTIME_NOT_QUALIFIED（"在但不
        # 合格"与"不存在"的细分归入口 registry 第二道，M0 §7）。
        error = resolve_composition(
            _intent([("m1", "architect", "rt-alpha"),
                     ("m2", "coder", "rt-unverified")]),
            _POOL)
        self.assertEqual(error.reason, "RUNTIME_NOT_QUALIFIED")
        self.assertIn("rt-unverified", error.detail)

    def test_hint_is_qualify_original(self):
        import host_entry
        error = resolve_composition(
            _intent([("m1", "architect", "rt-missing"),
                     ("m2", "coder", "rt-beta")]),
            _POOL)
        self.assertEqual(error.hint, host_entry._HINT_QUALIFY)

    def test_validation_errors_surface_through_resolve(self):
        # resolve 内部先 validate：结构错误在池门之前拦截。
        error = resolve_composition(
            _intent([("m1", "hacker", "rt-alpha"),
                     ("m2", "coder", "rt-beta")]),
            _POOL)
        self.assertEqual(error.reason, "UNKNOWN_ROLE")

    def test_multiple_members_same_runtime_valid(self):
        # ERRATA-2：多 Agent → 同 Runtime = VALID。
        resolved = resolve_composition(
            _intent([("m1", "architect", "rt-alpha"),
                     ("m2", "coder", "rt-alpha")]),
            _POOL)
        self.assertEqual(
            tuple(b.runtime_id for b in resolved.bindings),
            ("rt-alpha", "rt-alpha"))
        self.assertEqual(resolved.member_ids, ("m1", "m2"))

    def test_zero_fallback_no_substitution(self):
        # 零替换：点名缺席 → REJECT；成功时绑定集恰=点名集（绝不含
        # 未点名 runtime，无任何 automatic alternative）。
        failing = resolve_composition(
            _intent([("m1", "architect", "rt-missing"),
                     ("m2", "coder", "rt-beta")]),
            _POOL)
        self.assertIsInstance(failing, CompositionError)
        succeeding = resolve_composition(
            _intent([("m1", "architect", "rt-gamma"),
                     ("m2", "coder", "rt-beta")]),
            _POOL)
        self.assertEqual(
            {b.runtime_id for b in succeeding.bindings},
            {"rt-gamma", "rt-beta"})

    def test_bindings_contain_only_requested_runtimes(self):
        resolved = resolve_composition(
            _intent([("m1", "architect", "rt-alpha"),
                     ("m2", "coder", "rt-alpha")]),
            _POOL)  # 池 4 项，仅点名 1 项
        self.assertEqual(len(resolved.bindings), 2)
        self.assertEqual(
            {b.runtime_id for b in resolved.bindings}, {"rt-alpha"})
        self.assertNotIn("rt-gamma",
                         tuple(b.runtime_id
                               for b in resolved.bindings))

    def test_steps_follow_declaration_order(self):
        # steps 唯一来源 = members 声明序（与 runtime_id 字典序无关）。
        resolved = resolve_composition(
            _intent([("m3", "reviewer", "rt-alpha"),
                     ("m1", "coder", "rt-gamma"),
                     ("m2", "architect", "rt-beta")]),
            _POOL)
        self.assertEqual(
            resolved.steps,
            (("reviewer", "rt-alpha"), ("coder", "rt-gamma"),
             ("architect", "rt-beta")))


class GroupPermutationTests(unittest.TestCase):
    """O：groups 置换 → steps/bindings 逐字节不变（六不等式钉死）。"""

    _TRIPLES = (("m1", "architect", "rt-alpha"),
                ("m2", "coder", "rt-beta"),
                ("m3", "reviewer", "rt-gamma"))

    def _resolve(self, groups):
        return resolve_composition(
            _intent(list(self._TRIPLES), groups=groups), _POOL)

    def test_group_permutation_leaves_steps_unchanged(self):
        variants = {
            "no-groups": (),
            "single-full": (_group("solo", ("m1", "m2", "m3")),),
            "two-partitions": (_group("left", ("m1", "m2")),
                               _group("right", ("m3",))),
            "renamed": (_group("zzz", ("m3", "m1")),
                        _group("aaa", ("m2",))),
        }
        baseline = self._resolve(variants["no-groups"])
        for name, groups in variants.items():
            with self.subTest(variant=name):
                resolved = self._resolve(groups)
                self.assertEqual(resolved.steps, baseline.steps)
                self.assertEqual(resolved.bindings, baseline.bindings)
                self.assertEqual(resolved.member_ids,
                                 baseline.member_ids)

    def test_steps_elements_are_role_runtime_pairs(self):
        resolved = self._resolve((_group("g", ("m1",)),))
        for step in resolved.steps:
            self.assertEqual(len(step), 2)
            role, runtime_id = step
            self.assertIn(role, ROLE_VOCABULARY)
            self.assertIsInstance(runtime_id, str)


class AuthorityBoundaryTests(unittest.TestCase):
    """P/Q：zero execution authority；configuration 非 execution state。"""

    def test_source_has_no_execution_authority_tokens(self):
        for token in ("execution_slots", "sequential_pipeline",
                      "cockpit_session", "control_boundary",
                      "control_journal", "_assemble_execution",
                      "RunState", "EventIndex", "UsageLog"):
            self.assertNotIn(token, CORE_SOURCE)

    def test_resolved_fields_are_configuration_only(self):
        self.assertEqual(ResolvedComposition._fields,
                         ("bindings", "member_ids", "groups", "steps"))
        for forbidden in ("plan", "slots", "session", "run_state"):
            self.assertNotIn(forbidden, ResolvedComposition._fields)

    def test_resolve_returns_value_objects_only(self):
        happy = resolve_composition(
            _intent([("m1", "architect", "rt-alpha"),
                     ("m2", "coder", "rt-beta")]), _POOL)
        sad = resolve_composition(
            _intent([("m1", "architect", "rt-none"),
                     ("m2", "coder", "rt-beta")]), _POOL)
        self.assertIsInstance(happy, ResolvedComposition)
        self.assertIsInstance(sad, CompositionError)


class PurityGuardTests(unittest.TestCase):
    """R/S/T：零 filesystem、零 runtime 调用、runtime-neutral。"""

    _VALID = _intent([("m1", "architect", "rt-alpha"),
                      ("m2", "coder", "rt-beta")])
    _INVALID = _intent([("m1", "architect", "rt-none"),
                        ("m2", "coder", "rt-beta")])

    def test_no_filesystem_access(self):
        with mock.patch("builtins.open",
                        side_effect=AssertionError("fs touched")):
            validate_composition(self._VALID)
            resolve_composition(self._VALID, _POOL)
            resolve_composition(self._INVALID, _POOL)
        for token in ("open(", "import os", "pathlib", "Path("):
            self.assertNotIn(token, CORE_SOURCE)

    def test_no_runtime_invocation(self):
        for token in ("subprocess", "Popen", "adapter", ".run("):
            self.assertNotIn(token, CORE_SOURCE)

    def test_runtime_neutral_static_guard(self):
        # 词边界匹配：pipeline/provider 等词内子串不误伤。
        pattern = re.compile(r"\b(claude|codex|pi|qwen|gemini)\b",
                             re.IGNORECASE)
        self.assertIsNone(pattern.search(CORE_SOURCE))


if __name__ == "__main__":
    unittest.main()
