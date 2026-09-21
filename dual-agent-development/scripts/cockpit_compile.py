"""CU-CONTEXT-3 Context Compiler —— 编译层（设计：cu-context-3-design.md）。

Context Compiler 把单个 invocation 的语义 Context（快照形态的候选
集合）确定性编译为 invocation-oriented 结构表示。它是纯函数层：拥有
恰五职责——selection / ordering / budget / truncation / packing；
不拥有任何事实（输入只读）、不产生任何提示词文本（输出为 runtime-
neutral 结构段）、不调用 runtime、不写任何观察/控制/用量域。

固定链（本模块是第三级；零接线——没有任何调用方，零行为变化）：

    Existing Fact ──> Context Item ──> Context Snapshot ──(本模块)──>
    CompiledInvocationContext ──(未来显式授权)──> 文本组装（接缝域）

职责分离律（反混同）：事实 owner ≠ candidate 生成（桥）≠ 选择/排序/
裁剪（本层）≠ 文本序列化（接缝）≠ 传输 ≠ 调用（各归接入层）。
本层与语义层单向依赖：只 import 语义模型类型用于消费，
绝不反向被依赖。Memory 域自足：与本模块互相零 import（桥规格级
关闭，开启须三重显式授权——词表修订 + 零携带闸修订 + 桥模块）。

单通道律：唯一公共入口 compile_context(snapshot, policy)。快照之外
任何事实载体（跨 run 记录、Memory 域条目、观察/控制/用量日志、原始
请求文本）结构性不可达；输入类型校验仅认快照。

确定性律：同 (snapshot, policy) ⇒ 逐字节同输出——零时钟、零随机、
零集合迭代序。排序 = kind 秩 TASK < PRIOR_STEP_OUTPUT < REVISION，
kind 内保输入序（任务 ordinal 序 / 修订 FIFO 序）；该规则逐位复现
现行文本序。邻接旁证（adjacent_invocation_ids）沿语义层的派生见证
惯用法：它不携带内容，只标识「哪个 invocation 是本步紧邻前驱」；
缺席 ⇒ 零前步段（首步同构——现行接缝无前步即无该段，无占位）。

预算律：单位 = 字符（唯一确定性可计量）；item 预算按 kind 分列；
invocation 预算 = 本次编译的全量限额集（恰以单次 invocation 为作用
域，零跨 invocation 优化）。token 计量三态抽象 KNOWN / UNKNOWN /
UNSUPPORTED（镜像观察域词表，测量源=事后唯一）：编译时点恒
UNKNOWN ≠ 0（缺席不是零，把缺席折算成数值即伪造计量）；禁字符→
token 换算。token 预算执行延期（真实计量源边界建立后另须授权）。

截断律：默认政策 = 前步输出 4000 字符头部截断（codepoint 切片，
逐位复现现行接缝值）；任务原文恒不截断（mandatory 内容，现行无
任务限长）。该语义在现行系统中物理居住于接缝层；本层认领语义、
物理迁移另行显式授权（byte-parity 门）。头部模式与 codepoint 边界
是定律而非旋钮——仅复现现行，无其他截断模式。

装箱律：选中条目 → 按序产出 CompiledSegment（kind, payload,
provenance, validity_at_compile）frozen 段；零合并、零改写、零注释
注入；段边界 = 条目边界；每段携带原条目 provenance 逐字引用（编译
绝不剥离来源）。输出不含任何模板字符串/段标/前导指令——文本组装
留接缝域。

有效性消费律：本层消费不生产有效性（选择过滤器沿语义层三态）；
被排除条目进选择报告，原因词表封闭（恰四值，见模块常量）——编译
期记账，非条目状态。空快照 → 空编译产物（零段/零预算占用/空报告）
——合法，零占位零伪段。

非定义链：Compiler ≠ Context Model ≠ Memory ≠ 文本拼装器 ≠
Runtime 接入层 ≠ 路由器；CompiledInvocationContext 既非语义
Context 亦非提示词文本——它是编译域的 invocation-specific 投影
产物（派生、不可变、单 invocation 目标）。
"""
from dataclasses import dataclass
from enum import Enum
import hashlib
import json

try:
    from .cockpit_context import (  # noqa: E402
        ContextItemKind,
        ContextProvenance,
        ContextSnapshot,
        ContextValidity,
    )
