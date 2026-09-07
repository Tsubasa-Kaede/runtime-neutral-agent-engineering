# V3.1-F Specification — Remote Collaboration Composition Root

- Status: READY FOR REVIEW (specification only — no implementation, no plan)
- Baseline: HEAD `2f498b8` (V3.1-E CLOSED); V3.1 chain A→B1→B1b→B2→C1→D→E complete, Level 3 PROVEN
- Upstream: V3.1-F Boundary Discovery = READY FOR DESIGN; Boundary Design = APPROVED — READY FOR SPEC (four frozen boundary rulings incorporated verbatim in §6, §11, §12, §13)
- Change Unit: V3.1-F, one production file + two test files, zero modifications to any existing file

---

## 1. Facts（代码事实，非设计）

- **F-1** `AgentRegistry` surface = `register / get(agent_id) / list`; stores and
  enumerates only; never calls `adapter_factory` (agent_manifest.py:81-100).
- **F-2** `AgentManifest` = frozen `binding: AgentRuntimeBinding` +
  `declared_roles: tuple[str, ...]` + `adapter_factory: Callable`
  (agent_manifest.py:38-46).
- **F-3** `agent_address(agent, role) -> "agent:{agent_id}:{role}"` is the only
  address projection (agent_identity.py:109-115); the whole V3.1 chain treats
  addresses as opaque strings.
- **F-4** `run_endpoint(adapter, address, input_stream, output_stream)` is E's
  only production entry; it constructs the endpoint with frozen defaults
  (timeout 120, provenance OFFLINE). B2 accepts `stderr=subprocess.PIPE` and
  captures child stderr into `transport.last_diagnostics` at close — the
  existing observation seam, zero B2 changes.
- **F-5** `RuntimeProfile(agent_id, runtime, provider, model, role,
  capabilities)` — six fields, no defaults; shape differs from the binding
  four-tuple `(runtime_id, provider_id, model_id, config_fingerprint)`;
  `config_fingerprint` has no profile slot. V2 adapters expose
  `from_environment(profile=None)` classmethods.
- **F-6** B2 passes only a 6-key environment whitelist to the child; paths must
  be embedded in the child source.
- **F-7** Production consumers of the remote chain today = zero (tests only);
  `bootstrap_agent_team` (V3.0-G) composes the local facade only.
- **F-8** House composition refusal style: closed-message `ValueError` /
  `RuntimeError`, re-raised verbatim, never re-judged (agent_host.py:95-146).

## 2. Goal

Close the only gap between declaration and remote execution: a production
composition root that turns a DECLARED agent (`agent_id + role` in a
`AgentRegistry`) into a usable `RemoteAgentSession` — deriving the address,
carrying adapter construction knowledge across the process boundary, building
the transport and the session — with zero modifications to V2 / V3.0 / B1b /
B2 / C1 / D / E.

## 3. Non-goals（MUST NOT 混入 F）

New discovery models; new registries of any kind (see §13 banned vocabulary);
qualification / verification / admission / health assessment / capability
evaluation; capability-based or any automatic agent selection; runtime
selection; multi-agent orchestration; retry / reconnect; session lifecycle
redesign (F owns no lifecycle); UI / dashboard; network transport;
persistence; telemetry; supervision or observation APIs (facts are NOT one —
§11).

## 4. Public API（冻结）

```python
# dual-agent-development/scripts/remote_agent_composition.py

@dataclass(frozen=True)
class RemoteAdapterConstruction:
    """Declaration-attached construction description (NOT a registry)."""
    module: str                     # child-side import module name
    attr: str                       # dotted attribute path within module
    profile: dict | None            # RuntimeProfile kwargs, or None = child default
    source_path: str | None = None  # optional child sys.path entry (§12)


def importable_adapter_factory(module, attr, profile=None, *, source_path=None):
    """Declarer-facing: returns a parent-side lazy callable (resolves and
    constructs on call) tagged with `__remote_construction__` carrying the
    RemoteAdapterConstruction descriptor. Store it in an AgentManifest."""


@dataclass(frozen=True)
class RemoteCompositionFacts:
    agent_id: str
    role: str
    remote_address: str
    runtime_identity: tuple


def build_remote_session(registry, agent_id, role, local_address,
                         *, timeout_seconds: float = 300.0):
    """Composition root: declared agent -> (RemoteAgentSession, facts)."""
```

