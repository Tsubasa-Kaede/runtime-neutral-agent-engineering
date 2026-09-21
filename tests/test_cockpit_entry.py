"""P0-A (V3.2): cockpit CLI composition root — offline contract tests.

Mandate matrix (P0-A authorization, 2026-09-11): argument parsing,
runtime resolution (VERIFIED-only admission, no implicit
qualification), composition (order / handoff / reuse), execution
outcome delivery for all four run statuses, stdout/stderr separation
per output mode, exit codes 0/2/3/4, content-safe projection, opaque
task ids, and architecture guards that keep cockpit_entry a pure
composition/delivery layer.

All execution doubles are hand-rolled offline spies: no network, no
REAL providers, no real credentials, and no filesystem access outside
TemporaryDirectory. Evidence stores are always injected (or pointed at
an empty TemporaryDirectory); the real default evidence directory is
never read by this suite.
"""
import io
import json
import re
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import cockpit_entry
import host_entry
from candidate_validation import CandidateValidationStatus
from control_boundary import (
    ControlCommand,
    ControlCommandType,
    ControlLifecycle,
    ControlStatus,
)
from external_runtime import (
    InvocationResult,
    InvocationStatus,
    InvocationTrace,
)
from sequential_pipeline import RunStatus


# ---------------------------------------------------------------- doubles


class _FakeProfile:
    """Minimal profile namespace for the offline adapters."""

    def __init__(self, runtime, provider, model=None):
        self.runtime = runtime
        self.provider = provider
        self.model = model
        self.agent_id = f"{runtime}-agent"


class _ScriptedAdapter:
    """Offline double: records requests and replays scripted behaviors.

    Each behavior is either an Exception instance (raised), an
    InvocationResult (returned as-is), or None (a default SUCCESS with
    a fresh trace). Behaviors are consumed in order; exhaustion falls
    back to the default SUCCESS.
    """

    def __init__(self, runtime, provider, behaviors=None):
        self.profile = _FakeProfile(runtime, provider)
        self.requests = []
        self._behaviors = list(behaviors or [])

    def invoke(self, request):
        self.requests.append(request)
        behavior = self._behaviors.pop(0) if self._behaviors else None
        if isinstance(behavior, BaseException):
            raise behavior
        if behavior is not None:
            return behavior
        count = len(self.requests)
        return InvocationResult(
            status=InvocationStatus.SUCCESS,
            output=f"output-{self.profile.runtime}-{count}",
            error=None,
            trace=InvocationTrace(
                invocation_id=f"inv-{self.profile.runtime}-{count}",
                task_id=request.task_id,
                agent_id=request.agent_id,
                runtime=self.profile.runtime,
                provider=self.profile.provider,
                model=None,
                role=request.role,
                status=InvocationStatus.SUCCESS,
            ),
        )


def _factories(*adapters):
    """from_environment-style factories returning fixed adapters."""
    return [lambda bound=adapter: bound for adapter in adapters]


def _standard_adapters():
    return _ScriptedAdapter("rt-a", "prov-a"), _ScriptedAdapter("rt-b", "prov-b")


def _verified(registry, *runtime_ids):
    """Evidence dict keyed by full identity for the given runtimes."""
    evidence = {}
    for runtime_id in runtime_ids:
        evidence[registry.get(runtime_id).identity] = SimpleNamespace(
            status=CandidateValidationStatus.VERIFIED
        )
    return evidence


def _run_cockpit(argv, *, adapters, verified=(), boundary_hook=None, **kwargs):
    """Invoke cockpit_main with captured streams and injected doubles."""
    factories = _factories(*adapters)
    registry, _skipped = host_entry.environment_registry(factories)
    evidence = _verified(registry, *verified)
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cockpit_entry.cockpit_main(
            argv,
            factories=factories,
            evidence=evidence,
            boundary_hook=boundary_hook,
            **kwargs,
        )
    return code, out.getvalue(), err.getvalue()


def _abort_hook(boundary, execution_id):
    boundary.submit(ControlCommand(
        command_id="test-abort",
        execution_id=execution_id,
        command=ControlCommandType.ABORT,
    ))


def _pause_hook(boundary, execution_id):
    boundary.submit(ControlCommand(
        command_id="test-pause",
        execution_id=execution_id,
        command=ControlCommandType.PAUSE,
    ))


_BASIC_COMMAND = ["task text", "--step", "arch=rt-a", "--step", "dev=rt-b"]


# ------------------------------------------------------------- 1. parsing


