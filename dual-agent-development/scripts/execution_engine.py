from dataclasses import dataclass
from enum import Enum

from external_runtime import ExternalAgentRequest, InvocationStatus
from task_budget import BudgetExceeded
from loop_guard import GuardDecision
from handoff_context import HandoffContext, HandoffError


class ExecutionStatus(str, Enum):
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"


@dataclass(frozen=True)
class ExecutionResult:
    status: ExecutionStatus
    traces: tuple
    outputs: tuple
    errors: tuple[str, ...]
    packets: tuple = ()


class ExecutionEngine:
    def __init__(self, adapters, runtimes, budget, usage, loop_guard, fallback):
        self.adapters = dict(adapters)
        self.runtimes = dict(runtimes)
        self.budget = budget
        self.usage = usage
        self.loop_guard = loop_guard
        self.fallback = fallback

    def execute(self, plan, prompt: str):
        context = HandoffContext(plan.task_id)
        traces=[]; outputs=[]; errors=[]
        for stage in plan.stages:
            agent_id = stage.agent_id
            runtime_id = getattr(stage, "runtime_id", None) or agent_id
            status = self.runtimes.get(runtime_id)
            if status is None or status.status.value != "READY":
                return ExecutionResult(ExecutionStatus.FAILED, tuple(traces), tuple(outputs), ("RUNTIME_NOT_READY",), context.packets())
            guard = self.loop_guard.check(plan.task_id, stage.stage, agent_id)
            if guard != GuardDecision.ALLOW:
                return ExecutionResult(ExecutionStatus.FAILED, tuple(traces), tuple(outputs), ("LOOP_GUARD", guard), context.packets())
            try:
                handoff_input = context.input_for(stage.stage)
            except HandoffError as exc:
                return ExecutionResult(ExecutionStatus.FAILED, tuple(traces), tuple(outputs), (str(exc),), context.packets())
            try:
                self.budget.reserve_call(self.usage, stage.role)
            except BudgetExceeded:
                return ExecutionResult(ExecutionStatus.FAILED, tuple(traces), tuple(outputs), ("BUDGET_EXHAUSTED",), context.packets())
            self.loop_guard.record(plan.task_id, stage.stage, agent_id)
            result = self.adapters[agent_id].invoke(ExternalAgentRequest(
                plan.task_id, prompt, agent_id, stage.role, status.provider, status.model,
                self.budget.timeout_seconds or 120, self._handoff_tuple(handoff_input),
            ))
            if result.trace is not None:
                traces.append(result.trace)
            if result.status is InvocationStatus.SUCCESS:
                outputs.append(result.output)
                try:
                    context = context.accept(stage.stage, result.output)
                except HandoffError as exc:
                    return ExecutionResult(ExecutionStatus.FAILED, tuple(traces), tuple(outputs), (str(exc),), context.packets())
                continue
            errors.append("INVOKE_FAILED")
            self.loop_guard.record_failure(plan.task_id, stage.stage, agent_id, "invoke_failed")
            fallback = self.fallback.select(agent_id, self.runtimes, set(), budget_available=self.usage.total_agent_calls < self.budget.max_agent_calls)
            if fallback.agent_id is None or fallback.agent_id not in self.adapters:
                errors.append(getattr(fallback.reason, "value", str(fallback.reason)))
                return ExecutionResult(ExecutionStatus.FAILED, tuple(traces), tuple(outputs), tuple(errors), context.packets())
            backup_profile = next((profile for profile in self.fallback._agents if profile.agent_id == fallback.agent_id), None)
            backup_status = self.runtimes[backup_profile.runtime_id]
            try:
                self.budget.reserve_call(self.usage, stage.role)
            except BudgetExceeded:
                errors.append("BUDGET_EXHAUSTED")
                return ExecutionResult(ExecutionStatus.FAILED, tuple(traces), tuple(outputs), tuple(errors), context.packets())
            backup = self.adapters[fallback.agent_id].invoke(ExternalAgentRequest(
                plan.task_id, prompt, fallback.agent_id, stage.role, backup_status.provider, backup_status.model,
                self.budget.timeout_seconds or 120, self._handoff_tuple(handoff_input),
            ))
            if backup.trace is not None:
                traces.append(backup.trace)
            if backup.status is not InvocationStatus.SUCCESS:
                errors.append("FALLBACK_FAILED")
                return ExecutionResult(ExecutionStatus.FAILED, tuple(traces), tuple(outputs), tuple(errors), context.packets())
            try:
                context = context.accept(stage.stage, backup.output)
            except HandoffError as exc:
                errors.append(str(exc))
                return ExecutionResult(ExecutionStatus.FAILED, tuple(traces), tuple(outputs), tuple(errors), context.packets())
            outputs.append(backup.output)
        return ExecutionResult(ExecutionStatus.SUCCESS, tuple(traces), tuple(outputs), tuple(errors), context.packets())

    @staticmethod
    def _handoff_tuple(handoff_input):
        if handoff_input is None:
            return ()
        if isinstance(handoff_input, tuple):
            return handoff_input
        return (handoff_input,)
