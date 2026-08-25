# ReadyPool Path 与 Verified Path

> 引擎中存在两条平行的选择路径。本文档绘制两者、说明谁使用哪条，并钉死
> 承重不变量：**Verified Path 绝不静默借用 ReadyPool。**

## 并排对照

```text
ReadyPool path (classic engine)                Verified path (production stack)
──────────────────────────────────             ─────────────────────────────────
Runtime                                        Runtime
 → Health (runtime_health pipeline)             → Discovery (runtime_discovery)
 → CapabilityRegistry evidence                 → Health (same pipeline)
 → ReadyPool (runtime_pool.ready_statuses)     → Qualification (G1–G14, gated)
 → CapabilityRegistry.select (scored)          → Verification (VERIFIED + REAL)
 → StageRuntimeSelector / DualAgentSelection   → VerifiedRuntimePool admission
 → InvocationPlan                              → VerifiedSelectionBridge (score-less)
 → ExecutionEngine (real FallbackPolicy)       → VerifiedStageSelector
                                               → ExecutionEngine (EMPTY FallbackPolicy)
```

左右两列的差别在"成员资格从哪里来"：左列以 Health 快照与 registry 证据
打分选择，右列必须先挣得 VERIFIED + REAL 证据并被 pool 准入，选择时刻意
不打分，执行时注入空 fallback 策略。

## 每条路径各自回答什么

- **ReadyPool** 回答*"谁现在健康、按 registry 证据打分如何？"* —— 成员
  资格是 TTL 约束的健康快照；选择使用确定性的 registry 分数
  （capability 0.6 / confidence 0.25 / readiness 0.1 / history 0.05）。
- **Verified path** 回答*"谁拥有这些角色的、经证明的 REAL 资格？"* ——
  成员资格是挣来的证据（准入时 `VERIFIED` + `REAL` + 能力子集）；选择
  刻意**无分数**（按契约 `VerifiedRoleCandidate.score` 为 None；排序即
  identity 排序）。

两个 pool 互不隐含：

- ReadyPool 成员资格不是证据，且随 health TTL 过期。
- Verified pool 成员资格对*当前*健康不做断言 —— 这正是 verified
  selection bridge 在投影候选之前要重新检查注入的健康快照的原因。

## 谁使用哪条

| 入口 | 路径 |
|---|---|
| `runtime_integration.RuntimeIntegration`（经典/示例） | ReadyPool |
| `orchestrator.Orchestrator`（经典规划器） | ReadyPool |
| `verified_orchestrator.VerifiedOrchestrator` | Verified |
| `host.build_facade` / `build_facade_from_bootstrap` | Verified（SINGLE 执行器）+ 协作栈（verified 角色候选） |
| `production_facade` 验证阶段（tester/reviewer） | Verified（`VerifiedSelectionBridge`） |

## 不变量：不得静默借用

verified 路径绝不能悄悄退回 ready-pool 选择，原因有二：未经 qualification
的 runtime 将承担它从未挣得的角色；本应诚实暴露的失败
（`NO_CAPABLE_AGENT`）将被掩盖。

强制是结构性的，落在三处：

1. **Reason 归一化**（`verified_orchestrator.plan`）：空的 verified 选择
   上报为 `NO_CAPABLE_AGENT` —— verified 路径绝不咨询 ready-pool
   registry 来填补缺口。
2. **空 fallback 策略**（`verified_orchestrator.execute`）：引擎以
   `FallbackPolicy(())` 构造，因此调用失败暴露为 `NO_FALLBACK_AGENT`，
   而不是改在未验证的 peer 上重跑。
3. **无分数投影**（`verified_selection_bridge`）：verified 候选按契约携带
   `score=None`，因此永远不可能混入（或混淆于）registry 的打分候选。

## 准入（verified pool，经 bootstrap 组合）

```text
G1–G14 complete pass ─► VERIFIED
 + provenance REAL            (bootstrap refuses OFFLINE evidence)
 + required capabilities ⊆ validated_capabilities
 + health READY (at admission)
 + identity not already in pool
 ─────────────────────────────► ADMITTED
```

这段准入条件自上而下表示：完整通过 14 个 gate 得到 VERIFIED，再叠加
REAL provenance（bootstrap 拒绝 OFFLINE 证据）、所需能力是已验证能力的
子集、准入时刻 READY、身份不在 pool 中 —— 全部满足才 ADMITTED。

证据复用：身份匹配时，先前准入的 validation 结果原样复用；无效证据被
拒绝且**不重跑**（无重试语义）。因此一次 REAL qualification 可以服务
多个 facade/任务 —— RC-3 证明实测第二个 session 为
`qualification_count = 0`。

## 各路径的失败诚实性

| 路径 | 失败面 |
|---|---|
| ReadyPool | 无合格 agent 时 `NO_CAPABLE_AGENT`；调用失败时一次 fallback 尝试（`FallbackPolicy`） |
| Verified | `NO_CAPABLE_AGENT`（由 `EMPTY_SELECTION` 归一化）；调用失败时 `NO_FALLBACK_AGENT`；tester/reviewer 候选缺失时 facade 报 `NO_VERIFICATION_CAPABILITY` |

## 相关阅读

- [runtime-lifecycle.md](runtime-lifecycle.md) —— 两个 pool 背后的
  状态词汇
- [execution.md](execution.md) —— 空 fallback 与真实 fallback 策略
  在哪里生效
