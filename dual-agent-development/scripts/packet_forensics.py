"""CU-R4（Architect Raw Output Forensics Capture）：packet parse 失败时的
parser-input 原文取证槽 —— observation-only，零解析语义变化。

动机（Architect Packet Reliability Forensics Round 1 结论）：两次 REAL
architect 拒绝（CLI / API 注入两条入口）都只能落到
``JSON_PARSE_OR_NON_OBJECT`` 级别的门别，完整 parser input 未持久化 ⇒
malformed construct 定位 UNKNOWN。本模块让下一次 REAL failure 拿到逐字节
的 parser 输入原文，用于三分「runtime 产出非法 JSON / 中间层改写 /
进 parser 前损坏」：

    adapter invoke 成功
        ↓ remember_invocation_output(...)    纯内存槽：零 I/O、零输出
    （session 把 InvocationResult.output 原样送进 _packet_from_output ——
      冻结件 collaboration_session.py 的既有事实，本模块零改动）
    终态 *_PACKET_INVALID（host_entry 侧才可知晓）
        ↓ persist_pending(stage_hint=...)    失败专属落盘
    其余终态 / 入口
        ↓ reset()                            成功与无关路径零落盘

安全（不建第二套 scanner —— 判定与脱敏全部复用既有件）：
- 判定复用 content_safety.contains_unsafe_content（G15 单一来源）。干净
  字符串输出 → capture_mode=FULL，raw_parser_input 与 parser 输入逐字节
  相等（本 CU 的核心验收，测试钉定完整字符串 equality）。
- 含凭据形状 → capture_mode=REDACTED_UNSAFE：经既有
  claude_code_adapter._safe_error 转换（含 4096 截断）落盘。绝不明文写
  secret；绝不声称 redacted artifact 等于 parser input —— 原文 sha256
  摘要与原始长度独立保留供对账。
- 非字符串输出（CLI 封装解析为 dict 的形态）→ capture_mode=SERIALIZED：
  JSON 序列化落盘，raw_type 如实记录，绝不冒充字符串原文。
- 落盘 best-effort：IO 失败逐条跳过并继续（取证绝不反向破坏既有失败
  语义）；persist 取走即清，绝不跨 run 泄漏。

文件命名 ``packet_forensics_{runtime_id}_{invocation_id}.json`` 落在系统
tempdir：文件名只含 runtime/invocation 标识 —— 绝不含 task 原文，绝不含
secret。
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path

from content_safety import contains_unsafe_content

__all__ = (
    "remember_invocation_output",
    "pending",
    "persist_pending",
    "reset",
)

# 进程内记住槽：{invocation_id: entry dict}。同 invocation_id 重复 remember
# 按替换处理（同一调用只有最后一次成功输出是 parser 输入）。
_PENDING: dict[str, dict] = {}

# 文件名安全字符（防御性归一，非 secret 扫描：runtime/invocation 标识
# 本就不该含其余字符，出现时替换为 "_"）。
_UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9._-]")


def remember_invocation_output(runtime_id, invocation_id, task_id, role,
                               output) -> None:
    """记住一次成功 invoke 的输出（仅内存；零 I/O、零用户可见输出）。

    adapter 在构造 SUCCESS 的 InvocationResult 前调用 —— 同一 ``output``
    引用随即成为 InvocationResult.output，session 原样送进 packet
    parser，因此记住值即 parser 输入。"""
    _PENDING[invocation_id] = {
        "runtime_id": runtime_id,
        "invocation_id": invocation_id,
        "task_id": task_id,
        "role": role,
        "output": output,
    }


def pending() -> tuple:
    """当前记住条目的只读快照（浅拷贝，绝不让调用方改内部槽）。

    键名与落盘记录一致（output → raw_parser_input），便于测试直接
    对完整字符串做 equality 验收。"""
    return tuple({"runtime_id": entry["runtime_id"],
                  "invocation_id": entry["invocation_id"],
                  "task_id": entry["task_id"],
                  "role": entry["role"],
                  "raw_parser_input": entry["output"]}
                 for entry in _PENDING.values())


def reset() -> None:
    """清空记住槽（run 入口卫生 + 成功 run 的零落盘纪律）。"""
    _PENDING.clear()


def persist_pending(stage_hint=None) -> tuple:
    """把记住槽落盘为独立 JSON 文件；返回写入路径元组。

    取走即清（一次失败一次取证，绝不跨 run 泄漏）。逐条 best-effort：
    单条 OSError 跳过该条继续，绝不抛出 —— 取证不能改变失败语义。"""
    entries = list(_PENDING.values())
    _PENDING.clear()
    paths = []
    for entry in entries:
        try:
            paths.append(_write_entry(entry, stage_hint))
        except OSError:
            continue
    return tuple(paths)


def _write_entry(entry, stage_hint):
    record = _build_record(entry, stage_hint)
    filename = "packet_forensics_{}_{}.json".format(
        _UNSAFE_FILENAME_CHARS.sub("_", str(entry["runtime_id"])),
        _UNSAFE_FILENAME_CHARS.sub("_", str(entry["invocation_id"])),
    )
    path = Path(tempfile_dir()) / filename
    # ensure_ascii=True：任何代码页下都可读（GBK 控制台/编辑器安全）。
    path.write_text(json.dumps(record, ensure_ascii=True, sort_keys=True),
                    encoding="utf-8")
    return path


def _build_record(entry, stage_hint):
    output = entry["output"]
    if isinstance(output, str):
        text, serialized = output, False
    else:
        text = json.dumps(output, ensure_ascii=True, sort_keys=True,
                          default=repr)
        serialized = True
    # 判定在原始对象上做（dict 形态时递归语义与 packet 扫描同源）。
    if contains_unsafe_content(output):
        mode = "REDACTED_UNSAFE"
        stored = _redact(text)
    elif serialized:
        mode = "SERIALIZED"
        stored = text
    else:
        mode = "FULL"
        stored = text
    record = {
        "capture_mode": mode,
        "runtime_id": entry["runtime_id"],
        "invocation_id": entry["invocation_id"],
        "role": entry["role"],
        "task_id": _safe_task_id(entry["task_id"]),
        "raw_type": type(output).__name__,
        "raw_length": len(text),
        "sha256_raw": hashlib.sha256(
            text.encode("utf-8")).hexdigest(),
        "raw_parser_input": stored,
        "failure_stage_hint": stage_hint,
        "captured_at": time.time(),
    }
    if mode != "FULL":
        # 非 FULL 模式绝不声称等于 parser input（§七 红线）。
        record["verbatim_note"] = (
            "stored artifact is NOT the verbatim parser input"
            + ("; original object JSON-serialized" if serialized else ""))
    return record


def _safe_task_id(task_id):
    if isinstance(task_id, str) and contains_unsafe_content(task_id):
        return "REDACTED"
    return task_id


def _redact(text):
    # 复用既有脱敏变换（凭据形态 → [REDACTED]，4096 截断），绝不建第二
    # 套 scanner。懒导入避免模块级环；_safe_error 是类静态方法。
    from claude_code_adapter import ClaudeCodeAdapter
    return ClaudeCodeAdapter._safe_error(text)


def tempfile_dir():
    # 测试注入位（默认系统 tempdir）。
    import tempfile
    return tempfile.gettempdir()
