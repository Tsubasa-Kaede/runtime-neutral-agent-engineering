# 协作架构（Collaboration）

> 角色之间如何通过契约交接工作 —— 以及刻意**不**在这里的东西（没有
> 远程 agent、没有网络）。事实之源：`structured_packets.py`、
> `collaboration_packet.py`、`local_transport.py`、`remote_transport.py`、
> `collaboration_session.py`、`verification_collaboration.py`、
> `collaboration_state.py`、`collaboration_handoff.py`、
> `collaboration_orchestrator.py`。

## 层级图

```text
Business packets        ArchitecturePacket / ImplementationPacket / TestPacket / ReviewPacket
        ↓ wrapped in
Protocol contract       CollaborationPacket (the envelope: who owes what to whom)
        ↓ moved by
Delivery mechanism      LocalTransport (in-process mailbox) | RemoteTransport (contract + loopback)
        ↓ driven by
Sessions                CollaborationSession (architect→coder→architect)
                        VerificationCollaboration (tester→reviewer, ledger-backed)
        ↓ recorded in
Ledger                  SharedCollaborationState (append-only, wire-at-append)
```

这张层级图自上而下表示：四个业务 packet 被包进 CollaborationPacket
协议信封，由 Transport 投递，由 Session 驱动流转，最终以 wire 文本形式
追加进 append-only 的共享 ledger。

## Packet、Contract、Transport —— 三种不同的东西

1. **业务 packet**（`structured_packets.py`）是四个角色的输出。
   `REQUIRED_FIELDS` 是 **wire contract**：改一个字段就是改协议，
   不只是改一个类。构造即验证（schema、类型、秘密形态扫描）；规范化
   序列化是确定性的（key 排序、紧凑分隔符）。
2. **`CollaborationPacket` 是协议契约，不是 transport。** 它在冻结的信封
   schema（`correlation_id`、`task_id`、source/target agent+role、
   payload 类型、acceptance criteria、`protocol_version`、
   `provenance`）之上表达"哪个角色欠哪个角色什么工作"。它不携带任何
   runtime/provider/model 字段 —— runtime 选择留在它自己的层。
   `correlation_id` 连接一跳 request/response；`task_id` 界定整个任务。
3. **Transport 是投递机制。** 今天它是进程内邮箱（`LocalTransport`）。
   `remote_transport.py` 以 loopback 实现定义*边界契约*（acceptance
   语义、封闭的 receipt 词汇）—— 它**不是** Remote Agent Network。
   V2 中不存在任何远程 peer。

## 两个 Session

### CollaborationSession（architect → coder → architect）

- **architect** 携带任务被调用；其输出必须解析为 `ArchitecturePacket`
  （剥 fence → JSON → 归一化 → 身份 → from_dict → 整包内容扫描）。
  无法解析的输出是 `ARCHITECT_PACKET_INVALID` —— 绝不伪造 packet。
- 该 packet 被装封并投递给 **coder**，coder 的 prompt 就是序列化后的
  packet —— packet 是 coder 的*完整*输入契约，绝不是"原始任务文本加
  祈祷"。
- coder 的 `ImplementationPacket` 以同样方式返回。每一跳共享任务生命
  周期的 budget/guard（每个角色阶段 check → reserve → invoke）。

### VerificationCollaboration（tester → reviewer）

- 在 session **之后**组合，经由纯 `handoff_input_for` 投影从 ledger
  读取上游事实（tester 读最新的 `IMPLEMENTATION`；reviewer 读
  `ARCHITECTURE + IMPLEMENTATION + TEST`）。
- 上游事实缺失即 `MISSING_HANDOFF` —— 验证阶段绝不发明自己的输入。
- 每一跳以全新 correlation 发出 `TestPacket`/`ReviewPacket` 信封，
  使 ledger 不变量成立；装封前对开放的 dict 字段运行自己的内容扫描。

## Ledger（SharedCollaborationState）

Append-only，以"替换即不可变"方式实现：

- 记录类型：`DECISION`（路由）、`REQUEST`/`REPLY`（信封）、`FAILURE`。
- `sequence` 按 task 分配、稠密、且**由 ledger 指派** —— 调用方不能
  注入。`correlation_id` 绑定一对 request/reply。
- 信封在追加时刻以规范化 **wire 文本**存储，因此之后对共享 packet 对象
  的任何改动都改写不了历史；读取返回全新解码。
- 追加时强制的不变量：`REPLY` 要求其 `REQUEST` 在场；同一 correlation
  不允许重复的 `REQUEST`/`REPLY`；一个 correlation 绝不跨任务。
- Provenance 完全由信封推导 —— 没有可以自由断言它的字段。
- 原始输出、推理与聊天绝不进入 ledger；trace 事实只以封闭的
  `TraceSummary` 投影进入（invocation id、status、exit code、duration）。

## 交接：原始输出边界

两个投影强制"阶段只见 packet，绝不见原始输出"：

- 引擎路径：`handoff_context.HandoffContext` —— 逐阶段输入契约 + 带验证
  的 `accept`。
- 协作路径：`collaboration_handoff.handoff_input_for` —— 对 ledger 的
  只读、按 sequence 取最新的投影，以 payload 类型查询（绝不按方向、
  目标角色或 correlation id）。

## 路由进入协作

`CollaborationOrchestrator` 依据 ModeGate/classifier 路由 SINGLE 与
DUAL：OFF 与非 COMPLEX 的 AUTO 委托给 verified 单执行器；ON 与 COMPLEX
驱动 dual session。只有"用量增量为零的 dual 失败"才 fallback 到
SINGLE —— packet 无效、correlation 不匹配、预算耗尽或显式 ON 绝不
fallback。facade 以 dual 成功为门放行 tester+reviewer；缺少验证能力
即诚实的终态 `NO_VERIFICATION_CAPABILITY`。

## Agent 寻址

Agent 以完整 runtime identity 的确定性投影（`agent_id_for(identity)`）
寻址，在协作栈中再加上角色限定地址
（`collab_agent_address(identity, role)`）—— 同一 runtime 每个地址服务
一个角色。信封刻意不携带 runtime/provider/model 字段；地址字符串是
wire 上唯一的身份。

## 这不是什么

- 不是 Remote Agent Network —— 没有远程 peer、没有跨机器投递。
- 不是聊天总线 —— payload 只能是四个 packet。
- 不是 V3：远程协作与 multi-agent network 是未来工作，在当前代码库
  中任何地方都未实现。
