"""Provider-neutral external agent runtime contracts and invocation records."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, FrozenSet
from uuid import uuid4


class InvocationStatus(str, Enum):
    SELECTED = "SELECTED"
    STARTING = "STARTING"
    INVOKED = "INVOKED"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    TIMEOUT = "TIMEOUT"
    CANCELLED = "CANCELLED"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True)
class RuntimeProfile:
    agent_id: str
    runtime: str
    provider: str | None
    model: str | None
    role: str | None
    capabilities: FrozenSet[str]


@dataclass(frozen=True)
class RuntimeDiscovery:
    runtime: str
    available: bool
    version: str | None = None
    reason: str | None = None
    capabilities: FrozenSet[str] = frozenset()


@dataclass(frozen=True)
class ExternalAgentRequest:
    task_id: str
    prompt: str
    agent_id: str
    role: str | None = None
    provider: str | None = None
    model: str | None = None
    timeout_seconds: float = 120.0
    # Structured upstream packets for the stage (e.g. ArchitecturePacket for
    # the coder, three packets for the reviewer). Empty for the first stage.
    handoff_packets: tuple = ()


@dataclass(frozen=True)
class InvocationTrace:
    invocation_id: str
    task_id: str
    agent_id: str
    runtime: str
    provider: str | None
    model: str | None
    role: str | None
    status: InvocationStatus
    started_at: float | None = None
    finished_at: float | None = None
    duration_ms: int | None = None
    exit_code: int | None = None
    input_tokens: int | str = "unknown"
    output_tokens: int | str = "unknown"
    error: str | None = None


@dataclass(frozen=True)
class InvocationResult:
    status: InvocationStatus
    output: Any = None
    error: str | None = None
    trace: InvocationTrace | None = None


def new_invocation_id() -> str:
    return f"invocation-{uuid4().hex}"
