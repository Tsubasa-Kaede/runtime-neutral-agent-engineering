"""单路径执行引擎：从 InvocationPlan 到调用的 gate 链。

逐阶段执行一个 InvocationPlan。每个阶段都经过固定的起飞前检查
顺序 Health -> LoopGuard -> Handoff -> Budget reserve -> Invoke，
第一个失败的 gate 以其结构化 reason（RUNTIME_NOT_READY /
LOOP_GUARD / MISSING_HANDOFF / BUDGET_EXHAUSTED）终止该阶段 ——
不跳过任何 gate，也不把任何失败包装成成功。

不变量：
- Reserve-before-invoke：只有全部前置 gate 通过后才预留预算，且
  严格发生在 adapter 调用之前 —— gate 失败绝不消耗预算，调用
  绝不脱离核算进行。
- Health 在 EXECUTION 时刻重新检查，绝不沿用 planning 时刻的
  结论 —— 计划与执行之间的时间差可能使 runtime 失效。
- 阶段的输入是上游结构化 packet（HandoffContext），绝不是其它
  阶段的原始输出；阶段的输出只有通过 packet 验证后才进入
  context。
- Fallback 边界：失败的调用恰好获得一次（EXACTLY ONE）经由注入
  FallbackPolicy 的备用尝试。ReadyPool 路径提供真实候选；verified
  路径注入空策略，因此 verified candidate 绝不 fallback —— 其失败
  以 NO_FALLBACK_AGENT 诚实暴露。
"""
from dataclasses import dataclass
from enum import Enum

from external_runtime import ExternalAgentRequest, InvocationStatus
from task_budget import BudgetExceeded
from loop_guard import GuardDecision
from handoff_context import HandoffContext, HandoffError


class ExecutionStatus(str, Enum):
    # 刻意保持粗粒度：细粒度 reason 存放在 ExecutionResult.errors
    # （结构化词汇）中，调用方无法依据引擎内部分支，
    # 而 host 仍能诚实上报。
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"


@dataclass(frozen=True)
class ExecutionResult:
    """一次 plan 执行的封闭结果；packets 均为已验证的值。"""

    status: ExecutionStatus
    traces: tuple
    outputs: tuple
    errors: tuple[str, ...]
    packets: tuple = ()


class ExecutionEngine:
    """驱动已规划的阶段穿过 gate 链；自身绝不进行规划。"""

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
            # Gate 1 —— Health：执行时刻的可用性。planning 时 READY
            # 的 runtime 可能已失效；此处绝不信任陈旧 Health，非
            # READY 的 runtime 终止整个 plan（后续阶段依赖本阶段
            # 输出）。
            status = self.runtimes.get(runtime_id)
            if status is None or status.status.value != "READY":
                return ExecutionResult(ExecutionStatus.FAILED, tuple(traces), tuple(outputs), ("RUNTIME_NOT_READY",), context.packets())
            # Gate 2 —— LoopGuard：在花任何资源之前检查。guard 必须
            # 在 Budget 之前、调用之前被咨询，使被拒绝的重复/环
            # 绝不消耗预算、绝不发起调用。下方 `record` 补全
            # check/record 配对。
            guard = self.loop_guard.check(plan.task_id, stage.stage, agent_id)
            if guard != GuardDecision.ALLOW:
                return ExecutionResult(ExecutionStatus.FAILED, tuple(traces), tuple(outputs), ("LOOP_GUARD", guard), context.packets())
            # Gate 3 —— Handoff：该阶段的输入是上游 packet。上游
            # 事实缺失是诚实的终止（MISSING_HANDOFF）—— 绝不
            # 静默地给一个空输入。
            try:
                handoff_input = context.input_for(stage.stage)
            except HandoffError as exc:
                return ExecutionResult(ExecutionStatus.FAILED, tuple(traces), tuple(outputs), (str(exc),), context.packets())
            # Gate 4 —— Budget：reserve-before-invoke。预算耗尽时
            # reserve_call 抛出异常；下方调用只能在已预留的
            # 名额上运行。
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
                # 输出只有在通过 packet 验证后才被接受；无法解析进
                # 该阶段 packet 契约的原始文本在此失败
                # （PACKET_VALIDATION_FAILED），而不是污染下一阶段
                # 的输入。
                try:
                    context = context.accept(stage.stage, result.output)
                except HandoffError as exc:
                    return ExecutionResult(ExecutionStatus.FAILED, tuple(traces), tuple(outputs), (str(exc),), context.packets())
                continue
            # 调用失败：先记录失败签名（同类别重发即
            # REPEATED_FAILURE），然后若存在策略候选则恰好允许
            # 一次 fallback 尝试。
            errors.append("INVOKE_FAILED")
            self.loop_guard.record_failure(plan.task_id, stage.stage, agent_id, "invoke_failed")
            fallback = self.fallback.select(agent_id, self.runtimes, set(), budget_available=self.usage.total_agent_calls < self.budget.max_agent_calls)
            if fallback.agent_id is None or fallback.agent_id not in self.adapters:
                errors.append(getattr(fallback.reason, "value", str(fallback.reason)))
                return ExecutionResult(ExecutionStatus.FAILED, tuple(traces), tuple(outputs), tuple(errors), context.packets())
            backup_profile = next((profile for profile in self.fallback._agents if profile.agent_id == fallback.agent_id), None)
            backup_status = self.runtimes[backup_profile.runtime_id]
            # 备用调用是全新的一次预留（失败那次的名额已被消耗）；
            # 此处再耗尽即为终态。
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
                # 只允许一次 fallback 尝试 —— 第二次失败即终态
                # （FALLBACK_FAILED）；引擎绝不级联 fallback。
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
