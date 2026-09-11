# V3.2 COMP-3 Design Spec — Fixed Sequential Multi-Agent Orchestration

- 日期：2026-09-11
- 分支：feature/dual-agent-development
- 基线：COMP-2 CLOSED @ b851bbe（parent c6c24da）
- 状态：DESIGN SPEC（未实现；ORCH-1/2/3/4 未启动）
- 前置审查：Step Binding Architecture Review（裁决 Option A：Step → opaque slot）+ RunState Architecture Decision（裁决 Model A：显式 continuation 值）

---

## 1. Problem / Motivation

V3.2 已有（COMP-1/COMP-2）：一个 execution 的单槽组合根与多槽组合面——恰一 ControlBoundary、恰一 journal、N 条独立执行链、控制四命令（Pause/Resume/Revision/Abort）execution-scoped 生效。

缺失：**顺序**。固定顺序多 agent 协作（Architect → Coder → Tester → Reviewer）目前没有任何合法编排入口；caller 只能手工循环调 slot 句柄，自担游标、park、fail-fast 语义——每个 caller 都会发明一套私有编排状态，正是三真值分离要防止的事。

COMP-3 补上这一层：**固定顺序编排 = 对不可变组合面的确定性转移函数**，零新权威、零新事实域、零运行时知识。

## 2. Scope

- 新增编排域返回契约值：`StepSpec`、`SequentialPipeline`（不可变可执行定义）、`RunState`（caller 持有的瞬态 continuation 值）、`RunOutcome`/`RunStatus`
- 新增唯一入口：`build_sequential_pipeline(execution_slots, steps) → SequentialPipeline`（构造期 slot 引用校验）
- 转移模型：`run(run_state=None) → RunOutcome`；`PARKED` 携带 `run_state`，caller 回传续走
- Admission（步前只读控制真相）、Pause→PARKED→Resume 续走、Abort 终止、Revision 经既有队列自动生效、固定 fail-fast、transcript 投影
- 预期恰 2 个新文件：`dual-agent-development/scripts/sequential_pipeline.py` + `tests/test_sequential_pipeline.py`（ORCH-2 建、ORCH-3 扩展，不拆文件）

## 3. Non-goals（本 CU 一律不做）

DAG / 条件分支 / 循环节；retry / fallback / 自动 reassignment；dynamic routing；targeted revision（指派某 step/slot）；orchestration persistence（数据库 state、跨进程 resume、durable workflow——V3.2 写死不做）；RunState 持久化；TUI / Web / CLI / remote / A2A / registry / marketplace；ControlGate 接线（admission 缝隙继续 deferred）；修改任何 V2/V3.1/COMP-1/COMP-2 冻结件；新增 control fact / observation event / identity type / runtime adapter / provider 分支；LLM-based router / autonomous planner / scheduler framework / workflow DSL / distributed queue。

## 4. Architectural Context

- **Runtime ≠ Agent**：runtime 在 slot 链尾的 raw adapter 里，编排层不可见；agent 身份是 V3.0 组合期概念，执行期内仅以 `agent_id` 元数据随 request 流动
- **Composition ≠ Orchestration**：ExecutionSlots（COMP-2）= 资源组合；SequentialPipeline（COMP-3）= 顺序 + 进度 + 汇报，零资源词汇
- **单一权威**：控制真值恒在 THE ControlBoundary；编排层零 submit、零第二 journal、零新 lifecycle 真值
- **Trace/Observation-first、UI-second**：观察恒由既有 UsageCapture/UsageLog/journal 承载；UI 将来只做 projection，可持 RunState 显示、经 boundary 发命令
- **依赖方向**：`sequential_pipeline → execution_slots`（唯一组合依赖）；runtime-specific logic 只允许存在于 V2 adapter（本层零 provider 词汇）

## 5. Concept Model

```
Task（caller 业务数据；引擎域无 Task 概念，只有 request）
  ↓ caller 供应 StepSpec 序列（每步：opaque slot 引用 + request 构造函数）
build_sequential_pipeline（构造期 slot 引用校验）
  ↓
SequentialPipeline —— immutable executable definition（零内部状态、零 cursor）
  ↓ run(run_state | None) → RunOutcome        （纯转移函数）
  ├─ 每步 admission：只读 THE boundary.pending_intent（每次现读）
  ├─ 每步执行：ExecutionSlots.slot(step.slot_id).invoke(request)
  │             ↓ COMP-2 冻结链：AbortGate → RevisionAdapter → UsageCapture → raw
  └─ RunOutcome
       ├─ COMPLETED / FAILED / ABORTED —— 终局，run_state=None，无 continuation
       └─ PARKED —— run_state=RunState（caller 持有，回传 pipeline.run(state) 续走）
```

