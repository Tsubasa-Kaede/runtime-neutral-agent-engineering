"""G14 capability-evidence diagnostic — N=5 independent sanctioned runs.

Measurement, not repair: each run is a fresh sanctioned capability
validation. On failure the harness captures the failing role and a SAFE
shape diagnosis (seven failure categories, categorical facts only — never
raw model output). Aggregates per-role and per-category distributions.
"""
import json
import os
import statistics
import sys
import time
import unittest
from dataclasses import asdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from candidate_validation import (
    CandidateRuntimeInstance,
    CandidateValidationStatus,
    ValidationGate,
)
from claude_code_adapter import ClaudeCodeAdapter
from content_safety import SECRET_MARKERS, contains_unsafe_content
from external_runtime import InvocationStatus
from real_validation_executor import CAPABILITY_TASK_ID, run_real_validation
from structured_packets import (
    ArchitecturePacket,
    ImplementationPacket,
    ReviewPacket,
    TestPacket,
)

N_RUNS = 5
CAPS_ALL = ("architecture", "coding", "review", "testing")
ROLE_PACKETS = {
    "architect": ArchitecturePacket,
    "coder": ImplementationPacket,
    "tester": TestPacket,
    "reviewer": ReviewPacket,
}
ROLES = tuple(ROLE_PACKETS)


class RecorderAdapter:
    def __init__(self, inner):
        self._inner = inner
        self.captured = []  # (agent_id, status, output) — memory only

    def discover(self):
        return self._inner.discover()

    def check_authentication(self):
        return self._inner.check_authentication()

    def check_provider_model(self):
        return self._inner.check_provider_model()

    def cancel(self, invocation_id):
        return self._inner.cancel(invocation_id)

    def invoke(self, request):
        result = self._inner.invoke(request)
        self.captured.append((request.agent_id, result.status, result.output))
        return result


def _category_for(role, status, output):
    """Map one failed role experiment to the seven diagnostic categories."""
    if status is InvocationStatus.TIMEOUT:
        return "TIMEOUT", {"boundary_stage": "TIMEOUT"}
    if status is not InvocationStatus.SUCCESS:
        return "INVOKE_FAILED", {"boundary_stage": "INVOKE_FAILED",
                                 "status": status.value}
    packet_class = ROLE_PACKETS[role]
    text = output if isinstance(output, str) else ""
    text = text.strip()
    if text.startswith("```"):
        first_newline = text.find("\n")
        text = text[first_newline + 1:] if first_newline != -1 else ""
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    detail = {"parseable_json": False, "missing_required_fields": [],
              "type_mismatch_fields": [], "content_safety_category": None,
              "packet_parse_failure_category": None}
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return "RAW_PARSE", detail
    detail["parseable_json"] = True
    if not isinstance(data, dict):
        return "RAW_PARSE", detail
    data["task_id"] = CAPABILITY_TASK_ID
    missing = [f for f in packet_class.REQUIRED_FIELDS if f not in data]
    detail["missing_required_fields"] = missing
    if missing:
        return "MISSING_REQUIRED_FIELD", detail
    expected_role = packet_class.required_role() if hasattr(
        packet_class, "required_role") else "architect"
    if data.get("role") != expected_role:
        detail["type_mismatch_fields"] = ["role"]
        return "TYPE_MISMATCH", detail
    for field in packet_class.REQUIRED_FIELDS:
        if field in ("task_id", "role", "status", "implementation_summary"):
            continue
        if not isinstance(data[field], (list, tuple)):
            detail["type_mismatch_fields"].append(field)
    if detail["type_mismatch_fields"]:
        return "TYPE_MISMATCH", detail
    try:
        packet = packet_class.from_dict(data)
    except (ValueError, TypeError, KeyError) as exc:
        detail["packet_parse_failure_category"] = type(exc).__name__
        return "TYPE_MISMATCH", detail
    if contains_unsafe_content(packet):
        lowered = json.dumps(asdict(packet)).lower()
        detail["content_safety_category"] = next(
            (m for m in SECRET_MARKERS if m in lowered), "unknown")
        return "CONTENT_SAFETY", detail
    return "OTHER", detail  # everything valid yet gate failed


