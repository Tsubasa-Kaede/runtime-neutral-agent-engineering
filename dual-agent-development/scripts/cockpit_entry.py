"""P0-A (V3.2): `dual-agent cockpit` — CLI composition root + delivery.

Approved product entry chain (P0 Review, D-P0-1..4):

    User
      -> CLI (host_entry dispatches argv[0] == "cockpit" here)
      -> composition (this module: parse -> resolve -> gate -> assemble)
      -> ExecutionSlot / build_execution_slots (frozen V3.2 stack)
      -> SequentialPipeline run (frozen V3.2 stack)
      -> RunOutcome
      -> delivery (human text, or exactly one machine JSON line) + exit

This module is a composition and delivery layer ONLY:
- the single run call on the built pipeline is the whole execution:
  no second orchestration loop, no execution-position state, no second
  attempts, no alternate-path selection, no persistence;
- journal / control / observation / invocation truth all live in the
  frozen stack: a fresh journal is constructed and handed to the frozen
  assembler, and this module never writes a control fact and never
  mints an invocation identity (the adapter trace is the only source);
- packets are never parsed here: the prior step's output is embedded
  into the next prompt as plain text (truncated), nothing more;
- zero runtime-name branches: runtime behavior stays inside adapters.

Qualification boundary (READY is not VERIFIED, D-P0-4): every
referenced runtime must be present in the environment registry AND
hold persisted VERIFIED evidence. This entry never qualifies
implicitly and never accepts a qualifier, mirroring the V2 run path.

Output contract:
- human mode (default, D-P0-3): rendering printed after the run
  finishes; task echo and result preview are content-gated and
  truncated; diagnostics go to stderr;
- --json: stdout carries EXACTLY one machine JSON line; diagnostics go
  to stderr; invocation_id comes from the adapter trace when present
  and is null when absent (never fabricated); task_id is the opaque
  digest of the task text (CU-R3a rule, reused from host_entry);
- exit codes (D-P0-1): 0 COMPLETED, 2 FAILED or user error, 3 ABORTED,
  4 PARKED — control outcomes are neither plain success nor plain
  failure and are never re-classified here.
"""
from __future__ import annotations

import json
import sys
from typing import NamedTuple

try:  # installed-package mode: dependencies are package siblings
    from .candidate_validation import CandidateValidationStatus
    from .content_safety import REDACTED_ERROR, contains_unsafe_content
    from .control_journal import ControlJournal
    from .execution_slots import ExecutionSlotSpec, build_execution_slots
    from .external_runtime import ExternalAgentRequest
    from .sequential_pipeline import (
        RunStatus,
        StepSpec,
        build_sequential_pipeline,
    )
    from .usage_log import UsageLog
except ImportError:  # source-tree flat-import mode (tests/examples)
    from candidate_validation import CandidateValidationStatus
    from content_safety import REDACTED_ERROR, contains_unsafe_content
    from control_journal import ControlJournal
    from execution_slots import ExecutionSlotSpec, build_execution_slots
    from external_runtime import ExternalAgentRequest
    from sequential_pipeline import (
        RunStatus,
        StepSpec,
        build_sequential_pipeline,
    )
    from usage_log import UsageLog

__all__ = ("cockpit_main",)

_HOST_ENTRY = None

# Rendering limits (display projections only — never data limits).
_EMBED_LIMIT = 4000          # prior output embedded into the next prompt
_TASK_DISPLAY_LIMIT = 200    # task echo in human mode
_RESULT_PREVIEW_LIMIT = 2000  # result preview in human mode
_JSON_OUTPUT_LIMIT = 8192    # output embedded into the JSON line
_DETAIL_LIMIT = 200          # error/echo detail strings

_STEP_FLAG = "--step"
_JSON_FLAG = "--json"
_TIMEOUT_FLAG = "--timeout-seconds"

_EXIT_CODE_BY_STATUS = {
    RunStatus.COMPLETED: 0,
    RunStatus.FAILED: 2,
    RunStatus.ABORTED: 3,
    RunStatus.PARKED: 4,
}

_PROMPT_TEMPLATE = (
    "You are the \"{role}\" step of a sequential collaboration "
    "pipeline. Complete your part of the task below. Output plain "
    "text only.\n\n=== TASK ===\n{task}")
_PROMPT_PREVIOUS_SECTION = "\n\n=== PREVIOUS STEP OUTPUT ===\n"


