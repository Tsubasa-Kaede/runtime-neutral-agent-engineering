"""外部 runtime adapter 的 pi coding agent CLI 实现。

一个具体 adapter；所有 pi 专属知识只存在于本模块。
注册契约：from_environment 只要求 PATH 上的 pi 可执行文件 ——
缺失时返回 None（未注册），绝不安装 runtime、绝不修改 PATH 或
配置、绝不猜测凭据。调用形态：invoke 使用 pi 的 print 模式
（-p，一次性、无 TUI）+ JSON 事件流（--mode json），提示词经
stdin 传入 —— pi 在 print 模式下把管道 stdin 合并进首条 prompt，
由此规避 argv 长度限制；--no-session/--no-tools/--no-extensions/
--no-skills/--no-context-files 保证一次调用不留 session、不带
工具、不加载任何本地发现面。--model 仅在请求显式指定时附加。

事实面（6 方法）：discover/invoke/cancel 之外实现
check_authentication / check_provider_model / minimal_health_check。
Authentication 只通过 pi auth check --provider <p> --json
--no-refresh 这一个就绪检查变体"观测"：该变体输出分类化状态
（ready/not_ready/invalid）与 authType 标签，不返回凭据材料。
本模块绝不使用 --credentials、auth print-api-key 或
auth print-bearer-token（凭据打印面），绝不打开 pi 的凭据存储。
pi 是多 provider runtime：profile 未指明 provider 时，auth 与
provider 检查如实上报 UNSUPPORTED，默认 provider 不猜测。
minimal_health_check 只在 RUN_REAL_PROVIDER_TESTS=1（REAL
gate）时执行，否则诚实上报 skipped，绝不伪造通过。

输出解析（_parse_output）只信任 stdout 的 JSON-lines 事件流：
最终输出取最后一个 agent_end 事件里最后一条带文本部分的
assistant 消息；流缺失或无法解析时按原始进程文本原样返回，
并继续受上游 packet/内容安全边界约束 —— 绝不直接进入 packet
或 ledger。

错误安全边界：与原始错误字符串不同，所有可能离开本模块的
进程错误都经过 _safe_error，在文本进入 trace、discovery reason
或报告之前先抹除凭据形态的值（赋值形态、bearer 材料、hf_/sk-
key 形态）。invoke() 与 cancel() 共享的进程簿记由 _state_lock
保护，因此并发调用不会损坏 process/cancelled/completed 集合。
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


class PiAdapter:
    """通过真实子进程调用 pi CLI 的 print 模式（JSON 事件流）。"""

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
    def from_environment(
        cls,
        profile: RuntimeProfile | None = None,
    ) -> "PiAdapter | None":
        # 注册要求可执行文件在场：PATH 上没有 pi 的机器得到的是
        # 诚实的缺席（None），而不是错误或半配置的 adapter。默认
        # profile 的 provider 为 None —— pi 是多 provider runtime，
        # provider 必须由配置显式给出，本模块绝不猜测。
        executable = shutil.which("pi") or shutil.which("pi.exe")
        if not executable:
            return None
        return cls(
            profile or RuntimeProfile("coding-agent", "pi-cli", None, None, "coder", frozenset()),
            executable,
        )

    def discover(self) -> RuntimeDiscovery:
        ok, detail = self._probe()
        return RuntimeDiscovery(
            "pi-cli",
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

    def check_authentication(self):
        # Auth 只被"观测"，绝不被执行：就绪检查变体（auth check
        # --json --no-refresh）让 pi 汇报自己的分类化登录状态；
        # 本模块只存储分类化结果与 authType 标签，绝不使用
        # --credentials（凭据打印面），绝不打开 pi 的凭据存储。
        from runtime_health import AuthenticationCheck
        from runtime_status import AuthenticationState, ReasonCode
        provider = self.profile.provider
        if not provider:
            # 多 provider runtime：没有 provider 就无法定位任何
            # auth 检查 —— 上报 UNSUPPORTED，绝不盲目探测。
            return AuthenticationCheck(
                AuthenticationState.UNKNOWN,
                reason_code=ReasonCode.UNSUPPORTED_HEALTH_CHECK,
            )
        try:
            result = subprocess.run(
                [self.executable, "auth", "check", "--provider", provider, "--json", "--no-refresh"],
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
        payload = self._parse_status_json(result.stdout)
        status = payload.get("status")
        if status == "ready":
            self._auth_provider = provider
            self._auth_authenticated = True
            return AuthenticationCheck(AuthenticationState.AUTHENTICATED, payload.get("authType"))
        if status == "not_ready":
            return AuthenticationCheck(
                AuthenticationState.AUTH_REQUIRED,
                reason_code=ReasonCode.AUTH_REQUIRED,
            )
        # status "invalid"（pi 侧 invalid_state）或任何意外形态：
        # 分类化为 UNKNOWN，原始 reason 字符串不离开本模块。
        return AuthenticationCheck(
            AuthenticationState.UNKNOWN,
            reason_code=ReasonCode.PROTOCOL_ERROR,
        )

    def check_provider_model(self):
        # Provider 可用性以上方观测到的 auth 就绪状态为门
        # （auth check ready）；_auth_provider 是耦合点，这也是
        # check_authentication 必须先于本检查运行的原因。未观测或
        # provider 不匹配时如实上报"不可担保"，绝不默认可用。
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
            invocation_id,
            request.task_id,
            request.agent_id,
            "pi-cli",
            request.provider,
            request.model,
            request.role,
            InvocationStatus.STARTING,
            started_at=started,
        )
        process: subprocess.Popen[str] | None = None
        # pi print 模式的文档化非交互形态：提示词经 stdin 传入
        # （pi 把管道 stdin 合并进首条 prompt，规避 argv 长度
        # 限制），argv 不携带提示词；--model 仅在请求显式指定时
        # 附加。
        argv = [
            self.executable,
            "-p",
            "--mode", "json",
            "--no-session",
            "--no-tools",
            "--no-extensions",
            "--no-skills",
            "--no-context-files",
        ]
        if request.model:
            argv.extend(["--model", request.model])
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
            return InvocationResult(
                InvocationStatus.SUCCESS,
                output=self._parse_output(stdout),
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
        # 只信任 pi 的 JSON-lines 事件流封装：最终输出取最后一个
        # agent_end 事件里最后一条带文本部分的 assistant 消息
        # （事件流可能含多个 agent_end，如 steering 后的续跑）。
        # 流缺失或无法解析时按原始进程文本原样返回 —— 该文本是否
        # 可用、是否安全，仍由上游 packet 验证与内容扫描说了算。
        final: Any = None
        found = False
        for line in (stdout or "").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict) or event.get("type") != "agent_end":
                continue
            text = PiAdapter._final_assistant_text(event.get("messages"))
            if text is not None:
                final = text
                found = True
        if found:
            return final
        return (stdout or "").strip()

    @staticmethod
    def _final_assistant_text(messages: Any) -> str | None:
        # 最后一条带 text 部分的 assistant 消息的拼接文本；没有
        # 这样的消息（纯 toolcall、非 assistant 收尾）时返回
        # None，让调用方落到 raw 回退或更早的 agent_end。
        if not isinstance(messages, list):
            return None
        for message in reversed(messages):
            if not isinstance(message, dict) or message.get("role") != "assistant":
                continue
            parts = message.get("content")
            if not isinstance(parts, list):
                continue
            text = "".join(
                part.get("text", "")
                for part in parts
                if isinstance(part, dict) and part.get("type") == "text"
            )
            if text:
                return text
        return None

    @staticmethod
    def _parse_status_json(stdout: str) -> dict:
        # auth check --json 输出单行 JSON；取最后一个非空行，解析
        # 失败时返回空 dict（调用方按意外形态分类化）。
        lines = [line.strip() for line in (stdout or "").splitlines() if line.strip()]
        if not lines:
            return {}
        try:
            payload = json.loads(lines[-1])
        except json.JSONDecodeError:
            return {}
        return payload if isinstance(payload, dict) else {}

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
