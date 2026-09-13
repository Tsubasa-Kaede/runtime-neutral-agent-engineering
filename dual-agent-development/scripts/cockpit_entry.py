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
- execution lifecycle is delegated to CockpitSession (CU-TUI-2): the
  session owns the RunState continuation value and the control
  submission gate, and this module makes exactly one segment execution
  call through it — no second orchestration loop here, no
  execution-position state of its own, no second attempts, no
  alternate-path selection, no persistence;
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

# 单一 module graph 纪律（P1-U4 先例，同款注释见 host_entry；2.4.2-A）：
# 平铺名在两种模式下都解析（shim 保证安装态可用）；若包相对导入优先，
# 安装态会把每个兄弟模块实例化第二份，enum/class 身份比较跨图必假
# （validation.status is VERIFIED 与 ExecutionSlots isinstance 均断裂）。
# 相对拼写仅作无 shim 嵌入场景的回退。
try:  # flat-import mode (source tree/tests/examples; also installed: the
      # dual_agent shim keeps flat names resolvable and the graph single)
    from candidate_validation import CandidateValidationStatus
    from cockpit_session import CockpitSession
    from content_safety import REDACTED_ERROR, contains_unsafe_content
    from control_boundary import RevisionPayload, RevisionTarget
    from control_journal import ControlJournal
    from execution_observation import (
        ExecutionEvent,
        ExecutionEventType,
        ObservationError,
    )
    from execution_slots import ExecutionSlotSpec, build_execution_slots
    from external_runtime import ExternalAgentRequest
    from sequential_pipeline import (
        RunStatus,
        StepSpec,
    )
    from usage_log import UsageLog
except ImportError:  # embedded package context without the flat shim
    from .candidate_validation import CandidateValidationStatus
    from .cockpit_session import CockpitSession
    from .content_safety import REDACTED_ERROR, contains_unsafe_content
    from .control_boundary import RevisionPayload, RevisionTarget
    from .control_journal import ControlJournal
    from .execution_observation import (
        ExecutionEvent,
        ExecutionEventType,
        ObservationError,
    )
    from .execution_slots import ExecutionSlotSpec, build_execution_slots
    from .external_runtime import ExternalAgentRequest
    from .sequential_pipeline import (
        RunStatus,
        StepSpec,
        build_sequential_pipeline,
    )
    from .usage_log import UsageLog

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
                          provider, timeout_seconds: float, *,
                          emit=None, runtime_id=None, previous_role=None):
    """Build one StepSpec request_builder closure (pure function).

    The prior step's output is embedded as plain text (truncated)
    inside a fresh prompt; it is never parsed, never forwarded as the
    whole prompt, and never interpreted as a packet. agent_id is
    role-derived (never runtime-derived); model is left to the
    adapter's own default (ORCH-4 REAL-proven shape).

    The optional emit/runtime_id/previous_role arguments are the
    CU-TUI-1 observation seam (keyword-only, absent on the default
    path). When a prior output is actually embedded into this step's
    prompt, exactly one HANDOFF event records the producing role
    (stage) and the receiving runtime (runtime_id) — composition
    facts about the prompt, with no transport or delivery meaning."""
    def request_builder(previous_result):
        prompt = _PROMPT_TEMPLATE.format(role=role, task=task_text)
        if previous_result is not None:
            prior = getattr(previous_result, "output", None)
            if isinstance(prior, str) and prior:
                if len(prior) > _EMBED_LIMIT:
                    prior = prior[:_EMBED_LIMIT]
                prompt = prompt + _PROMPT_PREVIOUS_SECTION + prior
                if emit is not None:
                    emit(ExecutionEventType.HANDOFF,
                         stage=previous_role,
                         runtime_id=runtime_id,
                         status="EMBEDDED", reason="EMBEDDED")
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


# ---------------------------------------------------------- observation

# Scope label for orchestration-level facts (the TERMINAL event when no
# invocation started, mirroring the production facade's label).
_ORCHESTRATION_SCOPE = "ORCHESTRATION"


