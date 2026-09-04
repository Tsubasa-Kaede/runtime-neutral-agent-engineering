"""ADAPTER EXPANSION R1-Step1: Gemini REAL qualification test asset.

证明链（全部复用既有机器，零新架构、零生产代码修改）：

    Gemini CLI → GeminiAdapter（ExternalAgentAdapter 六方法）
      → real discovery → auth/provider 只读观测 → minimal health（REAL 门）
      → run_real_validation（既有 G1-G14 qualification，REAL provenance）
      → CandidateValidationResult → VerifiedRuntimePool admission
      → AgentManifest/AgentRuntimeBinding/factory（V3 声明）
      → build_facade_from_agents（V3 组合根）→ ProductionFacade
      → V2 冻结栈四阶段（architect → coder → tester → reviewer）

诚实边界（本机事实：gemini CLI 当前未安装）：
- 未设 RUN_REAL_PROVIDER_TESTS=1 → REAL 类整体 SKIP，默认套件零 REAL 调用
- 可执行文件缺失 → SKIPPED / UNAVAILABLE（正确结果，绝不伪 PASS）
- 认证不可用 → 链如实分类后 SKIPPED / AUTH_REQUIRED，绝不构造假证据
- provenance 断言只读真实 qualification 产物（绝不测试侧构造 REAL 标签）
- health snapshot 由 GenericRuntimeHealth 真实管线派生（绝不伪造 READY）
- invocation 预算：discovery=1、auth 观测=1、qualification=既有机器固有
  次数（如实统计打印）、四阶段=恰好 4；计数由 instrumentation 包装派生
"""
import os
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))

RUN_REAL_PROVIDER_TESTS = os.environ.get("RUN_REAL_PROVIDER_TESTS") == "1"

# 与 rc3 注册面/adapter 默认 profile 一致的 Gemini identity（源码为准：
# GeminiAdapter.from_environment 默认 RuntimeProfile("coding-agent",
# "gemini-cli", "google", None, "coder", frozenset())；descriptor 注册
# config_fingerprint="installed"）。
GEMINI_IDENTITY = ("gemini-cli", "google", None, "installed")
CAPS_ALL = ("architecture", "coding", "review", "testing")
SECRET_MARKERS = ("token", "secret", "api_key", "authorization", "bearer",
                  "stdout", "stderr")

# 四阶段任务串：短、无文件修改诉求、由 V2 packet 契约驱动输出形状。
TASK = "Design, implement, test, and review a one-function module change."

_REAL_CLASS_MARKER = "class Real" + "GeminiQualificationTests"


def _real_class_source() -> str:
    """REAL 类体的静态源（纪律扫描对象；扫描恒跑，无需 REAL）。"""
    source = Path(__file__).read_text(encoding="utf-8")
    start = source.index(_REAL_CLASS_MARKER)
    end = source.find("\nif __name__", start)
    return source[start:end if end != -1 else len(source)]


# ---------------------------------------------------------------------------
# TestFileDiscipline — 恒跑（默认环境），证明本文件自己的诚实规则
# ---------------------------------------------------------------------------


