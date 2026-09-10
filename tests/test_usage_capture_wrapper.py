"""CU-OBS-4b tests: UsageCapture（usage 观察 + raw handoff 缝）。

栈位：AbortGate → RevisionAdapter(未来) → UsageCapture → raw adapter。

核心契约：
- APPLIED = raw.invoke 已实际发生；delegation 一旦发生，即使随后
  timeout/failure/exception，handoff 仍然成立。
- invocation_id 由本层在真实 invocation 边界铸造（opaque+unique），
  只落在 UsageRecord；request 原样转发，绝不改写。
- raw 参数/返回值/异常（含 ControlAborted）原样透传。
- usage 三态：双方真实整数 → KNOWN；声明 UNSUPPORTED 且无事实 →
  UNSUPPORTED（OBS-3：已获事实不受 UNSUPPORTED 阻止）；其余 UNKNOWN。
  unknown 绝不变 0；absent record ≠ UNKNOWN；零估算。
- UsageLog append 失败被隔离：绝不改变 raw invocation outcome。
- applied_revisions 仅 correlation；无 REVISION_APPLIED；零执行事件。

路径注：本文件是 CU-OBS-4b wrapper 层测试；V2 冻结的 adapter 层
usage 契约测试在 tests/test_usage_capture.py（@ d231067），两者
零触碰、互不替代。
"""
import ast
import sys
import threading
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from usage_capture import UsageCapture, UsageCaptureError  # noqa: E402
from control_gate import ControlAborted  # noqa: E402  测试侧引用
from external_runtime import (  # noqa: E402  测试侧构造真实协议形状
    ExternalAgentRequest,
    InvocationResult,
    InvocationStatus,
    InvocationTrace,
)
from observation_capability import (  # noqa: E402
    ObservationCapabilities,
    ObservationCapabilityState,
    ObservationKind,
)
from usage_log import UsageLog, UsageObservation  # noqa: E402

MODULE_PATH = SCRIPTS / "usage_capture.py"


def make_request(task_id="task-1", agent_id="agent-1"):
    return ExternalAgentRequest(task_id=task_id, prompt="do work",
                                agent_id=agent_id, role="coder")


def make_trace(input_tokens="unknown", output_tokens="unknown",
               status=InvocationStatus.SUCCESS):
    return InvocationTrace(
        invocation_id="inner-trace-id", task_id="task-1", agent_id="agent-1",
        runtime="runtime-x", provider=None, model=None, role="coder",
        status=status, input_tokens=input_tokens,
        output_tokens=output_tokens)


class _SpyRaw:
    """结构化 raw adapter 替身：记录收到的 request，可配置返回/抛出。"""

    def __init__(self, result=None, raises=None):
        self.calls = []
        self._result = result
        self._raises = raises

    def invoke(self, request):
        self.calls.append(request)
        if self._raises is not None:
            raise self._raises
        return self._result


def make_capture(raw=None, runtime_id="runtime-x", role="coder",
                 capabilities="default", usage_log=None):
    if usage_log is None:
        usage_log = UsageLog()
    if capabilities == "default":
        capabilities = ObservationCapabilities(by_kind={})
    capture = UsageCapture(raw if raw is not None else _SpyRaw(),
                           usage_log, runtime_id=runtime_id, role=role,
                           capabilities=capabilities)
    return capture, usage_log


class ConstructionTests(unittest.TestCase):
    def test_requires_callable_raw_invoke(self):
        with self.assertRaises(UsageCaptureError):
            UsageCapture(object(), UsageLog(), runtime_id="r", role="coder")

    def test_requires_callable_usage_log_append(self):
        with self.assertRaises(UsageCaptureError):
            UsageCapture(_SpyRaw(), object(), runtime_id="r", role="coder")

    def test_requires_non_empty_context(self):
        for bad in ((" ", "coder"), ("runtime-x", "")):
            with self.assertRaises(UsageCaptureError):
                UsageCapture(_SpyRaw(), UsageLog(), runtime_id=bad[0],
                             role=bad[1])

    def test_rejects_wrong_capabilities_type(self):
        with self.assertRaises(UsageCaptureError):
            UsageCapture(_SpyRaw(), UsageLog(), runtime_id="r", role="coder",
                         capabilities={"INPUT_TOKENS": "SUPPORTED"})


