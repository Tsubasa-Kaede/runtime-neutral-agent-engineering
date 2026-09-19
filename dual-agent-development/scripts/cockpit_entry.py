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
    from cockpit_projection import DEFAULT_ROLE_TEMPLATES
    from cockpit_session import CockpitSession
    from content_safety import REDACTED_ERROR, contains_unsafe_content
    from control_boundary import (
        ControlCommand,
        ControlCommandType,
        ControlLifecycle,
        RevisionPayload,
        RevisionTarget,
    )
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
    from .cockpit_projection import DEFAULT_ROLE_TEMPLATES
    from .cockpit_session import CockpitSession
    from .content_safety import REDACTED_ERROR, contains_unsafe_content
    from .control_boundary import (
        ControlCommand,
        ControlCommandType,
        ControlLifecycle,
        RevisionPayload,
        RevisionTarget,
    )
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


# ------------------------------------------------- first-run funnel (TUI-5)

# CU-TUI-5（FIX-1/P1-2/P1-1，v2.1 勘误后实现）：零知识用户入口的
# entry 侧组合面。漏斗只经交互 TTY + 可导入 cockpit_tui 到达；一切
# 非 Funnel 形态由原 parser 按既有字节语义解释。本节不新增 engine
# event、不新增 Truth Source：runtime/qualification 真相仍在注册与
# evidence 路径（只读复用），identity 只作原值携带。


class FunnelIntent(NamedTuple):
    """预检意图：进入漏斗所需的最小 argv 事实。

    task_token 为唯一非 flag token 的原样携带（零内容验证——空白/
    超长/不安全内容交给漏斗 composer 语义与非交互原 parser 兜底）；
    timeout_seconds 为经既有 coercion 委托判定的合法 timeout。"""

    task_token: str | None
    timeout_seconds: float | None


def _funnel_preflight(argv):
    """最小意图分类（parser 之前，非第二套 CLI parser）。

    只识别进入漏斗所必需的最小语法：--json、任何 --step 形态（含
    malformed --step=，专属早退检查位于 task token 识别之前）、
    未知 flag、第二个 task token、悬空或非法 timeout 一律
    return None——交还原 _parse_cockpit_arguments 拥有完整 grammar
    与既有错误语义。本函数零 role/runtime 解析、零 step 校验、
    零自研数字 coercion（timeout 合法性只经 _coerce_qualify_timeout
    委托，非法即自我降级回原 parser）、零错误产出、零 task 内容
    验证。多次 timeout 与原 parser 同为 last-wins。"""
    task_token = None
    pending = None
    timeout_text = None
    for token in argv:
        if pending is not None:          # --timeout-seconds 的值槽
            timeout_text = token
            pending = None
            continue
        if token == "--json":
            return None
        if token == "--step":
            return None
        if token.startswith("--step="):
            return None
        if token == _TIMEOUT_FLAG:
            pending = token
        elif token.startswith(_TIMEOUT_FLAG + "="):
            timeout_text = token[len(_TIMEOUT_FLAG) + 1:]
        elif token.startswith("--"):
            return None                  # 未知 flag → 原 parser
        elif task_token is None:
            task_token = token           # 唯一 task token，原样携带
        else:
            return None                  # 第二个 task token → 原 parser
    if pending is not None:
        return None                      # 悬空值 → 原 parser
    timeout_seconds = None
    if timeout_text is not None:
        timeout_seconds, problem = (
            _host_entry()._coerce_qualify_timeout(timeout_text))
        if problem is not None:
            return None                  # 非法值 → 原 parser（字节同款）
    return FunnelIntent(task_token, timeout_seconds)


class CompositionBinding(NamedTuple):
    """默认组合的一个绑定（四字段全等，P1-1）。

    canonical_runtime_identity 是既有注册/evidence 身份的原值携带
    （descriptor.identity，注册路径构造的 evidence 四元组键）——本
    模块不计算、不派生、不重造 identity；呈现层（cockpit_tui /
    cockpit_projection）不接触此字段，它只是组合比较的透明载荷。
    runtime_id 与 provider_id 相同绝不掩盖 identity 变化。"""

    role: str
    runtime_id: str
    provider_id: str
    canonical_runtime_identity: tuple


