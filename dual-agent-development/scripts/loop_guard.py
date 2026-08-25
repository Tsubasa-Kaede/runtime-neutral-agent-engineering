"""Provider 中立的任务环与升级守卫。

一个 guard 实例跨越一个任务生命周期。与调用方的契约是
check/record 配对：check() 是咨询性预检，必须在消耗预算或调用
agent 之前被咨询；record() 在调用之后补全，使同一
(task, stage, agent) 的第二次 check 变为 DUPLICATE_TASK。
"先 check 后调用"正是重试环代价低的原因 —— 被拒绝的重复绝不
消耗预算或调用名额。判定是封闭词汇：

- MAX_ESCALATIONS / MAX_ITERATIONS —— 生命周期上限先命中。
- REPEATED_FAILURE —— 同一失败类别（hash 前缀）在同一 key 上
  复发；原始诊断文本绝不保留。
- CYCLE_DETECTED —— 振荡模式：候选事件恰好位于历史尾部往前数
  第 4 个位置，即尾部即将以周期 4 重复（紧邻的重复会更早被
  DUPLICATE_TASK 捕获）。
- DUPLICATE_TASK —— 该精确 key 已运行并被 record。
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json


class GuardDecision(str):
    """封闭判定词汇；以 string 为基类以兼容 wire/报告。"""

    ALLOW = "ALLOW"
    DUPLICATE_TASK = "DUPLICATE_TASK"
    REPEATED_FAILURE = "REPEATED_FAILURE"
    CYCLE_DETECTED = "CYCLE_DETECTED"
    MAX_ITERATIONS = "MAX_ITERATIONS"
    MAX_ESCALATIONS = "MAX_ESCALATIONS"


@dataclass(frozen=True)
class GuardEvent:
    task_id: str
    stage: str
    agent_id: str


class LoopGuard:
    def __init__(self, max_iterations: int = 3, max_escalations: int = 2, max_history: int = 8):
        self.max_iterations = max_iterations
        self.max_escalations = max_escalations
        self.max_history = max_history
        self._visited: set[tuple[str, str, str]] = set()
        self._history: list[GuardEvent] = []
        self._failures: set[str] = set()
        self._iterations = 0
        self._escalations = 0

    @property
    def failure_signatures(self) -> frozenset[str]:
        return frozenset(self._failures)

    def check(self, task_id: str, stage: str, agent_id: str, failure_signature: str | None = None) -> str:
        # 顺序为最廉价优先：生命周期上限，然后失败记忆，
        # 然后尾部环窗口，最后已访问集合。
        if self._escalations >= self.max_escalations:
            return GuardDecision.MAX_ESCALATIONS
        if self._iterations >= self.max_iterations:
            return GuardDecision.MAX_ITERATIONS
        key = (task_id, stage, agent_id)
        if failure_signature is not None and self._signature(task_id, stage, agent_id, failure_signature) in self._failures:
            return GuardDecision.REPEATED_FAILURE
        # CYCLE_DETECTED：历史尾部长度 >= 4 且其最旧元素等于候选
        # 事件，即 [candidate, e2, e3, e4] —— 现在运行候选事件将
        # 重演周期 4 的振荡。（后三个元素与自身比较，因此该相等
        # 判断等价于 history[-4] == candidate。）
        if len(self._history) >= 4 and self._history[-4:] == [
            GuardEvent(task_id, stage, agent_id),
            self._history[-3],
            self._history[-2],
            self._history[-1],
        ]:
            return GuardDecision.CYCLE_DETECTED
        if key in self._visited:
            return GuardDecision.DUPLICATE_TASK
        return GuardDecision.ALLOW

    def record(self, task_id: str, stage: str, agent_id: str) -> None:
        # 补全 check/record 配对：只有被 record 的事件才使未来对
        # 同一 key 的 check 变为 DUPLICATE_TASK。
        key = (task_id, stage, agent_id)
        self._visited.add(key)
        self._history.append(GuardEvent(task_id, stage, agent_id))
        self._history = self._history[-self.max_history:]

    def record_failure(self, task_id: str, stage: str, agent_id: str, failure_signature: str) -> None:
        # 只记住 hash 后的类别 —— 见 _signature。
        self._failures.add(self._signature(task_id, stage, agent_id, failure_signature))

    def record_iteration(self) -> None:
        self._iterations += 1

    def record_escalation(self) -> None:
        self._escalations += 1

    @staticmethod
    def _signature(task_id: str, stage: str, agent_id: str, failure_signature: str) -> str:
        # 只对结构化字段做 hash；绝不保留原始诊断文本 —— 只记忆
        # 类别前缀（":" 之前），因此失败记忆保持分类化且无秘密。
        payload = json.dumps([task_id, stage, agent_id, failure_signature.split(":", 1)[0]], separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()
