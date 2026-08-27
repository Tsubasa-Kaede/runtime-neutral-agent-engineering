"""Phase 10H-D: CollaborationSession — real dual-agent collaboration seam.

Three layers: offline unit (failure mapping, budget/guard discipline),
mock dual-agent E2E (full A->B->A contract chain with fake adapters),
and one gated real smoke (two real model calls under
RUN_REAL_PROVIDER_TESTS=1, single runtime, role-qualified addresses).
"""
import json
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
from collaboration_session import (
    CollaborationOutcome,
    CollaborationSession,
    CollaborationStatus,
    collab_agent_address,
)
from external_runtime import InvocationResult, InvocationStatus, InvocationTrace
from remote_transport import LoopbackRemoteTransport, RemoteExchangeError
from structured_packets import ArchitecturePacket, ImplementationPacket
from task_budget import BudgetUsage, TaskBudget
from loop_guard import LoopGuard
from verified_selection_bridge import agent_id_for

IDENTITY = ("rt-x", "provider-x", "model-x", "fp-x")
ARCHITECT_ADDRESS = agent_id_for(IDENTITY + ("architect",))
CODER_ADDRESS = agent_id_for(IDENTITY + ("coder",))

SECRET_MARKERS = ("token", "secret", "api_key", "authorization", "bearer", "stdout", "stderr")

TASK = "Design a tiny deterministic slug utility function."


def arch_dict(task_id="T1"):
    return {
        "task_id": task_id, "role": "architect",
        "goal": ["Provide a slug helper"],
        "constraints": ["Pure function only"],
        "architecture": ["One module-level function"],
        "interfaces": [{"name": "slug", "params": ["text"], "returns": "str"}],
        "implementation_steps": [{"step": 1, "action": "implement slug"}],
        "acceptance_criteria": ["lowercase hyphenated output"],
        "risks": [{"risk": "none", "mitigation": "n/a"}],
    }


def impl_dict(task_id="T1"):
    return {
        "task_id": task_id, "role": "coder",
        "changed_files": ["slug_utils.py"],
        "implementation_summary": "Implemented slug per packet",
        "implementation_details": ["def slug(text): ..."],
        "assumptions": [],
        "unresolved_items": [],
        "test_requirements": ["slug output is lowercase"],
    }


def trace(status=InvocationStatus.SUCCESS, exit_code=0, duration_ms=500):
    return InvocationTrace(
        invocation_id="inv-1", task_id="T1", agent_id="agent", runtime="rt",
        provider=None, model=None, role=None, status=status,
        started_at=1.0, finished_at=2.0, duration_ms=duration_ms,
        exit_code=exit_code, input_tokens="unknown", output_tokens="unknown",
        error=None,
    )


def success_output(payload_dict):
    return InvocationResult(InvocationStatus.SUCCESS, output=json.dumps(payload_dict),
                            trace=trace())


class FakeAgentAdapter:
    def __init__(self, results):
        self.results = list(results)
        self.requests = []

    def invoke(self, request):
        self.requests.append(request)
        return self.results.pop(0)


def session_with(adapters, transport=None):
    return CollaborationSession(
        transport or LoopbackRemoteTransport(),
        adapters,
        TaskBudget(4, 4, timeout_seconds=30.0),
        BudgetUsage(),
        LoopGuard(),
    )


def green_adapters():
    return {
        ARCHITECT_ADDRESS: FakeAgentAdapter([success_output(arch_dict())]),
        CODER_ADDRESS: FakeAgentAdapter([success_output(impl_dict())]),
    }


def run_green(session=None, **kwargs):
    values = dict(task_id="T1", task=TASK,
                  architect_address=ARCHITECT_ADDRESS, coder_address=CODER_ADDRESS,
                  correlation_id="C001", provenance="OFFLINE", runtime_mode="SINGLE_RUNTIME")
    values.update(kwargs)
    return (session or session_with(green_adapters())).run(**values)


