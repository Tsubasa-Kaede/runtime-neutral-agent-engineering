"""2.8.0 Composition Core M2: Verified Pool ↔ Core contract tests.

真实机器对拍：真 AdapterRegistry/AdapterDescriptor、真 evidence_store
save/load、真 cockpit_entry._verified_pool、真 resolve_default_
composition ↔ composition_core 的 resolve/default 环。

REAL=0 边界：fixture CandidateValidationResult 的 provenance="REAL" 是
evidence 持久层 schema 的数据标记（save_evidence 只收 VERIFIED+REAL，
见 evidence_store :185-192），不是任何真实 provider invocation 的产物
——全部测试离线构造数据对象：零 runtime 调用、零 subprocess、零网络、
零 credentials。fixture runtime_id 全中性值（rt-*）。
"""
import re
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import cockpit_entry  # noqa: E402
from candidate_validation import (  # noqa: E402
    CandidateValidationResult,
    CandidateValidationStatus,
)
from composition_core import (  # noqa: E402
    AgentSpec,
    CollaborationGroupSpec,
    CompositionIntent,
    ResolvedComposition,
    RuntimeBindingRequest,
    default_composition_to_intent,
    resolve_composition,
    validate_composition,
)
from cockpit_entry import CompositionError  # noqa: E402
from evidence_store import load_evidence, save_evidence  # noqa: E402
from runtime_adapter_registry import (  # noqa: E402
    AdapterDescriptor,
    AdapterRegistry,
)

CORE_SOURCE = Path(
    sys.modules["composition_core"].__file__).read_text(encoding="utf-8")


def _descriptor(runtime_id, provider_id, model_id=None):
    """真注册单元（池路径零调用 factory——_verified_pool 不触碰）。"""
    return AdapterDescriptor(
        runtime_id=runtime_id,
        provider_id=provider_id,
        runtime_type="neutral",
        display_name=runtime_id,
        adapter_factory=lambda: None,
        model_id=model_id,
    )


_DESCRIPTORS = (
    _descriptor("rt-alpha", "prov-a"),
    _descriptor("rt-beta", "prov-b", model_id="m-b"),
    _descriptor("rt-gamma", "prov-c"),
    _descriptor("rt-delta", "prov-d"),
    _descriptor("rt-epsilon", "prov-e"),
)


def _verified_result(descriptor):
    """VERIFIED+REAL fixture（schema 数据标记，非真实调用证据）。"""
    return CandidateValidationResult(
        identity=descriptor.identity,
        status=CandidateValidationStatus.VERIFIED,
        gates_passed=frozenset(),
        gate_results=(),
        block_reason=None,
        failure_point=None,
        experiment_id="m2-contract-fixture",
        executed_at=1.0,
        provenance="REAL",
    )


def _registry(descriptors):
    registry = AdapterRegistry()
    for descriptor in descriptors:
        registry.register(descriptor)
    return registry


def _pool_via_evidence(base_dir, descriptors):
    """真 evidence → verified pool 链：save → load → _verified_pool。"""
    for descriptor in descriptors:
        save_evidence(base_dir, _verified_result(descriptor))
    evidence, rejected = load_evidence(base_dir)
    assert not rejected
    return cockpit_entry._verified_pool(_registry(descriptors), evidence)


def _intent(triples, groups=()):
    members = tuple(AgentSpec(m, r) for m, r, _ in triples)
    requests = {m: RuntimeBindingRequest(rt) for m, _, rt in triples}
    return CompositionIntent(members=members, binding_requests=requests,
                             groups=tuple(groups))


class SnapshotContractTests(unittest.TestCase):
    """1-2：真 descriptor 满足三属性 duck contract；core 零多余字段。"""

    def test_real_descriptor_satisfies_duck_contract(self):
        for descriptor in _DESCRIPTORS:
            with self.subTest(runtime_id=descriptor.runtime_id):
                self.assertTrue(hasattr(descriptor, "runtime_id"))
                self.assertTrue(hasattr(descriptor, "provider_id"))
                self.assertTrue(hasattr(descriptor, "identity"))
        # 真注册 descriptor 直接作池快照项喂 core：
        resolved = resolve_composition(
            _intent([("m1", "architect", "rt-alpha"),
                     ("m2", "coder", "rt-beta")]),
            _DESCRIPTORS)
        self.assertIsInstance(resolved, ResolvedComposition)

    def test_core_source_reads_no_extra_fields(self):
        # core 只许触 .runtime_id/.provider_id/.identity 三属性；资格/
        # health/adapter/注册细节字段全部不可达（静态钉死）。
        for token in (".model_id", ".config_fingerprint", ".status",
                      ".gates_passed", ".gate_results",
                      ".validated_capabilities", ".adapter_factory",
                      ".admitted_at", ".runtime_type", ".display_name",
                      "health"):
            self.assertNotIn(token, CORE_SOURCE)


