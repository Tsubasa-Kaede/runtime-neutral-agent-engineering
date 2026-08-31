# Adapter Contract

This document defines the boundary between the V2 orchestrator and a real
CLI-backed runtime adapter (for example the Claude Code CLI). The engine is
runtime-neutral: it routes by verified capabilities, never by runtime,
provider, or model name. Any concrete adapter (Claude Code, Codex, Pi,
tiny-agents, others) is one implementation of this contract — none of them
is a required or hardcoded runtime.

---

## 0. The contract surface: six methods

The production adapter contract is SIX methods. Three are the core
invocation surface; three are the health surface. The health methods are
not optional: the health pipeline (`RuntimeHealthController` /
`GenericRuntimeHealth`) and the G1-G14 qualification chain call them
directly, so an adapter implementing only the core three cannot pass
discovery bootstrap.

- `discover()` → `RuntimeDiscovery`
- `invoke(request)` → `InvocationResult`
- `cancel(invocation_id)` → `InvocationResult`
- `check_authentication()` → `AuthenticationCheck`
- `check_provider_model()` → `ProviderModelCheck`
- `minimal_health_check(timeout_seconds)` → `MinimalHealthCheck`

"Having the six methods" is not REAL VERIFIED — qualification evidence is
granted only by a gated real validation run (see §7). A runtime whose CLI
has no observable authentication surface must not fake one; such an
adapter is declared discovery-only (L0) and stays there honestly.

---

## 1. Core invocation contract

Adapters launch real CLIs as subprocesses. The following are **normative**:

- **argv array, never a shell string.** The command line is passed as a
  `list[str]` to `subprocess.Popen` with `shell=False`. No shell quoting,
  concatenation, or `shell=True`. This keeps untrusted arguments from being
  interpreted by a shell.

- **Minimal inherited environment.** The child process does **not** inherit
  the parent environment wholesale. A minimal, explicitly constructed
  environment is built from these well-defined variables when the parent
  defines them:

  - `PATH` — the executable search path (needed to resolve and to run the
    documented CLI entry point).
  - `HOME` / `USERPROFILE` — the user's home directory (both spellings are
    kept because Windows and POSIX toolchains expect different names).
  - `SYSTEMROOT` — present on Windows; required by many command-line tools.

  No other parent variables are copied; in particular no credential-bearing
  variables are passed through by the engine.

- **Bounded execution.** Every subprocess is bounded by a wall-clock timeout;
  the adapter reports a structured `TIMEOUT` result instead of hanging.

- **Explicit UTF-8 decoding.** Every `Popen`/`run` that reads child output
  passes `encoding="utf-8", errors="replace"` — including the discovery
  probe. Without it, a GBK-default console raises `UnicodeDecodeError` out
  of `communicate()`, which the subprocess error handlers do not catch.

- **Cancellation.** `cancel(invocation_id)` terminates a tracked in-flight
  process and reports `CANCELLED`. An unknown invocation id reports
  `UNAVAILABLE` — a fake cancel success is never fabricated. Timeout expiry
  and cancellation stay distinct: a call cancelled during timeout handling
  reports `CANCELLED`, not `TIMEOUT`.

- **Normalized results.** The adapter returns a frozen `InvocationResult`
  (status / output / error / trace with exit code and duration).
  `invocation_id` is non-empty, `duration >= 0`, the `runtime` label is the
  adapter's runtime id, identity fields (agent/provider/model/role) stay
  separate from runtime execution facts, and token counts default to the
  literal `"unknown"`. Raw stdout and stderr never become structured packets
  or ledger records — the collaboration layer scans every packet with
  `content_safety` (the single secret-scan source) and redacts unsafe trace
  errors before they reach any public outcome.

---

## 2. Health surface contract

Health and unavailable semantics never invent a capability, cost,
availability, or result.

### `check_authentication()`

- Authentication is **observed**, never executed: only the runtime's own
  official, read-only diagnostic surface (status commands) may be used.
- The engine never reads, stores, prints, or modifies credentials, never
  logs in or out, and never touches runtime configuration.
- Credential-printing surfaces (API key / bearer token print commands) are
  forbidden.
- When the runtime has no reliable, read-only, machine-observable
  authentication surface, the adapter reports `UNSUPPORTED`/`UNKNOWN`
  honestly — it never guesses success.

### `check_provider_model()`

