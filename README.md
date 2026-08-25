# Dual-Agent Development（V2）

一个 **Runtime 中立的 agent 协作编排器**：给定一个任务，它对工作进行分类、
路由，并经由最多四个结构化协作阶段 —— **architect → coder → tester →
reviewer** —— 运行，全程使用结构化 packet 交接、append-only 共享 ledger、
单一任务生命周期预算、环保护与诚实的失败上报。

它**不是聊天机器人**，也不是模型提供商。它是位于你已有的编码 agent CLI
之上的编排层：它发现 runtime、检查其 Health、以经过验证的能力（绝不以
名字）对其进行 qualification，并通过已验证的契约协调它们的工作。

---

## 1. 项目定位（Overview）

引擎从六个正交的输入决定如何执行一个任务：

- **任务复杂度** —— 确定性分类（SIMPLE / MEDIUM / COMPLEX / UNRESOLVED）
- **Runtime Health** —— 某个 runtime 现在 是否 READY
- **Verified Capability** —— 一个 runtime *已经证明*自己能做什么
- **Mode** —— OFF / AUTO / ON，调用方意图
- **Budget** —— 每个任务一个生命周期内封闭的核算
- **LoopGuard** —— 在任何花费之前的 重复/环/升级 保护

链路中的每个决策都是结构化、分类化、诚实的：没有任何东西被包装成成功，
没有任何静默 fallback，任何一层都不宣称属于另一层的答案。

## 2. Core Architecture（核心架构）

```text
 Task
  ↓
 Classification (SIMPLE / MEDIUM / COMPLEX / UNRESOLVED)
  ↓
 Runtime Discovery        ── does the runtime exist?
  ↓
 Health                   ── is it READY right now?
  ↓
 Capability               ── what has it PROVEN it can do?
  ↓
 Qualification (G1–G14)   ── one sanctioned validation run
  ↓
 Verification             ── VERIFIED + REAL evidence
  ↓
 Selection                ── pick agents from the Verified Pool
  ↓
 Execution                ── architect → coder → tester → reviewer
  ↓
 Result                   ── closed, secret-free summary
```

这条链路表示一个任务从分类到最终执行的受控生命周期：先确认 runtime
存在（Discovery），再确认它此刻可用（Health），再确认它已证明的能力
（Capability），经过唯一一次受门控的 Qualification 运行（G1–G14）得到
VERIFIED + REAL 证据，才能进入 Verified Pool 参与选择与执行。

引擎绝不模糊的五条边界：

| 区分 | 含义 |
|---|---|
| Discovery ≠ Health | 被找到的 runtime 可能并不健康；DISCOVERED 候选绝不会被当作 READY |
| Health ≠ Qualification | READY 对 validation 证据不做任何断言 |
| Qualification ≠ Verification | qualification 运行产生一个结果；VERIFIED 是一次完整通过运行的结局 |
| Verification ≠ Capability | VERIFIED 但缺少所需能力集合，依然不能承担角色 |
| Runtime ≠ Agent | runtime 是执行基底；agent 是寄宿其上的可路由身份 |

深入阅读：[`docs/architecture/overview.md`](docs/architecture/overview.md)。

## 3. Runtime Lifecycle（Runtime 生命周期）

三套独立的词汇，三个独立的归属层：

**Discovery**（`runtime_discovery.py`）—— 只回答"存在与否"：

| 状态 | 含义 |
|---|---|
| `DISCOVERED` | 通过其 adapter 找到了该 runtime |
| `NOT_FOUND` | 缺席，或 discovery 出错（受控结果，绝不抛异常） |

**Health**（`runtime_status.py`）—— 检查时刻的 task-level 状态：

| 状态 | 含义 |
|---|---|
| `READY` | 通过 discovery + auth + provider/model + 最小 Health 检查 |
| `AUTH_REQUIRED` | 身份缺失或被拒 |
| `UNAVAILABLE` | 无法启动 / 可执行文件缺失 |
| `ERROR` | 已启动但行为异常 |

（`UNKNOWN` 属于 *authentication* 词汇，不在 RuntimeState 中。）

**Validation**（`candidate_validation.py`）—— 准入 qualification：

| 状态 | 含义 |
|---|---|
| `VERIFIED` | 全部 14 个 gate 通过；能力证据已收集 |
| `BLOCKED` | 缺少外部条件（gate 关闭、auth 缺席） |
| `FAILED` | 在某个具体 gate 上暴露出集成缺陷 |
| `NOT_VERIFIED` | 运行在完成前被短路 |

