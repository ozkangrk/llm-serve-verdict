# Backend v0.2 TDD Evidence

## Baseline

- Existing v0.1 suite: 121 passing.

## RED

New tests were written before implementation for:

- relative and overridden source roots;
- portable deterministic demo generation;
- append-only SQLite trial history and migration;
- content-addressed evidence archival and offline verification;
- history/reindex CLI contracts;
- read-only readiness, trial, and artifact APIs.

Initial failures were caused by missing modules/commands and later exposed concrete implementation defects: missing SQLite row factories/migration initialization, unresolved relative roots, readiness returning HTTP 200 for a missing data directory, and an incorrect archive fixture layout.

## GREEN

Final verified commands:

```text
uv run pytest -q                         176 passed
uv run ruff check src tests              All checks passed
uv run mypy src                          Success: 14 source files
uv build                                 sdist + wheel built
```

Security behavior retained:

- HTTP remains loopback-only and read-only.
- Server registry access uses SQLite read-only mode.
- Evidence files are bounded, path-confined, copied atomically, and re-hashed.
- Missing evidence produces INCONCLUSIVE; integrity failures fail closed.
- No dynamic plugins, shell execution, or runtime mutation were added.
