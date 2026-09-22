"""CU-BENCH fixture 数据面（BENCH-Q 交付，纯数据 + manifest 铸造）。

模块律（benchmark 设计 §11 Exact Scope）：
- 零生产 import（stdlib only）——benchmark 样本与历史 REAL 数据
  （Evidence Store / 资格域 / 任何持久面）结构性无通道；
- 纯数据 + 纯函数指纹：同输入恒同输出（可重放的前提）；
- fingerprint 只用于身份/可比性声明，绝不是成本/性能指标；
- 路由证据 fixture 的 token 字段恒 None——调用数维度（生产
  _ROUTE_DEFAULT_POLICY 同语义），零 token 语义渗入。

manifest 语义（设计 §6/§8）：run_id / task_fingerprint /
composition_fingerprint / runtime_fingerprint / cutoff_index /
warm / repeats——六元组可比性边界；warm 必须携带非空前缀证据
（cutoff_index >= 1），cold 恒 cutoff 0。
"""
from dataclasses import dataclass
from hashlib import sha256
from types import SimpleNamespace

__all__ = (
    "SINGLE_TASK", "MULTI_TASK", "PRIOR_SHORT", "PRIOR_OVER_LIMIT",
    "STEPS_SINGLE", "STEPS_MULTI",
    "pool_fixture", "routing_usage_records",
    "task_fingerprint", "composition_fingerprint",
    "runtime_fingerprint", "BenchmarkManifest",
)

SINGLE_TASK = "bench single-agent task: summarize the module contract."
MULTI_TASK = ("bench multi-agent task: design, implement, and "
              "review the module contract.")

PRIOR_SHORT = "prior architect output body for bench fixtures."
PRIOR_OVER_LIMIT = "Y" * 4500

STEPS_SINGLE = (("coder", "rt-a"),)
STEPS_MULTI = (("architect", "rt-a"), ("coder", "rt-b"),
               ("reviewer", "rt-c"))


def pool_fixture():
    """组合池快照 fixture（identity 三元组 = runtime 指纹原料）。"""
    return tuple(
        SimpleNamespace(runtime_id=f"rt-{name}",
                        provider_id=f"prov-{name}",
                        identity=(f"rt-{name}", f"prov-{name}", None,
                                  "bench"))
        for name in "abc")


def routing_usage_records():
    """warm 路由证据 fixture（rt-c×3 / rt-b×1 / rt-a 零记录）。

    调用数维度语义（与生产 _ROUTE_DEFAULT_POLICY 一致）：记录在场
    即调用事实；token 字段恒 None = 本 fixture 零 token 主张。
    CostFactView 鸭读（usage_status 取 .value 回落字符串）。
    """
    return tuple(
        SimpleNamespace(runtime_id=runtime_id, usage_status="KNOWN",
                        input_tokens=None, output_tokens=None,
                        duration_ms=None)
        for runtime_id in ("rt-c", "rt-c", "rt-c", "rt-b"))


def task_fingerprint(text):
    """任务指纹 = 文本内容寻址（确定性；身份声明非指标）。"""
    return "task_" + sha256(text.encode("utf-8")).hexdigest()[:16]


def composition_fingerprint(steps, policy_fingerprint):
    """组合指纹 = 交付序 steps + 编译政策指纹内容寻址。"""
    payload = "|".join(f"{role}:{runtime_id}" for role, runtime_id
                       in steps) + "|" + policy_fingerprint
    return "comp_" + sha256(payload.encode("utf-8")).hexdigest()[:16]


def runtime_fingerprint(pool):
    """runtime 指纹 = 池 identity 前三元的稳定串行（runtime/
    provider/model——资格真源原样携带，绝不在此重算资格）。"""
    return tuple(
        "=".join(str(part) for part in entry.identity[:3])
        for entry in pool)


@dataclass(frozen=True)
class BenchmarkManifest:
    """一次 benchmark 对象的六元组可比性边界（身份声明非指标）。"""

    run_id: str
    task_fingerprint: str
    composition_fingerprint: str
    runtime_fingerprint: tuple
    cutoff_index: int
    warm: bool
    repeats: int

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, str) or not self.run_id.strip():
            raise ValueError("run_id must be a non-empty string")
        for name in ("task_fingerprint", "composition_fingerprint"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if not isinstance(self.runtime_fingerprint, tuple):
            raise ValueError("runtime_fingerprint must be a tuple")
        for bad in (self.cutoff_index, self.repeats):
            if isinstance(bad, bool) or not isinstance(bad, int) \
                    or bad < 0:
                raise ValueError(
                    "cutoff_index/repeats must be non-negative ints")
        if self.repeats < 1:
            raise ValueError("repeats must be >= 1")
        if self.warm and self.cutoff_index < 1:
            raise ValueError(
                "warm manifest requires non-empty evidence prefix "
                "(cutoff_index >= 1)")
        if not self.warm and self.cutoff_index != 0:
            raise ValueError("cold manifest cutoff_index must be 0")
