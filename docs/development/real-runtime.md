# REAL Runtime 验证（REAL Runtime Validation）

> 从一个已注册 runtime 到一个被准入、REAL 验证的 pool 条目的门控链 ——
> 以及在真实机器上实际证明了的事实。事实之源：
> `runtime_adapter_registry.py`、`discovery_bootstrap.py`、
> `real_validation_executor.py`、`candidate_validation.py`、
> `verified_runtime_pool.py`、`tests/test_rc3_real_discovery.py`。

## 链路

```text
Registry (AdapterDescriptor per installed runtime)
  ↓ discovery_sources()
Discovery (RuntimeCandidateDiscovery)          DISCOVERED / NOT_FOUND
  ↓
Health (GenericRuntimeHealth → health pipeline)   READY / AUTH_REQUIRED / UNAVAILABLE / ERROR
  ↓ evidence lookup by identity (reuse) | exactly one qualification
Qualification (run_real_validation, G1–G14)     gated on RUN_REAL_PROVIDER_TESTS=1
  ↓
Verification                                    VERIFIED + provenance REAL
  ↓ capability subset ⊆ validated_capabilities, health READY
Admission (VerifiedRuntimePool)                 ADMITTED
```

这条链路表示：注册表为每个已安装 runtime 提供描述符，经发现与 Health
检查后，要么按 identity 复用既有证据、要么恰好运行一次门控
qualification；只有 VERIFIED + REAL、能力子集满足且 READY 的候选才会
被 pool 准入。

组合归属：`bootstrap_runtime_session`（discovery_bootstrap）。它绝不
自己启动进程，且只准入 **VERIFIED + REAL** 证据；被复用的无效证据会被
拒绝且不重跑（无重试语义）。

## 14 个 gate（一次 qualification 运行）

| Gate | 检查内容 | 类型 |
|---|---|---|
| G1 Discovery | runtime 可被发现 | 只读探测 |
| G2 Authentication | auth 状态为 authenticated | 只读探测 |
| G3 Provider | provider 检查可用 | 只读探测 |
| G4 Model | model 可解析（允许默认） | 只读探测 |
| G5 Minimal invocation | 一次真实的 `Return exactly OK` 调用 | **唯一的门控真实调用** |
| G6 Exit code | trace exit code 为 0 | 由 G5 派生 |
| G7 Timeout | 时长在界内 | 派生 |
| G8 Cancel | cancellation 契约在场、无孤儿进程 | 派生 |
| G9 Process cleanup | 进程被回收 | 派生 |
| G10 Invocation result | 结构化结果完整 | 派生 |
| G11 Structured output | 输出恰为 `OK` | 派生 |
| G12 Security | 对摘要表面做秘密形态扫描 | 审计 |
| G13 Configuration integrity | 受保护路径快照未变 | 审计 |
| G14 Capability evidence | 四个最小的真实角色实验（architect、coder、tester、reviewer）；只有当角色的真实输出经标准边界解析进其 packet 时，该能力才被计入 | 真实实验 |

gate 以固定顺序运行并确定性短路（BLOCKED / FAILED / NOT_RUN）；能力
证据**只**在完整 VERIFIED 的运行上收集。`REAL` provenance 只在 gate
打开且带真实调用证据时被赋予 —— 构造上无法伪造。

## 运行方式

```bash
# RC-3 Task D 证明（discovery → health → qualification → admission + 复用）
RUN_REAL_PROVIDER_TESTS=1 python -m pytest tests/test_rc3_real_discovery.py -v -s

# 单 runtime gate 冒烟
RUN_REAL_PROVIDER_TESTS=1 python -m pytest tests/test_real_runtime_validation.py -v
```

预计耗时数分钟：每次 qualification 包含一次 health 最小调用、G5 最小
调用与四个 G14 角色实验，外加复用 session 的 health 检查。输出是每个
session 一份封闭、无秘密的 JSON 摘要。

## 任何 REAL 运行的安全规则

1. gate 环境变量是 opt-in 且默认关闭；绝不写进共享配置或 CI 默认值。
2. 声明受保护路径（凭据/配置文件）让 G13 监视它们；运行后确认
   protected diff = 0。
3. 只打印封闭摘要 —— 绝不打印 prompt、原始输出、环境内容或凭据
   材料。
4. 依赖机器的分支诚实断言（例如非 READY health → 不准入、提前返回）；
   绝不硬编码一个机器状态。
5. 一个 runtime 的证明只是那个 runtime 的证明 —— 不在 runtime 或机器
   之间泛化证据。

## 已验证事实（RC-3 Task D，commit `198fbe9`，2026-08-25）

在该次运行中于本机实测 —— 是事实，不是愿景：

- **Claude Code CLI**（`claude` 2.1.227，first-party 登录）：完整链路
  REAL 证明 —— Discovery `FOUND` → Health `READY` → 一次 REAL
  qualification（timeout 300 s，受保护路径被监视）→ `VERIFIED` +
  `REAL`，含全部 4 项能力（`architecture`、`coding`、`review`、
  `testing`）→ 以身份 `("claude-cli", "anthropic", None, "installed")`
  准入 pool。
- **证据复用**：同一台机器上的第二个 bootstrap session，以第一个
  session 的证据为种子，报告 `qualification_count = 0` 并保住已准入的
  pool —— qualification 按 identity，不按任务。
- **受保护路径**：该证明的每次真实调用中，5 个受监视文件
  （凭据/配置）均 `diff = 0`。
- **该 commit 的离线基线**：942 passed / 21 门控 skip / 377 subtests
  （套件输出随测试演进是权威）。
- **当时的机器事实**：codex CLI 未安装；tiny-agents 可执行文件在场
  但未配置（`TINY_AGENTS_AGENT_PATH` / `TINY_AGENTS_COMMAND` 未设置
  → 诚实地未注册）。

在另一台机器上重跑或环境变化后，请从测试输出重新推导这些事实 ——
不要照抄。
