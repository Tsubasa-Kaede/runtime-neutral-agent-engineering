"""CU-BENCH BENCH-1 — offline token/cost benchmark harness（env-gated）。

门律：仅当 COCKPIT_BENCHMARK=1 时执行 benchmark 测试体（沿
COCKPIT_PERF_PROBE 先例）；缺省全部安全跳过（零执行）。本文件
绝不设置也不读取 REAL 门——benchmark 门与 REAL 门是两个独立
开关，本 harness 结构性离线：零 provider、零网络、零凭据。

真值域律（设计 v2 §2 域矩阵）：
- 一切 char 指标 = OBSERVED（A Compiler 域：经生产 W3-P disclosure
  缝离线确定性收集，可重放）；
- token offline = UNKNOWN（字符串状态，绝非数字——缺的是计量源
  非缝，加缝即伪造）；
- monetary = UNSUPPORTED（结构性无价格源，恒弃权）；
- B≡C = CHAR OUTPUT EQUIVALENCE 断言（F1：phase-1 编译产物与
  pre-W1 legacy 逐字节相同——等价断言本身是编译器回归闸；表述
  恒为"compiler path is currently character-equivalent to legacy
  path"，绝不为"编译器节省"）。

四实验对象（设计 §5a）：
- A Single-Agent baseline（1-step 组合）；
- B Multi-Agent legacy 臂（repo 冻结 oracle：test_context_wire.
  _legacy_prompt 直构——模板+TASK+PREVIOUS+4000 截断）；
- C Multi-Agent compiler 臂（生产 builder 接缝真编译）；
- D ORCH-5 routing（routed_default_composition ON vs resolve OFF，
  cold/warm 分桶 + 未来 run 防漏负向）。

样本律：一切臂 repeats=1（N<5 只报逐值，零分布统计）；manifest
六元组（benchmark_fixtures.BenchmarkManifest）只作身份声明非指标。
"""
import ast
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
TESTS = Path(__file__).resolve().parent
for _path in (str(SCRIPTS), str(TESTS)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import benchmark_fixtures  # noqa: E402
import cockpit_entry  # noqa: E402
from test_context_wire import _legacy_prompt  # noqa: E402

_BENCHMARK_ON = os.environ.get("COCKPIT_BENCHMARK") == "1"
_SELF_PATH = Path(__file__).resolve()
_SELF_SOURCE = _SELF_PATH.read_text(encoding="utf-8")

_RUN_TASK_ID = "bench-run-task"
_EMBED_LIMIT = cockpit_entry._EMBED_LIMIT

# 确定性步产出（固定文本，非执行产物；两臂共用同一序列）
_STEP_OUTPUTS = {
    "architect": "offline architect handoff body for bench arms.",
    "coder": "offline coder handoff body for bench arms.",
    "reviewer": "offline reviewer handoff body for bench arms.",
}

_TASK_KIND = "TASK"
_PRIOR_KIND = "PRIOR_STEP_OUTPUT"

_GATED_CLASSES = ("FourArmBenchmarkTests",
                  "ColdWarmRoutingBenchmarkTests",
                  "ReplayDeterminismBenchmarkTests",
                  "CoverageReportBenchmarkTests")


def _prior_double(role, step_index):
    """上一步产出 double（production contract 形：output+trace.id）。"""
    return SimpleNamespace(
        output=_STEP_OUTPUTS[role],
        trace=SimpleNamespace(invocation_id=f"bench-prior-{step_index}",
                              started_at=None, finished_at=None))


def _run_compiler_arm(task, steps):
    """C/A 臂：生产 builder 接缝（W3-P disclosure 缝）逐步离线构造。

    零执行、零 provider、零网络；prompt 与披露均为确定性纯函数
    产物（同输入恒同输出——重放律的前提）。
    """
    prompts, disclosures = [], []
    prior_result, previous_role = None, None
    for step_index, (role, runtime_id) in enumerate(steps):
        builder = cockpit_entry._make_request_builder(
            task, _RUN_TASK_ID, role, f"prov-{runtime_id}", 30.0,
            runtime_id=runtime_id, previous_role=previous_role,
            step_index=step_index, disclosure_sink=disclosures.append)
        prompts.append(builder(prior_result).prompt)
        prior_result, previous_role = _prior_double(role, step_index), role
    return prompts, disclosures


def _run_legacy_arm(task, steps):
    """B 臂：repo 冻结 legacy oracle 直构（counterfactual 基线）。"""
    prompts, prior_body = [], None
    for role, _runtime_id in steps:
        prompts.append(_legacy_prompt(role, task, prior_body))
        prior_body = _STEP_OUTPUTS[role]
    return prompts


def _chars_by_kind(disclosure, kind):
    for name, chars in disclosure.embedded_chars_by_kind:
        if name == kind:
            return chars
    return 0


def _arm_manifest(task, steps, policy_label, pool):
    return benchmark_fixtures.BenchmarkManifest(
        run_id=f"bench-{policy_label}",
        task_fingerprint=benchmark_fixtures.task_fingerprint(task),
        composition_fingerprint=benchmark_fixtures.composition_fingerprint(
            steps, policy_label),
        runtime_fingerprint=benchmark_fixtures.runtime_fingerprint(pool),
        cutoff_index=0, warm=False, repeats=1)


def _compiler_arm_report(arm, task, steps, prompts, disclosures, pool):
    per_agent = {}
    for role, _runtime_id in steps:
        per_agent[role] = per_agent.get(role, 0) + 1
    return {
        "arm": arm,
        "manifest": _arm_manifest(
            task, steps, disclosures[0].policy_fingerprint, pool),
        "total_invocations": len(steps),
        "per_agent_invocations": tuple(sorted(per_agent.items())),
        "prompt_chars_per_step": tuple(len(p) for p in prompts),
        "total_prompt_chars": sum(len(p) for p in prompts),
        "task_chars_per_step": tuple(
            _chars_by_kind(d, _TASK_KIND) for d in disclosures),
        "prior_output_chars_per_step": tuple(
            _chars_by_kind(d, _PRIOR_KIND) for d in disclosures),
        "total_embedded_chars": sum(
            d.total_embedded_chars for d in disclosures),
        "truncation_count": sum(len(d.truncations) for d in disclosures),
        "selection_count": sum(
            len(d.selection_notes) for d in disclosures),
        "token_usage": "UNKNOWN",
        "monetary_cost": "UNSUPPORTED",
    }


def _legacy_arm_report(task, steps, prompts, pool):
    per_agent = {}
    for role, _runtime_id in steps:
        per_agent[role] = per_agent.get(role, 0) + 1
    bodies = [None] + [_STEP_OUTPUTS[steps[i][0]]
                       for i in range(len(steps) - 1)]
    return {
        "arm": "B-legacy",
        "manifest": _arm_manifest(task, steps, "legacy-oracle-formula",
                                  pool),
        "total_invocations": len(steps),
        "per_agent_invocations": tuple(sorted(per_agent.items())),
        "prompt_chars_per_step": tuple(len(p) for p in prompts),
        "total_prompt_chars": sum(len(p) for p in prompts),
        "task_chars_per_step": (len(task),) * len(steps),
        "prior_output_chars_per_step": tuple(
            0 if body is None else min(len(body), _EMBED_LIMIT)
            for body in bodies),
        "token_usage": "UNKNOWN",
        "monetary_cost": "UNSUPPORTED",
    }


def _routing_report(label, pool, evidence_records, cutoff_index, warm):
    routed = cockpit_entry.routed_default_composition(
        pool, evidence_records)
    resolve = cockpit_entry.resolve_default_composition(pool)
    off = {b.role: b.runtime_id for b in resolve.bindings}
    on = {b.role: b.runtime_id for b in routed.bindings}
    return {
        "label": label,
        "manifest": benchmark_fixtures.BenchmarkManifest(
            run_id=f"bench-route-{label}",
            task_fingerprint=benchmark_fixtures.task_fingerprint(
                benchmark_fixtures.MULTI_TASK),
            composition_fingerprint=benchmark_fixtures.composition_fingerprint(
                benchmark_fixtures.STEPS_MULTI, "route-invocation-count"),
            runtime_fingerprint=benchmark_fixtures.runtime_fingerprint(
                pool),
            cutoff_index=cutoff_index, warm=warm, repeats=1),
        "selections": tuple(
            (b.role, b.runtime_id) for b in routed.bindings),
        "routing_selection_delta": tuple(
            (role, off[role], on[role])
            for role in resolve.roles if off[role] != on[role]),
        "invocation_delta": len(routed.bindings) - len(resolve.bindings),
        "coverage": _token_coverage_triple(evidence_records),
        "token_usage": "UNKNOWN",
        "monetary_cost": "UNSUPPORTED",
    }


def _token_coverage_triple(records):
    """token 计量覆盖三数（DERIVED 计数比，绝非 token 值）。"""
    known = total = 0
    for record in records:
        total += 1
        if (isinstance(getattr(record, "input_tokens", None), int)
                and isinstance(getattr(record, "output_tokens", None), int)):
            known += 1
    return {"known_records": known, "total_records": total,
            "coverage": f"{known}/{total}"}


def _all_offline_reports():
    """收集全部离线 benchmark 报告（供 key 律审计单一真源）。"""
    pool = benchmark_fixtures.pool_fixture()
    reports = []
    prompts_a, disc_a = _run_compiler_arm(
        benchmark_fixtures.SINGLE_TASK, benchmark_fixtures.STEPS_SINGLE)
    reports.append(_compiler_arm_report(
        "A-single", benchmark_fixtures.SINGLE_TASK,
        benchmark_fixtures.STEPS_SINGLE, prompts_a, disc_a, pool))
    prompts_b = _run_legacy_arm(
        benchmark_fixtures.MULTI_TASK, benchmark_fixtures.STEPS_MULTI)
    reports.append(_legacy_arm_report(
        benchmark_fixtures.MULTI_TASK, benchmark_fixtures.STEPS_MULTI,
        prompts_b, pool))
    prompts_c, disc_c = _run_compiler_arm(
        benchmark_fixtures.MULTI_TASK, benchmark_fixtures.STEPS_MULTI)
    reports.append(_compiler_arm_report(
        "C-compiler", benchmark_fixtures.MULTI_TASK,
        benchmark_fixtures.STEPS_MULTI, prompts_c, disc_c, pool))
    reports.append(_routing_report(
        "cold", pool, (), 0, False))
    reports.append(_routing_report(
        "warm", pool, benchmark_fixtures.routing_usage_records(),
        len(benchmark_fixtures.routing_usage_records()), True))
    return reports


# ------------------------------------------------ 门律与源律（always-on）


class GateLawTests(unittest.TestCase):
    """门常量在、缺省跳过、绝不触碰 REAL 门、零环境写入。"""

    def test_gate_literal_and_default_off_semantics(self):
        # 门谓词以字面量单点定义：缺席环境变量即 False（缺省跳过）
        self.assertIn('os.environ.get("COCKPIT_BENCHMARK") == "1"',
                      _SELF_SOURCE)

    def test_gated_classes_all_carry_gate_decorator(self):
        lines = _SELF_SOURCE.splitlines()
        found = set()
        for index, line in enumerate(lines):
            if not line.startswith("class "):
                continue
            name = line.split("(", 1)[0][len("class "):].strip()
            if name in _GATED_CLASSES:
                window = lines[max(0, index - 3):index]
                self.assertTrue(
                    any("@unittest.skipUnless(_BENCHMARK_ON" in text
                        for text in window),
                    f"{name} missing gate decorator")
                found.add(name)
        self.assertEqual(found, set(_GATED_CLASSES))

    def test_real_gate_string_entirely_absent(self):
        # 动态拼接被禁字面量，避免扫描器撞见自身源码（自指悖论）
        real_gate = "RUN_REAL_PROVIDER" + "_TESTS"
        self.assertNotIn(real_gate, _SELF_SOURCE)

    def test_no_environment_write_access(self):
        self.assertNotIn("environ" + "[", _SELF_SOURCE)
        self.assertNotIn("set" + "default", _SELF_SOURCE)
        self.assertNotIn("put" + "env", _SELF_SOURCE)


class SourceLawTests(unittest.TestCase):
    """源级计量律：零 token 换算、零节省表述、零持久化通道。"""

    def test_no_token_estimation_or_conversion_strings(self):
        banned = ("chars_to_" + "tok" + "ens",
                  "tokens_from_" + "cha" + "rs",
                  "estimate_" + "tok" + "ens",
                  "estimated_" + "co" + "st",
                  "token_" + "savin" + "gs",
                  "monetary_" + "savin" + "gs",
                  "predicted_" + "savin" + "gs",
                  "tokens_per_" + "cha" + "r")
        for token in banned:
            self.assertNotIn(token, _SELF_SOURCE, token[:4] + "...")

    def test_no_reduction_claim_or_distribution_words(self):
        banned = ("savin" + "g", "per" + "cent", "me" + "an",
                  "av" + "g", "st" + "d", "med" + "ian",
                  "p5" + "0", "p9" + "5")
        for token in banned:
            self.assertNotIn(token, _SELF_SOURCE, token[:3] + "...")

    def test_no_persistence_or_network_channels(self):
        tree = ast.parse(_SELF_SOURCE)
        identifiers = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                identifiers.add(node.id)
            elif isinstance(node, ast.Attribute):
                identifiers.add(node.attr)
            elif isinstance(node, ast.arg):
                identifiers.add(node.arg)
        for banned in ("sqlite", "subprocess", "socket", "requests",
                       "urlopen", "write_text", "write_bytes", "login"):
            self.assertNotIn(banned, identifiers, banned)


# ------------------------------------------------ 四臂（gated）


@unittest.skipUnless(_BENCHMARK_ON, "benchmark gate off")
class FourArmBenchmarkTests(unittest.TestCase):
    """A/B/C/D 四臂离线执行 + F1 字节等价闸。"""

    def test_arm_a_single_agent_report(self):
        task = benchmark_fixtures.SINGLE_TASK
        steps = benchmark_fixtures.STEPS_SINGLE
        prompts, disclosures = _run_compiler_arm(task, steps)
        report = _compiler_arm_report(
            "A-single", task, steps, prompts, disclosures,
            benchmark_fixtures.pool_fixture())
        self.assertEqual(report["total_invocations"], 1)
        self.assertEqual(report["per_agent_invocations"], (("coder", 1),))
        self.assertEqual(report["prompt_chars_per_step"],
                         (len(prompts[0]),))
        self.assertEqual(report["prior_output_chars_per_step"], (0,))
        self.assertEqual(report["selection_count"], 0)
        self.assertEqual(report["truncation_count"], 0)
        self.assertIs(report["token_usage"], "UNKNOWN")
        self.assertIs(report["monetary_cost"], "UNSUPPORTED")
        # 单步零 handoff：无 PREVIOUS 段
        self.assertNotIn(cockpit_entry._PROMPT_PREVIOUS_SECTION,
                         prompts[0])

    def test_arm_b_legacy_prompts_follow_oracle_formula(self):
        task = benchmark_fixtures.MULTI_TASK
        steps = benchmark_fixtures.STEPS_MULTI
        prompts = _run_legacy_arm(task, steps)
        bodies = [None] + [_STEP_OUTPUTS[steps[i][0]]
                           for i in range(len(steps) - 1)]
        for prompt, (role, _rt), body in zip(prompts, steps, bodies):
            self.assertEqual(
                prompt, _legacy_prompt(role, task, body))

    def test_arm_c_compiler_report_metrics(self):
        task = benchmark_fixtures.MULTI_TASK
        steps = benchmark_fixtures.STEPS_MULTI
        prompts, disclosures = _run_compiler_arm(task, steps)
        report = _compiler_arm_report(
            "C-compiler", task, steps, prompts, disclosures,
            benchmark_fixtures.pool_fixture())
        self.assertEqual(report["total_invocations"], 3)
        self.assertEqual(report["per_agent_invocations"],
                         (("architect", 1), ("coder", 1),
                          ("reviewer", 1)))
        self.assertEqual(report["task_chars_per_step"],
                         (len(task),) * 3)
        self.assertEqual(report["prior_output_chars_per_step"][0], 0)
        # 步 1 嵌 architect 产出、步 2 嵌 coder 产出（邻接传递）
        self.assertEqual(report["prior_output_chars_per_step"],
                         (0, len(_STEP_OUTPUTS["architect"]),
                          len(_STEP_OUTPUTS["coder"])))
        self.assertEqual(report["selection_count"], 0)  # phase-1 dormant
        self.assertEqual(report["truncation_count"], 0)
        # 计量状态词照实转录（现阶段恒 UNKNOWN）
        self.assertTrue(all(d.token_status == "UNKNOWN"
                            for d in disclosures))
        # 嵌入总字符 = TASK+PRIOR 分步之和（A 域 OBSERVED 自洽）
        self.assertEqual(
            report["total_embedded_chars"],
            sum(report["task_chars_per_step"])
            + sum(report["prior_output_chars_per_step"]))

    def test_f1_byte_identical_full_chain(self):
        # F1 闸门：compiler 臂产物与 legacy oracle 臂逐字节相同
        # （CHAR OUTPUT EQUIVALENCE——等价即回归闸，非节省主张）
        task = benchmark_fixtures.MULTI_TASK
        steps = benchmark_fixtures.STEPS_MULTI
        compiler_prompts, _disc = _run_compiler_arm(task, steps)
        legacy_prompts = _run_legacy_arm(task, steps)
        self.assertEqual(tuple(compiler_prompts), tuple(legacy_prompts))

    def test_f1_oracle_cases_byte_identical(self):
        # F1 全覆盖：prior None / short / 超限截断 / 角色输入
        task = "f1 oracle task"
        cases = (None, "short prior body",
                 benchmark_fixtures.PRIOR_OVER_LIMIT)
        for prior in cases:
            prior_result = None if prior is None else SimpleNamespace(
                output=prior, trace=SimpleNamespace(
                    invocation_id="f1-inv", started_at=None,
                    finished_at=None))
            for role in ("architect", "coder", "reviewer"):
                builder = cockpit_entry._make_request_builder(
                    task, _RUN_TASK_ID, role, "prov-x", 30.0,
                    runtime_id="rt-x", previous_role="architect",
                    step_index=1)
                prompt = builder(prior_result).prompt
                self.assertEqual(
                    prompt, _legacy_prompt(role, task, prior),
                    f"role={role} prior={type(prior)}")
        # 角色输入覆盖：同 task/prior 下角色不同则 prompt 不同
        b1 = cockpit_entry._make_request_builder(
            task, _RUN_TASK_ID, "architect", "p", 30.0,
            runtime_id="rt", previous_role=None, step_index=0)
        b2 = cockpit_entry._make_request_builder(
            task, _RUN_TASK_ID, "coder", "p", 30.0,
            runtime_id="rt", previous_role=None, step_index=0)
        self.assertNotEqual(b1(None).prompt, b2(None).prompt)

    def test_truncation_over_limit_disclosed(self):
        # 4000 截断单源（_EMBED_LIMIT）：超限 prior 在披露与 prompt
        # 双面一致，且 B≡C 等价在截断路径保持
        task = "truncation arm task"
        steps = (("architect", "rt-a"), ("coder", "rt-b"))
        outputs = {"architect": benchmark_fixtures.PRIOR_OVER_LIMIT,
                   "coder": "tail"}
        saved = dict(_STEP_OUTPUTS)

        def patched(role, step_index):
            return SimpleNamespace(
                output=outputs[role],
                trace=SimpleNamespace(
                    invocation_id=f"tr-{step_index}", started_at=None,
                    finished_at=None))

        prior = _prior_double
        try:
            globals()["_prior_double"] = patched
            prompts, disclosures = _run_compiler_arm(task, steps)
            legacy = [
                _legacy_prompt("architect", task, None),
                _legacy_prompt("coder", task, outputs["architect"]),
            ]
            self.assertEqual(tuple(prompts), tuple(legacy))
            self.assertEqual(len(disclosures[1].truncations), 1)
            kind, original, embedded = disclosures[1].truncations[0]
            self.assertEqual(kind, _PRIOR_KIND)
            self.assertEqual(original, 4500)
            self.assertEqual(embedded, _EMBED_LIMIT)
        finally:
            globals()["_prior_double"] = prior
            _STEP_OUTPUTS.clear()
            _STEP_OUTPUTS.update(saved)

    def test_invocation_delta_and_repeated_task_chars(self):
        # 调用数差分（OBSERVED 计数）与任务重复注入 char 面
        # （注入开销本身 = 真问题；编译器的削减当前为零 = F1）
        pool = benchmark_fixtures.pool_fixture()
        prompts_a, disc_a = _run_compiler_arm(
            benchmark_fixtures.SINGLE_TASK, benchmark_fixtures.STEPS_SINGLE)
        report_a = _compiler_arm_report(
            "A-single", benchmark_fixtures.SINGLE_TASK,
            benchmark_fixtures.STEPS_SINGLE, prompts_a, disc_a, pool)
        prompts_c, disc_c = _run_compiler_arm(
            benchmark_fixtures.MULTI_TASK, benchmark_fixtures.STEPS_MULTI)
        report_c = _compiler_arm_report(
            "C-compiler", benchmark_fixtures.MULTI_TASK,
            benchmark_fixtures.STEPS_MULTI, prompts_c, disc_c, pool)
        delta = (report_c["total_invocations"]
                 - report_a["total_invocations"])
        self.assertEqual(delta, 2)
        # 3 步 ⇒ task 文本注入 3 次（char 面 OBSERVED）
        self.assertEqual(sum(report_c["task_chars_per_step"]),
                         3 * len(benchmark_fixtures.MULTI_TASK))
        # 调用数差分 ≠ token 差分：报告无任何 token 数值通道
        for report in (report_a, report_c):
            self.assertEqual(report["token_usage"], "UNKNOWN")


# ------------------------------------------------ cold/warm 路由（gated）


@unittest.skipUnless(_BENCHMARK_ON, "benchmark gate off")
class ColdWarmRoutingBenchmarkTests(unittest.TestCase):
    """D 臂：ORCH-5 ON/OFF 决策差分 + cold/warm 分桶 + 防漏负向。"""

    def test_cold_on_equals_off_single_value_report(self):
        pool = benchmark_fixtures.pool_fixture()
        report = _routing_report("cold", pool, (), 0, False)
        # cold：路由证据空 ⇒ ON≡OFF canonical（既有集成真值）
        self.assertEqual(report["routing_selection_delta"], ())
        self.assertEqual(report["invocation_delta"], 0)
        manifest = report["manifest"]
        self.assertIs(manifest.warm, False)
        self.assertEqual(manifest.cutoff_index, 0)
        self.assertEqual(manifest.repeats, 1)
        # N=1 单独报告：逐席位值在场、零分布统计
        self.assertEqual(len(report["selections"]), 3)

    def test_warm_selection_delta_without_token_claims(self):
        pool = benchmark_fixtures.pool_fixture()
        records = benchmark_fixtures.routing_usage_records()
        report = _routing_report("warm", pool, records, len(records),
                                 True)
        # 探针实证的确定性 warm 真值（调用数维度升序 + 无证据殿后）
        self.assertEqual(report["selections"],
                         (("architect", "rt-b"), ("coder", "rt-c"),
                          ("reviewer", "rt-a")))
        # OFF canonical = sorted zip：三席全变
        self.assertEqual(report["routing_selection_delta"],
                         (("architect", "rt-a", "rt-b"),
                          ("coder", "rt-b", "rt-c"),
                          ("reviewer", "rt-c", "rt-a")))
        # 角色模板位次不动 ⇒ 调用计划零差分
        self.assertEqual(report["invocation_delta"], 0)
        # 证据 token 覆盖 0/4：无 KNOWN token 证据 ⇒ 只说
        # "selection changed"——报告结构性无节省/期望值通道
        self.assertEqual(report["coverage"]["known_records"], 0)
        self.assertEqual(report["coverage"]["total_records"], 4)
        self.assertEqual(report["coverage"]["coverage"], "0/4")
        self.assertIs(report["manifest"].warm, True)
        self.assertEqual(report["manifest"].cutoff_index, 4)

    def test_future_run_leakage_negative(self):
        # warm 证据 = composed_runs 前缀（cutoff 冻结快照）：
        # 注入未来 run usage 后，run-N 路由决策必须不变
        pool = benchmark_fixtures.pool_fixture()
        live = list(benchmark_fixtures.routing_usage_records())
        cutoff = len(live)
        snapshot = tuple(live)
        decision = _routing_report("warm-frozen", pool, snapshot,
                                   cutoff, True)
        # 模拟未来 run 追加证据（append-after-terminal 之后）
        live.append(SimpleNamespace(runtime_id="rt-a",
                                    usage_status="KNOWN",
                                    input_tokens=None,
                                    output_tokens=None,
                                    duration_ms=None))
        # harness 重放 run-N 决策：只读冻结快照
        replay = _routing_report("warm-frozen", pool, snapshot,
                                 cutoff, True)
        self.assertEqual(decision, replay)
        # 冻结快照不含未来记录（不读未来 run）
        self.assertEqual(len(snapshot), cutoff)
        self.assertNotIn(
            "rt-a", {record.runtime_id for record in snapshot})


# ------------------------------------------------ 重放确定性与 key 律（gated）


@unittest.skipUnless(_BENCHMARK_ON, "benchmark gate off")
class ReplayDeterminismBenchmarkTests(unittest.TestCase):
    """同 fixture 三跑同输出；报告 key 律运行时审计。"""

    def test_triple_compiler_run_identical(self):
        task = benchmark_fixtures.MULTI_TASK
        steps = benchmark_fixtures.STEPS_MULTI
        runs = [_run_compiler_arm(task, steps) for _ in range(3)]
        prompts = [tuple(run[0]) for run in runs]
        disclosures = [tuple(run[1]) for run in runs]
        self.assertEqual(prompts[0], prompts[1])
        self.assertEqual(prompts[1], prompts[2])
        self.assertEqual(disclosures[0], disclosures[1])
        self.assertEqual(disclosures[1], disclosures[2])

    def test_report_key_law_across_all_offline_reports(self):
        for report in _all_offline_reports():
            for key, value in report.items():
                if ("token" in key or "monetary" in key
                        or key.endswith("_cost")):
                    self.assertIsInstance(value, str, key)
                    self.assertIn(value, ("UNKNOWN", "UNSUPPORTED"), key)
                if key in ("prompt_chars_per_step",
                           "prior_output_chars_per_step",
                           "task_chars_per_step",
                           "total_prompt_chars",
                           "total_embedded_chars"):
                    self.assertIn("chars", key)
                lowered = key.lower()
                # 动态拼接避免源级扫描撞见自身（自指悖论）
                for banned in ("savin" + "g", "per" + "cent",
                               "me" + "an", "av" + "g", "st" + "d",
                               "med" + "ian", "expec" + "ted"):
                    self.assertNotIn(banned, lowered, key)

    def test_per_value_reporting_below_sample_floor(self):
        # N<5 样本律：一切臂 repeats=1 ⇒ 指标逐值 tuple 在场、
        # 长度恰等于步数；报告面零分布统计字段
        reports = _all_offline_reports()
        arm_reports = [r for r in reports
                       if r.get("arm") in ("A-single", "C-compiler")]
        self.assertEqual(len(arm_reports), 2)
        for report in arm_reports:
            steps_len = report["total_invocations"]
            self.assertEqual(len(report["prompt_chars_per_step"]),
                             steps_len)
            self.assertEqual(len(report["task_chars_per_step"]),
                             steps_len)
            self.assertEqual(report["manifest"].repeats, 1)
            for key in report:
                self.assertNotIn("distribution", key)


# ------------------------------------------------ 覆盖三数（gated）


@unittest.skipUnless(_BENCHMARK_ON, "benchmark gate off")
class CoverageReportBenchmarkTests(unittest.TestCase):
    """异构 runtime token 覆盖三数（DERIVED 计数比非 token 值）。"""

    def test_offline_coverage_triple_is_honest(self):
        records = benchmark_fixtures.routing_usage_records()
        triple = _token_coverage_triple(records)
        self.assertEqual(triple, {"known_records": 0,
                                  "total_records": 4,
                                  "coverage": "0/4"})
        # known_records=0 是比率分子（诚实计数），绝非 unknown→0
        # 折算：计数键名零 token 词根、值恒非负计数或串
        for key, value in triple.items():
            self.assertNotIn("token", key)
            if isinstance(value, bool):
                self.fail("boolean count")
            elif isinstance(value, int):
                self.assertGreaterEqual(value, 0)
            else:
                self.assertIsInstance(value, str)

    def test_coverage_counts_records_not_runtimes(self):
        # 分桶语义（Q 钉 7）：per-runtime 分桶保持；coverage 按
        # 记录数计（rt-c×3 + rt-b×1 = 4 条）
        records = benchmark_fixtures.routing_usage_records()
        by_runtime = {}
        for record in records:
            by_runtime[record.runtime_id] = (
                by_runtime.get(record.runtime_id, 0) + 1)
        self.assertEqual(by_runtime, {"rt-c": 3, "rt-b": 1})
        self.assertEqual(sum(by_runtime.values()),
                         _token_coverage_triple(records)["total_records"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
