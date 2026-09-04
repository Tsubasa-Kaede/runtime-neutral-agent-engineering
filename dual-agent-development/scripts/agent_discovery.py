"""V3.0-E: Agent binding liveness discovery — declaration joined with
runtime discovery facts.

V3.0-E 只回答一个问题：«一个已声明的 Agent，其 binding 指向的
Runtime 当前是否可被发现？» Agent 是逻辑身份（V3.0-A，只能被声明，
环境中不存在可扫描的 agent）；能被环境发现的只有 Runtime。因此
Agent Discovery 的唯一诚实语义是 join 投影：

    AgentRegistry（声明世界，V3.0-B，只读）
      + discover_runtime（注入的 runtime 粒度探测 callable）
      ↓ discover_agent_bindings（本模块：纯函数投影）
    tuple[AgentCandidate, ...]  <- 每 agent 一条绑定 liveness 候选

Invariants（locked by tests/test_agent_discovery.py）：
- 权威分离：registry 说«这个 agent 被声明存在»；runtime discovery 说
  «这个 runtime 当前可发现»；本模块只说«这个声明的绑定当前可发现»。
  declared ≠ discovered ≠ verified ≠ admitted —— 绝不合并成单一
  status，生命周期各归其 artifact（本模块只新增 discovered 这一个）。
- 探测去重键是完整 Runtime Identity 四元组（runtime_id, provider_id,
  model_id, config_fingerprint）：同 runtime_id 不同 fingerprint 是两
  次探测；同 runtime 上 N 个 agent 共享一次探测、各自独立成候选 ——
  agent 候选键是 agent_id，绝不退化成 runtime_id，也绝不被 runtime
  去重吞掉。
- 探测能力全部来自注入的 callable（收到完整 identity 四元组）；绝不
  把 AgentManifest 包装成 DiscoverySource（runtime_discovery 零去重
  且 discovery_sources 会消费 adapter_factory —— 见 Boundary Re-check
  三条代码证据）。registry 只读消费，adapter_factory 永不被本模块调用
  （composer 是唯一合法调用点，V3.0-C 契约）。
- 异常收敛沿用 V2 Runtime Discovery 的方式：注入 callable 抛异常 ->
  受控 discovered=False + "NOT_FOUND: discovery error (TypeName)"，
  绝不向上抛、绝不虚假成功；返回值不暴露 available 是调用方契约错误，
  封闭 ValueError。
- AgentCandidate 字段封闭为四项（candidate_field_names 反射锁定）：
  仅发现事实 —— 不携带 capability / verified / trust / health /
  score / admitted / status 等任何后续层语义（不提前锁死未来
  Verification / Admission 的接缝）。

NOT implemented here（later V3 phases）：capability 附着、verification、
admission、trust、health 探测、agent 选择、role assignment、
orchestration、remote、A2A、persistence、UI、CLI、以及本投影的
生产消费者（agent 侧 auto-entry 属后续阶段）。
"""
from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Callable, Tuple


@dataclass(frozen=True)
class AgentCandidate:
    """一个已声明 agent 的绑定 liveness 投影（仅发现事实）。

    字段词表封闭为四项（candidate_field_names 反射锁定）：agent_id
    （WHO，逻辑身份）、runtime_identity（WHERE，绑定指向的 V2 四元组
    逐字透传）、discovered（该 runtime 当前可发现）、reason（不可发现
    时的受控理由；可发现时必须为 None —— 自相矛盾的事实构造期拒绝）。
    """

    agent_id: str
    runtime_identity: tuple
    discovered: bool
    reason: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.agent_id, str) or not self.agent_id:
            raise ValueError("agent_id must be a non-empty string")
        if not isinstance(self.discovered, bool):
            raise ValueError("discovered must be a bool")
        if self.discovered and self.reason is not None:
            raise ValueError(
                "reason must be None when discovered is True")
        if self.reason is not None and (
                not isinstance(self.reason, str) or not self.reason):
            raise ValueError("reason must be None or a non-empty string")


def candidate_field_names() -> tuple[str, ...]:
    """AgentCandidate 的封闭字段词表（发现事实，仅此四项）。"""
    return tuple(field.name for field in fields(AgentCandidate))


def _probe(discover_runtime: Callable[[tuple], object],
           identity: tuple) -> Tuple[bool, "str | None"]:
    """探测一个 distinct runtime identity，收敛为 (discovered, reason)。

    注入 callable 抛异常时沿用 V2 Runtime Discovery 的收敛方式：受控
    NOT_FOUND（携带异常类型名），绝不向上抛、绝不虚假成功。返回值不
    暴露 available 是调用方契约错误，封闭 ValueError。
    """
    try:
        fact = discover_runtime(identity)
    except Exception as exc:  # discovery failure is a controlled outcome
        return False, f"NOT_FOUND: discovery error ({type(exc).__name__})"
    available = getattr(fact, "available", None)
    if available is None:
        raise ValueError(
            f"discovery fact for runtime {identity[0]} must expose "
            f"'available'")
    if available:
        return True, None
    reason = getattr(fact, "reason", None) or "NOT_FOUND"
    return False, str(reason)


def discover_agent_bindings(
    registry,
    discover_runtime: Callable[[tuple], object],
) -> tuple[AgentCandidate, ...]:
    """把声明世界与 runtime 发现事实 join 成每 agent 的 liveness 候选。

    输入：registry（只读消费，内容不变）；discover_runtime（注入的
    runtime 粒度探测 callable，收到完整 identity 四元组，返回暴露
    available/reason 的发现事实 —— 如 V2 RuntimeCandidate 形状）。
    输出：tuple[AgentCandidate, ...]，按 agent_id 排序；同输入必同输出。

    探测恰一次/distinct runtime identity（四元组去重；identity 元素
    可能含 None，排序键用字符串投影避免比较崩溃）；同 runtime 上 N
    个 agent 共享同一探测结果、各自独立成候选。空 registry -> 空元组、
    零探测。registry 与 adapter_factory 均不被触碰。
    """
    if not callable(discover_runtime):
        raise ValueError("discover_runtime must be callable")

    manifests = tuple(registry.list())
    facts = {}
    for identity in sorted(
            {tuple(manifest.binding.runtime_identity)
             for manifest in manifests},
            key=lambda item: tuple(str(element) for element in item)):
        facts[identity] = _probe(discover_runtime, identity)

    candidates = [
        AgentCandidate(
            agent_id=manifest.agent_id,
            runtime_identity=tuple(manifest.binding.runtime_identity),
            discovered=facts[tuple(manifest.binding.runtime_identity)][0],
            reason=facts[tuple(manifest.binding.runtime_identity)][1],
        )
        for manifest in manifests
    ]
    return tuple(sorted(candidates, key=lambda candidate: candidate.agent_id))
