# V2 → V3 Roadmap：从 Runtime 为中心到 Agent 为中心

> **状态：ARCHITECTURE / PLANNING ONLY（仅架构与规划）。** 本文档中
> 没有任何东西已被实现。下文每个 V3 条目都是设计目标。对照 V2 的
> commit `198fbe9`（2026-08-25）锚定；凡引用 V2 事实之处，以代码为
> 权威。

## 1. V2 现状（CURRENT，已建成）

V2 是一个 **以 Runtime 为中心的 Dual-Agent Development Skill**：它回答

> *"哪个 Runtime 可以执行这个任务？"*

并且通过一条由挣来的、不可互换的事实组成的链路来回答：

| V2 能力 | 模块 | 它确立什么 |
|---|---|---|
| Discovery | `runtime_discovery.py`, `runtime_adapter_registry.py` | 一个 runtime 存在 |
| Health | `runtime_health.py`, `generic_runtime_health.py`, `runtime_status.py` | 它现在 READY（task-level、TTL） |
| Capability | `real_validation_executor.py` 中的 G14 | 它已经证明了什么能力 |
| Qualification | `candidate_validation.py`, `real_validation_executor.py` | 一次受认可的 G1–G14 运行 |
| Verification | `CandidateValidationStatus.VERIFIED` + REAL provenance | 这次运行真的通过了 |
| Selection | `verified_selection_bridge.py`, `verified_stage_selector.py` | 从 verified pool 无分数地选角色 |
| Budget | `task_budget.py` | 一个生命周期，reserve-before-invoke |
| LoopGuard | `loop_guard.py` | 重复/环/升级保护 |
| Collaboration | `collaboration_*`, `structured_packets.py`, `*_transport.py` | 契约驱动的四阶段交接 |
| Facade / CLI | `production_facade.py`, `host.py`, `cli.py` | 封闭、诚实的入口 |

在 V2 中，"agent"是 runtime identity 的角色限定投影
（`agent_id_for(identity)` / `collab_agent_address(identity, role)`）
—— runtime 是一等公民；agent 是它上面的一个地址。

## 2. V3 转变（FUTURE，未来）

V3 逐步转而回答另一个问题：

> *"哪个 Agent 最适合接这个任务？"*

**Agent** 成为一等公民，沿九个维度描述：

```text
Agent Identity        agent 是谁（稳定、可验证）
Agent Role            它能扮演什么角色（architect/coder/tester/reviewer/…）
Agent Capability      它已经证明了什么能力
Agent Runtime         底层执行基底之一（未来可能有多个）
Agent Health          它当前的 task-level 状态
Agent Verification    它的 qualification 证据（VERIFIED + REAL）
Agent Trust           挣来的、策略限定的信任 —— 绝不默认给予
Agent Budget          它在一个任务（或任务图）预算下的核算
Agent Transport       工作如何到达它（今天本地，V3.1+ 远程）
```

**Runtime 降级为 Agent 的一种执行能力** —— 是 agent"如何执行"的
细节，不再是 agent"是谁"。runtime 层的任何东西都不会被丢弃；它被
重新安放在 agent 层之下。

## 3. V3.0 —— Agent Foundation（仅设计目标）

| 领域 | 设计目标 | 生长自（V2 种子） |
|---|---|---|
| Agent Identity | 独立于 runtime identity 的稳定、可验证 agent 身份 | `agent_id_for` 投影；identity 元组 |
| Agent Manifest | 一份声明的、带签名的 agent 描述：角色、能力、runtime 绑定、契约面 | `AdapterDescriptor`；`references/adapter-contract.md` |
| Agent Discovery | 按 manifest 发现 agent（先本地），不把"在场"与"胜任"混同 | `RuntimeCandidateDiscovery` |
| Agent Capability | 附着在 agent 上的能力证据，只由受认可的 verification 产生 | G14 / `validated_capabilities` |
| Agent Contract | agent 接受的工作契约（输入、输出、验收标准） | `CollaborationPacket` + 角色指令 |
| Agent Verification | 针对 agent 契约的 qualification 运行，带 provenance | `CandidateValidationRunner` |
| Agent Trust | 显式的信任状态，挣得且可撤销，叠在 verification 之上 | （全新 —— 不得与 VERIFIED 混同） |
| Agent Admission | 由 identity + capability + verification + trust + policy + budget 决定执行资格的准入 | `VerifiedRuntimePool` 准入契约 |

