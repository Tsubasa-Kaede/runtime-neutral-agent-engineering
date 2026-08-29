"""外部 runtime adapter 的 OpenAI Codex CLI 实现。

一个具体 adapter；所有 Codex 专属知识只存在于本模块。
注册契约：from_environment 只要求 PATH 上的 codex 可执行文件 ——
缺失时返回 None（未注册），绝不安装 runtime、绝不修改 PATH 或
配置、绝不猜测凭据。调用形态：invoke 使用 codex exec 的官方
非交互模式，提示词作为位置参数传递（不经 stdin），仅当
request.model 存在时附加 --model。

事实面（6 方法）：discover/invoke/cancel 之外实现
check_authentication / check_provider_model / minimal_health_check
—— "具备方法"不等于"已经 REAL VERIFIED"：REAL 资格仍只由
RealGateExecutor 的门控运行授予。Authentication 只通过
codex login status 这一个只读观测面"观测"：对 stdout+stderr 的
合并文本做分类化映射（本机实测 Codex 0.147.0 的状态行走
stderr；Logged in using / Not logged in），文本优先、退出码
辅助，无法可靠解释时如实上报 UNKNOWN —— 原始 stdout/stderr 绝不
离开本模块；绝不使用 --with-api-key / --with-access-token /
--device-auth（凭据写入面），绝不读取 ~/.codex 下的 auth.json 或
凭据文件。认证属于 Codex 自身（用户侧 codex login）。最小 env
白名单不含任何凭据变量 —— 经 env 注入的 API key 对本 adapter
的观测面天然不可见（只观测文件式认证）。minimal_health_check
只在 RUN_REAL_PROVIDER_TESTS=1（REAL gate）时执行，否则诚实
上报 skipped，绝不伪造通过。

错误安全边界：与原始错误字符串不同，所有可能离开本模块的进程
错误都经过 _safe_error，在文本进入 trace、discovery reason 或
报告之前先抹除凭据形态的值（赋值形态、bearer 材料、hf_/sk-
key 形态）。invoke() 与 cancel() 共享的进程簿记由 _state_lock
保护，因此并发调用不会损坏 process/cancelled/completed 集合。
"""
from __future__ import annotations

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


