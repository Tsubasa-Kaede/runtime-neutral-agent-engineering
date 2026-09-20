"""CU-CONTEXT-2 Collaboration Memory —— 语义层（设计：cu-context-2-design.md）。

Memory 是系统为未来检索而显式选择保留的协作事实记录集合：每条记录
是对已完成协作（run）终态事实的不可变、只读、带 scope / lifetime /
provenance 语义的引用。Memory 独立于任何具体 invocation 的 Context
构建——Memory 可以存在而不进入任何 Context；Context 可以完全不依赖
Memory 而由 run-local 事实构建。

概念链（本模块只钉 ①②③ 三级语义；④ 缝保持关闭）：

    Existing Fact（run 终态事实，经呈现摘要只读聚合）
        ↓ ① 显式转换边界（candidate_from_run_summary）
    MemoryCandidate（保留资格裁决通过；尚未铸造，非持久事实）
        ↓ ② 铸造（mint_record：授予 scope；内容寻址身份）
    MemoryRecord（不可变、只读）
        ↓ ③ 确定性检索（retrieve_records：封闭结构过滤）
    候选 MemoryRecord 集合
        ↓ ④ 【结构性缺席：须同时修订 CU-CONTEXT-1 源宇宙与 2.8-E
             零携带负向闸才可开启——本模块与 Context 语义模块互相
             零 import】
    （未来）Context Item → Snapshot → Compiler → Invocation

零接线声明：本模块是纯语义库——没有任何调用方，不 import 入口层/
呈现层/引擎/Context 任何模块，零文件 IO，零持久化。Memory Store
（USER scope 的未来载体，形态学 = qualification evidence 先例）与
Record→Context 桥（④ 缝）均为显式延期，非本模型组成部分。

保留资格（封闭，恰一源）：
- RUN_OUTCOME（run 级终态摘要；canonical source = ConversationRecord
  形呈现摘要，经显式转换边界 duck 读取；恰 COMPLETED 终态可铸——
  FAILED / ABORTED / PARKED 不具自动保留资格，失败排除 = 双先例：
  qualification 持久层拒存失败、packet 取证取走即清）。

非定义链：Memory ≠ Context ≠ Trace ≠ Usage ≠ Provenance ≠ Execution
State ≠ Conversation History ≠ Communication Method。排除中间输出 /
handoff / revision / RunState / 个体 trace / 个体 usage / 失败细节 /
组合分组面 / 成员席位 / V2 facade 结果。组合分组面 → Memory 零直接
语义（M0 不等式向 Memory 域的扩展）。

身份（零新 identity primitive）：记录身份 = (scope, 内容寻址散列)。
散列 derived 非 minted（同内容 → 同散列；同 task 重跑异果 → 异记录
合法并存）。task_id = 关联键，绝非全局身份（不透明任务 id 同文同值
跨进程撞号已证；会话内消解不跨进程成立）。SESSION scope 的 owner =
进程边界（载体结构性承担，零数据字段）；USER scope 的 owner = 未来
Store 的显式 base_dir（现有用户主目录事实）。记录无时钟（系统无
可靠时间源）：排序 = 铸造序（调用方容器序），检索保序，绝不编造
时间戳。

有效性：构造期接受即恒真（历史事实成立，不因 Context 前进而失效，
不因修订消费而删除）；INVALID = 仅构造期拒绝；零运行态失效模型
（无存活期、无过期、无隐式失效迁移——Context 式三态不复用）。
"""
from dataclasses import dataclass
from enum import Enum
import hashlib
import json

__all__ = (
    "MemoryModelError",
    "MemoryScope",
    "MemorySourceKind",
    "MemoryCandidate",
    "MemoryRecord",
    "MemoryProvenance",
    "candidate_from_run_summary",
    "mint_record",
    "retrieve_records",
)


class MemoryModelError(ValueError):
    """封闭的契约拒绝（INVALID）；message 只含字段名与规则，绝不含被拒值。"""


