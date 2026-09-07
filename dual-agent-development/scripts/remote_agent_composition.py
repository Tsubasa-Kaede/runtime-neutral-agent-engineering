"""V3.1-F: Remote collaboration composition root — declaration to session.

build_remote_session turns one DECLARED agent (an agent_id + role present
in an AgentRegistry) into a usable RemoteAgentSession over the V3.1 remote
seam: it derives the remote address from the binding, carries the adapter
construction knowledge across the process boundary, and builds the
transport and the session. It judges nothing — the declaration is
consumed exactly as registered, and the only checks are the
construction-consistency ones needed to build safely.

What it deliberately is not: a registry of adapters or constructions
(RemoteAdapterConstruction is a declaration-attached description that
never lives outside a manifest), a finder of agents (exact agent_id +
role lookup only), a judge of trust or readiness, a lifecycle owner (the
session and the transport own themselves), or a watcher of the child
process. It mints no envelope errors: every refusal here is a
composition-time ValueError in the house style, and anything the
transport or session layers own surfaces exactly as they raised it.

The child glue is generated source, never a second protocol: it makes the
declared module importable, resolves the declared attribute path,
constructs the adapter with the declared profile (or None), and hands the
adapter to the production endpoint loop verbatim.
"""
from __future__ import annotations

import importlib
import subprocess
import sys
from dataclasses import dataclass, fields
from pathlib import Path

from agent_identity import agent_address
from external_runtime import RuntimeProfile
from remote_agent_session import RemoteAgentSession
from remote_subprocess_transport import SubprocessStdioEnvelopeTransport

_SCRIPTS_DIR = Path(__file__).resolve().parent
_IDENTITY_KEYS = ("runtime", "provider", "model")

_ERROR_MODULE = "module must be a non-empty string"
_ERROR_ATTR = "attr must be a dotted path of identifiers"
_ERROR_PROFILE_DICT = (
    "profile must be a dict of RuntimeProfile keyword arguments "
    "when provided")
_ERROR_MISSING_IDENTITY_KEYS = (
    "profile must declare runtime, provider and model")
_ERROR_SOURCE_PATH = "source_path must be a non-empty string when provided"
_ERROR_NOT_CONSTRUCTIBLE = "adapter factory is not remotely constructible"


@dataclass(frozen=True)
class RemoteAdapterConstruction:
    """Declaration-attached description of how a child constructs the
    adapter: the import module, the dotted attribute path of a factory
    callable with the (profile=...) convention, optional RuntimeProfile
    keyword arguments, and an optional import path entry. A value carried
    on a manifest's factory — never collected, keyed or enumerated."""

    module: str
    attr: str
    profile: dict | None
    source_path: str | None = None

    def __post_init__(self):
        if not isinstance(self.module, str) or not self.module:
            raise ValueError(_ERROR_MODULE)
        if not isinstance(self.attr, str) or not self.attr:
            raise ValueError(_ERROR_ATTR)
        for segment in self.attr.split("."):
            if not segment.isidentifier():
                raise ValueError(_ERROR_ATTR)
        if self.profile is not None:
            if not isinstance(self.profile, dict):
                raise ValueError(_ERROR_PROFILE_DICT)
            for key in _IDENTITY_KEYS:
                if key not in self.profile:
                    raise ValueError(_ERROR_MISSING_IDENTITY_KEYS)
        if self.source_path is not None and (
                not isinstance(self.source_path, str)
                or not self.source_path):
            raise ValueError(_ERROR_SOURCE_PATH)


@dataclass(frozen=True)
class RemoteCompositionFacts:
    """The closed four-field echo of one successful composition: who was
    composed, in which role, at which derived address, on which declared
    runtime identity. Inert construction facts — nothing about processes,
    timings, health or diagnostics."""

    agent_id: str
    role: str
    remote_address: str
    runtime_identity: tuple


def facts_field_names() -> tuple[str, ...]:
    """The closed field vocabulary of RemoteCompositionFacts."""
    return tuple(field.name for field in fields(RemoteCompositionFacts))


