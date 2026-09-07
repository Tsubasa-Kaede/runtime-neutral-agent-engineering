"""V3.1-E offline tests — the endpoint translation boundary, in-process.

Every architectural fact of the RemoteAgentEndpoint is proven here with
a recording fake adapter (no child process, no network, no clock): the
constructor discipline, the frozen validation gates and their order, the
exact Envelope->ExternalAgentRequest field mapping, the typed-status
error matrix, the seven-step parsing discipline (including the task-id
override authority and the no-raw-output error details), the reply
construction, the run_endpoint wire loop (response flushed before ACK),
the non-catch-all behavior, and the source-scan constraints that lock
the module's import surface and forbidden vocabulary.
"""
import ast
import contextlib
import io
import json
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from collaboration_packet import (
    CollaborationPacket,
    CollaborationPayloadType,
    serialize_collaboration_packet,
)
from external_runtime import (
    ExternalAgentRequest,
    InvocationResult,
    InvocationStatus,
    InvocationTrace,
)
from remote_contract import (
    RemoteEnvelope,
    RemoteEnvelopeError,
    RemoteEnvelopeErrorCode,
    RemotePayloadType,
    new_message_id,
    serialize_remote_envelope,
)
from structured_packets import ArchitecturePacket, ImplementationPacket

from remote_agent_endpoint import (
    IMPLEMENTATION_INSTRUCTION,
    RemoteAgentEndpoint,
    run_endpoint,
)

LOCAL = "agent:arch-e2e:architect"
ENDPOINT = "agent:coder-e2e:coder"
CORRELATION = "collab-endpoint-test-0001"


def arch_packet(task_id="T1"):
    return ArchitecturePacket(
        task_id=task_id, role="architect", goal=("g",), constraints=("c",),
        architecture=("a",), interfaces=({},), implementation_steps=({},),
        acceptance_criteria=("ac",), risks=({},))


def inbound_packet(task_id="T1", target_role="coder"):
    return CollaborationPacket(
        correlation_id=CORRELATION, task_id=task_id,
        source_agent=LOCAL, target_agent=ENDPOINT,
        source_role="architect", target_role=target_role,
        payload_type=CollaborationPayloadType.ARCHITECTURE,
        payload=arch_packet(task_id))


def inbound_envelope(packet=None, recipient=None, role=None):
    packet = packet if packet is not None else inbound_packet()
    return RemoteEnvelope(
        message_id=new_message_id(),
        correlation_id=packet.correlation_id,
        sender=LOCAL,
        recipient=recipient if recipient is not None else ENDPOINT,
        role=role if role is not None else packet.target_role,
        payload=packet,
        payload_type=RemotePayloadType.COLLABORATION_PACKET)


def impl_json(task_id="T1", **overrides):
    data = {
        "task_id": task_id, "role": "coder",
        "changed_files": ["a.py"],
        "implementation_summary": "implemented the packet",
        "implementation_details": ["step one"],
        "assumptions": [],
        "unresolved_items": [],
        "test_requirements": ["t1"],
    }
    data.update(overrides)
    return json.dumps(data)


def trace_for(status, error=None, task_id="T1"):
    return InvocationTrace(
        "invocation-fake", task_id, ENDPOINT, "fake", None, None, "coder",
        status, error=error)


def success_result(output, task_id="T1"):
    return InvocationResult(
        InvocationStatus.SUCCESS, output=output,
        trace=trace_for(InvocationStatus.SUCCESS, task_id=task_id))


class FakeAdapter:
    """Records every request; returns the configured result or raises."""

    def __init__(self, result=None, error=None):
        self.requests = []
        self._result = result
        self._error = error

    def invoke(self, request):
        self.requests.append(request)
        if self._error is not None:
            raise self._error
        return self._result


def make_endpoint(adapter=None, **kwargs):
    if adapter is None:
        adapter = FakeAdapter(success_result(impl_json()))
    return RemoteAgentEndpoint(adapter, ENDPOINT, **kwargs)


