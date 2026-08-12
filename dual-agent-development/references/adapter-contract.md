# Adapter Contract

This document defines the boundary between the dual-agent orchestrator and a
real CLI-backed provider adapter (Claude CLI and Codex). The orchestrator is
provider-neutral: it routes by verified capabilities, never by model name.
Discovery (`adapter_probe.py`) is the only part allowed to inspect the local
environment, and it must do so without reading secrets, changing global
configuration, or touching the network.

For this increment (Task 5), real provider invocation remains **opt-in and
off by default**. The offline unit suite exercises the probe against fake
subprocesses only. Nothing here runs a real Claude or Codex CLI.

---

## 1. Invocation contract

Adapters launch real CLIs as subprocesses. The following are **normative** and
enforced by the probe and its contract tests:

- **argv array, never a shell string.** The command line is passed as a
  `list[str]` to `subprocess.Popen` with `shell=False` (the default). No
  shell quoting, concatenation, or `shell=True`. This keeps untrusted
  arguments from being interpreted by a shell.

- **Minimal inherited environment.** The child process does **not** inherit
  the parent environment wholesale. A minimal, explicitly constructed
  environment is built from these well-defined variables when the parent
  defines them:

  - `PATH` — the executable search path (needed to resolve and to run the
    documented CLI entry point).
  - `HOME` / `USERPROFILE` — the user's home directory (both spellings are
    kept because Windows and POSIX toolchains expect different names).
  - `SYSTEMROOT` — present on Windows; required by many command-line tools.

  No other parent variables are copied.

- **Bounded execution.** Every subprocess is bounded by a wall-clock timeout
  (`DISCOVERY_TIMEOUT` in `adapter_probe.py`). The probe blocks no longer
  than this budget.

- **Cancellation / process-tree control.** A probe that exceeds the timeout is
  terminated as a process tree, not just the direct child. On Windows this is
  achieved with a **Job Object** (via `ctypes`, `KERNEL32`) configured with
  `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`, and the probe process is assigned to
  that job; closing the handle kills every descendant in the job. On POSIX the
  child is started in its own session (`start_new_session=True`) and killed
  with `os.killpg`, so grandchildren cannot outlive the probe. Individual
  cancellation of an in-flight *invoke* (outside discovery) remains the
  `cancel` operation on the `Adapter` interface.

## 2. Unavailable semantics

Discovery must never invent a capability, cost, availability, or result.

- `AVAILABLE` is reported **only** when an executable is resolved and a bounded
  version probe succeeds. Even then it reports the executable path and a parsed
  version string, not any claim about model behavior.
- `UNAVAILABLE` is reported when no executable is found, the executable is
  malformed or not executable, the probe times out, the probe fails, or the
  adapter is known to be broken (see Codex below). A human-readable `reason`
  is always populated for `UNAVAILABLE`.
- `Codex` is currently reported `UNAVAILABLE` unconditionally because its
  native dependency/executable is not verifiably present in this environment.
  It must **never** be flagged `AVAILABLE` or be routed to on the strength of a
  guessed path. The orchestrator's `DiscoveryStatus.UNAVAILABLE` is the routing
  gate, so an unavailable adapter never becomes a candidate.

## 3. Result as untrusted data

Provider discovery does not return a raw object copy of the CLI output, and
the orchestrator never executes text coming back from a provider. Rules:

- Parse **only the stable schema** exported by `AdapterProbe` — the frozen
  fields `adapter_id`, `status`, `executable`, `version`, `reason`. Any other
  key present in a serialized result is rejected by the consumer; the probe's
  `to_dict()` emits exactly this schema.
- Treat CLI output as untrusted. If the output cannot be parsed as a plain
  version string, report `UNAVAILABLE`; do not interpret, evaluate, or
  execute any fragment of it.
- Evidence of availability always names its source (which adapter, which
  executable path, which version) and never fabricates a passing result.

## 4. Real-provider tests are opt-in

The unit suite runs only against faked subprocess/shm/shutil. Tests that would
launch a real Claude or Codex CLI are gated behind
`RUN_REAL_PROVIDER_TESTS=1` and are skipped by default. The default `unittest
discover` run must have **no file, process, or network side effects**; the
contract tests patch `subprocess` so any real invocation would fail loudly.

## 5. Packaging metadata

`agents/openai.yaml` describes the Skill for a packaging/discovery surface. It
carries **discovery metadata only**: an `interface` block with
`display_name`, `short_description`, and `default_prompt`. It contains **no
keys, tokens, endpoint credentials, or secret-bearing fields of any kind**.
The packager must refuse any such field. See the skill-creator convention for
the interface shape.
