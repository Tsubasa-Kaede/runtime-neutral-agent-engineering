"""P1-U1 + P1-U2b: 默认 host 组合根 —— 开箱入口的纯接线层。

安装后 `dual-agent run "task"`（console script）与 `python -m dual_agent`
（包内入口 __main__.py）都能直接进入现有 CLI，而不要求使用方手工构造
并注入 facade。本模块是唯一的 host 侧默认组合点，只做接线，绝不创建
第二套 bootstrap / discovery / selection / execution 逻辑：

    environment_registry     家族 from_environment → 既有 AdapterRegistry
                             （环境发现的真相住在各家 adapter 内；本模块
                             零 runtime 名分支，只按同一契约循环注册）
        ↓ observe_current_health
                             既有 GenericRuntimeHealth 对可用候选的时点
                             观测 → {runtime_id: RuntimeStatus}，绝不凭空
                             构造 READY 快照
        ↓ build_facade_from_bootstrap
                             既有 host.py 的 "Automatic entry"：内部即
                             bootstrap_runtime_session（discovery →
                             health → evidence 复用/qualification →
                             VerifiedRuntimePool）→ admitted → build_facade
                             （既有 VerifiedOrchestrator +
                             CollaborationOrchestrator + V2 执行链）
        ↓ 既有 run_cli（parse → policy → facade.run → 安全 JSON 渲染）
                             cli.py 保持 V2 冻结逐字节原状（零修改）；
                             渲染后的 exit 语义映射在本层（见下）
    cli.main(argv)           嵌入面：注入式入口原样保留（cli.py 既有行为）

P1-U2b 资格面（P1-EVIDENCE-BOUNDARY DESIGN READY 的实现）：
- `dual-agent qualify`：显式 qualification surface —— 读发现 → 既有
  bootstrap_runtime_session 的 qualifier 注入位执行 run_real_validation
  （adapter 真相 = bootstrap 构造 instance 时挂上的 probe；REAL 开门
  语义在 GATE_ENV_NAME 内，本模块绝不放宽）→ VERIFIED+REAL 结果经
  evidence_store.save_evidence 原子落盘。cli.py 零修改（argv[0] 分流）。
- `dual-agent run`：只读盘（evidence 未注入时 load_evidence 默认目录），
  绝不自动触发 REAL qualification —— 即使调用方显式传入 qualifier，
  run 路径也不使用它。无证据 = 既有诚实拒绝 + 指引 dual-agent qualify。

P1-U3 CLI 语义稳定契约（stdout=机器 JSON / stderr=人类诊断 / exit 稳定）：
- 语义失败（组合期 RuntimeError/ValueError 封闭词表）→ stdout 机器 JSON
  {"status": "NOT_QUALIFIED", "reason": <封闭词>, "detail": ..., "hint"?}
  + stderr 人类行 + exit 2。系统性 IO（OSError）不属于语义结果 →
  stderr-only + exit 2（expected 语义失败 vs 系统故障的既有区分）。
- --version/--help/-h 在组合之前直接交给既有 CLI（argparse parse-first
  先例）：新机无证据也能拿到版本/帮助，绝不先组合失败。
- 执行结果的 exit 映射 = exit_code_for：封闭成功词 "SUCCESS" → 0，
  任何封闭失败词 → 2。映射住在本组合根而非 cli.py —— cli.py 是 V2
  冻结件（五个 zero-diff 纪律测试钉定工作树零修改），而产品边界
  （console script / python -m）本来就是本层；纯呈现函数，无
  qualification/admission truth。
- 默认路径 ~/.dual-agent/qualification/ 只在本层提供；库面
  （evidence_store）强制显式 base_dir，测试一律 TemporaryDirectory。

P1-U4 打包/发布 UX 增量：
- 顶层 --help/-h 与 `qualify --help` 打印产品级帮助（_PRODUCT_HELP），
  明确列出 run / qualify / --observe 与首用流程 —— argparse 顶层 help
  只认 run 子命令（cli.py 冻结零修改），产品可发现性由本组合根补齐。
  纯静态文本：无证据、无发现、无组合、无网络。
- CandidateValidationStatus 改经平铺名导入（单一 module graph 纪律，
  见 import 处注释）：安装态包相对导入会实例化第二套 enum 类，
  qualify 的 VERIFIED `is` 判定跨图必假 —— 打包冒烟实测出的断裂点。

CU-P0（qualify observability + configurable timeout）：
- `qualify --timeout-seconds N`（空格或 = 赋值，恰一次，正 float）只覆盖
  G5/G14 每次 REAL model invocation 的 timeout；默认 300.0 不变，health
  的 min(30, ...) 与 G1/G2 固定界零触碰 —— 不改默认执行语义。解析住
  本组合根（cli.py 冻结零修改，--version/--help parse-first 先例同层）。
- G5/G14 逐调用进度行只走 stderr（stdout 单 JSON 契约独占），仅经默认
  REAL 桥发射；gate 关闭时零事件（不产生假的 invocation progress）。

CU-R3a（opaque task_id at the CLI composition boundary）：
- CLI 唯一 task 实参在进入 engine 前把 task_id 映射为确定性不透明摘要
  （sha256 前 12 hex，"task_" 前缀）；task/prompt 原文透传。词域误拒
  （任务正文含 marker 子串 → 标识符层『拒绝提及』）结构性消失，
  identifier 安全策略/扫描器零改动；stdout 8 键 schema 不变，task_id
  语义变为不透明（已批准变更）。

边界与诚实性：
- evidence 注入优先于盘：显式 evidence 参数（嵌入方面）原样传递，
  绝不被空盘覆盖。
- current_health 来自真实时点观测：未开启 REAL gate 的机器观测不出
  READY，后续链路按既有语义诚实失败 —— 本模块绝不放宽。
- provider-less 家族（profile.provider 为 None，如 L0 配置型）在默认
  注册面被诚实跳过（skipped 记录，非错误）：identity 四元组要求非空
  provider，代造身份是伪造。
- 家族缺席（from_environment → None）= 诚实静默：不注册、不报错、
  无半配置 descriptor（各家 adapter 的既有契约）。
- 双重健康观测的诚实代价：本模块观测一次（供 current_health 快照），
  build_facade_from_bootstrap 内部的 bootstrap_runtime_session 再观测
  一次（session 判定）。两次都是真实观测；消除重复需要修改 host.py
  （冻结边界），故如实接受。
- 入口一次性：main 结束后清空 cli.main._facade，不向同进程泄漏陈旧
  facade；嵌入方需要显式控制时请直接使用 default_facade 并自行注入。
"""
from __future__ import annotations

