"""V3.2 WrapStack：V3.2 包装栈的合法组合根（CU-OBS-4c）。

唯一执行路径（全部为既有冻结组件，零编排、零执行语义新增）：

    WrapStack.invoke → AbortGate → RevisionAdapter → UsageCapture → raw

旁挂 observation（不在委托路径上）：

    UsageCapture handoff seam → RevisionAppliedJournaler.on_handoff
        → RevisionWriter → 账本 REVISION_APPLIED

单账本原则：本组合根用调用方传入的同一个账本构造
ControlBoundary（accept 侧，REVISE_REQUESTED）与
RevisionAppliedJournaler（applied 侧，REVISION_APPLIED）——两类
事实恒落同一账本，accept/applied 分账在构造上不可能。

ControlGate（pause hold / park / lifecycle 确认）不在本栈：属后续
控制组合单元。本层纯构造：invoke 逐层透传，结果 / 异常原样返回。
"""
from __future__ import annotations

from abort_gate import AbortGate
from control_boundary import ControlBoundary
from revision_adapter import RevisionAdapter
from revision_applied_journaler import RevisionAppliedJournaler
from usage_capture import UsageCapture

__all__ = ("WrapStack", "build_wrap_stack")


class WrapStack:
    """包装栈入口：gate 门控的 revision-aware invocation。"""

    def __init__(self, boundary, revision_adapter, abort_gate):
        self._boundary = boundary
        self._revision_adapter = revision_adapter
        self._abort_gate = abort_gate

    @property
    def boundary(self):
        """控制面把手：submit 命令 / 读 snapshot 的唯一公开途径。"""
        return self._boundary

    def invoke(self, request):
        """唯一执行路径：pre-delegation ABORT 门控后逐层透传。"""
        return self._abort_gate.delegate(self._revision_adapter.invoke,
                                         request)


def build_wrap_stack(journal, raw_adapter, usage_log, *,
                     execution_id, runtime_id, role,
                     capabilities=None, initial_version=0):
    """组装唯一合法栈：boundary 自建 ⇒ 单账本；journaler 旁挂 seam。

    组件合法性由各组件构造器既有校验负责，异常原样传播。
    """
    boundary = ControlBoundary(journal=journal, execution_id=execution_id,
                               initial_version=initial_version)
    journaler = RevisionAppliedJournaler(boundary, journal)
    usage = UsageCapture(raw_adapter, usage_log, runtime_id=runtime_id,
                         role=role, capabilities=capabilities,
                         handoff_observer=journaler.on_handoff)
    revision_adapter = RevisionAdapter(boundary, usage)
    gate = AbortGate(boundary)
    return WrapStack(boundary, revision_adapter, gate)
