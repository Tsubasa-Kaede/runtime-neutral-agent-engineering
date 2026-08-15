# Dual-Agent Development Skill V2：性能与可用性最小升级设计

> V2 的第一优先级不是增加 Multi-Agent 能力，而是减少不必要的 Multi-Agent 行为。
>
> 决策顺序：能规则解决就本地处理；否则能一个 Agent 解决就走 Single-Agent；只有确实需要时才调用 Architect 或 Review。

## 目标与范围

V2 在 V1 的 provider-neutral 路由、适配器契约、结构化 packet 和安全边界之上，解决已观察到的过度编排风险：简单任务可能进入多 Agent 流程，Context 范围不可见，Provider Discovery 可能重复，调用、迭代和超时缺少统一预算。

本设计只定义最小架构和行为契约，不包含实现代码、真实 Provider 集成或 V1 协议模板重写。

已确认的产品原则：

- 能一个 Agent 完成，就不使用两个。
- 能一次调用完成，就不使用两次。
- 只传递任务所需的最小 Context。
- 可缓存的 Discovery 不重复探测。
- 结构化 Handoff 优先于长篇自然语言交接。
- 可用 Fast Path 时不进入完整 Multi-Agent Pipeline。

## 1. 模式与任务分类

### 运行模式

- `AUTO`：默认模式。先运行本地规则分类；只有规则无法判断时才允许一次轻量分类调用。随后由 Router 决定是否需要额外角色。
- `ON`：强制启用 Dual-Agent Orchestrator，但不强制启动全部角色；仍由 Router 根据分类、需求、能力和预算决定实际需要哪些 Agent。
- `OFF`：绕过 Dual-Agent Orchestrator，由宿主直接处理任务；不运行分类、Discovery、Architect 或 Review。

### 分类

基础分类：

- `SIMPLE`：单文件、单函数、小配置、参数校验、简单 Bug、简单测试、简单 CRUD 或格式调整。
- `MEDIUM`：跨越少量文件、需要一定设计判断，或存在可验证风险但不需要完整架构设计。
- `COMPLEX`：跨模块架构变化、多个独立工作流、复杂迁移、长期调试或明确需要独立设计与 Review。

辅助类型：`ARCHITECTURE_ONLY`、`DEBUG`、`REVIEW`。辅助类型不能绕过模式 Gate 或预算保护。

分类策略：

1. 明显 SIMPLE / MEDIUM / COMPLEX 由本地、可测试规则判定。
2. 规则无法判断时，AUTO/ON 最多调用一次轻量分类 Agent；这次调用计入整个任务生命周期的 `max_agent_calls`，不能隐藏在 Router 内部。
3. 分类 Agent 只接收任务文本、用户明确的文件范围、约束和验收条件，不接收整个仓库或完整历史。
4. 分类结果必须是结构化数据。分类 Agent 仍返回 `UNRESOLVED` 时，不再启动任何新的分类调用；Controller 按保守的单 Agent Path 处理，并报告不确定性。
5. 分类调用前，Controller 必须为“分类 + 预期后续实现”预留整个生命周期预算。规则明确的 SIMPLE 任务预算为 1；只有发生分类调用的歧义任务，预算才可显式设为至少 2，并把分类调用和实现调用都计入其中。
6. 分类 Agent 仍返回 `UNRESOLVED` 时，不再启动新的分类调用；按保守的单实现 Agent Path 继续，且该实现调用必须占用剩余预算。若没有剩余预算则停止并报告不确定性。
7. 任何分类调用都不能隐藏在 Router 内部，也不能把分类调用从 `total_agent_calls` 中排除。

## 2. Fast Path 与路由

### SIMPLE

```text
User Task → Mode Gate → Rule Classifier → SIMPLE
→ Capability Router → 一个 Agent → Minimal Context
→ 实现 → Minimal Verification → Done
```

默认上限：

- Architect：0
- Review：0
- `max_agent_calls`：1（整个任务生命周期）
- `max_iterations`：1
- Context：任务、必要约束、相关文件/代码、验收条件

对于明确由规则判定的 SIMPLE，`max_agent_calls` 固定为 1。若规则无法判断并实际调用了 Classification Agent，Controller 必须在调用前把预算显式设为至少 2；分类调用和实现调用共同计入总数，不能隐藏或重置。只有用户明确要求 Review，或最小验证暴露真实风险时，才允许升级；升级必须记录结构化原因并受整个任务生命周期的剩余预算约束。

