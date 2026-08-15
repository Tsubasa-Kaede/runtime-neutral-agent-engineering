"""Shared content-safety scan for collaboration packet and trace fields.

Single source of truth for the unsafe-marker scan used by the collaboration
layer: a recursive substring check over the whole packet (including open
dict fields like failures/findings/interfaces) and a trace sanitizer that
replaces raw stderr/error text with a safe label. Pure and offline.
"""
from __future__ import annotations

from dataclasses import asdict, replace

SECRET_MARKERS = ("token", "secret", "api_key", "authorization", "bearer", "stdout", "stderr")

# Safe label for redacted runtime output; carries no secret/raw marker.
REDACTED_ERROR = "RUNTIME_FAILURE_REDACTED"


def contains_unsafe_content(value) -> bool:
    """True if any string anywhere in value holds an unsafe marker."""
    if isinstance(value, str):
        lowered = value.lower()
        return any(marker in lowered for marker in SECRET_MARKERS)
    if isinstance(value, dict):
        return any(contains_unsafe_content(key) or contains_unsafe_content(item)
                   for key, item in value.items())
    if isinstance(value, (tuple, list, set, frozenset)):
        return any(contains_unsafe_content(item) for item in value)
    return False


def packet_has_unsafe_content(packet) -> bool:
    """Whole-packet recursive scan, including open dict fields."""
    return contains_unsafe_content(asdict(packet))


def sanitize_trace(trace):
    """Return a copy whose error field is redacted if it holds raw unsafe text.

    Leaves the trace untouched (or returns None as-is) when the error is
    absent or already safe, so safe error classifications are preserved.
    """
    if trace is None or trace.error is None:
        return trace
    if contains_unsafe_content(trace.error):
        return replace(trace, error=REDACTED_ERROR)
    return trace
