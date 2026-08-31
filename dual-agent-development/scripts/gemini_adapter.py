"""外部 runtime adapter 的 Gemini CLI 实现。

一个具体 adapter；所有 Gemini 专属知识只存在于本模块。
注册契约：from_environment 只要求 PATH 上的 gemini 可执行文件 ——
缺失时返回 None（未注册），绝不安装 runtime、绝不修改 PATH 或
配置、绝不猜测凭据。调用形态：invoke 使用 Gemini CLI 的非交互
print 模式（-p，一次性、无 TUI）+ 机器可读 JSON 输出
（--output-format json），提示词经 stdin 传入 —— 与 claude/pi
家族一致，规避 argv 长度限制，且 prompt 永不进入 shell 可达
位置。--model 仅在请求显式指定时附加。

事实面（6 方法）：discover/invoke/cancel 之外实现
check_authentication / check_provider_model / minimal_health_check。
"具备方法"不等于"已经 REAL VERIFIED"：REAL 资格仍只由
RealGateExecutor 的门控运行授予。Authentication 只通过 Gemini CLI
自身的只读状态面"观测"：本模块只存储分类化结果，绝不使用任何
凭据打印面，绝不打开 Gemini 的凭据存储，绝不 login/logout。

输出解析（_parse_output）只信任 stdout 中 CLI 的 JSON 封装
（"response" 字段）；无法解析的原始进程文本按原样返回，并继续
受上游 packet/内容安全边界约束 —— 绝不直接进入 packet 或
ledger。Usage 解析（_parse_usage）同样防御式：封装若携带 usage
键且值为非负 int 则如实填充；缺失、类型不符、负数、bool、stdout
不可解析一律保持 "unknown" —— 绝不猜测、绝不把 unknown 伪装成
0。解析失败绝不影响调用本身。

错误安全边界：所有可能离开本模块的进程错误（FAILED 的 stderr、
OSError 文本、probe 失败细节）都先经 _safe_error 抹除凭据形态
的值（赋值形态、bearer 材料、hf_/sk-/gemini- key 形态），再进入
trace、discovery reason 或报告。invoke() 与 cancel() 共享的进程
簿记（_processes/_cancelled/_completed）由 _state_lock 保护，并发
调用不会损坏集合；在超时过程中被取消的调用上报 CANCELLED 而非
TIMEOUT。
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


class GeminiAdapter:
    """通过真实子进程调用 Gemini CLI 的非交互 print 模式（JSON 输出）。"""

    def __init__(self, profile: RuntimeProfile, executable: str):
        self.profile = profile
        self.executable = executable
        self._processes: dict[str, subprocess.Popen[str]] = {}
        self._cancelled: set[str] = set()
        self._completed: set[str] = set()
        self._state_lock = threading.Lock()
        self.last_invocation_id: str | None = None
        self._auth_provider: str | None = None
        self._auth_authenticated: bool = False

    @classmethod
    def from_environment(cls, profile: RuntimeProfile | None = None):
        # 注册要求可执行文件在场：PATH 上没有 gemini 的机器得到的是
        # 诚实的缺席（None），而不是错误或半配置的 adapter。
        executable = shutil.which("gemini") or shutil.which("gemini.exe")
        if not executable:
            return None
        return cls(profile or RuntimeProfile(
            "coding-agent", "gemini-cli", "google", None, "coder", frozenset()),
            executable)

    def discover(self) -> RuntimeDiscovery:
        ok, detail = self._probe()
        return RuntimeDiscovery(
            "gemini-cli", ok,
            detail if ok else None,
            None if ok else detail,
            frozenset(),
        )

    def _probe(self) -> tuple[bool, str | None]:
        # 每条失败细节都经 _safe_error 清洗：discovery reason 可以携带
        # 进程错误文本（必须保持诚实），但绝不能携带其中的凭据形态
        # 材料。显式 UTF-8 解码防止 GBK 控制台下的解码异常逃过
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
        # Auth 只被"观测"，绝不被执行：只读状态命令让 Gemini CLI 汇报
        # 自己的登录状态；本模块只存储分类化结果，绝不读取/打印/存储
        # 凭据材料，绝不 login/logout。
        from runtime_health import AuthenticationCheck
        from runtime_status import AuthenticationState, ReasonCode
        try:
            result = subprocess.run(
                [self.executable, "auth", "status"],
                check=False, capture_output=True, text=True, timeout=10, shell=False,
                encoding="utf-8", errors="replace", env=self._minimal_env(),
            )
        except (OSError, subprocess.SubprocessError):
            return AuthenticationCheck(
                AuthenticationState.UNKNOWN, reason_code=ReasonCode.PROTOCOL_ERROR)
        # 文本优先、退出码辅助：只有 CLI 明说"logged in"且进程成功收尾
        # 才认 AUTHENTICATED —— 宁可 UNKNOWN，绝不猜测成功；无法可靠
        # 解释的形态一律分类化为 UNKNOWN。原始输出绝不离开本模块。
        observed = " ".join(
            (result.stdout or "").split() + (result.stderr or "").split())
        lowered = observed.lower()
        if result.returncode == 0 and "logged in" in lowered \
                and "not logged in" not in lowered:
            self._auth_provider = self.profile.provider
            self._auth_authenticated = True
            return AuthenticationCheck(AuthenticationState.AUTHENTICATED)
        if "not logged in" in lowered or result.returncode != 0:
            return AuthenticationCheck(
                AuthenticationState.AUTH_REQUIRED,
                reason_code=ReasonCode.AUTH_REQUIRED)
        return AuthenticationCheck(
            AuthenticationState.UNKNOWN, reason_code=ReasonCode.PROTOCOL_ERROR)

    def check_provider_model(self):
        # Provider 可用性以上方观测到的认证状态为门；_auth_provider
        # 是耦合点，这也是 check_authentication 必须先于本检查运行的
        # 原因。未观测或 provider 不匹配时如实上报"不可担保"，绝不
        # 默认可用；model 原样透传，绝不猜测。
        from runtime_health import ProviderModelCheck
        from runtime_status import ReasonCode
        if not self.profile.provider:
            return ProviderModelCheck(
                None, self.profile.model, False, ReasonCode.UNSUPPORTED_HEALTH_CHECK)
        if self._auth_provider != self.profile.provider or not self._auth_authenticated:
            return ProviderModelCheck(
                self.profile.provider, self.profile.model, False,
                ReasonCode.PROVIDER_UNREACHABLE)
        return ProviderModelCheck(
            self.profile.provider, self.profile.model, True, ReasonCode.NONE)

    def minimal_health_check(self, timeout_seconds: float):
        # 唯一的 Health 调用是 opt-in 的：没有 REAL gate 时它上报
        # 诚实的 UNSUPPORTED 检查，而不是悄悄运行（也不是悄悄
        # 跳过并伪造一个通过）。与家族语义逐行一致：timeout 钳位
        # [1, 30] 秒；成功只认 exact-OK 输出。
        from runtime_health import MinimalHealthCheck
        from runtime_status import ReasonCode
        if os.environ.get("RUN_REAL_PROVIDER_TESTS", "") != "1":
            return MinimalHealthCheck(False, ReasonCode.UNSUPPORTED_HEALTH_CHECK,
                                      output_class="skipped")
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
            return MinimalHealthCheck(False, ReasonCode.CLI_START_FAILED, trace,
                                      "runtime_unavailable")
        if result.status is not InvocationStatus.SUCCESS:
            return MinimalHealthCheck(False, ReasonCode.HEALTH_CHECK_FAILED, trace,
                                      "invoke_failed")
        if str(result.output).strip().upper() != "OK":
            return MinimalHealthCheck(False, ReasonCode.PROTOCOL_ERROR, trace,
                                      "unexpected_response")
        return MinimalHealthCheck(True, ReasonCode.NONE, trace, "exact_ok")

    def invoke(self, request: ExternalAgentRequest) -> InvocationResult:
        invocation_id = new_invocation_id()
        started = time.time()
        trace = InvocationTrace(
            invocation_id, request.task_id, request.agent_id, "gemini-cli",
            request.provider, request.model, request.role, InvocationStatus.STARTING,
            started_at=started,
        )
        # 非交互调用契约：-p（一次性、无 TUI）、--output-format json
        # （机器可读封装）；提示词经 stdin 传入，argv 不携带提示词；
        # --model 仅在请求显式指定时附加。
        argv = [self.executable, "-p", "--output-format", "json"]
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
            stdout, stderr = process.communicate(
                request.prompt, timeout=request.timeout_seconds)
            finished = time.time()
            with self._state_lock:
                cancelled = invocation_id in self._cancelled
            if cancelled:
                error = "external runtime cancelled"
                return InvocationResult(
                    InvocationStatus.CANCELLED,
                    error=error,
                    trace=self._finish_trace(
                        trace, InvocationStatus.CANCELLED, started,
                        process.returncode, finished, error),
                )
            if process.returncode != 0:
                error = self._safe_error(stderr or "external runtime failed")
                return InvocationResult(
                    InvocationStatus.FAILED,
                    error=error,
                    trace=self._finish_trace(
                        trace, InvocationStatus.FAILED, started,
                        process.returncode, finished, error),
                )
            output = self._parse_output(stdout)
            input_tokens, output_tokens = self._parse_usage(stdout)
            return InvocationResult(
                InvocationStatus.SUCCESS,
                output=output,
                trace=self._finish_trace(
                    trace, InvocationStatus.SUCCESS, started,
                    process.returncode, finished,
                    input_tokens=input_tokens, output_tokens=output_tokens),
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
                trace=self._finish_trace(
                    trace, InvocationStatus.UNAVAILABLE, started, None, time.time(), error),
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
                return InvocationResult(
                    InvocationStatus.UNAVAILABLE, error="unknown invocation")
            self._cancelled.add(invocation_id)
        try:
            process.kill()
        except OSError:
            with self._state_lock:
                self._cancelled.discard(invocation_id)
            return InvocationResult(
                InvocationStatus.UNAVAILABLE, error="invocation is no longer active")
        return InvocationResult(InvocationStatus.CANCELLED)

    @staticmethod
    def _parse_usage(stdout: str) -> tuple[int | str, int | str]:
        # 防御式 usage capture：CLI 的 JSON 封装若携带 usage 键且值为
        # 非负 int，则如实填充；缺失、类型不符、负数、bool、stdout
        # 不可解析一律保持 "unknown" —— 绝不猜测、绝不把 unknown
        # 伪装成 0。解析失败绝不影响调用本身。
        text = stdout.strip()
        try:
            payload = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return "unknown", "unknown"
        if not isinstance(payload, dict):
            return "unknown", "unknown"
        usage = payload.get("usage")
        if not isinstance(usage, dict):
            return "unknown", "unknown"

        def _observed(value):
            if isinstance(value, bool) or not isinstance(value, int):
                return "unknown"
            return value if value >= 0 else "unknown"

        return _observed(usage.get("input_tokens")), _observed(usage.get("output_tokens"))

    @staticmethod
    def _parse_output(stdout: str) -> Any:
        # 只信任 CLI 的 JSON 封装：当 stdout 解析为 {"response": ...}
        # 载荷时，response 文本即为调用输出。其余内容按原始文本原样
        # 返回 —— 该文本是否可用、是否安全，仍由上游 packet 验证
        # 与内容扫描说了算。
        text = stdout.strip()
        if not text:
            return ""
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return text
        if isinstance(payload, dict) and "response" in payload:
            return payload["response"]
        return payload

    @staticmethod
    def _minimal_env() -> dict[str, str]:
        # 白名单，而非黑名单：子进程只收到它执行与定位自身状态
        # 所需的变量 —— 父环境中的其余内容（尤其是携带凭据的
        # 变量）一概不转发。
        return {key: value for key in ("PATH", "HOME", "USERPROFILE", "SYSTEMROOT")
                if (value := os.environ.get(key))}

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
            r"(?<![A-Za-z0-9])(gemini-[A-Za-z0-9_-]{8,})",
        )
        for pattern in patterns:
            if pattern.startswith("(?<!"):
                text = re.sub(pattern, "[REDACTED]", text)
            else:
                text = re.sub(pattern, r"\1[REDACTED]", text)
        return text[:4096]

    @staticmethod
    def _finish_trace(
        trace: InvocationTrace,
        status: InvocationStatus,
        started: float,
        exit_code: int | None = None,
        finished: float | None = None,
        error: str | None = None,
        input_tokens: int | str | None = None,
        output_tokens: int | str | None = None,
    ) -> InvocationTrace:
        finished = time.time() if finished is None else finished
        return InvocationTrace(
            trace.invocation_id, trace.task_id, trace.agent_id, trace.runtime,
            trace.provider, trace.model, trace.role, status, trace.started_at,
            finished, max(0, int((finished - started) * 1000)), exit_code,
            trace.input_tokens if input_tokens is None else input_tokens,
            trace.output_tokens if output_tokens is None else output_tokens,
            error,
        )