class call_guard:
    """Captures the RemoteEnvelopeError category raised inside the block."""

    def __init__(self):
        self.category = None
        self.exception = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is RemoteEnvelopeError:
            self.exception = exc
            self.category = exc.category
            return True
        return False


class ConstructorTests(unittest.TestCase):
    def test_default_timeout_reaches_request(self):
        adapter = FakeAdapter(success_result(impl_json()))
        endpoint = RemoteAgentEndpoint(adapter, ENDPOINT)
        endpoint.handle(inbound_envelope())
        self.assertEqual(adapter.requests[0].timeout_seconds, 120.0)

    def test_custom_timeout_reaches_request(self):
        adapter = FakeAdapter(success_result(impl_json()))
        endpoint = RemoteAgentEndpoint(adapter, ENDPOINT, timeout_seconds=7)
        endpoint.handle(inbound_envelope())
        self.assertEqual(adapter.requests[0].timeout_seconds, 7)

    def test_invalid_timeout_refused(self):
        adapter = FakeAdapter()
        for bad in (0, -1, "soon"):
            with self.assertRaises(ValueError):
                RemoteAgentEndpoint(adapter, ENDPOINT, timeout_seconds=bad)

    def test_empty_address_refused(self):
        with call_guard() as caught:
            RemoteAgentEndpoint(FakeAdapter(), "")
        self.assertIs(caught.category, RemoteEnvelopeErrorCode.INVALID_ENVELOPE)

    def test_non_string_address_refused(self):
        with call_guard() as caught:
            RemoteAgentEndpoint(FakeAdapter(), 123)
        self.assertIs(caught.category, RemoteEnvelopeErrorCode.INVALID_ENVELOPE)

    def test_address_opaque(self):
        # Any non-empty string is a legal address; nothing is parsed.
        for weird in ("zzz", "agent:x:coder", "not an address at all"):
            endpoint = RemoteAgentEndpoint(FakeAdapter(success_result(
                impl_json())), weird)
            self.assertEqual(endpoint.address, weird)

    def test_invalid_provenance_refused(self):
        with self.assertRaises(ValueError):
            RemoteAgentEndpoint(FakeAdapter(), ENDPOINT, provenance="MAYBE")


class ValidationOrderTests(unittest.TestCase):
    def test_non_envelope_rejected(self):
        adapter = FakeAdapter()
        endpoint = make_endpoint(adapter)
        for not_an_envelope in (None, {}, inbound_packet()):
            with call_guard() as caught:
                endpoint.handle(not_an_envelope)
            self.assertIs(caught.category,
                          RemoteEnvelopeErrorCode.INVALID_ENVELOPE)
        self.assertEqual(adapter.requests, [])   # refused before invoking

    def test_wrong_recipient_rejected(self):
        adapter = FakeAdapter()
        endpoint = make_endpoint(adapter)
        with call_guard() as caught:
            endpoint.handle(inbound_envelope(recipient="agent:other:coder"))
        self.assertIs(caught.category, RemoteEnvelopeErrorCode.INVALID_ENVELOPE)
        self.assertEqual(adapter.requests, [])

    def test_unsupported_role_unavailable(self):
        adapter = FakeAdapter()
        endpoint = make_endpoint(adapter)
        envelope = inbound_envelope(
            packet=inbound_packet(target_role="tester"), role="tester")
        with call_guard() as caught:
            endpoint.handle(envelope)
        self.assertIs(caught.category,
                      RemoteEnvelopeErrorCode.ROLE_UNAVAILABLE)
        self.assertEqual(adapter.requests, [])

    def test_recipient_gate_precedes_role_gate(self):
        envelope = inbound_envelope(
            packet=inbound_packet(target_role="tester"), role="tester",
            recipient="agent:other:coder")
        with call_guard() as caught:
            make_endpoint().handle(envelope)
        self.assertIs(caught.category,
                      RemoteEnvelopeErrorCode.INVALID_ENVELOPE)


