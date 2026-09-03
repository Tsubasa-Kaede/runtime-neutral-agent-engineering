"""R7-A2 (re-issue): advisory precheck + host-build combination E2E.

两个授权缺口（其余 A2 交付已在 d4171d7 提交）：
1. 入口层 advisory 预检：min/max distinct 超过四角色槽位（4）是纯配置
   错误，CLI 构造期拒绝，零 facade 访问、零 invocation —— engine 永不
   感知该规则（collaboration_policy.py 不含 4）。
2. 组合 E2E：CLI argv -> policy_from_args -> host.build_facade(offline
   mock adapter) -> run_cli -> facade.run 的真实宿主链路（不只测 parser）。

全部离线：不触 runtime、不读环境、不走网络。
"""
import json
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import cli
import host
from cli import run_cli
from candidate_validation import (
    CandidateValidationResult,
    CandidateValidationStatus,
    GateResult,
    GateVerdict,
    ValidationGate,
)
from collaboration_policy import CollaborationPolicy
from external_runtime import InvocationResult, InvocationStatus, InvocationTrace
from mode_gate import Mode
from production_facade import ProductionFacade
from runtime_status import (
    HealthEvidence,
    ReasonCode,
    RuntimeState,
    RuntimeStatus,
)

TASK_COMPLEX = "redesign architecture across modules"
TASK_SIMPLE = "fix one simple bug"

IDENTITY = ("rt-a", "provider-h", None, "fp-h")
IDENTITY_B = ("rt-b", "provider-b", None, "fp-b")
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


class RecordingFacade:
    """Counts facade.run calls; never invokes an adapter."""

    def __init__(self):
        self.calls = []

    def run(self, **kwargs):
        self.calls.append(kwargs)
        raise AssertionError("precheck tests never reach the facade")


def trace():
    return InvocationTrace(
        invocation_id="inv-h2", task_id="t", agent_id="a", runtime="rt",
        provider=None, model=None, role=None, status=InvocationStatus.SUCCESS,
        started_at=0.0, finished_at=0.0, duration_ms=1, exit_code=0,
        input_tokens="unknown", output_tokens="unknown", error=None)


class RoleAdapter:
    """Offline adapter answering every role address with a valid packet.
    Counts invocations so policy prechecks can prove zero-invocation."""

    def __init__(self):
        self.invocations = 0

    def discover(self):
        from external_runtime import RuntimeDiscovery
        return RuntimeDiscovery("rt", True, "1.0", None, frozenset())

    def check_authentication(self):
        from runtime_health import AuthenticationCheck
        from runtime_status import AuthenticationState
        return AuthenticationCheck(AuthenticationState.AUTHENTICATED, "oauth")

    def check_provider_model(self):
        from runtime_health import ProviderModelCheck
        return ProviderModelCheck("p", None, True, ReasonCode.NONE)

    def cancel(self, invocation_id):
        return InvocationResult(InvocationStatus.CANCELLED)

    def invoke(self, request):
        self.invocations += 1
        for role, packet in (("architect", ARCH_P), ("coder", IMPL_P),
                             ("tester", TEST_P), ("reviewer", REVIEW_P)):
            if request.agent_id == role or request.agent_id.endswith(f',"{role}"]'):
                return InvocationResult(
                    InvocationStatus.SUCCESS, output=json.dumps(packet),
                    trace=trace())
        return InvocationResult(InvocationStatus.SUCCESS, output="OK", trace=trace())


def validation_result(identity=IDENTITY):
    return CandidateValidationResult(
        identity=identity, status=CandidateValidationStatus.VERIFIED,
        gates_passed=frozenset(ValidationGate),
        gate_results=tuple(GateResult(g, GateVerdict.PASS) for g in ValidationGate),
        block_reason=None, failure_point=None, experiment_id="host-exp2",
        executed_at=0.0, validated_capabilities=CAPS_ALL, evidence={},
        provenance="OFFLINE")


def health_for(*identities):
    return {i[0]: RuntimeStatus(
        runtime_id=i[0], executable="e", version="1", status=RuntimeState.READY,
        provider=i[1], model=None, auth_method=None, reason_code=ReasonCode.NONE,
        evidence=HealthEvidence("d", "a", "p", "m", "ok"),
        checked_at=0.0, expires_at=1.0) for i in identities}


def _args(runtimes=None, min_runtimes=None, max_runtimes=None,
          no_runtime_reuse=False):
    class A:
        pass
    a = A()
    a.runtimes = runtimes
    a.min_runtimes = min_runtimes
    a.max_runtimes = max_runtimes
    a.no_runtime_reuse = no_runtime_reuse
    return a


# ---------------------------------------------------------------------------
# 五：Advisory 预检 —— 纯配置层，engine 不感知
# ---------------------------------------------------------------------------


