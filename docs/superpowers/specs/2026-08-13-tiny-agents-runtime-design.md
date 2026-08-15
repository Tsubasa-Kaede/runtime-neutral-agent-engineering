# Tiny Agents 第二 External Runtime 设计

## 状态

设计已确认，暂不实现代码。

## 目标

在现有 Claude Code CLI External Runtime 之外，接入 Hugging Face `tiny-agents` 作为第二个真实 Agent Runtime，同时保持现有 `ExternalAgentAdapter` Protocol 不变，并确保 Runtime、Agent、Provider、Model 和 Role 相互解耦。

## 选择结论

选择 Hugging Face `tiny-agents`。

当前机器存在真实可执行文件：

```text
C:\Users\夜缘\AppData\Local\Programs\Python\Python312\Scripts\tiny-agents.exe
```

已确认 `tiny-agents --help` 可运行，并暴露：

```text
tiny-agents run [OPTIONS] [PATH] COMMAND [ARGS]...
```

当前尚未确认可用 Agent 配置、模型连接、Prompt 输入方式、真实成功调用、timeout 或 cancel 行为。因此实施前必须先做一次真实最小调用实验。若实验无法成功，不能把该 Runtime 标记为可用，也不能用 Mock 代替真实 Integration Test。

## 架构边界

现有协议继续作为唯一 Adapter 边界：

```python
class ExternalAgentAdapter(Protocol):
    def discover(self) -> RuntimeDiscovery: ...
    def invoke(self, request: ExternalAgentRequest) -> InvocationResult: ...
    def cancel(self, invocation_id: str) -> InvocationResult: ...
```

新增 `TinyAgentsAdapter`，但不继承 `ClaudeCodeAdapter`，不创建 Claude 或 Codex 专用抽象。两个 Adapter 只共享 Provider-neutral Runtime 数据模型和 Protocol。

核心 Orchestrator 只依赖 Protocol、请求和结果类型，不判断具体 Runtime：

```text
Orchestrator → Router → selected_adapter.invoke(request) → InvocationResult
```

新增 Runtime 时，新增对应 Adapter 和注册配置即可，不修改核心 Orchestrator 的流程控制。

## 身份解耦

`RuntimeProfile` 继续分别表达：

- `agent_id`：被选择的 Agent 身份
- `runtime`：实际执行程序，例如 `tiny-agents`
- `provider`：模型服务 Provider，可为空
- `model`：模型标识，可为空
- `role`：任务角色，例如 `coder`
- `capabilities`：仅填写已验证能力

`TinyAgentsAdapter` 不从 Runtime 名称、模型名称或 Provider 名称推断能力。Discovery 只证明可执行文件和探测命令的可用性，未经验证时返回空能力集合。

## 文件边界

### 新增

```text
dual-agent-development/scripts/tiny_agents_adapter.py
tests/test_tiny_agents_adapter.py
tests/test_tiny_agents_integration.py
```

`tiny_agents_adapter.py` 负责：

- 查找 `tiny-agents` 或 `tiny-agents.exe`。
- 执行 Discovery。
- 根据已验证的启动配置构造 argv。
- 通过 `subprocess.Popen(..., shell=False)` 启动进程。
- 传递 Prompt，捕获 stdout、stderr 和 exit code。
- 将结果转换为 `InvocationResult` 和 `InvocationTrace`。
- 保存活动进程并支持取消。
- 不记录或伪造认证信息和 Token 使用量。

`test_tiny_agents_adapter.py` 负责离线 Adapter 行为测试。Fake Process 只用于测试状态映射，不代表真实 Runtime 验证。

`test_tiny_agents_integration.py` 负责 opt-in 真实调用，默认跳过，不使用 Mock Runtime，不修改仓库文件。

### 可能修改

```text
dual-agent-development/scripts/adapter_probe.py
tests/test_adapter_contract.py
dual-agent-development/scripts/dual_agent.py
```

修改原则：

- `adapter_probe.py` 只增加独立的 `tiny-agents` Discovery，不加入 Prompt 或编排逻辑。
- `test_adapter_contract.py` 可增加对 `TinyAgentsAdapter` 的通用 Protocol 契约覆盖。
- `dual_agent.py` 仅在现有注入机制不足时做最小 Runtime-neutral Adapter 注册或传入扩展。
- 禁止在核心 Orchestrator 增加 `if runtime == ...` 分支。

## 启动配置

Runtime 通用请求不增加 Tiny Agents 专用字段。Adapter 通过专属配置接收：

```text
executable
agent_path
command
command_args
```

概念上的启动形式为：

```text
tiny-agents run <agent_path> <command> <command_args...>
```

