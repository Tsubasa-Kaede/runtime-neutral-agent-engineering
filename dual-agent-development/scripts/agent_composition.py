"""V3.0-C: Agent Composition Seam — the first consumer of V3.0-A/B.

V3.0-A 给出 agent 的 WHO/WHERE 与 V2 地址投影；V3.0-B 给出声明面
（AgentManifest/AgentRegistry）。两者至今零生产消费者。本模块是它们的
第一个消费者：把「选定的 agent × 需要的角色」组合成 V2 可以消费的槽位
（compat 地址 + adapter 实例），不执行、不发现、不准入：

    AgentManifest（声明）           <- registry 只读消费
      ↓ binding（V3.0-A）
    compose_agent_slots(...)        <- 本模块：纯函数组合
      ↓
    AgentSlotResolution             <- frozen 槽位：V2 可消费的最小事实

Invariants（locked by tests/test_agent_composition.py）：
- V2 地址投影保真度边界：V2 compat 地址由 (runtime identity, role)
  决定 —— 两个不同 agent 若共享 runtime 且共享 role 会塌缩为同一参与
  者；组合层显式检测并以封闭 ValueError 拒绝。这是投影保真度判断，
  不是 discovery/capability/verification/admission/trust 判断。
- adapter_factory 生命周期：registry 永不调用（V3.0-B 契约）；composer
  是第一个合法调用点 —— 恰好一次/agent（同一 agent 的多角色槽位共享
  同一实例）。工厂失败原样向上传播：不吞、不 retry、不 fallback。
- 确定性：agent 按 id 排序实例化，输出按 (role, agent_id) 排序；同输入
  必同输出。零 runtime 名、零 V2 直接 import、零状态。

NOT implemented here（later V3 phases）：discovery、capability、
verification、admission、trust、orchestration、host 接线、persistence、
remote、A2A、multi-binding。
"""
from __future__ import annotations

from dataclasses import dataclass, fields

from agent_identity import compat_collab_address


@dataclass(frozen=True)
class AgentSlotResolution:
    """一个组合槽位：agent 在某角色上投影到 V2 的最小事实。

    字段词表封闭为五项（resolution_field_names 反射锁定）：agent_id
    （WHO）、role（职责）、address（V2 compat 投影地址）、
    runtime_identity（WHERE，V2 四元组逐字透传）、adapter（契约面实例，
    来自 manifest.adapter_factory 的恰一次调用）。
    """

    agent_id: str
    role: str
    address: str
    runtime_identity: tuple
    adapter: object

    def __post_init__(self) -> None:
        for name in ("agent_id", "role", "address"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"{name} must be a non-empty string")


def resolution_field_names() -> tuple[str, ...]:
    """AgentSlotResolution 的封闭字段词表（组合槽位，仅此五项）。"""
    return tuple(field.name for field in fields(AgentSlotResolution))


def _as_clean_role_tuple(roles, field_name: str) -> tuple:
    """角色需求的封闭校验：非空字符串、无重复；不重建 secret policy
    （agent 侧字符串已在 Identity/Manifest 构造期过单一 policy）。"""
    items = tuple(roles)
    if not items:
        raise ValueError(f"{field_name} must not be empty")
    for item in items:
        if not isinstance(item, str) or not item:
            raise ValueError(f"{field_name} items must be non-empty strings")
    if len(set(items)) != len(items):
        raise ValueError(f"{field_name} must not contain duplicates")
    return items


def compose_agent_slots(registry, agent_ids, required_roles):
    """把选定的 agent × 需要的角色组合成 V2 可消费的槽位序列。

    输入：registry（只读消费，内容不变）；agent_ids（调用方已选定 的
    agent —— 选择策略不在本层）；required_roles（本次组合需要的角色）。
    输出：tuple[AgentSlotResolution, ...]，按 (role, agent_id) 排序，
    同输入必同输出。

    诚实拒绝（封闭 message，不携带 secret/prompt/output）：
    - 空 agent_ids / 空角色需求 / 重复项 / 非法项
    - 未知 agent_id
    - 某需求角色没有任何选定 agent 声明
    - 某选定 agent 与全部需求角色无交集（组合它没有意义）
    - 同 (runtime_identity, role) 被两个不同 agent 占据（V2 地址塌缩）
    adapter_factory 恰好一次/agent，失败原样传播。
    """
    requested = _as_clean_role_tuple(agent_ids, "agent_ids")
    roles = _as_clean_role_tuple(required_roles, "required_roles")

    manifests = []
    for agent_id in requested:
        manifest = registry.get(agent_id)
        if manifest is None:
            raise ValueError(f"unknown agent_id: {agent_id}")
        manifests.append(manifest)

    # 覆盖校验：每个需求角色至少被一个选定 agent 声明；每个选定 agent
    # 至少贡献一个角色 —— 两端的静默丢弃都是不诚实的组合。
    for role in roles:
        if not any(role in m.declared_roles for m in manifests):
            raise ValueError(f"required role declared by no requested agent: {role}")
    plan = []
    for manifest in manifests:
        relevant = tuple(role for role in roles
                         if role in manifest.declared_roles)
        if not relevant:
            raise ValueError(
                f"agent declares none of the required roles: "
                f"{manifest.agent_id}")
        plan.append((manifest, relevant))

    # adapter_factory：composer 是第一个合法调用点 —— 恰一次/agent，
    # 按 agent_id 排序实例化（确定性）；失败原样向上传播。
    adapters = {}
    for manifest, _relevant in sorted(plan, key=lambda item: item[0].agent_id):
        adapters[manifest.agent_id] = manifest.adapter_factory()

    slots = [
        AgentSlotResolution(
            agent_id=manifest.agent_id,
            role=role,
            address=compat_collab_address(manifest.binding, role),
            runtime_identity=manifest.binding.runtime_identity,
            adapter=adapters[manifest.agent_id])
        for manifest, relevant in plan
        for role in relevant
    ]

    # V2 地址投影保真度边界：compat 地址由 (runtime identity, role)
    # 决定；两个不同 agent 塌缩到同一地址 = 同一参与者的歧义组合。
    owners = {}
    for slot in slots:
        key = (tuple(slot.runtime_identity), slot.role)
        if key in owners and owners[key] != slot.agent_id:
            raise ValueError(
                f"ambiguous composition: role {slot.role} on one runtime "
                f"identity is claimed by multiple agents")
        owners[key] = slot.agent_id

    return tuple(sorted(slots, key=lambda s: (s.role, s.agent_id)))
