"""R7-A2: E2E helpers for the collaboration-policy entry-surface tests.

Shared offline composition for tests/test_collaboration_policy_entry.py:
a real VerifiedRuntimePool with N admitted identities, a real
VerifiedSelectionBridge projection, and mock adapters that answer every
role address with a valid packet. All proof functions read ONLY final
ledger envelopes — never internal assigner attributes.

This module is test-only: never imported by production code.
"""
import json
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from candidate_validation import (
    CandidateValidationResult,
    CandidateValidationStatus,
    GateResult,
    GateVerdict,
    ValidationGate,
)
from collaboration_orchestrator import CollaborationOrchestrator
from collaboration_session import (
    CollaborationSession,
    collab_agent_address,
)
from external_runtime import InvocationResult, InvocationStatus, InvocationTrace
from loop_guard import LoopGuard
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

CLAUDE_ENTRY = ("rt-x", "provider-x", "model-x", "fp-x")
CODEX_ENTRY = ("rt-y", "provider-y", "model-y", "fp-y")
PI_ENTRY = ("rt-z", "provider-z", "model-z", "fp-z")
GEMINI_ENTRY = ("rt-w", "provider-w", "model-w", "fp-w")

ALL_CAPS = ("architecture", "coding", "testing", "review")

ROLES = ("architect", "coder", "test", "review")
_ADDRESS_ROLE = {"architect": "architect", "coder": "coder",
                 "test": "tester", "review": "reviewer"}


def arch_dict(task_id="T1"):
    return {"task_id": task_id, "role": "architect", "goal": ["g"],
            "constraints": ["c"], "architecture": ["a"], "interfaces": [{}],
            "implementation_steps": [{}], "acceptance_criteria": ["ac"],
            "risks": [{}]}


def impl_dict(task_id="T1"):
    return {"task_id": task_id, "role": "coder", "changed_files": ["f.py"],
            "implementation_summary": "s", "implementation_details": ["d"],
            "assumptions": [], "unresolved_items": [],
            "test_requirements": ["tr"]}


def test_dict(task_id="T1"):
    return {"task_id": task_id, "role": "tester", "tests_run": ["t"],
            "tests_passed": ["t"], "tests_failed": [], "failures": [],
            "coverage_or_validation": [], "remaining_risks": []}


def review_dict(task_id="T1"):
    return {"task_id": task_id, "role": "reviewer", "status": "PASS",
            "findings": [], "severity": [], "affected_files": [],
            "required_changes": [], "acceptance_criteria_status": []}


def trace():
    return InvocationTrace(
        invocation_id="inv-e", task_id="T1", agent_id="a", runtime="rt",
        provider=None, model=None, role=None, status=InvocationStatus.SUCCESS,
        started_at=1.0, finished_at=2.0, duration_ms=10, exit_code=0,
        input_tokens="unknown", output_tokens="unknown", error=None)


class RepeatingAdapter:
    def __init__(self, result):
        self.result = result
        self.requests = []

    def invoke(self, request):
        self.requests.append(request)
        return self.result


def _ok(payload_dict):
    return InvocationResult(InvocationStatus.SUCCESS,
                            output=json.dumps(payload_dict), trace=trace())


def health_ready(runtime_id):
    return RuntimeStatus(
        runtime_id=runtime_id, executable="exe", version="1",
        status=RuntimeState.READY, provider="p", model="m", auth_method=None,
        reason_code=ReasonCode.NONE,
        evidence=HealthEvidence("d", "a", "p", "m", "ok"),
        checked_at=1.0, expires_at=2.0)


def _pool_result(identity):
    return CandidateValidationResult(
        identity=identity, status=CandidateValidationStatus.VERIFIED,
        gates_passed=frozenset(ValidationGate),
        gate_results=tuple(GateResult(g, GateVerdict.PASS)
                           for g in ValidationGate),
        block_reason=None, failure_point=None, experiment_id="exp-e",
        executed_at=1.0, validated_capabilities=ALL_CAPS, evidence={})


def make_pool(identities):
    pool = VerifiedRuntimePool(clock=lambda: 1.0)
    for identity in identities:
        pool.admit(_pool_result(identity), ALL_CAPS, health_now="READY")
    return pool


def _role_addresses(identity):
    return {
        role: collab_agent_address(identity, _ADDRESS_ROLE[role])
        for role in ROLES
    }


def compose_entry_facade(identities=(CLAUDE_ENTRY,)):
    """Offline E2E composition: real pool + real bridge projection + mock
    adapters answering every role address of every admitted identity."""
    budget = TaskBudget(8, 8, timeout_seconds=30.0)
    usage = BudgetUsage()
    guard = LoopGuard()

    collab_adapters = {}
    verify_adapters = {}
    for identity in identities:
        addresses = _role_addresses(identity)
        collab_adapters[addresses["architect"]] = RepeatingAdapter(_ok(arch_dict()))
        collab_adapters[addresses["coder"]] = RepeatingAdapter(_ok(impl_dict()))
        verify_adapters[addresses["test"]] = RepeatingAdapter(_ok(test_dict()))
        verify_adapters[addresses["review"]] = RepeatingAdapter(_ok(review_dict()))

    def session_factory():
        return CollaborationSession(LoopbackRemoteTransport(), collab_adapters,
                                    budget, usage, guard)

    health = {identity[0]: health_ready(identity[0]) for identity in identities}
    orchestrator = CollaborationOrchestrator(
        object(), make_pool(identities), health,
        budget, usage, guard, session_factory)
    facade = ProductionFacade(orchestrator, verify_adapters,
                              make_pool(identities), health,
                              budget, usage, guard)
    return facade


# --- ledger-only proof helpers (never read assigner internals) ------------


def envelope_runtime_ids(history):
    """Runtime-id membership of every envelope address in the task ledger."""
    used = set()
    for record in history:
        for address in (getattr(record, "source_agent", None),
                        getattr(record, "target_agent", None)):
            runtime_id = _runtime_of_address(address)
            if runtime_id is not None:
                used.add(runtime_id)
    return used


def role_runtime_ids(history):
    """role -> runtime_id, derived from the four stage envelopes: the
    ARCHITECTURE request names architect + coder; TEST/REVIEW requests name
    tester/reviewer as source. Pure ledger projection, no assigner access."""
    per_role = {}
    for record in history:
        payload_type = getattr(record, "payload_type", "")
        if payload_type == "ARCHITECTURE":
            per_role["architect"] = _runtime_of_address(record.source_agent)
            per_role["coder"] = _runtime_of_address(record.target_agent)
        elif payload_type == "TEST":
            per_role["tester"] = _runtime_of_address(record.source_agent)
        elif payload_type == "REVIEW":
            per_role["reviewer"] = _runtime_of_address(record.source_agent)
    return {role: runtime for role, runtime in per_role.items()
            if runtime is not None}


def decision_reasons(history):
    return [record.reason for record in history
            if getattr(record, "direction", None) is not None
            and record.direction.value == "DECISION"]


def _runtime_of_address(address):
    # collab_agent_address renders ["runtime_id","provider_id",...] JSON —
    # the runtime id is the first list element of the embedded array.
    if not address or not address.startswith("["):
        return None
    try:
        parsed = json.loads(address)
    except ValueError:
        return None
    if isinstance(parsed, list) and parsed and isinstance(parsed[0], str):
        return parsed[0]
    return None