class TestFileDisciplineTests(unittest.TestCase):

    def test_real_test_is_opt_in_gated(self):
        source = Path(__file__).read_text(encoding="utf-8")
        self.assertIn('RUN_REAL_PROVIDER_TESTS") == "1"', source)
        self.assertIn("skipUnless", source)

    def test_real_class_uses_no_mock_or_stub_adapters(self):
        source = _real_class_source()
        for forbidden in ("MockAdapter", "FakeProcess", "unittest.mock",
                          "Mock(", "patch(", "Stub"):
            self.assertNotIn(forbidden, source)

    def test_real_class_uses_existing_qualification_machinery(self):
        # qualification/admission 只能来自既有机器；绝不测试侧构造证据。
        source = _real_class_source()
        self.assertIn("bootstrap_runtime_session(", source)
        self.assertIn("run_real_validation(", source)
        self.assertNotIn("CandidateValidationResult(", source)

    def test_real_class_never_forges_provenance(self):
        source = _real_class_source()
        self.assertNotIn('provenance="REAL"', source)
        self.assertNotIn("provenance = 'REAL'", source)

    def test_real_class_drives_four_stage_through_v3_composition(self):
        # 四阶段必须经 V3 组合根进 ProductionFacade：声明 → 组合 → V2。
        source = _real_class_source()
        self.assertIn("AgentManifest(", source)
        self.assertIn("AgentRuntimeBinding(", source)
        self.assertIn("AgentRegistry()", source)
        self.assertIn("build_facade_from_agents(", source)
        self.assertIn("facade.run(", source)
        # 绝不手工组装 V2 facade、绝不测试侧直连执行栈内部件。
        self.assertNotIn("ProductionFacade(", source)
        self.assertNotIn("CollaborationSession(", source)
        self.assertNotIn("VerifiedOrchestrator(", source)
        self.assertNotIn("CollaborationOrchestrator(", source)

    def test_real_class_never_hand_assigns_stage_executors(self):
        source = _real_class_source()
        for forbidden in ("architect_address", "coder_address",
                          "tester_address", "reviewer_address",
                          "architect_addr", "coder_addr", "tester_addr",
                          "reviewer_addr", "% len", "% 2", "round_robin"):
            self.assertNotIn(forbidden, source)

    def test_real_class_health_snapshot_derived_not_forged(self):
        # health 输入必须由 GenericRuntimeHealth 真实管线派生。
        source = _real_class_source()
        self.assertIn("GenericRuntimeHealth(", source)
        self.assertNotIn("RuntimeStatus(", source)

    def test_core_files_unmodified_in_working_tree(self):
        import shutil
        if shutil.which("git") is None:
            self.skipTest("git not available")
        protected_paths = (
            "dual-agent-development/scripts/gemini_adapter.py",
            "dual-agent-development/scripts/external_agent_adapter.py",
            "dual-agent-development/scripts/external_runtime.py",
            "dual-agent-development/scripts/agent_identity.py",
            "dual-agent-development/scripts/agent_manifest.py",
            "dual-agent-development/scripts/agent_composition.py",
            "dual-agent-development/scripts/agent_host.py",
            "dual-agent-development/scripts/agent_discovery.py",
            "dual-agent-development/scripts/agent_capability.py",
            "dual-agent-development/scripts/agent_bootstrap.py",
            "dual-agent-development/scripts/cli.py",
            "dual-agent-development/scripts/discovery_bootstrap.py",
            "dual-agent-development/scripts/real_validation_executor.py",
            "dual-agent-development/scripts/host.py",
            "dual-agent-development/scripts/production_facade.py",
            "dual-agent-development/scripts/verified_runtime_pool.py",
            "dual-agent-development/scripts/candidate_validation.py",
            "dual-agent-development/scripts/execution_observation.py",
        )
        for relpath in protected_paths:
            proc = subprocess.run(
                ["git", "status", "--porcelain", "--", relpath],
                cwd=str(REPO), capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, relpath)
            self.assertEqual(proc.stdout, "",
                             f"{relpath} modified: {proc.stdout!r}")

    def test_protected_untracked_files_still_untracked(self):
        import shutil
        if shutil.which("git") is None:
            self.skipTest("git not available")
        for relpath in ("tests/test_policy_boundary_qualification.py",
                        "tests/test_real_cli_policy_collaboration.py"):
            if not (REPO / relpath).exists():
                self.skipTest(f"missing protected file: {relpath}")
            proc = subprocess.run(
                ["git", "status", "--porcelain", "--", relpath],
                cwd=str(REPO), capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, relpath)
            self.assertTrue(proc.stdout.startswith("?? "),
                            f"{relpath} expected untracked, got: "
                            f"{proc.stdout!r}")


# ---------------------------------------------------------------------------
# Gated REAL tests（RUN_REAL_PROVIDER_TESTS=1；本机 gemini 缺失时诚实 SKIP）
# ---------------------------------------------------------------------------


