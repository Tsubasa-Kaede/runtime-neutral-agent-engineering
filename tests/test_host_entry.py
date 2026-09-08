"""P1-U1: Default host composition root 的离线、确定性测试（TDD RED→GREEN）。

本文件先于 host_entry.py 存在（RED：模块缺席时 collection 失败）。
全部用 fake 家族 adapter 驱动：不调用真实 runtime、不访问网络、
不读取凭据、不开启 REAL gate。唯一的进程面（家族 from_environment）
以注入 factories 替代。

组合根目标链（全部复用既有件，本模块只做接线）：

    environment_registry（家族 from_environment → AdapterRegistry）
        ↓ observe_current_health（既有 GenericRuntimeHealth 时点观测）
    build_facade_from_bootstrap（既有 host.py "Automatic entry"：
        bootstrap_runtime_session → admitted → build_facade）
        ↓ 注入 cli.main._facade（既有注入面，cli.py 零修改）
    cli.main(argv)（既有 CLI）

锁定的边界（P1-U1 纪律）：
- 不创建第二套 runtime truth：无 Discovery/Selection/Executor/Adapter
  新类型；产物即既有 HostFacade；pool 身份来自 evidence（session 复用）
- cli.py 零修改：无 facade 时既有 "no facade configured" exit 2 行为
  原样保持
- 组合失败诚实退出（既有 RuntimeError 词表，exit 2 与 no-facade 同族；
  不发明新 exit-code 语义 —— P1-U3 范畴）
"""
import io
import json
import re
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))
REPO = Path(__file__).resolve().parents[1]

import evidence_store
import host_entry
import packet_forensics
from candidate_validation import (
    CandidateValidationResult,
    CandidateValidationStatus,
    GateResult,
    GateVerdict,
    ValidationGate,
)
from collaboration_packet import CollaborationPacket, CollaborationPayloadType
from collaboration_state import (
    CollaborationDirection,
    CollaborationRecord,
    CollaborationStateError,
    SharedCollaborationState,
)
from content_safety import (
    ValidationDiagnostic,
    next_diagnostic_generation,
    record_validation_diagnostic,
    reset_validation_diagnostic,
)
from structured_packets import (
    ArchitecturePacket,
    ImplementationPacket,
    ReviewPacket,
    TestPacket,
)
from external_runtime import (
    InvocationResult,
    InvocationStatus,
    InvocationTrace,
    RuntimeDiscovery,
    RuntimeProfile,
)
from production_facade import ProductionFacade
from runtime_status import (
    AuthenticationState,
    HealthEvidence,
    ReasonCode,
    RuntimeState,
)

CAPS_ALL = ("architecture", "coding", "review", "testing")

ARCH_P = {"task_id": "t", "role": "architect", "goal": ["g"], "constraints": ["c"],
          "architecture": ["a"], "interfaces": [{}], "implementation_steps": [{}],
          "acceptance_criteria": ["ac"], "risks": [{}]}
IMPL_P = {"task_id": "t", "role": "coder", "changed_files": ["f"],
          "implementation_summary": "s", "implementation_details": ["d"],
          "assumptions": [], "unresolved_items": [], "test_requirements": ["tr"]}
TEST_P = {"task_id": "t", "role": "tester", "tests_run": ["x"], "tests_passed": ["x"],
          "tests_failed": [], "failures": [], "coverage_or_validation": [],
          "remaining_risks": []}
REVIEW_P = {"task_id": "t", "role": "reviewer", "status": "PASS", "findings": [],
            "severity": [], "affected_files": [], "required_changes": [],
            "acceptance_criteria_status": []}


class FakeFamilyAdapter:
    """带 profile 的离线家族 adapter：四阶段全角色应答（rc3 先例同型）。

    invoke 按 agent_id 的 role 语义路由（与真实 adapter 同纪律：按请求
    内容应答，绝不按 runtime 名分支）。"""

    def __init__(self, runtime_id="rt-a", provider_id="provider-a",
                 fingerprint="default"):
        self.profile = RuntimeProfile(
            agent_id="coding-agent", runtime=runtime_id,
            provider=provider_id, model=None, role="coder",
            capabilities=frozenset())
        self.runtime_id = runtime_id
        self.provider_id = provider_id
        self.fingerprint = fingerprint

    def discover(self):
        return RuntimeDiscovery(self.runtime_id, True, "1.0", None, frozenset())

    def check_authentication(self):
        from runtime_health import AuthenticationCheck
        return AuthenticationCheck(AuthenticationState.AUTHENTICATED, "oauth")

    def check_provider_model(self):
        from runtime_health import ProviderModelCheck
        return ProviderModelCheck(self.provider_id, None, True, ReasonCode.NONE)

    def minimal_health_check(self, timeout_seconds):
        from runtime_health import MinimalHealthCheck
        return MinimalHealthCheck(True, ReasonCode.NONE, output_class="exact_ok")

    def invoke(self, request):
        if request.agent_id == self._identity_json():
            return self._packet(IMPL_P)
        for role, packet in (("architect", ARCH_P), ("coder", IMPL_P),
                             ("tester", TEST_P), ("reviewer", REVIEW_P)):
            if request.agent_id == role \
                    or request.agent_id.endswith(f',"{role}"]'):
                return self._packet(packet)
        return InvocationResult(
            InvocationStatus.SUCCESS, output="OK",
            trace=self._trace(request))

    def _identity_json(self):
        # agent_id_for 产紧凑分隔符（无空格）的 JSON 身份串。
        return json.dumps([self.runtime_id, self.provider_id, None,
                           self.fingerprint], separators=(",", ":"))

    @staticmethod
    def _packet(packet):
        return InvocationResult(
            InvocationStatus.SUCCESS, output=json.dumps(packet),
            trace=InvocationTrace(
                invocation_id="inv-f", task_id="t", agent_id="a",
                runtime="rt-a", provider=None, model=None, role=None,
                status=InvocationStatus.SUCCESS, started_at=0.0,
                finished_at=0.0, duration_ms=1, exit_code=0,
                input_tokens="unknown", output_tokens="unknown", error=None))

    @staticmethod
    def _trace(request):
        return InvocationTrace(
            invocation_id="inv-f2", task_id=request.task_id,
            agent_id=request.agent_id, runtime="rt-a", provider=None,
            model=None, role=request.role, status=InvocationStatus.SUCCESS,
            started_at=0.0, finished_at=0.0, duration_ms=1, exit_code=0,
            input_tokens="unknown", output_tokens="unknown", error=None)


class ProviderlessAdapter(FakeFamilyAdapter):
    """provider=None 的家族（如 L0 配置型）：identity 四元组要求非空
    provider —— 默认注册面必须诚实跳过，绝不代造身份。"""

    def __init__(self):
        super().__init__(runtime_id="rt-l0", provider_id=None)


def evidence_for(runtime_id="rt-a", provider_id="provider-a",
                 fingerprint="default"):
    return CandidateValidationResult(
        identity=(runtime_id, provider_id, None, fingerprint),
        status=CandidateValidationStatus.VERIFIED,
        gates_passed=frozenset(ValidationGate),
        gate_results=tuple(GateResult(g, GateVerdict.PASS)
                           for g in ValidationGate),
        block_reason=None, failure_point=None, experiment_id="exp-f",
        executed_at=0.0, validated_capabilities=CAPS_ALL,
        evidence={}, provenance="REAL")


def two_family_factories():
    adapter_a = FakeFamilyAdapter("rt-a")
    adapter_b = FakeFamilyAdapter("rt-b", "provider-b")
    return (lambda: adapter_a, lambda: adapter_b)


def two_family_evidence():
    return {
        ("rt-a", "provider-a", None, "default"): evidence_for("rt-a"),
        ("rt-b", "provider-b", None, "default"): evidence_for("rt-b", "provider-b"),
    }


def recording_qualifier(result_for=None):
    """离线 fake qualifier：按 instance 身份产确定性 VERIFIED+REAL 资格事实。

    calls 记录每次被调用的身份 —— 用于断言 run 路径绝不隐式 qualification
    （显式注入的 qualifier 也只在 qualify 分流里被调用）。"""
    calls = []

    def qualifier(instance):
        calls.append(instance.identity)
        if result_for is not None:
            return result_for(instance)
        return evidence_for(instance.runtime_id, instance.provider_id)

    qualifier.calls = calls
    return qualifier


class EnvironmentRegistryTests(unittest.TestCase):
    # -- 测试点 1/2：环境发现 → 注册（既有 AdapterRegistry/Descriptor） ----

    def test_available_families_are_registered_with_profile_identity(self):
        registry, skipped = host_entry.environment_registry(
            two_family_factories())

        descriptors = registry.list()
        self.assertEqual([d.runtime_id for d in descriptors], ["rt-a", "rt-b"])
        first = descriptors[0]
        self.assertEqual(first.provider_id, "provider-a")
        self.assertEqual(first.model_id, None)
        self.assertEqual(first.identity,
                         ("rt-a", "provider-a", None, "default"))
        # adapter_factory 交出的就是家族 from_environment 的实例
        self.assertIsInstance(first.adapter_factory(), FakeFamilyAdapter)

    def test_absent_family_is_honest_silence(self):
        # from_environment → None = 家族环境缺席：不注册、不报错、无
        # 半配置 descriptor（各家 adapter 的既有契约）。
        registry, skipped = host_entry.environment_registry(
            (lambda: None, lambda: FakeFamilyAdapter("rt-a")))
        self.assertEqual([d.runtime_id for d in registry.list()], ["rt-a"])
        self.assertEqual(skipped, ())

    def test_providerless_family_is_skipped_never_forged(self):
        registry, skipped = host_entry.environment_registry(
            (lambda: ProviderlessAdapter(),) + two_family_factories())
        self.assertEqual([d.runtime_id for d in registry.list()],
                         ["rt-a", "rt-b"])
        self.assertEqual(skipped, ("rt-l0",))

    def test_default_factory_table_covers_family_without_probing(self):
        # 默认家族表可枚举（8 家、均可调用），且枚举本身绝不执行探测
        # —— from_environment 只在 environment_registry 循环里被调用。
        factories = host_entry._default_factories()
        self.assertEqual(len(factories), 8)
        for factory in factories:
            self.assertTrue(callable(factory))


class ObserveHealthTests(unittest.TestCase):
    def test_current_health_observed_via_existing_pipeline(self):
        # current_health 来自既有 GenericRuntimeHealth 时点观测 —— 绝不
        # 凭空构造 READY（伪造快照）。
        registry, _ = host_entry.environment_registry(two_family_factories())
        health = host_entry.observe_current_health(registry)
        self.assertEqual(sorted(health), ["rt-a", "rt-b"])
        for status in health.values():
            self.assertEqual(status.status, RuntimeState.READY)

    def test_unavailable_candidate_reports_unavailable(self):
        class GoneAdapter(FakeFamilyAdapter):
            def discover(self):
                return RuntimeDiscovery("rt-a", False, None, "NOT_FOUND",
                                        frozenset())

        registry, _ = host_entry.environment_registry(
            (lambda: GoneAdapter(),))
        health = host_entry.observe_current_health(registry)
        self.assertEqual(health["rt-a"].status, RuntimeState.UNAVAILABLE)


