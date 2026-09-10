"""P1-U2b: Qualification Evidence Persistence 的离线、确定性测试（TDD RED）。

本文件先于 scripts/evidence_store.py 存在（RED：模块缺席时 collection 失败）。
全部测试离线：不调用真实 runtime、不开 REAL gate、不触网络/凭据/auth/config；
磁盘面全部落在 TemporaryDirectory —— 绝不写真实用户 home。

锁定的契约（P1-EVIDENCE-BOUNDARY DESIGN READY，P1-U2b 授权实现）：
- Persistence 只是 durable storage projection：save/load 是
  CandidateValidationResult 的忠实 JSON 投影，重建走同一构造器
  （post_init 校验全复跑 = 免费的 schema + secret-free 复查），
  绝不成为第二套 qualification / admission truth。
- 只持久化 VERIFIED + REAL；其余 NOT_PERSISTABLE（持久层不升级 provenance）。
- 单文件隔离失败：一个坏文件绝不连累整库。
- 原子写：同目录 temp → flush+fsync → os.replace；无半写 JSON、无残留 temp。
- 身份绑定：四元组逐字强匹配；filename 与 payload identity 不一致即拒绝
  （persistent state 自相矛盾必须拒绝，绝不二选一信任）。
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import evidence_store  # noqa: E402  (RED: 模块尚不存在)
from candidate_validation import (  # noqa: E402
    CandidateValidationResult,
    CandidateValidationStatus,
    GateResult,
    GateVerdict,
    ValidationGate,
)

CAPS_ALL = ("architecture", "coding", "review", "testing")

SECRET_MARKERS = ("token", "secret", "api_key", "authorization", "bearer",
                  "stdout", "stderr")


def verified_real_result(runtime_id="rt-a", provider_id="provider-a",
                          model_id=None, fingerprint="default",
                          executed_at=1.5, gate_evidence=None):
    """确定性 VERIFIED+REAL 资格事实（离线 fixture；证据面全部 JSON 原生）。"""
    gate_evidence = gate_evidence or {}
    return CandidateValidationResult(
        identity=(runtime_id, provider_id, model_id, fingerprint),
        status=CandidateValidationStatus.VERIFIED,
        gates_passed=frozenset(ValidationGate),
        gate_results=tuple(
            GateResult(gate, GateVerdict.PASS,
                       evidence=dict(gate_evidence.get(gate.name, {})))
            for gate in ValidationGate),
        block_reason=None, failure_point=None,
        experiment_id="exp-001",
        executed_at=executed_at,
        validated_capabilities=CAPS_ALL,
        evidence={gate.name: "PASS" for gate in ValidationGate},
        provenance="REAL")


class RoundTripTests(unittest.TestCase):
    # -- 覆盖 1/2/3/4：roundtrip / 13 字段 / 确定性 / schema_version -------

    def test_verified_real_roundtrip_equal(self):
        # 1: JSON 原生证据面的全字段深相等（save → load → 构造器重建）。
        result = verified_real_result(gate_evidence={
            "G1_DISCOVERY": {"available": True, "version": "1.2.3"}})
        with tempfile.TemporaryDirectory() as tmp:
            evidence_store.save_evidence(tmp, result)
            evidence, rejected = evidence_store.load_evidence(tmp)
            self.assertEqual(rejected, ())
            self.assertEqual(list(evidence), [result.identity])
            self.assertEqual(evidence[result.identity], result)

    def test_thirteen_fields_roundtrip(self):
        # 2: 逐字段（13 项 qualification fields）零漂移。
        result = verified_real_result("rt-x", "prov-x", "model-x", "fp-x",
                                      executed_at=12.5)
        with tempfile.TemporaryDirectory() as tmp:
            evidence_store.save_evidence(tmp, result)
            evidence, _ = evidence_store.load_evidence(tmp)
            loaded = evidence[result.identity]
            self.assertEqual(loaded.identity,
                             ("rt-x", "prov-x", "model-x", "fp-x"))
            self.assertIs(loaded.status, CandidateValidationStatus.VERIFIED)
            self.assertEqual(loaded.gates_passed, frozenset(ValidationGate))
            self.assertEqual(len(loaded.gate_results), 14)
            self.assertEqual(loaded.gate_results[2].gate,
                             ValidationGate.G3_PROVIDER)
            self.assertEqual(loaded.gate_results[2].verdict, GateVerdict.PASS)
            self.assertEqual(loaded.gate_results[2].capabilities, ())
            self.assertIsNone(loaded.block_reason)
            self.assertIsNone(loaded.failure_point)
            self.assertEqual(loaded.experiment_id, "exp-001")
            self.assertEqual(loaded.executed_at, 12.5)
            self.assertEqual(loaded.validated_capabilities, CAPS_ALL)
            self.assertEqual(dict(loaded.evidence), dict(result.evidence))
            self.assertEqual(loaded.provenance, "REAL")

    def test_deterministic_serialization(self):
        # 3: 同一 result 两次落盘 → 同文件名 + 逐字节相同（sort_keys + 紧凑分隔符）。
        result = verified_real_result()
        with tempfile.TemporaryDirectory() as tmp_a, \
                tempfile.TemporaryDirectory() as tmp_b:
            path_a = evidence_store.save_evidence(tmp_a, result)
            path_b = evidence_store.save_evidence(tmp_b, result)
            self.assertEqual(path_a.name, path_b.name)
            self.assertEqual(path_a.read_bytes(), path_b.read_bytes())
            text = path_a.read_text(encoding="utf-8")
            self.assertNotIn(", ", text)
            self.assertNotIn(": ", text)

    def test_payload_carries_schema_version_one_only(self):
        # 4: 顶层恰好 13 个 qualification 字段 + schema_version=1，
        #    无任何 save_time / TTL / cache metadata。
        result = verified_real_result()
        with tempfile.TemporaryDirectory() as tmp:
            path = evidence_store.save_evidence(tmp, result)
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["schema_version"], 1)
            self.assertEqual(set(payload), {
                "schema_version", "identity", "status", "gates_passed",
                "gate_results", "block_reason", "failure_point",
                "experiment_id", "executed_at", "validated_capabilities",
                "evidence", "provenance"})


class PersistabilityTests(unittest.TestCase):
    # -- 覆盖 8/9：只持久化 VERIFIED + REAL ------------------------------

    def test_offline_cannot_persist(self):
        import dataclasses
        offline = dataclasses.replace(verified_real_result(),
                                      provenance="OFFLINE")
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError) as caught:
                evidence_store.save_evidence(tmp, offline)
            self.assertIn("NOT_PERSISTABLE", str(caught.exception))
            self.assertEqual(list(Path(tmp).glob("*.json")), [])

    def test_failed_cannot_persist(self):
        import dataclasses
        failed = dataclasses.replace(
            verified_real_result(),
            status=CandidateValidationStatus.FAILED,
            failure_point=(ValidationGate.G5_MINIMAL_INVOCATION, "minimal-timeout"))
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError) as caught:
                evidence_store.save_evidence(tmp, failed)
            self.assertIn("NOT_PERSISTABLE", str(caught.exception))
            self.assertEqual(list(Path(tmp).glob("*.json")), [])


class RejectionTests(unittest.TestCase):
    # -- 覆盖 5/6/7/10/11：schema 拒绝 / 隔离 / 空库 / 身份不一致 --------

    @staticmethod
    def _write(tmp, name, text):
        (Path(tmp) / name).write_text(text, encoding="utf-8")

    def test_unsupported_schema_rejected(self):
        result = verified_real_result()
        with tempfile.TemporaryDirectory() as tmp:
            path = evidence_store.save_evidence(tmp, result)
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["schema_version"] = 2
            path.write_text(json.dumps(payload), encoding="utf-8")
            evidence, rejected = evidence_store.load_evidence(tmp)
            self.assertEqual(evidence, {})
            self.assertEqual([r.reason for r in rejected],
                             ["UNSUPPORTED_SCHEMA"])

    def test_missing_schema_version_rejected_without_best_effort(self):
        result = verified_real_result()
        with tempfile.TemporaryDirectory() as tmp:
            path = evidence_store.save_evidence(tmp, result)
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload.pop("schema_version")
            path.write_text(json.dumps(payload), encoding="utf-8")
            evidence, rejected = evidence_store.load_evidence(tmp)
            self.assertEqual(evidence, {})
            self.assertEqual([r.reason for r in rejected],
                             ["UNSUPPORTED_SCHEMA"])

    def test_malformed_json_isolated(self):
        # 6: 一个坏文件绝不连累整库。
        good = verified_real_result()
        with tempfile.TemporaryDirectory() as tmp:
            evidence_store.save_evidence(tmp, good)
            self._write(tmp, "broken.json", "{not json")
            evidence, rejected = evidence_store.load_evidence(tmp)
            self.assertEqual(list(evidence), [good.identity])
            self.assertEqual([r.filename for r in rejected], ["broken.json"])
            self.assertEqual(rejected[0].reason, "MALFORMED_JSON")

    def test_invalid_payload_isolated(self):
        # 7: 可解析但重建失败（未知 status）→ INVALID_PAYLOAD，好文件不受
        #    影响。crafted 载荷须放在其 identity 的规范文件名下 —— 授权
        #    §9 链路里 filename 一致性先于重建。
        good = verified_real_result()
        seed = verified_real_result("rt-z", "prov-z")
        with tempfile.TemporaryDirectory() as tmp:
            evidence_store.save_evidence(tmp, good)
            path = evidence_store.save_evidence(tmp, seed)
            path.write_text(json.dumps({
                "schema_version": 1,
                "identity": ["rt-z", "prov-z", None, "default"],
                "status": "BOGUS"}), encoding="utf-8")
            evidence, rejected = evidence_store.load_evidence(tmp)
            self.assertEqual(list(evidence), [good.identity])
            self.assertEqual([r.reason for r in rejected],
                             ["INVALID_PAYLOAD"])

    def test_offline_evidence_file_rejected_by_store_contract(self):
        # 盘上 VERIFIED+OFFLINE 载荷：持久层契约只收 VERIFIED+REAL ——
        # 持久层绝不因"文件存在"而放宽 provenance。
        result = verified_real_result()
        with tempfile.TemporaryDirectory() as tmp:
            path = evidence_store.save_evidence(tmp, result)
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["provenance"] = "OFFLINE"
            path.write_text(json.dumps(payload), encoding="utf-8")
            evidence, rejected = evidence_store.load_evidence(tmp)
            self.assertEqual(evidence, {})
            self.assertEqual([r.reason for r in rejected],
                             ["INVALID_PAYLOAD"])

    def test_missing_directory_is_empty_store(self):
        # 10: 目录不存在 = 诚实空库，不是错误（新机首跑的合法状态）。
        with tempfile.TemporaryDirectory() as tmp:
            absent = Path(tmp) / "absent"
            evidence, rejected = evidence_store.load_evidence(absent)
            self.assertEqual(evidence, {})
            self.assertEqual(rejected, ())
            self.assertFalse(absent.exists())  # 绝不为读而建目录

    def test_filename_payload_identity_mismatch_rejected(self):
        # 11 + 设计修正（授权 §16）：filename 与 payload identity 不一致 →
        # 拒绝。绝不"以文件内为准"，persistent state 自相矛盾必须拒绝。
        result = verified_real_result()
        with tempfile.TemporaryDirectory() as tmp:
            evidence_store.save_evidence(tmp, result)
            files = list(Path(tmp).glob("*.json"))
            self.assertEqual(len(files), 1)
            payload_text = files[0].read_text(encoding="utf-8")
            self._write(tmp, "wrong-name.json", payload_text)
            evidence, rejected = evidence_store.load_evidence(tmp)
            self.assertEqual(list(evidence), [result.identity])
            self.assertEqual([r.filename for r in rejected],
                             ["wrong-name.json"])
            self.assertEqual(rejected[0].reason, "IDENTITY_MISMATCH")

    def test_runtime_identity_exact_match(self):
        # 15: 键 = 完整四元组；runtime_id-only 匹配不存在（同 id 不同
        # fingerprint 是两条独立资格，各自成文件）。
        one = verified_real_result("rt-a", "provider-a", None, "fp-one")
        two = verified_real_result("rt-a", "provider-a", None, "fp-two")
        with tempfile.TemporaryDirectory() as tmp:
            evidence_store.save_evidence(tmp, one)
            evidence_store.save_evidence(tmp, two)
            evidence, rejected = evidence_store.load_evidence(tmp)
            self.assertEqual(rejected, ())
            self.assertEqual(sorted(evidence), sorted([one.identity,
                                                       two.identity]))
            self.assertEqual(len({key[0] for key in evidence}), 1)
            self.assertEqual(len(evidence), 2)


class AtomicityTests(unittest.TestCase):
    # -- 覆盖 12/13：temp 清理 + 原子 replace ---------------------------

    def test_successful_save_leaves_no_temp_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            evidence_store.save_evidence(tmp, verified_real_result())
            entries = list(Path(tmp).iterdir())
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0].suffix, ".json")

    def test_failed_replace_cleans_temp_and_writes_nothing(self):
        # 12: replace 失败 → temp 被清理、目标不出现半写 JSON。
        with tempfile.TemporaryDirectory() as tmp:
            with patch("os.replace", side_effect=OSError("replace refused")):
                with self.assertRaises(OSError):
                    evidence_store.save_evidence(tmp, verified_real_result())
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_resave_replaces_atomically(self):
        # 13: 同 identity 重存 → 恰好一次同目录 os.replace（tmp → final），
        #     后写者完整生效，目录无残留。
        first = verified_real_result(executed_at=1.0)
        second = verified_real_result(executed_at=2.0)
        calls = []
        real_replace = os.replace

        def spy_replace(src, dst):
            calls.append((Path(src), Path(dst)))
            return real_replace(src, dst)

        with tempfile.TemporaryDirectory() as tmp:
            path_one = evidence_store.save_evidence(tmp, first)
            with patch("os.replace", side_effect=spy_replace):
                path_two = evidence_store.save_evidence(tmp, second)
            self.assertEqual(path_two, path_one)
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][0].parent, calls[0][1].parent)
            self.assertEqual(calls[0][0].suffix, ".tmp")
            payload = json.loads(path_one.read_text(encoding="utf-8"))
            self.assertEqual(payload["executed_at"], 2.0)
            entries = list(Path(tmp).iterdir())
            self.assertEqual([p.suffix for p in entries], [".json"])


class SecurityTests(unittest.TestCase):
    # -- 覆盖 14：secret-free 载荷 --------------------------------------

    def test_saved_payload_is_secret_free(self):
        result = verified_real_result(gate_evidence={
            "G1_DISCOVERY": {"available": True, "version": "1.2.3"}})
        with tempfile.TemporaryDirectory() as tmp:
            path = evidence_store.save_evidence(tmp, result)
            lowered = path.read_text(encoding="utf-8").lower()
            for marker in SECRET_MARKERS:
                self.assertNotIn(marker, lowered, marker)


class ReconstructionFidelityTests(unittest.TestCase):
    def test_failure_point_and_mixed_verdicts_reconstruct(self):
        # 构造载荷（非 runner 产物）：验证投影对 failure_point / 混合 verdict
        # 的忠实重建 —— 序列化必须无损承载全部合法形态。
        payload = {
            "schema_version": 1,
            "identity": ["rt-c", "prov-c", "model-c", "fp-c"],
            "status": "VERIFIED",
            "gates_passed": ["G1_DISCOVERY", "G2_AUTHENTICATION"],
            "gate_results": [
                {"gate": "G1_DISCOVERY", "verdict": "PASS", "reason": None,
                 "evidence": {"available": True}, "capabilities": ["coding"]},
                {"gate": "G3_PROVIDER", "verdict": "FAILED",
                 "reason": "provider-missing: oauth",
                 "evidence": {"provider": "prov-c"}, "capabilities": []},
                {"gate": "G7_TIMEOUT", "verdict": "NOT_RUN", "reason": None,
                 "evidence": {}, "capabilities": []},
            ],
            "block_reason": None,
            "failure_point": ["G3_PROVIDER", "provider-missing"],
            "experiment_id": "exp-c",
            "executed_at": 3.25,
            "validated_capabilities": ["coding"],
            "evidence": {"G1_DISCOVERY": "PASS", "G3_PROVIDER": "FAILED",
                         "G7_TIMEOUT": "NOT_RUN"},
            "provenance": "REAL",
        }
        seed = verified_real_result("rt-c", "prov-c", "model-c", "fp-c")
        with tempfile.TemporaryDirectory() as tmp:
            path = evidence_store.save_evidence(tmp, seed)
            path.write_text(json.dumps(payload), encoding="utf-8")
            evidence, rejected = evidence_store.load_evidence(tmp)
            self.assertEqual(rejected, ())
            loaded = evidence[("rt-c", "prov-c", "model-c", "fp-c")]
            self.assertEqual(loaded.failure_point,
                             (ValidationGate.G3_PROVIDER, "provider-missing"))
            self.assertEqual(loaded.gate_results[1].verdict, GateVerdict.FAILED)
            self.assertEqual(loaded.gate_results[1].reason,
                             "provider-missing: oauth")
            self.assertEqual(loaded.gate_results[2].verdict, GateVerdict.NOT_RUN)
            self.assertEqual(loaded.gate_results[0].capabilities, ("coding",))
            self.assertEqual(
                loaded.gates_passed,
                frozenset({ValidationGate.G1_DISCOVERY,
                           ValidationGate.G2_AUTHENTICATION}))
            self.assertEqual(loaded.validated_capabilities, ("coding",))

    def test_known_projection_boundary_tuple_evidence_becomes_list(self):
        # 已知投影边界（设计裁决，如实记录）：GateResult.evidence 内层
        # tuple → list（audit-only 容器形态漂移；语义字段零漂移）。
        # 真实 executor 的 G14 evidence 含 tuple —— 不做猜测式 list→tuple
        # 还原（evidence 值无形态契约，猜语义比漂移更危险）。
        gate = GateResult(ValidationGate.G14_CAPABILITY_EVIDENCE,
                          GateVerdict.PASS,
                          evidence={"roles": ("architect", "reviewer")})
        result = CandidateValidationResult(
            identity=("rt-t", "prov-t", None, "default"),
            status=CandidateValidationStatus.VERIFIED,
            gates_passed=frozenset(ValidationGate),
            gate_results=(gate,), block_reason=None, failure_point=None,
            experiment_id="exp-t", executed_at=0.0,
            validated_capabilities=CAPS_ALL, evidence={},
            provenance="REAL")
        with tempfile.TemporaryDirectory() as tmp:
            evidence_store.save_evidence(tmp, result)
            evidence, _ = evidence_store.load_evidence(tmp)
            loaded = evidence[result.identity]
            self.assertEqual(dict(loaded.gate_results[0].evidence),
                             {"roles": ["architect", "reviewer"]})
            self.assertNotEqual(loaded.gate_results[0], gate)
            self.assertEqual(loaded.identity, result.identity)
            self.assertEqual(loaded.validated_capabilities, CAPS_ALL)


class ModuleDisciplineTests(unittest.TestCase):
    # -- 持久层边界：persistence ≠ qualification ------------------------

    def test_public_surface_is_closed(self):
        self.assertEqual(
            set(evidence_store.__all__),
            {"SCHEMA_VERSION", "NOT_PERSISTABLE", "MALFORMED_JSON",
             "UNSUPPORTED_SCHEMA", "INVALID_PAYLOAD", "IDENTITY_MISMATCH",
             "EvidenceRejection", "save_evidence", "load_evidence"})

    def test_persistence_layer_holds_no_qualification_truth(self):
        source = Path(SCRIPTS, "evidence_store.py").read_text(encoding="utf-8")
        for banned_qualification in (
                "VerifiedRuntimePool", "RuntimeCandidateDiscovery",
                "GenericRuntimeHealth", "bootstrap_runtime_session",
                "run_real_validation", "real_validation_executor"):
            self.assertNotIn(banned_qualification, source,
                             banned_qualification)
        for banned_mechanism in ("subprocess", "socket", "pickle", "eval(",
                                 "exec(", "sqlite", "urllib", "requests"):
            self.assertNotIn(banned_mechanism, source, banned_mechanism)
        for required in ("os.replace", "fsync", "mkstemp", "sort_keys",
                         "separators"):
            self.assertIn(required, source, required)


class DeferredAuthEvidencePersistenceTests(unittest.TestCase):
    """CU-QWEN-AUTH-2：DEFERRED auth 证据的持久化边界。

    持久层零修改即承载契约：_payload 逐 gate 忠实投影 evidence dict；
    非 VERIFIED+REAL 的 DEFERRED 结果不可落盘（既有 NOT_PERSISTABLE）。
    """

    def test_payload_carries_deferred_evidence_only_verified_real(self):
        # Case A：VERIFIED+REAL 的 G2 DEFERRED 证据被忠实投影。
        deferred = {"auth_state": "UNKNOWN",
                    "auth_evidence": "DEFERRED_TO_INVOCATION"}
        result = verified_real_result(gate_evidence={
            "G2_AUTHENTICATION": deferred})
        payload = evidence_store._payload(result)
        g2 = next(g for g in payload["gate_results"]
                  if g["gate"] == "G2_AUTHENTICATION")
        self.assertEqual(g2["verdict"], "PASS")
        self.assertEqual(g2["evidence"], deferred)

        # Case B：带同样 DEFERRED 证据的非 VERIFIED 结果不可持久化。
        import dataclasses
        blocked = dataclasses.replace(
            result, status=CandidateValidationStatus.BLOCKED,
            block_reason="AUTH_REQUIRED: authentication state not authenticated")
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                evidence_store.save_evidence(tmp, blocked)


if __name__ == "__main__":
    unittest.main()