### MEDIUM

```text
User Task → Classifier → MEDIUM → Capability Router → Coder
→ Tests → 成功则 Done；存在真实未决风险时才 Review 或 Architect escalation
```

Router 必须分别判断 Architect 和 Review 是否必要。默认不调用 Architect，不默认调用 Review。

### COMPLEX

```text
User Task → Classifier → COMPLEX → Architect 一次 Plan
→ Coder → Test → 一次 Review
→ PASS：Done；真实 Finding：Coder 修复
```

再次 Architect 仅在以下条件之一成立时允许：架构冲突、多次实现失败、新需求改变架构、Coder 请求升级，或验证证明确认原设计有问题。任何再次调用都必须增加 `escalation_count`，并受 `max_iterations` 约束。

## 3. 最小架构组件

### 3.1 Task Classifier

职责：把任务映射为分类、辅助类型、任务需求和置信度；不读取项目，不调用 Provider Discovery，不负责 Agent 选择。

建议边界：`scripts/task_classifier.py`。

### 3.2 Capability Router

职责：根据任务需求和候选 Agent Profile 选择 Agent。Profile 至少包含：

- `reasoning`
- `planning`
- `architecture`
- `coding`
- `debugging`
- `tool_use`
- `context_handling`
- `latency`
- `cost_efficiency`
- `reliability`
- availability evidence
- historical performance evidence

Router 不按 Provider、模型名称或固定角色绑定选择；它按任务需求、能力、可用性、成本、延迟、可靠性和历史表现计算候选结果。未知能力或未验证可用性是硬门槛，不得猜测。

建议边界：`scripts/capability_router.py`，或在 V2 第一增量中作为 `dual_agent.py` 的小型扩展；不得新增通用 Workflow Engine。

### 3.3 Context Budget

职责：根据任务范围选择并限制传给 Agent 的 Context。

输入只包括：任务、用户明确约束、相关文件、相关代码片段、必要 Architecture Summary、验收条件。默认排除整个项目、全部历史、无关文件、完整思考过程、重复自然语言交接和完整 Review Packet。

建议边界：`scripts/context_budget.py`。

### 3.4 Discovery Cache

职责：缓存 Adapter Discovery 结果，记录配置指纹、TTL、状态、失败原因、命中次数和实际探测次数。

仅在以下条件下重新 Discovery：配置变化、Provider 已不可用、TTL 到期或用户主动刷新。

建议边界：`scripts/discovery_cache.py`。它不改变 Provider-neutral Adapter 接口，不执行 Agent invocation。

### 3.5 Budget / Loop Guard

职责：统一保护 Token、Context、Agent calls、Iterations、Timeout 和估算成本，并阻止重复任务循环。

`TaskBudget` 属于整个 Task 生命周期，不属于单个阶段或单个 Agent。它至少包含：

```json
{
  "max_agent_calls": 1,
  "max_iterations": 1,
  "max_context_tokens_per_call": 16000,
  "max_total_input_tokens": 16000,
  "max_total_output_tokens": 8000,
  "timeout_seconds": 120,
  "estimated_cost": null
}
```

运行时累计计数至少包含：

- `total_agent_calls`
- `architect_calls`
- `coder_calls`
- `review_calls`
- `classification_calls`
- `iterations_used`
- `escalation_count`
- `visited_tasks`
- repeated failure signatures
- `total_input_tokens`
- `total_output_tokens`
- timeout deadline

`max_agent_calls` 是整个任务生命周期的硬保护边界。比如 Architect → Coder → Review → Coder Fix 计为 4 次，而不是按阶段分别重置。分类调用同样计入总数。

发现重复任务、重复失败签名或超预算时停止新增调用，优先压缩 Context、移除重复历史和跳过非必要 Review，最终报告未完成原因。

## 4. 动态 Agent Profile 与路由

候选 Profile 不写死 GPT、DeepSeek、Claude 或角色到模型的映射。每个 Profile 提供能力数值、可用性、成本、延迟和可靠性证据。Router 先执行安全和可用性硬门控，再进行偏好排序。

排序考虑：

