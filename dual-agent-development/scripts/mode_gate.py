"""Mode gate：基于任务复杂度的 OFF / AUTO / ON 路由决策。

OFF 完全关闭编排（返回被委托的空结果，绝不静默运行）。ON 不论
复杂度强制走编排路径。AUTO 按分类路由：SIMPLE 走快速单 agent
路径；其余 —— MEDIUM、COMPLEX 以及 UNRESOLVED —— 全部进入
编排路径，因为 UNRESOLVED 任务绝不能被静默当作 SIMPLE；
向上兜底才是诚实的方向。reason 词汇（MODE_OFF / MODE_ON /
FAST_PATH / MODE_AUTO）是封闭、可上报的集合。
"""
from dataclasses import dataclass
from enum import Enum

from task_classifier import Complexity, classify_task


class Mode(str, Enum):
    """调用方意图词汇；与 complexity 正交。"""

    AUTO = "AUTO"
    ON = "ON"
    OFF = "OFF"


@dataclass(frozen=True)
class ModeDecision:
    """一次路由决策：resolved mode + 分类结果 + 是否走编排路径，
    附带封闭的 reason 字符串。"""

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
        # AUTO：只有确认为 SIMPLE 的分类才走快速路径；
        # UNRESOLVED 刻意落入编排路径。
        use = complexity is not Complexity.SIMPLE
        return ModeDecision(mode, complexity, use, "FAST_PATH" if not use else "MODE_AUTO")
