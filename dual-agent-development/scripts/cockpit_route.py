"""ORCH-5 Dispatch Router —— 派发决策投影层（设计：orch-5-design.md）。

Router 把（组合形状声明， 已验证池事实， 用量历史事实视图， 显式
政策）确定性投影为一份派发推荐（DispatchPlan）。它是纯决策层：
回答「本次应该采用什么派发组合」；不回答「谁有资格执行」（资格/
池准入域）、不回答「该承担什么角色」（角色著作权域）、不创建
调用、不铸造执行身份、不触碰 transport——推荐权 only，执行永远
属既有机器。

固定链（本模块居池门与执行之间；零接线——没有任何调用方）：

    资格链 → 已验证池 →（角色已由组合声明）→ 本模块 → DispatchPlan
    → 既有执行机器（调用/装配/编译/接入均在他层）

字典序政策（每步理由入封闭词表，零神秘分数）：
1. 资格维（硬门）：候选 = 池快照成员 ∧（健康见证注入时）就绪。
   资格/能力判定由池准入链拥有——本层消费既有事实，绝不重造
   第二套资格判断。
2. 形状维：每个已声明席位恰一推荐；席位角色原文透传（本层
   永不裁决角色词表）。
3. 成本维（政策旋钮）：仅政策激活的维度参与，且仅 KNOWN 测量
   参与排序；UNKNOWN 保留候选（排于 KNOWN 之后，绝不排除）；
   UNSUPPORTED=该维度对该候选结构性不供给（维度弃权）。
4. 稳定序维（兜底）：输入序（池快照的 canonical 序）。

成本六概念分列（禁混同）：调用次数/时长=可测事实（事后观察
聚合）；token=三态；货币=结构性 UNSUPPORTED——全系统无价格
事实源，本层零推算零假设价格。UNKNOWN ≠ 0：缺席不是零；把
缺席折算成任何数值都是伪造计量。

三态镜像：CostStatus 复刻观察域三态语义（KNOWN/UNKNOWN/
UNSUPPORTED），本模块零观察域 import（duck 消费调用方聚合的
事实视图——聚合属消费方投影的仓库律）。

非定义链：Router ≠ 资格链 ≠ 能力发现 ≠ 健康检查 ≠ 角色分配
≠ 接入层 ≠ 语义编译层 ≠ Memory 检索 ≠ 执行管线 ≠ 测量基准。
Memory 桥关闭（未来扩展须显式桥，本层零 Memory 知识）。

确定性律：同 (席位, 池快照, 用量视图, 政策) ⇒ 逐字节同推荐——
零时钟、零随机、零新身份铸造、零集合迭代序；唯一稳定序来源=
输入序。DispatchPlan 身份 = (组合指纹, 政策指纹)——皆内容寻址
散列（derived）；既有执行身份（execution/invocation/slot 域）
充足且不被本层触及。
"""
from dataclasses import dataclass
from enum import Enum
import hashlib
import json

__all__ = (
    "RouteModelError",
    "CostStatus",
    "CostDimension",
    "CostMeasure",
    "RuntimeCostFacts",
    "CostFactView",
    "RoutingPolicy",
    "DispatchSeat",
    "DispatchSelection",
    "DispatchPlan",
    "route_composition",
)


class RouteModelError(ValueError):
    """封闭的契约拒绝（INVALID 惯用法沿仓库先例）；message 只含
    字段名与规则，绝不含被拒值。"""


class CostStatus(str, Enum):
    """成本计量三态（镜像观察域三态语义；测量源=事后唯一）。

    KNOWN 携带非负整数测量；UNKNOWN/UNSUPPORTED 不携带。缺证据
    ≠ 零；UNSUPPORTED=该来源结构性不供给该维度。"""

    KNOWN = "KNOWN"
    UNKNOWN = "UNKNOWN"
    UNSUPPORTED = "UNSUPPORTED"


class CostDimension(str, Enum):
    """成本维度封闭词表（恰四值；货币维度结构性无源，激活即弃权）。"""

    INVOCATION_COUNT = "INVOCATION_COUNT"
    LATENCY = "LATENCY"
    TOKEN_TOTAL = "TOKEN_TOTAL"
    MONETARY = "MONETARY"


# 选择理由封闭词表（恰三值）。
STABLE_ORDER = "STABLE_ORDER"
COST_ORDERED = "COST_ORDERED"
COST_UNKNOWN_RETAINED = "COST_UNKNOWN_RETAINED"
COST_UNSUPPORTED_DIMENSION_OFF = "COST_UNSUPPORTED_DIMENSION_OFF"

