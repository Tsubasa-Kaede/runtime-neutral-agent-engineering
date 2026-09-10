"""V3.2 UsageCapture：适配层包装栈的 usage 观察与 raw handoff 缝
（CU-OBS-4b）。

栈位（组合根未来接线）：

    AbortGate → RevisionAdapter(未来) → UsageCapture → raw adapter

本层只做三件事：

1. raw handoff——request 原样转发给被包装方的 invoke、返回值原样
   返回、异常（含 ControlAborted）原样传播；绝不 catch / 替换 /
   包装执行语义。APPLIED = raw invoke 已实际发生：delegation 一旦
   成立，即使随后 timeout / failure / exception，handoff 不撤销。
2. usage 观察——在真实 invocation 边界铸造 opaque+unique 的
   invocation_id（本层职责，不经手其他任何层），测量时长，按
   被包装方报告的事实记一条 UsageRecord 到既有 UsageLog。
3. handoff 观察（CU-REV-2）——把 raw invoke callable 的解引用放在
   try 之外、try 块内唯一语句是委托调用表达式；调用表达式一旦在
   当前线程被发起（return / raise / 中断全覆盖的 finally 语义），
   把 (invocation_id, applied_revisions) 原样通知可选 handoff
   观察者。认识论等级：只证明调用表达式被发起，不声称被包装方
   方法体一定进入、不声称 runtime 进程启动。仅携带修订的委托才
   通知（空 revisions 不触发）；观察者是未来 wrap 层的挂点——
   本层不落账、不产生应用事实、不新增身份。

usage 三态（结构性耦合，零估算）：
- KNOWN：trace 双方均报告真实整数计数；
- UNSUPPORTED：能力声明 UNSUPPORTED 且本次无已获事实（OBS-3
  裁决：UNSUPPORTED 不阻止已获事实记为 KNOWN——事实优先）；
- UNKNOWN：其余一切（trace 缺席 / "unknown" 字面量 / 半知）。
  unknown 绝不改写为 0；absent record ≠ UNKNOWN——被包装方抛出
  异常时无状态事实可记，诚实缺席（delegation 事实仍成立）。

观察隔离：UsageLog 追加失败被吞没，绝不改变 raw invocation
outcome。applied_revisions 仅作 correlation 引用落 record，绝不
转发给被包装方、绝不产生应用事实。运行时中立：零 provider 分支，
被包装方的私有解析留在其内部；本层对协议形状做结构化访问，
不 import 运行时协议模块。
"""
from __future__ import annotations

from time import monotonic
from uuid import uuid4

from observation_capability import (
    ObservationCapabilities,
    ObservationCapabilityState,
    ObservationKind,
)
from usage_log import UsageLog, UsageObservation, UsageRecord

__all__ = ("UsageCapture", "UsageCaptureError")


class UsageCaptureError(ValueError):
    """构造期拒绝（非 callable 被包装方/账本、非法上下文标识）。"""


def _token_count(value):
    """真实整数计数或 None（"unknown" 字面量/其他形状一律 None）。"""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _recordable_text(value):
    """可入账的非空 str（str-enum 取其 value；其余 None）。"""
    if isinstance(value, str) and value.strip():
        return getattr(value, "value", value)
    return None