class CodexAdapter:
    """通过真实子进程调用 Codex CLI 的非交互 exec 模式。"""

    def __init__(self, profile: RuntimeProfile, executable: str):
        self.profile = profile
        self.executable = executable
        self._processes: dict[str, subprocess.Popen[str]] = {}
        self._cancelled: set[str] = set()
        self._completed: set[str] = set()
        self._state_lock = threading.Lock()
        self.last_invocation_id: str | None = None
        # 已观测到的认证状态：仅用于 check_authentication() →
        # check_provider_model() 的耦合（provider/model 检查建立在
        # 观测到的认证上）。不是缓存系统，不持久化，不保存原始
        # auth 输出。
        self._auth_provider: str | None = None
        self._auth_authenticated: bool = False

    @classmethod
    def from_environment(
        cls,
        profile: RuntimeProfile | None = None,
    ) -> "CodexAdapter | None":
        # 注册要求可执行文件在场：PATH 上没有 codex 的机器得到的是
        # 诚实的缺席（None），而不是错误或半配置的 adapter。
        executable = shutil.which("codex") or shutil.which("codex.exe")
        if not executable:
            return None
        return cls(
            profile or RuntimeProfile("coding-agent", "codex-cli", "openai", None, "coder", frozenset()),
            executable,
        )

    def discover(self) -> RuntimeDiscovery:
        ok, detail = self._probe()
        return RuntimeDiscovery(
            "codex-cli",
            ok,
            detail if ok else None,
            None if ok else detail,
            frozenset(),
        )

    def _probe(self) -> tuple[bool, str | None]:
        # 每条失败细节都经 _safe_error 清洗：discovery reason 可以携带
        # 进程错误文本（必须保持诚实），但绝不能携带其中的凭据
        # 形态材料。
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

    def invoke(self, request: ExternalAgentRequest) -> InvocationResult:
        invocation_id = new_invocation_id()
        started = time.time()
        trace = InvocationTrace(
            invocation_id,
            request.task_id,
            request.agent_id,
            "codex-cli",
            request.provider,
            request.model,
            request.role,
            InvocationStatus.STARTING,
            started_at=started,
        )
        process: subprocess.Popen[str] | None = None
        # codex exec 的文档化非交互形态：提示词是位置参数（不经
        # stdin 传递）；--model 仅在请求显式指定时附加。
        argv = [self.executable, "exec"]
        if request.model:
            argv.extend(["--model", request.model])
        argv.append(request.prompt)
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
            stdout, stderr = process.communicate(timeout=request.timeout_seconds)
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
            return InvocationResult(
                InvocationStatus.SUCCESS,
                output=stdout.strip(),
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

    def check_authentication(self):
        # Auth 只被"观测"，绝不被执行：codex login status 让 CLI 汇报
        # 自己的登录状态；本模块只保留分类化结果与 method 标签，原始
        # stdout/stderr 永不离开（不进入 trace、结果或报告）。绝不使用
        # --with-api-key / --with-access-token / --device-auth（凭据
        # 写入面），绝不打开 auth.json。显式 UTF-8 解码防止 GBK 控制台
        # 下的解码异常逃过 SubprocessError 捕获。
        from runtime_health import AuthenticationCheck
        from runtime_status import AuthenticationState, ReasonCode
        try:
            result = subprocess.run(
                [self.executable, "login", "status"],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                shell=False,
                env=self._minimal_env(),
            )
        except (OSError, subprocess.SubprocessError):
            return AuthenticationCheck(
                AuthenticationState.UNKNOWN,
                reason_code=ReasonCode.PROTOCOL_ERROR,
            )
        # 合并观察面：本机实测 Codex 0.147.0 的 stdout 为空、状态行
        # 输出在 stderr（前置 PATH alias WARNING），只映射 stdout 会把
        # 真实已登录状态误判为 UNKNOWN。文本优先、退出码辅助不变：
        # 只有 CLI 明说"Logged in using"且进程成功收尾才认
        # AUTHENTICATED —— 宁可 UNKNOWN，绝不猜测成功；原始
        # stdout/stderr 永不离开本模块。
        observed = ((result.stdout or "") + "\n" + (result.stderr or "")).strip()
        if result.returncode == 0 and "Logged in using" in observed:
            method = None
            if "an API key" in observed:
                method = "api-key"
            elif "ChatGPT" in observed:
                method = "chatgpt"
            self._auth_provider = self.profile.provider
            self._auth_authenticated = True
            return AuthenticationCheck(AuthenticationState.AUTHENTICATED, method)
        if "Not logged in" in observed or result.returncode != 0:
            return AuthenticationCheck(
                AuthenticationState.AUTH_REQUIRED,
                reason_code=ReasonCode.AUTH_REQUIRED,
            )
        return AuthenticationCheck(
            AuthenticationState.UNKNOWN,
            reason_code=ReasonCode.PROTOCOL_ERROR,
        )

    def check_provider_model(self):
        # Provider 可用性以上方观测到的认证状态为门；_auth_provider 是
        # 耦合点，这也是 check_authentication 必须先于本检查运行的原因。
        # 未观测或 provider 不匹配时如实上报"不可担保"，绝不默认可用；
        # model 原样透传，绝不猜测。
        from runtime_health import ProviderModelCheck
        from runtime_status import ReasonCode
        provider = self.profile.provider
        if not provider:
            return ProviderModelCheck(
                None, self.profile.model, False, ReasonCode.UNSUPPORTED_HEALTH_CHECK)
        if self._auth_provider != provider or not self._auth_authenticated:
            return ProviderModelCheck(
                provider, self.profile.model, False, ReasonCode.PROVIDER_UNREACHABLE)
        return ProviderModelCheck(provider, self.profile.model, True, ReasonCode.NONE)

    def minimal_health_check(self, timeout_seconds: float):
        # 唯一的 Health 调用是 opt-in 的：没有 REAL gate 时它上报
        # 诚实的 UNSUPPORTED 检查，而不是悄悄运行（也不是悄悄
        # 跳过并伪造一个通过）。与 Claude/Pi adapter 家族语义逐行
        # 一致：timeout 钳位 [1, 30] 秒，CANCELLED 落入非 SUCCESS
        # 通用分支（invoke_failed）。
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

    @staticmethod
    def _minimal_env() -> dict[str, str]:
        # 与其它 CLI adapter 相同的白名单纪律：只转发定位执行所需
        # 的变量；父环境中任何携带凭据的内容都到不了子进程。
        return {
            key: value
            for key in ("PATH", "HOME", "USERPROFILE", "SYSTEMROOT")
            if (value := os.environ.get(key))
        }

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
    def _finish_trace(
        trace: InvocationTrace,
        status: InvocationStatus,
        started: float,
        exit_code: int | None = None,
        finished: float | None = None,
        error: str | None = None,
    ) -> InvocationTrace:
        finished = time.time() if finished is None else finished
        return InvocationTrace(
            trace.invocation_id,
            trace.task_id,
            trace.agent_id,
            trace.runtime,
            trace.provider,
            trace.model,
            trace.role,
            status,
            trace.started_at,
            finished,
            max(0, int((finished - started) * 1000)),
            exit_code,
            trace.input_tokens,
            trace.output_tokens,
            error,
        )
