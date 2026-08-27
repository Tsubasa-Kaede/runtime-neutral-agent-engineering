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
