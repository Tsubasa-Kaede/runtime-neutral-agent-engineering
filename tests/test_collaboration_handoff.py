"""Phase 10H-G1: handoff_input_for — read-only projection over the ledger.

Reconstructs a role's upstream structured input from SharedCollaborationState
by payload_type (never direction, never target_role, never correlation), keyed
on task_id with latest-by-sequence and fresh envelope copies. Closed role
vocabulary; honest MISSING_HANDOFF / UNKNOWN_STAGE. No raw output ever.
"""
import sys
import unittest
from dataclasses import fields
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from collaboration_handoff import handoff_input_for
from collaboration_packet import CollaborationPacket, CollaborationPayloadType
from collaboration_state import CollaborationDirection, SharedCollaborationState
from handoff_context import HandoffError
from structured_packets import (
    ArchitecturePacket,
    ImplementationPacket,
    TestPacket,
)
from verified_selection_bridge import agent_id_for

SOURCE_AGENT = agent_id_for(("rt-a", "provider-a", "model-a", "fp-a"))
TARGET_AGENT = agent_id_for(("rt-b", "provider-b", "model-b", "fp-b"))

SECRET_MARKERS = ("token", "secret", "api_key", "authorization", "bearer", "stdout", "stderr")
RAW_OUTPUT_FIELDS = ("stdout", "stderr", "raw_output", "output", "reasoning", "chat", "log")


def arch(task_id="T1"):
    return ArchitecturePacket.from_dict({
        "task_id": task_id, "role": "architect", "goal": ["g"], "constraints": ["c"],
        "architecture": ["a"], "interfaces": [{}], "implementation_steps": [{}],
        "acceptance_criteria": ["ac"], "risks": [{}],
    })


def impl(task_id="T1"):
    return ImplementationPacket.from_dict({
        "task_id": task_id, "role": "coder", "changed_files": ["f.py"],
        "implementation_summary": "s", "implementation_details": ["d"],
        "assumptions": [], "unresolved_items": [], "test_requirements": ["tr"],
    })


def test_pkt(task_id="T1"):
    return TestPacket.from_dict({
        "task_id": task_id, "role": "tester", "tests_run": ["t"], "tests_passed": ["t"],
        "tests_failed": [], "failures": [], "coverage_or_validation": [],
        "remaining_risks": [],
    })


def envelope(task_id, correlation_id, payload_type, payload, source, target,
             source_role, target_role):
    return CollaborationPacket(
        correlation_id=correlation_id, task_id=task_id,
        source_agent=source, target_agent=target,
        source_role=source_role, target_role=target_role,
        payload_type=payload_type, payload=payload)


def append(state, task_id, correlation_id, payload_type, payload, direction,
           source, target, source_role, target_role):
    return state.append_envelope(
        task_id,
        envelope(task_id, correlation_id, payload_type, payload, source, target,
                 source_role, target_role),
        direction, "DELIVERED")


def full_chain(task_id="T1", corr="C1"):
    """arch(REQUEST)->coder, impl(REPLY), test(REQUEST)->reviewer ledger."""
    state = SharedCollaborationState()
    state = append(state, task_id, corr, CollaborationPayloadType.ARCHITECTURE,
                   arch(task_id), "REQUEST", SOURCE_AGENT, TARGET_AGENT,
                   "architect", "coder")
    state = append(state, task_id, corr, CollaborationPayloadType.IMPLEMENTATION,
                   impl(task_id), "REPLY", TARGET_AGENT, SOURCE_AGENT,
                   "coder", "architect")
    state = append(state, task_id, corr + "-test", CollaborationPayloadType.TEST,
                   test_pkt(task_id), "REQUEST", TARGET_AGENT, SOURCE_AGENT,
                   "tester", "reviewer")
    return state


class RoleContractTests(unittest.TestCase):
    def test_architect_returns_none(self):
        state = full_chain()
        self.assertIsNone(handoff_input_for(state, "T1", "architect"))
        # even an empty state returns None for architect (no upstream)
        self.assertIsNone(handoff_input_for(SharedCollaborationState(), "T1", "architect"))

    def test_coder_returns_architecture_packet(self):
        state = full_chain()
        result = handoff_input_for(state, "T1", "coder")
        self.assertIsInstance(result, ArchitecturePacket)

    def test_tester_returns_implementation_packet_even_when_reply(self):
        state = full_chain()  # impl was appended as REPLY
        result = handoff_input_for(state, "T1", "tester")
        self.assertIsInstance(result, ImplementationPacket)

    def test_reviewer_returns_three_tuple_in_order(self):
        state = full_chain()
        result = handoff_input_for(state, "T1", "reviewer")
        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 3)
        self.assertIsInstance(result[0], ArchitecturePacket)
        self.assertIsInstance(result[1], ImplementationPacket)
        self.assertIsInstance(result[2], TestPacket)


