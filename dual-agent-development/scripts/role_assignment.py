"""Phase 10H-E: role assignment policy layer (architect/coder joint choice).

在全能力对称证据下，"哪个 runtime 当 architect、哪个当 coder"无法从
验证证据推导 —— 两个候选的 G14 证据都是逐能力 PASS，没有质量梯度。
因此 spread（跨 runtime 分配角色）是一个显式的 POLICY 决策：部署级
多样化选择（不同 runtime = 不同实现栈 = architect/coder 视角多样），
reason 词表如实记录 POLICY_SPREAD / POLICY_CONVERGED，绝不冒充证据
（CLEAR_SPECIALIZATION 属于能力差异化路径，不是本层的词）。

边界（测试逐项锁定）：
- 候选只能来自 bridge 返回集合：assign 只从注入的角色候选集中选择，
  绝不扩集、绝不新造、绝不读 pool/health。
- score-less：不引入任何评分；选出的候选保持 bridge 投影的
  score=None。
- 无 runtime 名、无环境、无子进程、无时钟：纯确定性函数。
- 诚实性：SIMPLE 收敛、单 runtime 收敛、空候选集返回 None（由
  orchestrator 走既有 DUAL_NO_CAPABLE_AGENT），绝不硬造 MULTI、绝不
  裁剪 capability。
- runtime_mode 不是本层的输出：orchestrator 仍从 identity 比较计算
  SINGLE_RUNTIME/MULTI（单一来源）。

本模块不 import runtime/adapter/pool —— 依赖只有 bridge 的候选集
数据类型与 task_classifier 的 Complexity 词表。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from task_classifier import Complexity
from verified_selection_bridge import VerifiedRoleCandidate, VerifiedRoleCandidateSet


@dataclass(frozen=True)
class RoleAssignment:
    """Immutable joint assignment for one dual run; candidates are chosen
    verbatim from the injected bridge sets (never minted here)."""

    assignments: Mapping[str, VerifiedRoleCandidate | None]
    reason: str

    def __post_init__(self) -> None:
        for candidate in self.assignments.values():
            if candidate is not None and not isinstance(candidate, VerifiedRoleCandidate):
                raise TypeError("assignments must hold VerifiedRoleCandidate values")


def _first_on_other_runtime(set_: VerifiedRoleCandidateSet, runtime_id: str):
    """First candidate of a set on a runtime OTHER than the given one."""
    for candidate in set_.candidates:
        if candidate.runtime_id != runtime_id:
            return candidate
    return None


class ConvergingAssigner:
    """Default policy: per-role candidates[0] (the bridge's global order).

    逐字复刻 10H-D 及此前的折叠行为 —— sorted(pool.identities()) 的
    首个合格候选对每个角色都中标。作为默认策略注入 orchestrator 后，
    现有全部行为保持不变。
    """

    def assign(
        self,
        role_candidate_sets: Mapping[str, VerifiedRoleCandidateSet],
        complexity: Complexity | str,
    ) -> RoleAssignment:
        assignments = {
            role: (set_.candidates[0] if set_ and set_.candidates else None)
            for role, set_ in role_candidate_sets.items()
        }
        return RoleAssignment(assignments, "POLICY_CONVERGED")


class DiversityAssigner:
    """Spread policy: distinct runtimes for architect and coder when the
    task is non-SIMPLE and a second runtime genuinely exists.

    architect 取候选集首个（与默认一致），coder 取候选集中首个
    runtime 不同的候选。没有第二 runtime、SIMPLE 任务或候选集为空时
    诚实收敛（POLICY_CONVERGED）—— 收敛不是失败，spread 不是义务。
    """

    def assign(
        self,
        role_candidate_sets: Mapping[str, VerifiedRoleCandidateSet],
        complexity: Complexity | str,
    ) -> RoleAssignment:
        complexity = Complexity(complexity)
        architect_set = role_candidate_sets.get("architect")
        coder_set = role_candidate_sets.get("coder")
        architect = architect_set.candidates[0] if architect_set and architect_set.candidates else None
        coder = coder_set.candidates[0] if coder_set and coder_set.candidates else None

        if (
            complexity is not Complexity.SIMPLE
            and architect is not None
            and coder is not None
        ):
            # 候选已在不同 runtime（能力差异化池的既有形态）或存在
            # 可换的其它 runtime 候选时，spread 成立。
            if architect.runtime_id != coder.runtime_id:
                return RoleAssignment(
                    {"architect": architect, "coder": coder}, "POLICY_SPREAD")
            spread = _first_on_other_runtime(coder_set, architect.runtime_id)
            if spread is not None:
                coder = spread
                return RoleAssignment(
                    {"architect": architect, "coder": coder}, "POLICY_SPREAD")

        assignments = {
            role: (set_.candidates[0] if set_ and set_.candidates else None)
            for role, set_ in role_candidate_sets.items()
        }
        return RoleAssignment(assignments, "POLICY_CONVERGED")
