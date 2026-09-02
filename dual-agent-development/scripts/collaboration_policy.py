"""R7-A1: user-controlled collaboration policy — pure, runtime-neutral core.

CollaborationPolicy 是调用方提供的运行级约束（runtime allowlist +
distinct-runtime 基数界 + reuse 开关），以纯数据形态作用于
"bridge 候选集 -> 角色指派" 之间：

    Verified Candidate Set
        ↓ apply_policy（纯过滤，绝不扩集/造候选）
    PolicyConstrainedAssigner.assign(sets, complexity)
        ↓ 复刻 role_assignment 的 runtime_order 轮转
    RoleAssignment（reason ∈ 封闭 POLICY_* 词表）

边界（与 role_assignment.py 同款纪律，测试逐项锁定）：
- 只能过滤输入候选集；被指派候选永远是注入集合的成员，绝不新造、
  绝不改 rank/score（score 恒 None）。
- min_distinct_runtimes 是期望下界，不是 retry/fallback/backfill 指令：
  不可满足时照常返回最优指派，reason 如实标注 POLICY_COUNT_UNSATISFIED。
- max_distinct_runtimes 只能在既有候选顺序内裁剪，绝不重排、绝不过滤
  之外重造。
- allow_runtime_reuse=False 要求 runtime->role 单射；runtime 不足时该
  角色 None（诚实降级，绝不扩候选、绝不调额外 runtime）。
- reason 是封闭词表：POLICY_SPREAD / POLICY_CONVERGED /
  POLICY_COUNT_UNSATISFIED / POLICY_RUNTIME_ABSENT=<ids>，绝不自由文本、
  绝不从环境状态生成。

本模块无 runtime 名、无环境、无子进程、无时钟、无随机：纯确定性
函数。不 import runtime/adapter/pool/health —— 依赖只有 bridge 候选集
数据类型、RoleAssignment 与 task_classifier 的 Complexity 词表。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from role_assignment import RoleAssignment
from task_classifier import Complexity
from verified_selection_bridge import VerifiedRoleCandidateSet

_SECRET_MARKERS = ("token", "secret", "api_key", "authorization", "bearer",
                   "stdout", "stderr")


def _assert_secret_free(value: str, field: str) -> None:
    lowered = value.lower()
    for marker in _SECRET_MARKERS:
        if marker in lowered:
            raise ValueError(
                f"{field} must not contain secret-shaped content: {marker}")


@dataclass(frozen=True)
class CollaborationPolicy:
    """Run-level collaboration constraint; pure data, caller-supplied.

    runtime_allowlist=None 表示"不限制"（与显式空 tuple 不同——空
    tuple 是构造期错误，绝不静默读作"谁都不许"）。字段语义见模块
    docstring；非法值在构造期拒绝，绝不静默修正。
    """

    runtime_allowlist: tuple[str, ...] | None = None
    min_distinct_runtimes: int | None = None
    max_distinct_runtimes: int | None = None
    allow_runtime_reuse: bool = True

    def __post_init__(self) -> None:
        if self.runtime_allowlist is not None:
            if not isinstance(self.runtime_allowlist, (tuple, list)):
                raise ValueError("runtime_allowlist must be a tuple of strings")
            for item in self.runtime_allowlist:
                if not isinstance(item, str):
                    raise ValueError("runtime_allowlist entries must be strings")
                if not item.strip():
                    raise ValueError("runtime_allowlist entries must be non-empty")
                _assert_secret_free(item, "runtime_allowlist entry")
            # 去重 + 排序规范化为确定性 tuple（重复是冗余，不是错误）。
            normalized = tuple(sorted(set(self.runtime_allowlist)))
            if not normalized:
                raise ValueError(
                    "runtime_allowlist must not be empty; use None for unrestricted")
            object.__setattr__(self, "runtime_allowlist", normalized)
        for name in ("min_distinct_runtimes", "max_distinct_runtimes"):
            value = getattr(self, name)
            if value is not None:
                if not isinstance(value, int) or isinstance(value, bool):
                    raise ValueError(f"{name} must be an integer")
                if value < 1:
                    raise ValueError(f"{name} must be >= 1")
        if (self.min_distinct_runtimes is not None
                and self.max_distinct_runtimes is not None
                and self.min_distinct_runtimes > self.max_distinct_runtimes):
            raise ValueError("min_distinct_runtimes must not exceed max_distinct_runtimes")
        if (self.runtime_allowlist is not None
                and self.min_distinct_runtimes is not None
                and len(self.runtime_allowlist) < self.min_distinct_runtimes):
            raise ValueError(
                "runtime_allowlist cardinality below min_distinct_runtimes")


@dataclass(frozen=True)
class PolicyApplication:
    """apply_policy 的结果：受限候选集 + 缺席报告（纯投影数据）。"""

    filtered_sets: Mapping[str, VerifiedRoleCandidateSet]
    absent_runtimes: tuple[str, ...]


def _runtime_order(candidate_sets, roles):
    """Deterministic runtime order — verbatim replica of
    role_assignment._runtime_ids_in_order: scan every role's candidates in
    bridge order (roles in sorted-key order), collecting each runtime the
    first time it appears. No scores, no runtime names, just positions."""
    ordered: list[str] = []
    for role in roles:
        set_ = candidate_sets.get(role)
        for candidate in (set_.candidates if set_ else ()):
            if candidate.runtime_id not in ordered:
                ordered.append(candidate.runtime_id)
    return ordered


def apply_policy(
    candidate_sets: Mapping[str, VerifiedRoleCandidateSet],
    policy: CollaborationPolicy,
) -> PolicyApplication:
    """Pure filter: allowlist 求交 + max 裁剪，附缺席报告。

    只能过滤输入候选集（输出 ⊆ 输入，构造层由成员关系保证）；绝不
    增加候选、绝不创建候选、绝不查询环境/runtime、绝不使用时间或
    随机。缺席报告识别 allowlist 中被点名但候选集完全没有的
    runtime —— 纯集合差运算，不做任何探测。
    """
    if not isinstance(policy, CollaborationPolicy):
        raise ValueError("policy must be a CollaborationPolicy")
    roles = sorted(candidate_sets)
    allowlist = policy.runtime_allowlist

    # 阶段 1：allowlist 求交（保留 bridge 候选顺序与原对象）。
    if allowlist is None:
        allowed: dict[str, list] = {
            role: list((candidate_sets.get(role) or VerifiedRoleCandidateSet(role, ())).candidates)
            for role in roles
        }
    else:
        allowed = {
            role: [candidate for candidate in (candidate_sets.get(role).candidates
                                               if candidate_sets.get(role) else ())
                   if candidate.runtime_id in allowlist]
            for role in roles
        }

    # 阶段 2：max_distinct_runtimes —— 只在既有候选顺序内保留前
    # max 个 runtime 的候选（bridge 顺序 = sorted(pool.identities())
    # 投影顺序），绝不重排、绝不新造。
    if policy.max_distinct_runtimes is not None:
        kept: list[str] = []
        for role in roles:
            for candidate in allowed[role]:
                if candidate.runtime_id not in kept:
                    kept.append(candidate.runtime_id)
        kept = kept[:policy.max_distinct_runtimes]
        allowed = {
            role: [candidate for candidate in allowed[role]
                   if candidate.runtime_id in kept]
            for role in roles
        }

    # 阶段 3：缺席报告 —— allowlist 中点名、但任何角色候选集中
    # 都没有的 runtime（确定性排序输出）。
    present: set[str] = set()
    for role in roles:
        for candidate in (candidate_sets.get(role).candidates
                          if candidate_sets.get(role) else ()):
            present.add(candidate.runtime_id)
    absent: tuple[str, ...] = (
        tuple(sorted(set(allowlist) - present)) if allowlist is not None else ())

    filtered_sets = {
        role: VerifiedRoleCandidateSet(role, tuple(allowed[role]))
        for role in roles
    }
    return PolicyApplication(filtered_sets=filtered_sets,
                             absent_runtimes=absent)


class PolicyConstrainedAssigner:
    """RoleAssigner 协议实现：policy 约束下的确定性 spread/converge。

    assign(role_candidate_sets, complexity) -> RoleAssignment——既有
    assigner 协议签名原样。runtime 顺序复刻 role_assignment 的
    _runtime_ids_in_order 语义（见上方 _runtime_order 的逐字复刻），
    reuse=True 时按 sorted role keys 的固定次序在 runtime 间轮转
    （DiversityAssigner 同款 round-robin）；reuse=False 时单射，
    runtime 不足的角色诚实 None。SINGLE runtime 或 SIMPLE 任务诚实
    收敛。reason 只从封闭 POLICY_* 词表取值。
    """

    def __init__(self, policy: CollaborationPolicy):
        if not isinstance(policy, CollaborationPolicy):
            raise ValueError("policy must be a CollaborationPolicy")
        self._policy = policy

    @property
    def policy(self) -> CollaborationPolicy:
        return self._policy

    def assign(
        self,
        role_candidate_sets: Mapping[str, VerifiedRoleCandidateSet],
        complexity: Complexity | str,
    ) -> RoleAssignment:
        complexity = Complexity(complexity)
        application = apply_policy(role_candidate_sets, self._policy)
        sets = application.filtered_sets
        roles = sorted(sets)

        converged = {
            role: (sets[role].candidates[0]
                   if sets.get(role) and sets[role].candidates else None)
            for role in roles
        }

        absent = application.absent_runtimes
        runtime_order = _runtime_order(sets, roles)

        # 诚实收敛路径：SIMPLE 任务、单 runtime 或空候选 —— 收敛
        # 不是失败，spread 不是义务（DiversityAssigner 同语义）。
        if complexity is Complexity.SIMPLE or len(runtime_order) < 2:
            assignments, reason = self._finalize(
                converged, absent, runtime_order)
            return RoleAssignment(assignments, reason)

        if self._policy.allow_runtime_reuse:
            # Round-robin spread（复刻 DiversityAssigner）：sorted role
            # keys 的固定次序，在 runtime 间交替分配；某角色候选集
            # 不含目标 runtime 的候选时保底 candidates[0]。
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
                switched = next(
                    (option for option in sets[role].candidates
                     if option.runtime_id == target_runtime), None)
                assignments[role] = switched if switched is not None else candidate
        else:
            # 单射：runtime 对 role 一一对应（按 sorted role keys 与
            # runtime_order 顺序配对）；runtime 不足的角色 None，
            # 绝不 backfill、绝不扩候选。
            assignments = {}
            used: set[str] = set()
            for role in roles:
                candidate = next(
                    (option for option in sets[role].candidates
                     if option.runtime_id not in used), None)
                if candidate is not None:
                    used.add(candidate.runtime_id)
                assignments[role] = candidate

        assignments, reason = self._finalize(
            assignments, absent, runtime_order)
        return RoleAssignment(assignments, reason)

    def _finalize(self, assignments, absent, runtime_order):
        """Closed-vocabulary reason computation（无自由文本）。

        优先级：点名 runtime 缺席 > 数量不可满足 > spread/收敛观察。
        min 不可满足时 assignment 照常返回（期望下界不是 retry 指令）。
        """
        distinct = {candidate.runtime_id
                    for candidate in assignments.values()
                    if candidate is not None}
        if absent:
            reason = "POLICY_RUNTIME_ABSENT=" + ",".join(absent)
        elif (self._policy.min_distinct_runtimes is not None
              and len(distinct) < self._policy.min_distinct_runtimes):
            reason = "POLICY_COUNT_UNSATISFIED"
        else:
            reason = "POLICY_SPREAD" if len(distinct) > 1 else "POLICY_CONVERGED"
        return assignments, reason
