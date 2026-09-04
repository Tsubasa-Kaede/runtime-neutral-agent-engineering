"""V3.0-F: Agent capability view — declared × runtime-evidence join.

V3.0-F 只回答一个问题：«这个 Agent 声称/被证明具有什么能力？»
以纯 join 视图表达，不组合、不执行、不产证据、不做能力裁决：

    AgentManifest.declared_roles（声明，V3.0-B）
      × evidence（runtime identity -> CandidateValidationResult，V2 资格）
      ↓ project_agent_capabilities（本模块：纯函数投影）
    tuple[AgentCapabilityView]  <- 每 agent 的 per-能力 backing

Invariants（locked by tests/test_agent_capability.py）：
- 必要非充分：RUNTIME_BACKED 表示「声明 ∧ 该绑定 runtime 的资格证据
  在位」——这是 agent 可执行该能力的必要条件被满足，绝不是「该
  agent 已被验证」。agent 级验证属未来阶段，本模块的输出词表刻意
  不含任何生命周期断言（CapabilityBacking 仅三值：RUNTIME_BACKED /
  DECLARED_ONLY / BEYOND_VOCABULARY）。
- Declared ≠ Verified（既有纪律：V2 CapabilityConfidence——DECLARED
  永不算证明；本模块是同一纪律在 agent 侧的只读投影）：
  DECLARED_ONLY = 声明在、runtime 证据缺；二者绝不互相冒充。
- 词表诚实边界：声明角色无 V2 capability 投影（冻结 4 词表之外）
  -> BEYOND_VOCABULARY + capability=None，绝不静默丢弃声明。
- 非 raising：能力状态缺失不抛错——与 V3.0-D 组合根的 raising 裁决
  互补（同一双向一致性不变量的两种消费形态：D 拒绝不一致的组合，
  本视图让调用方在组合前查询）。输入契约错误（证据 identity 与键
  不符）仍诚实拒绝。团队级「词表内多余证据」的裁决权留在 V3.0-D，
  本视图不重复、不改写。
- runtime 证据是共享前提：同 runtime 上 N 个 agent 各自独立成视图、
  共享同一份该 runtime 的资格证据；证据按完整 identity 四元组区分
  （同 runtime_id 不同 fingerprint 是不同前提）。
- registry 与 evidence 均只读；adapter_factory 永不被本模块调用
  （composer 是唯一合法调用点，V3.0-C 契约）。
- runtime-neutral + agent-neutral：零 runtime/provider/model 名、零
  分支；能力事实只来自注入的声明与证据。

NOT implemented here（later V3 phases）：capability verification、
agent 级证据产出、trust、admission、打分（V1 ReadyPool 谱系专属）、
选择、role assignment、orchestration、remote、A2A、persistence、
UI、CLI、以及本视图的生产消费者（auto-entry / agent 级验证属后续）。
"""
from __future__ import annotations

from dataclasses import dataclass, fields
from enum import Enum
from typing import Any, Mapping


class CapabilityBacking(str, Enum):
    """声明能力的背书状态（封闭三值，无生命周期断言）。

    RUNTIME_BACKED    声明 ∧ 该绑定 runtime 的资格证据在位（必要条件
                      满足 —— 绝不读作「该 agent 已被验证」）
    DECLARED_ONLY     声明在、runtime 证据缺（含证据缺失、非 VERIFIED
                      的资格结果、或证据不含该能力三种情形）
    BEYOND_VOCABULARY 声明角色在冻结 V2 capability 词表之外（无投影，
                      capability=None；诚实边界，不是错误）
    """

    RUNTIME_BACKED = "RUNTIME_BACKED"
    DECLARED_ONLY = "DECLARED_ONLY"
    BEYOND_VOCABULARY = "BEYOND_VOCABULARY"


# 声明角色 -> V2 capability 的必要投影（封闭词表）。与 agent_host 的
# _ROLE_CAPABILITY 及 V2 verified_stage_selector._ROLE_REQUIREMENTS 的值
# 三重锁定（tests 防漂移）——本模块刻意零 V2/V3 import（evidence 值
# 注入），故本地持有同一定义。
_ROLE_CAPABILITY = {
    "architect": "architecture",
    "coder": "coding",
    "tester": "testing",
    "reviewer": "review",
}


