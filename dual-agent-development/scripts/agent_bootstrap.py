"""V3.0-G: Agent team bootstrap — E→F→D 的第一个真正消费者。

V3.0-E（绑定 liveness 投影）与 V3.0-F（能力背书视图）至今零生产消费
者（Boundary Review 实测：死层债务 = 2）。本模块是它们的第一个消费
者：把「调用方选定的 agent 队伍 + 注入的发现 callable / 证据 / 健康
快照」串成一条组队入口，回答«这支队伍现在能组起来执行吗？为什么
不能？»——纯编排，不新增任何裁决：

    AgentRegistry + agent_ids + 注入的 discover_runtime/evidence/health
      ↓ E discover_agent_bindings      （绑定 liveness 预检）
      ↓ F project_agent_capabilities   （能力背书预检，全绿才进行）
      ↓ D build_facade_from_agents     （全绿才调用；raising 权威不变）
    AgentTeamBootstrap   <- entries（per-agent 事实）+ (facade, attribution)

Invariants（locked by tests/test_agent_bootstrap.py）：
- 纯编排，不新增裁决：探测由注入的 discover_runtime 完成（异常收敛
  沿用 E）；证据与健康快照由调用方注入，本模块绝不探测、不产出证据、
  不调用 adapter_factory、不经手 admission。V3.0-D 仍是组合期 raising
  裁决权威——预检全绿后 D 的拒绝（多余协作证据、缺健康、混合
  provenance、未知 agent、覆盖缺口、地址塌缩等）原样上抛，绝不吞成
  结构化结果，也绝不重复其裁决词表。
- 非 raising 边界仅限预检可预见的事实：绑定不可发现（E 的受控
  reason 逐字携带）与词表内声明未获 runtime 背书（F 的 DECLARED_ONLY
  投影，含证据缺失与非 VERIFIED 的空 validated 集）。BEYOND_VOCABULARY
  是诚实边界不是失败（与 V3.0-F 一致；组合语义由 V3.0-C/D 决定）——
  本模块不得比 D 严。
- 引用不复制：entries 引用 E 候选与 F 视图的事实，绝不重算第二套；
  成功路径的 (facade, attribution) 就是 V3.0-D 的原样返回物。
- 声明分层不合并：entry 只携带 discovered（E）+ backing（F 投影）+
  addresses（组合事实）；绝无 verified/trusted/admitted/status 字段，
  生命周期各归其 artifact。
- 探测范围 = 队伍范围：预检在仅含选定 agent 的 registry 视图上进行
  （manifest 引用组成，绝不复制声明），非队伍声明的 runtime 不被探测；
  组合仍使用调用方的原 registry。未知 / 重复 agent_id 不在预检裁决
  （组合层 C/D 的职责），原样留给 D。
- 确定性：entries 按 agent_id 排序、addresses 按字典序；同输入必同
  输出。runtime-neutral + agent-neutral：零 runtime/provider/model 名、
  零分支；事实只来自注入。

NOT implemented here（later V3 phases）：自动 qualification（V2
bootstrap_runtime_session 的 agent 侧等价物——一 runtime 多 agent 各有
factory，«runtime 的 adapter»无唯一诚实答案，见 Boundary Review 的
adapter 粒度陷阱）、agent 级 verification、trust、admission、选择、
role assignment、orchestration、remote、A2A、persistence、UI、CLI
接线、Observation 扩展（组队属 pre-execution，无执行事件）。
"""
from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Any, Mapping

from agent_capability import (
    CapabilityBacking,
    DeclaredCapability,
    project_agent_capabilities,
)
from agent_discovery import discover_agent_bindings
from agent_host import build_facade_from_agents
from agent_manifest import AgentRegistry


@dataclass(frozen=True)
class AgentTeamEntry:
    """一个选定 agent 的组队预检事实（引用不复制，无生命周期断言）。

    字段词表封闭为六项（entry_field_names 反射锁定）：agent_id（WHO）、
    runtime_identity（WHERE，绑定四元组逐字透传）、discovered（E 的绑定
    liveness）、reason（预检失败的受控机器理由；成功为 None）、backing
    （F 的 per-role 投影事实；空元组 = 能力预检未进行——发现预检先行
    失败的队伍级短路，绝不误读为「无声明」）、addresses（组合成功时该
    agent 的 compat 槽位地址，按字典序；未组成为空元组——队伍级组合
    是全有或全无，per-agent 事实照常独立报告）。
    """

    agent_id: str
    runtime_identity: tuple
    discovered: bool
    reason: str | None
    backing: tuple[DeclaredCapability, ...]
    addresses: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.agent_id, str) or not self.agent_id:
            raise ValueError("agent_id must be a non-empty string")
        if not isinstance(self.discovered, bool):
            raise ValueError("discovered must be a bool")
        if self.reason is not None and (
                not isinstance(self.reason, str) or not self.reason):
            raise ValueError("reason must be None or a non-empty string")
        for name in ("runtime_identity", "backing", "addresses"):
            object.__setattr__(self, name, tuple(getattr(self, name)))
        for address in self.addresses:
            if not isinstance(address, str) or not address:
                raise ValueError("addresses items must be non-empty strings")
        if not self.discovered and self.reason is None:
            raise ValueError("reason must be set when discovered is False")
        if self.addresses:
            if self.reason is not None:
                raise ValueError(
                    "reason must be None when the agent was composed")
            if not self.discovered:
                raise ValueError(
                    "discovered must be True when the agent was composed")


