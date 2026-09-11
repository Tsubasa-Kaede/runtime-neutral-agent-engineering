# Contributing

Thanks for considering a contribution.

## Prerequisites

Python 3.10+ (3.10 / 3.11 / 3.12 tested in CI). The engine is pure standard
library; a source checkout is all you need:

```bash
git clone https://github.com/Tsubasa-Kaede/runtime-neutral-agent-engineering.git
cd runtime-neutral-agent-engineering
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS / Linux
pip install -e .
```

## Workflow

1. Fork the repository
2. Create a branch for your change
3. Make the change
4. Run the offline suite and keep it green:

```bash
python -m pytest tests/ -q
python -m compileall -q dual-agent-development
```

5. Open a Pull Request

## Notes

- The offline suite must pass. Skipped entries are opt-in REAL-runtime
  tests; they stay skipped by default, and `RUN_REAL_PROVIDER_TESTS` is
  never required for a contribution.
- Never commit secrets, tokens, or credentials — the engine is built to be
  secret-free, and so are its tests.
- Match the surrounding code style rather than reformatting unrelated
  files; small, focused pull requests are easiest to review.

## Add a new runtime adapter

Adding a runtime means implementing the six-method `ExternalAgentAdapter`
protocol — the architecture allows it, and you own the adapter and its
verification. The orchestrator never changes.

1. Read the contract:
   [`dual-agent-development/references/adapter-contract.md`](dual-agent-development/references/adapter-contract.md).
   Three core invocation methods (`discover`, `invoke`, `cancel`) plus three
   health methods (`check_authentication`, `check_provider_model`,
   `minimal_health_check`).
2. Use an existing adapter as the shape reference — the smallest complete
   ones are `dual-agent-development/scripts/pi_adapter.py` and
   `cline_adapter.py`. Adapters own all runtime specifics: executable
   resolution on PATH, auth-state observation, error normalization, and the
   whitelisted subprocess environment (`PATH` / `HOME` / `USERPROFILE` /
   `SYSTEMROOT`).
3. Exercise it by hand with the developer probe: `adapter_probe.py`.
4. Add offline tests next to the existing ones
   (`tests/test_cline_adapter.py` is the minimal template): discovery
   present/absent, invoke normalization, honest health vocabulary
   (`skipped` / `unsupported` must stay honest — a runtime with no
   observable auth surface cannot be faked into this shape).
5. Register it in the runtime adapter registry and update the runtime
   matrix in the README (your adapter starts at "Adapter implemented" —
   see below).
6. Keep the offline suite green and open the PR.

## REAL-verify an adapter (community program)

The engine's honesty contract has two levels: **REAL VERIFIED** and
**adapter implemented**. Several adapters ship in the second state —
offline-tested, but never REAL-verified inside this repository (see the
matrix in the README). You can move one to the first state from your own
machine:

1. Install the runtime's CLI and log in through **its own** flow — this
   project never installs, authenticates, or configures a runtime.
2. Run the gated qualification:

   ```bash
   # Windows (PowerShell)
   $env:RUN_REAL_PROVIDER_TESTS="1"
   dual-agent qualify

   # macOS / Linux
   RUN_REAL_PROVIDER_TESTS=1 dual-agent qualify
   ```

   The run takes several minutes (real model calls) and persists
   `VERIFIED` + `REAL` evidence under `~/.dual-agent/qualification/`.
3. Open an issue with the
   [Adapter REAL-verification report](https://github.com/Tsubasa-Kaede/runtime-neutral-agent-engineering/issues/new?template=adapter_verification.yml)
   template: runtime CLI version, your OS, the four-capability outcome, and
   the provenance fields. The qualification summary is secret-free by
   design — but never paste credentials or runtime output that might carry
   them.
4. Maintainers review the evidence and update the matrix. For a code-level
   contribution (adapter fixes discovered during verification), open a PR
   referencing the issue.

Honesty rules of the program: a REAL run is `RUN_REAL_PROVIDER_TESTS=1` with
a logged-in CLI; an offline rehearsal never counts as verification; evidence
fields are never edited or fabricated. The engine enforces the same contract
itself — provenance is refused without real-call evidence.
