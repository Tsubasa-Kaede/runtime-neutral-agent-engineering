"""Offline four-stage demonstration of the ProductionFacade.

Runs architect -> coder -> tester -> reviewer entirely offline with mock
adapters over a verified mock pool — no runtime, no credentials, no network.
It prints only the closed, secret-free facade summary, proving the real
ProductionFacade composes the four stages correctly.

From a fresh clone:   python examples/offline_mock_run.py
When installed:       the same script also works via the dual_agent package.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "dual-agent-development" / "scripts"))

from candidate_validation import (
    CandidateValidationResult,
    CandidateValidationStatus,
    GateResult,
    GateVerdict,
    ValidationGate,
)
from cli import render_summary
from collaboration_orchestrator import CollaborationOrchestrator
from collaboration_session import CollaborationSession, collab_agent_address
from execution_engine import ExecutionResult, ExecutionStatus
from external_runtime import InvocationResult, InvocationStatus, InvocationTrace
from loop_guard import LoopGuard
from mode_gate import Mode
from production_facade import ProductionFacade
from remote_transport import LoopbackRemoteTransport
from runtime_status import (
    HealthEvidence,
    ReasonCode,
    RuntimeState,
    RuntimeStatus,
)
from task_budget import BudgetUsage, TaskBudget
from verified_runtime_pool import VerifiedRuntimePool

IDENTITY = ("mock-rt", "mock-provider", None, "mock-fp")
ALL_CAPS = ("architecture", "coding", "testing", "review")
ARCH = collab_agent_address(IDENTITY, "architect")
CODER = collab_agent_address(IDENTITY, "coder")
TESTER = collab_agent_address(IDENTITY, "tester")
REVIEWER = collab_agent_address(IDENTITY, "reviewer")

TASK = "redesign architecture across modules"  # classifies COMPLEX


def _trace():
    return InvocationTrace(
        invocation_id="mock-inv", task_id="demo", agent_id="mock", runtime="mock-rt",
        provider=None, model=None, role=None, status=InvocationStatus.SUCCESS,
        started_at=0.0, finished_at=0.0, duration_ms=1, exit_code=0,
        input_tokens="unknown", output_tokens="unknown", error=None)


class MockAdapter:
    """Offline adapter returning one fixed, contract-valid packet."""

    def __init__(self, payload):
        self._payload = payload
        self.requests = []

    def invoke(self, request):
        self.requests.append(request)
        return InvocationResult(InvocationStatus.SUCCESS,
                                output=json.dumps(self._payload), trace=_trace())


ARCH_PACKET = {
    "task_id": "demo", "role": "architect", "goal": ["design a tiny helper"],
    "constraints": ["pure function"], "architecture": ["one module function"],
    "interfaces": [{"name": "slug", "params": ["text"], "returns": "str"}],
    "implementation_steps": [{"step": 1, "action": "implement slug"}],
    "acceptance_criteria": ["output is lowercase"], "risks": [{"risk": "none",
    "mitigation": "n/a"}],
}
IMPL_PACKET = {
    "task_id": "demo", "role": "coder", "changed_files": ["slug.py"],
    "implementation_summary": "implemented slug per packet",
    "implementation_details": ["def slug(text): ..."], "assumptions": [],
    "unresolved_items": [], "test_requirements": ["slug returns lowercase"],
}
TEST_PACKET = {
    "task_id": "demo", "role": "tester", "tests_run": ["test_slug"],
    "tests_passed": ["test_slug"], "tests_failed": [], "failures": [],
    "coverage_or_validation": ["happy path covered"], "remaining_risks": [],
}
REVIEW_PACKET = {
    "task_id": "demo", "role": "reviewer", "status": "PASS", "findings": [],
    "severity": [], "affected_files": ["slug.py"], "required_changes": [],
    "acceptance_criteria_status": ["output is lowercase: satisfied"],
}


def _health_ready(runtime_id):
    return RuntimeStatus(
        runtime_id=runtime_id, executable="mock", version="1",
        status=RuntimeState.READY, provider="mock-provider", model=None,
        auth_method=None, reason_code=ReasonCode.NONE,
        evidence=HealthEvidence("d", "a", "p", "m", "ok"),
        checked_at=0.0, expires_at=1.0)


def _mock_pool():
    pool = VerifiedRuntimePool(clock=lambda: 0.0)
    verified = CandidateValidationResult(
        identity=IDENTITY, status=CandidateValidationStatus.VERIFIED,
        gates_passed=frozenset(ValidationGate),
        gate_results=tuple(GateResult(g, GateVerdict.PASS) for g in ValidationGate),
        block_reason=None, failure_point=None, experiment_id="mock-exp",
        executed_at=0.0, validated_capabilities=ALL_CAPS, evidence={})
    pool.admit(verified, ALL_CAPS, health_now="READY")
    return pool


def build_facade():
    budget = TaskBudget(4, 4, timeout_seconds=30.0)
    usage = BudgetUsage()
    guard = LoopGuard()
    arch_adapters = {
        ARCH: MockAdapter(ARCH_PACKET),
        CODER: MockAdapter(IMPL_PACKET),
    }

    def session_factory():
        return CollaborationSession(LoopbackRemoteTransport(), arch_adapters,
                                    budget, usage, guard)

    orchestrator = CollaborationOrchestrator(
        # OFF/single path delegate target (unused on the ON four-stage route):
        _OfflineVO(), _mock_pool(), {IDENTITY[0]: _health_ready(IDENTITY[0])},
        budget, usage, guard, session_factory)
    verification_adapters = {
        TESTER: MockAdapter(TEST_PACKET),
        REVIEWER: MockAdapter(REVIEW_PACKET),
    }
    return ProductionFacade(orchestrator, verification_adapters, _mock_pool(),
                            {IDENTITY[0]: _health_ready(IDENTITY[0])},
                            budget, usage, guard)


class _OfflineVO:
    """Delegate target for the single-agent path (returns an honest empty)."""

    def execute(self, task_id, task, prompt, mode):
        return ExecutionResult(ExecutionStatus.FAILED, (), (), ("MODE_OFF",))


def main() -> int:
    facade = build_facade()
    result = facade.run(task_id="demo", task=TASK, prompt=TASK, mode=Mode.ON,
                        provenance="OFFLINE")
    print(render_summary(result))
    return 0 if result.status == "SUCCESS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
