# Adapter Contract

This document defines the boundary between the V2 orchestrator and a real
CLI-backed runtime adapter (for example the Claude Code CLI). The engine is
runtime-neutral: it routes by verified capabilities, never by runtime,
provider, or model name. Any concrete adapter (Claude Code, Codex, others) is
one implementation of this contract — none of them is a required or hardcoded
runtime.

---

## 1. Invocation contract

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

- **Cancellation.** `cancel(invocation_id)` terminates a tracked in-flight
  process and reports `CANCELLED`. Timeout expiry kills the process tree, not
  just the direct child.

- **Normalized results.** The adapter returns a frozen `InvocationResult`
  (status / output / error / trace with exit code and duration). Raw stdout
  and stderr never become structured packets or ledger records — the
  collaboration layer scans every packet with `content_safety` (the single
  secret-scan source) and redacts unsafe trace errors before they reach any
  public outcome.

## 2. Health and unavailable semantics

Discovery and health never invent a capability, cost, availability, or result.

- `READY` is reported only when discovery, read-only authentication
  diagnostics, provider/model checks, and a minimal health invocation all
  pass. `DISCOVERED` is not `READY`; a CLI existing on disk is not `READY`.
- `AUTH_REQUIRED` / `UNAVAILABLE` / `ERROR` are reported honestly with a
  structured reason. An unavailable runtime never becomes a candidate.
- Authentication state is observed through the runtime's own official,
  read-only diagnostics. The engine never reads, stores, prints, or modifies
  credentials, never logs in or out, and never touches runtime configuration.

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

## 4. Result as untrusted data

- The orchestrator never executes text coming back from a runtime.
- Only frozen, schema-checked structures cross the adapter boundary; extra
  keys and secret-shaped content are rejected.
- Evidence of availability always names its source and never fabricates a
  passing result.

## 5. Real-provider tests are opt-in

The default unit suite is fully offline: no file, process, or network side
effects. Tests that would launch a real runtime are gated behind
`RUN_REAL_PROVIDER_TESTS=1` and skipped by default.

## 6. Packaging metadata

`agents/openai.yaml` describes the Skill for a packaging/discovery surface. It
carries **discovery metadata only**: an `interface` block with `display_name`,
`short_description`, and `default_prompt`. It contains **no keys, tokens,
endpoint credentials, or secret-bearing fields of any kind**. The packager
must refuse any such field.
