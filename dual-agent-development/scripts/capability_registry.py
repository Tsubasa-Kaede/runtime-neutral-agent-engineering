"""Provider 中立的能力证据与确定性 Agent 选择。

ReadyPool 路径的选择算法：profile 携带能力 EVIDENCE（带置信层级），
select() 先过硬门再打分。Verified 路径不使用该打分 ——
VerifiedSelectionBridge 刻意将 verified candidate 投影为无分数 ——
因此下面的公式只属于 ReadyPool 路径。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Mapping

from runtime_status import RuntimeState, RuntimeStatus


class CapabilityName(str, Enum):
    """封闭的能力词汇表；只描述角色"需要什么"，
    绝不描述由哪个 runtime 提供。"""

    ARCHITECTURE = "architecture"
    CODING = "coding"
    REVIEW = "review"
    TESTING = "testing"


class CapabilityConfidence(str, Enum):
    """证据层级。DECLARED 是自我声明，永远不算证明；
    只有 VERIFIED 证据才完全满足选择。真实 gate 链产出的
    validated capability evidence 从这里进入。"""

    VERIFIED = "VERIFIED"
    DECLARED = "DECLARED"
    UNKNOWN = "UNKNOWN"


class SelectionReason(str, Enum):
    SELECTED = "SELECTED"
    NO_CAPABLE_AGENT = "NO_CAPABLE_AGENT"


@dataclass(frozen=True)
class CapabilityEvidence:
    """单项能力的证据。score=None 表示"无分数"（该证据层级不携带
    分数）—— 绝不能读作低分；UNKNOWN 层级额外禁止携带分数且不要求
    source，而任何已知层级必须注明证据来源。"""

    capability: CapabilityName
    score: float | None
    confidence: CapabilityConfidence
    source: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.capability, CapabilityName):
            raise ValueError("invalid capability")
        if self.score is not None and not 0.0 <= self.score <= 1.0:
            raise ValueError("capability score must be between 0 and 1")
        if self.confidence is CapabilityConfidence.UNKNOWN and self.score is not None:
            raise ValueError("unknown capability cannot have a score")
        if self.confidence is not CapabilityConfidence.UNKNOWN and not self.source:
            raise ValueError("known capability requires evidence source")


@dataclass(frozen=True)
class AgentProfile:
    """寄宿在某个 runtime 上的可路由 agent 身份。

    Agent 不是 Runtime：agent_id 是可寻址的执行者，runtime_id 指明
    其所运行的、经过 Health 检查的执行基底，capabilities 是按 agent
    计的证据 —— 绝不因"runtime 存在"而推断得出。"""

    agent_id: str
    runtime_id: str
    provider: str | None
    model: str | None
    role: str | None
    capabilities: Mapping[CapabilityName, CapabilityEvidence]
    historical_success_rate: float | None = None

    def __post_init__(self) -> None:
        if not self.agent_id or not self.runtime_id:
            raise ValueError("agent and runtime identifiers are required")
        if self.historical_success_rate is not None and not 0.0 <= self.historical_success_rate <= 1.0:
            raise ValueError("historical success rate must be between 0 and 1")
        if any(not isinstance(key, CapabilityName) for key in self.capabilities):
            raise ValueError("invalid capability key")
        if any(value.capability is not key for key, value in self.capabilities.items()):
            raise ValueError("capability evidence key mismatch")


@dataclass(frozen=True)
class SelectionCandidate:
    """单个 profile 的选择结果。score=None 表示"不合格"
    （某个硬门失败；见 failures）—— 不是低分，绝不能与真实分数
    一起排序或平均。"""

    agent_id: str
    runtime_id: str
    score: float | None
    eligible: bool
    failures: tuple[str, ...]


@dataclass(frozen=True)
class SelectionResult:
    agent_id: str | None
    reason: SelectionReason
    candidates: tuple[SelectionCandidate, ...]


class CapabilityRegistry:
    def __init__(self, agents: list[AgentProfile] | tuple[AgentProfile, ...] = ()):
        self._agents = tuple(agents)

    def select(
        self,
        required_capabilities: set[CapabilityName] | frozenset[CapabilityName],
        runtimes: Mapping[str, RuntimeStatus],
        role: str | None = None,
    ) -> SelectionResult:
        """先过硬门，再计算确定性的 ReadyPool 分数。

        硬门（任一失败 => 不合格，score 保持 None）：runtime 在选择
        时刻必须 READY、role 必须匹配、每项必需能力必须在场且置信度
        非 UNKNOWN。只有完全合格的 profile 才打分；选择规则是取
        (-score, agent_id) 最小值，因此平分时按 agent_id 确定性
        决出。"""
        candidates = []
        for profile in sorted(self._agents, key=lambda item: item.agent_id):
            failures: list[str] = []
            status = runtimes.get(profile.runtime_id)
            if status is None or status.status is not RuntimeState.READY:
                failures.append("runtime_not_ready")
            if role is not None and profile.role != role:
                failures.append("role_mismatch")
            evidence = [profile.capabilities.get(capability) for capability in required_capabilities]
            if any(item is None for item in evidence):
                failures.append("capability_missing")
            elif any(item.confidence is CapabilityConfidence.UNKNOWN for item in evidence):
                failures.append("capability_unknown")
            score = None
            if not failures:
                # 确定性的 ReadyPool 路径分数（可观察的选择契约 ——
                # 权重绝不允许被悄悄调整）：
                #   capability_fit * 0.6   证据分数均值
                #   confidence_fit * 0.25  VERIFIED=1.0, DECLARED=0.5
                #   readiness * 0.1        READY runtime 加成（此处为
                #                          常量：只有 READY 的 runtime
                #                          才会进入打分）
                #   history * 0.05         历史成功率，未知时取 0.0
                #                          —— 没有历史绝不是惩罚。
                capability_fit = sum(item.score or 0.0 for item in evidence) / len(evidence) if evidence else 0.0
                confidence_fit = sum(
                    1.0 if item.confidence is CapabilityConfidence.VERIFIED else 0.5
                    for item in evidence
                ) / len(evidence) if evidence else 0.0
                readiness = 1.0
                history = profile.historical_success_rate if profile.historical_success_rate is not None else 0.0
                score = capability_fit * 0.6 + confidence_fit * 0.25 + readiness * 0.1 + history * 0.05
            candidates.append(SelectionCandidate(profile.agent_id, profile.runtime_id, score, not failures, tuple(failures)))

        eligible = [candidate for candidate in candidates if candidate.eligible]
        if not eligible:
            return SelectionResult(None, SelectionReason.NO_CAPABLE_AGENT, tuple(candidates))
        selected = sorted(eligible, key=lambda item: (-item.score, item.agent_id))[0]
        return SelectionResult(selected.agent_id, SelectionReason.SELECTED, tuple(candidates))
