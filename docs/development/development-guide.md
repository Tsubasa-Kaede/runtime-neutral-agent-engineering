# 开发指南（Development Guide）

> 本项目赖以构建的工作流（测试先行、逐阶段），以及保持一个 Runtime
> 中立引擎诚实运转的规则。

## 核心原则

1. **Contract First（契约先行）** —— 先定义数据契约（frozen dataclass、
   封闭词汇、验证规则），再写任何行为。契约就是可评审的单元。
2. **Boundary First（边界先行）** —— 写代码前先决定哪一层拥有这个
   决策。Discovery 绝不回答 Health；Health 绝不回答 Capability；
   verified 路径绝不借用 ready pool。如果一个改动需要某个层回答它不
   拥有的问题，那就是边界错了。
3. **TDD** —— 先 RED，再 GREEN，再重构。V2 的每个阶段都是这样构建的；
   套件就是回归网。
4. **Minimal Change（最小改动）** —— 满足契约的最小 diff。不顺手
   重构、不"既然来了就优化"、不做重排扫荡。
5. **Verification（验证）** —— 证据先于断言。一个说法（"能跑"、
   "通过"、"已验证"）的分量等同于它背后的命令输出。

## 工作流

```text
Read Architecture          读架构
  ↓
Identify Boundary          这个决策属于哪一层？
  ↓
Define Contract            frozen data + 封闭词汇 + 验证
  ↓
TDD RED                    表达契约的失败测试
  ↓
Minimum Implementation     让测试变绿的最小代码
  ↓
Offline Verification       python -m pytest tests/ -q   (+ compileall)
  ↓
REAL Verification          仅当改动触及真实 runtime 行为时：
                           RUN_REAL_PROVIDER_TESTS=1 跑门控测试
  ↓
Security Scan              对 diff 做秘密形态扫描；无原始输出/凭据
  ↓
Protected Diff             受保护路径（凭据/配置）未变
  ↓
Commit                     一个原子、已验证的增量；不自动 push
```

### 1–2. 读架构、定边界

从 [../architecture/overview.md](../architecture/overview.md) 和模块
地图开始。写下哪个模块将改、哪些不改。如果答案出乎意料地跨层，停下来
重读边界规则 —— 静默跨层正是 `DISCOVERED == READY` 这类 bug 的出生
方式。

### 3. 定义契约

优先 frozen dataclass + `__post_init__` 验证 + 封闭 enum 词汇。错误
字符串也是契约：分类化、稳定、无秘密（`MISSING_HANDOFF`、
`NO_CAPABLE_AGENT`、`BUDGET_EXHAUSTED` —— 绝不是原始诊断）。

### 4–5. TDD

先写失败的测试。测试放在 `tests/` 下、与被测模块同名
（`test_<module>.py`）。离线测试绝不调用真实 runtime；一切真实的
东西都进 gate。

### 6. 离线验证

```bash
python -m pytest tests/ -q
python -m compileall dual-agent-development
```

两者都必须干净才能继续。不要相信记忆中的结果。

### 7. REAL 验证（有条件）

仅针对触及真实 runtime 行为的改动（adapter、gate executor、面向真实
CLI 的 discovery/health）：

```bash
RUN_REAL_PROVIDER_TESTS=1 python -m pytest tests/test_real_runtime_validation.py -v
RUN_REAL_PROVIDER_TESTS=1 python -m pytest tests/test_rc3_real_discovery.py -v
```

门控链证明了什么、安全规则是什么，见 [real-runtime.md](real-runtime.md)。
**OFFLINE ≠ REAL**：离线通过永远不是真实能力的证据。

### 8. 安全扫描

- diff 不得加入秘密、token、原始 runtime 输出或凭据形态的字面量。
- 承载文本的新表面（reason、evidence、summary）必须在构造上无秘密 ——
  复用既有的扫描权威（`content_safety`、marker 检查），绝不另起炉灶。

### 9. Protected diff

REAL 运行声明受保护路径（凭据/配置文件），若被改动则 G13 失败。任何
REAL 验证之后，确认受保护文件未变。离线工作绝不触碰它们。

### 10. 提交

一个已验证的增量 = 一个原子提交。提交前审阅 `git diff --stat` 与完整
diff；绝不自动 push。提交信息沿用既有约定
（`feat:` / `test:` / `docs:` + 一行）。

## 硬规则（全项目）

- 引擎中不得出现任何 runtime、provider 或模型名 —— adapter 是 runtime
  知识唯一存在的地方。
- 无静默 fallback、无成功包装、无伪造的 packet 或证据。
- 原始 stdout/stderr、秘密与模型推理绝不进入 packet、ledger、trace
  或报告。
- 测试是确定性的：注入时钟、不 sleep 墙钟时间、不联网。
- 文档说法必须与源码一致；无法一致时，修其一 —— 绝不让它们漂移。