六概念关系：

| 概念 | 域 | 形态 | 关系 |
|---|---|---|---|
| Execution | 控制 | 一个 boundary + 一个 journal + 一个 execution_id | 管线 run 的宿主 |
| Slot | 组合（COMP-2） | slot_id → 一条完整独立执行链 | step 的唯一引用目标；对编排层不透明 |
| Runtime | V2 执行 | raw adapter 之后的引擎 | slot 链内不可见；选择权在 V2/V3.0（执行前已定） |
| Agent | V3.0 身份 | manifest/binding | 执行前指派到 runtime；执行期仅 agent_id 元数据 |
| Step | 编排 | (位置, slot 引用, request 构造函数) | 零身份零权威，只有「下一个」 |
| Orchestrator | 编排 | SequentialPipeline（不可变）+ RunState（值） | 只拥有顺序、进度、汇报 |

三真值分离（RunState 不是第四套真值的证明）：

| 真值 | 归属 | 形态 | RunState 与其关系 |
|---|---|---|---|
| 控制真值（intent/命令事实） | ControlBoundary + journal | 唯一权威、恒锁 | **零缓存**：每次 admission 现读 pending_intent；RunState 不携带任何控制投影 |
| 执行观察真值（发生了什么/用量） | UsageCapture/UsageLog + journal | 唯一铸造/记录 | RunState 不铸造 invocation_id/token/duration/事件/记录；transcript 仅引用 result 已携带的字段 |
| 进度真值（走到第几步） | **RunState 值，caller 持有** | 瞬态、显式传递、内存 | 本体即是；绝不回写上两域，不进任何账本 |

RunState 只是「把这次 run 走到哪了」装进一个 caller 可持有的值——它是控制/观察/usage 三域的**读者坐标**，不是任何域的**作者**，因此不构成第四套 truth。

## 6. StepSpec

冻结 dataclass，恰两字段：

```python
StepSpec(slot_id: str, request_builder: callable)
```

- `slot_id`：非空字符串（`__post_init__` 校验，复用 `ControlModelError`；strip 后非空）；是 COMP-2 组合域映射键的**引用**，非新身份类型——不进 fact、不进 UsageRecord、不是域身份
- `request_builder`：callable（`__post_init__` 校验）；签名 `request_builder(previous_result) → request`；`previous_result` 为上一步 `InvocationResult` 或 None（首步）；返回值交由 slot 链消费，构造期不做类型断言（usage-time 由链上组件拒绝）
- 无顺序字段（顺序 = 在 steps 元组中的位置）、无 step_id（游标即身份，YAGNI）、无行为方法

## 7. Step → Slot Binding

- 每个 StepSpec 只引用 **opaque slot_id**：编排层不知道也不询问 slot 背后的 Runtime/Agent/provider；不重做 discovery/qualification/assignment
- **构造期校验**：`build_sequential_pipeline` 对每个 `step.slot_id` 经**唯一公开面** `execution_slots.slot(slot_id)` 做一次探测（丢弃句柄）；缺席 → `KeyError` 原样传播（与 COMP-2 `slot()` 契约同语义、同类型——零翻译、零新增 error type）
- **同一 slot 可被多个 step 先后引用**：顺序执行无并发冲突；与 V3.0-C 的并发塌缩拒绝不冲突（那是并发角色覆盖不变量）
- slot 命名惯例（如 "architect"/"coder"）属 caller 词汇纪律，本层零感知

## 8. SequentialPipeline

**immutable executable definition**：

- `build_sequential_pipeline(execution_slots, steps)`：
  - `execution_slots` 须为 `ExecutionSlots`（isinstance 校验，`ControlModelError`）
  - `steps` 物化为 tuple；**至少一步**（空序列 → `ControlModelError`，沿 COMP-2「at least one slot spec」先例，避免零步退化返回契约）
  - 每项须为 `StepSpec`（`ControlModelError`）
  - 逐项 slot 探测（§7）
  - 任何构造失败：零残留（无 journal 事实、无可达半成品），异常原样传播
- `SequentialPipeline` 对象持有：ExecutionSlots 引用 + 已校验 steps 元组。**零 cursor、零隐藏执行状态、零可变面**；构造后唯一公开方法 `run(run_state=None)`
- 公开面恰 `{run}`（+ 构造入口为模块级函数）

## 9. RunState

**caller-owned transient continuation value**（身份写死）：

