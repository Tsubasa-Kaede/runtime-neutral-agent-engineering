"""Bridge RuntimeSelectionResult into the existing InvocationPlan.

Pure translation of an already-made selection: stage -> runtime/agent is
copied verbatim into StagePlan entries. No rescoring, no reselection, no
fallback, no invocation. Illegal inputs become a plan with empty stages and
deterministic, secret-free error reasons — never a fabricated runtime.
"""
from __future__ import annotations

from invocation_plan import InvocationPlan, StagePlan
from stage_runtime_selection import RuntimeSelectionResult, StageRuntimeSelector
from task_classifier import Complexity
from capability_registry import CapabilityName

_STAGE_CAPABILITIES = {
    "architect": (CapabilityName.ARCHITECTURE,),
    "coder": (CapabilityName.CODING,),
    "test": (CapabilityName.TESTING,),
    "review": (CapabilityName.REVIEW,),
}


def bridge_selection(selection, task_id, mode, complexity, budget, usage) -> InvocationPlan:
    complexity = Complexity(complexity)
    expected_stages = StageRuntimeSelector._stages(complexity)

    def rejected(reason: str) -> InvocationPlan:
        return InvocationPlan(task_id, mode, complexity.value, (), (), (), budget.to_dict(), (reason,))

    if not selection.stage_selections:
        return rejected("EMPTY_SELECTION")

    provided = [item.stage for item in selection.stage_selections]
    for stage_name in provided:
        if stage_name not in _STAGE_CAPABILITIES:
            return rejected(f"UNKNOWN_STAGE:{stage_name}")
    for stage_name in expected_stages:
        if stage_name not in provided:
            return rejected(f"MISSING_STAGE:{stage_name}")
    for item in selection.stage_selections:
        if not item.runtime_id:
            return rejected(f"MISSING_RUNTIME_ID:{item.stage}")
        if not item.agent_id:
            return rejected(f"MISSING_AGENT_ID:{item.stage}")

    by_stage = {item.stage: item for item in selection.stage_selections}
    stages = tuple(
        StagePlan(
            stage=stage_name,
            role=stage_name,
            agent_id=by_stage[stage_name].agent_id,
            required_capabilities=tuple(sorted(cap.value for cap in _STAGE_CAPABILITIES[stage_name])),
            reason="bridged_from_selection",
            runtime_id=by_stage[stage_name].runtime_id,
        )
        for stage_name in expected_stages
    )
    selected_agents = tuple(by_stage[stage_name].agent_id for stage_name in expected_stages)
    return InvocationPlan(
        task_id, mode, complexity.value, stages, selected_agents, (), budget.to_dict(), (),
    )