class AdvisoryPrecheckTests(unittest.TestCase):
    """min/max > 4（四角色槽位）在入口层构造期拒绝；4 本身合法。"""

    def _reject(self, argv):
        facade = RecordingFacade()
        with self.assertRaises(SystemExit) as ctx:
            run_cli(facade, argv)
        self.assertNotEqual(ctx.exception.code, 0)
        self.assertEqual(facade.calls, [])  # 零 facade 访问

    def test_min_above_role_slots_rejected_before_facade(self):
        self._reject(["run", "--min-runtimes", "5", TASK_COMPLEX])

    def test_max_above_role_slots_rejected_before_facade(self):
        self._reject(["run", "--max-runtimes", "5", TASK_COMPLEX])

    def test_min_equals_four_is_valid(self):
        policy = cli.policy_from_args(_args(min_runtimes=4))
        self.assertEqual(policy.min_distinct_runtimes, 4)

    def test_max_equals_four_is_valid(self):
        policy = cli.policy_from_args(_args(max_runtimes=4))
        self.assertEqual(policy.max_distinct_runtimes, 4)

    def test_precheck_is_config_only_no_engine_knowledge(self):
        # engine（collaboration_policy）不得感知槽位常量：source-scan。
        source = (Path(__file__).resolve().parents[1]
                  / "dual-agent-development" / "scripts"
                  / "collaboration_policy.py").read_text(encoding="utf-8")
        self.assertNotIn("ROLE_SLOT", source)
        self.assertNotIn("role_slot", source)

    def test_precheck_probes_nothing(self):
        # 预检不查询 health / auth / pool / runtime：cli 源码不含探测调用。
        import cli as module
        source = Path(module.__file__).read_text(encoding="utf-8")
        for forbidden in ("check_authentication", "check_provider_model",
                          "minimal_health_check", ".identities()", "invoke("):
            self.assertNotIn(forbidden, source)


# ---------------------------------------------------------------------------
# 八：组合 E2E —— CLI argv -> policy -> host build -> facade.run
# ---------------------------------------------------------------------------


class HostBuildPolicyE2ETests(unittest.TestCase):
    def _facade(self, adapters):
        """host.build_facade over an offline adapter per validation."""
        facades = []
        for adapter, identity in adapters:
            facades.append(host.build_facade(
                adapter, validation_result(identity),
                health_for(*[i for _, i in adapters])))
        return facades

    def test_cli_policy_flows_through_host_facade_run(self):
        adapter = RoleAdapter()
        facade = host.build_facade(adapter, validation_result(IDENTITY),
                                   health_for(IDENTITY))
        self.assertIsInstance(facade, ProductionFacade)
        summary = json.loads(run_cli(facade, [
            "run", "--mode", "on", "--min-runtimes", "1", TASK_COMPLEX]))
        # 单 runtime Host + min=1：诚实满足（无 COUNT_UNSATISFIED 借口）。
        self.assertEqual(summary["path"], "FOUR_STAGE")
        self.assertEqual(summary["status"], "SUCCESS")
        self.assertEqual(adapter.invocations, 4)  # 四阶段固定槽位

    def test_cli_default_policy_runs_historical_four_stage(self):
        adapter = RoleAdapter()
        facade = host.build_facade(adapter, validation_result(IDENTITY),
                                   health_for(IDENTITY))
        summary = json.loads(run_cli(facade, ["run", "--mode", "on", TASK_COMPLEX]))
        self.assertEqual(summary["path"], "FOUR_STAGE")
        self.assertEqual(summary["status"], "SUCCESS")
        self.assertEqual(summary["provenance"], "OFFLINE")

    def test_min_two_single_runtime_is_honest_not_backfilled(self):
        # Host 只有 1 个 admitted runtime；--min-runtimes 2 → 诚实
        # POLICY_COUNT_UNSATISFIED（A3 可观察），绝不自动补 runtime。
        adapter = RoleAdapter()
        facade = host.build_facade(adapter, validation_result(IDENTITY),
                                   health_for(IDENTITY))
        summary = json.loads(run_cli(facade, [
            "run", "--mode", "on", "--min-runtimes", "2", TASK_COMPLEX]))
        self.assertEqual(summary["status"], "SUCCESS")
        self.assertEqual(adapter.invocations, 4)
        # run_cli 以 task 文本作为 task_id —— ledger 按同一键取。
        history = facade.state.history(TASK_COMPLEX)
        reasons = [r.reason for r in history
                   if getattr(r, "direction", None) is not None
                   and r.direction.value == "DECISION"]
        self.assertTrue(any("POLICY_COUNT_UNSATISFIED" in reason
                            for reason in reasons), reasons)


class ModeOrthogonalityThroughHostTests(unittest.TestCase):
    """六：MODE 组合正交 —— 通过真实 host facade + run_cli 验证。"""

    def _run(self, argv):
        adapter = RoleAdapter()
        facade = host.build_facade(adapter, validation_result(IDENTITY),
                                   health_for(IDENTITY))
        return json.loads(run_cli(facade, argv)), adapter

    def test_off_mode_policy_is_inert(self):
        summary, adapter = self._run(
            ["run", "--mode", "off", "--min-runtimes", "2", "any task"])
        self.assertEqual(summary["path"], "OFF")
        self.assertEqual(adapter.invocations, 0)

    def test_auto_simple_policy_is_inert_fast_path(self):
        summary, adapter = self._run(
            ["run", "--min-runtimes", "2", TASK_SIMPLE])
        self.assertEqual(summary["path"], "SINGLE")
        self.assertEqual(adapter.invocations, 1)

    def test_auto_complex_policy_engages_collaboration(self):
        summary, adapter = self._run(
            ["run", "--min-runtimes", "2", TASK_COMPLEX])
        self.assertEqual(summary["path"], "FOUR_STAGE")

    def test_on_policy_engages_collaboration(self):
        summary, adapter = self._run(
            ["run", "--mode", "on", "--min-runtimes", "2", TASK_COMPLEX])
        self.assertEqual(summary["path"], "FOUR_STAGE")

    def test_min_runtimes_never_upgrades_simple_to_collaboration(self):
        # 反向禁止：min=3 不把 SIMPLE 升级成 collaboration。
        summary, adapter = self._run(
            ["run", "--mode", "auto", "--min-runtimes", "3", TASK_SIMPLE])
        self.assertEqual(summary["path"], "SINGLE")
        self.assertEqual(adapter.invocations, 1)


if __name__ == "__main__":
    unittest.main()
