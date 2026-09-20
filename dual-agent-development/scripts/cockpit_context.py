"""CU-CONTEXT-1 Context Model —— 语义层（设计：cu-context-1-design.md）。

Context 是针对特定 invocation，从已有协作事实中被允许引用的、具有明确
scope / validity / provenance 语义的 Context Item 集合；Context 本身独立
于具体序列化、Prompt 格式和 Communication Method。

固定链（本模块只钉前两级语义）：

    Existing Fact ──(桥接读取，run-local)──> Context Item
    Context Item  ──(铸造，不可变)────────> Context Snapshot
    Context Snapshot ──(未来 CU-CONTEXT-3，显式授权)──> 编译
    编译 ──> 具体 invocation 表示

零接线声明：本模块是纯派生语义库——没有任何调用方，不 import 入口层/
呈现层/引擎任何模块，不构造任何 invocation 表示。cockpit 入口的 prompt
接缝（2.8-E FROZEN）只是当前实现中的 consumption seam，不是本模型的
组成部分。

源宇宙（封闭，恰三类 run-local 事实流）：
- SUBMISSION（submission 文本，经 fresh-segment 修订合并）
- INVOCATION_OUTPUT（紧邻前步输出；截断属编译域，本层载荷为原文全量）
- REVISION_QUEUE（待定修订条目；accepted ≠ applied ≠ honored）

非定义链（与 §17 三律同锚）：Context ≠ Full History ≠ Memory ≠ Trace
≠ Usage ≠ Provenance ≠ Execution State ≠ Agent Identity ≠ Runtime
Identity ≠ Prompt Text ≠ Communication Method；Context Model ≠ Context
Compiler。

身份（零新 identity primitive）：快照身份 = (task_id, step_index)；条目
身份 = (kind, source_id, ordinal)。IDENTITY INVARIANT（已证明）：一个
task 中一个 step_index 在当前执行模型下至多对应一次 invocation，故至多
一个快照——见设计 PART 5 六段结构证明。

有效性（由既有事实推导，零新状态机；INVALID = 构造期拒绝，非运行态；
UNKNOWN 不创建）：VALID / SUPERSEDED / STALE 三态，语义见 derive_validity。
"""
from dataclasses import dataclass
from enum import Enum

__all__ = (
    "ContextModelError",
    "ContextItemKind",
    "ContextSource",
    "ContextValidity",
    "ContextProvenance",
    "ContextItem",
    "ContextSnapshot",
    "task_item",
    "item_from_invocation_result",
    "item_from_revision",
    "derive_validity",
)


class ContextModelError(ValueError):
    """封闭的契约拒绝（INVALID）；message 只含字段名与规则，绝不含被拒值。"""


class ContextItemKind(str, Enum):
    """条目种类封闭词表（恰三值，与源宇宙一一对应）。

    role 不是条目（请求模板参数，非自由内容）；修订两通道
    （NEXT_INVOCATION/SUBMISSION）共享 REVISION 种类。
    """

    TASK = "TASK"
    PRIOR_STEP_OUTPUT = "PRIOR_STEP_OUTPUT"
    REVISION = "REVISION"


class ContextSource(str, Enum):
    """来源类型封闭词表（恰三值；Trace/Usage/组合/分组/runtime 身份/
    ConversationRecord/Memory 皆不在源宇宙内）。"""

    SUBMISSION = "SUBMISSION"
    INVOCATION_OUTPUT = "INVOCATION_OUTPUT"
    REVISION_QUEUE = "REVISION_QUEUE"


class ContextValidity(str, Enum):
    """条目状态封闭词表（恰三运行态；INVALID=构造期拒绝非运行态，
    UNKNOWN 不创建——无可靠推导来源时诚实缺席）。"""

    VALID = "VALID"
    SUPERSEDED = "SUPERSEDED"
    STALE = "STALE"


# kind ↔ source_type 结构耦合（封闭映射，构造期强制）。
_KIND_SOURCE = {
    ContextItemKind.TASK: ContextSource.SUBMISSION,
    ContextItemKind.PRIOR_STEP_OUTPUT: ContextSource.INVOCATION_OUTPUT,
    ContextItemKind.REVISION: ContextSource.REVISION_QUEUE,
}