class ContractTests(unittest.TestCase):
    def test_status_members_and_values(self):
        self.assertEqual(
            {member.name for member in CollaborationStatus},
            {"SUCCESS", "ARCHITECT_INVOKE_FAILED", "ARCHITECT_PACKET_INVALID",
             "CODER_INVOKE_FAILED", "CODER_PACKET_INVALID", "TRANSPORT_FAILED",
             "CORRELATION_MISMATCH", "BUDGET_EXHAUSTED", "LOOP_GUARD_REJECTED"},
        )
        for member in CollaborationStatus:
            self.assertEqual(member.value, member.name)

    def test_agent_address_is_role_qualified_and_deterministic(self):
        self.assertEqual(collab_agent_address(IDENTITY, "architect"), ARCHITECT_ADDRESS)
        self.assertNotEqual(ARCHITECT_ADDRESS, CODER_ADDRESS)
        self.assertNotEqual(ARCHITECT_ADDRESS, agent_id_for(IDENTITY))
        self.assertEqual(collab_agent_address(IDENTITY, "architect"),
                         collab_agent_address(IDENTITY, "architect"))

    def test_outcome_field_set_is_frozen(self):
        names = {field.name for field in fields(CollaborationOutcome)}
        self.assertEqual(names, {"status", "task_id", "correlation_id", "runtime_mode",
                                 "request_envelope", "reply_envelope", "receipts", "traces"})
        with self.assertRaises(FrozenInstanceError):
            run_green().status = CollaborationStatus.LOOP_GUARD_REJECTED


class SuccessPathTests(unittest.TestCase):
    def test_full_chain_succeeds_with_two_envelopes(self):
        adapters = green_adapters()
        outcome = session_with(adapters).run(
            task_id="T1", task=TASK,
            architect_address=ARCHITECT_ADDRESS, coder_address=CODER_ADDRESS,
            correlation_id="C001", provenance="OFFLINE", runtime_mode="SINGLE_RUNTIME")
        self.assertEqual(outcome.status, CollaborationStatus.SUCCESS)
        self.assertEqual(outcome.correlation_id, "C001")
        self.assertEqual(outcome.runtime_mode, "SINGLE_RUNTIME")
        self.assertEqual(outcome.request_envelope.payload_type,
                         CollaborationPayloadType.ARCHITECTURE)
        self.assertEqual(outcome.reply_envelope.payload_type,
                         CollaborationPayloadType.IMPLEMENTATION)
        self.assertEqual(len(outcome.receipts), 2)
        self.assertTrue(all(receipt.status.value == "DELIVERED" for receipt in outcome.receipts))
        self.assertEqual(len(outcome.traces), 2)

    def test_budget_records_exactly_two_calls(self):
        usage = BudgetUsage()
        CollaborationSession(LoopbackRemoteTransport(), green_adapters(),
                             TaskBudget(4, 4, timeout_seconds=30.0), usage, LoopGuard()).run(
            task_id="T1", task=TASK,
            architect_address=ARCHITECT_ADDRESS, coder_address=CODER_ADDRESS,
            correlation_id="C001")
        self.assertEqual(usage.total_agent_calls, 2)

    def test_guard_allows_both_stages_and_records_them(self):
        session = session_with(green_adapters())
        first = session.run(task_id="T1", task=TASK,
                            architect_address=ARCHITECT_ADDRESS, coder_address=CODER_ADDRESS,
                            correlation_id="C001")
        self.assertEqual(first.status, CollaborationStatus.SUCCESS)
        second = session.run(task_id="T1", task=TASK,
                             architect_address=ARCHITECT_ADDRESS, coder_address=CODER_ADDRESS,
                             correlation_id="C002")
        self.assertEqual(second.status, CollaborationStatus.LOOP_GUARD_REJECTED)

    def test_envelope_criteria_come_from_packets(self):
        outcome = run_green()
        self.assertEqual(outcome.request_envelope.acceptance_criteria,
                         tuple(arch_dict()["acceptance_criteria"]))
        self.assertEqual(outcome.reply_envelope.acceptance_criteria,
                         tuple(impl_dict()["test_requirements"]))

    def test_outcome_repr_stays_clean(self):
        outcome = run_green()
        # Contract-owned surface: the session's fields, both envelopes and
        # the receipts. (Traces come from the pre-existing invocation
        # record whose FIELD names input_tokens/output_tokens contain the
        # marker substring; their error VALUES are checked below.)
        surface = (repr(outcome.status) + repr(outcome.request_envelope)
                   + repr(outcome.reply_envelope) + repr(outcome.receipts)
                   + outcome.task_id + outcome.correlation_id
                   + outcome.runtime_mode).lower()
        for marker in SECRET_MARKERS:
            self.assertNotIn(marker, surface)
        for trace_ in outcome.traces:
            error_text = (trace_.error or "").lower()
            for marker in SECRET_MARKERS:
                self.assertNotIn(marker, error_text)


