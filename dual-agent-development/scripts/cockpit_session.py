"""CU-TUI-2 (V3.2): CockpitSession — control + RunState session coordinator.

职责（CU-TUI-2 授权面，SESSION COORDINATOR 而非 SECOND ENGINE）：

- RunState 单一持有者：PARKED 保存、resume 消费、terminal 清空；
- 控制提交门面：真实终态后的本地拒绝（G3 终态门——零事实、
  零 boundary 副作用，复用既有 ALREADY_TERMINAL 保留 reason）；
- segment 串行执行：run_segment 一段一停，PARKED ≠ terminal；
- SUBMISSION 修订消费（G2）：接受时登记载荷值（intent 真相仍在
  boundary / journal，这里只持命令数据），fresh segment 起点按
  FIFO 逐字段后者胜合并，经注入的 steps 工厂重建受影响配置
  ——重建即 applied 边界；honored 由真实 invocation 请求见证，
  本层绝不声称；
- 终态投影：last_outcome 恒为管线返回的真实 RunOutcome（零合成）。

边界外（本层绝不做）：运行时选择 / 资格认定 / 准入策略 / 二次
尝试 / 替代路径 / 预算 / 用量记账 / provider 调用 / 观察真相生成
——全部真相仍归冻结栈（boundary / journal / slots / pipeline）。
NEXT_INVOCATION 修订经冻结链由既有 adapter 一次性消费；本层不读
修订队列、不复制其 drain 语义（G1 @ 1947fdf 冻结）。

线程模型：单 worker 串行段执行；控制提交走 boundary 既有锁安全
面；本层零新锁、零后台池、零跨进程状态。
"""
from __future__ import annotations

from control_boundary import (
    ControlBoundary,
    ControlCommand,
    ControlCommandType,
    ControlModelError,
    ControlReason,
    ControlResult,
    ControlStatus,
    RevisionPayload,
    RevisionTarget,
)
from execution_slots import ExecutionSlots
from sequential_pipeline import RunStatus, build_sequential_pipeline

__all__ = ("CockpitSession",)

_TERMINAL_STATUSES = (RunStatus.COMPLETED, RunStatus.FAILED,
                      RunStatus.ABORTED)


class CockpitSession:
    """session 协调者：RunState owner + 控制门面 + 段执行。"""

    def __init__(self, *, boundary, slots, submission, steps_factory):
        if not isinstance(boundary, ControlBoundary):
            raise ControlModelError("boundary must be a ControlBoundary")
        if not isinstance(slots, ExecutionSlots):
            raise ControlModelError(
                "slots must be ExecutionSlots values")
        if not isinstance(submission, RevisionPayload):
            raise ControlModelError(
                "submission must be a RevisionPayload")
        if submission.target is not RevisionTarget.SUBMISSION:
            raise ControlModelError(
                "submission payload must target SUBMISSION")
        if not callable(steps_factory):
            raise ControlModelError("steps_factory must be callable")
        self._boundary = boundary
        self._slots = slots
        self._steps_factory = steps_factory
        self._submission = submission
        self._pipeline = None
        self._run_state = None
        self._pending_submissions = []       # 已接受待应用的载荷（FIFO）
        self._recorded_submission_ids = set()  # 重放幂等：不重复登记
        self._terminal_status = None
        self._last_outcome = None
        self._rebuild_pipeline()  # 组合期 fail-fast：空/坏 steps 即拒绝

    # ------------------------------------------------------------ 投影

    @property
    def run_state(self):
        """当前持有的续走值（PARKED 后非 None；terminal 后恒 None）。"""
        return self._run_state

    @property
    def terminal(self):
        """真实终态（RunStatus）或 None——只来自真实 RunOutcome。"""
        return self._terminal_status

    @property
    def last_outcome(self):
        """最近一次真实 RunOutcome（零合成、零改写、零再执行）。"""
        return self._last_outcome

    # ------------------------------------------------------------ 控制

    def submit(self, command):
        """控制提交门面：终态门 → boundary 裁决 → SUBMISSION 登记。"""
        if not isinstance(command, ControlCommand):
            raise ControlModelError("submit expects a ControlCommand")
        if self._terminal_status is not None:
            # G3：真实终态后不再受理任何控制命令——本地拒绝，
            # 零事实、零 boundary 副作用（既有保留 reason）
            return ControlResult(
                command_id=command.command_id,
                execution_id=command.execution_id,
                status=ControlStatus.REJECTED,
                execution_version=self._boundary.execution_version,
                reason=ControlReason.ALREADY_TERMINAL)
        result = self._boundary.submit(command)
        if (result.status is ControlStatus.ACCEPTED
                and command.command is ControlCommandType.REVISE
                and command.payload.target is RevisionTarget.SUBMISSION
                and command.command_id not in self._recorded_submission_ids):
            # G2 登记：accepted（intent 真相在 boundary / journal）
            # ≠ applied（fresh segment 起点重建）≠ honored（真实
            # invocation 请求见证）。精确重放（同 id）零重复登记。
            self._recorded_submission_ids.add(command.command_id)
            self._pending_submissions.append(command.payload)
        return result

    # ------------------------------------------------------------ 执行

    def run_segment(self):
        """串行执行一段：PARKED 保存续走值；terminal 清空并封门。

        resume 路径原样传入持有的 RunState（不回 step 0、不重建
        历史 transcript）；终态后调用为幂等投影（返回最近真实
        outcome，零再执行）。"""
        if self._terminal_status is not None:
            return self._last_outcome
        if self._run_state is None:
            self._consume_pending_submissions()
        outcome = self._pipeline.run(self._run_state)
        self._last_outcome = outcome
        if outcome.status is RunStatus.PARKED:
            self._run_state = outcome.run_state
        elif outcome.status in _TERMINAL_STATUSES:
            self._terminal_status = outcome.status
            self._run_state = None  # terminal 后不持有可执行续走值
        return outcome

    # ------------------------------------------------------------ 内部

    def _rebuild_pipeline(self):
        self._pipeline = build_sequential_pipeline(
            self._slots, self._steps_factory(self._submission))

    def _consume_pending_submissions(self):
        """fresh segment 起点：FIFO 逐字段后者胜合并 → 重建 steps。

        这是 SUBMISSION 修订的 applied 边界：注入的工厂收到合并后
        的 submission 值并生成新 builders；旧 builders 自此不再被
        引用。停驻段恢复（持有 RunState）不经过此处——SUBMISSION
        修订只作用于下一段 fresh segment。"""
        if not self._pending_submissions:
            return
        task = self._submission.task
        prompt = self._submission.prompt
        for revision in self._pending_submissions:
            if revision.task is not None:
                task = revision.task
            if revision.prompt is not None:
                prompt = revision.prompt
        self._submission = RevisionPayload(
            target=RevisionTarget.SUBMISSION, task=task, prompt=prompt)
        self._pending_submissions = []
        self._rebuild_pipeline()