import hashlib
import importlib
import json
import sys
import types
from pathlib import Path

__all__ = (
    "DEFAULT_TIMEOUT_SECONDS",
    "DEFAULT_EVIDENCE_DIR",
    "environment_registry",
    "observe_current_health",
    "default_facade",
    "qualify_runtimes",
    "exit_code_for",
    "main",
)

DEFAULT_TIMEOUT_SECONDS = 300.0

# 默认 evidence 目录只在本组合层提供（授权 §8：CLI/host composition 层
# 负责默认路径；库面 evidence_store 强制显式 base_dir）。
DEFAULT_EVIDENCE_DIR = Path.home() / ".dual-agent" / "qualification"

# argparse 自处理标志：组合之前直接交给既有 CLI（RELEASE-2A parse-first
# 先例）—— 新机无证据/无家族也能拿到版本与帮助，绝不先组合失败。
_ARGPARSE_FLAGS = ("--version", "--help", "-h")

# CU-P0：qualify 的唯一可配置参数（每 invocation timeout，默认不变）。
_QUALIFY_TIMEOUT_FLAG = "--timeout-seconds"

_HINT_QUALIFY = "no persisted qualification evidence: run `dual-agent qualify` first"

# 顶层产品帮助（P1-U4 §6）：cli.py 的 argparse 只认 run 子命令（V2 冻结
# 零修改），qualify 的可发现性由本组合根提供。纯静态文本：无证据、无
# 发现、无组合、无网络即可打印；run 绝不自动 qualify 在此明示。
_PRODUCT_HELP = """dual-agent: runtime-neutral agent collaboration (local product entry)

Usage:
  dual-agent --version
  dual-agent --help
  dual-agent qualify [--timeout-seconds <seconds>]
  dual-agent run [--mode off|auto|on] [--observe] "<task>"

Commands:
  qualify   Explicitly qualify discovered runtimes through the G1-G14 gate
            chain and persist VERIFIED+REAL evidence under
            ~/.dual-agent/qualification/. REAL invocation requires
            RUN_REAL_PROVIDER_TESTS=1; offline results are reported
            honestly and are never persisted. Per-invocation model-call
            timeout defaults to 300 seconds; --timeout-seconds overrides
            it, and G5/G14 invocation progress streams to stderr.
  run       Execute a collaboration task through verified runtimes,
            reading persisted evidence. run never automatically qualifies:
            with no evidence it exits 2 and points to
            `dual-agent qualify`.

Run flags:
  --mode off|auto|on    orchestration mode (default auto)
  --observe             stream execution observation events to stderr;
                        stdout stays exactly one machine-readable JSON line
  --runtimes, --min-runtimes, --max-runtimes, --no-runtime-reuse
                        runtime policy (details: `dual-agent run --help`)

First use:
  dual-agent qualify
  dual-agent run "refactor the parser module"
"""

try:  # installed-package mode: dependencies are package siblings
    from .cli import main as cli_main, run_cli
    from .discovery_bootstrap import bootstrap_runtime_session
    from .evidence_store import load_evidence, save_evidence
    from .external_runtime import new_invocation_id
    from .generic_runtime_health import GenericRuntimeHealth
    from .host import (
        _CAPS_ALL,
        build_facade,
        build_facade_from_bootstrap,
    )
    from .real_validation_executor import run_real_validation
    from .runtime_adapter_registry import (
        AdapterDescriptor,
        AdapterRegistry,
        discovery_sources,
    )
    from .runtime_discovery import RuntimeCandidateDiscovery
