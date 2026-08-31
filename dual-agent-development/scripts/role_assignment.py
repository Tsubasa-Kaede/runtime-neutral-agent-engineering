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


def _first_of(set_: VerifiedRoleCandidateSet | None):
    """First candidate of a set, or None for absent/empty sets."""
    return set_.candidates[0] if set_ and set_.candidates else None


def _runtime_ids_in_order(role_candidate_sets, roles):
    """Deterministic runtime order: scan every role's candidates in
    bridge order (roles in sorted-key order), collecting each runtime the
    first time it appears. This is the single ordering source for the
    round-robin spread — no scores, no runtime names, just positions."""
    ordered: list[str] = []
    for role in roles:
        set_ = role_candidate_sets.get(role)
        for candidate in (set_.candidates if set_ else ()):
            if candidate.runtime_id not in ordered:
                ordered.append(candidate.runtime_id)
    return ordered


class ConvergingAssigner:
    """Default policy: per-role candidates[0] (the bridge's global order).

    逐字复刻 10H-D 及此前的折叠行为 —— sorted(pool.identities()) 的
    首个合格候选对每个角色都中标。对任意注入角色集合（两角色或
    四角色）语义相同。作为默认策略注入 orchestrator / facade 后，
    现有全部行为保持不变。
    """

    def assign(
        self,
        role_candidate_sets: Mapping[str, VerifiedRoleCandidateSet],
        complexity: Complexity | str,
    ) -> RoleAssignment:
        assignments = {
            role: _first_of(set_)
            for role, set_ in role_candidate_sets.items()
        }
        return RoleAssignment(assignments, "POLICY_CONVERGED")


class DiversityAssigner:
    """Spread policy: deterministic round-robin across distinct runtimes.

    对注入的全部角色（10H-E 的 architect/coder，10H-F 的四角色）按
    sorted role keys 的固定次序，在可用 runtime 间交替分配。双
    runtime 池下：architect→首 runtime、coder→次 runtime、review→首、
    test→次。某角色候选集不含目标 runtime 的候选时，该角色保底
    candidates[0]（诚实降级，不扩集）；候选集为空时返回 None。
    SIMPLE 任务、单 runtime 或候选集为空时诚实收敛
    （POLICY_CONVERGED）—— 收敛不是失败，spread 不是义务。

    向后兼容：只注入 architect/coder 两键时，输出与 10H-E 轮逐字
    一致（architect→X、coder→Y）。
    """

    def assign(
        self,
        role_candidate_sets: Mapping[str, VerifiedRoleCandidateSet],
        complexity: Complexity | str,
    ) -> RoleAssignment:
        complexity = Complexity(complexity)
        roles = sorted(role_candidate_sets)
        converged = {
            role: _first_of(role_candidate_sets.get(role))
            for role in roles
        }
        if complexity is Complexity.SIMPLE:
            return RoleAssignment(converged, "POLICY_CONVERGED")

        runtime_order = _runtime_ids_in_order(role_candidate_sets, roles)
        if len(runtime_order) < 2:
            # 单 runtime（或无候选）：没有可 spread 的第二方。
            return RoleAssignment(converged, "POLICY_CONVERGED")

        assignments = {}
        for index, role in enumerate(roles):
            candidate = converged[role]
            if candidate is None:
                assignments[role] = None
                continue
            target_runtime = runtime_order[index % len(runtime_order)]
            if candidate.runtime_id == target_runtime:
                assignments[role] = candidate
                continue
            # 目标 runtime 上的该角色候选；不存在则保底 candidates[0]。
            switched = None
            for option in (role_candidate_sets[role].candidates
                           if role_candidate_sets.get(role) else ()):
                if option.runtime_id == target_runtime:
                    switched = option
                    break
            if switched is not None:
                assignments[role] = switched
            else:
                assignments[role] = candidate
        # spread 是否成立由整体输出判断：只要最终分配真的落在多于
        # 一个 runtime 上（无论来自 round-robin 换选还是能力差异化
        # 池的天然分置），就是 POLICY_SPREAD；全部落回同一 runtime
        # 则诚实收敛。
        distinct = {c.runtime_id for c in assignments.values() if c is not None}
        reason = "POLICY_SPREAD" if len(distinct) > 1 else "POLICY_CONVERGED"
        return RoleAssignment(assignments, reason)
