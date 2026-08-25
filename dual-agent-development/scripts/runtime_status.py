"""Provider 中立、不含秘密的 Runtime Health 状态契约。

本模块是引擎全局的 Health "词汇表"：所有 Health 层共用的状态、
reason code 与证据结构。核心边界规则：这些值只描述 TASK-LEVEL
HEALTH。Health 对 qualification 不做任何断言 —— READY 不是
VERIFIED，不是 qualification 证据，也不是 Verified Pool 准入；
那些由 validation 层（candidate_validation /
verified_runtime_pool）在另一套状态词汇上决定。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class RuntimeState(str, Enum):
    """检查时刻单个 Runtime 的 task-level Health，仅此而已。

    READY 表示该 Runtime 在"本次检查"中通过了 discovery、
    authentication、provider/model 检查与最小 Health 调用；它是
    可续期的快照，不是凭据，也不是能力声明。非 READY 值同样是
    分类语义：AUTH_REQUIRED（身份缺失/被拒）、UNAVAILABLE
    （无法启动/未找到）、ERROR（已启动但行为异常）。"""

    READY = "READY"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    UNAVAILABLE = "UNAVAILABLE"
    ERROR = "ERROR"


class AuthenticationState(str, Enum):
    """仅描述 Authentication 的词汇；与 Health、Capability 正交。"""

    AUTHENTICATED = "AUTHENTICATED"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"


class ReasonCode(str, Enum):
    """封闭的分类失败词汇 —— 绝不出现原始诊断信息。"""

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
    """每项检查仅记录分类证据（"verified"/"failed"/…）。

    Runtime 原始输出、退出记录与秘密绝不进入该结构；
    __post_init__ 会拒绝秘密形态的值作为纵深防御，因此
    evidence 对象永远可以安全地写入日志或报告。"""

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
    """单个 Runtime 在 checked_at 时刻的不可变 Health 快照。

    expires_at 是 TTL/新鲜度边界，不是有效性声明：超过期限的
    状态即为 STALE，消费方必须重新检查而不是复用。该快照只
    关于 Health —— 它不授予、也不隐含 validation、qualification
    或 Pool admission。"""

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