@dataclass(frozen=True)
class DeclaredCapability:
    """一条声明能力的投影事实：声明角色 + 词表投影 + 背书状态。

    字段词表封闭为三项（capability_field_names 反射锁定）：role（声明
    拼写）、capability（V2 词表投影；恰在 BEYOND_VOCABULARY 时为
    None —— 一致性构造期强制）、backing（背书状态）。
    """

    role: str
    capability: str | None
    backing: CapabilityBacking

    def __post_init__(self) -> None:
        if not isinstance(self.role, str) or not self.role:
            raise ValueError("role must be a non-empty string")
        if not isinstance(self.backing, CapabilityBacking):
            raise ValueError("backing must be a CapabilityBacking")
        if self.backing is CapabilityBacking.BEYOND_VOCABULARY:
            if self.capability is not None:
                raise ValueError(
                    "capability must be None when backing is "
                    "BEYOND_VOCABULARY")
        elif not isinstance(self.capability, str) or not self.capability:
            raise ValueError(
                "capability must be a non-empty string for this backing")


@dataclass(frozen=True)
class AgentCapabilityView:
    """一个 agent 的能力视图快照（纯投影值，无生命周期）。

    字段词表封闭为三项（view_field_names 反射锁定）：agent_id（WHO）、
    runtime_identity（当前绑定的 V2 四元组逐字透传——视图随 rebind
    跟随新绑定）、capabilities（per-声明角色的投影事实，按 role 排序）。
    """

    agent_id: str
    runtime_identity: tuple
    capabilities: tuple[DeclaredCapability, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.agent_id, str) or not self.agent_id:
            raise ValueError("agent_id must be a non-empty string")
        object.__setattr__(self, "capabilities", tuple(self.capabilities))


def view_field_names() -> tuple[str, ...]:
    """AgentCapabilityView 的封闭字段词表（能力视图，仅此三项）。"""
    return tuple(field.name for field in fields(AgentCapabilityView))


def capability_field_names() -> tuple[str, ...]:
    """DeclaredCapability 的封闭字段词表（投影事实，仅此三项）。"""
    return tuple(field.name for field in fields(DeclaredCapability))


def project_agent_capabilities(
    registry,
    evidence: Mapping[tuple, Any],
) -> tuple[AgentCapabilityView, ...]:
    """把声明世界与 runtime 资格证据 join 成每 agent 的能力视图。

    输入：registry（只读消费，内容不变）；evidence（runtime identity
    -> CandidateValidationResult，与 V3.0-D 组合根同形；未覆盖某
    identity 时该 agent 全部声明为 DECLARED_ONLY —— 非 raising）。
    输出：tuple[AgentCapabilityView, ...]，按 agent_id 排序；视图内
    capabilities 按 role 排序；同输入必同输出。

    诚实拒绝（输入契约，封闭 message）：
    - 证据 identity 与其键不符（与 V3.0-D 同款校验，防错位证据）
    """
    manifests = tuple(registry.list())

    # 每个被绑定的 distinct runtime identity 只解析一次证据（同
    # runtime 多 agent 共享同一前提；identity 四元组区分 fingerprint）。
    evidence_caps: dict[tuple, frozenset] = {}
    for manifest in manifests:
        identity = tuple(manifest.binding.runtime_identity)
        if identity in evidence_caps:
            continue
        validation = evidence.get(identity)
        if validation is None:
            evidence_caps[identity] = frozenset()
            continue
        if tuple(validation.identity) != identity:
            raise ValueError(
                f"evidence identity mismatch for runtime: {identity[0]}")
        evidence_caps[identity] = frozenset(
            tuple(validation.validated_capabilities))

    views = []
    for manifest in manifests:
        identity = tuple(manifest.binding.runtime_identity)
        backed = evidence_caps[identity]
        facts = []
        for role in manifest.declared_roles:
            capability = _ROLE_CAPABILITY.get(role)
            if capability is None:
                facts.append(DeclaredCapability(
                    role=role, capability=None,
                    backing=CapabilityBacking.BEYOND_VOCABULARY))
            elif capability in backed:
                facts.append(DeclaredCapability(
                    role=role, capability=capability,
                    backing=CapabilityBacking.RUNTIME_BACKED))
            else:
                facts.append(DeclaredCapability(
                    role=role, capability=capability,
                    backing=CapabilityBacking.DECLARED_ONLY))
        views.append(AgentCapabilityView(
            agent_id=manifest.agent_id,
            runtime_identity=identity,
            capabilities=tuple(sorted(facts, key=lambda fact: fact.role))))
    return tuple(sorted(views, key=lambda view: view.agent_id))