@unittest.skipUnless(RUN_REAL_PROVIDER_TESTS, "RUN_REAL_PROVIDER_TESTS != 1")
class RealGeminiQualificationTests(unittest.TestCase):
    """G1-G11 一次会话证明：discovery → auth/health → qualification →
    admission → provenance=REAL → V3 组合 → 四阶段。

    全部事实来自真实机器；不可用 = 诚实 SKIP，绝不伪 PASS。
    invocation 预算：test_01 discovery 1 次、test_02 auth 观测 1 次、
    test_03 既有 qualification 机器固有次数（如实打印）、test_04 恰好 4。
    """

    @classmethod
    def setUpClass(cls):
        from gemini_adapter import GeminiAdapter
        import time as _time
        cls.adapter = GeminiAdapter.from_environment()
        if cls.adapter is None:
            raise unittest.SkipTest(
                "SKIPPED / UNAVAILABLE: gemini executable not on PATH")
        # instrumentation 包装：计数并打印每次真实调用的封闭取证事实
        # （调用的是真实 invoke，非替身）。凭据形状文本先扫描再打印。
        cls.invocations = {"n": 0}
        cls.stage_outputs = {}
        real_invoke = cls.adapter.invoke

        def _safe_text(value, limit=400):
            text = value if isinstance(value, str) else repr(value)
            if any(marker in text.lower() for marker in SECRET_MARKERS):
                return "<redacted:credential-shape>"
            return text.replace("\n", " ")[:limit]

        def _counting_invoke(request):
            cls.invocations["n"] += 1
            started = _time.monotonic()
            result = real_invoke(request)
            trace = getattr(result, "trace", None)
            status = getattr(result.status, "value", str(result.status))
            print(f"INVOCATION_EVIDENCE: gemini:{request.role} "
                  f"status={status} "
                  f"exit_code={getattr(trace, 'exit_code', None)} "
                  f"duration_s={round(_time.monotonic() - started, 1)} "
                  f"error={_safe_text(trace.error) if trace and trace.error else None}")
            if request.role in ("architect", "coder", "tester", "reviewer"):
                cls.stage_outputs[request.role] = result.output
            return result

        cls.adapter.invoke = _counting_invoke

    def test_01_discovery_and_identity(self):
        # G1 discovery 真实可用；G2 identity 与 adapter 声明规则一致。
        fact = self.adapter.discover()
        if not fact.available:
            self.skipTest(f"SKIPPED / UNAVAILABLE: {fact.reason}")
        self.assertEqual(fact.runtime, "gemini-cli")
        self.assertEqual(self.adapter.profile.runtime, "gemini-cli")
        self.assertEqual(self.adapter.profile.provider, "google")
        print("GEMINI_DISCOVERY:", fact.version)

    def test_02_authentication_observation(self):
        # G3 只读观测 CLI 自身认证状态（零 login / 零刷新 / 零凭据）；
        # G4 观测到的认证之上 provider/model 检查如实可用。
        from runtime_status import AuthenticationState
        auth = self.adapter.check_authentication()
        state = auth.state.value if hasattr(auth.state, "value") else auth.state
        print("GEMINI_AUTH:", state)
        if auth.state is not AuthenticationState.AUTHENTICATED:
            self.skipTest(f"SKIPPED / AUTH_REQUIRED: {state}")
        check = self.adapter.check_provider_model()
        reason = check.reason_code.value if check.reason_code else None
        print("GEMINI_PROVIDER_MODEL:", check.provider, check.model,
              check.available, reason)
        self.assertTrue(check.available, reason)

    def test_03_qualification_and_admission(self):
        # G5-G9 + G11：既有 bootstrap_runtime_session + run_real_validation
        # （G1-G14 链）是唯一 qualification/admission 权威。
        from discovery_bootstrap import bootstrap_runtime_session
        from real_validation_executor import run_real_validation
        from runtime_adapter_registry import AdapterDescriptor, AdapterRegistry
        home = Path.home()
        protected_paths = (
            home / ".gemini" / "oauth_creds.json",
            home / ".gemini" / "settings.json",
            home / ".gemini" / "gemini_settings.json",
        )
        registry = AdapterRegistry()
        registry.register(AdapterDescriptor(
            runtime_id="gemini-cli", provider_id="google", model_id=None,
            runtime_type="coding-agent", display_name="Gemini CLI",
            adapter_factory=lambda: self.adapter,
            config_fingerprint="installed"))

        def qualify(instance):
            validation, _executor = run_real_validation(
                instance, instance.probe, timeout_seconds=300.0,
                protected_paths=protected_paths,
                experiment_id="gemini-real-qualification")
            return validation

        session = bootstrap_runtime_session(
            registry, evidence={}, qualifier=qualify,
            required_capabilities=CAPS_ALL)
        self.assertEqual(len(session.entries), 1)
        entry = session.entries[0]
        # 封闭摘要（无 prompt / 无原始输出 / 无凭据）。
        print("GEMINI_SESSION:", {
            "discovery_available": entry.discovery_available,
            "health": entry.health_status,
            "validation": entry.validation_status,
            "provenance": entry.provenance,
            "capabilities": len(entry.capabilities),
            "admitted": entry.admitted,
            "reason": entry.reason,
            "qualification_count": session.qualification_count,
        })
        if not entry.discovery_available:
            self.skipTest(f"SKIPPED / UNAVAILABLE: {entry.reason}")
        if entry.health_status != "READY":
            self.skipTest(f"SKIPPED / {entry.health_status}")
        self.assertEqual(entry.validation_status, "VERIFIED")
        self.assertEqual(entry.provenance, "REAL")
        self.assertEqual(len(entry.capabilities), len(CAPS_ALL))
        self.assertTrue(entry.admitted, entry.reason)
        pool_identities = [list(identity) for identity in session.pool.identities()]
        self.assertIn(list(GEMINI_IDENTITY), pool_identities)
        # 保留真实 qualification 产物（复用，绝不重建）。
        RealGeminiQualificationTests.gemini_validation = \
            session.evidence[tuple(GEMINI_IDENTITY)]
        print("GEMINI_INVOCATIONS_SO_FAR:", self.invocations["n"])

    def test_04_agent_composition_four_stage(self):
        # G10 + V3 链：复用 test_03 的 REAL 证据（不再 qualification），
        # 声明 → 组合根 → ProductionFacade → 冻结 V2 四阶段，恰好 4 次调用。
        validation = getattr(RealGeminiQualificationTests,
                             "gemini_validation", None)
        if validation is None:
            self.skipTest("qualification did not complete on this machine")
        from agent_host import build_facade_from_agents
        from agent_identity import AgentIdentity, AgentRuntimeBinding
        from agent_manifest import AgentManifest, AgentRegistry
        from generic_runtime_health import GenericRuntimeHealth
        from mode_gate import Mode
        from runtime_discovery import RuntimeCandidate
        from runtime_status import RuntimeState
        # health 输入由真实管线派生（绝不伪造 READY 快照）。
        candidate = RuntimeCandidate(
            runtime_id="gemini-cli", runtime_type="coding-agent",
            display_name="Gemini CLI", available=True,
            executable=self.adapter.executable, version=None)
        health_result = GenericRuntimeHealth().check(candidate, self.adapter)
        health_state = health_result.status.status
        print("GEMINI_HEALTH_FOR_COMPOSITION:", health_state.value)
        if health_state is not RuntimeState.READY:
            self.skipTest(f"SKIPPED / {health_state.value}")
        manifest = AgentManifest(
            binding=AgentRuntimeBinding(
                agent=AgentIdentity(agent_id="gemini-agent"),
                runtime_identity=tuple(GEMINI_IDENTITY)),
            declared_roles=("architect", "coder", "tester", "reviewer"),
            adapter_factory=lambda: self.adapter)
        registry = AgentRegistry()
        registry.register(manifest)
        before = self.invocations["n"]
        facade, attribution = build_facade_from_agents(
            registry, ("gemini-agent",),
            {tuple(GEMINI_IDENTITY): validation},
            {"gemini-cli": health_result.status},
            timeout_seconds=300.0)
        self.assertEqual(sorted(attribution.values()),
                         ["gemini-agent"] * 4)
        result = facade.run(task_id="gemini-real-4stage", task=TASK,
                            prompt=TASK, mode=Mode.ON)
        print("REAL_OUTCOME_STATUS:", result.status)
        print("FACADE_PATH:", result.path)
        print("FACADE_STAGES:", result.stages)
        print("FACADE_FAILURE_CATEGORY:", result.failure_category)
        print("FACADE_PROVENANCE:", result.safe_summary["provenance"])
        # 预算：本测试恰好 4 次真实调用（四个阶段）。
        self.assertEqual(self.invocations["n"] - before, 4)
        # G11 全链：provenance 来自真实 qualification，经组合根进入结果。
        self.assertEqual(result.safe_summary["provenance"], "REAL")
        if result.status != "SUCCESS":
            for role, raw in sorted(self.stage_outputs.items()):
                text = raw if isinstance(raw, str) else repr(raw)
                if any(marker in text.lower() for marker in SECRET_MARKERS):
                    text = "<redacted:credential-shape>"
                print(f"STAGE_OUTPUT_DIAGNOSIS:{role}:",
                      text[:400].replace("\n", " "))
        self.assertEqual(result.status, "SUCCESS", result.failure_category)
        self.assertEqual(result.path, "FOUR_STAGE")
        self.assertEqual(result.stages,
                         ("architect", "coder", "tester", "reviewer"))
        print("GEMINI_TOTAL_INVOCATIONS:", self.invocations["n"])


if __name__ == "__main__":
    unittest.main()
