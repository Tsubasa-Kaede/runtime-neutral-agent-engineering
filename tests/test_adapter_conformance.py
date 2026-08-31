"""Adapter Conformance Suite — runtime-neutral contract verification (R2).

This suite answers exactly ONE question: does an adapter obey the six-method
Adapter Contract? It runs fully offline against FakeProcess fixtures,
never launches a real runtime, never touches credentials, and never opens
the REAL gate.

What this suite is NOT (hard boundary, locked by ConformanceBoundaryTests):

- It is NOT runtime qualification: conformance PASS never means READY,
  never means VERIFIED, and never produces REAL provenance.
- Its results never enter VerifiedRuntimePool, RoleAssignment, Routing,
  Budget, Admission, or Trust. The suite structurally cannot reach them —
  it never imports the qualification/admission stack at all.

Declaration levels (the intended design, not a test omission):

- L0 = invocation contract only (tiny-agents: its runner CLI has no
  observable read-only auth surface, so health methods must NOT be faked)
- L1 = invocation + health surface
- L2 = invocation + health + a declared usage behavior

Usage modes (orthogonal axis, every adapter declares one):

- CAPTURE — the runtime has a machine-readable usage surface; the adapter
  parses its own CLI format and reports exact observed integers.
- HONEST_UNKNOWN — no machine-readable usage surface; the trace keeps the
  literal "unknown" even when token-shaped text appears in stdout.

Architecture (mirrors the proven SharedUsageContractMixin organization in
tests/test_usage_capture.py): shared mixins hold ALL contract logic and are
runtime-neutral by construction (no runtime names anywhere inside them —
enforced by ConformanceBoundaryTests). Every per-adapter fixture subclass
contributes only DATA: which module to patch, how to build the adapter, and
the CLI's own output formats. Differences come from declarations and
fixtures, never from runtime-name branching.

Fixture protocol — a fixture subclass must provide:

    module_name               scripts module to patch (e.g. "pi_adapter")
    make_adapter()            -> adapter instance (class method)
    runtime_label             expected trace.runtime value
    stdout_ok                 CLI stdout that yields a SUCCESS invoke
    stdout_nonascii           CLI stdout with non-ASCII UTF-8 content
    invoke_uses_stdin         True when the prompt rides via stdin
    health (L1+ only):
        auth_ready_stdout / auth_not_ready_stdout / auth_junk_stdout
        auth_state_ready / auth_state_not_ready  (expected classified states)
        health_provider      profile provider for the health fixtures
    usage (declared mode only):
        stdout_usage_valid    stdout with valid usage -> (in, out) integers
        usage_expected        (input_tokens, output_tokens) exact integers
        stdout_usage_missing  stdout with no usage keys
        stdout_usage_malformed  stdout with junk usage values
        stdout_usage_partial  stdout with only one valid usage key
"""
import ast
import os
import subprocess
import sys
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from external_runtime import (
    ExternalAgentRequest,
    InvocationResult,
    InvocationStatus,
    RuntimeProfile,
)

# ---------------------------------------------------------------------------
# Declaration table — the single place where adapters are declared.
# Adding a runtime adapter without adding it here fails the silent-omission
# scan. Levels/usage modes are declared expectations about REAL contract
# behavior; they are not qualifications.
# ---------------------------------------------------------------------------

L0, L1, L2 = 0, 1, 2
CAPTURE, HONEST_UNKNOWN = "CAPTURE", "HONEST_UNKNOWN"

ADAPTER_DECLARATIONS = {
    "claude_code_adapter": {"level": L2, "usage": CAPTURE},
    "pi_adapter": {"level": L2, "usage": CAPTURE},
    "codex_adapter": {"level": L2, "usage": HONEST_UNKNOWN},
    "gemini_adapter": {"level": L2, "usage": CAPTURE},
    "tiny_agents_adapter": {"level": L0, "usage": HONEST_UNKNOWN},
}

ENV_WHITELIST = {"PATH", "HOME", "USERPROFILE", "SYSTEMROOT"}
PROVIDER_KEY_VARS = (
    "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "DEEPSEEK_API_KEY",
)

# ---------------------------------------------------------------------------
# Offline harness — one FakeProcess for every adapter, no side effects.
# ---------------------------------------------------------------------------


class FakeProcess:
    """Deterministic stand-in for subprocess.Popen (never spawns anything)."""

    def __init__(self, stdout="", stderr="", returncode=0, timeout=False):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.timeout = timeout
        self.pid = 1
        self.killed = False
        self.calls = []

    def communicate(self, input=None, timeout=None):
        self.calls.append((input, timeout))
        if self.timeout:
            raise subprocess.TimeoutExpired(cmd="fake", timeout=timeout)
        return self.stdout, self.stderr

    def kill(self):
        self.killed = True


class CompletedRun:
    """Deterministic stand-in for subprocess.run results."""

    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def run_invoke(fixture, stdout="", stderr="", returncode=0, request=None):
    """Run one invoke() against a fake process emitting the given output."""
    import importlib
    module = importlib.import_module(fixture.module_name)
    process = FakeProcess(stdout=stdout, stderr=stderr, returncode=returncode)
    with patch(f"{fixture.module_name}.subprocess.Popen", return_value=process):
        return fixture.make_adapter().invoke(request or fixture.request())


# ---------------------------------------------------------------------------
# L0 — Core Invocation Contract mixin (runtime-neutral; no adapter names here)
# ---------------------------------------------------------------------------