- 编排域**瞬态** continuation 状态；仅内存；**不持久化**（V3.2 第一版：不做 orchestration persistence / database state / cross-process resume / durable workflow——写死）
- **不是**：Control lifecycle truth / Observation truth / Usage truth / Journal fact / Execution identity / Agent identity；**不携带** execution_id / invocation_id / runtime_id / agent_id
- 冻结 dataclass，**恰三字段**：

| 字段 | 类型 | 语义 |
|---|---|---|
| `next_step_index` | 非负 int（bool 拒绝） | **下一次需要执行的 step 序号** |
| `previous_result` | 上一步 InvocationResult 或 None | 下一步 request_builder 的输入 |
| `transcript` | tuple（StepRecord，见 §17） | 已完成步的不可变累积（跨 park/resume） |

- 不可变累积：run 内部每次推进都构造新 RunState，绝不原地改
- caller 可自由持有/复制/丢弃；丢弃即放弃 continuation（显式可见，非静默）

**cursor 的 off-by-one 语义（唯一裁决）**：`next_step_index` =「下一步」，非「当前步」：

```
首跑 run()                    → 内部 state.next_step_index = 0
step N 成功                   → next_step_index = N+1
next_step_index == len(steps) → COMPLETED（终局）
PAUSE 于下一 admission        → PARKED，run_state.next_step_index 保持「未执行的下一步」
resume run(state)             → admission 通过后执行 step[next_step_index]
```

推论：admission 恰发生在执行 `step[next_step_index]` 之前，无 ±1 修正；全部步完成后 PAUSE 仍 pending ⇒ 管线照常 COMPLETED（不存在可停驻的下一步；pause 属 caller 的下一次执行概念）；`run(run_state)` 传入 `next_step_index == len(steps)` 的状态 ⇒ 立即 COMPLETED，`final_result = previous_result`（transcript 为空则 None）——循环自然收敛，零特判。

## 10. RunOutcome / RunStatus

`RunStatus`：编排域局部枚举，恰四值 `COMPLETED / FAILED / ABORTED / PARKED`。**与 ControlLifecycle 的域隔离（禁止概念混用）**：ControlLifecycle 是控制域七值生命周期投影（boundary 的派生态）；RunStatus 是一次 run 调用的返回值状态。词形相触（COMPLETED/FAILED/ABORTED）以三重机制隔离——不同 enum 类型、`sequential_pipeline.py` AST 禁 import `ControlLifecycle`（两域词汇零 import 混用）、UI 投影须显式映射域。**PARKED 是非终局 continuation 状态，但 RunOutcome 仍是一次 run 调用的完整返回值**（调用返回、栈帧结束；continuation 在值里，不在对象里）。

`RunOutcome`：单一冻结值（不做 PARKED 特化子类），字段与结构不变量（`__post_init__` 强制，沿 ControlResult 值模型纪律）：

| 字段 | 契约 |
|---|---|
| `status: RunStatus` | 恰四值之一 |
| `transcript: tuple` | §17 |
| `final_result` | COMPLETED：末步 InvocationResult（或收敛路径的 previous_result）；FAILED：失败步**实际返回**的失败结果（status 失败路径），request_builder 异常 / invoke 异常路径为 None；PARKED / ABORTED：一律 None |
| `error` | 仅 FAILED 可非 None（request_builder 普通异常或 invoke 普通异常的**原异常对象**）；成功 / PARKED / ABORTED 一律 None（ControlAborted 是控制域信号，不是 error） |
| `run_state` | **非 None ⟺ status is PARKED**（结构性强制）；COMPLETED / FAILED / ABORTED 一律 None——**终局不产生 continuation**（fail-fast 终止无续走；ABORTED 后 boundary 仍持 ABORT intent，重跑会在 admission 立即再终止，语义自洽） |

结构不变量清单（`__post_init__`）：status 类型校验；`run_state` 非 None ⟺ PARKED；`error` 非 None ⟹ FAILED；`final_result` 非 None ⟹ status ∈ {COMPLETED, FAILED}；PARKED/ABORTED 时 `final_result is None`。

## 11. Admission Semantics

每次准备执行 `step[next_step_index]` 前（含首步与每个续步；`next_step_index == len(steps)` 时无 admission，直接收敛 COMPLETED）：

1. **现读** THE `boundary.pending_intent`（属性直读；RunState 零缓存控制投影）
2. `kind is ABORT` → `RunOutcome(ABORTED, transcript=当前累积, final_result=None, error=None, run_state=None)`
3. `kind is PAUSE` → `RunOutcome(PARKED, transcript=当前累积, final_result=None, error=None, run_state=当前 RunState 原值)`
4. `kind is NONE` → 执行该步

