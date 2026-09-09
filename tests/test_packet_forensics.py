"""CU-R4（Architect Raw Output Forensics Capture）测试：packet parse
失败时的 parser-input 原文取证槽。

全部离线：fake executable 驱动真实 ClaudeCodeAdapter（零 REAL runtime）、
合成 malformed 输出驱动真实 host_entry CLI 链。核心验收 = 完整字符串
equality：captured raw 与真正送进 packet parser 的文本逐字节一致。
"""
import json
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
import sys

sys.path.insert(0, str(SCRIPTS))

import packet_forensics
from claude_code_adapter import ClaudeCodeAdapter
from external_runtime import (
    ExternalAgentRequest,
    InvocationStatus,
    RuntimeProfile,
)

# 与 REAL 失败同型的 malformed 形态：packet schema 开头、中段断裂。
MALFORMED_HEAD_BREAK = ('{"task_id": "t", "role": "architect", '
                        '"goal": ["a')
SECRET_SHAPED = ('before {"task_id": "t"} token=abc12345678 '
                'and api_key: zz9988776655 after')

# CU-R5 fixtures：合法 schema 的四家族 packet（clean 版与含凭据形状
# 描述词的版本 —— 后者复现 R2C/P1-1b 的 REDACTED_UNSAFE 失败类）。
VALID_ARCHITECT_PACKET_CLEAN = json.dumps({
    "task_id": "task_abc123def456", "role": "architect",
    "goal": ["analyze the helper"], "constraints": ["read-only"],
    "architecture": ["unify the fallback literals"],
    "interfaces": [{"name": "safe_error"}],
    "implementation_steps": [{"step": "add module"}],
    "acceptance_criteria": ["byte-identical output"],
    "risks": [{"risk": "none"}],
})
# 与 REAL 取证同型的 prose：描述脱敏模式本身的合法技术文本。
VALID_ARCHITECT_PACKET_BEARER_PROSE = json.dumps({
    "task_id": "task_abc123def456", "role": "architect",
    "goal": ["analyze the redaction helper"],
    "constraints": ["read-only"],
    "architecture": ["base 6 credential-shape patterns (api key / token "
                     "/ secret assignment, bearer material, hf_/sk- forms)"],
    "interfaces": [{"name": "safe_error"}],
    "implementation_steps": [{"step": "add module"}],
    "acceptance_criteria": ["byte-identical output"],
    "risks": [{"risk": "none"}],
})
VALID_CODER_PACKET = json.dumps({
    "task_id": "task_abc123def456", "role": "coder",
    "changed_files": ["a.py"], "implementation_summary": "s",
    "implementation_details": ["d"], "assumptions": [],
    "unresolved_items": [], "test_requirements": ["t"],
})
VALID_TESTER_PACKET = json.dumps({
    "task_id": "task_abc123def456", "role": "tester",
    "tests_run": ["t1"], "tests_passed": ["t1"], "tests_failed": [],
    "failures": [], "coverage_or_validation": ["full"],
    "remaining_risks": [],
})


def _remember_flush(*, runtime_id="rt-a", invocation_id="inv-x",
                    task_id="task_abc123def456", role="architect",
                    output=MALFORMED_HEAD_BREAK):
    packet_forensics.reset()
    packet_forensics.remember_invocation_output(
        runtime_id, invocation_id, task_id, role, output)


class ForensicsLifecycleTests(unittest.TestCase):
    """槽生命周期：remember 只进内存；reset 即清；persist 取走即清。"""

    def setUp(self):
        packet_forensics.reset()
        self._cleanup = []

    def tearDown(self):
        for path in self._cleanup:
            Path(path).unlink(missing_ok=True)
        packet_forensics.reset()

    def _persist(self, stage_hint="architect"):
        paths = packet_forensics.persist_pending(stage_hint=stage_hint)
        self._cleanup.extend(paths)
        return paths

    def test_remember_is_in_memory_only(self):
        packet_forensics.remember_invocation_output(
            "rt-a", "inv-1", "task_x", "architect", "raw")
        pending = packet_forensics.pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["raw_parser_input"], "raw")

    def test_reset_clears_and_nothing_persists(self):
        packet_forensics.remember_invocation_output(
            "rt-a", "inv-1", "task_x", "architect", "raw")
        packet_forensics.reset()
        self.assertEqual(packet_forensics.pending(), ())
        self.assertEqual(self._persist(), ())

    def test_persist_empty_store_returns_empty(self):
        self.assertEqual(self._persist(), ())


