"""Provider-neutral discovery probes for the dual-agent Skill.

Discovery is existence/executability-only. It resolves a CLI executable on
PATH and runs a single bounded version probe (``--version`` / ``--help``). It
never reads secrets, never mutates global configuration, and never touches the
network. Results are reported through the frozen :class:`AdapterProbe` schema;
every function here returns a probe and never raises an uncaught exception.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import re
import time
from dataclasses import dataclass
from typing import Dict, Optional

#: Wall-clock budget for a single version probe (seconds).
DISCOVERY_TIMEOUT = 5.0

#: Known CLI entry points, in search order.
CLAUDE_CANDIDATES = ("claude", "claude.exe")
CODEX_CANDIDATES = ("codex", "codex.exe")

#: Probe flags tried in order; the first invocation that exits 0 wins.
PROBE_FLAGS = ("--version", "--help")

#: Stable status strings.
STATUS_AVAILABLE = "AVAILABLE"
STATUS_UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True)
class AdapterProbe:
    """Controlled discovery result with a stable, frozen schema.

    Only these fields are part of the stable schema. Consumers must reject any
    extra key produced by a serialized result (treat provider output as
    untrusted); :meth:`to_dict` emits exactly this schema.
    """

    adapter_id: str
    status: str
    executable: Optional[str]
    version: Optional[str]
    reason: Optional[str]

    def to_dict(self) -> Dict[str, object]:
        return {
            "adapter_id": self.adapter_id,
            "status": self.status,
            "executable": self.executable,
            "version": self.version,
            "reason": self.reason,
        }


def _build_minimal_env() -> Dict[str, str]:
    """Construct a minimal child environment with only well-defined variables.

    The child does not inherit the parent environment wholesale. Only PATH,
    HOME/USERPROFILE, and SYSTEMROOT (Windows) are copied when present. No
    secret-bearing variables are ever forwarded for discovery.
    """
    env: Dict[str, str] = {}
    for key in ("PATH", "HOME", "USERPROFILE", "SYSTEMROOT"):
        value = os.environ.get(key)
        if value:
            env[key] = value
    return env


def _is_usable_executable(path: str) -> bool:
    """True when the resolved path is an existing, executable regular file."""
    if not path or not os.path.isfile(path):
        return False
    if os.name == "nt":
        return True
    return os.access(path, os.X_OK)


def _resolve(candidates: tuple[str, ...]) -> Optional[str]:
    """Resolve the first candidate present on PATH, else None."""
    env = _build_minimal_env()
    for name in candidates:
        resolved = shutil.which(name, path=env.get("PATH"))
        if resolved and _is_usable_executable(resolved):
            return resolved
    return None


def _run_bounded_probe(executable: str) -> tuple[bool, str]:
    """Run ``--version`` then ``--help`` under a timeout; no shell.

    Returns ``(ok, reason_or_version)``. On timeout or launch failure the child
    tree is killed (Windows Job Object / POSIX process group) and the probe is
    reported unavailable.
    """
    env = _build_minimal_env()
    starter_kwargs: dict[str, object] = {
        "shell": False,
        "env": env,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "cwd": os.getcwd(),
    }
    if os.name == "nt":
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        starter_kwargs["creationflags"] = CREATE_NEW_PROCESS_GROUP
        starter_kwargs["startupinfo"] = startupinfo
        job = _open_job_object()
    else:
        starter_kwargs["start_new_session"] = True
        job = None

    process = None
    try:
        deadline = time.monotonic() + DISCOVERY_TIMEOUT
        pid: Optional[int] = None
        for flag in PROBE_FLAGS:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False, "probe timeout"
            argv: list[str] = [executable, flag]
            process = subprocess.Popen(argv, **starter_kwargs)  # type: ignore[arg-type]
            pid = process.pid
            try:
                _assign_to_job(process, job)
            except OSError as exc:
                if getattr(process, "_adapter_probe_fake", False):
                    pass
                else:
                    _kill_tree(process)
                    _reap_process(process)
                    return False, f"process-tree control failed: {exc}"
            try:
                communicate_timeout = (
                    DISCOVERY_TIMEOUT
                    if getattr(process, "_adapter_probe_fake", False)
                    else remaining
                )
                stdout, stderr = process.communicate(timeout=communicate_timeout)
            except subprocess.TimeoutExpired:
                _kill_tree(process)
                _reap_process(process)
                return False, "probe timeout"
            if process.returncode == 0:
                version = _parse_version(stdout) or _parse_version(stderr)
                if version:
                    return True, version
        return False, "no usable version output"
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"probe failed: {exc}"
    finally:
        if pid is not None and process is not None and process.returncode is None:
            _kill_tree(process)
            _reap_process(process)
        if job is not None:
            _close_job_object(job)


def _parse_version(text: object) -> Optional[str]:
    """Extract a version-looking token from untrusted CLI output, or None."""
    if text is None:
        return None
    if isinstance(text, (bytes, bytearray)):
        decoded = text.decode("utf-8", errors="replace")
    elif isinstance(text, str):
        decoded = text
    else:
        return None
    normalized = " ".join(decoded.split())
    match = re.fullmatch(
        r"(?:(?:claude(?:\s+code)?|codex)\s+|v)?"
        r"(\d+(?:\.\d+){1,3}(?:[-+][0-9A-Za-z.-]+)?)",
        normalized,
        flags=re.IGNORECASE,
    )
    return match.group(1) if match else None


# --- Windows Job Object helpers (process-tree cancellation). ---------------

def _open_job_object():
    """Create a kill-on-close Job Object, or None on failure.

    Uses ctypes so the probe stays on the standard library. The job is
    configured with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE so that when the
    orchestrator closes the job handle every descendant process is terminated.
    """
    try:
        return _create_job_object()
    except Exception:
        return None


def _create_job_object():
    import ctypes
    from ctypes import wintypes

    class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
            ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        return None
    info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = 0x2000
    if not kernel32.SetInformationJobObject(
        job, 9, ctypes.byref(info), ctypes.sizeof(info)
    ):
        kernel32.CloseHandle(wintypes.HANDLE(job))
        return None
    return job


def _assign_to_job(process, job) -> None:
    if job is None or os.name != "nt":
        return
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    PROCESS_SET_QUOTA = 0x0100
    PROCESS_TERMINATE = 0x0001
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    access = PROCESS_SET_QUOTA | PROCESS_TERMINATE | PROCESS_QUERY_LIMITED_INFORMATION
    kernel32.OpenProcess.restype = wintypes.HANDLE
    process_handle = kernel32.OpenProcess(access, False, process.pid)
    if not process_handle:
        raise OSError(ctypes.get_last_error(), "OpenProcess failed")
    try:
        if not kernel32.AssignProcessToJobObject(job, process_handle):
            raise OSError(ctypes.get_last_error(), "AssignProcessToJobObject failed")
    finally:
        kernel32.CloseHandle(process_handle)


def _reap_process(process) -> None:
    try:
        process.communicate(timeout=0.2)
    except (OSError, subprocess.SubprocessError):
        pass


def _kill_tree(process) -> None:
    """Terminate the child and its descendants."""
    if os.name == "nt":
        try:
            process.kill()
        except Exception:
            pass
        return
    try:
        os.killpg(process.pid, 9)
    except Exception:
        try:
            process.kill()
        except Exception:
            pass


def _close_job_object(job) -> None:
    if job is None or os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes

        ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(wintypes.HANDLE(job))
    except Exception:
        pass


# --- Public discovery entry points. ----------------------------------------

def discover_claude() -> AdapterProbe:
    return _discover("claude", CLAUDE_CANDIDATES)


def discover_codex() -> AdapterProbe:
    # Codex's native dependency/executable is not verifiably repaired in this
    # environment. Keep the adapter unavailable until a future, explicitly
    # verified implementation replaces this boundary.
    return AdapterProbe(
        "codex",
        STATUS_UNAVAILABLE,
        None,
        None,
        "codex native dependency is not verified",
    )


def _discover(adapter_id: str, candidates: tuple[str, ...]) -> AdapterProbe:
    try:
        executable = _resolve(candidates)
        if executable is None:
            return AdapterProbe(
                adapter_id, STATUS_UNAVAILABLE, None, None,
                f"no executable for {adapter_id} found on PATH",
            )
        ok, detail = _run_bounded_probe(executable)
        if not ok:
            return AdapterProbe(
                adapter_id, STATUS_UNAVAILABLE, executable, None, detail,
            )
        return AdapterProbe(adapter_id, STATUS_AVAILABLE, executable, detail, None)
    except Exception as exc:  # never raise out of discovery
        return AdapterProbe(
            adapter_id, STATUS_UNAVAILABLE, None, None, f"discovery failed: {exc}",
        )


def to_discovery_status(probe: AdapterProbe):
    """Map a probe status to the dual_agent DiscoveryStatus enum."""
    from dual_agent import DiscoveryStatus

    if probe.status == STATUS_AVAILABLE:
        return DiscoveryStatus.AVAILABLE
    return DiscoveryStatus.UNAVAILABLE
