"""Runtime-neutral Phase 7 planning and execution bridge."""
from __future__ import annotations

from dataclasses import replace

from capability_registry import CapabilityRegistry
from execution_engine import ExecutionEngine, ExecutionResult, ExecutionStatus
from task_budget import BudgetUsage
from invocation_plan import InvocationPlan
from loop_guard import LoopGuard
from mode_gate import Mode
from fallback_policy import FallbackPolicy


class RuntimeIntegration:
    def __init__(self, adapters, profiles, statuses, budget, usage=None, loop_guard=None):
        self.adapters = dict(adapters)
        self.profiles = tuple(profiles)
        self.statuses = dict(statuses)
        self.budget = budget
        self.usage = usage or BudgetUsage()
        self.loop_guard = loop_guard or LoopGuard(max_iterations=budget.max_iterations)
        self.registry = CapabilityRegistry(self.profiles)

    def plan(self, task_id: str, task: str, mode: Mode | str = Mode.AUTO) -> InvocationPlan:
        from orchestrator import Orchestrator

        plan = Orchestrator(
            capability_registry=self.registry,
            runtimes=self.statuses,
            budget=self.budget,
            usage=self.usage,
            loop_guard=self.loop_guard,
        ).plan(task_id, task, mode=mode)
        stages = tuple(
            replace(stage, runtime_id=self._runtime_for_agent(stage.agent_id))
            for stage in plan.stages
        )
        return replace(plan, stages=stages)

    def execute(self, plan: InvocationPlan, prompt: str):
        if not plan.stages:
            reason = plan.reasons or ("NO_EXECUTABLE_STAGE",)
            return ExecutionResult(ExecutionStatus.FAILED, (), (), tuple(reason))
        return ExecutionEngine(
            adapters=self.adapters,
            runtimes=self.statuses,
            budget=self.budget,
            usage=self.usage,
            loop_guard=self.loop_guard,
            fallback=FallbackPolicy(self.profiles),
        ).execute(plan, prompt)

    def _runtime_for_agent(self, agent_id: str) -> str | None:
        for profile in self.profiles:
            if profile.agent_id == agent_id:
                return profile.runtime_id
        return None
