"""外部 runtime adapter 的 Cline CLI 实现。

一个具体 adapter；所有 Cline 专属知识只存在于本模块。
注册契约：from_environment 只要求 PATH 上的 cline 可执行文件 ——
缺失时返回 None（未注册），绝不安装 runtime、绝不修改 PATH 或
配置、绝不猜测凭据。调用形态：invoke 使用 Cline CLI 的非交互
headless 模式（--json 输出 NDJSON 事件流），提示词经 stdin 传入
—— 官方形态是"prompt 为 argv 参数或 piped stdin"，本 adapter 选
piped stdin，与 claude/pi/gemini/qwen 家族纪律一致：规避 argv
长度限制，且 prompt 永不进入 shell 可达位置。--model 仅在请求
显式指定时附加；provider 不进入请求契约（不同 provider 应注册
为不同 Runtime Identity / config fingerprint）。

事实面（3 方法，conformance L0）：只实现 discover/invoke/cancel。
Cline CLI 没有可观测的只读 auth 状态面 —— `cline auth` 是交互
登录动作（不是状态查询），`--key` 是凭据材料，env 凭据变量又不
被白名单转发 —— 因此 check_authentication / check_provider_model /
minimal_health_check 必须缺席而非伪造（家族纪律，tiny-agents
先例）。官方承诺非交互运行遇 OAuth provider 无已存凭据时快速
失败并给出 authentication 消息（不弹浏览器）：这类失败经 invoke
的正常 FAILED 面（stderr 细节抹除凭据后保留）诚实地到达上游，
由上游依据错误文本分类，本模块绝不替上游猜测。

输出解析（_parse_output）只信任 stdout 的 NDJSON 流：逐行
json.loads；文本只取官方 jq 示例证实的事件形态
（type == "agent_event" 且 event.text 为 str），按序以换行连接
（与 jq -r 的逐事件输出语义一致）。全部行可解析但零条文本事件
（如只有 session/tool 事件）或 stdout 为空时保留截断 raw 输出后
诚实 FAILED。任一行不可解析、或解析出非事件对象（数字/字符串/
数组）—— stdout 不是可靠 NDJSON —— 跟随家族 raw-fallback 契约
（conformance 锁定）：调用级成功 + 原样 raw 文本，是否可用/安全
由上游 packet 验证与内容扫描裁决；本模块绝不伪造内容可用性。
stderr 永不参与解析。

Usage 解析不做（HONEST_UNKNOWN）：NDJSON 的 usage 字段形态未经
REAL 核对，观测面尚未申报 —— token 形文本与 usage 形字段一律
不刮取，trace 保持 "unknown"，绝不猜测、绝不把 unknown 伪装成
0。REAL 资格与观测面申报仍只由 RealGateExecutor 的门控运行授予。

错误安全边界：所有可能离开本模块的进程错误（FAILED 的 stderr、
OSError 文本、probe 失败细节）都先经 _safe_error 抹除凭据形态的
值（赋值形态、bearer 材料、hf_/sk- 形态），再进入 trace、
discovery reason 或报告。invoke() 与 cancel() 共享的进程簿记
（_processes/_cancelled/_completed）由 _state_lock 保护，并发
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

# FAILED 错误文本里保留的截断 raw stdout 上限（家族 _safe_error 的
# 4096 总上限之外再收紧一层，避免大输出膨胀 trace）。
_RAW_OUTPUT_LIMIT = 512


class ClineAdapter:
    """通过真实子进程调用 Cline CLI 的非交互 headless 模式（NDJSON 输出）。"""

    def __init__(self, profile: RuntimeProfile, executable: str):
        self.profile = profile
        self.executable = executable
        self._processes: dict[str, subprocess.Popen[str]] = {}
        self._cancelled: set[str] = set()
        self._completed: set[str] = set()
        self._state_lock = threading.Lock()
        self.last_invocation_id: str | None = None

    @classmethod
    def from_environment(cls, profile: RuntimeProfile | None = None):
        # 注册要求可执行文件在场：PATH 上没有 cline 的机器得到的是
        # 诚实的缺席（None），而不是错误或半配置的 adapter。
        executable = shutil.which("cline") or shutil.which("cline.exe")
        if not executable:
            return None
        return cls(profile or RuntimeProfile(
            "coding-agent", "cline-cli", "cline", None, "coder", frozenset()),
            executable)

    def discover(self) -> RuntimeDiscovery:
        ok, detail = self._probe()
        return RuntimeDiscovery(
            "cline-cli", ok,
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

    def invoke(self, request: ExternalAgentRequest) -> InvocationResult:
        invocation_id = new_invocation_id()
        started = time.time()
        trace = InvocationTrace(
            invocation_id, request.task_id, request.agent_id, "cline-cli",
            request.provider, request.model, request.role, InvocationStatus.STARTING,
            started_at=started,
        )
        # 非交互调用契约：--json（NDJSON 事件流输出）；提示词经 stdin
        # 传入（官方允许 prompt argv 或 piped stdin，家族纪律选后者），
        # argv 不携带提示词；--model 仅在请求显式指定时附加。
        argv = [self.executable, "--json"]
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
            ok, output, reason = self._parse_output(stdout)
            if not ok:
                # 结构化输出不可判定成功：保留截断 raw（抹除后）并诚实
                # FAILED —— 绝不伪造成功（output 携带截断 raw 文本）。
                error = self._safe_error(f"{reason}; stdout: {output}")
                return InvocationResult(
                    InvocationStatus.FAILED,
                    error=error,
                    trace=self._finish_trace(
                        trace, InvocationStatus.FAILED, started,
                        process.returncode, finished, error),
                )
            return InvocationResult(
                InvocationStatus.SUCCESS,
                output=output,
                trace=self._finish_trace(
                    trace, InvocationStatus.SUCCESS, started,
                    process.returncode, finished),
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
    def _parse_output(stdout: str) -> tuple[bool, Any, str | None]:
        """解析 cline --json 的 NDJSON 事件流。

        返回 (ok, output, reason)：成功时 output 是按序以换行连接的
        agent_event 文本（官方 jq 示例证实的事件形态）；失败时 output
        是截断 raw stdout（供错误文本保留现场），reason 是结构化失败
        原因。任一行不可解析、或解析出非事件对象 —— stdout 不是可靠
        NDJSON —— 是唯一例外：跟随家族 raw-fallback 契约
        （conformance 锁定）返回调用级成功 + 原样 raw 文本，内容裁决
        权在上游 packet 验证。
        """
        text = stdout.strip()
        if not text:
            return False, "", "cline produced no structured output"
        texts: list[str] = []
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                return True, text, None
            if not isinstance(event, dict):
                return True, text, None
            if event.get("type") == "agent_event":
                payload = event.get("event")
                if isinstance(payload, dict):
                    value = payload.get("text")
                    if isinstance(value, str) and value:
                        texts.append(value)
        if not texts:
            return False, text[:_RAW_OUTPUT_LIMIT], \
                "cline output has no agent text events"
        return True, "\n".join(texts), None

    @staticmethod
    def _minimal_env() -> dict[str, str]:
        # 白名单，而非黑名单：子进程只收到它执行与定位自身状态
        # 所需的变量 —— 父环境中的其余内容（尤其是携带凭据的
        # 变量与 CLI 配置变量，如 *_API_KEY、CLINE_*）一概不转发；
        # cline 的已存凭据由其在 ~/.cline 的自身状态解析。
        return {key: value for key in ("PATH", "HOME", "USERPROFILE", "SYSTEMROOT", "TEMP", "TMP")
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
            trace.invocation_id, trace.task_id, trace.agent_id, trace.runtime,
            trace.provider, trace.model, trace.role, status, trace.started_at,
            finished, max(0, int((finished - started) * 1000)), exit_code,
            trace.input_tokens, trace.output_tokens, error,
        )