class InvocationIdentityTests(unittest.TestCase):
    def test_invocation_id_unique_opaque_per_call(self):
        raw = _SpyRaw(result=InvocationResult(status=InvocationStatus.SUCCESS))
        capture, log = make_capture(raw)
        capture.invoke(make_request())
        capture.invoke(make_request())
        records = log.snapshot()
        self.assertEqual(len(records), 2)
        ids = {record.invocation_id for record in records}
        self.assertEqual(len(ids), 2)
        for value in ids:
            self.assertRegex(value, r"^[0-9a-f]{32}$")  # opaque hex

    def test_invocation_id_not_from_request_or_inner_trace(self):
        raw = _SpyRaw(result=InvocationResult(
            status=InvocationStatus.SUCCESS, trace=make_trace()))
        capture, log = make_capture(raw)
        capture.invoke(make_request())
        record = log.snapshot()[0]
        self.assertNotEqual(record.invocation_id, "inner-trace-id")
        self.assertNotIn(record.invocation_id,
                         ("task-1", "agent-1", "coder", "runtime-x"))


class RawDelegationTests(unittest.TestCase):
    def test_raw_receives_request_unchanged(self):
        raw = _SpyRaw(result=InvocationResult(status=InvocationStatus.SUCCESS))
        capture, _ = make_capture(raw)
        request = make_request()
        capture.invoke(request)
        self.assertEqual(raw.calls, [request])
        self.assertIs(raw.calls[0], request)

    def test_raw_return_returned_unchanged(self):
        sentinel = InvocationResult(status=InvocationStatus.SUCCESS)
        capture, _ = make_capture(_SpyRaw(result=sentinel))
        self.assertIs(capture.invoke(make_request()), sentinel)

    def test_raw_exception_propagates_identically(self):
        sentinel = ValueError("raw adapter exploded")
        capture, log = make_capture(_SpyRaw(raises=sentinel))
        with self.assertRaises(ValueError) as caught:
            capture.invoke(make_request())
        self.assertIs(caught.exception, sentinel)
        self.assertEqual(log.snapshot(), ())

    def test_control_aborted_propagates_identically(self):
        sentinel = ControlAborted("exec-1")
        capture, log = make_capture(_SpyRaw(raises=sentinel))
        with self.assertRaises(ControlAborted) as caught:
            capture.invoke(make_request())
        self.assertIs(caught.exception, sentinel)
        self.assertEqual(log.snapshot(), ())

    def test_failed_result_returned_as_is_and_recorded(self):
        # APPLIED 边界：delegation 已发生，failure 不撤销 handoff，
        # 执行语义不改写（FAILED 照样原样返回、照样观察）
        failed = InvocationResult(status=InvocationStatus.FAILED,
                                  error="boom")
        capture, log = make_capture(_SpyRaw(result=failed))
        self.assertIs(capture.invoke(make_request()), failed)
        record = log.snapshot()[0]
        self.assertEqual(record.status, "FAILED")

    def test_timeout_result_handoff_holds(self):
        timed_out = InvocationResult(status=InvocationStatus.TIMEOUT,
                                     trace=make_trace(
                                         status=InvocationStatus.TIMEOUT))
        capture, log = make_capture(_SpyRaw(result=timed_out))
        self.assertIs(capture.invoke(make_request()), timed_out)
        record = log.snapshot()[0]
        self.assertEqual(record.status, "TIMEOUT")
        self.assertEqual(record.usage_status, UsageObservation.UNKNOWN)

    def test_raised_invocation_delegated_but_no_record(self):
        # delegation 事实成立（raw 被调用），但无 adapter 报告的状态
        # 可记：record 缺席（absent ≠ UNKNOWN），异常原样传播
        raw = _SpyRaw(raises=RuntimeError("late crash"))
        capture, log = make_capture(raw)
        with self.assertRaises(RuntimeError):
            capture.invoke(make_request())
        self.assertEqual(len(raw.calls), 1)  # handoff 成立
        self.assertEqual(log.snapshot(), ())  # 诚实缺席


