from dataclasses import dataclass

from structured_packets import ArchitecturePacket, ImplementationPacket, ReviewPacket, TestPacket, PacketValidationError


class HandoffError(ValueError):
    pass


@dataclass(frozen=True)
class HandoffContext:
    task_id: str
    architecture_packet: ArchitecturePacket | None = None
    implementation_packet: ImplementationPacket | None = None
    test_packet: TestPacket | None = None
    review_packet: ReviewPacket | None = None

    def input_for(self, stage: str, requires_architecture: bool = False):
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