class RequestMappingTests(unittest.TestCase):
    def test_all_fields_exact(self):
        packet = inbound_packet()
        envelope = inbound_envelope(packet=packet)
        adapter = FakeAdapter(success_result(impl_json()))
        RemoteAgentEndpoint(adapter, ENDPOINT, timeout_seconds=33).handle(
            envelope)
        request = adapter.requests[0]
        self.assertIsInstance(request, ExternalAgentRequest)
        self.assertEqual(request.task_id, "T1")
        self.assertEqual(
            request.prompt,
            IMPLEMENTATION_INSTRUCTION
            + serialize_collaboration_packet(packet))
        self.assertEqual(request.agent_id, ENDPOINT)
        self.assertEqual(request.role, "coder")
        self.assertIsNone(request.provider)
        self.assertIsNone(request.model)
        self.assertEqual(request.timeout_seconds, 33)
        self.assertEqual(request.handoff_packets, ())


class InvocationErrorMappingTests(unittest.TestCase):
    def test_typed_terminal_statuses_map_to_remote_execution(self):
        for status in (InvocationStatus.FAILED, InvocationStatus.TIMEOUT,
                       InvocationStatus.CANCELLED,
                       InvocationStatus.UNAVAILABLE):
            adapter = FakeAdapter(InvocationResult(
                status, error=None, trace=trace_for(status, "cli exploded")))
            with call_guard() as caught:
                make_endpoint(adapter).handle(inbound_envelope())
            self.assertIs(caught.category,
                          RemoteEnvelopeErrorCode.REMOTE_EXECUTION_FAILED)
            self.assertEqual(caught.exception.detail, "cli exploded")

    def test_failure_without_trace_uses_closed_detail(self):
        adapter = FakeAdapter(InvocationResult(InvocationStatus.FAILED))
        with call_guard() as caught:
            make_endpoint(adapter).handle(inbound_envelope())
        self.assertIs(caught.category,
                      RemoteEnvelopeErrorCode.REMOTE_EXECUTION_FAILED)
        self.assertEqual(caught.exception.detail,
                         "invocation failed without trace")


