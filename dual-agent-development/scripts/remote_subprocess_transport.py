"""V3.1-B2: Subprocess stdio envelope transport — the real process boundary.

This transport moves RemoteEnvelope values (V3.1-A) across a REAL process
boundary: an argv command (never a shell) spawns the ONE child this
instance will ever have, lazily on the first send, and every later send
reuses it. Each send writes exactly one canonical UTF-8 JSONL line to the
child's stdin; the child's stdout may return acknowledgement and envelope
lines. One line is one fact: canonical JSON escapes line breaks, so the
framing needs no extra protocol.

Boundedness: the stdin write is carried by a helper thread and the
child's stdout is drained (and classified) by a daemon thread, so a full
pipe on either side can never pin the exchange. The whole exchange —
write, acknowledgement wait, child lifecycle — is bounded by one
monotonic deadline enforced inside send(); an overrun reaps the child
(terminate -> kill -> wait) and breaks the channel.

Delivery honesty: DELIVERED is authorised only by the child's explicit
acknowledgement of the exact message inside the window — one JSON line
{"delivered": "<message_id>"} emitted after the child read the envelope
line. The child's exit code is a lifecycle fact and never changes a
delivered result; a child that ends without acknowledging the message
yields FAILED; a stdout line that violates the protocol refuses the
exchange (REJECTED with the violation's category) and poisons the
channel. ACCEPTED is never produced (send() is synchronous for its
caller), and nothing here claims the far side executed anything.

Inbound stdout lines are classified by the drain thread with the V3.1-A
authority and routed by their own recipient into per-recipient
mailboxes; a line that cannot decode poisons the channel and surfaces
loudly at the next receive() — never silently skipped, never rerouted.
Lines the child emitted before a timeout or refusal are still routed:
they are inbound facts, not verdicts. stderr is a diagnostics channel
only (default discarded, read once the child has ended) and never enters
protocol parsing.

A child that dies, a spawn failure, a window overrun or a protocol
violation marks the channel broken — the instance then refuses further
sends (no replacement child, no pool, no reuse across instances, no
session); close() is the only cleanup authority and reaps the child so
no orphan survives.

Process invocation: argv list, shell=False, explicit cwd, and a
whitelisted environment (six keys from the host process, or a
host-supplied mapping) — credential-shaped values in the parent
environment are not forwarded. The transport mints nothing: message and
correlation identity arrive inside the envelope and are only echoed.
This module performs no network operations of any kind.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from queue import Empty, Queue

from remote_contract import (
    RemoteEnvelope,
    RemoteEnvelopeError,
    RemoteEnvelopeErrorCode,
    RemoteEnvelopeStatus,
    deserialize_remote_envelope,
    serialize_remote_envelope,
)
from remote_envelope_transport import RemoteEnvelopeReceipt

# Whitelist, not blacklist: the child receives only the variables it needs
# to locate itself — everything else in the parent environment (especially
# credential-shaped values) is not forwarded. The same whitelist discipline
# governs every process invocation in this repository.
_ENV_KEYS = ("PATH", "HOME", "USERPROFILE", "SYSTEMROOT", "TEMP", "TMP")

# The transport-level delivery fact: the child boundary confirms it has
# read one envelope by writing a single canonical JSON line naming that
# envelope's message id. This is the ONLY acknowledgement in the protocol
# and it never claims anything was executed.
_ACK_FIELD = "delivered"

# Bounded joins for the helper threads once their process side is gone.
_SETTLE_SECONDS = 5.0


def _parse_ack(text: str):
    """Classify one stdout line as a delivery acknowledgement.

    Returns the acknowledged message id, or None when the line is not an
    acknowledgement (it is then an inbound envelope line or a violation,
    per the single classification chain in the drain thread).
    """
    try:
        payload = json.loads(text)
    except ValueError:
        return None
    if (isinstance(payload, dict) and set(payload) == {_ACK_FIELD}
            and isinstance(payload[_ACK_FIELD], str)):
        return payload[_ACK_FIELD]
    return None


class SubprocessStdioEnvelopeTransport:
    """Envelope delivery across a child-process stdio boundary."""

    def __init__(self, command, *, timeout_seconds: float = 120.0,
                 env=None, cwd=None, stderr=subprocess.DEVNULL):
        if not isinstance(command, (list, tuple)) or not command or not all(
                isinstance(part, str) for part in command):
            raise TypeError(
                "command must be a non-empty argv sequence of strings")
        if not isinstance(timeout_seconds, (int, float)) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be a positive number")
        self._command = tuple(command)
        self._timeout_seconds = float(timeout_seconds)
        self._env = None if env is None else dict(env)
        self._cwd = cwd
        self._stderr = stderr
        self._process = None
        self._mailboxes: dict = {}
        self._poison: RemoteEnvelopeError | None = None
        self._broken = False
        self._closed = False
        self.last_diagnostics = b""
        # One exchange at a time on the single child: send order is wire
        # order (first-in-first-out by construction).
        self._send_lock = threading.Lock()
        # Guards the mailboxes and the poison, which the drain thread
        # writes and receive() reads.
        self._state_lock = threading.Lock()
        # Facts from the drain and writer threads, consumed by the waiting
        # exchange: ("ack", mid) | ("written", None) | ("write_error",
        # None) | ("violation", exc) | ("eof", None).
        self._events: Queue = Queue()
        self._reader = None
        # Acks already published for the CURRENT exchange. Scoped per
        # exchange (not per child): a later send of the same message id
        # is a new delivery fact that must be acknowledged afresh.
        self._exchange_acks: set = set()

    # -- public surface ----------------------------------------------------

    def send(self, envelope) -> RemoteEnvelopeReceipt:
        if self._closed:
            return self._failed()
        if type(envelope) is not RemoteEnvelope:
            # Never read attributes off an untyped object.
            return RemoteEnvelopeReceipt(
                RemoteEnvelopeStatus.REJECTED,
                error_code=RemoteEnvelopeErrorCode.INVALID_ENVELOPE)
        echo = {
            "message_id": envelope.message_id,
            "correlation_id": envelope.correlation_id,
            "recipient": envelope.recipient,
        }
        try:
            wire = serialize_remote_envelope(envelope)
            decoded = deserialize_remote_envelope(wire)
        except RemoteEnvelopeError as exc:
            return RemoteEnvelopeReceipt(
                RemoteEnvelopeStatus.REJECTED, error_code=exc.category, **echo)
        except (TypeError, ValueError, RecursionError):
            # Expected codec/validation failure on a constructed envelope;
            # the boundary rejects the input structurally.
            return RemoteEnvelopeReceipt(
                RemoteEnvelopeStatus.REJECTED,
                error_code=RemoteEnvelopeErrorCode.INVALID_ENVELOPE, **echo)
        if decoded != envelope:
            return RemoteEnvelopeReceipt(
                RemoteEnvelopeStatus.REJECTED,
                error_code=RemoteEnvelopeErrorCode.INVALID_ENVELOPE, **echo)
        with self._send_lock:
            return self._exchange(envelope, wire, echo)

    def receive(self, recipient: str):
        with self._state_lock:
            if self._poison is not None:
                exc = self._poison
                self._poison = None
                raise exc
            mailbox = self._mailboxes.get(recipient)
            if not mailbox:
                return None
            wire = mailbox.pop(0)
        decoded = deserialize_remote_envelope(wire)
        if decoded.recipient != recipient:
            # A mailbox only ever yields its own mail; a wire addressed
            # elsewhere is a corrupted routing fact and fails loudly.
            raise RemoteEnvelopeError(
                RemoteEnvelopeErrorCode.INVALID_ENVELOPE,
                "mailbox wire is addressed to another recipient")
        return decoded

    def close(self):
        """Reap the child: stdin EOF, bounded wait, then terminate."""
        if self._closed:
            return
        self._closed = True
        process = self._process
        if process is None:
            return
        try:
            if process.stdin is not None and not process.stdin.closed:
                process.stdin.close()
        except OSError:
            pass
        try:
            process.wait(timeout=self._timeout_seconds)
        except subprocess.TimeoutExpired:
            self._terminate(process, self._timeout_seconds)
        if self._reader is not None:
            self._reader.join(timeout=_SETTLE_SECONDS)
        self._capture_diagnostics(process)
        self._release(process)

    # -- exchange ------------------------------------------------------------

    def _exchange(self, envelope, wire, echo):
        """One bounded exchange: write, await the acknowledgement, reap on
        every terminal path but delivery."""
        deadline = time.monotonic() + self._timeout_seconds
        # Facts left over from a previous exchange belong to a dead
        # channel and cannot testify about this one.
        self._drop_stale_events()
        try:
            process = self._ensure_process()
        except OSError:
            self._broken = True
            return self._failed(**echo)
        if process is None:
            return self._failed(**echo)
        writer = threading.Thread(
            target=self._write_line,
            args=(process, wire.encode("utf-8") + b"\n"),
            daemon=True, name="b2-stdio-write")
        with self._state_lock:
            self._exchange_acks = set()
        writer.start()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return self._window_overrun(process, echo)
            try:
                kind, value = self._events.get(timeout=remaining)
            except Empty:
                return self._window_overrun(process, echo)
            if kind == "ack":
                if value == envelope.message_id:
                    return RemoteEnvelopeReceipt(
                        RemoteEnvelopeStatus.DELIVERED, **echo)
                # An acknowledgement naming a message this channel was
                # never sent is a protocol violation, not a delivery.
                return self._protocol_refusal(process, echo, remaining,
                                              RemoteEnvelopeError(
                                                  RemoteEnvelopeErrorCode.INVALID_ENVELOPE,
                                                  "acknowledgement names an unexpected message"))
            if kind == "violation":
                return self._protocol_refusal(process, echo, remaining, value)
            if kind == "write_error":
                # Dead or broken channel: the delivery fact never came to
                # be.
                self._reap(process, remaining)
                self._broken = True
                return self._failed(**echo)
            if kind == "eof":
                # No acknowledgement can arrive once the child's stdout is
                # gone; confirm the exit and fail honestly.
                self._reap(process, remaining)
                self._broken = True
                return self._failed(**echo)
            # kind == "written": the line is on the wire; keep waiting for
            # the acknowledgement that authorises delivery.

    # -- helper threads ------------------------------------------------------

    def _write_line(self, process, payload):
        """Carry one stdin write so a full pipe cannot pin the exchange."""
        try:
            process.stdin.write(payload)
            process.stdin.flush()
        except (OSError, ValueError):
            self._events.put(("write_error", None))
        else:
            self._events.put(("written", None))

    def _drain_stdout(self, process):
        """Drain and classify every stdout line as it arrives:
        acknowledgement, inbound envelope, or protocol violation (poison).
        This is pipe plumbing only — it exists so the child's output can
        never block the exchange, and it makes no decision beyond the
        line classification below."""
        stdout = process.stdout
        while True:
            try:
                raw = stdout.readline()
            except (OSError, ValueError):
                self._events.put(("eof", None))
                return
            if not raw:
                self._events.put(("eof", None))
                return
            if raw.endswith(b"\n"):
                raw = raw[:-1]
            if not raw:
                continue
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                self._record_violation(RemoteEnvelopeError(
                    RemoteEnvelopeErrorCode.INVALID_ENVELOPE,
                    "malformed stdout line: not UTF-8"))
                continue
            ack = _parse_ack(text)
            if ack is not None:
                with self._state_lock:
                    if ack in self._exchange_acks:
                        # Duplicate acknowledgement inside one exchange:
                        # the boundary already confirmed this delivery.
                        continue
                    self._exchange_acks.add(ack)
                self._events.put(("ack", ack))
                continue
            try:
                decoded = deserialize_remote_envelope(text)
            except RemoteEnvelopeError as exc:
                self._record_violation(exc)
                continue
            with self._state_lock:
                self._mailboxes.setdefault(decoded.recipient, []).append(text)

    # -- internals ----------------------------------------------------------

    def _failed(self, **echo):
        return RemoteEnvelopeReceipt(
            RemoteEnvelopeStatus.FAILED,
            error_code=RemoteEnvelopeErrorCode.DELIVERY_FAILED, **echo)

    def _record_violation(self, exc):
        with self._state_lock:
            if self._poison is None:
                self._poison = exc
        self._events.put(("violation", exc))

    def _drop_stale_events(self):
        while True:
            try:
                self._events.get_nowait()
            except Empty:
                return

    def _child_env(self):
        if self._env is not None:
            return dict(self._env)
        return {key: value for key, value in (
            (key, os.environ.get(key)) for key in _ENV_KEYS) if value}

    def _ensure_process(self):
        """Lazy spawn of the ONE child this instance will ever have; its
        drain thread starts with it. Returns None when the channel must
        refuse (closed, broken, or the child already ended — there is no
        replacement). Raises OSError when the spawn itself fails."""
        if self._broken or self._closed:
            return None
        if self._process is not None:
            if self._process.poll() is None:
                return self._process
            self._capture_diagnostics(self._process)
            self._release(self._process)
            self._broken = True
            return None
        process = subprocess.Popen(
            list(self._command), stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=self._stderr, shell=False,
            cwd=self._cwd, env=self._child_env())
        self._process = process
        self._reader = threading.Thread(
            target=self._drain_stdout, args=(process,),
            daemon=True, name="b2-stdio-drain")
        self._reader.start()
        return process

    def _window_overrun(self, process, echo):
        # A timed-out exchange has no protocol verdict; the child is
        # reaped and the channel refuses further sends.
        self._reap(process, 0.0)
        self._broken = True
        return RemoteEnvelopeReceipt(
            RemoteEnvelopeStatus.TIMEOUT,
            error_code=RemoteEnvelopeErrorCode.DELIVERY_TIMEOUT, **echo)

    def _protocol_refusal(self, process, echo, remaining, exc):
        """A stdout protocol violation refuses the exchange and ends the
        channel; the violation itself surfaces as poison at receive()."""
        with self._state_lock:
            if self._poison is None:
                self._poison = exc
        self._reap(process, remaining)
        self._broken = True
        return RemoteEnvelopeReceipt(
            RemoteEnvelopeStatus.REJECTED, error_code=exc.category, **echo)

    def _reap(self, process, budget):
        """End the child and settle its drain thread: every line it
        managed to write is classified before this returns."""
        if process.poll() is None:
            self._terminate(process, budget)
        else:
            process.wait()
        if self._reader is not None:
            self._reader.join(timeout=_SETTLE_SECONDS)
        self._capture_diagnostics(process)
        self._release(process)

    def _terminate(self, process, budget):
        process.terminate()
        try:
            process.wait(timeout=max(budget, 0.0))
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()

    def _capture_diagnostics(self, process):
        if (self._stderr is subprocess.PIPE and process.stderr is not None
                and not process.stderr.closed):
            self.last_diagnostics = process.stderr.read()

    def _release(self, process):
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None and not stream.closed:
                try:
                    stream.close()
                except OSError:
                    pass