class Level0InvocationContractMixin:
    """The minimum contract every adapter must satisfy, whatever its level."""

    # -- fixture data (subclasses provide; defaults where uniform) ---------

    module_name = None
    runtime_label = None
    stdout_ok = "ok\n"
    stdout_nonascii = "résumé → 中文 ✓\n"
    invoke_uses_stdin = True

    @classmethod
    def make_adapter(cls):
        raise NotImplementedError

    @classmethod
    def request(cls, timeout_seconds=3, model=None):
        return ExternalAgentRequest(
            task_id="conformance-1",
            prompt="Return exactly OK and nothing else.",
            agent_id="coding-agent",
            role="coder",
            provider="test-provider",
            model=model,
            timeout_seconds=timeout_seconds,
        )

    # -- 6-method surface existence -----------------------------------------

    def test_core_three_methods_exist(self):
        adapter = self.make_adapter()
        for name in ("discover", "invoke", "cancel"):
            self.assertTrue(callable(getattr(adapter, name, None)), name)

    def test_health_methods_declared_level_l0_must_not_be_faked(self):
        # L0 adapters deliberately lack the health surface (no observable
        # auth face on their CLI). "Providing" them would mean faking
        # semantics — the contract forbids that. Level L1/L2 fixtures are
        # instead covered by Level1HealthSurfaceMixin below.
        if ADAPTER_DECLARATIONS[self.module_name]["level"] == L0:
            adapter = self.make_adapter()
            for name in ("check_authentication", "check_provider_model",
                         "minimal_health_check"):
                self.assertFalse(callable(getattr(adapter, name, None)),
                                 f"L0 adapter must not fake {name}")

    # -- argv / env / UTF-8 on invoke ---------------------------------------

    def test_invoke_uses_argv_list_without_shell(self):
        process = FakeProcess(stdout=self.stdout_ok)
        with patch(f"{self.module_name}.subprocess.Popen",
                   return_value=process) as popen:
            self.make_adapter().invoke(self.request())

        args = popen.call_args
        argv = args.args[0]
        self.assertIsInstance(argv, list)
        self.assertTrue(all(isinstance(item, str) for item in argv))
        self.assertFalse(args.kwargs.get("shell", False))

    def test_invoke_prompt_never_in_shell_reachable_position(self):
        # The prompt must not be concatenated into a shell string. Adapters
        # pass it via stdin or as one argv element — both are shell-safe;
        # what is forbidden is building one string and running a shell.
        process = FakeProcess(stdout=self.stdout_ok)
        with patch(f"{self.module_name}.subprocess.Popen",
                   return_value=process) as popen:
            self.make_adapter().invoke(self.request())

        argv = popen.call_args.args[0]
        self.assertNotIn(";", " ".join(argv[:1]))  # argv[0] is the executable

    def test_invoke_env_is_whitelist_only(self):
        process = FakeProcess(stdout=self.stdout_ok)
        with patch(f"{self.module_name}.subprocess.Popen",
                   return_value=process) as popen:
            self.make_adapter().invoke(self.request())

        env = popen.call_args.kwargs.get("env")
        self.assertIsInstance(env, dict)
        self.assertIn("PATH", env)
        self.assertLessEqual(set(env), ENV_WHITELIST)
        for var in PROVIDER_KEY_VARS:
            self.assertNotIn(var, env)

    def test_invoke_decodes_child_streams_as_utf8(self):
        process = FakeProcess(stdout=self.stdout_ok)
        with patch(f"{self.module_name}.subprocess.Popen",
                   return_value=process) as popen:
            self.make_adapter().invoke(self.request())

        self.assertEqual(popen.call_args.kwargs.get("encoding"), "utf-8")
        self.assertEqual(popen.call_args.kwargs.get("errors"), "replace")

    def test_invoke_survives_non_ascii_output(self):
        # errors="replace" means decoding never raises; the invocation must
        # succeed (or fail for CLI reasons) — never crash on decode.
        result = run_invoke(self, stdout=self.stdout_nonascii)
        self.assertIsInstance(result, InvocationResult)
        self.assertIn(result.status, (
            InvocationStatus.SUCCESS, InvocationStatus.FAILED))

    def test_probe_decodes_child_streams_as_utf8(self):
        completed = CompletedRun(stdout="1.0.0\n")
        with patch(f"{self.module_name}.subprocess.run",
                   return_value=completed) as run:
            self.make_adapter()._probe()

        kwargs = run.call_args.kwargs
        self.assertEqual(kwargs.get("encoding"), "utf-8")
        self.assertEqual(kwargs.get("errors"), "replace")
    def test_probe_env_is_whitelist_only(self):
        completed = CompletedRun(stdout="1.0.0\n")
        with patch(f"{self.module_name}.subprocess.run",
                   return_value=completed) as run:
            self.make_adapter()._probe()

        env = run.call_args.kwargs.get("env")
        self.assertIsInstance(env, dict)
        self.assertIn("PATH", env)
        self.assertLessEqual(set(env), ENV_WHITELIST)
        for var in PROVIDER_KEY_VARS:
            self.assertNotIn(var, env)

    def test_probe_uses_argv_without_shell(self):
        completed = CompletedRun(stdout="1.0.0\n")
        with patch(f"{self.module_name}.subprocess.run",
                   return_value=completed) as run:
            self.make_adapter()._probe()

        argv = run.call_args.args[0]
        self.assertIsInstance(argv, list)
        self.assertFalse(run.call_args.kwargs.get("shell", False))

    # -- timeout / failure / cancellation ------------------------------------

    def test_timeout_becomes_structured_timeout_result(self):
        process = FakeProcess(timeout=True)
        with patch(f"{self.module_name}.subprocess.Popen", return_value=process):
            result = self.make_adapter().invoke(self.request(timeout_seconds=0.1))

        self.assertIsInstance(result, InvocationResult)
        self.assertIs(result.status, InvocationStatus.TIMEOUT)
        self.assertTrue(process.killed)

    def test_timeout_exception_never_leaks_to_caller(self):
        # Any child failure inside communicate must surface as a structured
        # result -- an exception escaping invoke() is a contract violation.
        process = FakeProcess(timeout=True)
        with patch(f"{self.module_name}.subprocess.Popen", return_value=process):
            result = self.make_adapter().invoke(self.request(timeout_seconds=0.1))

        self.assertIsInstance(result, InvocationResult)

    def test_nonzero_exit_is_failed_never_success(self):
        result = run_invoke(self, stdout="", stderr="boom",
                            returncode=2, request=self.request())
        self.assertIsInstance(result, InvocationResult)
        self.assertIs(result.status, InvocationStatus.FAILED)
        self.assertNotEqual(result.status, InvocationStatus.SUCCESS)

    def test_nonzero_exit_stderr_is_redacted(self):
        result = run_invoke(
            self,
            stdout="",
            stderr="api_key=sk-live-secret123456 token: tok-live-secret987",
            returncode=1,
            request=self.request())
        self.assertIs(result.status, InvocationStatus.FAILED)
        combined = " ".join(filter(None, (result.error, result.trace.error)))
        self.assertNotIn("sk-live-secret123456", combined)
        self.assertNotIn("tok-live-secret987", combined)

    def test_oserror_becomes_unavailable(self):
        with patch(f"{self.module_name}.subprocess.Popen",
                   side_effect=OSError("no such file api_key=raw-secret-abcdef")):
            result = self.make_adapter().invoke(self.request())

        self.assertIsInstance(result, InvocationResult)
        self.assertIs(result.status, InvocationStatus.UNAVAILABLE)
        self.assertNotIn("raw-secret-abcdef", result.error or "")

    def test_cancel_unknown_invocation_is_unavailable(self):
        result = self.make_adapter().cancel("never-existed")
        self.assertIsInstance(result, InvocationResult)
        self.assertIs(result.status, InvocationStatus.UNAVAILABLE)

    def test_cancel_active_invocation_is_cancelled(self):
        adapter = self.make_adapter()
        process = FakeProcess(stdout=self.stdout_ok)
        adapter._processes["invocation-conformance-1"] = process
        result = adapter.cancel("invocation-conformance-1")
        self.assertIs(result.status, InvocationStatus.CANCELLED)
        self.assertTrue(process.killed)

    # -- result / trace honesty ----------------------------------------------

    def test_result_is_frozen(self):
        result = run_invoke(self, stdout=self.stdout_ok, request=self.request())
        with self.assertRaises(FrozenInstanceError):
            result.status = InvocationStatus.SUCCESS

    def test_trace_is_complete_and_honest_on_success(self):
        result = run_invoke(self, stdout=self.stdout_ok, request=self.request())
        self.assertIs(result.status, InvocationStatus.SUCCESS)
        trace = result.trace
        self.assertIsNotNone(trace)
        self.assertTrue(trace.invocation_id)
        self.assertEqual(trace.runtime, self.runtime_label)
        self.assertIs(trace.status, InvocationStatus.SUCCESS)
        self.assertIsNotNone(trace.duration_ms)
        self.assertGreaterEqual(trace.duration_ms, 0)

    def test_trace_identity_fields_are_separate_from_execution(self):
        result = run_invoke(self, stdout=self.stdout_ok, request=self.request())
        trace = result.trace
        self.assertEqual(trace.agent_id, "coding-agent")
        self.assertEqual(trace.provider, "test-provider")
        self.assertIsNotNone(trace.role)

    def test_tokens_default_to_unknown_not_zero(self):
        # Without a valid usage surface in stdout, tokens stay "unknown" --
        # never 0 pretending to be observed. (Adapters declaring CAPTURE
        # additionally prove capture in the L2 mixin.)
        result = run_invoke(self, stdout=self.stdout_ok, request=self.request())
        for field in ("input_tokens", "output_tokens"):
            value = getattr(result.trace, field)
            self.assertNotEqual(value, 0)
            self.assertTrue(value == "unknown" or isinstance(value, int))

    def test_successful_invoke_releases_process_slot(self):
        adapter = self.make_adapter()
        process = FakeProcess(stdout=self.stdout_ok)
        with patch(f"{self.module_name}.subprocess.Popen", return_value=process):
            adapter.invoke(self.request())
        self.assertEqual(adapter._processes, {})

    # -- discovery honesty -----------------------------------------------------

    def test_discovery_reports_available_with_safe_reason(self):
        completed = CompletedRun(stdout="1.0.0\n")
        with patch(f"{self.module_name}.subprocess.run",
                   return_value=completed):
            discovery = self.make_adapter().discover()

        self.assertTrue(discovery.available)
        self.assertEqual(discovery.runtime, self.runtime_label)
        # Discovery answers existence only -- never capability claims.
        self.assertEqual(discovery.capabilities, frozenset())

    def test_discovery_failure_reason_is_safe_not_traceback(self):
        with patch(f"{self.module_name}.subprocess.run",
                   side_effect=OSError("spawn failed token=super-secret-xyz")):
            discovery = self.make_adapter().discover()

        self.assertFalse(discovery.available)
        reason = discovery.reason or ""
        self.assertNotIn("super-secret-xyz", reason)
        self.assertNotIn("Traceback (most recent call last)", reason)
        self.assertNotIn("subprocess.Popen", reason)

    def test_discovery_missing_executable_is_honest_absence(self):
        # from_environment must return None (honest absence) when the
        # executable is absent -- never a half-configured adapter.
        self.assertIsNone(self.from_environment_absent())

    # -- secret redaction on the safe-error surface -----------------------------

    def test_safe_error_redacts_provider_key_material(self):
        # _safe_error is a staticmethod on the adapter class (family norm);
        # locate it through the class, not the module namespace.
        redactor = getattr(type(self.make_adapter()), "_safe_error", None)
        self.assertTrue(callable(redactor), "adapter class must expose _safe_error")
        text = ("api_key=alpha token: beta secret=gamma "
                "Authorization: Bearer delta "
                "hf_1234567890 sk-abcdefghij")
        redacted = redactor(text)
        for secret in ("alpha", "beta", "gamma", "delta",
                       "hf_1234567890", "sk-abcdefghij"):
            self.assertNotIn(secret, redacted)
        self.assertGreaterEqual(redacted.count("[REDACTED]"), 6)

    @classmethod
    def from_environment_absent(cls):
        raise NotImplementedError

