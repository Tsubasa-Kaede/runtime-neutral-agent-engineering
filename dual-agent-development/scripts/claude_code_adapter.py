"""Claude Code CLI implementation of the provider-neutral runtime adapter."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
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
        self._auth_provider: str | None = None

    @classmethod
    def from_environment(cls, profile: RuntimeProfile | None = None):
        executable = shutil.which("claude") or shutil.which("claude.exe")
        if not executable:
            return None
        return cls(profile or RuntimeProfile("coding-agent", "claude-cli", "anthropic", None, "coder", frozenset()), executable)

    def discover(self) -> RuntimeDiscovery:
        ok, detail = self._probe()
        return RuntimeDiscovery("claude-cli", ok, detail if ok else None, None if ok else detail, frozenset())

    def _probe(self) -> tuple[bool, str]:
        try:
            process = subprocess.run(
                [self.executable, "--version"],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
                shell=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return False, str(exc)
        if process.returncode != 0:
            return False, (process.stderr or "runtime probe failed").strip()
        return True, (process.stdout or process.stderr).strip()

    def check_authentication(self):
        from runtime_health import AuthenticationCheck
        from runtime_status import AuthenticationState, ReasonCode
        try:
            result = subprocess.run(
                [self.executable, "auth", "status", "--json"],
                check=False, capture_output=True, text=True, timeout=10, shell=False,
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
        from runtime_health import ProviderModelCheck
        from runtime_status import ReasonCode
        if not self.profile.provider:
            return ProviderModelCheck(None, self.profile.model, False, ReasonCode.UNSUPPORTED_HEALTH_CHECK)
        if self.profile.provider != "anthropic" or self._auth_provider not in {"firstParty", "anthropic"}:
            return ProviderModelCheck(self.profile.provider, self.profile.model, False, ReasonCode.PROVIDER_UNREACHABLE)
        return ProviderModelCheck(self.profile.provider, self.profile.model, True, ReasonCode.NONE)

    def minimal_health_check(self, timeout_seconds: float):
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
        argv = [self.executable, "--print", "--output-format", "json", "--no-session-persistence"]
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
            self._processes[invocation_id] = process
            trace = self._finish_trace(trace, InvocationStatus.INVOKED, started)
            stdout, stderr = process.communicate(request.prompt, timeout=request.timeout_seconds)
            finished = time.time()
            if process.returncode != 0:
                return InvocationResult(
                    InvocationStatus.FAILED,
                    error=(stderr or "external runtime failed").strip(),
                    trace=self._finish_trace(trace, InvocationStatus.FAILED, started, process.returncode, finished, (stderr or "").strip()),
                )
            output = self._parse_output(stdout)
            return InvocationResult(
                InvocationStatus.SUCCESS,
                output=output,
                trace=self._finish_trace(trace, InvocationStatus.SUCCESS, started, process.returncode, finished),
            )
        except (subprocess.TimeoutExpired, TimeoutError):
            process.kill()
            try:
                process.communicate()
            except (subprocess.TimeoutExpired, TimeoutError, OSError):
                pass
            return InvocationResult(
                InvocationStatus.TIMEOUT,
                error="external runtime timeout",
                trace=self._finish_trace(trace, InvocationStatus.TIMEOUT, started, None, time.time(), "external runtime timeout"),
            )
        except OSError as exc:
            return InvocationResult(
                InvocationStatus.UNAVAILABLE,
                error=str(exc),
                trace=self._finish_trace(trace, InvocationStatus.UNAVAILABLE, started, None, time.time(), str(exc)),
            )
        finally:
            self._processes.pop(invocation_id, None)

    def cancel(self, invocation_id: str) -> InvocationResult:
        process = self._processes.get(invocation_id)
        if process is None:
            return InvocationResult(InvocationStatus.UNAVAILABLE, error="unknown invocation")
        process.kill()
        return InvocationResult(InvocationStatus.CANCELLED)

    @staticmethod
    def _parse_output(stdout: str) -> Any:
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
        return {key: value for key in ("PATH", "HOME", "USERPROFILE", "SYSTEMROOT") if (value := os.environ.get(key))}

    @staticmethod
    def _finish_trace(trace, status, started, exit_code=None, finished=None, error=None):
        finished = time.time() if finished is None else finished
        return InvocationTrace(
            trace.invocation_id, trace.task_id, trace.agent_id, trace.runtime,
            trace.provider, trace.model, trace.role, status, trace.started_at,
            finished, max(0, int((finished - started) * 1000)), exit_code,
            trace.input_tokens, trace.output_tokens, error,
        )
