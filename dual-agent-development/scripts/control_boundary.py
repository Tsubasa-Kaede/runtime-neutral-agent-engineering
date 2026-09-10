"""V3.2 CU-CTRL-1: Control Domain 的最小不可变值模型（contract-only）。

Sections 1-6 冻结架构的 control-plane 数据契约：封闭词表（command /
reason / revision-target / lifecycle / park-point / pending-intent）与
值对象（ControlCommand / ControlResult / ControlSnapshot /
PendingIntent / PendingRevision / RevisionPayload）。

边界（本 CU 只立契约，不做行为）：
- 只做结构校验：非空标识、词表成员、类型、组合约束。裁决、版本递增、
  safety policy、journal、replay 缓存、admission 决策属于后续 CU
  （ControlJournal / ControlBoundary / ControlGate）。
- command_id 由 caller 供应（无默认值 = 结构性禁止隐式生成）；
  replay identity = (execution_id, command_id)，replay 行为后续 CU 实现。
- runtime-neutral：import 仅标准库（dataclasses/enum），零 runtime 名、
  零执行引擎类型、零 UI 框架引用 —— 命令域与执行观察域词汇分离。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

__all__ = (
    "ControlCommand", "ControlCommandType", "ControlLifecycle",
    "ControlModelError", "ControlReason", "ControlResult",
    "ControlSnapshot", "ControlStatus", "ParkPoint", "PendingIntent",
    "PendingIntentKind", "PendingRevision", "RevisionPayload",
    "RevisionTarget",
)


class ControlModelError(ValueError):
    """控制域值模型的构造期拒绝（词表外值 / 非法形状 / 非法组合）。"""


class ControlCommandType(str, Enum):
    """冻结命令词表（Sections 1-2）。UI 键（P/C/E/A）只是呈现映射，
    不是域命令 —— 例如 E 映射为 REVISE，而 "EDIT" 不是命令。"""

    PAUSE = "PAUSE"
    RESUME = "RESUME"
    REVISE = "REVISE"
    ABORT = "ABORT"


class ControlStatus(str, Enum):
    """命令结果三态（Section 2 冻结：不引入 DEFERRED/FAILED）。"""

    ACCEPTED = "ACCEPTED"
    NO_OP = "NO_OP"
    REJECTED = "REJECTED"


class ControlReason(str, Enum):
    """Sections 1-6 已裁决的封闭 reason 词表（本 CU 只立契约，
    各 reason 的裁决语义属于 ControlBoundary 后续 CU）。"""

    USER_PAUSED = "USER_PAUSED"
    ALREADY_REQUESTED = "ALREADY_REQUESTED"
    ALREADY_PAUSED = "ALREADY_PAUSED"
    ALREADY_ABORTING = "ALREADY_ABORTING"
    ALREADY_TERMINAL = "ALREADY_TERMINAL"
    NOT_PAUSED = "NOT_PAUSED"
    STALE_VERSION = "STALE_VERSION"
    INVALID_STATE = "INVALID_STATE"
    INVALID_TARGET = "INVALID_TARGET"
    COMMAND_ID_CONFLICT = "COMMAND_ID_CONFLICT"
    LENGTH_EXCEEDED = "LENGTH_EXCEEDED"
    REVISION_BUDGET_EXCEEDED = "REVISION_BUDGET_EXCEEDED"
    UNSAFE_CONTENT = "UNSAFE_CONTENT"


class RevisionTarget(str, Enum):
    """Section 3 冻结的两个 revision target。"""

    SUBMISSION = "SUBMISSION"
    NEXT_INVOCATION = "NEXT_INVOCATION"


class ControlLifecycle(str, Enum):
    """Section 2 冻结的 7 值生命周期词表（本 CU 只定义词汇；
    转换/停驻/终态裁决属于 Boundary 后续 CU）。"""

    RUNNING = "RUNNING"
    PAUSE_PENDING = "PAUSE_PENDING"
    PAUSED = "PAUSED"
    ABORT_PENDING = "ABORT_PENDING"
    ABORTED = "ABORTED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class ParkPoint(str, Enum):
    """Section 2 冻结：只有 PAUSED 拥有有效 park point。"""

    NONE = "NONE"
    DISPATCH = "DISPATCH"
    ADMISSION = "ADMISSION"


class PendingIntentKind(str, Enum):
    """Section 2 冻结：RESUME / REVISE 不形成 pending intent ——
    由词表结构性排除（无对应成员）。"""

    NONE = "NONE"
    PAUSE = "PAUSE"
    ABORT = "ABORT"


def _require_non_empty_string(value, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ControlModelError(f"{field_name} must be a non-empty string")


def _require_version(value, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ControlModelError(f"{field_name} must be a non-negative int")


@dataclass(frozen=True)
class RevisionPayload:
    """REVISE 的载荷：SUBMISSION 携带 task/prompt 新值（set semantics）；
    NEXT_INVOCATION 携带 trailing overlay 文本。"""

    target: RevisionTarget
    text: str | None = None
    task: str | None = None
    prompt: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.target, RevisionTarget):
            raise ControlModelError("target must be a RevisionTarget")
        if self.target is RevisionTarget.SUBMISSION:
            if self.text is not None:
                raise ControlModelError(
                    "SUBMISSION revision does not carry text")
            provided = [value for value in (self.task, self.prompt)
                        if value is not None]
            if not provided:
                raise ControlModelError(
                    "SUBMISSION revision requires task or prompt")
            for value in provided:
                _require_non_empty_string(value, "submission field")
        else:
            if self.task is not None or self.prompt is not None:
                raise ControlModelError(
                    "NEXT_INVOCATION revision carries text only")
            _require_non_empty_string(self.text, "text")


@dataclass(frozen=True)
class ControlCommand:
    """一条控制命令（caller 构造并供应 command_id；Boundary 不生成）。

    REVISE 必须携带 payload 与 expected_version；其余命令两者皆无
    （结构层即拒绝越界组合；裁决语义属于 Boundary 后续 CU）。
    """

    command_id: str
    execution_id: str
    command: ControlCommandType
    payload: RevisionPayload | None = None
    expected_version: int | None = None

    def __post_init__(self) -> None:
        _require_non_empty_string(self.command_id, "command_id")
        _require_non_empty_string(self.execution_id, "execution_id")
        if not isinstance(self.command, ControlCommandType):
            raise ControlModelError(f"unknown command: {self.command!r}")
        if self.command is ControlCommandType.REVISE:
            if self.payload is None:
                raise ControlModelError("REVISE requires a payload")
            if self.expected_version is None:
                raise ControlModelError("REVISE requires expected_version")
            _require_version(self.expected_version, "expected_version")
        else:
            if self.payload is not None:
                raise ControlModelError(
                    f"{self.command.value} takes no payload")
            if self.expected_version is not None:
                raise ControlModelError(
                    f"{self.command.value} takes no expected_version")


@dataclass(frozen=True)
class PendingIntent:
    """单一 kind 的 pending intent 值（结构上不可能同时 pending 两个）。

    superseded_by 是纯值转换，编码 Section 2 的 intent 不变量：
    ABORT 可替换 pending PAUSE（abort precedence）；ABORT 一旦 pending，
    PAUSE 不可再形成；任何 intent 可被清除为 NONE。
    """

    kind: PendingIntentKind = PendingIntentKind.NONE

    def __post_init__(self) -> None:
        if not isinstance(self.kind, PendingIntentKind):
            raise ControlModelError(
                f"unknown pending intent: {self.kind!r}")

    def superseded_by(self, kind: PendingIntentKind) -> "PendingIntent":
        if not isinstance(kind, PendingIntentKind):
            raise ControlModelError(f"unknown pending intent: {kind!r}")
        if (kind is PendingIntentKind.PAUSE
                and self.kind is PendingIntentKind.ABORT):
            raise ControlModelError(
                "PAUSE cannot supersede a pending ABORT")
        return PendingIntent(kind)


@dataclass(frozen=True)
class ControlResult:
    """命令裁决结果（不可变值，可承载 exact replay —— 行为后续 CU）。

    ACCEPTED 不携带 reason；NO_OP / REJECTED 必须携带封闭词表 reason。
    execution_version 为该命令裁决后的版本（ACCEPTED 已 +1，
    NO_OP/REJECTED 保持原值 —— 契约表达；递增行为属于 Boundary）。
    """

    command_id: str
    execution_id: str
    status: ControlStatus
    execution_version: int
    reason: ControlReason | None = None

    def __post_init__(self) -> None:
        _require_non_empty_string(self.command_id, "command_id")
        _require_non_empty_string(self.execution_id, "execution_id")
        if not isinstance(self.status, ControlStatus):
            raise ControlModelError(f"unknown status: {self.status!r}")
        _require_version(self.execution_version, "execution_version")
        if self.status is ControlStatus.ACCEPTED:
            if self.reason is not None:
                raise ControlModelError("ACCEPTED carries no reason")
        elif not isinstance(self.reason, ControlReason):
            raise ControlModelError(f"{self.status.value} requires a reason")


@dataclass(frozen=True)
class PendingRevision:
    """暂停窗内已接受、尚未消费的 revision 队列条目。

    队列只承载 NEXT_INVOCATION（Section 3：SUBMISSION 是 set semantics
    修改 draft，从不入队 —— 构造期结构性拒绝）。
    """

    revision_id: str
    text: str
    target: RevisionTarget = RevisionTarget.NEXT_INVOCATION

    def __post_init__(self) -> None:
        _require_non_empty_string(self.revision_id, "revision_id")
        _require_non_empty_string(self.text, "text")
        if self.target is not RevisionTarget.NEXT_INVOCATION:
            raise ControlModelError(
                "revision queue entries are NEXT_INVOCATION only")


@dataclass(frozen=True)
class ControlSnapshot:
    """control-plane read model（Boundary 投影产物；本 CU 只立形状）。

    模型约束（Sections 2-3）：只有 PAUSED 拥有有效 park point；
    revision_queue 是 pending 条目元组（applied 历史属于 journal，
    绝不进入 queue）。
    """

    execution_id: str
    execution_version: int
    lifecycle: ControlLifecycle
    pending_intent: PendingIntent = field(default_factory=PendingIntent)
    park_point: ParkPoint = ParkPoint.NONE
    revision_queue: tuple = ()

    def __post_init__(self) -> None:
        _require_non_empty_string(self.execution_id, "execution_id")
        _require_version(self.execution_version, "execution_version")
        if not isinstance(self.lifecycle, ControlLifecycle):
            raise ControlModelError(
                f"unknown lifecycle: {self.lifecycle!r}")
        if not isinstance(self.pending_intent, PendingIntent):
            raise ControlModelError("pending_intent must be a PendingIntent")
        if not isinstance(self.park_point, ParkPoint):
            raise ControlModelError(
                f"unknown park point: {self.park_point!r}")
        if self.lifecycle is ControlLifecycle.PAUSED:
            if self.park_point not in (ParkPoint.DISPATCH,
                                       ParkPoint.ADMISSION):
                raise ControlModelError(
                    "PAUSED requires a DISPATCH or ADMISSION park point")
        elif self.park_point is not ParkPoint.NONE:
            raise ControlModelError("park point is valid only when PAUSED")
        if not isinstance(self.revision_queue, tuple):
            raise ControlModelError("revision_queue must be a tuple")
        for entry in self.revision_queue:
            if not isinstance(entry, PendingRevision):
                raise ControlModelError(
                    "revision_queue entries must be PendingRevision values")
