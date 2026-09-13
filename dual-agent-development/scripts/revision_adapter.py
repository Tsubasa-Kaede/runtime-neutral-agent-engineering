"""V3.2 RevisionAdapter：消费 pending NEXT_INVOCATION 修订并构造
下一次 invocation 的 prompt overlay（CU-REV-1）。

栈位（组合根未来接线）：

    AbortGate → RevisionAdapter → UsageCapture → raw adapter

本层只做一件事：在下一次 delegation 前把已接受的修订按 FIFO
追加到原 prompt 之后，其余一切原样。具体地：

- 队列只读快照（snapshot 的 lifecycle 实参是投影上下文，队列
  本身与 lifecycle 无关——CTRL-6 已锁定该无关性）；本层在委托
  真正发起之后按 revision_id 精确消费所携带条目（G1：one-shot
  trailing FIFO——消费事实不因结果成败改变，队列读取仍是脱钩
  快照、不重排不合并不改写）。
- overlay 精确格式（逐字保真，零合并/去重/改写）：

      <原 prompt 逐字不动>
      [USER REVISION <revision_id 逐字>]
      <修订文本逐字>
      [Obey all format rules above. END OF REVISION]

- 队列空 ⇒ request 原对象原样下传（等价调用，零重建）。
- 队列非空 ⇒ 新构造仅改 prompt 的 request（dataclasses.replace，
  其余字段恒等共享），并把 revision ids 作为 correlation 元数据
  传给下层 invoke 的冻结缝（applied_revisions）；下层语义对它们
  只引用、不解释。
- 下层返回值/异常（含 ControlAborted）原样透传；零 retry/fallback。

边界外（本层绝不做）：裁决/重放/journal 写入/生命周期判断/
身份铸造/执行事件——分别属于控制域与观察域的既定所有者。
运行时中立：零 provider 分支。
"""
from __future__ import annotations

from dataclasses import replace

from control_boundary import (
    ControlBoundary,
    ControlLifecycle,
    ControlModelError,
)

__all__ = ("RevisionAdapter",)

_REVISION_END = "[Obey all format rules above. END OF REVISION]"


def _overlay(prompt: str, revisions) -> str:
    """原 prompt 逐字在前，FIFO 修订块逐字追加，零改写。"""
    blocks = [
        f"[USER REVISION {entry.revision_id}]\n{entry.text}\n"
        f"{_REVISION_END}"
        for entry in revisions
    ]
    return f"{prompt}\n" + "\n".join(blocks)


class RevisionAdapter:
    """prompt overlay 包装：快照修订队列 → 构造新 request 下传，
    委托发起后一次性精确消费（G1）。"""

    def __init__(self, boundary, next_layer):
        if not isinstance(boundary, ControlBoundary):
            raise ControlModelError("boundary must be a ControlBoundary")
        if not callable(getattr(next_layer, "invoke", None)):
            raise ControlModelError("next_layer must expose a callable invoke")
        self._boundary = boundary
        self._next = next_layer

    def invoke(self, request):
        """应用 pending 修订后委托下层；raw 语义原样透传。

        委托调用表达式发起后（finally 边界）一次性精确消费所携带
        修订：结果成败、下层异常、观察侧落账失败均不改变消费事实；
        构造失败（未发起）零消费。
        """
        queue = self._boundary.snapshot(
            ControlLifecycle.RUNNING).revision_queue
        if not queue:
            return self._next.invoke(request)
        overlaid = replace(request, prompt=_overlay(request.prompt, queue))
        applied = tuple(entry.revision_id for entry in queue)
        try:
            return self._next.invoke(overlaid, applied_revisions=applied)
        finally:
            # 委托已跨越真实 invocation 边界 ⇒ 一次性消费；与 submit
            # 同锁精确按 id 删除，在途新提交的不同 id 不受影响
            self._boundary.consume_revisions(applied)