class EvidencePoolPathTests(unittest.TestCase):
    """3/6/7：evidence → pool membership → resolve；协议保持。"""

    def test_evidence_to_pool_membership(self):
        with tempfile.TemporaryDirectory() as base_dir:
            pool = _pool_via_evidence(base_dir, _DESCRIPTORS[:3])
        self.assertEqual(pool, _DESCRIPTORS[:3])  # 同对象 canonical 序

    def test_pool_canonical_order_regardless_of_registration(self):
        shuffled = (_DESCRIPTORS[2], _DESCRIPTORS[0])  # gamma, alpha
        with tempfile.TemporaryDirectory() as base_dir:
            pool = _pool_via_evidence(base_dir, shuffled)
        self.assertEqual(tuple(d.runtime_id for d in pool),
                         ("rt-alpha", "rt-gamma"))

    def test_save_rejects_not_verified(self):
        result = CandidateValidationResult(
            identity=_DESCRIPTORS[0].identity,
            status=CandidateValidationStatus.NOT_VERIFIED,
            gates_passed=frozenset(), gate_results=(),
            block_reason=None, failure_point=None,
            experiment_id="m2-contract-fixture", executed_at=1.0,
            provenance="REAL")
        with tempfile.TemporaryDirectory() as base_dir:
            with self.assertRaises(ValueError):
                save_evidence(base_dir, result)

    def test_save_rejects_offline_provenance(self):
        # OFFLINE ≠ REAL 的持久层结构性实现：不升级、不落盘。
        result = CandidateValidationResult(
            identity=_DESCRIPTORS[0].identity,
            status=CandidateValidationStatus.VERIFIED,
            gates_passed=frozenset(), gate_results=(),
            block_reason=None, failure_point=None,
            experiment_id="m2-contract-fixture", executed_at=1.0,
            provenance="OFFLINE")
        with tempfile.TemporaryDirectory() as base_dir:
            with self.assertRaises(ValueError):
                save_evidence(base_dir, result)

    def test_missing_evidence_dir_is_empty_pool(self):
        with tempfile.TemporaryDirectory() as base_dir:
            missing = Path(base_dir) / "absent"
            evidence, rejected = load_evidence(missing)
            self.assertEqual(evidence, {})
            self.assertEqual(rejected, ())
            pool = cockpit_entry._verified_pool(
                _registry(_DESCRIPTORS[:2]), evidence)
            self.assertEqual(pool, ())

    def test_pool_membership_to_resolve_success(self):
        # 端到端：真 evidence→真 _verified_pool→core resolve。
        with tempfile.TemporaryDirectory() as base_dir:
            pool = _pool_via_evidence(base_dir, _DESCRIPTORS[:3])
        resolved = resolve_composition(
            _intent([("m1", "architect", "rt-beta"),
                     ("m2", "coder", "rt-alpha")]),
            pool)
        self.assertIsInstance(resolved, ResolvedComposition)
        self.assertEqual(
            tuple(b.runtime_id for b in resolved.bindings),
            ("rt-beta", "rt-alpha"))

    def test_identity_verbatim_from_real_descriptor(self):
        # identity 原值透传：model_id=None 与 "m-b" 的四元组原样进
        # binding，绝不折叠为 (runtime_id, provider_id)。
        with tempfile.TemporaryDirectory() as base_dir:
            pool = _pool_via_evidence(base_dir, _DESCRIPTORS[:2])
        resolved = resolve_composition(
            _intent([("m1", "architect", "rt-alpha"),
                     ("m2", "coder", "rt-beta")]),
            pool)
        self.assertEqual(resolved.bindings[0].canonical_runtime_identity,
                         _DESCRIPTORS[0].identity)  # model_id=None 原值
        self.assertEqual(resolved.bindings[1].canonical_runtime_identity,
                         _DESCRIPTORS[1].identity)  # model_id="m-b" 原值

    def test_runtime_not_qualified_protocol_unchanged(self):
        # 协议保持既有语义：与 cockpit_entry 既有用法同词、hint 同源。
        with tempfile.TemporaryDirectory() as base_dir:
            pool = _pool_via_evidence(base_dir, _DESCRIPTORS[:2])
        error = resolve_composition(
            _intent([("m1", "architect", "rt-zeta"),
                     ("m2", "coder", "rt-alpha")]),
            pool)
        self.assertIsInstance(error, CompositionError)
        self.assertEqual(error.reason, "RUNTIME_NOT_QUALIFIED")
        self.assertIn("rt-zeta", error.detail)
        self.assertIn("rt-beta", error.detail)  # verified 列表披露
        import host_entry
        self.assertEqual(error.hint, host_entry._HINT_QUALIFY)


