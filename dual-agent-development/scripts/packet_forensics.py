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

CU-R5（Packet Forensics Self-Test Fields）：落盘记录内新增两个只读自测
字段 —— ``self_test_json_loads``（``OK`` | ``FAIL:<exception-class>:<pos>``）
与 ``self_test_packet_schema``（``PASS`` | ``FAIL:<rule>`` | ``N/A``）。
对**脱敏之前的内存原文**重新执行 JSON decode 与 packet schema 检查，
使 *_PACKET_INVALID 取证可直接区分「JSON 解析失败 / schema 失败 /
后置内容扫描拒绝」（R2C/P1-1b 两取证因 REDACTED+4096 截断丧失全文，
无法离线复跑 json.loads —— 本 CU 关闭该 observability gap）。

自测红线（forensic-only）：
- 在 persist 时（run 终态之后）对槽内原文计算，绝不改变生产
  acceptance/rejection —— ``_packet_from_output`` 行为零变化，self-test
  FAIL 绝不反向影响任何生产路径。
- schema 检查复用既有 primitive（structured_packets 的 from_dict 家族 +
  R6-C11 结构化诊断 rule + session 家族的 _normalize 归一语义），绝不建
  第二套业务 parser；两家族忠实映射：architect/coder 走 session 家族
  （from_dict 前 _normalize，collaboration_session.py 语义），tester/
  reviewer 走 verification 家族（无归一，verification_collaboration.py
  语义）；未映射 role → ``N/A``。
- task_id 覆写与两家族生产语义一致（orchestration 拥有任务身份）。
- 诊断槽在自测前后 reset：陈旧诊断绝不冒充本次失败原因，自测绝不
  留下跨调用污染。
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
    # 生产 parser input 语义（_packet_from_output：非字符串按 "" 处理）。
    parser_text = output if isinstance(output, str) else ""
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
        # CU-R5：自测在脱敏之前的内存原文上执行（stored 已损失原文时，
        # 这两个字段是判别「parse vs schema vs 后置扫描」的唯一证据）。
        "self_test_json_loads": _self_test_json_loads(parser_text),
        "self_test_packet_schema": _self_test_packet_schema(
            parser_text, entry["task_id"], entry["role"]),
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


def _self_test_json_loads(parser_text: str) -> str:
    """CU-R5：对 parser input 原文直接执行 JSON decode，记录异常类与
    位置（json.JSONDecodeError.pos）。只描述「现在重新检查会得到
    什么」，绝不影响任何生产判定。"""
    try:
        json.loads(parser_text)
    except ValueError as exc:
        position = getattr(exc, "pos", None)
        suffix = "" if position is None else f":{position}"
        return f"FAIL:{type(exc).__name__}{suffix}"
    return "OK"


def _self_test_packet_class(role):
    """role → (packet_class, 是否 session 家族归一)。懒导入避免模块级
    权重/环；映射忠实两家族生产路径（见模块 docstring）。未映射 →
    None（自测如实 N/A，绝不猜测 schema）。"""
    from structured_packets import (
        ArchitecturePacket,
        ImplementationPacket,
        ReviewPacket,
        TestPacket,
    )
    return {
        "architect": (ArchitecturePacket, True),
        "coder": (ImplementationPacket, True),
        "tester": (TestPacket, False),
        "reviewer": (ReviewPacket, False),
    }.get(role)


def _self_test_packet_schema(parser_text: str, task_id, role) -> str:
    """CU-R5：对已解析对象按生产同族规则重跑 packet schema 检查。

    与 ``_packet_from_output`` 的 gate 顺序保持同族语义（dict 检查 →
    task_id 覆写 → [session 家族] _normalize → from_dict），但不做 fence
    剥离、不跑后置全包内容扫描 —— 自测要分离的恰是「schema 之前 vs
    之后」。失败 rule 复用 R6-C11 结构化诊断（无诊断可读时退化为
    PACKET_VALIDATION_ERROR），绝不携带被拒值。"""
    mapped = _self_test_packet_class(role)
    if mapped is None:
        return "N/A"
    packet_class, session_family = mapped
    try:
        data = json.loads(parser_text)
    except ValueError:
        return "N/A"
    if not isinstance(data, dict):
        return "FAIL:NON_OBJECT"
    data = dict(data)
    data["task_id"] = task_id  # orchestration owns task identity（两家族一致）
    if session_family:
        from collaboration_session import _normalize  # 冻结件复用，非复制
        data = _normalize(data)
    from content_safety import (
        last_validation_diagnostic,
        reset_validation_diagnostic,
    )
    reset_validation_diagnostic()  # 陈旧诊断绝不冒充本次失败原因
    try:
        packet_class.from_dict(data)
    except (ValueError, TypeError, KeyError):
        diagnostic = last_validation_diagnostic()
        rule = (diagnostic.rule if diagnostic is not None
                else "PACKET_VALIDATION_ERROR")
        reset_validation_diagnostic()  # 自测绝不留下跨调用污染
        return f"FAIL:{rule}"
    reset_validation_diagnostic()
    return "PASS"


def _redact(text):
    # 复用既有脱敏变换（凭据形态 → [REDACTED]，4096 截断），绝不建第二
    # 套 scanner。懒导入避免模块级环；_safe_error 是类静态方法。
    from claude_code_adapter import ClaudeCodeAdapter
    return ClaudeCodeAdapter._safe_error(text)


def tempfile_dir():
    # 测试注入位（默认系统 tempdir）。
    import tempfile
    return tempfile.gettempdir()
