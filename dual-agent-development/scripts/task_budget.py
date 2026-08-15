"""Task-lifecycle budget accounting with immutable limits."""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any


class BudgetExceeded(RuntimeError):
    pass


@dataclass(frozen=True)
class TaskBudget:
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
    def __init__(self) -> None:
        self.total_agent_calls = 0
        self.iterations_used = 0
        self.total_input_tokens: int | str = "unknown"
        self.total_output_tokens: int | str = "unknown"
        self.escalation_count = 0
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
