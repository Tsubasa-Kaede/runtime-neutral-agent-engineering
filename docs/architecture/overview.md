# 架构总览（Architecture Overview）

> V2 引擎架构的入口。事实之源是代码；本文档是它的地图。最后对照
> `198fbe9` 验证（2026-08-25）。

## 引擎是什么

一个位于编码 agent CLI 之上的 Runtime 中立编排层。它按顺序回答以下
问题，且绝不模糊层与层的边界：

1. **存在哪些 runtime？**（Discovery）
2. **哪些现在健康？**（Health）
3. **它们证明了什么能力？**（Qualification / Verification）
4. **谁执行哪个角色？**（Selection）
5. **工作如何在角色之间流动？**（Collaboration / Execution）

## 完整流水线

```text
 Task
  ↓
 Classification (task_classifier)     SIMPLE / MEDIUM / COMPLEX / UNRESOLVED
  ↓
 Mode Gate (mode_gate)                OFF / AUTO / ON
  ↓
 Runtime Discovery (runtime_discovery)         DISCOVERED / NOT_FOUND
  ↓
 Health (runtime_health, generic_runtime_health)   READY / AUTH_REQUIRED / UNAVAILABLE / ERROR
  ↓
 Capability evidence (G14, candidate_validation)   architecture / coding / review / testing
  ↓
 Qualification (real_validation_executor, G1–G14)  gated, opt-in
  ↓
 Verification                          VERIFIED + REAL
  ↓
 Admission (verified_runtime_pool)     VERIFIED + REAL + capability subset + READY
  ↓
 Selection (verified_selection_bridge, verified_stage_selector)
  ↓
 Execution (execution_engine / collaboration stack)
  ↓
 Result (production_facade → closed FacadeResult)
```

这条流水线表示一个任务从进入引擎到产出封闭结果的完整受控生命周期：
分类与模式门决定"走不走编排"，随后是挣取事实的准入链 —— 存在、健康、
能力证据、门控 qualification、VERIFIED+REAL、pool 准入 —— 然后才是
选择、执行与协作，最后由 facade 投影出封闭结果。

## 边界（五个 ≠）

| 区分 | 在哪里被强制 |
|---|---|
| Discovery ≠ Health | `runtime_discovery.py` 只回答存在性；DISCOVERED 候选不经 Health 流水线绝不会变成 READY |
| Health ≠ Qualification | `runtime_status.py` 的状态只关于 Health；任何健康值都不产生证据 |
| Qualification ≠ Verification | 一次运行产出 `CandidateValidationResult`；只有完整通过的运行才是 `VERIFIED`（短路为 BLOCKED/FAILED/NOT_VERIFIED） |
| Verification ≠ Capability | 准入与角色选择以子集方式检查 `validated_capabilities`；VERIFIED 但缺少所需能力依然不能承担角色 |
| Runtime ≠ Agent | `AgentProfile`/地址区分可路由的 agent 身份与其 runtime 基底；`agent_id_for(identity)` 是投影，不是替代 |

外加两条 provenance 规则：**READY ≠ VERIFIED** 与 **OFFLINE ≠ REAL**
（见 [runtime-lifecycle.md](runtime-lifecycle.md)）。

## 模块地图

### 准入链（谁可用）

| 模块 | 职责 |
|---|---|
| `runtime_adapter_registry.py` | 中立描述符注册；桥接到 discovery；绝不探测 |
| `runtime_discovery.py` | 基于 adapter 的"仅存在性"候选发现 |
| `runtime_health.py` / `generic_runtime_health.py` | Health 流水线（auth → provider/model → 最小检查） |
| `runtime_pool.py` | Health TTL 缓存 + `ready_statuses()`（ReadyPool 快照） |
| `runtime_pool_construction.py` | discovery → health → ReadyPool/排除 的构建 |
| `candidate_validation.py` | gate 模型 + validation runner（纯数据与编排） |
| `real_validation_executor.py` | 真实 adapter 之上的 G1–G14 gate 执行器（opt-in 门控） |
| `verified_runtime_pool.py` | validation 结果之上的准入边界 |
| `discovery_bootstrap.py` | Registry → Discovery → Health → 证据复用/qualification → pool |
| `verified_selection_bridge.py` | verified pool + health → 角色候选集（设计上无分数） |
| `verified_stage_selector.py` | verified 候选集之上的 SINGLE/MULTI 决策 |
| `role_candidates.py` / `stage_runtime_selection.py` / `selection_plan_bridge.py` | ReadyPool 路径的对应物 |

### 执行（工作如何运行）

| 模块 | 职责 |
|---|---|
| `task_classifier.py` / `mode_gate.py` | 路由决策 |
| `orchestrator.py` | 经典单路径规划器（ReadyPool 选择） |
| `verified_orchestrator.py` | verified 路径的规划+执行；绝不借用 ReadyPool |
| `execution_engine.py` | gate 链 Health → Guard → Handoff → Budget → Reserve → Invoke |
| `fallback_policy.py` | ReadyPool 路径 fallback（verified 路径注入空的） |
| `invocation_plan.py` | 规划输出契约 |
| `task_budget.py` / `loop_guard.py` | 生命周期核算；重复/环保护 |

### 协作（角色如何交接）

| 模块 | 职责 |
|---|---|
| `structured_packets.py` | 四个业务 packet（wire contract） |
| `collaboration_packet.py` | agent 到 agent 的信封（协议契约） |
| `local_transport.py` / `remote_transport.py` | 投递机制（进程内 / 边界契约+loopback） |
| `collaboration_session.py` | transport 之上的一次 architect → coder → architect 运行 |
| `verification_collaboration.py` | ledger 支撑的 tester → reviewer |
| `collaboration_state.py` | append-only 共享 ledger |
| `collaboration_handoff.py` | ledger 之上的只读交接投影 |
| `collaboration_orchestrator.py` | SINGLE/DUAL 路由进入协作栈 |
| `production_facade.py` / `host.py` / `cli.py` | 入口与组合 |

### Adapter（runtime）

| 模块 | 职责 |
|---|---|
| `external_runtime.py` | 中立的 request/trace/result 契约 |
| `external_agent_adapter.py` | 最小 adapter 协议 |
| `claude_code_adapter.py` | Claude Code CLI adapter |
| `tiny_agents_adapter.py` | tiny-agents CLI adapter |
| `adapter_probe.py` | 有界的可执行文件探测（仅存在性） |
| `candidate_adapter_contract.py` | adapter → 候选实例 桥 |
| `mock_adapter.py` | 离线测试 adapter |

### 横切

`content_safety.py`（唯一扫描权威）、`handoff_context.py`（引擎 packet
交接）、`dual_agent.py`（遗留路由/评审核心）、`runtime_integration.py`
（经典桥）、`logging_utils.py`。

## 入口

- **生产**：`host.build_facade(adapter, validation, health)`（手动）与
  `host.build_facade_from_bootstrap(registry, ...)`（自动：Registry →
  bootstrap → facade）。一个 facade = 一个任务生命周期。
- **CLI**：`dual-agent run --mode off|auto|on "<task>"` —— 渲染封闭的
  `FacadeResult`；绝不自行构建 facade。

## 去哪里读

- [runtime-lifecycle.md](runtime-lifecycle.md) —— 深入状态词汇
- [ready-vs-verified.md](ready-vs-verified.md) —— 双路径与不借用不变量
- [execution.md](execution.md) —— 执行 gate 链
- [collaboration.md](collaboration.md) —— packet、契约、transport、session