class ArgumentParsingTests(unittest.TestCase):
    """Closed-word validation of the cockpit argument surface."""

    def test_valid_full_command(self):
        parsed, error = cockpit_entry._parse_cockpit_arguments(
            ["task text", "--step", "arch=rt-a", "--step=dev=rt-b",
             "--json", "--timeout-seconds", "60"])
        self.assertIsNone(error)
        self.assertEqual(parsed.task, "task text")
        self.assertEqual(parsed.steps, (("arch", "rt-a"), ("dev", "rt-b")))
        self.assertTrue(parsed.json_mode)
        self.assertEqual(parsed.timeout_seconds, 60.0)

    def test_flag_position_is_free(self):
        parsed, error = cockpit_entry._parse_cockpit_arguments(
            ["--json", "task text", "--step", "arch=rt-a",
             "--timeout-seconds=30"])
        self.assertIsNone(error)
        self.assertEqual(parsed.task, "task text")
        self.assertEqual(parsed.timeout_seconds, 30.0)

    def test_missing_task_rejected(self):
        parsed, error = cockpit_entry._parse_cockpit_arguments(
            ["--step", "arch=rt-a"])
        self.assertIsNone(parsed)
        self.assertEqual(error[0], "INVALID_TASK")

    def test_empty_task_rejected(self):
        parsed, error = cockpit_entry._parse_cockpit_arguments(
            ["", "--step", "arch=rt-a"])
        self.assertIsNone(parsed)
        self.assertEqual(error[0], "INVALID_TASK")

    def test_whitespace_task_rejected(self):
        parsed, error = cockpit_entry._parse_cockpit_arguments(
            ["   ", "--step", "arch=rt-a"])
        self.assertIsNone(parsed)
        self.assertEqual(error[0], "INVALID_TASK")

    def test_missing_step_rejected(self):
        parsed, error = cockpit_entry._parse_cockpit_arguments(["task text"])
        self.assertIsNone(parsed)
        self.assertEqual(error[0], "MISSING_STEP")

    def test_malformed_step_without_equals_rejected(self):
        parsed, error = cockpit_entry._parse_cockpit_arguments(
            ["task text", "--step", "archrt-a"])
        self.assertIsNone(parsed)
        self.assertEqual(error[0], "INVALID_STEP_SPEC")

    def test_malformed_step_empty_role_rejected(self):
        parsed, error = cockpit_entry._parse_cockpit_arguments(
            ["task text", "--step", "=rt-a"])
        self.assertIsNone(parsed)
        self.assertEqual(error[0], "INVALID_STEP_SPEC")

    def test_malformed_step_empty_runtime_rejected(self):
        parsed, error = cockpit_entry._parse_cockpit_arguments(
            ["task text", "--step", "arch="])
        self.assertIsNone(parsed)
        self.assertEqual(error[0], "INVALID_STEP_SPEC")

    def test_malformed_step_double_equals_rejected(self):
        parsed, error = cockpit_entry._parse_cockpit_arguments(
            ["task text", "--step", "arch=rt=a"])
        self.assertIsNone(parsed)
        self.assertEqual(error[0], "INVALID_STEP_SPEC")

    def test_malformed_step_value_not_echoed_when_unsafe(self):
        parsed, error = cockpit_entry._parse_cockpit_arguments(
            ["task text", "--step", "arch=rt-a token=supersecret"])
        self.assertIsNone(parsed)
        self.assertEqual(error[0], "INVALID_STEP_SPEC")
        self.assertNotIn("supersecret", error[1])

    def test_repeated_step_order_preserved(self):
        parsed, error = cockpit_entry._parse_cockpit_arguments(
            ["task text", "--step", "dev=rt-a", "--step", "dev=rt-b",
             "--step", "rev=rt-a"])
        self.assertIsNone(error)
        self.assertEqual(parsed.steps,
                         (("dev", "rt-a"), ("dev", "rt-b"), ("rev", "rt-a")))

    def test_json_flag_defaults_false(self):
        parsed, _ = cockpit_entry._parse_cockpit_arguments(
            ["task text", "--step", "arch=rt-a"])
        self.assertFalse(parsed.json_mode)

    def test_timeout_accepts_positive_numbers(self):
        for text in ("60", "0.5", "300"):
            parsed, error = cockpit_entry._parse_cockpit_arguments(
                ["task text", "--step", "arch=rt-a",
                 "--timeout-seconds", text])
            self.assertIsNone(error, text)
            self.assertEqual(parsed.timeout_seconds, float(text))

    def test_timeout_rejects_non_positive_or_non_numeric(self):
        for text in ("abc", "0", "-3", "nan"):
            parsed, error = cockpit_entry._parse_cockpit_arguments(
                ["task text", "--step", "arch=rt-a",
                 "--timeout-seconds", text])
            self.assertIsNone(parsed, text)
            self.assertEqual(error[0], "INVALID_TIMEOUT", text)

    def test_missing_flag_value_rejected(self):
        parsed, error = cockpit_entry._parse_cockpit_arguments(
            ["task text", "--step"])
        self.assertIsNone(parsed)
        self.assertEqual(error[0], "MISSING_FLAG_VALUE")

    def test_unknown_flag_rejected(self):
        parsed, error = cockpit_entry._parse_cockpit_arguments(
            ["task text", "--step", "arch=rt-a", "--verbose"])
        self.assertIsNone(parsed)
        self.assertEqual(error[0], "UNSUPPORTED_ARGUMENT")

    def test_extra_positional_rejected(self):
        parsed, error = cockpit_entry._parse_cockpit_arguments(
            ["task text", "more text", "--step", "arch=rt-a"])
        self.assertIsNone(parsed)
        self.assertEqual(error[0], "UNSUPPORTED_ARGUMENT")

    def test_control_hook_is_not_reachable_from_argv(self):
        parsed, error = cockpit_entry._parse_cockpit_arguments(
            ["task text", "--step", "arch=rt-a", "--boundary-hook", "x"])
        self.assertIsNone(parsed)
        self.assertEqual(error[0], "UNSUPPORTED_ARGUMENT")


# ------------------------------------------------- 2. runtime resolution


class RuntimeResolutionTests(unittest.TestCase):
    """READY != VERIFIED at the product entry: registry + evidence gates."""

    def test_unknown_runtime_rejected_with_available_list(self):
        code, out, err = _run_cockpit(
            ["task text", "--step", "arch=rt-nope"],
            adapters=_standard_adapters(), verified=("rt-a", "rt-b"))
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("RUNTIME_NOT_FOUND", err)
        self.assertIn("rt-a", err)

    def test_unavailable_runtime_reports_providerless_family(self):
        skipped_adapter = _ScriptedAdapter("rt-s", None)
        code, out, err = _run_cockpit(
            ["task text", "--step", "arch=rt-s"],
            adapters=(_ScriptedAdapter("rt-a", "prov-a"), skipped_adapter),
            verified=("rt-a",))
        self.assertEqual(code, 2)
        self.assertIn("RUNTIME_UNAVAILABLE", err)

    def test_non_verified_runtime_rejected_with_qualify_hint(self):
        code, out, err = _run_cockpit(
            ["task text", "--step", "arch=rt-a", "--step", "dev=rt-b"],
            adapters=_standard_adapters(), verified=("rt-a",))
        self.assertEqual(code, 2)
        self.assertIn("RUNTIME_NOT_QUALIFIED", err)
        self.assertIn("dual-agent qualify", err)

    def test_non_verified_status_object_rejected(self):
        adapter_a, adapter_b = _standard_adapters()
        factories = _factories(adapter_a, adapter_b)
        registry, _ = host_entry.environment_registry(factories)
        evidence = _verified(registry, "rt-a")
        identity_b = registry.get("rt-b").identity
        evidence[identity_b] = SimpleNamespace(
            status=CandidateValidationStatus.FAILED)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cockpit_entry.cockpit_main(
                ["task text", "--step", "arch=rt-a", "--step", "dev=rt-b"],
                factories=factories, evidence=evidence)
        self.assertEqual(code, 2)
        self.assertIn("RUNTIME_NOT_QUALIFIED", err.getvalue())

    def test_json_mode_user_error_prints_single_machine_line(self):
        code, out, err = _run_cockpit(
            ["task text", "--step", "arch=rt-nope", "--json"],
            adapters=_standard_adapters(), verified=("rt-a", "rt-b"))
        self.assertEqual(code, 2)
        lines = out.splitlines()
        self.assertEqual(len(lines), 1)
        payload = json.loads(lines[0])
        self.assertEqual(payload["command"], "cockpit")
        self.assertEqual(payload["status"], "FAILED")
        self.assertEqual(payload["error"]["reason"], "RUNTIME_NOT_FOUND")
        self.assertIn("RUNTIME_NOT_FOUND", err)

    def test_evidence_loaded_from_base_dir_when_not_injected(self):
        import tempfile
        adapter_a, _adapter_b = _standard_adapters()
        factories = _factories(adapter_a)
        with tempfile.TemporaryDirectory() as directory:
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = cockpit_entry.cockpit_main(
                    ["task text", "--step", "arch=rt-a"],
                    factories=factories, evidence=None, base_dir=directory)
        self.assertEqual(code, 2)
        self.assertIn("RUNTIME_NOT_QUALIFIED", err.getvalue())

    def test_verified_runtime_proceeds_to_execution(self):
        code, out, _err = _run_cockpit(
            _BASIC_COMMAND, adapters=_standard_adapters(),
            verified=("rt-a", "rt-b"))
        self.assertEqual(code, 0)
        self.assertIn("COMPLETED", out)


