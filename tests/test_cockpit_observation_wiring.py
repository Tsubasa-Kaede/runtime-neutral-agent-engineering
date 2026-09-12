"""CU-TUI-1 (V3.2): cockpit sequential execution observation wiring.

Execution -> ExecutionEvent -> ObservationSink / EventIndex for the
cockpit composition root. All doubles are offline: no network, no REAL
providers, no credentials, no runtime installation. Evidence is always
injected; the default evidence directory is never read.

RED proof (written before the production wiring lands): cockpit_main
does not accept the observation injection keywords (TypeError:
unexpected keyword argument 'observation_sink') and the amended
architecture guard in test_cockpit_entry.py expects exactly one
.invoke( delegation point — both fail against the un-wired
composition root.

Locked semantics under test (CU-TUI-1 authorization):
- HANDOFF carries the PRODUCING role in stage and the RECEIVING
  runtime in runtime_id (transport/delivery facts are not handoffs).
- INVOCATION_FINISHED exists only when a real InvocationResult
  returned; its status is the result's own status value verbatim; an
  adapter that raises produces no FINISHED event and the exception
  propagates unchanged.
- TERMINAL observes the real RunOutcome only; PARKED/ABORTED arrive
  from the pipeline's admission boundary, never from observation.
- duration_ms only passes a value the result's trace already carries.
- The default path (no injection) emits nothing and constructs no
  event store; stdout/exit/JSON stay byte-identical to the
  pre-wiring contract.
"""
import io
import json
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import cockpit_entry
import host_entry
from event_index import EventIndex
from execution_observation import ExecutionEventType
from external_runtime import InvocationResult, InvocationStatus, InvocationTrace

from tests.test_cockpit_entry import (
    _FakeProfile,
    _ScriptedAdapter,
    _abort_hook,
    _pause_hook,
    _run_cockpit,
    _standard_adapters,
)

_FROZEN_EVENT_TYPES = {
    "DECISION", "STAGE_STARTED", "INVOCATION_STARTED",
    "INVOCATION_FINISHED", "STAGE_FINISHED", "HANDOFF", "TERMINAL",
}
_FROZEN_EVENT_FIELDS = (
    "event_type", "sequence", "task_id", "correlation_id", "stage",
    "runtime_id", "status", "reason", "duration_ms",
)

_COMMAND = ["task text", "--step", "arch=rt-a", "--step", "dev=rt-b"]
_JSON_COMMAND = _COMMAND + ["--json"]

_STARTED = ExecutionEventType.INVOCATION_STARTED
_FINISHED = ExecutionEventType.INVOCATION_FINISHED
_HANDOFF = ExecutionEventType.HANDOFF
_TERMINAL = ExecutionEventType.TERMINAL


class RecordingSink:
    """Minimal structured sink: keeps the event objects (identity)."""

    def __init__(self):
        self.events = []

    def on_event(self, event):
        self.events.append(event)


class FailingSink:
    """on_event always raises: observation failure must not reach
    execution."""

    def on_event(self, event):
        raise RuntimeError("observation failure")


class FailingIndex:
    """observe always raises: store failure must not reach execution."""

    def observe(self, event):
        raise RuntimeError("index failure")


def _run(argv, adapters, *, verified=("rt-a", "rt-b"), sink=None,
         index=None, boundary_hook=None):
    return _run_cockpit(argv, adapters=adapters, verified=verified,
                        boundary_hook=boundary_hook,
                        observation_sink=sink, event_index=index)


def _traced_result(runtime, *, status=InvocationStatus.SUCCESS,
                   duration_ms=None):
    """A result with a full trace (identity + optional duration)."""
    return InvocationResult(
        status=status, output=f"out-{runtime}", error=None,
        trace=InvocationTrace(
            invocation_id=f"inv-{runtime}-1", task_id="t", agent_id="a",
            runtime=runtime, provider="prov-a", model=None, role="arch",
            status=status, duration_ms=duration_ms))


# ------------------------------------------------------ default zero drift


