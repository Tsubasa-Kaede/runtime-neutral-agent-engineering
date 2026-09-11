"""V3.2 编排值模型（CU-ORCH-1）：固定顺序管线的返回契约值。

本模块只立值：运行状态词表、步规格、步投影记录、续走值、
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

执行语义（顺序循环、准入、交接、失败即停）属后续单元；
本文件零执行逻辑、零捕获、零锁。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from control_boundary import ControlModelError

__all__ = (
    "RunOutcome", "RunState", "RunStatus", "StepRecord", "StepSpec",
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