class FailureMatrixTests(unittest.TestCase):
    """4-5：池缺席 truthful REJECT（registry 在场无 evidence 同拒）。"""

    def test_registry_present_without_evidence_rejected(self):
        # C 情形：descriptor 已注册但无 qualification evidence →
        # 不在池 → core 层统一 truthful REJECT（无 silent promotion）。
        with tempfile.TemporaryDirectory() as base_dir:
            save_evidence(base_dir, _verified_result(_DESCRIPTORS[0]))
            save_evidence(base_dir, _verified_result(_DESCRIPTORS[1]))
            evidence, rejected = load_evidence(base_dir)
            self.assertEqual(rejected, ())
            registry = _registry(_DESCRIPTORS[:3])
            pool = cockpit_entry._verified_pool(registry, evidence)
        self.assertEqual(len(pool), 2)
        error = resolve_composition(
            _intent([("m1", "architect", "rt-gamma"),
                     ("m2", "coder", "rt-alpha")]),
            pool)
        self.assertEqual(error.reason, "RUNTIME_NOT_QUALIFIED")

    def test_pool_absence_rejects_without_bindings(self):
        with tempfile.TemporaryDirectory() as base_dir:
            pool = _pool_via_evidence(base_dir, _DESCRIPTORS[:2])
        outcome = resolve_composition(
            _intent([("m1", "architect", "rt-nowhere"),
                     ("m2", "coder", "rt-beta")]),
            pool)
        self.assertIsInstance(outcome, CompositionError)
        # 零绑定产出：拒绝路径绝不部分装配。
        self.assertNotIsInstance(outcome, ResolvedComposition)


class NoFallbackTests(unittest.TestCase):
    """8-9：no fallback / no silent substitution（真池版五绝不）。"""

    def test_requested_absent_runtime_rejects_without_substitution(self):
        # requested = rt-alpha；verified pool = {rt-beta, rt-gamma}：
        # REJECT——绝不自动改绑/删 member/改 role/缩减 composition。
        pool = _DESCRIPTORS[1:3]
        intent = _intent([("m1", "architect", "rt-alpha"),
                          ("m2", "coder", "rt-beta")])
        outcome = resolve_composition(intent, pool)
        self.assertIsInstance(outcome, CompositionError)
        # 用户声明原样保留（resolve 不 repair/optimize/substitute）：
        self.assertEqual(intent.members,
                         (AgentSpec("m1", "architect"),
                          AgentSpec("m2", "coder")))
        self.assertEqual(
            intent.binding_requests,
            {"m1": RuntimeBindingRequest("rt-alpha"),
             "m2": RuntimeBindingRequest("rt-beta")})

    def test_no_silent_substitution_on_success(self):
        pool = _DESCRIPTORS  # 5 项
        resolved = resolve_composition(
            _intent([("m1", "architect", "rt-delta"),
                     ("m2", "coder", "rt-delta")]),
            pool)
        self.assertEqual(
            {b.runtime_id for b in resolved.bindings}, {"rt-delta"})
        self.assertEqual(len(resolved.bindings), 2)


class DomainValidityRealPoolTests(unittest.TestCase):
    """10-12：真池复钉 duplicate Role / 同 Runtime / Group 置换。"""

    def test_duplicate_role_valid_real_pool(self):
        resolved = resolve_composition(
            _intent([("m1", "coder", "rt-alpha"),
                     ("m2", "coder", "rt-beta")]),
            _DESCRIPTORS)
        self.assertEqual(tuple(b.role for b in resolved.bindings),
                         ("coder", "coder"))

    def test_multiple_members_same_runtime_valid_real_pool(self):
        resolved = resolve_composition(
            _intent([("m1", "architect", "rt-gamma"),
                     ("m2", "coder", "rt-gamma")]),
            _DESCRIPTORS)
        self.assertEqual(tuple(b.runtime_id for b in resolved.bindings),
                         ("rt-gamma", "rt-gamma"))

    def test_group_permutation_steps_unchanged_real_pool(self):
        triples = [("m1", "architect", "rt-alpha"),
                   ("m2", "coder", "rt-beta"),
                   ("m3", "reviewer", "rt-gamma")]
        variants = {
            "none": (),
            "full": (CollaborationGroupSpec("solo", ("m1", "m2", "m3")),),
            "split": (CollaborationGroupSpec("left", ("m1", "m2")),
                      CollaborationGroupSpec("right", ("m3",))),
        }
        baseline = resolve_composition(
            _intent(triples, variants["none"]), _DESCRIPTORS)
        for name, groups in variants.items():
            with self.subTest(variant=name):
                resolved = resolve_composition(
                    _intent(triples, groups), _DESCRIPTORS)
                self.assertEqual(resolved.steps, baseline.steps)
                self.assertEqual(resolved.bindings, baseline.bindings)


