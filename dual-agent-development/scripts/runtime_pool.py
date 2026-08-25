"""不含秘密的 RuntimeStatus TTL 缓存与 READY 池（"ReadyPool"）。

这是 READY 路径的快照存储：Health 结果按 runtime 键控，当配置
指纹变化或 TTL（expires_at）过期时通过 get_or_refresh 刷新。
ready_statuses() 精确投影处于 READY 且未过期的条目 —— 即
ReadyPool。

路径边界：ReadyPool 回答"谁现在 HEALTHY"。它与
VerifiedRuntimePool（"谁拥有 qualification 证据"）是平行路径，
不是它的上游或 fallback —— 互不隐含，且 verified 编排路径
绝不借用本池。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable
import time

from runtime_status import ReasonCode, RuntimeState, RuntimeStatus


@dataclass
class _Entry:
    """一条缓存判定，连同做出该判定时的配置指纹。"""

    fingerprint: str
    status: RuntimeStatus


class RuntimeHealthCache:
    """Health 检查之上的 TTL+指纹缓存；只存储判定结果。"""

    def __init__(self, clock=time.time):
        self.clock = clock
        self._entries: dict[str, _Entry] = {}

    def store(self, runtime_id: str, fingerprint: str, status: RuntimeStatus) -> None:
        self._entries[runtime_id] = _Entry(fingerprint, status)

    def get_or_refresh(
        self,
        runtime_id: str,
        fingerprint: str,
        refresh: Callable[[], RuntimeStatus],
    ) -> RuntimeStatus:
        # 缓存判定仅在"配置指纹未变且 TTL 未过期"时可复用：
        # runtime 重新配置后即使 TTL 未到也必须重新检查，
        # 过期的判定绝不能以陈旧状态被继续使用。
        entry = self._entries.get(runtime_id)
        if entry and entry.fingerprint == fingerprint and entry.status.expires_at > self.clock():
            return entry.status
        status = refresh()
        self.store(runtime_id, fingerprint, status)
        return status

    def invalidate(self, runtime_id: str, reason: ReasonCode | None = None) -> None:
        # 丢弃缓存判定；`reason` 仅供调用方记录日志，
        # 缓存本身不存储任何诊断信息。
        self._entries.pop(runtime_id, None)

    def ready_statuses(self) -> tuple[RuntimeStatus, ...]:
        """ReadyPool 投影：仅包含 READY 且未过期的条目。

        成员资格只关于 Health —— 在场不说明任何 qualification
        或 verified-pool 准入。"""
        now = self.clock()
        return tuple(
            entry.status
            for entry in self._entries.values()
            if entry.status.status is RuntimeState.READY and entry.status.expires_at > now
        )
