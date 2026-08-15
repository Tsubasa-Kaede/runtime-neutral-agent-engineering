"""Candidate Adapter Contract — runtime-neutral bridge.

Any adapter exposing the neutral CandidateAdapter surface (identity fields,
declared capabilities, an injected probe and a future invocation spec) can
be described as a CandidateRuntimeInstance. The bridge copies fields
verbatim: it never branches on runtime/provider/model values, never guesses
missing evidence, and never calls into a runtime.
"""
from __future__ import annotations

from typing import Any, Mapping, Protocol

from candidate_validation import CandidateRuntimeInstance


class CandidateAdapter(Protocol):
    """Minimal contract: identity dimensions stay independent fields."""

    runtime_id: str
    provider_id: str
    model_id: str | None
    config_fingerprint: str
    capability_context: tuple
    probe: Any
    invocation_spec: Mapping[str, Any]


def candidate_from_adapter(adapter: CandidateAdapter) -> CandidateRuntimeInstance:
    return CandidateRuntimeInstance(
        runtime_id=adapter.runtime_id,
        provider_id=adapter.provider_id,
        model_id=adapter.model_id,
        config_fingerprint=adapter.config_fingerprint,
        capability_context=tuple(adapter.capability_context),
        probe=adapter.probe,
        invocation_spec=adapter.invocation_spec,
    )
