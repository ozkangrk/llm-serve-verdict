# Contributing

Thank you for considering a contribution to **LLM ServeVerdict**. This is a
small, deliberately narrow tool, so the contribution bar is "keep it narrow
and keep it provable." Please read this before opening a PR.

## Ground rules

- **No exaggerated claims.** If a change does not come with a test that proves
  the behavior, it is not ready. We do not ship "should work" or "probably
  fine."
- **Preserve the invariants.** The security and determinism invariants in
  `SECURITY.md` are the contract. A change that weakens any of them needs an
  explicit discussion *first*, not after.
- **Deterministic verdicts.** The verdict must remain a pure function of the
  bound evidence and the case policy. Introducing randomness, a model call, or
  a wall-clock-dependent decision into the verdict path is out of scope.

## Setting up

```bash
uv sync --extra dev          # create the environment with dev tools
uv run pytest -q             # run the test suite
uv run ruff check src tests  # lint
uv run mypy src              # type check
uv build                     # build the distributions
```

All four gates must be green. The same matrix runs in CI
(`.github/workflows/ci.yaml`) on Linux + macOS with Python 3.11 and 3.12.

## How to contribute

1. **Small, single-purpose changes.** One behavior per PR.
2. **Test-driven.** Write or extend the test that fails for the change you want
   (`tests/`), then make it pass. See `docs/tdd-journal.md` for how the core
   was built this way.
3. **Run the full gate set locally** before pushing.
4. **Document behavior changes** in `CHANGELOG.md` under `Unreleased`.

### Good first contributions

- A new **artifact adapter** for a well-specified, real benchmark schema.
  Use the [Adapter request](https://github.com/ozkangrk/llm-serve-verdict/issues/new?template=adapter_request.yml)
  form first so we agree on the schema and semantics before code.
- Clarifications to the README, the honest test matrix, or the diagrams.
- Fixing a flaky or missing test.

## Branching and commits

- Branch from `main`, e.g. `feature/short-description`.
- Conventional commit prefixes help: `feat:`, `fix:`, `docs:`, `test:`,
  `ci:`, `chore:`.
- Keep commits focused; squash noisy history before opening the PR.

## Code style

- Python 3.11+ typing, `from __future__ import annotations`.
- `ruff` is the linter/formatter of record (config in `pyproject.toml`).
- `mypy` runs in check mode over `src/`; keep new code type-checked.

## Process

1. Open an **issue** for anything non-trivial before writing code. Small fixes
   can go straight to a PR.
2. Open a **pull request** using the PR template; fill in all sections.
3. CI must pass; at least one maintainer approves.
4. We merge, and the change is noted in the changelog and (if user-visible) the
   release notes.

## What we do not accept

- Dependencies or features that broaden the surface (load generation, runtime
  control, LLM-backed verdicts, live system probing) — see "What it is not"
  in the README.
- Changes to the security invariants without prior discussion.
- Screenshots or marketing copy that overstate what is verified.

## Code of conduct

Contributors are expected to adhere to the [Code of Conduct](CODE_OF_CONDUCT.md).
