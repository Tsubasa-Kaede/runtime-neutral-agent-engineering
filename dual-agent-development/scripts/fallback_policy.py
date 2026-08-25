"""确定性的、Provider 中立的 fallback 选择策略。

仅限 ReadyPool 路径：本策略在经典执行路径上一次调用失败后选择
备用 agent。Verified 路径刻意注入空 agent 列表，因此 verified
candidate 绝不 fallback —— 其失败以 NO_FALLBACK_AGENT 诚实暴露，
而不是悄悄改在未验证的 peer 上重跑。

选择逻辑复刻 registry 的硬门（fallback 时刻 runtime READY、必需
能力在场且非 UNKNOWN），然后以下方固定权重确定性打分；平局按
agent_id 裁决。
"""
from __future__ import annotations

from enum import Enum
from typing import Mapping, Iterable

from capability_registry import AgentProfile, CapabilityConfidence, CapabilityName
from runtime_status import RuntimeState, RuntimeStatus


class FallbackReason(str, Enum):
    """封闭的结果词汇；可上报，绝不是诊断文本。"""

    SELECTED = "SELECTED"
    NO_FALLBACK_AGENT = "NO_FALLBACK_AGENT"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"


class FallbackPolicy:
    def __init__(self, agents: Iterable[AgentProfile]):
        self._agents = tuple(agents)

    def select(
        self,
        failed_agent_id: str,
        runtimes: Mapping[str, RuntimeStatus],
        required_capabilities: set[CapabilityName] | frozenset[CapabilityName],
        *,
        budget_available: bool = True,
    ):
        if not budget_available:
            # Budget 先行检查：没有余量的 fallback 连被考虑的资格
            # 都没有，因此绝不会有 agent 被"选中"去参加一次根本
            # 无法预留的调用。
            return type("FallbackResult", (), {"agent_id": None, "reason": FallbackReason.BUDGET_EXHAUSTED})()
        candidates = []
        for agent in self._agents:
            if agent.agent_id == failed_agent_id:
                continue
            status = runtimes.get(agent.runtime_id)
            if status is None or status.status is not RuntimeState.READY:
                continue
            evidence = [agent.capabilities.get(capability) for capability in required_capabilities]
            if any(item is None or item.confidence is CapabilityConfidence.UNKNOWN for item in evidence):
                continue
            # 固定的可观察权重：能力证据 0.9、历史成功率 0.1 ——
            # 确定性打分，平局按 id 裁决。
            score = sum(item.score or 0.0 for item in evidence) / len(evidence) if evidence else 0.0
            score = score * 0.9 + (agent.historical_success_rate or 0.0) * 0.1
            candidates.append((score, agent.agent_id))
        if not candidates:
            return type("FallbackResult", (), {"agent_id": None, "reason": FallbackReason.NO_FALLBACK_AGENT})()
        selected = sorted(candidates, key=lambda item: (-item[0], item[1]))[0][1]
        # 临时构造的 FallbackResult 形态（agent_id/reason 属性）是
        # 历史形成的调用点契约；原样保留而不替换为具名 dataclass，
        # 以免扰动既有消费方。
        return type("FallbackResult", (), {"agent_id": selected, "reason": FallbackReason.SELECTED})()