intent 只可能 NONE/PAUSE/ABORT（冻结词表），无其他分支。admission 不调 `boundary.snapshot()`、不投影 lifecycle、不写任何东西。

## 12. Pause / Resume

- Pause：当前在途 invocation **不取消**，自然完成（AbortGate 对 PAUSE 照常放行——冻结语义）；下一真实 Step Admission 阻塞（§11 规则 3）→ PARKED，`outcome.run_state` 即 continuation
- PARKED 后：caller 持有 RunState；caller 对 **Boundary** 执行 `RESUME`（清 pending pause，边界权威）；再调 `pipeline.run(outcome.run_state)` 续走
- Resume 时重新读 Boundary 当前状态：parked 期间发生的一切（新 revision 入队、再次 pause、甚至 abort）由续走时的下一次现读决定——陈旧控制状态在构造上不可能
- RunState 不保存 pause/resume 状态；**admission barrier 归属**：决策权威在控制层（pending_intent），执行点在编排层步间缝（「下一 admission」在顺序管线中即下一步入口），组合层不参与；不新增 ControlGate 组件、不占用其 deferred 缝隙

## 13. Revision Semantics

- **零新增 revision machinery**：复用 ControlBoundary（accept 入队）+ RevisionAdapter（下一次真实 invocation 自动 overlay 全部 pending）+ RevisionAppliedJournaler/UsageCapture（既有 APPLIED 链）
- 语义不变式：**修订作用于 execution 的下一次真实 invocation**——顺序管线中即下一个被执行的 step。Architect 完成后、Coder 未执行时提交的修订 → 进 THE boundary 队列 → Coder 步 invoke 经 RevisionAdapter 自动携带、自动落 REVISION_APPLIED
- 编排层不操作 journal/revision queue、不判断 HONORED、不创建新 revision fact、不排干、不过滤、不指派目标（targeted revision 沿 COMP-2 裁决 deferred）
- 已完成步的输出不被追溯修改；parked 期间入队的修订在 resume 后第一个 step 生效（诚实语义，如实记录）

## 14. Abort Semantics

- admission 发现 ABORT → `ABORTED`（该步零执行、raw 零进入、transcript 原样）
- step invoke 链抛出 `ControlAborted` → **仅在 SequentialPipeline 的 run boundary 捕获并转换为 ABORTED**（run 边界是链外翻译点，与 host_entry 把 CollaborationStateError 译为 exit 2 同构）：消费 ≠ 裁决——零事实写入、零 boundary mutation、零异常改写，仅汇报。composition 链内「零捕获」纪律不变
- request_builder 抛出 `ControlAborted` → 同样 ABORTED（统一翻译，不区分来源，零再裁决）
- 不 cancel（在途完成）、不 retry/fallback、不产生 ABORT_CONFIRMED / 终态事实（CompositionWriter 持有者问题维持 deferred）
- in-flight ABORT 与已完成步共存于 transcript/journal——不同域事实并存，无冲突

## 15. Request Builder / Handoff Boundary

三层数据严格区分（V2 契约证据：`ExternalAgentRequest.handoff_packets` 是一等字段、`InvocationResult.output` 明文「绝不被原样转发给下一个阶段」）：

| 层 | 内容 | 拥有者 |
|---|---|---|
| Execution observation | UsageRecord / trace / journal 事实 | 既有观察域（冻结） |
| Orchestration data | `previous_result`（整个 InvocationResult）在转移函数内的内存传递 | SequentialPipeline（只搬运不解释，不读 output 内容） |
| Runtime invocation request | 下一步 `ExternalAgentRequest`（含 handoff_packets） | caller 供应的 `request_builder` 纯函数 |

- 编排层不解析 runtime packet、不拼接 provider-specific prompt、不读 output 内容
- `request_builder(previous_result) → request` 是 V3.1 packet 机器（collaboration_packet 验证/解析）的 caller hook 挂点；离线测试用最简确定性函数
- fn 契约（纪律，非结构强制，如实声明）：纯函数、无控制/观察副作用、不触 boundary/journal；violation 由测试级 spy 与文档暴露，无法 AST 强迫
- fn 抛普通异常 → FAILED（§16）；fn 抛 ControlAborted → ABORTED（§14）

## 16. Fail-fast Semantics（第一版冻结策略）