class UsageSemanticsTests(unittest.TestCase):
    def test_known_when_both_real_ints(self):
        raw = _SpyRaw(result=InvocationResult(
            status=InvocationStatus.SUCCESS,
            trace=make_trace(input_tokens=120, output_tokens=80)))
        capture, log = make_capture(raw)
        capture.invoke(make_request())
        record = log.snapshot()[0]
        self.assertEqual(record.usage_status, UsageObservation.KNOWN)
        self.assertEqual(record.input_tokens, 120)
        self.assertEqual(record.output_tokens, 80)

    def test_unknown_when_trace_missing(self):
        capture, log = make_capture(_SpyRaw(result=InvocationResult(
            status=InvocationStatus.SUCCESS)))
        capture.invoke(make_request())
        record = log.snapshot()[0]
        self.assertEqual(record.usage_status, UsageObservation.UNKNOWN)
        self.assertIsNone(record.input_tokens)
        self.assertIsNone(record.output_tokens)

    def test_unknown_literals_stay_unknown_never_zero(self):
        raw = _SpyRaw(result=InvocationResult(
            status=InvocationStatus.SUCCESS,
            trace=make_trace(input_tokens="unknown",
                             output_tokens="unknown")))
        capture, log = make_capture(raw)
        capture.invoke(make_request())
        record = log.snapshot()[0]
        self.assertEqual(record.usage_status, UsageObservation.UNKNOWN)
        self.assertNotEqual(record.input_tokens, 0)
        self.assertNotEqual(record.output_tokens, 0)
        self.assertIsNone(record.input_tokens)
        self.assertIsNone(record.output_tokens)

    def test_partial_ints_are_unknown_both_none(self):
        raw = _SpyRaw(result=InvocationResult(
            status=InvocationStatus.SUCCESS,
            trace=make_trace(input_tokens=120, output_tokens="unknown")))
        capture, log = make_capture(raw)
        capture.invoke(make_request())
        record = log.snapshot()[0]
        self.assertEqual(record.usage_status, UsageObservation.UNKNOWN)
        self.assertIsNone(record.input_tokens)  # 半知不得半记
        self.assertIsNone(record.output_tokens)

    def test_unsupported_when_declared_and_no_facts(self):
        raw = _SpyRaw(result=InvocationResult(
            status=InvocationStatus.SUCCESS,
            trace=make_trace(input_tokens="unknown",
                             output_tokens="unknown")))
        capabilities = ObservationCapabilities(by_kind={
            ObservationKind.INPUT_TOKENS:
                ObservationCapabilityState.UNSUPPORTED,
            ObservationKind.OUTPUT_TOKENS:
                ObservationCapabilityState.UNSUPPORTED,
        })
        capture, log = make_capture(raw, capabilities=capabilities)
        capture.invoke(make_request())
        record = log.snapshot()[0]
        self.assertEqual(record.usage_status, UsageObservation.UNSUPPORTED)
        self.assertIsNone(record.input_tokens)

    def test_obtained_facts_known_despite_unsupported_declaration(self):
        # OBS-3 语义：UNSUPPORTED 不阻止已获事实记为 KNOWN
        raw = _SpyRaw(result=InvocationResult(
            status=InvocationStatus.SUCCESS,
            trace=make_trace(input_tokens=120, output_tokens=80)))
        capabilities = ObservationCapabilities(by_kind={
            ObservationKind.INPUT_TOKENS:
                ObservationCapabilityState.UNSUPPORTED,
            ObservationKind.OUTPUT_TOKENS:
                ObservationCapabilityState.UNSUPPORTED,
        })
        capture, log = make_capture(raw, capabilities=capabilities)
        capture.invoke(make_request())
        record = log.snapshot()[0]
        self.assertEqual(record.usage_status, UsageObservation.KNOWN)
        self.assertEqual(record.input_tokens, 120)

    def test_record_context_fields(self):
        raw = _SpyRaw(result=InvocationResult(status=InvocationStatus.SUCCESS))
        capture, log = make_capture(raw, runtime_id="runtime-9",
                                    role="reviewer")
        capture.invoke(make_request(task_id="task-9", agent_id="agent-9"))
        record = log.snapshot()[0]
        self.assertEqual(record.task_id, "task-9")
        self.assertEqual(record.agent_id, "agent-9")
        self.assertEqual(record.role, "reviewer")
        self.assertEqual(record.runtime_id, "runtime-9")
        self.assertEqual(record.status, "SUCCESS")
        self.assertIsInstance(record.duration_ms, int)

    def test_records_arrive_in_invocation_order(self):
        raw = _SpyRaw(result=InvocationResult(status=InvocationStatus.SUCCESS))
        capture, log = make_capture(raw)
        capture.invoke(make_request(task_id="task-a"))
        capture.invoke(make_request(task_id="task-b"))
        self.assertEqual([record.task_id for record in log.snapshot()],
                         ["task-a", "task-b"])