class MemoryScope(str, Enum):
    """Memory 作用域封闭词表（恰两值；扩展 = 设计修订）。

    SESSION = 进程内跨 run（owner = 进程边界，由载体结构性承担，
    零数据字段）；USER = 用户级跨进程（owner = 未来 Store 的显式
    base_dir；本阶段无 Store 载体，仅词表语义）。RUN / INVOCATION
    排除：run 内全量保留已由观察域承担；invocation 级保留即
    Context 准入——皆非 Memory 的存在理由（Memory 始于 run 之上）。
    """

    SESSION = "SESSION"
    USER = "USER"


class MemorySourceKind(str, Enum):
    """保留来源封闭词表（恰一值；扩展 = 设计修订）。

    唯一自动资格级 = run 级终态摘要（RUN_OUTCOME）。中间输出 /
    handoff / revision / RunState / 个体 trace / 个体 usage / 失败
    细节 / 组合分组面 / 成员席位均不具资格。未来扩展位（如用户显式
    钉存）须新授权 + 词表修订。
    """

    RUN_OUTCOME = "RUN_OUTCOME"


def _require_non_empty_string(value, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise MemoryModelError(f"{field_name} must be a non-empty string")


def _require_usage_quad(value, field_name: str) -> None:
    if not isinstance(value, tuple) or len(value) != 4:
        raise MemoryModelError(
            f"{field_name} must be a 4-tuple (known_input, known_output, "
            "unknown_count, unsupported_count)")
    for count in value:
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise MemoryModelError(
                f"{field_name} entries must be non-negative ints")


@dataclass(frozen=True)
class MemoryCandidate:
    """显式转换边界的输出：通过保留资格裁决的 run 终态事实引用。

    Candidate ≠ Record：本体只是「可保留性」的载体——尚未铸造、
    不属于持久 Memory，检索面不得将其作为事实操作。字段全部直读
    呈现摘要所载（duck 读取，零入口层 import）；final_preview 为
    呈现层截断预览——Memory 忠实引用摘要所载事实，绝不虚构持有
    全文（全文引用属未来转换边界修订裁决）。producer 为描述性
    属性（绝不参与身份判定）。
    """

    source_kind: MemorySourceKind
    task_text: str
    outcome_status: str
    final_preview: str
    steps: tuple
    task_id: str
    usage: tuple
    producer: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.source_kind, MemorySourceKind):
            raise MemoryModelError("source_kind must be a MemorySourceKind")
        _require_non_empty_string(self.task_text, "task_text")
        _require_non_empty_string(self.outcome_status, "outcome_status")
        if not isinstance(self.final_preview, str):
            raise MemoryModelError("final_preview must be a string")
        if not isinstance(self.steps, tuple):
            raise MemoryModelError("steps must be a tuple")
        for step in self.steps:
            if not (isinstance(step, tuple) and len(step) == 2
                    and all(isinstance(part, str) and part
                            for part in step)):
                raise MemoryModelError(
                    "steps entries must be (role, runtime_id) string pairs")
        _require_non_empty_string(self.task_id, "task_id")
        _require_usage_quad(self.usage, "usage")
        if self.producer is not None:
            _require_non_empty_string(self.producer, "producer")


@dataclass(frozen=True)
class MemoryProvenance:
    """记录来源描述符（≠记录内容；描述性，绝不参与身份判定）。

    字段全部直读既有事实：source_kind（恰 RUN_OUTCOME）、task_id
    （关联键，非全局身份——跨进程撞号容忍并列）、composition
    （交付序快照，描述性——组合形状不进身份）、usage（三态聚合
    投影：仅 KNOWN 计量求和的既有呈现值）、producer（描述性角色）。
    零时间字段：系统无可靠时间源，诚实缺席优于编造。
    """

    source_kind: MemorySourceKind
    task_id: str
    composition: tuple
    usage: tuple
    producer: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.source_kind, MemorySourceKind):
            raise MemoryModelError("source_kind must be a MemorySourceKind")
        _require_non_empty_string(self.task_id, "task_id")
        for step in self.composition:
            if not (isinstance(step, tuple) and len(step) == 2
                    and all(isinstance(part, str) and part
                            for part in step)):
                raise MemoryModelError(
                    "composition entries must be (role, runtime_id) pairs")
        _require_usage_quad(self.usage, "usage")
        if self.producer is not None:
            _require_non_empty_string(self.producer, "producer")


