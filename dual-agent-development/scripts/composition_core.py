"""2.8.0 Composition Core (M1): pure composition domain.

M0 ARCHITECTURE LOCK 的 M1 落地——只做 validate/resolve 两个纯函数门：

- CompositionIntent   = 用户声明（zero execution authority）
- ResolvedComposition = 池门裁决后的 configuration（非 execution state）
- steps = members 声明序 + binding 派生，是 execution order 的唯一
  来源；groups 只是 run-local 分组元数据，永不进入任何派生链
  （Group ≠ Order/Scheduler/DAG/Runtime lifecycle/Session/Transport）
- 池门零替换：点名 runtime ∉ verified pool → CompositionError
  (RUNTIME_NOT_QUALIFIED)——零替补、零猜测、零 automatic fallback，
  bindings 绝不包含用户未点名的 runtime

值对象（CompositionBinding/CompositionError）自 cockpit_entry 只读
复用（零第二真源）；角色词表自 cockpit_projection 冻结表取词集；
qualify hint 经 host_entry 惰性取原文（host_entry ↔ cockpit_entry
既有循环纪律，_host_entry() 同型——M3 入口接线时零新增环）。

本模块零 IO/零时钟/零随机/零 runtime 名分支；Identity 三元
（Runtime/Role/Agent-member）永不混同（M0 ERRATA-2）。
"""
from __future__ import annotations

from typing import Mapping, NamedTuple

# 单一 module graph 纪律（P1-U4 先例，同款注释见 cockpit_entry /
# host_entry；2.4.2-A）：平铺名在两种模式下都解析；相对拼写仅作无
# shim 嵌入场景的回退。
try:  # flat-import mode (source tree/tests; installed: dual_agent shim)
    from cockpit_entry import CompositionBinding, CompositionError
    from cockpit_projection import DEFAULT_ROLE_TEMPLATES
except ImportError:  # pragma: no cover - embedded fallback
    from .cockpit_entry import CompositionBinding, CompositionError
    from .cockpit_projection import DEFAULT_ROLE_TEMPLATES


# --- Role 封闭词表（M0：源=投影层冻结表词集，零第二真源） ---
ROLE_VOCABULARY = frozenset(
    role for template in DEFAULT_ROLE_TEMPLATES.values()
    for role in template)

# CompositionError.reason 的封闭词表（文档集；RUNTIME_NOT_QUALIFIED
# 与 cockpit_entry 既有协议同词——非本域新造）。
COMPOSITION_ERROR_REASONS = frozenset({
    "UNKNOWN_ROLE",
    "INVALID_MEMBER_COUNT",
    "INVALID_MEMBER_ID",
    "DUPLICATE_MEMBER_ID",
    "BINDING_MEMBER_UNKNOWN",
    "MISSING_BINDING",
    "INVALID_GROUP_ID",
    "DUPLICATE_GROUP_ID",
    "GROUP_MEMBER_UNKNOWN",
    "MEMBER_IN_MULTIPLE_GROUPS",
    "RUNTIME_NOT_QUALIFIED",
})


class AgentSpec(NamedTuple):
    """组合成员席位（Agent Identity = composition-local member_id）。

    member_id 在单个 Composition 内唯一；绝不跨 run 持久、绝不与
    runtime identity 混同。duplicate Role 合法（M0 ERRATA-2）。
    """

    member_id: str
    role: str


class RuntimeBindingRequest(NamedTuple):
    """意图层绑定请求：仅点名 runtime_id——零身份、零资格、零执行效应。"""

    runtime_id: str


class CollaborationGroupSpec(NamedTuple):
    """run-local 分组元数据：只表达成员归属（M0 ERRATA-1 六不等式）。"""

    group_id: str
    member_ids: tuple


class CompositionIntent(NamedTuple):
    """用户组合声明（zero execution authority）。

    members 的声明序即 steps 派生序（execution order 唯一来源）；
    binding_requests 应传不可变映射（成员→请求，恰一对应，由
    validate 强制）；groups 为分区（每成员至多一组）。
    """

    members: tuple
    binding_requests: Mapping
    groups: tuple


class ResolvedComposition(NamedTuple):
    """池门裁决后的可执行配置快照——configuration，非 execution state。

    不含 plan/slot/session/run_state；plan 仍由入口装配层
    （cockpit_entry，M3 受控接线）从 steps 派生。
    groups 透传仅供呈现层分组渲染（止步于投影）。
    """

    bindings: tuple
    member_ids: tuple
    groups: tuple
    steps: tuple


def _qualify_hint():
    """Late-bound qualify hint 原文（与 cockpit_entry._host_entry() 同型）。

    host_entry 顶层 import cockpit_entry（既有 dispatch 环），故不可
    模块级反向 import；到 resolve 运行时 host_entry 必已初始化。
    """
    import host_entry
    return host_entry._HINT_QUALIFY


