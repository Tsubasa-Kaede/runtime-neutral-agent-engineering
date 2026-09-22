"""CU-BENCH BENCH-REAL — REAL execution benchmark（双门控）。

门：RUN_REAL_PROVIDER_TESTS=1 且 COCKPIT_BENCHMARK=1（双门缺一
即整体 skip）。本文件只在用户显式 REAL benchmark 授权轮运行
（2026-09-23 BENCH-REAL 授权）；一次全量执行 = 固定 manifest 的
至多 46 次真实 provider invocation（claude-cli / pi-cli，预算硬顶
由代码断言——超顶即失败停机，零自动扩样、零自动重跑）。

实验对象（承 BENCH-1 四臂，REAL 执行）：
- A Single-Agent baseline（compiler 生产 builder，1 步）；
- B Multi-Agent legacy 臂（repo 冻结 oracle _legacy_prompt 直构，
  harness 级 builder——零生产改动）；
- C Multi-Agent compiler 臂（生产 _make_request_builder 真编译，
  disclosure_sink 同步采集 char 面）；
- D ORCH-5 OFF（resolve canonical）vs ON（routed + 会话前缀冻结）。

阶段（授权 §二十）：cold N=1 各臂 → usage integrity 门（零 KNOWN
即 telemetry 疑似破相停机）→ warm N=5 各臂 → D OFF/ON 各 N=1 →
分域汇总 + coverage 审计。

真值律（授权 §九-§十一）：Runtime token 唯一真值 = UsageCapture
→ UsageRecord / UsageLog；三态 KNOWN / UNKNOWN / ABSENT 诚实分列，
禁 UNKNOWN→0、禁 ABSENT→0、禁字符→token、禁均值填充；per-runtime
分桶 + 会话级 KNOWN-only 聚合；coverage<100% 时 token 结论恒
"KNOWN-SUBSET OBSERVED"。

解释边界（授权 §十三-§十六）：比较顺序 invocation→chars→KNOWN
tokens→latency→monetary(UNSUPPORTED)；禁一切节省句式；B≡C 离线
字节等价已证，REAL 只观察——chars 同而 tokens 异时归因候选=
provider sampling variability，非 Compiler；ORCH-5 selection
change 与 token change 分列，无 KNOWN 证据只说 SELECTION_CHANGED。
"""
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
TESTS = Path(__file__).resolve().parent
for _path in (str(SCRIPTS), str(TESTS)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import benchmark_fixtures  # noqa: E402
import cockpit_entry  # noqa: E402
from claude_code_adapter import ClaudeCodeAdapter  # noqa: E402
from control_journal import ControlJournal  # noqa: E402
from execution_slots import ExecutionSlotSpec, build_execution_slots  # noqa: E402
from external_runtime import ExternalAgentRequest  # noqa: E402
from pi_adapter import PiAdapter  # noqa: E402
from sequential_pipeline import (  # noqa: E402
    RunStatus,
    StepSpec,
    build_sequential_pipeline,
)
from test_context_wire import _legacy_prompt  # noqa: E402
from usage_log import UsageLog, UsageObservation  # noqa: E402

RUN_REAL_PROVIDER_TESTS = os.environ.get("RUN_REAL_PROVIDER_TESTS") == "1"
_BENCHMARK_GATE = os.environ.get("COCKPIT_BENCHMARK") == "1"
_BENCH_REAL_ON = RUN_REAL_PROVIDER_TESTS and _BENCHMARK_GATE

_TIMEOUT_SECONDS = 300.0
_WARM_REPEATS = 5   # warm N（授权 §七：warm >= 5）
_MAX_INVOCATIONS = 46  # 预算硬顶（§十八）：A6+B18+C18+D4
_RUN_PREFIX = "benchreal2"  # RECOVERY 轮全新 run_id（旧轮=historical）

# ---- 固定实验常量（manifest 恒定项；一次授权轮内零漂移）----
_STEPS_MULTI = (("architect", "claude-cli"), ("coder", "pi-cli"),
                ("reviewer", "claude-cli"))
_STEPS_SINGLE = (("coder", "claude-cli"),)
_POLICY_COMPILER = "compiler-production-path"
_POLICY_LEGACY = "legacy-oracle-formula"
_POLICY_ROUTE_OFF = "route-off-canonical"
_POLICY_ROUTE_ON = "route-on-invocation-count"


def _stdout(payload):
    import time
    payload["t"] = round(time.time(), 3)
    print(json.dumps(payload, ensure_ascii=False, default=str),
          flush=True)


def _runtime_versions():
    """本地 CLI 版本（--version 本地打印，非 provider invocation）。

    shutil.which 解析 Windows npm .cmd shim（裸 argv 在 CreateProcess
    下不可见——RECOVERY 轮实证）。
    """
    import shutil
    versions = {}
    for runtime_id, executable in (("claude-cli", "claude"),
                                   ("pi-cli", "pi")):
        resolved = shutil.which(executable)
        if resolved is None:
            versions[runtime_id] = "executable not on PATH"
            continue
        try:
            done = subprocess.run(
                [resolved, "--version"], capture_output=True,
                text=True, timeout=30)
            versions[runtime_id] = (
                done.stdout + done.stderr).strip()[:80]
        except Exception as error:  # noqa: BLE001 - 版本探测失败即记录
            versions[runtime_id] = f"version probe failed: {error}"
    return versions


class _RealSession:
    """一次 BENCH-REAL 会话：适配器、预算、证据前缀、逐 run 执行。"""

    def __init__(self):
        self.invocation_count = 0
        self.prompt_log = []      # (runtime_id, prompt_chars) 逐调用
        self.session_records = []  # append-after-terminal 会话证据
        self.rows = []             # §十二 per-invocation 行
        self.adapters, self.providers = {}, {}
        for runtime_id, factory in (("claude-cli",
                                     ClaudeCodeAdapter.from_environment),
                                    ("pi-cli",
                                     PiAdapter.from_environment)):
            adapter = factory()
            if adapter is None:
                raise unittest.SkipTest(
                    f"{runtime_id} executable not found")
            self._wrap(adapter, runtime_id)
            self.adapters[runtime_id] = adapter
            self.providers[runtime_id] = adapter.profile.provider
        self.versions = _runtime_versions()
        self.identity = tuple(
            (runtime_id, self.providers[runtime_id], None)
            for runtime_id in sorted(self.adapters))

    def _wrap(self, adapter, runtime_id):
        """ORCH-4 先例：实例级 delegate-through 包装——预算计数 +
        prompt_chars 采集（request 在此可见；结果零拦截零改写）。"""
        real_invoke = adapter.invoke

        def wrapped(request):
            self.invocation_count += 1
            if self.invocation_count > _MAX_INVOCATIONS:
                raise RuntimeError(
                    "benchmark budget exceeded: "
                    f"{self.invocation_count} > {_MAX_INVOCATIONS}")
            result = real_invoke(request)
            self.prompt_log.append(
                (runtime_id, len(request.prompt)))
            return result

        adapter.invoke = wrapped

    def prefix(self):
        """pre-run cutoff 冻结：run 前缀快照（tuple 拷贝语义）。"""
        return tuple(self.session_records)

    def _compiler_builder(self, task, task_id, role, runtime_id,
                          step_index, previous_role, disclosures):
        return cockpit_entry._make_request_builder(
            task, task_id, role, self.providers[runtime_id],
            _TIMEOUT_SECONDS, runtime_id=runtime_id,
            previous_role=previous_role, step_index=step_index,
            disclosure_sink=disclosures.append)

    def _legacy_builder(self, task, task_id, role, runtime_id,
                        char_facts):
        provider = self.providers[runtime_id]

        def request_builder(previous_result):
            prior = ""
            if previous_result is not None:
                prior = previous_result.output or ""
            prompt = _legacy_prompt(role, task, prior)
            char_facts.append({
                "prior_output_chars": (
                    min(len(prior), cockpit_entry._EMBED_LIMIT)
                    if prior else 0),
                "truncation_count": (
                    1 if len(prior) > cockpit_entry._EMBED_LIMIT else 0),
            })
            return ExternalAgentRequest(
                task_id=task_id, prompt=prompt,
                agent_id=f"cockpit-{role}", role=role,
                provider=provider, model=None,
                timeout_seconds=_TIMEOUT_SECONDS)

        return request_builder

    def run_arm(self, arm, task, steps, mode, run_index, policy_label,
                routing_decision=None):
        """一次 REAL run：ORCH-4 装配先例 + 生产 WrapStack→UsageLog。

        mode: "compiler"（A/C/D 臂，生产 builder）或 "legacy"（B 臂）。
        """
        cutoff_prefix = self.prefix()
        warm = run_index > 0
        run_id = f"{_RUN_PREFIX}-{arm}-r{run_index}"
        task_id = f"{run_id}-task"
        disclosures, char_facts = [], []
        journal = ControlJournal()
        usage_log = UsageLog()
        slot_specs, step_specs = [], []
        previous_role = None
        for step_index, (role, runtime_id) in enumerate(steps):
            adapter = self.adapters[runtime_id]
            slot_id = f"slot-{step_index}-{role}"
            slot_specs.append(ExecutionSlotSpec(
                slot_id=slot_id, raw_adapter=adapter,
                usage_log=usage_log, runtime_id=runtime_id, role=role))
            if mode == "compiler":
                builder = self._compiler_builder(
                    task, task_id, role, runtime_id, step_index,
                    previous_role, disclosures)
            else:
                builder = self._legacy_builder(
                    task, task_id, role, runtime_id, char_facts)
            step_specs.append(StepSpec(slot_id=slot_id,
                                       request_builder=builder))
            previous_role = role
        slots = build_execution_slots(journal, tuple(slot_specs),
                                      execution_id=f"{run_id}-exec")
        pipeline = build_sequential_pipeline(slots, tuple(step_specs))
        outcome = pipeline.run()
        records = usage_log.snapshot()

        # ---- 完整性门（§十七：任一失败即 raise 停机）----
        if outcome.status is not RunStatus.COMPLETED or outcome.error:
            # pipeline :236-238：非 SUCCESS 步骤 → FAILED + final_result
            # 携带真实细节（outcome.error 恒 None）——取证必读面。
            final = outcome.final_result
            transcript = [
                {"step_index": rec.step_index, "slot_id": rec.slot_id,
                 "status": str(rec.status),
                 "invocation_id": rec.invocation_id}
                for rec in outcome.transcript]
            _stdout({"event": "run_failed", "run_id": run_id,
                     "status": str(outcome.status),
                     "error": str(outcome.error),
                     "final_result_status": (
                         str(final.status) if final is not None
                         else None),
                     "final_result_error": (
                         final.error if final is not None else None),
                     "final_result_trace": (
                         {"invocation_id": final.trace.invocation_id,
                          "input_tokens": final.trace.input_tokens,
                          "output_tokens": final.trace.output_tokens}
                         if final is not None
                         and final.trace is not None else None),
                     "transcript": transcript})
            raise AssertionError(
                f"{run_id} did not complete: status={outcome.status} "
                f"final_status="
                f"{final.status if final is not None else None!r} "
                f"final_error="
                f"{final.error if final is not None else None!r}")
        if len(records) != len(steps):
            _stdout({"event": "usage_integrity_violation",
                     "run_id": run_id, "records": len(records),
                     "plan": len(steps)})
            raise AssertionError(
                f"{run_id} usage records {len(records)} != plan "
                f"{len(steps)} (hidden retry suspected)")

        run_prompts = self.prompt_log[-len(steps):]
        manifest = benchmark_fixtures.BenchmarkManifest(
            run_id=run_id,
            task_fingerprint=benchmark_fixtures.task_fingerprint(task),
            composition_fingerprint=(
                benchmark_fixtures.composition_fingerprint(
                    steps, policy_label)),
            runtime_fingerprint=self.identity,
            cutoff_index=len(cutoff_prefix), warm=warm, repeats=1)
        for step_index, ((role, runtime_id), record) in enumerate(
                zip(steps, records)):
            if mode == "compiler":
                disclosure = disclosures[step_index]
                prior_chars = dict(
                    disclosure.embedded_chars_by_kind).get(
                        "PRIOR_STEP_OUTPUT", 0)
                embedded = disclosure.total_embedded_chars
                truncations = len(disclosure.truncations)
            else:
                fact = char_facts[step_index]
                prior_chars = fact["prior_output_chars"]
                embedded = None
                truncations = fact["truncation_count"]
            self.rows.append({
                "benchmark_run_id": run_id,
                "experiment_arm": arm,
                "run_index": run_index,
                "cold_or_warm": "warm" if warm else "cold",
                "task_fingerprint": manifest.task_fingerprint,
                "composition_fingerprint":
                    manifest.composition_fingerprint,
                "cutoff_index": manifest.cutoff_index,
                "step_index": step_index,
                "role": role,
                "runtime_id": runtime_id,
                "runtime_version": self.versions[runtime_id],
                "provider_id": self.providers[runtime_id],
                "invocation_id": record.invocation_id,
                "usage_state": record.usage_status.value,
                "input_tokens": record.input_tokens,
                "output_tokens": record.output_tokens,
                "total_tokens": (
                    record.input_tokens + record.output_tokens
                    if isinstance(record.input_tokens, int)
                    and isinstance(record.output_tokens, int)
                    else None),
                "duration_ms": record.duration_ms,
                "prompt_chars": run_prompts[step_index][1],
                "prior_output_chars": prior_chars,
                "embedded_chars": embedded,
                "truncation_count": truncations,
                "routing_decision": routing_decision,
                "revision_count": 0,
            })
        self.session_records.extend(records)
        summary = {
            "event": "run_completed", "run_id": run_id, "arm": arm,
            "run_index": run_index,
            "cold_or_warm": "warm" if warm else "cold",
            "cutoff_index": manifest.cutoff_index,
            "invocation_count": len(records),
            "usage_states": [record.usage_status.value
                             for record in records],
            "durations_ms": [record.duration_ms for record in records],
            "prompt_chars": [entry[1] for entry in run_prompts],
            "budget_spent": self.invocation_count,
        }
        _stdout(summary)
        return summary


def _coverage_of(rows):
    """行级 coverage 三数（§十；行即 UsageRecord 真值转录）。"""
    known = sum(1 for row in rows if row["usage_state"] == "KNOWN")
    total = len(rows)
    return {"known_records": known,
            "unknown_records": total - known,
            "absent_records": 0,  # 完整性门已证 plan==records
            "total_records": total,
            "coverage": f"{known}/{total}",
            "coverage_complete": known == total}


def _arm_totals(rows):
    """臂级 KNOWN-only token 观察（coverage<100% 恒 KNOWN-SUBSET）。"""
    coverage = _coverage_of(rows)
    known = [row for row in rows if row["usage_state"] == "KNOWN"]
    return {
        "invocations": len(rows),
        "coverage": coverage["coverage"],
        "coverage_complete": coverage["coverage_complete"],
        "known_input_tokens": sum(
            row["input_tokens"] for row in known
            if isinstance(row["input_tokens"], int)),
        "known_output_tokens": sum(
            row["output_tokens"] for row in known
            if isinstance(row["output_tokens"], int)),
        "known_total_tokens": sum(
            row["total_tokens"] for row in known
            if isinstance(row["total_tokens"], int)),
        "prompt_chars_total": sum(row["prompt_chars"] for row in rows),
        "duration_ms_values": [row["duration_ms"] for row in rows],
        "claim": "OBSERVED (full coverage)" if coverage[
            "coverage_complete"] else "KNOWN-SUBSET OBSERVED",
    }


def _runtime_buckets(rows):
    """per-runtime 分桶（§十一：异构 runtime 绝不混桶）。"""
    buckets = {}
    for row in rows:
        buckets.setdefault(row["runtime_id"], []).append(row)
    report = {}
    for runtime_id, bucket in sorted(buckets.items()):
        known = [row for row in bucket
                 if row["usage_state"] == "KNOWN"]
        report[runtime_id] = {
            "runtime_version": bucket[0]["runtime_version"],
            "provider_id": bucket[0]["provider_id"],
            "records": len(bucket),
            "known_records": len(known),
            "unknown_records": len(bucket) - len(known),
            "coverage": f"{len(known)}/{len(bucket)}",
            "known_input_tokens": sum(
                row["input_tokens"] for row in known
                if isinstance(row["input_tokens"], int)),
            "known_output_tokens": sum(
                row["output_tokens"] for row in known
                if isinstance(row["output_tokens"], int)),
            "duration_ms_values": [row["duration_ms"]
                                   for row in bucket],
        }
    return report


@unittest.skipUnless(_BENCH_REAL_ON,
                     "bench-real requires RUN_REAL_PROVIDER_TESTS=1 "
                     "and COCKPIT_BENCHMARK=1")
class BenchRealSessionTests(unittest.TestCase):
    """一次受控 BENCH-REAL 会话：A/B/C/D 四臂 + 完整性门 + 报告。"""

    def _arm_gate(self, arm):
        """RECOVERY §八：每臂 cold 后完整性门（失败即停机）。

        检查：records==plan（per-run 已断言，此处臂级复核）、
        usage state 完整（无 UNSUPPORTED）、claude 席位 telemetry
        存活（至少一条 KNOWN）、runtime 版本零漂移、manifest
        fingerprint 臂内一致。
        """
        arm_rows = [row for row in self.session.rows
                    if row["experiment_arm"] == arm]
        self.assertTrue(arm_rows, f"{arm} has no rows")
        for row in arm_rows:
            self.assertIn(row["usage_state"], ("KNOWN", "UNKNOWN"),
                          f"{arm} row {row['invocation_id']}")
        known = [row for row in arm_rows
                 if row["usage_state"] == "KNOWN"]
        if not known:
            _stdout({"event": "telemetry_schema_violation",
                     "arm": arm,
                     "detail": "zero KNOWN records — claude seat "
                               "telemetry suspected broken"})
            raise AssertionError(
                f"{arm}: zero KNOWN usage records (token telemetry "
                "schema violation suspected; STOP per authorization "
                "section 16)")
        fingerprints = {(row["task_fingerprint"],
                         row["composition_fingerprint"])
                        for row in arm_rows}
        self.assertEqual(len(fingerprints), 1,
                         f"{arm} manifest fingerprint drift: "
                         f"{fingerprints}")
        versions_now = _runtime_versions()
        if versions_now != self.session.versions:
            _stdout({"event": "runtime_version_drift",
                     "arm": arm, "start": self.session.versions,
                     "now": versions_now})
            raise AssertionError(
                f"{arm}: runtime version drift detected")
        _stdout({"event": "arm_gate_passed", "arm": arm,
                 "rows": len(arm_rows),
                 "known_records": len(known)})

    def test_bench_real_full_session(self):
        self.session = _RealSession()
        session = self.session
        _stdout({"event": "session_start",
                 "run_prefix": _RUN_PREFIX,
                 "runtimes": list(session.identity),
                 "versions": session.versions,
                 "budget_max_invocations": _MAX_INVOCATIONS})

        arm_specs = (
            ("A-single", benchmark_fixtures.SINGLE_TASK, _STEPS_SINGLE,
             "compiler", _POLICY_COMPILER),
            ("B-legacy", benchmark_fixtures.MULTI_TASK, _STEPS_MULTI,
             "legacy", _POLICY_LEGACY),
            ("C-compiler", benchmark_fixtures.MULTI_TASK, _STEPS_MULTI,
             "compiler", _POLICY_COMPILER),
        )

        # ---- RECOVERY §八：cold 逐臂执行，每臂后即完整性门 ----
        for arm, task, steps, mode, policy in arm_specs:
            session.run_arm(arm, task, steps, mode, 0, policy)
            self._arm_gate(arm)

        # ---- D ORCH-5 OFF vs ON（各 N=1，cold 全成后才执行）----
        pool2 = tuple(sorted(
            ({"runtime_id": runtime_id,
              "provider_id": session.providers[runtime_id],
              "identity": (runtime_id, session.providers[runtime_id],
                           None, "bench-real")}
             for runtime_id in session.adapters),
            key=lambda entry: entry["runtime_id"]))

        def _steps_from_bindings(bindings):
            return tuple((binding.role, binding.runtime_id)
                         for binding in bindings)

        off = cockpit_entry.resolve_default_composition(pool2)
        self.assertIsNone(off.blocked_reason)
        off_steps = _steps_from_bindings(off.bindings)
        session.run_arm("D-route-off", benchmark_fixtures.MULTI_TASK,
                        off_steps, "compiler", 0, _POLICY_ROUTE_OFF,
                        routing_decision="OFF canonical sorted zip")
        self._arm_gate("D-route-off")

        frozen_prefix = session.prefix()  # ON 冻结前缀（cutoff）
        on = cockpit_entry.routed_default_composition(
            pool2, frozen_prefix)
        self.assertIsNone(on.blocked_reason)
        on_steps = _steps_from_bindings(on.bindings)
        # 决策确定性交叉验证：同一冻结前缀重算恒同（零未来读取）
        on_replay = cockpit_entry.routed_default_composition(
            pool2, frozen_prefix)
        self.assertEqual(
            _steps_from_bindings(on_replay.bindings), on_steps)
        session.run_arm("D-route-on", benchmark_fixtures.MULTI_TASK,
                        on_steps, "compiler", 1, _POLICY_ROUTE_ON,
                        routing_decision=(
                            f"ON warm prefix cutoff="
                            f"{len(frozen_prefix)} invocation-count"))
        self._arm_gate("D-route-on")

        # ---- RECOVERY §八/§十三：全 cold 完成后才进入 warm N=5 ----
        for arm, task, steps, mode, policy in arm_specs:
            for run_index in range(1, 1 + _WARM_REPEATS):
                session.run_arm(arm, task, steps, mode, run_index,
                                policy)

        # ---- 分域汇总 + coverage 审计 ----
        rows = session.rows
        self.assertEqual(session.invocation_count, _MAX_INVOCATIONS)
        arms = {arm: _arm_totals([row for row in rows
                                  if row["experiment_arm"] == arm])
                for arm in ("A-single", "B-legacy", "C-compiler",
                            "D-route-off", "D-route-on")}
        report = {
            "event": "bench_real_final_report",
            "run_prefix": _RUN_PREFIX,
            "baseline": "7b81b4c",
            "versions": session.versions,
            "runtimes": list(session.identity),
            "n_requested": {"A": 6, "B": 6, "C": 6, "D-off": 1,
                            "D-on": 1},
            "n_achieved": {"A": 6, "B": 6, "C": 6, "D-off": 1,
                           "D-on": 1},
            "real_invocation_count": session.invocation_count,
            "arms": arms,
            "runtime_buckets": _runtime_buckets(rows),
            "orch5": {
                "off_selection": [list(step) for step in off_steps],
                "on_selection": [list(step) for step in on_steps],
                "selection_changed": off_steps != on_steps,
                "invocation_delta": len(on_steps) - len(off_steps),
                "classification": (
                    "SELECTION_CHANGED" if off_steps != on_steps
                    else "SELECTION_UNCHANGED"),
            },
            "b_vs_c": {
                "offline_facts": (
                    "B≡C CHAR OUTPUT EQUIVALENCE (BENCH-1, "
                    "byte-identical prompts given identical priors)"),
                "real_caveat": (
                    "REAL priors diverge run-to-run (provider "
                    "non-determinism); token differences between "
                    "arms are OBSERVED only — attribution candidates "
                    "include sampling variability, not Compiler"),
                "B_known_total_tokens": arms["B-legacy"][
                    "known_total_tokens"],
                "C_known_total_tokens": arms["C-compiler"][
                    "known_total_tokens"],
                "B_coverage": arms["B-legacy"]["coverage"],
                "C_coverage": arms["C-compiler"]["coverage"],
                "B_prompt_chars_total": arms["B-legacy"][
                    "prompt_chars_total"],
                "C_prompt_chars_total": arms["C-compiler"][
                    "prompt_chars_total"],
            },
            "monetary": "UNSUPPORTED (no price facts)",
            "rows": rows,
        }
        _stdout(report)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