@dataclass(frozen=True)
class MemoryRecord:
    """铸造后的不可变保留记录 = Candidate + scope + 内容寻址身份。

    只读：不能反向修改 source fact（candidate 以值内嵌为快照）。
    有效性 = 构造期接受即恒真；无存活期 / 过期 / 隐式失效迁移（零
    运行态失效模型）。排序契约 = 调用方铸造序（本模块零时钟）。
    这不是 God Object：显式排除 invocation 表示、trace、usage 个体、
    控制事实、跨进程存储职责。
    """

    scope: MemoryScope
    candidate: MemoryCandidate

    def __post_init__(self) -> None:
        if not isinstance(self.scope, MemoryScope):
            raise MemoryModelError("scope must be a MemoryScope")
        if not isinstance(self.candidate, MemoryCandidate):
            raise MemoryModelError("candidate must be a MemoryCandidate")

    @property
    def identity(self) -> tuple:
        """记录身份 = (scope, 内容寻址散列)——derived 非 minted。

        散列只对 candidate 内容计算：同内容跨 scope 同散列但身份
        不同（scope 是身份成分；内容散列绝不单独作跨 scope 身份）。
        """
        return (self.scope, _content_digest(self.candidate))

    @property
    def provenance(self) -> MemoryProvenance:
        """来源描述视图（直读 candidate 所载既有事实，零新铸）。"""
        return MemoryProvenance(
            source_kind=self.candidate.source_kind,
            task_id=self.candidate.task_id,
            composition=self.candidate.steps,
            usage=self.candidate.usage,
            producer=self.candidate.producer)


def _content_digest(candidate: MemoryCandidate) -> str:
    """candidate 内容的确定性散列（qualification 持久层同型形态学）。

    输入 = 字段投影的规范 JSON（sort_keys + 紧凑分隔符 + ensure_
    ascii；producer 为描述性属性，不入散列）；输出 = "memory_" +
    sha256 前 12 位十六进制（与既有不透明任务 id 同形态：hex 词表、
    无 secret 形状子串、零时间 / 随机 / 路径依赖——同内容必产同
    散列）。json 在本模块仅作散列规范化输入，绝非序列化协议 /
    持久化 / 传输格式。
    """
    payload = {
        "source_kind": candidate.source_kind.value,
        "task_text": candidate.task_text,
        "outcome_status": candidate.outcome_status,
        "final_preview": candidate.final_preview,
        "steps": [list(step) for step in candidate.steps],
        "task_id": candidate.task_id,
        "usage": list(candidate.usage),
    }
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    return f"memory_{digest}"


# ------------------------------------------------- 呈现摘要 → Candidate 桥

_ELIGIBLE_OUTCOME_STATUS = "COMPLETED"  # RunStatus 权威词（管线四值词表）