# ------------------------------------------------------- 3. composition


class CompositionTests(unittest.TestCase):
    """Step plan assembly: order, handoff embedding, slot/agent identity."""

    def test_one_step_pipeline_completes(self):
        adapter_a, _ = _standard_adapters()
        code, out, _err = _run_cockpit(
            ["task text", "--step", "arch=rt-a", "--json"],
            adapters=(adapter_a, _ScriptedAdapter("rt-b", "prov-b")),
            verified=("rt-a", "rt-b"))
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(len(payload["steps"]), 1)
        self.assertEqual(len(adapter_a.requests), 1)

    def test_multi_step_order_and_prompt_handoff(self):
        adapter_a, adapter_b = _standard_adapters()
        code, out, _err = _run_cockpit(
            ["task text", "--step", "arch=rt-a", "--step", "dev=rt-b",
             "--step", "rev=rt-a", "--json"],
            adapters=(adapter_a, adapter_b), verified=("rt-a", "rt-b"))
        self.assertEqual(code, 0)
        self.assertEqual(len(adapter_a.requests), 2)
        self.assertEqual(len(adapter_b.requests), 1)
        # role order follows CLI order on the right adapters
        self.assertEqual(
            [request.role for request in adapter_a.requests],
            ["arch", "rev"])
        self.assertEqual(adapter_b.requests[0].role, "dev")
        # prompt embedding: next prompt carries prior output as plain text
        self.assertIn("output-rt-a-1", adapter_b.requests[0].prompt)
        self.assertIn("output-rt-b-1", adapter_a.requests[1].prompt)
        # task text embedded in the first prompt
        self.assertIn("task text", adapter_a.requests[0].prompt)

    def test_first_step_prompt_has_no_previous_section(self):
        adapter_a, _ = _standard_adapters()
        _run_cockpit(
            _BASIC_COMMAND, adapters=(adapter_a, _ScriptedAdapter(
                "rt-b", "prov-b")), verified=("rt-a", "rt-b"))
        self.assertNotIn("PREVIOUS STEP OUTPUT", adapter_a.requests[0].prompt)

    def test_same_runtime_reused_across_roles(self):
        adapter_a, _ = _standard_adapters()
        code, out, _err = _run_cockpit(
            ["task text", "--step", "arch=rt-a", "--step", "dev=rt-a",
             "--json"],
            adapters=(adapter_a, _ScriptedAdapter("rt-b", "prov-b")),
            verified=("rt-a", "rt-b"))
        self.assertEqual(code, 0)
        self.assertEqual(len(adapter_a.requests), 2)
        payload = json.loads(out)
        self.assertEqual(
            [step["runtime_id"] for step in payload["steps"]],
            ["rt-a", "rt-a"])
        self.assertEqual(
            [step["slot_id"] for step in payload["steps"]],
            ["step-0-arch", "step-1-dev"])

    def test_same_role_reused_with_distinct_slots(self):
        adapter_a, adapter_b = _standard_adapters()
        code, out, _err = _run_cockpit(
            ["task text", "--step", "dev=rt-a", "--step", "dev=rt-b",
             "--json"],
            adapters=(adapter_a, adapter_b), verified=("rt-a", "rt-b"))
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(
            [step["slot_id"] for step in payload["steps"]],
            ["step-0-dev", "step-1-dev"])
        self.assertEqual(
            [request.agent_id for request in adapter_a.requests],
            ["cockpit-dev"])
        self.assertEqual(
            [request.agent_id for request in adapter_b.requests],
            ["cockpit-dev"])

    def test_agent_id_is_role_derived_not_runtime_derived(self):
        adapter_a, _ = _standard_adapters()
        _run_cockpit(
            ["task text", "--step", "arch=rt-a"],
            adapters=(adapter_a, _ScriptedAdapter("rt-b", "prov-b")),
            verified=("rt-a", "rt-b"))
        self.assertEqual(adapter_a.requests[0].agent_id, "cockpit-arch")

    def test_timeout_flows_to_requests(self):
        adapter_a, _ = _standard_adapters()
        _run_cockpit(
            ["task text", "--step", "arch=rt-a", "--timeout-seconds", "90"],
            adapters=(adapter_a, _ScriptedAdapter("rt-b", "prov-b")),
            verified=("rt-a", "rt-b"))
        self.assertEqual(adapter_a.requests[0].timeout_seconds, 90.0)

    def test_default_timeout_is_host_default(self):
        adapter_a, _ = _standard_adapters()
        _run_cockpit(
            ["task text", "--step", "arch=rt-a"],
            adapters=(adapter_a, _ScriptedAdapter("rt-b", "prov-b")),
            verified=("rt-a", "rt-b"))
        self.assertEqual(adapter_a.requests[0].timeout_seconds,
                         host_entry.DEFAULT_TIMEOUT_SECONDS)


# -------------------------------------------------------- 4. execution