class DefaultComposition(NamedTuple):
    """resolve_default_composition 的纯输出：绑定，或诚实 BLOCKED。"""

    roles: tuple
    bindings: tuple
    blocked_reason: str | None
    blocked_hint: str | None


class CompositionError(NamedTuple):
    """漏斗启动的诚实拒绝（与 _fail 同词表原因；呈现层红行消化，
    不 exit、零回退路径）。"""

    reason: str
    detail: str
    hint: str | None


class CompositionChanged(NamedTuple):
    """披露组合与活组合不一致（TUI/漏斗私有组合结果词，绝非
    engine event——零 observation/event_index/lifecycle 写入）。

    composition = 应当重新披露的活组合；reasons = 逐类诚实原因；
    调用方刷新披露、停留漏斗，用户再次 Enter 才重新判断。"""

    reasons: tuple
    composition: DefaultComposition


def resolve_default_composition(verified_pool_snapshot):
    """唯一默认组合源（FIX-3）：纯函数，零 IO/时钟/随机/UUID。

    canonical ordering = sorted(runtime_id)（输入顺序被规范化——
    注册顺序漂移不构成组合变化）；角色模板 = 投影层冻结表；池
    不足 2 诚实 BLOCKED 并附既有 qualify hint 原文——零 silent
    shrink、零替补 runtime、零 auto-reroute。identity 原值
    携带进 binding（见 CompositionBinding）。"""
    entries = tuple(sorted(verified_pool_snapshot,
                           key=lambda entry: entry.runtime_id))
    if len(entries) < 2:
        return DefaultComposition(
            roles=(), bindings=(),
            blocked_reason=(
                "default collaboration needs at least 2 VERIFIED "
                f"runtimes (found {len(entries)})"),
            blocked_hint=_host_entry()._HINT_QUALIFY)
    roles = DEFAULT_ROLE_TEMPLATES[min(4, len(entries))]
    bindings = tuple(
        CompositionBinding(
            role=role,
            runtime_id=entry.runtime_id,
            provider_id=entry.provider_id,
            canonical_runtime_identity=entry.identity)
        for role, entry in zip(roles, entries))
    return DefaultComposition(roles=tuple(roles), bindings=bindings,
                              blocked_reason=None, blocked_hint=None)


def _is_verified(evidence, descriptor):
    """既有验证门比较的唯一实现（显式解析循环与漏斗池同源）。"""
    validation = evidence.get(descriptor.identity)
    return (validation is not None
            and validation.status is CandidateValidationStatus.VERIFIED)


def _verified_pool(registry, evidence):
    """VERIFIED-only 池（READ）：registry.list() 自带 canonical
    sorted(runtime_id) 序，只读快照、零注册、零写入。"""
    return tuple(entry for entry in registry.list()
                 if _is_verified(evidence, entry))


def _composition_change_reasons(expected, live, registry):
    """披露组合 vs 活组合的逐类诚实差异（sorted 输出，确定性）。

    消失归因 no longer VERIFIED（registry 仍在场）或 unavailable
    （registry 缺席）；新增归因 new VERIFIED runtime changes the
    default plan；同 runtime_id 的绑定四字段不等（含 canonical
    identity 变化——三字段相等绝不掩盖）归因 identity changed。
    独立 ordering-diff 不可能：canonical 序由 identity 集合派生。"""
    reasons = []
    expected_by_runtime = {binding.runtime_id: binding
                           for binding in expected.bindings}
    live_by_runtime = {binding.runtime_id: binding
                       for binding in live.bindings}
    for runtime_id in sorted(set(expected_by_runtime)
                             - set(live_by_runtime)):
        try:
            registry.get(runtime_id)
            reasons.append(f"runtime {runtime_id} no longer VERIFIED")
        except KeyError:
            reasons.append(f"runtime {runtime_id} unavailable")
    for runtime_id in sorted(set(live_by_runtime)
                             - set(expected_by_runtime)):
        reasons.append(f"new VERIFIED runtime {runtime_id} "
                       f"changes the default plan")
    for runtime_id in sorted(set(expected_by_runtime)
                             & set(live_by_runtime)):
        if (expected_by_runtime[runtime_id]
                != live_by_runtime[runtime_id]):
            reasons.append(f"runtime {runtime_id} identity changed")
    return tuple(reasons)