def _host_entry():
    """Late-bound access to the host composition layer.

    host_entry imports this module for dispatch, so a module-level
    back-import would be circular; by dispatch time host_entry is fully
    initialized. Reused surfaces: environment_registry, evidence
    loading, the opaque task-id projection, shared constants, timeout
    coercion and the qualify hint — one source of truth, zero
    duplication."""
    global _HOST_ENTRY
    if _HOST_ENTRY is None:
        try:
            from . import host_entry as module
        except ImportError:
            import host_entry as module
        _HOST_ENTRY = module
    return _HOST_ENTRY


# ------------------------------------------------------------- parsing


class _ParsedArguments(NamedTuple):
    task: str
    steps: tuple          # ((role, runtime_id), ...) in CLI order
    json_mode: bool
    timeout_seconds: float | None   # None = caller/default timeout


def _step_spec_error(step_spec: str) -> tuple:
    detail = ("invalid --step value"
              if contains_unsafe_content(step_spec)
              else f"invalid --step value: {step_spec}")
    return "INVALID_STEP_SPEC", detail


def _parse_cockpit_arguments(argv):
    """Parse the cockpit argument surface (qualify-precedent style).

    Returns (parsed | None, (reason, detail) | None). ROLE and
    RUNTIME_ID are single non-empty tokens (one '=', no whitespace);
    echo of an offending value is content-gated."""
    task = None
    step_values = []
    json_mode = False
    timeout_text = None
    pending_value = None  # flag awaiting its value token
    for argument in argv:
        if pending_value is not None:
            if pending_value == _STEP_FLAG:
                step_values.append(argument)
            else:
                timeout_text = argument
            pending_value = None
            continue
        if argument == _JSON_FLAG:
            json_mode = True
        elif argument == _STEP_FLAG:
            pending_value = _STEP_FLAG
        elif argument.startswith(_STEP_FLAG + "="):
            step_values.append(argument[len(_STEP_FLAG) + 1:])
        elif argument == _TIMEOUT_FLAG:
            pending_value = _TIMEOUT_FLAG
        elif argument.startswith(_TIMEOUT_FLAG + "="):
            timeout_text = argument[len(_TIMEOUT_FLAG) + 1:]
        elif argument.startswith("--"):
            return None, ("UNSUPPORTED_ARGUMENT",
                          f"unknown flag: {argument}")
        elif task is None:
            task = argument
        else:
            return None, ("UNSUPPORTED_ARGUMENT",
                          f"unexpected argument: {argument}")
    if pending_value is not None:
        return None, ("MISSING_FLAG_VALUE",
                      f"{pending_value} requires a value")
    if task is None:
        return None, ("INVALID_TASK", "task argument is missing")
    if not task.strip():
        return None, ("INVALID_TASK", "task must be a non-empty string")
    if not step_values:
        return None, ("MISSING_STEP",
                      "at least one --step ROLE=RUNTIME_ID is required")
    parsed_steps = []
    for step_spec in step_values:
        if step_spec.count("=") != 1:
            return None, _step_spec_error(step_spec)
        role, runtime_id = step_spec.split("=")
        role = role.strip()
        runtime_id = runtime_id.strip()
        if (not role or not runtime_id
                or " " in role or " " in runtime_id):
            return None, _step_spec_error(step_spec)
        parsed_steps.append((role, runtime_id))
    timeout_seconds = None
    if timeout_text is not None:
        timeout_seconds, problem = _host_entry()._coerce_qualify_timeout(
            timeout_text)
        if problem is not None:
            return None, ("INVALID_TIMEOUT",
                          f"not a positive number: {timeout_text}")
    return _ParsedArguments(task, tuple(parsed_steps), json_mode,
                            timeout_seconds), None


# ------------------------------------------------------------ delivery


def _fail(reason: str, detail: str, json_mode: bool, hint: bool = False) -> int:
    """User-error delivery: exit 2; stderr human line always; when the
    caller asked for --json, stdout still carries exactly one machine
    JSON line so scripts get a uniform surface."""
    if json_mode:
        print(json.dumps(_error_payload(reason, detail), sort_keys=True,
                         separators=(",", ":")))
    print(f"dual-agent cockpit: {reason}: {detail}", file=sys.stderr)
    if hint:
        print(_host_entry()._HINT_QUALIFY, file=sys.stderr)
    return 2


def _error_payload(reason: str, detail: str) -> dict:
    return {
        "command": "cockpit",
        "status": "FAILED",
        "task_id": None,
        "steps": [],
        "final_result": None,
        "error": {"reason": reason, "detail": detail[:_DETAIL_LIMIT]},
    }


def _status_text(value) -> str:
    """Status projection: the value's closed vocabulary word, verbatim."""
    return getattr(value, "value", str(value))