def run_once(index):
    inner = ClaudeCodeAdapter.from_environment()
    adapter = RecorderAdapter(inner)
    count = {"n": 0}
    real_invoke = inner.invoke

    def counting(request):
        count["n"] += 1
        return real_invoke(request)

    inner.invoke = counting
    identity = ("claude-cli", "anthropic", None, f"fp-g14diag-{index}")
    instance = CandidateRuntimeInstance(
        runtime_id=identity[0], provider_id=identity[1], model_id=None,
        config_fingerprint=identity[3], capability_context=(), probe=inner,
        invocation_spec={"timeout_seconds": 300})
    started = time.monotonic()
    validation, _ = run_real_validation(
        instance, inner, timeout_seconds=300.0,
        experiment_id=f"g14diag-{index}")
    elapsed = round(time.monotonic() - started, 1)

    record = {
        "run": index, "status": validation.status.value,
        "provenance": validation.provenance,
        "validated_capabilities": list(validation.validated_capabilities),
        "invocation_count": count["n"], "elapsed_seconds": elapsed,
        "failure_gate": None, "failure_role": None, "failure_category": None,
        "failure_detail": None, "exception_type": None,
        "shape": None, "diagnosis": None,
    }
    if validation.status is CandidateValidationStatus.VERIFIED:
        return record
    # Read the G14 gate's structured evidence directly (production already
    # records failure_role/category/exception_type/shape — never re-derive
    # from reason strings or captured raw output, which misses adapter
    # exceptions that raise before returning).
    g14 = next((g for g in validation.gate_results
                if g.gate is ValidationGate.G14_CAPABILITY_EVIDENCE), None)
    if validation.failure_point is not None:
        record["failure_gate"] = validation.failure_point[0].name
        record["failure_category"] = str(validation.failure_point[1])
    if g14 is not None:
        evidence = g14.evidence
        record["failure_role"] = evidence.get("failure_role")
        record["failure_detail"] = evidence.get("failure_detail")
        record["exception_type"] = evidence.get("exception_type")
        record["shape"] = evidence.get("shape")
        record["diagnosis"] = {
            "evidence_category": evidence.get("failure_category"),
            "invocation_count": evidence.get("invocation_count"),
        }
    return record


class G14DiagnosticTests(unittest.TestCase):
    def setUp(self):
        if os.environ.get("RUN_REAL_PROVIDER_TESTS", "") != "1":
            self.skipTest("RUN_REAL_PROVIDER_TESTS != 1")

    def test_g14_diagnostic_n5(self):
        records = []
        for index in range(1, N_RUNS + 1):
            record = run_once(index)
            records.append(record)
            print(f"G14RUN {index}:", json.dumps(record, sort_keys=True,
                                                 default=str))
        successful = [r for r in records if r["status"] == "VERIFIED"]
        failed = [r for r in records if r["status"] != "VERIFIED"]
        elapsed = [r["elapsed_seconds"] for r in records]
        stats = {
            "N": N_RUNS,
            "successful": len(successful),
            "failed": len(failed),
            "success_rate": round(len(successful) / N_RUNS, 3),
            "role_failure_counts": {
                role: sum(1 for r in failed if r["failure_role"] == role)
                for role in ROLES},
            "category_distribution": {},
            "gate_distribution": {},
            "invocation_counts": [r["invocation_count"] for r in records],
            "elapsed_mean": round(statistics.mean(elapsed), 1),
        }
        for r in failed:
            stats["category_distribution"][r["failure_category"] or "UNKNOWN"] = \
                stats["category_distribution"].get(r["failure_category"] or "UNKNOWN", 0) + 1
            stats["gate_distribution"][r["failure_gate"] or "UNKNOWN"] = \
                stats["gate_distribution"].get(r["failure_gate"] or "UNKNOWN", 0) + 1
        print("G14_DIAGNOSTIC_STATS:", json.dumps(stats, sort_keys=True))
        self.assertEqual(len(records), N_RUNS)


if __name__ == "__main__":
    unittest.main()