except ImportError:  # 平铺导入（源码树/测试直挂 scripts 目录）
    from cockpit_context import (  # noqa: E402
        ContextItemKind,
        ContextProvenance,
        ContextSnapshot,
        ContextValidity,
    )

__all__ = (
    "CompileModelError",
    "CompilePolicy",
    "CompiledSegment",
    "CompiledInvocationContext",
    "TokenStatus",
    "TokenMeasure",
    "BudgetReport",
    "TruncationFact",
    "SelectionNote",
    "STALE_SOURCE",
    "SUPERSEDED",
    "BUDGET_EXCLUDED",
    "DUPLICATE_COLLAPSED",
    "compile_context",
)


class CompileModelError(ValueError):
    """封闭的契约拒绝（INVALID 惯用法沿语义层）；message 只含字段名
    与规则，绝不含被拒值。"""


# 选择报告封闭原因词表（恰四值；扩展须设计修订授权）。
STALE_SOURCE = "STALE_SOURCE"
SUPERSEDED = "SUPERSEDED"
BUDGET_EXCLUDED = "BUDGET_EXCLUDED"
DUPLICATE_COLLAPSED = "DUPLICATE_COLLAPSED"

_SELECTION_REASONS = (
    STALE_SOURCE, SUPERSEDED, BUDGET_EXCLUDED, DUPLICATE_COLLAPSED)

# kind 秩（排序定律；kind 内保输入序——sorted 稳定性承担）。
_KIND_RANK = {
    ContextItemKind.TASK: 0,
    ContextItemKind.PRIOR_STEP_OUTPUT: 1,
    ContextItemKind.REVISION: 2,
}

_KIND_ORDER = (
    ContextItemKind.TASK,
    ContextItemKind.PRIOR_STEP_OUTPUT,
    ContextItemKind.REVISION,
)


def _require_non_empty_string(value, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise CompileModelError(f"{field_name} must be a non-empty string")


def _require_positive_int_or_none(value, field_name: str) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise CompileModelError(f"{field_name} must be a positive int or None")


class TokenStatus(str, Enum):
    """token 计量状态封闭词表（三态镜像观察域；测量源=事后唯一）。"""

    KNOWN = "KNOWN"
    UNKNOWN = "UNKNOWN"
    UNSUPPORTED = "UNSUPPORTED"


@dataclass(frozen=True)
class TokenMeasure:
    """token 计量三态值。

    KNOWN 携带非负整数计数（仅由真实测量供给——未来授权域）；
    UNKNOWN / UNSUPPORTED 不携带计数。UNKNOWN ≠ 0：缺席不是零。
    编译时点恒 UNKNOWN（本层零换算零推测）。
    """

    status: TokenStatus
    tokens: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, TokenStatus):
            raise CompileModelError("status must be a TokenStatus")
        if self.status is TokenStatus.KNOWN:
            if isinstance(self.tokens, bool) or \
                    not isinstance(self.tokens, int) or self.tokens < 0:
                raise CompileModelError(
                    "tokens must be a non-negative int when status is KNOWN")
        elif self.tokens is not None:
            raise CompileModelError("tokens must be None unless status is KNOWN")

    @classmethod
    def known(cls, count: int) -> "TokenMeasure":
        return cls(status=TokenStatus.KNOWN, tokens=count)

    @classmethod
    def unknown(cls) -> "TokenMeasure":
        return cls(status=TokenStatus.UNKNOWN)

    @classmethod
    def unsupported(cls) -> "TokenMeasure":
        return cls(status=TokenStatus.UNSUPPORTED)