| 情形 | 判定 | 结果 |
|---|---|---|
| request_builder 抛普通异常 | 编排数据失败 | `FAILED`，`error=原异常`，`final_result=None`，后续步零执行 |
| invoke 抛普通异常 | 执行崩溃 | 同上 |
| `result.status` ≠ `InvocationStatus.SUCCESS`（含 FAILED/TIMEOUT/CANCELLED/UNAVAILABLE 及一切非终态值） | 任务失败 | `FAILED`，`final_result=该 result 原样`，`error=None`，该步计入 transcript，后续步零执行 |
| ControlAborted（fn 或 invoke） | 控制域终止（**不是** step failure） | `ABORTED`，`error=None` |
| admission ABORT | 控制域终止 | `ABORTED`，零步新增执行 |
| 全部 SUCCESS | — | `COMPLETED`，`final_result=末步 result` |

- **request_builder exception 与 invoke exception 的 FAILED 语义相同**（都 FAILED + error=原异常 + final_result=None）——第一版不区分汇报（区分属未来细化，§25）
- 零 retry / 零 fallback / 零跳步 / 零自动 reassignment；失败策略即本冻结规则，无参数化
- **失败位置推导规则**（无需额外字段）：失败发生在 `step[len(transcript)]`（transcript 只含实际返回过 result 的步；异常步无可投影事实故不在 transcript，位置由长度推出；resume 后 transcript 为累积值，规则不变）

## 17. Transcript Projection

`transcript` 是 **orchestration projection，不是新的 observation authority**：只投影 result 已携带的执行事实，**绝不创造** invocation_id / token 计数 / duration / ExecutionEvent / UsageLog 记录 / JournalFact；trace 缺失时 invocation_id 诚实为 None，不伪造。

StepRecord（冻结值，恰四字段）：

| 字段 | 来源 |
|---|---|
| `step_index` | 推进序号 |
| `slot_id` | 该步绑定的 slot（原样引用） |
| `status` | `result.status` 原样（InvocationStatus 值） |
| `invocation_id` | `result.trace.invocation_id`（trace 在场时）；trace 为 None → None |

收录规则：仅**实际返回过 result** 的步入册（成功步与 status 失败步）；异常步 / 未启动步不入册（无可投影事实）；transcript 不可变累积，跨 park/resume 连续。

## 18. Concurrency Contract

| 情形 | 裁决 | 分级 |
|---|---|---|
| 同一不可变 Pipeline + 多个独立 RunState 并行 run | **允许**：每次 run 是对不可变定义的独立函数应用；共享物恰为 COMP-2 已证并发的组合面（boundary 恒锁 / journal 恒锁 / slot 链独立 / 共享 journaler 无状态）；共享控制真值治理全部在途 run（revision 命中所有 run 的下一次 invocation、ABORT 终止所有 run）——execution-scoped 既定语义 | STRUCTURALLY SAFE |
| 同一 RunState 并发推进 | **OUT OF CONTRACT**：行为未定义（同 iterator 并发 next()）；契约明文由 caller 串行推进一个 RunState；不加锁、不加保护（加锁即违反「不因 RunState 引入全局锁」；RunState 是值，无法也不应自防） | OUT OF CONTRACT |
| 新增 threading / Lock | **零**：编排层零 threading import；run() 只触既有域锁（admission 读 pending_intent 为不可变值原子换引用） | — |

## 19. Authority Matrix

| Responsibility | Owner |
|---|---|
| Step ordering | SequentialPipeline（执行不可变 steps 的确定性顺序；caller 构造期定义） |
| Slot selection | 组合期：caller 建 ExecutionSlots + StepSpec 绑定；pipeline 仅构造期校验引用（探测），**从不选择** |
| Runtime selection | V2/V3.0 discovery/qualification/assignment（COMP-3 域外；执行期已固化于 slot 链内） |
| Agent identity | V3.0 agent 层；编排层不铸造不持有；执行期 agent_id 随 request 流动 |
| Pause | ControlBoundary（intent 权威）；执行点在编排层步间 admission（park） |
| Resume | ControlBoundary（清 pending）；编排层经下次 run(state) 续走 |
| Revision | ControlBoundary（accept 入队）→ 既有 RevisionAdapter 于下一次真实 invocation 自动 overlay；编排层零参与 |
| Abort | ControlBoundary（accept）；编排层 admission 终止 + run 边界翻译 ControlAborted → ABORTED（仅汇报） |
| Continuation / 进度 | **RunState 值（caller 持有）**；Pipeline 是无状态转移函数 |
| Observation | 既有 UsageCapture/UsageLog/journal；编排层只产出内存 transcript 投影 |
| Final result | 末步 InvocationResult 原样经 RunOutcome 返回；编排层不创作结果 |