class ParsingDisciplineTests(unittest.TestCase):
    def test_fenced_json_accepted(self):
        output = "```json\n" + impl_json() + "\n```"
        reply = make_endpoint(FakeAdapter(success_result(output))).handle(
            inbound_envelope())
        self.assertIsInstance(reply.payload.payload, ImplementationPacket)

    def test_plain_json_accepted(self):
        reply = make_endpoint().handle(inbound_envelope())
        self.assertIsInstance(reply.payload.payload, ImplementationPacket)

    def test_malformed_json_rejected(self):
        adapter = FakeAdapter(success_result("this is not json"))
        with call_guard() as caught:
            make_endpoint(adapter).handle(inbound_envelope())
        self.assertIs(caught.category,
                      RemoteEnvelopeErrorCode.REMOTE_EXECUTION_FAILED)

    def test_non_dict_json_rejected(self):
        adapter = FakeAdapter(success_result("[1, 2, 3]"))
        with call_guard() as caught:
            make_endpoint(adapter).handle(inbound_envelope())
        self.assertIs(caught.category,
                      RemoteEnvelopeErrorCode.REMOTE_EXECUTION_FAILED)

    def test_non_string_output_rejected(self):
        adapter = FakeAdapter(success_result(None))
        with call_guard() as caught:
            make_endpoint(adapter).handle(inbound_envelope())
        self.assertIs(caught.category,
                      RemoteEnvelopeErrorCode.REMOTE_EXECUTION_FAILED)

    def test_task_id_overwritten_from_inbound(self):
        # The model echoes the WRONG task id; authority stays with the
        # inbound packet.
        adapter = FakeAdapter(success_result(impl_json(task_id="EVIL")))
        reply = make_endpoint(adapter).handle(inbound_envelope())
        self.assertEqual(reply.payload.task_id, "T1")
        self.assertEqual(reply.payload.payload.task_id, "T1")

    def test_list_normalization(self):
        # A bare string in a list field is normalized to one element.
        adapter = FakeAdapter(success_result(impl_json(changed_files="a.py")))
        reply = make_endpoint(adapter).handle(inbound_envelope())
        self.assertEqual(reply.payload.payload.changed_files, ("a.py",))

    def test_wrong_packet_role_rejected(self):
        adapter = FakeAdapter(success_result(impl_json(role="architect")))
        with call_guard() as caught:
            make_endpoint(adapter).handle(inbound_envelope())
        self.assertIs(caught.category,
                      RemoteEnvelopeErrorCode.REMOTE_EXECUTION_FAILED)

    def test_missing_field_rejected(self):
        output = json.dumps({"task_id": "T1", "role": "coder"})
        adapter = FakeAdapter(success_result(output))
        with call_guard() as caught:
            make_endpoint(adapter).handle(inbound_envelope())
        self.assertIs(caught.category,
                      RemoteEnvelopeErrorCode.REMOTE_EXECUTION_FAILED)

    def test_unsafe_content_rejected(self):
        adapter = FakeAdapter(success_result(impl_json(
            implementation_summary="api_key=sk-1234567890abcdef")))
        with call_guard() as caught:
            make_endpoint(adapter).handle(inbound_envelope())
        self.assertIs(caught.category,
                      RemoteEnvelopeErrorCode.REMOTE_EXECUTION_FAILED)

    def test_raw_output_never_in_error_detail(self):
        sentinel = "SENTINELXYZ"
        adapter = FakeAdapter(success_result("garbage " + sentinel))
        with call_guard() as caught:
            make_endpoint(adapter).handle(inbound_envelope())
        self.assertIs(caught.category,
                      RemoteEnvelopeErrorCode.REMOTE_EXECUTION_FAILED)
        self.assertNotIn(sentinel, str(caught.exception))
        self.assertNotIn(sentinel, caught.exception.detail)


class ReplyConstructionTests(unittest.TestCase):
    def reply_for(self, **endpoint_kwargs):
        envelope = inbound_envelope()
        reply = make_endpoint(**endpoint_kwargs).handle(envelope)
        return envelope, reply

    def test_fresh_message_id(self):
        envelope = inbound_envelope()
        first = make_endpoint().handle(envelope)
        second = make_endpoint().handle(inbound_envelope())
        self.assertNotEqual(first.message_id, envelope.message_id)
        self.assertNotEqual(first.message_id, second.message_id)

    def test_correlation_fidelity(self):
        _, reply = self.reply_for()
        self.assertEqual(reply.correlation_id, CORRELATION)

    def test_sender_recipient_reversal(self):
        _, reply = self.reply_for()
        self.assertEqual(reply.sender, ENDPOINT)
        self.assertEqual(reply.recipient, LOCAL)

    def test_role_from_requester_source_role(self):
        _, reply = self.reply_for()
        self.assertEqual(reply.role, "architect")

    def test_payload_types(self):
        _, reply = self.reply_for()
        self.assertIs(reply.payload_type, RemotePayloadType.COLLABORATION_PACKET)
        self.assertIs(reply.payload.payload_type,
                      CollaborationPayloadType.IMPLEMENTATION)

    def test_reply_packet_fields(self):
        _, reply = self.reply_for()
        packet = reply.payload
        self.assertIsInstance(packet, CollaborationPacket)
        self.assertEqual(packet.task_id, "T1")
        self.assertEqual(packet.source_agent, ENDPOINT)
        self.assertEqual(packet.target_agent, LOCAL)
        self.assertEqual(packet.source_role, "coder")
        self.assertEqual(packet.target_role, "architect")
        self.assertIsInstance(packet.payload, ImplementationPacket)
        self.assertEqual(packet.payload.implementation_summary,
                         "implemented the packet")
        self.assertEqual(packet.acceptance_criteria, ("t1",))
        self.assertEqual(packet.acceptance_criteria,
                         packet.payload.test_requirements)

    def test_provenance_passthrough_default_offline(self):
        _, reply = self.reply_for()
        self.assertEqual(reply.payload.provenance, "OFFLINE")

    def test_provenance_passthrough_declared_real(self):
        _, reply = self.reply_for(provenance="REAL")
        self.assertEqual(reply.payload.provenance, "REAL")

    def test_inbound_envelope_unchanged(self):
        envelope = inbound_envelope()
        before = serialize_remote_envelope(envelope)
        make_endpoint().handle(envelope)
        self.assertEqual(serialize_remote_envelope(envelope), before)


