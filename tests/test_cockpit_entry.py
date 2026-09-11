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
from types import SimpleNamespace

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import cockpit_entry
import host_entry
from candidate_validation import CandidateValidationStatus
from control_boundary import ControlCommand, ControlCommandType
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
    "candidate_validation", "content_safety", "control_journal",
    "execution_slots", "external_runtime", "host_entry",
    "sequential_pipeline", "usage_log",
}

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
        self.assertNotIn(".invoke(", self.source)

    def test_no_control_or_journal_writes(self):
        for token in (".submit(", "on_handoff", "journal.append"):
            self.assertNotIn(token, self.source)

    def test_no_identity_minting(self):
        for token in ("uuid4", "new_invocation_id"):
            self.assertNotIn(token, self.source)

    def test_pipeline_built_only_through_factory(self):
        self.assertIn("build_sequential_pipeline(", self.source)
        self.assertNotIn("SequentialPipeline(", self.source)

    def test_exactly_one_pipeline_execution_call(self):
        self.assertEqual(self.source.count(".run("), 1)

    def test_no_second_orchestration_loop(self):
        self.assertNotIn("while ", self.source)

    def test_no_second_attempt_or_alternate_path_machinery(self):
        for token in ("retry", "fallback", "sleep("):
            self.assertNotIn(token, self.source)

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


if __name__ == "__main__":
    unittest.main()