class UsageLogIsolationTests(unittest.TestCase):
    def test_append_failure_does_not_change_outcome(self):
        sentinel = InvocationResult(status=InvocationStatus.SUCCESS)
        raw = _SpyRaw(result=sentinel)

        class _FailingLog:
            def append(self, record):
                raise RuntimeError("observation store down")

        capture = UsageCapture(raw, _FailingLog(), runtime_id="r",
                               role="coder")
        self.assertIs(capture.invoke(make_request()), sentinel)  # 语义不变


class RevisionCorrelationTests(unittest.TestCase):
    def test_applied_revisions_recorded_verbatim(self):
        raw = _SpyRaw(result=InvocationResult(status=InvocationStatus.SUCCESS))
        capture, log = make_capture(raw)
        capture.invoke(make_request(), applied_revisions=["rev-1", "rev-2"])
        record = log.snapshot()[0]
        self.assertEqual(record.applied_revisions, ("rev-1", "rev-2"))
        # correlation 元数据绝不进 raw：request 原样单参转发
        self.assertEqual(len(raw.calls), 1)

    def test_default_applied_revisions_empty(self):
        capture, log = make_capture(_SpyRaw(result=InvocationResult(
            status=InvocationStatus.SUCCESS)))
        capture.invoke(make_request())
        self.assertEqual(log.snapshot()[0].applied_revisions, ())


class ExecutionIsolationTests(unittest.TestCase):
    def test_execution_isolation_between_instances(self):
        log_a, log_b = UsageLog(), UsageLog()
        capture_a, _ = make_capture(_SpyRaw(result=InvocationResult(
            status=InvocationStatus.SUCCESS)),
            runtime_id="runtime-a", role="coder", usage_log=log_a)
        capture_b, _ = make_capture(_SpyRaw(result=InvocationResult(
            status=InvocationStatus.SUCCESS)),
            runtime_id="runtime-b", role="reviewer", usage_log=log_b)
        capture_a.invoke(make_request(task_id="task-a"))
        capture_b.invoke(make_request(task_id="task-b"))
        self.assertEqual([r.runtime_id for r in log_a.snapshot()],
                         ["runtime-a"])
        self.assertEqual([r.runtime_id for r in log_b.snapshot()],
                         ["runtime-b"])


class ConcurrencyTests(unittest.TestCase):
    def test_concurrent_invocations_distinct_records(self):
        raw = _SpyRaw(result=InvocationResult(status=InvocationStatus.SUCCESS))
        capture, log = make_capture(raw)
        count = 8
        barrier = threading.Barrier(count)

        def worker():
            barrier.wait()
            capture.invoke(make_request())

        threads = [threading.Thread(target=worker) for _ in range(count)]
        for thread in threads:
            thread.start()
            self.addCleanup(thread.join, 3.0)
        for thread in threads:
            thread.join()
        records = log.snapshot()
        self.assertEqual(len(records), count)
        self.assertEqual(len(raw.calls), count)
        self.assertEqual(len({r.invocation_id for r in records}), count)


class ArchitectureTests(unittest.TestCase):
    """§13 import 守卫 + §9/10/11 纯度扫描。"""

    def test_import_roots_locked(self):
        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        roots = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                roots.add(node.module.split(".")[0])
        self.assertEqual(roots, {"__future__", "time", "uuid",
                                 "observation_capability", "usage_log"})

    def test_no_forbidden_domains(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        for token in ("control_boundary", "control_gate", "control_journal",
                      "event_index", "trace_projector", "cockpit",
                      "external_runtime", "subprocess", "Popen", "kill",
                      "terminate", "REVISION_APPLIED", "_queue", "_replay"):
            self.assertNotIn(token, source, token)

    def test_no_execution_event_vocabulary(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        for token in ("ExecutionEvent", "INVOCATION_STARTED",
                      "INVOCATION_FINISHED", "execution_observation"):
            self.assertNotIn(token, source, token)

    def test_no_provider_branching(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        for token in ("claude", "gemini", "codex", "qwen", "opencode",
                      "cline", "deepseek"):
            self.assertNotIn(token, source, token)


if __name__ == "__main__":
    unittest.main()