未实现。以上 agent 层构造没有任何代码存在。

## 4. V3.1 —— Remote Collaboration（仅设计目标）

- **Remote Agent** —— 可跨机器/进程边界到达的 agent。
- **Artifact-based Collaboration** —— 对等体之间交换已验证的
  artifact（packet），绝不交换对话。
- **Context Isolation** —— 每个 agent 在自己的 context 内工作；无共享
  历史（见 §8）。
- **Transport** —— 投递机制在既有边界契约之后获得真实的远程实现。
- **Session** —— 跨边界、带显式生命周期的 session。
- **Timeout / Cancellation** —— 跨边界的有界等待与显式取消语义
  （V2 的 trace 词汇已经建模了两者）。
- **Authentication / Authorization** —— 接受任何工作之前先确认对端
  身份与权限。

**明确状态：当前没有 Remote Agent Network。** V2 的
`remote_transport.py` 以仅有的 loopback 实现定义了边界*契约*；它没有
把任何东西送过任何真实边界。本节是设计目标，不是对已交付行为的描述。

## 5. V3.2 —— Multi-Agent Orchestration（仅设计目标）

- **Task Decomposition** —— 把任务拆成由角色塑形的子任务图（V2 的
  固定四阶段链是其刚性特例）。
- **Agent Scheduling** —— 在预算之内跨已准入 agent 排序并指派子任务。
- **Multi-Agent Workflow** —— 运行该任务图，每条边都是契约驱动的
  交接。
- **Failure Recovery** —— 结构化、有界的恢复（以新 id 重试、改派给
  verified peer），绝不静默降级；LoopGuard 与 provenance 纪律延伸到
  任务图。
- **Distributed Execution** —— V3.1 就绪后子任务跨边界执行。

## 6. V3.5+ —— Agent Network（仅设计目标）

- **Agent Pool** —— 一个常驻的已准入 agent 群体。
- **Dynamic Agent Selection** —— 在 trust/capability/budget 约束下按
  子任务从 pool 中选择。
- **Agent Reputation** —— 以观测结果形成的 reputation 作为准入的
  *一个*输入，绝不替代 verification。
- **Agent Marketplace / Network** —— 在策略之下跨组织边界发现并
  聘用 agent。

全部远期；除这些名字外不在此承诺任何设计。

## 7. V3 协作架构（目标形态）

```text
Controller (task owner)
     │
     ├── Agent A ── Architect
     ├── Agent B ── Coder
     ├── Agent C ── Tester
     └── Agent D ── Reviewer
```

上图表示：Controller（任务所有者）持有任务，把角色工作分派给一组
agent；每个 agent 只通过契约接收和交付工作。

工作经由契约流动，而非对话：

```text
Architect
  ↓ ArchitecturePacket
CollaborationPacket (envelope: who owes what to whom)
  ↓ Transport
Coder
  ↓ ImplementationPacket
Reviewer
```

**Contract-driven，不是 conversation-driven。** 每一跳都是带验收标准的
已验证 packet、处于显式契约之下；没有任何一跳是"聊到看起来完成为止"。
V2 在单进程内已经这样工作 —— V3 把同一纪律扩展到多个 agent 与（之后
的）边界。

## 8. Context 模型：Minimal Context Transfer（FUTURE，及 V2 先例）

未来的 agent 默认**不**共享完整对话历史。跨过一跳的内容恰好是：

- **Required context** —— 该角色所需的最小事实
- **Required artifact** —— 上游 packet（们）
- **Required contract** —— 角色指令 / schema
- **Acceptance criteria** —— 输出将如何被评判

明确避免：完整聊天历史、无界的 token 复制、无边界的 context 共享。
V2 的"每阶段一个 packet"设计是先例 —— coder 的 prompt 就是序列化的
ArchitecturePacket，是它的*完整*输入契约 —— 这正是 V3 保留的模型。

## 9. Internal Contract 与 External Protocol（FUTURE 分层）

```text
Internal Collaboration Contract      CollaborationPacket (V2, 已存在)
        ↓
Transport Adapter                    （未来）契约 → wire 协议 的映射
        ↓
Local Transport                      （V2 已存在：进程内邮箱）
Remote Transport                     （V3.1：真实远程实现）
        ↓
External Agent Protocol              （未来；A2A 是候选之一）
```

