"""CU-BENCH BENCH-Q — benchmark 测量宪法资格钉定测试。

对 TOKEN/COST BENCHMARK DESIGN（v2，基线 f22e9c9）的结构与事实
边界钉定（qualification）：在零生产改动、零 REAL、零 benchmark
执行前提下，以当前真实代码结构证明 13 项测量边界事实——未来任何
benchmark 阶段（BENCH-1 离线 harness / BENCH-REAL）都不得改变
这些事实。

13 钉（设计 §二）：
 1 token offline = UNKNOWN（结构性，非未测量）
 2 monetary = UNSUPPORTED（结构性无价格源）
 3 chars 与 tokens 命名域隔离
 4 runtime usage 真值唯一来源 = UsageCapture / UsageLog
 5 missing usage（诚实缺席）与 UNKNOWN（记录在案无值）分离
 6 retry/fallback 边界保持当前语义（cockpit 栈结构性恒零）
 7 异构 runtime 不允许伪造跨 runtime token 聚合
 8 invocation delta 不等于 token delta（结构分离）
 9 char delta 不等于 token delta（UNKNOWN 带数即拒）
10 benchmark fixture 具备 task/composition/runtime/cutoff/
   cold-warm 明确语义（benchmark_fixtures.BenchmarkManifest）
11 benchmark 默认不触发 REAL（fixture/pins 零 REAL 通道）
12 env gate 缺省不执行 REAL（门控先例 + BENCH-1 门钉定）
13 历史 REAL 数据不能无条件当作当前 benchmark 样本（零通道）

方法：AST / source inspection / 纯 fixture / 确定性断言。
本文件不依赖 COCKPIT_BENCHMARK（资格测试与执行门无关）。
全部离线；REAL=0。
"""
import ast
import dataclasses
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

