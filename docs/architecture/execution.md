# 执行：gate 链（Execution）

> 一个已规划的阶段如何变成（或诚实地未能变成）一次调用。事实之源：
> `execution_engine.py`、`task_budget.py`、`loop_guard.py`、
> `handoff_context.py`；协作栈执行同样的纪律。

## 固定的起飞前顺序

`ExecutionEngine.execute` 中的每个阶段按序通过：

```text
Health → LoopGuard → Handoff → Budget → Reserve → Invoke → Accept
```

| # | Gate | 失败词汇 | 为什么在这个位置 |
|---|---|---|---|
| 1 | **Health**（执行时刻重新检查） | `RUNTIME_NOT_READY` | planning 时的健康可能已失效；绝不信任陈旧快照 |
| 2 | **LoopGuard**（`check`） | `LOOP_GUARD <verdict>` | guard 在任何花费**之前**被咨询 —— 被拒绝的重复/环绝不消耗预算或调用 |
| 3 | **Handoff**（`input_for`） | `MISSING_HANDOFF` | 阶段的输入是上游 packet；事实缺席是诚实的终止，绝不是静默的空输入 |
| 4+5 | **Budget**（`reserve_call`） | `BUDGET_EXHAUSTED` | **reserve-before-invoke**：名额严格在 adapter 调用之前被预留（耗尽即抛出） |
| 6 | **Invoke**（adapter） | `INVOKE_FAILED` | 唯一花钱的步骤；只在已预留的名额上运行 |
| 7 | **Accept**（`accept` → packet 验证） | `PACKET_VALIDATION_FAILED` | 输出只有在解析进该阶段的 packet 契约后才进入任务 context |

第一个失败的 gate 以其结构化 reason 终止该阶段；不跳过任何 gate，
没有任何东西被包装成成功。

## 为什么顺序本身是承重的

- **先检查后花费**：guard（与 health）的拒绝是免费的。如果 budget 排在
  前面，一个循环调用的调用方会在 guard 制止它之前先榨干预算。
- **Reserve-before-invoke**：一次调用要么已被支付、要么从未发生。上游
  gate 失败绝不消耗预算，每次调用在发生的那一刻即被核算 —— 而不是事后
  对账。
- **先验证后存储**：阶段的原始输出绝不直接成为另一个阶段的输入。
  `HandoffContext.accept` 是进入任务 packet 状态的唯一入口。

## Fallback 边界

一次失败的调用通过注入的 `FallbackPolicy` 获得**恰好一次**备用尝试：

- ReadyPool 路径：真实策略选择一个 READY、有能力的 peer（capability
  0.9 / history 0.1，确定性平局裁决）—— 附带一次全新预留。
- Verified 路径：orchestrator 注入**空**策略，因此失败暴露为
  `NO_FALLBACK_AGENT`。verified 候选绝不 fallback 到未验证的 peer
  （见 [ready-vs-verified.md](ready-vs-verified.md)）。
- 备用失败即终态（`FALLBACK_FAILED`）；引擎绝不级联 fallback。

## Budget 与 guard 语义（两个栈共享）

- 一个 `TaskBudget` 跨越一个任务生命周期：`SINGLE` ≤ 1 次调用，
  `FOUR_STAGE` 恰好 4 次（每角色一次）。Token 计数默认为诚实的
  `"unknown"`；只有观测到的非负整数才累计。
- 一个 `LoopGuard` 跨越一个任务。check/record 配对：`check()` 是咨询性
  预检；调用后的 `record()` 使同一 `(task, stage, agent)` 的重跑成为
  `DUPLICATE_TASK`。当候选事件已位于历史尾部往前数第 4 个位置时触发
  `CYCLE_DETECTED`（周期 4 的振荡）。失败记忆只存**失败类别的 hash**
  —— 原始诊断绝不持久化。
- 协作 session 与验证协作在每个角色阶段（`architect`、`coder`、
  `test`、`review`）执行同样的 reserve/check 纪律，共享 facade 构建时
  所用的同一批实例。

## 规划时的门控（咨询性）

两个 orchestrator 在*规划*时也会检查 budget/guard；那些检查只是咨询性
的 —— 它们塑造 plan（`BUDGET_EXHAUSTED` / `LOOP_GUARD_REJECTED`
reason），但不做任何预留或记录。预留只属于引擎，每次调用恰好一次。

## 结果契约

`ExecutionResult` 是封闭且结构化的：粗粒度 `SUCCESS`/`FAILED` 状态、
trace（已清洗）、outputs、结构化错误字符串、已验证的 packets。细粒度
reason 存放在 `errors` 中供 host 使用；CLI 只投影粗粒度类别。
