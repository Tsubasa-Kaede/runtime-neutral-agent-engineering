"""Phase 10H-E1: Shared Collaboration State — append-only immutable ledger.

Wire-at-append storage, per-task dense ledger-assigned sequences,
provenance derived exclusively from envelopes, secret-free trace
summaries, and cross-record invariants. No chat, no raw output, no
in-place mutation: every append returns a new state.
"""
import inspect
import sys
import unittest
from dataclasses import FrozenInstanceError, fields
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from collaboration_packet import (
    CollaborationPacket,
    CollaborationPayloadType,
    serialize_collaboration_packet,
)
from collaboration_state import (
    CollaborationDirection,
    CollaborationRecord,
    SharedCollaborationState,
    TraceSummary,
)
from structured_packets import ArchitecturePacket, ImplementationPacket
from verified_selection_bridge import agent_id_for

SOURCE_AGENT = agent_id_for(("rt-a", "provider-a", "model-a", "fp-a"))
TARGET_AGENT = agent_id_for(("rt-b", "provider-b", "model-b", "fp-b"))

SECRET_MARKERS = ("token", "secret", "api_key", "authorization", "bearer", "stdout", "stderr")


def arch(task_id="T1", interfaces=({},)):
    return ArchitecturePacket(
        task_id=task_id, role="architect", goal=("g",), constraints=("c",),
        architecture=("a",), interfaces=interfaces, implementation_steps=({},),
        acceptance_criteria=("ac1",), risks=({},),
    )


def impl(task_id="T1"):
    return ImplementationPacket(
        task_id=task_id, role="coder", changed_files=("f.py",),
        implementation_summary="s", implementation_details=("d",),
        assumptions=(), unresolved_items=(), test_requirements=(),
    )


def envelope(correlation_id="C001", provenance="OFFLINE", payload=None, **overrides):
    values = dict(
        correlation_id=correlation_id,
        task_id="T1",
        source_agent=SOURCE_AGENT,
        target_agent=TARGET_AGENT,
        source_role="architect",
        target_role="coder",
        payload_type=CollaborationPayloadType.ARCHITECTURE,
        payload=payload if payload is not None else arch(),
        provenance=provenance,
    )
    values.update(overrides)
    return CollaborationPacket(**values)


def reply_envelope(correlation_id="C001", provenance="OFFLINE"):
    return CollaborationPacket(
        correlation_id=correlation_id, task_id="T1",
        source_agent=TARGET_AGENT, target_agent=SOURCE_AGENT,
        source_role="coder", target_role="architect",
        payload_type=CollaborationPayloadType.IMPLEMENTATION,
        payload=impl(), provenance=provenance,
    )


def trace_summary(invocation_id="inv-1", status="SUCCESS", exit_code=0, duration_ms=42):
    return TraceSummary(invocation_id=invocation_id, status=status,
                        exit_code=exit_code, duration_ms=duration_ms)


class BoobyTrapped:
    @property
    def correlation_id(self):
        raise AssertionError("attribute access leaked through append")