except ImportError:  # source-tree flat-import mode (tests/examples)
    from cli import main as cli_main, run_cli
    from discovery_bootstrap import bootstrap_runtime_session
    from evidence_store import load_evidence, save_evidence
    from external_runtime import new_invocation_id
    from generic_runtime_health import GenericRuntimeHealth
    from host import (
        _CAPS_ALL,
        build_facade,
        build_facade_from_bootstrap,
    )
    from real_validation_executor import run_real_validation
    from runtime_adapter_registry import (
        AdapterDescriptor,
        AdapterRegistry,
        discovery_sources,
    )
    from runtime_discovery import RuntimeCandidateDiscovery

# 单一 module graph 纪律（P1-U4 打包冒烟实测出的安装态断裂点）：
# CandidateValidationStatus 必须经平铺名导入。安装态下包相对导入会
# 实例化第二套 enum 类，而 discovery_bootstrap / real_validation_executor /
# evidence_store 的内部平铺 import 用的是另一套 —— `is` 身份比较跨图
# 必假，qualify 的 VERIFIED 判定就会静默失守（实测：安装态 qualify
# 报成功但零落盘）。shim（dual_agent/__init__.py）保证两种模式下平铺
# 名都可用；其余导入是函数/工厂（无身份比较），双模式原样保留。
from candidate_validation import CandidateValidationStatus
from collaboration_state import CollaborationStateError
from content_safety import (
    last_diagnostic_generation,
    last_validation_diagnostic,
    next_diagnostic_generation,
)
import packet_forensics

# P1-1（multi-runtime composition wiring）：既有 V3.0 组合根的只读复用 ——
# 本模块零新裁决、零 composition 逻辑复制（agent_host 已按不变量锁定）。
from agent_host import build_facade_from_agents
from agent_identity import AgentIdentity, AgentRuntimeBinding
from agent_manifest import AgentManifest, AgentRegistry

# 默认家族接线表（数据，非行为）：每家环境发现的真相在其自身
# from_environment 内 —— 这里只登记 (模块, 类型) 并按同一契约循环，
# 绝不按 runtime 名分支。顺序即注册顺序（确定性）。
_FAMILY_MODULES = (
    ("claude_code_adapter", "ClaudeCodeAdapter"),
    ("codex_adapter", "CodexAdapter"),
    ("pi_adapter", "PiAdapter"),
    ("gemini_adapter", "GeminiAdapter"),
    ("qwen_adapter", "QwenCodeAdapter"),
    ("opencode_adapter", "OpenCodeAdapter"),
    ("cline_adapter", "ClineAdapter"),
    ("tiny_agents_adapter", "TinyAgentsAdapter"),
)


def _default_factories():
    """枚举家族 from_environment callable（绝不执行它们 —— 探测只发生在
    environment_registry 的循环里）。"""
    factories = []
    for module_name, class_name in _FAMILY_MODULES:
        if __package__:  # installed-package mode（平铺态 __package__ 为 ""）
            module = importlib.import_module(f".{module_name}", __package__)
        else:
            module = importlib.import_module(module_name)
        factories.append(getattr(module, class_name).from_environment)
    return tuple(factories)


def environment_registry(factories=None):
    """家族环境发现 → 既有 AdapterRegistry 的注册接线。

    每家 from_environment 的结果就是注册真相：None（环境缺席）不注册；
    成功则按其 profile 声明注册 descriptor（identity 四元组的来源即
    profile 声明 + 默认 config fingerprint）。provider 为 None 的家族
    被记入 skipped（identity 四元组要求非空 provider，代造即伪造）。
    返回 (registry, skipped)。
    """
    if factories is None:
        factories = _default_factories()
    registry = AdapterRegistry()
    skipped = []
    for factory in factories:
        adapter = factory()
        if adapter is None:
            continue
        profile = adapter.profile
        if not profile.provider:
            skipped.append(profile.runtime)
            continue

        def _stable(candidate=adapter):
            return candidate

        registry.register(AdapterDescriptor(
            runtime_id=profile.runtime,
            provider_id=profile.provider,
            runtime_type=profile.agent_id,
            display_name=profile.runtime,
            adapter_factory=_stable,
            model_id=profile.model,
            config_fingerprint="default",
        ))
    return registry, tuple(skipped)


def observe_current_health(registry):
    """既有通用健康管线对注册候选的时点观测。

    复用 RuntimeCandidateDiscovery + discovery_sources +
    GenericRuntimeHealth：产出 {runtime_id: RuntimeStatus}。这是真实观测
    （未开 REAL gate 的机器观测不出 READY），绝不凭空构造快照。
    """
    health = GenericRuntimeHealth()
    discovery = RuntimeCandidateDiscovery(discovery_sources(registry))
    statuses = {}
    for candidate in discovery.discover_all():
        descriptor = registry.get(candidate.runtime_id)
        statuses[candidate.runtime_id] = health.check(
            candidate, descriptor.adapter_factory()).status
    return statuses


# CLI 组合翻译层的确定性 agent 命名（V3.0-A 纪律：agent_id 不含任何
# runtime/provider/model/config 事实 —— 名字只有位置序号，顺序 = sorted
# runtime_id；与 runtime 的对应关系由 binding 携带，同输入必同输出）。
_CLI_AGENT_ID = "cli-agent-"

