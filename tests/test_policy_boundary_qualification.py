"""R7-C: CollaborationPolicy boundary qualification (offline matrix).

锁定授权矩阵中的四个缺口语义（其余边界已由既有套件证明，各 case
注释中标注来源）：

- Case A: allowlist 点名不存在的 runtime —— 经真实 CLI argv +
  host.build_facade 组合链（R7-A2 host E2E 只锁了 min-runtimes 形态，
  未锁 absent-allowlist 形态）：诚实 DUAL_NO_CAPABLE_AGENT 终态、零
  invocation、零探测（discover/auth/provider 计数器）、reason 经 A3
  通道携带 POLICY_RUNTIME_ABSENT、零 envelope、ON 绝不 fallback。
  （assigner 层的缺席报告/成员关系已由 test_collaboration_policy.py
  与 test_policy_reason_observability.py 锁定。）
- Case B: 点名 runtime 不在 Verified Pool —— 三重锁：admission 不因
  点名而提升（FAILED 与 health DOWN 均 REJECTED，既有
  test_verified_pool_boundary.py 的组合形态）；health READY 但未
  admitted 的 runtime 不能偷渡进 bridge 候选；absent+present 混合
  allowlist 下 assignment 收敛于在场成员、缺席如实报告、非成员绝不
  invoke（envelope 成员关系证明）。
- Case D: max_distinct_runtimes=1 是 distinct-runtime 界，不是
  invocation 界 —— 四阶段 invocation 形状（4 次调用、4 个角色槽位、
  逐阶段 reserve-before-invoke、canonical 5 条 ledger）完全不变。
  （assignment 收敛语义已由 test_max_one_converges_* 锁定；此处补
  invocation/预算不变性。）
- Case F: allowlist=(A,B) + min=3 + max=2 组合冲突 —— CLI 构造期拒绝、
  零 facade 访问（既有 test_min_above_max_rejected_before_facade 只锁
  了无 allowlist 的 min>max 形态）。
- 安全边界: reason 不携带 task/prompt 原文、候选对象字段、packet
  payload 字段名；构造期拒绝消息只点名规则与 marker，绝不携带被拒值
  本身；policy 运行前后 pool 身份集合逐字不变（绝不扩池）。

全部离线：不触 runtime、不读环境、不走网络、零 REAL 路径。既有测试
文件零修改；生产代码零修改（本轮无 RED —— 语义已由 R7-A 链证明正确，
本文件是纯边界回归锁，RED 只在存在生产缺陷时才可能出现）。
"""
import json
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from cli import build_parser, policy_from_args, run_cli
from collaboration_policy import CollaborationPolicy
from collaboration_orchestrator import CollaborationOrchestrator
from collaboration_session import CollaborationSession, collab_agent_address
from collaboration_state import CollaborationDirection
from content_safety import contains_unsafe_content
from external_runtime import InvocationResult, InvocationStatus, InvocationTrace
from host import build_facade
from mode_gate import Mode
from production_facade import ProductionFacade
from remote_transport import LoopbackRemoteTransport
from loop_guard import LoopGuard
from task_budget import BudgetUsage, TaskBudget
from verified_runtime_pool import AdmissionKind
from verified_selection_bridge import VerifiedSelectionBridge

from candidate_validation import (
    CandidateValidationResult,
    CandidateValidationStatus,
)

from test_policy_entry_host import (  # R7-A2 既有 host fixtures
    IDENTITY,
    RoleAdapter,
    health_for,
    validation_result,
)
from test_collaboration_policy_entry import RecordingFacade
from tests.test_helpers_entry import (
    ALL_CAPS,
    CLAUDE_ENTRY as X,
    CODEX_ENTRY as Y,
    PI_ENTRY as Z,
    RepeatingAdapter,
    arch_dict,
    compose_entry_facade,
    decision_reasons,
    envelope_runtime_ids,
    health_ready,
    impl_dict,
    make_pool,
    review_dict,
    role_runtime_ids,
)
from tests.test_helpers_entry import test_dict as _tester_packet_factory

TASK_COMPLEX = "redesign architecture across modules"
TASK_QUALIFY = "TQ"