class ExecutionOutcomeTests(unittest.TestCase):
    """All four run statuses reach delivery with their own exit code."""

    def test_completed(self):
        code, out, _err = _run_cockpit(
            _BASIC_COMMAND + ["--json"],
            adapters=_standard_adapters(), verified=("rt-a", "rt-b"))
        payload = json.loads(out)
        self.assertEqual(payload["status"], "COMPLETED")
        self.assertEqual(
            [step["status"] for step in payload["steps"]],
            ["SUCCESS", "SUCCESS"])
        self.assertTrue(all(step["invocation_id"]
                            for step in payload["steps"]))
        self.assertEqual(payload["final_result"]["status"], "SUCCESS")
        self.assertEqual(payload["final_result"]["output"], "output-rt-b-1")
        self.assertIsNone(payload["error"])

    def test_failed_step_stops_chain(self):
        adapter_a, adapter_b = _standard_adapters()
        adapter_b._behaviors = [InvocationResult(
            status=InvocationStatus.FAILED,
            output=None,
            error="boom",
            trace=None)]
        code, out, _err = _run_cockpit(
            _BASIC_COMMAND + ["--json"],
            adapters=(adapter_a, adapter_b), verified=("rt-a", "rt-b"))
        self.assertEqual(code, 2)
        payload = json.loads(out)
        self.assertEqual(payload["status"], "FAILED")
        self.assertEqual(len(payload["steps"]), 2)
        self.assertEqual(payload["steps"][1]["status"], "FAILED")
        self.assertEqual(payload["final_result"]["status"], "FAILED")
        self.assertIsNone(payload["error"])

    def test_failed_by_exception_reports_error_object(self):
        adapter_a, adapter_b = _standard_adapters()
        adapter_b._behaviors = [RuntimeError("kaboom")]
        code, out, _err = _run_cockpit(
            _BASIC_COMMAND + ["--json"],
            adapters=(adapter_a, adapter_b), verified=("rt-a", "rt-b"))
        self.assertEqual(code, 2)
        payload = json.loads(out)
        self.assertEqual(payload["status"], "FAILED")
        self.assertIsNone(payload["final_result"])
        self.assertEqual(payload["error"]["reason"], "RuntimeError")
        self.assertEqual(payload["error"]["detail"], "kaboom")

    def test_aborted_before_first_step(self):
        adapter_a, adapter_b = _standard_adapters()
        code, out, _err = _run_cockpit(
            _BASIC_COMMAND + ["--json"],
            adapters=(adapter_a, adapter_b), verified=("rt-a", "rt-b"),
            boundary_hook=_abort_hook)
        self.assertEqual(code, 3)
        payload = json.loads(out)
        self.assertEqual(payload["status"], "ABORTED")
        self.assertEqual(payload["steps"], [])
        self.assertEqual(adapter_a.requests, [])
        self.assertEqual(adapter_b.requests, [])

    def test_parked_before_first_step(self):
        adapter_a, adapter_b = _standard_adapters()
        code, out, _err = _run_cockpit(
            _BASIC_COMMAND + ["--json"],
            adapters=(adapter_a, adapter_b), verified=("rt-a", "rt-b"),
            boundary_hook=_pause_hook)
        self.assertEqual(code, 4)
        payload = json.loads(out)
        self.assertEqual(payload["status"], "PARKED")
        self.assertEqual(payload["steps"], [])
        self.assertFalse(payload["resumable"])
        self.assertNotIn("run_state", payload)
        self.assertEqual(adapter_a.requests, [])


# ---------------------------------------------------------- 5. delivery


class DeliveryTests(unittest.TestCase):
    """stdout/stderr separation, redaction, honest absence, opaque ids."""

    def test_json_stdout_exactly_one_line_with_clean_stderr(self):
        code, out, err = _run_cockpit(
            _BASIC_COMMAND + ["--json"],
            adapters=_standard_adapters(), verified=("rt-a", "rt-b"))
        self.assertEqual(code, 0)
        self.assertEqual(len(out.splitlines()), 1)
        json.loads(out)
        self.assertEqual(err, "")

    def test_json_schema_keys_are_the_frozen_set(self):
        _code, out, _err = _run_cockpit(
            _BASIC_COMMAND + ["--json"],
            adapters=_standard_adapters(), verified=("rt-a", "rt-b"))
        payload = json.loads(out)
        self.assertEqual(
            set(payload),
            {"command", "status", "task_id", "steps", "final_result",
             "error"})
        for step in payload["steps"]:
            self.assertEqual(
                set(step),
                {"step_index", "role", "runtime_id", "slot_id",
                 "invocation_id", "status"})

    def test_human_mode_renders_mandated_sections(self):
        code, out, err = _run_cockpit(
            _BASIC_COMMAND,
            adapters=_standard_adapters(), verified=("rt-a", "rt-b"))
        self.assertEqual(code, 0)
        for fragment in ("Task:", "Composition:", "[1] arch / rt-a",
                         "[2] dev / rt-b", "Status: COMPLETED",
                         "Result:", "output-rt-b-1"):
            self.assertIn(fragment, out)
        self.assertIn("->", out)

    def test_human_mode_is_not_a_json_line(self):
        _code, out, _err = _run_cockpit(
            _BASIC_COMMAND,
            adapters=_standard_adapters(), verified=("rt-a", "rt-b"))
        self.assertFalse(out.startswith("{"))

    def test_unsafe_output_redacted_in_json_mode(self):
        adapter_a, adapter_b = _standard_adapters()
        adapter_b._behaviors = [InvocationResult(
            status=InvocationStatus.SUCCESS,
            output="token=supersecret123",
            error=None,
            trace=InvocationTrace(
                invocation_id="inv-unsafe-1",
                task_id="t", agent_id="a", runtime="rt-b",
                provider="prov-b", model=None, role="dev",
                status=InvocationStatus.SUCCESS))]
        code, out, err = _run_cockpit(
            _BASIC_COMMAND + ["--json"],
            adapters=(adapter_a, adapter_b), verified=("rt-a", "rt-b"))
        self.assertEqual(code, 0)
        self.assertNotIn("supersecret", out)
        self.assertNotIn("supersecret", err)
        payload = json.loads(out)
        self.assertIsNone(payload["final_result"]["output"])
        self.assertTrue(payload["final_result"]["output_redacted"])

    def test_unsafe_output_redacted_in_human_mode(self):
        adapter_a, adapter_b = _standard_adapters()
        adapter_b._behaviors = [InvocationResult(
            status=InvocationStatus.SUCCESS,
            output="token=supersecret123",
            error=None,
            trace=None)]
        _code, out, _err = _run_cockpit(
            _BASIC_COMMAND,
            adapters=(adapter_a, adapter_b), verified=("rt-a", "rt-b"))
        self.assertNotIn("supersecret", out)
        self.assertIn("redacted", out)

    def test_unsafe_task_echo_redacted_in_human_mode(self):
        _code, out, _err = _run_cockpit(
            ["task token=verysecret42", "--step", "arch=rt-a"],
            adapters=_standard_adapters(), verified=("rt-a", "rt-b"))
        self.assertNotIn("verysecret", out)

    def test_missing_invocation_id_is_null(self):
        adapter_a, _ = _standard_adapters()
        adapter_a._behaviors = [InvocationResult(
            status=InvocationStatus.SUCCESS,
            output="plain",
            error=None,
            trace=None)]
        _code, out, _err = _run_cockpit(
            ["task text", "--step", "arch=rt-a", "--json"],
            adapters=(adapter_a, _ScriptedAdapter("rt-b", "prov-b")),
            verified=("rt-a", "rt-b"))
        payload = json.loads(out)
        self.assertIsNone(payload["steps"][0]["invocation_id"])

    def test_task_id_is_opaque_digest(self):
        _code, out, _err = _run_cockpit(
            ["refactor the parser module", "--step", "arch=rt-a", "--json"],
            adapters=_standard_adapters(), verified=("rt-a", "rt-b"))
        payload = json.loads(out)
        self.assertRegex(payload["task_id"], r"^task_[0-9a-f]{12}$")
        self.assertNotIn("refactor", payload["task_id"])

    def test_task_id_deterministic_across_runs(self):
        argv = ["same task text", "--step", "arch=rt-a", "--json"]
        ids = []
        for _ in range(2):
            _code, out, _err = _run_cockpit(
                argv, adapters=_standard_adapters(), verified=("rt-a", "rt-b"))
            ids.append(json.loads(out)["task_id"])
        self.assertEqual(ids[0], ids[1])

    def test_marker_words_in_task_do_not_reach_identifiers(self):
        adapter_a, adapter_b = _standard_adapters()
        _run_cockpit(
            ["do not mention token in identifiers", "--step",
             "arch=rt-a"],
            adapters=(adapter_a, adapter_b), verified=("rt-a", "rt-b"))
        request = adapter_a.requests[0]
        self.assertTrue(request.task_id.startswith("task_"))
        self.assertNotIn("token", request.task_id)
        self.assertIn("token", request.prompt)  # prose stays in the prompt


