"""V3.2 Control Domain：值模型契约（CU-CTRL-1）+ intent authority
（CU-CTRL-3 ControlBoundary Core）。

Sections 1-6 冻结架构的 control-plane：封闭词表（command / reason /
revision-target / lifecycle / park-point / pending-intent）、值对象
（ControlCommand / ControlResult / ControlSnapshot / PendingIntent /
PendingRevision / RevisionPayload），以及自 CU-CTRL-3 起的
ControlBoundary —— intent authority + effect non-authority。

边界分层：
- 值模型只做结构校验（非空标识、词表成员、类型、组合约束）。
- ControlBoundary（CU-CTRL-3）：submit 做确定性 intent 裁决，经自持
  boundary writer 把 REQUESTED 事实写入账本，是唯一 control-intent
  写入入口。effect non-authority：不调用 runtime、不改变执行生命周期、
  不产生 CONFIRMED / APPLIED / SUPERSEDED 事实、不执行 pause/abort、
  不递增 execution_version（accepted-command epoch 语义属后续单元）、
  不做 revision 执行或 queue 管理。自 CU-CTRL-4 起，submit 以
  (execution_id, command_id) 为幂等 identity：exact replay 纯查找
  返回首次结果，同 id 不同 fingerprint → REJECTED/COMMAND_ID_CONFLICT，
  两者均零副作用。
- command_id 由 caller 供应（无默认值 = 结构性禁止隐式生成）；
  replay identity = (execution_id, command_id)，fingerprint 覆盖全部
  语义字段的不可变规格元组，缓存为 per-boundary 实例域（CU-CTRL-4）。
- runtime-neutral：依赖仅标准库 + 账本（control_boundary →
  control_journal 是 V3.2 唯一获准的组合方向），零 runtime 名、
  零执行引擎类型、零 UI 框架引用 —— 命令域与执行观察域词汇分离。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from threading import Lock

from control_journal import ControlFactType, ControlJournal

__all__ = (
    "ControlBoundary", "ControlCommand", "ControlCommandType",
    "ControlLifecycle", "ControlModelError", "ControlReason",
    "ControlResult", "ControlSnapshot", "ControlStatus", "ParkPoint",
    "PendingIntent", "PendingIntentKind", "PendingRevision",
    "RevisionPayload", "RevisionTarget",
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


_COMMAND_FACT = {
    ControlCommandType.PAUSE: ControlFactType.PAUSE_REQUESTED,
    ControlCommandType.RESUME: ControlFactType.RESUME_REQUESTED,
    ControlCommandType.REVISE: ControlFactType.REVISE_REQUESTED,
    ControlCommandType.ABORT: ControlFactType.ABORT_REQUESTED,
}


def _command_fingerprint(command: ControlCommand) -> tuple:
    """命令语义身份的不可变规格元组（CU-CTRL-4 replay 契约）。

    覆盖 ControlCommand 全部语义字段：execution_id、command_id、
    命令类型、revision 载荷（target/text/task/prompt）、
    expected_version。刻意不使用哈希内建、不序列化为 JSON —— 规格元组
    本身即不可变且可精确相等比较。
    """
    revision = None
    if command.payload is not None:
        revision = (command.payload.target, command.payload.text,
                    command.payload.task, command.payload.prompt)
    return (command.execution_id, command.command_id, command.command,
            revision, command.expected_version)


@dataclass(frozen=True)
class _ReplayEntry:
    """最小内部 replay 记录：fingerprint → 首次裁决结果。

    不可变、不暴露 mutation API；不是第二套 journal —— 无 seq、无
    事实语义，仅是 per-boundary 实例域内的 identity→result 幂等缓存。
    """

    fingerprint: tuple
    result: ControlResult


class ControlBoundary:
    """Control Domain 的 intent authority（CU-CTRL-3 Core）。

    唯一 control-intent 写入入口：submit(command) 做确定性 intent
    裁决，经自持的 boundary writer 把 REQUESTED 事实落入账本。
    effect non-authority：不调用 runtime、不改变执行生命周期、不产生
    CONFIRMED / APPLIED / SUPERSEDED 事实（它们属于后续 composition /
    revision / lifecycle 单元）。

    裁决表（intent 层，pending ∈ {NONE, PAUSE, ABORT}）：
      PAUSE ：NONE→ACCEPTED；PAUSE→NO_OP/ALREADY_REQUESTED；
              ABORT→REJECTED/ALREADY_ABORTING
      RESUME：PAUSE→ACCEPTED（清除 pending pause；不做任何唤醒）；
              NONE→NO_OP/NOT_PAUSED（intent 层读法：无可恢复的
              pending pause）；ABORT→REJECTED/ALREADY_ABORTING
      ABORT ：NONE/PAUSE→ACCEPTED（ABORT > PAUSE，intent 翻转为
              ABORT，不写 SUPERSEDED）；ABORT→NO_OP/ALREADY_REQUESTED
      REVISE：NONE/PAUSE→ACCEPTED（只记录请求；不执行、不动 queue）；
              ABORT→REJECTED/ALREADY_ABORTING

    原子性：锁内先落事实、后改 pending —— writer 失败则异常原样传播，
    状态零变化（不声称 accepted、不伪造 fact、不吞异常）。
    版本：execution_version 保持构造值，绝不因 submit 自行递增。

    幂等（CU-CTRL-4）：replay identity = (execution_id, command_id)。
    exact replay = 纯查找，返回首次 ControlResult —— 零事实、零
    pending 变化、零版本变化、不重新裁决（即使 pending 已前进）；
    同 id 不同 fingerprint → REJECTED/COMMAND_ID_CONFLICT（同样零
    副作用，且不损坏原 replay）。不同 command_id 的语义重复不是
    replay：照走 CTRL-3 裁决。并发：查找→裁决→落账→存 replay→
    改 pending 全在同一临界区 —— 同一新命令的两线程竞争恰走一次
    accept 路径，输者取存储结果。
    """

    def __init__(self, journal: ControlJournal, execution_id: str, *,
                 initial_version: int = 0) -> None:
        _require_non_empty_string(execution_id, "execution_id")
        _require_version(initial_version, "initial_version")
        self._execution_id = execution_id
        self._version = initial_version
        self._pending = PendingIntent()
        self._writer = journal.boundary_writer()
        self._lock = Lock()
        self._replay = {}  # per-instance 幂等缓存：command_id → _ReplayEntry

    @property
    def execution_id(self) -> str:
        return self._execution_id

    @property
    def execution_version(self) -> int:
        return self._version

    @property
    def pending_intent(self) -> PendingIntent:
        return self._pending

    def submit(self, command: ControlCommand) -> ControlResult:
        """裁决一条控制命令；只产生 intent 决策与 REQUESTED 事实。

        同 (execution_id, command_id) 的重放走幂等路径：exact replay
        纯查找返回首次结果；同 id 不同 fingerprint 拒绝为
        COMMAND_ID_CONFLICT —— 两者均零事实、零状态变化。
        """
        if not isinstance(command, ControlCommand):
            raise ControlModelError("submit expects a ControlCommand")
        with self._lock:
            if command.execution_id != self._execution_id:
                # 外域命令不属于本 execution 的 identity 空间：
                # 照 CTRL-3 拒绝，且不进入本域 replay 缓存
                return ControlResult(
                    command_id=command.command_id,
                    execution_id=command.execution_id,
                    status=ControlStatus.REJECTED,
                    execution_version=self._version,
                    reason=ControlReason.INVALID_TARGET)
            fingerprint = _command_fingerprint(command)
            entry = self._replay.get(command.command_id)
            if entry is not None:
                if entry.fingerprint == fingerprint:
                    # EXACT REPLAY：纯查找，零裁决、零事实、零状态变化
                    return entry.result
                return ControlResult(
                    command_id=command.command_id,
                    execution_id=self._execution_id,
                    status=ControlStatus.REJECTED,
                    execution_version=self._version,
                    reason=ControlReason.COMMAND_ID_CONFLICT)
            result = self._adjudicate_and_apply(command)
            # 仅在完整成功路径后落 replay：append 抛异常则无 entry，
            # 重试照常重新裁决（失败的 submit 不留任何痕迹）
            self._replay[command.command_id] = _ReplayEntry(
                fingerprint=fingerprint, result=result)
            return result

    def _adjudicate_and_apply(self, command: ControlCommand) -> ControlResult:
        """CTRL-3 首发路径：裁决 → 落事实 → 推进 pending（调用方持锁）。"""
        status, reason = self._adjudicate(
            command.command, self._pending.kind)
        if status is not ControlStatus.ACCEPTED:
            return ControlResult(
                command_id=command.command_id,
                execution_id=self._execution_id,
                status=status,
                execution_version=self._version,
                reason=reason)
        # 先落事实、后改状态：append 失败 ⇒ 异常传播 + pending 不变
        payload = None
        if command.command is ControlCommandType.REVISE:
            payload = {
                "revision_id": command.command_id,
                "target": command.payload.target.value,
            }
        self._writer.append(
            fact_type=_COMMAND_FACT[command.command],
            execution_id=self._execution_id,
            command_id=command.command_id,
            execution_version=self._version,
            payload=payload)
        next_kind = _NEXT_PENDING[command.command]
        if next_kind is not None:
            self._pending = self._pending.superseded_by(next_kind)
        return ControlResult(
            command_id=command.command_id,
            execution_id=self._execution_id,
            status=ControlStatus.ACCEPTED,
            execution_version=self._version)

    def snapshot(self, lifecycle: ControlLifecycle) -> ControlSnapshot:
        """control-plane 投影：lifecycle 由持有执行真值的组合方供应，
        Boundary 绝不自行推导；park point 恒 NONE（park 权威属于
        Gate，故 PAUSED 投影在此被值模型结构性拒绝）。"""
        if not isinstance(lifecycle, ControlLifecycle):
            raise ControlModelError(
                f"unknown lifecycle: {lifecycle!r}")
        with self._lock:
            return ControlSnapshot(
                execution_id=self._execution_id,
                execution_version=self._version,
                lifecycle=lifecycle,
                pending_intent=self._pending,
                park_point=ParkPoint.NONE,
                revision_queue=())

    def _adjudicate(self, command_type, pending_kind):
        """intent 层确定性裁决；只产生 (status, reason) 决策。"""
        if command_type is ControlCommandType.ABORT:
            if pending_kind is PendingIntentKind.ABORT:
                return ControlStatus.NO_OP, ControlReason.ALREADY_REQUESTED
            return ControlStatus.ACCEPTED, None
        if pending_kind is PendingIntentKind.ABORT:
            # ABORT pending 之下：PAUSE/RESUME/REVISE 一律不再受理
            return ControlStatus.REJECTED, ControlReason.ALREADY_ABORTING
        if command_type is ControlCommandType.PAUSE:
            if pending_kind is PendingIntentKind.PAUSE:
                return ControlStatus.NO_OP, ControlReason.ALREADY_REQUESTED
            return ControlStatus.ACCEPTED, None
        if command_type is ControlCommandType.RESUME:
            if pending_kind is PendingIntentKind.PAUSE:
                return ControlStatus.ACCEPTED, None
            return ControlStatus.NO_OP, ControlReason.NOT_PAUSED
        # REVISE：NONE/PAUSE 之下只记录请求（queue 属 CTRL-5）
        return ControlStatus.ACCEPTED, None


_NEXT_PENDING = {
    ControlCommandType.PAUSE: PendingIntentKind.PAUSE,
    ControlCommandType.RESUME: PendingIntentKind.NONE,
    ControlCommandType.REVISE: None,  # REVISE 不改变 pending intent
    ControlCommandType.ABORT: PendingIntentKind.ABORT,
}
