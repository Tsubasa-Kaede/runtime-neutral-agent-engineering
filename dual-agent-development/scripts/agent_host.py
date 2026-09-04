"""V3.0-D: Agent composition root — Agent Composition first wired into
the frozen V2 execution stack.

V3.0-A/B/C 至今零执行侧消费者（A/B 是纯声明层，C 产出槽位但无人消
费）。本模块是它们的第一个消费者：把「调用方选定的 agent 队伍 + 各
runtime 的 V2 资格证据 + 健康快照」组装成一个未修改的 ProductionFacade，
其地址/adapter 宇宙完全由 agent 声明派生：

    AgentRegistry（声明，V3.0-B）
      ↓ compose_agent_slots（V3.0-C，纯函数）
    AgentSlotResolution × 4 角色（compat 地址 + adapter 实例）
      ↓ build_facade_from_agents（本模块：组合根）
    (ProductionFacade, attribution)   <- V2 原样，零修改

职责边界（locked by tests/test_agent_agent_host.py）：
- 组合根只翻译，不裁决。哪个 runtime 被准入由既有 VerifiedRuntimePool
  admission 机器决定（VERIFIED→capability→health→duplicate）；哪个
  runtime 扮演哪个角色由既有 bridge + assigner + policy 决定。本模块
  绝不包含任何角色选择逻辑，也绝不探测 runtime 或调用 adapter。
- 能力一致性不变量（双向）：对四个协作 capability，
  「该 runtime 上选定 agent 的声明角色投影」必须与证据的
  validated_capabilities 完全一致。声明角色缺乏证据 capability 时由
  V2 admission 诚实拒绝（CAPABILITY_INSUFFICIENT 原样上抛，本模块绝
  不代补）；反之，证据含未被任何选定 agent 声明的协作 capability 时
  组合期拒绝——冻结 bridge 按 validation.validated_capabilities 过滤
  候选（admission 传入的能力集不进入选择），该 runtime 会被选中扮演
  未声明角色而命中未注册地址，这种组合无法诚实执行。推论：冻结选择
  器可能选中的任何 (runtime, role) 地址必然已注册（有声明者 ⇒ 有
  adapter）——不存在选择期 KeyError 深洞，不存在归属谎言。
- SINGLE 语义复用 V2：SINGLE 的指令契约就是 coder 契约（见 host 的
  解析 seam），因此 SINGLE 执行器取该 runtime 上 coder 声明 agent 的
  adapter；每 (runtime, coder) 恰一 owner 由 V3.0-C 塌缩拒绝保证。
- attribution = compat_address -> agent_id 只活在组合层，供未来
  Observation consumer 做 post-hoc 归属；V2 执行栈（packet / ledger /
  ExecutionEvent）继续只看到 runtime identity、compat 地址与 adapter。
  D1/D2 Observation 契约零修改。

NOT implemented here（later V3 phases）：discovery、capability
verification、trust、health probe、admission policy 重实现、role
selection、orchestration、remote、A2A、persistence、UI、CLI 接线。
"""
from __future__ import annotations

from typing import Any, Mapping

from agent_composition import compose_agent_slots
from collaboration_orchestrator import CollaborationOrchestrator
from collaboration_session import CollaborationSession
from host import HostFacade, _ParsedPacketAdapter
from remote_transport import LoopbackRemoteTransport
from runtime_status import RuntimeStatus
from task_budget import BudgetUsage, TaskBudget
from loop_guard import LoopGuard
from verified_orchestrator import VerifiedOrchestrator
from verified_runtime_pool import AdmissionKind, VerifiedRuntimePool
from verified_selection_bridge import agent_id_for

# The closed collaboration-role vocabulary the production facade routes
# (address-role spelling). A selected team must cover all four across its
# agents — compose_agent_slots enforces that honestly.
_COLLABORATION_ROLES = ("architect", "coder", "tester", "reviewer")

# 声明角色 -> V2 capability 的必要投影（封闭词表）。值与
# verified_stage_selector._ROLE_REQUIREMENTS 的需求 capability 逐一对应
# （tester/reviewer 是地址拼写，stage 名拼写为 test/review）——测试锁定
# 两处词表不得漂移。
_ROLE_CAPABILITY = {
    "architect": "architecture",
    "coder": "coding",
    "tester": "testing",
    "reviewer": "review",
}