def _resolve_runtimes(registry, skipped, evidence, steps):
    """显式步骤的运行时解析（legacy 解析循环的提取，语义逐字保持：
    detail 文本、首错即停、首用序 resolved、hint 仅 NOT_QUALIFIED）。

    返回 {runtime_id: descriptor}，或 (reason, detail, hint) 三元组
    ——legacy 调用方转 _fail，漏斗调用方转 CompositionError，同一
    解析真相、两种诚实呈现。"""
    resolved = {}
    for _role, runtime_id in steps:
        if runtime_id in resolved:
            continue
        try:
            descriptor = registry.get(runtime_id)
        except KeyError:
            if runtime_id in skipped:
                return ("RUNTIME_UNAVAILABLE",
                        "runtime family present but unusable "
                        "(no provider identity)", False)
            shown = (runtime_id
                     if not contains_unsafe_content(runtime_id)
                     else "(unsafe value suppressed)")
            available = ", ".join(sorted(
                entry.runtime_id for entry in registry.list())) or "(none)"
            return ("RUNTIME_NOT_FOUND",
                    f"unknown runtime_id {shown}; "
                    f"available: {available}", False)
        if not _is_verified(evidence, descriptor):
            return ("RUNTIME_NOT_QUALIFIED",
                    f"no persisted VERIFIED evidence for "
                    f"{descriptor.runtime_id}", True)
        resolved[runtime_id] = descriptor
    return resolved


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


class ComposedRun(NamedTuple):
    """装配产物：legacy --step 路径与漏斗 Start 的共同唯一装配真相
    （_assemble_execution 的输出）。drive 是唯一段执行点；emit/
    events/facts/usage 为只读投影面；dispatch_control/revision_
    pending 为 TUI-4 注入闭包原样。"""

    task: str
    steps: tuple                 # ((role, runtime_id), ...) 交付序
    plan: tuple                  # (slot_id, role, runtime, provider)
    task_id: str
    execution_id: str
    emit: object | None
    drive: object
    session: object
    dispatch_control: object
    revision_pending: object
    events: object
    facts: object
    usage: object
    # run-local 组合分组元数据（M3）：user composition 路径经
    # _replace 注入 live.groups；legacy/default 路径不传恒 ()。
    # 纯呈现载荷——装配层（ExecutionSlot/plan/RunState/pipeline）
    # 零感知（M0 Group 六不等式）。
    groups: tuple = ()


def _task_id_mint():
    """2.8-A 会话级 task_id 铸造器（entry 组装层私有）。

    _opaque_task_id 对同文本任务确定性相同——同会话内重跑同任务
    （轮间再提交/改写再跑）若复用同 task_id，EventIndex 按 task_id
    分组会把两次执行并入一组，且两执行各自 0 起的 execution-scoped
    sequence 会撞号。铸造器在会话作用域内保唯一：首次调用 = 原值
    （单轮路径与既有行为逐字节一致），冲突时计数式追加 -2/-3/…
    序标。execution_id 由 task_id 派生（既有 f-string），随之天然
    唯一。碰撞消解为有界 for（鸽笼保证 len(taken)+1 次内命中，
    绝不空转）——与 ArchitectureGuard 源级「零 while」钉定相容
    （entry 层永不出现第二编排循环）。"""
    counts = {}
    taken = set()

    def mint(task):
        base = _host_entry()._opaque_task_id(task)
        index = counts.get(base, 0) + 1
        candidate = base if index == 1 else f"{base}-{index}"
        # 防御：后缀形态与他任务真基名撞车时逐级让位（结构性极少
        # 发生；index 单调递增保证有界探测内必命中）
        for _ in range(len(taken) + 1):
            if candidate not in taken:
                break
            index += 1
            candidate = f"{base}-{index}"
        counts[base] = index
        taken.add(candidate)
        return candidate

    return mint


def _emit_run_terminal(composed, status_value):
    """单 run TERMINAL 观察发射（2.8-A 抽出的既有发射面）。

    与 2.7.0 post-App 发射逐字节同形（stage=SEQUENTIAL、
    runtime_id=None、status=reason=status 词）；零新词汇——仅复用
    既有 ExecutionEventType.TERMINAL 通道。status 缺席 = 零发射
    （绝不伪造终态词）。"""
    if composed.emit is not None and status_value is not None:
        composed.emit(ExecutionEventType.TERMINAL,
                      stage="SEQUENTIAL", runtime_id=None,
                      status=status_value, reason=status_value)


