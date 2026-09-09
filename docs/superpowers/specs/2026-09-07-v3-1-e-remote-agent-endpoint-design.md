# V3.1-E Specification — Remote Agent Endpoint

- Status: READY FOR REVIEW (specification only — no implementation in this round)
- Baseline: HEAD `0f4da0f` (V3.1 chain: B2 `837b27d` → B1b `40de456` → C1 `dc1e57b` → D `0f4da0f`)
- Upstream rulings: V3.1-E Discovery = READY FOR DESIGN; V3.1-E Boundary Design = READY FOR SPEC
- Change Unit: V3.1-E, one production file + two test files, zero modifications to any existing file

---

## 1. Goal

Add the one missing production component between the proven V3.1 remote
collaboration seam and the proven V2 runtime invocation stack:

```
RemoteAgentSession          (C1, committed)
→ RemoteEnvelopeTransport   (B1b Protocol, committed)
→ SubprocessStdioEnvelopeTransport  (B2, committed)
→ REAL child process        (D proved the boundary, test fixture only)
→ RemoteAgentEndpoint       (E — THIS SPEC)
→ ExternalAgentAdapter      (V2, frozen, 8 implementations)
→ REAL Agent Runtime        (e.g. claude CLI)
→ REAL Provider
```

E proves, for the first time, a REAL Level 3 interaction: a
CollaborationPacket crosses the real process boundary, is translated into a
real runtime invocation, the real provider's output is parsed back into a
valid CollaborationPacket through the production parsing discipline, and the
reply envelope returns through the boundary into `RemoteAgentSession.receive()`.

## 2. Non-goals

- No orchestration, scheduling, retry, reconnect, recovery, broker, bus.
- No discovery, qualification, admission, capability, verification, trust.
- No runtime/provider/model selection of any kind.
- No second wire protocol, no error-envelope, no tracing platform.
- No Endpoint lifecycle (no open/close/start/stop).
- No Protocol/ABC extraction for the endpoint.
- No modification of V2, B1/B1b, B2, C1, V3.0 files, or any existing file.
- No product surface (dashboard/UI/accounts/cloud/analytics) — see §22.
- No multi-role support beyond the single "coder" role contract.

## 3. Architecture

One new production module: `dual-agent-development/scripts/remote_agent_endpoint.py`.

Responsibilities, exactly three:

1. **Translation in**: `RemoteEnvelope` → `ExternalAgentRequest`.
2. **One invocation**: delegate to the injected `ExternalAgentAdapter.invoke()`.
3. **Translation out**: `InvocationResult` output → `CollaborationPacket`
   (production parsing discipline) → reply `RemoteEnvelope`.

Plus one process-side wire loop `run_endpoint(...)` that speaks the existing
B2 JSONL protocol on stdin/stdout. The endpoint is a **concrete class**; no
Protocol is established (single implementation; extraction trigger documented
in §21). The endpoint is **stateless per envelope**: constructor state is
`(adapter, address, timeout_seconds, provenance)` and nothing else; `handle`
mutates nothing and shares nothing between calls.

What the endpoint never knows (enforced by source scan, §18): runtime_id,
provider_id, model_id, config_fingerprint; it never parses the agent address;
it never creates, selects, or health-checks an adapter; it holds no
mailbox, no queue, no pending-message bookkeeping, no clock, no randomness
beyond the contract's own id factories.

## 4. Public API (frozen)

```python
class RemoteAgentEndpoint:
    def __init__(
        self,
        adapter,                    # an ExternalAgentAdapter, already selected
        address: str,               # this endpoint's bound agent address (opaque)
        *,
        timeout_seconds: float = 120.0,
        provenance: str = "OFFLINE",
    ): ...

    @property
    def address(self) -> str: ...

    def handle(self, envelope: RemoteEnvelope) -> RemoteEnvelope: ...


def run_endpoint(adapter, address, input_stream, output_stream) -> None: ...
```

- `handle` returns a **fresh reply envelope or raises `RemoteEnvelopeError`**.
  It never returns None, never mutates its input, never reuses the request
  `message_id`.
- `run_endpoint` runs the child-side loop (§12) until stdin EOF, then returns
  normally (exit 0). On a typed endpoint error it writes no ACK, emits a
  sanitized stderr diagnostic, and exits non-zero.