class FailureMappingTests(unittest.TestCase):
    def check(self, adapters, expected, usage_expected, coder_calls=0, **run_kwargs):
        session = session_with(adapters)
        outcome = session.run(task_id="T1", task=TASK,
                              architect_address=ARCHITECT_ADDRESS, coder_address=CODER_ADDRESS,
                              correlation_id="C001", **run_kwargs)
        self.assertEqual(outcome.status, expected)
        self.assertEqual(session.usage.total_agent_calls, usage_expected)
        coder = session.adapters.get(CODER_ADDRESS)
        if coder is not None and coder_calls == 0:
            self.assertEqual(coder.requests, [])
        return outcome

    def test_architect_invoke_failure_short_circuits(self):
        adapters = green_adapters()
        adapters[ARCHITECT_ADDRESS] = FakeAgentAdapter(
            [InvocationResult(InvocationStatus.FAILED, error="boom", trace=trace(
                InvocationStatus.FAILED, exit_code=1))])
        outcome = self.check(adapters, CollaborationStatus.ARCHITECT_INVOKE_FAILED, 1)
        self.assertIsNone(outcome.reply_envelope)

    def test_architect_packet_invalid_is_never_forwarded(self):
        adapters = green_adapters()
        adapters[ARCHITECT_ADDRESS] = FakeAgentAdapter(
            [InvocationResult(InvocationStatus.SUCCESS, output="free text, not json",
                              trace=trace())])
        outcome = self.check(adapters, CollaborationStatus.ARCHITECT_PACKET_INVALID, 1)
        self.assertIsNone(outcome.request_envelope)

    def test_coder_invoke_failure(self):
        adapters = green_adapters()
        adapters[CODER_ADDRESS] = FakeAgentAdapter(
            [InvocationResult(InvocationStatus.TIMEOUT, error="timeout",
                              trace=trace(InvocationStatus.TIMEOUT, exit_code=None))])
        self.check(adapters, CollaborationStatus.CODER_INVOKE_FAILED, 2, coder_calls=1)
        self.assertEqual(len(adapters[ARCHITECT_ADDRESS].requests), 1)

    def test_coder_packet_invalid(self):
        adapters = green_adapters()
        adapters[CODER_ADDRESS] = FakeAgentAdapter(
            [InvocationResult(InvocationStatus.SUCCESS, output={"not": "a packet"},
                              trace=trace())])
        self.check(adapters, CollaborationStatus.CODER_PACKET_INVALID, 2, coder_calls=1)

    def test_transport_failure_on_first_send(self):
        def failing(target_agent, wire, sink):
            raise RemoteExchangeError("REMOTE_UNAVAILABLE")

        adapters = green_adapters()
        session = CollaborationSession(LoopbackRemoteTransport(failing), adapters,
                                       TaskBudget(4, 4, timeout_seconds=30.0),
                                       BudgetUsage(), LoopGuard())
        outcome = session.run(task_id="T1", task=TASK,
                              architect_address=ARCHITECT_ADDRESS, coder_address=CODER_ADDRESS,
                              correlation_id="C001")
        self.assertEqual(outcome.status, CollaborationStatus.TRANSPORT_FAILED)
        self.assertEqual(outcome.receipts[0].status.value, "REMOTE_UNAVAILABLE")
        self.assertEqual(session.usage.total_agent_calls, 1)
        self.assertEqual(adapters[CODER_ADDRESS].requests, [])

    def test_transport_drop_means_receive_none(self):
        def dropping(target_agent, wire, sink):
            return None

        adapters = green_adapters()
        session = CollaborationSession(LoopbackRemoteTransport(dropping), adapters,
                                       TaskBudget(4, 4, timeout_seconds=30.0),
                                       BudgetUsage(), LoopGuard())
        outcome = session.run(task_id="T1", task=TASK,
                              architect_address=ARCHITECT_ADDRESS, coder_address=CODER_ADDRESS,
                              correlation_id="C001")
        self.assertEqual(outcome.status, CollaborationStatus.TRANSPORT_FAILED)

    def test_correlation_mismatch_on_coder_side(self):
        def mismatch(target_agent, wire, sink):
            if target_agent == CODER_ADDRESS:
                wrong = CollaborationPacket(
                    correlation_id="C999", task_id="T1",
                    source_agent=ARCHITECT_ADDRESS, target_agent=CODER_ADDRESS,
                    source_role="architect", target_role="coder",
                    payload_type=CollaborationPayloadType.ARCHITECTURE,
                    payload=ArchitecturePacket.from_dict(arch_dict()),
                )
                sink(target_agent, serialize_collaboration_packet(wrong))
                return
            sink(target_agent, wire)

        adapters = green_adapters()
        session = CollaborationSession(LoopbackRemoteTransport(mismatch), adapters,
                                       TaskBudget(4, 4, timeout_seconds=30.0),
                                       BudgetUsage(), LoopGuard())
        outcome = session.run(task_id="T1", task=TASK,
                              architect_address=ARCHITECT_ADDRESS, coder_address=CODER_ADDRESS,
                              correlation_id="C001")
        self.assertEqual(outcome.status, CollaborationStatus.CORRELATION_MISMATCH)
        self.assertEqual(adapters[CODER_ADDRESS].requests, [])

    def test_correlation_mismatch_on_architect_side(self):
        def mismatch_reply(target_agent, wire, sink):
            if target_agent == ARCHITECT_ADDRESS:
                wrong_reply = CollaborationPacket(
                    correlation_id="C777", task_id="T1",
                    source_agent=CODER_ADDRESS, target_agent=ARCHITECT_ADDRESS,
                    source_role="coder", target_role="architect",
                    payload_type=CollaborationPayloadType.IMPLEMENTATION,
                    payload=ImplementationPacket.from_dict(impl_dict()),
                )
                sink(target_agent, serialize_collaboration_packet(wrong_reply))
                return
            sink(target_agent, wire)

        adapters = green_adapters()
        session = CollaborationSession(LoopbackRemoteTransport(mismatch_reply), adapters,
                                       TaskBudget(4, 4, timeout_seconds=30.0),
                                       BudgetUsage(), LoopGuard())
        outcome = session.run(task_id="T1", task=TASK,
                              architect_address=ARCHITECT_ADDRESS, coder_address=CODER_ADDRESS,
                              correlation_id="C001")
        self.assertEqual(outcome.status, CollaborationStatus.CORRELATION_MISMATCH)
        self.assertEqual(session.usage.total_agent_calls, 2)

    def test_budget_exhausted_before_any_call(self):
        budget = TaskBudget(1, 1, timeout_seconds=30.0)
        usage = BudgetUsage()
        usage.total_agent_calls = 1
        adapters = green_adapters()
        session = CollaborationSession(LoopbackRemoteTransport(), adapters, budget, usage,
                                       LoopGuard())
        outcome = session.run(task_id="T1", task=TASK,
                              architect_address=ARCHITECT_ADDRESS, coder_address=CODER_ADDRESS,
                              correlation_id="C001")
        self.assertEqual(outcome.status, CollaborationStatus.BUDGET_EXHAUSTED)
        self.assertEqual(adapters[ARCHITECT_ADDRESS].requests, [])

    def test_loop_guard_rejection_on_architect_stage(self):
        guard = LoopGuard()
        guard.record("T1", "architect", ARCHITECT_ADDRESS)
        adapters = green_adapters()
        session = CollaborationSession(LoopbackRemoteTransport(), adapters,
                                       TaskBudget(4, 4, timeout_seconds=30.0),
                                       BudgetUsage(), guard)
        outcome = session.run(task_id="T1", task=TASK,
                              architect_address=ARCHITECT_ADDRESS, coder_address=CODER_ADDRESS,
                              correlation_id="C001")
        self.assertEqual(outcome.status, CollaborationStatus.LOOP_GUARD_REJECTED)
        self.assertEqual(adapters[ARCHITECT_ADDRESS].requests, [])

    def test_loop_guard_rejection_on_coder_stage(self):
        guard = LoopGuard()
        guard.record("T1", "coder", CODER_ADDRESS)
        adapters = green_adapters()
        session = CollaborationSession(LoopbackRemoteTransport(), adapters,
                                       TaskBudget(4, 4, timeout_seconds=30.0),
                                       BudgetUsage(), guard)
        outcome = session.run(task_id="T1", task=TASK,
                              architect_address=ARCHITECT_ADDRESS, coder_address=CODER_ADDRESS,
                              correlation_id="C001")
        self.assertEqual(outcome.status, CollaborationStatus.LOOP_GUARD_REJECTED)
        self.assertEqual(len(adapters[ARCHITECT_ADDRESS].requests), 1)
        self.assertEqual(adapters[CODER_ADDRESS].requests, [])