无任何一格双 owner。

## 20. Data Flow

```
Task
 ↓ caller 写每步 request_builder
steps = (StepSpec(slot_a, fn₁), StepSpec(slot_b, fn₂), …)
 ↓ build_sequential_pipeline(slots, steps)        # 构造期探测 slot 引用
pipeline = SequentialPipeline(slots, steps)        # 不可变可执行定义

run(run_state=None):                                # (pipeline, run_state|None) → RunOutcome
    state = run_state or RunState(next_step_index=0,
                                  previous_result=None, transcript=())
    while state.next_step_index < len(steps):
        pending = slots.boundary.pending_intent      # ① 现读（零缓存）
        if pending.kind is ABORT:  return RunOutcome(ABORTED, state.transcript, …, run_state=None)
        if pending.kind is PAUSE:  return RunOutcome(PARKED,  state.transcript, …, run_state=state)
        try:                                         # ② 唯一 Try：run 边界
            request = steps[next].request_builder(state.previous_result)
            result  = slots.slot(steps[next].slot_id).invoke(request)
        except ControlAborted: return RunOutcome(ABORTED, …, run_state=None)
        except Exception as exc: return RunOutcome(FAILED, error=exc, …, run_state=None)
        record = StepRecord(next, slot_id, result.status,
                            result.trace.invocation_id if trace else None)
        if result.status is not SUCCESS:             # ③ fail-fast
            return RunOutcome(FAILED, transcript+(record,), final_result=result, run_state=None)
        state = RunState(next+1, result, state.transcript+(record,))   # ④ 不可变累积
    return RunOutcome(COMPLETED, transcript=state.transcript,
                      final_result=state.previous_result, run_state=None)
```

Runtime 仅出现在 `slots.slot(...).invoke` 链尾，对 ①②③④ 不可见。

## 21. Error Matrix

| # | 触发 | status | error | final_result | transcript 增量 | run_state |
|---|---|---|---|---|---|---|
| E1 | admission ABORT（步前） | ABORTED | None | None | 无 | None |
| E2 | admission PAUSE（步前） | PARKED | None | None | 无 | 当前 RunState |
| E3 | request_builder 普通异常 | FAILED | 原异常 | None | 无（该步未获 result） | None |
| E4 | invoke 普通异常 | FAILED | 原异常 | None | 无 | None |
| E5 | result.status ≠ SUCCESS | FAILED | None | 该 result | +该步 StepRecord | None |
| E6 | ControlAborted（fn 或 invoke 链） | ABORTED | None | None | 无 | None |
| E7 | 全部成功走完 | COMPLETED | None | 末步 result | +各成功步 | None |
| E8 | run(state) 且 next==len（收敛路径） | COMPLETED | None | previous_result | 无 | None |

构造期拒绝（`build_sequential_pipeline`）：非 ExecutionSlots / 空 steps / 非 StepSpec 项 / StepSpec 自身非法（slot_id 空、request_builder 非 callable）→ `ControlModelError`；slot 引用缺席 → `KeyError`（探测原样）。全部零残留（无 journal 事实、无可达半成品）。运行期：`run` 传入非法 RunState（next_step_index 负数/bool）→ `ControlModelError`（值模型校验）。

## 22. Structural / AST Guardrails（production 文件锁定）

- **import roots 恰 6**：`{__future__, control_boundary, control_gate, dataclasses, execution_slots, external_runtime}`；用途钉死：`control_boundary → ControlModelError`、`control_gate → ControlAborted`（**恰一名字**，AST 可证——是错误定义的权威家，不是 gate 接线）、`external_runtime → InvocationStatus`（恰一名字，仅 SUCCESS 基准）、`execution_slots → ExecutionSlots`
- **禁 import**：`threading` / `uuid` / `time` / `subprocess` / `ControlLifecycle` / `ControlJournal` / `wrap_stack` / 一切 provider 模块
- **恰一 Try 节点**：run 边界（两个 handler：`ControlAborted`、`Exception`）；构造探测**零 try**（KeyError 原样传播）；全模块零其他 ExceptHandler
- **源扫描零命中**：`threading` / `Lock` / `acquire` / `release` / `uuid` / `random` / `monotonic` / `subprocess` / `Popen` / `cockpit` / `claude` `gemini` `codex` `qwen` `opencode` `cline` `deepseek` / `ExecutionEvent` / `INVOCATION_STARTED` / `INVOCATION_FINISHED` / `event_index` / `trace_projector` / `boundary_writer` / `revision_writer` / `composition_writer` / `ABORT_CONFIRMED` / `PAUSE_CONFIRMED` / `ABORT_SUPERSEDED` / `submit(` / `snapshot(` / `drain` / `broadcast` / `routing` / `schedule` / `orchestrat`（英文词；docstring 用中文表达；`control_gate` **不在**禁词表——它是 ControlAborted 的 import 来源，白名单受 §22 首条约束）——注意 `ControlLifecycle` 禁 import 但 RunStatus 值 `COMPLETED/FAILED/ABORTED` 本身合法（类型隔离即域隔离）
- **结构计数**：`ControlBoundary(` 零构造点；`ExecutionSlots(` 零构造点（只消费既有组）；`slot(` 调用**恰两处**（构造期探测一处 + run 循环内逐步查取一处——探测丢弃句柄、run 每步经公开面现查，两处均锁定，不留实现自由度）；`pending_intent` 读点恰在 admission 一处
- **RunState 恰三字段、零行为方法**（AST dataclass 字段锁）；`RunOutcome.__post_init__` 不变量如 §10