@dataclass(frozen=True)
class CompilePolicy:
    """编译政策值对象（确定性限额集；恰以单次 invocation 为作用域）。

    - prior_output_char_limit：前步输出字符上限（默认 4000 = 现行
      接缝值逐位复现；None = 不限）。
    - revision_char_limit：修订载荷字符上限（默认 None = 现行零截断）。
    - max_revision_items：修订条数上限（默认 None = FIFO 全量现行）。
    - total_char_limit：全量嵌入字符上限（默认 None = 现行无全量限额；
      超限时按逆优先序确定性丢弃——修订尾部先行、前步继后、任务
      永不丢弃；任务独超即政策矛盾=拒绝）。

    任务字符上限不存在（任务原文恒不截断定律——mandatory 内容）。
    """

    prior_output_char_limit: int | None = 4000
    revision_char_limit: int | None = None
    max_revision_items: int | None = None
    total_char_limit: int | None = None

    def __post_init__(self) -> None:
        _require_positive_int_or_none(
            self.prior_output_char_limit, "prior_output_char_limit")
        _require_positive_int_or_none(
            self.revision_char_limit, "revision_char_limit")
        _require_positive_int_or_none(
            self.max_revision_items, "max_revision_items")
        _require_positive_int_or_none(
            self.total_char_limit, "total_char_limit")

    @property
    def fingerprint(self) -> str:
        """政策指纹 = 政策值内容寻址散列（derived 非 minted；确定性：
        同值同指纹、零时钟零随机）。"""
        payload = json.dumps(
            {
                "prior_output_char_limit": self.prior_output_char_limit,
                "revision_char_limit": self.revision_char_limit,
                "max_revision_items": self.max_revision_items,
                "total_char_limit": self.total_char_limit,
            },
            sort_keys=True, ensure_ascii=True, separators=(",", ":"))
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]
        return f"policy_{digest}"


@dataclass(frozen=True)
class SelectionNote:
    """选择报告条目：被排除条目的身份 + 封闭原因词表之一。

    编译期记账，非条目状态（有效性由语义层拥有，本层只消费）。
    """

    item_identity: tuple
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.item_identity, tuple):
            raise CompileModelError("item_identity must be a tuple")
        if self.reason not in _SELECTION_REASONS:
            raise CompileModelError(
                "reason must be one of the closed selection vocabulary")


@dataclass(frozen=True)
class TruncationFact:
    """截断记账：原长 vs 嵌入长（字符，codepoint 计）——诚实披露，
    非遥测（零时间零采样）。仅收录实际发生截断的段。"""

    kind: ContextItemKind
    original_chars: int
    embedded_chars: int

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ContextItemKind):
            raise CompileModelError("kind must be a ContextItemKind")
        for field_name in ("original_chars", "embedded_chars"):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) \
                    or value < 0:
                raise CompileModelError(
                    f"{field_name} must be a non-negative int")
        if self.embedded_chars > self.original_chars:
            raise CompileModelError(
                "embedded_chars must not exceed original_chars")


@dataclass(frozen=True)
class BudgetReport:
    """确定性字符/条目记账（可测事实，零遥测编造）。

    per-kind 双元组序列按 kind 秩排列（封闭三 kind）。"""

    item_counts_by_kind: tuple
    embedded_chars_by_kind: tuple
    total_embedded_chars: int
    truncations: tuple

    def __post_init__(self) -> None:
        for field_name in ("item_counts_by_kind", "embedded_chars_by_kind",
                           "truncations"):
            if not isinstance(getattr(self, field_name), tuple):
                raise CompileModelError(f"{field_name} must be a tuple")
        if isinstance(self.total_embedded_chars, bool) or \
                not isinstance(self.total_embedded_chars, int) or \
                self.total_embedded_chars < 0:
            raise CompileModelError(
                "total_embedded_chars must be a non-negative int")


@dataclass(frozen=True)
class CompiledSegment:
    """装箱产物：一个条目的 invocation-oriented 结构段。

    payload = 原文（或按政策的头部截断结果）——零合并、零改写、
    零注释注入。provenance 为原条目来源描述的逐字引用。文本组装
    （段标/前导指令/模板）不属本层。"""

    kind: ContextItemKind
    payload: str
    provenance: ContextProvenance
    validity_at_compile: ContextValidity

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ContextItemKind):
            raise CompileModelError("kind must be a ContextItemKind")
        _require_non_empty_string(self.payload, "payload")
        if not isinstance(self.provenance, ContextProvenance):
            raise CompileModelError("provenance must be a ContextProvenance")
        if not isinstance(self.validity_at_compile, ContextValidity):
            raise CompileModelError(
                "validity_at_compile must be a ContextValidity")


