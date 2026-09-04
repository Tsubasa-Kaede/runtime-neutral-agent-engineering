"""V3.0-A: Agent Identity — the logical identity layer above Runtime.

V2 froze with runtime as the first-class citizen: the only identity
entity is the runtime four-tuple (runtime_id, provider_id, model_id,
config_fingerprint), and every "agent" in the collaboration stack is a
deterministic projection of that tuple plus a role. V3.0-A introduces
the missing first-class concept WITHOUT touching any V2 module:

    AgentIdentity      WHO  — a stable logical identity, nothing else
    AgentRuntimeBinding WHO uses WHERE — the mutable fact that one agent
                          currently executes on one runtime identity
    agent_address      the collaboration address for (agent, role):
                          runtime-neutral, stable across rebinding
    compat_collab_address  projection of an AgentIdentity through its
                          binding onto V2's collab_agent_address, so a
                          V3 agent can drive the frozen V2 stack verbatim

Invariants (locked by tests/test_agent_identity.py):
- agent_id is never derived from, and never contains, any runtime fact
  (runtime/provider/model/config), capability, trust, verification,
  provenance or budget data. Identity answers WHO only.
- Runtime-side changes (rebinding, model swap, config fingerprint drift)
  change the binding, never the identity.
- One runtime identity may host many agents; one agent binds exactly
  one runtime at a time in V3.0-A (no multi-binding yet).
- The compatibility projection is exact: for the same runtime identity
  and role it reproduces V2's collab_agent_address byte-for-byte.

NOT implemented here (later V3 phases): manifests, discovery, registries,
capability attachment, verification, trust, admission, orchestration,
remote transports, token governance. This module deliberately contains
no registry and no state — values only.
"""
from __future__ import annotations

from content_safety import SECRET_MARKERS, contains_unsafe_content
from dataclasses import dataclass, fields

# V2's runtime identity is a plain 4-tuple (see candidate_validation.py
# CandidateValidationResult.identity). Reused verbatim — no new type.
RUNTIME_IDENTITY_LENGTH = 4


@dataclass(frozen=True)
class AgentIdentity:
    """WHO: one stable logical agent identity. Exactly one field.

    Identity carries nothing about WHERE the agent executes (that is a
    binding), WHAT it can do (capability evidence, later), WHETHER it
    is trusted (trust state, later), or any provenance/budget fact.
    """

    agent_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.agent_id, str) or not self.agent_id:
            raise ValueError("agent_id must be a non-empty string")
        # A V3 agent id must never smuggle runtime facts. The reserved
        # prefix below is the V2 address namespace (a JSON array string);
        # rejecting it keeps the two address spaces disjoint.
        if self.agent_id.lstrip().startswith(("[", "{")):
            raise ValueError(
                "agent_id must be a logical name, not an encoded structure")
        # 复用既有单一 secret policy（与 D1 的 _assert_clean 同款）：marker
        # 提及（大小写不敏感子串）与 credential 形状都在构造期被拒收 ——
        # identity 要安全充当 log 标识符 / 协作地址 / 观察投影组件。
        lowered = self.agent_id.lower()
        for marker in SECRET_MARKERS:
            if marker in lowered:
                raise ValueError(
                    "agent_id must not contain secret-shaped content")
        if contains_unsafe_content(self.agent_id):
            raise ValueError(
                "agent_id must not contain secret-shaped content")


@dataclass(frozen=True)
class AgentRuntimeBinding:
    """WHO uses WHERE: one agent's currently active runtime identity.

    The binding is the ONLY place runtime facts attach to an agent.
    Rebinding (creating a new binding with a different runtime identity)
    never touches AgentIdentity.
    """

    agent: AgentIdentity
    runtime_identity: tuple

    def __post_init__(self) -> None:
        if not isinstance(self.agent, AgentIdentity):
            raise TypeError("agent must be an AgentIdentity")
        identity = tuple(self.runtime_identity)
        if len(identity) != RUNTIME_IDENTITY_LENGTH:
            raise ValueError(
                "runtime_identity must be the V2 four-tuple "
                "(runtime_id, provider_id, model_id, config_fingerprint)")
        object.__setattr__(self, "runtime_identity", identity)

    @property
    def agent_id(self) -> str:
        return self.agent.agent_id

    def rebind(self, runtime_identity: tuple) -> "AgentRuntimeBinding":
        """Same agent, different WHERE. Identity is untouched by design."""
        return AgentRuntimeBinding(agent=self.agent,
                                   runtime_identity=runtime_identity)


def agent_address(agent: AgentIdentity, role: str) -> str:
    """Collaboration address for one agent in one role: WHO + WHAT
    RESPONSIBILITY. Runtime-neutral, opaque, stable across rebinding —
    contains no runtime/provider/model/config information."""
    if not isinstance(role, str) or not role:
        raise ValueError("role must be a non-empty string")
    return f"agent:{agent.agent_id}:{role}"


def compat_collab_address(binding: AgentRuntimeBinding, role: str) -> str:
    """Project one binding onto V2's address space, byte-for-byte.

    The frozen V2 collaboration stack addresses participants by
    runtime identity + role; this projection lets a V3 agent drive that
    stack unchanged. Exactness is contract: for the same runtime
    identity and role the result equals V2's collab_agent_address."""
    from collaboration_session import collab_agent_address

    return collab_agent_address(binding.runtime_identity, role)


def identity_field_names() -> tuple[str, ...]:
    """The closed field vocabulary of AgentIdentity (WHO only)."""
    return tuple(field.name for field in fields(AgentIdentity))
