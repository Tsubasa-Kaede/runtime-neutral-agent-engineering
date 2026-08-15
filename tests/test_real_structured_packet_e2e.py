"""Structured Packet E2E: offline rehearsal + real single-runtime entry.

Real path (opt-in, RUN_REAL_PROVIDER_TESTS == "1"): four genuine Claude Code
invocations produce the four packets; the harness acts as controller only
(writes coder output to a temp workspace, really executes the tester's
declared commands, and cross-checks them against the model's claims).
"""
import dataclasses
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from capability_registry import (
    AgentProfile,
    CapabilityConfidence,
    CapabilityEvidence,
    CapabilityName,
    CapabilityRegistry,
)
from dual_agent_selection import DecisionReason, DualAgentMode, DualAgentSelectionResult
from external_runtime import InvocationResult, InvocationStatus
from orchestrator import Orchestrator
from runtime_status import HealthEvidence, ReasonCode, RuntimeState, RuntimeStatus
from structured_packets import (
    ArchitecturePacket,
    ImplementationPacket,
    ReviewPacket,
    TestPacket,
    deserialize_packet,
    serialize_packet,
)
from task_budget import BudgetUsage, TaskBudget
from loop_guard import LoopGuard
from execution_engine import ExecutionStatus

RUN_REAL_PROVIDER_TESTS = os.environ.get("RUN_REAL_PROVIDER_TESTS") == "1"


def runtime(rid, state=RuntimeState.READY, provider="p", model="m"):
    return RuntimeStatus(rid, rid + ".exe", "1", state, provider, model, "managed", ReasonCode.NONE, HealthEvidence("v", "v", "v", "v", "v"), 1, 100)