def _flush_prior_run_terminals(composed_runs, emitted):
    """2.8-A 轮次推进时的前轮 TERMINAL 补发（调用点恰在新 run
    append 之前——此时列表仅含已完成前轮）。

    发射时机裁决：run N 的 TERMINAL 在「run N+1 装配成功时」或
    「App 退出时」二者较早者补发——单轮路径永不经过本函数（无
    run 2），post-App 发射与 2.7.0 逐字节一致；多轮路径在下一轮
    启动前补齐上一轮观察（EventIndex 每 run 完整）。status 取该
    run session 的 last_outcome 投影（真实 RunOutcome，零合成）；
    emitted 集合防重（幂等）。"""
    for composed in composed_runs:
        if composed.execution_id in emitted:
            continue
        outcome = getattr(composed.session, "last_outcome", None)
        status = getattr(outcome, "status", None)
        value = getattr(status, "value", status)
        _emit_run_terminal(composed, value)
        if value is not None:
            emitted.add(composed.execution_id)


def _assemble_execution(resolved, task, steps, timeout_seconds, *,
                        observation_sink=None, event_index=None,
                        boundary_hook=None, task_id=None):
    """组合段装配（legacy 与漏斗的共同唯一装配真相）。

    输入为已 VERIFIED 解析的 runtimes + 任务文本 + 角色计划；产出
    冻结栈执行句柄（slots/session/控制闭包/唯一 drive）。本函数不
    读 argv、不产生 CLI 输出——错误呈现归调用方（_fail 或
    CompositionError）。"""
    host = _host_entry()
    task_id = (task_id if task_id is not None
               else host._opaque_task_id(task))
    execution_id = f"cockpit-{task_id}"
    journal = ControlJournal()
    usage_log = UsageLog()
    emit = (_observation_channel(task_id, execution_id,
                                 observation_sink, event_index)
            if (observation_sink is not None
                or event_index is not None)
            else None)
    slot_specs = []
    for index, (role, runtime_id) in enumerate(steps):
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
                      else task))
        return tuple(
            StepSpec(
                slot_id=f"step-{index}-{role}",
                request_builder=_make_request_builder(
                    text, task_id, role,
                    resolved[runtime_id].provider_id, timeout_seconds,
                    emit=emit, runtime_id=runtime_id,
                    previous_role=(steps[index - 1][0]
                                   if index > 0 else None)))
            for index, (role, runtime_id) in enumerate(steps))

    session = CockpitSession(
        boundary=slots.boundary, slots=slots,
        submission=RevisionPayload(target=RevisionTarget.SUBMISSION,
                                   task=task),
        steps_factory=_steps)
    if boundary_hook is not None:
        boundary_hook(slots.boundary, execution_id)

    command_counter = [0]

    def _dispatch_control(kind, text=None, target=None):
        """CU-TUI-4 DISPATCH 边界：呈现层意图 → 冻结命令值 →
        session 门面（G3 终态门 → boundary 裁决 → 同步回执）。

        command_id 在此单调铸造（ui-N；进程内唯一即满足 replay
        identity，零持久化承诺）。REVISE 附 expected_version（读取
        时点真值，竞态由 STALE_VERSION 如实裁决）；载荷映射沿冻结
        值对象的结构面：NEXT_INVOCATION 只收 text，SUBMISSION 只收
        prompt/task。PAUSE/RESUME/ABORT 零载荷。"""
        command_counter[0] += 1
        payload = None
        expected_version = None
        if kind == "REVISE":
            if target == "SUBMISSION":
                payload = RevisionPayload(
                    target=RevisionTarget.SUBMISSION, prompt=text)
            else:
                payload = RevisionPayload(
                    target=RevisionTarget.NEXT_INVOCATION, text=text)
            expected_version = slots.boundary.execution_version
        return session.submit(ControlCommand(
            command_id=f"ui-{command_counter[0]}",
            execution_id=execution_id,
            command=ControlCommandType(kind),
            payload=payload,
            expected_version=expected_version))

    def _revision_pending():
        """CU-TUI-4 READ 边界：pending 修订队列的只读长度。

        零缓存、零变更、零 enqueue、零命令构造（与 DISPATCH 严格
        分离）。lifecycle 实参沿 revision_adapter 的既有组合惯例
        （revision_adapter.py 同款调用形状）——队列读数与 lifecycle
        投影正交，绝不借读数伪造执行态。"""
        return len(slots.boundary.snapshot(
            ControlLifecycle.RUNNING).revision_queue)

    def _drive():
        """唯一段执行点（G4）：呈现层经此注入回调驱动，
        无呈现层时本层直接调用——两条路径零行为分叉。"""
        return session.run_segment()

    plan = tuple(
        (f"step-{index}-{role}", role, runtime_id,
         resolved[runtime_id].provider_id)
        for index, (role, runtime_id) in enumerate(steps))
    return ComposedRun(
        task=task, steps=tuple(steps), plan=plan,
        task_id=task_id, execution_id=execution_id, emit=emit,
        drive=_drive, session=session,
        dispatch_control=_dispatch_control,
        revision_pending=_revision_pending,
        events=lambda: (event_index.snapshot(task_id)
                        if event_index is not None else ()),
        facts=journal.snapshot, usage=usage_log.snapshot)


