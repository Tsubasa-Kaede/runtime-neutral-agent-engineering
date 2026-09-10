"""V3.2 RevisionAppliedJournaler：REV-2 handoff observation 的第一个
生产 listener（CU-OBS-4c）。

链路（组合根接线；本模块旁挂于 seam，不在委托路径上）：

    UsageCapture handoff seam → on_handoff → RevisionWriter → 账本

职责唯一：把 REV-2 seam 触发时携带的 (invocation_id, applied_revisions)
按 FIFO 逐条转译为 REVISION_APPLIED 事实（每 revision 一条）。
它是 factual write authority——不是裁决者：不判 acceptance、不碰
队列/版本/生命周期，不排序、不去重、不改写 id；空携带为 no-op。
单条落账失败按条隔离（Exception 级）：成功项已记录、失败项诚实
缺席，绝不伪造成功，也绝不改变委托 outcome；BaseException 政策
不在本层扩张（沿 REV-2 observer contract）。

唯一生产入口是 on_handoff——语义上必须对应真实 handoff
observation；不存在任何以 revision id 直接生产事实的旁路 API。

execution_id 与 execution_version 取触发时刻 boundary 的只读
快照——版本真值始终归 boundary，本层只观察、绝不自维护。
认识论上限：REVISION_APPLIED 记录"该修订随本次委托越过了
applied 边界"（B 级：委托调用表达式已发起），绝不声称 runtime
已遵守该修订（honored 属未来 runtime 证据）。
"""
from __future__ import annotations

from control_boundary import ControlBoundary, ControlModelError
from control_journal import ControlFactType

__all__ = ("RevisionAppliedJournaler",)


class RevisionAppliedJournaler:
    """handoff seam listener：factual write authority，非裁决者。"""

    def __init__(self, boundary, journal):
        if not isinstance(boundary, ControlBoundary):
            raise ControlModelError("boundary must be a ControlBoundary")
        writer_factory = getattr(journal, "revision_writer", None)
        if not callable(writer_factory):
            raise ControlModelError(
                "journal must expose a callable revision_writer")
        writer = writer_factory()
        if not callable(getattr(writer, "append", None)):
            raise ControlModelError(
                "revision writer must expose a callable append")
        self._boundary = boundary
        self._writer = writer

    def on_handoff(self, invocation_id, applied_revisions):
        """REV-2 handoff observation 的唯一消费入口（FIFO 逐条落账）。

        invocation_id 原样入 payload；applied_revisions 原样作为
        correlation 源（不排序 / 不去重 / 不转换 / 不复制）；
        execution_id / execution_version 取触发时刻 boundary 只读值。
        """
        for revision_id in applied_revisions:
            try:
                self._writer.append(
                    fact_type=ControlFactType.REVISION_APPLIED,
                    execution_id=self._boundary.execution_id,
                    command_id=revision_id,
                    execution_version=self._boundary.execution_version,
                    payload={"invocation_id": invocation_id})
            except Exception:
                continue  # 单条失败诚实缺席；绝不改变委托 outcome