- User inputs MUST be exactly: `registry, agent_id, role, local_address`
  (+ `timeout_seconds`). 
- MUST NOT accept a raw address, a transport override, an adapter override,
  or runtime/provider/model parameters (second selection surface).
- `facts` field set is closed at four (§11); reflection-lockable via a
  `facts_field_names()` helper (house style).

## 5. adapter 跨进程构造协议（冻结）

The parent cannot ship a callable across the B2 boundary; the child must
self-construct. The ONE mechanism:

1. The declarer calls `importable_adapter_factory(module, attr, profile,
   source_path=...)` and stores the result as the manifest's
   `adapter_factory`. The returned object is a lazy parent-side callable
   (import + resolve + construct on call — usable by parent-side
   composition callers) carrying `__remote_construction__` =
   the `RemoteAdapterConstruction`.
2. `build_remote_session` reads that attribute. An untagged factory is
   honestly refused (`ValueError`): it genuinely cannot cross the boundary.
3. The private `_build_glue(descriptor, remote_address)` emits the child
   source, which MUST do exactly, in order:
   - `sys.path.insert` of the scripts directory and (if declared)
     `source_path`;
   - resolve `module` then each `attr` segment via `getattr` (dotted paths
     are legal — this is what makes `ClaudeCodeAdapter.from_environment`
     declarable with zero wrapper modules);
   - if `profile` is not None: `from external_runtime import RuntimeProfile`;
     `factory(profile=RuntimeProfile(**profile))`, else
     `factory(profile=None)`;
   - a None or raising factory result → non-zero exit (honest construction
     failure);
   - `from remote_agent_endpoint import run_endpoint` and
     `run_endpoint(adapter, <remote_address>, sys.stdin.buffer,
     sys.stdout.buffer)`.
4. The factory convention (MUST, frozen): the named callable accepts a
   single keyword argument `profile` (a `RuntimeProfile` or None) and
   returns an adapter or None. `from_environment(profile=None)` satisfies it.
5. `profile` is a dict of `RuntimeProfile` kwargs embedded into the glue
   source via `repr` (values must be Python-literal representable;
   `frozenset()` reprs fine).
6. MUST NOT: a second wire protocol, an inlined copy of the endpoint loop
   (glue calls `run_endpoint` — the only production loop), any modification
   of E or B2.

`RemoteAdapterConstruction` is a declaration-attached description — it never
exists outside a manifest, is never collected, enumerated, keyed, or queried
(§13).

## 6. Error ownership（冻结 — 含审核裁决 1）

| Failure | Owner | Form |
|---|---|---|
| `agent_id` not in registry | **F** | `ValueError("unknown agent: <id>")` — composition-time; the first production "unknown agent" fact, in house composition style (F-8) |
| role not in `manifest.declared_roles` | **F** | closed-message `ValueError` |
| untagged factory / malformed descriptor / profile↔binding inconsistency | **F** | closed-message `ValueError` |
| child-side import/construct failure, factory returns None | **child → B2** | first send's receipt FAILED + `last_diagnostics` (F MUST build B2 with `stderr=subprocess.PIPE`) |
| transport construction failure | **B2** | B2's own `TypeError/ValueError`, propagated unwrapped |
| session construction failure (e.g. invalid `local_address`) | **C1** | C1's `INVALID_ENVELOPE` constructor refusal, propagated unwrapped |

**FROZEN RULING 1 — UNKNOWN_AGENT**: F MUST NOT emit
`RemoteEnvelopeError(UNKNOWN_AGENT)`. Unknown agent is a composition-time
failure: registry lookup miss → `ValueError`. `UNKNOWN_AGENT` stays
RESERVED in the remote envelope vocabulary for a future genuine
remote-routing/envelope boundary. F mints **no** `RemoteEnvelopeError` of
any kind and adds no error vocabulary to E's model.

## 7. Address 投影

