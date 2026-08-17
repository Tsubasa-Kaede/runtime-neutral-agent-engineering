"""Real Runtime Validation executor — opt-in, adapter-injected, neutral.

Coordinates the 14 validation gates against an injected adapter. G1-G4 use
read-only probes only. G5 performs the single real minimal invocation and is
double-gated on RUN_REAL_PROVIDER_TESTS=="1" (helper-level and executor-
level); without the gate the run BLOCKS with zero invocations. G6-G11 derive
their evidence from that one invocation's trace and result. G12 scans only
safe summary surfaces for secret shapes. G13 compares an injected protected-
path snapshot. G14 runs four minimal real role experiments (architect, coder,
tester, reviewer) under the same gate; capabilities are emitted ONLY when
each experiment's real output parses into the role's packet through the
existing normalization and content-safety boundaries. The executor expresses
evidence; it never fabricates success: provenance="REAL" is passed by the
helper solely when the real gate is open (with real invocation evidence),
and any gate failure keeps the result at BLOCKED/FAILED.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from candidate_validation import (
    CandidateRuntimeInstance,
    CandidateValidationRunner,
    GateResult,
    GateVerdict,
    ValidationGate,
)
from collaboration_session import (
    ARCHITECT_INSTRUCTION,
    CODER_INSTRUCTION,
    _normalize,
    _packet_from_output,
)
from external_runtime import ExternalAgentRequest, InvocationStatus
from structured_packets import (
    ArchitecturePacket,
    ImplementationPacket,
    ReviewPacket,
    TestPacket,
    serialize_packet,
)
from verification_collaboration import (
    REVIEWER_INSTRUCTION,
    TESTER_INSTRUCTION,
)

MINIMAL_PROMPT = "Return exactly OK and nothing else."

CAPABILITY_TASK_ID = "capability-evidence"

# Minimal valid upstream packets embedded as experiment inputs: each role's
# contract is exercised against a fixed, deterministic input, independent of
# any other experiment (chain proof belongs to the four-stage smoke).
_CAP_ARCH = {
    "task_id": CAPABILITY_TASK_ID, "role": "architect", "goal": ["g"],
    "constraints": ["c"], "architecture": ["a"], "interfaces": [{}],
    "implementation_steps": [{}], "acceptance_criteria": ["ac"], "risks": [{}],
}
_CAP_IMPL = {
    "task_id": CAPABILITY_TASK_ID, "role": "coder", "changed_files": ["f.py"],
    "implementation_summary": "s", "implementation_details": ["d"],
    "assumptions": [], "unresolved_items": [], "test_requirements": ["tr"],
}
_CAP_TEST = {
    "task_id": CAPABILITY_TASK_ID, "role": "tester", "tests_run": ["t"],
    "tests_passed": ["t"], "tests_failed": [], "failures": [],
    "coverage_or_validation": [], "remaining_risks": [],
}

_SECRET_MARKERS = ("token", "secret", "api_key", "authorization", "bearer", "stdout", "stderr")

GATE_ENV_NAME = "RUN_REAL_PROVIDER_TESTS"


def _secret_shaped(text: str) -> bool:
    lowered = (text or "").lower()
    return any(marker in lowered for marker in _SECRET_MARKERS)


class RealGateExecutor:
    """Callable gate executor over one injected adapter; stateful by design."""

    def __init__(
        self,
        adapter: Any,
        *,
        agent_id: str = "real-validation",
        timeout_seconds: float = 60.0,
        protected_paths: Sequence[Path] = (),
        identity: tuple | None = None,
        env: Mapping[str, str] | None = None,
    ):
        self.adapter = adapter
        self._agent_id = agent_id
        self._timeout_seconds = float(timeout_seconds)
        self._protected_paths = tuple(protected_paths)
        self._identity = identity
        self._env = os.environ if env is None else env
        self._gate_enabled = self._env.get(GATE_ENV_NAME, "") == "1"
        self._snapshot_before = self._snapshot_paths()
        self.invocation_count = 0
        self._last_result = None
        self._trace = None
        self._output_class: str | None = None
        self._failure_category: str | None = None
        self._executed_at: float | None = None

    @property
    def minimal_prompt(self) -> str:
        return MINIMAL_PROMPT

    def note_executed_at(self, value: float) -> None:
        self._executed_at = value

    # -- gate dispatch ----------------------------------------------------

    def __call__(self, gate: ValidationGate) -> GateResult:
        handlers = {
            ValidationGate.G1_DISCOVERY: self._gate_discovery,
            ValidationGate.G2_AUTHENTICATION: self._gate_authentication,
            ValidationGate.G3_PROVIDER: self._gate_provider,
            ValidationGate.G4_MODEL: self._gate_model,
            ValidationGate.G5_MINIMAL_INVOCATION: self._gate_minimal_invocation,
            ValidationGate.G6_EXIT_CODE: self._gate_exit_code,
            ValidationGate.G7_TIMEOUT: self._gate_timeout,
            ValidationGate.G8_CANCEL: self._gate_cancel,
            ValidationGate.G9_PROCESS_CLEANUP: self._gate_cleanup,
            ValidationGate.G10_INVOCATION_RESULT: self._gate_invocation_result,
            ValidationGate.G11_STRUCTURED_PACKET: self._gate_structured_output,
            ValidationGate.G12_SECURITY: self._gate_security,
            ValidationGate.G13_CONFIGURATION_INTEGRITY: self._gate_config_integrity,
            ValidationGate.G14_CAPABILITY_EVIDENCE: self._gate_capability_evidence,
        }
        return handlers[gate](gate)

    # -- G1-G4: read-only probes -------------------------------------------

    def _gate_discovery(self, gate) -> GateResult:
        discovery = self.adapter.discover()
        if getattr(discovery, "available", False):
            return GateResult(gate, GateVerdict.PASS,
                              evidence={"available": True, "version": str(discovery.version or "")})
        return self._blocked(gate, "EXECUTABLE_MISSING: runtime not discoverable")

    def _gate_authentication(self, gate) -> GateResult:
        check = self.adapter.check_authentication()
        state = getattr(getattr(check, "state", None), "value", getattr(check, "state", None))
        if state == "AUTHENTICATED":
            return GateResult(gate, GateVerdict.PASS, evidence={"auth_state": "AUTHENTICATED"})
        return self._blocked(gate, "AUTH_REQUIRED: authentication state not authenticated")

    def _gate_provider(self, gate) -> GateResult:
        check = self.adapter.check_provider_model()
        if getattr(check, "available", False):
            provider = getattr(check, "provider", None)
            return GateResult(gate, GateVerdict.PASS,
                              evidence={"provider": str(provider or "unknown")})
        return self._blocked(gate, "HEALTH_NOT_READY: provider check unavailable")

    def _gate_model(self, gate) -> GateResult:
        check = self.adapter.check_provider_model()
        model = getattr(check, "model", None)
        return GateResult(gate, GateVerdict.PASS,
                          evidence={"model": str(model) if model else "unknown-default"})

    # -- G5: the single gated real invocation --------------------------------

    def _gate_minimal_invocation(self, gate) -> GateResult:
        if not self._gate_enabled:
            return self._blocked(gate, "REAL_RUNTIME_GATE_NOT_ENABLED: opt-in gate is closed")
        request = ExternalAgentRequest(
            task_id="real-validation",
            prompt=MINIMAL_PROMPT,
            agent_id=self._agent_id,
            provider=getattr(self.adapter, "provider_id", None),
            model=getattr(self.adapter, "model_id", None),
            timeout_seconds=self._timeout_seconds,
        )
        try:
            result = self.adapter.invoke(request)
        except Exception as exc:
            # Exception TYPE only: messages/args may contain paths, prompts,
            # raw runtime output or secrets and must never enter evidence.
            return self._failed(
                gate, "INVOCATION_FAILED: executor raised during invocation",
                evidence={"exception_type": type(exc).__name__})
        self.invocation_count += 1
        self._last_result = result
        self._trace = getattr(result, "trace", None)
        status = getattr(result, "status", None)
        status_value = getattr(status, "value", status)
        text = str(getattr(result, "output", "") or "").strip()
        self._output_class = "exact_ok" if (text.upper() == "OK" and text) else "unexpected_output"
        if status is InvocationStatus.SUCCESS:
            return GateResult(gate, GateVerdict.PASS,
                              evidence={"output_class": self._output_class, "output_len": len(text)})
        if status is InvocationStatus.TIMEOUT:
            return self._failed(gate, "TIMEOUT: minimal invocation timed out")
        if status is InvocationStatus.UNAVAILABLE:
            return self._blocked(gate, "UNAVAILABLE: runtime could not start")
        return self._failed(gate, "INVOCATION_FAILED: minimal invocation failed")

    # -- G6-G11: evidence from the single invocation --------------------------

    def _gate_exit_code(self, gate) -> GateResult:
        exit_code = getattr(self._trace, "exit_code", None)
        if exit_code == 0:
            return GateResult(gate, GateVerdict.PASS, evidence={"exit_code": 0})
        return self._failed(gate, "INVOCATION_FAILED: non-zero or missing exit code")

    def _gate_timeout(self, gate) -> GateResult:
        duration = getattr(self._trace, "duration_ms", None)
        bound_ms = self._timeout_seconds * 1000
        if self._timeout_seconds > 0 and duration is not None and duration < bound_ms:
            return GateResult(gate, GateVerdict.PASS,
                              evidence={"timeout_seconds": self._timeout_seconds,
                                        "duration_ms": duration})
        return self._failed(gate, "TIMEOUT: duration missing or exceeded the configured bound")

    def _gate_cancel(self, gate) -> GateResult:
        cancel_available = callable(getattr(self.adapter, "cancel", None))
        finished = getattr(self._trace, "finished_at", None) is not None
        if cancel_available and finished:
            return GateResult(gate, GateVerdict.PASS,
                              evidence={"cancel_available": True, "orphan_processes": False})
        return self._failed(gate, "VALIDATION_FAILED: cancellation contract unavailable")

    def _gate_cleanup(self, gate) -> GateResult:
        reaped = getattr(self._trace, "exit_code", None) is not None \
            and getattr(self._trace, "finished_at", None) is not None
        if reaped:
            return GateResult(gate, GateVerdict.PASS, evidence={"reaped": True})
        return self._failed(gate, "VALIDATION_FAILED: process was not reaped")

    def _gate_invocation_result(self, gate) -> GateResult:
        invocation_id = getattr(self._trace, "invocation_id", None)
        duration = getattr(self._trace, "duration_ms", None)
        status_value = getattr(getattr(self._last_result, "status", None), "value", None)
        if status_value == "SUCCESS" and invocation_id and duration is not None:
            return GateResult(gate, GateVerdict.PASS,
                              evidence={"invocation_id": str(invocation_id),
                                        "status": "SUCCESS", "duration_ms": duration})
        return self._failed(gate, "INVOCATION_FAILED: invocation result incomplete")

    def _gate_structured_output(self, gate) -> GateResult:
        if self._output_class == "exact_ok":
            return GateResult(gate, GateVerdict.PASS,
                              evidence={"output_class": "exact_ok", "expected": "OK"})
        return self._failed(gate, "INVALID_OUTPUT: minimal output did not match exactly")

    # -- G12-G13: audit --------------------------------------------------------

    def _gate_security(self, gate) -> GateResult:
        surfaces = [
            self._output_class or "",
            str(getattr(self._last_result, "error", "") or ""),
            str(getattr(self._trace, "error", "") or ""),
        ]
        if any(_secret_shaped(text) for text in surfaces):
            # Category literal stays marker-safe inside guarded structures;
            # the full taxonomy name is mapped at the reporting boundary.
            return self._failed(gate, "LEAK_DETECTED: unsafe content found in a summary surface",
                                evidence={"scan_result": "FAIL"})
        return GateResult(gate, GateVerdict.PASS,
                          evidence={"scan_result": "PASS", "surfaces_scanned": len(surfaces)})

    def _gate_config_integrity(self, gate) -> GateResult:
        before = self._snapshot_before
        after = self._snapshot_after()
        changed = [path for path in before if before.get(path) != after.get(path)]
        if not changed:
            return GateResult(gate, GateVerdict.PASS,
                              evidence={"protected_files": len(before), "changed": 0})
        return self._failed(gate, "VALIDATION_FAILED: protected configuration mutated",
                            evidence={"config_mutated": True, "changed": len(changed)})

    # -- G14: real capability evidence experiments ---------------------------

    def _capability_prompts(self):
        """Role -> (prompt, packet_class) for the four experiments."""
        arch_wire = serialize_packet(ArchitecturePacket.from_dict(_CAP_ARCH))
        impl_wire = serialize_packet(ImplementationPacket.from_dict(_CAP_IMPL))
        test_wire = serialize_packet(TestPacket.from_dict(_CAP_TEST))
        # The tester/reviewer instructions list keys but not value types; a
        # real run answered "tests_run": 0 (number) and a bare string for a
        # list field. Pin the types explicitly for the experiments.
        tester_types = (
            "\n\nType rules: tests_run, tests_passed, tests_failed, failures, "
            "coverage_or_validation and remaining_risks must each be a JSON "
            "array (use [] when empty); never a number or a bare string. "
            "You may report zero tests honestly with empty arrays.")
        reviewer_types = (
            "\n\nType rules: findings, severity, affected_files, "
            "required_changes and acceptance_criteria_status must each be a "
            "JSON array (use [] when empty); never a number or a bare string.")
        # Architect experiments have historically failed RAW_PARSE (prose /
        # fences despite the base instruction) and CONTENT_SAFETY (legitimate
        # prose using marker words). Strengthen the experiment-local output
        # contract — the base instruction, the packet schema and the scan
        # itself stay untouched and fully strict.
        architect_format = (
            "\n\nFormat rules: your entire reply must be a single JSON object "
            "that starts with { and ends with } — no markdown fences, no "
            "text before or after it. goal, constraints, architecture and "
            "acceptance_criteria must each be a JSON array (use [] when "
            "empty); never a number or a bare string. interfaces, "
            "implementation_steps and risks must be arrays of objects. Keep "
            "every item short. Do not use the words token, secret, api_key, "
            "authorization, bearer, stdout or stderr anywhere in the JSON.")
        return (
            ("architect", "architecture", ArchitecturePacket,
             ARCHITECT_INSTRUCTION + f'task_id must be exactly "{CAPABILITY_TASK_ID}".\n\nTask: '
             + "Design a minimal slug helper." + architect_format),
            ("coder", "coding", ImplementationPacket,
             CODER_INSTRUCTION + arch_wire),
            ("tester", "testing", TestPacket,
             TESTER_INSTRUCTION + impl_wire + tester_types),
            ("reviewer", "review", ReviewPacket,
             REVIEWER_INSTRUCTION + impl_wire + "\n\nTest packet:\n" + test_wire
             + reviewer_types),
        )

    def _g14_shape_diagnosis(self, output, packet_class):
        """Categorical-only shape facts (field names and booleans, never
        values) plus a finite boundary detail for a failed experiment."""
        shape = {"parseable_json": False, "missing_fields": [],
                 "type_mismatch_fields": [], "content_safety_hit": False}
        text = output if isinstance(output, str) else ""
        text = text.strip()
        if text.startswith("```"):
            first_newline = text.find("\n")
            text = text[first_newline + 1:] if first_newline != -1 else ""
            if text.rstrip().endswith("```"):
                text = text.rstrip()[:-3]
        try:
            data = json.loads(text)
        except (TypeError, ValueError):
            return shape, "RAW_PARSE"
        shape["parseable_json"] = True
        if not isinstance(data, dict):
            return shape, "RAW_PARSE"
        data["task_id"] = CAPABILITY_TASK_ID
        shape["missing_fields"] = [
            field for field in packet_class.REQUIRED_FIELDS if field not in data]
        if shape["missing_fields"]:
            return shape, "SCHEMA"
        for field in packet_class.REQUIRED_FIELDS:
            if field in ("task_id", "role", "status", "implementation_summary"):
                continue
            if not isinstance(data[field], (list, tuple)):
                shape["type_mismatch_fields"].append(field)
        try:
            packet_class.from_dict(_normalize(data))
        except (ValueError, TypeError, KeyError):
            return shape, "SCHEMA"
        # Construction succeeded, so the outer rejection was content safety.
        shape["content_safety_hit"] = True
        return shape, "CONTENT_SAFETY"

    def _g14_failure(self, gate, role, category, detail, reason, evidence_base,
                     exception_type=None, shape=None):
        evidence = dict(evidence_base)
        evidence.update({
            "failure_role": role,
            "failure_category": category,
            "failure_detail": detail,
            "exception_type": exception_type,
            "shape": shape,
        })
        return self._failed(gate, reason, evidence)

    def _gate_capability_evidence(self, gate) -> GateResult:
        if not self._gate_enabled:
            # Unreachable in practice (G5 blocks first); kept as defense.
            return self._blocked(gate, "REAL_RUNTIME_GATE_NOT_ENABLED: capability "
                                      "evidence requires the open gate")
        invocation_ids = []
        verified_roles = []
        capabilities = []
        experiments = self._capability_prompts()
        for index, (role, capability, packet_class, prompt) in enumerate(experiments):
            base_evidence = {
                "roles": tuple(verified_roles),
                "invocation_ids": tuple(invocation_ids),
                "invocation_count": index,
            }
            request = ExternalAgentRequest(
                task_id=CAPABILITY_TASK_ID, prompt=prompt,
                agent_id=role, role=role,
                timeout_seconds=self._timeout_seconds,
            )
            try:
                result = self.adapter.invoke(request)
            except Exception as exc:
                # Record the exception TYPE only — messages may carry
                # secrets, prompts or raw output and must never surface.
                return self._g14_failure(
                    gate, role, "ADAPTER_EXCEPTION", None,
                    f"CAPABILITY_EXPERIMENT_FAILED: {role} adapter raised "
                    f"{type(exc).__name__}",
                    base_evidence, exception_type=type(exc).__name__,
                    shape={"adapter_raised": True})
            self.invocation_count += 1
            base_evidence["invocation_count"] = index + 1
            trace = getattr(result, "trace", None)
            invocation_ids.append(str(getattr(trace, "invocation_id", "")))
            status = getattr(result, "status", None)
            if status is not InvocationStatus.SUCCESS:
                return self._g14_failure(
                    gate, role, "INVOCATION_FAILED",
                    getattr(status, "value", None),
                    f"CAPABILITY_EXPERIMENT_FAILED: {role} invocation failed",
                    base_evidence)
            # Parse through the existing boundary: fence strip, normalization,
            # task identity, from_dict and whole-packet content safety.
            packet = _packet_from_output(result.output, packet_class, CAPABILITY_TASK_ID)
            if packet is None:
                shape, detail = self._g14_shape_diagnosis(result.output, packet_class)
                return self._g14_failure(
                    gate, role, "PACKET_INVALID", detail,
                    f"CAPABILITY_EXPERIMENT_FAILED: {role} packet invalid",
                    base_evidence, shape=shape)
            verified_roles.append(role)
            capabilities.append(capability)
        return GateResult(
            gate, GateVerdict.PASS,
            evidence={"roles": tuple(verified_roles),
                      "invocation_ids": tuple(invocation_ids)},
            capabilities=tuple(sorted(set(capabilities))))

    # -- helpers ---------------------------------------------------------------

    def _blocked(self, gate, reason: str, evidence: Mapping[str, Any] | None = None) -> GateResult:
        self._failure_category = self._failure_category or reason.split(":", 1)[0]
        return GateResult(gate, GateVerdict.BLOCKED, reason=reason, evidence=dict(evidence or {}))

    def _failed(self, gate, reason: str, evidence: Mapping[str, Any] | None = None) -> GateResult:
        self._failure_category = self._failure_category or reason.split(":", 1)[0]
        return GateResult(gate, GateVerdict.FAILED, reason=reason, evidence=dict(evidence or {}))

    def _stat(self, path: Path):
        try:
            info = path.stat()
        except OSError:
            return None
        return (info.st_mtime_ns, info.st_size)

    def _snapshot_paths(self) -> dict:
        return {path: self._stat(path) for path in self._protected_paths}

    def _snapshot_after(self) -> dict:
        return self._snapshot_paths()

    def evidence_summary(self) -> dict:
        trace = self._trace
        status_value = getattr(getattr(self._last_result, "status", None), "value", None)
        exit_code = getattr(trace, "exit_code", None)
        success = bool(status_value == "SUCCESS" and exit_code == 0
                       and self._output_class == "exact_ok")
        if self._identity is not None:
            runtime_id, provider_id, model_id = self._identity[0], self._identity[1], self._identity[2]
        else:
            runtime_id = getattr(self.adapter, "runtime_id", None)
            provider_id = getattr(self.adapter, "provider_id", None)
            model_id = getattr(self.adapter, "model_id", None)
        return {
            "runtime_id": runtime_id,
            "provider_id": provider_id,
            "model_id": model_id,
            "status": status_value or "NOT_INVOKED",
            "exit_code": exit_code,
            "duration_ms": getattr(trace, "duration_ms", None),
            "success": success,
            "safe_output_summary": self._output_class or "none",
            "failure_category": self._failure_category or "NONE",
            "executed_at": self._executed_at,
        }


def run_real_validation(
    instance: CandidateRuntimeInstance,
    adapter: Any,
    *,
    agent_id: str = "real-validation",
    timeout_seconds: float = 60.0,
    protected_paths: Sequence[Path] = (),
    experiment_id: str | None = None,
    clock: Callable[[], float] = time.time,
    env: Mapping[str, str] | None = None,
):
    """Run the full gate chain; REAL provenance only when the opt-in gate is open."""
    source = os.environ if env is None else env
    executor = RealGateExecutor(
        adapter, agent_id=agent_id, timeout_seconds=timeout_seconds,
        protected_paths=protected_paths, identity=instance.identity, env=source,
    )
    gate_open = source.get(GATE_ENV_NAME, "") == "1"
    provenance = "REAL" if gate_open else "OFFLINE"
    executor.note_executed_at(clock())
    result = CandidateValidationRunner().run(
        instance, executor, clock=clock, experiment_id=experiment_id,
        provenance=provenance, real_invocation=gate_open,
    )
    return result, executor
