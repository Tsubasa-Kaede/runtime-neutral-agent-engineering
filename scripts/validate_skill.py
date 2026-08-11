"""Validate untrusted dual-agent protocol packets without executing them."""

from typing import Any
import re


REVIEW_STATUSES = {
    "PASS",
    "NEED_FIX",
    "BLOCKED",
    "ARCHITECTURE_VIOLATION",
}
PACKET_KINDS = {"architecture", "review"}
PROVENANCE_SOURCES = {"controller", "agent_proposal", "verified_evidence"}
ALLOWED_CAPABILITIES = {
    "read_repository",
    "propose_commands",
    "write_files",
    "run_tests",
    "review_diff",
}
ARCHITECTURE_CAPABILITIES = {"read_repository", "propose_commands"}
PACKET_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]+$")
MAX_PACKET_DEPTH = 64
MAX_PACKET_NODES = 10_000


def _scan_packet(value: Any) -> tuple[list[str], bool]:
    errors: list[str] = []
    stack: list[tuple[Any, int, Any]] = [(value, 0, None)]
    active: set[int] = set()
    visited: set[int] = set()
    node_count = 0

    while stack:
        current, depth, iterator = stack[-1]
        if iterator is not None:
            try:
                key, nested = next(iterator) if isinstance(current, dict) else (None, next(iterator))
            except StopIteration:
                container_id = id(current)
                stack.pop()
                active.discard(container_id)
                visited.add(container_id)
                continue
            if isinstance(current, dict) and key in {"execute", "shellCommand"}:
                errors.append(f"forbidden packet key: {key}")
            stack.append((nested, depth + 1, None))
            continue

        is_container = isinstance(current, (dict, list))
        container_id = id(current) if is_container else None
        if depth > MAX_PACKET_DEPTH:
            errors.append(f"packet depth exceeds {MAX_PACKET_DEPTH}")
            return errors, True
        if is_container:
            if container_id in active:
                errors.append("packet contains a container cycle")
                stack.pop()
                continue
            if container_id in visited:
                stack.pop()
                continue
        node_count += 1
        if node_count > MAX_PACKET_NODES:
            errors.append(f"packet node count exceeds {MAX_PACKET_NODES}")
            return errors, True
        if isinstance(current, (dict, list)):
            active.add(container_id)
            iterator = iter(current.items()) if isinstance(current, dict) else iter(current)
            stack[-1] = (current, depth, iterator)
        else:
            stack.pop()

    return errors, False


def validate_packet(packet: Any, kind: Any) -> list[str]:
    """Return protocol errors for a packet. Packet content is never executed."""
    if not isinstance(packet, dict):
        return ["packet must be an object"]

    errors: list[str] = []
    if packet.get("protocolVersion") != "1.0":
        errors.append("protocolVersion must be '1.0'")

    packet_id = packet.get("packetId")
    if (
        not isinstance(packet_id, str)
        or not packet_id.strip()
        or PACKET_ID_PATTERN.fullmatch(packet_id) is None
    ):
        errors.append("packetId must be a non-empty stable identifier")

    packet_version = packet.get("packetVersion")
    if isinstance(packet_version, bool) or not isinstance(packet_version, int) or packet_version < 1:
        errors.append("packetVersion must be a positive integer")

    scan_errors, limit_exceeded = _scan_packet(packet)
    errors.extend(scan_errors)
    if limit_exceeded:
        return errors

    if not isinstance(kind, str) or kind not in PACKET_KINDS:
        errors.append("kind argument must be architecture or review")

    packet_kind = packet.get("kind")
    if not isinstance(packet_kind, str) or packet_kind not in PACKET_KINDS:
        errors.append("packet kind must be architecture or review")
    elif packet_kind != kind:
        errors.append("packet kind must match the validation kind")

    provenance = packet.get("provenance")
    if not isinstance(provenance, dict):
        errors.append("provenance must be an object")
    else:
        source = provenance.get("source")
        if not isinstance(source, str) or not source.strip():
            errors.append("provenance source must be a non-empty string")
        elif source not in PROVENANCE_SOURCES:
            errors.append("provenance source is not allowed")

    if kind == "review":
        status = packet.get("status")
        if not isinstance(status, str) or status not in REVIEW_STATUSES:
            errors.append(
                "review status must be PASS, NEED_FIX, BLOCKED, or ARCHITECTURE_VIOLATION"
            )

    capabilities = packet.get("capabilities")
    if not isinstance(capabilities, list):
        errors.append("capabilities must be a list")
    else:
        seen_capabilities: set[str] = set()
        valid_capabilities: set[str] = set()
        for capability in capabilities:
            if not isinstance(capability, str):
                errors.append("each capability must be a string")
                continue
            valid_capabilities.add(capability)
            if capability not in ALLOWED_CAPABILITIES:
                errors.append("capability is not allowed")
            if capability in seen_capabilities:
                errors.append("capabilities must not contain duplicates")
            seen_capabilities.add(capability)
        if packet_kind == "architecture" and not valid_capabilities.issubset(ARCHITECTURE_CAPABILITIES):
            errors.append("architecture capabilities exceed the architecture allowlist")

    findings = packet.get("findings", [])
    if not isinstance(findings, list):
        errors.append("findings must be a list")
        return errors

    finding_ids: set[str] = set()
    for finding in findings:
        if not isinstance(finding, dict):
            errors.append("each finding must be an object")
            continue
        finding_id = finding.get("findingId")
        if (
            not isinstance(finding_id, str)
            or not finding_id.strip()
            or PACKET_ID_PATTERN.fullmatch(finding_id) is None
        ):
            errors.append("findingId must be a non-empty stable identifier")
        elif finding_id in finding_ids:
            errors.append("findingId values must be unique")
        else:
            finding_ids.add(finding_id)
        closed_by = finding.get("closedBy")
        if closed_by is not None and not isinstance(closed_by, str):
            errors.append("finding closedBy must be a string")
            continue
        if finding.get("status") == "RESOLVED":
            if closed_by is None or closed_by.strip().casefold() != "controller":
                errors.append("RESOLVED finding closedBy must be controller")

    return errors