# 声明角色 = 资格证据已证明的四协作能力投影：bootstrap 以 _CAPS_ALL 准入
# ⇒ admitted 必然四能力全证 ⇒ 声明四角色与证据一致（组合根的双向能力
# 一致性不变量因此成立）—— 本层不制造新的能力真相。
_CLI_DECLARED_ROLES = ("architect", "coder", "tester", "reviewer")


def _facade_from_admitted(session, registry, current_health,
                          timeout_seconds):
    """P1-1 翻译层：admitted ≥ 2 → 既有 build_facade_from_agents。

    职责严格局限于 admitted entries → 内存 AgentManifest/binding 投影 →
    确定性 agent_ids → 既有 V3.0 组合根（逐 runtime admission、跨 runtime
    角色地址宇宙、多 runtime 池全部由其不变量锁定，本层零复制零裁决）。
    attribution 仅透传不消费（Observation/Event 契约零改动）。缺
    health/evidence/组合前置的拒绝由组合根封闭词表原样上抛 —— 诚实失败，
    绝不静默降级单 runtime。"""
    agent_registry = AgentRegistry()
    agent_ids = []
    admitted = sorted(
        (entry for entry in session.entries if entry.admitted),
        key=lambda entry: entry.runtime_id)
    for index, entry in enumerate(admitted, start=1):
        descriptor = registry.get(entry.runtime_id)
        agent_id = f"{_CLI_AGENT_ID}{index}"
        agent_registry.register(AgentManifest(
            binding=AgentRuntimeBinding(
                AgentIdentity(agent_id), descriptor.identity),
            declared_roles=_CLI_DECLARED_ROLES,
            adapter_factory=descriptor.adapter_factory,
        ))
        agent_ids.append(agent_id)
    facade, _attribution = build_facade_from_agents(
        agent_registry, tuple(agent_ids), session.evidence,
        current_health, timeout_seconds=timeout_seconds)
    return facade


def default_facade(*, factories=None, evidence=None, qualifier=None,
                   current_health=None,
                   timeout_seconds=DEFAULT_TIMEOUT_SECONDS):
    """默认组合：registry → 时点健康 → bootstrap → 按 admitted 基数分流。

    P1-1（multi-runtime composition wiring）：
    - admitted ≥ 2 → _facade_from_admitted → 既有 build_facade_from_agents：
      admitted 全集装进一个多 runtime 池，角色指派/policy/admission 真相
      全部留在既有件（bridge + assigner/policy + 既有准入机器）。
    - admitted == 1 → 既有单 runtime 组合原样尾部（同一 session 的
      admitted[0] → 既有 build_facade；与既有 Automatic entry 的差别仅在
      不重复 bootstrap —— 输入对象与构造调用逐字相同，健康观测次数与
      既有路径一致）。
    - admitted == 0 → 交给既有 Automatic entry 抛出规范组合失败（含全部
      entry 理由）。
    """
    registry, _skipped = environment_registry(factories)
    if current_health is None:
        current_health = observe_current_health(registry)
    session = bootstrap_runtime_session(
        registry, evidence=evidence, qualifier=qualifier,
        required_capabilities=_CAPS_ALL)
    admitted = [entry for entry in session.entries if entry.admitted]
    if len(admitted) >= 2:
        return _facade_from_admitted(
            session, registry, current_health, timeout_seconds)
    if not admitted:
        return build_facade_from_bootstrap(
            registry, evidence=evidence, qualifier=qualifier,
            current_health=current_health,
            timeout_seconds=timeout_seconds,
        )
    # admitted == 1：既有 Automatic entry 的组合尾部（同一 session、同一
    # admitted[0]、同一既有 build_facade 调用）。
    entry = admitted[0]
    descriptor = registry.get(entry.runtime_id)
    validation = session.evidence[descriptor.identity]
    adapter = descriptor.adapter_factory()
    return build_facade(adapter, validation, current_health,
                        timeout_seconds=timeout_seconds)


def qualify_runtimes(*, factories=None, base_dir=DEFAULT_EVIDENCE_DIR,
                     qualifier=None,
                     timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
                     progress=None):
    """显式 qualification surface（P1-U2b）：读发现 → 执行 qualifier →
    成功（VERIFIED+REAL）即持久化。

    全部编排语义在既有 bootstrap_runtime_session 内（evidence 复用、
    no-retry、admission 一概不重建）。默认 qualifier 是 run_real_validation
    的最小桥：adapter 真相 = bootstrap 构造 instance 时挂上的 probe
    （rc3 / gemini REAL 驱动同款接线），REAL 开门语义留在 GATE_ENV_NAME
    内 —— OFFLINE 结果照常返回、照常被 admission 拒绝，只是不落盘。
    持久化只发生在成功结果上：persistence 保存事实，不制造事实。
    progress（CU-P0）只在默认桥里生效：G5/G14 逐调用进度行交给回调；
    注入 qualifier 的嵌入面按其自身契约行事，本层不干预。
    返回 (session, rejected, saved_paths)。"""
    registry, _skipped = environment_registry(factories)
    if not registry.list():
        raise RuntimeError("NO RUNTIMES REGISTERED")
    evidence, rejected = load_evidence(base_dir)

    if qualifier is None:
        # 一次 qualification run 一个 experiment 标签（仓库语义：调用方
        # 贴标签；复用既有 new_invocation_id 机制）。verified_selection_
        # bridge 只消费非空 experiment_id 的池条目 —— REAL 资格若不带
        # 标签，落盘证据会被所有 selection 路径滤除（2.2.0 用户态 REAL
        # E2E 实测出的缺陷）。
        experiment_id = f"qualify-{new_invocation_id()}"

        def _real_bridge(instance):
            result, _executor = run_real_validation(
                instance, instance.probe,
                experiment_id=experiment_id,
                timeout_seconds=timeout_seconds,
                progress=progress)
            return result

        qualifier = _real_bridge

    saved = []

    def _persisting_qualifier(instance):
        result = qualifier(instance)
        if (result.status is CandidateValidationStatus.VERIFIED
                and result.provenance == "REAL"):
            saved.append(save_evidence(base_dir, result))
        return result

    session = bootstrap_runtime_session(
        registry, evidence=evidence, qualifier=_persisting_qualifier)
    return session, rejected, tuple(saved)


