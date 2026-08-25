"""不可变、可序列化、无秘密的结构化交接 packet。

四个业务 packet 是角色之间的 WIRE CONTRACT：REQUIRED_FIELDS 不是
内部 schema 细节 —— 它是 runtime 必须应答的协议，因此增删一个
字段都是协议变更，不是重构。packet 是 frozen dataclass，只能通过
验证构造（from_dict 或带 __post_init__ 检查的直接构造）；下方的
秘密形态扫描是边界防御，不是完整的安全系统 —— 共享的两级权威是
content_safety（值中的凭据形态、结构上的 marker key）。
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from typing import Any, ClassVar


class PacketValidationError(ValueError):
    """封闭的验证失败；可作为 reason 安全上报。"""

    pass


# 凭据"赋值形态"（token=..., api_key: ...）。纯文本中提及一个
# marker 词不会被匹配 —— 本扫描拒绝的是形态化的秘密，
# 不是词汇本身。
_SECRET_PATTERN = re.compile(
    r"(?i)(api[-_ ]?key|token|secret|authorization|password)\s*[:=]"
)


def _clean(value: Any) -> Any:
    if isinstance(value, str):
        if _SECRET_PATTERN.search(value):
            raise PacketValidationError("packet contains secret-shaped content")
        return value
    if isinstance(value, dict):
        if any(_SECRET_PATTERN.search(str(key)) for key in value):
            raise PacketValidationError("packet contains secret-shaped field")
        return {str(key): _clean(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return tuple(_clean(item) for item in value)
    return value


def _required(data: dict[str, Any], fields: tuple[str, ...]) -> None:
    missing = [field for field in fields if field not in data]
    if missing:
        raise PacketValidationError(f"missing required fields: {', '.join(missing)}")


def _tuple(value: Any, field: str) -> tuple:
    if not isinstance(value, (list, tuple)):
        raise PacketValidationError(f"{field} must be a list")
    return tuple(_clean(item) for item in value)


@dataclass(frozen=True)
class ArchitecturePacket:
    """Architect 的设计回答：目标、约束、结构、步骤、验收标准与
    风险 —— 是 coder 的完整输入契约。"""

    task_id: str
    role: str
    goal: tuple[str, ...]
    constraints: tuple[str, ...]
    architecture: tuple[str, ...]
    interfaces: tuple[dict, ...]
    implementation_steps: tuple[dict, ...]
    acceptance_criteria: tuple[str, ...]
    risks: tuple[dict, ...]

    REQUIRED_FIELDS: ClassVar[tuple[str, ...]] = (
        "task_id", "role", "goal", "constraints", "architecture", "interfaces",
        "implementation_steps", "acceptance_criteria", "risks",
    )

    def __post_init__(self):
        self._validate_identity("architect")
        for field in ("goal", "constraints", "architecture", "acceptance_criteria"):
            if not isinstance(getattr(self, field), tuple):
                raise PacketValidationError(f"{field} must be a tuple")
        _clean(asdict(self))

    def _validate_identity(self, expected_role: str):
        if not isinstance(self.task_id, str) or not self.task_id.strip():
            raise PacketValidationError("task_id is required")
        if self.role != expected_role:
            raise PacketValidationError(f"role must be {expected_role}")

    @classmethod
    def from_dict(cls, data):
        if not isinstance(data, dict):
            raise PacketValidationError("packet must be an object")
        _required(data, cls.REQUIRED_FIELDS)
        return cls(
            task_id=data["task_id"], role=data["role"],
            goal=_tuple(data["goal"], "goal"), constraints=_tuple(data["constraints"], "constraints"),
            architecture=_tuple(data["architecture"], "architecture"), interfaces=_tuple(data["interfaces"], "interfaces"),
            implementation_steps=_tuple(data["implementation_steps"], "implementation_steps"),
            acceptance_criteria=_tuple(data["acceptance_criteria"], "acceptance_criteria"), risks=_tuple(data["risks"], "risks"),
        )


@dataclass(frozen=True)
class ImplementationPacket:
    """Coder 的实现回答：变更文件、摘要、细节、假设，以及 tester
    必须满足的测试要求。"""

    task_id: str
    role: str
    changed_files: tuple[str, ...]
    implementation_summary: str
    implementation_details: tuple[str, ...]
    assumptions: tuple[str, ...]
    unresolved_items: tuple[str, ...]
    test_requirements: tuple[str, ...]

    REQUIRED_FIELDS: ClassVar[tuple[str, ...]] = (
        "task_id", "role", "changed_files", "implementation_summary", "implementation_details",
        "assumptions", "unresolved_items", "test_requirements",
    )

    @staticmethod
    def required_role() -> str:
        return "coder"

    def __post_init__(self):
        self._validate()

    def _validate(self):
        if not isinstance(self.task_id, str) or not self.task_id.strip() or self.role != self.required_role():
            raise PacketValidationError("ImplementationPacket requires task_id and coder role")
        _clean(asdict(self))

    @classmethod
    def from_dict(cls, data):
        if not isinstance(data, dict):
            raise PacketValidationError("packet must be an object")
        _required(data, cls.REQUIRED_FIELDS)
        return cls(data["task_id"], data["role"], _tuple(data["changed_files"], "changed_files"), data["implementation_summary"], _tuple(data["implementation_details"], "implementation_details"), _tuple(data["assumptions"], "assumptions"), _tuple(data["unresolved_items"], "unresolved_items"), _tuple(data["test_requirements"], "test_requirements"))


@dataclass(frozen=True)
class ReviewPacket:
    """Reviewer 的裁决：发现、严重度、受影响文件、必需变更与逐条
    验收状态。"""

    task_id: str
    role: str
    status: str
    findings: tuple[dict, ...]
    severity: tuple[str, ...]
    affected_files: tuple[str, ...]
    required_changes: tuple[str, ...]
    acceptance_criteria_status: tuple[str, ...]

    REQUIRED_FIELDS: ClassVar[tuple[str, ...]] = ("task_id", "role", "status", "findings", "severity", "affected_files", "required_changes", "acceptance_criteria_status")

    @staticmethod
    def required_role() -> str:
        return "reviewer"

    def __post_init__(self):
        if not isinstance(self.task_id, str) or not self.task_id.strip() or self.role != self.required_role():
            raise PacketValidationError("ReviewPacket requires task_id and reviewer role")
        _clean(asdict(self))

    @classmethod
    def from_dict(cls, data):
        if not isinstance(data, dict):
            raise PacketValidationError("packet must be an object")
        _required(data, cls.REQUIRED_FIELDS)
        return cls(data["task_id"], data["role"], data["status"], _tuple(data["findings"], "findings"), _tuple(data["severity"], "severity"), _tuple(data["affected_files"], "affected_files"), _tuple(data["required_changes"], "required_changes"), _tuple(data["acceptance_criteria_status"], "acceptance_criteria_status"))


@dataclass(frozen=True)
class TestPacket:
    """Tester 的验证回答：运行/通过/失败的测试、失败细节、覆盖
    证据与剩余风险。"""

    task_id: str
    role: str
    tests_run: tuple[str, ...]
    tests_passed: tuple[str, ...]
    tests_failed: tuple[str, ...]
    failures: tuple[dict, ...]
    coverage_or_validation: tuple[str, ...]
    remaining_risks: tuple[str, ...]

    REQUIRED_FIELDS: ClassVar[tuple[str, ...]] = ("task_id", "role", "tests_run", "tests_passed", "tests_failed", "failures", "coverage_or_validation", "remaining_risks")

    @staticmethod
    def required_role() -> str:
        return "tester"

    def __post_init__(self):
        if not isinstance(self.task_id, str) or not self.task_id.strip() or self.role != self.required_role():
            raise PacketValidationError("TestPacket requires task_id and tester role")
        _clean(asdict(self))

    @classmethod
    def from_dict(cls, data):
        if not isinstance(data, dict):
            raise PacketValidationError("packet must be an object")
        _required(data, cls.REQUIRED_FIELDS)
        return cls(data["task_id"], data["role"], _tuple(data["tests_run"], "tests_run"), _tuple(data["tests_passed"], "tests_passed"), _tuple(data["tests_failed"], "tests_failed"), _tuple(data["failures"], "failures"), _tuple(data["coverage_or_validation"], "coverage_or_validation"), _tuple(data["remaining_risks"], "remaining_risks"))


_PACKET_TYPES = {name: cls for name, cls in (("ArchitecturePacket", ArchitecturePacket), ("ImplementationPacket", ImplementationPacket), ("ReviewPacket", ReviewPacket), ("TestPacket", TestPacket))}


def serialize_packet(packet) -> str:
    """规范化 wire 文本（key 排序、紧凑分隔符）—— 正是 ledger 存储
    与 transport 比对所用的精确表示，因此必须保持确定性。"""
    if type(packet).__name__ not in _PACKET_TYPES:
        raise PacketValidationError("unsupported packet type")
    payload = _clean(asdict(packet))
    payload["packet_type"] = type(packet).__name__
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def deserialize_packet(payload: str):
    """wire 文本 -> packet，通过与构造相同的 REQUIRED_FIELDS 契约；
    未知 packet 类型与畸形 JSON 一律拒绝，绝不 best-effort 解析。"""
    try:
        data = json.loads(payload)
    except (TypeError, json.JSONDecodeError) as exc:
        raise PacketValidationError("invalid packet JSON") from exc
    if not isinstance(data, dict) or data.get("packet_type") not in _PACKET_TYPES:
        raise PacketValidationError("unknown packet type")
    packet_type = data.pop("packet_type")
    return _PACKET_TYPES[packet_type].from_dict(data)
