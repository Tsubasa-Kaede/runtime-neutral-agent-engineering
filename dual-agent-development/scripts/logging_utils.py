"""Small dependency-free logging helper for local orchestration scripts."""
from __future__ import annotations

import sys
from typing import TextIO

_LEVELS = frozenset({"debug", "info", "warning", "error"})


def log(message: str, *, level: str = "info", stream: TextIO | None = None) -> None:
    """Write one normalized log line without configuring global logging state."""
    if not isinstance(message, str) or not message.strip():
        raise ValueError("message must be a non-empty string")
    if not isinstance(level, str) or level.casefold() not in _LEVELS:
        raise ValueError("level must be debug, info, warning, or error")
    target = sys.stderr if stream is None else stream
    target.write(f"[{level.upper()}] {message.strip()}\n")