**READY ≠ VERIFIED。** Health 是可续期的状态；qualification 是挣来的
证据。两者互不隐含，任一方向都不行。

深入阅读：[`docs/architecture/runtime-lifecycle.md`](docs/architecture/runtime-lifecycle.md)。

## 4. ReadyPool Path 与 Verified Path

两条平行路径并存；运行哪一条取决于入口：

```text
ReadyPool path (classic engine)              Verified path (production stack)
─────────────────────────────                ────────────────────────────────
Runtime                                      Runtime
 → Health                                     → Discovery
 → Capability (registry evidence)             → Health
 → ReadyPool (runtime_pool)                   → Qualification (G1–G14, gated)
 → CapabilityRegistry selection               → Verification (VERIFIED + REAL)
 → ExecutionEngine                            → VerifiedRuntimePool admission
                                              → Verified selection (score-less)
                                              → Execution (never falls back)
```

左列是经典引擎路径：Health 通过即可进入 ReadyPool，由 CapabilityRegistry
打分选择；右列是生产路径：必须经过门控的 G1–G14 qualification 取得
VERIFIED + REAL 证据、被 VerifiedRuntimePool 准入后才执行，且绝不 fallback。

承重不变量：**Verified Path 绝不静默借用 ReadyPool。** 在代码中这是结构
性的 —— `VerifiedOrchestrator` 将空的 verified 选择归一化为
`NO_CAPABLE_AGENT`，而不是去咨询 ready-pool registry，并以**空的
fallback 策略**执行。

Pool 准入（经 RC-3 bootstrap 组合）要求 **VERIFIED + REAL provenance**
（外加所需能力子集 + READY Health）。仅 `VERIFIED`、或 `OFFLINE` 证据，
一律拒绝。

深入阅读：[`docs/architecture/ready-vs-verified.md`](docs/architecture/ready-vs-verified.md)。

## 5. Provenance：OFFLINE 与 REAL

每个 validation 结果都携带 `provenance`：

- `OFFLINE` —— 由 mock/注入 executor 产生；可用于契约验证，但**不是**
  真实能力的证据。
- `REAL` —— 只在显式的真实验证 gate（`RUN_REAL_PROVIDER_TESTS=1`）之下、
  带真实调用证据地产生。

离线（Offline）验证不等于真实（REAL）验证。引擎在结构上阻止两者被互换：
runner 拒绝在没有真实调用证据时给 `REAL`；一次 REAL qualification 即可将
runtime 准入 pool 并服务多个任务（绝不按任务重复 qualification）。

## 6. Capability（能力）

能力（`architecture`、`coding`、`review`、`testing`）描述一个
agent/runtime 能为某个角色做什么 —— 它**不是** Health，也**不是**
verification：

- Capability ≠ Health：一个 READY 的 runtime 可以没有任何 verified 能力。
- Capability ≠ Verification：`VERIFIED` 但缺少所需能力集合，依然不能承担
  角色（准入检查的是子集）。

`validated_capabilities` **只**由结构化的 gate 证据（G14 的四个角色实验）
构建；候选*声明*的能力上下文绝不会被晋升进去。在证据层级中，`DECLARED`
永远不算 `VERIFIED`。

## 7. Security Boundaries（安全边界）

- **无秘密契约**：原始输出、秘密与模型推理绝不进入 packet、ledger、
  trace 或公开结果；`content_safety` 是唯一的扫描权威（值中的凭据形态、
  结构上的 marker key）。
- **原始输出隔离**：一个阶段的输入永远是上游 *packet*；原始调用输出必须
  先通过 packet 契约与内容扫描解析，才能到达下一个阶段。
- **Protected paths**：REAL validation 对调用方声明的受保护文件
  （凭据/配置）做快照，运行期间任何变更都会使 G13 失败。
- **最小环境**：CLI adapter 以白名单 env（`PATH`/`HOME`/`USERPROFILE`/
  `SYSTEMROOT`）启动子进程 —— 携带凭据的变量绝不转发。
- **安全的错误归一化**：adapter 错误文本在到达 trace 或报告之前先做形态
  抹除（`_safe_error` / `sanitize_trace`）。
- 引擎绝不读取、存储、打印或修改凭据，绝不登录或登出，绝不触碰 runtime
  配置。
- 真实 runtime 调用是 opt-in 且默认关闭的（`RUN_REAL_PROVIDER_TESTS=1`
  门控真实测试）。
- CLI 输出只是封闭的 allow-list 摘要。

