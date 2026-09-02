"""R7-A2: CollaborationPolicy entry-surface tests (CLI/Host/Facade/Orchestrator).

验证链：CLI flags -> CollaborationPolicy 构造 -> facade.run(..., policy=None
尾参数) -> orchestrator.run(..., policy=None) -> PolicyConstrainedAssigner。
全部离线 mock；不触 runtime、不读环境、不走网络。E2E 证明全部从最终
invocation/ledger envelope 的 identity 成员关系得出，绝不读取内部
PolicyConstrainedAssigner 属性。
"""
import inspect
import json
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import cli
import host
import production_facade
import collaboration_orchestrator
from cli import build_parser, run_cli
from collaboration_policy import (
    CollaborationPolicy,
    PolicyConstrainedAssigner,
)
from collaboration_orchestrator import CollaborationOrchestrator
from host import HostFacade
from production_facade import FacadeResult, ProductionFacade
from mode_gate import Mode

SECRET_MARKERS = ("token", "secret", "api_key", "authorization", "bearer",
                  "stdout", "stderr")

TASK_COMPLEX = "redesign architecture across modules"
TASK_SIMPLE = "fix one simple bug"


def stub_result(status="SUCCESS", path="FOUR_STAGE"):
    return FacadeResult(status=status, mode="AUTO", path=path, task_id="T1",
                        provenance="OFFLINE",
                        stages=("architect", "coder", "tester", "reviewer"),
                        failure_category="",
                        safe_summary={"task_id": "T1", "provenance": "OFFLINE",
                                      "stage_counts": {}})