def exit_code_for(status: str) -> int:
    """P1-U3 稳定 exit 映射：封闭成功词恰为 "SUCCESS"（ExecutionStatus /
    CollaborationStatus / VerificationStatus 三枚举交叉验证，另有
    DUAL_NO_CAPABLE_AGENT / NO_VERIFICATION_CAPABILITY 两特例失败词）
    → 0；其余封闭失败词一律 → 2。纯呈现函数：stateless、确定性、
    绝不承载 qualification / admission / selection truth。住在组合根
    而非 cli.py —— cli.py 是 V2 冻结件，产品边界本来就是本层。"""
    return 0 if status == "SUCCESS" else 2


def _composition_reason(message: str) -> str:
    """从既有 bootstrap RuntimeError 封闭词表提取稳定 reason（纯投影）。

    消息形如 "no admitted verified runtime (rt-a:REASON; rt-b:REASON)"：
    取首个 per-runtime 原因（词表封闭：NO_EVIDENCE_NO_QUALIFIER /
    HEALTH_* / NOT ADMITTED ... / NO RUNTIMES REGISTERED）。绝不改写
    语义 —— 完整原文始终在 detail 字段。"""
    inner = message[message.find("(") + 1:] if "(" in message else message
    first = inner.split(";", 1)[0].rstrip(")")
    if ":" in first:
        return first.split(":", 1)[1].strip()
    return first.strip()


def _semantic_failure(reason: str, message: str, human: str | None = None) -> int:
    """P1-U3 语义失败契约：stdout 机器 JSON + stderr 人类行 + exit 2。

    human 可覆盖 stderr 人类行（CU-R3b：ledger 安全拒收不是 runtime
    资格问题，不得误导）；缺省行为与 P1-U3 逐字节一致。"""
    payload = {"status": "NOT_QUALIFIED", "reason": reason, "detail": message}
    if "NO_EVIDENCE_NO_QUALIFIER" in message:
        payload["hint"] = _HINT_QUALIFY
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    human_default = ("No qualified runtime evidence is available. "
                     "Run `dual-agent qualify` first."
                     if "NO_EVIDENCE_NO_QUALIFIER" in message
                     else f"dual-agent: no admitted verified runtime ({reason})")
    print(human or human_default, file=sys.stderr)
    return 2


def _print_rejections(rejected) -> None:
    for item in rejected:
        print(f"dual-agent: evidence rejected: {item.filename}: "
              f"{item.reason}: {item.detail}", file=sys.stderr)


_RESULT_PREFIX = "dual-agent: result "


def _projection_items(out, label, values) -> None:
    """列表字段逐项一行（1 基序号，确定性输出）。"""
    for index, value in enumerate(values, start=1):
        out.append(f"{_RESULT_PREFIX}{label}[{index}]: {value}")


def _packet_projection_lines(envelope) -> list:
    """单个已验证 envelope 的白名单投影。

    只读 payload 的字符串/字符串元组字段；dict 形状字段（interfaces/
    implementation_steps/risks/failures/findings）按 CU-R1 设计只计数、
    不展开（自由形状不做渲染面）。字段清单是封闭白名单：goal/
    architecture/constraints；summary/changed_files/details/unresolved；
    测试计数/coverage/remaining_risks；status/severity/required_changes/
    findings_count。绝不输出 wire 原文、trace、agent 地址或
    correlation_id。
    """
    payload = envelope.payload
    kind = envelope.payload_type.value
    out = []
    if kind == "ARCHITECTURE":
        _projection_items(out, "architecture goal", payload.goal)
        _projection_items(out, "architecture", payload.architecture)
        _projection_items(out, "architecture constraints", payload.constraints)
    elif kind == "IMPLEMENTATION":
        out.append(f"{_RESULT_PREFIX}implementation summary: "
                   f"{payload.implementation_summary}")
        _projection_items(out, "implementation changed_files",
                          payload.changed_files)
        _projection_items(out, "implementation details",
                          payload.implementation_details)
        _projection_items(out, "implementation unresolved",
                          payload.unresolved_items)
    elif kind == "TEST":
        out.append(f"{_RESULT_PREFIX}tests run={len(payload.tests_run)} "
                   f"passed={len(payload.tests_passed)} "
                   f"failed={len(payload.tests_failed)} "
                   f"failure_items={len(payload.failures)}")
        _projection_items(out, "tests coverage",
                          payload.coverage_or_validation)
        _projection_items(out, "tests remaining_risks",
                          payload.remaining_risks)
    elif kind == "REVIEW":
        out.append(f"{_RESULT_PREFIX}review status: {payload.status}")
        _projection_items(out, "review severity", payload.severity)
        _projection_items(out, "review required_changes",
                          payload.required_changes)
        out.append(f"{_RESULT_PREFIX}review findings_count="
                   f"{len(payload.findings)}")
    return out