def _display_text(text: str, limit: int) -> str:
    """Content-gated, truncated display projection (never a leak face)."""
    if contains_unsafe_content(text):
        return "[redacted: unsafe content]"
    if len(text) > limit:
        return text[:limit] + " [...truncated]"
    return text


def _final_result_projection(final_result):
    if final_result is None:
        return None
    projection = {"status": _status_text(final_result.status)}
    output = final_result.output
    if isinstance(output, str):
        if contains_unsafe_content(output):
            projection["output"] = None
            projection["output_redacted"] = True
        elif len(output) > _JSON_OUTPUT_LIMIT:
            projection["output"] = output[:_JSON_OUTPUT_LIMIT]
            projection["output_truncated"] = True
        else:
            projection["output"] = output
    else:
        projection["output"] = None
    return projection


def _error_projection(error):
    if error is None:
        return None
    detail = str(error)
    if contains_unsafe_content(detail):
        detail = REDACTED_ERROR
    return {"reason": type(error).__name__,
            "detail": detail[:_DETAIL_LIMIT]}


def _step_projection(record, step_plan) -> dict:
    """One executed step as JSON: transcript fact + the caller's own
    composition mapping (display_role / display_runtime projection)."""
    role, runtime_id = step_plan[record.step_index]
    return {
        "step_index": record.step_index,
        "role": role,
        "runtime_id": runtime_id,
        "slot_id": record.slot_id,
        "invocation_id": record.invocation_id,
        "status": _status_text(record.status),
    }


def _outcome_payload(task_id: str, outcome, step_plan) -> dict:
    payload = {
        "command": "cockpit",
        "status": outcome.status.value,
        "task_id": task_id,
        "steps": [_step_projection(record, step_plan)
                  for record in outcome.transcript],
        "final_result": _final_result_projection(outcome.final_result),
        "error": _error_projection(outcome.error),
    }
    if outcome.status is RunStatus.PARKED:
        # Honest note: the continuation value exists on the outcome but
        # this CLI version persists nothing and offers no resume
        # surface, so a parked run cannot be continued from here.
        payload["resumable"] = False
    return payload


def _human_lines(task: str, step_plan, outcome) -> list:
    """Post-run rendering projection (no state is kept anywhere)."""
    lines = ["dual-agent cockpit",
             f"Task: {_display_text(task, _TASK_DISPLAY_LIMIT)}",
             "Composition:"]
    for index, (role, runtime_id) in enumerate(step_plan):
        lines.append(f"  [{index + 1}] {role} / {runtime_id}")
        if index + 1 < len(step_plan):
            lines.append("      ->")
    lines.append("Steps:")
    for record in outcome.transcript:
        role, runtime_id = step_plan[record.step_index]
        lines.append(f"  [{record.step_index + 1}] {role} / {runtime_id}"
                     f" -> {_status_text(record.status)}")
    lines.append(f"Status: {outcome.status.value}")
    if outcome.status is RunStatus.PARKED:
        lines.append("Note: parked execution; this CLI version has no "
                     "resume surface.")
    if outcome.final_result is not None:
        output = outcome.final_result.output
        lines.append("Result:")
        if isinstance(output, str) and output:
            lines.append(_display_text(output, _RESULT_PREVIEW_LIMIT))
        else:
            lines.append("(no text output)")
    error_projection = _error_projection(outcome.error)
    if error_projection is not None:
        lines.append(f"Error: {error_projection['reason']}: "
                     f"{error_projection['detail']}")
    return lines


# ------------------------------------------------------------- request


def _make_request_builder(task_text: str, task_id: str, role: str,
                          provider, timeout_seconds: float):
    """Build one StepSpec request_builder closure (pure function).

    The prior step's output is embedded as plain text (truncated)
    inside a fresh prompt; it is never parsed, never forwarded as the
    whole prompt, and never interpreted as a packet. agent_id is
    role-derived (never runtime-derived); model is left to the
    adapter's own default (ORCH-4 REAL-proven shape)."""
    def request_builder(previous_result):
        prompt = _PROMPT_TEMPLATE.format(role=role, task=task_text)
        if previous_result is not None:
            prior = getattr(previous_result, "output", None)
            if isinstance(prior, str) and prior:
                if len(prior) > _EMBED_LIMIT:
                    prior = prior[:_EMBED_LIMIT]
                prompt = prompt + _PROMPT_PREVIOUS_SECTION + prior
        return ExternalAgentRequest(
            task_id=task_id,
            prompt=prompt,
            agent_id=f"cockpit-{role}",
            role=role,
            provider=provider,
            model=None,
            timeout_seconds=timeout_seconds,
        )
    return request_builder


