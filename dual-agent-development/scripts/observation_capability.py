"""V3.2 CU-OBS-3: Adapter 观察能力声明契约（declaration，非事实）。

Sections 1-6 冻结架构中 capability 层的最小契约：

    Adapter（能力声明的唯一来源）
        ↓ 携带 ObservationCapabilities
    允许 / 不允许 / 未知能否期待某类观察
        ↓
    后续 wrapper（实际捕获）→ 观察 store（事实）

边界（本 CU 只立声明契约，不接线、不捕获）：
- 声明 ≠ 事实：SUPPORTED 只表示 adapter 声明具备该观察能力，绝不
  表示任何一次调用一定获得数据，也绝不生成任何用量记录或数字
  （不支持把缺席伪造成 0）；UNSUPPORTED 是能力缺席，不是调用失败；
  UNKNOWN 是无法可靠判断，永不升级为 SUPPORTED。
- 未声明的观察类型一律按 UNKNOWN 解释 —— 绝不默认 SUPPORTED。
- 能力知识只属于 adapter：本模块零 runtime 知识、零按 runtime 身份
  的分支；不同 adapter 的差异只来自各自携带的声明。
- 声明不可变：frozen + 只读映射；无任何增删面。
- 依赖仅标准库；不依赖控制域、事件索引、用量记录、投影器或任何
  runtime adapter 模块；context 百分比等派生指标明确 deferred，
  不入词表。
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType

__all__ = (
    "ObservationCapabilities", "ObservationCapabilityError",
    "ObservationCapabilityState", "ObservationKind",
)


class ObservationCapabilityError(ValueError):
    """观察能力声明的构造期/查询期拒绝（词表外值 / 非法形状）。"""


class ObservationKind(str, Enum):
    """封闭的观察能力词表（恰 4 值，Sections 4-6 冻结）。

    不含 TOTAL_TOKENS / CONTEXT_PERCENTAGE / COST / LATENCY ——
    其中 context 百分比明确 deferred，其余未入冻结契约。
    """

    INPUT_TOKENS = "INPUT_TOKENS"
    OUTPUT_TOKENS = "OUTPUT_TOKENS"
    CONTEXT_USED = "CONTEXT_USED"
    CONTEXT_LIMIT = "CONTEXT_LIMIT"


class ObservationCapabilityState(str, Enum):
    """能力三态（声明层；与用量记录的事实三态是不同词汇域）。

    SUPPORTED —— 声明具备能力（不代表任一次调用必得数据）；
    UNSUPPORTED —— 明确不能提供（缺席不可伪造为数字）；
    UNKNOWN —— 无法可靠判断（不可升级为 SUPPORTED）。
    """

    SUPPORTED = "SUPPORTED"
    UNSUPPORTED = "UNSUPPORTED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class ObservationCapabilities:
    """adapter 供应的不可变观察能力声明。

    构造自「观察类型 → 三态」映射（防御性拷贝为只读视图）；
    未声明的类型在 state() 查询下按 UNKNOWN 解释。
    """

    by_kind: Mapping

    def __post_init__(self) -> None:
        if not isinstance(self.by_kind, Mapping):
            raise ObservationCapabilityError(
                "capabilities must be a mapping of kind to state")
        for kind, state in self.by_kind.items():
            if not isinstance(kind, ObservationKind):
                raise ObservationCapabilityError(
                    "capability key must be an ObservationKind member")
            if not isinstance(state, ObservationCapabilityState):
                raise ObservationCapabilityError(
                    "capability value must be an "
                    "ObservationCapabilityState member")
        object.__setattr__(
            self, "by_kind", MappingProxyType(dict(self.by_kind)))

    def state(self, kind: ObservationKind) -> ObservationCapabilityState:
        """该观察类型的声明态；未声明一律 UNKNOWN（绝不默认 SUPPORTED）。"""
        if not isinstance(kind, ObservationKind):
            raise ObservationCapabilityError(
                "kind must be an ObservationKind member")
        return self.by_kind.get(kind, ObservationCapabilityState.UNKNOWN)
