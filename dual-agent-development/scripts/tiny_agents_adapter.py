"""Hugging Face tiny-agents CLI implementation of the external runtime adapter."""
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


class TinyAgentsAdapter:
    """Invoke a configured tiny-agents runner through a real subprocess."""

    def __init__(
        self,
        profile: RuntimeProfile,
        executable: str,
        agent_path: str,
        command: str,
        command_args: tuple[str, ...] = (),
    ):
        self.profile = profile
        self.executable = executable
        self.agent_path = agent_path
        self.command = command
        self.command_args = tuple(command_args)
        self._processes: dict[str, subprocess.Popen[str]] = {}
        self._cancelled: set[str] = set()
        self._completed: set[str] = set()
        self._state_lock = threading.Lock()
        self.last_invocation_id: str | None = None

    @classmethod
    def from_environment(
        cls,
        profile: RuntimeProfile | None = None,
        *,
        agent_path: str | None = None,
        command: str | None = None,
        command_args: tuple[str, ...] = (),
    ) -> "TinyAgentsAdapter" | None:
        executable = shutil.which("tiny-agents") or shutil.which("tiny-agents.exe")
        resolved_path = agent_path or os.environ.get("TINY_AGENTS_AGENT_PATH")
        resolved_command = command or os.environ.get("TINY_AGENTS_COMMAND")
        if not executable or not resolved_path or not resolved_command:
            return None
        return cls(
            profile or RuntimeProfile("tiny-agent", "tiny-agents", None, None, "coder", frozenset()),
            executable,
            resolved_path,
            resolved_command,
            command_args,
        )

    def discover(self) -> RuntimeDiscovery:
        ok, detail = self._probe()
        return RuntimeDiscovery(
            "tiny-agents",
            ok,
            None,
            None if ok else detail,
            frozenset(),
        )

    def _probe(self) -> tuple[bool, str | None]:
        try:
            process = subprocess.run(
                [self.executable, "--help"],
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
        return True, None

    def invoke(self, request: ExternalAgentRequest) -> InvocationResult:
        invocation_id = new_invocation_id()
        started = time.time()
        trace = InvocationTrace(
            invocation_id,
            request.task_id,
            request.agent_id,
            "tiny-agents",
            request.provider,
            request.model,
            request.role,
            InvocationStatus.STARTING,
            started_at=started,
        )
        process: subprocess.Popen[str] | None = None
        argv = [self.executable, "run", self.agent_path, self.command, *self.command_args]
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
                output=stdout.strip(),
                trace=self._finish_trace(trace, InvocationStatus.SUCCESS, started, process.returncode, finished),
            )
        except (subprocess.TimeoutExpired, TimeoutError):
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
        return {
            key: value
            for key in ("PATH", "HOME", "USERPROFILE", "SYSTEMROOT")
            if (value := os.environ.get(key))
        }

    @staticmethod
    def _safe_error(value: str) -> str:
        """Redact common credential-shaped values before exposing process errors."""
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
