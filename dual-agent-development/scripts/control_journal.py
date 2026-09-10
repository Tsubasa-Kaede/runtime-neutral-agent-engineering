"""V3.2 CU-CTRL-2: append-only Control facts ledger（ControlJournal）。

Sections 1-6 冻结架构中 control-plane 的唯一事实账本：三类受限 writer
capability（boundary / revision / composition）追加不可变 JournalFact，
journal-local seq 单调递增，读取只提供 detached immutable view。

边界（本 CU 只立账本，不做行为）：
- 只追加、只读取：无 delete/update/reset 面；seq 由 journal 铸造，
  caller 不可指定。
- 不做裁决：不判断命令合法性、不去重（ALREADY_REQUESTED / STALE_VERSION
  属于 ControlBoundary，CTRL-3）、不做 safety 校验。
- 不拥有 version：execution_version 只是记录 caller 供应的 control
  epoch，递增属于 ControlBoundary。
- 不拥有 lifecycle：PAUSE_CONFIRMED 等只是事实，状态投影属于后续
  projector；journal 不是第二状态机。
- 不拥有 replay：同 identity 重复 append 得到两条事实，exact replay
  缓存属于 ControlBoundary。
- 事实域分离：与执行观察事件域词汇完全分离（两个事实域，零 import），
  import 仅标准库；与 control_boundary.py 的组合推迟到 CTRL-3。
- V1 为 in-memory 本地账本，无跨进程持久化（Section 6 冻结范围外）。
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from threading import Lock
from types import MappingProxyType

__all__ = (
    "BoundaryWriter", "CompositionWriter", "ControlFactType",
    "ControlJournal", "ControlJournalError", "JournalFact", "RevisionWriter",
)


class ControlJournalError(ValueError):
    """ControlJournal 的构造期/权限期拒绝（词表外值 / 非法形状 / 越权）。"""


class ControlFactType(str, Enum):
    """Sections 1-6 冻结的封闭 Control fact 词表（恰 8 值）。

    这是 control 域自己的事实词汇；执行观察事件域的事件词汇是另一套
    契约，二者互不引用、互不混入。
    """

    PAUSE_REQUESTED = "PAUSE_REQUESTED"
    PAUSE_CONFIRMED = "PAUSE_CONFIRMED"
    RESUME_REQUESTED = "RESUME_REQUESTED"
    REVISE_REQUESTED = "REVISE_REQUESTED"
    REVISION_APPLIED = "REVISION_APPLIED"
    ABORT_REQUESTED = "ABORT_REQUESTED"
    ABORT_CONFIRMED = "ABORT_CONFIRMED"
    ABORT_SUPERSEDED = "ABORT_SUPERSEDED"


_ALLOWED_PAYLOAD_VALUE_TYPES = (str, int, float, bool, type(None))


def _require_non_empty_string(value, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ControlJournalError(
            f"{field_name} must be a non-empty string")


def _require_non_negative_int(value, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ControlJournalError(
            f"{field_name} must be a non-negative int")


@dataclass(frozen=True)
class JournalFact:
    """一条不可变 Control 事实。

    seq 由 journal 铸造（caller 不可指定）；fact_type 来自封闭词表；
    command_id / execution_id / execution_version 均 caller 供应、原样
    保存（journal 不生成 identity、不递增 version）。payload 只承载该
    fact 的结构化 control 标量信息（暴露为只读映射，构造时防御性拷贝）。
    """

    seq: int
    fact_type: ControlFactType
    execution_id: str
    command_id: str
    execution_version: int
    payload: Mapping | None = None

    def __post_init__(self) -> None:
        _require_non_negative_int(self.seq, "seq")
        if not isinstance(self.fact_type, ControlFactType):
            raise ControlJournalError(
                f"unknown control fact: {self.fact_type!r}")
        _require_non_empty_string(self.execution_id, "execution_id")
        _require_non_empty_string(self.command_id, "command_id")
        _require_non_negative_int(self.execution_version, "execution_version")
        if self.payload is None:
            object.__setattr__(self, "payload", MappingProxyType({}))
            return
        if not isinstance(self.payload, Mapping):
            raise ControlJournalError("payload must be a mapping")
        for key, value in self.payload.items():
            if not isinstance(value, _ALLOWED_PAYLOAD_VALUE_TYPES):
                raise ControlJournalError(
                    f"payload value for {key!r} must be an immutable scalar")
        object.__setattr__(
            self, "payload", MappingProxyType(dict(self.payload)))


class _JournalWriter:
    """受限 writer capability 基类：append 权限由子类 allowed_facts 钉死。

    权限边界是结构性的（capability 携带白名单），不依赖调用方自觉；
    越权 append 在进入 journal 前即被拒绝。
    """

    allowed_facts: frozenset = frozenset()

    def __init__(self, journal: "ControlJournal") -> None:
        self._journal = journal

    def append(
        self,
        *,
        fact_type: ControlFactType,
        execution_id: str,
        command_id: str,
        execution_version: int,
        payload: Mapping | None = None,
    ) -> JournalFact:
        if not isinstance(fact_type, ControlFactType):
            raise ControlJournalError(
                f"unknown control fact: {fact_type!r}")
        if fact_type not in self.allowed_facts:
            raise ControlJournalError(
                f"{type(self).__name__} may not append "
                f"{getattr(fact_type, 'value', fact_type)!r}")
        return self._journal._append(
            fact_type=fact_type,
            execution_id=execution_id,
            command_id=command_id,
            execution_version=execution_version,
            payload=payload,
        )


class BoundaryWriter(_JournalWriter):
    """ControlBoundary 专用：全部裁决回声事实。

    REVISION_APPLIED 被 Section 6 Amendment C 独占划给 RevisionWriter，
    BoundaryWriter 不得追加。
    """

    allowed_facts = frozenset({
        ControlFactType.PAUSE_REQUESTED,
        ControlFactType.PAUSE_CONFIRMED,
        ControlFactType.RESUME_REQUESTED,
        ControlFactType.REVISE_REQUESTED,
        ControlFactType.ABORT_REQUESTED,
        ControlFactType.ABORT_CONFIRMED,
        ControlFactType.ABORT_SUPERSEDED,
    })


class RevisionWriter(_JournalWriter):
    """RevisionAdapter 专用（Section 6 Amendment C）。

    只可追加 REVISION_APPLIED —— 它是 factual write authority，不是
    revision 裁决者（accept/reject/version/lifecycle/queue 一概无权）。
    """

    allowed_facts = frozenset({ControlFactType.REVISION_APPLIED})


class CompositionWriter(_JournalWriter):
    """组合根 / wrapper 层专用：abort 落地结果事实。

    wrapper 层观察 gate 结果时追加 ABORT_CONFIRMED / ABORT_SUPERSEDED；
    不得冒充 RevisionWriter 或 BoundaryWriter。
    """

    allowed_facts = frozenset({
        ControlFactType.ABORT_CONFIRMED,
        ControlFactType.ABORT_SUPERSEDED,
    })


class ControlJournal:
    """append-only Control facts ledger（in-memory、线程安全）。

    seq 规则：首条 = 0，其后 = 前条 + 1，journal 内严格单调连续。
    Journal-local ordering：seq 不是任何其他序列概念（execution
    version / command identity / invocation 计数），也不与之互换。
    """

    def __init__(self) -> None:
        self._facts: list[JournalFact] = []
        self._next_seq = 0
        self._lock = Lock()

    def boundary_writer(self) -> BoundaryWriter:
        return BoundaryWriter(self)

    def revision_writer(self) -> RevisionWriter:
        return RevisionWriter(self)

    def composition_writer(self) -> CompositionWriter:
        return CompositionWriter(self)

    def snapshot(self) -> tuple[JournalFact, ...]:
        """全部事实的 detached immutable view（按 seq 升序）。"""
        with self._lock:
            return tuple(self._facts)

    def since(self, seq: int) -> tuple[JournalFact, ...]:
        """seq 严格大于 cursor 的全部事实（cursor 读取契约）。"""
        _require_non_negative_int(seq, "seq")
        with self._lock:
            return tuple(
                fact for fact in self._facts if fact.seq > seq)

    def _append(
        self,
        *,
        fact_type: ControlFactType,
        execution_id: str,
        command_id: str,
        execution_version: int,
        payload: Mapping | None,
    ) -> JournalFact:
        # 权限校验已在 writer 层完成；这里只铸造 seq 并追加。
        with self._lock:
            fact = JournalFact(
                seq=self._next_seq,
                fact_type=fact_type,
                execution_id=execution_id,
                command_id=command_id,
                execution_version=execution_version,
                payload=payload,
            )
            self._facts.append(fact)
            self._next_seq += 1
        return fact
