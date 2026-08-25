# 测试（Testing）

> 套件覆盖什么、各层如何映射到测试文件，以及"一切真实的都是 opt-in"
> 这条规则。**OFFLINE ≠ REAL。**

## 命令

```bash
python -m pytest tests/ -q                # 完整离线套件（门控 REAL 测试 skip）
python -m unittest discover -s tests      # 等价的 stdlib 运行器
python -m compileall dual-agent-development   # 语法门
RUN_REAL_PROVIDER_TESTS=1 python -m pytest tests/<real_test>.py -v   # 门控 REAL 运行
```

## 测试层次

| 层 | 证明什么 | 代表文件 |
|---|---|---|
| **Unit** | 单个模块在隔离下的契约（注入时钟、fake、无 I/O） | `test_task_budget.py`, `test_loop_guard.py`, `test_runtime_status.py`, `test_capability_registry.py`, `test_structured_packets.py`, `test_collaboration_state.py` |
| **Integration** | 引擎内部各层的组合，仍完全离线 | `test_runtime_integration.py`, `test_execution_engine.py`, `test_orchestrator.py`, `test_phase10*`（discovery/health/pool/selection）, `test_discovery_bootstrap.py`, `test_rc3_host_bootstrap.py` |
| **E2E（离线）** | mock/应答式 adapter 之上的生产 facade/CLI | `test_production_facade.py`, `test_cli.py`, `test_host_integration.py`, `examples/offline_mock_run.py` |
| **E2E（REAL，门控）** | 同样路径之上的真实已安装 runtime | `test_real_runtime_validation.py`, `test_real_claude_health.py`, `test_rc3_real_discovery.py` |

## Offline 与 REAL

- **离线**测试使用注入的 executor、mock 与确定性时钟。它们验证的是
  *契约*：schema、词汇、gate 顺序、拒绝语义、拒绝伪造。它们**永远**
  不是真实能力的证据 —— 其结果在构造上携带
  `provenance="OFFLINE"`。
- **REAL** 测试经由受认可的 gate
  （`RUN_REAL_PROVIDER_TESTS=1`）调用真实 runtime。只有完整的门控
  运行才能产出 `VERIFIED` + `REAL` 证据。runner 在结构上拒绝没有
  真实调用证据的 `REAL` provenance，因此两者绝不可能被意外混同。

gate 是双重检查的（helper 层与 executor 层）；gate 关闭时 REAL 测试
**skip** —— 在套件输出中体现为门控 skip。skip 数随门控测试的加入
而增长；以套件输出为权威，不要以文档引用的数字为准。

## 保持套件确定性的约定

- 注入时钟（`clock=lambda: …`）—— 不做墙钟时间断言、不 sleep。
- 无网络、无 temp 路径之外的文件系统夹具、离线测试中无真实子进程。
- 离线测试中的 adapter 在契约说"不允许调用"的地方于 invoke 时抛出
  `AssertionError` —— 证明否定面。
- 少数边界存在源码文本断言（例如 runtime 中立模块不得提及 runtime
  名）；在这些模块里新增注释/docstring 必须保持 marker 干净。

## REAL 测试卫生

- 门控测试只打印**封闭、无秘密的摘要**（含分类字段的结构化 JSON）；
  绝不打印 prompt、原始输出、路径或凭据。
- 受保护路径（凭据/配置）会被快照；任何改动都会使 G13 失败该运行。
- 依赖机器的结果被诚实地断言："health 就是真实机器报告的样子"，
  非 READY 分支提前诚实返回 —— REAL 测试绝不伪造一个它没有观测到的
  机器状态。

## 如何解读一次运行

| 观察 | 含义 |
|---|---|
| 全部通过 + 门控 skip | 健康的离线 checkout |
| 一列门控 skip | 本树中在册的 opt-in REAL 测试 |
| `test_phase10*` / `test_rc3_*` 失败 | 准入/选择契约回归 —— 先查边界文档 |
| REAL 测试失败 | 真实缺陷或环境变化的真实证据；读结构化的失败类别，不要盲目重试 |