1. 任务必需能力是否满足。
2. Provider 是否可用且证据已验证。
3. 可靠性是否达到任务下限。
4. 预计延迟和成本是否适合当前 Budget。
5. 历史成功率、调用时长和 Token 表现。
6. 平分时使用稳定的 adapter ID 作为 deterministic tie-break。

未验证的成本、延迟或能力必须保持未知，不得用模型名推断。

## 5. Context 流程

### 初始结构

```json
{
  "task_id": "...",
  "task_type": "SIMPLE",
  "files": ["relevant/file.py"],
  "requirements": ["..."],
  "constraints": ["..."],
  "acceptance_criteria": ["..."]
}
```

文件选择：

- 用户点名文件时只读取这些文件。
- 未点名时先做局部索引或定向搜索，再读取命中的必要片段。
- 不默认读取全项目、所有参考资料或完整历史。
- 重复内容只保留一个权威来源和引用。

超出 Context Budget 时按顺序：删除重复历史、删除已完成阶段叙述、移除无关文件、把大工具输出替换为摘要和 file:line 引用、保留任务/约束/相关代码/验收条件；仍超限则停止额外调用并报告。

## 6. Structured Handoff

### Architect → Coder

```json
{
  "task_id": "...",
  "status": "ready",
  "task_type": "COMPLEX",
  "files": ["..."],
  "requirements": ["..."],
  "constraints": ["..."],
  "acceptance_criteria": ["..."],
  "architecture_decisions": ["..."],
  "open_risks": ["..."]
}
```

不传完整思考过程、无关探索记录、整个项目快照或已由文件覆盖的长篇说明。

### Coder → Controller

```json
{
  "status": "success",
  "changed_files": ["..."],
  "tests": ["..."],
  "build": "passed",
  "issues": [],
  "needs_escalation": false
}
```

### Reviewer → Controller

```json
{
  "status": "PASS",
  "findings": [],
  "evidence_refs": ["..."],
  "needs_coder_fix": false
}
```

Reviewer 只返回结构化结果，不启动新的 Reviewer、Architect 或 Coder。

## 7. Architect 与 Review 策略

Architect 是受 Router 授权的稀缺阶段，不是 Skill 的默认入口。SIMPLE 永远默认跳过；MEDIUM 仅在架构风险证据成立时进入；COMPLEX 最多一次初始 Plan。

Review 也由任务分类和验证证据控制：SIMPLE 默认跳过；MEDIUM 仅在真实未决风险、用户明确要求或验证结果不足时进入；COMPLEX 默认一次。Review 发现真实问题才允许进入 Coder 修复，不能以“再确认一次”为理由重复 Review。

## 8. Budget 流程

每个任务建立一个生命周期级 `TaskBudget`。下列数字是 SIMPLE 的默认示例；MEDIUM/COMPLEX 必须由 Controller 在任务开始时显式设置更高的生命周期预算，不能按阶段隐式重置：

```json
{
  "max_agent_calls": 1,
  "max_iterations": 1,
  "max_context_tokens_per_call": 16000,
  "max_total_input_tokens": 16000,
  "max_total_output_tokens": 8000,
  "timeout_seconds": 120,
  "estimated_cost": null
}
```

其中：

- `max_context_tokens_per_call`：单次传给 Agent 的 Context 上限。
- `max_total_input_tokens`：整个任务生命周期累计输入 Token 上限，包含每次调用传入的 Context。
- `max_total_output_tokens`：整个任务生命周期累计输出 Token 上限。
- `max_agent_calls`：整个任务生命周期所有 Agent 调用的总上限，包含分类调用、Architect、Coder、Review 和 Coder Fix。
- `max_iterations`：整个任务生命周期的修复/升级轮次上限。
- `timeout_seconds`：任务生命周期墙钟上限；单次 Adapter invocation 也必须有自己的超时边界。
- `estimated_cost`：未知时保持 `null`，不伪造估算。

Budget 是保护机制而非僵硬拒绝机制。超预算处理顺序：压缩 Context、删除重复历史、删除无关文件、禁止额外 Review、减少 Agent 调用、必要时停止并报告。任何预算扩大必须由 Controller 基于新证据记录原因，不能由 Agent 自行扩大。

## 9. Discovery Cache 流程

```text
请求 Discovery
  ↓
配置指纹 + provider id 查缓存
  ├─ 命中且 TTL 有效 → 返回缓存结果
  └─ 未命中/失效 → 执行一次受限探测并写入缓存
```

