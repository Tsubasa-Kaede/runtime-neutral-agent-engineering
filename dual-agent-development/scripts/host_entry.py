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

import importlib
import json
import sys
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

_HINT_QUALIFY = "no persisted qualification evidence: run `dual-agent qualify` first"

# 顶层产品帮助（P1-U4 §6）：cli.py 的 argparse 只认 run 子命令（V2 冻结
# 零修改），qualify 的可发现性由本组合根提供。纯静态文本：无证据、无
# 发现、无组合、无网络即可打印；run 绝不自动 qualify 在此明示。
_PRODUCT_HELP = """dual-agent: runtime-neutral agent collaboration (local product entry)

Usage:
  dual-agent --version
  dual-agent --help
  dual-agent qualify
  dual-agent run [--mode off|auto|on] [--observe] "<task>"

Commands:
  qualify   Explicitly qualify discovered runtimes through the G1-G14 gate
            chain and persist VERIFIED+REAL evidence under
            ~/.dual-agent/qualification/. REAL invocation requires
            RUN_REAL_PROVIDER_TESTS=1; offline results are reported
            honestly and are never persisted.
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
    from .generic_runtime_health import GenericRuntimeHealth
    from .host import build_facade_from_bootstrap
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
    from generic_runtime_health import GenericRuntimeHealth
    from host import build_facade_from_bootstrap
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


def default_facade(*, factories=None, evidence=None, qualifier=None,
                   current_health=None,
                   timeout_seconds=DEFAULT_TIMEOUT_SECONDS):
    """默认组合：registry → 时点健康 → 既有 Automatic entry。

    全部语义在既有件内：admission/选择/执行由 build_facade_from_bootstrap
    内部的 bootstrap_runtime_session 与 build_facade 承担；本函数只做
    顺序接线（registry 先行，health 未注入时观测，然后交给既有入口）。
    """
    registry, _skipped = environment_registry(factories)
    if current_health is None:
        current_health = observe_current_health(registry)
    return build_facade_from_bootstrap(
        registry, evidence=evidence, qualifier=qualifier,
        current_health=current_health,
        timeout_seconds=timeout_seconds,
    )


def qualify_runtimes(*, factories=None, base_dir=DEFAULT_EVIDENCE_DIR,
                     qualifier=None,
                     timeout_seconds=DEFAULT_TIMEOUT_SECONDS):
    """显式 qualification surface（P1-U2b）：读发现 → 执行 qualifier →
    成功（VERIFIED+REAL）即持久化。

    全部编排语义在既有 bootstrap_runtime_session 内（evidence 复用、
    no-retry、admission 一概不重建）。默认 qualifier 是 run_real_validation
    的最小桥：adapter 真相 = bootstrap 构造 instance 时挂上的 probe
    （rc3 / gemini REAL 驱动同款接线），REAL 开门语义留在 GATE_ENV_NAME
    内 —— OFFLINE 结果照常返回、照常被 admission 拒绝，只是不落盘。
    持久化只发生在成功结果上：persistence 保存事实，不制造事实。
    返回 (session, rejected, saved_paths)。"""
    registry, _skipped = environment_registry(factories)
    if not registry.list():
        raise RuntimeError("NO RUNTIMES REGISTERED")
    evidence, rejected = load_evidence(base_dir)

    if qualifier is None:
        def _real_bridge(instance):
            result, _executor = run_real_validation(
                instance, instance.probe, timeout_seconds=timeout_seconds)
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


def _semantic_failure(reason: str, message: str) -> int:
    """P1-U3 语义失败契约：stdout 机器 JSON + stderr 人类行 + exit 2。"""
    payload = {"status": "NOT_QUALIFIED", "reason": reason, "detail": message}
    if "NO_EVIDENCE_NO_QUALIFIER" in message:
        payload["hint"] = _HINT_QUALIFY
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    human = ("No qualified runtime evidence is available. "
             "Run `dual-agent qualify` first."
             if "NO_EVIDENCE_NO_QUALIFIER" in message
             else f"dual-agent: no admitted verified runtime ({reason})")
    print(human, file=sys.stderr)
    return 2


def _print_rejections(rejected) -> None:
    for item in rejected:
        print(f"dual-agent: evidence rejected: {item.filename}: "
              f"{item.reason}: {item.detail}", file=sys.stderr)


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


def _main_qualify(argv_rest, *, factories, qualifier, base_dir,
                  timeout_seconds) -> int:
    if argv_rest:
        print(json.dumps({"error": "unsupported qualify arguments",
                          "detail": " ".join(argv_rest)}), file=sys.stderr)
        return 2
    directory = DEFAULT_EVIDENCE_DIR if base_dir is None else base_dir
    try:
        session, rejected, saved = qualify_runtimes(
            factories=factories, qualifier=qualifier, base_dir=directory,
            timeout_seconds=timeout_seconds)
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
    # exit 语义在本层稳定映射。
    summary = run_cli(facade, argv)
    print(summary)
    return exit_code_for(json.loads(summary)["status"])


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