class ForensicsPersistIntegrityTests(unittest.TestCase):
    """落盘完整性：FULL 模式下 raw_parser_input 与记住的原文逐字节相等。"""

    def setUp(self):
        packet_forensics.reset()
        self._cleanup = []

    def tearDown(self):
        for path in self._cleanup:
            Path(path).unlink(missing_ok=True)
        packet_forensics.reset()

    def _persist_and_load(self, output, *, task_id="task_abc123def456",
                          invocation_id="inv-cu4-1", role="architect"):
        _remember_flush(output=output, task_id=task_id,
                        invocation_id=invocation_id, role=role)
        paths = packet_forensics.persist_pending(stage_hint="architect")
        self._cleanup.extend(paths)
        self.assertEqual(len(paths), 1)
        return json.loads(Path(paths[0]).read_text(encoding="utf-8"))

    def test_full_mode_capture_is_byte_identical(self):
        record = self._persist_and_load(MALFORMED_HEAD_BREAK)
        self.assertEqual(record["capture_mode"], "FULL")
        # 核心验收：完整字符串 equality，不是 length/head/tail。
        self.assertEqual(record["raw_parser_input"], MALFORMED_HEAD_BREAK)
        self.assertEqual(record["raw_type"], "str")
        self.assertEqual(record["raw_length"], len(MALFORMED_HEAD_BREAK))
        self.assertEqual(record["runtime_id"], "rt-a")
        self.assertEqual(record["invocation_id"], "inv-cu4-1")
        self.assertEqual(record["role"], "architect")
        self.assertEqual(record["failure_stage_hint"], "architect")
        self.assertIn("captured_at", record)

    def test_digest_matches_original(self):
        import hashlib
        record = self._persist_and_load(MALFORMED_HEAD_BREAK)
        expected = hashlib.sha256(
            MALFORMED_HEAD_BREAK.encode("utf-8")).hexdigest()
        self.assertEqual(record["sha256_raw"], expected)

    def test_two_invocations_never_overwrite(self):
        _remember_flush(output='{"a": 1', invocation_id="inv-cu4-a")
        packet_forensics.remember_invocation_output(
            "rt-a", "inv-cu4-b", "task_x", "coder", "second body")
        paths = packet_forensics.persist_pending(stage_hint="coder")
        self._cleanup.extend(paths)
        self.assertEqual(len(paths), 2)
        by_id = {}
        for path in paths:
            record = json.loads(Path(path).read_text(encoding="utf-8"))
            by_id[record["invocation_id"]] = record
        self.assertEqual(sorted(by_id), ["inv-cu4-a", "inv-cu4-b"])
        self.assertEqual(by_id["inv-cu4-a"]["raw_parser_input"], '{"a": 1')
        self.assertEqual(by_id["inv-cu4-b"]["raw_parser_input"],
                         "second body")

    def test_secret_shaped_output_never_plaintext_on_disk(self):
        record = self._persist_and_load(SECRET_SHAPED)
        self.assertEqual(record["capture_mode"], "REDACTED_UNSAFE")
        stored = record["raw_parser_input"]
        # 凭据值绝不明文落盘（marker 词本身按 G15 语义可以留下）
        self.assertNotIn("abc12345678", stored)
        self.assertNotIn("zz9988776655", stored)
        # redacted artifact 绝不声称等于 parser input：原文摘要与原长度
        # 独立保留供对账
        self.assertNotEqual(stored, SECRET_SHAPED)
        import hashlib
        self.assertEqual(
            record["sha256_raw"],
            hashlib.sha256(SECRET_SHAPED.encode("utf-8")).hexdigest())
        self.assertEqual(record["raw_length"], len(SECRET_SHAPED))
        self.assertIn("REDACTED_UNSAFE", json.dumps(record))

    def test_unsafe_task_id_redacted_in_metadata(self):
        record = self._persist_and_load(
            MALFORMED_HEAD_BREAK, task_id="token=abcd1234")
        self.assertEqual(record["task_id"], "REDACTED")

    def test_persist_io_failure_never_raises(self):
        _remember_flush()
        original = packet_forensics._write_entry

        def _boom(entry, stage_hint):
            raise OSError("disk unavailable")

        packet_forensics._write_entry = _boom
        try:
            paths = packet_forensics.persist_pending(stage_hint="architect")
        finally:
            packet_forensics._write_entry = original
        self.assertEqual(paths, ())
        self.assertEqual(packet_forensics.pending(), ())  # 取走即清仍成立