class DefaultZeroDriftTests(unittest.TestCase):

    def test_default_run_matches_current_contract(self):
        code, out, err = _run(_JSON_COMMAND, _standard_adapters())
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["status"], "COMPLETED")
        # Step identity still comes from the adapter traces only.
        self.assertEqual(payload["steps"][0]["invocation_id"], "inv-rt-a-1")
        self.assertEqual(payload["steps"][1]["invocation_id"], "inv-rt-b-1")

    def test_default_path_constructs_no_event_store(self):
        source = Path(cockpit_entry.__file__).read_text(encoding="utf-8")
        self.assertNotIn("EventIndex(", source)
        self.assertNotIn("ConsoleObservationSink", source)
        self.assertNotIn("ObservationSink(", source)


# --------------------------------------------------- order / fields / ids


class TwoStepEventOrderTests(unittest.TestCase):

    def test_event_order_and_strict_sequence(self):
        sink = RecordingSink()
        code, out, err = _run(_JSON_COMMAND, _standard_adapters(),
                              sink=sink)
        self.assertEqual(code, 0)
        self.assertEqual([e.event_type for e in sink.events],
                         [_STARTED, _FINISHED, _HANDOFF,
                          _STARTED, _FINISHED, _TERMINAL])
        self.assertEqual([e.sequence for e in sink.events],
                         [0, 1, 2, 3, 4, 5])
        self.assertEqual([e.status for e in sink.events],
                         ["STARTED", "SUCCESS", "EMBEDDED",
                          "STARTED", "SUCCESS", "COMPLETED"])

    def test_invocation_events_carry_composition_facts(self):
        sink = RecordingSink()
        code, out, err = _run(_JSON_COMMAND, _standard_adapters(),
                              sink=sink)
        payload = json.loads(out)
        first_started, first_finished = sink.events[0], sink.events[1]
        self.assertEqual(first_started.stage, "arch")
        self.assertEqual(first_started.runtime_id, "rt-a")
        self.assertEqual(first_finished.stage, "arch")
        self.assertEqual(first_finished.runtime_id, "rt-a")
        second_started = sink.events[3]
        self.assertEqual(second_started.stage, "dev")
        self.assertEqual(second_started.runtime_id, "rt-b")
        terminal = sink.events[5]
        self.assertEqual(terminal.stage, "SEQUENTIAL")
        self.assertEqual(terminal.runtime_id, "rt-b")  # last real runtime
        # Identity fields: events group under this run only.
        self.assertEqual(first_started.task_id, payload["task_id"])
        self.assertEqual(first_started.correlation_id,
                         f"cockpit-{payload['task_id']}")
        for event in sink.events:
            self.assertEqual(event.correlation_id,
                             first_started.correlation_id)

    def test_handoff_is_producing_stage_and_receiving_runtime(self):
        sink = RecordingSink()
        code, out, err = _run(_JSON_COMMAND, _standard_adapters(),
                              sink=sink)
        handoffs = [e for e in sink.events if e.event_type is _HANDOFF]
        self.assertEqual(len(handoffs), 1)
        self.assertEqual(handoffs[0].stage, "arch")        # producing role
        self.assertEqual(handoffs[0].runtime_id, "rt-b")  # receiving side
        self.assertEqual(handoffs[0].status, "EMBEDDED")
        self.assertEqual(handoffs[0].reason, "EMBEDDED")

    def test_events_carry_exactly_the_frozen_field_set(self):
        sink = RecordingSink()
        code, out, err = _run(_JSON_COMMAND, _standard_adapters(),
                              sink=sink)
        self.assertTrue(sink.events)
        for event in sink.events:
            self.assertEqual(tuple(event.to_dict().keys()),
                             _FROZEN_EVENT_FIELDS)

    def test_duration_passes_through_trace_only(self):
        sink = RecordingSink()
        adapters = (
            _ScriptedAdapter("rt-a", "prov-a", behaviors=[
                _traced_result("rt-a", duration_ms=1234)]),
            _ScriptedAdapter("rt-b", "prov-b"))
        code, out, err = _run(_JSON_COMMAND, adapters, sink=sink)
        self.assertEqual(code, 0)
        finished = [e for e in sink.events
                    if e.event_type is _FINISHED]
        self.assertEqual(finished[0].duration_ms, 1234)
        self.assertIsNone(finished[1].duration_ms)  # no trace duration


# ------------------------------------------------------- failure semantics


