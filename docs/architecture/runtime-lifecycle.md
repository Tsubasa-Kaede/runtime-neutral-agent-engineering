# Runtime 生命周期（Runtime Lifecycle）

> 三套状态词汇、各自的归属层、它们暗示 —— 与不暗示 —— 什么。事实之源：
> `runtime_discovery.py`、`runtime_status.py`、`candidate_validation.py`、
> `verified_runtime_pool.py`、`discovery_bootstrap.py`。

## 层级分离

```text
Discovery          Health                Qualification/Verification       Admission
(existence)        (task-level state)    (earned evidence)                (pool entry)

DISCOVERED ────►  READY ──────────────►  VERIFIED  ─┐
NOT_FOUND         AUTH_REQUIRED           BLOCKED    ├─ VERIFIED + REAL + caps + READY
                  UNAVAILABLE             FAILED     │   → VerifiedRuntimePool
                  ERROR                   NOT_VERIFIED ┘
```

图中箭头表示：runtime 首先通过 Discovery 被发现，随后进入 task-level
Health 检查，再经由一次 Qualification 挣得 VERIFIED 证据，最终满足全部
条件才被 VerifiedRuntimePool 准入。**任何箭头都不可逆，任何层都不向
回隐含另一层。** 健康失效的 pool 条目不会被重新 qualification；失去
discovery 的 runtime 也不会被静默保留。

## 1. Discovery —— 只回答存在性

归属：`runtime_discovery.py`（`DiscoveryState`）。

| 状态 | 含义 |
|---|---|
| `DISCOVERED` | adapter 的 `discover()` 回答了 `available=True` |
| `NOT_FOUND` | 缺席，或 discovery 抛出异常（受控结果 —— adapter 异常变成 reason，绝不崩溃） |

Discovery 回答的是**可找到性**，仅此而已：

- DISCOVERED ≠ READY：被找到的 runtime 可能已损坏、未认证或无法启动。
- Discovery 不携带任何能力或质量信号。`version` 字段仅为信息。
- 新 runtime 通过 `DiscoverySource` 注册接入；本模块绝不按 runtime 名称
  分支。

## 2. Health —— 检查时刻的 task-level 状态

归属：`runtime_status.py`（`RuntimeState`）；流水线在
`runtime_health.py` / `generic_runtime_health.py`。

| 状态 | 含义 |
|---|---|
| `READY` | 通过 discovery + authentication + provider/model 检查 + 最小 Health 检查 |
| `AUTH_REQUIRED` | 身份缺失或被拒 |
| `UNAVAILABLE` | 无法启动 / 可执行文件未找到 |
| `ERROR` | 已启动但行为异常（协议违规、最小检查失败……） |

性质：

- **READY 是可续期的快照，不是凭据。** `RuntimeStatus` 携带
  `checked_at`/`expires_at`；超过 TTL 的状态即过期，必须重新检查，
  绝不重用。
- **READY ≠ VERIFIED。** READY 对 qualification 证据或 pool 准入不做
  任何断言 —— 任一方向都不行。
- `AUTHENTICATION` 有自己的子词汇（`AUTHENTICATED`、`AUTH_REQUIRED`、
  `REJECTED`、`UNKNOWN`）；`UNKNOWN` 属于那里，不在 `RuntimeState` 中。
- 证据是分类化的（`HealthEvidence`："verified"/"failed"/…）；runtime
  原始输出与秘密形态的值绝不进入。

## 3. Qualification / Verification —— 准入资格

归属：`candidate_validation.py`（`CandidateValidationStatus`）；真实
执行器为 `real_validation_executor.py`（G1–G14，opt-in）。

| 状态 | 含义 |
|---|---|
| `VERIFIED` | 全部 14 个 gate 通过；结构化能力证据已收集 |
| `BLOCKED` | 缺少外部条件（gate 关闭、auth 缺席）—— 不是缺陷 |
| `FAILED` | 在某个具体 gate 上暴露出集成缺陷（记录失败点） |
| `NOT_VERIFIED` | 运行在完成前短路 |

性质：

- **Qualification 是挣来的证据，不是状态。** 它由一次受认可的运行产
  生，并以元组 `(runtime_id, provider_id, model_id, config_fingerprint)`
  标识。
- 能力证据（`validated_capabilities`）**只**在完整 `VERIFIED` 的运行上
  收集；短路的运行不贡献任何证据。
- Provenance 附着在结果上：`OFFLINE`（注入 executor，契约检查）对比
  `REAL`（只在显式真实验证 gate 之下、带真实调用证据）。runner 在结构
  上拒绝没有真实调用证据的 `REAL`。**OFFLINE ≠ REAL。**

## 4. Admission —— 进入 pool

归属：`verified_runtime_pool.py`（原语）+ `discovery_bootstrap.py`
（组合）。

pool 原语（`admit`）按固定决策顺序执行：

```text
status VERIFIED → required capabilities ⊆ validated → health READY → not a duplicate → ACCEPTED
```

这行决策顺序表示：先确认 VERIFIED，再确认所需能力是已验证能力的子集，
再确认此刻 READY，最后排除重复 —— 四关全过才 ACCEPTED。RC-3 bootstrap
组合在此基础上额外要求 **provenance REAL** 才尝试准入，拒绝无效的复用
证据且不重跑（无重试语义），并按 runtime 上报结构化结果
（`NOT_QUALIFIED`、`NO_EVIDENCE_NO_QUALIFIER`、`HEALTH_<state>`、
`CAPABILITY_INSUFFICIENT`、……）。

## 情景推演（Worked transitions）

| 情景 | 诚实结果 |
|---|---|
| 两次会话之间 runtime 可执行文件被卸载 | Discovery `NOT_FOUND`；条目不被准入；记录 reason |
| 已登出 | Health `AUTH_REQUIRED`；bootstrap 条目 `HEALTH_AUTH_REQUIRED`，不准入 |
| gate 关闭时运行 qualification | G5 `BLOCKED` → 结果 `BLOCKED`，provenance `OFFLINE`；绝不会被当作 REAL 准入 |
| VERIFIED 但缺少一项所需能力 | `CAPABILITY_INSUFFICIENT`；不准入 |
| VERIFIED + REAL + caps + READY | 准入；身份进入 pool；可跨 facade/任务复用 |

## 相关阅读

- [ready-vs-verified.md](ready-vs-verified.md) —— 健康快照池与证据池
  各自供给什么
- [../development/real-runtime.md](../development/real-runtime.md) ——
  为真实地运行门控 qualification 链