## 23. Testing Strategy（按 CU 分层，全部离线 spy，零 REAL）

**ORCH-2（值模型 + 单步）**：StepSpec 校验矩阵（slot_id 空/非串、fn 非 callable）；build 校验矩阵（非组/空 steps/非 StepSpec/缺席 slot→KeyError/合法构造）；RunState 冻结与三字段；RunOutcome 不变量（run_state⟺PARKED、error⟺可 FAILED、final_result 在场规则、非法组合拒绝）；单步管线：COMPLETED（E7 单步版）/ PAUSE-before-first（E2，next=0）/ resume 后执行 step 0 / ABORT-before-start（E1）/ E3/E4/E5 单步版；零残留断言（journal 事实数不变）。

**ORCH-3（顺序语义）**：多步 state 不可变线程化；中步 E5 fail-fast（后续步零执行、失败位置=len(transcript)）；E3/E4 中步版；中步 PAUSE→run(state) 精确续走（next_step_index 语义、transcript 跨续累积、值断言）；RESUME 前再 revision → 续走首步 overlay 携带（既有队列零新机制）；revision 于两步之间提交 → 下一步自动携带 + REVISION_APPLIED 落同一 journal；在途 invocation 期间 ABORT → 该步正常完成后 admission 终止（E1 于中步）；同 slot 被 step1/step3 复用；同 Pipeline 双 RunState 并行（互不串线、共享控制真值治理：ABORT 双双终止、revision 双双命中）；transcript 投影完整性（成功/失败步收录、异常步缺席、trace None→invocation None）；§22 全部 AST/源扫描守卫；冻结件零修改证明（git diff 对照）。

**ORCH-4（gated）**：REAL 跨 runtime 顺序 E2E（claude+pi 通道复用 R3 已证资产；V3.1 packet fn 作 caller hook）。单独授权，不预先实现。

## 24. ORCH-1 / ORCH-2 / ORCH-3 / ORCH-4 Implementation Boundaries

| CU | 产出 | 文件动作 | 门 |
|---|---|---|---|
| ORCH-1 | Implementation Plan（writing-plans：测试清单、TDD 步序、风险）+ 审查 | 零代码；plan 文档 | plan 审查通过 |
| ORCH-2 | 值模型 + 构造 + 单步转移（§23 ORCH-2 列表全量） | 建 `sequential_pipeline.py` + `tests/test_sequential_pipeline.py`（RED→GREEN）；提交 1 | 全量回归恰基线集（8F，当前 3234P 基线 + 新测试数） |
| ORCH-3 | 顺序全语义 + 守卫全量（§23 ORCH-3 列表全量） | 同两文件扩展（RED→GREEN）；提交 2 | 同上；AST 守卫全绿 |
| ORCH-4 | REAL E2E | 零生产改动（或仅测试资产） | 单独 gated 授权 |

每 CU：独立授权、独立 TDD、精确 staging（恰涉及的两文件）、基线 8 failure 逐项对照、第 9 失败即 STOP。

## 25. Deferred Work（显式延期清单）

DAG/条件分支/循环；retry/fallback/自动 reassignment；dynamic routing；targeted revision routing；**RunState persistence（值持久化 CU——零架构变更但需新授权）**；跨进程 resume / durable workflow；ControlGate 接线（admission 缝隙的通用化）；CompositionWriter 持有者（ABORT_CONFIRMED）；HONORED 证据；queue drain；orchestration 事实持久化（transcript 入账）；request_builder 异常与 invoke 异常的差异化汇报；fn 纯性的结构强制；TUI/Web/CLI/remote/A2A/registry/marketplace；REAL 扩展 runtime。

