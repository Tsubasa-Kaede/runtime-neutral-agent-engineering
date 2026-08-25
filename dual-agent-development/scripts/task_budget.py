"""任务生命周期预算核算，限制不可变。

一个 TaskBudget 跨越一个任务生命周期；用量只能通过
reserve/record API 变动。核心不变量是 reserve-before-invoke：
调用名额在任何调用之前被预留（耗尽即抛出异常），因此 agent
调用要么已被支付、要么从未发生，上游 gate 失败绝不消耗预算。
Token 计数默认诚实地为 "unknown" —— 无法观测用量的 adapter
上报字面量，绝不猜测。
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any


class BudgetExceeded(RuntimeError):
    """预留命中上限时抛出；message 是封闭的上限词汇
    （MAX_AGENT_CALLS / MAX_ITERATIONS / MAX_TOTAL_*_TOKENS /
    MAX_ESCALATIONS）。"""

    pass


@dataclass(frozen=True)
class TaskBudget:
    """不可变的限制；token 上限为 None 表示该维度无界，
    timeout_seconds 是交给 adapter 的单次调用时限。"""

    max_agent_calls: int
    max_iterations: int
    max_total_input_tokens: int | None = None
    max_total_output_tokens: int | None = None
    max_context_tokens_per_call: int | None = None
    timeout_seconds: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TaskBudget":
        return cls(**data)

    def reserve_call(self, usage: "BudgetUsage", role: str) -> None:
        # Reserve-before-invoke：在调用发生之前抛出；未知 role 属于
        # 调用方 bug，直接拒绝，绝不静默计入一个编造的桶。
        if usage.total_agent_calls >= self.max_agent_calls:
            raise BudgetExceeded("MAX_AGENT_CALLS")
        if role not in usage._role_counts:
            raise ValueError(f"unknown agent role: {role}")
        usage.total_agent_calls += 1
        usage._role_counts[role] += 1

    def reserve_iteration(self, usage: "BudgetUsage") -> None:
        if usage.iterations_used >= self.max_iterations:
            raise BudgetExceeded("MAX_ITERATIONS")
        usage.iterations_used += 1


class BudgetUsage:
    """一个生命周期的可变计数器；只有 TaskBudget 能改动它们。"""

    def __init__(self) -> None:
        self.total_agent_calls = 0
        self.iterations_used = 0
        self.total_input_tokens: int | str = "unknown"
        self.total_output_tokens: int | str = "unknown"
        self.escalation_count = 0
        # 封闭的 role 词汇 —— 按角色的桶是核算契约的一部分；
        # 词汇之外的 role 会使 reserve_call 失败。
        self._role_counts = {
            "classification": 0,
            "architect": 0,
            "coder": 0,
            "test": 0,
            "review": 0,
            "fix": 0,
        }

    classification_calls = property(lambda self: self._role_counts["classification"])
    architect_calls = property(lambda self: self._role_counts["architect"])
    coder_calls = property(lambda self: self._role_counts["coder"])
    test_calls = property(lambda self: self._role_counts["test"])
    review_calls = property(lambda self: self._role_counts["review"])
    fix_calls = property(lambda self: self._role_counts["fix"])

    def record_tokens(self, input_tokens: int | str, output_tokens: int | str, budget: TaskBudget | None = None) -> None:
        # "unknown" 是诚实的默认值，不参与累计；只有观测到的
        # 非负整数才计数，且上限在记录的那一刻即抛出，
        # 绝不事后补报。
        if input_tokens != "unknown":
            if not isinstance(input_tokens, int) or input_tokens < 0:
                raise ValueError("input_tokens must be a non-negative integer or unknown")
            new_value = (self.total_input_tokens if isinstance(self.total_input_tokens, int) else 0) + input_tokens
            if budget and budget.max_total_input_tokens is not None and new_value > budget.max_total_input_tokens:
                raise BudgetExceeded("MAX_TOTAL_INPUT_TOKENS")
            self.total_input_tokens = new_value
        if output_tokens != "unknown":
            if not isinstance(output_tokens, int) or output_tokens < 0:
                raise ValueError("output_tokens must be a non-negative integer or unknown")
            new_value = (self.total_output_tokens if isinstance(self.total_output_tokens, int) else 0) + output_tokens
            if budget and budget.max_total_output_tokens is not None and new_value > budget.max_total_output_tokens:
                raise BudgetExceeded("MAX_TOTAL_OUTPUT_TOKENS")
            self.total_output_tokens = new_value

    def record_escalation(self, max_escalations: int | None = None) -> None:
        if max_escalations is not None and self.escalation_count >= max_escalations:
            raise BudgetExceeded("MAX_ESCALATIONS")
        self.escalation_count += 1
