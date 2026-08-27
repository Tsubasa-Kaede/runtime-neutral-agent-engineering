"""外部 runtime adapter 的 OpenAI Codex CLI 实现。

一个具体 adapter；所有 Codex 专属知识只存在于本模块。
注册契约：from_environment 只要求 PATH 上的 codex 可执行文件 ——
缺失时返回 None（未注册），绝不安装 runtime、绝不修改 PATH 或
配置、绝不猜测凭据。调用形态：invoke 使用 codex exec 的官方
非交互模式，提示词作为位置参数传递（不经 stdin），仅当
request.model 存在时附加 --model。

Level B 边界：本 adapter 不实现 check_authentication /
check_provider_model / minimal_health_check —— 没有 health 就不可
能 READY，更不可能进入 Verified Runtime Pool；REAL 验证属于未来
独立的门控任务。认证属于 Codex 自身（用户侧 codex login），本
模块绝不读取 ~/.codex 下的凭据或配置文件。

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