def _result_projection_lines(state, task_id) -> tuple:
    """CU-R1：ledger → 安全结果摘要行（纯函数：无 I/O、无时间、无随机）。

    数据源是 append-only 账本里已经固化的 envelope wire —— append 之前
    payload 已通过 packet 构造（schema + secret-shape 扫描）与全包 unsafe
    扫描，envelope() 重解码构成读取侧二次验证。逐 record 按 sequence：
    DECISION 静默跳过；FAILURE → 诚实未交付行（不伪造结果）；解码失败 →
    skip 行绝不中断；REQUEST/REPLY → 白名单投影。SINGLE/OFF 路径的
    payload 不入账本（结构事实）→ 自然零投影。
    """
    lines = []
    for record in state.history(task_id):
        direction = record.direction.value
        if direction == "DECISION":
            continue
        if direction == "FAILURE":
            lines.append(f"{_RESULT_PREFIX}not delivered: {record.status}")
            continue
        try:
            envelope = record.envelope()
        except (ValueError, TypeError):
            lines.append(f"{_RESULT_PREFIX}record skipped: UNDECODABLE")
            continue
        lines.extend(_packet_projection_lines(envelope))
    return tuple(lines)


def _packet_reject_line(status, generation) -> str | None:
    """CU-R2：*_PACKET_INVALID 终态 → 值安全的拒绝诊断行（或 None）。

    只读全局诊断槽并比对代数（run 前由 _main_run 换代）：代数匹配才
    采信 —— 陈旧 REJECT 结构性无法冒充本 run 观测。诊断坐标来自
    structured_packets/content_safety 的 R6-C11 契约（layer/field/
    rule，绝不含被拒值）。无新鲜诊断时只能按消去法报告 G2/G3 的
    不可分诊形态 —— JSON 解析失败与非对象顶层住在冻结解析器内，
    本层无证据区分，绝不声称可分。
    """
    if not status.endswith("_PACKET_INVALID"):
        return None
    stage = status[: -len("_PACKET_INVALID")].lower()
    diagnostic = last_validation_diagnostic()
    if (diagnostic is not None
            and last_diagnostic_generation() == generation):
        return (f"dual-agent: packet reject stage={stage} "
                f"rule={diagnostic.rule} field={diagnostic.field} "
                f"layer={diagnostic.layer}")
    return (f"dual-agent: packet reject stage={stage} "
            f"rule=JSON_PARSE_OR_NON_OBJECT")


def _opaque_task_id(task_id: str) -> str:
    """CU-R3a：任务正文 → 确定性不透明标识（标准库 sha256 摘要前 12 位
    十六进制，"task_" 前缀 —— 无连字符，结构性不构成 "sk-" 等任何凭据
    形状子串）。无时间/随机/PID/路径/runtime 依赖：同一正文必产同一 id
    （retry 身份确定性保持），hex 词表 [0-9a-f] 不含任何 secret marker
    子串，也不含用户任务正文。"""
    digest = hashlib.sha256(task_id.encode("utf-8")).hexdigest()[:12]
    return f"task_{digest}"


def _cli_task_boundary(facade):
    """CU-R3a 组合边界：CLI host 路径把唯一的 task 实参映射为不透明
    task_id。

    既有 CLI（冻结件）对 facade.run 传 task_id==task==prompt（同一
    实参）；engine 的标识符层（ledger/envelope/observation）对标识符
    拒绝 marker『提及』是有文档的安全策略 —— 任务散文进标识符区即与
    该策略碰撞（REAL 已证：record_tokens → token 误拒）。本代理只把
    task_id 换成 _opaque_task_id 摘要：task/prompt 原文与其余实参
    （mode/observation_sink/policy）原样透传，返回值与异常原样传播
    （绝不吞掉）。代理仅存在于 run_cli 调用点 —— default_facade 的
    直接嵌入 API（task_id 显式指定）行为不变。"""
    def _run(task_id, task, prompt, **kwargs):
        return facade.run(task_id=_opaque_task_id(task_id),
                          task=task, prompt=prompt, **kwargs)
    return types.SimpleNamespace(run=_run)