`remote_address = agent_address(manifest.binding.agent, role)` — the
existing V3.0-A projection (F-3), consumed verbatim. MUST NOT define a second
address format; MUST NOT build an address resolver (derivation is a
deterministic projection, not routing).

## 8. Lifecycle（冻结）

F is stateless pure construction: it holds no session references, no live
registry of sessions, no manager. The `RemoteAgentSession` remains the only
lifecycle object (delegating to B2, which alone owns the child). F returns
`(session, facts)` and is out; it MUST NOT register, observe, or close
anything.

## 9. Runtime / binding consistency（冻结 — 含审核裁决 4）

F consumes `AgentRuntimeBinding` for exactly one job: **declaration
consistency**. If `profile` is not None, MUST hold:

- `profile["runtime"] == binding.runtime_identity[0]`
- `profile["provider"] == binding.runtime_identity[1]`
- `profile["model"]    == binding.runtime_identity[2]`

Violation → closed-message `ValueError` (two conflicting runtime
declarations). `config_fingerprint` (`[3]`) has no profile slot — documented
honest omission (F-5), never a fabricated mapping.

**FROZEN RULING 4**: F MAY perform only the declaration-consistency checks
necessary for safe construction (registry hit, declared role, tagged +
well-formed descriptor, profile↔binding match). F MUST NOT perform
discovery, qualification, verification, admission, health assessment,
capability evaluation, a second lookup strategy, or runtime selection. F's
job is `declared agent → exact lookup → construction consistency → remote
session` — NOT re-judging whether the agent is trustworthy or available.

## 10. Offline / REAL boundary

**Offline** (`tests/test_remote_agent_composition.py`): no provider.
- Composition facts: address derivation, facts field closure, both
  `ValueError` refusal classes, untagged-factory refusal, consistency
  check, glue text correctness (embedded module/attr/profile/address,
  path inserts, `run_endpoint` call).
- Construction proof over a REAL child: a test-owned importable double
  module (in `tests/`, reached via `source_path`) exposing a
  `profile`-convention factory returning a scripted adapter whose invoke
  yields valid ImplementationPacket JSON — full
  declaration→glue→child→session→send→receive round trip.
- Source scan (§13) + API surface AST locks.

**REAL** (`tests/test_remote_agent_composition_real_e2e.py`): gated by
`RUN_REAL_PROVIDER_TESTS == "1"`; honest skip when claude is absent; no
doubles, no hardcoded output. A real manifest
(`importable_adapter_factory("claude_code_adapter",
"ClaudeCodeAdapter.from_environment", profile={…claude…, model None,
capabilities frozenset()})` on a matching binding) → `build_remote_session`
→ Level 3 round trip with the E evidence family: DELIVERED asserted alone,
model-authored IMPLEMENTATION packet, task/correlation fidelity, role
mapping, provenance OFFLINE, fresh message ids, `last_diagnostics` trace
line naming `runtime=claude-cli` / `provider=anthropic`. The provider fact
comes from the adapter profile (composition-declared), never from the
endpoint (frozen E mapping).

## 11. facts contract（冻结 — 审核裁决 2）

`RemoteCompositionFacts` = exactly four fields: `agent_id`, `role`,
`remote_address`, `runtime_identity`. MUST NOT add: pid, duration, provider
output, transport state, child status, diagnostics, capabilities, health,
selection reason, telemetry. facts are inert construction echoes — NOT an
observation/supervision API. (A future observation CU composes its own
consumers; it does not grow this type.)

## 12. source_path contract（冻结 — 审核裁决 3）

`source_path` survives as a pure import-path construction input:
`declared module + optional declared source_path → child sys.path entry →
import`. MUST NOT: automatic search, directory discovery, fallback chains,
adapter discovery, runtime-based path selection, environment-based adapter
selection. It adds no authority the declarer lacks (they already name the
module). Default None; never inferred.

## 13. Protected boundaries & banned vocabulary

Protected (byte-identical): all of V2, all of V3.0, B1b, B2, C1, D, E, and
prior CU specs/plans.