class _FunnelSurfaces(NamedTuple):
    """漏斗组合面（注入呈现层的两闭包 + 成功启动记录）。"""

    preview: object
    start: object
    composed_runs: list


def _funnel_composition_closures(registry, skipped, evidence, *,
                                 timeout_seconds, boundary_hook=None,
                                 observation_sink=None,
                                 event_index=None, task_id_mint=None,
                                 terminal_emitted=None):
    """CU-TUI-5 组合面（READ/DISPATCH 注入范式，C1 先例同构）。

    preview = 只读默认组合预览（零引擎对象、零副作用、零事件）；
    start = 唯一启动出口：活读 VERIFIED 池 → 与调用方当前披露的
    组合四字段全等比较 → 相等才经 _assemble_execution 装配；任何
    变化如实返回 CompositionChanged（零装配、零回退、零
    reroute、零二次尝试），调用方刷新披露后再次 Enter 才可启动。
    composed_runs 记录成功启动（2.8-A 起会话内可多次——每 run 一次）。
    2.8-A 会话注入件：task_id_mint（会话级唯一 task_id）与
    terminal_emitted（前轮 TERMINAL 补发防重集）由调用方共享供给
    两面；缺席时本面局部默认（单 run 语义与既有逐字节一致）。"""

    if terminal_emitted is None:
        terminal_emitted = set()

    def composition_preview():
        return resolve_default_composition(
            _verified_pool(registry, evidence))

    composed_runs = []

    def start_composition(task_text, expected_composition):
        if not isinstance(task_text, str) or not task_text.strip():
            return CompositionError("INVALID_TASK",
                                    "task must be a non-empty string",
                                    None)
        live = resolve_default_composition(
            _verified_pool(registry, evidence))
        if live.blocked_reason is not None:
            return CompositionError("RUNTIME_NOT_QUALIFIED",
                                    live.blocked_reason,
                                    live.blocked_hint)
        if live != expected_composition:
            return CompositionChanged(
                _composition_change_reasons(expected_composition, live,
                                            registry), live)
        steps = tuple((binding.role, binding.runtime_id)
                      for binding in live.bindings)
        resolved_or_error = _resolve_runtimes(registry, skipped, evidence,
                                              steps)
        if not isinstance(resolved_or_error, dict):
            reason, detail, hint = resolved_or_error
            return CompositionError(
                reason, detail,
                _host_entry()._HINT_QUALIFY if hint else None)
        composed = _assemble_execution(
            resolved_or_error, task_text, steps, timeout_seconds,
            observation_sink=observation_sink, event_index=event_index,
            boundary_hook=boundary_hook,
            task_id=(task_id_mint(task_text)
                     if task_id_mint is not None else None))
        _flush_prior_run_terminals(composed_runs, terminal_emitted)
        composed_runs.append(composed)
        return composed

    return _FunnelSurfaces(preview=composition_preview,
                           start=start_composition,
                           composed_runs=composed_runs)


