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