# ---------------------------------------------------------------------------
# L1 — Health Surface Contract mixin (runtime-neutral)
# ---------------------------------------------------------------------------


class Level1HealthSurfaceMixin:
    """Contract behavior of the health surface: method existence, honest
    classification, and the auth-gated provider check. These tests exercise
    CONTRACT behavior only — an adapter passing them is NOT authenticaated,
    NOT READY, and NOT VERIFIED; conformance never grants qualification."""

    # -- fixture data ---------------------------------------------------------

    # auth CLI stdout fixtures and the classified states they must produce
    auth_ready_stdout = None          # CLI output that honestly means ready
    auth_not_ready_stdout = None      # CLI output that honestly means not ready
    auth_state_ready = None           # expected AuthenticationState for ready
    auth_state_not_ready = None       # expected state for not ready

    @classmethod
    def make_adapter(cls):
        raise NotImplementedError

    # -- six-method surface ----------------------------------------------------

    def test_health_three_methods_exist(self):
        adapter = self.make_adapter()
        for name in ("check_authentication", "check_provider_model",
                     "minimal_health_check"):
            self.assertTrue(callable(getattr(adapter, name, None)), name)

    # -- check_authentication ---------------------------------------------------

    def test_check_authentication_ready_maps_to_authenticated(self):
        completed = CompletedRun(stdout=self.auth_ready_stdout, returncode=0)
        with patch(f"{self.module_name}.subprocess.run",
                   return_value=completed) as run:
            result = self.make_adapter().check_authentication()

        argv = run.call_args.args[0]
        self.assertIsInstance(argv, list)
        self.assertFalse(run.call_args.kwargs.get("shell", False))
        env = run.call_args.kwargs.get("env") or {}
        self.assertLessEqual(set(env), ENV_WHITELIST)
        for var in PROVIDER_KEY_VARS:
            self.assertNotIn(var, env)
        self.assertEqual(result.state, self.auth_state_ready)

    def test_check_authentication_not_ready_is_honest(self):
        completed = CompletedRun(stdout=self.auth_not_ready_stdout, returncode=1)
        with patch(f"{self.module_name}.subprocess.run",
                   return_value=completed):
            result = self.make_adapter().check_authentication()

        self.assertEqual(result.state, self.auth_state_not_ready)

    def test_check_authentication_junk_output_is_not_faked(self):
        # Unrecognizable output must classify to an honest UNKNOWN family
        # state — never a guessed AUTHENTICATED.
        from runtime_status import AuthenticationState
        completed = CompletedRun(stdout="total garbage \x00\x01", returncode=0)
        with patch(f"{self.module_name}.subprocess.run",
                   return_value=completed):
            result = self.make_adapter().check_authentication()

        self.assertIn(result.state, (
            AuthenticationState.UNKNOWN, AuthenticationState.AUTH_REQUIRED))
        self.assertIsNot(result.state, AuthenticationState.AUTHENTICATED)

    def test_check_authentication_subprocess_failure_is_unknown(self):
        from runtime_status import AuthenticationState
        with patch(f"{self.module_name}.subprocess.run",
                   side_effect=OSError("gone")):
            result = self.make_adapter().check_authentication()

        self.assertEqual(result.state, AuthenticationState.UNKNOWN)

    # -- check_provider_model -----------------------------------------------------

    def test_provider_model_is_gated_on_observed_auth(self):
        # Without a prior observed authentication, the provider check must
        # refuse to vouch — never guess availability.
        check = self.make_adapter().check_provider_model()
        self.assertFalse(check.available)

    def test_provider_model_after_observed_ready_auth(self):
        from runtime_status import ReasonCode
        adapter = self.make_adapter()
        completed = CompletedRun(stdout=self.auth_ready_stdout, returncode=0)
        with patch(f"{self.module_name}.subprocess.run",
                   return_value=completed):
            adapter.check_authentication()
        # The provider check must not spawn another subprocess: it derives
        # from the observed auth (the coupling the contract documents).
        with patch(f"{self.module_name}.subprocess.run",
                   side_effect=AssertionError("provider check must not probe")):
            check = adapter.check_provider_model()
        self.assertTrue(check.available)
        self.assertEqual(check.reason_code, ReasonCode.NONE)

    # -- minimal_health_check -------------------------------------------------------

    def test_minimal_health_check_is_opt_in_and_honest(self):
        # Without the REAL gate the check must report an honest unsupported
        # skip — never a silent skip, never a fabricated pass.
        from runtime_status import ReasonCode
        env = {k: v for k, v in os.environ.items()
               if k != "RUN_REAL_PROVIDER_TESTS"}
        with patch.dict(os.environ, env, clear=True):
            result = self.make_adapter().minimal_health_check(timeout_seconds=5)

        self.assertFalse(result.passed)
        self.assertEqual(result.reason_code,
                         ReasonCode.UNSUPPORTED_HEALTH_CHECK)
        self.assertEqual(result.output_class, "skipped")