# -------------------------------------------------------- 6. exit codes


class ExitCodeTests(unittest.TestCase):
    """0 COMPLETED / 2 FAILED / 3 ABORTED / 4 PARKED (D-P0-1)."""

    def test_exit_zero_on_completed(self):
        code, _out, _err = _run_cockpit(
            _BASIC_COMMAND, adapters=_standard_adapters(),
            verified=("rt-a", "rt-b"))
        self.assertEqual(code, 0)

    def test_exit_two_on_failed(self):
        adapter_a, adapter_b = _standard_adapters()
        adapter_b._behaviors = [InvocationResult(
            status=InvocationStatus.FAILED, output=None, error="x",
            trace=None)]
        code, _out, _err = _run_cockpit(
            _BASIC_COMMAND, adapters=(adapter_a, adapter_b),
            verified=("rt-a", "rt-b"))
        self.assertEqual(code, 2)

    def test_exit_three_on_aborted(self):
        code, _out, _err = _run_cockpit(
            _BASIC_COMMAND, adapters=_standard_adapters(),
            verified=("rt-a", "rt-b"), boundary_hook=_abort_hook)
        self.assertEqual(code, 3)

    def test_exit_four_on_parked(self):
        code, _out, _err = _run_cockpit(
            _BASIC_COMMAND, adapters=_standard_adapters(),
            verified=("rt-a", "rt-b"), boundary_hook=_pause_hook)
        self.assertEqual(code, 4)

    def test_exit_two_on_user_error(self):
        code, _out, _err = _run_cockpit(
            ["", "--step", "arch=rt-a"],
            adapters=_standard_adapters(), verified=("rt-a", "rt-b"))
        self.assertEqual(code, 2)


# ----------------------------------------------- 7. architecture guards


_ALLOWED_IMPORT_ROOTS = {
    "__future__", "json", "sys", "typing",
    "candidate_validation", "cockpit_projection", "cockpit_route",
    "cockpit_session",
    "cockpit_tui", "composition_core", "content_safety",
    "control_boundary",
    "control_journal", "execution_observation", "execution_slots",
    "external_runtime", "host_entry", "sequential_pipeline",
    "usage_log",
}
# ORCH-5 Architecture A 精确放宽（2026-09-21 集成授权）：cockpit_route
# = stdlib-only 纯决策投影模块（零引擎/零 provider/零 IO 面，经其
# 92 测试 + 集成测试背书），entry 仅经 routed_default_composition
# 桥消费——默认路径专用，显式组合路径结构性不经。

_BANNED_RUNTIME_NAMES = (
    "claude", "codex", "pi-cli", "gemini", "qwen", "opencode",
    "cline", "tiny-agents",
)


