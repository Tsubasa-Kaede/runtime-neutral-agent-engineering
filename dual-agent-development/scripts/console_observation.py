"""R7-D4: 第一个观察消费者 —— 无状态 console projection（Option A）。

ConsoleObservationSink 是 ObservationSink 协议的第一个生产消费者：纯消费
端的旁路观察投影，把恰好一个 ExecutionEvent 渲染为恰好一行人类可读文本，
写入注入的 writer（CLI 绑定 sys.stderr；stdout 保持机器可读 JSON）。

边界（与 D1/D2 纪律同款，测试逐项锁定）：
- 无状态、无缓冲、无 finalize：on_event -> format_event_line ->
  writer.write —— 不聚合、不排序、不重编号（sequence 原样输出）。
- 只读 ExecutionEvent 的既有字段：事件在 D1 构造期已通过 secret 校验，
  本模块逐字透传 reason/status，绝不解析、不截断、不建立第二套
  secret policy；duration_ms=None 时省略字段（诚实的未知，绝不伪造 0）。
- 零依赖：除 __future__ 外零 import —— 不读执行引擎/ledger/packet/
  adapter/环境；无 runtime 名、无时间、无随机、无 UUID。同事件流必然
  产生逐字节相同的输出。
- writer 故障即观察故障：本模块不吞异常、不重试 —— 隔离责任唯一属于
  D2 的 emit 隔离层（观察失败绝不影响执行，绝不 retry/fallback）。
"""
from __future__ import annotations


def format_event_line(event) -> str:
    """单行确定性投影：同事件 -> 逐字节相同的一行（含换行符）。

    布局（固定键序）：[sequence] TYPE stage= runtime= status=
    [duration_ms=] reason= —— 只选择既有字段子集，不添加新语义；
    sequence 直接使用事件原值（0 基 execution-scoped，绝不重编号）。
    """
    line = (f"[{event.sequence}] {event.event_type.value} "
            f"stage={event.stage} runtime={event.runtime_id} "
            f"status={event.status}")
    if event.duration_ms is not None:
        line += f" duration_ms={event.duration_ms}"
    line += f" reason={event.reason}"
    return line + "\n"


class ConsoleObservationSink:
    """ObservationSink 的无状态 console 实现（组合由调用方完成）。"""

    def __init__(self, writer):
        self._writer = writer

    def on_event(self, event) -> None:
        self._writer.write(format_event_line(event))