- Constructor validation: `address` must be a non-empty string
  (`INVALID_ENVELOPE` refusal otherwise — the closed vocabulary is the
  session's, mirroring C1 constructor discipline); `timeout_seconds` must be
  a positive number (`ValueError`); `provenance` must be `"OFFLINE"` or
  `"REAL"` (`ValueError` — the packet constructor enforces this vocabulary
  anyway; the endpoint refuses early, at its own boundary).

## 5. Endpoint lifecycle

**The endpoint has none.** The only lifecycle chain is:

```
Session.close() → Transport.close() → child termination → endpoint disappears
```

The endpoint is not consulted, does not observe, and does not register
itself anywhere on that chain. `ExternalAgentEndpoint.close` MUST NOT exist.
`ExternalAgentAdapter` has no close in its six-method contract; the adapter's
per-invocation process bookkeeping is adapter-internal.

## 6. Envelope validation (in `handle`)

Validation gates, in order; each failure raises `RemoteEnvelopeError`:

| # | Condition | Category | Detail (fixed, closed message) |
|---|-----------|----------|-------------------------------|
| 1 | `type(envelope) is not RemoteEnvelope` | INVALID_ENVELOPE | "envelope must be a RemoteEnvelope" |
| 2 | `envelope.recipient != self._address` | INVALID_ENVELOPE | "envelope is addressed to another recipient" |
| 3 | `envelope.role` not in the role-contract table | ROLE_UNAVAILABLE | "no role contract for role: <role>" |

No further inbound validation: the contract constructors already guarantee
payload is a `CollaborationPacket`, envelope correlation equals payload
correlation, and envelope role equals payload target_role. The endpoint adds
no redundant pre-checks (single validation authority = the contract).
Inbound `payload_type` is not restricted: the coder contract consumes any
`CollaborationPacket` verbatim into the prompt (V2 coder precedent,
collaboration_session.py:277).

## 7. Role contract

A module-level closed table mapping exactly one role today:

```python
# role -> (instruction, output payload_type, output packet class)
_ROLE_CONTRACTS = {
    "coder": (
        IMPLEMENTATION_INSTRUCTION,          # E-owned constant (§9)
        CollaborationPayloadType.IMPLEMENTATION,
        ImplementationPacket,
    ),
}
```

- Coder contract: input = one `CollaborationPacket`; output = an
  `IMPLEMENTATION` CollaborationPacket.
- Roles outside the table → `ROLE_UNAVAILABLE` (§6 gate 3). This is the
  **first real fact source** for that reserved code: the semantics are exact
  — "this endpoint serves no contract for that role".
- Architect / Tester / Reviewer are NOT added in E. No scaffolding, no
  partial implementation, no forward hooks for them.

## 8. Envelope → ExternalAgentRequest mapping (frozen)

| Request field | Source | Ruling |
|---------------|--------|--------|
| `task_id` | `envelope.payload.task_id` | packet fact; never inferred |
| `prompt` | E-owned instruction + `serialize_collaboration_packet(envelope.payload)` | byte-discipline identical to V2 coder stage |
| `agent_id` | endpoint bound address (`self._address`) | V2 precedent: request.agent_id carries the routing address (collaboration_session.py:218) |
| `role` | `envelope.role` | verbatim passthrough, zero interpretation |
| `provider` | `None` | MUST be None; runtime facts live inside the adapter; the endpoint must not infer |
| `model` | `None` | MUST be None; same ruling |
| `timeout_seconds` | endpoint construction (`timeout_seconds`) | never taken from the envelope |
| `handoff_packets` | `()` | MUST be empty; the packet is already serialized into the prompt (V2 coder precedent passes none) |

Explicitly excluded from the request: `correlation_id` (interaction fact,
stays in envelope-land), `sender`/`recipient` (address facts, used only on
the reply side).

## 9. Invocation → CollaborationPacket parsing (production discipline, E-owned)

V2 is not modified and its private helpers (`_packet_from_output`,
`ARCHITECT_INSTRUCTION`, `CODER_INSTRUCTION`) are not imported. E implements
the **identical discipline** in `remote_agent_endpoint.py`:

1. **Output to text**: `output if isinstance(output, str) else ""`, stripped.
2. **Strip code fence**: leading ``` (with optional language tag) and
   trailing ``` removed (V2-exact fence handling).
3. **JSON parse**: `json.loads` → must be a `dict`; any failure → invalid.
4. **task_id override**: `data["task_id"] = inbound.payload.task_id`.
   **The authoritative source of task identity is the inbound
   CollaborationPacket, never the model output.** Whatever the model echoes,
   the packet belongs to this task by construction.
5. **Normalize list fields**: the V2 `_normalize` vocabulary exactly
   (goal, constraints, architecture, interfaces, implementation_steps,
   acceptance_criteria, risks, changed_files, implementation_details,
   assumptions, unresolved_items, test_requirements, findings, severity,
   affected_files, required_changes, acceptance_criteria_status, tests_run,
   tests_passed, tests_failed, failures, coverage_or_validation,
   remaining_risks) — a string value becomes a one-element list.
6. **`ImplementationPacket.from_dict(normalized)`**: tolerated failures are
   exactly `PacketValidationError, TypeError, KeyError, ValueError`.
7. **Whole-packet safety scan**: `packet_has_unsafe_content(packet)` (public
   function defined in `content_safety.py:158` at this spec's baseline
   `0f4da0f` — erratum fix: defined in `content_safety`, not in the packet
   module) → unsafe means invalid.

Any failure at any step → `REMOTE_EXECUTION_FAILED` with a **fixed, closed
detail message** ("invocation output did not satisfy the role packet
contract"). The model's raw output MUST NOT appear in the error detail —
unparsed, unscanned text never leaves the endpoint through the error path.

The instruction constant (`IMPLEMENTATION_INSTRUCTION`) is E-owned. Its
contract: instruct the model to answer with a single JSON object matching
the `ImplementationPacket` schema (task_id echo, role "coder", changed_files,
implementation_summary, implementation_details, assumptions,
unresolved_items, test_requirements — list fields as JSON arrays), and
nothing else outside an optional code fence. The parse override (step 4)
keeps task authority regardless of model compliance.

## 10. Reply envelope construction

On SUCCESS + valid parse:

```python
reply_packet = CollaborationPacket(
    correlation_id=inbound.correlation_id,       # same interaction
    task_id=inbound.payload.task_id,             # authoritative task identity
    source_agent=self._address,                  # who is answering
    target_agent=inbound.sender,                 # who asked
    source_role=inbound.role,                    # the role just served
    target_role=inbound.payload.source_role,     # requester's declared role
    payload_type=CollaborationPayloadType.IMPLEMENTATION,   # role contract output
    payload=parsed_implementation_packet,
    acceptance_criteria=parsed.test_requirements,  # V2-exact (collaboration_session.py:304)
    provenance=self._provenance,                  # §14
)
reply_envelope = RemoteEnvelope(
    message_id=new_message_id(),                  # FRESH identity — request id is never reused
    correlation_id=inbound.correlation_id,
    sender=self._address,
    recipient=inbound.sender,
    role=reply_packet.target_role,                # contract invariant holds by construction
    payload=reply_packet,
    payload_type=RemotePayloadType.COLLABORATION_PACKET,
    protocol_version=PROTOCOL_VERSION,            # default
)
```

- `RemoteEnvelope` has no timestamp field; none is invented.
- The reply is a fully new object graph; the inbound envelope is untouched.
- Contract invariants (correlation match, role == payload.target_role) are
  enforced by the constructors themselves — the endpoint passes values that
  satisfy them by construction.

## 11. Error mapping (frozen)

| Condition (typed fact) | Error category | Detail |
|------------------------|----------------|--------|
| invalid envelope / recipient mismatch (§6 gates 1-2) | INVALID_ENVELOPE | fixed closed message |
| unsupported role (§6 gate 3) | ROLE_UNAVAILABLE | fixed closed message incl. role name |
| `InvocationStatus.FAILED` | REMOTE_EXECUTION_FAILED | `result.trace.error` (adapter-sanitized upstream) |
| `InvocationStatus.TIMEOUT` | REMOTE_EXECUTION_FAILED | same |
| `InvocationStatus.CANCELLED` | REMOTE_EXECUTION_FAILED | same |
| `InvocationStatus.UNAVAILABLE` | REMOTE_EXECUTION_FAILED | same — the envelope WAS delivered to this endpoint; the failure is that the execution machinery could not run. DELIVERY_FAILED/DELIVERY_TIMEOUT are transport-owned reserved codes (B1 ruling); the endpoint MUST NOT mint delivery facts |
| SUCCESS but output fails the §9 parse discipline | REMOTE_EXECUTION_FAILED | fixed closed message; never the raw output |

Hard prohibitions:

- **No `except Exception → REMOTE_EXECUTION_FAILED`.** Error mapping fires
  only on the typed `InvocationStatus` of a returned `InvocationResult` or
  the typed parse-discipline outcome. An unexpected exception from
  `adapter.invoke` propagates uncaught and unclassified (offline tests prove
  this path is not swallowed).
- `UNKNOWN_AGENT` is NOT produced by E (fact source belongs to a future
  composition/routing factory; no factory exists in E).
- `DELIVERY_*` is NOT produced by E, ever.
- If `result.trace is None` on a non-SUCCESS status, the detail is the fixed
  closed message ("invocation failed without trace") — never fabricated.

Cross-boundary projection (documented honesty): the wire has no
error-envelope (a second protocol is forbidden). `run_endpoint` on a typed
error: no ACK, one sanitized stderr diagnostic line, non-zero exit. The
parent therefore observes transport-level `FAILED` (B2 eof semantics). The
typed error is the endpoint-local fact; the receipt is the transport fact;
the two vocabularies never impersonate each other.

## 12. `run_endpoint` wire loop

```
for raw_line in input_stream:                 # binary stdin, B2 JSONL
    if raw_line is whitespace-only: continue  # defensive tolerance only
    inbound = deserialize_remote_envelope(raw_line.decode("utf-8"))
    reply   = endpoint.handle(inbound)        # may raise RemoteEnvelopeError
    output_stream.write(serialize_remote_envelope(reply).encode("utf-8") + b"\n")
    output_stream.flush()                     # EVERY write flushed
    ack = json.dumps({"delivered": inbound.message_id},
                     sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
    output_stream.write(ack)
    output_stream.flush()                     # EVERY write flushed
return                                        # stdin EOF → normal exit 0
```

Frozen obligations:

- **Response MUST be written and flushed BEFORE the ACK is written** —
  DELIVERED may only authorize an exchange whose response is already on the
  wire (B2 causal order; D-proven; M1 lesson locked as "flush after every
  write").
- ACK is canonical JSON `{"delivered":"<message_id>"}` (sort_keys, compact
  separators) — the existing B2 convention.
- Typed endpoint error: write nothing further, emit ONE stderr diagnostic
  line (category + detail; both already sanitized/closed-vocabulary), raise
  `SystemExit(1)`. No retry, no continuation, no error-envelope.
- Unexpected (non-`RemoteEnvelopeError`) exception: propagates; the process
  dies; B2 reports FAILED. Same honest projection.
- `run_endpoint` reads no environment, takes no argv, holds no registry —
  adapter and address arrive as parameters from the composition glue.

## 13. Child launcher contract

**Production side (in `remote_agent_endpoint.py`):** `RemoteAgentEndpoint`
and `run_endpoint` only. No `main()`, no argv parsing, no env reading, no
runtime-name → adapter table (that would be runtime selection in disguise).

**Composition side (NOT production code in E):** the child command is
assembled by the composition root. In E's REAL test the test itself is the
composition root (V2 REAL-test precedent):

```python
GLUE = (
    "import sys\n"
    "sys.path.insert(0, " + repr(str(SCRIPTS)) + ")\n"      # env whitelist: embed, never env
    "from claude_code_adapter import ClaudeCodeAdapter\n"
    "from runtime_profile_source import ...  # (actual import per composition)\n"
    "adapter = ClaudeCodeAdapter.from_environment(profile=RuntimeProfile(...))\n"
    "from remote_agent_endpoint import run_endpoint\n"
    "run_endpoint(adapter, " + repr(REMOTE_ADDRESS) +
    ", sys.stdin.buffer, sys.stdout.buffer)\n"
)
transport = SubprocessStdioEnvelopeTransport(
    [sys.executable, "-c", GLUE], timeout_seconds=..., stderr=subprocess.PIPE)
```

Glue obligations: import real production parts only; construct exactly one
real adapter via its `from_environment`; call `run_endpoint`; embed any path
in the source (B2 passes only the 6-key env whitelist); `sys.executable` for
the interpreter. A future production factory CU may own this glue; E does
not ship one.

Rejected alternatives (recorded): argv-configured runner module (forces a
runtime-name→factory table into production = selection in disguise, and
duplicates `AdapterDescriptor` registry's job at the wrong layer);
env-configured runner (impossible under the B2 whitelist); treating D's
`-c` test fixtures as a production API (they are test doubles; production
units are the importable module + glue wiring).

## 14. Provenance semantics (final ruling, code-grounded)

What the field means in V2 (verified): `provenance` on
`CandidateValidationResult` (candidate_validation.py:149-153) is the
**qualification-evidence provenance** — `"REAL"` is only legal when real
invocation evidence backs the validation. That value flows caller-side:
REAL tests pass `facade.run(..., provenance=claude_validation.provenance)`
(test_real_cli_policy_collaboration.py:369), and the facade/session stamp it
onto packets they construct — the session itself knows nothing about
evidence (collaboration_session.py:176/246/305).

E ruling, matching V2 exactly:

- `RemoteAgentEndpoint.__init__(..., provenance="OFFLINE")` — a **declared
  label carried verbatim**, never inferred, never upgraded.
- The composition root that holds the adapter's qualification evidence is
  the only legitimate declarer of `"REAL"`.
- The endpoint neither knows nor checks whether the declaration is true; it
  validates only the closed vocabulary (`OFFLINE | REAL`, the packet
  constructor enforces it too) and stamps it on every reply packet.
- Default `"OFFLINE"`: an undeclared endpoint is honestly offline-provenance.
  A REAL-qualified composition declares `"REAL"` explicitly.

This is not invocation provenance: a REAL provider invocation under an
undeclared composition still produces `"OFFLINE"` packets, exactly as V2
REAL runs default to `"OFFLINE"` when the caller does not propagate
evidence provenance. The fact the field certifies is about evidence
pedigree, not about this call having happened.

## 15. REAL_AGENT evidence standard (Level 3)

Evidence that proves ONLY process/transport and therefore does NOT count
toward REAL_AGENT (all already proven by D/B2): distinct PIDs, real pipes,
ACK-authorized DELIVERED, FIFO, close reaping. A test fixture produces all
of these; they have zero discriminating power for agent-ness.

REAL_AGENT requires the full chain, each link asserted:

```
REAL adapter (from_environment, production class)
→ REAL runtime (claude CLI grandchild process)
→ REAL model output
→ production parsing discipline (§9) actually executed
→ valid CollaborationPacket (ImplementationPacket)
→ reply RemoteEnvelope (§10)
→ session.receive() delivers it under C1 validation
```

Minimum assertions in the REAL test:

1. The production adapter was invoked (composition injects it; no fake in
   the REAL path).
2. The reply payload is NOT a fixture echo: the REAL composition contains
   no test doubles and no hardcoded provider output; the inbound packet is
   type ARCHITECTURE and the reply is type IMPLEMENTATION, so a byte-echo
   is structurally impossible.
3. The parsing discipline is a real gate: offline negative control proves
   garbage output → `REMOTE_EXECUTION_FAILED`.
4. Reply is a valid `CollaborationPacket` with `payload_type`
   IMPLEMENTATION and a payload passing `ImplementationPacket` construction.
5. `task_id` equals the inbound packet's task_id (override authority).
6. `correlation_id` equals the session correlation end-to-end.
7. `role`/`payload_type` per §10 (role = inbound payload source_role).
8. Delivery facts stay separated: DELIVERED receipt asserted on its own;
   the response asserted on its own; never one substituted for the other.

## 16. REAL_PROVIDER evidence standard

Weak proof, explicitly banned as sufficient: "provider command exited 0".

Required chain: parent → B2 child → endpoint → adapter → provider CLI →
REAL Provider, with production code at every hop.

Obtaining the `InvocationTrace` (which is created inside the child):

- **Ruling: use the existing B2 observation seam — zero B2 modification.**
  B2's constructor accepts `stderr=subprocess.PIPE`
  (remote_subprocess_transport.py:106) and, at close, captures the child's
  stderr into the public attribute `transport.last_diagnostics`
  (`_capture_diagnostics`, :419-423).
- The composition glue wraps the real adapter's `invoke` (test-owned
  wrapper around the production adapter — V2 REAL-test `_wrap` precedent)
  to emit ONE canonical diagnostics line to stderr after each invocation:
  runtime id, provider, invocation_id, status value, exit_code,
  duration_ms — all from the real `InvocationTrace`, redacted the V2 way.
- The REAL test reads `transport.last_diagnostics` after `session.close()`
  and asserts the trace line names the real runtime/provider.
- If a future fact makes this seam insufficient, the fix is a new
  observation CU — never a quiet B2 change, never a fabricated trace.

Also required: honest skip when the provider is absent (no `claude` on
PATH → `skipTest`), gate `RUN_REAL_PROVIDER_TESTS=1` (V2 discipline),
positive assertion that content fields are model-authored (non-empty
implementation_summary etc. — presence, not exact text).

## 17. Testing strategy

**Offline** — `tests/test_remote_agent_endpoint.py` (no subprocess, no
network, fake adapter only):

- API surface: constructor validation (address/timeout/provenance), address
  property, handle signature.
- Role contract: coder happy path; unsupported role → ROLE_UNAVAILABLE.
- Field mapping: every §8 field asserted on the captured request (provider
  None, model None, handoff (), timeout from constructor, agent_id =
  address, role passthrough, prompt = instruction + serialized packet).
- Recipient rejection → INVALID_ENVELOPE; invalid envelope type →
  INVALID_ENVELOPE.
- Error mapping matrix: adapter returns FAILED/TIMEOUT/CANCELLED/UNAVAILABLE
  → REMOTE_EXECUTION_FAILED each; success + garbage output (non-JSON, JSON
  non-dict, fence-wrapped bad schema, unsafe content, wrong packet role) →
  REMOTE_EXECUTION_FAILED with the closed detail (no raw output leak).
- Successful response: full §10 field assertion on reply packet + envelope.
- Fresh message_id (≠ inbound), correlation preserved, task_id authority
  (model echoes wrong id → overridden), acceptance_criteria from
  test_requirements, provenance passthrough (both vocabulary values).
- Input immutability: inbound envelope unchanged after handle.
- Non-catch-all: adapter raising an unexpected exception propagates
  unclassified.
- `run_endpoint` loop: happy JSONL round trip on in-memory streams (reply
  before ACK, both flushed — observable via unbuffered fakes); typed error →
  SystemExit(1) + no ACK + stderr line; blank-line tolerance.
- Source scan (§18).

**REAL** — `tests/test_remote_agent_endpoint_real_e2e.py`:

- Class-level gate `RUN_REAL_PROVIDER_TESTS == "1"` else skip.
- Honest skip: `ClaudeCodeAdapter.from_environment()` returns None → skip.
- No fake adapter, no hardcoded provider output, no test doubles in the
  child; glue wires production parts only (§13).
- Assert the §15 eight-point evidence set and the §16 trace line via
  `last_diagnostics`.
- Negative expectation: the test does NOT force success — if the real
  provider's output does not parse, the observed fact is
  REMOTE_EXECUTION_FAILED and the test fails honestly (that IS the
  discipline working; instruction quality is tuned until the real chain
  passes, never by weakening the parse).

## 18. Source-scan constraints (locked by offline tests)

`remote_agent_endpoint.py` MUST NOT contain (whole-word scan, code + prose):

- Architectural vocabulary: `discovery`, `admission`, `qualification`,
  `selection`, `orchestration`, `schedule`, `retry`, `reconnect`,
  `supervisor`, `registry` (the endpoint holds none of these roles).
- Runtime knowledge: `runtime_id`, `provider_id`, `model_id`,
  `config_fingerprint` as concepts it acts on (the words appear nowhere).
- `subprocess`, `Popen`, `threading`, `asyncio`, `socket` — the endpoint is
  I/O-free; only `run_endpoint` touches streams handed to it.
- `time.`, `datetime`, `random` — no clocks, no randomness (id factories
  belong to the contract).
- `os.environ` — no environment reads.

Import surface: standard library (`json`, `sys` for `run_endpoint`'s exit)
plus exactly `collaboration_packet` (CollaborationPacket,
CollaborationPayloadType, serialize_collaboration_packet,
PacketValidationError), `content_safety` (packet_has_unsafe_content —
erratum fix: defined in content_safety.py, not collaboration_packet),
`external_runtime` (InvocationStatus, the status enum only),
`structured_packets` (ImplementationPacket), and `remote_contract`
(RemoteEnvelope, RemotePayloadType, RemoteEnvelopeError,
RemoteEnvelopeErrorCode, deserialize/serialize_remote_envelope,
new_message_id, PROTOCOL_VERSION). Nothing else — no adapter import, no
V2 session import, no transport import.

## 19. File ownership

```
NEW  dual-agent-development/scripts/remote_agent_endpoint.py
NEW  tests/test_remote_agent_endpoint.py
NEW  tests/test_remote_agent_endpoint_real_e2e.py
MODIFIED  — none. Zero.

Explicitly protected (MUST remain byte-identical):
  collaboration_session.py, production_facade.py,
  collaboration_orchestrator.py, orchestrator.py, execution_engine.py,
  all V2 adapters, remote_contract.py, remote_envelope_transport.py,
  remote_subprocess_transport.py, remote_agent_session.py,
  all V3.0 identity/manifest/composition/host files, cli.py, host_entry.py
```

## 20. Boundary invariants (the audit checklist for review)

1. Endpoint holds no lifecycle; no `close()` exists.
2. Endpoint performs zero runtime selection/discovery/admission — its only
   adapter is the constructor-injected one.
3. C1 and B2 are untouched and unaware of the endpoint's existence.
4. provider/model are never inferred; they are always `None` in the request.
5. Role never selects a runtime; it selects only the I/O contract row.
6. DELIVERY_* never originates in the endpoint.
7. No catch-all exception mapping; only typed statuses map to
   REMOTE_EXECUTION_FAILED.
8. Process-boundary evidence (PIDs, pipes, ACK) is never presented as
   agent/provider evidence.
9. task identity is authoritative from the inbound packet, never from the
   model.
10. Raw model output never escapes through error details.
11. Provenance is a carried declaration, never an inference.
12. Exactly one wire protocol exists; no error-envelope.

## 21. Future extension points (explicitly NOT in E)

- Additional role contracts (architect/tester/reviewer) — one closed-table
  row each, own CU, own instruction + parse tests.
- Production composition factory (address → adapter registry, the honest
  home of UNKNOWN_AGENT) — future CU; today the REAL test is the
  composition root.
- Endpoint Protocol extraction — trigger: a second implementation exists
   AND a consumer needs to program against the abstraction (B1b precedent:
   RED the rejection face first).
- Cross-boundary trace transport beyond the stderr diagnostics seam —
  future observation CU if `last_diagnostics` proves insufficient.
- User-facing release path — §22.

## 22. Explicit exclusions + recorded release-level requirement

Excluded from E and from any near CU without a new explicit ruling:
multi-agent networking, broker/message bus, distributed registry, remote
discovery/admission/verification, retry framework, reconnect, persistent
recovery, tracing/observability platform, dashboard, web UI, accounts,
cloud services, marketplace, analytics.

**Recorded product requirement (release level, NOT E's scope):** the
technical closed loop must eventually become a minimal user path that a
developer new to the project can quickly understand, install, configure,
run with one command, and see a REAL agent collaboration produce a visible,
useful result:

```
Install → Configure → One command → REAL Agent collaboration → Visible result
```

This belongs to a future User-facing / Release CU. E's contribution is the
production endpoint that such a path will invoke — nothing more.

---

## Self-audit (performed at spec time; read-only)

- **Placeholder scan**: no TBD/TODO/“以后再决定”. The only deferred items are
  §21 future CUs, explicitly marked as not-in-E, none blocking
  implementation.
- **Contradiction scan**: endpoint has no lifecycle (§5 vs §4 — no
  close anywhere); C1/B2 untouched (§19 vs §3 — endpoint imports neither);
  provider/model never inferred (§8 vs §18 — vocabulary absent from source
  scan); role never selects runtime (§7 vs §20.5); DELIVERY_* never minted
  (§11 vs §20.6); no catch-all (§11 vs §20.7 and offline test proof);
  process evidence never claimed as provider evidence (§15 vs §16 vs
  §20.8).
- **Scope scan**: one production file (~200-260 lines), two test files,
  zero modifications — a single implementation plan can complete E
  independently; no second CU hiding inside (the only embedded future work,
  the composition factory, is excluded in §13/§21).
- **Open ambiguities**: none blocking. The instruction-constant wording is
  an implementation detail bounded by §9's contract; the REAL run either
  passes or fails honestly per §17.
