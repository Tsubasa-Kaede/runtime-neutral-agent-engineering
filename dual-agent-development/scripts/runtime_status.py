"""Provider-neutral, non-secret Runtime health status contracts."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class RuntimeState(str, Enum):
    READY = "READY"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    UNAVAILABLE = "UNAVAILABLE"
    ERROR = "ERROR"


class AuthenticationState(str, Enum):
    AUTHENTICATED = "AUTHENTICATED"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"


class ReasonCode(str, Enum):
    NONE = "NONE"
    EXECUTABLE_NOT_FOUND = "EXECUTABLE_NOT_FOUND"
    CLI_START_FAILED = "CLI_START_FAILED"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    AUTH_REJECTED = "AUTH_REJECTED"
    PROVIDER_UNREACHABLE = "PROVIDER_UNREACHABLE"
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
    HEALTH_CHECK_FAILED = "HEALTH_CHECK_FAILED"
    UNSUPPORTED_HEALTH_CHECK = "UNSUPPORTED_HEALTH_CHECK"
    TIMEOUT = "TIMEOUT"
    PROTOCOL_ERROR = "PROTOCOL_ERROR"
    UNKNOWN_ERROR = "UNKNOWN_ERROR"


@dataclass(frozen=True)
class HealthEvidence:
    discovery: str
    authentication: str
    provider: str
    model: str
    health: str
    exit_code: int | None = None
    duration_ms: int | None = None
    output_class: str | None = None

    def __post_init__(self) -> None:
        values = (self.discovery, self.authentication, self.provider, self.model, self.health, self.output_class)
        if any(value is not None and any(secret in value.lower() for secret in ("secret", "token", "api_key", "apikey")) for value in values):
            raise ValueError("health evidence must not contain raw secret-shaped output")


@dataclass(frozen=True)
class RuntimeStatus:
    runtime_id: str
    executable: str | None
    version: str | None
    status: RuntimeState
    provider: str | None
    model: str | None
    auth_method: str | None
    reason_code: ReasonCode
    evidence: HealthEvidence
    checked_at: float
    expires_at: float