class NonCatchAllTests(unittest.TestCase):
    def test_unexpected_adapter_error_propagates(self):
        adapter = FakeAdapter(error=RuntimeError("kaboom"))
        with self.assertRaises(RuntimeError):
            make_endpoint(adapter).handle(inbound_envelope())


class RecordingStream:
    """Binary output stream that records write/flush order."""

    def __init__(self):
        self.events = []
        self.buffer = io.BytesIO()

    def write(self, data):
        self.events.append(("write", bytes(data)))
        self.buffer.write(data)
        return len(data)

    def flush(self):
        self.events.append(("flush",))


class RunEndpointLoopTests(unittest.TestCase):
    def wire(self, adapter, lines):
        input_stream = io.BytesIO(b"".join(lines))
        output = RecordingStream()
        return input_stream, output

    def line(self, envelope):
        return serialize_remote_envelope(envelope).encode("utf-8") + b"\n"

    def test_happy_round_trip(self):
        envelope = inbound_envelope()
        adapter = FakeAdapter(success_result(impl_json()))
        input_stream, output = self.wire(adapter, [self.line(envelope)])
        run_endpoint(adapter, ENDPOINT, input_stream, output)
        lines = output.buffer.getvalue().splitlines()
        self.assertEqual(len(lines), 2)
        reply = deserialize_line(lines[0])
        self.assertEqual(reply.recipient, LOCAL)
        self.assertEqual(reply.correlation_id, CORRELATION)
        ack = json.loads(lines[1].decode("utf-8"))
        self.assertEqual(ack, {"delivered": envelope.message_id})

    def test_response_flushed_before_ack(self):
        envelope = inbound_envelope()
        adapter = FakeAdapter(success_result(impl_json()))
        input_stream, output = self.wire(adapter, [self.line(envelope)])
        run_endpoint(adapter, ENDPOINT, input_stream, output)
        kinds = [event[0] for event in output.events]
        self.assertEqual(kinds, ["write", "flush", "write", "flush"])
        first_write = output.events[0][1]
        second_write = output.events[2][1]
        self.assertIn(b'"delivered"', second_write)
        self.assertNotIn(b'"delivered"', first_write)

    def test_ack_is_canonical_json(self):
        envelope = inbound_envelope()
        adapter = FakeAdapter(success_result(impl_json()))
        input_stream, output = self.wire(adapter, [self.line(envelope)])
        run_endpoint(adapter, ENDPOINT, input_stream, output)
        expected = json.dumps(
            {"delivered": envelope.message_id},
            sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
        lines = output.buffer.getvalue().splitlines(keepends=True)
        self.assertEqual(lines[1], expected)

    def test_typed_error_no_ack_exit_nonzero(self):
        envelope = inbound_envelope(
            packet=inbound_packet(target_role="tester"), role="tester")
        adapter = FakeAdapter(success_result(impl_json()))
        input_stream, output = self.wire(adapter, [self.line(envelope)])
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as caught:
                run_endpoint(adapter, ENDPOINT, input_stream, output)
        self.assertEqual(caught.exception.code, 1)
        self.assertEqual(output.events, [])          # nothing written, no ACK
        self.assertIn("ROLE_UNAVAILABLE", stderr.getvalue())

    def test_undecodable_line_is_typed_exit(self):
        adapter = FakeAdapter(success_result(impl_json()))
        input_stream, output = self.wire(adapter, [b"not json at all\n"])
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                run_endpoint(adapter, ENDPOINT, input_stream, output)
        self.assertEqual(output.events, [])

    def test_blank_lines_skipped(self):
        envelope = inbound_envelope()
        adapter = FakeAdapter(success_result(impl_json()))
        input_stream, output = self.wire(
            adapter, [b"\n", b"   \n", self.line(envelope)])
        run_endpoint(adapter, ENDPOINT, input_stream, output)
        self.assertEqual(len(output.buffer.getvalue().splitlines()), 2)

    def test_eof_returns_normally(self):
        adapter = FakeAdapter(success_result(impl_json()))
        input_stream, output = self.wire(adapter, [])
        run_endpoint(adapter, ENDPOINT, input_stream, output)
        self.assertEqual(output.events, [])


def deserialize_line(raw):
    from remote_contract import deserialize_remote_envelope
    return deserialize_remote_envelope(raw.decode("utf-8"))


SOURCE = Path(SCRIPTS / "remote_agent_endpoint.py").read_text(
    encoding="utf-8")

FORBIDDEN_VOCABULARY = (
    "discovery", "admission", "qualification", "selection",
    "orchestration", "schedul", "retry", "reconnect", "supervisor",
    "registry", "subprocess", "popen", "threading", "asyncio", "socket",
    "os.environ", "runtime_id", "provider_id", "model_id",
    "config_fingerprint", "datetime", "random", "delivery_", "unknown_agent",
    "except exception",
)

ALLOWED_PROJECT_MODULES = {
    "collaboration_packet", "content_safety", "external_runtime",
    "remote_contract", "structured_packets",
}


class SourceScanTests(unittest.TestCase):
    def test_import_surface_exact(self):
        tree = ast.parse(SOURCE)
        modules = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.module is not None and node.level == 0:
                    modules.add(node.module.split(".")[0])
        self.assertEqual(
            modules,
            ALLOWED_PROJECT_MODULES | {"json", "sys", "__future__"})

    def test_no_forbidden_vocabulary(self):
        lowered = SOURCE.lower()
        for term in FORBIDDEN_VOCABULARY:
            self.assertNotIn(term, lowered, f"forbidden term: {term}")

    def test_no_broad_except(self):
        tree = ast.parse(SOURCE)
        handlers = [node for node in ast.walk(tree)
                    if isinstance(node, ast.ExceptHandler)]
        self.assertTrue(handlers)          # the typed paths do exist
        for node in handlers:
            self.assertIsNotNone(node.type, "bare except is forbidden")
            dumped = ast.dump(node.type)
            self.assertNotIn("Exception", dumped.replace(
                "RemoteEnvelopeError", ""))

    def test_no_delivery_or_unknown_agent_minting(self):
        self.assertNotIn("DELIVERY_", SOURCE)
        self.assertNotIn("UNKNOWN_AGENT", SOURCE)

    def test_public_surface_frozen(self):
        tree = ast.parse(SOURCE)
        classes = {node.name: node for node in tree.body
                   if isinstance(node, ast.ClassDef)}
        self.assertIn("RemoteAgentEndpoint", classes)
        public = sorted(
            node.name for node in classes["RemoteAgentEndpoint"].body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and not node.name.startswith("_"))
        self.assertEqual(public, ["address", "handle"])
        functions = sorted(
            node.name for node in tree.body
            if isinstance(node, ast.FunctionDef) and not node.name.startswith("_"))
        self.assertEqual(functions, ["run_endpoint"])


if __name__ == "__main__":
    unittest.main()
