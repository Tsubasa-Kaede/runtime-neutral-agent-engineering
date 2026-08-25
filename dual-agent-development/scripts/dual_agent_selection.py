"""确定性的、Provider 中立的双 agent 选择决策。

三种结果形态，各有自己的消费方（刻意保持区分；它们不是冗余
视图）：
- select()：供经典引擎 plan 循环使用的逐阶段 agent 指派
  （每个阶段得到其最优合格 agent）。
- decide()：带 fallback 链与预算前置条件的 architect/coder DUAL
  决策 —— 最丰富的形态，供需要推理字段的调用方使用。
- to_selection_result()：从 decide() 结果到指派形态的纯桥接，
  角色亲和性固定（tester 工作跟随 coder 侧 agent，review 工作
  跟随 architect 侧 agent）。

所有决策都是确定性的：候选已排序、平局裁决固定、无随机、
无 runtime 名称分支。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Mapping

from capability_registry import AgentProfile, CapabilityConfidence, CapabilityName
from runtime_status import RuntimeState, RuntimeStatus
from task_classifier import Complexity
from task_budget import TaskBudget, BudgetUsage


class DualAgentMode(str, Enum):
    """封闭的结果词汇：两个 agent、一个 agent、或没有。"""

    TWO_AGENT = "TWO_AGENT"
    SINGLE_AGENT = "SINGLE_AGENT"
    NO_AGENT = "NO_AGENT"


class DecisionReason(str, Enum):
    """封闭的决策词汇；可上报，绝不是诊断文本。"""

    TWO_CAPABLE_AGENTS = "TWO_CAPABLE_AGENTS"
    SINGLE_CAPABLE_AGENT = "SINGLE_CAPABLE_AGENT"
    SIMPLE_TASK = "SIMPLE_TASK"
    NO_CAPABLE_AGENT = "NO_CAPABLE_AGENT"
    BUDGET_LIMIT = "BUDGET_LIMIT"
    BUDGET_INSUFFICIENT = "BUDGET_INSUFFICIENT"
    INSUFFICIENT_DIVERSITY = "INSUFFICIENT_DIVERSITY"


_STAGE_REQUIREMENTS: dict[str, tuple[CapabilityName, ...]] = {
    "architect": (CapabilityName.ARCHITECTURE,),
    "coder": (CapabilityName.CODING,),
    "test": (CapabilityName.TESTING,),
    "review": (CapabilityName.REVIEW,),
}

_REQUIRED_CALLS: dict[Complexity, int] = {
    # Complexity -> dual 决策所要求的最小剩余预算；低于它时诚实的
    # 结果是 BUDGET_INSUFFICIENT，绝不半规划地跑 dual。
    Complexity.SIMPLE: 1,
    Complexity.MEDIUM: 2,
    Complexity.COMPLEX: 4,
}

# 专业化门：只有当每一侧都在自己的角色上严格更好、且增益之和
# 超过 0.1 时 dual 才成立 —— 这是可观察决策契约的一部分，
# 不是可随意调节的参数。
_SPECIALIZATION_THRESHOLD = 0.1


@dataclass(frozen=True)
class DualAgentSelectionResult:
    decision: DualAgentMode
    assignments: Mapping[str, str]
    primary_agent: str | None
    secondary_agent: str | None
    reason: DecisionReason
    evidence: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "assignments", dict(self.assignments))


@dataclass(frozen=True)
class DualAgentDecision:
    mode: str
    complexity: str
    use_dual_agent: bool
    architect_agent_id: str | None
    coder_agent_id: str | None
    architect_runtime_id: str | None = None
    coder_runtime_id: str | None = None
    architect_fallback_agents: tuple[str, ...] = ()
    coder_fallback_agents: tuple[str, ...] = ()
    reason: DecisionReason = DecisionReason.SINGLE_CAPABLE_AGENT


class DualAgentSelection:
    def select(
        self,
        profiles,
        runtimes: Mapping[str, RuntimeStatus],
        complexity: Complexity,
        budget: TaskBudget,
        usage: BudgetUsage,
    ) -> DualAgentSelectionResult:
        eligible = tuple(
            profile for profile in sorted(profiles, key=lambda item: item.agent_id)
            if self._eligible(profile, runtimes)
        )
        if not eligible:
            return DualAgentSelectionResult(DualAgentMode.NO_AGENT, {}, None, None, DecisionReason.NO_CAPABLE_AGENT)

        if complexity is Complexity.SIMPLE:
            primary = self._best_for(eligible, "coder") or eligible[0]
            return DualAgentSelectionResult(
                DualAgentMode.SINGLE_AGENT,
                {"coder": primary.agent_id},
                primary.agent_id,
                None,
                DecisionReason.SIMPLE_TASK,
            )

        stages = self._stages(complexity)
        if not stages:
            primary = eligible[0]
            return DualAgentSelectionResult(
                DualAgentMode.SINGLE_AGENT,
                {"coder": primary.agent_id},
                primary.agent_id,
                None,
                DecisionReason.NO_CAPABLE_AGENT,
            )

        remaining_calls = max(0, budget.max_agent_calls - usage.total_agent_calls)
        needs_two = remaining_calls >= 2 and len(eligible) >= 2

        assignments: dict[str, str] = {}
        for stage in stages:
            best = self._best_for(eligible, stage)
            if best is not None:
                assignments[stage] = best.agent_id
        if not assignments:
            return DualAgentSelectionResult(
                DualAgentMode.NO_AGENT, {}, None, None, DecisionReason.NO_CAPABLE_AGENT
            )

        distinct = sorted(set(assignments.values()))
        if needs_two and len(distinct) >= 2:
            primary = distinct[0]
            secondary = distinct[1]
            return DualAgentSelectionResult(
                DualAgentMode.TWO_AGENT,
                assignments,
                primary,
                secondary,
                DecisionReason.TWO_CAPABLE_AGENTS,
                tuple(f"{stage}={agent}" for stage, agent in sorted(assignments.items())),
            )
        primary_agent = assignments.get("coder") or distinct[0]
        return DualAgentSelectionResult(
            DualAgentMode.SINGLE_AGENT,
            assignments,
            primary_agent,
            None,
            DecisionReason.BUDGET_LIMIT if not needs_two and remaining_calls < 2 and len(distinct) >= 2 else DecisionReason.INSUFFICIENT_DIVERSITY,
            tuple(f"{stage}={agent}" for stage, agent in sorted(assignments.items())),
        )

    def decide(
        self,
        profiles,
        runtimes: Mapping[str, RuntimeStatus],
        complexity: Complexity | str,
        budget: TaskBudget,
        usage: BudgetUsage,
        mode: str = "AUTO",
    ) -> DualAgentDecision:
        """带预算前置条件的 architect/coder dual 决策。

        gate 顺序：合格性（READY runtime）-> coder 必须存在 ->
        剩余预算必须覆盖该复杂度所需的调用数 -> 差异化 + 专业化
        决定 TWO 还是 SINGLE。"""
        complexity = Complexity(complexity)
        eligible = tuple(
            profile for profile in sorted(profiles, key=lambda item: item.agent_id)
            if self._eligible(profile, runtimes)
        )
        if not eligible:
            return DualAgentDecision(mode, complexity.value, False, None, None, None, None, (), (), DecisionReason.NO_CAPABLE_AGENT)

        coder = self._best_for(eligible, "coder")
        if coder is None:
            return DualAgentDecision(mode, complexity.value, False, None, None, None, None, (), (), DecisionReason.NO_CAPABLE_AGENT)
        architect = self._best_for(eligible, "architect") or coder

        remaining_calls = max(0, budget.max_agent_calls - usage.total_agent_calls)
        required_calls = _REQUIRED_CALLS.get(complexity, 1)
        if remaining_calls < required_calls:
            return DualAgentDecision(
                mode, complexity.value, False, architect.agent_id, coder.agent_id,
                architect.runtime_id, coder.runtime_id,
                self._fallbacks_for(eligible, "architect", architect.agent_id),
                self._fallbacks_for(eligible, "coder", coder.agent_id),
                DecisionReason.BUDGET_INSUFFICIENT,
            )

        distinct = architect.agent_id != coder.agent_id
        specialization = self._specialization(architect, coder)

        if complexity is Complexity.SIMPLE:
            use_dual = False
            reason = DecisionReason.SIMPLE_TASK
        elif complexity is Complexity.MEDIUM:
            use_dual = distinct and remaining_calls >= 2 and specialization >= _SPECIALIZATION_THRESHOLD
            reason = DecisionReason.TWO_CAPABLE_AGENTS if use_dual else (
                DecisionReason.INSUFFICIENT_DIVERSITY if distinct else DecisionReason.SINGLE_CAPABLE_AGENT
            )
        else:
            use_dual = distinct and remaining_calls >= 2
            reason = DecisionReason.TWO_CAPABLE_AGENTS if use_dual else (
                DecisionReason.INSUFFICIENT_DIVERSITY if distinct else DecisionReason.SINGLE_CAPABLE_AGENT
            )

        return DualAgentDecision(
            mode, complexity.value, use_dual,
            architect.agent_id, coder.agent_id,
            architect.runtime_id, coder.runtime_id,
            self._fallbacks_for(eligible, "architect", architect.agent_id),
            self._fallbacks_for(eligible, "coder", coder.agent_id),
            reason,
        )

    def to_selection_result(self, decision: DualAgentDecision) -> DualAgentSelectionResult:
        """把 DualAgentDecision 桥接为 orchestrator/execution engine
        消费的 stage->agent 指派形态。阶段亲和性：tester 工作归
        coder 侧 agent，review 工作归 architect 侧 agent。"""
        complexity = Complexity(decision.complexity)
        assignments: dict[str, str | None] = {}
        for stage in self._stages(complexity):
            if stage == "architect" or stage == "review":
                assignments[stage] = decision.architect_agent_id
            else:
                assignments[stage] = decision.coder_agent_id
        if decision.architect_agent_id is None and decision.coder_agent_id is None:
            return DualAgentSelectionResult(DualAgentMode.NO_AGENT, {}, None, None, DecisionReason.NO_CAPABLE_AGENT)
        mode = DualAgentMode.TWO_AGENT if decision.use_dual_agent else DualAgentMode.SINGLE_AGENT
        secondary = decision.coder_agent_id if decision.use_dual_agent else None
        return DualAgentSelectionResult(
            mode, assignments, decision.architect_agent_id, secondary, decision.reason,
            tuple(f"{stage}={agent}" for stage, agent in sorted(assignments.items())),
        )

    @staticmethod
    def _specialization(architect: AgentProfile, coder: AgentProfile) -> float:
        arch_on_arch = DualAgentSelection._score_for(architect, _STAGE_REQUIREMENTS["architect"])
        coder_on_arch = DualAgentSelection._score_for(coder, _STAGE_REQUIREMENTS["architect"])
        arch_on_coding = DualAgentSelection._score_for(architect, _STAGE_REQUIREMENTS["coder"])
        coder_on_coding = DualAgentSelection._score_for(coder, _STAGE_REQUIREMENTS["coder"])
        if None in (arch_on_arch, coder_on_arch, arch_on_coding, coder_on_coding):
            return 0.0
        architect_gain = arch_on_arch - coder_on_arch
        coder_gain = coder_on_coding - arch_on_coding
        if architect_gain <= 0 or coder_gain <= 0:
            return 0.0
        return architect_gain + coder_gain

    @staticmethod
    def _fallbacks_for(eligible, stage: str, exclude: str) -> tuple[str, ...]:
        required = _STAGE_REQUIREMENTS[stage]
        scored = []
        for profile in eligible:
            if profile.agent_id == exclude:
                continue
            score = DualAgentSelection._score_for(profile, required)
            if score is None:
                continue
            scored.append((-score, profile.agent_id))
        return tuple(agent_id for _, agent_id in sorted(scored))

    @staticmethod
    def _stages(complexity: Complexity) -> tuple[str, ...]:
        if complexity is Complexity.MEDIUM:
            return ("coder", "test")
        if complexity is Complexity.COMPLEX:
            return ("architect", "coder", "test", "review")
        return ("coder",)

    @staticmethod
    def _eligible(profile: AgentProfile, runtimes: Mapping[str, RuntimeStatus]) -> bool:
        status = runtimes.get(profile.runtime_id)
        return status is not None and status.status is RuntimeState.READY

    @staticmethod
    def _score_for(profile: AgentProfile, required: tuple[CapabilityName, ...]) -> float | None:
        evidence = [profile.capabilities.get(capability) for capability in required]
        if any(item is None or item.confidence is CapabilityConfidence.UNKNOWN for item in evidence):
            return None
        score = sum(item.score or 0.0 for item in evidence) / len(evidence)
        return score + (profile.historical_success_rate or 0.0) * 0.1

    @staticmethod
    def _best_for(eligible, stage: str):
        best = None
        for profile in eligible:
            score = DualAgentSelection._score_for(profile, _STAGE_REQUIREMENTS[stage])
            if score is None:
                continue
            if best is None or score > best[0]:
                best = (score, profile)
        return best[1] if best else None