def _observation_channel(task_id, execution_id, sink, index):
    """Execution-local observation channel for one cockpit run.

    Mirrors the production facade's proven isolation shape: the
    sequence is execution-scoped (starts at 0, strictly increasing,
    never shared across runs), event construction refusals surface as
    ObservationError and are dropped (a value-contract problem in
    observation never touches the execution), and each consumer call
    is individually isolated — an observation failure never changes an
    invocation result, the run outcome, or the delivery. The caller
    passes no consumer at all on the default path, so no channel and
    no event exist there."""
    counter = [0]
    last_runtime = [None]

    def emit(event_type, *, stage, runtime_id, status, reason,
             duration_ms=None):
        if runtime_id is not None:
            last_runtime[0] = runtime_id
        resolved_runtime = (runtime_id if runtime_id is not None else
                            (last_runtime[0] or _ORCHESTRATION_SCOPE))
        sequence = counter[0]
        counter[0] += 1
        try:
            event = ExecutionEvent(
                event_type=event_type,
                sequence=sequence,
                task_id=task_id,
                correlation_id=execution_id,
                stage=stage,
                runtime_id=resolved_runtime,
                status=status,
                reason=reason,
                duration_ms=duration_ms)
        except ObservationError:
            return
        if sink is not None:
            try:
                sink.on_event(event)
            except Exception:
                pass
        if index is not None:
            try:
                index.observe(event)
            except Exception:
                pass

    return emit


class _ObservingAdapter:
    """Runtime-neutral observation wrapper around one raw adapter.

    The wrapped product sits in the slot's raw position, so every
    invocation still executes through the single sequential pipeline
    path. The wrapper depends only on the duck-type execution contract
    — invoke(request) returning an InvocationResult — so any adapter,
    local or bridged through any seam, flows through unchanged.

    INVOCATION_STARTED precedes delegation. INVOCATION_FINISHED
    exists only when a real InvocationResult returned: its status is
    the result's own status value verbatim, and its duration is only a
    value the result's trace already holds. When the raw adapter
    raises there is no result to observe — no FINISHED event is
    fabricated and the exception propagates unchanged; the failure
    truth is observed later, at the real terminal boundary. stage and
    runtime_id are composition facts injected as data; this wrapper
    holds no runtime knowledge of its own."""

    def __init__(self, raw_adapter, emit, *, stage, runtime_id):
        self._raw = raw_adapter
        self._emit = emit
        self._stage = stage
        self._runtime_id = runtime_id

    def invoke(self, request):
        self._emit(ExecutionEventType.INVOCATION_STARTED,
                   stage=self._stage,
                   runtime_id=self._runtime_id,
                   status="STARTED", reason="STARTED")
        result = self._raw.invoke(request)
        status = getattr(result, "status", None)
        status_value = getattr(status, "value", None)
        if status_value is not None:
            trace = getattr(result, "trace", None)
            self._emit(ExecutionEventType.INVOCATION_FINISHED,
                       stage=self._stage,
                       runtime_id=self._runtime_id,
                       status=status_value, reason=status_value,
                       duration_ms=(None if trace is None
                                    else getattr(trace, "duration_ms",
                                                 None)))
        return result


# ---------------------------------------------------------------- main


def _terminal_present() -> bool:
    """交互终端在场（呈现层拥有终端的前提）。

    重定向/管道流恒走原人类路径——UI 依赖的在场与否绝不改变
    stdout/exit 字节契约（CU-TUI-3 授权 CASE C）。"""
    try:
        return bool(sys.stdout.isatty())
    except Exception:
        return False


def _cockpit_tui_module():
    """CU-TUI-3 装载面（G3/T1）：UI 框架 import 只存在于
    cockpit_tui 模块内部；本层仅尝试导入该模块并询问其可用性。
    ImportError 之外的异常照常传播（真实故障绝不误判为依赖
    缺席，G16）。"""
    try:
        import cockpit_tui
    except ImportError:
        try:
            from . import cockpit_tui  # embedded package context
        except ImportError:
            return None
    if not cockpit_tui.textual_available():
        return None
    return cockpit_tui