class InvocationFailureTests(unittest.TestCase):

    def test_failed_result_status_is_verbatim(self):
        sink = RecordingSink()
        adapters = (
            _ScriptedAdapter("rt-a", "prov-a", behaviors=[
                _traced_result("rt-a", status=InvocationStatus.FAILED)]),
            _ScriptedAdapter("rt-b", "prov-b"))
        code, out, err = _run(_JSON_COMMAND, adapters, sink=sink)
        self.assertEqual(code, 2)
        self.assertEqual([e.event_type for e in sink.events],
                         [_STARTED, _FINISHED, _TERMINAL])
        self.assertEqual([e.status for e in sink.events],
                         ["STARTED", "FAILED", "FAILED"])
        self.assertEqual(json.loads(out)["status"], "FAILED")

    def test_timeout_result_status_is_verbatim(self):
        sink = RecordingSink()
        adapters = (
            _ScriptedAdapter("rt-a", "prov-a", behaviors=[
                _traced_result("rt-a",
                               status=InvocationStatus.TIMEOUT)]),
            _ScriptedAdapter("rt-b", "prov-b"))
        code, out, err = _run(_JSON_COMMAND, adapters, sink=sink)
        self.assertEqual(code, 2)
        finished = [e for e in sink.events if e.event_type is _FINISHED]
        self.assertEqual(len(finished), 1)
        self.assertEqual(finished[0].status, "TIMEOUT")

    def test_raising_adapter_emits_no_finished(self):
        sink = RecordingSink()
        adapters = (
            _ScriptedAdapter("rt-a", "prov-a",
                             behaviors=[KeyError("boom")]),
            _ScriptedAdapter("rt-b", "prov-b"))
        code, out, err = _run(_JSON_COMMAND, adapters, sink=sink)
        self.assertEqual(code, 2)
        self.assertEqual([e.event_type for e in sink.events],
                         [_STARTED, _TERMINAL])
        self.assertEqual([e.status for e in sink.events],
                         ["STARTED", "FAILED"])
        payload = json.loads(out)
        self.assertEqual(payload["error"]["reason"], "KeyError")


# ----------------------------------------------------------- sink isolation


class ObservationIsolationTests(unittest.TestCase):

    def test_failing_sink_changes_nothing(self):
        clean_code, clean_out, _ = _run(_JSON_COMMAND,
                                        _standard_adapters())
        code, out, err = _run(_JSON_COMMAND, _standard_adapters(),
                              sink=FailingSink())
        self.assertEqual(code, clean_code)
        self.assertEqual(out, clean_out)

    def test_failing_index_changes_nothing(self):
        clean_code, clean_out, _ = _run(_JSON_COMMAND,
                                        _standard_adapters())
        code, out, err = _run(_JSON_COMMAND, _standard_adapters(),
                              index=FailingIndex())
        self.assertEqual(code, clean_code)
        self.assertEqual(out, clean_out)


# -------------------------------------------------------- EventIndex wiring


class EventIndexIntegrationTests(unittest.TestCase):

    def test_snapshot_and_since_incremental(self):
        index = EventIndex()
        code, out, err = _run(_JSON_COMMAND, _standard_adapters(),
                              index=index)
        self.assertEqual(code, 0)
        task_id = json.loads(out)["task_id"]
        snapshot = index.snapshot(task_id)
        self.assertEqual(len(snapshot), 6)
        self.assertEqual([e.sequence for e in snapshot], [0, 1, 2, 3, 4, 5])
        self.assertEqual([e.sequence for e in index.since(task_id, 2)],
                         [3, 4, 5])
        self.assertEqual(index.since(task_id, 5), ())

    def test_sink_and_index_receive_the_same_event_objects(self):
        sink, index = RecordingSink(), EventIndex()
        code, out, err = _run(_JSON_COMMAND, _standard_adapters(),
                              sink=sink, index=index)
        task_id = json.loads(out)["task_id"]
        stored = index.snapshot(task_id)
        self.assertEqual(len(stored), len(sink.events))
        for delivered, stored_event in zip(sink.events, stored):
            self.assertIs(delivered, stored_event)


# ------------------------------------------------------- terminal outcomes