## 8. Budget 与 LoopGuard

两道守卫都运行在任何调用**之前** —— 这正是关键：被拒绝的重复或已耗尽的
预算绝不能消耗金钱或调用。

- **TaskBudget** —— 一个预算跨越一个任务生命周期。预留是
  reserve-before-invoke：调用名额在 adapter 调用之前被预留（耗尽即抛出
  异常），因此一次调用要么已被支付、要么从未发生。Token 计数默认为诚实
  的 `"unknown"`，绝不猜测。
- **LoopGuard** —— 一个 guard 跨越一个任务。`check()` 是预检
  （DUPLICATE_TASK / REPEATED_FAILURE / CYCLE_DETECTED / 上限），
  `record()` 在调用之后补全配对。被记忆的只有 hash 后的失败*类别*，
  绝无原始诊断。

## 9. Collaboration（协作）

四阶段链路通过 packet 通信，绝不传递原始输出：

```text
Architect → ArchitecturePacket → CollaborationPacket → Transport → Coder → …
```

这条链路表示：architect 的设计被封装为 ArchitecturePacket，再由
CollaborationPacket（协议信封）经 Transport 送达 coder，逐阶段传递 ——
每次交接的都是已验证的结构化契约，而不是聊天文本。

| 角色 | 读取 | 产出 |
|---|---|---|
| architect | 任务本身 | `ArchitecturePacket` |
| coder | architecture packet 的 wire 文本 | `ImplementationPacket` |
| tester | 最新 implementation packet | `TestPacket` |
| reviewer | architecture + implementation + test | `ReviewPacket` |

两个重要的区分：

- **`CollaborationPacket` 是协议契约**（在冻结的信封 schema 上，谁欠谁
  什么工作）。**Transport 是投递机制**（今天是进程内邮箱）。它们是不同的
  层。
- 当前的 transport **不是** Remote Agent Network。remote transport 模块
  以 loopback 实现定义了边界契约；V2 中不存在任何远程 peer。

## 10. REAL Runtime Validation（经验证的事实）

以下为实测事实，不是愿景（记录于 2026-08-25，commit `198fbe9`，
本机）：

- **Claude Code CLI**（2.1.227，first-party 登录）：完整链路已被 REAL
  证明 —— Discovery（FOUND）→ Health（READY）→ REAL Qualification →
  `VERIFIED` + `REAL`，含全部 4 项能力 → Verified Pool 准入。
  测试：`tests/test_rc3_real_discovery.py`（门控）。
- **证据复用**：同一台机器上的第二个 bootstrap session，携带第一个
  session 的证据，不再重复 qualification（`qualification_count = 0`）。
- **G13 protected paths**：证明运行的每次真实调用中，5 个受保护文件
  （凭据/配置）均为 `diff = 0`。
- **离线基线**：`python -m pytest tests/ -q` →
  **942 passed / 21 skipped / 377 subtests**（21 个 skip 是 opt-in 的
  REAL 门控测试）。该数字随测试增加而变化；套件输出本身才是权威。
- **Codex CLI**：本环境未安装 —— 不做任何断言。
- **tiny-agents**：可执行文件在场但未配置（`TINY_AGENTS_AGENT_PATH` /
  `TINY_AGENTS_COMMAND` 未设置）→ 不可注册；诚实地缺席，而非"失败"。

## 11. RC-3 Status（RC-3 状态）

| 任务 | 范围 | 状态 |
|---|---|---|
| A | runtime adapter registry | ✅（`7bfdece`） |
| B | discovery bootstrap 组合 | ✅（`7bfdece` / `55546be`） |
| C | Host/CLI 离线接线（`build_facade_from_bootstrap`） | ✅（`55546be`） |
| D | REAL 本地 runtime discovery 证明 | ✅（`198fbe9`） |

**RC-3 Task D：COMPLETED。**

## 12. V2 → V3 Roadmap（未实现）

- **V2（当前）**：可靠的 agent/runtime 编排 —— 本 README 的一切。V2 通过
  挣取事实的链路（Discovery → Health → Qualification → VERIFIED+REAL →
  Admission）回答*"哪个 runtime 可以执行这个任务？"*。
- **V3（未来）**：以 agent 为中心的协作基础设施 —— 它问的是*"哪个 agent
  最适合接这个任务？"*，runtime 降级为 agent 的一种执行能力。远程协作、
  multi-agent network、跨机器 transport。

分阶段设计目标（完整细节：[docs/roadmap/v2-to-v3.md](docs/roadmap/v2-to-v3.md)）：