@dataclass(frozen=True)
class CompiledInvocationContext:
    """编译产物：单 invocation 目标的确定性投影。

    作用域恰一个 (task_id, step_index)。identity = (快照身份, 政策
    指纹)——零新身份铸造（政策指纹为政策值内容寻址散列，derived）。
    零调用方即零行为：本类型不触发任何执行、观察、记录或文件效果。"""

    task_id: str
    step_index: int
    segments: tuple
    selection_notes: tuple
    budget: BudgetReport
    token_measure: TokenMeasure
    policy_fingerprint: str

    def __post_init__(self) -> None:
        _require_non_empty_string(self.task_id, "task_id")
        if isinstance(self.step_index, bool) or \
                not isinstance(self.step_index, int) or self.step_index < 0:
            raise CompileModelError("step_index must be a non-negative int")
        if not isinstance(self.segments, tuple):
            raise CompileModelError("segments must be a tuple")
        if not isinstance(self.selection_notes, tuple):
            raise CompileModelError("selection_notes must be a tuple")
        if not isinstance(self.budget, BudgetReport):
            raise CompileModelError("budget must be a BudgetReport")
        if not isinstance(self.token_measure, TokenMeasure):
            raise CompileModelError("token_measure must be a TokenMeasure")
        _require_non_empty_string(self.policy_fingerprint, "policy_fingerprint")
        for segment in self.segments:
            if not isinstance(segment, CompiledSegment):
                raise CompileModelError(
                    "segments entries must be CompiledSegment values")
        for note in self.selection_notes:
            if not isinstance(note, SelectionNote):
                raise CompileModelError(
                    "selection_notes entries must be SelectionNote values")

    @property
    def identity(self) -> tuple:
        """输出身份 = (快照身份, 政策指纹)。"""
        return ((self.task_id, self.step_index), self.policy_fingerprint)


# ------------------------------------------------------------- 编译（纯函数）


def _normalize_adjacent(value) -> frozenset:
    """邻接旁证规范化：None/空 = 无紧邻前驱（首步同构）；否则非空
    字符串 id 的集合（成员判定用，永不迭代）。"""
    if value is None:
        return frozenset()
    entries = tuple(value)
    for entry in entries:
        _require_non_empty_string(entry, "adjacent_invocation_ids entry")
    return frozenset(entries)


def _select(snapshot: ContextSnapshot, adjacent: frozenset):
    """选择：有效性过滤 + 邻接旁证 + 身份全等坍缩。

    - 非 VALID：STALE → STALE_SOURCE；SUPERSEDED → SUPERSEDED（选择
      报告记账后排除；INVALID 是构造期拒绝，运行态不可达）。
    - PRIOR_STEP_OUTPUT：source_id 不在邻接旁证内 → 非本步前驱，
      非候选（结构性缺席，零报告条目——非候选不是被排除）。
    - 身份全等重复 → 保留首个，后续坍缩记账 DUPLICATE_COLLAPSED。
    - TASK（VALID）：恒候选（mandatory；不可被预算/截断排除）。
    """
    notes: tuple = ()
    kept: tuple = ()
    seen = set()  # 仅成员判定，永不迭代（输出序零集合依赖）
    for item, state in zip(snapshot.items, snapshot.validity):
        if state is ContextValidity.STALE:
            notes = notes + (SelectionNote(item.identity, STALE_SOURCE),)
            continue
        if state is ContextValidity.SUPERSEDED:
            notes = notes + (SelectionNote(item.identity, SUPERSEDED),)
            continue
        if item.kind is ContextItemKind.PRIOR_STEP_OUTPUT:
            if item.provenance.source_id not in adjacent:
                continue
        identity = item.identity
        if identity in seen:
            notes = notes + (SelectionNote(identity, DUPLICATE_COLLAPSED),)
            continue
        seen.add(identity)
        kept = kept + (item,)
    return kept, notes


def _apply_item_budget(ordered: tuple, policy: CompilePolicy, notes: tuple):
    """修订条数上限：FIFO 头部保留、尾部丢弃并记 BUDGET_EXCLUDED。"""
    if policy.max_revision_items is None:
        return ordered, notes
    revision_identities = tuple(
        item.identity for item in ordered
        if item.kind is ContextItemKind.REVISION)
    allowed = frozenset(revision_identities[:policy.max_revision_items])
    kept: tuple = ()
    for item in ordered:
        if item.kind is ContextItemKind.REVISION and \
                item.identity not in allowed:
            notes = notes + (SelectionNote(item.identity, BUDGET_EXCLUDED),)
            continue
        kept = kept + (item,)
    return kept, notes


def _char_limit(kind: ContextItemKind, policy: CompilePolicy):
    """按 kind 取字符上限；任务恒 None（原文不截断定律）。"""
    if kind is ContextItemKind.TASK:
        return None
    if kind is ContextItemKind.PRIOR_STEP_OUTPUT:
        return policy.prior_output_char_limit
    return policy.revision_char_limit