class TerminalOutcomeTests(unittest.TestCase):

    def test_parked_terminal_comes_from_real_outcome(self):
        sink = RecordingSink()
        code, out, err = _run(_JSON_COMMAND, _standard_adapters(),
                              sink=sink, boundary_hook=_pause_hook)
        self.assertEqual(code, 4)
        self.assertEqual([e.event_type for e in sink.events], [_TERMINAL])
        terminal = sink.events[0]
        self.assertEqual(terminal.status, "PARKED")
        self.assertEqual(terminal.reason, "PARKED")
        self.assertEqual(terminal.runtime_id, "ORCHESTRATION")
        self.assertEqual(json.loads(out)["status"], "PARKED")

    def test_aborted_terminal_comes_from_real_outcome(self):
        sink = RecordingSink()
        code, out, err = _run(_JSON_COMMAND, _standard_adapters(),
                              sink=sink, boundary_hook=_abort_hook)
        self.assertEqual(code, 3)
        self.assertEqual([e.event_type for e in sink.events], [_TERMINAL])
        self.assertEqual(sink.events[0].status, "ABORTED")
        self.assertEqual(json.loads(out)["status"], "ABORTED")


# -------------------------------------------------------- sequence scoping


class SequenceScopeTests(unittest.TestCase):

    def test_each_execution_restarts_at_zero(self):
        first, second = RecordingSink(), RecordingSink()
        code_one, _, _ = _run(_JSON_COMMAND, _standard_adapters(),
                              sink=first)
        code_two, _, _ = _run(_JSON_COMMAND, _standard_adapters(),
                              sink=second)
        self.assertEqual((code_one, code_two), (0, 0))
        self.assertEqual([e.sequence for e in first.events],
                         [0, 1, 2, 3, 4, 5])
        self.assertEqual([e.sequence for e in second.events],
                         [0, 1, 2, 3, 4, 5])
        self.assertIsNot(first.events[0], second.events[0])


# --------------------------------------------- neutrality / vocabulary locks


class RuntimeNeutralityTests(unittest.TestCase):

    def setUp(self):
        self.source = Path(cockpit_entry.__file__).read_text(
            encoding="utf-8")

    def test_no_runtime_or_remote_branching(self):
        banned = (
            "claude", "codex", "pi-cli", "gemini", "qwen", "opencode",
            "cline", "tiny-agents",
            "RemoteAgentSession", "RemoteAgentEndpoint", "RemoteEnvelope",
            "remote_transport", "isinstance(adapter", "isinstance(raw",
            "isinstance(self._raw",
            # Branching on identity strings (runtime/agent/stage) is the
            # realistic neutrality risk; plain argv string parsing keeps
            # its startswith idiom.
            "runtime_id.startswith", "agent_id.startswith",
            "stage.startswith",
        )
        for token in banned:
            self.assertNotIn(token, self.source)

    def test_event_vocabulary_is_the_frozen_seven(self):
        self.assertEqual(
            {member.value for member in ExecutionEventType},
            _FROZEN_EVENT_TYPES)

    def test_no_synthetic_event_types_emitted(self):
        sink = RecordingSink()
        code, out, err = _run(_JSON_COMMAND, _standard_adapters(),
                              sink=sink)
        emitted = {e.event_type.value for e in sink.events}
        self.assertEqual(
            emitted,
            {"INVOCATION_STARTED", "INVOCATION_FINISHED", "HANDOFF",
             "TERMINAL"})

    def test_any_duck_type_adapter_is_covered(self):
        """Remote-bridge compatibility proof: an adapter that only
        satisfies invoke(request) -> InvocationResult (no base class,
        no known type) flows through the same observation wiring."""

        class ForeignAdapter:
            def __init__(self):
                self.profile = _FakeProfile("foreign-rt", "foreign-prov")

            def invoke(self, request):
                return _traced_result("foreign-rt")

        sink = RecordingSink()
        adapters = (ForeignAdapter(), _ScriptedAdapter("rt-b", "prov-b"))
        duck_command = ["task text", "--step", "arch=foreign-rt",
                        "--step", "dev=rt-b", "--json"]
        code, out, err = _run(duck_command, adapters,
                              verified=("foreign-rt", "rt-b"), sink=sink)
        self.assertEqual(code, 0)
        self.assertEqual([e.event_type for e in sink.events],
                         [_STARTED, _FINISHED, _HANDOFF,
                          _STARTED, _FINISHED, _TERMINAL])
        self.assertEqual(sink.events[0].runtime_id, "foreign-rt")
        self.assertEqual(json.loads(out)["steps"][0]["invocation_id"],
                         "inv-foreign-rt-1")


if __name__ == "__main__":
    unittest.main()