| 阶段 | 主题 | 状态 |
|---|---|---|
| V3.0 | Agent Foundation —— identity、manifest、discovery、capability、contract、verification、trust、admission | NOT IMPLEMENTED |
| V3.1 | Remote Collaboration —— 远程 agent、基于 artifact 的交换、context 隔离、authN/authZ | NOT IMPLEMENTED |
| V3.2 | Multi-Agent Orchestration —— 任务分解、调度、工作流、失败恢复 | NOT IMPLEMENTED |
| V3.5+ | Agent Network —— pool、动态选择、reputation、marketplace | NOT IMPLEMENTED |

V3 能力**尚未构建**。当前代码库中没有任何东西实现远程 peer、A2A 协议或
multi-agent network；不要把 remote-transport *契约*读成已部署的网络 ——
`remote_transport.py` 是一个仅有 **loopback 实现**的边界契约。V3 是在
V2 之上的演进（contract-first、verification-first、minimal-context
transfer —— roadmap 文档列出了全部十条继承原则），而不是对它的推翻。

## 13. Development（开发）

### 安装

要求 Python 3.10+（仅标准库）：

```bash
git clone <repo-url>
cd dual-agent-development-repo
pip install -e .
```

这将安装 `dual_agent` 包（从 `dual-agent-development/scripts/` 映射）、
`dual-agent` console script，以及 package data 区域下的 skill 资产
（`SKILL.md`、`references/`、`templates/`、`agents/`、`examples/`）。

### 快速开始（离线，无需 runtime）

```bash
python examples/offline_mock_run.py
```

预期输出 —— 一份封闭、无秘密的 JSON 摘要：

```json
{"path": "FOUR_STAGE", "status": "SUCCESS", "stages": ["architect","coder","tester","reviewer"], ...}
```

### CLI

```bash
dual-agent run --mode off  "实现 GitHub Webhook"
dual-agent run --mode auto "实现 GitHub Webhook"
dual-agent run --mode on   "实现 GitHub Webhook"
```

**诚实的限制**：CLI 解析参数、调用被注入（injected）的 facade 并打印
封闭的 JSON 摘要
（status、mode、path、stages、失败类别、阶段计数）。`ProductionFacade`
必须**由 host 应用配置并注入** —— CLI 绝不自行创建 runtime、adapter、
凭据或默认 facade，也绝不会自动配置 provider 或读取 API key。没有注入
facade 时它会以明确的错误退出。Host 应用这样注入：

```python
from dual_agent import cli
cli.main._facade = my_configured_facade   # 用你的 adapters/pool 构建
```

facade 如何由真实引擎组件构建，见 `examples/offline_mock_run.py`。

### Modes（模式）

| 模式 | 行为 |
|---|---|
| `off` | 不编排；返回被委托的空结果 |
| `auto` | 对任务分类；SIMPLE/MEDIUM/UNRESOLVED 走单 agent 路径，COMPLEX 走双 agent 路径 |
| `on` | 强制双 agent 路径（architect+coder；存在合格候选时再跑 tester+reviewer） |

没有 verified tester/reviewer 候选时的双 agent 成功会上报为
`NO_VERIFICATION_CAPABILITY` —— 绝不是静默的两阶段成功，也绝不是伪造的
四阶段成功。

### 失败语义

失败是结构化且终态的；上游失败后下游阶段不再运行。没有任何东西被包装成
成功，也没有任何静默 fallback：

- `*_INVOKE_FAILED`、`*_PACKET_INVALID` —— 某阶段在 runtime 或 packet
  契约上失败
- `MISSING_HANDOFF` —— ledger 中缺少必需的上游 packet
- `BUDGET_EXHAUSTED`、`LOOP_GUARD_REJECTED` —— 任务生命周期守卫
- `NO_VERIFICATION_CAPABILITY` —— 没有 verified 的 tester/reviewer 候选

SINGLE 路径上，封闭的 CLI 摘要上报粗粒度 `FAILED` 类别；细粒度 reason
（budget/guard/handoff）保留在引擎的结构化 errors 中，供直接消费
`ExecutionResult` 的 host 使用。

诚实的重试需要新的 `task_id`；loop guard 会拒绝同一任务同一阶段的
重跑。

### 任务生命周期（一个 facade = 一个任务）

```text
Task 1 → Facade 1 → done        Task 2 → Facade 2 → done
```

图示含义：每个任务拥有自己的 facade 及其 budget/guard/ledger；facade
之间互不共享，也不在运行之间重置。

