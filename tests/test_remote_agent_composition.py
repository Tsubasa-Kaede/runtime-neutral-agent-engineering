"""V3.1-F offline tests — Remote Collaboration Composition Root.

Covers the composition root over the production V3.1 seam: the
declaration-attached adapter construction descriptor, the zero-arg tagged
factory (compatible with the V3.0-C composer call convention), the closed
composition refusals, the closed facts contract, the generated child glue
text, full child-process construction round trips through a test-owned
scripted module reached via the declared source_path, and the source-scan
locks (import surface, banned vocabulary, public surface).

The scripted double module's SOURCE lives in this file and is materialized
at run time into a temp directory (spec §10 erratum) — the production
three-file set is untouched and source_path is exercised against a
genuinely non-scripts directory.
"""
from __future__ import annotations

import ast
import shutil
import sys
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from agent_identity import AgentIdentity, AgentRuntimeBinding
from agent_manifest import AgentManifest, AgentRegistry
from collaboration_packet import CollaborationPacket, CollaborationPayloadType
from external_runtime import RuntimeProfile
from remote_agent_composition import (  # noqa: E402
    RemoteAdapterConstruction,
    RemoteCompositionFacts,
    _build_glue,
    _SCRIPTS_DIR,
    build_remote_session,
    facts_field_names,
    importable_adapter_factory,
)
from remote_contract import (  # noqa: E402
    RemoteEnvelopeErrorCode,
    RemoteEnvelopeError,
    RemoteEnvelopeStatus,
)
from remote_agent_session import RemoteAgentSession  # noqa: E402
from structured_packets import ArchitecturePacket, ImplementationPacket  # noqa: E402

SOURCE = (SCRIPTS / "remote_agent_composition.py").read_text(encoding="utf-8")
MODULE_AST = ast.parse(SOURCE)

LOCAL = "agent:parent-arch:architect"
REMOTE = "agent:double-agent:coder"
TASK_ID = "T-F1"

IDENTITY = ("double-runtime", None, None, "default")

DOUBLE_PROFILE = {
    "agent_id": "double-agent",
    "runtime": "double-runtime",
    "provider": None,
    "model": None,
    "role": "coder",
    "capabilities": frozenset(),
}

# The test-owned scripted adapter module (child side AND parent side).
# build follows the production factory convention (single profile keyword
# argument); invoke returns a valid ImplementationPacket JSON payload.
SCRIPTED_SOURCE = '''"""Test-owned scripted adapter module (offline proof)."""
import json

from external_runtime import (
    InvocationResult,
    InvocationStatus,
    InvocationTrace,
    new_invocation_id,
)

OUTPUT_TEXT = json.dumps({
    "task_id": "double-task",
    "role": "coder",
    "changed_files": [],
    "implementation_summary": "double summary",
    "implementation_details": ["double detail"],
    "assumptions": [],
    "unresolved_items": [],
    "test_requirements": ["double test"],
})


class ScriptedAdapter:
    def __init__(self, profile=None):
        self.profile = profile

    def invoke(self, request):
        trace = InvocationTrace(
            invocation_id=new_invocation_id(),
            task_id=request.task_id,
            agent_id=request.agent_id,
            runtime="double-runtime",
            provider=None,
            model=None,
            role=request.role,
            status=InvocationStatus.SUCCESS,
            duration_ms=1,
            exit_code=0,
        )
        return InvocationResult(
            status=InvocationStatus.SUCCESS,
            output=OUTPUT_TEXT,
            trace=trace,
        )


def build(profile=None):
    return ScriptedAdapter(profile=profile)


def build_none(profile=None):
    return None
'''


def make_binding(agent_id="double-agent", identity=IDENTITY):
    return AgentRuntimeBinding(agent=AgentIdentity(agent_id),
                               runtime_identity=identity)


def make_manifest(factory, agent_id="double-agent", roles=("coder",),
                  identity=IDENTITY):
    return AgentManifest(binding=make_binding(agent_id, identity),
                         declared_roles=roles, adapter_factory=factory)


def make_registry(*manifests):
    registry = AgentRegistry()
    for manifest in manifests:
        registry.register(manifest)
    return registry


def arch_packet(correlation_id, task_id=TASK_ID, target=REMOTE):
    return CollaborationPacket(
        correlation_id=correlation_id,
        task_id=task_id,
        source_agent=LOCAL,
        target_agent=target,
        source_role="architect",
        target_role="coder",
        payload_type=CollaborationPayloadType.ARCHITECTURE,
        payload=ArchitecturePacket(
            task_id=task_id, role="architect", goal=("g",),
            constraints=("c",), architecture=("a",), interfaces=({},),
            implementation_steps=({},), acceptance_criteria=("ac",),
            risks=({},)),
    )


