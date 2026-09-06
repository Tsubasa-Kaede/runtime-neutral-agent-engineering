# Remote Collaboration Contract 设计（V3.1-A）

## 状态

设计已确认并已实现 Contract 层；Transport（V3.1-B）暂不实现。

## 目标

为 V3.1 Remote Collaboration 建立与 Transport 无关、可序列化的远程消息边界
（Remote Collaboration Contract），使后续 V3.1-B Remote Transport、
V3.1-C Remote Agent Session 与 V3.1-D REAL Remote E2E 有稳定契约可依赖。

## 架构决策（已批准）

采用 `RemoteEnvelope` 包裹**现有** `CollaborationPacket`，不建立第二套
RemotePacket 业务协议：

```text
Agent A ── RemoteEnvelope ──→ Transport ──→ Remote Agent B
             │
             └── payload = 既有 CollaborationPacket（冻结语义，原样复用）
```

职责切分：

- `RemoteEnvelope` —— 远程边界事实：protocol_version、message_id、
  correlation_id、sender、recipient、role、payload_type、payload。
- `CollaborationPacket` —— 协作业务语义（V2 冻结，一字不改）。
- `RemoteEnvelope` 不得重新定义 packet 的业务语义；仅强制两个无命名空间
  的一致性事实：信封 correlation_id == payload.correlation_id，
  信封 role == payload.target_role。

## 关键语义

### Identity：Agent Address，不是 Runtime Identity

sender/recipient 使用 V3.0-A 的 `agent_address()` 地址空间
（`agent:{agent_id}:{role}`），rebinding 不改变地址；runtime 四元组
（runtime_id/provider_id/model_id/config_fingerprint）不得作为远程寻址
真相，也不出现在信封任何字段中。信封地址与 V2 packet 的
source_agent/target_agent（runtime-keyed 地址空间）有意不做交叉校验——
两个地址空间按 `agent_identity.py` 的设计保持不相交。

### message_id ≠ correlation_id

- `message_id`：唯一标识当前这一条消息（工厂 `new_message_id()`，
  `remote-` 前缀）。
- `correlation_id`：关联同一次 request/response/handoff 交互（复用
  `new_correlation_id()`，`collab-` 前缀）。
- 两个字段独立存在、独立工厂，永不合并。

### 序列化规则

- 跨边界唯一标准：确定性 UTF-8 JSON（`sort_keys=True,
  separators=(",", ":")`，与全库既有 canonical 约定一致）。
- payload 以 packet 的 canonical wire text（字符串）内嵌，信封对 packet
  内部 schema 保持无感知；解码走既有
  `deserialize_collaboration_packet`。
- 禁止 pickle/marshal/cloudpickle、Python repr、二进制对象。
- `frozenset`/`tuple`/`Enum` 一律不出现于信封字段（均为 str/封闭 Enum
  value）。

### 版本

`PROTOCOL_VERSION = "1.0"`（沿用仓库既有版本约定）。精确匹配：识别当前
支持版本、拒绝未知版本、不做 best-effort downgrade、不静默接受。

### 投递语义（本轮只定义，不实现）

`ACCEPTED / DELIVERED / REJECTED / FAILED / TIMEOUT`
（`RemoteEnvelopeStatus`，封闭）。语义边界：

- `ACCEPTED != DELIVERED`：边界接收 ≠ 对端边界已收到。
- `DELIVERED != EXECUTED`：对端收到 ≠ 对端 Agent 已执行；"EXECUTED"
  有意不存在于投递词表（执行是 Agent 侧结果，属于 V3.1-C）。
- 与既有 `remote_transport.RemoteDeliveryStatus.DELIVERED` 同值对齐，
  V3.1-B Transport 将其回执映射到本词表，不改既有 Enum。

### 错误语义（结构化、可判断）

`RemoteEnvelopeError`（携带封闭 `RemoteEnvelopeErrorCode`）：
`INVALID_ENVELOPE / UNSUPPORTED_PROTOCOL / UNKNOWN_AGENT /
ROLE_UNAVAILABLE / DELIVERY_FAILED / DELIVERY_TIMEOUT /
REMOTE_EXECUTION_FAILED`。

- 公共 API 不以 KeyError/AttributeError/TypeError 为错误语义。
- Transport-specific 条件（HTTP 503、connection reset、socket timeout、
  TLS failure 等）明确不属于本词表，留给 V3.1-B Transport 层。
- `UNKNOWN_AGENT` / `ROLE_UNAVAILABLE` / `DELIVERY_FAILED` /
  `DELIVERY_TIMEOUT` / `REMOTE_EXECUTION_FAILED` 本轮仅作为封闭词表
  存在，由后续层抛出；Contract 层自身只抛 `INVALID_ENVELOPE` 与
  `UNSUPPORTED_PROTOCOL`。

### 数据边界

信封不得携带 adapter/runtime/filesystem 对象、文件句柄、凭据或 Python
callable：payload 槽位只接受 `CollaborationPacket` 实例，头字段走
content_safety 单一扫描权威（markers + credential shapes）。本轮不实现
authentication/encryption。

## 文件

- `dual-agent-development/scripts/remote_contract.py` —— Contract 层
  （RemoteEnvelope、词表、序列化、版本）。
- `tests/test_remote_contract.py` —— 行为测试（构造/序列化/往返/确定性/
  畸形拒绝/版本/寻址语义/投递语义/错误语义/无 pickle/无第三方传输依赖）。
- `remote_transport.py`（既有）—— 本轮一字未改；V3.1-B 在其上实现。

## 非目标（明确推迟）

HTTP/WebSocket/TCP/UDP/socket/gRPC/REST/A2A、authentication、encryption、
retry、service discovery、remote session（V3.1-C）、REAL Remote E2E
（V3.1-D）、ZCode adapter（ZCode 不是 Runtime，不做逆向）。

## 验收

见 `tests/test_remote_contract.py`；V3.1-A 验收时 REAL Remote E2E =
NOT RUN（推迟至 V3.1-D）。