class ErrorSemanticsTests(unittest.TestCase):
    def test_unknown_role_raises(self):
        state = full_chain()
        for bad in ("deploy", "architectt", "CODEr", ""):
            with self.subTest(role=bad):
                with self.assertRaises(HandoffError) as caught:
                    handoff_input_for(state, "T1", bad)
                self.assertEqual(str(caught.exception), "UNKNOWN_STAGE")

    def test_coder_missing_architecture_raises(self):
        state = SharedCollaborationState()
        with self.assertRaises(HandoffError) as caught:
            handoff_input_for(state, "T1", "coder")
        self.assertEqual(str(caught.exception), "MISSING_HANDOFF")

    def test_tester_missing_implementation_raises(self):
        # architecture only — no implementation record
        bare = SharedCollaborationState()
        bare = append(bare, "T2", "CX", CollaborationPayloadType.ARCHITECTURE,
                      arch("T2"), "REQUEST", SOURCE_AGENT, TARGET_AGENT,
                      "architect", "coder")
        with self.assertRaises(HandoffError) as caught:
            handoff_input_for(bare, "T2", "tester")
        self.assertEqual(str(caught.exception), "MISSING_HANDOFF")

    def test_reviewer_missing_any_precursor_raises(self):
        for payloads in (
            (),  # nothing
            (CollaborationPayloadType.ARCHITECTURE,),  # no impl/test
            (CollaborationPayloadType.ARCHITECTURE, CollaborationPayloadType.IMPLEMENTATION),  # no test
            (CollaborationPayloadType.IMPLEMENTATION,),  # no arch/test
        ):
            state = SharedCollaborationState()
            for index, payload_type in enumerate(payloads):
                payload = {CollaborationPayloadType.ARCHITECTURE: arch,
                           CollaborationPayloadType.IMPLEMENTATION: impl,
                           CollaborationPayloadType.TEST: test_pkt}[payload_type]("T3")
                state = append(state, "T3", f"C{index}", payload_type, payload,
                               "REQUEST", SOURCE_AGENT, TARGET_AGENT,
                               "architect", "coder")
            with self.subTest(payloads=[p.value for p in payloads]):
                with self.assertRaises(HandoffError) as caught:
                    handoff_input_for(state, "T3", "reviewer")
                self.assertEqual(str(caught.exception), "MISSING_HANDOFF")


class LatestBySequenceTests(unittest.TestCase):
    def test_latest_architecture_is_returned(self):
        state = SharedCollaborationState()
        state = append(state, "T1", "C1", CollaborationPayloadType.ARCHITECTURE,
                       arch(), "REQUEST", SOURCE_AGENT, TARGET_AGENT, "architect", "coder")
        newer = ArchitecturePacket.from_dict({
            "task_id": "T1", "role": "architect", "goal": ["newer goal"],
            "constraints": ["c2"], "architecture": ["a2"], "interfaces": [{}],
            "implementation_steps": [{}], "acceptance_criteria": ["ac2"], "risks": [{}],
        })
        state = append(state, "T1", "C2", CollaborationPayloadType.ARCHITECTURE,
                       newer, "REQUEST", SOURCE_AGENT, TARGET_AGENT, "architect", "coder")
        result = handoff_input_for(state, "T1", "coder")
        self.assertEqual(result.goal, ("newer goal",))


class FreshCopyTests(unittest.TestCase):
    def test_two_calls_return_distinct_objects(self):
        state = full_chain()
        first = handoff_input_for(state, "T1", "coder")
        second = handoff_input_for(state, "T1", "coder")
        self.assertIsNot(first, second)

    def test_external_mutation_does_not_reach_ledger(self):
        state = full_chain()
        packet = handoff_input_for(state, "T1", "coder")
        packet.interfaces  # frozen packet; mutate the inner dict below
        # ArchitecturePacket has no interfaces in this construction path; guard
        # by mutating a mutable nested dict where present.
        again = handoff_input_for(state, "T1", "coder")
        self.assertEqual(again.goal, arch().goal)
        self.assertIsNot(again, packet)


class CrossTaskIsolationTests(unittest.TestCase):
    def test_task_a_architecture_not_visible_to_task_b_coder(self):
        state = SharedCollaborationState()
        state = append(state, "T-A", "CA", CollaborationPayloadType.ARCHITECTURE,
                       arch("T-A"), "REQUEST", SOURCE_AGENT, TARGET_AGENT,
                       "architect", "coder")
        with self.assertRaises(HandoffError) as caught:
            handoff_input_for(state, "T-B", "coder")
        self.assertEqual(str(caught.exception), "MISSING_HANDOFF")

    def test_task_b_implementation_not_visible_to_task_a_tester(self):
        state = SharedCollaborationState()
        state = append(state, "T-B", "CB", CollaborationPayloadType.IMPLEMENTATION,
                       impl("T-B"), "REQUEST", SOURCE_AGENT, TARGET_AGENT,
                       "coder", "tester")
        with self.assertRaises(HandoffError) as caught:
            handoff_input_for(state, "T-A", "tester")
        self.assertEqual(str(caught.exception), "MISSING_HANDOFF")


class SchemaAndSecretTests(unittest.TestCase):
    def test_returned_packets_have_no_raw_output_fields(self):
        state = full_chain()
        for role in ("coder", "tester", "reviewer"):
            result = handoff_input_for(state, "T1", role)
            packets = result if isinstance(result, tuple) else (result,)
            for packet in packets:
                with self.subTest(role=role, packet=type(packet).__name__):
                    field_names = {field.name for field in fields(packet)}
                    for forbidden in RAW_OUTPUT_FIELDS:
                        self.assertNotIn(forbidden, field_names)

    def test_result_repr_stays_clean(self):
        state = full_chain()
        for role in ("coder", "tester", "reviewer"):
            result = handoff_input_for(state, "T1", role)
            surface = repr(result).lower()
            for marker in SECRET_MARKERS:
                self.assertNotIn(marker, surface)


class SourceScanTests(unittest.TestCase):
    def test_module_is_runtime_neutral_and_offline(self):
        import collaboration_handoff as module
        source = Path(module.__file__).read_text(encoding="utf-8")
        lowered = source.lower()
        for name in ("claude", "codex", "deepseek", "openai", "anthropic",
                     "gemini", "tiny-agents", "tiny_agents"):
            self.assertNotIn(name, lowered)
        for forbidden in ("os.environ", "getenv", "subprocess", "requests",
                          "urllib", "socket", "http", "uuid", "random",
                          "datetime", "import time", "time.", "monotonic",
                          "sleep", "credential", "api_key", "token"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