class UserCompositionSurface(NamedTuple):
    """M3 用户组合面（与 _FunnelSurfaces 同注入范式）。

    start = start_user_composition 闭包；装配仍归 _assemble_
    execution——本面零第二执行路径。CU-COCKPIT-1 P1 增两个只读
    闭包：listing（Verified 池清单）/ preview（selection →
    (intent, resolve 结果) 纯装配+解析，零副作用）。"""

    start: object
    composed_runs: list
    listing: object
    preview: object


def _user_composition_surface(registry, skipped, evidence, *,
                              timeout_seconds, boundary_hook=None,
                              observation_sink=None, event_index=None,
                              task_id_mint=None, terminal_emitted=None):
    """M3 用户组合入口工厂（READ/DISPATCH 注入范式，
    _funnel_composition_closures 同型；2.8-A 会话注入件同彼——
    task_id_mint/terminal_emitted 由调用方跨面共享供给）。

    start = 唯一启动出口，七步与 default start_composition 同律、
    同一装配真相：INVALID_TASK → 活读池 → core resolve（结构+池门，
    truthful REJECT）→ 披露全等门（CompositionChanged：零装配零
    回退零 reroute）→ _resolve_runtimes 既有第二道 → _assemble_
    execution → groups 透传。组合差异仅在 intent 来源（用户点名 vs
    默认模板）；intent 全程零改写（无回退/替换/重绑）。"""

    if terminal_emitted is None:
        terminal_emitted = set()
    composed_runs = []

    def verified_listing():
        """CU-COCKPIT-1：Verified 池只读清单（UI 唯一选择数据源）。

        registry.list() 自带 canonical sorted(runtime_id) 序——
        确定性、零注册、零写入；UI 绝不直接触 registry/evidence。"""
        return _verified_pool(registry, evidence)

    def _selection_to_intent(selection):
        """P1 selection → CompositionIntent（纯装配，零 IO）。

        selection = ((runtime_id, role), ...) 声明序（P1：一勾选
        runtime = 一 participant）；member_id 按声明序稳定生成
        member-1..N；groups 恒空（P1 平面协作）。合法性门归
        validate_composition（UI 结构上只产闭集 role + 2-4 勾选，
        非法形状由 core 诚实拒）。"""
        from composition_core import (
            AgentSpec,
            CompositionIntent,
            RuntimeBindingRequest,
        )
        members = []
        requests = {}
        for index, (runtime_id, role) in enumerate(selection, start=1):
            member_id = f"member-{index}"
            members.append(AgentSpec(member_id, role))
            requests[member_id] = RuntimeBindingRequest(runtime_id)
        return CompositionIntent(
            members=tuple(members),
            binding_requests=requests,
            groups=())

    def _prefill_default_roles(selection):
        """role=None 成员的默认角色回填——复用唯一默认指派真源：
        选中成员的子集池过 resolve_default_composition（同 sorted
        序 + 同模板位次，语义 = "这 N 个 runtime 的默认指派"）。
        子集不足 2（blocked）或 runtime 不在池 = 保持 None（后续
        core validate 首错即停，诚实拒）。零第二套指派逻辑。"""
        if all(role is not None for _, role in selection):
            return selection
        wanted = {runtime_id for runtime_id, _ in selection}
        subset = tuple(entry for entry in _verified_pool(
            registry, evidence) if entry.runtime_id in wanted)
        defaults = resolve_default_composition(subset)
        if getattr(defaults, "blocked_reason", None) is not None:
            return selection
        by_runtime = {binding.runtime_id: binding.role
                      for binding in defaults.bindings}
        return tuple(
            (runtime_id,
             role if role is not None else by_runtime.get(runtime_id))
            for runtime_id, role in selection)

    def preview_selection(selection):
        """CU-COCKPIT-1：只读预览（零副作用零执行）。

        活读 Verified 池 → role=None 回填默认（唯一指派真源复用）
        → core resolve（结构门+池门）；成功 = ResolvedComposition
        （调用方经 expected_resolved 传入 start 作披露全等门基准）；
        失败 = CompositionError 原词（intent 恒 None）。与 start 各自
        活读池——两次读池间池可变正是 start 第 4 步全等门的存在
        意义（CompositionChanged，零静默重绑）。"""
        from composition_core import resolve_composition
        selection = _prefill_default_roles(selection)
        intent = _selection_to_intent(selection)
        result = resolve_composition(intent,
                                     _verified_pool(registry, evidence))
        if isinstance(result, CompositionError):
            return None, result
        return intent, result

    def start_user_composition(task_text, intent,
                               expected_resolved=None):
        # 惰性接线（_host_entry 同型先例）：composition_core 顶层
        # import 本模块（值对象复用），顶层反向 import 会成环；到
        # 运行时本模块必已初始化，此处直取缓存。
        from composition_core import resolve_composition

        # STEP 1 INVALID_TASK（与 default 逐字同律）
        if not isinstance(task_text, str) or not task_text.strip():
            return CompositionError("INVALID_TASK",
                                    "task must be a non-empty string",
                                    None)
        # STEP 2 一次活读；STEP 3 消费同一池快照
        pool = _verified_pool(registry, evidence)
        # STEP 3 core 池门（validate+resolve；失败立即返回——零
        # slots/session/journal/usage/drive/重试/回退）
        live = resolve_composition(intent, pool)
        if isinstance(live, CompositionError):
            return live
        # STEP 4 披露全等门：expected_resolved=None = 无披露跳过；
        # 比较恰 bindings 四字段值等（含 canonical_runtime_identity
        # ——三字段相等绝不掩盖 identity 变化）。groups/member_ids
        # 非权威面不参与；mismatch = re-selection required（刷新
        # 披露重确认），绝不自动修复/替换/重新解析/重试。
        if (expected_resolved is not None
                and live.bindings != expected_resolved.bindings):
            return CompositionChanged(
                _composition_change_reasons(expected_resolved, live,
                                            registry), live)
        # STEP 5 既有第二道（registry 活验证；零复制）
        resolved_or_error = _resolve_runtimes(registry, skipped,
                                              evidence, live.steps)
        if not isinstance(resolved_or_error, dict):
            reason, detail, hint = resolved_or_error
            return CompositionError(
                reason, detail,
                _host_entry()._HINT_QUALIFY if hint else None)
        # STEP 6 唯一装配真相（零新增 slot/plan/session/pipeline；
        # 2.8-A 会话内 task_id 经注入铸造器保唯一）
        composed = _assemble_execution(
            resolved_or_error, task_text, live.steps, timeout_seconds,
            observation_sink=observation_sink, event_index=event_index,
            boundary_hook=boundary_hook,
            task_id=(task_id_mint(task_text)
                     if task_id_mint is not None else None))
        # STEP 6b 2.8-A 前轮 TERMINAL 补发（恰在 append 前——列表
        # 此刻仅含已完成前轮；单 run 路径列表空 = 零行为差）
        _flush_prior_run_terminals(composed_runs, terminal_emitted)
        # STEP 7 groups = run-local 呈现元数据（装配零感知，NamedTuple
        # 官方 _replace 注入；不经 ExecutionSlot/CockpitSession/
        # RunState/pipeline）
        composed = composed._replace(groups=live.groups)
        composed_runs.append(composed)
        return composed

    return UserCompositionSurface(start=start_user_composition,
                                  composed_runs=composed_runs,
                                  listing=verified_listing,
                                  preview=preview_selection)