@dataclass(frozen=True)
class AgentTeamBootstrap:
    """整支队伍的组队结果：entries + 组合产物（未组成为 None）。

    字段词表封闭为三项（bootstrap_field_names 反射锁定）：entries
    （per-agent 预检事实，按 agent_id 排序）、facade / attribution
    （V3.0-D 的原样返回物；二者必须同时有值或同时为 None——预检失败
    或未达组合时均为 None）。
    """

    entries: tuple[AgentTeamEntry, ...]
    facade: Any | None
    attribution: Mapping[str, str] | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "entries", tuple(self.entries))
        if (self.facade is None) != (self.attribution is None):
            raise ValueError(
                "facade and attribution must be both set or both None")


def entry_field_names() -> tuple[str, ...]:
    """AgentTeamEntry 的封闭字段词表（组队预检事实，仅此六项）。"""
    return tuple(field.name for field in fields(AgentTeamEntry))


def bootstrap_field_names() -> tuple[str, ...]:
    """AgentTeamBootstrap 的封闭字段词表（组队结果，仅此三项）。"""
    return tuple(field.name for field in fields(AgentTeamBootstrap))


def _unbacked_facts(view) -> tuple:
    """词表内声明中未获 runtime 背书的投影事实。

    BEYOND_VOCABULARY（capability 为 None）不算失败——词表外声明是
    诚实边界，其组合语义由 V3.0-C/D 决定，本模块不预裁决。
    """
    return tuple(fact for fact in view.capabilities
                 if fact.capability is not None
                 and fact.backing is not CapabilityBacking.RUNTIME_BACKED)


def bootstrap_agent_team(
    registry,
    agent_ids,
    *,
    discover_runtime,
    evidence: Mapping[tuple, Any],
    current_health: Mapping[str, Any],
    timeout_seconds: float = 300.0,
    budget: Any | None = None,
    usage: Any | None = None,
    loop_guard: Any | None = None,
) -> AgentTeamBootstrap:
    """E → F → D 的纯编排组队入口。

    输入：registry（只读消费，内容不变）；agent_ids（调用方已选定的
    队伍——选择策略不在本层）；discover_runtime（注入的 runtime 粒度
    探测 callable，收到完整 identity 四元组，仅队伍绑定的 runtime 被
    探测）；evidence / current_health（与 V3.0-D 组合根同形的注入
    事实，逐字转发）。
    输出：AgentTeamBootstrap——entries 逐 agent 携带 discovered /
    backing / addresses 或诚实 reason；预检全绿且组合成功时
    facade / attribution 即 V3.0-D 的原样返回物，否则均为 None。

    诚实拒绝（raising，封闭 message，由被编排层原样上抛——本模块
    绝不重判）：非 callable 探测、畸形发现事实、证据 identity 与键
    不符（E/F），以及预检全绿后 V3.0-D 的全部组合期裁决（未知
    agent、空/重复 agent_ids、覆盖缺口、地址塌缩、多余协作证据、缺
    健康、混合 provenance 等）。
    非 raising 结果（结构化，绝不静默）：绑定不可发现、词表内声明
    未获 runtime 背书——预检可预见的失败以 per-agent 事实与机器
    reason 报告。
    """
    # 预检范围 = 队伍范围：manifest 引用组成 scoped registry 视图
    # （去重仅为探测范围；重复项的裁决权在 C/D）。未知 agent_id 不在
    # 此处裁决——原样留给组合根。
    team_registry = AgentRegistry()
    for agent_id in dict.fromkeys(agent_ids):
        manifest = registry.get(agent_id)
        if manifest is not None:
            team_registry.register(manifest)

    # E：绑定 liveness 预检（每 distinct 队伍 runtime 恰一次探测，
    # 异常收敛为受控 reason）。
    candidates = discover_agent_bindings(team_registry, discover_runtime)

    # F：能力背书预检。发现预检全队全绿才进行——绑定不可发现时，
    # 证据问题对该队伍已无意义（队伍级短路，backing 留空为诚实未
    # 计算）；证据 identity 与键不符仍由 F 诚实 raising。
    discovery_green = all(candidate.discovered for candidate in candidates)
    views_by_agent = {}
    if discovery_green:
        for view in project_agent_capabilities(team_registry, evidence):
            views_by_agent[view.agent_id] = view

    # D：组合（预检全绿才调用）。D 仍是 raising 裁决权威——本模块
    # 绝不吞其组合期拒绝，也绝不代其重判。
    facade = None
    attribution = None
    composed_addresses: dict[str, list[str]] = {}
    if discovery_green and not any(
            _unbacked_facts(view) for view in views_by_agent.values()):
        facade, attribution = build_facade_from_agents(
            registry, agent_ids, evidence, current_health,
            timeout_seconds=timeout_seconds, budget=budget,
            usage=usage, loop_guard=loop_guard)
        for address, agent_id in attribution.items():
            composed_addresses.setdefault(agent_id, []).append(address)

    entries = tuple(
        AgentTeamEntry(
            agent_id=candidate.agent_id,
            runtime_identity=candidate.runtime_identity,
            discovered=candidate.discovered,
            reason=(
                candidate.reason
                if not candidate.discovered
                else "CAPABILITY_DECLARED_ONLY"
                if candidate.agent_id in views_by_agent
                and _unbacked_facts(views_by_agent[candidate.agent_id])
                else None),
            backing=(views_by_agent[candidate.agent_id].capabilities
                     if candidate.agent_id in views_by_agent else ()),
            addresses=tuple(sorted(
                composed_addresses.get(candidate.agent_id, ()))),
        )
        for candidate in sorted(candidates,
                                key=lambda candidate: candidate.agent_id))
    return AgentTeamBootstrap(
        entries=entries, facade=facade, attribution=attribution)