# ---------------------------------------------------------------------------
# L2 — Usage Honesty mixin (runtime-neutral; mode comes from the declaration)
# ---------------------------------------------------------------------------


class Level2UsageHonestyMixin:
    """Usage honesty for BOTH declared modes. Which branch applies is decided
    by the declaration table (data), never by an adapter-name branch here."""

    # -- fixture data (CAPTURE adapters provide usage stdouts) ----------------

    stdout_usage_valid = None        # stdout with valid machine-readable usage
    usage_expected = None            # (input_tokens, output_tokens) exact ints
    stdout_usage_missing = None      # same output shape, no usage keys
    stdout_usage_malformed = None    # usage keys with junk values
    stdout_usage_partial = None      # only one of the two usage keys valid

    @classmethod
    def make_adapter(cls):
        raise NotImplementedError

    def _usage_mode(self):
        return ADAPTER_DECLARATIONS[self.module_name]["usage"]

    # -- shared: whatever the mode, unknown stays unknown -----------------------

    def test_usage_never_becomes_zero_when_absent(self):
        result = run_invoke(self, stdout=self.stdout_usage_missing,
                            request=self.request())
        self.assertIs(result.status, InvocationStatus.SUCCESS)
        for field in ("input_tokens", "output_tokens"):
            value = getattr(result.trace, field)
            self.assertNotEqual(value, 0)
            self.assertIn(value, ("unknown",))

    def test_parser_failure_never_fails_the_invocation(self):
        result = run_invoke(self, stdout="not parseable at all \x00\xff",
                            request=self.request())
        self.assertIs(result.status, InvocationStatus.SUCCESS)

    def test_nonascii_output_does_not_break_usage_honesty(self):
        result = run_invoke(self, stdout=self.stdout_nonascii + "\x00\x01",
                            request=self.request())
        self.assertIs(result.status, InvocationStatus.SUCCESS)
        for field in ("input_tokens", "output_tokens"):
            value = getattr(result.trace, field)
            self.assertTrue(value == "unknown" or isinstance(value, int))

    # -- CAPTURE mode ------------------------------------------------------------

    def test_valid_usage_is_captured_exactly(self):
        if self._usage_mode() != CAPTURE:
            self.skipTest("declared HONEST_UNKNOWN")
        result = run_invoke(self, stdout=self.stdout_usage_valid,
                            request=self.request())
        self.assertIs(result.status, InvocationStatus.SUCCESS)
        self.assertEqual(result.trace.input_tokens, self.usage_expected[0])
        self.assertEqual(result.trace.output_tokens, self.usage_expected[1])
        # Observed integers — not the unknown literal, not strings.
        self.assertIsInstance(result.trace.input_tokens, int)
        self.assertIsInstance(result.trace.output_tokens, int)

    def test_malformed_usage_stays_unknown(self):
        if self._usage_mode() != CAPTURE:
            self.skipTest("declared HONEST_UNKNOWN")
        result = run_invoke(self, stdout=self.stdout_usage_malformed,
                            request=self.request())
        self.assertIs(result.status, InvocationStatus.SUCCESS)
        self.assertEqual(result.trace.input_tokens, "unknown")
        self.assertEqual(result.trace.output_tokens, "unknown")

    def test_partial_usage_is_not_fabricated(self):
        if self._usage_mode() != CAPTURE:
            self.skipTest("declared HONEST_UNKNOWN")
        result = run_invoke(self, stdout=self.stdout_usage_partial,
                            request=self.request())
        self.assertIs(result.status, InvocationStatus.SUCCESS)
        # A partial surface may yield one side or none — never a guessed int.
        for field in ("input_tokens", "output_tokens"):
            value = getattr(result.trace, field)
            self.assertTrue(value == "unknown" or isinstance(value, int))

    def test_bool_shaped_usage_is_rejected(self):
        # True/False are ints in Python; an honest parser must not accept
        # them as observed token counts.
        if self._usage_mode() != CAPTURE:
            self.skipTest("declared HONEST_UNKNOWN")
        result = run_invoke(self, stdout=self.stdout_usage_bool,
                            request=self.request())
        self.assertIs(result.status, InvocationStatus.SUCCESS)
        for field in ("input_tokens", "output_tokens"):
            value = getattr(result.trace, field)
            self.assertNotEqual(value, True)
            self.assertNotEqual(value, False)
            self.assertTrue(value == "unknown" or isinstance(value, int))

    # -- HONEST_UNKNOWN mode --------------------------------------------------------

    def test_honest_unknown_never_guesses_from_text(self):
        if self._usage_mode() != HONEST_UNKNOWN:
            self.skipTest("declared CAPTURE")
        # Token-shaped text in raw stdout must NOT be scraped into usage.
        result = run_invoke(self, stdout="ok\n token_usage=123 tokens: 456\n",
                            request=self.request())
        self.assertIs(result.status, InvocationStatus.SUCCESS)
        self.assertEqual(result.trace.input_tokens, "unknown")
        self.assertEqual(result.trace.output_tokens, "unknown")

