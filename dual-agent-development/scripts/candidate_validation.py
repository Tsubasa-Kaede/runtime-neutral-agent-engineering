"""Candidate Runtime Validation Skeleton — offline, runtime-neutral.

Implements the confirmed Gate design as pure data + orchestration: gate
models, verdict merge semantics and a runner that coordinates an injected
gate executor. Nothing here starts processes, calls runtimes, reads
credentials or touches the production orchestration stack.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, IntEnum
from types import MappingProxyType
from typing import Any, Callable, Mapping


_SECRET_MARKERS = ("token", "secret", "api_key", "authorization", "bearer", "stdout", "stderr")


def _assert_secret_free(value: Any, where: str) -> None:
    if isinstance(value, str):
        lowered = value.lower()
        for marker in _SECRET_MARKERS:
            if marker in lowered:
                raise ValueError(f"{where} must not contain secret-shaped content")
    elif isinstance(value, Mapping):
        for key, item in value.items():
            _assert_secret_free(str(key), where)
            _assert_secret_free(item, where)
    elif isinstance(value, (tuple, list, frozenset, set)):
        for item in value:
            _assert_secret_free(item, where)


class ValidationGate(IntEnum):
    G1_DISCOVERY = 1
    G2_AUTHENTICATION = 2
    G3_PROVIDER = 3
    G4_MODEL = 4
    G5_MINIMAL_INVOCATION = 5
    G6_EXIT_CODE = 6
    G7_TIMEOUT = 7
    G8_CANCEL = 8
    G9_PROCESS_CLEANUP = 9
    G10_INVOCATION_RESULT = 10
    G11_STRUCTURED_PACKET = 11
    G12_SECURITY = 12
    G13_CONFIGURATION_INTEGRITY = 13


class GateVerdict(str, Enum):
    PASS = "PASS"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    NOT_RUN = "NOT_RUN"


class CandidateValidationStatus(str, Enum):
    VERIFIED = "VERIFIED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    NOT_VERIFIED = "NOT_VERIFIED"


@dataclass(frozen=True)
class CandidateRuntimeInstance:
    runtime_id: str
    provider_id: str
    model_id: str
    config_fingerprint: str
    capability_context: tuple
    probe: Any = field(repr=False, compare=False)
    invocation_spec: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not self.runtime_id or not self.provider_id:
            raise ValueError("runtime_id and provider_id are required")
        if not self.config_fingerprint:
            raise ValueError("config_fingerprint is required")
        _assert_secret_free(self.capability_context, "capability_context")
        _assert_secret_free(self.invocation_spec, "invocation_spec")
        # Freeze the mapping: the instance must not mutate through an
        # externally held reference to the original dict.
        object.__setattr__(
            self, "invocation_spec",
            MappingProxyType(dict(self.invocation_spec)),
        )

    @property
    def identity(self) -> tuple:
        return (self.runtime_id, self.provider_id, self.model_id, self.config_fingerprint)


@dataclass(frozen=True)
class GateResult:
    gate: ValidationGate
    verdict: GateVerdict
    reason: str | None = None
    evidence: Mapping[str, Any] = field(default_factory=dict)
    # Explicit, structured capability evidence produced by this gate.
    # Never inferred from `evidence` strings; empty when the gate produced none.
    capabilities: tuple = ()

    def __post_init__(self) -> None:
        # Capabilities are set-semantics evidence: normalize so ordering and
        # duplicates in executor input can never affect downstream results.
        object.__setattr__(self, "capabilities", tuple(sorted(set(self.capabilities))))
        _assert_secret_free(self.reason or "", "gate reason")
        _assert_secret_free(self.evidence, "gate evidence")
        _assert_secret_free(self.capabilities, "gate capabilities")


@dataclass(frozen=True)
class CandidateValidationResult:
    identity: tuple
    status: CandidateValidationStatus
    gates_passed: frozenset
    gate_results: tuple
    block_reason: str | None
    failure_point: tuple | None
    experiment_id: str | None
    executed_at: float | None
    # Positively validated capability evidence collected from explicit
    # GateResult.capabilities. Distinct from the candidate's declared
    # capability_context; empty unless the run reached VERIFIED.
    validated_capabilities: tuple = ()
    evidence: Mapping[str, Any] = field(default_factory=dict)
    # Structural evidence origin: "OFFLINE" for injected executors,
    # "REAL" only for an opt-in live-runtime gate run.
    provenance: str = "OFFLINE"

    def __post_init__(self) -> None:
        _assert_secret_free(self.block_reason or "", "block_reason")
        _assert_secret_free(self.evidence, "result evidence")
        if self.provenance not in ("OFFLINE", "REAL"):
            raise ValueError("provenance must be OFFLINE or REAL")


class CandidateValidationRunner:
    """Coordinates an injected gate executor in fixed gate order with
    deterministic short-circuit semantics; executes nothing itself."""

    def run(
        self,
        instance: CandidateRuntimeInstance,
        gate_executor: Callable[[ValidationGate], GateResult],
        clock: Callable[[], float] = lambda: None,
        experiment_id: str | None = None,
        provenance: str = "OFFLINE",
        real_invocation: bool = False,
    ) -> CandidateValidationResult:
        if provenance == "REAL" and not real_invocation:
            raise ValueError("REAL provenance requires real invocation evidence")
        executed_at = clock()
        passed: list = []
        results: list = []
        evidence: dict = {}
        block_reason = None
        failure_point = None
        collected_capabilities: set = set()

        for gate in ValidationGate:
            outcome = gate_executor(gate)
            if not isinstance(outcome, GateResult):
                raise ValueError("gate executor must return a GateResult")
            if outcome.gate is not gate:
                raise ValueError("gate executor returned a result for a different gate")
            results.append(outcome)
            evidence[gate.name] = outcome.verdict.value
            if outcome.verdict is GateVerdict.PASS:
                passed.append(gate)
                # Only explicit structured evidence counts; never the
                # candidate's declared capability_context, never plain strings.
                collected_capabilities.update(outcome.capabilities)
                continue
            if outcome.verdict is GateVerdict.BLOCKED:
                block_reason = outcome.reason or "external condition missing"
                break
            if outcome.verdict is GateVerdict.FAILED:
                failure_point = (gate, (outcome.reason or "integration defect").split(":", 1)[0])
                break
            # NOT_RUN: validation not executed for this candidate yet.
            break

        if block_reason is not None:
            status = CandidateValidationStatus.BLOCKED
        elif failure_point is not None:
            status = CandidateValidationStatus.FAILED
        elif len(passed) == len(list(ValidationGate)):
            status = CandidateValidationStatus.VERIFIED
        else:
            status = CandidateValidationStatus.NOT_VERIFIED

        # Capability evidence only counts for a fully verified run: a
        # short-circuited validation is incomplete evidence and must not
        # feed pool admission.
        validated = tuple(sorted(collected_capabilities)) if status is CandidateValidationStatus.VERIFIED else ()

        return CandidateValidationResult(
            identity=instance.identity,
            status=status,
            gates_passed=frozenset(passed),
            gate_results=tuple(results),
            block_reason=block_reason,
            failure_point=failure_point,
            experiment_id=experiment_id,
            executed_at=executed_at,
            validated_capabilities=validated,
            evidence=evidence,
            provenance=provenance,
        )