class TempModuleMixin(unittest.TestCase):
    """Materialize a module source into a fresh temp directory."""

    def write_module(self, name):
        tmpdir = tempfile.mkdtemp(prefix="f-composition-")
        self.addCleanup(shutil.rmtree, tmpdir, True)
        path = Path(tmpdir) / f"{name}.py"
        path.write_text(SCRIPTED_SOURCE, encoding="utf-8")
        return tmpdir


class ImportableAdapterFactoryTests(TempModuleMixin):

    def test_returns_zero_arg_callable_with_descriptor_tag(self):
        factory = importable_adapter_factory("m", "A.b")
        self.assertTrue(callable(factory))
        construction = getattr(factory, "__remote_construction__", None)
        self.assertIsInstance(construction, RemoteAdapterConstruction)

    def test_descriptor_fields_exact_with_defaults(self):
        construction = importable_adapter_factory(
            "m", "A.b").__remote_construction__
        self.assertEqual(construction.module, "m")
        self.assertEqual(construction.attr, "A.b")
        self.assertIsNone(construction.profile)
        self.assertIsNone(construction.source_path)

    def test_parent_side_zero_arg_call_constructs_profiled_adapter(self):
        tmpdir = self.write_module("double_parent")
        sys.path.insert(0, tmpdir)
        self.addCleanup(sys.path.remove, tmpdir)
        self.addCleanup(sys.modules.pop, "double_parent", None)
        factory = importable_adapter_factory(
            "double_parent", "build", profile=DOUBLE_PROFILE,
            source_path=tmpdir)
        adapter = factory()
        self.assertIsNotNone(adapter)
        self.assertIsInstance(adapter.profile, RuntimeProfile)
        self.assertEqual(adapter.profile.runtime, "double-runtime")
        self.assertIsNone(adapter.profile.provider)
        self.assertIsNone(adapter.profile.model)
        self.assertEqual(adapter.profile.role, "coder")

    def test_parent_side_profile_none_calls_factory_with_none(self):
        tmpdir = self.write_module("double_parent")
        sys.path.insert(0, tmpdir)
        self.addCleanup(sys.path.remove, tmpdir)
        self.addCleanup(sys.modules.pop, "double_parent", None)
        factory = importable_adapter_factory("double_parent", "build")
        adapter = factory()
        self.assertIsNotNone(adapter)
        self.assertIsNone(adapter.profile)

    def test_factory_composes_with_v3c_caller(self):
        # The V3.0-C composer calls manifest.adapter_factory() with zero
        # arguments exactly once per agent — the tagged lazy callable must
        # satisfy that convention unchanged.
        from agent_composition import compose_agent_slots
        tmpdir = self.write_module("double_parent")
        sys.path.insert(0, tmpdir)
        self.addCleanup(sys.path.remove, tmpdir)
        self.addCleanup(sys.modules.pop, "double_parent", None)
        factory = importable_adapter_factory(
            "double_parent", "build", profile=DOUBLE_PROFILE,
            source_path=tmpdir)
        slots = compose_agent_slots(
            make_registry(make_manifest(factory)),
            ("double-agent",), ("coder",))
        self.assertEqual(len(slots), 1)
        self.assertIsNotNone(slots[0].adapter)
        self.assertEqual(slots[0].agent_id, "double-agent")