- Provider/model availability is gated on authentication that was actually
  observed by a prior `check_authentication()` call; unobserved auth means
  "cannot vouch", never "available by default".
- The adapter never guesses provider or model, and never infers
  authentication success from unrelated strings.

### `minimal_health_check(timeout_seconds)`

- The only health invocation is opt-in: without the REAL gate
  (`RUN_REAL_PROVIDER_TESTS=1`) it reports an honest
  `UNSUPPORTED_HEALTH_CHECK` / `skipped` — never a silent skip, never a
  fabricated pass.
- With the gate, health success requires the exact `OK` output; anything
  else (`timeout`, `unavailable`, non-OK output, failed invoke) is an
  honest failure with a structured reason.

### `discover()`

- Discovery answers existence only: `DISCOVERED` is not `READY`; a CLI
  existing on disk is not `READY`.
- When the runtime is not installed, `from_environment()` returns `None` —
  no auto-install, no auto-configuration, no guessed paths, no half
  configured adapter, no fabricated discovery success.
- `AUTH_REQUIRED` / `UNAVAILABLE` / `ERROR` are reported honestly with a
  structured reason. An unavailable runtime never becomes a candidate.

---

## 3. Runtime Validation Gate and provenance

A runtime enters the verified pool only through the gated validation chain
(executable, auth diagnostic, provider/model, minimal safe invocation with
`Return exactly OK and nothing else.`, exit code, timeout, cancellation,
result normalization, secret scan, identity, configuration integrity).

Real invocation is **opt-in and off by default** (`RUN_REAL_PROVIDER_TESTS=1`
for the gated tests; the production helper derives it, never a bare caller
string). Results carry `provenance`:

- `OFFLINE` — produced by mocks/injected executors without real invocation.
- `REAL` — produced under the open gate with real invocation evidence.

Offline verification is not real verification, `provenance` keeps the two
honest, and nothing in the engine can upgrade one to the other.

---

## 4. Result as untrusted data

- The orchestrator never executes text coming back from a runtime.
- Only frozen, schema-checked structures cross the adapter boundary; extra
  keys and secret-shaped content are rejected.
- Evidence of availability always names its source and never fabricates a
  passing result.

---

## 5. Usage honesty (token accounting)

Token counts on the trace are **observed values only**:

- `CAPTURE` — if the runtime provides a machine-readable usage surface, the
  adapter parses its OWN CLI output format and reports exact integers.
- `HONEST_UNKNOWN` — if the runtime has no machine-readable usage surface,
  the trace keeps the literal `"unknown"`. Even if raw stdout happens to
  contain token-shaped text (`token_usage=123`), the adapter must not guess
  that this is real usage.

Missing usage → `"unknown"`. Malformed usage → `"unknown"` (never `0`,
never an estimate). A usage parser failure never fails the invocation
itself.

---

## 6. Offline conformance vs REAL qualification

These two are strictly separated and must never be conflated:

- **Offline conformance** proves the *adapter* obeys the Adapter Contract —
  argv discipline, minimal env, UTF-8, bounded timeout, cancellation
  semantics, redaction, honest discovery, usage honesty. It runs against
  fake processes and fixture stdout/stderr, needs no installed runtime,
  and can run on any machine at any time.
- **REAL qualification** proves a *runtime* actually runs on this machine
  and earns `VERIFIED` status with `REAL` provenance, through the gated
  G1-G14 chain.

In particular:

- Conformance PASS does not mean the runtime is READY.
- Runtime READY does not mean VERIFIED.
- Offline fixtures produce no REAL provenance.
- Offline test results never enter the Verified Runtime Pool.
- Offline test results never influence RoleAssignment, Routing, Budget,
  Trust, or Admission.

Adapters can be developed and offline-verified on machines where the
runtime is not installed, not logged in, and not configured. Installation,
authentication, and REAL verification are separate later steps, each under
explicit authorization.

---

## 7. Real-provider tests are opt-in

The default unit suite is fully offline: no file, process, or network side
effects. Tests that would launch a real runtime are gated behind
`RUN_REAL_PROVIDER_TESTS=1` and skipped by default.

---

## 8. Packaging metadata

`agents/openai.yaml` describes the Skill for a packaging/discovery surface. It
carries **discovery metadata only**: an `interface` block with `display_name`,
`short_description`, and `default_prompt`. It contains **no keys, tokens,
endpoint credentials, or secret-bearing fields of any kind**. The packager
must refuse any such field.