_ROLE_REQUIREMENTS = {"architect": ("architecture",), "coder": ("coding",),
                      "test": ("testing",), "review": ("review",)}


# ---------------------------------------------------------------------------
# Case A: absent allowlist through the real CLI + host chain
# ---------------------------------------------------------------------------


class ProbingRoleAdapter(RoleAdapter):
    """RoleAdapter + 探测计数器：证明 absent-allowlist 边界运行期间
    discover/auth/provider 零探测（零 invocation 之外的第二重证据）。"""

    def __init__(self):
        super().__init__()
        self.probe_counts = {"discover": 0, "check_authentication": 0,
                             "check_provider_model": 0}

    def discover(self):
        self.probe_counts["discover"] += 1
        return super().discover()

    def check_authentication(self):
        self.probe_counts["check_authentication"] += 1
        return super().check_authentication()

    def check_provider_model(self):
        self.probe_counts["check_provider_model"] += 1
        return super().check_provider_model()


class CaseAAbsentAllowlistEntryChainTests(unittest.TestCase):
    """Case A：allowlist 点名不存在的 runtime —— CLI argv -> policy_from_args
    -> host facade.run 的真实组合链。"""

    def _run_cli_boundary(self, argv):
        adapter = ProbingRoleAdapter()
        facade = build_facade(adapter, validation_result(IDENTITY),
                              health_for(IDENTITY))
        summary = json.loads(run_cli(facade, argv))
        # run_cli 以 task 文本作为 task_id —— ledger 按同一键取。
        history = facade.state.history(argv[-1])
        return adapter, summary, history

    def test_two_absent_allowlist_cli_host_honest_terminal(self):
        adapter, summary, history = self._run_cli_boundary([
            "run", "--mode", "on", "--runtimes", "no-such-1,no-such-2",
            TASK_COMPLEX])
        # 既有诚实终态：绝不因 policy 压力伪造候选或绕过 policy。
        self.assertEqual(summary["status"], "DUAL_NO_CAPABLE_AGENT")
        self.assertEqual(summary["path"], "DUAL")
        self.assertEqual(summary["stages"], [])
        # 零 invocation、零探测：不查 runtime、不试运行。
        self.assertEqual(adapter.invocations, 0)
        self.assertEqual(adapter.probe_counts,
                         {"discover": 0, "check_authentication": 0,
                          "check_provider_model": 0})
        # reason 经 A3 通道点名缺席（封闭词表 + runtime id 列表）。
        reasons = decision_reasons(history)
        self.assertTrue(
            any("POLICY_RUNTIME_ABSENT=no-such-1,no-such-2" in reason
                for reason in reasons), reasons)
        # 零 envelope（零调用）；ON 绝不 fallback（只有 DECISION+FAILURE）。
        directions = [r.direction for r in history]
        self.assertIn(CollaborationDirection.DECISION, directions)
        self.assertIn(CollaborationDirection.FAILURE, directions)
        self.assertNotIn(CollaborationDirection.REQUEST, directions)
        self.assertNotIn(CollaborationDirection.REPLY, directions)
        # 终态表面 secret-safe。
        self.assertFalse(contains_unsafe_content(" ".join(reasons)))

    def test_single_absent_entry_with_explicit_min_one_stays_honest(self):
        # 单条缺席 + 显式 --min-runtimes 1：入口不把单条 allowlist 静默
        # 拓宽，也不因 min 可满足而绕过缺席。
        adapter, summary, history = self._run_cli_boundary([
            "run", "--mode", "on", "--runtimes", "no-such-1",
            "--min-runtimes", "1", TASK_COMPLEX])
        self.assertEqual(summary["status"], "DUAL_NO_CAPABLE_AGENT")
        self.assertEqual(adapter.invocations, 0)
        reasons = decision_reasons(history)
        self.assertTrue(any("POLICY_RUNTIME_ABSENT=no-such-1" in reason
                            for reason in reasons), reasons)


# ---------------------------------------------------------------------------
# Case B: named runtime absent from the Verified Pool
# ---------------------------------------------------------------------------