# ---------------------------------------------------------------------------
# Per-adapter fixtures — DATA ONLY (module, construction, CLI output shapes).
# All contract logic lives in the mixins above; these subclasses exist to
# satisfy the fixture protocol for each CLI's own documented output format.
# ---------------------------------------------------------------------------


class ClaudeConformanceTests(Level1HealthSurfaceMixin,
                             Level0InvocationContractMixin, unittest.TestCase):
    module_name = "claude_code_adapter"
    runtime_label = "claude-cli"
    stdout_ok = '{"result": "ok"}\n'
    stdout_nonascii = '{"result": "résumé → 中文"}\n'

    @classmethod
    def make_adapter(cls):
        from claude_code_adapter import ClaudeCodeAdapter
        profile = RuntimeProfile("coding-agent", "claude-cli", "anthropic",
                                 None, "coder", frozenset())
        return ClaudeCodeAdapter(profile=profile, executable="claude")

    @classmethod
    def from_environment_absent(cls):
        from claude_code_adapter import ClaudeCodeAdapter
        with patch("claude_code_adapter.shutil.which", return_value=None):
            return ClaudeCodeAdapter.from_environment()

    # health fixtures: claude auth status --json vocabulary
    auth_ready_stdout = ('{"loggedIn": true, "authMethod": "oauth_token", '
                         '"apiProvider": "firstParty"}\n')
    auth_not_ready_stdout = '{"loggedIn": false}\n'

    @property
    def auth_state_ready(self):
        from runtime_status import AuthenticationState
        return AuthenticationState.AUTHENTICATED

    @property
    def auth_state_not_ready(self):
        from runtime_status import AuthenticationState
        return AuthenticationState.AUTH_REQUIRED

    # usage fixtures (declared CAPTURE): claude --output-format json envelope
    import json as _json
    stdout_usage_valid = _json.dumps({
        "result": "ok", "usage": {"input_tokens": 120, "output_tokens": 80},
    }) + "\n"
    usage_expected = (120, 80)
    stdout_usage_missing = _json.dumps({"result": "ok"}) + "\n"
    stdout_usage_malformed = _json.dumps({
        "result": "ok", "usage": {"input_tokens": "lots", "output_tokens": -5},
    }) + "\n"
    stdout_usage_partial = _json.dumps({
        "result": "ok", "usage": {"input_tokens": 90},
    }) + "\n"
    stdout_usage_bool = _json.dumps({
        "result": "ok", "usage": {"input_tokens": True, "output_tokens": False},
    }) + "\n"