# ---------------------------------------------------------------- main


def cockpit_main(argv, *, factories=None, evidence=None, base_dir=None,
                 timeout_seconds=None, boundary_hook=None) -> int:
    """`dual-agent cockpit` entry (called by the host_entry dispatch).

    Composition + delivery only. Injection surface mirrors the
    host_entry precedent: ``factories`` (environment discovery
    doubles), ``evidence`` (persisted qualification facts; loaded from
    ``base_dir`` / the default directory when not injected), and
    ``timeout_seconds`` (default per-request timeout, overridable via
    --timeout-seconds). There is deliberately no qualifier parameter:
    this entry never qualifies implicitly (D-P0-4).

    ``boundary_hook`` is an embedding/test seam called with (boundary,
    execution_id) after assembly and before the single execution call;
    it is not reachable from argv and backs no product control surface
    (v1 has none). It exists so the ABORTED/PARKED delivery contracts
    stay testable offline."""
    argv = list(argv)
    parsed, error = _parse_cockpit_arguments(argv)
    json_mode = (parsed.json_mode if parsed is not None
                 else _JSON_FLAG in argv)
    if error is not None:
        reason, detail = error
        return _fail(reason, detail, json_mode)

    host = _host_entry()
    registry, skipped = host.environment_registry(factories)
    if evidence is None:
        directory = (host.DEFAULT_EVIDENCE_DIR if base_dir is None
                     else base_dir)
        try:
            evidence, rejected = host.load_evidence(directory)
        except OSError as failure:  # system IO: stderr-only, exit 2
            print(json.dumps({"error": "evidence store unreadable",
                              "detail": str(failure)}), file=sys.stderr)
            return 2
        host._print_rejections(rejected)

    resolved = {}  # runtime_id -> descriptor, in first-use order
    for _role, runtime_id in parsed.steps:
        if runtime_id in resolved:
            continue
        try:
            descriptor = registry.get(runtime_id)
        except KeyError:
            if runtime_id in skipped:
                return _fail("RUNTIME_UNAVAILABLE",
                             "runtime family present but unusable "
                             "(no provider identity)", parsed.json_mode)
            shown = (runtime_id
                     if not contains_unsafe_content(runtime_id)
                     else "(unsafe value suppressed)")
            available = ", ".join(sorted(
                entry.runtime_id for entry in registry.list())) or "(none)"
            return _fail("RUNTIME_NOT_FOUND",
                         f"unknown runtime_id {shown}; "
                         f"available: {available}", parsed.json_mode)
        validation = evidence.get(descriptor.identity)
        if (validation is None
                or validation.status
                is not CandidateValidationStatus.VERIFIED):
            return _fail("RUNTIME_NOT_QUALIFIED",
                         f"no persisted VERIFIED evidence for "
                         f"{descriptor.runtime_id}",
                         parsed.json_mode, hint=True)
        resolved[runtime_id] = descriptor

    task_id = host._opaque_task_id(parsed.task)
    execution_id = f"cockpit-{task_id}"
    effective_timeout = parsed.timeout_seconds
    if effective_timeout is None:
        effective_timeout = (
            timeout_seconds if timeout_seconds is not None
            else host.DEFAULT_TIMEOUT_SECONDS)

    journal = ControlJournal()
    usage_log = UsageLog()
    slot_specs = []
    step_specs = []
    for index, (role, runtime_id) in enumerate(parsed.steps):
        slot_id = f"step-{index}-{role}"
        slot_specs.append(ExecutionSlotSpec(
            slot_id=slot_id,
            raw_adapter=resolved[runtime_id].adapter_factory(),
            usage_log=usage_log,
            runtime_id=runtime_id,
            role=role))
        step_specs.append(StepSpec(
            slot_id=slot_id,
            request_builder=_make_request_builder(
                parsed.task, task_id, role,
                resolved[runtime_id].provider_id, effective_timeout)))
    slots = build_execution_slots(journal, tuple(slot_specs),
                                  execution_id=execution_id)
    pipeline = build_sequential_pipeline(slots, tuple(step_specs))
    if boundary_hook is not None:
        boundary_hook(slots.boundary, execution_id)
    outcome = pipeline.run()

    if parsed.json_mode:
        payload = _outcome_payload(task_id, outcome, parsed.steps)
        print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    else:
        for line in _human_lines(parsed.task, parsed.steps, outcome):
            print(line)
    return _EXIT_CODE_BY_STATUS[outcome.status]
