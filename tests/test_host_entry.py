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
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))
REPO = Path(__file__).resolve().parents[1]

import evidence_store
import host_entry
from candidate_validation import (
    CandidateValidationResult,
    CandidateValidationStatus,
    GateResult,
    GateVerdict,
    ValidationGate,
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
        # 不创建第二套 runtime truth：既有 Automatic entry 语义 = 排序后
        # 首个 admitted runtime 装 pool（零语义漂移）；其身份 = evidence 键
        # （bootstrap_runtime_session 的复用语义，rc3 C4 先例）。
        facade = host_entry.default_facade(
            factories=two_family_factories(),
            evidence=two_family_evidence())
        identities = facade._orchestrator._pool.identities()
        self.assertEqual(identities, (("rt-a", "provider-a", None, "default"),))

    def test_composition_order_registry_then_health_then_bootstrap(self):
        # 顺序证据：default_facade 先构造 registry 与 current_health，
        # 再交给既有 build_facade_from_bootstrap（patch 记录调用序）。
        calls = []
        real_bootstrap = host_entry.build_facade_from_bootstrap

        def spy_bootstrap(registry, **kwargs):
            calls.append(("bootstrap", sorted(
                d.runtime_id for d in registry.list()),
                sorted(kwargs.get("current_health") or {})))
            return real_bootstrap(registry, **kwargs)

        with patch.object(host_entry, "build_facade_from_bootstrap",
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
            self.assertEqual(stderr.getvalue(), "")  # 无 --observe 时无 observation

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


if __name__ == "__main__":
    unittest.main()
