"""V3.0-B: Agent Manifest + Registry — the minimal declaration layer.

V2 冻结在 runtime 为一等公民：AdapterDescriptor/AdapterRegistry 以
runtime_id 为键声明执行基底，链路每一环（discovery -> qualification ->
admission -> selection）都只认识 runtime identity。V3.0-A 给出了 agent
的 WHO（AgentIdentity）与 WHERE（AgentRuntimeBinding）；本模块补上最小
的声明面 —— 让后续阶段（capability 附着、verification、admission）有
可挂载的 agent 粒度事实：

    AgentManifest    一份声明：WHO + WHERE + 意图角色 + 契约面（工厂）
    AgentRegistry    组合根持有的内存清单：sorted、去重、零执行面

Invariants（locked by tests/test_agent_manifest.py）：
- 声明不是验证：declared_roles 是意图（可空），capability/trust/
  verified/health/status 等字段词表封闭，构造期以 TypeError 拒绝夹带。
- registry 从不调用 adapter_factory、从不 probe、从不 discovery、
  从不 admission、从不执行 —— 存储与枚举是它的全部表面。
- 复用单一 secret policy（content_safety 词表 + credential 形状），
  与 V2/V3.0-A/D1 同源，不建第二套。
- runtime-neutral：无 runtime 名、无 runtime 分支；runtime 特定知识
  封在 adapter 内（工厂惯例，同 AdapterDescriptor）。
- 内存态、组合根持有：无全局单例、无 persistence、无网络。

NOT implemented here（later V3 phases）：manifest 签名（全库零密钥管理
设施）、agent discovery（一 runtime 多 agent 的桥接会污染 V2 discovery，
见 Boundary Review）、capability 附着、admission、trust、remote、A2A。
"""
from __future__ import annotations

from content_safety import SECRET_MARKERS, contains_unsafe_content
from dataclasses import dataclass, fields
from typing import Callable

from agent_identity import AgentRuntimeBinding


@dataclass(frozen=True)
class AgentManifest:
    """一份 agent 声明：binding（WHO+WHERE，V3.0-A 原样组合）+
    declared_roles（意图角色，声明 ≠ 验证）+ adapter_factory（契约面）。

    frozen 值，无生命周期：修改声明 = 构造新的 manifest 值。字段词表
    恰好三项（manifest_field_names 反射锁定）。
    """

    binding: AgentRuntimeBinding
    declared_roles: tuple[str, ...]
    adapter_factory: Callable

    def __post_init__(self) -> None:
        if not isinstance(self.binding, AgentRuntimeBinding):
            raise TypeError("binding must be an AgentRuntimeBinding")
        roles = tuple(self.declared_roles)
        if len(set(roles)) != len(roles):
            raise ValueError("declared_roles must not contain duplicates")
        for role in roles:
            if not isinstance(role, str) or not role:
                raise ValueError(
                    "declared_roles items must be non-empty strings")
            # 复用既有单一 secret policy（与 AgentIdentity/D1 同款）：
            # marker 提及（大小写不敏感子串）与 credential 形状都拒收。
            lowered = role.lower()
            for marker in SECRET_MARKERS:
                if marker in lowered:
                    raise ValueError(
                        "declared_roles items must not contain "
                        "secret-shaped content")
            if contains_unsafe_content(role):
                raise ValueError(
                    "declared_roles items must not contain "
                    "secret-shaped content")
        object.__setattr__(self, "declared_roles", roles)
        if not callable(self.adapter_factory):
            raise ValueError("adapter_factory must be callable")

    @property
    def agent_id(self) -> str:
        return self.binding.agent_id


class AgentRegistry:
    """组合根持有的 sorted、去重 agent 清单；存储之外零表面。"""

    def __init__(self) -> None:
        self._manifests: dict[str, AgentManifest] = {}

    def register(self, manifest: AgentManifest) -> None:
        if not isinstance(manifest, AgentManifest):
            raise TypeError("register requires an AgentManifest")
        agent_id = manifest.agent_id
        if agent_id in self._manifests:
            raise ValueError(f"duplicate agent_id: {agent_id}")
        self._manifests[agent_id] = manifest

    def get(self, agent_id: str) -> AgentManifest | None:
        return self._manifests.get(agent_id)

    def list(self) -> tuple[AgentManifest, ...]:
        return tuple(self._manifests[key] for key in sorted(self._manifests))


def manifest_field_names() -> tuple[str, ...]:
    """AgentManifest 的封闭字段词表（声明面，仅此三项）。"""
    return tuple(field.name for field in fields(AgentManifest))