def validate_composition(intent):
    """结构/词表门：CompositionError | None（首错即停，入口同风格）。

    只校验形状与封闭词表——零 discovery、零 filesystem、零 runtime
    执行（M0 lifecycle 的 validated 阶段；池门归 resolve）。
    duplicate Role 与多成员点名同一 runtime 均合法（ERRATA-2）。
    """
    members = tuple(intent.members)
    if not 2 <= len(members) <= 4:
        return CompositionError(
            "INVALID_MEMBER_COUNT",
            f"composition needs 2-4 members (found {len(members)})",
            None)
    member_ids = set()
    for spec in members:
        member_id = spec.member_id
        if not isinstance(member_id, str) or not member_id.strip():
            return CompositionError(
                "INVALID_MEMBER_ID",
                f"member_id must be a non-empty string "
                f"(got {member_id!r})",
                None)
        if member_id in member_ids:
            return CompositionError(
                "DUPLICATE_MEMBER_ID",
                f"member_id {member_id} declared twice",
                None)
        member_ids.add(member_id)
        if spec.role not in ROLE_VOCABULARY:
            return CompositionError(
                "UNKNOWN_ROLE",
                f"member {member_id}: role {spec.role!r} not in closed "
                f"vocabulary {sorted(ROLE_VOCABULARY)}",
                None)
    requests = dict(intent.binding_requests)
    unknown = sorted(set(requests) - member_ids)
    if unknown:
        return CompositionError(
            "BINDING_MEMBER_UNKNOWN",
            f"binding requests reference unknown members: {unknown}",
            None)
    missing = sorted(member_ids - set(requests))
    if missing:
        return CompositionError(
            "MISSING_BINDING",
            f"members without a runtime binding request: {missing}",
            None)
    group_ids = set()
    owner = {}
    for group in intent.groups:
        group_id = group.group_id
        if not isinstance(group_id, str) or not group_id.strip():
            return CompositionError(
                "INVALID_GROUP_ID",
                f"group_id must be a non-empty string (got {group_id!r})",
                None)
        if group_id in group_ids:
            return CompositionError(
                "DUPLICATE_GROUP_ID",
                f"group_id {group_id} declared twice",
                None)
        group_ids.add(group_id)
        for member_id in group.member_ids:
            if member_id not in member_ids:
                return CompositionError(
                    "GROUP_MEMBER_UNKNOWN",
                    f"group {group_id} references unknown member "
                    f"{member_id!r}",
                    None)
            if member_id in owner:
                return CompositionError(
                    "MEMBER_IN_MULTIPLE_GROUPS",
                    f"member {member_id} belongs to groups "
                    f"{owner[member_id]!r} and {group_id!r}",
                    None)
            owner[member_id] = group_id
    return None


def resolve_composition(intent, verified_pool_snapshot):
    """池门裁决：ResolvedComposition | CompositionError。

    消费恰两输入：CompositionIntent + verified pool snapshot（池项
    协议 = runtime_id/provider_id/identity 三属性，与
    cockpit_entry._verified_pool 快照同形）。点名 runtime ∉ 池 →
    RUNTIME_NOT_QUALIFIED（附 verified 列表 + qualify hint 原文）——
    零替换零替补零猜测；identity 原值透传（CompositionBinding
    契约：不计算、不派生、不重造）。内部先 validate（结构错误在
    池门之前拦截，M0 lifecycle declared→validated→resolved）。
    """
    problem = validate_composition(intent)
    if problem is not None:
        return problem
    pool = {entry.runtime_id: entry
            for entry in verified_pool_snapshot}
    verified = ", ".join(sorted(pool)) or "(none)"
    bindings = []
    for spec in intent.members:
        request = intent.binding_requests[spec.member_id]
        entry = pool.get(request.runtime_id)
        if entry is None:
            return CompositionError(
                "RUNTIME_NOT_QUALIFIED",
                f"runtime {request.runtime_id} not in VERIFIED pool "
                f"(verified: {verified})",
                _qualify_hint())
        bindings.append(CompositionBinding(
            role=spec.role,
            runtime_id=entry.runtime_id,
            provider_id=entry.provider_id,
            canonical_runtime_identity=entry.identity))
    return ResolvedComposition(
        bindings=tuple(bindings),
        member_ids=tuple(spec.member_id for spec in intent.members),
        groups=tuple(intent.groups),
        steps=tuple((binding.role, binding.runtime_id)
                    for binding in bindings))


def default_composition_to_intent(verified_pool_snapshot):
    """默认组合 → 预填 CompositionIntent（convenience，非新 authority）。

    复刻 resolve_default_composition 的 canonical 绑定序（sorted
    runtime_id + 投影层冻结角色模板 zip），反向构造 intent；等值环
    （resolve(default_composition_to_intent(pool), pool).bindings ≡
    resolve_default_composition(pool).bindings）由契约测试钉死（M2
    对真实入口函数对拍）。池 <2 时产出的 intent 过不了 validate
    （INVALID_MEMBER_COUNT）——与 default 路径的诚实 BLOCKED 同向，
    本函数零重复 BLOCKED 逻辑。
    """
    entries = tuple(sorted(verified_pool_snapshot,
                           key=lambda entry: entry.runtime_id))
    if len(entries) < 2:
        # 与 default 路径 BLOCKED 同向：池不足不虚构预填（零重复
        # BLOCKED 文案），产出的空 intent 由 validate 诚实拒绝。
        roles = ()
    else:
        roles = DEFAULT_ROLE_TEMPLATES[min(4, len(entries))]
    members = tuple(
        AgentSpec(member_id=f"a{i + 1}", role=role)
        for i, role in enumerate(roles))
    binding_requests = {
        spec.member_id: RuntimeBindingRequest(runtime_id=entry.runtime_id)
        for spec, entry in zip(members, entries)}
    return CompositionIntent(
        members=members,
        binding_requests=binding_requests,
        groups=())
