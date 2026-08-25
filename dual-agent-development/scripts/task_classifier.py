"""确定性关键词任务分类器 —— 封闭规则表，不是 ML。

Complexity 由"小写任务文本中的精确关键词包含"决定，并按固定优先
级检查（先 COMPLEX 关键词，再 MEDIUM，再 SIMPLE）：第一个命中的
层级获胜。因此关键词列表、其顺序与优先级本身就是可观察的路由
契约 —— 改动其中任何一项都会改变路由行为。

UNRESOLVED 表示"无关键词命中"。它不是错误，也明确不是 SIMPLE：
路由层将 UNRESOLVED 任务送入编排路径而不是猜测快速路径，因此
无法分类的任务要么失败、要么走完整机制 —— 绝不被静默地误路由
到快捷路径。
"""
from enum import Enum


class Complexity(str, Enum):
    """封闭的复杂度词汇，被所有路由层消费。"""

    SIMPLE = "SIMPLE"
    MEDIUM = "MEDIUM"
    COMPLEX = "COMPLEX"
    UNRESOLVED = "UNRESOLVED"


def classify_task(task: str) -> Complexity:
    if not isinstance(task, str) or not task.strip():
        # 没有任务文本就没有路由猜测的依据。
        return Complexity.UNRESOLVED
    text = task.lower()
    if any(word in text for word in ("redesign architecture", "cross-module", "across modules", "complex migration", "migrate entire")):
        return Complexity.COMPLEX
    if any(word in text for word in ("two files", "multiple files", "related files", "add tests", "cross-file")):
        return Complexity.MEDIUM
    if any(word in text for word in ("one function", "one config", "one file", "simple bug", "fix one")):
        return Complexity.SIMPLE
    return Complexity.UNRESOLVED