class ForensicsSelfTestFieldsTests(unittest.TestCase):
    """CU-R5（Packet Forensics Self-Test Fields）：取证记录内的两个只读
    自测字段 —— 对记住的 raw parser input 原文（落盘脱敏之前的内存值）
    重新执行 JSON decode 与 packet schema 检查，使下一次 *_PACKET_INVALID
    可以区分「JSON 解析失败 / schema 失败 / 后置内容扫描」。

    语义红线：自测只回答「现在重新检查会得到什么」，绝不参与生产
    acceptance/rejection（_packet_from_output 行为零变化）。
    """

    def setUp(self):
        packet_forensics.reset()
        self._cleanup = []

    def tearDown(self):
        for path in self._cleanup:
            Path(path).unlink(missing_ok=True)
        packet_forensics.reset()

    def _persist_and_load(self, output, *, task_id="task_abc123def456",
                          invocation_id="inv-cu5", role="architect"):
        packet_forensics.remember_invocation_output(
            "rt-a", invocation_id, task_id, role, output)
        paths = packet_forensics.persist_pending(stage_hint="architect")
        self._cleanup.extend(paths)
        self.assertEqual(len(paths), 1)
        return json.loads(Path(paths[0]).read_text(encoding="utf-8"))

    def test_a_valid_json_and_schema_pass(self):
        # A：合法 JSON + 合法 ArchitecturePacket schema → OK / PASS。
        record = self._persist_and_load(VALID_ARCHITECT_PACKET_CLEAN)
        self.assertEqual(record["capture_mode"], "FULL")
        self.assertEqual(record["self_test_json_loads"], "OK")
        self.assertEqual(record["self_test_packet_schema"], "PASS")

    def test_b_invalid_json_reports_exception_class_and_position(self):
        # B：坏 JSON → FAIL:<exception-class>:<pos>，schema N/A。
        record = self._persist_and_load(MALFORMED_HEAD_BREAK)
        self.assertEqual(record["capture_mode"], "FULL")
        self.assertEqual(record["self_test_packet_schema"], "N/A")
        self.assertRegex(record["self_test_json_loads"],
                         r"^FAIL:JSONDecodeError:\d+$")

    def test_c_valid_json_invalid_schema_reports_structured_rule(self):
        # C：合法 JSON + 缺必需字段 → OK / FAIL:MISSING_FIELDS（复用
        # R6-C11 结构化诊断 rule，绝不携带被拒值）。
        record = self._persist_and_load(
            json.dumps({"task_id": "task_abc123def456",
                        "role": "architect"}))
        self.assertEqual(record["self_test_json_loads"], "OK")
        self.assertEqual(record["self_test_packet_schema"],
                         "FAIL:MISSING_FIELDS")

    def test_session_family_scalar_normalization_is_honored(self):
        # session 家族忠实性：bare-string 列表字段经 _normalize 归一后
        # 生产会接受 → 自测同样 PASS（绝不误报 schema 失败）。
        packet = json.loads(VALID_ARCHITECT_PACKET_CLEAN)
        packet["goal"] = "single string goal"
        record = self._persist_and_load(json.dumps(packet))
        self.assertEqual(record["self_test_json_loads"], "OK")
        self.assertEqual(record["self_test_packet_schema"], "PASS")

    def test_verification_family_has_no_normalization(self):
        # verification 家族忠实性：tester 路径无 _normalize，bare-string
        # 列表字段按生产语义拒绝 → FAIL:NOT_A_LIST（两家族差异被钉死）。
        packet = json.loads(VALID_TESTER_PACKET)
        packet["tests_run"] = "t1"
        record = self._persist_and_load(json.dumps(packet), role="tester")
        self.assertEqual(record["self_test_json_loads"], "OK")
        self.assertEqual(record["self_test_packet_schema"],
                         "FAIL:NOT_A_LIST")

    def test_e_redacted_capture_self_test_runs_on_raw_pre_redaction(self):
        # E（核心）：REDACTED 模式下自测在脱敏前的原文上执行 —— 与
        # R2C/P1-1b 同型的 bearer-prose packet：mode=REDACTED_UNSAFE 证明
        # G15 扫描命中，但 json=OK、schema=PASS ⇒ 后置内容扫描即唯一
        # 剩余拒收 gate（本 CU 要买的判别力）。真实取证签名：bearer
        # 描述词不命中 _safe_error 六模式，stored 原样可见 + verbatim_note
        # 如实声明非逐字节相等。
        record = self._persist_and_load(VALID_ARCHITECT_PACKET_BEARER_PROSE)
        self.assertEqual(record["capture_mode"], "REDACTED_UNSAFE")
        self.assertIn("bearer material", record["raw_parser_input"])
        self.assertEqual(record["verbatim_note"],
                         "stored artifact is NOT the verbatim parser input")
        self.assertEqual(record["self_test_json_loads"], "OK")
        self.assertEqual(record["self_test_packet_schema"], "PASS")

    def test_non_object_json_fails_schema_as_non_object(self):
        # 顶层非对象：生产在 isinstance dict 检查处拒绝 → FAIL:NON_OBJECT。
        record = self._persist_and_load("[1, 2]")
        self.assertEqual(record["self_test_json_loads"], "OK")
        self.assertEqual(record["self_test_packet_schema"],
                         "FAIL:NON_OBJECT")

    def test_coder_and_tester_roles_map_to_their_families(self):
        for role, output in (("coder", VALID_CODER_PACKET),
                             ("tester", VALID_TESTER_PACKET)):
            with self.subTest(role=role):
                record = self._persist_and_load(output, role=role)
                self.assertEqual(record["self_test_json_loads"], "OK")
                self.assertEqual(record["self_test_packet_schema"], "PASS")

    def test_unmapped_role_is_na(self):
        record = self._persist_and_load(VALID_ARCHITECT_PACKET_CLEAN,
                                        role="observer")
        self.assertEqual(record["self_test_json_loads"], "OK")
        self.assertEqual(record["self_test_packet_schema"], "N/A")

    def test_non_string_output_matches_production_empty_text_path(self):
        # 非字符串输出：生产 _packet_from_output 取 "" → 解析必败；
        # 自测如实复现（FAIL:…:0），schema N/A。
        record = self._persist_and_load({"already": "parsed"})
        self.assertEqual(record["self_test_json_loads"],
                         "FAIL:JSONDecodeError:0")
        self.assertEqual(record["self_test_packet_schema"], "N/A")

    def test_diagnostic_slot_left_clean_and_stale_stamps_ignored(self):
        # 诊断槽卫生：自测前后 reset —— 陈旧诊断绝不冒充本次失败原因；
        # persist 完成后槽为 None（零跨调用污染）。
        from content_safety import (
            ValidationDiagnostic,
            last_validation_diagnostic,
            record_validation_diagnostic,
            reset_validation_diagnostic,
        )
        record_validation_diagnostic(
            ValidationDiagnostic("packet", "goal", None, "UNSAFE_SHAPE"))
        record = self._persist_and_load(VALID_ARCHITECT_PACKET_CLEAN)
        self.assertEqual(record["self_test_packet_schema"], "PASS")
        self.assertIsNone(last_validation_diagnostic())
        record = self._persist_and_load(
            json.dumps({"task_id": "task_abc123def456",
                        "role": "architect"}))
        self.assertEqual(record["self_test_packet_schema"],
                         "FAIL:MISSING_FIELDS")
        self.assertIsNone(last_validation_diagnostic())
        reset_validation_diagnostic()


