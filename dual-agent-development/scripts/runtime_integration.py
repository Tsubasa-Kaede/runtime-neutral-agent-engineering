"""Runtime 中立的 Phase 7 规划与执行桥（经典路径）。

经典（CLASSIC）栈的组合入口：CapabilityRegistry + Orchestrator +
ExecutionEngine + FallbackPolicy，作用于调用方提供的
adapter/profile/status 字典。它不是 facade —— 不做任何结果投影，
也不了解协作栈或四阶段链。带 verified 准入、协作 session 与封闭
FacadeResult 的生产组合是 host.build_facade /
build_facade_from_bootstrap；本模块服务于经典/示例路径，除
plan->execute 接线（包括从 agent 的 profile 解析每个阶段的
runtime_id）之外不添加任何自身行为。
"""
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
        # 薄委托：规划属于经典 Orchestrator；本桥只负责为每个阶段
        # 回填 runtime_id（registry 路径规划的是 agent，engine 需要
        # 的是 runtime）。
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
        # 不可规划的 plan 在 engine 运行之前诚实失败；
        # 执行本身带真实 fallback 策略进行委托。
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
