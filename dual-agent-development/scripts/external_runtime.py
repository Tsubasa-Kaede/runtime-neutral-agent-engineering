"""Provider 中立的外部 Runtime 契约与调用记录。

所有 adapter 与 executor 共享的数据词汇：request 携带什么、trace
记录什么、result 可能包含什么。adapter 必须遵守的边界规则：

- RuntimeDiscovery.reason 必须是安全、分类化的解释 —— 绝不是原始
  探测输出、路径或记录（runtime 中立的 discovery 层会将其逐字
  复制进可上报的 reason）。
- InvocationTrace 的 token 计数默认为字面量 "unknown"：无法观测
  真实 token 用量的 adapter 必须上报"诚实的未知"，绝不编造数字。
- InvocationTrace.error 与 InvocationResult.error 只承载封闭的
  错误面；runtime 原始文本留在 adapter 内部，并在任何 trace 被存储
  或上报之前先经 redaction（content_safety.sanitize_trace）处理。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, FrozenSet
from uuid import uuid4


class InvocationStatus(str, Enum):
    """一次调用尝试的生命周期（终态值：SUCCESS、FAILED、TIMEOUT、
    CANCELLED、UNAVAILABLE）。"""

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
    """中立的 adapter 身份：agent 是谁、在哪个 runtime 上、声明什么
    角色 —— 仅声明事实，不含 Health 或能力断言（那些由各自的层
    决定）。"""

    agent_id: str
    runtime: str
    provider: str | None
    model: str | None
    role: str | None
    capabilities: FrozenSet[str]


@dataclass(frozen=True)
class RuntimeDiscovery:
    """对单个 runtime 的"存在/可找到"回答。`reason` 必须保持分类化
    且安全 —— 它会被逐字汇入可上报的 discovery reason，因此原始
    探测输出绝不允许出现在这里。"""

    runtime: str
    available: bool
    version: str | None = None
    reason: str | None = None
    capabilities: FrozenSet[str] = frozenset()


@dataclass(frozen=True)
class ExternalAgentRequest:
    """一次 agent 调用请求。prompt 是该调用的完整输入契约；
    handoff_packets 在某阶段消费上游 packet 时携带已验证的上游
    packet（首个阶段为空）。"""

    task_id: str
    prompt: str
    agent_id: str
    role: str | None = None
    provider: str | None = None
    model: str | None = None
    timeout_seconds: float = 120.0
    # 该阶段的结构化上游 packet（例如 coder 的 ArchitecturePacket、
    # reviewer 的三个 packet）。首个阶段为空。
    handoff_packets: tuple = ()


@dataclass(frozen=True)
class InvocationTrace:
    """一次调用的结构化记录。token 计数默认为字面量 "unknown" ——
    诚实的未知，绝不猜测；能观测真实计数的 adapter 可以上报
    整数。"""

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
    """一次调用的结果。`output` 可以是原始模型文本 —— 它是阶段局部
    值，必须先通过 packet 解析与内容扫描，才能进入 packet、ledger
    或报告；绝不被原样转发给下一个阶段。"""

    status: InvocationStatus
    output: Any = None
    error: str | None = None
    trace: InvocationTrace | None = None


def new_invocation_id() -> str:
    return f"invocation-{uuid4().hex}"