class ArchitectureGuardTests(unittest.TestCase):
    """Source-level locks: composition/delivery layer and nothing more."""

    def setUp(self):
        with open(cockpit_entry.__file__, "r", encoding="utf-8") as handle:
            self.source = handle.read()

    def test_imports_are_stdlib_or_frozen_stack_or_host_layer(self):
        found = set()
        for match in re.finditer(
                r"^\s*(?:from|import)\s+([A-Za-z_][\w.]*)",
                self.source, re.MULTILINE):
            found.add(match.group(1).split(".")[0])
        self.assertTrue(found)
        self.assertTrue(
            found <= _ALLOWED_IMPORT_ROOTS,
            f"unexpected imports: {sorted(found - _ALLOWED_IMPORT_ROOTS)}")

    def test_no_packet_parsing_surface(self):
        for token in ("collaboration_packet", "packet_forensics",
                      "structured_packets", "serialize_packet",
                      "parse_packet"):
            self.assertNotIn(token, self.source)

    def test_no_runtime_specific_branching(self):
        for token in _BANNED_RUNTIME_NAMES:
            self.assertNotIn(token, self.source)

    def test_no_execution_state_truth_fields(self):
        for token in ("cursor", "current_agent", "current_stage",
                      "current_runtime"):
            self.assertNotIn(token, self.source)

    def test_never_invokes_slots_directly(self):
        # CU-TUI-1: exactly one .invoke( exists — the runtime-neutral
        # observation wrapper delegating to the raw adapter it wraps
        # (the adapter product sits in the slot's raw position, so the
        # wrapper is not a second execution path). Everything else
        # still goes through the single pipeline execution call.
        self.assertEqual(self.source.count(".invoke("), 1)

    def test_no_control_or_journal_writes(self):
        # CU-TUI-4 精确放宽：控制提交唯一出口 = 组合根 dispatcher
        # 恰一次 session 门面调用；journal 直写与其它提交面仍禁。
        for token in ("on_handoff", "journal.append"):
            self.assertNotIn(token, self.source)
        self.assertEqual(self.source.count(".submit("), 1)

    def test_no_identity_minting(self):
        for token in ("uuid4", "new_invocation_id"):
            self.assertNotIn(token, self.source)

    def test_pipeline_built_only_through_factory(self):
        # CU-TUI-2: pipeline 构建移入 session 组件；本层只构造
        # session 与 steps 工厂——直接构造器零出现、构建函数零引用
        self.assertIn("CockpitSession(", self.source)
        self.assertNotIn("SequentialPipeline(", self.source)
        self.assertNotIn("build_sequential_pipeline(", self.source)

    def test_exactly_one_pipeline_execution_call(self):
        # CU-TUI-2: 恰一次段执行调用（经 session）；裸 .run( 零出现
        self.assertEqual(self.source.count(".run_segment("), 1)
        self.assertNotIn(".run(", self.source)

    def test_no_second_orchestration_loop(self):
        self.assertNotIn("while ", self.source)

    def test_no_second_attempt_or_alternate_path_machinery(self):
        for token in ("retry", "fallback", "sleep("):
            self.assertNotIn(token, self.source)


class FunnelEntryGuardTests(unittest.TestCase):
    """CU-TUI-5 §二十 source guards: the preflight stays a pure
    intent classifier, and default-binding composition exists only in
    resolve_default_composition."""

    def setUp(self):
        with open(cockpit_entry.__file__, "r", encoding="utf-8") as handle:
            self.source = handle.read()

    def test_preflight_is_pure_classifier(self):
        # §二十-2：零自研数字 coercion、零 role/runtime/step 解析
        import inspect
        body = inspect.getsource(cockpit_entry._funnel_preflight)
        for token in ("int(", "float(", ".split(", "runtime_id"):
            self.assertNotIn(token, body)

    def test_preflight_step_exits_precede_task_token(self):
        # §二十-1：--step 专属早退存在于 task-token 识别之前
        import inspect
        body = inspect.getsource(cockpit_entry._funnel_preflight)
        self.assertIn('== "--step"', body)
        self.assertIn('startswith("--step=")', body)
        first_task = body.find("task_token = token")
        self.assertGreater(first_task, 0)
        self.assertLess(body.find('== "--step"'), first_task)
        self.assertLess(body.find('startswith("--step=")'), first_task)

    def test_binding_composition_single_source(self):
        # §二十-5/H-2：sorted+模板 zip 默认指派恰一处，且位于
        # resolve_default_composition 函数体内（TUI/projection 零出现）
        import inspect
        resolve = inspect.getsource(
            cockpit_entry.resolve_default_composition)
        self.assertEqual(self.source.count("zip("), 1)
        self.assertIn("zip(", resolve)
        self.assertEqual(self.source.count("DEFAULT_ROLE_TEMPLATES["), 1)
        self.assertIn("DEFAULT_ROLE_TEMPLATES[", resolve)

    def test_boundary_hook_is_injection_only(self):
        # The hook exists as an injected seam and is applied exactly
        # once, after assembly and before the single execution call.
        self.assertGreaterEqual(self.source.count("boundary_hook"), 2)


# ------------------------------------------------- 8. host_entry wiring