def _require_non_empty_string(value, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ContextModelError(f"{field_name} must be a non-empty string")


def _require_optional_id(value, field_name: str) -> None:
    if value is not None:
        _require_non_empty_string(value, field_name)


def _require_optional_version(value, field_name: str) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ContextModelError(f"{field_name} must be a non-negative int")


def _require_optional_time(value, field_name: str) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise ContextModelError(f"{field_name} must be a non-negative number")


@dataclass(frozen=True)
class ContextProvenance:
    """条目来源描述符（≠条目内容，≠ OFFLINE/REAL 证据词——后者留在
    各自载体域，本模型零重定义）。

    source_id 恰用现有唯一身份原语：输出条目=invocation_id（单独即
    唯一）；修订条目=revision_id；submission 无独立 id，以
    execution_version（boundary 身份，现读）定位。producer_role 为
    描述性属性（role 可重复，绝不参与身份判定）。时间字段仅当来源
    已可靠持有时携带（trace 的 float|None；缺席=None，本层零新时钟）。
    """

    source_type: ContextSource
    task_id: str
    source_id: str | None = None
    producer_role: str | None = None
    execution_version: int | None = None
    started_at: float | None = None
    finished_at: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.source_type, ContextSource):
            raise ContextModelError("source_type must be a ContextSource")
        _require_non_empty_string(self.task_id, "task_id")
        _require_optional_id(self.source_id, "source_id")
        _require_optional_id(self.producer_role, "producer_role")
        _require_optional_version(self.execution_version, "execution_version")
        _require_optional_time(self.started_at, "started_at")
        _require_optional_time(self.finished_at, "finished_at")


@dataclass(frozen=True)
class ContextItem:
    """Context Item：对一条 run-local 协作事实的引用（非 prompt 序列化）。

    本体 = kind + payload（事实原文引用）+ provenance（来源描述）；
    排序/截断/打包/格式化一律属编译域（CU-CONTEXT-3），本层不做。
    """

    kind: ContextItemKind
    payload: str
    provenance: ContextProvenance
    ordinal: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ContextItemKind):
            raise ContextModelError("kind must be a ContextItemKind")
        _require_non_empty_string(self.payload, "payload")
        if not isinstance(self.provenance, ContextProvenance):
            raise ContextModelError("provenance must be a ContextProvenance")
        if _KIND_SOURCE[self.kind] is not self.provenance.source_type:
            raise ContextModelError(
                "kind and provenance.source_type must match the closed map")
        if isinstance(self.ordinal, bool) or not isinstance(self.ordinal, int) \
                or self.ordinal < 0:
            raise ContextModelError("ordinal must be a non-negative int")

    @property
    def identity(self) -> tuple:
        """条目身份 = (kind, 来源身份, 序数)——复用既有原语，零新铸造。"""
        return (self.kind, self.provenance.source_id or "", self.ordinal)


@dataclass(frozen=True)
class ContextSnapshot:
    """Context Snapshot：某 (task_id, step_index) 的不可变条目集合。

    作用域恰 RUN+STEP；run 隔离为构造期强制（跨 run 条目结构性
    不可入本快照——provenance.task_id 必须全等于快照 task_id）。
    validity 与 items 位置对齐、长度相等。这不是 God Object：显式
    排除 invocation 表示、trace、usage、UI 史、跨 run 记录、组合
    披露全文。快照缺席（No Context）是合法状态——本模型零默认铸造。
    """

    task_id: str
    step_index: int
    items: tuple
    validity: tuple

    def __post_init__(self) -> None:
        _require_non_empty_string(self.task_id, "task_id")
        if isinstance(self.step_index, bool) or not isinstance(self.step_index, int) \
                or self.step_index < 0:
            raise ContextModelError("step_index must be a non-negative int")
        if not isinstance(self.items, tuple):
            raise ContextModelError("items must be a tuple")
        if not isinstance(self.validity, tuple):
            raise ContextModelError("validity must be a tuple")
        if len(self.items) != len(self.validity):
            raise ContextModelError(
                "items and validity must be position-aligned equal-length tuples")
        for item in self.items:
            if not isinstance(item, ContextItem):
                raise ContextModelError("items entries must be ContextItem values")
            if item.provenance.task_id != self.task_id:
                raise ContextModelError(
                    "cross-run item rejected: provenance.task_id must equal "
                    "snapshot task_id")
        for state in self.validity:
            if not isinstance(state, ContextValidity):
                raise ContextModelError(
                    "validity entries must be ContextValidity values")

    @property
    def identity(self) -> tuple:
        """快照身份 = (task_id, step_index)——IDENTITY INVARIANT：
        当前执行模型下同 task 同 step 至多一次 invocation，故至多一个快照。"""
        return (self.task_id, self.step_index)


# ------------------------------------------------- Existing Fact → Item 桥


def task_item(task_text, *, task_id, execution_version=None, ordinal=0):
    """submission 文本 → TASK 条目（桥接读取，纯函数）。

    execution_version 取 boundary 现读值（submission 的权威定位）；
    ordinal 用于同源历史链的稳定排序（如后者胜合并前的旧条目）。
    """
    _require_non_empty_string(task_text, "task_text")
    _require_non_empty_string(task_id, "task_id")
    _require_optional_version(execution_version, "execution_version")
    return ContextItem(
        kind=ContextItemKind.TASK,
        payload=task_text,
        provenance=ContextProvenance(
            source_type=ContextSource.SUBMISSION,
            task_id=task_id,
            execution_version=execution_version),
        ordinal=ordinal)