def _qualify_summary(session, saved) -> dict:
    """qualify 的安全 JSON summary：逐 runtime 诚实结果（结构来自既有
    RuntimeBootstrapEntry，构造时已 secret-free 校验），别无敏感面。"""
    identities = session.pool.identities()
    return {
        "command": "qualify",
        "status": "QUALIFIED" if identities else "NOT_QUALIFIED",
        "admitted": [list(identity) for identity in identities],
        "entries": [
            {
                "runtime_id": entry.runtime_id,
                "discovery_available": entry.discovery_available,
                "health_status": entry.health_status,
                "validation_status": entry.validation_status,
                "provenance": entry.provenance,
                "capabilities": list(entry.capabilities),
                "admitted": entry.admitted,
                "reason": entry.reason,
            }
            for entry in session.entries
        ],
        "qualification_count": session.qualification_count,
        "saved_files": [path.name for path in saved],
    }


def _coerce_qualify_timeout(text):
    """正 float 校验（拒绝 0/负/NaN/inf/非数字）；返回 (value, problem)。

    problem 是现成的 stderr JSON 错误载荷（invalid --timeout-seconds），
    value 无问题时为 float、有问题时为 None。"""
    try:
        value = float(text)
    except (TypeError, ValueError):
        value = None
    if value is None or not (value > 0.0) or value == float("inf"):
        return None, {"error": "invalid --timeout-seconds",
                      "detail": f"not a positive number: {text}"}
    return value, None


def _parse_qualify_arguments(argv_rest):
    """qualify 参数面解析（CU-P0；组合根层，cli.py 冻结零修改）。

    只认恰一次 --timeout-seconds（`N` 空格赋值或 `=N`）；其余任何参数
    返回既有 unsupported-arguments 错误载荷。返回 (timeout|None,
    error|None) —— error 为现成 stderr JSON 载荷，调用方 exit 2；
    全空 argv → (None, None) = 默认 300.0 既有行为。"""
    timeout = None
    index = 0
    while index < len(argv_rest):
        arg = argv_rest[index]
        if arg == _QUALIFY_TIMEOUT_FLAG:
            if timeout is not None:
                return None, {"error": "duplicate --timeout-seconds"}
            if index + 1 >= len(argv_rest):
                return None, {"error": "missing --timeout-seconds value"}
            timeout, problem = _coerce_qualify_timeout(argv_rest[index + 1])
            if problem:
                return None, problem
            index += 2
        elif arg.startswith(_QUALIFY_TIMEOUT_FLAG + "="):
            if timeout is not None:
                return None, {"error": "duplicate --timeout-seconds"}
            timeout, problem = _coerce_qualify_timeout(
                arg[len(_QUALIFY_TIMEOUT_FLAG) + 1:])
            if problem:
                return None, problem
            index += 1
        else:
            return None, {"error": "unsupported qualify arguments",
                          "detail": " ".join(argv_rest)}
    return timeout, None


def _main_qualify(argv_rest, *, factories, qualifier, base_dir,
                  timeout_seconds) -> int:
    timeout_override, problem = _parse_qualify_arguments(argv_rest)
    if problem is not None:
        print(json.dumps(problem), file=sys.stderr)
        return 2
    if timeout_override is not None:
        timeout_seconds = timeout_override

    def _progress(line: str) -> None:
        # CU-P0：进度只走 stderr（stdout 被 P1-U3 单 JSON 契约独占）；
        # flush 保证长等待期间逐行可见（含管道重定向场景）。
        print(line, file=sys.stderr, flush=True)

    directory = DEFAULT_EVIDENCE_DIR if base_dir is None else base_dir
    try:
        session, rejected, saved = qualify_runtimes(
            factories=factories, qualifier=qualifier, base_dir=directory,
            timeout_seconds=timeout_seconds, progress=_progress)
    except (RuntimeError, ValueError) as error:
        message = str(error)
        print(json.dumps({"status": "NOT_QUALIFIED",
                          "reason": _composition_reason(message),
                          "detail": message}, sort_keys=True,
                         separators=(",", ":")))
        print(f"dual-agent: qualification could not proceed "
              f"({_composition_reason(message)})", file=sys.stderr)
        return 2
    except OSError as error:  # 系统性 IO：非语义结果，stderr-only
        print(json.dumps({"error": "qualification failed",
                          "detail": str(error)}), file=sys.stderr)
        return 2
    _print_rejections(rejected)
    summary = _qualify_summary(session, saved)
    print(json.dumps(summary, sort_keys=True, separators=(",", ":")))
    if session.pool.identities():
        return 0
    print("dual-agent: no runtime admitted; REAL qualification requires "
          "RUN_REAL_PROVIDER_TESTS=1 (see JSON summary on stdout)",
          file=sys.stderr)
    return 2


