"""V3.2 组合根：execution 级装配的唯一入口（CU-COMP-1）。

    caller（未来编排层）
        ↓ CompositionRoot.compose_execution(execution_id, raw, log, …)
    唯一合法执行栈（既有冻结组件，恰一次组装调用）
        ↓ ExecutionComposition（不可变薄句柄）
    .boundary（控制面把手） / .invoke(request)（唯一执行路径）

职责切分：
- root 是装配者不是引擎：不持有执行状态、不做生命周期裁决、
  不捕获执行异常——委托链异常原样穿透，本层零确认事实。
- 唯一性闸（组合域）：同一 root 内 execution_id 已组即拒；账本中
  已存在该 execution_id 的历史事实亦拒（同一账本上不允许出现
  第二个同 id boundary）。check-register 在组合域锁内原子完成；
  组装失败不登记（execution_id 不被烧毁，可合法重试）。
- 单账本：accept 侧与 applied 侧恒用调用方传入（或 root 自建）的
  同一个账本——分账在构造上不可能。
- invoke 路径零锁：组合域锁只保护 compose_execution 临界区。

execution_id 一律 caller 供应；本层零生成、零假设。
facts() 仅代理账本 detached 快照，不外泄任何 writer capability。
"""
from __future__ import annotations

from control_boundary import ControlModelError
from control_journal import ControlJournal
from threading import Lock
from wrap_stack import build_wrap_stack

__all__ = ("CompositionRoot", "ExecutionComposition")


class CompositionRoot:
    """execution 装配入口：唯一性闸 + 复用唯一合法组装路径。"""

    def __init__(self, journal=None):
        self._journal = ControlJournal() if journal is None else journal
        self._lock = Lock()  # 组合域：check-register 唯一临界区
        self._composed = set()

    def compose_execution(self, execution_id, raw_adapter, usage_log, *,
                          runtime_id, role, capabilities=None,
                          initial_version=0):
        """组装一个 execution：恰一次组装调用；失败不登记。

        组件合法性由各组件构造器既有校验负责，异常原样传播；
        传播发生时 execution_id 尚未登记，同 id 重试合法。
        """
        with self._lock:
            if execution_id in self._composed:
                raise ControlModelError(
                    "execution_id already composed in this root: "
                    f"{execution_id!r}")
            for fact in self._journal.snapshot():
                if fact.execution_id == execution_id:
                    raise ControlModelError(
                        "journal already holds facts for execution_id: "
                        f"{execution_id!r}")
            stack = build_wrap_stack(
                self._journal, raw_adapter, usage_log,
                execution_id=execution_id, runtime_id=runtime_id,
                role=role, capabilities=capabilities,
                initial_version=initial_version)
            self._composed.add(execution_id)
            return ExecutionComposition(stack)

    def facts(self):
        """全账本 detached 快照（仅代理；零 writer capability 外泄）。"""
        return self._journal.snapshot()


class ExecutionComposition:
    """不可变薄句柄：恰 .boundary 与 .invoke，纯委托零语义。"""

    def __init__(self, stack):
        self._stack = stack

    @property
    def boundary(self):
        """控制面把手（submit / snapshot 的唯一公开途径）。"""
        return self._stack.boundary

    def invoke(self, request):
        """唯一执行路径：委托已装配栈；结果 / 异常原样返回。"""
        return self._stack.invoke(request)