class PacketStageAdapters:
    """Mock adapters that rehearse the real E2E: each stage consumes its
    structured handoff input, does real local work in the workspace, and
    emits a validated packet. No runtime or provider is invoked."""

    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.requests_by_role: dict[str, list] = {}
        self.adapters: dict[str, Mock] = {}

    def build(self, agent_id: str):
        adapter = Mock()
        adapter.invoke.side_effect = lambda request: self._dispatch(request)
        self.adapters[agent_id] = adapter
        return adapter

    def _dispatch(self, request):
        self.requests_by_role.setdefault(request.role, []).append(request)
        trace = Mock(
            invocation_id=f"inv-{request.role}-{len(self.requests_by_role[request.role])}",
            input_tokens="unknown",
            output_tokens="unknown",
        )
        if request.role == "architect":
            return InvocationResult(InvocationStatus.SUCCESS, self._architect(request.task_id), trace=trace)
        if request.role == "coder":
            return InvocationResult(InvocationStatus.SUCCESS, self._coder(request), trace=trace)
        if request.role == "test":
            return InvocationResult(InvocationStatus.SUCCESS, self._tester(request), trace=trace)
        return InvocationResult(InvocationStatus.SUCCESS, self._reviewer(request), trace=trace)

    @staticmethod
    def _architect(task_id):
        return ArchitecturePacket(
            task_id, "architect",
            goal=("Implement calculator add function",),
            constraints=("single module calculator.py", "pure function"),
            architecture=("calculator.py exposes add(a, b)",),
            interfaces=({"name": "add", "inputs": "a:int, b:int", "outputs": "int"},),
            implementation_steps=({"id": "s1", "description": "write add in calculator.py", "files": ("calculator.py",)},),
            acceptance_criteria=("add(2, 3) == 5",),
            risks=({"description": "trivial scope", "mitigation": "none"},),
        )

    def _coder(self, request):
        handoff = getattr(request, "handoff_packets", ())
        assert isinstance(handoff, tuple) and len(handoff) == 1 and isinstance(handoff[0], ArchitecturePacket), \
            "coder must consume the ArchitecturePacket"
        (self.workspace / "calculator.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
        return ImplementationPacket(
            request.task_id, "coder",
            changed_files=("calculator.py",),
            implementation_summary="added add(a, b)",
            implementation_details=("single return expression",),
            assumptions=(), unresolved_items=(),
            test_requirements=("verify add(2,3)==5",),
        )

    def _tester(self, request):
        handoff = getattr(request, "handoff_packets", ())
        assert isinstance(handoff, tuple) and len(handoff) == 1 and isinstance(handoff[0], ImplementationPacket), \
            "tester must consume the ImplementationPacket"
        probe = subprocess.run(
            [sys.executable, "-c", "import calculator; assert calculator.add(2, 3) == 5"],
            cwd=self.workspace, capture_output=True, text=True, timeout=30,
        )
        passed = probe.returncode == 0
        return TestPacket(
            request.task_id, "tester",
            tests_run=("verify_add",),
            tests_passed=("verify_add",) if passed else (),
            tests_failed=() if passed else ("verify_add",),
            failures=() if passed else ({"test": "verify_add", "error_class": "AssertionError"},),
            coverage_or_validation=("local import+assert",),
            remaining_risks=(),
        )

    def _reviewer(self, request):
        handoff = getattr(request, "handoff_packets", ())
        assert isinstance(handoff, tuple) and len(handoff) == 3, "reviewer must consume three packets"
        assert isinstance(handoff[0], ArchitecturePacket)
        assert isinstance(handoff[1], ImplementationPacket)
        assert isinstance(handoff[2], TestPacket)
        return ReviewPacket(
            request.task_id, "reviewer", "PASS",
            findings=(), severity=(), affected_files=("calculator.py",),
            required_changes=(), acceptance_criteria_status=("add(2, 3) == 5: verified",),
        )


class OfflineStructuredPacketChainTests(unittest.TestCase):
    """Offline rehearsal of the real E2E — runs always, no runtime involved."""

    def setUp(self):
        self.workspace = tempfile.TemporaryDirectory()
        self.addCleanup(self.workspace.cleanup)
        self.root = Path(self.workspace.name)

    def test_full_packet_chain_with_handoff_inputs(self):
        stage_adapters = PacketStageAdapters(self.root)
        profiles = [AgentProfile(
            "claude-agent", "claude-cli", "anthropic", None, None,
            {cap: CapabilityEvidence(cap, 0.9, CapabilityConfidence.VERIFIED, "integration-test")
             for cap in (CapabilityName.ARCHITECTURE, CapabilityName.CODING, CapabilityName.TESTING, CapabilityName.REVIEW)},
            0.8,
        )]
        statuses = {"claude-cli": runtime("claude-cli", provider="anthropic", model=None)}
        budget = TaskBudget(4, 4)
        usage = BudgetUsage()
        guard = LoopGuard(max_iterations=4)
        selection = DualAgentSelectionResult(
            DualAgentMode.SINGLE_AGENT,
            {"architect": "claude-agent", "coder": "claude-agent", "test": "claude-agent", "review": "claude-agent"},
            "claude-agent", None, DecisionReason.SINGLE_CAPABLE_AGENT,
            ("architect=claude-agent", "coder=claude-agent", "test=claude-agent", "review=claude-agent"),
        )
        adapters = {"claude-agent": stage_adapters.build("claude-agent")}
        orchestrator = Orchestrator(CapabilityRegistry(profiles), statuses, budget, usage, guard)

        result = orchestrator.execute(
            "e2e-task", "redesign architecture across modules", adapters, "Implement calculator add.",
            dual_selection=selection,
        )

        # 1,5,10,13: four packets produced in order
        self.assertEqual(result.status, ExecutionStatus.SUCCESS)
        packets = result.packets
        self.assertEqual([type(p).__name__ for p in packets],
                         ["ArchitecturePacket", "ImplementationPacket", "TestPacket", "ReviewPacket"])

        # 2,6,11,14: each packet passes structured validation (round-trip through from_dict)
        for packet in packets:
            self.assertEqual(type(packet).from_dict(vars(packet)), packet)

        # 3,4: coder consumed the ArchitecturePacket via handoff input
        coder_request = stage_adapters.requests_by_role["coder"][0]
        self.assertEqual(coder_request.handoff_packets[0], packets[0])

        # 7,8: tester consumed the ImplementationPacket
        tester_request = stage_adapters.requests_by_role["test"][0]
        self.assertEqual(tester_request.handoff_packets[0], packets[1])

        # 9: tester actually executed the local verification and it passed
        self.assertEqual(packets[2].tests_passed, ("verify_add",))
        self.assertEqual(packets[2].tests_failed, ())

        # 12: reviewer consumed all three upstream packets in order
        reviewer_request = stage_adapters.requests_by_role["review"][0]
        self.assertEqual(reviewer_request.handoff_packets, (packets[0], packets[1], packets[2]))

        # 15: task_id consistent across all four packets
        task_ids = {p.task_id for p in packets}
        self.assertEqual(task_ids, {"e2e-task"})

        # 16: roles are correct per stage
        self.assertEqual([p.role for p in packets], ["architect", "coder", "tester", "reviewer"])

        # 17: serialize -> deserialize -> equality for one packet
        revived = deserialize_packet(serialize_packet(packets[0]))
        self.assertEqual(revived, packets[0])

        # 18: adapter traces preserved, four invocations
        self.assertEqual(len(result.traces), 4)
        self.assertTrue(all(trace.invocation_id for trace in result.traces))

        # 19: token values stay unknown (runtime provided none)
        self.assertTrue(all(trace.input_tokens == "unknown" and trace.output_tokens == "unknown" for trace in result.traces))

        # 20: one shared lifecycle budget across all stages
        self.assertEqual(usage.total_agent_calls, 4)
        self.assertEqual(usage.architect_calls, 1)
        self.assertEqual(usage.coder_calls, 1)
        self.assertEqual(usage.test_calls, 1)
        self.assertEqual(usage.review_calls, 1)

        # 21: every stage went through the loop guard
        self.assertNotEqual(guard.check("e2e-task", "architect", "claude-agent"), "ALLOW")
        self.assertNotEqual(guard.check("e2e-task", "coder", "claude-agent"), "ALLOW")
        self.assertNotEqual(guard.check("e2e-task", "test", "claude-agent"), "ALLOW")
        self.assertNotEqual(guard.check("e2e-task", "review", "claude-agent"), "ALLOW")

        # workspace really contains the implemented calculator
        self.assertEqual((self.root / "calculator.py").read_text(encoding="utf-8").count("def add"), 1)


def _extract_json_object(text: str) -> dict:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    start, end = stripped.find("{"), stripped.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in stage output")
    return json.loads(stripped[start:end + 1])


def _redact(text: str) -> str:
    text = re.sub("(?i)(api[-_ ]?key|token|secret|authorization)[^ ]*",
                  lambda m: m.group(1) + "[REDACTED]", text)
    return text[:120].replace(chr(10), " ")




_PACKET_TYPES = {
    "architect": ArchitecturePacket,
    "coder": ImplementationPacket,
    "test": TestPacket,
    "review": ReviewPacket,
}

# Known list-valued fields per packet type; real model output occasionally
# returns a bare string where the schema wants a one-element array.
_LIST_FIELDS = {
    "architect": ("goal", "constraints", "architecture", "interfaces", "implementation_steps", "acceptance_criteria", "risks"),
    "coder": ("changed_files", "implementation_details", "assumptions", "unresolved_items", "test_requirements"),
    "test": ("tests_run", "tests_passed", "tests_failed", "failures", "coverage_or_validation", "remaining_risks"),
    "review": ("findings", "severity", "affected_files", "required_changes", "acceptance_criteria_status"),
}


def _normalize_payload(role: str, payload: dict) -> dict:
    for field in _LIST_FIELDS.get(role, ()):
        value = payload.get(field)
        if isinstance(value, (str, int, float)):
            payload[field] = [value]
    return payload



class RealClaudeStageAdapter:
    """Wraps ClaudeCodeAdapter for one pipeline stage: builds the stage prompt
    (embedding serialized upstream packets), invokes the real runtime once,
    and parses the model output into the stage packet. Controller-side
    environment actions stay in the harness, not here."""

    def __init__(self, claude_adapter, role, prompt_builder, recorder, timeout_seconds=300.0):
        self.claude = claude_adapter
        self.role = role
        self.prompt_builder = prompt_builder
        self.recorder = recorder
        self.timeout_seconds = timeout_seconds

    def invoke(self, request):
        real_request = dataclasses.replace(request, prompt=self.prompt_builder(request), timeout_seconds=self.timeout_seconds)
        self.recorder.setdefault(request.role, []).append(real_request)
        result = self.claude.invoke(real_request)
        if result.status is not InvocationStatus.SUCCESS:
            return result
        try:
            payload = _extract_json_object(str(result.output))
        except (ValueError, json.JSONDecodeError):
            return InvocationResult(
                InvocationStatus.FAILED, None, f"stage {self.role}: output was not parseable JSON", result.trace)
        try:
            packet = _PACKET_TYPES[self.role].from_dict(_normalize_payload(self.role, payload))
        except Exception as exc:
            return InvocationResult(
                InvocationStatus.FAILED, None,
                f"stage {self.role}: packet validation failed: {_redact(str(exc))}", result.trace)
        return InvocationResult(InvocationStatus.SUCCESS, packet, None, result.trace)



@unittest.skipUnless(
    RUN_REAL_PROVIDER_TESTS,
    "Real Claude Code structured-packet E2E is opt-in; set RUN_REAL_PROVIDER_TESTS=1",
)
class RealStructuredPacketE2ETests(unittest.TestCase):
    TASK = "real-e2e-calculator"

    def setUp(self):
        workspace = tempfile.TemporaryDirectory()
        self.addCleanup(workspace.cleanup)
        self.root = Path(workspace.name)
        auth_paths = [Path.home() / ".codex" / "auth.json", Path.home() / ".codex" / "config.toml"]
        self.auth_before = {p: (p.stat().st_mtime_ns, p.stat().st_size) for p in auth_paths if p.exists()}

    def test_real_claude_packet_e2e(self):
        from claude_code_adapter import ClaudeCodeAdapter
        from runtime_status import AuthenticationState

        claude = ClaudeCodeAdapter.from_environment()
        if claude is None:
            self.fail("UNAVAILABLE: Claude Code executable not found")

        auth = claude.check_authentication()
        self.assertEqual(
            auth.state, AuthenticationState.AUTHENTICATED,
            f"Claude authentication state was {auth.state.value}; run official auth and retry",
        )

        recorder: dict[str, list] = {}

        def architect_prompt(request):
            return (
                "You are the ARCHITECT stage of a coding pipeline. The task: in a Python workspace, "
                "implement a calculator module with add(a, b) and a unittest that checks add(2, 3) == 5.\n"
                "Respond with ONLY a JSON object (no markdown fences, no extra text) with EXACTLY these keys:\n"
                f'"task_id": "{self.TASK}" (use this exact value)\n'
                '"role": "architect"\n'
                '"goal": [one-line goal]\n'
                '"constraints": [list of strings]\n'
                '"architecture": [list of design decision strings]\n'
                '"interfaces": [{"name": "add", "inputs": "a: int, b: int", "outputs": "int"}]\n'
                '"implementation_steps": [{"id": "s1", "description": "...", "files": ["calculator.py", "test_calculator.py"]}]\n'
                '"acceptance_criteria": ["add(2, 3) == 5 verified by test_calculator.py"]\n'
                '"risks": [{"description": "...", "mitigation": "..."}]\n'
                "All list values must be JSON arrays of strings/objects. JSON only."
            )

        def coder_prompt(request):
            arch_json = serialize_packet(request.handoff_packets[0])
            return (
                "You are the CODER stage of a coding pipeline. The architect produced this "
                "ArchitecturePacket; it is your input contract — consume it, do not redesign:\n"
                f"{arch_json}\n"
                "Write the complete implementation. Respond with ONLY a JSON object with EXACTLY these keys:\n"
                f'"task_id": "{self.TASK}"\n'
                '"role": "coder"\n'
                '"changed_files": ["calculator.py", "test_calculator.py"]\n'
                '"implementation_summary": "one line"\n'
                '"implementation_details": [TWO entries, one per file, each formatted exactly as '
                '"<relative path>:\\n<complete runnable file content>"]\n'
                '"assumptions": []\n"unresolved_items": []\n'
                '"test_requirements": ["python -m unittest test_calculator -v"]\n'
                "JSON only. File contents must be complete, syntactically valid Python."
            )

        def tester_prompt(request):
            impl_json = serialize_packet(request.handoff_packets[0])
            real_cmd, real_rc = self.tester_real
            real_outcome = "PASSED (exit code 0)" if real_rc == 0 else f"FAILED (exit code {real_rc})"
            return (
                "You are the TESTER stage of a coding pipeline. The coder produced this "
                "ImplementationPacket; consume it (file contents are inside implementation_details):\n"
                f"{impl_json}\n"
                f"The harness has ALREADY executed the test command for real in the workspace.\n"
                f"Command: {real_cmd}\n"
                f"Real outcome: {real_outcome}\n"
                "Your tests_passed / tests_failed MUST state exactly this real outcome.\n"
                "Respond with ONLY a JSON object with EXACTLY these keys:\n"
                f'"task_id": "{self.TASK}"\n'
                '"role": "tester"\n'
                f'"tests_run": ["{real_cmd}"]\n'
                '"tests_passed": [test names that really passed]\n'
                '"tests_failed": [test names that really failed]\n'
                '"failures": [] if passed, else [{"test": name, "error_class": "..."}]\n'
                '"coverage_or_validation": ["real execution of the test command in the workspace"]\n'
                '"remaining_risks": []\n'
                "JSON only."
            )

        def reviewer_prompt(request):
            arch, impl, test = request.handoff_packets
            return (
                "You are the REVIEWER stage of a coding pipeline. Consume these three upstream packets:\n"
                f"ArchitecturePacket: {serialize_packet(arch)}\n"
                f"ImplementationPacket: {serialize_packet(impl)}\n"
                f"TestPacket: {serialize_packet(test)}\n"
                "Respond with ONLY a JSON object with EXACTLY these keys:\n"
                f'"task_id": "{self.TASK}"\n'
                '"role": "reviewer"\n'
                '"status": "PASS" or "FAIL"\n'
                '"findings": []\n'
                '"severity": []\n'
                '"affected_files": ["calculator.py", "test_calculator.py"]\n'
                '"required_changes": []\n'
                '"acceptance_criteria_status": [one entry per acceptance criterion, stating verified or not]\n'
                "JSON only."
            )

        adapters = {
            "claude-agent": None,  # replaced by the routing adapter below
        }

        class RoutingAdapter:
            def __init__(self, harness):
                self.harness = harness
                self.harness.stage_outcomes = []

            def invoke(self, request):
                builders = {
                    "architect": architect_prompt,
                    "coder": coder_prompt,
                    "test": tester_prompt,
                    "review": reviewer_prompt,
                }
                if request.role == "test":
                    self.harness.pre_execute_tests(request)
                stage = RealClaudeStageAdapter(claude, request.role, builders[request.role], recorder)
                result = stage.invoke(request)
                self.harness.stage_outcomes.append((request.role, result.status.value, _redact(result.error or "")))
                self.harness.on_stage_done(request, result)
                return result

        adapters = {"claude-agent": RoutingAdapter(self)}

        profiles = [AgentProfile(
            "claude-agent", "claude-cli", "anthropic", None, None,
            {cap: CapabilityEvidence(cap, 0.9, CapabilityConfidence.VERIFIED, "real-e2e")
             for cap in (CapabilityName.ARCHITECTURE, CapabilityName.CODING, CapabilityName.TESTING, CapabilityName.REVIEW)},
            0.8,
        )]
        statuses = {"claude-cli": runtime("claude-cli", provider="anthropic", model=None)}
        budget = TaskBudget(4, 4)
        usage = BudgetUsage()
        guard = LoopGuard(max_iterations=4)
        selection = DualAgentSelectionResult(
            DualAgentMode.SINGLE_AGENT,
            {"architect": "claude-agent", "coder": "claude-agent", "test": "claude-agent", "review": "claude-agent"},
            "claude-agent", None, DecisionReason.SINGLE_CAPABLE_AGENT,
            ("architect=claude-agent", "coder=claude-agent", "test=claude-agent", "review=claude-agent"),
        )
        orchestrator = Orchestrator(CapabilityRegistry(profiles), statuses, budget, usage, guard)
        result = orchestrator.execute(
            self.TASK,
            "redesign architecture across modules: implement calculator add with a test",
            adapters, "Implement calculator add.",
            dual_selection=selection,
        )

        # --- stage-level assertions (fail with safe messages only) ---
        self.assertEqual(result.status, ExecutionStatus.SUCCESS, f"pipeline errors: {result.errors}; stages: {getattr(self, 'stage_outcomes', [])}")

        packets = result.packets
        self.assertEqual([type(p).__name__ for p in packets],
                         ["ArchitecturePacket", "ImplementationPacket", "TestPacket", "ReviewPacket"])

        # Handoff inputs actually reached each real invocation
        self.assertIsInstance(recorder["coder"][0].handoff_packets[0], ArchitecturePacket)
        self.assertEqual(recorder["coder"][0].handoff_packets[0], packets[0])
        self.assertIsInstance(recorder["test"][0].handoff_packets[0], ImplementationPacket)
        self.assertEqual(recorder["test"][0].handoff_packets[0], packets[1])
        self.assertEqual(
            recorder["review"][0].handoff_packets,
            (packets[0], packets[1], packets[2]),
        )
        coder_prompt_text = recorder["coder"][0].prompt
        self.assertNotIn("You are the ARCHITECT", coder_prompt_text)
        self.assertIn("ArchitecturePacket", coder_prompt_text)

        # task_id consistent, roles correct, validation passed (via construction)
        self.assertEqual({p.task_id for p in packets}, {self.TASK})
        self.assertEqual([p.role for p in packets], ["architect", "coder", "tester", "reviewer"])

        # serialization round-trip on a real model-produced packet
        self.assertEqual(deserialize_packet(serialize_packet(packets[0])), packets[0])

        # Real traces preserved; tokens unknown (runtime gave no usage data)
        self.assertEqual(len(result.traces), 4)
        self.assertTrue(all(t.invocation_id for t in result.traces))
        self.assertTrue(all(t.duration_ms is not None for t in result.traces))
        self.assertTrue(all(t.input_tokens == "unknown" and t.output_tokens == "unknown" for t in result.traces))

        # One shared lifecycle budget; loop guard visited every stage
        self.assertEqual(usage.total_agent_calls, 4)
        for stage in ("architect", "coder", "test", "review"):
            self.assertNotEqual(guard.check(self.TASK, stage, "claude-agent"), "ALLOW")

        # No real fallback ran: exactly four runtime invocations, single runtime
        self.assertEqual(sum(len(v) for v in recorder.values()), 4)
        self.assertTrue(all(r.agent_id == "claude-agent" for calls in recorder.values() for r in calls))

        # Tester's declared commands were really executed and matched its claims
        self.assertEqual(packets[2].tests_failed, ())
        # Reviewer verdict is the model's judgement (handoff is what we verify);
        # a FAIL verdict must at least carry structured findings/changes.
        self.assertIn(packets[3].status, ("PASS", "FAIL"))
        if packets[3].status == "FAIL":
            self.assertTrue(
                packets[3].findings or packets[3].required_changes,
                "FAIL review must carry structured findings or required changes",
            )

        # Security: auth/config untouched, no secrets in any packet surface
        auth_paths = [Path.home() / ".codex" / "auth.json", Path.home() / ".codex" / "config.toml"]
        auth_after = {p: (p.stat().st_mtime_ns, p.stat().st_size) for p in auth_paths if p.exists()}
        self.assertEqual(self.auth_before, auth_after)
        surface = repr(packets).lower()
        for marker in ("token", "secret", "api_key", "authorization"):
            self.assertNotIn(marker, surface)

    # Controller-side environment actions between real stages
    def pre_execute_tests(self, request):
        impl = request.handoff_packets[0]
        command = impl.test_requirements[0] if impl.test_requirements else "python -m unittest discover"
        completed = subprocess.run(
            command.split(), cwd=self.root, capture_output=True, text=True, timeout=120,
        )
        self.tester_real = (command, completed.returncode)

    def on_stage_done(self, request, result):
        from external_runtime import InvocationStatus as IS
        if result.status is not IS.SUCCESS:
            return
        packet = result.output
        if request.role == "coder":
            for entry in packet.implementation_details:
                path_part, content = entry.split(":\n", 1)
                target = self.root / path_part.strip()
                target.write_text(content.lstrip("\n"), encoding="utf-8")
        elif request.role == "test":
            for command in packet.tests_run:
                completed = subprocess.run(
                    command.split(), cwd=self.root, capture_output=True, text=True, timeout=120,
                )
                if completed.returncode != 0 and not packet.tests_failed:
                    self.fail(
                        "Tester declared all tests passed but the real execution of its own "
                        f"declared command failed with exit code {completed.returncode}"
                    )
                if completed.returncode == 0 and packet.tests_failed:
                    self.fail("Tester declared failures but its declared command really passed")


if __name__ == "__main__":
    unittest.main()