class DefaultFacadeTests(unittest.TestCase):
    # -- 测试点 1：默认入口构造组合根 ----------------------------------------

    def test_default_facade_is_existing_host_facade(self):
        facade = host_entry.default_facade(
            factories=two_family_factories(),
            evidence=two_family_evidence())
        self.assertIsInstance(facade, ProductionFacade)

    def test_facade_pool_comes_from_session_evidence(self):
        # 不创建第二套 runtime truth：P1-1 后 admitted 全集（≥2）经既有
        # build_facade_from_agents 装进同一个 VerifiedRuntimePool —— 身份
        # = evidence 键（bootstrap_runtime_session 的复用语义，rc3 C4
        # 先例）；确定性排序（sorted identities）。
        facade = host_entry.default_facade(
            factories=two_family_factories(),
            evidence=two_family_evidence())
        identities = facade._orchestrator._pool.identities()
        self.assertEqual(identities, (
            ("rt-a", "provider-a", None, "default"),
            ("rt-b", "provider-b", None, "default")))

    def test_composition_order_registry_then_health_then_bootstrap(self):
        # 顺序证据：default_facade 先构造 registry 与 current_health，
        # 再 bootstrap（patch 记录调用序）；P1-1 起 bootstrap 在本层一次
        # 完成并按 admitted 基数分流组合。
        calls = []
        real_bootstrap = host_entry.bootstrap_runtime_session

        def spy_bootstrap(registry, **kwargs):
            calls.append(("bootstrap", sorted(
                d.runtime_id for d in registry.list()),
                sorted(host_entry.observe_current_health(registry))))
            return real_bootstrap(registry, **kwargs)

        with patch.object(host_entry, "bootstrap_runtime_session",
                          side_effect=spy_bootstrap):
            host_entry.default_facade(
                factories=two_family_factories(),
                evidence=two_family_evidence())

        self.assertEqual(calls, [("bootstrap", ["rt-a", "rt-b"],
                                  ["rt-a", "rt-b"])])

    def test_no_evidence_honest_failure(self):
        with self.assertRaises(RuntimeError) as caught:
            host_entry.default_facade(
                factories=two_family_factories(), evidence={})
        self.assertIn("NO_EVIDENCE_NO_QUALIFIER", str(caught.exception))

    def test_no_families_honest_failure(self):
        with self.assertRaises(RuntimeError) as caught:
            host_entry.default_facade(factories=(), evidence={})
        self.assertIn("NO RUNTIMES REGISTERED", str(caught.exception))


class MainEntryTests(unittest.TestCase):
    # -- 测试点 3/5：facade 注入 CLI + 入口行为一致 --------------------------

    def _run_main(self, argv):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = host_entry.main(
                argv, factories=two_family_factories(),
                evidence=two_family_evidence())
        return code, stdout.getvalue(), stderr.getvalue()

    def test_main_runs_cli_offline_four_stage(self):
        # 全链离线：注入双 fake 家族 → 既有 Automatic entry 取首个
        # admitted 构造单 runtime facade → --mode on + 复杂文本走
        # FOUR_STAGE 四阶段（先例文本，test_host_integration 同款）→
        # 既有 CLI 安全 JSON summary。（AUTO + 简单文本 → SINGLE 的既有
        # 语义由 test_cli_auto_simple_routes_single 先例锁定，不在此
        # 重复；默认 min=2 对单 runtime 的诚实 UNSATISFIED 见 R7-B。）
        code, out, err = self._run_main(
            ["run", "--min-runtimes", "1", "--mode", "on",
             "redesign architecture across modules"])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["status"], "SUCCESS")
        self.assertEqual(payload["path"], "FOUR_STAGE")
        self.assertEqual(payload["provenance"], "REAL")
        self.assertEqual(payload["stage_counts"],
                         {"architect": 1, "coder": 1,
                          "reviewer": 1, "tester": 1})

    def test_main_clears_injection_after_run(self):
        # 入口一次性：结束后不向同进程泄漏陈旧 facade。
        import cli as cli_module
        self._run_main(["run", "task"])
        self.assertIsNone(getattr(cli_module.main, "_facade", None))

    def test_composition_failure_exits_two_with_machine_json(self):
        # P1-U3 契约：组合期诚实拒绝（既有 RuntimeError 词表）→ stdout
        # 机器 JSON（status/reason/detail）+ stderr 人类诊断 + exit 2
        #（与用法错误/no-facade 同族，不发明新 exit-code 语义）。
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = host_entry.main(["run", "task"], factories=(),
                                   evidence={})
        self.assertEqual(code, 2)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["status"], "NOT_QUALIFIED")
        self.assertEqual(payload["reason"], "NO RUNTIMES REGISTERED")
        self.assertIn("NO RUNTIMES REGISTERED", payload["detail"])
        self.assertIn("dual-agent", stderr.getvalue())  # 人类行

    def test_cli_main_without_facade_behavior_unchanged(self):
        # 测试点 4：cli.py 零修改 —— 无 facade 的既有行为原样保持。
        import cli as cli_module
        previous = getattr(cli_module.main, "_facade", None)
        cli_module.main._facade = None
        try:
            stdout, stderr = io.StringIO(), io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = cli_module.main(["run", "task"])
        finally:
            cli_module.main._facade = previous
        self.assertEqual(code, 2)
        self.assertIn("no facade configured", stderr.getvalue())


