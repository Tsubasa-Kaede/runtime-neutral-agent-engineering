"""P1-U2b: Qualification Evidence Persistence — durable storage projection.

本模块是 CandidateValidationResult 的磁盘投影层，且仅此而已：
- save 是忠实 JSON 序列化（sort_keys + 紧凑分隔符，structured_packets
  的确定性先例同型）；load 通过同一个 CandidateValidationResult 构造器
  重建 —— post_init 的全部校验（枚举合法性、provenance 两值铁律、
  secret-free）在加载时原样复跑。本模块绝不添加第二套 qualification /
  admission / selection truth：admission 仍然只发生在既有 discovery
  bootstrap 会话与已验证池的 admit 语义里，本模块不 import 它们中的
  任何一件。
- 只持久化 status=VERIFIED 且 provenance="REAL" 的资格事实（失败/OFFLINE
  属于当场诚实呈现，不属于 store —— 否则会命中复用分支的 no-retry 语义
  把失败焊死）。持久层保存事实，不制造事实：provenance 两值词表逐字搬运。
- 原子写：同目录 temp → flush+fsync → os.replace；无锁、无 backup、无
  数据库。temp 文件以 .tmp 结尾（绝不匹配 *.json 装载面）。
- 身份绑定：文件名 = identity 四元组的人读 slug + 短稳定散列；加载时
  从 payload identity 重算规范名并与实际文件名强比对 —— 不一致即拒绝
  （IDENTITY_MISMATCH）。绝不 runtime-id-only 匹配，绝不"以文件内为准"。
- 单文件隔离失败：一个坏文件（MALFORMED_JSON / UNSUPPORTED_SCHEMA /
  INVALID_PAYLOAD / IDENTITY_MISMATCH）只拒绝自身，绝不连累整库。
  目录不存在 = 诚实空库（新机首跑的合法状态），读操作绝不创建目录。
- 库 API 强制显式 base_dir；默认路径由 host 组合层（host_entry）提供。
  测试一律 TemporaryDirectory，绝不写真实用户 home。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Mapping, NamedTuple

from candidate_validation import (
    CandidateValidationResult,
    CandidateValidationStatus,
    GateResult,
    GateVerdict,
    ValidationGate,
)

__all__ = (
    "SCHEMA_VERSION",
    "NOT_PERSISTABLE",
    "MALFORMED_JSON",
    "UNSUPPORTED_SCHEMA",
    "INVALID_PAYLOAD",
    "IDENTITY_MISMATCH",
    "EvidenceRejection",
    "save_evidence",
    "load_evidence",
)

SCHEMA_VERSION = 1

# 持久层本地封闭词表（非 qualification 词表）。
NOT_PERSISTABLE = "NOT_PERSISTABLE"
MALFORMED_JSON = "MALFORMED_JSON"
UNSUPPORTED_SCHEMA = "UNSUPPORTED_SCHEMA"
INVALID_PAYLOAD = "INVALID_PAYLOAD"
IDENTITY_MISMATCH = "IDENTITY_MISMATCH"

_DETAIL_LIMIT = 200

_SLUG_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


class EvidenceRejection(NamedTuple):
    """单文件隔离拒绝的诊断（文件名 + 封闭原因词 + 安全细节）。"""

    filename: str
    reason: str
    detail: str


def _slug_component(value: Any) -> str:
    if value is None:
        return "none"
    cleaned = _SLUG_UNSAFE.sub("_", str(value)).strip("._")
    return cleaned[:48] or "_"


def _identity_filename(identity: tuple) -> str:
    """identity 四元组的规范文件名：人读 slug + 短稳定散列。

    散列输入是 identity 的紧凑确定性 JSON —— 区分仅 slug 相同的不同
    identity（如 model_id=None 与字面量 "none"），保证每 identity 恰好
    一个文件。加载侧以同一函数重算并强比对文件名。"""
    digest = hashlib.sha256(json.dumps(
        list(identity), sort_keys=True, separators=(",", ":"),
        ensure_ascii=True).encode("utf-8")).hexdigest()[:8]
    slug = "__".join(_slug_component(part) for part in identity)
    return f"{slug}--{digest}.json"


def _json_safe(value: Any) -> Any:
    """递归把 tuple/frozenset/set 投影为 list（确定性排序），其余原样。

    已知投影边界（设计裁决）：GateResult.evidence 内层 tuple 落盘后成
    list —— audit-only 容器形态漂移，语义字段零漂移；不做猜测式还原。"""
    if isinstance(value, Mapping):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (frozenset, set)):
        return sorted(
            (_json_safe(item) for item in value),
            key=lambda item: json.dumps(item, sort_keys=True,
                                       separators=(",", ":")))
    if isinstance(value, (tuple, list)):
        return [_json_safe(item) for item in value]
    return value


def _payload(result: CandidateValidationResult) -> dict:
    """13 个 qualification 字段的忠实投影 + schema_version，别无其他。"""
    failure = result.failure_point
    return {
        "schema_version": SCHEMA_VERSION,
        "identity": list(result.identity),
        "status": result.status.value,
        "gates_passed": sorted(gate.name for gate in result.gates_passed),
        "gate_results": [
            {
                "gate": gate_result.gate.name,
                "verdict": gate_result.verdict.value,
                "reason": gate_result.reason,
                "evidence": _json_safe(dict(gate_result.evidence)),
                "capabilities": list(gate_result.capabilities),
            }
            for gate_result in result.gate_results
        ],
        "block_reason": result.block_reason,
        "failure_point": (
            [failure[0].name, failure[1]] if failure is not None else None),
        "experiment_id": result.experiment_id,
        "executed_at": result.executed_at,
        "validated_capabilities": list(result.validated_capabilities),
        "evidence": _json_safe(dict(result.evidence)),
        "provenance": result.provenance,
    }


def _reconstruct(payload: Mapping[str, Any]) -> CandidateValidationResult:
    """载荷 → CandidateValidationResult：枚举按 name/值反解，重建走同一
    构造器（post_init 校验全复跑）。任何失败由调用方收敛为
    INVALID_PAYLOAD —— 本层绝不 best-effort 解析。"""
    identity = payload["identity"]
    if not isinstance(identity, list) or len(identity) != 4:
        raise ValueError("identity must be the canonical 4-tuple projection")
    failure = payload.get("failure_point")
    return CandidateValidationResult(
        identity=tuple(identity),
        status=CandidateValidationStatus(payload["status"]),
        gates_passed=frozenset(
            ValidationGate[name] for name in payload.get("gates_passed") or ()),
        gate_results=tuple(
            GateResult(
                gate=ValidationGate[gate_payload["gate"]],
                verdict=GateVerdict(gate_payload["verdict"]),
                reason=gate_payload.get("reason"),
                evidence=dict(gate_payload.get("evidence") or {}),
                capabilities=tuple(gate_payload.get("capabilities") or ()),
            )
            for gate_payload in payload.get("gate_results") or ()),
        block_reason=payload.get("block_reason"),
        failure_point=(
            (ValidationGate[failure[0]], failure[1])
            if failure is not None else None),
        experiment_id=payload.get("experiment_id"),
        executed_at=payload.get("executed_at"),
        validated_capabilities=tuple(
            payload.get("validated_capabilities") or ()),
        evidence=dict(payload.get("evidence") or {}),
        provenance=payload.get("provenance"),
    )


def _detail(error: BaseException) -> str:
    return str(error)[:_DETAIL_LIMIT]


def save_evidence(base_dir: Any, result: CandidateValidationResult) -> Path:
    """把一条 VERIFIED+REAL 资格事实原子写入 base_dir。

    只接受 status=VERIFIED 且 provenance="REAL"（其余 NOT_PERSISTABLE，
    ValueError）—— 持久层不升级 provenance，失败事实不属于 store。
    返回写入的规范文件路径；IO 失败如实抛 OSError，temp 一律清理。"""
    if (result.status is not CandidateValidationStatus.VERIFIED
            or result.provenance != "REAL"):
        raise ValueError(
            f"{NOT_PERSISTABLE} status={result.status.value} "
            f"provenance={result.provenance}")
    directory = Path(base_dir)
    directory.mkdir(parents=True, exist_ok=True)
    # 先整体序列化：任何不可编码值在触碰文件系统之前就失败。
    text = json.dumps(_payload(result), sort_keys=True,
                      separators=(",", ":"), ensure_ascii=True)
    path = directory / _identity_filename(result.identity)
    handle_fd, temp_name = tempfile.mkstemp(
        dir=directory, prefix="evidence-", suffix=".tmp")
    try:
        with os.fdopen(handle_fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise
    return path


def load_evidence(base_dir: Any):
    """装载整个 evidence store：→ (evidence dict, rejected tuple)。

    evidence 以完整 identity 四元组为键（逐字强匹配，无 runtime-id-only
    折叠）。单文件坏只拒绝自身（EvidenceRejection），绝不连累整库；
    目录不存在 = 诚实空库 ({}, ())，读操作绝不创建目录。
    链路：file → JSON parse → schema_version → identity/filename 一致性
    → 构造器重建（post_init）→ VERIFIED+REAL → 返回。"""
    evidence: dict = {}
    rejected: list = []
    directory = Path(base_dir)
    if not directory.is_dir():
        return evidence, ()
    for path in sorted(directory.glob("*.json")):
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            raise  # 系统性 IO 问题：如实上抛（非数据腐烂）
        try:
            payload = json.loads(raw)
        except ValueError as error:
            rejected.append(EvidenceRejection(
                path.name, MALFORMED_JSON, _detail(error)))
            continue
        if not isinstance(payload, dict):
            rejected.append(EvidenceRejection(
                path.name, MALFORMED_JSON, "payload is not a JSON object"))
            continue
        version = payload.get("schema_version")
        if version != SCHEMA_VERSION:
            rejected.append(EvidenceRejection(
                path.name, UNSUPPORTED_SCHEMA,
                f"schema_version={version}" if isinstance(version, int)
                else "schema_version missing or non-integer"))
            continue
        identity = payload.get("identity")
        if not isinstance(identity, list) or len(identity) != 4:
            rejected.append(EvidenceRejection(
                path.name, INVALID_PAYLOAD,
                "identity must be the canonical 4-tuple projection"))
            continue
        if _identity_filename(tuple(identity)) != path.name:
            rejected.append(EvidenceRejection(
                path.name, IDENTITY_MISMATCH,
                f"filename does not match payload identity"))
            continue
        try:
            result = _reconstruct(payload)
        except Exception as error:  # 枚举/构造器封闭词表拒绝
            rejected.append(EvidenceRejection(
                path.name, INVALID_PAYLOAD, _detail(error)))
            continue
        if (result.status is not CandidateValidationStatus.VERIFIED
                or result.provenance != "REAL"):
            rejected.append(EvidenceRejection(
                path.name, INVALID_PAYLOAD,
                "store holds only VERIFIED+REAL results"))
            continue
        evidence[result.identity] = result
    return evidence, tuple(rejected)
