"""UX2 意图翻译层（CU-UX-2 DESIGN LOCK v1.1 §1/§3；R2 完整四类）。

四类意图（锁定集；QUERY=DEFER——开放问句分类不实现，词表零预留）：
- TASK：漏斗前置态提交（经既有漏斗判定路径——判定逻辑冻结，
  斜杠在漏斗域亦是任务文本，不属命令）；
- STEER：Start 后普通文本提交（经既有 REVISE/NEXT_INVOCATION 冻结
  通道 FIFO 排队）；
- REVISION：E 召回改写的提交（同一 REVISE 通道——召回的是 UI 自有
  echo 缓存，改写提交铸造新 command_id，绝不触碰 pending 真相）；
- COMMAND：slash 命令（注册表封闭集；未知命令诚实 no-op）。

铁律（AC18 Intent Boundary）：
- 零引擎 import、零 Textual import——纯字符串/纯数据；
- ComposerIntent ≠ Execution Semantics：分类只决定呈现层走哪条
  既有通道，提交后的真相唯一由 ControlBoundary 裁决
  （accepted / rejected / no-op），UI 不推断执行结果。
"""

INTENT_TASK = "TASK"
INTENT_STEER = "STEER"
INTENT_REVISION = "REVISION"
INTENT_COMMAND = "COMMAND"

# 2.8-A 会话相位（分类参数三值——呈现层路由用，非引擎状态、非
# 第二真源；推广自 funnel_pre_start 布尔，旧签名逐字兼容）：
# - PRE_START：首跑漏斗前置态（= funnel_pre_start=True；判定逻辑
#   冻结——斜杠亦是任务文本域）；
# - RUNNING：run 执行中（既有 Start 后语义）；
# - BETWEEN_RUNS：会话轮间态（终态后再入漏斗："/"=COMMAND、
#   其余文本=下一轮 TASK——分类只决定呈现层走哪条既有通道，
#   引擎语义零变化）。
PHASE_PRE_START = "PRE_START"
PHASE_RUNNING = "RUNNING"
PHASE_BETWEEN_RUNS = "BETWEEN_RUNS"

# Slash 命令注册表（R2 封闭集；2.8-A 会话四命令扩充恰十三条——
# DESIGN LOCK v1.1 → 2.8 修订面，九命令语义零变）。
# kind 语义（呈现层路由，非引擎词汇）：
# - "dispatch"：经既有 _dispatch 外发（dispatch_kind = 既有注入回调
#   协议词），boundary 唯一裁决、回执行 Log；
# - "confirm"：走既有 y/n 确认条路径（abort/new 恒在确认后——
#   确认动作随命令名携带，TUI 侧分支兑现）；
# - "screen"：走既有只读观察推屏；
# - "local"：纯呈现层切换（零外发、零事实触碰）。
# help 为 /help 输出原文（EN 冻结——漏斗 R2 ERRATA 同律）。
SLASH_REGISTRY = {
    "pause": {"kind": "dispatch", "dispatch_kind": "PAUSE",
              "help": "pause the running collaboration"},
    "resume": {"kind": "dispatch", "dispatch_kind": "RESUME",
               "help": "resume a paused collaboration"},
    "abort": {"kind": "confirm", "dispatch_kind": None,
              "help": "abort behind the y/n confirm bar"},
    "trace": {"kind": "screen", "dispatch_kind": None,
              "help": "open the read-only observation trace"},
    "help": {"kind": "local", "dispatch_kind": None,
             "help": "list slash commands"},
    "lang": {"kind": "local", "dispatch_kind": None,
             "help": "toggle EN/ZH display"},
    "context": {"kind": "local", "dispatch_kind": None,
                "help": "toggle the context panel (wide)"},
    "clear": {"kind": "local", "dispatch_kind": None,
              "help": "clear the collaboration log display"},
    "target": {"kind": "local", "dispatch_kind": None,
               "help": "toggle revision target: queue vs prompt "
                       "(<agent> not implemented)"},
    # 2.8-A 会话四命令（轮间域为主场；语义全为呈现层——/again 载
    # 入上轮任务文本须再过 Enter 两段律，绝不免检复活）
    "again": {"kind": "local", "dispatch_kind": None,
              "help": "reload the last task to run it again"},
    "compose": {"kind": "local", "dispatch_kind": None,
                "help": "open the runtime selection screen"},
    "new": {"kind": "confirm", "dispatch_kind": None,
            "help": "reset the session display behind the y/n bar"},
    "runs": {"kind": "local", "dispatch_kind": None,
             "help": "list the collaborations of this session"},
}

__all__ = ("INTENT_TASK", "INTENT_STEER", "INTENT_REVISION",
           "INTENT_COMMAND", "PHASE_PRE_START", "PHASE_RUNNING",
           "PHASE_BETWEEN_RUNS", "SLASH_REGISTRY", "classify_submit",
           "parse_slash", "slash_candidates", "slash_help_lines")


def classify_submit(*, funnel_pre_start: bool, text: str,
                    revision_recall: bool = False,
                    phase: str = "") -> str:
    """提交意图分类（纯函数）。

    漏斗前置态恒 TASK（漏斗判定冻结，斜杠亦是任务文本域）；Start
    后：斜杠=COMMAND；E 召回态=REVISION；其余=STEER。空白 no-op 由
    调用方处理，本函数不做空白特判。
    2.8-A 相位推广：phase 显式给出时覆盖 funnel_pre_start 布尔
    （未给出时由布尔派生——旧调用路径逐字节同径）。BETWEEN_RUNS：
    斜杠=COMMAND（轮间域命令面）；召回/普通文本=下一轮 TASK（经
    漏斗再入装配全新 run——绝不复活旧 run，零上下文携带）。
    """
    if not phase:
        phase = PHASE_PRE_START if funnel_pre_start else PHASE_RUNNING
    if phase == PHASE_PRE_START:
        return INTENT_TASK
    if str(text).startswith("/"):
        return INTENT_COMMAND
    if phase == PHASE_BETWEEN_RUNS:
        return INTENT_TASK
    if revision_recall:
        return INTENT_REVISION
    return INTENT_STEER


def parse_slash(text: str):
    """"/name arg..." → (name, arg)；非斜杠 → (None, "")。

    首 token 为命令名（大小写敏感——注册表全小写，变体诚实落
    unknown）；参数为其余原文（已去首尾空白）。
    """
    raw = str(text)
    if not raw.startswith("/"):
        return None, ""
    name, _, arg = raw[1:].partition(" ")
    return name, arg.strip()


def slash_candidates(prefix: str):
    """注册表前缀匹配（注册序稳定；空前缀=全量十三条——
    autocomplete 最小实现：hint 行呈现候选，无补全键）。"""
    return tuple(name for name in SLASH_REGISTRY
                 if name.startswith(prefix))


def slash_help_lines():
    """/help 输出面（EN 冻结——命令名/键字母不译，漏斗同律）。"""
    return tuple(f"/{name} — {spec['help']}"
                 for name, spec in SLASH_REGISTRY.items())
