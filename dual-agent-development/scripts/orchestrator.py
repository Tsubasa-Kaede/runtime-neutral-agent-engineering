"""经典单路径 Orchestrator（Phase 6-7 规划层）。

引擎中三个 orchestrator 之一，职责刻意区分 —— 本模块只做规划，
绝不发起调用：
- Orchestrator（本模块）：基于 CapabilityRegistry 的经典单路径
  （ReadyPool 选择）：mode+complexity -> stage 规格 -> 能力选择 ->
  InvocationPlan，交由 ExecutionEngine 执行。
- CollaborationOrchestrator：路由 SINGLE 与 DUAL 协作
  （architect+coder 会话）并写入共享 ledger。
- VerifiedOrchestrator：基于 VerifiedRuntimePool 的 verified 路径；
  绝不借用本模块的 ready-pool 选择。

complexity -> 阶段映射（固定词汇）：SIMPLE -> coder；
MEDIUM -> coder + test；COMPLEX -> architect + coder + test + review。
规划时的 budget/guard 检查是塑造 plan 的咨询性 gate；真正的预留与
guard 记录只发生在 ExecutionEngine（reserve-before-invoke）。
结构化 reason 词汇：NO_CAPABLE_AGENT, BUDGET_EXHAUSTED,
LOOP_GUARD_REJECTED。
"""
from invocation_plan import InvocationPlan, StagePlan
from mode_gate import Mode, ModeGate
from task_classifier import Complexity
from capability_registry import CapabilityName


class Orchestrator:
    """规划经典路径；执行委托给 ExecutionEngine。"""

    def __init__(self, capability_registry, runtimes, budget, usage, loop_guard):
        self.registry = capability_registry
        self.runtimes = dict(runtimes)
        self.budget = budget
        self.usage = usage
        self.loop_guard = loop_guard

    def plan(self, task_id, task, mode=Mode.AUTO, dual_selection=None):
        decision = ModeGate().decide(mode, task)
        if decision.mode.value == "OFF":
            return InvocationPlan(task_id, decision.mode.value, decision.complexity.value, (), (), (), self.budget.to_dict(), (decision.reason,))
        if decision.complexity is Complexity.SIMPLE:
            # SIMPLE 永远走快速单 coder 路径，即使在 ON 模式下 ——
            # 单文件任务从不需要四阶段机制。
            stage_specs = self._stages(Complexity.SIMPLE)
        elif not decision.use_orchestrator:
            return InvocationPlan(task_id, decision.mode.value, decision.complexity.value, (), (), (), self.budget.to_dict(), (decision.reason,))
        else:
            stage_specs = self._stages(decision.complexity)
        if dual_selection is not None and dual_selection.decision.value == "NO_AGENT":
            return InvocationPlan(task_id, decision.mode.value, decision.complexity.value, (), (), (), self.budget.to_dict(), ("NO_CAPABLE_AGENT",))
        stages=[]; selected=[]; reasons=[]
        for stage, role, caps in stage_specs:
            if dual_selection is not None:
                # 预先做好的 dual 决策直接提供指派；规划器只检查
                # 每个阶段是否有一个存在。
                agent_id = dual_selection.assignments.get(stage)
                if agent_id is None:
                    reasons.append("NO_CAPABLE_AGENT")
                    continue
            else:
                result = self.registry.select(set(caps), self.runtimes, role=None)
                if result.agent_id is None:
                    # 本阶段没有有能力的 agent：记录 reason 并继续
                    # 规划其余阶段（与下方 budget/guard 不同，能力
                    # 缺口不中止循环）。
                    reasons.append("NO_CAPABLE_AGENT")
                    continue
                agent_id = result.agent_id
            # 咨询性 budget/guard gate：耗尽或 guard 拒绝使"其余的
            # plan"不可能 —— 规划就此停止，plan 绝不承诺生命周期
            # 支付不起的调用。
            if self.usage.total_agent_calls >= self.budget.max_agent_calls:
                reasons.append("BUDGET_EXHAUSTED")
                break
            if self.loop_guard.check(task_id, stage, agent_id) != "ALLOW":
                reasons.append("LOOP_GUARD_REJECTED")
                break
            if dual_selection is not None:
                runtime_id = next(
                    (candidate.runtime_id for candidate in getattr(self.registry, "_agents", ()) if candidate.agent_id == agent_id),
                    None,
                )
            else:
                runtime_id = next(
                    (candidate.runtime_id for candidate in result.candidates if candidate.agent_id == agent_id),
                    None,
                )
            stages.append(StagePlan(stage, role, agent_id, tuple(sorted(cap.value for cap in caps)), "dual_agent_selection" if dual_selection is not None else "capability_selection", runtime_id))
            selected.append(agent_id)
        if not stages and not reasons:
            reasons.append("NO_CAPABLE_AGENT")
        return InvocationPlan(task_id, decision.mode.value, decision.complexity.value, tuple(stages), tuple(selected), (), self.budget.to_dict(), tuple(reasons))

    def execute(self, task_id, task, adapters, prompt, mode=Mode.AUTO, dual_selection=None):
        # 不可规划的 任务带着 plan 的 reasons 诚实失败；绝不作为
        # 空成功被执行。
        from execution_engine import ExecutionEngine
        from fallback_policy import FallbackPolicy
        plan = self.plan(task_id, task, mode, dual_selection=dual_selection)
        if not plan.stages:
            from execution_engine import ExecutionResult, ExecutionStatus
            return ExecutionResult(ExecutionStatus.FAILED, (), (), plan.reasons or ("NO_EXECUTABLE_STAGE",))
        engine = ExecutionEngine(adapters, self.runtimes, self.budget, self.usage, self.loop_guard, FallbackPolicy(getattr(self.registry, "_agents", ())))
        return engine.execute(plan, prompt)

    @staticmethod
    def _stages(complexity):
        # complexity -> (stage, role, 必需能力)：经典路径阶段词汇的
        # 唯一来源。
        if complexity is Complexity.SIMPLE:
            return (("coder", "coder", (CapabilityName.CODING,)),)
        if complexity is Complexity.MEDIUM:
            return (("coder", "coder", (CapabilityName.CODING,)), ("test", "test", (CapabilityName.TESTING,)))
        if complexity is Complexity.COMPLEX:
            return (("architect", "architect", (CapabilityName.ARCHITECTURE,)), ("coder", "coder", (CapabilityName.CODING,)), ("test", "test", (CapabilityName.TESTING,)), ("review", "review", (CapabilityName.REVIEW,)))
        return ()