规则：

- **内部 `CollaborationPacket` 绝不绑定任何外部协议** —— 它是引擎自己
  的契约；外部协议在 Transport Adapter 层被适配到它，绝不反过来。
- **A2A 未来可以被适配**，但未实现，且本阶段明令禁止实现它。
- 更换/新增外部协议绝不允许改动 packet schema、provenance 语义或
  verification 语义。

## 10. Trust 与 Admission（FUTURE）

V3 继承 V2 最承重的分离，并再延伸一项：

```text
Discovery ≠ Health ≠ Verification ≠ Trust
```

未来的 **Agent Admission** 权衡：

| 输入 | 含义 |
|---|---|
| Identity | 可验证的"是谁" |
| Capability | 已证明的"能做什么" |
| Verification | 带 provenance 的 qualification 证据 |
| Trust | 挣来的、策略限定的、可撤销的 |
| Policy | 本部署允许什么 |
| Budget | 本任务能花什么 |

V3 必须在结构上阻止的反模式：**"agent 能调用模型"被当作"agent 可信"。**
执行能力永远不是被信任的能力；trust 是独立的、显式的、可撤销的事实。

## 11. V3 安全模型（设计清单 —— 仅记录，未构建）

Identity · Authentication · Authorization · Capability-based permission ·
Artifact isolation · Secret isolation · Audit trail · Execution policy。

V2 已提供种子：无秘密契约、最小子进程环境、安全的错误归一化、受保护
路径快照（G13）、append-only 审计 ledger。清单中除此之外的一切都是
未来设计。

## 12. Current 与 Future 的边界

| CURRENT（V2，已建成） | FUTURE（V3，未构建） |
|---|---|
| 以 Runtime 为中心的编排 | 以 Agent 为中心的协作 |
| 进程内、基于 packet 的协作 | 远程协作 |
| 单机单进程 | 多机、多 agent |
| 固定四阶段链 | 任务分解与工作流 |
| Verified pool 准入（VERIFIED+REAL） | trust/policy/budget 感知的 agent 准入 |
| 封闭、确定性的选择 | 动态选择、reputation、marketplace |
| loopback transport 契约 | 真实远程 transport、A2A 适配 |

**今日未实现 —— 以下各项绝不可在任何地方被写成现状：**
Remote Agent Network · Multi-machine Agent Collaboration · Agent
Marketplace · Agent Reputation · Distributed Agent Execution · A2A ·
Dynamic Agent Network。

`remote_transport.py` 的现状是**带 loopback 实现的契约层** —— 它不是
"远程 agent 支持"。

## 13. 与 V2 的关系：演进，不是推翻

V3 不推翻 V2。V2 建成的 runtime 层成为 agent 层站立的地基：

```text
V2 Runtime Layer (exists)
        ↓
Agent Runtime Adapter (future)      an agent's execution substrate
        ↓
Agent Identity (V3.0)
        ↓
Agent Capability (V3.0)
        ↓
Agent Verification (V3.0)
        ↓
Agent Selection (V3.0+)
        ↓
Agent Collaboration (V3.1+)
        ↓
Distributed Agent Network (V3.5+)
```

这张演进栈自下而上表示：V2 的 runtime 层在最底，经（未来的）Agent
Runtime Adapter 之上逐层长出身份、能力、验证、选择、协作，最顶端才是
远期的分布式网络 —— 每一层都站在 V2 纪律之上。

既有的 **`CollaborationPacket` 是 V3 的基础之一** —— 但今天它恰好
且仅仅是一个**协作契约**：一个由进程内 transport 搬运的冻结信封
schema。它不是远程 agent 系统，当前代码库中也没有任何东西使它成为
那样的系统。

## 14. V3 必须从 V2 继承的原则

Runtime-neutral · Contract-first · Verification-first · Minimal
Context · Secret-free collaboration · Deterministic selection ·
Budget-aware execution · LoopGuard · Provenance · Explicit trust。

以及对以上一切的统摄警告：

> **不要把"多个模型"变成"不受控的多个 agent"。**
> 每增加一个 agent 都在倍增协调面；加入网络的每项能力都必须携带
> V2 已在单个 runtime 上强制执行的同款 verification、budget、guard
> 与 trust 纪律 —— 否则网络是负债，不是基础设施。