class UsageCapture:
    """usage 观察包装：铸 invocation 边界身份 → 交予 raw → 记观察。"""

    def __init__(self, raw_adapter, usage_log, *, runtime_id: str,
                 role: str,
                 capabilities: ObservationCapabilities | None = None,
                 handoff_observer=None):
        if not callable(getattr(raw_adapter, "invoke", None)):
            raise UsageCaptureError(
                "raw_adapter must expose a callable invoke")
        if not callable(getattr(usage_log, "append", None)):
            raise UsageCaptureError(
                "usage_log must expose a callable append")
        for name, value in (("runtime_id", runtime_id), ("role", role)):
            if not _recordable_text(value):
                raise UsageCaptureError(
                    f"{name} must be a non-empty string")
        if handoff_observer is not None and not callable(handoff_observer):
            raise UsageCaptureError("handoff_observer must be callable")
        if capabilities is None:
            capabilities = ObservationCapabilities(by_kind={})
        if not isinstance(capabilities, ObservationCapabilities):
            raise UsageCaptureError(
                "capabilities must be an ObservationCapabilities value")
        self._raw = raw_adapter
        self._usage_log = usage_log
        self._runtime_id = runtime_id
        self._role = role
        self._capabilities = capabilities
        self._handoff_observer = handoff_observer

    def invoke(self, request, *, applied_revisions=()):
        """真实 invocation 边界：铸 id → 交予 raw → 按事实记观察。

        raw 参数原样传递（correlation 元数据绝不转发）、返回值原样
        返回、异常原样传播（无观察可记，诚实缺席）。委托调用表达式
        发起后经 finally 通知 handoff 观察者（若有、且携带修订）——
        观察者异常被隔离，绝不改变本方法的 outcome。
        """
        invocation_id = uuid4().hex  # opaque + unique，仅本层铸造
        started_at = monotonic()
        raw_invoke = self._raw.invoke  # 解引用在 try 外：解析失败 ⇒ 不触发
        try:
            result = raw_invoke(request)
        finally:
            self._notify_handoff(invocation_id, applied_revisions)
        duration_ms = int((monotonic() - started_at) * 1000)
        self._record(request, invocation_id, result, duration_ms,
                     applied_revisions)
        return result

    def _notify_handoff(self, invocation_id, applied_revisions):
        """委托调用表达式已发起后的 handoff 观察（仅携带修订的委托）。

        两参原样：invocation_id 原值、applied_revisions 原对象
        （不排序 / 不去重 / 不转换 / 不复制）。观察者异常 Exception
        级隔离——绝不改变执行 outcome。
        """
        if self._handoff_observer is None or not applied_revisions:
            return
        try:
            self._handoff_observer(invocation_id, applied_revisions)
        except Exception:
            pass  # 观察失败绝不改变执行 outcome（Exception 级隔离）

    def _record(self, request, invocation_id, result, duration_ms,
                applied_revisions):
        """按被包装方报告的事实落一条 UsageRecord（缺席即诚实）。"""
        status = _recordable_text(getattr(result, "status", None))
        task_id = _recordable_text(getattr(request, "task_id", None))
        agent_id = _recordable_text(getattr(request, "agent_id", None))
        if status is None or task_id is None or agent_id is None:
            return  # 无诚实事实可记：record 缺席（≠ UNKNOWN）
        trace = getattr(result, "trace", None)
        input_tokens = _token_count(getattr(trace, "input_tokens", None))
        output_tokens = _token_count(getattr(trace, "output_tokens", None))
        if input_tokens is not None and output_tokens is not None:
            usage_status = UsageObservation.KNOWN
        elif (self._capabilities.state(ObservationKind.INPUT_TOKENS)
                is ObservationCapabilityState.UNSUPPORTED
                or self._capabilities.state(ObservationKind.OUTPUT_TOKENS)
                is ObservationCapabilityState.UNSUPPORTED):
            usage_status = UsageObservation.UNSUPPORTED
            input_tokens = None
            output_tokens = None
        else:
            usage_status = UsageObservation.UNKNOWN
            input_tokens = None
            output_tokens = None
        record = UsageRecord(
            invocation_id=invocation_id,
            task_id=task_id,
            agent_id=agent_id,
            role=self._role,
            runtime_id=self._runtime_id,
            status=status,
            usage_status=usage_status,
            duration_ms=duration_ms,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            applied_revisions=applied_revisions)
        try:
            self._usage_log.append(record)
        except Exception:
            pass  # 观察失败绝不改变执行 outcome（§7 隔离）
