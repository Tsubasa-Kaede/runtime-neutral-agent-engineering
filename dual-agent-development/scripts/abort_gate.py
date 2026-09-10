"""V3.2 AbortGate：适配层包装栈的 pre-delegation 控制窗检查
（CU-OBS-4a）。

职责窗：ControlGate 已 PASS、单次调用尚未真正交予底层适配层的
窗口——只读 ControlBoundary 的公开控制真相 pending_intent：

    NONE  → 照常 delegation
    PAUSE → 照常 delegation（停驻在下一 admission 生效，不中断本次）
    ABORT → raise ControlAborted（复用 control_gate 的唯一定义）

边界契约：
- 只 raise、不 catch；不写控制账本、不写执行事件、不触事件索引/
  用量/trace；不管理生命周期、不做修订、不碰重放缓存私有结构。
- 委托保真：参数原样转发、返回值原样返回、被委托方异常原样传播
  （包括其自身抛出的 ControlAborted——本门不拦截不包装）。
- 无取消面：一旦已经 delegation，本门不负责取消；不触系统进程、
  不持有调用标识。check → delegation 之间允许自然竞争，不要求
  全局原子化（出口由真实顺序决定）。
- 纯同步检查：无轮询、无睡眠、无后台线程、零内部状态。
"""
from __future__ import annotations

from control_boundary import (
    ControlBoundary,
    ControlModelError,
    PendingIntentKind,
)
from control_gate import ControlAborted

__all__ = ("AbortGate",)


class AbortGate:
    """pre-delegation 控制窗检查：ABORT pending 时拒绝交予底层。"""

    def __init__(self, boundary: ControlBoundary) -> None:
        if not isinstance(boundary, ControlBoundary):
            raise ControlModelError("AbortGate expects a ControlBoundary")
        self._boundary = boundary

    def delegate(self, function, *args, **kwargs):
        """检查控制真相后交予被委托方；ABORT pending 则拒绝。

        NONE / PAUSE pending → 立即调用 function(*args, **kwargs)，
        返回值与异常原样透传；ABORT pending → raise ControlAborted
        （此时被委托方绝不被调用）。
        """
        if self._boundary.pending_intent.kind is PendingIntentKind.ABORT:
            raise ControlAborted(self._boundary.execution_id)
        return function(*args, **kwargs)