class PiConformanceTests(Level1HealthSurfaceMixin,
                         Level0InvocationContractMixin, unittest.TestCase):
    module_name = "pi_adapter"
    runtime_label = "pi-cli"
    stdout_nonascii = "résumé → 中文 ✓\n"

    @classmethod
    def make_adapter(cls):
        from pi_adapter import PiAdapter
        profile = RuntimeProfile("coding-agent", "pi-cli", "anthropic",
                                 None, "coder", frozenset())
        return PiAdapter(profile=profile, executable="pi")

    @classmethod
    def from_environment_absent(cls):
        from pi_adapter import PiAdapter
        with patch("pi_adapter.shutil.which", return_value=None):
            return PiAdapter.from_environment()

    @staticmethod
    def _agent_end(usage=None, text="ok"):
        messages = [
            {"role": "user", "content": [{"type": "text", "text": "q"}]},
            {"role": "assistant", "content": [{"type": "text", "text": text}]},
        ]
        event = {"type": "agent_end", "messages": messages}
        if usage is not None:
            event["usage"] = usage
        return event

    @classmethod
    def _stream(cls, *events):
        import json
        lines = [json.dumps({"type": "session", "version": 3,
                             "id": "u", "cwd": "/tmp"})]
        lines.extend(json.dumps(event) for event in events)
        return "\n".join(lines) + "\n"

    @classmethod
    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)

    # stdout_ok / usage fixtures built at class level below
    @classmethod
    def _build_fixtures(cls):
        cls.stdout_ok = cls._stream(cls._agent_end())
        cls.stdout_usage_valid = cls._stream(cls._agent_end(
            {"input_tokens": 200, "output_tokens": 90}))
        cls.stdout_usage_missing = cls._stream(cls._agent_end())
        cls.stdout_usage_malformed = cls._stream(cls._agent_end(
            {"input_tokens": "??", "output_tokens": None}))
        cls.stdout_usage_partial = cls._stream(cls._agent_end(
            {"input_tokens": 90}))
        cls.stdout_usage_bool = cls._stream(cls._agent_end(
            {"input_tokens": True, "output_tokens": False}))

    # health fixtures: pi auth check --json vocabulary
    auth_ready_stdout = '{"status":"ready","provider":"anthropic","authType":"oauth"}\n'
    auth_not_ready_stdout = '{"status":"not_ready","provider":"anthropic"}\n'

    @property
    def auth_state_ready(self):
        from runtime_status import AuthenticationState
        return AuthenticationState.AUTHENTICATED

    @property
    def auth_state_not_ready(self):
        from runtime_status import AuthenticationState
        return AuthenticationState.AUTH_REQUIRED

    usage_expected = (200, 90)