class OutputNormalizationTests(unittest.TestCase):
    def test_scalar_list_field_is_conservatively_normalized(self):
        data = arch_dict()
        data["goal"] = "Provide a slug helper"  # scalar where a list is required
        adapters = green_adapters()
        adapters[ARCHITECT_ADDRESS] = FakeAgentAdapter([success_output(data)])
        outcome = session_with(adapters).run(
            task_id="T1", task=TASK,
            architect_address=ARCHITECT_ADDRESS, coder_address=CODER_ADDRESS,
            correlation_id="C001")
        self.assertEqual(outcome.status, CollaborationStatus.SUCCESS)
        self.assertEqual(outcome.request_envelope.payload.goal,
                         ("Provide a slug helper",))

    def test_fenced_json_output_is_accepted(self):
        fenced = "```json\n" + json.dumps(arch_dict()) + "\n```"
        adapters = green_adapters()
        adapters[ARCHITECT_ADDRESS] = FakeAgentAdapter(
            [InvocationResult(InvocationStatus.SUCCESS, output=fenced, trace=trace())])
        outcome = session_with(adapters).run(
            task_id="T1", task=TASK,
            architect_address=ARCHITECT_ADDRESS, coder_address=CODER_ADDRESS,
            correlation_id="C001")
        self.assertEqual(outcome.status, CollaborationStatus.SUCCESS)

    def test_model_invented_task_id_is_normalized_to_session_task_id(self):
        data = arch_dict(task_id="model-invented-001")
        adapters = green_adapters()
        adapters[ARCHITECT_ADDRESS] = FakeAgentAdapter([success_output(data)])
        outcome = session_with(adapters).run(
            task_id="T1", task=TASK,
            architect_address=ARCHITECT_ADDRESS, coder_address=CODER_ADDRESS,
            correlation_id="C001")
        self.assertEqual(outcome.status, CollaborationStatus.SUCCESS)
        self.assertEqual(outcome.request_envelope.payload.task_id, "T1")
        self.assertEqual(outcome.request_envelope.task_id, "T1")


