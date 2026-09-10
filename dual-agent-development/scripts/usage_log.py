"""V3.2 CU-OBS-2: append-only per-invocation usage 观察 store（UsageLog）。

Sections 1-6 冻结架构中用量域的 in-memory store：

    InvocationTrace（上游 runtime 观察）
        ↓ 由后续 wrapper CU 映射
    UsageLog（本模块：只记录已获得的 usage observation）
        ↓
    Trace / Budget projection（后续 CU）

边界（本 CU 只立 store，不做投影/推断）：
- 只记录已经获得的 usage observation：不推断 invocation 是否存在、
  不推断成败、不聚合同 task 的多次 invocation、不去重同 id 的重复
  append —— 一切裁决属于消费者投影层。
- 三态 usage 语义（KNOWN / UNKNOWN / UNSUPPORTED）由 record 结构性
  耦合承载：KNOWN ⇒ 双 token 在场；UNKNOWN / UNSUPPORTED ⇒ 双 token
  缺席。零编造：不猜数字、UNKNOWN 保持 UNKNOWN、UNSUPPORTED 保持
  UNSUPPORTED。
- record absent ≠ record exists + usage UNKNOWN：store 绝不代缺席的
  invocation 编造记录（含 UNKNOWN 记录）；缺席只能在消费者侧由
  「无记录」表达。
- applied_revisions 只是 usage observation 里的 correlation 引用，
  不是 revision 真值 —— revision 真值永远属于控制域事实账本，
  本模块零依赖控制域。
- 依赖仅标准库；不定义第二套执行事件 schema。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from threading import Lock

__all__ = (
    "UsageLog", "UsageLogError", "UsageObservation", "UsageRecord",
)


class UsageLogError(ValueError):
    """usage record 构造期拒绝（词表外值 / 非法形状 / 非法耦合）。"""


class UsageObservation(str, Enum):
    """usage 观察三态（Sections 4-5 冻结）。

    KNOWN —— runtime 实报了 token 数字；UNKNOWN —— invocation 已观察
    但 usage 数字未获提供；UNSUPPORTED —— runtime 明确不提供 usage。
    """

    KNOWN = "KNOWN"
    UNKNOWN = "UNKNOWN"
    UNSUPPORTED = "UNSUPPORTED"


def _require_id(value, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise UsageLogError(f"{field_name} must be a non-empty string")


def _require_count(value, field_name: str) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise UsageLogError(
            f"{field_name} must be a non-negative integer or None")


def _require_position(value) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise UsageLogError("cursor must be a non-negative integer")


@dataclass(frozen=True)
class UsageRecord:
    """一次 invocation 的 usage observation（canonical 字段集）。

    invocation_id 由 caller（wrapper 层）供应、原样保存 —— store 不
    生成 identity。token 字段与 usage_status 结构性耦合（见模块
    docstring）；applied_revisions 消费 caller 供应的任意可迭代引用集，
    固化为不可变 tuple。
    """

    invocation_id: str
    task_id: str
    agent_id: str
    role: str
    runtime_id: str
    status: str
    usage_status: UsageObservation
    duration_ms: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    applied_revisions: tuple = ()

    def __post_init__(self) -> None:
        if not isinstance(self.usage_status, UsageObservation):
            raise UsageLogError(
                f"unknown usage observation: {self.usage_status!r}")
        for field_name in ("invocation_id", "task_id", "agent_id", "role",
                           "runtime_id", "status"):
            _require_id(getattr(self, field_name), field_name)
        _require_count(self.duration_ms, "duration_ms")
        _require_count(self.input_tokens, "input_tokens")
        _require_count(self.output_tokens, "output_tokens")
        known = self.usage_status is UsageObservation.KNOWN
        if known:
            if self.input_tokens is None or self.output_tokens is None:
                raise UsageLogError(
                    "KNOWN usage requires both token counts")
        elif self.input_tokens is not None or self.output_tokens is not None:
            raise UsageLogError(
                f"{self.usage_status.value} usage carries no token counts")
        if isinstance(self.applied_revisions, str):
            raise UsageLogError(
                "applied_revisions must be an iterable of revision ids")
        try:
            items = tuple(self.applied_revisions)
        except TypeError:
            raise UsageLogError(
                "applied_revisions must be an iterable of revision ids"
            ) from None
        for item in items:
            _require_id(item, "applied_revisions entry")
        object.__setattr__(self, "applied_revisions", items)


class UsageLog:
    """append-only per-invocation usage 观察 store（in-memory、线程安全）。

    顺序即到达顺序；since(cursor) 的 cursor 是 log-local 0-based 位置
    （返回位置严格大于 cursor 的记录，与控制域账本的 cursor 读取契约
    同形，但两者是不同域的序列概念，互不共用）。
    """

    def __init__(self) -> None:
        self._records: list[UsageRecord] = []
        self._lock = Lock()

    def append(self, record: UsageRecord) -> None:
        """按到达顺序追加一条已获得的 usage observation（原样保存）。"""
        if not isinstance(record, UsageRecord):
            raise UsageLogError("append expects a UsageRecord")
        with self._lock:
            self._records.append(record)

    def snapshot(self) -> tuple:
        """全部记录（到达顺序；detached immutable view）。"""
        with self._lock:
            return tuple(self._records)

    def since(self, position: int) -> tuple:
        """位置严格大于 cursor 的全部记录（cursor 增量读取）。"""
        _require_position(position)
        with self._lock:
            return tuple(self._records[position + 1:])
