"""Phase 10F: VerifiedOrchestrator — verified path wired into task planning.

Composes the already-verified chain (VerifiedRuntimePool ->
VerifiedSelectionBridge -> VerifiedStageSelector -> InvocationPlan) under
the existing ModeGate semantics, applies plan-level budget/loop-guard
gating aligned with the classic Orchestrator (advisory only — reservation
stays with ExecutionEngine), and executes through the existing
ExecutionEngine with an empty FallbackPolicy: verified candidates never
fall back, failures surface honestly as NO_FALLBACK_AGENT.
"""
from __future__ import annotations

from execution_engine import ExecutionEngine, ExecutionResult, ExecutionStatus
from fallback_policy import FallbackPolicy
from invocation_plan import InvocationPlan
from mode_gate import Mode, ModeGate
from verified_stage_selector import verified_plan


class VerifiedOrchestrator:
    def __init__(self, pool, current_health, adapters, budget, usage, loop_guard):
        self.pool = pool
        self.current_health = dict(current_health)
        self.adapters = dict(adapters)
        self.budget = budget
        self.usage = usage
        self.loop_guard = loop_guard

    def plan(self, task_id, task, mode=Mode.AUTO):
        decision = ModeGate().decide(mode, task)
        if decision.mode.value == "OFF":
            return self._empty(task_id, decision.mode.value, decision.complexity.value,
                               (decision.reason,))
        if self.pool is None:
            return self._empty(task_id, decision.mode.value, decision.complexity.value,
                               ("VERIFIED_POOL_NOT_ENABLED",))
        plan = verified_plan(self.pool, self.current_health, task_id,
                             decision.mode.value, decision.complexity,
                             self.budget, self.usage)
        if not plan.stages:
            # Reason normalization: an empty verified selection is reported
            # as NO_CAPABLE_AGENT; the verified path never borrows the
            # ready-pool path.
            reasons = tuple(
                "NO_CAPABLE_AGENT" if reason == "EMPTY_SELECTION" else reason
                for reason in plan.reasons
            ) or ("NO_CAPABLE_AGENT",)
            return self._empty(task_id, plan.mode, plan.complexity, reasons)
        # Plan-level gating mirrors the classic Orchestrator: advisory
        # checks only, no usage reservation and no guard recording.
        if self.usage.total_agent_calls >= self.budget.max_agent_calls:
            return self._empty(task_id, plan.mode, plan.complexity, ("BUDGET_EXHAUSTED",))
        for stage in plan.stages:
            if self.loop_guard.check(task_id, stage.stage, stage.agent_id) != "ALLOW":
                return self._empty(task_id, plan.mode, plan.complexity, ("LOOP_GUARD_REJECTED",))
        return plan

    def execute(self, task_id, task, prompt, mode=Mode.AUTO):
        plan = self.plan(task_id, task, mode)
        if not plan.stages:
            return ExecutionResult(ExecutionStatus.FAILED, (), (),
                                   plan.reasons or ("NO_EXECUTABLE_STAGE",))
        engine = ExecutionEngine(
            self.adapters, self.current_health, self.budget, self.usage,
            self.loop_guard, FallbackPolicy(()),
        )
        return engine.execute(plan, prompt)

    def _empty(self, task_id, mode, complexity, reasons):
        return InvocationPlan(task_id, mode, complexity, (), (), (),
                              self.budget.to_dict(), reasons)