class HostEntryDispatchTests(unittest.TestCase):
    """Additive dispatch: cockpit reaches this entry before V2 run."""

    def test_product_help_documents_cockpit(self):
        self.assertIn("cockpit", host_entry._PRODUCT_HELP)

    def test_main_dispatches_cockpit_subcommand(self):
        adapter_a, adapter_b = _standard_adapters()
        factories = _factories(adapter_a, adapter_b)
        registry, _ = host_entry.environment_registry(factories)
        evidence = _verified(registry, "rt-a", "rt-b")
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = host_entry.main(
                ["cockpit", "task text", "--step", "arch=rt-a",
                 "--step", "dev=rt-b", "--json"],
                factories=factories, evidence=evidence)
        self.assertEqual(code, 0)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["command"], "cockpit")
        self.assertEqual(payload["status"], "COMPLETED")

    def test_main_cockpit_help_prints_product_help(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = host_entry.main(["cockpit", "--help"])
        self.assertEqual(code, 0)
        self.assertIn("cockpit", out.getvalue())

    def test_main_cockpit_rejects_unknown_runtime(self):
        adapter_a, adapter_b = _standard_adapters()
        factories = _factories(adapter_a, adapter_b)
        registry, _ = host_entry.environment_registry(factories)
        evidence = _verified(registry, "rt-a", "rt-b")
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = host_entry.main(
                ["cockpit", "task text", "--step", "arch=rt-nope"],
                factories=factories, evidence=evidence)
        self.assertEqual(code, 2)
        self.assertIn("RUNTIME_NOT_FOUND", err.getvalue())


# --------------------------------------------- 9. CU-TUI-3 TUI routing


def _fake_tui_module(available):
    """Offline double of cockpit_tui: records the hand-off, runs the
    injected driver exactly once, never touches a real framework."""
    module = ModuleType("cockpit_tui")
    module.calls = []
    module.textual_available = lambda: available
    from event_index import EventIndex
    module.new_event_store = EventIndex

    def run_ui(**kwargs):
        module.calls.append(kwargs)
        return kwargs["driver"]()

    module.run_cockpit_tui = run_ui
    return module


class TuiRoutingTests(unittest.TestCase):
    """Scenarios 39-45: three-way routing + the human-path byte
    contract (stdout/exit unchanged when the UI layer is unavailable;
    any diagnostic lands on stderr only)."""

    def test_human_with_ui_routes_to_tui(self):            # 39
        module = _fake_tui_module(True)
        with mock.patch.dict(sys.modules, {"cockpit_tui": module}), \
                mock.patch.object(cockpit_entry, "_terminal_present",
                                  return_value=True):
            code, out, err = _run_cockpit(
                _BASIC_COMMAND, adapters=_standard_adapters(),
                verified=("rt-a", "rt-b"))
        self.assertEqual(code, 0)
        self.assertEqual(len(module.calls), 1)
        handed = module.calls[0]
        self.assertEqual(handed["task"], "task text")
        self.assertEqual(handed["plan"][0], ("step-0-arch", "arch",
                                             "rt-a", "prov-a"))
        self.assertIn("driver", handed)
        # delivery after the UI closes is the unchanged human surface
        self.assertIn("Status: COMPLETED", out)
        self.assertEqual(err, "")  # UI present -> no diagnostic

    def test_human_without_ui_falls_back_with_stderr_hint(self):  # 40
        module = _fake_tui_module(False)
        with mock.patch.dict(sys.modules, {"cockpit_tui": module}), \
                mock.patch.object(cockpit_entry, "_terminal_present",
                                  return_value=True):
            code, out, err = _run_cockpit(
                _BASIC_COMMAND, adapters=_standard_adapters(),
                verified=("rt-a", "rt-b"))
        self.assertEqual(code, 0)
        self.assertEqual(module.calls, [])
        self.assertIn("Status: COMPLETED", out)
        self.assertIn("textual", err)

    def test_human_with_missing_ui_module_falls_back(self):  # 40b
        with mock.patch.dict(sys.modules, {"cockpit_tui": None}), \
                mock.patch.object(cockpit_entry, "_terminal_present",
                                  return_value=True):
            code, out, err = _run_cockpit(
                _BASIC_COMMAND, adapters=_standard_adapters(),
                verified=("rt-a", "rt-b"))
        self.assertEqual(code, 0)
        self.assertIn("Status: COMPLETED", out)
        self.assertIn("textual", err)

    def test_json_with_ui_stays_json(self):                # 41
        module = _fake_tui_module(True)
        with mock.patch.dict(sys.modules, {"cockpit_tui": module}), \
                mock.patch.object(cockpit_entry, "_terminal_present",
                                  return_value=True):
            code, out, err = _run_cockpit(
                _BASIC_COMMAND + ["--json"],
                adapters=_standard_adapters(), verified=("rt-a", "rt-b"))
        self.assertEqual(code, 0)
        self.assertEqual(module.calls, [])
        self.assertEqual(len(out.splitlines()), 1)
        self.assertEqual(err, "")

    def test_json_without_ui_is_byte_equal(self):          # 42
        module = _fake_tui_module(True)
        with mock.patch.dict(sys.modules, {"cockpit_tui": module}), \
                mock.patch.object(cockpit_entry, "_terminal_present",
                                  return_value=True):
            _code_a, out_a, _err_a = _run_cockpit(
                _BASIC_COMMAND + ["--json"],
                adapters=_standard_adapters(), verified=("rt-a", "rt-b"))
        with mock.patch.dict(sys.modules, {"cockpit_tui": None}):
            _code_b, out_b, _err_b = _run_cockpit(
                _BASIC_COMMAND + ["--json"],
                adapters=_standard_adapters(), verified=("rt-a", "rt-b"))
        self.assertEqual(out_a, out_b)
        self.assertEqual(json.loads(out_a)["status"], "COMPLETED")

    def test_fallback_stdout_and_exit_are_byte_identical(self):  # 43/44
        # baseline: piped/redirected stream (no terminal) -> pure
        # original human path, zero diagnostic
        code_a, out_a, err_a = _run_cockpit(
            _BASIC_COMMAND, adapters=_standard_adapters(),
            verified=("rt-a", "rt-b"))
        # UI layer unavailable on an interactive terminal -> same bytes
        module = _fake_tui_module(False)
        with mock.patch.dict(sys.modules, {"cockpit_tui": module}), \
                mock.patch.object(cockpit_entry, "_terminal_present",
                                  return_value=True):
            code_b, out_b, err_b = _run_cockpit(
                _BASIC_COMMAND, adapters=_standard_adapters(),
                verified=("rt-a", "rt-b"))
        self.assertEqual(code_a, code_b)
        self.assertEqual(out_a, out_b)
        self.assertEqual(err_a, "")
        self.assertNotEqual(err_b, "")

    def test_diagnostic_never_reaches_stdout(self):        # 45
        module = _fake_tui_module(False)
        with mock.patch.dict(sys.modules, {"cockpit_tui": module}), \
                mock.patch.object(cockpit_entry, "_terminal_present",
                                  return_value=True):
            _code, out, err = _run_cockpit(
                _BASIC_COMMAND, adapters=_standard_adapters(),
                verified=("rt-a", "rt-b"))
        self.assertIn("textual", err)
        self.assertNotIn("textual", out)
        self.assertNotIn("未安装", out)

    def test_piped_stream_without_terminal_never_routes_to_ui(self):
        # stdout redirected (the offline default) keeps the original
        # path even when the UI layer is fully installed
        module = _fake_tui_module(True)
        with mock.patch.dict(sys.modules, {"cockpit_tui": module}):
            code, out, err = _run_cockpit(
                _BASIC_COMMAND, adapters=_standard_adapters(),
                verified=("rt-a", "rt-b"))
        self.assertEqual(code, 0)
        self.assertEqual(module.calls, [])
        self.assertIn("Status: COMPLETED", out)
        self.assertEqual(err, "")

    def test_entry_never_imports_ui_framework_directly(self):  # G17
        with open(cockpit_entry.__file__, "r", encoding="utf-8") as h:
            source = h.read()
        self.assertNotIn("import textual", source)
        # the only UI touchpoint is the cockpit_tui module load helper
        self.assertIn("_cockpit_tui_module", source)


# ------------------------------------ 10. CU-TUI-4 control surface (C1/C2)


def _probing_tui_module(shared, script):
    """Offline double: 握住注入面，在 driver 前按脚本派发真实控制
    意图并采集读数（READ/DISPATCH 两径的行为见证）。"""
    module = ModuleType("cockpit_tui")
    module.calls = []
    module.textual_available = lambda: True
    from event_index import EventIndex
    module.new_event_store = EventIndex

    def run_ui(**kwargs):
        module.calls.append(kwargs)
        control = kwargs["control"]
        pending = kwargs["revision_pending"]
        boundary = shared.get("boundary")
        module.readings = {}
        for name, call in script:
            module.readings[name] = call(control, pending, boundary,
                                         kwargs)
        return kwargs["driver"]()

    module.run_cockpit_tui = run_ui
    return module


def _run_probed(script, *, adapters=None, boundary_capture=None):
    """Route through the probing UI with a terminal present."""
    adapters = adapters or _standard_adapters()
    shared = {}
    module = _probing_tui_module(shared, script)
    if boundary_capture is not None:
        def hook(boundary, execution_id):
            shared["boundary"] = boundary
            boundary_capture(boundary, execution_id)
    else:
        def hook(boundary, execution_id):
            shared["boundary"] = boundary
    with mock.patch.dict(sys.modules, {"cockpit_tui": module}), \
            mock.patch.object(cockpit_entry, "_terminal_present",
                              return_value=True):
        code, out, err = _run_cockpit(
            _BASIC_COMMAND, adapters=adapters,
            verified=("rt-a", "rt-b"), boundary_hook=hook)
    return code, out, err, module


class DispatcherSurfaceTests(unittest.TestCase):
    """CU-TUI-4 §五：dispatcher = 意图 → ControlCommand(ui-N) →
    session 门面；回执三态原样；REVISE 携 target/text/version。"""

    def test_pause_replay_resume_revise_round_trip(self):
        script = [
            ("pause", lambda c, p, b, kw: c("PAUSE")),
            ("pause_again", lambda c, p, b, kw: c("PAUSE")),
            ("resume", lambda c, p, b, kw: c("RESUME")),
            ("revise", lambda c, p, b, kw: c(
                "REVISE", text="adjust", target="NEXT_INVOCATION")),
        ]
        code, _out, _err, module = _run_probed(script)
        self.assertEqual(code, 0)
        readings = module.readings
        self.assertEqual(readings["pause"].status, ControlStatus.ACCEPTED)
        self.assertEqual(readings["pause_again"].status,
                         ControlStatus.NO_OP)
        self.assertEqual(readings["pause_again"].reason.value,
                         "ALREADY_REQUESTED")
        self.assertEqual(readings["resume"].status,
                         ControlStatus.ACCEPTED)
        self.assertEqual(readings["revise"].status,
                         ControlStatus.ACCEPTED)
        # 回执 command_id 单调唯一（ui-N）
        ids = [readings[name].command_id
               for name, _call in script]
        self.assertEqual(ids, ["ui-1", "ui-2", "ui-3", "ui-4"])
        self.assertEqual(len(set(ids)), 4)

    def test_pause_fact_journaling_and_replay_zero_facts(self):
        script = [
            ("pause", lambda c, p, b, kw: c("PAUSE")),
            ("facts", lambda c, p, b, kw: kw["facts"]()),
        ]
        _code, _out, _err, module = _run_probed(script)
        kinds = [entry.fact_type.value
                 for entry in module.readings["facts"]]
        self.assertEqual(kinds.count("PAUSE_REQUESTED"), 1)

    def test_abort_pair_accepted_then_noop(self):
        script = [
            ("abort", lambda c, p, b, kw: c("ABORT")),
            ("abort_again", lambda c, p, b, kw: c("ABORT")),
        ]
        code, _out, _err, module = _run_probed(script)
        self.assertEqual(code, 3)   # 真实 ABORTED 交付
        self.assertEqual(module.readings["abort"].status,
                         ControlStatus.ACCEPTED)
        self.assertEqual(module.readings["abort_again"].status,
                         ControlStatus.NO_OP)
        self.assertNotEqual(module.readings["abort"].command_id,
                            module.readings["abort_again"].command_id)

    def test_revise_submission_maps_to_prompt_field(self):
        # NEXT_INVOCATION 结构性只收 text、SUBMISSION 只收 prompt/
        # task——错误映射会被冻结值对象直接拒绝；ACCEPTED 即映射
        # 正确的结构性证明。
        script = [
            ("revise_next", lambda c, p, b, kw: c(
                "REVISE", text="adjust", target="NEXT_INVOCATION")),
            ("revise_submission", lambda c, p, b, kw: c(
                "REVISE", text="new prompt", target="SUBMISSION")),
        ]
        _code, _out, _err, module = _run_probed(script)
        self.assertEqual(module.readings["revise_next"].status,
                         ControlStatus.ACCEPTED)
        self.assertEqual(module.readings["revise_submission"].status,
                         ControlStatus.ACCEPTED)


class RevisionPendingProviderTests(unittest.TestCase):
    """CU-TUI-4 §三（C1 READ 边界）：provider = 队列长度只读读数。"""

    def test_pending_counts_queue_across_revise(self):
        script = [
            ("before", lambda c, p, b, kw: p()),
            ("revise", lambda c, p, b, kw: c(
                "REVISE", text="adjust", target="NEXT_INVOCATION")),
            ("after", lambda c, p, b, kw: p()),
        ]
        _code, _out, _err, module = _run_probed(script)
        self.assertEqual(module.readings["before"], 0)
        self.assertEqual(module.readings["after"], 1)

    def test_provider_repeated_reads_are_pure(self):
        def probe(c, p, b, kw):
            facts_before = kw["facts"]()
            readings = [p() for _ in range(50)]
            facts_after = kw["facts"]()
            return (len(set(map(str, readings))) == 1,
                    facts_before == facts_after)
        script = [("purity", probe)]
        _code, _out, _err, module = _run_probed(script)
        stable, facts_equal = module.readings["purity"]
        self.assertTrue(stable)
        self.assertTrue(facts_equal)

    def test_queue_entry_text_carries_revision_text(self):
        def probe(c, p, b, kw):
            c("REVISE", text="adjust", target="NEXT_INVOCATION")
            queue = b.snapshot(
                ControlLifecycle.RUNNING).revision_queue
            return queue[0].text if queue else None
        script = [("queue_text", probe)]
        _code, _out, _err, module = _run_probed(script)
        self.assertEqual(module.readings["queue_text"], "adjust")


if __name__ == "__main__":
    unittest.main()
