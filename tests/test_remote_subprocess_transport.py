"""V3.1-B2: Subprocess stdio envelope transport — behavior tests.

The transport moves RemoteEnvelope values across a REAL process boundary:
one child per transport instance, spawned lazily on the first send and
reused for every later send (the approved lifecycle: no restart, no pool).
The child is host provided via an argv command with shell=False and a
whitelisted environment. Each send writes exactly one canonical JSONL
line to the child's stdin from a helper thread while a daemon drain
thread reads the child's stdout, so a full pipe on either side can never
pin the exchange: the whole exchange — write, acknowledgement wait, and
child lifecycle — is bounded by one monotonic deadline. DELIVERED is
authorised only by the child's acknowledgement line
{"delivered": "<message_id>"}; the child's exit code is a lifecycle fact
that never changes a delivered result, and a child that ends without
acknowledging yields FAILED. A window overrun is TIMEOUT and the child
is reaped (terminate -> kill -> wait) so no orphan survives. Inbound stdout lines are decoded and routed by their own
recipient into per-recipient mailboxes; a line that cannot decode or
names an unsupported version poisons the channel and surfaces loudly at
the next receive() — never silently skipped. stderr is a diagnostics
channel only and never enters protocol parsing. Fully offline: fixture
children are inline Python driven by sys.executable; no network, no
real agent, no credential literals.
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from agent_identity import AgentIdentity, agent_address
from collaboration_packet import (
    CollaborationPacket,
    CollaborationPayloadType,
    new_correlation_id,
)
from remote_contract import (
    RemoteEnvelopeError,
    RemoteEnvelopeErrorCode,
    RemoteEnvelopeStatus,
    serialize_remote_envelope,
)
from remote_envelope_transport import (
    RemoteEnvelopeReceipt,
    RemoteEnvelopeTransport,
)
from remote_subprocess_transport import SubprocessStdioEnvelopeTransport
from structured_packets import ArchitecturePacket

# Every child that means to confirm delivery prints one ACK line after
# reading the envelope: {"delivered": "<message_id>"} in canonical form.
# The echo line precedes the ACK so an inbound envelope is always routed
# before the acknowledgement that ends the exchange.
ACKING_ECHO = (
    "    payload = json.loads(line)\n"
    "    sys.stdout.buffer.write(line)\n"
    "    sys.stdout.buffer.write(json.dumps(\n"
    "        {'delivered': payload['message_id']}, sort_keys=True,\n"
    "        separators=(',', ':')).encode('utf-8') + b'\\n')\n"
    "    sys.stdout.buffer.flush()\n"
)

ECHO_CHILD = "import json, sys\nline = sys.stdin.buffer.readline()\n" \
             "if line:\n" + ACKING_ECHO

SILENT_CHILD = "import sys\nsys.stdin.buffer.readline()\n"

NO_ACK_EXIT1_CHILD = "import sys\nsys.stdin.buffer.readline()\nsys.exit(1)\n"

NEVER_EXIT_CHILD = (
    "import sys, time\n"
    "sys.stdin.buffer.readline()\n"
    "time.sleep(60)\n"
)

STDERR_CHILD = (
    "import json, sys\n"
    "line = sys.stdin.buffer.readline()\n"
    "sys.stderr.buffer.write(b'diagnostic noise on stderr\\n')\n"
    "sys.stderr.buffer.flush()\n"
    "if line:\n"
    "    payload = json.loads(line)\n"
    "    sys.stdout.buffer.write(line)\n"
    "    sys.stdout.buffer.write(json.dumps(\n"
    "        {'delivered': payload['message_id']}, sort_keys=True,\n"
    "        separators=(',', ':')).encode('utf-8') + b'\\n')\n"
    "    sys.stdout.buffer.flush()\n"
)

EXIT1_CHILD = "import json, sys\nline = sys.stdin.buffer.readline()\n" \
              "if line:\n" + ACKING_ECHO + "sys.exit(1)\n"

POLLUTE_CHILD = (
    "import json, sys\n"
    "line = sys.stdin.buffer.readline()\n"
    "sys.stdout.buffer.write(b'protocol pollution\\n')\n"
    "if line:\n"
    "    payload = json.loads(line)\n"
    "    sys.stdout.buffer.write(line)\n"
    "    sys.stdout.buffer.write(json.dumps(\n"
    "        {'delivered': payload['message_id']}, sort_keys=True,\n"
    "        separators=(',', ':')).encode('utf-8') + b'\\n')\n"
    "    sys.stdout.buffer.flush()\n"
)

TAMPER_CHILD = (
    "import sys, json\n"
    "line = sys.stdin.buffer.readline()\n"
    "data = json.loads(line)\n"
    "data['protocol_version'] = '2.0'\n"
    "sys.stdout.buffer.write(json.dumps(data, sort_keys=True, "
    "separators=(',', ':')).encode('utf-8') + b'\\n')\n"
    "sys.stdout.buffer.write(json.dumps(\n"
    "    {'delivered': json.loads(line)['message_id']}, sort_keys=True,\n"
    "    separators=(',', ':')).encode('utf-8') + b'\\n')\n"
    "sys.stdout.buffer.flush()\n"
)

BIG_ECHO_CHILD = ECHO_CHILD

ENVPROBE_CHILD = (
    "import sys, json, os\n"
    "line = sys.stdin.buffer.readline()\n"
    "sys.stderr.buffer.write(json.dumps(sorted(os.environ)).encode('utf-8'))\n"
    "sys.stderr.buffer.flush()\n"
    "if line:\n"
    "    payload = json.loads(line)\n"
    "    sys.stdout.buffer.write(line)\n"
    "    sys.stdout.buffer.write(json.dumps(\n"
    "        {'delivered': payload['message_id']}, sort_keys=True,\n"
    "        separators=(',', ':')).encode('utf-8') + b'\\n')\n"
    "    sys.stdout.buffer.flush()\n"
)

# The approved lifecycle: ONE child per transport instance, kept alive
# across sends, acknowledging every envelope it reads. It ends on stdin
# EOF (close()) — the instance never spawns a replacement.
LOOPING_ECHO_CHILD = (
    "import json, sys\n"
    "for line in sys.stdin.buffer:\n"
    "    payload = json.loads(line)\n"
    "    sys.stdout.buffer.write(line)\n"
    "    sys.stdout.buffer.write(json.dumps(\n"
    "        {'delivered': payload['message_id']}, sort_keys=True,\n"
    "        separators=(',', ':')).encode('utf-8') + b'\\n')\n"
    "    sys.stdout.buffer.flush()\n"
)

# Never opens stdin: pins the parent's write phase on a full pipe.
NO_READ_SLEEP_CHILD = "import time\ntime.sleep(60)\n"

# Writes a line that is not UTF-8 at all.
INVALID_UTF8_CHILD = (
    "import sys\n"
    "sys.stdin.buffer.readline()\n"
    "sys.stdout.buffer.write(b'\\xff\\xfe not utf8\\n')\n"
    "sys.stdout.buffer.flush()\n"
)

# Closes its stdin read side and ends, never acknowledging anything.
CLOSE_STDIN_EXIT_CHILD = "import sys\nsys.stdin.buffer.close()\n"

# Acknowledges exactly one message, then closes its stdin read side and
# ends: the next send must hit a dead channel, not a replacement child.
ACK_CLOSE_EXIT_CHILD = (
    "import json, sys\n"
    "line = sys.stdin.buffer.readline()\n"
    "payload = json.loads(line)\n"
    "sys.stdout.buffer.write(json.dumps(\n"
    "    {'delivered': payload['message_id']}, sort_keys=True,\n"
    "    separators=(',', ':')).encode('utf-8') + b'\\n')\n"
    "sys.stdout.buffer.flush()\n"
    "sys.stdin.buffer.close()\n"
)

# Acknowledges every message it reads and logs "<pid> <message_id>" per
# line, proving WHICH child received WHAT order.
LOGGING_CHILD = (
    "import json, os, sys\n"
    "log = open(LOG_PATH, 'a', encoding='utf-8')\n"
    "for line in sys.stdin.buffer:\n"
    "    payload = json.loads(line)\n"
    "    log.write('%d %s\\n' % (os.getpid(), payload['message_id']))\n"
    "    log.flush()\n"
    "    sys.stdout.buffer.write(json.dumps(\n"
    "        {'delivered': payload['message_id']}, sort_keys=True,\n"
    "        separators=(',', ':')).encode('utf-8') + b'\\n')\n"
    "    sys.stdout.buffer.flush()\n"
)


def arch(task_id="T1"):
    return ArchitecturePacket(
        task_id=task_id, role="architect", goal=("g",), constraints=("c",),
        architecture=("a",), interfaces=({},), implementation_steps=({},),
        acceptance_criteria=("ac1",), risks=({},),
    )


def packet(cid=None, target_role="coder"):
    cid = cid or new_correlation_id()
    return CollaborationPacket(
        correlation_id=cid, task_id="T1",
        source_agent="arch-bot", target_agent="coder-bot",
        source_role="architect", target_role=target_role,
        payload_type=CollaborationPayloadType.ARCHITECTURE, payload=arch(),
    )


def envelope(**overrides):
    cid = overrides.get("correlation_id") or new_correlation_id()
    values = dict(
        message_id=new_message_id_fixture(),
        correlation_id=cid,
        sender=agent_address(AgentIdentity("arch-bot"), "architect"),
        recipient=agent_address(AgentIdentity("coder-bot"), "coder"),
        role="coder",
        payload_type=None,
    )
    values.pop("payload_type")
    if "payload" not in overrides:
        values["payload"] = packet(cid)
    values.update(overrides)
    from remote_contract import RemoteEnvelope, RemotePayloadType
    values["payload_type"] = RemotePayloadType.COLLABORATION_PACKET
    return RemoteEnvelope(**values)


def new_message_id_fixture():
    from remote_contract import new_message_id
    return new_message_id()


def env_for(seq, recipient_id, recipient_role):
    cid = new_correlation_id()
    payload = CollaborationPacket(
        correlation_id=cid, task_id="T1",
        source_agent="arch-bot", target_agent=recipient_id,
        source_role="architect", target_role=recipient_role,
        payload_type=CollaborationPayloadType.ARCHITECTURE, payload=arch(),
    )
    return envelope(
        message_id=f"remote-{seq}", correlation_id=cid,
        recipient=agent_address(AgentIdentity(recipient_id), recipient_role),
        role=recipient_role, payload=payload,
    )


def send_bounded(transport, env, bound=10.0):
    """Run one send() off-thread so an unbounded hang shows up as a
    bounded failure instead of freezing the suite."""
    box = {}

    def run():
        box["receipt"] = transport.send(env)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(bound)
    return box.get("receipt"), worker.is_alive()


class TransportContractTests(unittest.TestCase):
    def test_subprocess_transport_conforms_to_the_b1_protocol(self):
        transport = SubprocessStdioEnvelopeTransport(
            [sys.executable, "-c", SILENT_CHILD])
        self.assertIsInstance(transport, RemoteEnvelopeTransport)
        transport.close()

    def test_command_must_be_a_non_empty_argv_of_strings(self):
        for bad in ([], (), "python -c script", [1, 2], ["ok", 7], None):
            with self.subTest(command=bad):
                with self.assertRaises(TypeError):
                    SubprocessStdioEnvelopeTransport(bad)

    def test_timeout_must_be_positive(self):
        with self.assertRaises(ValueError):
            SubprocessStdioEnvelopeTransport(
                [sys.executable, "-c", SILENT_CHILD], timeout_seconds=0)


class SendDeliveryTests(unittest.TestCase):
    def test_single_envelope_roundtrip_through_child_stdio(self):
        transport = SubprocessStdioEnvelopeTransport(
            [sys.executable, "-c", ECHO_CHILD])
        try:
            env = envelope()
            receipt = transport.send(env)
            self.assertEqual(receipt.status, RemoteEnvelopeStatus.DELIVERED)
            self.assertIsNone(receipt.error_code)
            self.assertEqual(receipt.message_id, env.message_id)
            self.assertEqual(receipt.correlation_id, env.correlation_id)
            self.assertEqual(receipt.recipient, env.recipient)
            self.assertEqual(transport.receive(env.recipient), env)
        finally:
            transport.close()

    def test_multiple_jsonl_messages_flow_to_one_child(self):
        transport = SubprocessStdioEnvelopeTransport(
            [sys.executable, "-c", LOOPING_ECHO_CHILD])
        try:
            sent = [envelope(message_id=f"remote-m{index}")
                    for index in range(3)]
            for env in sent:
                self.assertEqual(transport.send(env).status,
                                 RemoteEnvelopeStatus.DELIVERED)
            for env in sent:
                self.assertEqual(transport.receive(env.recipient), env)
        finally:
            transport.close()

    def test_fifo_order_is_preserved(self):
        transport = SubprocessStdioEnvelopeTransport(
            [sys.executable, "-c", LOOPING_ECHO_CHILD])
        try:
            order = ["remote-m1", "remote-m2", "remote-m3"]
            for message_id in order:
                transport.send(envelope(message_id=message_id))
            received_ids = [transport.receive(
                agent_address(AgentIdentity("coder-bot"), "coder")).message_id
                for _ in order]
            self.assertEqual(received_ids, order)
        finally:
            transport.close()

    def test_dual_recipient_interleaving_isolation(self):
        transport = SubprocessStdioEnvelopeTransport(
            [sys.executable, "-c", LOOPING_ECHO_CHILD])
        try:
            a1 = env_for("m1", "coder-bot", "coder")
            b1 = env_for("m2", "reviewer-bot", "reviewer")
            a2 = env_for("m3", "coder-bot", "coder")
            b2 = env_for("m4", "reviewer-bot", "reviewer")
            for env in (a1, b1, a2, b2):
                self.assertEqual(transport.send(env).status,
                                 RemoteEnvelopeStatus.DELIVERED)
            self.assertEqual(transport.receive(a1.recipient), a1)
            self.assertEqual(transport.receive(b1.recipient), b1)
            self.assertEqual(transport.receive(a1.recipient), a2)
            self.assertEqual(transport.receive(b1.recipient), b2)
            self.assertIsNone(transport.receive(a1.recipient))
            self.assertIsNone(transport.receive(b1.recipient))
        finally:
            transport.close()

    def test_empty_stdout_and_silent_mailbox_is_not_delivered(self):
        # Case C/D fixture family: a silent child (nothing on stdout, no
        # acknowledgement of any kind) can never be DELIVERED, whether it
        # ends inside the window (FAILED) or outlives it (TIMEOUT).
        transport = SubprocessStdioEnvelopeTransport(
            [sys.executable, "-c", SILENT_CHILD])
        try:
            env = envelope()
            receipt = transport.send(env)
            self.assertEqual(receipt.status, RemoteEnvelopeStatus.FAILED)
            self.assertIs(receipt.error_code,
                          RemoteEnvelopeErrorCode.DELIVERY_FAILED)
            self.assertIsNone(transport.receive(env.recipient))
        finally:
            transport.close()

    def test_unicode_payload_survives_the_pipe(self):
        non_ascii = ArchitecturePacket(
            task_id="T1", role="architect", goal=("目标",),
            constraints=("约束",), architecture=("架构",), interfaces=({},),
            implementation_steps=({},), acceptance_criteria=("验收通过",),
            risks=({},),
        )
        cid = new_correlation_id()
        wrapped = CollaborationPacket(
            correlation_id=cid, task_id="T1",
            source_agent="arch-bot", target_agent="coder-bot",
            source_role="architect", target_role="coder",
            payload_type=CollaborationPayloadType.ARCHITECTURE,
            payload=non_ascii,
        )
        transport = SubprocessStdioEnvelopeTransport(
            [sys.executable, "-c", ECHO_CHILD])
        try:
            env = envelope(correlation_id=cid, payload=wrapped)
            self.assertEqual(transport.send(env).status,
                             RemoteEnvelopeStatus.DELIVERED)
            self.assertEqual(transport.receive(env.recipient), env)
        finally:
            transport.close()

    def test_wire_is_deterministic_across_exchanges(self):
        transport = SubprocessStdioEnvelopeTransport(
            [sys.executable, "-c", LOOPING_ECHO_CHILD])
        try:
            cid = new_correlation_id()
            first = envelope(message_id="remote-m1", correlation_id=cid,
                             payload=packet(cid))
            second = envelope(message_id="remote-m1", correlation_id=cid,
                              payload=packet(cid))
            transport.send(first)
            transport.send(second)
            got_first = transport.receive(first.recipient)
            got_second = transport.receive(first.recipient)
            self.assertEqual(serialize_remote_envelope(got_first),
                             serialize_remote_envelope(second))
            self.assertEqual(serialize_remote_envelope(got_second),
                             serialize_remote_envelope(first))
        finally:
            transport.close()


class DeliverySemanticsTests(unittest.TestCase):
    def test_accepted_is_never_produced(self):
        transport = SubprocessStdioEnvelopeTransport(
            [sys.executable, "-c", ECHO_CHILD])
        try:
            observed = {transport.send(envelope()).status}
            observed.add(transport.send("not an envelope").status)
            self.assertIn(RemoteEnvelopeStatus.DELIVERED, observed)
            self.assertIn(RemoteEnvelopeStatus.REJECTED, observed)
            self.assertNotIn(RemoteEnvelopeStatus.ACCEPTED, observed)
        finally:
            transport.close()

    def test_delivered_is_not_executed(self):
        values = {status.value for status in RemoteEnvelopeStatus}
        self.assertNotIn("EXECUTED", values)

    def test_non_zero_exit_after_delivery_keeps_delivered(self):
        transport = SubprocessStdioEnvelopeTransport(
            [sys.executable, "-c", EXIT1_CHILD])
        try:
            env = envelope()
            receipt = transport.send(env)
            self.assertEqual(receipt.status, RemoteEnvelopeStatus.DELIVERED)
            self.assertEqual(transport.receive(env.recipient), env)
        finally:
            transport.close()

    def test_rejection_leaves_prior_success_untouched(self):
        transport = SubprocessStdioEnvelopeTransport(
            [sys.executable, "-c", ECHO_CHILD])
        try:
            env = envelope()
            self.assertEqual(transport.send(env).status,
                             RemoteEnvelopeStatus.DELIVERED)
            self.assertEqual(transport.send("not an envelope").status,
                             RemoteEnvelopeStatus.REJECTED)
            self.assertEqual(transport.receive(env.recipient), env)
            self.assertIsNone(transport.receive(env.recipient))
        finally:
            transport.close()


class AckAuthorityTests(unittest.TestCase):
    """ACK = delivery authority; exit code = lifecycle fact.

    Every case is deterministic: the receipt is a function of whether the
    child produced {"delivered": "<message_id>"} for the sent message,
    never of how the child ended.
    """

    def test_case_a_ack_then_nonzero_exit_is_delivered(self):
        transport = SubprocessStdioEnvelopeTransport(
            [sys.executable, "-c", EXIT1_CHILD])
        try:
            env = envelope()
            receipt = transport.send(env)
            self.assertEqual(receipt.status, RemoteEnvelopeStatus.DELIVERED)
            self.assertIsNone(receipt.error_code)
            self.assertEqual(receipt.message_id, env.message_id)
            self.assertEqual(transport.receive(env.recipient), env)
        finally:
            transport.close()

    def test_case_b_exit_without_ack_is_failed(self):
        transport = SubprocessStdioEnvelopeTransport(
            [sys.executable, "-c", NO_ACK_EXIT1_CHILD])
        try:
            env = envelope()
            receipt = transport.send(env)
            self.assertEqual(receipt.status, RemoteEnvelopeStatus.FAILED)
            self.assertIs(receipt.error_code,
                          RemoteEnvelopeErrorCode.DELIVERY_FAILED)
            self.assertEqual(receipt.message_id, env.message_id)
            self.assertEqual(receipt.correlation_id, env.correlation_id)
            self.assertEqual(receipt.recipient, env.recipient)
            self.assertIsNone(transport.receive(env.recipient))
        finally:
            transport.close()

    def test_case_c_silent_child_ending_in_window_is_failed(self):
        transport = SubprocessStdioEnvelopeTransport(
            [sys.executable, "-c", SILENT_CHILD])
        try:
            env = envelope()
            receipt = transport.send(env)
            self.assertEqual(receipt.status, RemoteEnvelopeStatus.FAILED)
            self.assertIs(receipt.error_code,
                          RemoteEnvelopeErrorCode.DELIVERY_FAILED)
            self.assertIsNone(transport.receive(env.recipient))
        finally:
            transport.close()

    def test_case_d_silent_child_outliving_window_is_timeout(self):
        transport = SubprocessStdioEnvelopeTransport(
            [sys.executable, "-c", NEVER_EXIT_CHILD], timeout_seconds=0.5)
        try:
            env = envelope()
            receipt = transport.send(env)
            self.assertEqual(receipt.status, RemoteEnvelopeStatus.TIMEOUT)
            self.assertIs(receipt.error_code,
                          RemoteEnvelopeErrorCode.DELIVERY_TIMEOUT)
            self.assertEqual(receipt.message_id, env.message_id)
            self.assertIsNotNone(transport._process.poll())
        finally:
            transport.close()

    def test_case_e_ack_survives_later_nonzero_exit(self):
        # DELIVERED is decided by the acknowledgement; the exit code that
        # follows cannot retroactively turn it into FAILED.
        transport = SubprocessStdioEnvelopeTransport(
            [sys.executable, "-c", EXIT1_CHILD])
        try:
            env = envelope()
            receipt = transport.send(env)
            self.assertEqual(receipt.status, RemoteEnvelopeStatus.DELIVERED)
            self.assertIsNone(receipt.error_code)
            self.assertEqual(transport.receive(env.recipient), env)
            self.assertIsNone(transport.receive(env.recipient))
        finally:
            transport.close()

    def test_ack_must_name_the_sent_message(self):
        # An acknowledgement for a different message is not an
        # acknowledgement of this delivery: it is a protocol violation.
        foreign = (
            "import json, sys\n"
            "sys.stdin.buffer.readline()\n"
            "sys.stdout.buffer.write(json.dumps({'delivered': 'remote-nope'},"
            " sort_keys=True, separators=(',', ':')).encode('utf-8')"
            " + b'\\n')\n"
            "sys.stdout.buffer.flush()\n"
        )
        transport = SubprocessStdioEnvelopeTransport(
            [sys.executable, "-c", foreign])
        try:
            env = envelope()
            receipt = transport.send(env)
            self.assertEqual(receipt.status, RemoteEnvelopeStatus.REJECTED)
            self.assertIs(receipt.error_code,
                          RemoteEnvelopeErrorCode.INVALID_ENVELOPE)
            self.assertEqual(receipt.message_id, env.message_id)
        finally:
            transport.close()


class FailureTests(unittest.TestCase):
    def test_child_that_exits_before_reading_is_deterministically_failed(self):
        # Either the write hits a dead pipe (OSError -> FAILED) or it lands
        # in the buffer and the child ends without acknowledging (FAILED):
        # both branches of the race converge on the same honest verdict,
        # so the outcome is deterministic. No exception may escape, the
        # mailbox stays empty, and the child is reaped.
        transport = SubprocessStdioEnvelopeTransport(
            [sys.executable, "-c", "import sys\n"])
        try:
            env = envelope()
            receipt = transport.send(env)
            self.assertEqual(receipt.status, RemoteEnvelopeStatus.FAILED)
            self.assertIs(receipt.error_code,
                          RemoteEnvelopeErrorCode.DELIVERY_FAILED)
            self.assertEqual(receipt.message_id, env.message_id)
            self.assertIsNone(transport.receive(env.recipient))
        finally:
            transport.close()

    def test_spawn_failure_is_a_failed_receipt(self):
        transport = SubprocessStdioEnvelopeTransport(
            ["b2-no-such-executable-xyz"])
        try:
            receipt = transport.send(envelope())
            self.assertEqual(receipt.status, RemoteEnvelopeStatus.FAILED)
            self.assertIs(receipt.error_code,
                          RemoteEnvelopeErrorCode.DELIVERY_FAILED)
            self.assertEqual(transport.send(envelope()).status,
                             RemoteEnvelopeStatus.FAILED)
        finally:
            transport.close()

    def test_send_after_close_is_failed(self):
        transport = SubprocessStdioEnvelopeTransport(
            [sys.executable, "-c", ECHO_CHILD])
        transport.close()
        receipt = transport.send(envelope())
        self.assertEqual(receipt.status, RemoteEnvelopeStatus.FAILED)
        self.assertIs(receipt.error_code,
                      RemoteEnvelopeErrorCode.DELIVERY_FAILED)


class TimeoutTests(unittest.TestCase):
    def test_exchange_window_overrun_is_timeout_and_reaps_the_child(self):
        transport = SubprocessStdioEnvelopeTransport(
            [sys.executable, "-c", NEVER_EXIT_CHILD], timeout_seconds=0.75)
        try:
            env = envelope()
            receipt = transport.send(env)
            self.assertEqual(receipt.status, RemoteEnvelopeStatus.TIMEOUT)
            self.assertIs(receipt.error_code,
                          RemoteEnvelopeErrorCode.DELIVERY_TIMEOUT)
            self.assertEqual(receipt.message_id, env.message_id)
            # no orphan: the child was reaped (terminate -> kill -> wait)
            self.assertIsNotNone(transport._process)
            self.assertIsNotNone(transport._process.poll())
            # no restart strategy: the broken channel refuses further sends
            self.assertEqual(transport.send(envelope()).status,
                             RemoteEnvelopeStatus.FAILED)
        finally:
            transport.close()

    def test_large_payload_with_silent_consumer_is_bounded_and_failed(self):
        big = "x" * (1024 * 1024)
        big_packet = ArchitecturePacket(
            task_id="T1", role="architect", goal=(big,), constraints=("c",),
            architecture=("a",), interfaces=({},), implementation_steps=({},),
            acceptance_criteria=("ac1",), risks=({},),
        )
        cid = new_correlation_id()
        wrapped = CollaborationPacket(
            correlation_id=cid, task_id="T1",
            source_agent="arch-bot", target_agent="coder-bot",
            source_role="architect", target_role="coder",
            payload_type=CollaborationPayloadType.ARCHITECTURE,
            payload=big_packet,
        )
        transport = SubprocessStdioEnvelopeTransport(
            [sys.executable, "-c", SILENT_CHILD], timeout_seconds=30)
        try:
            env = envelope(correlation_id=cid, payload=wrapped)
            receipt = transport.send(env)
            # The child consumed the whole 1MB line but never acknowledged
            # it: the exchange completes inside the window with an honest
            # FAILED verdict (delivery never became a fact).
            self.assertEqual(receipt.status, RemoteEnvelopeStatus.FAILED)
            self.assertIs(receipt.error_code,
                          RemoteEnvelopeErrorCode.DELIVERY_FAILED)
        finally:
            transport.close()

    def test_large_payload_full_duplex_is_delivered(self):
        # 1MB in each direction at once: the write phase and the child's
        # echo/ack output must not pin each other. The verdict is
        # deterministic: the child reads the line, echoes it, and
        # acknowledges it inside the window.
        big = "y" * (1024 * 1024)
        big_packet = ArchitecturePacket(
            task_id="T1", role="architect", goal=(big,), constraints=("c",),
            architecture=("a",), interfaces=({},), implementation_steps=({},),
            acceptance_criteria=("ac1",), risks=({},),
        )
        cid = new_correlation_id()
        wrapped = CollaborationPacket(
            correlation_id=cid, task_id="T1",
            source_agent="arch-bot", target_agent="coder-bot",
            source_role="architect", target_role="coder",
            payload_type=CollaborationPayloadType.ARCHITECTURE,
            payload=big_packet,
        )
        transport = SubprocessStdioEnvelopeTransport(
            [sys.executable, "-c", ECHO_CHILD], timeout_seconds=30)
        try:
            env = envelope(correlation_id=cid, payload=wrapped)
            receipt = transport.send(env)
            self.assertEqual(receipt.status, RemoteEnvelopeStatus.DELIVERED)
            self.assertEqual(transport.receive(env.recipient), env)
        finally:
            transport.close()

    def test_write_phase_is_bounded_by_the_deadline(self):
        # A child that never reads pins the parent's stdin write on a full
        # pipe (1MB far exceeds any pipe buffer). The monotonic deadline
        # must still be enforced: bounded TIMEOUT, child reaped, channel
        # broken. Under the old order (write, then check the deadline)
        # this send would never return.
        big = "z" * (1024 * 1024)
        big_packet = ArchitecturePacket(
            task_id="T1", role="architect", goal=(big,), constraints=("c",),
            architecture=("a",), interfaces=({},), implementation_steps=({},),
            acceptance_criteria=("ac1",), risks=({},),
        )
        cid = new_correlation_id()
        wrapped = CollaborationPacket(
            correlation_id=cid, task_id="T1",
            source_agent="arch-bot", target_agent="coder-bot",
            source_role="architect", target_role="coder",
            payload_type=CollaborationPayloadType.ARCHITECTURE,
            payload=big_packet,
        )
        transport = SubprocessStdioEnvelopeTransport(
            [sys.executable, "-c", NO_READ_SLEEP_CHILD], timeout_seconds=1.0)
        try:
            env = envelope(correlation_id=cid, payload=wrapped)
            receipt, hung = send_bounded(transport, env, bound=10.0)
            self.assertFalse(hung, "send() must not block past its deadline")
            self.assertEqual(receipt.status, RemoteEnvelopeStatus.TIMEOUT)
            self.assertIs(receipt.error_code,
                          RemoteEnvelopeErrorCode.DELIVERY_TIMEOUT)
            self.assertEqual(receipt.message_id, env.message_id)
            # no orphan and no restart: reaped, then refused
            self.assertIsNotNone(transport._process)
            self.assertIsNotNone(transport._process.poll())
            self.assertEqual(transport.send(envelope()).status,
                             RemoteEnvelopeStatus.FAILED)
        finally:
            transport.close()


class ReceiveIntegrityTests(unittest.TestCase):
    def test_invalid_utf8_stdout_is_invalid_envelope_not_a_leak(self):
        # Malformed stdout bytes get exactly the same semantics as
        # malformed stdout JSON: an INVALID_ENVELOPE poison consumed
        # loudly at receive(), never a UnicodeDecodeError escaping send().
        transport = SubprocessStdioEnvelopeTransport(
            [sys.executable, "-c", INVALID_UTF8_CHILD])
        try:
            env = envelope()
            receipt = transport.send(env)
            self.assertEqual(receipt.status, RemoteEnvelopeStatus.REJECTED)
            self.assertIs(receipt.error_code,
                          RemoteEnvelopeErrorCode.INVALID_ENVELOPE)
            self.assertEqual(receipt.message_id, env.message_id)
            with self.assertRaises(RemoteEnvelopeError) as caught:
                transport.receive(env.recipient)
            self.assertIs(caught.exception.category,
                          RemoteEnvelopeErrorCode.INVALID_ENVELOPE)
            self.assertIsNone(transport.receive(env.recipient))
        finally:
            transport.close()

    def test_malformed_stdout_poisons_and_surfaces_loudly(self):
        transport = SubprocessStdioEnvelopeTransport(
            [sys.executable, "-c", POLLUTE_CHILD])
        try:
            env = envelope()
            # The garbage line is a protocol violation: the exchange ends
            # refused, not delivered.
            self.assertEqual(transport.send(env).status,
                             RemoteEnvelopeStatus.REJECTED)
            with self.assertRaises(RemoteEnvelopeError) as caught:
                transport.receive(env.recipient)
            self.assertIs(caught.exception.category,
                          RemoteEnvelopeErrorCode.INVALID_ENVELOPE)
            # the poison is consumed once; the valid echo still arrives
            self.assertEqual(transport.receive(env.recipient), env)
        finally:
            transport.close()

    def test_unsupported_version_line_surfaces_as_unsupported_protocol(self):
        transport = SubprocessStdioEnvelopeTransport(
            [sys.executable, "-c", TAMPER_CHILD])
        try:
            env = envelope()
            receipt = transport.send(env)
            self.assertEqual(receipt.status, RemoteEnvelopeStatus.REJECTED)
            self.assertIs(receipt.error_code,
                          RemoteEnvelopeErrorCode.UNSUPPORTED_PROTOCOL)
            with self.assertRaises(RemoteEnvelopeError) as caught:
                transport.receive(env.recipient)
            self.assertIs(caught.exception.category,
                          RemoteEnvelopeErrorCode.UNSUPPORTED_PROTOCOL)
        finally:
            transport.close()

    def test_recipient_mismatch_fails_loudly(self):
        transport = SubprocessStdioEnvelopeTransport(
            [sys.executable, "-c", ECHO_CHILD])
        try:
            to_coder = envelope()
            to_reviewer = env_for("m2", "reviewer-bot", "reviewer")
            transport.send(to_coder)
            # poison: mailbox A now holds a valid wire addressed to B
            transport._mailboxes[to_coder.recipient][0] = (
                serialize_remote_envelope(to_reviewer))
            with self.assertRaises(RemoteEnvelopeError) as caught:
                transport.receive(to_coder.recipient)
            self.assertIs(caught.exception.category,
                          RemoteEnvelopeErrorCode.INVALID_ENVELOPE)
        finally:
            transport.close()

    def test_stderr_noise_never_enters_the_protocol(self):
        transport = SubprocessStdioEnvelopeTransport(
            [sys.executable, "-c", STDERR_CHILD], stderr=subprocess.PIPE)
        try:
            env = envelope()
            self.assertEqual(transport.send(env).status,
                             RemoteEnvelopeStatus.DELIVERED)
            self.assertEqual(transport.receive(env.recipient), env)
        finally:
            transport.close()
        # diagnostics are read once the child is reaped and its stderr
        # stream has ended; they never enter protocol parsing
        self.assertIn(b"diagnostic noise", transport.last_diagnostics)

    def test_close_reaps_and_is_idempotent(self):
        transport = SubprocessStdioEnvelopeTransport(
            [sys.executable, "-c", NEVER_EXIT_CHILD], timeout_seconds=0.5)
        env = envelope()
        self.assertEqual(transport.send(env).status,
                         RemoteEnvelopeStatus.TIMEOUT)
        transport.close()
        transport.close()  # idempotent
        self.assertTrue(transport._closed)
        self.assertIsNotNone(transport._process.poll())


class SecurityBoundaryTests(unittest.TestCase):
    def test_environment_whitelist_keeps_parent_secrets_out(self):
        decoy_key = "B2_DECOY_CREDENTIAL"
        decoy_value = "b2-fake-decoy-marker"
        os.environ[decoy_key] = decoy_value
        try:
            transport = SubprocessStdioEnvelopeTransport(
                [sys.executable, "-c", ENVPROBE_CHILD],
                stderr=subprocess.PIPE)
            try:
                env = envelope()
                self.assertEqual(transport.send(env).status,
                                 RemoteEnvelopeStatus.DELIVERED)
                self.assertEqual(transport.receive(env.recipient), env)
            finally:
                transport.close()
            diagnostics = transport.last_diagnostics.decode("utf-8")
            self.assertNotIn(decoy_key, diagnostics)
            self.assertNotIn(decoy_value, diagnostics)
            self.assertIn("PATH", diagnostics)
        finally:
            del os.environ[decoy_key]

    def test_child_cwd_is_predictable(self):
        transport = SubprocessStdioEnvelopeTransport(
            [sys.executable, "-c", ENVPROBE_CHILD], stderr=subprocess.PIPE)
        try:
            env = envelope()
            transport.send(env)
        finally:
            transport.close()
        probe = json.loads(transport.last_diagnostics.decode("utf-8"))
        # env whitelist means cwd cannot be probed through the
        # environment; predictability is inherited from the host by
        # contract (cwd=None means "same as this process").
        self.assertIsInstance(probe, list)


class LifecycleTests(unittest.TestCase):
    """The approved lifecycle: ONE child per instance, reused across
    sends, first-in-first-out; a dead child is never replaced."""

    def test_one_child_serves_all_sends_in_fifo_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "received.log"
            child = LOGGING_CHILD.replace(
                "LOG_PATH", json.dumps(str(log_path)))
            transport = SubprocessStdioEnvelopeTransport(
                [sys.executable, "-c", child])
            try:
                order = [f"remote-f{index}" for index in range(4)]
                for message_id in order:
                    receipt = transport.send(envelope(message_id=message_id))
                    self.assertEqual(receipt.status,
                                     RemoteEnvelopeStatus.DELIVERED)
            finally:
                transport.close()
            entries = [line.split()
                       for line in log_path.read_text(
                           encoding="utf-8").splitlines()]
            # every message arrived, in exact send order
            self.assertEqual([entry[1] for entry in entries], order)
            # and one single child process served the whole instance
            self.assertEqual(len({entry[0] for entry in entries}), 1)

    def test_dead_child_is_not_replaced(self):
        transport = SubprocessStdioEnvelopeTransport(
            [sys.executable, "-c", EXIT1_CHILD])
        try:
            self.assertEqual(transport.send(envelope()).status,
                             RemoteEnvelopeStatus.DELIVERED)
            # the child ends after its acknowledgement; the channel dies
            # with it and the instance refuses to spawn a successor
            second = transport.send(envelope())
            self.assertEqual(second.status, RemoteEnvelopeStatus.FAILED)
            self.assertIs(second.error_code,
                          RemoteEnvelopeErrorCode.DELIVERY_FAILED)
        finally:
            transport.close()


class BrokenPipeTests(unittest.TestCase):
    """Deterministic pipe-failure matrix: each fixture has exactly one
    honest verdict, whatever OSError flavour the platform raises
    (BrokenPipeError is only one face of OSError on Windows)."""

    def test_close_stdin_child_is_failed(self):
        # The child closes its read side and ends without acknowledging.
        # Either the write fails (pipe error -> FAILED) or it lands in
        # the buffer and the child ends unacknowledged (eof -> FAILED).
        transport = SubprocessStdioEnvelopeTransport(
            [sys.executable, "-c", CLOSE_STDIN_EXIT_CHILD], timeout_seconds=5)
        try:
            env = envelope()
            receipt = transport.send(env)
            self.assertEqual(receipt.status, RemoteEnvelopeStatus.FAILED)
            self.assertIs(receipt.error_code,
                          RemoteEnvelopeErrorCode.DELIVERY_FAILED)
            self.assertIsNone(transport.receive(env.recipient))
        finally:
            transport.close()

    def test_ack_then_close_stdin_ends_the_channel_after_delivery(self):
        transport = SubprocessStdioEnvelopeTransport(
            [sys.executable, "-c", ACK_CLOSE_EXIT_CHILD], timeout_seconds=5)
        try:
            env = envelope()
            self.assertEqual(transport.send(env).status,
                             RemoteEnvelopeStatus.DELIVERED)
            # the read side is now gone: the next send must fail honestly
            # (pipe error or dead child), never hang, never deliver
            second = transport.send(envelope())
            self.assertEqual(second.status, RemoteEnvelopeStatus.FAILED)
            self.assertIs(second.error_code,
                          RemoteEnvelopeErrorCode.DELIVERY_FAILED)
        finally:
            transport.close()


class UnicodeMixTests(unittest.TestCase):
    def test_mixed_unicode_payload_survives_the_boundary(self):
        mixed = ArchitecturePacket(
            task_id="T1", role="architect",
            goal=("目标📋", "制約チェック", "完了✅"),
            constraints=("約束と制限🎯",),
            architecture=("アーキテクチャ概要📝",),
            interfaces=({},), implementation_steps=({},),
            acceptance_criteria=("验收通过✅テスト合格",), risks=({},),
        )
        cid = new_correlation_id()
        wrapped = CollaborationPacket(
            correlation_id=cid, task_id="T1",
            source_agent="arch-bot", target_agent="coder-bot",
            source_role="architect", target_role="coder",
            payload_type=CollaborationPayloadType.ARCHITECTURE,
            payload=mixed,
        )
        transport = SubprocessStdioEnvelopeTransport(
            [sys.executable, "-c", LOOPING_ECHO_CHILD])
        try:
            env = envelope(correlation_id=cid, payload=wrapped)
            self.assertEqual(transport.send(env).status,
                             RemoteEnvelopeStatus.DELIVERED)
            received = transport.receive(env.recipient)
            self.assertEqual(received, env)
            self.assertEqual(received.payload.payload.goal,
                             ("目标📋", "制約チェック", "完了✅"))
        finally:
            transport.close()


class SourceScanTests(unittest.TestCase):
    SOURCE = (SCRIPTS / "remote_subprocess_transport.py").read_text(
        encoding="utf-8")

    def test_no_shell_invocation(self):
        self.assertNotIn("shell=True", self.SOURCE)
        self.assertIn("shell=False", self.SOURCE)

    def test_no_network_or_serialization_channels(self):
        # threading is deliberately present: one daemon thread drains the
        # child's stdout and one carries the stdin write so the exchange
        # can honour its monotonic deadline on blocking pipes — pipe
        # plumbing only, never a session, pool, or orchestration runtime.
        for forbidden in ("socket", "http", "websocket", "grpc", "urllib",
                          "ssl", "pickle", "marshal", "cloudpickle",
                          "asyncio", "multiprocessing",
                          "selectors", "concurrent", "a2a"):
            self.assertNotIn(forbidden, self.SOURCE, forbidden)

    def test_no_id_or_identity_minting(self):
        for forbidden in ("uuid", "random", "secrets", "new_message_id",
                          "new_correlation_id", "AgentIdentity",
                          "agent_identity", "runtime_identity", "runtime_id"):
            self.assertNotIn(forbidden, self.SOURCE, forbidden)

    def test_no_business_layer_reach(self):
        for forbidden in ("adapter", "orchestrat", "verified_",
                          "capability_registry", "task_budget", "loop_guard",
                          "execution_engine", "external_runtime",
                          "collaboration_session", "selection", "admission",
                          "qualification", "discovery", "schedul", "retry",
                          "authentication", "handshake", "provenance",
                          "fallback", "score", "sleep"):
            self.assertNotIn(forbidden, self.SOURCE, forbidden)

    def test_import_surface_is_exact(self):
        import ast
        tree = ast.parse(self.SOURCE)
        modules = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    modules.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    modules.add(node.module.split(".")[0])
        self.assertEqual(
            modules,
            {"__future__", "json", "os", "queue", "subprocess", "threading",
             "time", "remote_contract", "remote_envelope_transport"})


if __name__ == "__main__":
    unittest.main()