def candidate_from_run_summary(summary, *, producer=None):
    """呈现摘要（ConversationRecord 形鸭）→ MemoryCandidate。

    唯一显式转换边界（设计 ①）：调用侧的显式动作，绝不是观察副
    作用。资格裁决（封闭）：
    - status 必须为终态权威词且恰 COMPLETED——PARKED 形（None /
      非终局词）结构性不可达保留；FAILED / ABORTED 不具自动保留
      资格（失败排除律；失败经验属未来用户显式钉存扩展位，须词表
      修订 + 新授权）。
    - task / task_id 必须非空（事实缺席 = 拒绝，绝不编造）。
    duck 读取（零入口层 import）：.task .steps .status .final_
    preview .usage .task_id；usage duck 读四计数字段，缺席计零
    （诚实零聚合）。源对象零改动。
    """
    if producer is not None:
        _require_non_empty_string(producer, "producer")
    task_text = getattr(summary, "task", None)
    status = getattr(summary, "status", None)
    task_id = getattr(summary, "task_id", None)
    steps_raw = getattr(summary, "steps", None)
    final_preview = getattr(summary, "final_preview", None)
    usage_raw = getattr(summary, "usage", None)
    _require_non_empty_string(task_text, "summary task")
    _require_non_empty_string(task_id, "summary task_id")
    _require_non_empty_string(status, "summary status")
    if status != _ELIGIBLE_OUTCOME_STATUS:
        raise MemoryModelError(
            "run summary is not eligible for automatic retention: "
            "outcome status must be COMPLETED")
    if final_preview is None:
        final_preview = ""
    if not isinstance(final_preview, str):
        raise MemoryModelError("summary final_preview must be a string")
    usage = (
        int(getattr(usage_raw, "known_input", 0) or 0),
        int(getattr(usage_raw, "known_output", 0) or 0),
        int(getattr(usage_raw, "unknown_count", 0) or 0),
        int(getattr(usage_raw, "unsupported_count", 0) or 0),
    )
    return MemoryCandidate(
        source_kind=MemorySourceKind.RUN_OUTCOME,
        task_text=task_text,
        outcome_status=status,
        final_preview=final_preview,
        steps=tuple(steps_raw or ()),
        task_id=task_id,
        usage=usage,
        producer=producer)


# --------------------------------------------------------- 铸造（②）


def mint_record(candidate, scope):
    """铸造：MemoryCandidate → MemoryRecord（授予 scope + 身份）。

    Candidate ≠ Record 的唯一晋升面；调用方持铸造序（本模块零
    时钟，排序契约 = 容器序）。铸造后不可变、只读，不能反向修改
    source fact。
    """
    if not isinstance(candidate, MemoryCandidate):
        raise MemoryModelError("candidate must be a MemoryCandidate")
    if not isinstance(scope, MemoryScope):
        raise MemoryModelError("scope must be a MemoryScope")
    return MemoryRecord(scope=scope, candidate=candidate)


# ------------------------------------------------- 确定性检索（③）


def retrieve_records(records, *, scope=None, task_id=None, status=None,
                     member=None):
    """确定性检索（封闭结构过滤，零相关性模型；设计 ③）。

    只操作 MemoryRecord（Candidate 非持久事实，结构性拒绝）。过滤
    词表封闭恰四：scope / task_id（关联键，容忍并列——同 task_id
    多记录全部返回，绝不合并）/ status / member（(role, runtime_id)
    精确成员匹配，描述性组合事实）。多条件 AND；输出保持输入序
    （= 铸造序；确定性，零重排零时钟）。检索 ≠ 编译：本函数止步
    于候选集合，绝不构造任何 invocation 表示（④ 缝保持关闭）。
    """
    if scope is not None and not isinstance(scope, MemoryScope):
        raise MemoryModelError("scope filter must be a MemoryScope or None")
    if task_id is not None:
        _require_non_empty_string(task_id, "task_id filter")
    if status is not None:
        _require_non_empty_string(status, "status filter")
    if member is not None and not (
            isinstance(member, tuple) and len(member) == 2
            and all(isinstance(part, str) and part for part in member)):
        raise MemoryModelError(
            "member filter must be a (role, runtime_id) string pair")
    verified = tuple(records)
    for record in verified:
        if not isinstance(record, MemoryRecord):
            raise MemoryModelError(
                "retrieval operates on records only: candidates are not "
                "persisted facts")
    return tuple(
        record for record in verified
        if (scope is None or record.scope is scope)
        and (task_id is None or record.candidate.task_id == task_id)
        and (status is None
             or record.candidate.outcome_status == status)
        and (member is None or member in record.candidate.steps))