class CaseBPoolMembershipTests(unittest.TestCase):
    """Case B：用户点名不改变 admission —— 绝不提升、绝不偷渡。"""

    def test_named_runtime_is_not_promoted_by_naming(self):
        pool = make_pool((X, Y))
        before = pool.identities()
        # FAILED 验证结果的点名 runtime：REJECTED，绝不提升为 VERIFIED。
        failed = CandidateValidationResult(
            identity=Z, status=CandidateValidationStatus.FAILED,
            gates_passed=frozenset(), gate_results=(),
            block_reason="boundary-probe", failure_point=None,
            experiment_id="", executed_at=1.0,
            validated_capabilities=ALL_CAPS, evidence={})
        outcome = pool.admit(failed, ALL_CAPS, health_now="READY")
        self.assertIs(outcome.kind, AdmissionKind.REJECTED)
        # VERIFIED 但 health DOWN：同样 REJECTED（不偷渡）。
        outcome_down = pool.admit(validation_result(Z), ALL_CAPS,
                                  health_now="DOWN")
        self.assertIs(outcome_down.kind, AdmissionKind.REJECTED)
        # 两次拒绝后 pool 身份集合不变。
        self.assertEqual(pool.identities(), before)

    def test_ready_health_cannot_smuggle_unadmitted_runtime(self):
        # health 快照 KNOWS rt-zz（READY）但 pool 未 admit：bridge 候选
        # 集绝不包含 rt-zz —— 候选只能来自 pool.identities() 的投影。
        pool = make_pool((X, Y))
        health = {identity[0]: health_ready(identity[0])
                  for identity in (X, Y)}
        health["rt-zz"] = health_ready("rt-zz")
        bridge = VerifiedSelectionBridge()
        for role, requirements in _ROLE_REQUIREMENTS.items():
            candidates = bridge.candidates_for(pool, health, role,
                                               requirements)
            for candidate in candidates.candidates:
                self.assertNotEqual(candidate.runtime_id, "rt-zz", role)

    def test_named_absent_plus_present_member_e2e_membership(self):
        # allowlist=(缺席 rt-zz + 在场 rt-x)：assignment 收敛于在场成员，
        # 缺席如实报告，非成员 rt-y 绝不 invoke（envelope 成员关系证明）。
        facade = compose_entry_facade((X, Y))
        policy = CollaborationPolicy(runtime_allowlist=("rt-zz", "rt-x"))
        result = facade.run(task_id="TB", task=TASK_COMPLEX, prompt="p",
                            mode=Mode.ON, policy=policy)
        self.assertEqual(result.status, "SUCCESS", result.failure_category)
        self.assertEqual(result.path, "FOUR_STAGE")
        history = facade.state.history("TB")
        used = envelope_runtime_ids(history)
        self.assertEqual(used, {"rt-x"})
        reasons = decision_reasons(history)
        self.assertTrue(any("POLICY_RUNTIME_ABSENT=rt-zz" in reason
                            for reason in reasons), reasons)


# ---------------------------------------------------------------------------
# Case D: max_distinct_runtimes is a distinct-runtime bound, not an
# invocation bound — invocation/budget shape invariance
# ---------------------------------------------------------------------------


def trace_for(task_id=TASK_QUALIFY):
    return InvocationTrace(
        invocation_id="inv-bq", task_id=task_id, agent_id="a", runtime="rt",
        provider=None, model=None, role=None, status=InvocationStatus.SUCCESS,
        started_at=1.0, finished_at=2.0, duration_ms=10, exit_code=0,
        input_tokens="unknown", output_tokens="unknown", error=None)


def ok_result(payload_dict, task_id=TASK_QUALIFY):
    return InvocationResult(InvocationStatus.SUCCESS,
                            output=json.dumps(payload_dict),
                            trace=trace_for(task_id))


