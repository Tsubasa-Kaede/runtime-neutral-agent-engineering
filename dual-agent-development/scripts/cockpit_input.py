"""UX2-R1 意图翻译最小骨架（CU-UX-2 DESIGN LOCK v1.1 §1/§3）。

R1 只承载两个真实分支：漏斗前置态提交 = TASK、Start 后提交 =
STEER。REVISION / COMMAND 是词汇位（意图集锁定为四），语义
（/revise 召回、slash 命令面）属 UX2-R2，本模块绝不提前实现。

铁律（AC18 Intent Boundary）：
- 零引擎 import、零 Textual import——纯字符串分类函数；
- ComposerIntent ≠ Execution Semantics：分类只决定呈现层走哪条
  既有通道，提交后的真相唯一由 ControlBoundary 裁决
  （accepted / rejected / no-op），UI 不推断执行结果。
"""

INTENT_TASK = "TASK"
INTENT_STEER = "STEER"
INTENT_REVISION = "REVISION"   # R2 语义位：召回/改写 pending invocation
INTENT_COMMAND = "COMMAND"     # R2 语义位：slash 命令面

__all__ = ("INTENT_TASK", "INTENT_STEER", "INTENT_REVISION",
           "INTENT_COMMAND", "classify_submit")


def classify_submit(*, funnel_pre_start: bool, text: str) -> str:
    """提交意图分类（R1 真子集）。

    漏斗前置态（Start 前）的提交是任务文本（TASK——经既有漏斗
    判定路径）；Start 后的文本提交统一是转向（STEER——经既有
    REVISE/NEXT_INVOCATION 冻结通道 FIFO 排队）。空白文本的
    no-op 由调用方处理，本函数不做空白特判。
    """
    if funnel_pre_start:
        return INTENT_TASK
    return INTENT_STEER
