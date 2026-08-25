"""规划输出契约：只描述"执行什么"，绝不描述"执行得如何"。

InvocationPlan 是规划层产出的纯数据 —— 它选择 agent、塑造阶段，
但不执行任何东西。执行层以只读方式消费它，这正是"选了谁、为什么"
能够与"实际发生了什么"分开审视的原因。
"""
from dataclasses import dataclass, asdict
from typing import Any
import json


@dataclass(frozen=True)
class StagePlan:
    """单个阶段的指派。`reason` 是封闭的选择来源词汇（例如
    capability_selection / dual_agent_selection / bridged_from_selection）；
    runtime_id 仅在经典 registry 路径由 agent profile 隐式给出的
    场景下才允许为 None。"""

    stage: str
    role: str
    agent_id: str
    required_capabilities: tuple[str, ...]
    reason: str
    runtime_id: str | None = None


@dataclass(frozen=True)
class InvocationPlan:
    """一份完整 plan：要运行的阶段、选中的 agent、以及原因。

    reasons 是"空 plan"的结构化失败词汇（NO_CAPABLE_AGENT /
    BUDGET_EXHAUSTED / LOOP_GUARD_REJECTED / EMPTY_SELECTION /
    MISSING_STAGE:... —— 由调用方归一化）；budget_snapshot 是
    规划时刻的即时副本，因此后续的用量绝不会改写这份 plan 当时
    所依据的预算。"""

    task_id: str
    mode: str
    complexity: str
    stages: tuple[StagePlan, ...]
    selected_agents: tuple[str, ...]
    fallback_agents: tuple[str, ...]
    budget_snapshot: dict[str, Any]
    reasons: tuple[str, ...]

    def to_dict(self):
        return asdict(self)

    def to_json(self):
        # 规范化的确定性 wire 形态（key 排序、紧凑分隔符）——
        # 与 packet 序列化同一约定。
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
