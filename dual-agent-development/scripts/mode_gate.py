from dataclasses import dataclass
from enum import Enum

from task_classifier import Complexity, classify_task


class Mode(str, Enum):
    AUTO = "AUTO"
    ON = "ON"
    OFF = "OFF"


@dataclass(frozen=True)
class ModeDecision:
    mode: Mode
    complexity: Complexity
    use_orchestrator: bool
    reason: str


class ModeGate:
    def decide(self, mode: Mode | str, task: str) -> ModeDecision:
        mode = Mode(mode)
        complexity = classify_task(task)
        if mode is Mode.OFF:
            return ModeDecision(mode, complexity, False, "MODE_OFF")
        if mode is Mode.ON:
            return ModeDecision(mode, complexity, True, "MODE_ON")
        use = complexity is not Complexity.SIMPLE
        return ModeDecision(mode, complexity, use, "FAST_PATH" if not use else "MODE_AUTO")
