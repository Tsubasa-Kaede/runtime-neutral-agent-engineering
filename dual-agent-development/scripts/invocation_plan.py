from dataclasses import dataclass, asdict
from typing import Any
import json


@dataclass(frozen=True)
class StagePlan:
    stage: str
    role: str
    agent_id: str
    required_capabilities: tuple[str, ...]
    reason: str
    runtime_id: str | None = None


@dataclass(frozen=True)
class InvocationPlan:
    task_id: str
    mode: str
    complexity: str
    stages: tuple[StagePlan, ...]
    selected_agents: tuple[str, ...]
    fallback_agents: tuple[str, ...]
    budget_snapshot: dict[str, Any]
    reasons: tuple[str, ...]

    def to_dict(self):
        return asdict(self)

    def to_json(self):
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