class ContractTests(unittest.TestCase):
    def test_direction_members_and_values(self):
        self.assertEqual(
            {member.name for member in CollaborationDirection},
            {"DECISION", "REQUEST", "REPLY", "FAILURE"},
        )
        for member in CollaborationDirection:
            self.assertEqual(member.value, member.name)

    def test_trace_summary_field_set_and_frozen(self):
        self.assertEqual(
            {field.name for field in fields(TraceSummary)},
            {"invocation_id", "status", "exit_code", "duration_ms"},
        )
        summary = trace_summary()
        with self.assertRaises(FrozenInstanceError):
            summary.invocation_id = "x"

    def test_trace_summary_rejects_secret_shaped_ids(self):
        for bad in ("token=1", "api_key=x", "bearer cred"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    trace_summary(invocation_id=bad)

    def test_record_field_set_is_exact(self):
        self.assertEqual(
            {field.name for field in fields(CollaborationRecord)},
            {"task_id", "correlation_id", "sequence", "direction", "role",
             "source_agent", "target_agent", "payload_type", "wire",
             "provenance", "status", "trace_summaries",
             "mode", "complexity", "path", "runtime_mode", "reason"},
        )

    def test_record_is_frozen(self):
        state = SharedCollaborationState().append_envelope("T1", envelope(), "REQUEST", "DELIVERED")
        record = state.history("T1")[0]
        with self.assertRaises(FrozenInstanceError):
            record.status = "MUTATED"

    def test_record_rejects_secret_shaped_strings(self):
        for field_name in ("task_id", "correlation_id", "role", "status", "reason"):
            values = dict(task_id="T1", correlation_id="C1",
                          sequence=1, direction=CollaborationDirection.REQUEST)
            values[field_name] = "token=leak"
            with self.subTest(field=field_name):
                with self.assertRaises(ValueError):
                    CollaborationRecord(**values)


class AppendTests(unittest.TestCase):
    def test_append_returns_new_state_and_leaves_old_untouched(self):
        first = SharedCollaborationState()
        second = first.append_envelope("T1", envelope(), "REQUEST", "DELIVERED")
        self.assertIsNot(second, first)
        self.assertEqual(first.history("T1"), ())
        self.assertEqual(len(second.history("T1")), 1)

    def test_per_task_dense_sequence_with_interleaved_tasks(self):
        state = SharedCollaborationState()
        state = state.append_envelope("T1", envelope("C1"), "REQUEST", "DELIVERED")
        state = state.append_envelope(
            "T2", envelope("C9", task_id="T2", payload=arch(task_id="T2")),
            "REQUEST", "DELIVERED")
        state = state.append_envelope("T1", reply_envelope("C1"), "REPLY", "DELIVERED")
        self.assertEqual([r.sequence for r in state.history("T1")], [1, 2])
        self.assertEqual([r.sequence for r in state.history("T2")], [1])

    def test_record_facts_exclude_sequence_and_provenance(self):
        for method, kwargs in (
            ("append_envelope", dict(task_id="T1", envelope=envelope(),
                                     direction="REQUEST", status="DELIVERED")),
            ("append_decision", dict(task_id="T1", mode="AUTO", complexity="COMPLEX",
                                     path="DUAL", runtime_mode="SINGLE_RUNTIME",
                                     reason="MODE_AUTO")),
            ("append_failure", dict(task_id="T1", status="TRANSPORT_FAILED")),
        ):
            signature = inspect.signature(getattr(SharedCollaborationState, method))
            for forbidden in ("sequence", "provenance"):
                with self.subTest(method=method, forbidden=forbidden):
                    self.assertNotIn(forbidden, signature.parameters)

    def test_non_envelope_inputs_are_rejected_without_attribute_access(self):
        for bad in (object(), {}, None, BoobyTrapped()):
            with self.subTest(bad=type(bad).__name__):
                with self.assertRaises(ValueError):
                    SharedCollaborationState().append_envelope("T1", bad, "REQUEST", "DELIVERED")

    def test_envelope_direction_must_be_request_or_reply(self):
        with self.assertRaises(ValueError):
            SharedCollaborationState().append_envelope("T1", envelope(), "DECISION", "DELIVERED")
        with self.assertRaises(ValueError):
            SharedCollaborationState().append_envelope("T1", envelope(), "FAILURE", "DELIVERED")


class EnvelopeDerivationTests(unittest.TestCase):
    def test_fields_derived_from_envelope(self):
        request = envelope(correlation_id="C7", provenance="REAL")
        state = SharedCollaborationState().append_envelope("T1", request, "REQUEST", "DELIVERED")
        record = state.history("T1")[0]
        self.assertEqual(record.correlation_id, "C7")
        self.assertEqual(record.role, "architect")
        self.assertEqual(record.source_agent, SOURCE_AGENT)
        self.assertEqual(record.target_agent, TARGET_AGENT)
        self.assertEqual(record.payload_type, "ARCHITECTURE")
        self.assertEqual(record.provenance, "REAL")
        self.assertEqual(record.wire, serialize_collaboration_packet(request))
        self.assertEqual(record.trace_summaries, ())

    def test_wire_integrity_against_later_payload_mutation(self):
        payload = arch(interfaces=({"name": "slug"},))
        request = envelope(payload=payload)
        state = SharedCollaborationState().append_envelope("T1", request, "REQUEST", "DELIVERED")
        wire_before = state.history("T1")[0].wire
        payload.interfaces[0]["name"] = "MUTATED-AFTER-APPEND"  # dict is mutable
        record = state.history("T1")[0]
        self.assertEqual(record.wire, wire_before)
        self.assertIn("slug", record.wire)
        self.assertNotIn("MUTATED-AFTER-APPEND", record.wire)

    def test_envelope_accessor_returns_fresh_copies(self):
        request = envelope()
        state = SharedCollaborationState().append_envelope("T1", request, "REQUEST", "DELIVERED")
        record = state.history("T1")[0]
        first = record.envelope()
        second = record.envelope()
        self.assertEqual(first, request)
        self.assertEqual(second, request)
        self.assertIsNot(first, second)

    def test_provenance_is_verbatim_offline_and_real(self):
        for provenance in ("OFFLINE", "REAL"):
            with self.subTest(provenance=provenance):
                state = SharedCollaborationState().append_envelope(
                    "T1", envelope(provenance=provenance), "REQUEST", "DELIVERED")
                self.assertEqual(state.history("T1")[0].provenance, provenance)


class DecisionAndFailureTests(unittest.TestCase):
    def test_decision_record_carries_routing_facts(self):
        state = SharedCollaborationState().append_decision(
            "T1", mode="AUTO", complexity="COMPLEX", path="DUAL",
            runtime_mode="SINGLE_RUNTIME", reason="MODE_AUTO")
        record = state.history("T1")[0]
        self.assertEqual(record.direction, CollaborationDirection.DECISION)
        self.assertEqual(record.mode, "AUTO")
        self.assertEqual(record.complexity, "COMPLEX")
        self.assertEqual(record.path, "DUAL")
        self.assertEqual(record.runtime_mode, "SINGLE_RUNTIME")
        self.assertEqual(record.reason, "MODE_AUTO")
        self.assertEqual(record.wire, "")
        self.assertEqual(record.provenance, "")
        self.assertEqual(state.failures("T1"), ())

    def test_failure_record_has_status_no_payload(self):
        state = SharedCollaborationState().append_failure("T1", status="TRANSPORT_FAILED")
        record = state.history("T1")[0]
        self.assertEqual(record.direction, CollaborationDirection.FAILURE)
        self.assertEqual(record.status, "TRANSPORT_FAILED")
        self.assertEqual(record.wire, "")
        self.assertEqual(record.provenance, "")
        self.assertEqual(len(state.failures("T1")), 1)

    def test_failure_with_envelope_derives_wire_and_parties(self):
        request = envelope()
        state = SharedCollaborationState().append_failure(
            "T1", status="CODER_INVOKE_FAILED", correlation_id="C001",
            envelope=request, trace_summaries=(trace_summary(),))
        record = state.history("T1")[0]
        self.assertEqual(record.wire, serialize_collaboration_packet(request))
        self.assertEqual(record.provenance, "OFFLINE")
        self.assertEqual(record.source_agent, SOURCE_AGENT)
        self.assertEqual(len(record.trace_summaries), 1)

    def test_failure_envelope_correlation_must_match(self):
        with self.assertRaises(ValueError):
            SharedCollaborationState().append_failure(
                "T1", status="X", correlation_id="C002", envelope=envelope("C001"))


class QueryTests(unittest.TestCase):
    def test_history_unknown_task_is_empty_tuple(self):
        self.assertEqual(SharedCollaborationState().history("nobody"), ())

    def test_history_returns_immutable_tuple(self):
        state = SharedCollaborationState().append_envelope("T1", envelope(), "REQUEST", "DELIVERED")
        self.assertIsInstance(state.history("T1"), tuple)

    def test_records_for_groups_by_correlation(self):
        state = SharedCollaborationState()
        state = state.append_decision("T1", mode="AUTO", complexity="COMPLEX",
                                      path="DUAL", runtime_mode="SINGLE_RUNTIME",
                                      reason="MODE_AUTO")
        state = state.append_envelope("T1", envelope("C1"), "REQUEST", "DELIVERED")
        state = state.append_envelope("T1", reply_envelope("C1"), "REPLY", "DELIVERED")
        state = state.append_envelope("T1", envelope("C2"), "REQUEST", "DELIVERED")
        grouped = state.records_for("C1")
        self.assertEqual(len(grouped), 2)
        self.assertEqual({r.direction for r in grouped},
                         {CollaborationDirection.REQUEST, CollaborationDirection.REPLY})
        self.assertEqual(state.records_for("unknown"), ())

    def test_failures_returns_only_failure_direction(self):
        state = SharedCollaborationState()
        state = state.append_envelope("T1", envelope("C1"), "REQUEST", "DELIVERED")
        state = state.append_failure("T1", status="TRANSPORT_FAILED", correlation_id="C1")
        self.assertEqual([r.status for r in state.failures("T1")], ["TRANSPORT_FAILED"])


class InvariantTests(unittest.TestCase):
    def test_correlation_cannot_bind_two_tasks(self):
        state = SharedCollaborationState().append_envelope(
            "T1", envelope("C1"), "REQUEST", "DELIVERED")
        with self.assertRaises(ValueError):
            state.append_envelope("T2", envelope("C1", task_id="T2"), "REQUEST", "DELIVERED")

    def test_request_and_reply_are_unique_per_correlation(self):
        state = SharedCollaborationState().append_envelope(
            "T1", envelope("C1"), "REQUEST", "DELIVERED")
        with self.assertRaises(ValueError):
            state.append_envelope("T1", envelope("C1"), "REQUEST", "DELIVERED")
        state = state.append_envelope("T1", reply_envelope("C1"), "REPLY", "DELIVERED")
        with self.assertRaises(ValueError):
            state.append_envelope("T1", reply_envelope("C1"), "REPLY", "DELIVERED")

    def test_reply_requires_prior_request(self):
        with self.assertRaises(ValueError):
            SharedCollaborationState().append_envelope(
                "T1", reply_envelope("C1"), "REPLY", "DELIVERED")

    def test_state_repr_stays_clean(self):
        state = SharedCollaborationState()
        state = state.append_decision("T1", mode="AUTO", complexity="COMPLEX",
                                      path="DUAL", runtime_mode="SINGLE_RUNTIME",
                                      reason="MODE_AUTO")
        state = state.append_envelope("T1", envelope("C1"), "REQUEST", "DELIVERED")
        state = state.append_envelope("T1", reply_envelope("C1"), "REPLY", "DELIVERED")
        surface = repr(state).lower()
        for marker in SECRET_MARKERS:
            self.assertNotIn(marker, surface)


class SourceScanTests(unittest.TestCase):
    def test_no_runtime_names_or_forbidden_channels(self):
        import collaboration_state as module
        source = Path(module.__file__).read_text(encoding="utf-8")
        lowered = source.lower()
        for name in ("claude", "codex", "deepseek", "openai", "anthropic",
                     "gemini", "tiny-agents", "tiny_agents"):
            self.assertNotIn(name, lowered)
        for forbidden in ("os.environ", "getenv", "RUN_REAL_PROVIDER_TESTS",
                          "subprocess", "requests", "urllib", "socket",
                          "http", "websocket", "a2a", "async", "threading",
                          "uuid", "random", "datetime", "import time",
                          "time.", "monotonic", "sleep", "clock"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
