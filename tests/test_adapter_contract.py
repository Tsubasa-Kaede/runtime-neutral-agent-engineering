import os
import subprocess
import sys
import unittest
from dataclasses import FrozenInstanceError, asdict
from pathlib import Path
from unittest.mock import patch

# Follow the repo convention: expose dual-agent-development/scripts on sys.path.
SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import adapter_probe
from adapter_probe import AdapterProbe, discover_claude, discover_codex, to_discovery_status
from dual_agent import DiscoveryStatus

# Real-provider tests are opt-in only: default run must never touch real CLIs,
# real credentials, or the network. Skip unless explicitly enabled.
RUN_REAL_PROVIDER_TESTS = os.environ.get("RUN_REAL_PROVIDER_TESTS", "").lower() in {
    "1", "true", "yes",
}


class ProbeResultContractTests(unittest.TestCase):
    """AdapterProbe is a controlled, frozen shape with only stable fields."""

    def test_stable_fields_and_immutability(self):
        probe = AdapterProbe("claude", "UNAVAILABLE", None, None, "not found")
        self.assertEqual(asdict(probe), {
            "adapter_id": "claude",
            "status": "UNAVAILABLE",
            "executable": None,
            "version": None,
            "reason": "not found",
        })
        with self.assertRaises(FrozenInstanceError):
            probe.status = "AVAILABLE"  # type: ignore[misc]

    def test_to_dict_exposes_only_stable_fields(self):
        probe = AdapterProbe("claude", "AVAILABLE", "/usr/bin/claude", "1.0.0", None)
        self.assertEqual(
            frozenset(probe.to_dict()),
            frozenset({"adapter_id", "status", "executable", "version", "reason"}),
        )

    def test_to_discovery_status_maps_probe_status(self):
        ok = AdapterProbe("claude", "AVAILABLE", None, None, None)
        missing = AdapterProbe("codex", "UNAVAILABLE", None, None, "missing")
        self.assertEqual(to_discovery_status(ok), DiscoveryStatus.AVAILABLE)
        self.assertEqual(to_discovery_status(missing), DiscoveryStatus.UNAVAILABLE)


class MockProcess:
    """Stand-in for subprocess.Popen so tests never spawn real processes."""

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.env = kwargs.get("env")
        self.cwd = kwargs.get("cwd")
        self.stdout = b""
        self.stderr = b"1.0.0"
        self.returncode = 0
        self.pid = 12345
        self.timeout = None
        self._adapter_probe_fake = True

    def communicate(self, timeout=None):
        self.timeout = timeout
        return self.stdout, self.stderr

    def kill(self):
        self.killed = True


