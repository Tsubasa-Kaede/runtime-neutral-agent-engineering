# 快速开始（Getting Started）

> 如何在五分钟内在本地跑起本项目 —— 离线、无需任何 runtime 凭据。
> 引擎是纯 Python 标准库。

## 项目结构

```text
dual-agent-development/
├── SKILL.md                  # skill 入口（角色边界、硬规则）
├── scripts/                  # 全部引擎模块（安装为包 dual_agent）
│   ├── … discovery / health / validation / pool …   # 准入链
│   ├── … orchestrator / engine / budget / guard …   # 执行
│   ├── … collaboration_* / *_transport …            # 协作栈
│   └── … *_adapter.py                               # runtime adapter
├── references/               # adapter-contract.md, workflow.md
├── templates/                # 四个 packet 的 JSON 形态
├── agents/                   # 角色 prompt（architect/coder/tester/reviewer）
├── examples/                 # offline_mock_run.py —— 最快的端到端演示
└── …

tests/                        # 离线套件 + 门控 REAL 测试
docs/                         # 本文档树
scripts/validate_skill.py     # skill 结构校验
pyproject.toml                # 包映射 (scripts/ -> dual_agent)
```

## 环境

- **Python 3.10+**（在 3.12 上开发）。无第三方运行时依赖；只有当你
  偏好用 pytest 而非 unittest 跑套件时才需要 `pytest`。
- 操作系统：在 Windows 上开发并完成 REAL 验证；离线部分全部是跨平台的
  标准库。

## 安装

```bash
git clone <repo-url>
cd dual-agent-development-repo
pip install -e .          # 安装 dual_agent 包 + dual-agent CLI
```

可编辑安装对开发已足够；无需其它配置。

## 首次运行（离线，无需 runtime）

```bash
python examples/offline_mock_run.py
```

预期输出：一份封闭、无秘密的 JSON 摘要，含
`"path": "FOUR_STAGE", "status": "SUCCESS"`。

## 运行测试套件

```bash
python -m pytest tests/ -q          # 离线套件（REAL 门控测试 skip）
python -m unittest discover -s tests   # 等价的 stdlib 运行器
python -m compileall dual-agent-development   # 语法门
```

健康的 checkout 表现为：全部离线测试通过，外加一组**门控 skip** ——
那些是 opt-in 的 REAL-runtime 测试（见 [testing.md](testing.md)）。
skip 数量随门控测试的加入而增长；套件输出是权威，不是任何写在文档里
的数字。

## 本地开发循环

1. 动任何一层之前，先读相关的
   [架构文档](../architecture/overview.md)。
2. 做满足你所实现契约的最小改动。
3. 保持套件绿色；每次提交前运行它。
4. 完整的 contract-first 工作流见
   [development-guide.md](development-guide.md)。

## Runtime 前置条件（全部可选）

引擎在离线 mock adapter 下完全可用。真实 runtime 是严格 opt-in 的：

| Runtime | 前置条件 | 说明 |
|---|---|---|
| Claude Code CLI | PATH 上有 `claude`，经其自身登录流程登录 | REAL validation 测试使用；引擎绝不替你登录或登出 |
| tiny-agents | PATH 上有 `tiny-agents` **且**设置了 `TINY_AGENTS_AGENT_PATH` + `TINY_AGENTS_COMMAND` | 三者缺一即诚实地缺席（不注册），绝不半配置 |
| 其它任意 CLI | 实现 adapter 契约（`references/adapter-contract.md`） | 新增 runtime 绝不意味着修改 orchestrator |

**绝不把秘密放进仓库、测试、环境变量默认值或文档。** 引擎的契约在
构造上就是无秘密的；保持这一点。真实调用被
`RUN_REAL_PROVIDER_TESTS=1` 门控且默认关闭 —— 见
[real-runtime.md](real-runtime.md)。
