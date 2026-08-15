from invocation_plan import InvocationPlan, StagePlan
from mode_gate import Mode, ModeGate
from task_classifier import Complexity
from capability_registry import CapabilityName


class Orchestrator:
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
                agent_id = dual_selection.assignments.get(stage)
                if agent_id is None:
                    reasons.append("NO_CAPABLE_AGENT")
                    continue
            else:
                result = self.registry.select(set(caps), self.runtimes, role=None)
                if result.agent_id is None:
                    reasons.append("NO_CAPABLE_AGENT")
                    continue
                agent_id = result.agent_id
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
        if complexity is Complexity.SIMPLE:
            return (("coder", "coder", (CapabilityName.CODING,)),)
        if complexity is Complexity.MEDIUM:
            return (("coder", "coder", (CapabilityName.CODING,)), ("test", "test", (CapabilityName.TESTING,)))
        if complexity is Complexity.COMPLEX:
            return (("architect", "architect", (CapabilityName.ARCHITECTURE,)), ("coder", "coder", (CapabilityName.CODING,)), ("test", "test", (CapabilityName.TESTING,)), ("review", "review", (CapabilityName.REVIEW,)))
        return ()