def item_from_invocation_result(result, *, task_id, producer_role=None):
    """紧邻前步 InvocationResult 鸭 → PRIOR_STEP_OUTPUT 条目（桥接读取）。

    读 .output（str，非空）与 .trace.invocation_id（必在、即唯一身份）；
    可选携带 .trace.started_at/.finished_at（float|None，缺席=None）。
    载荷 = 输出原文全量——接缝处的 4000 截断属编译域，本层不做。
    非紧邻前步的事实没有桥：本函数是唯一输出桥，调用纪律由
    （未来）铸造点裁决，本层不预设非相邻引用。
    """
    _require_non_empty_string(task_id, "task_id")
    _require_optional_id(producer_role, "producer_role")
    output = getattr(result, "output", None)
    if not isinstance(output, str) or not output.strip():
        raise ContextModelError(
            "invocation result output must be a non-empty string")
    trace = getattr(result, "trace", None)
    invocation_id = getattr(trace, "invocation_id", None)
    _require_non_empty_string(invocation_id, "invocation_id")
    started_at = getattr(trace, "started_at", None)
    finished_at = getattr(trace, "finished_at", None)
    return ContextItem(
        kind=ContextItemKind.PRIOR_STEP_OUTPUT,
        payload=output,
        provenance=ContextProvenance(
            source_type=ContextSource.INVOCATION_OUTPUT,
            task_id=task_id,
            source_id=invocation_id,
            producer_role=producer_role,
            started_at=started_at,
            finished_at=finished_at))


def item_from_revision(entry, *, task_id):
    """修订队列条目鸭（revision_id + text）→ REVISION 条目（桥接读取）。

    队列条目语义保持既有三段律：accepted ≠ applied ≠ honored——派生
    时点条目即「已受理待定」，是否已消费由 derive_validity 以
    applied 见证裁决，本桥不解释、不改写。
    """
    _require_non_empty_string(task_id, "task_id")
    revision_id = getattr(entry, "revision_id", None)
    _require_non_empty_string(revision_id, "revision_id")
    text = getattr(entry, "text", None)
    _require_non_empty_string(text, "text")
    return ContextItem(
        kind=ContextItemKind.REVISION,
        payload=text,
        provenance=ContextProvenance(
            source_type=ContextSource.REVISION_QUEUE,
            task_id=task_id,
            source_id=revision_id))


# ------------------------------------------------------- validity 推导（纯）


def derive_validity(item, *, current_submission_text=None,
                    applied_revision_ids=(), live_invocation_ids=None):
    """由既有事实推导条目状态（零新状态机；只返回三运行态之一）。

    - TASK：无当前文本见证（None）→ VALID（无取代证据 = 构建时点仍是
      当前授权态）；有见证 → 载荷与当前一致 VALID / 不一致 SUPERSEDED
      （SUBMISSION 修订 fresh-segment 合并的后者胜语义）。
    - REVISION：source_id ∈ applied_revision_ids → SUPERSEDED（NEXT_
      INVOCATION 一次性精确消费的既有见证）；否则 VALID（pending =
      已受理未消费）。honored（真实请求见证）属观察域，本层不裁决。
    - PRIOR_STEP_OUTPUT：无活跃链见证（None）→ VALID（含 PARKED 恢复
      ——RunState 原样携带即链活跃）；有集合 → 在链 VALID / 不在链
      STALE（fresh-segment 重建替换旧链；被替换≠被消费）。
    """
    if not isinstance(item, ContextItem):
        raise ContextModelError("item must be a ContextItem")
    if item.kind is ContextItemKind.TASK:
        if current_submission_text is None:
            return ContextValidity.VALID
        if not isinstance(current_submission_text, str) or \
                not current_submission_text.strip():
            raise ContextModelError(
                "current_submission_text must be a non-empty string")
        return (ContextValidity.VALID if item.payload == current_submission_text
                else ContextValidity.SUPERSEDED)
    if item.kind is ContextItemKind.REVISION:
        applied = tuple(applied_revision_ids)
        if item.provenance.source_id in applied:
            return ContextValidity.SUPERSEDED
        return ContextValidity.VALID
    # PRIOR_STEP_OUTPUT
    if live_invocation_ids is None:
        return ContextValidity.VALID
    live = tuple(live_invocation_ids)
    if item.provenance.source_id in live:
        return ContextValidity.VALID
    return ContextValidity.STALE