TESTS_DIR = Path(__file__).resolve().parent
SCRIPTS = TESTS_DIR.parent / "dual-agent-development" / "scripts"
for path in (str(SCRIPTS), str(TESTS_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

import benchmark_fixtures  # noqa: E402
import cockpit_entry  # noqa: E402
from cockpit_compile import TokenMeasure, TokenStatus  # noqa: E402
from cockpit_projection import CompileDisclosure  # noqa: E402
from cockpit_route import CostFactView, CostStatus  # noqa: E402
from external_runtime import InvocationTrace  # noqa: E402
from usage_capture import UsageCapture  # noqa: E402
from usage_log import UsageLog, UsageObservation, UsageRecord  # noqa: E402

_FIXTURES_SOURCE = (TESTS_DIR / "benchmark_fixtures.py").read_text(
    encoding="utf-8")
_BENCH1_PATH = TESTS_DIR / "test_token_cost_benchmark.py"
_COMPILED_PRODUCT_SOURCES = None  # lazy：见 _production_sources


def _production_sources():
    for path in sorted(SCRIPTS.glob("*.py")):
        yield path.name, path.read_text(encoding="utf-8")


def _files_containing(token):
    return {name for name, text in _production_sources() if token in text}


def _usage_result(*, status="SUCCESS", input_tokens="unknown",
                  output_tokens="unknown"):
    """offline invocation result double：诚实 "unknown" 计量缺省。"""
    return SimpleNamespace(
        status=status,
        trace=SimpleNamespace(input_tokens=input_tokens,
                              output_tokens=output_tokens))


def _usage_request():
    return SimpleNamespace(task_id="bench-task", agent_id="bench-agent")


class _RawDouble:
    """被包装方 double：可配置返回/抛出，记录调用次数。"""

    def __init__(self, result=None, raises=None):
        self._result = result if result is not None else _usage_result()
        self._raises = raises
        self.calls = 0

    def invoke(self, request):
        self.calls += 1
        if self._raises is not None:
            raise self._raises
        return self._result


# ------------------------------------------------ 1/2 三态与 UNSUPPORTED


class TokenMonetaryStateTests(unittest.TestCase):
    """钉 1/2：token offline 恒 UNKNOWN；monetary 恒 UNSUPPORTED。"""

    def test_invocation_trace_defaults_are_honest_unknown(self):
        trace = InvocationTrace(
            invocation_id="bench-inv", task_id="bench-task",
            agent_id="bench-agent", runtime="rt", provider=None,
            model=None, role="coder", status="SUCCESS")
        self.assertEqual(trace.input_tokens, "unknown")
        self.assertEqual(trace.output_tokens, "unknown")

    def test_offline_capture_records_unknown_never_zero(self):
        # offline 执行（无 provider）→ 记录在案、无值、绝不折 0
        log = UsageLog()
        raw = _RawDouble()
        UsageCapture(raw, log, runtime_id="rt-x", role="coder").invoke(
            _usage_request())
        self.assertEqual(len(log.snapshot()), 1)
        record = log.snapshot()[0]
        self.assertIs(record.usage_status, UsageObservation.UNKNOWN)
        self.assertIsNone(record.input_tokens)
        self.assertIsNone(record.output_tokens)

    def test_monetary_dimension_is_structurally_unsupported(self):
        view = CostFactView.from_usage_records(
            benchmark_fixtures.routing_usage_records())
        self.assertTrue(view.entries)
        for entry in view.entries:
            self.assertIs(entry.monetary.status, CostStatus.UNSUPPORTED)
            self.assertIsNone(entry.monetary.value)

    def test_missing_runtime_returns_unknown_not_zero(self):
        view = CostFactView.from_usage_records(
            benchmark_fixtures.routing_usage_records())
        # rt-a 零记录：缺席 = 无证据（UNKNOWN），绝不返回零
        known_ids = {entry.runtime_id for entry in view.entries}
        self.assertNotIn("rt-a", known_ids)


# ------------------------------------------------ 3/8/9 命名与维度隔离


class NamingDomainIsolationTests(unittest.TestCase):
    """钉 3/9：chars/tokens 命名域隔离；char≠token 结构分离。"""

    def test_compiler_char_fields_never_carry_token_word_root(self):
        # token_status 除外：它是计量状态词的转录字段（恒 "UNKNOWN"
        # 字符串、零计数语义）——命名域律禁的是 char 字段冒充 token
        # 计量，不是禁诚实状态词
        from cockpit_compile import BudgetReport
        for cls in (BudgetReport, CompileDisclosure):
            names = {field.name
                     for field in dataclasses.fields(cls)}
            for name in names - {"token_status"}:
                self.assertNotIn("token", name, f"{cls.__name__}.{name}")

    def test_no_chars_to_tokens_conversion_helper_exists(self):
        for token in ("chars_to_tokens", "tokens_from_chars",
                      "estimate_tokens", "estimated_cost",
                      "tokens_per_char", "char_to_token"):
            self.assertEqual(_files_containing(token), set(), msg=token)

    def test_unknown_token_measure_with_count_is_rejected(self):
        with self.assertRaises(Exception):
            TokenMeasure(status=TokenStatus.UNKNOWN, tokens=5)
        measure = TokenMeasure.unknown()
        self.assertIsNone(measure.tokens)

    def test_invocation_count_and_token_total_are_separate_measures(self):
        # 钉 8 前半：RuntimeCostFacts 四维各自独立计量——调用数与
        # token 是平行维度，结构上不存在一者推导另一者的通路
        view = CostFactView.from_usage_records(
            benchmark_fixtures.routing_usage_records())
        entry = view.entries[0]
        names = {field.name for field in dataclasses.fields(entry)}
        self.assertEqual(names, {"runtime_id", "invocation_count",
                                 "mean_latency_ms", "token_total",
                                 "monetary"})

    def test_invocation_delta_is_not_token_delta_in_summary(self):
        # 钉 8 后半：会话聚合面 invocation 与 token 分列——
        # usage_summary 无任何"调用数折算 token"通道
        summary_fields = cockpit_entry.UsageSummary._fields
        self.assertIn("unknown_count", summary_fields)
        source = Path(
            cockpit_entry.usage_summary.__code__.co_filename
        ).read_text(encoding="utf-8")
        start = source.index("def usage_summary")
        body = source[start:source.index("\ndef ", start + 1)]
        self.assertIn("known_input", body)
        self.assertNotIn("* len(", body)
        self.assertNotIn("count *", body)


# ------------------------------------------------ 4/5 真值源与缺席


class UsageTruthSourceTests(unittest.TestCase):
    """钉 4/5：usage 真值唯一来源；缺席 ≠ UNKNOWN。"""

    def test_usage_record_constructed_only_in_usage_capture(self):
        self.assertEqual(_files_containing("UsageRecord("),
                         {"usage_capture.py"})

    def test_summary_sums_known_only_and_counts_unknown(self):
        records = (
            UsageRecord(invocation_id="i1", task_id="t", agent_id="a",
                        role="coder", runtime_id="rt-a", status="SUCCESS",
                        usage_status=UsageObservation.KNOWN,
                        duration_ms=10, input_tokens=120,
                        output_tokens=80),
            UsageRecord(invocation_id="i2", task_id="t", agent_id="a",
                        role="coder", runtime_id="rt-b", status="SUCCESS",
                        usage_status=UsageObservation.UNKNOWN,
                        duration_ms=8),
        )
        summary = cockpit_entry.usage_summary(records)
        self.assertEqual(summary.known_input, 120)
        self.assertEqual(summary.known_output, 80)
        self.assertEqual(summary.unknown_count, 1)
        self.assertEqual(summary.unsupported_count, 0)

    def test_raising_adapter_leaves_no_record(self):
        # 缺席：被包装方抛出 → 无状态事实可记（诚实缺席 ≠ UNKNOWN）
        log = UsageLog()
        raw = _RawDouble(raises=RuntimeError("boom"))
        capture = UsageCapture(raw, log, runtime_id="rt-x", role="coder")
        with self.assertRaises(RuntimeError):
            capture.invoke(_usage_request())
        self.assertEqual(log.snapshot(), ())

    def test_capture_with_real_ints_records_known(self):
        log = UsageLog()
        raw = _RawDouble(result=_usage_result(
            input_tokens=7, output_tokens=11))
        UsageCapture(raw, log, runtime_id="rt-x", role="coder").invoke(
            _usage_request())
        record = log.snapshot()[0]
        self.assertIs(record.usage_status, UsageObservation.KNOWN)
        self.assertEqual(record.input_tokens, 7)
        self.assertEqual(record.output_tokens, 11)
        self.assertIsInstance(record.duration_ms, int)


# ------------------------------------------------ 6 retry/fallback 边界


class RetryFallbackBoundaryTests(unittest.TestCase):
    """钉 6：cockpit 执行栈零 retry/fallback（结构性恒零非未测量）。"""

    FROZEN_EXECUTION_FILES = ("sequential_pipeline.py",
                              "composition_core.py", "wrap_stack.py",
                              "execution_slots.py", "cockpit_session.py")

    def test_execution_stack_sources_free_of_retry_tokens(self):
        # 标识符级扫描（AST Name/Attribute/arg/keyword）：retry/
        # fallback 作为代码词零出现——docstring 里的「零 retry」
        # 声明文本不算代码面
        for filename in self.FROZEN_EXECUTION_FILES:
            tree = ast.parse(
                (SCRIPTS / filename).read_text(encoding="utf-8"))
            identifiers = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Name):
                    identifiers.add(node.id)
                elif isinstance(node, ast.Attribute):
                    identifiers.add(node.attr)
                elif isinstance(node, ast.arg):
                    identifiers.add(node.arg)
                elif isinstance(node, ast.keyword):
                    identifiers.add(node.arg or "")
            for banned in ("retry", "fallback", "backoff"):
                self.assertFalse(
                    any(banned in name.lower()
                        for name in identifiers),
                    f"{filename} has {banned} identifier")

    def test_single_invoke_per_invocation_boundary(self):
        # 行为面：一次 capture.invoke = 恰一次 raw.invoke（无重放）
        log = UsageLog()
        raw = _RawDouble()
        UsageCapture(raw, log, runtime_id="rt-x", role="coder").invoke(
            _usage_request())
        self.assertEqual(raw.calls, 1)


# ------------------------------------------------ 7 异构 runtime 边界


class HeterogeneousRuntimeTests(unittest.TestCase):
    """钉 7：异构 runtime 分桶；未知绝不折 0、绝不跨桶伪造聚合。"""

    @staticmethod
    def _mixed_records():
        return (
            UsageRecord(invocation_id="i1", task_id="t", agent_id="a",
                        role="coder", runtime_id="rt-a", status="SUCCESS",
                        usage_status=UsageObservation.KNOWN,
                        duration_ms=10, input_tokens=100,
                        output_tokens=50),
            UsageRecord(invocation_id="i2", task_id="t", agent_id="a",
                        role="coder", runtime_id="rt-b", status="SUCCESS",
                        usage_status=UsageObservation.UNKNOWN,
                        duration_ms=8),
        )

    def test_buckets_stay_per_runtime_and_unknown_survives(self):
        view = CostFactView.from_usage_records(self._mixed_records())
        by_runtime = {entry.runtime_id: entry for entry in view.entries}
        self.assertIs(by_runtime["rt-a"].token_total.status,
                      CostStatus.KNOWN)
        self.assertEqual(by_runtime["rt-a"].token_total.value, 150)
        self.assertIs(by_runtime["rt-b"].token_total.status,
                      CostStatus.UNKNOWN)
        self.assertIsNone(by_runtime["rt-b"].token_total.value)

    def test_session_total_follows_usage_summary_semantics(self):
        summary = cockpit_entry.usage_summary(self._mixed_records())
        # 只有 KNOWN 进和；UNKNOWN 计数在场——绝不 unknown→0
        self.assertEqual(summary.known_input, 100)
        self.assertEqual(summary.known_output, 50)
        self.assertEqual(summary.unknown_count, 1)


# ------------------------------------------------ 10/13 fixture 语义


class FixtureManifestSemanticsTests(unittest.TestCase):
    """钉 10/13：manifest 六元组语义 + 历史数据零通道。"""

    def test_manifest_field_set_is_exact(self):
        names = {field.name for field in dataclasses.fields(
            benchmark_fixtures.BenchmarkManifest)}
        self.assertEqual(names, {
            "run_id", "task_fingerprint", "composition_fingerprint",
            "runtime_fingerprint", "cutoff_index", "warm", "repeats"})

    def test_warm_requires_prefix_and_cold_requires_zero(self):
        with self.assertRaises(ValueError):
            benchmark_fixtures.BenchmarkManifest(
                run_id="r", task_fingerprint="task_x",
                composition_fingerprint="comp_x",
                runtime_fingerprint=("rt=p",), cutoff_index=0,
                warm=True, repeats=1)
        with self.assertRaises(ValueError):
            benchmark_fixtures.BenchmarkManifest(
                run_id="r", task_fingerprint="task_x",
                composition_fingerprint="comp_x",
                runtime_fingerprint=("rt=p",), cutoff_index=2,
                warm=False, repeats=1)
        with self.assertRaises(ValueError):
            benchmark_fixtures.BenchmarkManifest(
                run_id="r", task_fingerprint="task_x",
                composition_fingerprint="comp_x",
                runtime_fingerprint=("rt=p",), cutoff_index=0,
                warm=False, repeats=0)

    def test_fingerprints_are_content_derived_and_deterministic(self):
        self.assertEqual(benchmark_fixtures.task_fingerprint("t"),
                         benchmark_fixtures.task_fingerprint("t"))
        self.assertNotEqual(benchmark_fixtures.task_fingerprint("t"),
                            benchmark_fixtures.task_fingerprint("u"))
        self.assertTrue(benchmark_fixtures.task_fingerprint("t").startswith(
            "task_"))
        self.assertTrue(benchmark_fixtures.composition_fingerprint(
            benchmark_fixtures.STEPS_SINGLE, "policy_x").startswith(
            "comp_"))
        self.assertEqual(
            benchmark_fixtures.runtime_fingerprint(
                benchmark_fixtures.pool_fixture()),
            ("rt-a=prov-a=None", "rt-b=prov-b=None", "rt-c=prov-c=None"))

    def test_routing_fixture_carries_no_token_semantics(self):
        for record in benchmark_fixtures.routing_usage_records():
            self.assertIsNone(record.input_tokens)
            self.assertIsNone(record.output_tokens)

    def test_fixture_module_has_zero_production_imports(self):
        # 钉 13：fixture 面零生产 import——历史 REAL 数据（Evidence
        # Store/资格域/持久面）结构性无通道进入 benchmark 样本
        tree = ast.parse(_FIXTURES_SOURCE)
        roots = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    roots.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    roots.add(node.module.split(".")[0])
        self.assertEqual(roots, {"dataclasses", "hashlib", "types"})

    def test_fixture_module_free_of_persistence_channels(self):
        # 代码面扫描：持久化/网络通道词零出现（import 图钉定已证
        # 零生产依赖；此处补运行时通道词）
        tree = ast.parse(_FIXTURES_SOURCE)
        identifiers = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                identifiers.add(node.id)
            elif isinstance(node, ast.Attribute):
                identifiers.add(node.attr)
            elif isinstance(node, ast.arg):
                identifiers.add(node.arg)
        for banned in ("sqlite", "open", "subprocess", "socket",
                       "requests", "host_entry", "Path", "environ"):
            self.assertNotIn(banned, identifiers, banned)


# ------------------------------------------------ 11/12 REAL 门


class RealGateTests(unittest.TestCase):
    """钉 11/12：benchmark 默认零 REAL；env gate 缺省不执行。"""

    def test_pins_and_fixtures_never_touch_real_gate(self):
        # 动态拼接被禁字面量，避免扫描器撞见自身源码（自指悖论）
        real_gate = "RUN_REAL_PROVIDER" + "_TESTS"
        for path in (TESTS_DIR / "test_benchmark_measurement_pins.py",
                     TESTS_DIR / "benchmark_fixtures.py"):
            source = path.read_text(encoding="utf-8")
            self.assertNotIn(real_gate + " =", source)
            self.assertNotIn("environ" + "[", source)
            self.assertNotIn("lo" + "gin", source)

    def test_repo_gate_precedent_exists(self):
        # env-gated 缺省 skip 先例在库（CU-PERF-1 门）
        source = (TESTS_DIR / "test_cockpit_perf_probe.py").read_text(
            encoding="utf-8")
        self.assertIn('os.environ.get("COCKPIT_PERF_PROBE")', source)

    @unittest.skipUnless(_BENCH1_PATH.exists(),
                         "BENCH-1 harness not implemented yet")
    def test_bench1_gate_defaults_to_skip_and_never_sets_real(self):
        source = _BENCH1_PATH.read_text(encoding="utf-8")
        # 门常量在：缺省（环境变量缺席）= 不启用
        self.assertIn('os.environ.get("COCKPIT_BENCHMARK")', source)
        self.assertIn('"1"', source)
        # benchmark 门与 REAL 门是两个独立开关；harness 绝不越权
        # 打开 REAL 门（动态拼接避免扫描器撞见自身源码）
        self.assertNotIn("RUN_REAL_PROVIDER" + "_TESTS =", source)
        self.assertNotIn("environ" + "[", source)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