def _main_run(argv, *, factories, evidence, qualifier, base_dir,
              current_health, timeout_seconds) -> int:
    # run 只读盘：显式注入的 evidence 优先；qualifier 在此路径不可达
    # （绝不隐式 qualification —— 即便调用方显式传入了 qualifier）。
    if evidence is None:
        directory = DEFAULT_EVIDENCE_DIR if base_dir is None else base_dir
        try:
            evidence, rejected = load_evidence(directory)
        except OSError as error:
            print(json.dumps({"error": "evidence store unreadable",
                              "detail": str(error)}), file=sys.stderr)
            return 2
        _print_rejections(rejected)
    try:
        facade = default_facade(
            factories=factories, evidence=evidence, qualifier=None,
            current_health=current_health,
            timeout_seconds=timeout_seconds)
    except (RuntimeError, ValueError) as error:
        return _semantic_failure(_composition_reason(str(error)),
                                 str(error))
    except OSError as error:  # 系统性 IO：非语义结果，stderr-only
        print(json.dumps({"error": "host composition failed",
                          "detail": str(error)}), file=sys.stderr)
        return 2
    # 既有 run_cli 直接组合（parse → policy → run → 渲染；cli.py 零修改，
    # cli.main 的注入式嵌入面原样保留给直接嵌入方）：渲染字符串即规范形，
    # exit 语义在本层稳定映射。CU-R3a：facade 经组合边界代理 —— CLI 的
    # task_id 映射为确定性不透明摘要后再进 engine（task/prompt 原文
    # 透传；stdout 的 task_id 随之变为不透明，CU-R1 投影按该 id 查
    # ledger，账本键一致）。run 前换代（CU-R2）：run 期间记录的
    # 拒绝诊断带本代数戳，事后只采信本代观测。
    generation = next_diagnostic_generation()
    # CU-R4（forensics capture）：run 入口清取证槽 —— 跨 run 绝不泄漏旧
    # 输出；adapter 只在成功 invoke 时进槽（纯内存，零用户可见输出）。
    packet_forensics.reset()
    # CU-R3b：run 内 pre-collaboration 域拒绝（REAL 探针 + 离线复现已
    # 证：append_decision 对 task_id 的封闭词表安全拒收，消息不含值）收敛
    # 到既有语义失败表面；其余异常保持 traceback 可见性，绝不吞掉。
    try:
        summary = run_cli(_cli_task_boundary(facade), argv)
    except CollaborationStateError as error:
        reason = _composition_reason(str(error))
        return _semantic_failure(
            reason, str(error),
            human=f"dual-agent: run rejected by ledger safety rules "
                  f"({reason})")
    print(summary)
    payload = json.loads(summary)
    # CU-R1（Run Result Delivery）：安全结果投影走 stderr —— stdout 保持
    # 恰一行机器 JSON 的既有契约。task_id 即 CLI 的任务标识（渲染层
    # 权威投影）；投影只读 facade.state 账本里已验证的 envelope。
    for line in _result_projection_lines(facade.state, payload["task_id"]):
        print(line, file=sys.stderr, flush=True)
    # CU-R2（Packet Rejection Diagnostics）：packet 拒绝的值安全诊断行
    # （无新鲜诊断 ⇒ G2/G3 消去法形态）。
    reject_line = _packet_reject_line(payload["status"], generation)
    if reject_line is not None:
        print(reject_line, file=sys.stderr, flush=True)
    # CU-R4（forensics capture）：*_PACKET_INVALID 终态专属 —— 记住的
    # parser input 原文落盘取证（FULL 模式与 parser 输入逐字节相等；
    # 含凭据形状走既有脱敏，绝不明文、绝不声称相等）。成功与其余
    # 终态零落盘零输出；adapter 未接线（非 claude 家族）时槽为空，
    # 安静无副作用。取证 IO 失败在库层已被吞掉 —— 绝不改变失败语义。
    if payload["status"].endswith("_PACKET_INVALID"):
        paths = packet_forensics.persist_pending(
            stage_hint=payload["status"].split("_", 1)[0].lower())
        if paths:
            print("dual-agent: packet forensics: "
                  + "; ".join(str(path) for path in paths),
                  file=sys.stderr, flush=True)
    else:
        packet_forensics.reset()
    return exit_code_for(payload["status"])


def main(argv=None, *, factories=None, evidence=None, qualifier=None,
         current_health=None, base_dir=None,
         timeout_seconds=DEFAULT_TIMEOUT_SECONDS) -> int:
    """console-script / python -m 入口：默认 facade → 注入 → 既有 CLI。

    `qualify` 子命令在 argv[0] 分流到显式资格面（cli.py 零修改）；
    其余 argv 原样交给既有 CLI。组合期诚实拒绝（既有 RuntimeError/
    ValueError 词表，封闭且无 secret）以 stderr JSON 携带机器原因、
    exit 2 退出；CLI 运行结束后清空注入（入口一次性）。
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("--help", "-h"):
        # 顶层产品帮助住本组合根（P1-U4 §6）：argparse 的顶层 help 只认识
        # run 子命令（cli.py 冻结零修改）。纯打印：exit 0，无组合。
        print(_PRODUCT_HELP)
        return 0
    if argv and argv[0] == "qualify":
        if any(flag in argv[1:] for flag in ("--help", "-h")):
            print(_PRODUCT_HELP)
            return 0
        return _main_qualify(argv[1:], factories=factories,
                             qualifier=qualifier, base_dir=base_dir,
                             timeout_seconds=timeout_seconds)
    if any(flag in argv for flag in _ARGPARSE_FLAGS):
        # parse-first 先例：--version/--help/-h 由 argparse 在组合之前
        # 处理（SystemExit(0)），无需 facade、无需 evidence。
        return cli_main(argv)
    return _main_run(argv, factories=factories, evidence=evidence,
                     qualifier=qualifier, base_dir=base_dir,
                     current_health=current_health,
                     timeout_seconds=timeout_seconds)