class MockE2ETests(unittest.TestCase):
    def test_coder_receives_packet_json_not_raw_task(self):
        adapters = green_adapters()
        session_with(adapters).run(
            task_id="T1", task=TASK,
            architect_address=ARCHITECT_ADDRESS, coder_address=CODER_ADDRESS,
            correlation_id="C001", provenance="OFFLINE", runtime_mode="SINGLE_RUNTIME")
        coder_prompt = adapters[CODER_ADDRESS].requests[0].prompt
        self.assertIn('"goal"', coder_prompt)          # the packet wire is embedded
        self.assertNotIn(TASK, coder_prompt)           # the raw task text never reaches the coder

    def test_both_transport_directions_used_once_each(self):
        transport = LoopbackRemoteTransport()
        outcome = CollaborationSession(transport, green_adapters(),
                                       TaskBudget(4, 4, timeout_seconds=30.0),
                                       BudgetUsage(), LoopGuard()).run(
            task_id="T1", task=TASK,
            architect_address=ARCHITECT_ADDRESS, coder_address=CODER_ADDRESS,
            correlation_id="C001")
        self.assertEqual(outcome.status, CollaborationStatus.SUCCESS)
        self.assertEqual(len(outcome.receipts), 2)
        self.assertIsNone(transport.receive(ARCHITECT_ADDRESS))
        self.assertIsNone(transport.receive(CODER_ADDRESS))

    def test_correlation_consistent_across_the_whole_chain(self):
        outcome = run_green(correlation_id="C4242")
        self.assertEqual(outcome.correlation_id, "C4242")
        self.assertEqual(outcome.request_envelope.correlation_id, "C4242")
        self.assertEqual(outcome.reply_envelope.correlation_id, "C4242")

    def test_provenance_offline_by_default(self):
        outcome = run_green()
        self.assertEqual(outcome.request_envelope.provenance, "OFFLINE")
        self.assertEqual(outcome.reply_envelope.provenance, "OFFLINE")

    def test_coder_request_carries_architect_packet_wire(self):
        adapters = green_adapters()
        session_with(adapters).run(
            task_id="T1", task=TASK,
            architect_address=ARCHITECT_ADDRESS, coder_address=CODER_ADDRESS,
            correlation_id="C001")
        coder_prompt = adapters[CODER_ADDRESS].requests[0].prompt
        self.assertIn('"packet_type":"ArchitecturePacket"', coder_prompt)