## 26. Risks / Known Limitations

1. 同 RunState 并发推进 OUT OF CONTRACT（caller 纪律，无锁无保护）
2. RunState 丢弃即 continuation 消失（显式可见；相对隐藏 cursor 的静默陈旧态是可接受代价）
3. PARKED + 进程死亡 ⇒ continuation 不可恢复；boundary 事实/UsageRecord 长存的不对称（与 boundary 内存态同族限制）
4. `previous_result`（含大 output）随 RunState 驻内存——v1 接受；裁剪/投影属未来观察域议题
5. RunStatus 与 ControlLifecycle 词形相触——类型隔离 + AST 禁 import + UI 显式域映射（残余风险：报告阅读混淆，文档消歧）
6. fn 纯性不可结构强制——测试级 spy + 文档契约
7. transcript 的 invocation_id 依赖 result.trace 在场（契约允许 None）——缺席即缺席，不伪造；与 UsageLog join 退化为顺序对齐
8. 同 slot 多 step 复用合法但 slot 内无 step 粒度隔离（revision/abort 仍 execution-scoped——特性而非缺陷，语义自洽）
9. run() 把异常译为返回值可能掩盖编程错误——error 携带原异常对象 + 测试覆盖缓解，契约明示
10. E5 判定 `status is not SUCCESS`：非终态值（SELECTED/STARTING/INVOKED）若被异常 adapter 返回也按失败处理——保守且确定，如实记录
11. **RunState 跨管线误用属契约外**：RunState 零管线绑定标识（加标识即新 identity，YAGNI 拒绝）；把 A 管线的 RunState 回传给 B 管线 ⇒ `next==len` 垃圾收敛或错位执行，无检测无保护——caller 纪律（与「同 RunState 并发推进」同族，§18）

## 27. Acceptance Criteria

- AC1 恰 2 新文件（`sequential_pipeline.py` + `test_sequential_pipeline.py`），零修改任何冻结件（git diff 零输出，含两份 COMP 测试）
- AC2 转移模型成立：`(pipeline, run_state|None) → RunOutcome`；Pipeline 不可变（AST 零 cursor/零赋值自字段；run 不改 self）
- AC3 RunState 恰三字段、冻结、caller 持有、零持久化、零控制/观察/usage 缓存
- AC4 RunOutcome 不变量全部结构强制并测试覆盖（run_state⟺PARKED 等）
- AC5 E1-E8 全矩阵测试通过；失败位置推导规则验证
- AC6 Pause/Resume：中步 PARKED→resume 精确续走；revision 经既有队列自动命中下一步（同一 journal 落 REVISION_APPLIED）；在途不取消
- AC7 Abort：admission 终止 + ControlAborted 仅 run 边界翻译；零 ABORT_CONFIRMED/终态事实
- AC8 同 Pipeline 双 RunState 并行 STRUCTURALLY SAFE 测试通过；零新 threading/Lock（AST）
- AC9 §22 守卫全绿（import roots 恰 6、恰一 Try、源扫描零命中、结构计数）
- AC10 全量回归恰基线失败集（8F 逐项对照；无第 9 失败；无 frozen test regression）
- AC11 报告 + 记忆落库；未进入未授权 CU

---

## 附：本 Spec 对「额外要求」的逐条覆盖索引

| 要求 | 节 |
|---|---|
| Pipeline = immutable executable definition | §8 |
| RunState = caller-owned transient continuation value | §9 |
| (pipeline, run_state\|None) → RunOutcome 转移模型 | §9、§20 |
| next_step_index off-by-one 语义 | §9 |
| RunState 不构成第四套 truth | §5（三真值分离表 + 读者坐标论证） |
| RunStatus ≠ ControlLifecycle、禁止混用 | §10、§22 |
| request_builder exception vs invoke exception 的 FAILED 语义 | §16、§21（E3/E4 同语义裁决） |
| ControlAborted 与普通异常的区别 | §14、§16、§21（E6 vs E3/E4） |
| PARKED = 非终局 continuation，RunOutcome 仍是单次 run 返回值 | §10 |
| ABORTED/FAILED 不产生 continuation | §10、§21 |
| Orchestrator 不生成 invocation_id/token/duration/usage/event/journal facts | §17、§22 |
| transcript = orchestration projection 非新 observation authority | §17 |
| 同 Pipeline 多 RunState 并行安全；同 RunState 并发 OUT OF CONTRACT | §18 |
| V3.2 不做 RunState persistence | §9、§25 |
