"""V3.2 CU-OBS-1: per-execution 执行事件观察索引（EventIndex）。

Sections 1-6 冻结架构中观察域的 in-memory 索引：

    Engine → ExecutionEvent → EventIndex → 后续 Trace/TUI projection

per-execution 分组键 = task_id：观察契约自身携带的执行标识（R7-D1
冻结的 ExecutionEvent 字段集中不含 execution_id；生产侧每个执行通道
恰为一个 task_id 产生 execution-scoped sequence）。

边界（本 CU 只立索引，不做投影）：
- 只保存/查询已发生的 ExecutionEvent：不排序、不去重、不补号、
  不推导 lifecycle（TERMINAL 之后仍可继续 observe —— 索引不是
  状态机，投影属于后续 CU）。
- append-only：无 delete/update/reset 面；读取只提供 detached
  immutable view。
- 到达顺序即真相：snapshot 按到达顺序返回；since(cursor) 按事件
  自身 sequence 严格大于 cursor 过滤（sequence 由调用方供应，索引
  不假设连续）。
- 依赖恰 execution_observation + threading：不依赖控制域（命令/
  事实账本）、不依赖用量记录、投影器、runtime adapter 或任何 UI；
  不重定义事件 schema / 事件词表。
"""
from __future__ import annotations

from threading import Lock

from execution_observation import ExecutionEvent, ObservationError

__all__ = ("EventIndex",)


def _require_task_id(value) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ObservationError("task_id must be a non-empty string")


def _require_cursor(value) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ObservationError("cursor must be a non-negative integer")


class EventIndex:
    """per-execution、in-memory、append-only 的 ExecutionEvent 索引。"""

    def __init__(self) -> None:
        self._by_execution: dict[str, list[ExecutionEvent]] = {}
        self._lock = Lock()

    def observe(self, event: ExecutionEvent) -> None:
        """按到达顺序追加一条已发生的执行事件（原对象原样保留）。"""
        if not isinstance(event, ExecutionEvent):
            raise ObservationError("observe expects an ExecutionEvent")
        with self._lock:
            self._by_execution.setdefault(event.task_id, []).append(event)

    def execution_ids(self) -> tuple:
        """已知执行标识（按首次 observe 顺序）。"""
        with self._lock:
            return tuple(self._by_execution)

    def snapshot(self, task_id: str) -> tuple:
        """该 execution 的全量事件（到达顺序；detached immutable view）。"""
        _require_task_id(task_id)
        with self._lock:
            return tuple(self._by_execution.get(task_id, ()))

    def since(self, task_id: str, sequence: int) -> tuple:
        """该 execution 中 sequence 严格大于 cursor 的事件（到达顺序）。"""
        _require_task_id(task_id)
        _require_cursor(sequence)
        with self._lock:
            return tuple(
                event for event in self._by_execution.get(task_id, ())
                if event.sequence > sequence)
