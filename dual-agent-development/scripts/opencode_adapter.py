"""外部 runtime adapter 的 OpenCode CLI 实现。

一个具体 adapter；所有 OpenCode 专属知识只存在于本模块。
注册契约：from_environment 只要求 PATH 上的 opencode 可执行文件 ——
缺失时返回 None（未注册），绝不安装 runtime、绝不修改 PATH 或
配置、绝不猜测凭据。调用形态：invoke 使用官方非交互模式
`opencode run "<prompt>" --format json`（docs+源码双证），prompt 是
位置参数（argv，与 codex 家族同构）；Coder 位固定 `--agent build`
（官方默认全权 agent —— 显式钉住，防项目级 default_agent 配置把
run 悄悄改道）；--model 仅在请求显式指定时附加，且为
"provider/model" 单串原样透传 —— provider 隐含在 model 串内，
opencode 是多 provider harness，默认 profile 的 provider=None，
绝不把 opencode 当成 provider。

stdin=DEVNULL 是源码正确性而非防御猜测：run.ts 实证非 TTY stdin
会被读取并追加到 message —— /dev/null 读空立即 EOF 无害，而
PIPE 不关会挂起（历史 issue #11891 的真实机制），故绝不给 PIPE。

输出解析（_parse_output）只信任 stdout 的 JSON-lines 事件流
（--format json，每行 {type, timestamp, sessionID, ...data}）：
非 JSON 行一律忽略（源码事实：少数 UI.println 警告不受 format
门控）；error 事件优先于一切 —— 源码实证 session.error 只累积
不置非零退出码，故 rc=0 绝不能掩盖 FAILED。session.idle 不进
stdout 流：干净退出（rc=0）+ 无 error 事件 + 至少一个 text 事件
= idle 的可观测代理，text 事件按序拼接为业务结果。只有工具/步骤
事件而无 text 事件、或输出全空时诚实 FAILED；完全非 JSON 的
stdout 是唯一例外：跟随家族契约（conformance 框架锁定）——
调用级成功 + 原样 raw 文本，是否可用/安全由上游 packet 验证与
内容扫描裁决；本模块绝不伪造内容可用性。stderr 永不参与解析。

Usage 解析（_parse_usage）防御式 step-sum：step_finish.part.tokens
的 input/output 子字段名来自第三方 schema（COMMUNITY 级），REAL
资格轮须逐字段核对 —— 不符时先报告差异（C 类路径），不顺手改
本模块。同一调用同一模型，多 step 用量 = 各 step 之和；某一
step 的某一侧缺失/非 int/负数/bool，该侧总和即整体 unknown
（部分观测污染总和），绝不猜测、绝不把 unknown 伪装成 0。

Authentication 只通过 OpenCode 自身的只读状态面（opencode
auth list，官方文档："Lists all the authenticated providers as
stored in the credentials file"）"观测"：文本优先分类化，输出
形态未实证（REAL 项）—— 无法可靠解释的形态一律归 UNKNOWN，
绝不强行 AUTHENTICATED；本模块绝不 login/logout/refresh，绝不
打开 ~/.local/share/opencode/auth.json。

环境边界：_minimal_env 白名单（PATH/HOME/USERPROFILE/
SYSTEMROOT）之外仅注入一个 adapter 自有的安全旋钮
OPENCODE_DISABLE_AUTOUPDATE=1 —— 官方文档 autoupdate 默认开，
一次调用绝不应触发 runtime 自更新副作用。--dir 是官方工作目录
flag，但冻结的 ExternalAgentRequest 不携带 cwd 字段，本模块不
发明通道（后续契约演进时再接线）。

错误安全边界：所有可能离开本模块的进程错误（FAILED 的 stderr、
error 事件消息、OSError 文本、probe 失败细节）都先经 _safe_error
抹除凭据形态的值，再进入 trace、discovery reason 或报告。
invoke() 与 cancel() 共享的进程簿记（_processes/_cancelled/
_completed）由 _state_lock 保护，并发调用不会损坏集合；在超时
过程中被取消的调用上报 CANCELLED 而非 TIMEOUT。
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


class OpenCodeAdapter:
    """通过真实子进程调用 OpenCode 的非交互 run 模式（JSONL 事件流）。"""

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
        # 注册要求可执行文件在场：PATH 上没有 opencode 的机器得到的是
        # 诚实的缺席（None），而不是错误或半配置的 adapter。默认
        # profile 的 provider 为 None —— opencode 是多 provider
        # harness，provider 隐含在 "provider/model" 串内，绝不猜测。
        executable = shutil.which("opencode") or shutil.which("opencode.exe")
        if not executable:
            return None
        return cls(profile or RuntimeProfile(
            "coding-agent", "opencode", None, None, "coder", frozenset()),
            executable)

    def discover(self) -> RuntimeDiscovery:
        ok, detail = self._probe()
        return RuntimeDiscovery(
            "opencode", ok,
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
        # Auth 只被"观测"，绝不被执行：只读列表命令让 OpenCode 汇报
        # credentials 文件里的已认证 provider；本模块只存储分类化结果，
        # 绝不读取/打印/存储凭据材料，绝不 login/logout，绝不打开
        # auth.json。输出形态未实证（REAL 项）：文本优先分类化，
        # 无法可靠解释的形态一律 UNKNOWN（junk / 未识别的 JSON
        # 形态都算），绝不猜测成功。原始输出绝不离开本模块。
        from runtime_health import AuthenticationCheck
        from runtime_status import AuthenticationState, ReasonCode
        try:
            result = subprocess.run(
                [self.executable, "auth", "list"],
                check=False, capture_output=True, text=True, timeout=10,
                shell=False,
                encoding="utf-8", errors="replace", env=self._minimal_env(),
            )
        except (OSError, subprocess.SubprocessError):
            return AuthenticationCheck(
                AuthenticationState.UNKNOWN, reason_code=ReasonCode.PROTOCOL_ERROR)
        observed = " ".join(
            (result.stdout or "").split() + (result.stderr or "").split())
        lowered = observed.lower()
        negative_stems = (
            "not logged in", "no authenticated", "not authenticated",
            "unauthenticated", "no providers", "logged out",
        )
        if result.returncode != 0 or not observed \
                or any(stem in lowered for stem in negative_stems):
            return AuthenticationCheck(
                AuthenticationState.AUTH_REQUIRED,
                reason_code=ReasonCode.AUTH_REQUIRED)
        if "authenticated" in lowered or "logged in" in lowered:
            self._auth_provider = self.profile.provider
            self._auth_authenticated = True
            return AuthenticationCheck(AuthenticationState.AUTHENTICATED)
        return AuthenticationCheck(
            AuthenticationState.UNKNOWN, reason_code=ReasonCode.PROTOCOL_ERROR)

    def check_provider_model(self):
        # Provider 可用性以上方观测到的认证状态为门；_auth_provider
        # 是耦合点，这也是 check_authentication 必须先于本检查运行的
        # 原因。未观测或 provider 不匹配时如实上报"不可担保"，绝不
        # 默认可用；model 原样透传（provider/model 单串），绝不猜测。
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
            invocation_id, request.task_id, request.agent_id, "opencode",
            request.provider, request.model, request.role, InvocationStatus.STARTING,
            started_at=started,
        )
        # 非交互调用契约：官方 headless 形态 —— prompt 是 run 的位置
        # 参数（不进 stdin，不进 shell 可达位置之外的通道）；
        # --format json 固定（JSONL 事件流）；Coder 位钉住 --agent
        # build（官方默认全权 agent，防 config default_agent 漂移）；
        # --model 仅在请求显式指定时附加（provider/model 单串透传）。
        argv = [
            self.executable,
            "run", request.prompt,
            "--format", "json",
            "--agent", "build",
        ]
        if request.model:
            argv.extend(["--model", request.model])
        process: subprocess.Popen[str] | None = None
        try:
            # stdin=DEVNULL：源码实证非 TTY stdin 会被读取并追加到
            # message —— /dev/null 读空立即 EOF 无害；PIPE 不关会挂起
            # （#11891 机制），故绝不给 PIPE。
            process = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
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
                timeout=request.timeout_seconds)
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
    def _parse_events(stdout: str) -> list[dict]:
        """逐行解析 JSONL 事件流；非 JSON 行一律忽略（源码事实：少数
        UI.println 警告不受 --format 门控）。返回带 type 键的事件列表，
        顺序保持。"""
        events: list[dict] = []
        for line in (stdout or "").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if isinstance(event, dict) and "type" in event:
                events.append(event)
        return events

    @staticmethod
    def _error_text(payload: Any) -> str:
        """从 error 事件的载荷提取诊断文本（不猜测结构：字符串直取，
        dict 依 data.message → name 递减，其余归通用描述）。"""
        if isinstance(payload, str) and payload.strip():
            return payload
        if isinstance(payload, dict):
            data = payload.get("data")
            if isinstance(data, dict) and isinstance(data.get("message"), str):
                return data["message"]
            if isinstance(payload.get("name"), str):
                return payload["name"]
        return "opencode reported an error event"

    @classmethod
    def _parse_output(cls, stdout: str) -> tuple[bool, Any, str | None]:
        """解析 opencode 的 JSONL 事件流。

        返回 (ok, output, reason)：成功时 output 是 text 事件按序拼接
        的业务结果；失败时 output 是截断 raw stdout（供错误文本保留
        现场），reason 是结构化失败原因。裁决顺序：error 事件优先
        （源码：session.error 只累积不置非零退出码，rc=0 掩盖不了
        FAILED）；其次 text 事件（idle 的可观测代理 = 干净退出 + 无
        error + 有业务文本）；只有结构化事件而无 text、或输出全空
        时诚实 FAILED；完全非 JSON 的 stdout 是唯一例外：跟随家族
        raw-fallback 契约（conformance 锁定）返回调用级成功 + 原样
        raw 文本，内容裁决权在上游 packet 验证。
        """
        events = cls._parse_events(stdout)
        for event in events:
            if event.get("type") == "error":
                return False, "", cls._error_text(event.get("error"))
        texts: list[str] = []
        for event in events:
            if event.get("type") != "text":
                continue
            part = event.get("part")
            text = part.get("text") if isinstance(part, dict) else None
            if isinstance(text, str) and text:
                texts.append(text)
        if texts:
            return True, "\n".join(texts), None
        raw = (stdout or "").strip()
        if not events:
            if raw:
                return True, raw, None
            return False, "", "opencode produced no output"
        return False, raw[:_RAW_OUTPUT_LIMIT], \
            "opencode produced no text output"

    @staticmethod
    def _parse_usage(stdout: str) -> tuple[int | str, int | str]:
        # 防御式 step-sum：多 step 同调用同模型，token 用量 = 各
        # step_finish 事件的 part.tokens 求和。子字段名（input/output）
        # 来自第三方 schema（COMMUNITY 级），REAL 资格轮须逐字段核对。
        # 某一 step 的某一侧缺失/非 int/bool/负数 → 该侧总和整体
        # unknown（部分观测污染总和），绝不猜测、绝不把 unknown 伪装
        # 成 0。没有 step_finish 事件、流不可解析一律保持 unknown。
        # 解析失败绝不影响调用本身。
        events = OpenCodeAdapter._parse_events(stdout)
        input_total = 0
        input_ok = True
        output_total = 0
        output_ok = True
        steps = 0
        for event in events:
            if event.get("type") != "step_finish":
                continue
            steps += 1
            part = event.get("part")
            tokens = part.get("tokens") if isinstance(part, dict) else None
            if not isinstance(tokens, dict):
                tokens = {}
            input_value = tokens.get("input")
            if isinstance(input_value, bool) or not isinstance(input_value, int) \
                    or input_value < 0:
                input_ok = False
            else:
                input_total += input_value
            output_value = tokens.get("output")
            if isinstance(output_value, bool) or not isinstance(output_value, int) \
                    or output_value < 0:
                output_ok = False
            else:
                output_total += output_value
        if steps == 0:
            return "unknown", "unknown"
        return (input_total if input_ok else "unknown",
                output_total if output_ok else "unknown")

    @staticmethod
    def _minimal_env() -> dict[str, str]:
        # 白名单，而非黑名单：子进程只收到它执行与定位自身状态
        # 所需的变量 —— 父环境中的其余内容（尤其是携带凭据的
        # 变量与 CLI 配置变量，如 *_API_KEY、OPENCODE_CONFIG*）
        # 一概不转发。唯一的额外项是 adapter 自有的安全旋钮
        # OPENCODE_DISABLE_AUTOUPDATE=1：官方文档 autoupdate 默认
        # 开，一次调用绝不应触发 runtime 自更新副作用。
        env = {key: value for key in ("PATH", "HOME", "USERPROFILE", "SYSTEMROOT")
               if (value := os.environ.get(key))}
        env["OPENCODE_DISABLE_AUTOUPDATE"] = "1"
        return env

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