class AdapterRememberWiringTests(unittest.TestCase):
    """真实 ClaudeCodeAdapter 的 remember 接线（fake executable，零 REAL）：
    记住的字符串与 InvocationResult.output 逐字节一致 —— 而冻结件
    collaboration_session.py:235 把 result.output 原样送进 parser，因此
    该相等即「captured == parser input」。"""

    def setUp(self):
        packet_forensics.reset()

    def tearDown(self):
        packet_forensics.reset()

    def test_remembered_output_equals_invocation_result_output(self):
        payload = '{"task_id": "t", "role": "architect", "goal": ["a'
        envelope = ('{"type":"result","subtype":"success","result":'
                    + json.dumps(payload) + '}')
        with tempfile.TemporaryDirectory() as tmp:
            stub = Path(tmp) / "fake-claude.cmd"
            stub.write_text("@echo " + envelope + "\n", encoding="ascii")
            profile = RuntimeProfile(
                "coding-agent", "claude-cli", "anthropic", None, "coder",
                frozenset())
            adapter = ClaudeCodeAdapter(profile, str(stub))
            result = adapter.invoke(ExternalAgentRequest(
                task_id="task_9f8e7d6c5b4a", prompt="design prompt",
                agent_id="architect", role="architect", timeout_seconds=30))
        self.assertIs(result.status, InvocationStatus.SUCCESS)
        self.assertEqual(result.output, payload)  # envelope 提取成立
        pending = packet_forensics.pending()
        self.assertEqual(len(pending), 1)
        # 核心验收：完整字符串 equality（与 parser 实际输入一致）。
        self.assertEqual(pending[0]["raw_parser_input"], result.output)
        self.assertEqual(pending[0]["runtime_id"], "claude-cli")
        self.assertEqual(pending[0]["role"], "architect")
        self.assertEqual(pending[0]["task_id"], "task_9f8e7d6c5b4a")


if __name__ == "__main__":
    unittest.main()
