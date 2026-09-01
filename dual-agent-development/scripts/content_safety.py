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

R6-C11 secret-safe validation diagnostics: a REJECT is observable at
field/index/rule granularity WITHOUT the rejected value ever leaving
the scanner. `record_validation_diagnostic` stores the last structured
diagnostic (layer/field/index/rule — never the raw text) next to the
scan that produced it; `last_validation_diagnostic` reads it back. The
scan semantics are untouched: REJECT stays REJECT, ACCEPT stays ACCEPT,
and the stored diagnostic carries no value material whatsoever.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, replace

SECRET_MARKERS = ("token", "secret", "api_key", "authorization", "bearer", "stdout", "stderr")

# Credential shapes: assignment forms, bearer-credential forms and sk-
_CREDENTIAL_SHAPE = re.compile(
    r"(?i)(api[-_ ]?key|token|secret|authorization|password)"
    r"\s*[:=]\s*\S+"
    r"|bearer\s+\S+"
    r"|sk-[A-Za-z0-9_-]{8,}"
)

# Credential shape in a KEY ("api_key=x" used as a dict key): the packet
# layer's _clean rejects these separately from marker substrings.
_SECRET_PATTERN_KEY = re.compile(
    r"(?i)(api[-_ ]?key|token|secret|authorization|password)\s*[:=]"
)

# Safe label for redacted runtime output; carries no secret/raw marker.
REDACTED_ERROR = "RUNTIME_FAILURE_REDACTED"


@dataclass(frozen=True)
class ValidationDiagnostic:
    """Secret-safe, field-level record of ONE validation REJECT.

    Carries only structural coordinates — layer, field name, list index
    (None for scalar fields), and a deterministic rule code. It NEVER
    carries the rejected value, any fragment of it, or any credential
    material; str/repr expose the coordinates only.
    """

    layer: str
    field: str
    index: int | None
    rule: str

    def __str__(self) -> str:
        index_part = "" if self.index is None else f"[{self.index}]"
        return (f"layer={self.layer} field={self.field}"
                f"{index_part} rule={self.rule}")


_LAST_DIAGNOSTIC: ValidationDiagnostic | None = None


def record_validation_diagnostic(diagnostic: ValidationDiagnostic) -> None:
    """Store the structured (value-free) diagnostic of the latest REJECT."""
    global _LAST_DIAGNOSTIC
    _LAST_DIAGNOSTIC = diagnostic


def last_validation_diagnostic() -> ValidationDiagnostic | None:
    """The latest recorded REJECT diagnostic (or None when the last scan
    accepted). Read-only observability; never carries value material."""
    return _LAST_DIAGNOSTIC


def reset_validation_diagnostic() -> None:
    """Clear the stored diagnostic (test/inspection boundary helper).

    The diagnostic slot is a single global "last REJECT" observation —
    tests read it right after the scan under test and reset it first so
    a prior reject cannot leak into the assertion."""
    global _LAST_DIAGNOSTIC
    _LAST_DIAGNOSTIC = None


def _marker_in_key(key) -> bool:
    lowered = str(key).lower()
    return any(marker in lowered for marker in SECRET_MARKERS)


def _diagnose_unsafe(value, layer: str, field: str):
    """Walk the value exactly like contains_unsafe_content, returning the
    FIRST offending coordinates (field, index, rule) — never the value.

    Used by the packet/envelope layers to make a REJECT observable at
    field granularity; the walk has no effect on the verdict itself.
    Dict nesting appends the key path so deep failures stay attributable
    (keys are structural metadata, safe to name as a path)."""
    if isinstance(value, str):
        if _CREDENTIAL_SHAPE.search(value):
            return ValidationDiagnostic(layer, field, None, "UNSAFE_SHAPE")
        return None
    if isinstance(value, dict):
        for key, item in value.items():
            lowered = str(key).lower()
            if _marker_in_key(key) or _SECRET_PATTERN_KEY.search(lowered):
                return ValidationDiagnostic(
                    layer, f"{field}.{key}", None, "UNSAFE_KEY")
            found = _diagnose_unsafe(item, layer, f"{field}.{key}")
            if found is not None:
                return found
        return None
    if isinstance(value, (tuple, list)):
        for index, item in enumerate(value):
            found = _diagnose_unsafe(item, layer, field)
            if found is not None:
                if found.field == field:
                    return ValidationDiagnostic(
                        layer, field, index, found.rule)
                return found
        return None
    return None


def diagnose_packet_reject(packet, layer: str) -> ValidationDiagnostic | None:
    """Locate the first unsafe coordinate in an ALREADY-BUILT packet.

    Returns None when the packet is clean. The scan mirrors
    packet_has_unsafe_content exactly (same verdicts, same first-match
    order); only observability is added. Used after a reject to name
    the layer/field/index/rule — the rejected values never surface."""
    for field_name, value in asdict(packet).items():
        found = _diagnose_unsafe(value, layer, field_name)
        if found is not None:
            return found
    return None


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
