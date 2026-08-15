"""Immutable, serializable, secret-free structured handoff packets."""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from typing import Any, ClassVar


class PacketValidationError(ValueError):
    pass


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
    if type(packet).__name__ not in _PACKET_TYPES:
        raise PacketValidationError("unsupported packet type")
    payload = _clean(asdict(packet))
    payload["packet_type"] = type(packet).__name__
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def deserialize_packet(payload: str):
    try:
        data = json.loads(payload)
    except (TypeError, json.JSONDecodeError) as exc:
        raise PacketValidationError("invalid packet JSON") from exc
    if not isinstance(data, dict) or data.get("packet_type") not in _PACKET_TYPES:
        raise PacketValidationError("unknown packet type")
    packet_type = data.pop("packet_type")
    return _PACKET_TYPES[packet_type].from_dict(data)