class QualifySurfaceTests(unittest.TestCase):
    # -- 覆盖 16/17：dual-agent qualify 显式资格面 ------------------------

    def _qualify(self, tmp, *, factories=None, qualifier=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = host_entry.main(
                ["qualify"],
                factories=(factories if factories is not None
                           else two_family_factories()),
                qualifier=qualifier, base_dir=tmp)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_qualify_explicit_surface_persists_and_reports(self):
        # 16: 显式 qualify → 既有 bootstrap 编排（evidence 复用/qualifier
        # 注入位）→ VERIFIED+REAL 落盘 → stdout 安全 JSON summary。
        with tempfile.TemporaryDirectory() as tmp:
            qualifier = recording_qualifier()
            code, out, err = self._qualify(tmp, qualifier=qualifier)
            self.assertEqual(code, 0, err)
            payload = json.loads(out)
            self.assertEqual(payload["command"], "qualify")
            self.assertEqual(payload["status"], "QUALIFIED")
            self.assertEqual(payload["qualification_count"], 2)
            self.assertEqual(payload["admitted"],
                             [["rt-a", "provider-a", None, "default"],
                              ["rt-b", "provider-b", None, "default"]])
            self.assertEqual(len(payload["saved_files"]), 2)
            self.assertEqual(len(qualifier.calls), 2)
            self.assertEqual(len(list(Path(tmp).glob("*.json"))), 2)

    def test_qualify_persists_loadable_evidence(self):
        # 17: fake qualifier → 落盘文件可被 load_evidence 原样读回
        #     （持久化 = 可跨进程消费的 durable projection）。
        with tempfile.TemporaryDirectory() as tmp:
            code, _, err = self._qualify(tmp, qualifier=recording_qualifier())
            self.assertEqual(code, 0, err)
            evidence, rejected = evidence_store.load_evidence(tmp)
            self.assertEqual(rejected, ())
            self.assertEqual(sorted(evidence),
                             [("rt-a", "provider-a", None, "default"),
                              ("rt-b", "provider-b", None, "default")])
            for result in evidence.values():
                self.assertIs(result.status,
                              CandidateValidationStatus.VERIFIED)
                self.assertEqual(result.provenance, "REAL")
                self.assertEqual(result.validated_capabilities, CAPS_ALL)

    def test_default_real_bridge_provides_nonempty_experiment_id(self):
        # P1 BUGFIX 特征测试（2.2.0 用户态 REAL E2E 实测缺陷）：
        # 生产默认 qualifier（_real_bridge → run_real_validation）必须为
        # qualification 提供非空 experiment_id —— verified_selection_bridge
        # 对空 experiment_id 的池条目无条件滤除，落盘证据将无法被任何
        # selection 路径消费（DUAL_NO_CAPABLE_AGENT / NO_CAPABLE_AGENT）。
        # patch 掉真实 REAL 调用面，只验证接线契约（零 runtime/网络/凭据）。
        captured = []

        def stub_run_real_validation(instance, probe, **kwargs):
            captured.append(kwargs.get("experiment_id"))
            return (evidence_for(instance.runtime_id,
                                 instance.provider_id), object())

        with patch.object(host_entry, "run_real_validation",
                          stub_run_real_validation):
            with tempfile.TemporaryDirectory() as tmp:
                code, out, err = self._qualify(tmp)  # qualifier=None → 默认桥
        self.assertEqual(code, 0, err)
        self.assertEqual(len(captured), 2)  # 两个家族各一次 qualification
        for experiment_id in captured:
            self.assertIsNotNone(experiment_id)
            self.assertNotEqual(experiment_id, "")

    def test_qualify_offline_verified_not_saved_not_admitted(self):
        # 门未开（OFFLINE provenance）：诚实不落盘、不进池 —— 持久层
        # 绝不升级 provenance，admission 语义原样拒绝。
        def offline_for(instance):
            return CandidateValidationResult(
                identity=instance.identity,
                status=CandidateValidationStatus.VERIFIED,
                gates_passed=frozenset(ValidationGate),
                gate_results=tuple(GateResult(g, GateVerdict.PASS)
                                   for g in ValidationGate),
                block_reason=None, failure_point=None,
                experiment_id="exp-off", executed_at=0.0,
                validated_capabilities=CAPS_ALL, evidence={},
                provenance="OFFLINE")

        with tempfile.TemporaryDirectory() as tmp:
            code, out, err = self._qualify(
                tmp, qualifier=recording_qualifier(result_for=offline_for))
            self.assertEqual(code, 2)
            payload = json.loads(out)
            self.assertEqual(payload["status"], "NOT_QUALIFIED")
            self.assertEqual(payload["saved_files"], [])
            self.assertEqual(list(Path(tmp).glob("*.json")), [])
            self.assertIn("provenance=OFFLINE", out)  # 逐 entry 诚实呈现

    def test_qualify_without_families_honest_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, out, err = self._qualify(tmp, factories=())
            self.assertEqual(code, 2)
            payload = json.loads(out)
            self.assertEqual(payload["status"], "NOT_QUALIFIED")
            self.assertEqual(payload["reason"], "NO RUNTIMES REGISTERED")
            self.assertIn("dual-agent", err)  # 人类行在 stderr

    def test_qualify_rejects_extra_arguments(self):
        with tempfile.TemporaryDirectory() as tmp:
            stdout, stderr = io.StringIO(), io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = host_entry.main(["qualify", "--bogus"],
                                       factories=two_family_factories(),
                                       base_dir=tmp)
            self.assertEqual(code, 2)
            self.assertEqual(stdout.getvalue(), "")
            payload = json.loads(stderr.getvalue())
            self.assertEqual(payload["error"], "unsupported qualify arguments")


class QualifyObservabilityTests(unittest.TestCase):
    """CU-P0：`qualify --timeout-seconds` 与 stderr progress 的 CLI 契约。

    stdout 单 JSON summary 契约（P1-U3）不变：进度行只走 stderr；
    拒绝路径沿用既有 exit 2 + stderr JSON + stdout 空；默认 300.0 与
    既有执行语义零漂移。"""

    def _qualify(self, argv_extra, tmp, *, factories=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = host_entry.main(
                ["qualify"] + argv_extra,
                factories=(factories if factories is not None
                           else two_family_factories()),
                base_dir=tmp)
        return code, stdout.getvalue(), stderr.getvalue()

    def _bridge_stub(self, captured, lines=()):
        """默认 REAL 桥的离线替身：捕获 timeout 透传 + 回放进度行。"""

        def stub(instance, probe, **kwargs):
            captured.append(kwargs.get("timeout_seconds"))
            progress = kwargs.get("progress")
            if progress is not None:
                for line in lines:
                    progress(line)
            return (evidence_for(instance.runtime_id,
                                 instance.provider_id), object())

        return stub

    def test_default_timeout_remains_300_seconds(self):
        self.assertEqual(host_entry.DEFAULT_TIMEOUT_SECONDS, 300.0)

    def test_space_form_flag_reaches_bridge_as_exact_float(self):
        captured = []
        with patch.object(host_entry, "run_real_validation",
                          self._bridge_stub(captured)):
            with tempfile.TemporaryDirectory() as tmp:
                code, out, err = self._qualify(["--timeout-seconds", "60"], tmp)
        self.assertEqual(code, 0, err)
        self.assertEqual(captured, [60.0, 60.0])  # 两个 fake 家族各一次
        self.assertIsInstance(captured[0], float)

    def test_equals_form_flag_reaches_bridge(self):
        captured = []
        with patch.object(host_entry, "run_real_validation",
                          self._bridge_stub(captured)):
            with tempfile.TemporaryDirectory() as tmp:
                code, out, err = self._qualify(["--timeout-seconds=60"], tmp)
        self.assertEqual(code, 0, err)
        self.assertEqual(captured, [60.0, 60.0])

    def test_no_flag_uses_default_300(self):
        captured = []
        with patch.object(host_entry, "run_real_validation",
                          self._bridge_stub(captured)):
            with tempfile.TemporaryDirectory() as tmp:
                code, out, err = self._qualify([], tmp)
        self.assertEqual(code, 0, err)
        self.assertEqual(captured, [300.0, 300.0])

    def _assert_rejected(self, argv_extra, expected_error):
        with tempfile.TemporaryDirectory() as tmp:
            code, out, err = self._qualify(argv_extra, tmp)
        self.assertEqual(code, 2)
        self.assertEqual(out, "")  # 拒绝路径 stdout 保持空（既有契约）
        payload = json.loads(err)
        self.assertEqual(payload["error"], expected_error)

    def test_duplicate_flag_rejected(self):
        self._assert_rejected(
            ["--timeout-seconds", "60", "--timeout-seconds", "30"],
            "duplicate --timeout-seconds")

    def test_zero_rejected(self):
        self._assert_rejected(["--timeout-seconds", "0"],
                              "invalid --timeout-seconds")

    def test_negative_rejected(self):
        self._assert_rejected(["--timeout-seconds", "-5"],
                              "invalid --timeout-seconds")

    def test_non_numeric_rejected(self):
        self._assert_rejected(["--timeout-seconds", "abc"],
                              "invalid --timeout-seconds")

    def test_missing_value_rejected(self):
        self._assert_rejected(["--timeout-seconds"],
                              "missing --timeout-seconds value")

    def test_unknown_argument_with_flag_still_rejected(self):
        self._assert_rejected(
            ["--timeout-seconds", "60", "--bogus"],
            "unsupported qualify arguments")

    def test_progress_streams_to_stderr_stdout_stays_single_json(self):
        captured = []
        lines = ("dual-agent: qualify G5 minimal invocation starting (timeout=60s)",
                 "dual-agent: qualify G5 minimal invocation completed (0.0s)")
        with patch.object(host_entry, "run_real_validation",
                          self._bridge_stub(captured, lines)):
            with tempfile.TemporaryDirectory() as tmp:
                code, out, err = self._qualify(
                    ["--timeout-seconds", "60"], tmp)
        self.assertEqual(code, 0)
        payload = json.loads(out)  # stdout 仍恰一行合法 JSON
        self.assertEqual(payload["command"], "qualify")
        self.assertEqual(out.count("\n"), 1)
        self.assertIn("starting (timeout=60s)", err)
        self.assertIn("completed (0.0s)", err)
        self.assertNotIn("qualify G5", out)

    def test_offline_default_bridge_emits_no_invocation_progress(self):
        # gate 关闭的真实 offline 链（不打桩）：G5 在 invoke 之前 BLOCKED
        # —— 不产生 REAL invocation，也不产生假的 starting 事件。
        with tempfile.TemporaryDirectory() as tmp:
            code, out, err = self._qualify([], tmp)
        self.assertEqual(code, 2)
        payload = json.loads(out)
        self.assertEqual(payload["status"], "NOT_QUALIFIED")
        self.assertNotIn("qualify G5", err)
        self.assertNotIn("qualify G14", err)
        self.assertNotIn("qualify G", out)


class RunPersistenceTests(unittest.TestCase):
    # -- 覆盖 18/19/20/21/22：run 只读盘，绝不隐式 qualification ---------

    def _run(self, argv, *, factories, base_dir, qualifier=None,
             evidence=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = host_entry.main(argv, factories=factories,
                                   base_dir=base_dir, qualifier=qualifier,
                                   evidence=evidence)
        return code, stdout.getvalue(), stderr.getvalue()

    def _qualify_into(self, tmp, factories):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = host_entry.main(["qualify"], factories=factories,
                                   qualifier=recording_qualifier(),
                                   base_dir=tmp)
        self.assertEqual(code, 0, stderr.getvalue())

    def test_new_process_run_loads_persisted_evidence(self):
        # 18: 新进程模拟 —— qualify（A）与 run（B）两个独立 main 调用之间
        #     零内存态（无 evidence 注入、无 qualifier）：组合只能来自盘。
        with tempfile.TemporaryDirectory() as tmp:
            self._qualify_into(tmp, two_family_factories())
            code, out, err = self._run(
                ["run", "--min-runtimes", "1", "--mode", "on",
                 "redesign architecture across modules"],
                factories=two_family_factories(), base_dir=tmp)
            self.assertEqual(code, 0, err)
            payload = json.loads(out)
            self.assertEqual(payload["status"], "SUCCESS")
            self.assertEqual(payload["path"], "FOUR_STAGE")
            self.assertEqual(payload["provenance"], "REAL")

    def test_run_consumes_persisted_evidence_subset(self):
        # 19: 盘上只有 rt-a 的资格 → 双家族 run 消费 rt-a 证据；
        #     rt-b 保持诚实 NOT_QUALIFIED（绝不隐式补资格）。
        with tempfile.TemporaryDirectory() as tmp:
            self._qualify_into(tmp, (lambda: FakeFamilyAdapter("rt-a"),))
            code, out, err = self._run(
                ["run", "--min-runtimes", "1", "--mode", "on",
                 "redesign architecture across modules"],
                factories=two_family_factories(), base_dir=tmp)
            self.assertEqual(code, 0, err)
            self.assertEqual(json.loads(out)["status"], "SUCCESS")

    def test_run_without_evidence_never_invokes_qualifier(self):
        # 20: 无证据时 run 绝不隐式 qualification —— 即便显式注入
        #     qualifier 也不会被 run 路径调用；诚实 NO_EVIDENCE_NO_QUALIFIER
        #     + 指引 dual-agent qualify（P1-U3：机器 JSON 在 stdout）。
        with tempfile.TemporaryDirectory() as tmp:
            spy = recording_qualifier()
            code, out, err = self._run(["run", "task"],
                                       factories=two_family_factories(),
                                       base_dir=tmp, qualifier=spy)
            self.assertEqual(code, 2)
            self.assertEqual(spy.calls, [])
            payload = json.loads(out)
            self.assertEqual(payload["status"], "NOT_QUALIFIED")
            self.assertEqual(payload["reason"], "NO_EVIDENCE_NO_QUALIFIER")
            self.assertIn("dual-agent qualify", payload["hint"])
            self.assertIn("dual-agent qualify", err)  # 人类行在 stderr

    def test_run_single_runtime_default_min_two_honest_unsatisfied(self):
        # 21: 单 runtime + 默认 min=2 → 照常最优指派 + observation 旁路
        #     诚实标注 POLICY_COUNT_UNSATISFIED（绝不伪造第二 runtime）。
        with tempfile.TemporaryDirectory() as tmp:
            factories = (lambda: FakeFamilyAdapter("rt-a"),)
            self._qualify_into(tmp, factories)
            code, out, err = self._run(
                ["run", "--observe", "--mode", "on",
                 "redesign architecture across modules"],
                factories=factories, base_dir=tmp)
            self.assertEqual(code, 0, err)
            payload = json.loads(out)
            self.assertEqual(payload["status"], "SUCCESS")
            self.assertIn("POLICY_COUNT_UNSATISFIED", err)

    def test_run_with_injected_evidence_skips_disk(self):
        # 22: 既有 P1-U1 注入面原样 —— 显式 evidence 优先，空盘不覆盖。
        with tempfile.TemporaryDirectory() as tmp:
            code, out, err = self._run(
                ["run", "--min-runtimes", "1", "--mode", "on",
                 "redesign architecture across modules"],
                factories=two_family_factories(), base_dir=tmp,
                evidence=two_family_evidence())
            self.assertEqual(code, 0, err)
            self.assertEqual(json.loads(out)["path"], "FOUR_STAGE")


class CliContractStabilityTests(unittest.TestCase):
    """P1-U3：CLI 语义稳定 —— stdout=机器 JSON / stderr=人类诊断 / exit 稳定。"""

    def _main(self, argv, *, factories, base_dir, qualifier=None,
              evidence=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = host_entry.main(argv, factories=factories,
                                   base_dir=base_dir, qualifier=qualifier,
                                   evidence=evidence)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_run_no_evidence_machine_json_stdout_human_hint_stderr(self):
        # §7/§8.B：无 evidence → stdout 机器 JSON（status/reason/hint），
        # stderr 人类提示；exit 2；qualifier 绝不被调用。
        with tempfile.TemporaryDirectory() as tmp:
            spy = recording_qualifier()
            code, out, err = self._main(["run", "task"],
                                        factories=two_family_factories(),
                                        base_dir=tmp, qualifier=spy)
            self.assertEqual(code, 2)
            self.assertEqual(spy.calls, [])
            payload = json.loads(out)
            self.assertEqual(payload["status"], "NOT_QUALIFIED")
            self.assertEqual(payload["reason"], "NO_EVIDENCE_NO_QUALIFIER")
            self.assertIn("dual-agent qualify", payload["hint"])
            # stderr 只有人类行：包含指引、绝无机器 JSON。
            self.assertIn("dual-agent qualify", err)
            self.assertNotIn('"status"', err)
            self.assertNotIn('"reason"', err)

    def test_run_no_runtimes_machine_json_reason_word(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, out, err = self._main(["run", "task"], factories=(),
                                        base_dir=tmp)
            self.assertEqual(code, 2)
            payload = json.loads(out)
            self.assertEqual(payload["status"], "NOT_QUALIFIED")
            self.assertEqual(payload["reason"], "NO RUNTIMES REGISTERED")

    def test_version_works_on_fresh_machine(self):
        # 新机（无证据、无家族）上 --version 必须可用：argparse 在组合
        # 之前处理（cli.main 既有 parse-first 先例），单一版本 truth。
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit) as ctx:
                self._main(["--version"], factories=(), base_dir=tmp)
            self.assertEqual(ctx.exception.code, 0)

    def test_top_level_help_is_product_help(self):
        # P1-U4 §6：顶层 --help/-h 是产品级帮助（host_entry 层；cli.py
        # 冻结零改）：exit 0、无证据/无发现/无组合；明确列出 run、
        # qualify、--observe；说明 run 绝不自动 qualify。
        with tempfile.TemporaryDirectory() as tmp:
            for flag in ("--help", "-h"):
                with self.subTest(flag=flag):
                    code, out, err = self._main([flag], factories=(),
                                                base_dir=tmp)
                    self.assertEqual(code, 0, flag)
                    self.assertEqual(err, "", flag)
                    self.assertIn("dual-agent run", out)
                    self.assertIn("dual-agent qualify", out)
                    self.assertIn("--observe", out)
                    self.assertIn("never automatically qualifies", out)

    def test_qualify_help_shows_product_help(self):
        # `qualify --help`：同产品帮助（不是 unsupported-arguments 错误）。
        with tempfile.TemporaryDirectory() as tmp:
            code, out, err = self._main(["qualify", "--help"], factories=(),
                                        base_dir=tmp)
            self.assertEqual(code, 0)
            self.assertEqual(err, "")
            self.assertIn("dual-agent qualify", out)

    def test_run_subcommand_help_works_on_fresh_machine(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit) as ctx:
                self._main(["run", "--help"], factories=(), base_dir=tmp)
            self.assertEqual(ctx.exception.code, 0)

    def test_run_observe_keeps_stdout_single_machine_json_line(self):
        # §14：observation 只去 stderr；stdout 恰一行合法 JSON。
        with tempfile.TemporaryDirectory() as tmp:
            factories = (lambda: FakeFamilyAdapter("rt-a"),)
            stdout, stderr = io.StringIO(), io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = host_entry.main(
                    ["qualify"], factories=factories,
                    qualifier=recording_qualifier(), base_dir=tmp)
            self.assertEqual(code, 0)
            code, out, err = self._main(
                ["run", "--observe", "--min-runtimes", "1", "--mode", "on",
                 "redesign architecture across modules"],
                factories=factories, base_dir=tmp)
            self.assertEqual(code, 0, err)
            self.assertEqual(out.count("\n"), 1)
            payload = json.loads(out)
            self.assertEqual(payload["status"], "SUCCESS")
            self.assertIn("DECISION", err)  # observation 在 stderr
            self.assertNotIn("DECISION", out)

    def test_policy_unsatisfied_not_mislabeled_as_no_evidence(self):
        # §8.C：policy count 不满足必须保持 POLICY_COUNT_UNSATISFIED 语义
        # —— 绝不误映射成 NO_EVIDENCE。当前 facade 封闭结果不含 policy
        # 字段（R7-A3：reason 只经 DECISION observation 呈现）→ 运行照常
        # 完成且 exit 0，诚实 reason 经 --observe 可证（见下方 observe 断言）。
        with tempfile.TemporaryDirectory() as tmp:
            factories = (lambda: FakeFamilyAdapter("rt-a"),)
            stdout, stderr = io.StringIO(), io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = host_entry.main(
                    ["qualify"], factories=factories,
                    qualifier=recording_qualifier(), base_dir=tmp)
            self.assertEqual(code, 0)
            code, out, err = self._main(
                ["run", "--observe", "--mode", "on",
                 "redesign architecture across modules"],
                factories=factories, base_dir=tmp)
            self.assertEqual(code, 0)
            payload = json.loads(out)
            self.assertEqual(payload["status"], "SUCCESS")
            self.assertNotIn("NO_EVIDENCE", out)
            self.assertIn("POLICY_COUNT_UNSATISFIED", err)


class FailingInvokeAdapter(FakeFamilyAdapter):
    """健康面全好但 invoke 恒失败：驱动真实执行链的诚实失败终态
    （P1-U3 exit 映射的端到端证据，绝不 stub 执行结果）。"""

    def invoke(self, request):
        return InvocationResult(
            InvocationStatus.FAILED, output="",
            trace=self._trace(request))


class ExitContractTests(unittest.TestCase):
    """P1-U3：封闭词表 → exit code 的稳定映射（纯呈现，无新 truth）。

    映射住在 host_entry（产品边界组合根）而非 cli.py —— cli.py 是 V2
    冻结件（五个 zero-diff 纪律测试钉定工作树零修改），console script
    与 python -m 的入口本来就是本层。"""

    def test_exit_code_for_success_word_is_zero(self):
        self.assertEqual(host_entry.exit_code_for("SUCCESS"), 0)

    def test_exit_code_for_every_closed_failure_word_is_two(self):
        # 三个状态枚举（ExecutionStatus / CollaborationStatus /
        # VerificationStatus）+ facade 两特例词的全失败词表。
        for word in ("FAILED",
                     "ARCHITECT_INVOKE_FAILED", "ARCHITECT_PACKET_INVALID",
                     "CODER_INVOKE_FAILED", "CODER_PACKET_INVALID",
                     "TRANSPORT_FAILED", "CORRELATION_MISMATCH",
                     "BUDGET_EXHAUSTED", "LOOP_GUARD_REJECTED",
                     "TESTER_INVOKE_FAILED", "TESTER_PACKET_INVALID",
                     "REVIEWER_INVOKE_FAILED", "REVIEWER_PACKET_INVALID",
                     "MISSING_HANDOFF",
                     "DUAL_NO_CAPABLE_AGENT", "NO_VERIFICATION_CAPABILITY"):
            self.assertEqual(host_entry.exit_code_for(word), 2, word)

    def _qualify_one(self, tmp, factory):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = host_entry.main(["qualify"], factories=(factory,),
                                   qualifier=recording_qualifier(),
                                   base_dir=tmp)
        self.assertEqual(code, 0, stderr.getvalue())

    def test_run_execution_failure_returns_two_end_to_end(self):
        # §8.D：真实执行链的诚实失败终态（架构阶段 invoke 失败）→
        # stdout 机器 JSON（封闭失败词）+ exit 2；绝不伪装成功。
        with tempfile.TemporaryDirectory() as tmp:
            factory = lambda: FailingInvokeAdapter("rt-a")
            self._qualify_one(tmp, factory)
            stdout, stderr = io.StringIO(), io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = host_entry.main(
                    ["run", "--min-runtimes", "1", "--mode", "on",
                     "redesign architecture across modules"],
                    factories=(factory,), base_dir=tmp)
            self.assertEqual(code, 2)
            self.assertEqual(stdout.getvalue().count("\n"), 1)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["status"], "ARCHITECT_INVOKE_FAILED")
            self.assertEqual(payload["failure_category"],
                             "ARCHITECT_INVOKE_FAILED")
            self.assertNotIn("Traceback", stdout.getvalue())
            # 无 --observe 时零 observation 事件（P1-U3 本意）；CU-R1 起
            # stderr 额外携带恰好一行诚实的未交付投影（Case B 契约）。
            self.assertNotIn("DECISION", stderr.getvalue())
            self.assertNotIn("INVOCATION", stderr.getvalue())
            self.assertEqual(
                stderr.getvalue(),
                "dual-agent: result not delivered: ARCHITECT_INVOKE_FAILED\n")

    def test_run_success_output_is_canonical_single_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            factory = (lambda: FakeFamilyAdapter("rt-a"),)
            self._qualify_one(tmp, factory[0])
            stdout, stderr = io.StringIO(), io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = host_entry.main(
                    ["run", "--min-runtimes", "1", "--mode", "on",
                     "redesign architecture across modules"],
                    factories=factory, base_dir=tmp)
            self.assertEqual(code, 0, stderr.getvalue())
            out = stdout.getvalue()
            self.assertEqual(out.count("\n"), 1)
            payload = json.loads(out)
            self.assertEqual(payload["status"], "SUCCESS")
            self.assertEqual(
                out, json.dumps(payload, sort_keys=True,
                                separators=(",", ":")) + "\n")
            lowered = out.lower()
            for marker in ("token", "secret", "api_key", "authorization",
                           "bearer", "stdout", "stderr"):
                self.assertNotIn(marker, lowered, marker)


class BoundaryDisciplineTests(unittest.TestCase):
    # -- 测试点 6/7：不创建第二套 truth；链路全用既有实现 ---------------------

    def test_module_defines_no_second_runtime_truth(self):
        # 公开面（__all__）只有组合函数与常量：绝不出现新的 Discovery/
        # Selection/Executor/Adapter/Orchestrator 类型。
        self.assertEqual(
            set(host_entry.__all__),
            {"DEFAULT_TIMEOUT_SECONDS", "DEFAULT_EVIDENCE_DIR",
             "environment_registry", "observe_current_health",
             "default_facade", "qualify_runtimes", "exit_code_for", "main"})
        source = Path(SCRIPTS, "host_entry.py").read_text(encoding="utf-8")
        for forbidden in ("class ", "VerifiedOrchestrator(",
                          "CollaborationSession(", "CollaborationOrchestrator(",
                          "ProductionFacade("):
            self.assertNotIn(forbidden, source, forbidden)

    def test_chain_reuses_existing_modules_by_import(self):
        # 接线证据：依赖恰好是既有组合件（host 的 Automatic entry、通用
        # 健康管线、候选发现、注册表）—— 不 import 任何执行栈内部。
        # P1-U2b 起 qualify 面正向复用 bootstrap_runtime_session +
        # run_real_validation（instance.probe 桥 = rc3/gemini REAL 驱动
        # 同款），以及持久层 evidence_store 的 save/load —— 全部既有/授权件。
        source = Path(SCRIPTS, "host_entry.py").read_text(encoding="utf-8")
        for required in ("build_facade_from_bootstrap",
                         "GenericRuntimeHealth",
                         "RuntimeCandidateDiscovery",
                         "discovery_sources",
                         "bootstrap_runtime_session",
                         "run_real_validation(",
                         "instance.probe",
                         "load_evidence",
                         "save_evidence",
                         "run_cli",
                         "exit_code_for"):
            self.assertIn(required, source, required)
        for banned in ("verified_orchestrator", "collaboration_session",
                       "execution_engine"):
            self.assertNotIn(banned, source, banned)

    def test_python_m_entry_module_wires_host_entry(self):
        # 测试点 5：python -m 入口存在并指向 host_entry.main（双模式
        # import 与 cli.py 的 __version__ 先例同型）。
        main_py = Path(SCRIPTS, "__main__.py")
        self.assertTrue(main_py.exists(), "__main__.py must exist")
        source = main_py.read_text(encoding="utf-8")
        self.assertIn("host_entry", source)
        self.assertIn("main", source)

    def test_console_script_points_at_host_entry(self):
        # packaging 入口调整（且仅此一处）：console script 指向默认组合根。
        pyproject = (REPO / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('dual-agent = "dual_agent.host_entry:main"', pyproject)
        self.assertNotIn('dual-agent = "dual_agent.cli:main"', pyproject)


class RunResultProjectionTests(unittest.TestCase):
    """CU-R1（Run Result Delivery）：run 后从 ledger 投影安全结果摘要到
    stderr —— stdout 恰一行机器 JSON 的契约逐字节不变。

    数据源边界：只投影 append 期已通过 packet schema + secret-shape 扫描
    + 全包 unsafe 扫描的 envelope wire（envelope() 重解码 = 读取侧二次验
    证）；dict 形状字段只计数；FAILURE → 诚实未交付行；SINGLE/DECISION
    无 payload 入账 → 零投影（结构事实锁定，不用冻结层去"补"）。
    """

    TASK = "projection-task"

    EXPECTED_FOUR_STAGE = (
        "dual-agent: result architecture goal[1]: goal-a",
        "dual-agent: result architecture goal[2]: goal-b",
        "dual-agent: result architecture[1]: component split",
        "dual-agent: result architecture constraints[1]: constraint-a",
        "dual-agent: result implementation summary: Split parser into a module.",
        "dual-agent: result implementation changed_files[1]: parser.py",
        "dual-agent: result implementation details[1]: extracted helpers",
        "dual-agent: result implementation unresolved[1]: none",
        "dual-agent: result tests run=1 passed=1 failed=0 failure_items=0",
        "dual-agent: result tests coverage[1]: manual review only",
        "dual-agent: result tests remaining_risks[1]: sample risk",
        "dual-agent: result review status: APPROVED",
        "dual-agent: result review severity[1]: minor",
        "dual-agent: result review required_changes[1]: add docstring",
        "dual-agent: result review findings_count=1",
    )

    # -- ledger fixtures --------------------------------------------------

    def _arch_packet(self, task=None):
        return ArchitecturePacket(
            task_id=task or self.TASK, role="architect",
            goal=("goal-a", "goal-b"), constraints=("constraint-a",),
            architecture=("component split",), interfaces=({"name": "iface-1"},),
            implementation_steps=({"step": 1},),
            acceptance_criteria=("criteria-a",), risks=({"risk": "latency"},))

    def _impl_packet(self, task=None):
        return ImplementationPacket(
            task_id=task or self.TASK, role="coder",
            changed_files=("parser.py",),
            implementation_summary="Split parser into a module.",
            implementation_details=("extracted helpers",),
            assumptions=("ambient",), unresolved_items=("none",),
            test_requirements=("unit",))

    def _test_packet(self, task=None):
        return TestPacket(
            task_id=task or self.TASK, role="tester",
            tests_run=("unit",), tests_passed=("unit",), tests_failed=(),
            failures=(), coverage_or_validation=("manual review only",),
            remaining_risks=("sample risk",))

    def _review_packet(self, task=None):
        return ReviewPacket(
            task_id=task or self.TASK, role="reviewer", status="APPROVED",
            findings=({"finding": "finding-x"},), severity=("minor",),
            affected_files=("parser.py",), required_changes=("add docstring",),
            acceptance_criteria_status=("met",))

    def _envelope(self, payload, correlation, source_role, target_role,
                  task=None):
        kind = {
            "architect": CollaborationPayloadType.ARCHITECTURE,
            "coder": CollaborationPayloadType.IMPLEMENTATION,
            "tester": CollaborationPayloadType.TEST,
            "reviewer": CollaborationPayloadType.REVIEW,
        }[source_role]
        return CollaborationPacket(
            correlation_id=correlation, task_id=task or self.TASK,
            source_agent=f"addr-{source_role}",
            target_agent=f"addr-{target_role}",
            source_role=source_role, target_role=target_role,
            payload_type=kind, payload=payload, provenance="REAL")

    def _decision(self, state, task=None):
        return state.append_decision(
            task or self.TASK, mode="on", complexity="COMPLEX",
            path="DUAL", runtime_mode="SINGLE_RUNTIME", reason="MODE_ON")

    def _four_stage_state(self):
        state = SharedCollaborationState()
        state = self._decision(state)
        state = state.append_envelope(
            self.TASK,
            self._envelope(self._arch_packet(), "corr-1",
                           "architect", "coder"),
            "REQUEST", "DELIVERED")
        state = state.append_envelope(
            self.TASK,
            self._envelope(self._impl_packet(), "corr-1",
                           "coder", "architect"),
            "REPLY", "DELIVERED")
        state = state.append_envelope(
            self.TASK,
            self._envelope(self._test_packet(), "corr-2",
                           "tester", "reviewer"),
            "REQUEST", "DELIVERED")
        state = state.append_envelope(
            self.TASK,
            self._envelope(self._review_packet(), "corr-3",
                           "reviewer", "architect"),
            "REQUEST", "DELIVERED")
        return state

    # -- 2. FOUR_STAGE 成功投影 -------------------------------------------

    def test_four_stage_success_full_projection(self):
        lines = host_entry._result_projection_lines(
            self._four_stage_state(), self.TASK)
        self.assertEqual(tuple(lines), self.EXPECTED_FOUR_STAGE)

    # -- 3. failure-only：不伪造结果 --------------------------------------

    def test_architect_invalid_failure_only_is_honest(self):
        state = self._decision(SharedCollaborationState())
        state = state.append_failure(self.TASK,
                                     status="ARCHITECT_PACKET_INVALID")
        self.assertEqual(
            host_entry._result_projection_lines(state, self.TASK),
            ("dual-agent: result not delivered: ARCHITECT_PACKET_INVALID",))

    # -- 4. 前置合法结果 + tester 失败 ------------------------------------

    def test_partial_results_with_tester_failure(self):
        state = SharedCollaborationState()
        state = self._decision(state)
        state = state.append_envelope(
            self.TASK,
            self._envelope(self._arch_packet(), "corr-1",
                           "architect", "coder"),
            "REQUEST", "DELIVERED")
        state = state.append_envelope(
            self.TASK,
            self._envelope(self._impl_packet(), "corr-1",
                           "coder", "architect"),
            "REPLY", "DELIVERED")
        state = state.append_failure(self.TASK,
                                     status="TESTER_PACKET_INVALID")
        lines = host_entry._result_projection_lines(state, self.TASK)
        self.assertEqual(len(lines), 9)  # arch 4 + impl 4 + 诚实失败行
        self.assertTrue(lines[0].startswith(
            "dual-agent: result architecture goal[1]: "))
        self.assertIn(
            "dual-agent: result implementation summary: "
            "Split parser into a module.", lines)
        self.assertEqual(
            lines[-1],
            "dual-agent: result not delivered: TESTER_PACKET_INVALID")

    # -- 5. SINGLE / decision-only：结构事实=无 payload 入账 ---------------

    def test_single_decision_only_projects_nothing(self):
        state = self._decision(SharedCollaborationState())
        self.assertEqual(
            host_entry._result_projection_lines(state, self.TASK), ())

    # -- 6. dict 字段只计数 ------------------------------------------------

    def test_dict_fields_counted_not_expanded(self):
        lines = host_entry._result_projection_lines(
            self._four_stage_state(), self.TASK)
        joined = "\n".join(lines)
        for dict_content in ("iface-1", "finding-x", "criteria-a", "latency"):
            self.assertNotIn(dict_content, joined, dict_content)
        self.assertIn("dual-agent: result review findings_count=1", lines)
        self.assertIn(
            "dual-agent: result tests run=1 passed=1 failed=0 "
            "failure_items=0", lines)

    # -- 7. 禁区字段 -------------------------------------------------------

    def test_forbidden_internal_fields_absent(self):
        lines = host_entry._result_projection_lines(
            self._four_stage_state(), self.TASK)
        joined = "\n".join(lines)
        for forbidden in ("addr-architect", "addr-coder", "addr-tester",
                          "addr-reviewer", "corr-1", "corr-2", "corr-3"):
            self.assertNotIn(forbidden, joined, forbidden)

    # -- 8. 确定性 ---------------------------------------------------------

    def test_deterministic_output(self):
        state = self._four_stage_state()
        first = host_entry._result_projection_lines(state, self.TASK)
        second = host_entry._result_projection_lines(state, self.TASK)
        self.assertEqual(tuple(first), tuple(second))

    # -- 9. 畸形 wire 隔离 --------------------------------------------------

    def test_malformed_wire_isolated(self):
        bad = CollaborationRecord(
            task_id=self.TASK, correlation_id="corr-bad", sequence=1,
            direction=CollaborationDirection.REQUEST, wire="{not-json")
        state = SharedCollaborationState(_records={self.TASK: (bad,)})
        state = state.append_envelope(
            self.TASK,
            self._envelope(self._arch_packet(), "corr-1",
                           "architect", "coder"),
            "REQUEST", "DELIVERED")
        self.assertEqual(
            host_entry._result_projection_lines(state, self.TASK),
            ("dual-agent: result record skipped: UNDECODABLE",
             "dual-agent: result architecture goal[1]: goal-a",
             "dual-agent: result architecture goal[2]: goal-b",
             "dual-agent: result architecture[1]: component split",
             "dual-agent: result architecture constraints[1]: constraint-a"))

    # -- 10. ledger sequence 顺序 -------------------------------------------

    def test_ledger_sequence_ordering(self):
        state = SharedCollaborationState()
        state = state.append_envelope(
            self.TASK,
            self._envelope(self._arch_packet(), "corr-1",
                           "architect", "coder"),
            "REQUEST", "DELIVERED")
        state = state.append_failure(self.TASK,
                                     status="TRANSPORT_FAILED")
        state = state.append_envelope(
            self.TASK,
            self._envelope(self._impl_packet(), "corr-1",
                           "coder", "architect"),
            "REPLY", "DELIVERED")
        lines = host_entry._result_projection_lines(state, self.TASK)
        self.assertEqual(len(lines), 9)
        self.assertTrue(lines[3].startswith(
            "dual-agent: result architecture constraints[1]: "))
        self.assertEqual(lines[4],
                         "dual-agent: result not delivered: TRANSPORT_FAILED")
        self.assertTrue(lines[5].startswith(
            "dual-agent: result implementation summary: "))

    # -- 11. 空 ledger ------------------------------------------------------

    def test_empty_ledger_yields_no_lines(self):
        self.assertEqual(
            host_entry._result_projection_lines(
                SharedCollaborationState(), self.TASK), ())

    # -- 12. 多 task 隔离 ---------------------------------------------------

    def test_multiple_tasks_isolated(self):
        other = "other-task"
        state = self._four_stage_state()
        state = state.append_envelope(
            other,
            self._envelope(self._arch_packet(other), "corr-x",
                           "architect", "coder", task=other),
            "REQUEST", "DELIVERED")
        mine = host_entry._result_projection_lines(state, self.TASK)
        self.assertEqual(tuple(mine), self.EXPECTED_FOUR_STAGE)
        theirs = host_entry._result_projection_lines(state, other)
        self.assertEqual(len(theirs), 4)  # 只含 other 的 arch 投影

    # -- 1. stdout 不变量 + stderr 投影（集成） -----------------------------

    def _run_main(self, argv):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = host_entry.main(
                argv, factories=two_family_factories(),
                evidence=two_family_evidence())
        return code, stdout.getvalue(), stderr.getvalue()

    def test_stdout_invariant_stderr_projection_offline_four_stage(self):
        code, out, err = self._run_main(
            ["run", "--min-runtimes", "1", "--mode", "on",
             "redesign architecture across modules"])
        self.assertEqual(code, 0)
        lines = out.splitlines()
        self.assertEqual(len(lines), 1)  # stdout 恰一行
        payload = json.loads(lines[0])
        self.assertEqual(
            sorted(payload),
            ["failure_category", "mode", "path", "provenance",
             "stage_counts", "stages", "status", "task_id"])  # schema 不变
        self.assertIn("dual-agent: result architecture goal[1]: ", err)
        self.assertIn("dual-agent: result implementation summary: ", err)
        self.assertIn("dual-agent: result tests run=", err)
        self.assertIn("dual-agent: result review status: ", err)
        self.assertNotIn("dual-agent: result ", out)  # 投影绝不入 stdout

    def test_single_path_emits_no_result_lines(self):
        # SINGLE/OFF 路径 payload 不入 ledger（结构事实）→ 零结果行；
        # 该路径离线引擎结果（SUCCESS/FAILED）与本 CU 无关，只锁定投影
        # 行为：stdout 单行契约保持 + stderr 无任何 result 投影行。
        code, out, err = self._run_main(["run", "--mode", "off", "tiny task"])
        self.assertIn(code, (0, 2))  # 既有 exit 契约不变
        self.assertEqual(len(out.splitlines()), 1)
        self.assertNotIn("dual-agent: result ", err)


_MISSING_FIELD_OUTPUT = json.dumps({
    "task_id": "task-1", "role": "architect",
    "constraints": ["c"], "architecture": ["a"], "interfaces": [],
    "implementation_steps": [], "acceptance_criteria": ["ac"], "risks": [],
})

_PROSE_OUTPUT = "抱歉，该约束下无法只输出 JSON 对象，以下是分析说明。"

_CODER_NUMBER_LIST_OUTPUT = json.dumps({
    "task_id": "task-1", "role": "coder",
    "changed_files": 123,
    "implementation_summary": "s",
    "implementation_details": [], "assumptions": [],
    "unresolved_items": [], "test_requirements": [],
})


class PacketRejectDiagnosticsTests(unittest.TestCase):
    """CU-R2（Packet Rejection Diagnostics）：*_PACKET_INVALID 终态在
    stderr 获得恰一行值安全的拒绝诊断（rule/field/layer 坐标，绝无被
    拒值）；无新鲜诊断时按消去法只报 G2/G3 不可分诊形态。stdout 恰
    一行机器 JSON 的契约逐字节不变。"""

    TASK = "diagnostics probe task"

    def setUp(self):
        reset_validation_diagnostic()

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _reject_role_factory(role, output):
        class _BadRole(FakeFamilyAdapter):
            def invoke(self, request):
                if request.role == role:
                    return InvocationResult(
                        InvocationStatus.SUCCESS, output=output, trace=None)
                return super().invoke(request)

        adapter = _BadRole("rt-a")
        return (lambda: adapter,
                lambda: FakeFamilyAdapter("rt-b", "provider-b"))

    def _run(self, factories, flags=()):
        argv = ["run", "--min-runtimes", "1", "--mode", "on", *flags,
                self.TASK]
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = host_entry.main(
                argv,
                factories=factories, evidence=two_family_evidence())
        return code, stdout.getvalue(), stderr.getvalue()

    # -- unit：_packet_reject_line ------------------------------------------

    def test_reject_line_unit_fresh_diagnostic(self):
        generation = next_diagnostic_generation()
        record_validation_diagnostic(
            ValidationDiagnostic("packet", "goal", None, "NOT_A_LIST"))
        self.assertEqual(
            host_entry._packet_reject_line("ARCHITECT_PACKET_INVALID",
                                           generation),
            "dual-agent: packet reject stage=architect rule=NOT_A_LIST "
            "field=goal layer=packet")

    def test_reject_line_unit_stale_diagnostic_falls_back(self):
        # Case A（单元）：代数不匹配的旧诊断绝不被冒用。
        next_diagnostic_generation()
        record_validation_diagnostic(
            ValidationDiagnostic("packet", "goal", None, "NOT_A_LIST"))
        current = next_diagnostic_generation()
        line = host_entry._packet_reject_line("TESTER_PACKET_INVALID",
                                              current)
        self.assertIn("rule=JSON_PARSE_OR_NON_OBJECT", line)
        self.assertIn("stage=tester", line)
        self.assertNotIn("NOT_A_LIST", line)

    def test_reject_line_unit_fallback_without_any_diagnostic(self):
        generation = next_diagnostic_generation()
        line = host_entry._packet_reject_line("CODER_PACKET_INVALID",
                                              generation)
        self.assertEqual(
            line, "dual-agent: packet reject stage=coder "
                  "rule=JSON_PARSE_OR_NON_OBJECT")

    def test_reject_line_unit_none_for_non_packet_invalid_status(self):
        generation = next_diagnostic_generation()
        self.assertIsNone(
            host_entry._packet_reject_line("SUCCESS", generation))
        self.assertIsNone(
            host_entry._packet_reject_line("ARCHITECT_INVOKE_FAILED",
                                           generation))

    # -- e2e：经真实 main() 链 ------------------------------------------------

    def test_missing_fields_e2e_diagnostic_and_stdout_invariant(self):
        # Case B（e2e）：本次 run 的 REJECT 带当前代数 → 采信并暴露。
        code, out, err = self._run(
            self._reject_role_factory("architect", _MISSING_FIELD_OUTPUT))
        self.assertEqual(code, 2)
        self.assertEqual(len(out.splitlines()), 1)
        payload = json.loads(out)
        self.assertEqual(payload["status"], "ARCHITECT_PACKET_INVALID")
        self.assertIn(
            "dual-agent: packet reject stage=architect rule=MISSING_FIELDS",
            err)
        self.assertNotIn("packet reject", out)

    def test_stale_diagnostic_not_surfaced_next_run_uses_fallback(self):
        # Case A（e2e）：run1 记录 MISSING_FIELDS；run2 换代后 G2 拒绝
        # （散文输出，无记录）→ 只报消去法行，绝不冒用 run1 的旧观测。
        code, _, first_err = self._run(
            self._reject_role_factory("architect", _MISSING_FIELD_OUTPUT))
        self.assertEqual(code, 2)
        self.assertIn("rule=MISSING_FIELDS", first_err)
        code, out, err = self._run(
            self._reject_role_factory("architect", _PROSE_OUTPUT))
        self.assertEqual(code, 2)
        self.assertEqual(len(out.splitlines()), 1)
        self.assertIn("rule=JSON_PARSE_OR_NON_OBJECT", err)
        self.assertNotIn("MISSING_FIELDS", err)

    def test_coder_not_a_list_e2e_attributed_to_stage(self):
        # 同 run 内跨阶段归属：architect 成功（无记录）、coder 拒绝 →
        # 诊断新鲜且归属 coder；_normalize 原样保留（数字非字符串，
        # 归一化救不了 → 依旧拒绝，语义零变化）。P1-1 起 spread 会把
        # coder 落到 rt-b（好 adapter），故钉 allowlist=rt-a 保持本缝隙
        # 的原证明场景（拒收归因按角色键、runtime-neutral；多 runtime
        # 分工由 MultiRuntimeCompositionWiringTests 锁定）。
        code, out, err = self._run(
            self._reject_role_factory("coder", _CODER_NUMBER_LIST_OUTPUT),
            flags=("--runtimes", "rt-a"))
        self.assertEqual(code, 2)
        self.assertIn("stage=coder rule=NOT_A_LIST", err)
        self.assertIn("field=changed_files", err)

    def test_success_run_emits_no_false_reject_line(self):
        code, out, err = self._run(two_family_factories())
        self.assertEqual(code, 0)
        self.assertNotIn("packet reject", err)
        self.assertEqual(len(out.splitlines()), 1)
        # CU-R1 投影回归：成功 run 的结果投影仍在 stderr。
        self.assertIn("dual-agent: result architecture goal[1]: ", err)


class OpaqueTaskIdBoundaryTests(unittest.TestCase):
    """CU-R3a（Restore Opaque task_id at the CLI Composition Boundary）：
    CLI host 路径在组合边界把 task_id 映射为确定性不透明摘要 —— 词域误拒
    （record_tokens→token 的 identifier 裸子串拒收）结构性消失，而
    identifier 安全策略/扫描器零改动；task/prompt 原文原样进入 engine；
    stdout 8 键 schema 不变，仅 task_id 语义变为不透明（已批准变更）。"""

    # 与 REAL 探针同型的词域任务：正文含 marker 子串（record_tokens→token）
    TASK = "分析 record_tokens 函数的预算职责边界"

    MARKERS = ("token", "secret", "api_key", "authorization",
               "bearer", "stdout", "stderr")

    DISTINCT_TASKS = (
        "refactor the parser module",
        "分析 record_tokens 函数的预算职责边界",
        "review auth bearer handling and api_key storage",
        "summarize stdout and stderr capture paths",
        "tiny task",
        "a slightly longer collaboration task about module boundaries",
        "检查 secret 扫描规则的覆盖面",
        "token budget audit",
    )

    # -- Test A：确定性 -------------------------------------------------------

    def test_derivation_deterministic(self):
        first = host_entry._opaque_task_id(self.TASK)
        second = host_entry._opaque_task_id(self.TASK)
        self.assertEqual(first, second)
        self.assertEqual(
            host_entry._opaque_task_id("tiny task"),
            host_entry._opaque_task_id("tiny task"))

    # -- Test B：互异 ---------------------------------------------------------

    def test_derivation_distinct_across_tasks(self):
        ids = [host_entry._opaque_task_id(task)
               for task in self.DISTINCT_TASKS]
        self.assertEqual(len(set(ids)), len(ids))

    # -- Test C：不透明（无正文、无 marker、稳定形态） -----------------------

    def test_derived_id_is_opaque(self):
        for task in self.DISTINCT_TASKS:
            derived = host_entry._opaque_task_id(task)
            self.assertNotEqual(derived, task)
            self.assertNotIn(task, derived)
            self.assertTrue(derived.startswith("task_"), derived)
            lowered = derived.lower()
            for marker in self.MARKERS:
                self.assertNotIn(marker, lowered, marker)
        # 形态稳定：同长度、仅十六进制摘要字符
        first = host_entry._opaque_task_id(self.DISTINCT_TASKS[0])
        second = host_entry._opaque_task_id(self.DISTINCT_TASKS[1])
        self.assertEqual(len(first), len(second))

    # -- Test D：组合边界保全 task/prompt 原文、只换 task_id ------------------

    def test_boundary_proxy_preserves_task_and_prompt(self):
        recorded = {}

        def _run(**kwargs):
            recorded.update(kwargs)
            return "sentinel-result"

        namespace = types.SimpleNamespace(run=_run)
        proxy = host_entry._cli_task_boundary(namespace)
        sentinel_mode = object()
        sentinel_sink = object()
        sentinel_policy = object()
        returned = proxy.run(
            task_id=self.TASK, task=self.TASK, prompt=self.TASK,
            mode=sentinel_mode, observation_sink=sentinel_sink,
            policy=sentinel_policy)
        self.assertIs(returned, "sentinel-result")  # 返回值透传
        self.assertEqual(recorded["task"], self.TASK)  # 原文保全
        self.assertEqual(recorded["prompt"], self.TASK)  # 原文保全
        self.assertIs(recorded["mode"], sentinel_mode)  # 其余实参透传
        self.assertIs(recorded["observation_sink"], sentinel_sink)
        self.assertIs(recorded["policy"], sentinel_policy)
        # task_id 已换为不透明摘要（≠ 原文、无 marker）
        self.assertNotEqual(recorded["task_id"], self.TASK)
        self.assertNotIn(self.TASK, recorded["task_id"])
        for marker in self.MARKERS:
            self.assertNotIn(marker, recorded["task_id"].lower(), marker)

    # -- Test D（e2e 补充）：引擎侧标识符不透明、任务正文原文到达 -----------

    def test_engine_sees_opaque_id_and_original_task_text(self):
        seen = []
        base = FakeFamilyAdapter("rt-a")

        class _Recording(FakeFamilyAdapter):
            def invoke(self, request):
                seen.append((request.task_id, request.prompt))
                return base.invoke(request)

        adapter = _Recording("rt-a")
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = host_entry.main(
                ["run", "--min-runtimes", "1", "--mode", "on", self.TASK],
                factories=(lambda: adapter,
                           lambda: FakeFamilyAdapter("rt-b", "provider-b")),
                evidence=two_family_evidence())
        self.assertEqual(code, 0)
        self.assertTrue(seen)  # 四阶段确实调用了 adapter
        for task_id, prompt in seen:
            # 引擎侧标识符 = 不透明摘要（每次调用一致）；前缀无连字符，
            # 结构性不构成 "sk-" 等凭据形状子串
            self.assertTrue(task_id.startswith("task_"), task_id)
            for marker in self.MARKERS:
                self.assertNotIn(marker, task_id.lower(), marker)
        # 任务正文原文进入首个调用（architect 的 prompt —— 引擎语义：
        # 原文只喂 architect，下游角色收 packet 交接，正文不经清洗）
        self.assertIn(self.TASK, seen[0][1])

    # -- Test E：词域任务不再触发 ledger 安全拒收 ----------------------------

    def test_problematic_vocabulary_runs_without_ledger_rejection(self):
        # RED→GREEN 的主证：该任务文本在 CU-R3a 之前 100% 复现
        # CollaborationStateError（REAL 探针 + 离线复现）；现在必须正常
        # 跑完四阶段并成功交付。
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = host_entry.main(
                ["run", "--min-runtimes", "1", "--mode", "on", self.TASK],
                factories=two_family_factories(),
                evidence=two_family_evidence())
        out, err = stdout.getvalue(), stderr.getvalue()
        self.assertEqual(code, 0)
        self.assertNotIn("NOT_QUALIFIED", out)
        self.assertNotIn("secret-shaped", out)
        self.assertNotIn("secret-shaped", err)
        self.assertNotIn("Traceback", err)
        # 四阶段确实跑完：CU-R1 结果投影在场（engine 真执行，非绕过）
        self.assertIn("dual-agent: result architecture goal[1]: ", err)
        self.assertIn("dual-agent: result review status: ", err)

    # -- Test F：retry 身份确定性 ---------------------------------------------

    def test_retry_identity_stable_across_invocations(self):
        def _once():
            stdout, _ = io.StringIO(), io.StringIO()
            with redirect_stdout(stdout):
                code = host_entry.main(
                    ["run", "--min-runtimes", "1", "--mode", "on", self.TASK],
                    factories=two_family_factories(),
                    evidence=two_family_evidence())
            return code, json.loads(stdout.getvalue())["task_id"]

        code_a, id_a = _once()
        code_b, id_b = _once()
        self.assertEqual((code_a, code_b), (0, 0))
        self.assertEqual(id_a, id_b)  # 同任务两次 host 级调用 → 同 task_id

    # -- Test G：stdout 契约（单行 / 8 键 / task_id 不透明）------------------

    def test_cli_stdout_contract_opaque_task_id(self):
        stdout, _ = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout):
            code = host_entry.main(
                ["run", "--min-runtimes", "1", "--mode", "on", self.TASK],
                factories=two_family_factories(),
                evidence=two_family_evidence())
        self.assertEqual(code, 0)
        lines = stdout.getvalue().splitlines()
        self.assertEqual(len(lines), 1)  # stdout 恰一行
        payload = json.loads(lines[0])
        self.assertEqual(
            sorted(payload),
            ["failure_category", "mode", "path", "provenance",
             "stage_counts", "stages", "status", "task_id"])  # 8 键不变
        task_id = payload["task_id"]
        self.assertNotEqual(task_id, self.TASK)  # 已批准的语义变更
        self.assertTrue(task_id.startswith("task_"), task_id)
        for marker in self.MARKERS:
            self.assertNotIn(marker, task_id.lower(), marker)


class RunExceptionRenderingTests(unittest.TestCase):
    """CU-R3b（CLI Pre-Collaboration Exception Rendering）：run 内
    pre-collaboration 域拒绝（CollaborationStateError，REAL 探针 + 离线
    复现已证）收敛为既有语义失败表面（stdout 恰 1 行 JSON + stderr 人类
    行 + exit 2）；真 unexpected 异常保持传播可见性，绝不吞成 exit 2。"""

    def test_ledger_domain_rejection_renders_semantic_failure(self):
        # CU-R3b 锁定的渲染面回归。自然触发（任务文本含 marker 子串）已被
        # CU-R3a 组合边界映射结构性移除（同型任务见
        # OpaqueTaskIdBoundaryTests.Test E —— 现在正常成功）；本测试改为
        # 模拟触发：append_decision 抛出与 REAL 探针逐字同型的域异常，
        # 经真实 main() 全链（离线 stub runtime）证明渲染面不变。
        from collaboration_state import SharedCollaborationState as _State
        original = _State.append_decision

        def _reject(self, *args, **kwargs):
            raise CollaborationStateError(
                "task_id must not contain secret-shaped content")

        _State.append_decision = _reject
        try:
            stdout, stderr = io.StringIO(), io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = host_entry.main(
                    ["run", "--min-runtimes", "1", "--mode", "on",
                     "分析 task_budget.py 的 record_tokens 记账职责。只读分析。"],
                    factories=two_family_factories(),
                    evidence=two_family_evidence())
        finally:
            _State.append_decision = original
        self.assertEqual(code, 2)
        out = stdout.getvalue()
        err = stderr.getvalue()
        self.assertNotIn("Traceback", out)
        self.assertNotIn("Traceback", err)
        self.assertEqual(len(out.splitlines()), 1)  # 恰 1 行机器 JSON
        payload = json.loads(out)
        self.assertEqual(payload["status"], "NOT_QUALIFIED")
        self.assertIn("secret-shaped", payload["reason"])  # 封闭词表 reason
        # 任务原文不泄漏到任何表面（reason/detail 是封闭词表坐标）
        self.assertNotIn("record_tokens", out)
        self.assertNotIn("record_tokens", err)
        # 人类行不得误导为 runtime 未获资格（runtime 已入池，被拒的是
        # ledger 安全规则）
        self.assertNotIn("no admitted verified runtime", err)
        # 下游表面零伪造：CU-R1 投影 / CU-R2 诊断 / observation 事件
        self.assertNotIn("dual-agent: result ", err)
        self.assertNotIn("packet reject", err)
        self.assertNotIn("DECISION", err)

    def test_unexpected_exception_keeps_traceback_visibility(self):
        # 真 unexpected 异常（普通 RuntimeError）从 run_cli 逃逸时必须
        # 原样传播 —— 不被包装成语义失败、不被吞掉。
        original = host_entry.run_cli

        def _boom(facade, argv):
            raise RuntimeError("unexpected engine fault")

        host_entry.run_cli = _boom
        try:
            stdout, stderr = io.StringIO(), io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                with self.assertRaises(RuntimeError):
                    host_entry.main(
                        ["run", "--mode", "off", "tiny task"],
                        factories=two_family_factories(),
                        evidence=two_family_evidence())
            self.assertEqual(stdout.getvalue(), "")  # 不伪造 JSON
        finally:
            host_entry.run_cli = original


class PacketForensicsCaptureTests(unittest.TestCase):
    """CU-R4（Architect Raw Output Forensics Capture）：*_PACKET_INVALID
    终态专属的 parser-input 原文取证。fixture 镜像真实 adapter 接线
    （每次成功 invoke 都 remember；真实接线由
    test_packet_forensics.AdapterRememberWiringTests 离线证明）；成功
    run 零落盘零输出；CU-R2 拒绝行与 stdout 契约逐字节不变。"""

    TASK = "forensics capture probe task"
    _MALFORMED_ARCH = '{"task_id": "t", "role": "architect", "goal": ["a'
    _SECRET_CODER = ('{"task_id": "t", "role": "coder", '
                     '"changed_files": ["f"], "note": "api_key: zz9988776655"')

    def setUp(self):
        packet_forensics.reset()
        self._cleanup = []

    def tearDown(self):
        for path in self._cleanup:
            Path(path).unlink(missing_ok=True)
        packet_forensics.reset()

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _forensics_factory(bad_role, bad_output):
        """镜像真实 adapter：每次成功 invoke 都 remember（invocation id
        递增 → 文件名天然不重叠）；仅 bad_role 返回 malformed 输出。"""

        class _ForensicsProbe(FakeFamilyAdapter):
            def __init__(self, runtime_id):
                super().__init__(runtime_id)
                self._invocations = 0

            def invoke(self, request):
                self._invocations += 1
                if request.role == bad_role:
                    output = bad_output
                else:
                    output = json.dumps(ARCH_P if request.role == "architect"
                                        else IMPL_P)
                packet_forensics.remember_invocation_output(
                    self.runtime_id, f"inv-cu4-{self._invocations}",
                    request.task_id, request.role, output)
                return InvocationResult(
                    InvocationStatus.SUCCESS, output=output, trace=None)

        adapter = _ForensicsProbe("rt-a")
        return (lambda: adapter,
                lambda: FakeFamilyAdapter("rt-b", "provider-b"))

    def _run(self, factories, flags=()):
        argv = ["run", "--min-runtimes", "1", "--mode", "on", *flags,
                self.TASK]
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = host_entry.main(
                argv,
                factories=factories, evidence=two_family_evidence())
        return code, stdout.getvalue(), stderr.getvalue()

    def _forensics_files(self, err):
        prefix = "dual-agent: packet forensics: "
        lines = [line[len(prefix):] for line in err.splitlines()
                 if line.startswith(prefix)]
        self.assertEqual(len(lines), 1, "取证行必须恰一行")
        self._cleanup.extend(lines[0].split("; "))
        return [json.loads(Path(path).read_text(encoding="utf-8"))
                for path in lines[0].split("; ")]

    # -- e2e ---------------------------------------------------------------

    def test_architect_reject_captures_exact_parser_input(self):
        code, out, err = self._run(
            self._forensics_factory("architect", self._MALFORMED_ARCH))
        self.assertEqual(code, 2)
        # stdout 契约不变：恰一行 8 键 JSON（CU-R2 同面）。
        self.assertEqual(len(out.splitlines()), 1)
        payload = json.loads(out)
        self.assertEqual(payload["status"], "ARCHITECT_PACKET_INVALID")
        self.assertEqual(sorted(payload), sorted([
            "status", "task_id", "mode", "path", "stages", "stage_counts",
            "provenance", "failure_category"]))
        # CU-R2 拒绝行不变，取证行是新增加的恰一行。
        self.assertIn(
            "dual-agent: packet reject stage=architect "
            "rule=JSON_PARSE_OR_NON_OBJECT", err)
        records = self._forensics_files(err)
        self.assertEqual(len(records), 1)
        record = records[0]
        # 核心验收：captured == parser input，完整字符串 equality。
        self.assertEqual(record["raw_parser_input"], self._MALFORMED_ARCH)
        self.assertEqual(record["capture_mode"], "FULL")
        self.assertEqual(record["runtime_id"], "rt-a")
        self.assertEqual(record["role"], "architect")
        self.assertEqual(record["failure_stage_hint"], "architect")
        # CU-R3a 相关性：记录里的 task_id 与 stdout 的不透明 id 一致。
        self.assertEqual(record["task_id"], payload["task_id"])
        self.assertTrue(record["task_id"].startswith("task_"))

    def test_success_run_persists_nothing_and_stays_silent(self):
        class _RememberingGood(FakeFamilyAdapter):
            def invoke(self, request):
                packet_forensics.remember_invocation_output(
                    self.runtime_id, "inv-cu4-good", request.task_id,
                    request.role, "captured-but-clean-run")
                return super().invoke(request)

        adapter = _RememberingGood("rt-a")
        code, out, err = self._run(
            (lambda: adapter, lambda: FakeFamilyAdapter("rt-b", "provider-b")))
        self.assertEqual(code, 0)
        self.assertNotIn("packet forensics", err)
        self.assertEqual(packet_forensics.pending(), ())  # 成功即清槽
        self.assertEqual(len(out.splitlines()), 1)

    def test_two_invocations_distinct_files_no_overwrite(self):
        # P1-1 起 spread 把 coder 落到 rt-b，角色注入的坏输出将不再到达
        # parser —— 钉 allowlist=rt-a 保持「两次成功调用、两份取证、互不
        # 覆盖」的原证明场景。
        code, out, err = self._run(
            self._forensics_factory("coder", '{"a": 1'),
            flags=("--runtimes", "rt-a"))
        self.assertEqual(code, 2)
        records = self._forensics_files(err)
        self.assertEqual(len(records), 2)
        by_role = {record["role"]: record for record in records}
        self.assertEqual(sorted(by_role), ["architect", "coder"])
        self.assertEqual(by_role["architect"]["capture_mode"], "FULL")
        self.assertEqual(by_role["architect"]["raw_parser_input"],
                         json.dumps(ARCH_P))
        self.assertEqual(by_role["coder"]["raw_parser_input"], '{"a": 1')
        self.assertNotEqual(by_role["architect"]["invocation_id"],
                            by_role["coder"]["invocation_id"])

    def test_secret_shaped_reject_never_plaintext_on_disk(self):
        code, out, err = self._run(
            self._forensics_factory("coder", self._SECRET_CODER),
            flags=("--runtimes", "rt-a"))
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out)["status"], "CODER_PACKET_INVALID")
        records = self._forensics_files(err)
        coder = [record for record in records
                 if record["role"] == "coder"][0]
        self.assertEqual(coder["capture_mode"], "REDACTED_UNSAFE")
        self.assertNotIn("zz9988776655",
                         json.dumps(coder, ensure_ascii=True))
        # redacted artifact 绝不声称等于 parser input（原文摘要独立保留）。
        self.assertNotEqual(coder["raw_parser_input"], self._SECRET_CODER)
        self.assertEqual(coder["raw_length"], len(self._SECRET_CODER))

    def test_reject_without_any_remembered_output_is_graceful(self):
        # 非 claude 家族 adapter（未接线 remember）拒绝时：零文件、零
        # 取证行 —— 取证绝不反向破坏既有失败语义。
        bad_output = self._MALFORMED_ARCH

        class _PlainBad(FakeFamilyAdapter):
            def invoke(self, request):
                if request.role == "architect":
                    return InvocationResult(
                        InvocationStatus.SUCCESS,
                        output=bad_output, trace=None)
                return super().invoke(request)

        adapter = _PlainBad("rt-a")
        code, out, err = self._run(
            (lambda: adapter, lambda: FakeFamilyAdapter("rt-b", "provider-b")))
        self.assertEqual(code, 2)
        self.assertNotIn("packet forensics", err)
        self.assertIn("rule=JSON_PARSE_OR_NON_OBJECT", err)


class MultiRuntimeCompositionWiringTests(unittest.TestCase):
    """P1-1（Normal CLI Multi-Runtime Composition Wiring）：admitted ≥ 2 时
    默认组合根经既有 build_facade_from_agents 形成多 runtime 池 —— 角色指
    派/policy/admission 真相全部留在既有件；admitted == 1 既有单 runtime
    路径逐字保持；组合前置缺失诚实失败，绝不静默降级。"""

    COMPLEX_TASK = "redesign architecture across modules"
    SIMPLE_TASK = "fix one simple bug"

    @staticmethod
    def _runtime_by_stage(err):
        mapping = {}
        for line in err.splitlines():
            match = re.match(
                r"\[\d+\] INVOCATION_FINISHED stage=(\S+) runtime=(\S+)",
                line)
            if match:
                mapping[match.group(1)] = match.group(2)
        return mapping

    def _run(self, argv, *, factories=None, evidence=None,
             current_health=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = host_entry.main(
                argv,
                factories=factories or two_family_factories(),
                evidence=evidence or two_family_evidence(),
                current_health=current_health)
        return code, stdout.getvalue(), stderr.getvalue()

    # -- 1. DOUBLE ADMITTED ------------------------------------------------

    def test_double_admitted_multi_pool_and_heterogeneous_stages(self):
        facade = host_entry.default_facade(
            factories=two_family_factories(),
            evidence=two_family_evidence())
        identities = facade._orchestrator._pool.identities()
        self.assertEqual(identities, (
            ("rt-a", "provider-a", None, "default"),
            ("rt-b", "provider-b", None, "default")))

        # 默认 policy（min=2）在多池上不再因组合层原因 UNSATISFIED。
        code, out, err = self._run(["run", "--mode", "on", "--observe",
                                    self.COMPLEX_TASK])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["status"], "SUCCESS")
        self.assertEqual(payload["path"], "FOUR_STAGE")
        self.assertIn("ROLE_ASSIGNMENT=POLICY_SPREAD", err)
        self.assertNotIn("POLICY_COUNT_UNSATISFIED", err)
        stage_runtime = self._runtime_by_stage(err)
        self.assertEqual(stage_runtime.get("architect"), "rt-a")
        self.assertEqual(stage_runtime.get("coder"), "rt-b")
        # 跨 runtime 移交：DUAL 半程 handoff 的目的 runtime = coder 方。
        self.assertIn("HANDOFF stage=architect runtime=rt-b", err)

    # -- 2. SINGLE REGRESSION ----------------------------------------------

    def test_single_admitted_keeps_existing_single_runtime_path(self):
        single_evidence = {
            ("rt-a", "provider-a", None, "default"): evidence_for("rt-a"),
        }
        facade = host_entry.default_facade(
            factories=(lambda: FakeFamilyAdapter("rt-a"),),
            evidence=single_evidence)
        identities = facade._orchestrator._pool.identities()
        self.assertEqual(identities,
                         (("rt-a", "provider-a", None, "default"),))

        code, out, err = self._run(
            ["run", "--min-runtimes", "1", "--mode", "on", "--observe",
             self.COMPLEX_TASK],
            factories=(lambda: FakeFamilyAdapter("rt-a"),),
            evidence=single_evidence)
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["status"], "SUCCESS")
        self.assertEqual(payload["path"], "FOUR_STAGE")
        stage_runtime = self._runtime_by_stage(err)
        self.assertTrue(stage_runtime)
        self.assertEqual(set(stage_runtime.values()), {"rt-a"})

    # -- 3. NO-RUNTIME-REUSE -------------------------------------------------

    def test_no_runtime_reuse_keeps_roles_on_distinct_runtimes(self):
        code, out, err = self._run(
            ["run", "--no-runtime-reuse", "--mode", "on", "--observe",
             self.COMPLEX_TASK])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["status"], "SUCCESS")
        stage_runtime = self._runtime_by_stage(err)
        self.assertNotEqual(stage_runtime.get("architect"),
                            stage_runtime.get("coder"))

    # -- 4. RUNTIME ALLOWLIST ------------------------------------------------

    def test_runtime_allowlist_flows_through_existing_policy(self):
        code, out, err = self._run(
            ["run", "--runtimes", "rt-b", "--min-runtimes", "1",
             "--mode", "on", "--observe", self.COMPLEX_TASK])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["status"], "SUCCESS")
        stage_runtime = self._runtime_by_stage(err)
        self.assertTrue(stage_runtime)
        self.assertEqual(set(stage_runtime.values()), {"rt-b"})

    # -- 5. SIMPLE/AUTO REGRESSION -------------------------------------------

    def test_auto_simple_routes_single_on_multi_pool(self):
        code, out, err = self._run(["run", self.SIMPLE_TASK])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["status"], "SUCCESS")
        self.assertEqual(payload["path"], "SINGLE")
        self.assertEqual(sorted(payload), sorted([
            "status", "task_id", "mode", "path", "stages", "stage_counts",
            "provenance", "failure_category"]))

    # -- 6. MISSING HEALTH / EVIDENCE（D4 诚实失败）--------------------------

    def test_missing_health_for_admitted_runtime_fails_honestly(self):
        registry, _ = host_entry.environment_registry(
            two_family_factories())
        partial_health = {
            runtime_id: status
            for runtime_id, status
            in host_entry.observe_current_health(registry).items()
            if runtime_id == "rt-a"
        }
        self.assertEqual(sorted(partial_health), ["rt-a"])
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = host_entry.main(
                ["run", "--min-runtimes", "2", "--mode", "on", "--observe",
                 self.COMPLEX_TASK],
                factories=two_family_factories(),
                evidence=two_family_evidence(),
                current_health=partial_health)
        self.assertEqual(code, 2)
        combined = stdout.getvalue() + stderr.getvalue()
        self.assertIn("no current health", combined)
        self.assertIn("rt-b", combined)
        # 绝不静默降级成单 runtime 成功。
        self.assertNotIn('"status":"SUCCESS"', stdout.getvalue().replace(" ", ""))

    # -- 7. ATTRIBUTION 只透传不消费 -----------------------------------------

    def test_attribution_never_surfaces_in_cli_output(self):
        code, out, err = self._run(["run", "--mode", "on", "--observe",
                                    self.COMPLEX_TASK])
        self.assertEqual(code, 0)
        self.assertNotIn("attribution", out)
        self.assertNotIn("attribution", err)
        event_types = set()
        for line in err.splitlines():
            match = re.match(r"\[\d+\] ([A-Z_]+) ", line)
            if match:
                event_types.add(match.group(1))
        self.assertTrue(event_types)
        self.assertTrue(event_types <= {
            "DECISION", "STAGE_STARTED", "INVOCATION_STARTED",
            "INVOCATION_FINISHED", "STAGE_FINISHED", "HANDOFF",
            "TERMINAL"})


if __name__ == "__main__":
    unittest.main()