def importable_adapter_factory(module, attr, profile=None, *,
                               source_path=None):
    """Declarer-facing: a zero-arg parent-side lazy callable (import +
    resolve + construct on call — usable by parent-side composition
    callers) tagged with __remote_construction__ carrying the
    RemoteAdapterConstruction descriptor. Store it in an AgentManifest."""
    construction = RemoteAdapterConstruction(
        module=module, attr=attr, profile=profile, source_path=source_path)

    def _remote_adapter_factory():
        factory = _resolve_dotted(construction.module, construction.attr)
        if construction.profile is not None:
            return factory(profile=RuntimeProfile(**construction.profile))
        return factory(profile=None)

    _remote_adapter_factory.__remote_construction__ = construction
    return _remote_adapter_factory


def build_remote_session(registry, agent_id, role, local_address, *,
                         timeout_seconds: float = 300.0):
    """Composition root: one declared agent -> (RemoteAgentSession, facts).

    Spec §16 order: exact lookup, declared-role gate, tagged-descriptor
    gate, binding consistency, address projection, glue, transport,
    session, facts. Composition refusals are closed-message ValueErrors;
    transport and session refusals surface exactly as their owners raised
    them (with the already-built transport cleaned up first).
    """
    manifest = registry.get(agent_id)
    if manifest is None:
        raise ValueError(f"unknown agent: {agent_id}")
    if role not in manifest.declared_roles:
        raise ValueError(f"role not declared by agent: {agent_id}/{role}")
    construction = getattr(
        manifest.adapter_factory, "__remote_construction__", None)
    if not isinstance(construction, RemoteAdapterConstruction):
        raise ValueError(_ERROR_NOT_CONSTRUCTIBLE)
    if construction.profile is not None:
        identity = manifest.binding.runtime_identity
        for key, declared in zip(_IDENTITY_KEYS, identity):
            if construction.profile[key] != declared:
                raise ValueError(
                    f"profile conflicts with agent binding: {key}")
    remote_address = agent_address(manifest.binding.agent, role)
    transport = SubprocessStdioEnvelopeTransport(
        [sys.executable, "-c", _build_glue(construction, remote_address)],
        timeout_seconds=timeout_seconds,
        stderr=subprocess.PIPE)
    try:
        session = RemoteAgentSession(
            local_address, remote_address, transport)
    except BaseException:
        transport.close()
        raise
    facts = RemoteCompositionFacts(
        agent_id=agent_id,
        role=role,
        remote_address=remote_address,
        runtime_identity=manifest.binding.runtime_identity)
    return session, facts


def _resolve_dotted(module, attr):
    """Parent-side resolution: import the module, then walk the dotted
    attribute path. The child glue performs the same resolution."""
    target = importlib.import_module(module)
    for segment in attr.split("."):
        target = getattr(target, segment)
    return target


def _build_glue(construction, remote_address):
    """Generate the child source: make the declared module importable,
    resolve the declared factory, construct the adapter, hand it to the
    production endpoint loop. Every dynamic value is embedded via repr."""
    lines = [
        "import sys",
        f"sys.path.insert(0, {str(_SCRIPTS_DIR)!r})",
    ]
    if construction.source_path is not None:
        lines.append(f"sys.path.insert(0, {construction.source_path!r})")
    lines.append("import importlib")
    lines.append(
        f"factory = importlib.import_module({construction.module!r})")
    for segment in construction.attr.split("."):
        lines.append(f"factory = getattr(factory, {segment!r})")
    if construction.profile is not None:
        lines.append("from external_runtime import RuntimeProfile")
        lines.append(
            "adapter = factory("
            f"profile=RuntimeProfile(**{construction.profile!r}))")
    else:
        lines.append("adapter = factory(profile=None)")
    lines.extend([
        "if adapter is None:",
        '    sys.stderr.write("remote adapter construction failed\\n")',
        "    raise SystemExit(3)",
        "from remote_agent_endpoint import run_endpoint",
        "run_endpoint(adapter, "
        f"{remote_address!r}, sys.stdin.buffer, sys.stdout.buffer)",
    ])
    return "\n".join(lines) + "\n"