_SELECTION_REASONS = (
    STABLE_ORDER, COST_ORDERED, COST_UNKNOWN_RETAINED,
    COST_UNSUPPORTED_DIMENSION_OFF)

# 计划级注记封闭词表（恰四值）。
EMPTY_CANDIDATE_POOL = "EMPTY_CANDIDATE_POOL"
NO_HEALTH_WITNESS = "NO_HEALTH_WITNESS"
COST_EVIDENCE_REQUIRED_UNSATISFIED = "COST_EVIDENCE_REQUIRED_UNSATISFIED"

_PLAN_NOTES = (
    EMPTY_CANDIDATE_POOL, NO_HEALTH_WITNESS,
    COST_EVIDENCE_REQUIRED_UNSATISFIED)


def _require_non_empty_string(value, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise RouteModelError(f"{field_name} must be a non-empty string")


def _require_positive_int_or_none(value, field_name: str) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise RouteModelError(f"{field_name} must be a positive int or None")


def _digest(prefix: str, payload: dict) -> str:
    """内容寻址指纹（derived 非 minted）：规范 JSON → 散列前 12 hex。"""
    canonical = json.dumps(
        payload, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return prefix + hashlib.sha256(
        canonical.encode("utf-8")).hexdigest()[:12]


@dataclass(frozen=True)
class CostMeasure:
    """单维度计量值：三态 + 仅 KNOWN 携带非负整数。"""

    status: CostStatus
    value: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, CostStatus):
            raise RouteModelError("status must be a CostStatus")
        if self.status is CostStatus.KNOWN:
            if isinstance(self.value, bool) or \
                    not isinstance(self.value, int) or self.value < 0:
                raise RouteModelError(
                    "value must be a non-negative int when status is KNOWN")
        elif self.value is not None:
            raise RouteModelError("value must be None unless status is KNOWN")

    @classmethod
    def known(cls, value: int) -> "CostMeasure":
        return cls(status=CostStatus.KNOWN, value=value)

    @classmethod
    def unknown(cls) -> "CostMeasure":
        return cls(status=CostStatus.UNKNOWN)

    @classmethod
    def unsupported(cls) -> "CostMeasure":
        return cls(status=CostStatus.UNSUPPORTED)


@dataclass(frozen=True)
class RuntimeCostFacts:
    """单候选 runtime 的四维成本事实（调用方聚合产物，本层只读）。

    手工构造是未来计量源接入面（价格表等）——本层零实现零推算。
    """

    runtime_id: str
    invocation_count: CostMeasure
    mean_latency_ms: CostMeasure
    token_total: CostMeasure
    monetary: CostMeasure

    def __post_init__(self) -> None:
        _require_non_empty_string(self.runtime_id, "runtime_id")
        for field_name in ("invocation_count", "mean_latency_ms",
                           "token_total", "monetary"):
            if not isinstance(getattr(self, field_name), CostMeasure):
                raise RouteModelError(
                    f"{field_name} must be a CostMeasure")

    def measure(self, dimension: CostDimension) -> CostMeasure:
        if dimension is CostDimension.INVOCATION_COUNT:
            return self.invocation_count
        if dimension is CostDimension.LATENCY:
            return self.mean_latency_ms
        if dimension is CostDimension.TOKEN_TOTAL:
            return self.token_total
        return self.monetary


@dataclass(frozen=True)
class CostFactView:
    """用量历史事实视图（聚合属消费方——本视图即消费方投影产物）。

    entries 顺序=构造输入序（确定性：同输入同视图）；缺席 runtime
    的维度查询返回 UNKNOWN（无证据，绝不返回零）。
    """

    entries: tuple

    def __post_init__(self) -> None:
        if not isinstance(self.entries, tuple):
            raise RouteModelError("entries must be a tuple")
        for entry in self.entries:
            if not isinstance(entry, RuntimeCostFacts):
                raise RouteModelError(
                    "entries must hold RuntimeCostFacts values")

    def measure(self, runtime_id: str, dimension: CostDimension) -> CostMeasure:
        """缺席=UNKNOWN（无该 runtime 的历史证据）。"""
        for entry in self.entries:
            if entry.runtime_id == runtime_id:
                return entry.measure(dimension)
        return CostMeasure.unknown()

    @classmethod
    def from_usage_records(cls, records) -> "CostFactView":
        """用量记录鸭（逐调用观察形）→ 聚合视图（纯函数，零观察域 import）。

        duck 读：runtime_id、duration_ms（int|None）、usage 状态串
        （KNOWN/UNKNOWN/UNSUPPORTED）、双 token（int|None）。聚合律：
        - 调用数=记录数（恒 KNOWN——这是观察计数）；
        - 时长=实测记录的整数均值（sum//n；无实测记录但有记录 →
          UNKNOWN）；
        - token=仅 KNOWN 状态且双数在场的记录求和；有记录而无 KNOWN
          → 若存在 UNSUPPORTED 状态记录则 UNSUPPORTED，否则 UNKNOWN；
        - 货币=恒 UNSUPPORTED（结构性无价格源）。
        entries 序=runtime 首次出现序（输入序）。异常形状记录以
        UNKNOWN 诚实处理，绝不抛入聚合（记录域自身已保证形状；
        此处防御性降级）。
        """
        order: tuple = ()
        counts: dict = {}
        latency_sum: dict = {}
        latency_n: dict = {}
        token_sum: dict = {}
        has_known: dict = {}
        has_unsupported: dict = {}
        for record in records:
            runtime_id = getattr(record, "runtime_id", None)
            if not isinstance(runtime_id, str) or not runtime_id.strip():
                continue  # 无诚实身份的记录不进聚合
            if runtime_id not in counts:
                order = order + (runtime_id,)
                counts[runtime_id] = 0
                latency_sum[runtime_id] = 0
                latency_n[runtime_id] = 0
                token_sum[runtime_id] = 0
                has_known[runtime_id] = False
                has_unsupported[runtime_id] = False
            counts[runtime_id] += 1
            duration = getattr(record, "duration_ms", None)
            if isinstance(duration, int) and not isinstance(duration, bool) \
                    and duration >= 0:
                latency_sum[runtime_id] += duration
                latency_n[runtime_id] += 1
            status_text = getattr(
                getattr(record, "usage_status", None), "value",
                getattr(record, "usage_status", None))
            if status_text == "UNSUPPORTED":
                has_unsupported[runtime_id] = True
            input_tokens = getattr(record, "input_tokens", None)
            output_tokens = getattr(record, "output_tokens", None)
            both_ints = all(
                isinstance(item, int) and not isinstance(item, bool)
                for item in (input_tokens, output_tokens))
            if status_text == "KNOWN" and both_ints:
                token_sum[runtime_id] += input_tokens + output_tokens
                has_known[runtime_id] = True
        entries: tuple = ()
        for runtime_id in order:
            if latency_n[runtime_id]:
                latency = CostMeasure.known(
                    latency_sum[runtime_id] // latency_n[runtime_id])
            else:
                latency = CostMeasure.unknown()
            if has_known[runtime_id]:
                tokens = CostMeasure.known(token_sum[runtime_id])
            elif has_unsupported[runtime_id]:
                tokens = CostMeasure.unsupported()
            else:
                tokens = CostMeasure.unknown()
            entries = entries + (RuntimeCostFacts(
                runtime_id=runtime_id,
                invocation_count=CostMeasure.known(counts[runtime_id]),
                mean_latency_ms=latency,
                token_total=tokens,
                monetary=CostMeasure.unsupported()),)
        return cls(entries=entries)


@dataclass(frozen=True)
class RoutingPolicy:
    """派发政策值对象（确定性；恰以单次派发决策为作用域）。

    - cost_dimensions：激活的成本维度（政策声明序=字典序；默认空=
      成本维关闭，纯稳定序）。
    - require_known_cost：显式要求选中者对每个激活维度持 KNOWN
      证据——不满足时不排除候选（保留律），仅在计划注记
      COST_EVIDENCE_REQUIRED_UNSATISFIED 如实披露。
    """

    cost_dimensions: tuple = ()
    require_known_cost: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.cost_dimensions, tuple):
            raise RouteModelError("cost_dimensions must be a tuple")
        for dimension in self.cost_dimensions:
            if not isinstance(dimension, CostDimension):
                raise RouteModelError(
                    "cost_dimensions entries must be CostDimension values")
        if not isinstance(self.require_known_cost, bool):
            raise RouteModelError("require_known_cost must be a bool")

    @property
    def fingerprint(self) -> str:
        """政策指纹（内容寻址，derived）。"""
        return _digest("route_", {
            "cost_dimensions": [
                dimension.value for dimension in self.cost_dimensions],
            "require_known_cost": self.require_known_cost,
        })


@dataclass(frozen=True)
class DispatchSeat:
    """派发席位声明（角色著作权域的投影；角色原文透传，本层零裁决）。"""

    member_id: str
    role: str

    def __post_init__(self) -> None:
        _require_non_empty_string(self.member_id, "member_id")
        _require_non_empty_string(self.role, "role")


@dataclass(frozen=True)
class DispatchSelection:
    """单席位推荐：席位引用 + 候选 runtime + 封闭理由词。"""

    member_id: str
    role: str
    runtime_id: str
    provider_id: str | None
    reason: str

    def __post_init__(self) -> None:
        _require_non_empty_string(self.member_id, "member_id")
        _require_non_empty_string(self.role, "role")
        _require_non_empty_string(self.runtime_id, "runtime_id")
        if self.provider_id is not None:
            _require_non_empty_string(self.provider_id, "provider_id")
        if self.reason not in _SELECTION_REASONS:
            raise RouteModelError(
                "reason must be one of the closed selection vocabulary")


@dataclass(frozen=True)
class DispatchPlan:
    """派发推荐产物：决策投影，非执行生命周期对象。

    恰含：组合指纹、政策指纹、逐席位推荐、成本报告、计划级注记。
    显式排除：执行身份铸造、调用创建、重试/回退语义、会话生命周期、
    语义编译、文本组装、transport——全部属既有他层。零持久化
    （内存值对象）。身份 = (组合指纹, 政策指纹)，皆 derived。"""

    composition_fingerprint: str
    policy_fingerprint: str
    selections: tuple
    cost_report: tuple
    notes: tuple

    def __post_init__(self) -> None:
        _require_non_empty_string(
            self.composition_fingerprint, "composition_fingerprint")
        _require_non_empty_string(
            self.policy_fingerprint, "policy_fingerprint")
        for field_name in ("selections", "cost_report", "notes"):
            if not isinstance(getattr(self, field_name), tuple):
                raise RouteModelError(f"{field_name} must be a tuple")
        for selection in self.selections:
            if not isinstance(selection, DispatchSelection):
                raise RouteModelError(
                    "selections entries must be DispatchSelection values")
        for note in self.notes:
            if note not in _PLAN_NOTES:
                raise RouteModelError(
                    "notes entries must be from the closed plan vocabulary")

    @property
    def identity(self) -> tuple:
        """推荐身份 = (组合指纹, 政策指纹)。"""
        return (self.composition_fingerprint, self.policy_fingerprint)


# ------------------------------------------------------- 派发（纯函数）


def _pool_entry_runtime(entry) -> str:
    runtime_id = getattr(entry, "runtime_id", None)
    if not isinstance(runtime_id, str) or not runtime_id.strip():
        raise RouteModelError(
            "pool snapshot entries must expose a non-empty runtime_id")
    return runtime_id


def _pool_entry_provider(entry) -> str | None:
    provider_id = getattr(entry, "provider_id", None)
    if provider_id is None:
        return None
    if not isinstance(provider_id, str) or not provider_id.strip():
        raise RouteModelError("provider_id must be a non-empty string or None")
    return provider_id


def _is_ready(status_value) -> bool:
    """健康见证判读（duck：对象带 status 或裸字符串；零观察域 import）。"""
    status = getattr(status_value, "status", status_value)
    return getattr(status, "value", status) == "READY"


def _eligible_entries(pool_snapshot, health):
    """资格维硬门：池成员 ∧（健康见证注入时）就绪。输入序保留。"""
    eligible: tuple = ()
    for entry in pool_snapshot:
        _pool_entry_provider(entry)  # 形状校验（诚实拒绝坏形状）
        runtime_id = _pool_entry_runtime(entry)
        if health is not None:
            witness = health.get(runtime_id)
            if witness is None or not _is_ready(witness):
                continue
        eligible = eligible + ((entry, runtime_id),)
    return eligible


def _cost_key(runtime_id, dimensions, usage: CostFactView):
    """字典序键（政策声明序）：KNOWN → (0, 测量值)；否则 → (1, 0)。

    UNKNOWN/UNSUPPORTED 同居 (1, 0)——保留且不参与值序；区分由
    理由词承担（COST_UNKNOWN_RETAINED / COST_UNSUPPORTED_DIMENSION_OFF）。
    """
    key: tuple = ()
    for dimension in dimensions:
        measure = usage.measure(runtime_id, dimension)
        if measure.status is CostStatus.KNOWN:
            key = key + ((0, measure.value),)
        else:
            key = key + ((1, 0),)
    return key


def _selection_reason(runtime_id, dimensions, usage: CostFactView,
                      cost_keys) -> str:
    """封闭理由词裁决（见模块 docstring 字典序政策）。"""
    if not dimensions:
        return STABLE_ORDER
    own = cost_keys[runtime_id]
    others_different = any(
        cost_keys[other] != own for other in cost_keys if other != runtime_id)
    has_known = any(
        usage.measure(runtime_id, dimension).status is CostStatus.KNOWN
        for dimension in dimensions)
    if has_known:
        return COST_ORDERED if others_different else STABLE_ORDER
    measures = [usage.measure(runtime_id, dimension)
                for dimension in dimensions]
    if any(m.status is CostStatus.UNSUPPORTED for m in measures):
        return COST_UNSUPPORTED_DIMENSION_OFF
    return COST_UNKNOWN_RETAINED


def _cost_report(selections, dimensions, usage: CostFactView) -> tuple:
    """成本报告：选中 runtime × 激活维度的三态计量快照（去重保序）。"""
    seen: tuple = ()
    report: tuple = ()
    for selection in selections:
        if selection.runtime_id in seen:
            continue
        seen = seen + (selection.runtime_id,)
        measures = tuple(
            (dimension, usage.measure(selection.runtime_id, dimension))
            for dimension in dimensions)
        report = report + ((selection.runtime_id, measures),)
    return report


def route_composition(seats, pool_snapshot, *, health=None, usage=None,
                      policy=None):
    """派发唯一入口（纯函数；建议权 only）。

    seats：DispatchSeat 序列（角色著作权域声明，原文透传）。
    pool_snapshot：已验证池只读快照（成员须暴露 runtime_id，
    provider_id 可选——三属性协议同形）。health：注入的健康见证
    （runtime_id → 状态；缺席=None ⇒ 无当前见证，以池成员为准并
    注记 NO_HEALTH_WITNESS）。usage：CostFactView（None ⇒ 空视图，
    一切成本维度 UNKNOWN）。policy：RoutingPolicy（None ⇒ 默认）。

    返回 DispatchPlan：确定性推荐——同输入 ⇒ 逐字节同输出。
    """
    if policy is None:
        policy = RoutingPolicy()
    if not isinstance(policy, RoutingPolicy):
        raise RouteModelError("policy must be a RoutingPolicy")
    if usage is None:
        usage = CostFactView(entries=())
    if not isinstance(usage, CostFactView):
        raise RouteModelError("usage must be a CostFactView")
    seat_values = tuple(seats)
    for seat in seat_values:
        if not isinstance(seat, DispatchSeat):
            raise RouteModelError("seats entries must be DispatchSeat values")
    member_ids = tuple(seat.member_id for seat in seat_values)
    if len(member_ids) != len(set(member_ids)):
        raise RouteModelError("duplicate member_id in seats")
    if health is not None and not hasattr(health, "get"):
        raise RouteModelError("health must expose .get(runtime_id)")
    pool_values = tuple(pool_snapshot)

    notes: tuple = ()
    composition_fingerprint = _digest("composition_", {
        "seats": [[seat.member_id, seat.role] for seat in seat_values],
    })

    eligible = _eligible_entries(pool_values, health)
    if health is None and pool_values:
        notes = notes + (NO_HEALTH_WITNESS,)

    selections: tuple = ()
    if not eligible:
        if seat_values:
            notes = notes + (EMPTY_CANDIDATE_POOL,)
    else:
        dimensions = policy.cost_dimensions
        cost_keys = {
            runtime_id: _cost_key(runtime_id, dimensions, usage)
            for _, runtime_id in eligible}
        ranked = sorted(
            eligible,
            key=lambda pair: cost_keys[pair[1]] + (pair[1],))
        for seat in seat_values:
            chosen_entry, chosen_runtime = ranked[0]
            selections = selections + (DispatchSelection(
                member_id=seat.member_id,
                role=seat.role,
                runtime_id=chosen_runtime,
                provider_id=_pool_entry_provider(chosen_entry),
                reason=_selection_reason(
                    chosen_runtime, dimensions, usage, cost_keys),),)

    if policy.require_known_cost and policy.cost_dimensions and selections:
        unsatisfied = any(
            usage.measure(selection.runtime_id, dimension).status
            is not CostStatus.KNOWN
            for selection in selections
            for dimension in policy.cost_dimensions)
        if unsatisfied:
            notes = notes + (COST_EVIDENCE_REQUIRED_UNSATISFIED,)

    return DispatchPlan(
        composition_fingerprint=composition_fingerprint,
        policy_fingerprint=policy.fingerprint,
        selections=selections,
        cost_report=_cost_report(
            selections, policy.cost_dimensions, usage),
        notes=notes)
