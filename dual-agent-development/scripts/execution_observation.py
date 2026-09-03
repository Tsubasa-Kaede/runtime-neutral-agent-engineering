"""R7-D1: execution observation contract — runtime-neutral, observation-only.

独立于编排执行流的旁路观察契约（本轮只定义契约，不接线任何生产调用链）：

    ExecutionEvent          # 一次性生命周期通知（frozen、deterministic、secret-safe）
    ExecutionEventType      # 封闭词表：恰好 7 词，构造期拒绝一切其余值
    ObservationSink         # 最小同步协议：on_event(event) -> None

边界（与 house 纪律同款，测试逐项锁定）：
- runtime-neutral：不 import runtime/adapter/pool/health；模块无 runtime 名、
  无环境、无子进程、无时钟、无随机、无 UUID —— sequence 由调用方提供，
  相同输入必然产生等价事件。
- observation-only：事件是通知，不是真值 —— 审计真值唯一存在于 ledger
  （SharedCollaborationState），事件绝不携带 packet payload、prompt、
  provider/model、credential 或任何原始错误文本。
- secret-safe：复用 content_safety 的既有 marker 词表与 credential-shape
  规则（单一 secret policy，不建第二套）；字段校验失败时错误信息只点名
  字段与规则，绝不回显被拒绝的值。
- 非 ledger / 非 packet：ExecutionEvent 不是 CollaborationRecord，不是
  CollaborationPacket，不是 InvocationResult，不是 UI state —— 字段集与
  词表都是观察契约自己的，不与任何持久结构共享。

依赖：content_safety（既有校验工具）与 dataclasses —— 无其它 import。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from content_safety import contains_unsafe_content

# 与仓库其余模块同一 marker 词表（单一 secret policy 的唯一来源）。
from content_safety import SECRET_MARKERS


class ObservationError(ValueError):
    """封闭的契约拒绝；message 只含字段名与规则，绝不含被拒值。"""


class ExecutionEventType(str, Enum):
    """封闭事件词表：恰好七个生命周期值，绝不静默扩展。

    语义（R7-D Boundary Design 已裁决）：
    - DECISION / TERMINAL       —— 编排决策与终态（对应既有封闭词表）
    - STAGE_STARTED / STAGE_FINISHED —— 四角色槽位边界
    - INVOCATION_STARTED / INVOCATION_FINISHED —— 单次 runtime 调用边界
    - HANDOFF                   —— 角色间结构化交接发生
    """

    DECISION = "DECISION"
    STAGE_STARTED = "STAGE_STARTED"
    INVOCATION_STARTED = "INVOCATION_STARTED"
    INVOCATION_FINISHED = "INVOCATION_FINISHED"
    STAGE_FINISHED = "STAGE_FINISHED"
    HANDOFF = "HANDOFF"
    TERMINAL = "TERMINAL"


def _assert_clean(value, field_name: str) -> None:
    """非空字符串 + 无 secret 内容；失败信息只点名字段。

    判定复用 content_safety 的既有规则（单一 secret policy 的唯一来源，
    不建第二套）：marker 词表小写子串匹配（与 collaboration_state /
    collaboration_packet / verified_runtime_pool 的 `_assert_clean` 同款，
    拒绝"提及"）∪ contains_unsafe_content credential 形状规则（拒绝
    "sk-..." 等裸形状）。
    """
    if not isinstance(value, str) or not value.strip():
        raise ObservationError(f"{field_name} must be a non-empty string")
    lowered = value.lower()
    for marker in SECRET_MARKERS:
        if marker in lowered:
            raise ObservationError(
                f"{field_name} must not contain secret-shaped content")
    if contains_unsafe_content(value):
        raise ObservationError(
            f"{field_name} must not contain secret-shaped content")


def _assert_non_negative_int(value, field_name: str, *, allow_none: bool) -> None:
    """合法非负整数；bool 一律拒绝（bool 是 int 的子类，须显式排除）。

    失败信息只含字段名与规则，绝不含值本身。
    """
    if value is None and allow_none:
        return
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ObservationError(f"{field_name} must be a non-negative integer")


@dataclass(frozen=True)
class ExecutionEvent:
    """一次 runtime-neutral 执行观察通知。

    必需字段（最小集，授权字段集之外的任何字段都不存在）：
      event_type / sequence / task_id / correlation_id / stage /
      runtime_id / status / reason
    可选字段：duration_ms（相对时长，绝不是时间戳）。
    明确不存在：timestamp、agent_address、execution_id、safe_summary、
    prompt、packet payload、provider、model、credentials、environment、
    raw exception —— 全部属于本轮与后续轮次的排除项。

    序号语义：sequence 由调用方提供（事件本身无计数器状态）；deterministic
    的责任在"同输入同事件"，不在全局唯一性。
    """

    event_type: ExecutionEventType
    sequence: int
    task_id: str
    correlation_id: str
    stage: str
    runtime_id: str
    status: str
    reason: str
    duration_ms: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.event_type, ExecutionEventType):
            # 不允许任意字符串绕过枚举；str-Enum 值也必须显式转换。
            # 捕获 ValueError + TypeError：非可哈希值（list/dict 等）在
            # 某些 Python 版本经枚举查找抛 TypeError，同样收敛到封闭错误。
            try:
                converted = ExecutionEventType(self.event_type)
            except (ValueError, TypeError):
                raise ObservationError(
                    "event_type must be an ExecutionEventType member") from None
            object.__setattr__(self, "event_type", converted)
        _assert_non_negative_int(self.sequence, "sequence", allow_none=False)
        for field_name in ("task_id", "correlation_id", "stage",
                           "runtime_id", "status", "reason"):
            _assert_clean(getattr(self, field_name), field_name)
        _assert_non_negative_int(self.duration_ms, "duration_ms",
                                 allow_none=True)

    def to_dict(self) -> dict:
        """最小 deterministic 投影：固定字段、固定顺序、零额外 metadata。

        键按字段定义顺序输出（Python 3.7+ dict 保序）；不生成 timestamp、
        不注入 runtime/provider/model、不携带任何值之外的信息。event_type
        序列化为词表字符串。序列化前事件已经过构造期校验，因此投影天然
        secret-safe；本方法绝不抛出新异常或二次读取环境。
        """
        return {
            "event_type": self.event_type.value,
            "sequence": self.sequence,
            "task_id": self.task_id,
            "correlation_id": self.correlation_id,
            "stage": self.stage,
            "runtime_id": self.runtime_id,
            "status": self.status,
            "reason": self.reason,
            "duration_ms": self.duration_ms,
        }


class ObservationSink(Protocol):
    """最小同步观察协议：恰好一个方法。

    契约（本轮只定义、不接线）：
    - on_event 在事件产生点被同步调用（无 async、无线程、无队列）。
    - 实现方不得从事件中要求 runtime/provider/UI 信息 —— 事件字段集就是
      全部输入；Agent A/B/C 之类的呈现映射是消费端自己的投影。
    - 实现方抛出的异常属于实现方自己的错误；编排侧的隔离规则（观察失败
      不影响执行）属于 R7-D2 的接线契约，本轮只声明：协议本身不规定、
      也不要求任何错误处理语义。
    - 协议不持有全局状态、不依赖 CLI/TUI/Web/adapter/ledger。
    """

    def on_event(self, event: ExecutionEvent) -> None: ...