class SourceScanTests(unittest.TestCase):
    def test_no_runtime_names_or_forbidden_channels(self):
        import collaboration_session as module
        source = Path(module.__file__).read_text(encoding="utf-8")
        lowered = source.lower()
        for name in ("claude", "codex", "deepseek", "openai", "anthropic",
                     "gemini", "tiny-agents", "tiny_agents"):
            self.assertNotIn(name, lowered)
        for forbidden in ("os.environ", "getenv", "RUN_REAL_PROVIDER_TESTS",
                          "subprocess", "requests", "urllib", "socket",
                          "http", "websocket", "a2a", "async", "threading",
                          "uuid", "random", "datetime"):
            self.assertNotIn(forbidden, source)


class RealDualAgentSmokeTests(unittest.TestCase):
    def setUp(self):
        import os
        if os.environ.get("RUN_REAL_PROVIDER_TESTS", "") != "1":
            self.skipTest("RUN_REAL_PROVIDER_TESTS != 1")

    def test_real_architect_coder_collaboration_single_runtime(self):
        import os
        from claude_code_adapter import ClaudeCodeAdapter

        adapter = ClaudeCodeAdapter.from_environment()
        if adapter is None:
            self.skipTest("claude executable not found")

        identity = ("claude-cli", "anthropic", None, "fp-10hd-real")
        architect = collab_agent_address(identity, "architect")
        coder = collab_agent_address(identity, "coder")
        budget = TaskBudget(4, 4, timeout_seconds=300.0)
        usage = BudgetUsage()
        protected = tuple(
            Path.home().joinpath(*part) for part in (
                (".claude", ".credentials.json"), (".claude.json",),
                (".claude", "settings.json"), (".codex", "auth.json"),
                (".codex", "config.toml"))
        )
        before = {p: (p.stat().st_mtime_ns, p.stat().st_size) for p in protected if p.exists()}

        outcome = CollaborationSession(
            LoopbackRemoteTransport(),
            {architect: adapter, coder: adapter},
            budget, usage, LoopGuard(),
        ).run(
            task_id="T-real-1",
            task="设计一个简单字符串工具函数，并给出实现方案和实现结果。",
            architect_address=architect, coder_address=coder,
            correlation_id="10hd-real-smoke-1",
            provenance="REAL", runtime_mode="SINGLE_RUNTIME",
        )
        print("REAL_OUTCOME_STATUS:", outcome.status.value)
        if outcome.request_envelope is not None and outcome.reply_envelope is not None:
            print("REAL_PROVENANCE:", outcome.request_envelope.provenance,
                  outcome.reply_envelope.provenance)
        else:
            print("REAL_PROVENANCE: envelopes absent (failed before wrapping)")
        self.assertEqual(outcome.status, CollaborationStatus.SUCCESS)
        self.assertIsInstance(outcome.request_envelope.payload, ArchitecturePacket)
        self.assertIsInstance(outcome.reply_envelope.payload, ImplementationPacket)
        self.assertEqual(outcome.request_envelope.correlation_id, "10hd-real-smoke-1")
        self.assertEqual(outcome.reply_envelope.correlation_id, "10hd-real-smoke-1")
        self.assertEqual(outcome.request_envelope.provenance, "REAL")
        self.assertEqual(outcome.reply_envelope.provenance, "REAL")
        self.assertEqual(outcome.runtime_mode, "SINGLE_RUNTIME")
        self.assertEqual(usage.total_agent_calls, 2)
        self.assertEqual(len(outcome.traces), 2)
        for trace_ in outcome.traces:
            self.assertEqual(trace_.exit_code, 0)
        surface = (repr(outcome.status) + repr(outcome.request_envelope)
                   + repr(outcome.reply_envelope) + repr(outcome.receipts)
                   + outcome.task_id + outcome.correlation_id
                   + outcome.runtime_mode).lower()
        for marker in SECRET_MARKERS:
            self.assertNotIn(marker, surface)
        for trace_ in outcome.traces:
            error_text = (trace_.error or "").lower()
            for marker in SECRET_MARKERS:
                self.assertNotIn(marker, error_text)
        after = {p: (p.stat().st_mtime_ns, p.stat().st_size) for p in protected if p.exists()}
        self.assertEqual(before, after)
        self.assertEqual(adapter._processes, {})


if __name__ == "__main__":
    unittest.main()