class PoolSizeMatrixTests(unittest.TestCase):
    """13 + 等值环：池 0/1/2/3/4/>4（真 descriptor 快照）。"""

    def test_pool_size_zero_and_one_honest_refusal(self):
        # 0/1：旧 default=BLOCKED、新环=INVALID_MEMBER_COUNT——错误词
        # 允许不同，只断言零绑定 + truthful 拒绝。
        for size in (0, 1):
            with self.subTest(size=size):
                pool = _DESCRIPTORS[:size]
                expected = cockpit_entry.resolve_default_composition(pool)
                self.assertIsNotNone(expected.blocked_reason)
                self.assertEqual(expected.bindings, ())
                intent = default_composition_to_intent(pool)
                error = validate_composition(intent)
                self.assertEqual(error.reason, "INVALID_MEMBER_COUNT")
                outcome = resolve_composition(intent, pool)
                self.assertIsInstance(outcome, CompositionError)

    def test_default_equality_ring(self):
        # 2/3/4/5(>4)：四字段 CompositionBinding 值等 + steps 同构 +
        # plan/slot 推导输入（i, role, runtime_id）逐位同构。
        for size in (2, 3, 4, 5):
            with self.subTest(size=size):
                pool = _DESCRIPTORS[:size]
                expected = cockpit_entry.resolve_default_composition(pool)
                self.assertIsNone(expected.blocked_reason)
                expected_bindings = expected.bindings
                self.assertLessEqual(len(expected_bindings), 4)  # >4 截断
                resolved = resolve_composition(
                    default_composition_to_intent(pool), pool)
                self.assertIsInstance(resolved, ResolvedComposition)
                self.assertEqual(resolved.bindings, expected_bindings)
                self.assertEqual(
                    resolved.steps,
                    tuple((b.role, b.runtime_id)
                          for b in expected_bindings))
                self.assertEqual(
                    tuple((i, b.role, b.runtime_id)
                          for i, b in enumerate(resolved.bindings)),
                    tuple((i, b.role, b.runtime_id)
                          for i, b in enumerate(expected_bindings)))


class PurityAuthorityTests(unittest.TestCase):
    """七：core 全链零执行权威；零 discovery/health/qualification 下探。"""

    def test_static_no_execution_authority_tokens(self):
        for token in ("execution_slots", "sequential_pipeline",
                      "cockpit_session", "control_boundary",
                      "control_journal", "_assemble_execution",
                      "RunState", "EventIndex", "UsageLog"):
            self.assertNotIn(token, CORE_SOURCE)

    def test_static_no_pool_machinery_imports(self):
        # core 零池构造知识：不 import discovery/health/evidence/
        # registry——池永远作为快照被传入。
        for token in ("runtime_discovery", "runtime_health",
                      "evidence_store", "environment_registry",
                      "AdapterRegistry", "adapter"):
            self.assertNotIn(token, CORE_SOURCE)

    def test_pool_chain_creates_no_execution_state(self):
        # 真链产物类型：池项=AdapterDescriptor，resolve 产物=值对象——
        # 零 slot/plan/session/事件/usage。
        with tempfile.TemporaryDirectory() as base_dir:
            pool = _pool_via_evidence(base_dir, _DESCRIPTORS[:2])
        for entry in pool:
            self.assertIsInstance(entry, AdapterDescriptor)
        resolved = resolve_composition(
            _intent([("m1", "architect", "rt-alpha"),
                     ("m2", "coder", "rt-beta")]),
            pool)
        self.assertIsInstance(resolved, ResolvedComposition)
        self.assertEqual(ResolvedComposition._fields,
                         ("bindings", "member_ids", "groups", "steps"))


class NeutralityTests(unittest.TestCase):
    """八：core 与 fixture 双侧 runtime-neutral。"""

    _PATTERN = re.compile(r"\b(claude|codex|pi|qwen|gemini)\b",
                          re.IGNORECASE)

    def test_core_and_fixtures_runtime_neutral(self):
        self.assertIsNone(self._PATTERN.search(CORE_SOURCE))
        for descriptor in _DESCRIPTORS:
            self.assertTrue(descriptor.runtime_id.startswith("rt-"))
            self.assertIsNone(
                self._PATTERN.search(descriptor.runtime_id))
            self.assertIsNone(
                self._PATTERN.search(descriptor.provider_id))


if __name__ == "__main__":
    unittest.main()
