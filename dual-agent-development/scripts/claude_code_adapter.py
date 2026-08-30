"""Provider 中立 runtime adapter 的 Claude Code CLI 实现。

引擎中立契约背后的一个具体 adapter —— Claude Code CLI 是"一个"
adapter，绝不是"那个" runtime。关于这个具体 CLI 的全部知识只
存在于本模块，别处皆无。

本 adapter 保持的安全/行为边界：
- 环境：discovery/auth 探测与调用都以最小 env 白名单
  （_minimal_env）启动 CLI 子进程 —— 凭据与配置绝不转发进子进程；
  CLI 自行读取自己的状态。
- Authentication 只通过 CLI 自身的状态面（auth status --json）
  "观测"；adapter 绝不自己打开、解析或存储凭据材料。
- 错误安全边界：所有可能离开本模块的进程错误（FAILED 的 stderr、
  OSError 文本、probe 失败细节）都先经 _safe_error 抹除凭据形态
  的值（赋值形态、bearer 材料、hf_/sk- key 形态），再进入 trace、
  discovery reason 或报告。
- 生命周期：invoke() 与 cancel() 共享的进程簿记（_processes /
  _cancelled / _completed）由 _state_lock 保护，并发调用不会损坏
  集合；在超时过程中被取消的调用上报 CANCELLED 而非 TIMEOUT。
- 输出解析（_parse_output）只信任 stdout 中 CLI 的 JSON 封装
  （"result" 字段）；无法解析的原始进程文本按原样返回，并继续
  受上游 packet/内容安全边界约束 —— 绝不直接进入 packet 或
  ledger。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
from typing import Any

from external_runtime import (
    ExternalAgentRequest,
    InvocationResult,
    InvocationStatus,
    InvocationTrace,
    RuntimeDiscovery,
    RuntimeProfile,
    new_invocation_id,
)


class ClaudeCodeAdapter:
    def __init__(self, profile: RuntimeProfile, executable: str):
        self.profile = profile
        self.executable = executable
        self._processes: dict[str, subprocess.Popen[str]] = {}
        self._cancelled: set[str] = set()
        self._completed: set[str] = set()
        self._state_lock = threading.Lock()
        self.last_invocation_id: str | None = None
        self._auth_provider: str | None = None

    @classmethod
    def from_environment(cls, profile: RuntimeProfile | None = None):
        # 仅查存在的注册：PATH 查找，不访问任何配置或凭据；
        # 可执行文件不存在意味着"未安装"（None），绝不是错误。
        executable = shutil.which("claude") or shutil.which("claude.exe")
        if not executable:
            return None
        return cls(profile or RuntimeProfile("coding-agent", "claude-cli", "anthropic", None, "coder", frozenset()), executable)

    def discover(self) -> RuntimeDiscovery:
        ok, detail = self._probe()
        return RuntimeDiscovery("claude-cli", ok, detail if ok else None, None if ok else detail, frozenset())

    def _probe(self) -> tuple[bool, str | None]:
        # 每条失败细节都经 _safe_error 清洗：discovery reason 可以携带
        # 进程错误文本（必须保持诚实），但绝不能携带其中的凭据
        # 形态材料。显式 UTF-8 解码防止 GBK 控制台下的解码异常逃过
        # SubprocessError 捕获。
        try:
            process = subprocess.run(
                [self.executable, "--version"],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                shell=False,
                env=self._minimal_env(),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return False, self._safe_error(str(exc))
        if process.returncode != 0:
            return False, self._safe_error(process.stderr or "runtime probe failed")
        return True, (process.stdout or "").strip() or None

    def check_authentication(self):
        # Auth 只被"观测"，绝不被执行：CLI 汇报自己的登录状态；
        # adapter 只存储分类化结果，以及下方 check_provider_model
        # 所需的 provider 标签。
        from runtime_health import AuthenticationCheck
        from runtime_status import AuthenticationState, ReasonCode
        try:
            result = subprocess.run(
                [self.executable, "auth", "status", "--json"],
                check=False, capture_output=True, text=True, timeout=10, shell=False,
                encoding="utf-8", errors="replace", env=self._minimal_env(),
            )
        except (OSError, subprocess.SubprocessError):
            return AuthenticationCheck(AuthenticationState.UNKNOWN, reason_code=ReasonCode.PROTOCOL_ERROR)
        try:
            payload = json.loads(result.stdout or "{}")
        except json.JSONDecodeError:
            payload = {}
        if result.returncode == 0 and payload.get("loggedIn") is True:
            self._auth_provider = payload.get("apiProvider")
            return AuthenticationCheck(AuthenticationState.AUTHENTICATED, payload.get("authMethod"))
        if result.returncode != 0 or payload.get("loggedIn") is False:
            return AuthenticationCheck(AuthenticationState.AUTH_REQUIRED, reason_code=ReasonCode.AUTH_REQUIRED)
        return AuthenticationCheck(AuthenticationState.UNKNOWN, reason_code=ReasonCode.PROTOCOL_ERROR)

    def check_provider_model(self):
        # Provider 可用性以上方观测到的 auth 状态（first-party 登录）
        # 为门；_auth_provider 是耦合点，这也是 check_authentication
        # 必须先于本检查运行的原因。
        from runtime_health import ProviderModelCheck
        from runtime_status import ReasonCode
        if not self.profile.provider:
            return ProviderModelCheck(None, self.profile.model, False, ReasonCode.UNSUPPORTED_HEALTH_CHECK)
        if self.profile.provider != "anthropic" or self._auth_provider not in {"firstParty", "anthropic"}:
            return ProviderModelCheck(self.profile.provider, self.profile.model, False, ReasonCode.PROVIDER_UNREACHABLE)
        return ProviderModelCheck(self.profile.provider, self.profile.model, True, ReasonCode.NONE)

    def minimal_health_check(self, timeout_seconds: float):
        # 唯一的 Health 调用是 opt-in 的：没有 REAL gate 时它上报
        # 诚实的 UNSUPPORTED 检查，而不是悄悄运行（也不是悄悄
        # 跳过并伪造一个通过）。
        from runtime_health import MinimalHealthCheck
        from runtime_status import ReasonCode
        if os.environ.get("RUN_REAL_PROVIDER_TESTS", "") != "1":
            return MinimalHealthCheck(False, ReasonCode.UNSUPPORTED_HEALTH_CHECK, output_class="skipped")
        request = ExternalAgentRequest(
            task_id="runtime-health",
            prompt="Return exactly OK and nothing else.",
            agent_id=self.profile.agent_id,
            role=self.profile.role,
            provider=self.profile.provider,
            model=self.profile.model,
            timeout_seconds=min(30.0, max(1.0, timeout_seconds)),
        )
        result = self.invoke(request)
        trace = result.trace
        if result.status is InvocationStatus.TIMEOUT:
            return MinimalHealthCheck(False, ReasonCode.TIMEOUT, trace, "timeout")
        if result.status is InvocationStatus.UNAVAILABLE:
            return MinimalHealthCheck(False, ReasonCode.CLI_START_FAILED, trace, "runtime_unavailable")
        if result.status is not InvocationStatus.SUCCESS:
            return MinimalHealthCheck(False, ReasonCode.HEALTH_CHECK_FAILED, trace, "invoke_failed")
        if str(result.output).strip().upper() != "OK":
            return MinimalHealthCheck(False, ReasonCode.PROTOCOL_ERROR, trace, "unexpected_response")
        return MinimalHealthCheck(True, ReasonCode.NONE, trace, "exact_ok")

    def invoke(self, request: ExternalAgentRequest) -> InvocationResult:
        invocation_id = new_invocation_id()
        started = time.time()
        trace = InvocationTrace(
            invocation_id, request.task_id, request.agent_id, "claude-cli",
            request.provider, request.model, request.role, InvocationStatus.STARTING,
            started_at=started,
        )
        # 非交互调用契约：--print（一次性、无 TUI）、
        # --output-format json（机器可读封装）、
        # --no-session-persistence（health/validation 调用不留任何
        # session 状态）。
        argv = [self.executable, "--print", "--output-format", "json", "--no-session-persistence"]
        if request.model:
            argv.extend(["--model", request.model])
        process: subprocess.Popen[str] | None = None
        try:
            process = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                shell=False,
                env=self._minimal_env(),
            )
            with self._state_lock:
                self._processes[invocation_id] = process
                self.last_invocation_id = invocation_id
            trace = self._finish_trace(trace, InvocationStatus.INVOKED, started)
            stdout, stderr = process.communicate(request.prompt, timeout=request.timeout_seconds)
            finished = time.time()
            with self._state_lock:
                cancelled = invocation_id in self._cancelled
            if cancelled:
                error = "external runtime cancelled"
                return InvocationResult(
                    InvocationStatus.CANCELLED,
                    error=error,
                    trace=self._finish_trace(trace, InvocationStatus.CANCELLED, started, process.returncode, finished, error),
                )
            if process.returncode != 0:
                error = self._safe_error(stderr or "external runtime failed")
                return InvocationResult(
                    InvocationStatus.FAILED,
                    error=error,
                    trace=self._finish_trace(trace, InvocationStatus.FAILED, started, process.returncode, finished, error),
                )
            output = self._parse_output(stdout)
            return InvocationResult(
                InvocationStatus.SUCCESS,
                output=output,
                trace=self._finish_trace(trace, InvocationStatus.SUCCESS, started, process.returncode, finished),
            )
        except (subprocess.TimeoutExpired, TimeoutError):
            # 在锁下复查 cancellation 集合：在超时过程中被取消的调用
            # 上报 CANCELLED（调用方意图），而不是 TIMEOUT ——
            # 两者是不同的诚实结果。
            with self._state_lock:
                cancelled = invocation_id in self._cancelled
            if process is not None:
                process.kill()
                try:
                    process.communicate()
                except (subprocess.TimeoutExpired, TimeoutError, OSError):
                    pass
            error = "external runtime cancelled" if cancelled else "external runtime timeout"
            status = InvocationStatus.CANCELLED if cancelled else InvocationStatus.TIMEOUT
            return InvocationResult(
                status,
                error=error,
                trace=self._finish_trace(trace, status, started, None, time.time(), error),
            )
        except OSError as exc:
            error = self._safe_error(str(exc))
            return InvocationResult(
                InvocationStatus.UNAVAILABLE,
                error=error,
                trace=self._finish_trace(trace, InvocationStatus.UNAVAILABLE, started, None, time.time(), error),
            )
        finally:
            with self._state_lock:
                self._processes.pop(invocation_id, None)
                self._cancelled.discard(invocation_id)
                self._completed.add(invocation_id)

    def cancel(self, invocation_id: str) -> InvocationResult:
        with self._state_lock:
            process = self._processes.get(invocation_id)
            if process is None or invocation_id in self._completed:
                return InvocationResult(InvocationStatus.UNAVAILABLE, error="unknown invocation")
            self._cancelled.add(invocation_id)
        try:
            process.kill()
        except OSError:
            with self._state_lock:
                self._cancelled.discard(invocation_id)
            return InvocationResult(InvocationStatus.UNAVAILABLE, error="invocation is no longer active")
        return InvocationResult(InvocationStatus.CANCELLED)

    @staticmethod
    def _parse_output(stdout: str) -> Any:
        # 只信任 CLI 的 JSON 封装：当 stdout 解析为 {"result": ...}
        # 载荷时，result 文本即为调用输出。其余内容按原始文本原样
        # 返回 —— 该文本是否可用、是否安全，仍由上游 packet 验证
        # 与内容扫描说了算。
        text = stdout.strip()
        if not text:
            return ""
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return text
        if isinstance(payload, dict) and "result" in payload:
            return payload["result"]
        return payload

    @staticmethod
    def _minimal_env() -> dict[str, str]:
        # 白名单，而非黑名单：子进程只收到它执行与定位自身状态
        # 所需的变量 —— 父环境中的其余内容（尤其是携带凭据的
        # 变量）一概不转发。
        return {key: value for key in ("PATH", "HOME", "USERPROFILE", "SYSTEMROOT") if (value := os.environ.get(key))}

    @staticmethod
    def _safe_error(value: str) -> str:
        """在暴露进程错误之前抹除常见凭据形态的值。"""
        text = value.strip()
        patterns = (
            r"(?i)(api[-_ ]?key\s*[\"']?\s*[:=]\s*[\"']?)[^\s,;\"']+",
            r"(?i)(token\s*[\"']?\s*[:=]\s*[\"']?)[^\s,;\"']+",
            r"(?i)(secret\s*[\"']?\s*[:=]\s*[\"']?)[^\s,;\"']+",
            r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,;]+",
            r"(?<![A-Za-z0-9])(hf_[A-Za-z0-9_-]{8,})",
            r"(?<![A-Za-z0-9])(sk-[A-Za-z0-9_-]{8,})",
        )
        for pattern in patterns:
            if pattern.startswith("(?<!"):
                text = re.sub(pattern, "[REDACTED]", text)
            else:
                text = re.sub(pattern, r"\1[REDACTED]", text)
        return text[:4096]

    @staticmethod
    def _finish_trace(trace, status, started, exit_code=None, finished=None, error=None):
        finished = time.time() if finished is None else finished
        return InvocationTrace(
            trace.invocation_id, trace.task_id, trace.agent_id, trace.runtime,
            trace.provider, trace.model, trace.role, status, trace.started_at,
            finished, max(0, int((finished - started) * 1000)), exit_code,
            trace.input_tokens, trace.output_tokens, error,
        )