class CompositionRefusalTests(TempModuleMixin):

    def _build(self, registry, local_address=LOCAL, **kwargs):
        return build_remote_session(
            registry, "double-agent", "coder", local_address, **kwargs)

    def test_unknown_agent_is_a_value_error_not_an_envelope_error(self):
        with self.assertRaises(ValueError) as caught:
            build_remote_session(make_registry(), "double-agent", "coder",
                                 LOCAL)
        self.assertEqual(str(caught.exception), "unknown agent: double-agent")
        self.assertNotIsInstance(caught.exception, RemoteEnvelopeError)

    def test_role_not_declared_is_a_closed_value_error(self):
        factory = importable_adapter_factory("m", "build")
        with self.assertRaises(ValueError) as caught:
            build_remote_session(
                make_registry(make_manifest(factory)), "double-agent",
                "tester", LOCAL)
        self.assertIn("role not declared", str(caught.exception))

    def test_untagged_factory_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            self._build(make_registry(make_manifest(lambda: None)))
        self.assertIn("not remotely constructible", str(caught.exception))

    def test_junk_tag_value_is_refused(self):
        def plain():
            return None
        plain.__remote_construction__ = "junk"
        with self.assertRaises(ValueError):
            self._build(make_registry(make_manifest(plain)))

    def test_empty_module_is_refused_at_descriptor_construction(self):
        with self.assertRaises(ValueError):
            RemoteAdapterConstruction(module="", attr="build",
                                      profile=None, source_path=None)

    def test_non_identifier_attr_segment_is_refused(self):
        with self.assertRaises(ValueError):
            RemoteAdapterConstruction(module="m", attr="ClaudeCode.1x",
                                      profile=None, source_path=None)

    def test_profile_missing_identity_key_is_refused(self):
        partial = dict(DOUBLE_PROFILE)
        del partial["model"]
        with self.assertRaises(ValueError) as caught:
            RemoteAdapterConstruction(module="m", attr="build",
                                      profile=partial, source_path=None)
        self.assertIn("runtime, provider and model", str(caught.exception))

    def _mismatch_case(self, key, declared_value):
        profile = dict(DOUBLE_PROFILE)
        profile[key] = declared_value
        factory = importable_adapter_factory(
            "double_scripted", "build", profile=profile, source_path="/nowhere")
        binding_identity = IDENTITY
        with self.assertRaises(ValueError) as caught:
            self._build(make_registry(make_manifest(
                factory, identity=binding_identity)))
        self.assertIn("profile conflicts with agent binding",
                      str(caught.exception))
        self.assertIn(key, str(caught.exception))

    def test_runtime_mismatch_is_refused(self):
        self._mismatch_case("runtime", "other-runtime")

    def test_provider_mismatch_is_refused(self):
        self._mismatch_case("provider", "somebody")

    def test_model_mismatch_is_refused(self):
        self._mismatch_case("model", "some-model")

    def test_profile_none_skips_consistency_entirely(self):
        # A None profile declares no runtime facts, so any binding is
        # consistent by construction — the composition proceeds to a
        # session without spawning anything (the transport spawns lazily).
        factory = importable_adapter_factory("double_scripted", "build")
        registry = make_registry(make_manifest(
            factory, identity=("anything", "x", "y", "z")))
        session, facts = self._build(registry)
        self.addCleanup(session.close)
        self.assertIsInstance(session, RemoteAgentSession)
        self.assertEqual(facts.runtime_identity,
                         ("anything", "x", "y", "z"))


class CompositionFactsTests(TempModuleMixin):

    def test_facts_exact_values(self):
        factory = importable_adapter_factory(
            "double_scripted", "build", profile=DOUBLE_PROFILE,
            source_path="/nowhere")
        session, facts = build_remote_session(
            make_registry(make_manifest(factory)), "double-agent", "coder",
            LOCAL)
        self.addCleanup(session.close)
        self.assertIsInstance(facts, RemoteCompositionFacts)
        self.assertEqual(facts.agent_id, "double-agent")
        self.assertEqual(facts.role, "coder")
        self.assertEqual(facts.remote_address, REMOTE)
        self.assertEqual(facts.runtime_identity, IDENTITY)

    def test_facts_field_names_are_the_closed_four(self):
        self.assertEqual(
            facts_field_names(),
            ("agent_id", "role", "remote_address", "runtime_identity"))

    def test_facts_are_frozen(self):
        factory = importable_adapter_factory("double_scripted", "build")
        session, facts = build_remote_session(
            make_registry(make_manifest(factory)), "double-agent", "coder",
            LOCAL)
        self.addCleanup(session.close)
        with self.assertRaises(FrozenInstanceError):
            facts.agent_id = "mutated"

    def test_timeout_keyword_is_accepted(self):
        factory = importable_adapter_factory(
            "double_scripted", "build", profile=DOUBLE_PROFILE,
            source_path="/nowhere")
        session, _facts = build_remote_session(
            make_registry(make_manifest(factory)), "double-agent", "coder",
            LOCAL, timeout_seconds=7.0)
        self.addCleanup(session.close)
        self.assertIsInstance(session, RemoteAgentSession)