一个 `ProductionFacade` 拥有**一个**任务生命周期 —— 它的 budget、guard
与 ledger 都是按任务的，且不在运行之间重置：

- **SINGLE 生命周期**：最多 1 次真实 agent 调用（一次 coder 调用）。
- **FOUR_STAGE 生命周期**：最多 4 次（architect、coder、tester、
  reviewer —— 各恰好一次）。超出预算上报 `BUDGET_EXHAUSTED`；新任务需要
  新 facade（每任务 `host.build_facade`）。
- **Budget**：`TaskBudget(4, 4)` 覆盖整个任务；SINGLE 路径消耗 4 中的
  1，四阶段路径消耗全部 4。
- **Qualification 证据**：一次受认可的 REAL qualification 结果即可将
  runtime 准入 pool 并服务多个 facade/任务（用同一 validation 结果构建
  facade）；绝不按任务重复 qualification。
- **Qualification ≠ 稳定性**：G14 qualification 一次性证明 runtime 的
  角色能力；FOUR_STAGE 稳定性是在 N 次独立运行上单独测量的。N=10 的
  测量是一个样本，不是长期稳定性保证；一次 SINGLE 成功也不意味着所有
  简单任务都稳定。

### Runtime 中立设计

引擎中任何地方都没有硬编码 runtime、provider 或模型名。具体 adapter
（例如 Claude Code CLI adapter）是 adapter 契约
（`references/adapter-contract.md`）的个别实现 —— Claude Code 是"一个"
adapter，不是"那个" runtime。新增一个 runtime 意味着实现 adapter 协议，
而不是修改 orchestrator。

### 测试

```bash
python -m pytest tests/ -q                        # 完整离线套件 + 门控 skip
python -m unittest discover -s tests              # 等价的 unittest 运行器
python -m compileall dual-agent-development       # 语法门
```

引擎是纯标准库。V2 的每个阶段都是测试先行构建的。

### 故障排查

| 症状 | 原因 / 处置 |
|---|---|
| `{"error": "no facade configured"}` | 没有 host 注入 facade 时的预期行为；配置一个（见 CLI 一节） |
| 一切都报 `NO_VERIFICATION_CAPABILITY` / 无候选 | 尚无 runtime 拥有 verified 能力证据；runtime 必须先通过门控 validation 链才能被选择 |
| 重试时报 `LOOP_GUARD_REJECTED` | 同一任务 + 同一阶段已运行过；使用新的 `task_id` |
| 测试出现门控 skip | REAL-runtime 测试条目是 opt-in 的；设置 `RUN_REAL_PROVIDER_TESTS=1` 运行它们（它们会调用真实 runtime） |

## 14. Documentation（文档树）

```text
docs/
├── architecture/
│   ├── overview.md           # 完整架构与模块地图
│   ├── runtime-lifecycle.md  # Discovery / Health / Qualification / Verification / Admission
│   ├── ready-vs-verified.md  # 双路径与"不得静默借用"不变量
│   ├── execution.md          # Health→Guard→Handoff→Budget→Reserve→Invoke gate 链
│   └── collaboration.md      # packet、契约、transport、session、交接
├── development/
    ├── getting-started.md    # 结构、环境、安装、首次测试
    ├── development-guide.md  # contract-first、boundary-first 工作流
    ├── testing.md            # unit / integration / E2E / OFFLINE 与 REAL
    └── real-runtime.md       # 门控的 Registry→…→Admission 链与 RC-3 证明
└── roadmap/
    └── v2-to-v3.md           # V3 设计目标 —— agent 为中心的演进（未实现）
```

Skill 面向资产：`dual-agent-development/SKILL.md`、
`references/adapter-contract.md`、`references/workflow.md`、`templates/`。

## 15. Known Limitations（已知限制）

- 当前机器上只有**一个 runtime**（Claude Code CLI）拥有 REAL 证明的能力
  证据；其它 adapter 存在但在本机未经证明。
- 任务分类是一张**封闭的关键词表**，不是模型 —— 无关键词命中的任务分类
  为 UNRESOLVED 并走编排路径。
- 双路径覆盖 **architect+coder**；tester+reviewer 作为验证阶段运行，以
  双 agent 成功为门（否则 `NO_VERIFICATION_CAPABILITY`）。
- Qualification 是时点证明；**N 次运行的稳定性是单独的测量**（抽样，
  不保证）。
- V2 中不存在远程协作（见 §12）。
