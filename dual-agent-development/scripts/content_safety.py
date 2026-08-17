"""Shared content-safety scan for collaboration packet and trace fields.

Two-tier semantics (Phase G15, evidence-based split):
- STRING VALUES are scanned for credential SHAPES — assignment patterns
  (token=..., api_key:..., password=...), bearer-credential shapes and
  sk- style key shapes — matching the proven structured_packets pattern.
  Bare prose mentioning a marker word ("must not write to stdout") is not
  a leak; a real credential assignment is.
- STRUCTURAL KEYS keep marker-substring strictness: a dict key like
  "stdout"/"api_key" IS a raw-dump field regardless of its value.
Single source of truth; nothing here relaxes trace redaction.
"""
from __future__ import annotations

import re
from dataclasses import asdict, replace

SECRET_MARKERS = ("token", "secret", "api_key", "authorization", "bearer", "stdout", "stderr")

# Credential shapes: assignment forms, bearer-credential forms and sk-
# style key material in ANY string value.
_CREDENTIAL_SHAPE = re.compile(
    r"(?i)(api[-_ ]?key|token|secret|authorization|password)"
    r"\s*[:=]\s*\S+"
    r"|bearer\s+\S+"
    r"|sk-[A-Za-z0-9_-]{8,}"
)

# Safe label for redacted runtime output; carries no secret/raw marker.
REDACTED_ERROR = "RUNTIME_FAILURE_REDACTED"


def _marker_in_key(key) -> bool:
    lowered = str(key).lower()
    return any(marker in lowered for marker in SECRET_MARKERS)


def contains_unsafe_content(value) -> bool:
    """True if a credential shape appears in any string value, or a marker
    word appears in any structural dict key."""
    if isinstance(value, str):
        return bool(_CREDENTIAL_SHAPE.search(value))
    if isinstance(value, dict):
        return any(_marker_in_key(key) or contains_unsafe_content(item)
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
    if contains_unsafe_content(trace.error) or _marker_in_key(trace.error):
        return replace(trace, error=REDACTED_ERROR)
    return trace