class GlueTextTests(unittest.TestCase):

    def _glue(self, **overrides):
        values = {"module": "mm", "attr": "A.b", "profile": None,
                  "source_path": None}
        values.update(overrides)
        construction = RemoteAdapterConstruction(**values)
        return _build_glue(construction, "agent:x:coder")

    def test_scripts_directory_is_inserted(self):
        glue = self._glue()
        self.assertIn(f"sys.path.insert(0, {str(_SCRIPTS_DIR)!r})", glue)

    def test_source_path_line_present_iff_declared(self):
        with_path = self._glue(source_path="some/where")
        self.assertIn('sys.path.insert(0, \'some/where\')', with_path)
        without_path = self._glue()
        self.assertNotIn("some/where", without_path)

    def test_attr_segments_resolved_by_getattr_lines(self):
        glue = self._glue()
        self.assertIn("factory = getattr(factory, 'A')", glue)
        self.assertIn("factory = getattr(factory, 'b')", glue)
        self.assertIn("factory = importlib.import_module('mm')", glue)

    def test_profile_branch(self):
        profiled = self._glue(profile=DOUBLE_PROFILE)
        self.assertIn("from external_runtime import RuntimeProfile",
                      profiled)
        self.assertIn("adapter = factory(", profiled)
        self.assertIn("RuntimeProfile(**", profiled)
        self.assertNotIn("factory(profile=None)", profiled)
        unprofiled = self._glue()
        self.assertIn("adapter = factory(profile=None)", unprofiled)
        self.assertNotIn("RuntimeProfile", unprofiled)

    def test_none_result_guard_writes_closed_diagnostic_then_exits(self):
        glue = self._glue()
        self.assertIn("remote adapter construction failed", glue)
        self.assertIn("raise SystemExit(3)", glue)

    def test_endpoint_handoff_and_no_second_protocol(self):
        glue = self._glue()
        self.assertIn(
            "run_endpoint(adapter, 'agent:x:coder', "
            "sys.stdin.buffer, sys.stdout.buffer)", glue)
        self.assertNotIn("deserialize_remote_envelope", glue)
        self.assertNotIn("serialize_remote_envelope", glue)


class ChildConstructionE2ETests(TempModuleMixin):

    def setUp(self):
        self.tmpdir = self.write_module("double_scripted")

    def _session(self, attr="build", agent_id="double-agent",
                 local_address=LOCAL, **kwargs):
        factory = importable_adapter_factory(
            "double_scripted", attr, profile=DOUBLE_PROFILE,
            source_path=self.tmpdir)
        session, facts = build_remote_session(
            make_registry(make_manifest(factory, agent_id=agent_id)),
            agent_id, "coder", local_address, **kwargs)
        return session, facts

    def test_full_round_trip_through_a_real_child(self):
        session, facts = self._session()
        self.addCleanup(session.close)
        self.assertEqual(facts.remote_address, REMOTE)
        receipt = session.send(arch_packet(session.correlation_id))
        self.assertIs(receipt.status, RemoteEnvelopeStatus.DELIVERED)
        response = session.receive()
        self.assertIsNotNone(response)
        self.assertEqual(response.sender, REMOTE)
        self.assertEqual(response.recipient, LOCAL)
        self.assertEqual(response.correlation_id, session.correlation_id)
        self.assertEqual(response.role, "architect")
        self.assertIsInstance(response.payload, CollaborationPacket)
        self.assertIs(response.payload.payload_type,
                      CollaborationPayloadType.IMPLEMENTATION)
        self.assertIsInstance(response.payload.payload, ImplementationPacket)
        self.assertEqual(response.payload.payload.implementation_summary,
                         "double summary")
        self.assertEqual(response.payload.task_id, TASK_ID)
        self.assertEqual(response.payload.payload.task_id, TASK_ID)
        self.assertEqual(response.payload.acceptance_criteria,
                         response.payload.payload.test_requirements)
        self.assertEqual(response.payload.provenance, "OFFLINE")

    def test_none_factory_result_fails_first_send_with_diagnostic(self):
        session, _facts = self._session(attr="build_none")
        self.addCleanup(session.close)
        receipt = session.send(arch_packet(session.correlation_id))
        self.assertIs(receipt.status, RemoteEnvelopeStatus.FAILED)
        self.assertIs(receipt.error_code,
                      RemoteEnvelopeErrorCode.DELIVERY_FAILED)
        # The reaped child's closed diagnostic line, through the existing
        # B2 observation seam (captured already during the failed
        # exchange; the transport is reachable only through the session
        # this composition root built).
        self.assertIn(b"remote adapter construction failed",
                      session._transport.last_diagnostics)

    def test_unknown_module_fails_first_send_with_traceback(self):
        factory = importable_adapter_factory(
            "double_missing_mod", "build", profile=DOUBLE_PROFILE,
            source_path=self.tmpdir)
        session, _facts = build_remote_session(
            make_registry(make_manifest(factory)), "double-agent", "coder",
            LOCAL)
        self.addCleanup(session.close)
        receipt = session.send(arch_packet(session.correlation_id))
        self.assertIs(receipt.status, RemoteEnvelopeStatus.FAILED)
        self.assertIn(b"ModuleNotFoundError",
                      session._transport.last_diagnostics)

    def test_source_path_is_genuinely_load_bearing(self):
        # The scripted module exists only in the temp directory; without
        # the declared source_path the child cannot import it.
        factory = importable_adapter_factory(
            "double_scripted", "build", profile=DOUBLE_PROFILE)
        session, _facts = build_remote_session(
            make_registry(make_manifest(factory)), "double-agent", "coder",
            LOCAL)
        self.addCleanup(session.close)
        receipt = session.send(arch_packet(session.correlation_id))
        self.assertIs(receipt.status, RemoteEnvelopeStatus.FAILED)
        self.assertIn(b"ModuleNotFoundError",
                      session._transport.last_diagnostics)