def cockpit_main(argv, *, factories=None, evidence=None, base_dir=None,
                 timeout_seconds=None, boundary_hook=None,
                 observation_sink=None, event_index=None) -> int:
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
    stay testable offline.

    ``observation_sink`` / ``event_index`` (CU-TUI-1) are in-process
    execution-event consumers for this composition root — the only
    observation surface. Both default to None and argv cannot reach
    either parameter: the default path emits no event and constructs
    no event store, and stdout/exit codes are byte-identical whether
    or not observation is injected. Consumer failures stay isolated from
    the execution path (see _observation_channel).

    CU-TUI-3 routing (G5): ``--json`` never touches the UI layer; a
    human run on an interactive terminal hands the same single segment
    execution to the read-only Textual cockpit (cockpit_tui) when that
    module and its framework are importable, and otherwise keeps this
    module's original human path byte-for-byte (any hint goes to
    stderr only; a redirected/piped stream never routes to the UI)."""
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
    tui = None
    if not parsed.json_mode and _terminal_present():
        tui = _cockpit_tui_module()
        if tui is None:
            # 交互终端在场而呈现层缺席：仅 stderr 诚实提示；
            # stdout/exit 契约不动，管道/机器面零噪声（G5/G16）
            print("dual-agent cockpit: textual 未安装，交互界面不可用，"
                  "按标准人类模式输出", file=sys.stderr)
    if tui is not None and event_index is None:
        # CU-TUI-3：呈现层在场而调用方未注入观察面时，向呈现层
        # 要它自己的只读事件索引（消费面组合先例 = CU-TUI-1
        # 注入口；构造下沉 UI 层，默认路径零事件面零漂移）
        event_index = tui.new_event_store()
    emit = (_observation_channel(task_id, execution_id,
                                 observation_sink, event_index)
            if (observation_sink is not None
                or event_index is not None)
            else None)
    slot_specs = []
    for index, (role, runtime_id) in enumerate(parsed.steps):
        slot_id = f"step-{index}-{role}"
        raw_adapter = resolved[runtime_id].adapter_factory()
        if emit is not None:
            raw_adapter = _ObservingAdapter(
                raw_adapter, emit, stage=role, runtime_id=runtime_id)
        slot_specs.append(ExecutionSlotSpec(
            slot_id=slot_id,
            raw_adapter=raw_adapter,
            usage_log=usage_log,
            runtime_id=runtime_id,
            role=role))
    slots = build_execution_slots(journal, tuple(slot_specs),
                                  execution_id=execution_id)

    def _steps(submission):
        """CU-TUI-2 submission 缝：submission 值 → 受影响 StepSpecs。

        纯闭包：捕获本组合的角色计划与观察参数，按 submission
        文本构造每步 builder。task 为权威任务文本；prompt-only
        修订按同一文本位生效（v1 任务面单文本）；全空时回落
        初始任务。SUBMISSION 修订由 session 在 fresh segment
        起点消费后经本工厂重建（旧 builders 不再被引用）。"""
        text = (submission.task if submission.task is not None
                else (submission.prompt if submission.prompt is not None
                      else parsed.task))
        return tuple(
            StepSpec(
                slot_id=f"step-{index}-{role}",
                request_builder=_make_request_builder(
                    text, task_id, role,
                    resolved[runtime_id].provider_id, effective_timeout,
                    emit=emit, runtime_id=runtime_id,
                    previous_role=(parsed.steps[index - 1][0]
                                   if index > 0 else None)))
            for index, (role, runtime_id) in enumerate(parsed.steps))

    session = CockpitSession(
        boundary=slots.boundary, slots=slots,
        submission=RevisionPayload(target=RevisionTarget.SUBMISSION,
                                   task=parsed.task),
        steps_factory=_steps)
    if boundary_hook is not None:
        boundary_hook(slots.boundary, execution_id)

    def _drive():
        """唯一段执行点（G4）：呈现层经此注入回调驱动，
        无呈现层时本层直接调用——两条路径零行为分叉。"""
        return session.run_segment()

    if tui is not None:
        # 呈现层 = 只读投影 + 注入驱动；执行/控制/观察真相
        # 仍在冻结栈（session / boundary / journal / stores）
        outcome = tui.run_cockpit_tui(
            driver=_drive,
            task=parsed.task,
            plan=tuple(
                (f"step-{index}-{role}", role, runtime_id,
                 resolved[runtime_id].provider_id)
                for index, (role, runtime_id)
                in enumerate(parsed.steps)),
            events=lambda: (event_index.snapshot(task_id)
                            if event_index is not None else ()),
            facts=journal.snapshot,
            usage=usage_log.snapshot,
            session=session)
    else:
        outcome = _drive()
    if emit is not None:
        emit(ExecutionEventType.TERMINAL,
             stage="SEQUENTIAL", runtime_id=None,
             status=outcome.status.value, reason=outcome.status.value)

    if parsed.json_mode:
        payload = _outcome_payload(task_id, outcome, parsed.steps)
        print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    else:
        for line in _human_lines(parsed.task, parsed.steps, outcome):
            print(line)
    return _EXIT_CODE_BY_STATUS[outcome.status]
