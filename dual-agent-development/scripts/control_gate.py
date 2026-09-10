"""V3.2 ControlGate：B2 ADMISSION 控制门（CU-CTRL-7）。

控制平面成员：位于真实 Stage Admission 边界，对 ControlBoundary 的
只读控制事实（pending_intent 公共属性）做出 admission 裁决——

    gate.check() → 返回 None：放行（PASS）
                 → 阻塞：PAUSE pending 停驻（HOLD）
                 → raise ControlAborted：ABORT pending

边界契约：
- 引擎仍是执行权威。Gate 不执行 Stage、不触碰引擎/适配层/系统进程、
  不管理 invocation 与重试、不获取 invocation 标识、不做在途取消
  （abort 只发生在 admission 边界；drain 语义属后续组合单元）。
- HOLD 是 PAUSED 的唯一实际控制确认来源：is_holding 只读事实供
  composition 层向 CTRL-6 投影供应 park_point=ADMISSION（B0 调度口
  停驻属组合根 dispatch 控制、属后续 COMP 单元，本门只产生
  ADMISSION 一种停驻证据）。
- PAUSE_REQUESTED ≠ PAUSED：无 HOLD 绝不呈现停驻；gate 不写
  ControlJournal、不写 ExecutionEvent、不落任何确认事实
  （ABORT_CONFIRMED / ABORT_SUPERSEDED 归 COMP-* 的
  CompositionWriter）。
- ControlAborted（授权在本文件定义一次）：gate 侧只 raise 不 catch，
  catch 属未来 COMP-* 组合层。
- 唤醒：composition 层在 RESUME / ABORT 被接受后调用 release() /
  abort()；信号不携带真相——等待者唤醒后重读控制真值自决。锁内
  复查真值与唤醒信号同锁互斥，杜绝丢失唤醒；全程 Condition 阻塞，
  零忙轮询、零超时控制。
- 终态真值优先（CTRL-6）：terminal 已终态时 gate 不 resurrect、
  不 hold、不 abort——直接放行，裁决交还执行层。
- ControlGate 内部状态只描述 waiting / wake condition / 绑定的
  boundary，不存 execution lifecycle，不是第二套控制意图存储。
"""
from __future__ import annotations

from threading import Condition

from control_boundary import (
    ControlBoundary,
    ControlLifecycle,
    ControlModelError,
    PendingIntentKind,
)

__all__ = ("ControlAborted", "ControlGate")


class ControlAborted(Exception):
    """控制域 abort 异常（授权裁决 2026-09-11：本文件定义一次）。

    gate 侧只 raise、绝不 catch；组合层 catch 属未来 COMP-* 单元。
    """

    def __init__(self, execution_id: str) -> None:
        super().__init__(
            f"execution {execution_id} aborted at admission boundary")
        self.execution_id = execution_id


_TERMINAL_LIFECYCLES = frozenset({
    ControlLifecycle.COMPLETED,
    ControlLifecycle.ABORTED,
    ControlLifecycle.FAILED,
})


class ControlGate:
    """B2 ADMISSION 控制门：真实 Stage Admission 边界的控制裁决。"""

    def __init__(self, boundary: ControlBoundary) -> None:
        if not isinstance(boundary, ControlBoundary):
            raise ControlModelError("ControlGate expects a ControlBoundary")
        self._boundary = boundary
        self._condition = Condition()
        self._holders = 0  # 当前停驻等待者数（>0 即 holding）

    @property
    def is_holding(self) -> bool:
        """真实 ADMISSION 停驻确认：PAUSED 投影的唯一控制确认来源。

        只读事实，不是 lifecycle 存储；无等待者即 False。
        """
        with self._condition:
            return self._holders > 0

    def check(self, *, terminal: ControlLifecycle | None = None) -> None:
        """Stage Admission 前置检查（B2）。

        返回 None = 放行；PAUSE pending = 阻塞停驻直至 RESUME 唤醒
        或 ABORT 唤醒；ABORT pending = raise ControlAborted。
        terminal 供应真实终态真值（CTRL-6 词表复用）时 gate 直接
        放行——不复活、不停驻、不 abort。
        """
        if terminal is not None and not isinstance(terminal,
                                                   ControlLifecycle):
            raise ControlModelError(f"unknown lifecycle: {terminal!r}")
        if terminal in _TERMINAL_LIFECYCLES:
            return  # 终态真值优先：裁决交还执行层
        kind = self._boundary.pending_intent.kind
        if kind is PendingIntentKind.ABORT:
            raise ControlAborted(self._boundary.execution_id)
        if kind is not PendingIntentKind.PAUSE:
            return  # PASS：无停驻意图，admission 放行
        holding = True
        while holding:
            holding = self._hold_once()
        # 唤醒后按最新控制真值终裁：ABORT → raise；RESUME 已清 → 放行
        if (self._boundary.pending_intent.kind
                is PendingIntentKind.ABORT):
            raise ControlAborted(self._boundary.execution_id)
        return  # PASS

    def _hold_once(self) -> bool:
        """进入一次停驻等待；返回 True = 唤醒后真值仍为 PAUSE。

        锁内复查真值：与 release()/abort()（同一把锁）互斥——若
        唤醒信号先于本方法到达，复查即见真值变化并立即返回 False，
        杜绝丢失唤醒。Condition.wait 全程阻塞（非轮询、非超时）。
        """
        with self._condition:
            if (self._boundary.pending_intent.kind
                    is not PendingIntentKind.PAUSE):
                return False  # 停驻已被 RESUME/ABORT 解除
            self._holders += 1
            try:
                self._condition.wait()
            finally:
                self._holders -= 1
            return (self._boundary.pending_intent.kind
                    is PendingIntentKind.PAUSE)

    def release(self) -> None:
        """RESUME 被接受后的唤醒信号（composition 层调用）。

        信号不携带真相：等待者唤醒后重读控制真值自决——RESUME
        未真正被接受时等待者继续停驻。
        """
        with self._condition:
            self._condition.notify_all()

    def abort(self) -> None:
        """ABORT 被接受后的唤醒信号（composition 层调用）。

        等待者唤醒后重读真值并自行 raise ControlAborted；本方法
        绝不代为 raise（gate 对每个观察只 raise 一次，由等待者完成）。
        """
        with self._condition:
            self._condition.notify_all()
