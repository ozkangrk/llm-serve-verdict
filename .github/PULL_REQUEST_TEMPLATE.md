## What this PR does

<!-- One or two sentences. What behavior changes, and for whom. -->

## Why

<!-- Link the issue(s): Closes #NNN. If no issue, explain the motivation. -->

## Ground rules checklist

- [ ] I did not add exaggerated claims (README, docs, or comments).
- [ ] I preserved the security and determinism invariants (see SECURITY.md).
      If this PR touches them, I opened a discussion **before** writing code.
- [ ] I added/updated a test that fails without the change and passes with it.
- [ ] I updated CHANGELOG.md under `Unreleased` (if user-visible).
- [ ] I did not commit UI screenshots that are not from a released build.

## Verification

<!-- Paste the real, local output. Do not paraphrase. -->

```bash
# e.g.
$ uv run pytest -q
# ...

$ uv run ruff check src tests
# All checks passed!

$ uv run mypy src
# Success: no issues found in N source files

$ uv build
# Successfully built dist/...
```

## Out of scope / follow-ups

<!-- Anything intentionally left for a later PR. -->