class PropagationTests(TempModuleMixin):

    def test_invalid_local_address_propagates_c1_refusal_unwrapped(self):
        factory = importable_adapter_factory(
            "double_scripted", "build", profile=DOUBLE_PROFILE,
            source_path="/nowhere")
        with self.assertRaises(RemoteEnvelopeError) as caught:
            build_remote_session(
                make_registry(make_manifest(factory)), "double-agent",
                "coder", "")
        self.assertIs(caught.exception.category,
                      RemoteEnvelopeErrorCode.INVALID_ENVELOPE)
        self.assertIn("local_address", caught.exception.detail)

    def test_non_positive_timeout_propagates_b2_refusal_verbatim(self):
        factory = importable_adapter_factory(
            "double_scripted", "build", profile=DOUBLE_PROFILE,
            source_path="/nowhere")
        with self.assertRaises(ValueError) as caught:
            build_remote_session(
                make_registry(make_manifest(factory)), "double-agent",
                "coder", LOCAL, timeout_seconds=0)
        self.assertEqual(str(caught.exception),
                         "timeout_seconds must be a positive number")


class SourceScanTests(unittest.TestCase):

    def test_import_surface_exact(self):
        imported = set()
        for node in ast.walk(MODULE_AST):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module)
        self.assertEqual(
            imported,
            {"__future__", "importlib", "subprocess", "sys",
             "dataclasses", "pathlib", "agent_identity",
             "external_runtime", "remote_agent_session",
             "remote_subprocess_transport"})

    def test_banned_vocabulary_absent(self):
        banned = (
            "AdapterRegistry", "RemoteAdapterRegistry",
            "RuntimeFactoryRegistry", "descriptor registry",
            "plugin registry", "def register",
            "discover", "admission", "qualification", "select",
            "orchestration", "retry", "reconnect", "supervis",
            "telemetry",
        )
        for term in banned:
            self.assertNotIn(term, SOURCE, term)

    def test_no_envelope_error_or_unknown_agent_minting(self):
        self.assertNotIn("RemoteEnvelopeError", SOURCE)
        self.assertNotIn("UNKNOWN_AGENT", SOURCE)

    def test_no_catch_all_except(self):
        self.assertNotIn("except Exception", SOURCE)
        self.assertNotIn("except:", SOURCE)

    def test_public_surface_frozen(self):
        names = set()
        for node in MODULE_AST.body:
            if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                names.add(node.name)
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        names.add(target.id)
        self.assertEqual(
            names,
            {"RemoteAdapterConstruction", "RemoteCompositionFacts",
             "importable_adapter_factory", "build_remote_session",
             "facts_field_names", "_build_glue", "_resolve_dotted",
             "_SCRIPTS_DIR", "_IDENTITY_KEYS",
             "_ERROR_MODULE", "_ERROR_ATTR", "_ERROR_PROFILE_DICT",
             "_ERROR_MISSING_IDENTITY_KEYS", "_ERROR_SOURCE_PATH",
             "_ERROR_NOT_CONSTRUCTIBLE"})

    def test_timeout_feeds_the_transport(self):
        self.assertIn("timeout_seconds=timeout_seconds", SOURCE)


if __name__ == "__main__":
    unittest.main()
