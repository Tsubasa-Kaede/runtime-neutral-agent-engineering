"""V3.2 多槽组合：一个 execution 的多执行路径合法组合（CU-COMP-2）。

    caller（未来编排层；供应 execution_id、账本、slot 规格）
        ↓ build_execution_slots(journal, specs, execution_id=…)
    ExecutionSlots（不可变组合产物）
        ├─ 恰一个 ControlBoundary（intent 单权威）
        ├─ 恰一个 RevisionAppliedJournaler（APPLIED 唯一转译实例）
        └─ slot_id → WrapStack（每 slot 一条完整独立执行链）
             SlotHandle.invoke(request)（唯一公开执行面）

结构性保证：
- 单账本：caller 传入的同一个账本贯穿 boundary 裁决回声与
  APPLIED 事实——分账在构造上不可能；事实域零第二账本。
- 单权威：intent 翻转只发生在 boundary.submit 临界区；每 slot
  的门与适配器只是同一真相的只读观察者——执行异常（含控制域
  中止异常）从每条链原样穿透，本层零捕获、零确认事实、零终态。
- 共享修订队列是 execution 级：每 slot 委托前读同一账本权威的
  detached 快照，当前全部待定修订随该次委托携带——不排干、
  不移除、不过滤、不指派目标（指派属未来编排单元）。
- 身份零新增：execution_id 由 caller 供应（本层零铸造）；调用
  身份仍由既有执行链内唯一铸造点生成；slot_id 只是组合映射键
  ——不进任何事实、不进任何用量记录、不是域身份类型。
- 组装一次性、产物不可变：slot 集构造后封闭（无增删、无动态
  拓扑）；本层零锁、零线程引用——并发安全完全依赖既有组件
  （boundary / 账本 / 用量日志各自持锁；门与适配器无状态）。
- 构造失败零残留：异常原样传播（零捕获零翻译），无登记、
  账本零新事实，同 execution_id 重试合法。
"""
from __future__ import annotations

from dataclasses import dataclass

from abort_gate import AbortGate
from control_boundary import ControlBoundary, ControlModelError
from revision_adapter import RevisionAdapter
from revision_applied_journaler import RevisionAppliedJournaler
from usage_capture import UsageCapture
from wrap_stack import WrapStack

__all__ = (
    "ExecutionSlotSpec", "ExecutionSlots", "SlotHandle",
    "build_execution_slots",
)


@dataclass(frozen=True)
class ExecutionSlotSpec:
    """一个 slot 的组合规格（纯值；slot_id 仅为组合映射键）。"""

    slot_id: str
    raw_adapter: object
    usage_log: object
    runtime_id: str
    role: str
    capabilities: object = None

    def __post_init__(self):
        if not isinstance(self.slot_id, str) or not self.slot_id.strip():
            raise ControlModelError("slot_id must be a non-empty string")


class ExecutionSlots:
    """不可变多槽组合产物：单 boundary + 封闭的 slot 执行链映射。"""

    def __init__(self, boundary, stacks):
        self._boundary = boundary
        self._stacks = dict(stacks)

    @property
    def boundary(self):
        """唯一控制面把手（submit / snapshot 的唯一公开途径）。"""
        return self._boundary

    def slot(self, slot_id):
        """取一个 slot 的执行句柄；缺席即 KeyError（零回退零改写）。"""
        return SlotHandle(self._stacks[slot_id])


class SlotHandle:
    """不可变薄句柄：唯一公开执行面 invoke(request)，纯委托。"""

    def __init__(self, stack):
        self._stack = stack

    def invoke(self, request):
        """委托该 slot 的已装配执行链；结果 / 异常原样返回。"""
        return self._stack.invoke(request)


def build_execution_slots(journal, slot_specs, *, execution_id,
                          initial_version=0):
    """一次性组装：恰一 boundary、恰一 journaler、N 条独立执行链。

    组件合法性由各组件构造器既有校验负责，异常原样传播；传播
    发生时零登记、账本零新事实——同 execution_id 重试合法。
    账本中已存在该 execution_id 的事实即拒绝：同一账本上不允许
    出现第二个同 id boundary。
    """
    specs = tuple(slot_specs)
    if not specs:
        raise ControlModelError("at least one slot spec is required")
    seen = set()
    for spec in specs:
        if not isinstance(spec, ExecutionSlotSpec):
            raise ControlModelError(
                "slot specs must be ExecutionSlotSpec values")
        if spec.slot_id in seen:
            raise ControlModelError(
                f"duplicate slot_id: {spec.slot_id!r}")
        seen.add(spec.slot_id)
    for fact in journal.snapshot():
        if fact.execution_id == execution_id:
            raise ControlModelError(
                "journal already holds facts for execution_id: "
                f"{execution_id!r}")
    boundary = ControlBoundary(journal=journal, execution_id=execution_id,
                               initial_version=initial_version)
    journaler = RevisionAppliedJournaler(boundary, journal)
    stacks = {}
    for spec in specs:
        usage = UsageCapture(
            spec.raw_adapter, spec.usage_log,
            runtime_id=spec.runtime_id, role=spec.role,
            capabilities=spec.capabilities,
            handoff_observer=journaler.on_handoff)
        revision_adapter = RevisionAdapter(boundary, usage)
        gate = AbortGate(boundary)
        stacks[spec.slot_id] = WrapStack(boundary, revision_adapter, gate)
    return ExecutionSlots(boundary, stacks)