def compose_counting_facade(identities):
    """暴露 pool/usage/adapters 的离线组合（helpers 的 compose_entry_facade
    隐藏它们；边界测试需要数 invocation 与检查 pool）。"""
    budget = TaskBudget(8, 8, timeout_seconds=30.0)
    usage = BudgetUsage()
    guard = LoopGuard()
    collab = {}
    verify = {}
    for identity in identities:
        for role, payload in (("architect", arch_dict(TASK_QUALIFY)),
                              ("coder", impl_dict(TASK_QUALIFY))):
            collab[collab_agent_address(identity, role)] = \
                RepeatingAdapter(ok_result(payload))
        for role, payload in (("tester", _tester_packet_factory(TASK_QUALIFY)),
                              ("reviewer", review_dict(TASK_QUALIFY))):
            verify[collab_agent_address(identity, role)] = \
                RepeatingAdapter(ok_result(payload))

    def session_factory():
        return CollaborationSession(LoopbackRemoteTransport(), collab,
                                    budget, usage, guard)

    pool = make_pool(identities)
    health = {identity[0]: health_ready(identity[0])
              for identity in identities}
    orchestrator = CollaborationOrchestrator(
        object(), pool, health, budget, usage, guard, session_factory)
    facade = ProductionFacade(orchestrator, verify, pool, health,
                              budget, usage, guard)
    adapters = list(collab.values()) + list(verify.values())
    return facade, pool, usage, adapters


class CaseDMaxOneInvocationInvarianceTests(unittest.TestCase):

    def test_max_one_keeps_four_stage_invocation_shape(self):
        facade, _pool, usage, adapters = compose_counting_facade((X, Y, Z))
        policy = CollaborationPolicy(max_distinct_runtimes=1)
        result = facade.run(task_id=TASK_QUALIFY, task=TASK_COMPLEX,
                            prompt="p", mode=Mode.ON, policy=policy)
        # distinct-runtime 界成立：assignment 只用一个 runtime。
        self.assertEqual(result.status, "SUCCESS", result.failure_category)
        self.assertEqual(result.path, "FOUR_STAGE")
        history = facade.state.history(TASK_QUALIFY)
        self.assertEqual(envelope_runtime_ids(history), {"rt-x"})
        per_role = role_runtime_ids(history)
        self.assertEqual(set(per_role.values()), {"rt-x"})
        # invocation 界完全不变：四角色各一次，管线形状决定 —— max=1
        # 绝不理解为"一次 invocation"。
        self.assertEqual(result.stages,
                         ("architect", "coder", "tester", "reviewer"))
        role_counts = {}
        for adapter in adapters:
            for request in adapter.requests:
                role_counts[request.role] = \
                    role_counts.get(request.role, 0) + 1
        self.assertEqual(role_counts,
                         {"architect": 1, "coder": 1,
                          "tester": 1, "reviewer": 1})
        # budget reserve-before-invoke：逐阶段 4 次预留，界不变。
        self.assertEqual(usage.total_agent_calls, 4)
        self.assertEqual(usage.architect_calls, 1)
        self.assertEqual(usage.coder_calls, 1)
        self.assertEqual(usage.test_calls, 1)
        self.assertEqual(usage.review_calls, 1)
        # canonical ledger 形状不变：1 DECISION + 4 envelope。
        self.assertEqual(
            [r.payload_type for r in history],
            ["", "ARCHITECTURE", "IMPLEMENTATION", "TEST", "REVIEW"])

    def test_max_only_cli_maps_to_distinct_bound_not_min(self):
        # CLI max-only：min 保持 None（部署默认 min 不注入）——max 是
        # distinct-runtime 界，与 invocation 数、min 均无耦合。
        args = build_parser().parse_args(
            ["run", "--runtimes", "rt-x,rt-y,rt-z", "--max-runtimes", "1",
             TASK_COMPLEX])
        policy = policy_from_args(args)
        self.assertEqual(policy.max_distinct_runtimes, 1)
        self.assertIsNone(policy.min_distinct_runtimes)
        self.assertEqual(policy.runtime_allowlist, ("rt-x", "rt-y", "rt-z"))


# ---------------------------------------------------------------------------
# Case F: conflicting allowlist + min + max combination
# ---------------------------------------------------------------------------