def build_facade_from_agents(
    registry,
    agent_ids,
    evidence: Mapping[tuple, Any],
    current_health: Mapping[str, RuntimeStatus],
    *,
    timeout_seconds: float = 300.0,
    budget: TaskBudget | None = None,
    usage: BudgetUsage | None = None,
    loop_guard: LoopGuard | None = None,
):
    """Compose the frozen V2 execution stack around an agent team.

    输入：registry（只读消费，内容不变）；agent_ids（调用方已选定的队
    伍——选择策略不在本层）；evidence（runtime identity ->
    CandidateValidationResult，须来自既有 qualification 路径）；
    current_health（runtime_id -> RuntimeStatus 快照，调用方注入）。
    输出：(facade, attribution)——未修改的 ProductionFacade 与
    compat_address -> agent_id 的组合层归属记录。

    诚实拒绝（组合期，先于任何 facade 构造；封闭 message，不携带
    secret/prompt/output）：
    - 队伍未覆盖四角色 / 未知 agent / 塌缩组合 —— 由 V3.0-C 原样上抛
    - 缺 qualification evidence / 缺 current_health / 证据 identity 与
      键不符 / 混合 provenance —— 输入完整性错误
    - V2 admission 拒绝（如声明角色缺乏对应 capability 证据）——
      RuntimeError 如实携带机器的裁决理由，绝不代补、绝不静默丢弃
    """
    slots = compose_agent_slots(registry, tuple(agent_ids),
                                _COLLABORATION_ROLES)

    by_runtime = {}
    for slot in slots:
        by_runtime.setdefault(tuple(slot.runtime_identity), []).append(slot)

    provenances = set()
    pool = VerifiedRuntimePool(clock=lambda: 0.0)
    for identity in sorted(by_runtime):
        runtime_slots = by_runtime[identity]
        validation = evidence.get(identity)
        if validation is None:
            raise ValueError(
                f"no qualification evidence for runtime: {identity[0]}")
        if tuple(validation.identity) != identity:
            raise ValueError(
                f"evidence identity mismatch for runtime: {identity[0]}")
        if identity[0] not in current_health:
            raise ValueError(
                f"no current health for runtime: {identity[0]}")
        # 能力一致性不变量（双向）：准入能力集 = 该 runtime 上声明角色
        # 的投影。声明了某角色而证据缺少该 capability 时，admission 机
        # 器裁决并在此原样上抛 —— 本模块绝不补齐、绝不放宽。
        capabilities = tuple(sorted(
            {_ROLE_CAPABILITY[slot.role] for slot in runtime_slots}))
        outcome = pool.admit(validation, capabilities, health_now="READY")
        if outcome.kind is not AdmissionKind.ACCEPTED:
            reason = outcome.reason.value if outcome.reason is not None \
                else outcome.kind.value
            raise RuntimeError(
                f"admission rejected for runtime {identity[0]}: {reason}")
        # 反方向：冻结 bridge 按 validation.validated_capabilities 选择
        # 候选（admission 能力集不进入选择），证据中未被声明的协作
        # capability 会让该 runtime 被选中扮演无声明者的角色 —— 命中
        # 未注册地址，无法诚实执行，组合期拒绝。
        extra = sorted(
            frozenset(validation.validated_capabilities)
            & frozenset(_ROLE_CAPABILITY.values())
            - frozenset(capabilities))
        if extra:
            raise ValueError(
                f"runtime {identity[0]} has validated collaboration "
                f"capabilities declared by no selected agent: {extra}")
        provenances.add(getattr(validation, "provenance", "OFFLINE"))
    if len(provenances) != 1:
        raise ValueError(
            f"mixed qualification provenance across team: "
            f"{sorted(provenances)}")
    provenance = provenances.pop()

    budget = budget or TaskBudget(4, 4, timeout_seconds=timeout_seconds)
    usage = usage or BudgetUsage()
    guard = loop_guard or LoopGuard()

    # 地址键 adapter 字典：slot.address 与冻结选择器将为同一 (runtime,
    # role) 推导的地址字节一致（V3.0-A 投影锁定），因此角色选择落在哪个
    # runtime，地址就落在该 runtime 上声明该角色的 agent 的 adapter 上
    # —— 组合期无选择，执行期无缺洞。
    collab_adapters = {slot.address: slot.adapter for slot in slots
                       if slot.role in ("architect", "coder")}
    verification_adapters = {slot.address: slot.adapter for slot in slots
                             if slot.role in ("tester", "reviewer")}

    # SINGLE 执行器：V2 SINGLE 的指令契约就是 coder 契约，故每个
    # coding-capability runtime 的执行器是其 coder 声明 agent 的
    # adapter，经 host 的既有解析 seam 包装（每 (runtime, coder) 恰一
    # owner，由 V3.0-C 塌缩拒绝保证）。
    executors = {}
    for identity in sorted(by_runtime):
        coder_slot = next((slot for slot in by_runtime[identity]
                           if slot.role == "coder"), None)
        if coder_slot is not None:
            executors[agent_id_for(identity)] = \
                _ParsedPacketAdapter(coder_slot.adapter)

    verified_orchestrator = VerifiedOrchestrator(
        pool, current_health, executors, budget, usage, guard)

    def session_factory():
        return CollaborationSession(
            LoopbackRemoteTransport(),
            dict(collab_adapters), budget, usage, guard)

    orchestrator = CollaborationOrchestrator(
        verified_orchestrator, pool, current_health,
        budget, usage, guard, session_factory)
    facade = HostFacade(
        orchestrator,
        dict(verification_adapters),
        pool, dict(current_health), budget, usage, guard)
    facade._evidence_provenance = provenance

    attribution = {slot.address: slot.agent_id for slot in slots}
    return facade, attribution
