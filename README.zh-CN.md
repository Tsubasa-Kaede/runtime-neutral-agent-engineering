# dual-agent

[![CI](https://github.com/Tsubasa-Kaede/runtime-neutral-agent-engineering/actions/workflows/ci.yml/badge.svg)](https://github.com/Tsubasa-Kaede/runtime-neutral-agent-engineering/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/dual-agent-development.svg)](https://pypi.org/project/dual-agent-development/)
[![Python](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12-blue)](https://github.com/Tsubasa-Kaede/runtime-neutral-agent-engineering/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

> **发现能力 · 验证执行 · 控制协作。**
> 给编码 Agent CLI 加一层可验证的编排层 —— 你的 Agent 干活，带着凭证。

**English → [README.md](README.md)**

`dual-agent` 是位于你的应用与编码 Agent CLI（Claude Code、Codex CLI、
Gemini CLI 等）之间的工程层：发现本机安装了哪些 runtime，验证每个
runtime 能证明什么，准入到已验证池，然后在明确的预算与防循环保护下编排
architect → coder → tester → reviewer 的协作流 —— 每个结果都带 provenance。

**Agent runtime ≠ Agent 编排。** runtime 负责执行，本项目负责其上的工程。
不是聊天机器人、不是模型服务商、不是单一 runtime 的包装、也不是分布式
Agent 网络。无网络传输、不碰凭据、零运行时依赖（纯标准库）。

> 命名对照：GitHub 仓库 `runtime-neutral-agent-engineering` · PyPI 包
> `dual-agent-development` · 导入名 `dual_agent` · 命令行 `dual-agent`。
> 同一产品，同一版本真相（`dual_agent.__version__`）。

## 30 秒尝鲜 —— 离线、零凭据

不需要 runtime、不需要登录、不需要 API key、不联网，克隆即可：

```bash
git clone https://github.com/Tsubasa-Kaede/runtime-neutral-agent-engineering.git
cd runtime-neutral-agent-engineering
python examples/offline_mock_run.py
```

预期输出 —— 一行闭合、无密钥的 JSON 摘要：

```json
{"path": "FOUR_STAGE", "status": "SUCCESS", "stages": ["architect", "coder", "tester", "reviewer"], ...}
```

这是用 mock adapter 驱动真实生产 facade 的完整离线运行 —— 同一台引擎，
诚实地标注 `OFFLINE`。

## 安装

Python >= 3.10，零运行时依赖，无需克隆：

```bash
pip install dual-agent-development
dual-agent --version

# 或用 uv 免安装直接跑：
uvx --from dual-agent-development dual-agent --version
```

## 它做什么

- **Runtime 发现** —— 机器上到底有没有这个 runtime？
- **Runtime 验证** —— 门控的 G1–G14 资格认证，产出真实证据
- **按能力选择** —— 依据已证明的能力选择，绝不按名字
- **Agent 编排** —— architect → coder → tester → reviewer 阶段链
- **结构化协作** —— 经校验的 packet 走 append-only 账本
- **预算控制** —— 每次调用前先预留槽位
- **LoopGuard** —— 重复任务 / 反复失败 / 环路，在任何花费之前拦截
- **Provenance** —— 每个验证结果都带 `OFFLINE` 或 `REAL` 证据
- **安全边界** —— 无密钥契约、内容扫描、受保护路径

## 三种典型用法

1. **跨 runtime 第二意见**：你日常用 Claude Code，想让 Codex / Gemini 参与
   同一任务。adapter 把所有 runtime 归一到同一个契约，编排代码永远不点名
   任何厂商。
2. **要有凭证的 Agent 干活**：预算先行、LoopGuard 拦截、账本 append-only、
   结果带 provenance —— `REAL` 必须有真实调用证据，否则诚实标注
   `OFFLINE`。无静默回退、无伪造成功。
3. **不绑厂商的 Agent 工具**：实现六方法 `ExternalAgentAdapter` 契约，你的
   runtime 即可接入发现、认证与编排，引擎零改动。

## 两条命令，严格分离

```bash
dual-agent qualify                       # 唯一执行资格认证的命令
dual-agent run "加一个 slug 工具函数和它的测试"
dual-agent run --observe "任务"          # stderr 追加执行事件
```

- `qualify` 执行门控 G1–G14 认证，把 `VERIFIED` + `REAL` 证据持久化到
  `~/.dual-agent/qualification/`。真实调用需要 `RUN_REAL_PROVIDER_TESTS=1`；
  Offline 结果诚实上报且永不持久化 —— Offline 验证不等于 REAL 验证。
- `run` 只读已持久化的证据。没有证据就 exit `2`，机器可读原因
  （`NO_EVIDENCE_NO_QUALIFIER`）—— 绝不自动认证、绝不回退。

模式（`--mode`）：`OFF` 不编排；`AUTO`（默认）分类任务路由；`ON` 强制
dual-agent 路径。嵌入式应用可直接注入配置好的 facade
（`cli.main._facade = my_facade`）。

**多 Agent 协作驾驶舱（V3.2）**：`dual-agent cockpit TASK --step
ROLE=RUNTIME_ID [--step ...]` 按序编排多 Agent 协作，所有引用的 runtime
必须持有已持久化的 `VERIFIED` 证据。退出码：`0` COMPLETED / `2` FAILED /
`3` ABORTED / `4` PARKED。

## 为什么不用 CrewAI / AutoGen / LangGraph？

它们是优秀的 LLM 应用编排框架；本项目解决的是另一个问题 —— 对**已存在于
你机器上的编码 Agent CLI** 施加工程纪律：

| | 常见编排框架 | dual-agent |
|---|---|---|
| 编排对象 | 你自己接线的 LLM API 调用 | 外部编码 Agent CLI（经 adapter） |
| 厂商耦合 | 常绑定单一 SDK/厂商 | runtime 中立，不点名任何厂商 |
| 准入方式 | 配置即用 | 门控 G1–G14 认证，只认 `VERIFIED` + `REAL` 证据 |
| 结果声明 | 框架自行报告 | 每个信封带 provenance，无真实调用证据不给 `REAL` |
| 失败行为 | 回退与重试常作为特性 | 不回退、不静默成功 —— 封闭失败词表 |
| 依赖 | 重型 SDK 栈 | 纯标准库，零运行时依赖 |
| 传输 | 常涉及云/网络 | 仅本机进程边界，无网络传输 |

两者可以一起用：这一层不替代你的应用框架，而是位于应用与 Agent CLI 之间。

## Runtime 支持矩阵

支持级别只有两个：**REAL VERIFIED**（门控认证产出证据并获准入池）与
**Adapter implemented**（离线测试覆盖，本仓库尚未 REAL 验证）。后者请视为
未验证 —— 在你自己的环境跑一次 `dual-agent qualify` 再上生产。

| Agent Runtime | 离线测试 | REAL 验证 |
|---|---|---|
| Claude Code CLI | ✅ | ✅ REAL VERIFIED（全链路 + 真实双 Agent 协作） |
| Codex CLI | ✅ | ✅ REAL VERIFIED（多 runtime 四阶段 E2E 审计，2026-09） |
| Pi | ✅ | ✅ REAL VERIFIED（多 runtime 四阶段 E2E 审计，2026-09） |
| Gemini CLI / Qwen Code / OpenCode / Cline / tiny-agents | ✅ | ❌ 未执行 |

**帮其余 adapter 完成 REAL 验证**是目前最有价值的贡献方式：装好 CLI，带
`RUN_REAL_PROVIDER_TESTS=1` 跑 `dual-agent qualify`，提交你的证据。见
[CONTRIBUTING.md](CONTRIBUTING.md)。

## 安全

- **无密钥契约**：原始输出、密钥、模型推理永不进入 packet、账本、trace 与
  公开结果；`content_safety` 是唯一扫描权威。
- **受保护路径**：REAL 验证对调用方声明的凭据文件做快照，运行期间任何变动
  触发 G13 门失败。
- **最小环境**：adapter 子进程使用白名单环境变量（`PATH` / `HOME` /
  `USERPROFILE` / `SYSTEMROOT`），携带凭据的变量永不转发。
- 真实调用默认关闭，必须显式 `RUN_REAL_PROVIDER_TESTS=1`。
- 引擎永不读取、存储、打印或修改凭据。

## 发布与版本

通过 Trusted Publishing（仅 OIDC，无 token 无密钥）在推送 `vX.Y.Z` tag 时
发布 PyPI；发布前由 [scripts/version_gate.py](scripts/version_gate.py) 强制
tag == `dual_agent.__version__`。每次发布同时生成带产物的 GitHub Release ——
见 [Releases 页面](https://github.com/Tsubasa-Kaede/runtime-neutral-agent-engineering/releases)。

## 参与贡献

Fork → 分支 → 离线测试保持绿 → PR。当前最有价值的两项贡献：[新增 runtime
adapter](CONTRIBUTING.md#add-a-new-runtime-adapter) 与 [为既有 adapter 完成
REAL 验证](CONTRIBUTING.md#real-verify-an-adapter-community-program)。
行为准则见 [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)。

## 许可证

MIT —— 见 [LICENSE](LICENSE)。

## 深入文档（英文）

- 架构：[docs/architecture/overview.md](docs/architecture/overview.md)
- 开发：[docs/development/development-guide.md](docs/development/development-guide.md)
- 测试：[docs/development/testing.md](docs/development/testing.md)
- Adapter 契约：[dual-agent-development/references/adapter-contract.md](dual-agent-development/references/adapter-contract.md)