def _run_first_run_funnel(intent, tui, *, factories, evidence, base_dir,
                          timeout_seconds, boundary_hook,
                          observation_sink, event_index):
    """首跑漏斗分支（交互 TTY + textual 专有；路由谓词已排除一切
    非交互/机器面/开发者形态）。

    前置退出 = exit 0（零执行、零事件、零交付）；启动后的终态
    交付与 legacy human 路径同一函数同一 exit 映射。"""
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
    if event_index is None:
        event_index = tui.new_event_store()
    effective_timeout = (
        intent.timeout_seconds if intent.timeout_seconds is not None
        else (timeout_seconds if timeout_seconds is not None
              else host.DEFAULT_TIMEOUT_SECONDS))
    # 2.8-A 会话级共享件：task_id 铸造器与前轮 TERMINAL 防重集
    # 跨两面（漏斗/default 面与 user COMPOSE 面）共享——同一会话
    # 内无论从哪面启动 run，task_id 唯一性与 TERMINAL 补发防重
    # 全程一致。
    task_id_mint = _task_id_mint()
    terminal_emitted = set()
    surfaces = _funnel_composition_closures(
        registry, skipped, evidence, timeout_seconds=effective_timeout,
        boundary_hook=boundary_hook, observation_sink=observation_sink,
        event_index=event_index, task_id_mint=task_id_mint,
        terminal_emitted=terminal_emitted)
    # CU-COCKPIT-1：user 面（listing/preview/start）与漏斗面共享同一
    # registry/evidence/timeout 现场件——单一池真源、同一装配真相。
    user_surface = _user_composition_surface(
        registry, skipped, evidence, timeout_seconds=effective_timeout,
        boundary_hook=boundary_hook, observation_sink=observation_sink,
        event_index=event_index, task_id_mint=task_id_mint,
        terminal_emitted=terminal_emitted)
    outcome = tui.run_cockpit_funnel(
        composition_preview=surfaces.preview,
        start_composition=surfaces.start,
        task_token=intent.task_token,
        timeout_seconds=intent.timeout_seconds,
        user_composition_surface=user_surface)
    if outcome is None:
        return 0
    composed = surfaces.composed_runs[-1]
    if composed.execution_id not in terminal_emitted:
        # 末轮 TERMINAL 兜底发射（既有 2.7.0 位置；防重集跳过已在前
        # 轮补发中发射过的 run——含 PARKED-at-quit 边沿原样保持：
        # 以 App 外壳 outcome（真实 RunOutcome 投影）为准）
        _emit_run_terminal(composed, outcome.status.value)
        terminal_emitted.add(composed.execution_id)
    for line in _human_lines(composed.task, composed.steps, outcome):
        print(line)
    return _EXIT_CODE_BY_STATUS[outcome.status]


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
    # CU-TUI-5 路由（FIX-1）：预检意图分类位于原 parser 之前——
    # 仅「交互 TTY ∧ 可导入呈现层 ∧ 无 --json/无 --step 形态/无
    # 未知 flag/单 task token/timeout 合法」进入漏斗；其余一切
    # 形态（含全部开发者/机器面/错误形态）逐字节走原 parser。
    intent = _funnel_preflight(argv)
    if intent is not None and _terminal_present():
        tui = _cockpit_tui_module()
        if tui is not None:
            return _run_first_run_funnel(
                intent, tui, factories=factories, evidence=evidence,
                base_dir=base_dir, timeout_seconds=timeout_seconds,
                boundary_hook=boundary_hook,
                observation_sink=observation_sink,
                event_index=event_index)
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

    resolved_or_error = _resolve_runtimes(registry, skipped, evidence,
                                          parsed.steps)
    if not isinstance(resolved_or_error, dict):
        reason, detail, hint = resolved_or_error
        return _fail(reason, detail, json_mode, hint=hint)
    resolved = resolved_or_error

    effective_timeout = (parsed.timeout_seconds
                         if parsed.timeout_seconds is not None
                         else (timeout_seconds if timeout_seconds is not None
                               else host.DEFAULT_TIMEOUT_SECONDS))

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

    composed = _assemble_execution(
        resolved, parsed.task, parsed.steps, effective_timeout,
        observation_sink=observation_sink, event_index=event_index,
        boundary_hook=boundary_hook)

    if tui is not None:
        # 呈现层 = 只读投影 + 注入驱动 + 注入意图外发；执行/控制/
        # 观察真相仍在冻结栈（session / boundary / journal / stores）
        outcome = tui.run_cockpit_tui(
            driver=composed.drive,
            task=composed.task,
            plan=composed.plan,
            events=composed.events,
            facts=composed.facts,
            usage=composed.usage,
            session=composed.session,
            control=composed.dispatch_control,
            revision_pending=composed.revision_pending)
    else:
        outcome = composed.drive()
    if composed.emit is not None:
        composed.emit(ExecutionEventType.TERMINAL,
                      stage="SEQUENTIAL", runtime_id=None,
                      status=outcome.status.value,
                      reason=outcome.status.value)

    if parsed.json_mode:
        payload = _outcome_payload(composed.task_id, outcome, parsed.steps)
        print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    else:
        for line in _human_lines(parsed.task, parsed.steps, outcome):
            print(line)
    return _EXIT_CODE_BY_STATUS[outcome.status]