Provider 标记不可用时允许立即重新探测一次以确认恢复；连续失败由 Loop Guard 限制。用户刷新绕过 TTL，但仍受单任务调用预算保护。

## 10. 状态摘要

默认只输出：

```text
Mode: AUTO
Task: SIMPLE
Selected Agent: Coder
Architect: skipped
Review: skipped
Context: minimal
Reason: task can be completed by one agent
```

详细 Trace 进入结构化日志或 Controller evidence，不默认塞入 Agent Context。

## 11. V1 文件变更边界

建议修改：

- `dual-agent-development/SKILL.md`：增加 Mode Gate、分类优先、Fast Path、Context 和预算原则。
- `dual-agent-development/references/workflow.md`：增加分类、路由上限、升级条件、Handoff 和 Loop Protection 契约。
- `dual-agent-development/scripts/dual_agent.py`：最小扩展分类结果、Mode、Budget、调用观测和统一返回边界；保持 Router 核心 provider-neutral。
- `dual-agent-development/scripts/adapter_probe.py`：只接入 Discovery Cache 边界和结构化探测元数据，不加入分类或 Prompt 逻辑。
- `tests/test_router.py`：覆盖三类任务的调用上限、Mode、升级、预算和循环保护。
- `tests/test_adapter_contract.py`：覆盖缓存命中、TTL、配置指纹、不可用重探测和刷新。

可按角色契约需要小幅修改：

- `agents/architect.md`：强调仅在 Router 授权后调用，一次 Plan 优先。
- `agents/coder.md`：强调 SIMPLE/MEDIUM 独立完成优先和结构化返回。
- `agents/reviewer.md`：强调只在 Router 授权后调用，不主动发起新 Agent。

不应修改：

- `templates/architecture-packet.json`
- `templates/review-packet.json`
- `agents/openai.yaml`
- 已验证的协议校验器和 Provider 安全契约
- 与路由、预算、Context、Discovery 无关的工具文件

## 12. 新增文件边界

建议新增：

- `scripts/task_classifier.py`：规则优先分类和歧义结果。
- `scripts/context_budget.py`：最小文件/片段选择和 Context 限制。
- `scripts/discovery_cache.py`：TTL、配置指纹和 Discovery 统计。

`capability_router.py` 和独立 `budget.py` 不作为第一增量的必需文件：如果其接口可以由现有 `dual_agent.py` 小幅承载，则先不拆文件，避免 V2 产生新的分层和包装成本。只有当 `dual_agent.py` 因此超过清晰边界，才拆出模块。

## 13. 测试与验收

所有测试保持离线、可重复、无真实 Provider 副作用。新增验收必须证明：

- 明确 SIMPLE 任务一次调用、无 Architect、无 Review；规则无法判断的任务若调用分类 Agent，分类调用也必须计入整个任务生命周期的总调用数。
- MEDIUM 不自动启用完整流水线，只有风险证据触发升级。
- COMPLEX 的默认序列为 Architect → Coder → Test → 一次 Review；Coder Fix 仍计入同一个生命周期总调用数。
- AUTO、ON、OFF 的行为差异清晰且可测试。
- Handoff 只包含结构化字段和必要引用。
- Context 预算超限会压缩或停止，不会静默加载整个项目。
- Discovery 在缓存有效时不重复探测，失效条件可观测。
- `max_iterations`、`visited_tasks`、`escalation_count` 和重复失败检测阻止 A → B → A → B 循环。
- 任务级累计计数能够区分 `total_agent_calls`、`classification_calls`、`architect_calls`、`coder_calls`、`review_calls`、`total_input_tokens` 和 `total_output_tokens`。
- 每次 Agent 调用、Context 大小、耗时和结果都可被 Controller 记录。

## 14. 反过度工程约束

V2 不新增 Planner Agent、通用 Workflow Engine、数据库、远程队列、完整 Provider invocation 层或每能力一个 Agent。SIMPLE 不创建额外分类 Agent；MEDIUM 不默认创建 Architect/Reviewer；结构化 Handoff 只传必要字段；预算和缓存以纯逻辑实现。每个新模块必须对应上一阶段已确认的真实缺口，并能由离线测试证明，否则不纳入 V2。