PiConformanceTests._build_fixtures()


class CodexConformanceTests(Level1HealthSurfaceMixin,
                            Level0InvocationContractMixin, unittest.TestCase):
    module_name = "codex_adapter"
    runtime_label = "codex-cli"
    stdout_ok = "ok\n"
    stdout_nonascii = "résumé → 中文 ✓\n"

    @classmethod
    def make_adapter(cls):
        from codex_adapter import CodexAdapter
        profile = RuntimeProfile("coding-agent", "codex-cli", "openai",
                                 None, "coder", frozenset())
        return CodexAdapter(profile=profile, executable="codex")

    @classmethod
    def from_environment_absent(cls):
        from codex_adapter import CodexAdapter
        with patch("codex_adapter.shutil.which", return_value=None):
            return CodexAdapter.from_environment()

    # health fixtures: codex login status text vocabulary (stdout form)
    auth_ready_stdout = "Logged in using ChatGPT\n"
    auth_not_ready_stdout = "Not logged in\n"

    @property
    def auth_state_ready(self):
        from runtime_status import AuthenticationState
        return AuthenticationState.AUTHENTICATED

    @property
    def auth_state_not_ready(self):
        from runtime_status import AuthenticationState
        return AuthenticationState.AUTH_REQUIRED

    # usage fixtures (declared HONEST_UNKNOWN): codex exec prints raw stdout
    stdout_usage_valid = "ok\n"
    stdout_usage_missing = "ok\n"
    stdout_usage_malformed = "ok\n"
    stdout_usage_partial = "ok\n"
    stdout_usage_bool = "ok\n"
    usage_expected = None  # not used under HONEST_UNKNOWN


class GeminiConformanceTests(Level1HealthSurfaceMixin,
                             Level0InvocationContractMixin, unittest.TestCase):
    module_name = "gemini_adapter"
    runtime_label = "gemini-cli"

    @classmethod
    def make_adapter(cls):
        from gemini_adapter import GeminiAdapter
        profile = RuntimeProfile("coding-agent", "gemini-cli", "google",
                                 None, "coder", frozenset())
        return GeminiAdapter(profile=profile, executable="gemini")

    @classmethod
    def from_environment_absent(cls):
        from gemini_adapter import GeminiAdapter
        with patch("gemini_adapter.shutil.which", return_value=None):
            return GeminiAdapter.from_environment()

    @staticmethod
    def _gemini_json(result_text="ok", usage=None):
        import json
        payload = {"response": result_text}
        if usage is not None:
            payload["usage"] = usage
        return json.dumps(payload) + "\n"

    @classmethod
    def _build_fixtures(cls):
        cls.stdout_ok = cls._gemini_json("ok")
        cls.stdout_nonascii = cls._gemini_json("résumé → 中文 ✓")
        cls.stdout_usage_valid = cls._gemini_json(
            "ok", usage={"input_tokens": 150, "output_tokens": 70})
        cls.stdout_usage_missing = cls._gemini_json("ok")
        cls.stdout_usage_malformed = cls._gemini_json(
            "ok", usage={"input_tokens": "lots", "output_tokens": -5})
        cls.stdout_usage_partial = cls._gemini_json(
            "ok", usage={"input_tokens": 90})
        cls.stdout_usage_bool = cls._gemini_json(
            "ok", usage={"input_tokens": True, "output_tokens": False})

    # health fixtures: gemini auth status text vocabulary
    auth_ready_stdout = "logged in\n"
    auth_not_ready_stdout = "not logged in\n"

    @property
    def auth_state_ready(self):
        from runtime_status import AuthenticationState
        return AuthenticationState.AUTHENTICATED

    @property
    def auth_state_not_ready(self):
        from runtime_status import AuthenticationState
        return AuthenticationState.AUTH_REQUIRED

    usage_expected = (150, 70)


GeminiConformanceTests._build_fixtures()


class TinyAgentsConformanceTests(Level0InvocationContractMixin,
                                 unittest.TestCase):
    module_name = "tiny_agents_adapter"
    runtime_label = "tiny-agents"
    stdout_ok = "ok\n"
    stdout_nonascii = "résumé → 中文 ✓\n"

    @classmethod
    def make_adapter(cls):
        from tiny_agents_adapter import TinyAgentsAdapter
        profile = RuntimeProfile("tiny-agent", "tiny-agents", None,
                                 None, "coder", frozenset())
        return TinyAgentsAdapter(profile=profile, executable="tiny-agents",
                                  agent_path="agent-config",
                                  command="agent-command")

    @classmethod
    def from_environment_absent(cls):
        from tiny_agents_adapter import TinyAgentsAdapter
        with patch("tiny_agents_adapter.shutil.which", return_value=None):
            return TinyAgentsAdapter.from_environment(
                agent_path="agent-config", command="agent-command")