class RecordingFacade:
    """Stub facade: records every run() call; never invokes an adapter."""

    def __init__(self, result=None):
        self.result = result if result is not None else stub_result()
        self.calls = []

    def run(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


# ---------------------------------------------------------------------------
# 一、五、六：尾参数 policy=None（真实 RED：签名断言在实现前必然 FAILED）
# ---------------------------------------------------------------------------


class TrailingPolicySignatureTests(unittest.TestCase):
    def test_production_facade_run_has_trailing_policy_none(self):
        signature = inspect.signature(ProductionFacade.run)
        parameters = list(signature.parameters)
        self.assertIn("policy", parameters)
        self.assertIsNone(signature.parameters["policy"].default)
        self.assertEqual(parameters[-1], "policy")

    def test_collaboration_orchestrator_run_has_trailing_policy_none(self):
        signature = inspect.signature(CollaborationOrchestrator.run)
        parameters = list(signature.parameters)
        self.assertIn("policy", parameters)
        self.assertIsNone(signature.parameters["policy"].default)
        self.assertEqual(parameters[-1], "policy")

    def test_host_facade_run_has_trailing_policy_none(self):
        signature = inspect.signature(HostFacade.run)
        parameters = list(signature.parameters)
        self.assertIn("policy", parameters)
        self.assertIsNone(signature.parameters["policy"].default)
        self.assertEqual(parameters[-1], "policy")


# ---------------------------------------------------------------------------
# 八、九：CLI flags 解析
# ---------------------------------------------------------------------------


class CliFlagParsingTests(unittest.TestCase):
    def test_runtimes_flag_parses_comma_separated(self):
        args = build_parser().parse_args(
            ["run", "--runtimes", "claude-cli,codex-cli,pi-cli", TASK_SIMPLE])
        self.assertEqual(args.runtimes, "claude-cli,codex-cli,pi-cli")

    def test_min_and_max_runtimes_parse_as_int(self):
        args = build_parser().parse_args(
            ["run", "--min-runtimes", "2", "--max-runtimes", "3", TASK_SIMPLE])
        self.assertEqual(args.min_runtimes, 2)
        self.assertEqual(args.max_runtimes, 3)

    def test_no_runtime_reuse_defaults_false_and_flips_true(self):
        default = build_parser().parse_args(["run", TASK_SIMPLE])
        self.assertFalse(default.no_runtime_reuse)
        flagged = build_parser().parse_args(["run", "--no-runtime-reuse", TASK_SIMPLE])
        self.assertTrue(flagged.no_runtime_reuse)

    def test_all_flags_together(self):
        args = build_parser().parse_args(
            ["run", "--mode", "on", "--runtimes", "claude-cli,codex-cli",
             "--min-runtimes", "2", "--max-runtimes", "2",
             "--no-runtime-reuse", TASK_COMPLEX])
        self.assertEqual(args.mode, "on")
        self.assertEqual(args.runtimes, "claude-cli,codex-cli")
        self.assertEqual(args.min_runtimes, 2)
        self.assertEqual(args.max_runtimes, 2)
        self.assertTrue(args.no_runtime_reuse)


# ---------------------------------------------------------------------------
# 九、十一：CLI -> CollaborationPolicy 构造语义
# ---------------------------------------------------------------------------


class CliPolicyConstructionTests(unittest.TestCase):
    def test_runtimes_dedupe_and_sort(self):
        policy = cli.policy_from_args(_args(runtimes="codex-cli,claude-cli,codex-cli"))
        self.assertEqual(policy.runtime_allowlist, ("claude-cli", "codex-cli"))

    def test_min_runtimes_reaches_policy(self):
        policy = cli.policy_from_args(_args(min_runtimes=2))
        self.assertEqual(policy.min_distinct_runtimes, 2)

    def test_max_runtimes_reaches_policy(self):
        policy = cli.policy_from_args(_args(max_runtimes=1))
        self.assertEqual(policy.max_distinct_runtimes, 1)

    def test_no_runtime_reuse_maps_to_allow_runtime_reuse_false(self):
        policy = cli.policy_from_args(_args(no_runtime_reuse=True))
        self.assertFalse(policy.allow_runtime_reuse)

    def test_default_is_min_only_from_host_constant(self):
        # CLI 默认 min 来自 host.DEFAULT_MIN_DISTINCT_RUNTIMES，不是复制 magic number。
        self.assertEqual(host.DEFAULT_MIN_DISTINCT_RUNTIMES, 2)
        policy = cli.policy_from_args(_args())
        self.assertEqual(policy.min_distinct_runtimes,
                         host.DEFAULT_MIN_DISTINCT_RUNTIMES)
        self.assertIsNone(policy.max_distinct_runtimes)
        self.assertIsNone(policy.runtime_allowlist)
        self.assertTrue(policy.allow_runtime_reuse)

    def test_two_never_lives_in_core_policy_module(self):
        # «2» 不得进入 collaboration_policy.py / role_assignment.py。
        for module_name in ("collaboration_policy", "role_assignment"):
            import importlib
            module = importlib.import_module(module_name)
            source = Path(module.__file__).read_text(encoding="utf-8")
            self.assertNotIn("DEFAULT_MIN_DISTINCT_RUNTIMES", source,
                             module_name)
            self.assertNotIn("default_min", source.lower(), module_name)


class _Args:
    def __init__(self, runtimes=None, min_runtimes=None, max_runtimes=None,
                 no_runtime_reuse=False):
        self.runtimes = runtimes
        self.min_runtimes = min_runtimes
        self.max_runtimes = max_runtimes
        self.no_runtime_reuse = no_runtime_reuse


def _args(**kwargs):
    return _Args(**kwargs)


# ---------------------------------------------------------------------------
# 十、十八：非法 CLI 在构造期拒绝（零 facade 访问 / 零 invocation）
# ---------------------------------------------------------------------------


class InvalidCliTests(unittest.TestCase):
    def _reject(self, argv):
        facade = RecordingFacade()
        with self.assertRaises(SystemExit) as ctx:
            run_cli(facade, argv)
        self.assertNotEqual(ctx.exception.code, 0)
        self.assertEqual(facade.calls, [])

    def test_min_below_one_rejected_before_facade(self):
        self._reject(["run", "--min-runtimes", "0", TASK_SIMPLE])

    def test_max_below_one_rejected_before_facade(self):
        self._reject(["run", "--max-runtimes", "0", TASK_SIMPLE])

    def test_min_above_max_rejected_before_facade(self):
        self._reject(["run", "--min-runtimes", "2", "--max-runtimes", "1",
                      TASK_SIMPLE])

    def test_allowlist_cardinality_below_min_rejected_before_facade(self):
        self._reject(["run", "--runtimes", "claude-cli", "--min-runtimes", "2",
                      TASK_SIMPLE])

    def test_empty_allowlist_entry_rejected_before_facade(self):
        self._reject(["run", "--runtimes", "claude-cli,,codex-cli", TASK_SIMPLE])

    def test_secret_marker_allowlist_rejected_before_facade(self):
        self._reject(["run", "--runtimes", "claude-cli,api_key", TASK_SIMPLE])

    def test_non_integer_min_rejected_by_argparse(self):
        self._reject(["run", "--min-runtimes", "two", TASK_SIMPLE])


# ---------------------------------------------------------------------------
# 五、七、八：run_cli 把 policy 传给 facade.run
# ---------------------------------------------------------------------------


class CliPolicyThreadingTests(unittest.TestCase):
    def test_run_cli_passes_policy_object(self):
        facade = RecordingFacade()
        run_cli(facade, ["run", "--runtimes", "claude-cli,codex-cli",
                         "--min-runtimes", "2", TASK_COMPLEX])
        self.assertEqual(len(facade.calls), 1)
        policy = facade.calls[0].get("policy")
        self.assertIsInstance(policy, CollaborationPolicy)
        self.assertEqual(policy.runtime_allowlist, ("claude-cli", "codex-cli"))
        self.assertEqual(policy.min_distinct_runtimes, 2)

    def test_run_cli_passes_default_min_policy_when_no_flags(self):
        facade = RecordingFacade()
        run_cli(facade, ["run", TASK_COMPLEX])
        policy = facade.calls[0].get("policy")
        self.assertIsInstance(policy, CollaborationPolicy)
        self.assertEqual(policy.min_distinct_runtimes,
                         host.DEFAULT_MIN_DISTINCT_RUNTIMES)

    def test_run_cli_still_passes_mode_and_task(self):
        facade = RecordingFacade()
        run_cli(facade, ["run", "--mode", "on", TASK_COMPLEX])
        call = facade.calls[0]
        self.assertEqual(call["mode"], Mode.ON)
        self.assertEqual(call["task"], TASK_COMPLEX)
        self.assertEqual(call["task_id"], TASK_COMPLEX)
        self.assertEqual(call["prompt"], TASK_COMPLEX)



# ---------------------------------------------------------------------------
# 十三：policy 不改变 ModeGate 语义
# ---------------------------------------------------------------------------


class ModeSemanticsTests(unittest.TestCase):
    def test_policy_with_off_mode_still_zero_orchestration(self):
        facade = RecordingFacade()
        run_cli(facade, ["run", "--mode", "off", "--min-runtimes", "2",
                         TASK_COMPLEX])
        call = facade.calls[0]
        self.assertEqual(call["mode"].value, "OFF")

    def test_mode_kwarg_unchanged_when_policy_present(self):
        facade = RecordingFacade()
        run_cli(facade, ["run", "--mode", "on", "--no-runtime-reuse",
                         TASK_COMPLEX])
        self.assertEqual(facade.calls[0]["mode"].value, "ON")


# ---------------------------------------------------------------------------
# 十四：policy=None 零漂移（对比 run() 有/无 policy=None）
# ---------------------------------------------------------------------------


class ZeroDriftTests(unittest.TestCase):
    def _facade(self):
        from tests.test_helpers_entry import compose_entry_facade
        return compose_entry_facade()

    def test_explicit_policy_none_equals_historical_call(self):
        # 同一 facade 两次调用：一次历史形态（不传 policy），一次显式
        # policy=None —— invocation 序列、结果字段、ledger 投影全部相等。
        historical = self._facade()
        explicit = self._facade()
        result_a = historical.run(task_id="T1", task=TASK_COMPLEX, prompt="p",
                                  mode=historical_mode_on())
        result_b = explicit.run(task_id="T1", task=TASK_COMPLEX, prompt="p",
                                mode=historical_mode_on(), policy=None)
        self.assertEqual(result_a.status, result_b.status)
        self.assertEqual(result_a.mode, result_b.mode)
        self.assertEqual(result_a.path, result_b.path)
        self.assertEqual(result_a.stages, result_b.stages)
        self.assertEqual(result_a.failure_category, result_b.failure_category)
        self.assertEqual(result_a.safe_summary, result_b.safe_summary)
        history_a = historical.state.history("T1")
        history_b = explicit.state.history("T1")
        self.assertEqual([(r.sequence, r.direction, r.payload_type)
                          for r in history_a],
                         [(r.sequence, r.direction, r.payload_type)
                          for r in history_b])
        self.assertEqual(len(history_a), len(history_b))


def historical_mode_on():
    from mode_gate import Mode
    return Mode.ON


# ---------------------------------------------------------------------------
# 十五-十七：离线 mock E2E —— 从 invocation/ledger envelope identity 证明
# ---------------------------------------------------------------------------


class EntryE2ETests(unittest.TestCase):
    """A2 E2E：真实 pool + 真实 bridge + mock adapter。证明来自最终
    envelope source/target identity 的成员关系，不读内部 assigner。"""

    def _facade(self, identities):
        from tests.test_helpers_entry import compose_entry_facade
        return compose_entry_facade(identities=identities)

    def test_two_runtime_spread_via_ledger_envelope_identities(self):
        # --runtimes claude-cli,codex-cli --min-runtimes 2:
        # Architect→Claude、Coder→Codex（或既有确定性 runtime order）。
        from collaboration_session import collab_agent_address
        from tests.test_helpers_entry import (
            CLAUDE_ENTRY as X,
            CODEX_ENTRY as Y,
        )
        facade = self._facade((X, Y))
        policy = CollaborationPolicy(
            runtime_allowlist=("rt-x", "rt-y"), min_distinct_runtimes=2)
        result = facade.run(task_id="T1", task=TASK_COMPLEX, prompt="p",
                            mode=historical_mode_on(), policy=policy)
        self.assertEqual(result.status, "SUCCESS", result.failure_category)
        history = facade.state.history("T1")
        request = next(r for r in history if r.payload_type == "ARCHITECTURE")
        self.assertEqual(request.source_agent,
                         collab_agent_address(X, "architect"))
        self.assertEqual(request.target_agent,
                         collab_agent_address(Y, "coder"))
        # 成员关系证明：envelope identity 属于两个不同 runtime。
        self.assertNotEqual(request.source_agent, request.target_agent)

    def test_allowlist_excludes_codex_via_membership(self):
        # 三 runtime（rt-x/rt-y/rt-z），allowlist 只留 rt-x+rt-z：
        # rt-y 绝不出现在最终 assignment envelope identity。
        from tests.test_helpers_entry import (
            CLAUDE_ENTRY as X,
            CODEX_ENTRY as Y,
            PI_ENTRY as Z,
            envelope_runtime_ids,
        )
        facade = self._facade((X, Y, Z))
        policy = CollaborationPolicy(
            runtime_allowlist=("rt-x", "rt-z"), min_distinct_runtimes=2)
        result = facade.run(task_id="T1", task=TASK_COMPLEX, prompt="p",
                            mode=historical_mode_on(), policy=policy)
        self.assertEqual(result.status, "SUCCESS", result.failure_category)
        used = envelope_runtime_ids(facade.state.history("T1"))
        self.assertIn("rt-x", used)
        self.assertIn("rt-z", used)
        self.assertNotIn("rt-y", used)  # membership 证明，非字符串目测

    def test_min_two_unsatisfied_single_runtime_is_honest(self):
        # 单 runtime 池 + min=2（host 默认部署语义）：assignment 正常，
        # reason 如实 POLICY_COUNT_UNSATISFIED（不扩池、不 backfill）。
        from tests.test_helpers_entry import (
            CLAUDE_ENTRY as X,
            decision_reasons,
        )
        facade = self._facade((X,))
        policy = CollaborationPolicy(min_distinct_runtimes=2)
        result = facade.run(task_id="T1", task=TASK_COMPLEX, prompt="p",
                            mode=historical_mode_on(), policy=policy)
        self.assertEqual(result.status, "SUCCESS", result.failure_category)
        reasons = decision_reasons(facade.state.history("T1"))
        self.assertTrue(
            any("POLICY_COUNT_UNSATISFIED" in reason for reason in reasons),
            reasons)

    def test_max_one_converges_runtime_membership(self):
        from tests.test_helpers_entry import (
            CLAUDE_ENTRY as X,
            CODEX_ENTRY as Y,
            envelope_runtime_ids,
        )
        facade = self._facade((X, Y))
        policy = CollaborationPolicy(max_distinct_runtimes=1)
        result = facade.run(task_id="T1", task=TASK_COMPLEX, prompt="p",
                            mode=historical_mode_on(), policy=policy)
        self.assertEqual(result.status, "SUCCESS", result.failure_category)
        used = envelope_runtime_ids(facade.state.history("T1"))
        self.assertEqual(used, {"rt-x"})

    def test_no_reuse_maps_to_allow_runtime_reuse_false(self):
        # CLI --no-runtime-reuse → policy.allow_runtime_reuse=False（十七），
        # 不用其他字段表达该语义。reuse=False 的单射语义按 A1 核心定义
        # 在「一次 assign() 调用」内成立：dual 指派（architect/coder）与
        # verification 指派（tester/reviewer）是两个独立 assign 事件，
        # 各自内部 runtime 互异。证明只读 ledger envelope identity。
        from tests.test_helpers_entry import (
            CLAUDE_ENTRY as X,
            CODEX_ENTRY as Y,
            PI_ENTRY as Z,
            GEMINI_ENTRY as W,
            role_runtime_ids,
        )
        facade = self._facade((X, Y, Z, W))
        policy = cli.policy_from_args(_args(min_runtimes=None,
                                            no_runtime_reuse=True))
        self.assertFalse(policy.allow_runtime_reuse)
        result = facade.run(task_id="T1", task=TASK_COMPLEX, prompt="p",
                            mode=historical_mode_on(), policy=policy)
        self.assertEqual(result.status, "SUCCESS", result.failure_category)
        per_role = role_runtime_ids(facade.state.history("T1"))
        self.assertEqual(len(per_role), 4)  # architect/coder/tester/reviewer
        # 单射证明（成员关系，非内部属性）：dual 相内 architect≠coder，
        # verification 相内 tester≠reviewer。
        self.assertNotEqual(per_role["architect"], per_role["coder"])
        self.assertNotEqual(per_role["tester"], per_role["reviewer"])


# ---------------------------------------------------------------------------
# 十九：A2 不处理 A3 —— ledger 记录数与 DECISION 形态保持
# ---------------------------------------------------------------------------


class NoA3Tests(unittest.TestCase):
    def _facade(self, identities):
        from tests.test_helpers_entry import compose_entry_facade
        return compose_entry_facade(identities=identities)

    def test_ledger_stays_five_records_with_policy(self):
        from tests.test_helpers_entry import (
            CLAUDE_ENTRY as X,
            CODEX_ENTRY as Y,
        )
        facade = self._facade((X, Y))
        policy = CollaborationPolicy(
            runtime_allowlist=("rt-x", "rt-y"), min_distinct_runtimes=2)
        facade.run(task_id="T1", task=TASK_COMPLEX, prompt="p",
                   mode=historical_mode_on(), policy=policy)
        history = facade.state.history("T1")
        self.assertEqual(len(history), 5)  # 1 DECISION + 4 envelope
        from collaboration_state import CollaborationDirection
        self.assertEqual(history[0].direction, CollaborationDirection.DECISION)

    def test_render_summary_has_no_policy_fields(self):
        # A3 禁令：render_summary 不新增 policy 字段。
        rendered = json.loads(cli.render_summary(stub_result()))
        for key in ("policy", "runtimes", "min_runtimes", "max_runtimes",
                    "no_runtime_reuse", "allowlist"):
            self.assertNotIn(key, rendered)


# ---------------------------------------------------------------------------
# 二十一：Source scan —— A2 文件未引入禁用通道
# ---------------------------------------------------------------------------


class SourceScanTests(unittest.TestCase):
    MODULES = ("cli", "host", "production_facade", "collaboration_orchestrator")

    def test_no_parallel_channels_or_env_access(self):
        import importlib
        for name in self.MODULES:
            module = importlib.import_module(name)
            source = Path(module.__file__).read_text(encoding="utf-8")
            for forbidden in ("import asyncio", "import threading",
                              "import multiprocessing", "import concurrent",
                              "import subprocess", "import requests",
                              "import urllib", "import socket",
                              "import websocket", "a2a", "os.environ",
                              "getenv", "RUN_REAL_PROVIDER_TESTS"):
                self.assertNotIn(forbidden, source, name)

    def test_no_runtime_specific_branching(self):
        import importlib
        for name in self.MODULES:
            module = importlib.import_module(name)
            source = Path(module.__file__).read_text(encoding="utf-8")
            for forbidden in ("if runtime ==", "if runtime_id =="):
                self.assertNotIn(forbidden, source, name)

    def test_cli_has_no_runtime_names(self):
        module = importlib_import_cli()
        source = Path(module.__file__).read_text(encoding="utf-8")
        lowered = source.lower()
        for name in ("claude", "codex", "deepseek", "openai", "anthropic",
                     "gemini"):
            self.assertNotIn(name, lowered)


def importlib_import_cli():
    import cli
    return cli


if __name__ == "__main__":
    unittest.main()