class DiscoverySemanticsTests(unittest.TestCase):
    """Discovery uses argv list, minimal env, bounded timeouts, never a shell."""

    def patch_which(self, found):
        return patch.object(adapter_probe.shutil, "which", return_value=found)

    def test_missing_executable_is_unavailable_not_exception(self):
        with self.patch_which(None), patch.object(adapter_probe.subprocess, "Popen") as popen:
            outcome = discover_codex()
        self.assertIsInstance(outcome, AdapterProbe)
        self.assertEqual(outcome.status, "UNAVAILABLE")
        self.assertIsNotNone(outcome.reason)
        popen.assert_not_called()

    def test_malformed_executable_is_unavailable(self):
        with self.patch_which("/does/not/exist"), patch.object(adapter_probe.subprocess, "Popen") as popen:
            outcome = discover_claude()
        self.assertEqual(outcome.status, "UNAVAILABLE")
        popen.assert_not_called()

    def test_probe_uses_argv_list_minimal_env_timeout_and_no_shell(self):
        fake_process = MockProcess()
        with self.patch_which(sys.executable):
            with patch.object(
                adapter_probe.subprocess, "Popen", return_value=fake_process
            ) as popen:
                outcome = discover_claude()

        popen.assert_called_once()
        args = popen.call_args
        positional = args.args[0]
        self.assertIsInstance(positional, list)
        self.assertTrue(all(isinstance(s, str) for s in positional))
        self.assertIn("--version", positional)
        # Never a shell string.
        self.assertFalse(args.kwargs.get("shell", False))
        # Environment is a minimal, explicitly constructed dict with the
        # documented well-defined variables (PATH/HOME/USERPROFILE).
        env = args.kwargs.get("env")
        self.assertIsInstance(env, dict)
        self.assertIn("PATH", env)
        if os.environ.get("HOME"):
            self.assertIn("HOME", env)
        if os.environ.get("USERPROFILE"):
            self.assertIn("USERPROFILE", env)
        # Bounded subprocess timeout was forwarded into communicate().
        self.assertEqual(fake_process.timeout, adapter_probe.DISCOVERY_TIMEOUT)
        # Probe reports a discovered executable and parsed version.
        self.assertEqual(outcome.status, "AVAILABLE")
        self.assertEqual(outcome.executable, sys.executable)
        self.assertIsNotNone(outcome.version)

    def test_probe_accepts_version_from_stdout(self):
        fake_process = MockProcess()
        fake_process.stdout = b"claude 2.1.0\n"
        fake_process.stderr = b""
        with self.patch_which(sys.executable), patch.object(
            adapter_probe.subprocess, "Popen", return_value=fake_process
        ):
            outcome = discover_claude()
        self.assertEqual(outcome.status, "AVAILABLE")
        self.assertEqual(outcome.version, "2.1.0")

    def test_non_version_output_is_unavailable(self):
        fake_process = MockProcess()
        fake_process.stdout = b"untrusted output without a version\n"
        fake_process.stderr = b""
        with self.patch_which(sys.executable), patch.object(
            adapter_probe.subprocess, "Popen", return_value=fake_process
        ):
            outcome = discover_claude()
        self.assertEqual(outcome.status, "UNAVAILABLE")
        self.assertIsNone(outcome.version)

    def test_codex_is_unavailable_even_if_a_candidate_looks_runnable(self):
        fake_process = MockProcess()
        fake_process.stdout = b"codex 1.0.0\n"
        with self.patch_which(sys.executable), patch.object(
            adapter_probe.subprocess, "Popen", return_value=fake_process
        ) as popen:
            outcome = discover_codex()
        self.assertEqual(outcome.status, "UNAVAILABLE")
        self.assertIsNotNone(outcome.reason)
        popen.assert_not_called()

    def test_codex_is_unavailable_by_default_never_faked_success(self):
        # In the default environment Codex's native dependency is broken, so the
        # adapter must report UNAVAILABLE (with a reason) rather than claim a
        # capability it cannot satisfy.
        with self.patch_which(sys.executable):
            with patch.object(
                adapter_probe.subprocess,
                "Popen",
                side_effect=OSError("codex native dependency missing"),
            ) as popen:
                outcome = discover_codex()
        self.assertEqual(outcome.status, "UNAVAILABLE")
        self.assertIsNotNone(outcome.reason)

    def test_timeout_yields_unavailable(self):
        class TimeoutProcess(MockProcess):
            def communicate(self, timeout=None):
                raise subprocess.TimeoutExpired(cmd="probe", timeout=timeout or 1)

        with self.patch_which(sys.executable):
            with patch.object(adapter_probe.subprocess, "Popen", return_value=TimeoutProcess()) as popen:
                outcome = discover_claude()
        self.assertEqual(outcome.status, "UNAVAILABLE")
        self.assertIn("timeout", (outcome.reason or "").lower())

    def test_version_probe_error_yields_unavailable(self):
        with self.patch_which(sys.executable):
            with patch.object(
                adapter_probe.subprocess, "Popen", side_effect=subprocess.SubprocessError()
            ):
                outcome = discover_claude()
        self.assertEqual(outcome.status, "UNAVAILABLE")

    def test_no_side_effects_when_discovery_is_a_noop(self):
        # Missing executable path must not spawn a process, write a file, or
        # touch the network. Patch subprocess to make any real call explode.
        with self.patch_which(None):
            with patch.object(adapter_probe.subprocess, "Popen", side_effect=AssertionError("subprocess called")):
                outcome = discover_claude()
        self.assertEqual(outcome.status, "UNAVAILABLE")
        self.assertEqual(outcome.executable, None)


@unittest.skipUnless(
    RUN_REAL_PROVIDER_TESTS,
    "Real Claude/Codex invocation is opt-in; skipped by default. "
    "Set RUN_REAL_PROVIDER_TESTS=1 to run against real CLIs.",
)
class RealProviderTests(unittest.TestCase):
    """Backstop guarding the opt-in real-provider surface.

    Never runs in CI or the default test run. When enabled it shells out to a
    real CLI, so it is strictly off by default."""

    def test_real_claude_discovery_is_opt_in(self):
        outcome = discover_claude()
        self.assertIn(outcome.status, {"AVAILABLE", "UNAVAILABLE"})


if __name__ == "__main__":
    unittest.main()