`RemoteAdapterConstruction` is declaration-attached — the production module
and its tests MUST NOT define or grow, as identifiers or API names
(class / function / parameter / module-level names — an identifier-scope
scan, not a raw substring scan over prose, per the E over-strict-test
lesson): `AdapterRegistry`, `RemoteAdapterRegistry`,
`RuntimeFactoryRegistry`, "descriptor registry", "plugin registry", or any
collecting/enumerating surface over descriptors. This section is the lock
document; it names the banned vocabulary precisely so the scan can lock it.
The same identifier-scope scan locks the §3 non-goal terms (discovery,
admission, qualification, selection, orchestration, retry, reconnect,
supervision, telemetry) as defined names in the production module.

## 14. V3.2 seam（预留，不实现）

The `(agent_id, role) → session` signature IS the seam: a future V3.2
selection layer consumes V3.0-F capability views and emits `(agent_id,
role)` pairs into `build_remote_session`. facts' closed echo set is what a
future attribution consumer may read. Nothing V3.2 is implemented,
scaffolded, or forward-hooked here.

## 15. 方案比较与最终裁决（自 Design 轮携带）

| | Option 1 (chosen): tagged-factory + descriptor | Option 2: parallel descriptor parameter | Option 3: child-side factory registry module |
|---|---|---|---|
| API complexity | low (two entries) | medium (dual source per call) | low but hidden |
| cross-process | single source of truth | manifest.factory vs descriptor drift | works but = second registry (REJECTED) |
| V3.0 reuse | full, zero changes | weakens manifest authority | violates V3.0-B boundary |
| V3.2 impact | selection sits cleanly on the seam | dirty seam | polluted seam |
| premature abstraction | minimal (one attribute convention) | none | yes |

**FINAL RULING = Option 1** (Design APPROVED; carried verbatim).

## 16. Composition order（冻结，十步）

1. `registry.get(agent_id)` → miss → `ValueError("unknown agent: …")`
2. `role in manifest.declared_roles` → else closed `ValueError`
3. read `__remote_construction__` → absent → closed `ValueError`
4. descriptor shape validation (module/attr non-empty str; attr segments
   are identifiers; source_path None-or-str; profile None-or-kwargs dict)
5. profile↔binding consistency (§9)
6. `remote_address = agent_address(manifest.binding.agent, role)`
7. `glue = _build_glue(descriptor, remote_address)`
8. `transport = SubprocessStdioEnvelopeTransport(
   [sys.executable, "-c", glue], timeout_seconds=timeout_seconds,
   stderr=subprocess.PIPE)`  ← PIPE is frozen (diagnostics seam, F-4)
9. `session = RemoteAgentSession(local_address, remote_address, transport)`
   (C1 validates local_address — single validation authority)
10. `return (session, RemoteCompositionFacts(agent_id, role, remote_address,
    manifest.binding.runtime_identity))`

B2/C1 refusals at steps 8/9 propagate unwrapped (§6).

## 17. File ownership

```
NEW  dual-agent-development/scripts/remote_agent_composition.py
NEW  tests/test_remote_agent_composition.py
NEW  tests/test_remote_agent_composition_real_e2e.py
MODIFIED  none — zero.
```

## 18. Self-review（performed at spec time）

- **Placeholder scan**: no TBD/TODO/deferred-within-scope. The only forward
  items are §14 (V3.2 seam, explicitly not-implemented) and the reserved
  UNKNOWN_AGENT (explicitly not-F), both rulings, not gaps.
- **Contradiction scan**: F mints no RemoteEnvelopeError (§6) vs error table
  ✓; facts four fields (§11) vs §4 API ✓; source_path pure input (§12) vs
  glue §5 ✓; consistency-only binding use (§9) vs no-selection §3 ✓;
  descriptor-is-not-a-registry (§13) vs no registry API in §4 ✓; B2/C1/E
  untouched (§13) vs §5/§16 consuming their public surfaces only ✓.
- **Scope scan**: one production file, two test files, zero modifications —
  single-plan completable; no hidden second CU (selection layer, observation
  consumers, endpoint parameterization all explicitly excluded).

**OPEN items for review**: none blocking. (Design OPEN points 1-3 are now
CLOSED by the four frozen rulings in this spec.)