def _pack(kept: tuple, policy: CompilePolicy):
    """装箱（含截断）：条目 → (原条目, 段) 对序列 + 截断记账。

    头部截断 = payload[:limit]（codepoint 切片，逐位复现现行接缝
    语义）。"""
    packed: tuple = ()
    truncations: tuple = ()
    for item in kept:
        limit = _char_limit(item.kind, policy)
        payload = item.payload
        original = len(payload)
        if limit is not None and original > limit:
            payload = payload[:limit]
            truncations = truncations + (
                TruncationFact(item.kind, original, len(payload)),)
        packed = packed + (
            (item, CompiledSegment(
                kind=item.kind,
                payload=payload,
                provenance=item.provenance,
                validity_at_compile=ContextValidity.VALID)),)
    return packed, truncations


def _apply_total_budget(packed: tuple, policy: CompilePolicy, notes: tuple):
    """全量字符上限：逆优先序确定性丢弃（修订先于前步、尾部先行、
    任务永不丢弃）；任务独超 = 政策矛盾（拒绝）。"""
    if policy.total_char_limit is None:
        return packed, notes
    total = sum(len(pair[1].payload) for pair in packed)
    if total <= policy.total_char_limit:
        return packed, notes
    droppable = sorted(
        ((index, pair) for index, pair in enumerate(packed)
         if pair[0].kind is not ContextItemKind.TASK),
        key=lambda entry: (_KIND_RANK[entry[1][0].kind], entry[0]),
        reverse=True)
    dropped = frozenset()  # 仅成员判定，永不迭代
    for index, pair in droppable:
        if total <= policy.total_char_limit:
            break
        total -= len(pair[1].payload)
        dropped = dropped | frozenset((index,))
        notes = notes + (SelectionNote(pair[0].identity, BUDGET_EXCLUDED),)
    if total > policy.total_char_limit:
        raise CompileModelError(
            "total_char_limit cannot admit the mandatory task content")
    survivors = tuple(
        pair for index, pair in enumerate(packed) if index not in dropped)
    return survivors, notes


def _budget_report(segments: tuple, truncations: tuple) -> BudgetReport:
    """按 kind 秩聚合确定性记账。"""
    counts: tuple = ()
    chars: tuple = ()
    total = 0
    for kind in _KIND_ORDER:
        kind_segments = tuple(
            segment for segment in segments if segment.kind is kind)
        embedded = sum(len(segment.payload) for segment in kind_segments)
        counts = counts + ((kind, len(kind_segments)),)
        chars = chars + ((kind, embedded),)
        total += embedded
    return BudgetReport(
        item_counts_by_kind=counts,
        embedded_chars_by_kind=chars,
        total_embedded_chars=total,
        truncations=truncations)


def compile_context(snapshot, policy=None, *, adjacent_invocation_ids=None):
    """编译唯一入口（纯函数；单通道律）。

    snapshot：ContextSnapshot（唯一事实通道；其他任何载体结构性
    拒绝）。policy：CompilePolicy（None = 默认政策）。adjacent_
    invocation_ids：邻接旁证（不携带内容，只标识紧邻前驱 invocation
    id；缺席 = 首步同构零前步段）。

    返回 CompiledInvocationContext：确定性投影——同 (snapshot,
    policy, 旁证) ⇒ 逐字节同输出。
    """
    if not isinstance(snapshot, ContextSnapshot):
        raise CompileModelError("snapshot must be a ContextSnapshot")
    if policy is None:
        policy = CompilePolicy()
    if not isinstance(policy, CompilePolicy):
        raise CompileModelError("policy must be a CompilePolicy")
    adjacent = _normalize_adjacent(adjacent_invocation_ids)
    kept, notes = _select(snapshot, adjacent)
    ordered = tuple(sorted(kept, key=lambda item: _KIND_RANK[item.kind]))
    budgeted, notes = _apply_item_budget(ordered, policy, notes)
    packed, truncations = _pack(budgeted, policy)
    packed, notes = _apply_total_budget(packed, policy, notes)
    segments = tuple(pair[1] for pair in packed)
    report = _budget_report(segments, truncations)
    return CompiledInvocationContext(
        task_id=snapshot.task_id,
        step_index=snapshot.step_index,
        segments=segments,
        selection_notes=notes,
        budget=report,
        token_measure=TokenMeasure.unknown(),
        policy_fingerprint=policy.fingerprint)