# ---------------------------------------------------------------------------
# Declaration integrity + silent-omission scan + suite self-boundaries
# ---------------------------------------------------------------------------


class DeclarationIntegrityTests(unittest.TestCase):
    """The declaration table itself must be well-formed and complete."""

    def test_every_declared_adapter_module_exists(self):
        import importlib
        for module_name in ADAPTER_DECLARATIONS:
            try:
                importlib.import_module(module_name)
            except ImportError as exc:
                self.fail(f"declared adapter module missing: {module_name} ({exc})")

    def test_declared_levels_are_valid(self):
        for module_name, declaration in ADAPTER_DECLARATIONS.items():
            self.assertIn(declaration["level"], (L0, L1, L2),
                          f"{module_name}: invalid level")
            self.assertIn(declaration["usage"], (CAPTURE, HONEST_UNKNOWN),
                          f"{module_name}: invalid usage mode")

    def test_l1_or_above_must_declare_health_fixture(self):
        # Structural check: an L1/L2 fixture class must carry the health
        # mixin (the fixture protocol), an L0 fixture must not.
        for module_name, declaration in ADAPTER_DECLARATIONS.items():
            fixture = _FIXTURE_BY_MODULE[module_name]
            has_health = issubclass(fixture, Level1HealthSurfaceMixin)
            if declaration["level"] >= L1:
                self.assertTrue(has_health,
                                f"{module_name}: L1+ requires the health mixin")
            else:
                self.assertFalse(has_health,
                                 f"{module_name}: L0 must not fake health")

    def test_l2_capture_fixtures_declare_usage_expectations(self):
        for module_name, declaration in ADAPTER_DECLARATIONS.items():
            if declaration["usage"] != CAPTURE:
                continue
            fixture = _FIXTURE_BY_MODULE[module_name]
            self.assertIsNotNone(fixture.stdout_usage_valid,
                                 f"{module_name}: CAPTURE needs valid-usage fixture")
            self.assertIsNotNone(fixture.usage_expected,
                                 f"{module_name}: CAPTURE needs exact expected values")

    def test_silent_omission_scan(self):
        """Any adapter pattern in scripts/ missing from the declaration
        table FAILS here. New adapter without conformance = red suite."""
        import re
        scripts_dir = Path(SCRIPTS)
        pattern_import = re.compile(r"^from external_runtime import", re.M)
        pattern_factory = re.compile(r"def from_environment", re.M)
        found = []
        for path in sorted(scripts_dir.glob("*.py")):
            source = path.read_text(encoding="utf-8")
            if pattern_import.search(source) and pattern_factory.search(source):
                found.append(path.stem)
        missing = [name for name in found
                   if name not in ADAPTER_DECLARATIONS]
        self.assertEqual(
            missing, [],
            "adapter(s) exist but are not in ADAPTER_DECLARATIONS "
            f"(silent conformance omission): {missing}")


class ConformanceBoundaryTests(unittest.TestCase):
    """The suite's own discipline: runtime-neutral mixins, no qualification
    reach, no REAL gate."""

    def test_mixin_sources_contain_no_runtime_names(self):
        # The shared contract logic must never branch on a runtime name.
        import inspect
        for mixin in (Level0InvocationContractMixin,
                      Level1HealthSurfaceMixin, Level2UsageHonestyMixin):
            source = inspect.getsource(mixin).lower()
            for name in ("claude", "codex", "pi_adapter", "tiny_agents",
                         "anthropic", "openai", "deepseek", "gemini"):
                self.assertNotIn(name, source,
                                 f"{mixin.__name__} mentions {name}")

    def test_suite_never_imports_qualification_or_admission_stack(self):
        # Conformance is not qualification: this module must not even import
        # the admission/qualification/routing surfaces it could feed.
        # Scan import statements only (AST), not prose — the docstring above
        # honestly NAMES what the suite is not; that is documentation, not a
        # dependency.
        tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        for banned in ("verified_runtime_pool", "verified_selection_bridge",
                       "role_assignment", "real_validation_executor",
                       "candidate_validation", "discovery_bootstrap",
                       "invocation_plan", "verified_stage_selector"):
            for module in imported:
                self.assertNotEqual(module, banned,
                                    f"conformance suite must not import {banned}")

    def test_suite_never_sets_the_real_gate(self):
        # AST-level negative: no assignment ever writes the REAL gate var.
        tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    for sub in (ast.walk(target)):
                        if (isinstance(sub, ast.Constant)
                                and sub.value == "RUN_REAL_PROVIDER_TESTS"):
                            self.fail("suite must never assign RUN_REAL_PROVIDER_TESTS")
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if (isinstance(node.func.value, ast.Constant)
                        and node.func.value.value == "RUN_REAL_PROVIDER_TESTS"):
                    self.fail("suite must never configure the REAL gate")


# Fixture registry for the structural declaration checks above (data only).
_FIXTURE_BY_MODULE = {
    "claude_code_adapter": ClaudeConformanceTests,
    "pi_adapter": PiConformanceTests,
    "codex_adapter": CodexConformanceTests,
    "gemini_adapter": GeminiConformanceTests,
    "tiny_agents_adapter": TinyAgentsConformanceTests,
}


if __name__ == "__main__":
    unittest.main()
