"""引擎级阶段交接：只走结构化 packet，绝不传递原始输出。

HandoffContext 累积一个任务已验证的业务 packet，并强制维持
"阶段与 Runtime 输出解耦"的边界：

- input_for(stage) 是该阶段的输入契约：上游 PACKET（或 packet
  元组），绝不是其它阶段的原始模型输出。缺少必需的上游事实
  抛出 MISSING_HANDOFF —— 一个诚实的终止，绝不是静默的 None。
- accept(stage, value) 是输出进入 context 的唯一途径，且必须先
  通过 from_dict 验证；无法解析进该阶段 packet 契约的内容抛出
  PACKET_VALIDATION_FAILED，绝不污染下一阶段的输入。

Reviewer 契约：review 阶段消费三个 packet ——
(architecture, implementation, test) —— 三者必须全部在场。
"""
from dataclasses import dataclass

from structured_packets import ArchitecturePacket, ImplementationPacket, ReviewPacket, TestPacket, PacketValidationError


class HandoffError(ValueError):
    """结构化交接失败；str(exc) 是封闭的 reason 词汇
    （MISSING_HANDOFF / PACKET_VALIDATION_FAILED /
    UNKNOWN_STAGE），可安全上报。"""


@dataclass(frozen=True)
class HandoffContext:
    """按任务存储的不可变 packet 容器；每次变更都返回新值。"""

    task_id: str
    architecture_packet: ArchitecturePacket | None = None
    implementation_packet: ImplementationPacket | None = None
    test_packet: TestPacket | None = None
    review_packet: ReviewPacket | None = None

    def input_for(self, stage: str, requires_architecture: bool = False):
        # 各阶段输入契约：architect 读任务本身（无 packet）；
        # coder 读 ARCHITECTURE；test 读 IMPLEMENTATION；review 读
        # (ARCHITECTURE, IMPLEMENTATION, TEST)。任一依赖缺失即
        # MISSING_HANDOFF —— 下游阶段绝不在事实缺失时运行。
        if stage == "architect":
            return None
        if stage == "coder":
            if requires_architecture and self.architecture_packet is None:
                raise HandoffError("MISSING_HANDOFF")
            return self.architecture_packet
        if stage == "test":
            if self.implementation_packet is None:
                raise HandoffError("MISSING_HANDOFF")
            return self.implementation_packet
        if stage == "review":
            if self.implementation_packet is None or self.test_packet is None:
                raise HandoffError("MISSING_HANDOFF")
            return (self.architecture_packet, self.implementation_packet, self.test_packet)
        return None

    def accept(self, stage: str, value):
        # 验证边界：阶段原始输出只有通过 from_dict 解析后才能成为
        # context 状态；违反 packet 契约一律抛出
        # PACKET_VALIDATION_FAILED（绝不部分存储、绝不重试）。
        try:
            if stage == "architect":
                packet = value if isinstance(value, ArchitecturePacket) else ArchitecturePacket.from_dict(value)
                return HandoffContext(self.task_id, packet, self.implementation_packet, self.test_packet, self.review_packet)
            if stage == "coder":
                if isinstance(value, ImplementationPacket):
                    return HandoffContext(self.task_id, self.architecture_packet, value, self.test_packet, self.review_packet)
                if isinstance(value, dict):
                    packet = ImplementationPacket.from_dict(value)
                    return HandoffContext(self.task_id, self.architecture_packet, packet, self.test_packet, self.review_packet)
                raise HandoffError("MISSING_HANDOFF")
            if stage == "test":
                packet = value if isinstance(value, TestPacket) else TestPacket.from_dict(value)
                return HandoffContext(self.task_id, self.architecture_packet, self.implementation_packet, packet, self.review_packet)
            if stage == "review":
                packet = value if isinstance(value, ReviewPacket) else ReviewPacket.from_dict(value)
                return HandoffContext(self.task_id, self.architecture_packet, self.implementation_packet, self.test_packet, packet)
        except (PacketValidationError, TypeError, KeyError) as exc:
            raise HandoffError("PACKET_VALIDATION_FAILED") from exc
        raise HandoffError("UNKNOWN_STAGE")

    def packets(self):
        return tuple(packet for packet in (self.architecture_packet, self.implementation_packet, self.test_packet, self.review_packet) if packet is not None)