class CaseFConflictingBoundsTests(unittest.TestCase):

    def test_allowlist_min_max_conflict_rejected_before_facade(self):
        # 授权矩阵原始形态：allowlist=(A,B) + min=3 + max=2 —— 构造期
        # 拒绝（min>max），零 facade 访问、零 runtime 触碰。
        facade = RecordingFacade()
        with self.assertRaises(SystemExit) as ctx:
            run_cli(facade, ["run", "--mode", "on", "--runtimes",
                             "rt-a,rt-b", "--min-runtimes", "3",
                             "--max-runtimes", "2", TASK_COMPLEX])
        self.assertNotEqual(ctx.exception.code, 0)
        self.assertEqual(facade.calls, [])  # 零 facade 访问
        # 拒绝消息点名规则（封闭词汇），不携带自由文本。
        self.assertIn("must not exceed", str(ctx.exception))


# ---------------------------------------------------------------------------
# 安全边界（授权第三节）
# ---------------------------------------------------------------------------


class BoundarySecurityTests(unittest.TestCase):

    def test_reasons_carry_no_task_prompt_candidate_or_payload_material(self):
        task_text = ("redesign architecture across modules for the "
                     "boundary qualification matrix")
        facade = compose_entry_facade((X, Y))
        policy = CollaborationPolicy(runtime_allowlist=("rt-zz", "rt-x"))
        result = facade.run(task_id="TSEC", task=task_text, prompt="p",
                            mode=Mode.ON, policy=policy)
        self.assertEqual(result.status, "SUCCESS", result.failure_category)
        reasons = decision_reasons(facade.state.history("TSEC"))
        self.assertTrue(reasons)
        surface = " ".join(reasons).lower()
        # 不携带 task/prompt 原文、不携带候选对象字段、不携带 packet
        # payload 字段名、不携带认证词形。
        for forbidden in (task_text.lower(), "prompt", "fp-x", "provider-x",
                          "model-x", "agent_id", "capabilities", "rank",
                          "changed_files", "implementation_summary",
                          "acceptance_criteria", "correlation"):
            self.assertNotIn(forbidden, surface, forbidden)
        # reason 与封闭结果表面均无 credential 形状。
        self.assertFalse(contains_unsafe_content(" ".join(reasons)))
        self.assertFalse(contains_unsafe_content(repr(result)))

    def test_constructor_error_never_carries_rejected_value(self):
        secret_material = "api_key=sk-BOUNDARYMATRIX99"
        with self.assertRaises(ValueError) as ctx:
            CollaborationPolicy(
                runtime_allowlist=("claude-cli", secret_material))
        message = str(ctx.exception)
        # 被拒值本身（credential 材料）绝不出现；规则/marker 可被点名。
        self.assertNotIn(secret_material, message)
        self.assertNotIn("sk-BOUNDARYMATRIX99", message)
        self.assertIn("secret-shaped", message)
        # 同一边界经真实 CLI：拒绝消息同样不携带值，且零 facade 访问。
        facade = RecordingFacade()
        with self.assertRaises(SystemExit) as cli_ctx:
            run_cli(facade, ["run", "--runtimes",
                             f"claude-cli,{secret_material}", TASK_COMPLEX])
        self.assertNotIn("sk-BOUNDARYMATRIX99", str(cli_ctx.exception))
        self.assertEqual(facade.calls, [])

    def test_pool_never_extended_by_policy_run(self):
        # policy 过滤绝不扩池：一次完整 facade 运行（absent-allowlist）
        # 前后 pool 身份集合逐字不变。
        facade, pool, _usage, _adapters = compose_counting_facade((X, Y))
        before = pool.identities()
        policy = CollaborationPolicy(runtime_allowlist=("rt-zz", "rt-x"))
        result = facade.run(task_id=TASK_QUALIFY, task=TASK_COMPLEX,
                            prompt="p", mode=Mode.ON, policy=policy)
        self.assertEqual(result.status, "SUCCESS", result.failure_category)
        self.assertEqual(pool.identities(), before)
        self.assertEqual(envelope_runtime_ids(
            facade.state.history(TASK_QUALIFY)), {"rt-x"})


if __name__ == "__main__":
    unittest.main()