具体参数不能凭猜测实现。实施前必须根据选定 Agent 的帮助输出或真实实验确认 Prompt 是通过 stdin、位置参数还是 Agent Command 参数传入，然后把已验证的方式封装在 Adapter 内或配置模板中。

## Discovery

`TinyAgentsAdapter.discover()`：

1. 查找 `tiny-agents` 和 `tiny-agents.exe`。
2. 执行已确认的探测命令。
3. 返回 `RuntimeDiscovery(runtime="tiny-agents", available=...)`。
4. 当前没有受支持的 `--version` 证据时，`version` 保持 `None`。
5. 未经验证的能力返回 `frozenset()`。

Discovery 不执行 Agent Invocation，也不选择 Provider、Model 或 Role。

## Invocation

```text
ExternalAgentRequest
        ↓
TinyAgentsAdapter.invoke()
        ↓
校验 Agent Path 与 Command 配置
        ↓
构造 argv
        ↓
Popen(shell=False)
        ↓
已验证的 Prompt 输入方式
        ↓
communicate(timeout=deadline)
        ↓
stdout/stderr/exit_code
        ↓
InvocationResult + InvocationTrace
```

状态映射：

```text
进程启动成功       → INVOKED
exit_code == 0     → SUCCESS
exit_code != 0     → FAILED
TimeoutExpired     → TIMEOUT，并 kill
cancel()           → CANCELLED，并 kill
可执行文件缺失     → UNAVAILABLE
```

Trace 使用：

```text
runtime       = "tiny-agents"
agent_id      = request.agent_id
provider      = request.provider
model         = request.model
role          = request.role
invocation_id = new_invocation_id()
```

若 Runtime 没有可验证 Token usage，`input_tokens` 和 `output_tokens` 均保持 `"unknown"`。

进程调用使用 argv 数组和 `shell=False`，不把用户 Prompt 拼入 Shell 字符串，环境变量采用最小集合。认证内容不能进入 Trace、异常或测试输出。

## 取消和超时

当前调查没有证明 Tiny Agents 自身提供 timeout 或 cancel 参数，因此不依赖未确认的 CLI 参数。Adapter 通过进程句柄实现：

- `communicate(timeout=request.timeout_seconds)` 超时后 kill，并返回 `TIMEOUT`。
- `cancel(invocation_id)` 查找活动进程，kill 后返回 `CANCELLED`。
- 活动调用结束后从进程表移除。

真实 timeout/cancel 测试使用临时、无副作用且可控时长的 Agent Command，验证 Adapter 的进程控制，不推断 Tiny Agents CLI 的额外能力。

## Integration Test

实施前置实验必须：

1. 在临时目录准备可运行的 Agent 配置。
2. 使用已确认的 `tiny-agents run` 入口。
3. 使用不会修改仓库的最小 Agent Command。
4. 发送 `Return exactly OK and nothing else.`。
5. 捕获 stdout、stderr、exit code 和 duration。
6. 确认 Prompt 输入位置。
7. 确认模型或 Provider 凭据要求。
8. 确认失败行为。
9. 在可控临时进程上验证 timeout/cancel。

真实测试通过环境变量显式开启，例如：

```text
RUN_REAL_PROVIDER_TESTS=1
TINY_AGENTS_AGENT_PATH=<temporary-agent-path>
TINY_AGENTS_COMMAND=<validated-command>
```

真实测试必须验证：

- `InvocationStatus.SUCCESS`
- Trace 状态为 `SUCCESS`
- Runtime 为 `tiny-agents`
- Invocation ID、开始时间、结束时间和耗时存在
- stdout 包含真实响应
- 没有 Mock Adapter
- 没有修改仓库文件

离线测试覆盖成功输出、纯文本输出、非零退出、空输出、超时 kill、取消活动调用、未知 Invocation、Discovery 失败和 Shell 参数安全。

## 未来 Runtime 扩展

未来新增 Runtime 的标准流程：

```text
新增 <runtime>_adapter.py
        ↓
实现 ExternalAgentAdapter
        ↓
提供 RuntimeProfile 与 Adapter-specific 配置
        ↓
注册 Adapter
```

核心 Orchestrator、通用请求模型、通用结果模型和 Router 不绑定 Claude Code、Tiny Agents 或 Codex。

## 验收标准

- Claude Code 和 Tiny Agents 都通过同一个 `ExternalAgentAdapter` Protocol。
- Runtime、Agent、Provider、Model、Role 在请求和 Trace 中保持独立字段。
- Tiny Agents 的真实调用成功前，不宣称第二 Runtime 已可用。
- 不使用 Mock 作为真实 Integration Test 替代。
- 核心 Orchestrator 不包含 Runtime 名称分支。
- 新增第三个 Runtime 时无需修改核心 Orchestrator。
- 未验证的 CLI 参数、能力、Token、Provider 或 Model 不被猜测。
