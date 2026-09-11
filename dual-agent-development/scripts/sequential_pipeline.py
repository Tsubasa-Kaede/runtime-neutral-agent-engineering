"""V3.2 固定顺序管线：值模型（CU-ORCH-1）+ 最小执行面（CU-ORCH-2）。

值层只立契约：运行状态词表、步规格、步投影记录、续走值、
运行结果——全部为不可变值对象，构造期完成全部结构校验
（复用控制域既有构造期拒绝类型，零新增类型）。

- 词表隔离：运行状态是编排域局部的单次调用返回状态，不是
  控制域生命周期投影——两域零 import 混用，概念不互换。
- 步规格 = 不透明槽引用 + request 构造函数；槽引用只是组合
  域映射键的引用，不是身份。
- 步投影记录只承载既有执行事实的引用（步号、槽引用、调用
  状态原样、调用标识若有）——绝不铸造任何新事实；调用标识
  缺席即缺席，不伪造。
- 续走值是 caller 持有的瞬态编排数据：恰三字段、只在内存、
  不携带任何身份、不缓存任何控制或观察投影。
- 运行结果结构不变量：续走值非空当且仅当停驻态；普通错误
  仅失败态可携带；final_result 仅完成/失败态可携带；终局态
  （完成/失败/中止）一律零续走值。

执行层（CU-ORCH-2 最小面）：

- 构造期全量校验 + 槽引用探测：缺席槽以 KeyError 原样暴露
  （零翻译零回退），绝不留待运行期首步才失败；探测是纯读，
  任何失败路径账本零新事实、零可达半成品。
- 管线零状态（Model A）：无游标、无缓存、无可变执行上下文——
  一切续走信息由 caller 持有的 RunState 显式携带；同管线可
  承载任意多个互不可见的并行续走值。
- 单次 run = 确定性转移：每步先现读控制真相（ABORT 待定即
  中止返回；PAUSE 待定即停驻携带续走值），再由 caller 供应的
  纯函数构造请求，经不可变组合面委托既有执行链，结果状态非
  成功即失败即停（fail-fast 冻结：零重试、零回退、零改道）。
- 捕获恰一处（run 边界、两分支）：控制域中止信号只翻译为
  中止返回（消费不是裁决：零写入、零确认、零终态事实）；
  普通异常一律译为失败结果，error 携带原异常对象。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from control_boundary import ControlModelError, PendingIntentKind
from control_gate import ControlAborted
from execution_slots import ExecutionSlots
from external_runtime import InvocationStatus

__all__ = (
    "RunOutcome", "RunState", "RunStatus", "SequentialPipeline",
    "StepRecord", "StepSpec", "build_sequential_pipeline",
)


class RunStatus(str, Enum):
    """编排域局部词表：单次 run 调用的返回值状态（恰四值）。

    与控制域生命周期词表是两个域：本词表不投影、不派生、
    不驱动任何控制状态，仅描述一次调用如何结束（停驻是唯一
    非终局态——携带续走值）。
    """

    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    ABORTED = "ABORTED"
    PARKED = "PARKED"


def _require_non_empty_string(value, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ControlModelError(f"{field_name} must be a non-empty string")


def _require_step_index(value, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ControlModelError(
            f"{field_name} must be a non-negative int")


def _require_step_record_tuple(value, field_name: str) -> None:
    if not isinstance(value, tuple):
        raise ControlModelError(f"{field_name} must be a tuple")
    for entry in value:
        if not isinstance(entry, StepRecord):
            raise ControlModelError(
                f"{field_name} entries must be StepRecord values")


@dataclass(frozen=True)
class StepSpec:
    """一个步的组合规格：不透明槽引用 + request 构造函数。

    request 构造函数契约（纪律）：纯函数，接受上一步结果或
    None，返回下一步调用请求；本层不执行、不检查返回类型
    （用法期由链上组件拒绝）。
    """

    slot_id: str
    request_builder: object

    def __post_init__(self) -> None:
        _require_non_empty_string(self.slot_id, "slot_id")
        if not callable(self.request_builder):
            raise ControlModelError(
                "request_builder must be callable")


@dataclass(frozen=True)
class StepRecord:
    """步投影记录：只承载既有执行事实的引用（恰四字段）。

    status 是调用结果自带状态的原样引用（本层零转换零铸造）；
    invocation_id 来自调用结果的 trace——trace 缺席即 None，
    绝不伪造。
    """

    step_index: int
    slot_id: str
    status: object
    invocation_id: str | None = None

    def __post_init__(self) -> None:
        _require_step_index(self.step_index, "step_index")
        _require_non_empty_string(self.slot_id, "slot_id")
        if self.invocation_id is not None:
            _require_non_empty_string(self.invocation_id, "invocation_id")


@dataclass(frozen=True)
class RunState:
    """caller 持有的瞬态续走值（恰三字段，只在内存）。

    编排域瞬态 continuation：不是控制生命周期真值、不是观察
    真值、不是用量真值、不是账本事实、不是任何身份；不缓存
    任何控制/观察投影（准入时一律现读控制真相）。
    next_step_index 语义 =「下一次需要执行的步序号」：首值 0，
    第 N 步成功后为 N+1，等于步总数即完成。
    """

    next_step_index: int
    previous_result: object = None
    transcript: tuple = ()

    def __post_init__(self) -> None:
        _require_step_index(self.next_step_index, "next_step_index")
        _require_step_record_tuple(self.transcript, "transcript")


@dataclass(frozen=True)
class RunOutcome:
    """单次 run 调用的返回值（恰五字段 + 结构不变量）。

    不变量（构造期强制）：
    - run_state 非 None ⟺ status is PARKED（终局态零续走值）；
    - error 非 None ⟹ FAILED（普通错误仅失败态；控制域中止
      信号不是 error）；
    - final_result 非 None ⟹ status ∈ {COMPLETED, FAILED}；
      PARKED / ABORTED 强制 None。
    """

    status: RunStatus
    transcript: tuple = ()
    final_result: object = None
    error: object = None
    run_state: object = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, RunStatus):
            raise ControlModelError(f"unknown run status: {self.status!r}")
        _require_step_record_tuple(self.transcript, "transcript")
        if (self.status is RunStatus.PARKED) != (self.run_state is not None):
            raise ControlModelError(
                "run_state is carried if and only if status is PARKED")
        if self.error is not None and self.status is not RunStatus.FAILED:
            raise ControlModelError(
                "error is carried only on FAILED")
        if self.final_result is not None and self.status not in (
                RunStatus.COMPLETED, RunStatus.FAILED):
            raise ControlModelError(
                "final_result is carried only on COMPLETED or FAILED")


class SequentialPipeline:
    """固定顺序管线的最小执行面：不可变组合上的确定性转移函数。

    (self, 续走值 | None) → RunOutcome。管线自身零状态：无游标、
    无缓存、无可变执行上下文——一切续走信息由 caller 持有的
    RunState 显式携带（Model A）；构造产物不可变，slot 集封闭。
    """

    def __init__(self, slots, steps):
        self._slots = slots
        self._steps = steps

    def run(self, run_state=None):
        """单次转移：逐步「现读准入 → 构造请求 → 委托 → 失败即停」。

        准入每步现读控制真相（零缓存）：ABORT 待定即中止返回，
        PAUSE 待定即停驻并携带续走值（续步序号 = 下一步序号）。
        控制域中止信号只在本边界捕获并翻译为中止返回（消费不是
        裁决：零写入、零确认）；普通异常一律译为失败结果，error
        携带原异常对象；成功步结果原样成为交接结果与投影记录。
        """
        if run_state is None:
            next_index = 0
            previous_result = None
            transcript = ()
        else:
            if not isinstance(run_state, RunState):
                raise ControlModelError(
                    "run_state must be a RunState value")
            next_index = run_state.next_step_index
            previous_result = run_state.previous_result
            transcript = run_state.transcript
        try:
            while next_index < len(self._steps):
                intent = self._slots.boundary.pending_intent.kind
                if intent is PendingIntentKind.ABORT:
                    return RunOutcome(status=RunStatus.ABORTED,
                                      transcript=transcript)
                if intent is PendingIntentKind.PAUSE:
                    return RunOutcome(
                        status=RunStatus.PARKED, transcript=transcript,
                        run_state=RunState(
                            next_step_index=next_index,
                            previous_result=previous_result,
                            transcript=transcript))
                step = self._steps[next_index]
                request = step.request_builder(previous_result)
                handle = self._slots.slot(step.slot_id)
                result = handle.invoke(request)
                trace = result.trace
                transcript = transcript + (StepRecord(
                    step_index=next_index, slot_id=step.slot_id,
                    status=result.status,
                    invocation_id=(trace.invocation_id
                                   if trace is not None else None)),)
                if result.status is not InvocationStatus.SUCCESS:
                    return RunOutcome(status=RunStatus.FAILED,
                                      transcript=transcript,
                                      final_result=result)
                previous_result = result
                next_index += 1
            return RunOutcome(status=RunStatus.COMPLETED,
                              transcript=transcript,
                              final_result=previous_result)
        except ControlAborted:
            return RunOutcome(status=RunStatus.ABORTED,
                              transcript=transcript)
        except Exception as error:
            return RunOutcome(status=RunStatus.FAILED,
                              transcript=transcript, error=error)


def build_sequential_pipeline(slots, steps):
    """构造固定顺序管线：全量结构校验 + 构造期槽探测（零残留）。

    槽引用缺席在构造期即以 KeyError 原样暴露（零翻译、零回退、
    零捕获），绝不留待运行期首步才失败；探测是纯读——任何失败
    路径账本零新事实、无可达半成品，同组合重试合法。
    """
    if not isinstance(slots, ExecutionSlots):
        raise ControlModelError("slots must be ExecutionSlots values")
    steps = tuple(steps)
    if not steps:
        raise ControlModelError("at least one step spec is required")
    for step in steps:
        if not isinstance(step, StepSpec):
            raise ControlModelError("steps must be StepSpec values")
    for step in steps:
        slots.slot(step.slot_id)  # 构造期探测：缺席槽即刻暴露
    return SequentialPipeline(slots, steps)
