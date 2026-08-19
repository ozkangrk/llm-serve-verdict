# Release checklist

A tag/release is complete only when every applicable box is checked and the
commit/tree hash plus CI URL are recorded in the release notes.

## 0. Scope and claim boundary

- [ ] `CHANGELOG.md` contains the target version and honest limitations.
- [ ] README CLI/API/UI claims match the exact release tree.
- [ ] Historical MVP/TDD documents are not presented as the current surface.
- [ ] Real screenshots come from the exact release build; desktop and 390 px
      mobile have no horizontal page overflow.
- [ ] No claim says production-ready, deterministic model output, any-model, or
      stress/load testing without linked evidence.

## 1. Code state

- [ ] `pyproject.toml`, package `__version__`, wheel metadata and tag version are
      identical (for example `0.3.0` / `v0.3.0`).
- [ ] Working tree is clean and the release branch is based on current `main`.
- [ ] Artifact schemas and old v0.1/v0.2 bundle verification remain compatible.

## 2. Local and fresh-clone gates

```bash
uv sync --extra dev
uv run pytest -q
uv run ruff check src tests
uv run mypy src
uv build
```

- [ ] All gates pass in the working tree.
- [ ] The same gates pass in a fresh clone.
- [ ] The built wheel imports and its metadata version equals `__version__`.
- [ ] Portable demo emits one PROMOTE and one REJECT; both verify offline.
- [ ] Quick benchmark mock-HTTP E2E proves warmup exclusion, concurrency math,
      failure classification, secret absence and artifact tamper detection.
- [ ] Automation API tests prove loopback-only endpoint use, environment-only
      secrets, bounded jobs, cancellation/result discard and no data-dir writes.

## 3. Product E2E

- [ ] Run the Automation Wizard against an approved loopback endpoint.
- [ ] Confirm endpoint preflight, quick benchmark phases, gates and sealed result.
- [ ] Confirm Doctor/Capacity and Advisor/rollback artifacts on a real local
      configuration; label unexercised runtime/model combinations `UNTESTED`.
- [ ] Replay uses a privacy-reviewed local workload; raw prompts do not appear in
      artifacts, logs, errors or GitHub summaries.

## 4. Review and CI

- [ ] Independent fail-closed review confirms the exact tree hash.
- [ ] No open HIGH/MEDIUM finding.
- [ ] Pull-request CI passes Ubuntu/macOS × Python 3.11/3.12.
- [ ] Merge commit main CI passes the same matrix.

## 5. Tag and GitHub Release

- [ ] Create and push annotated `v<version>` on the release commit.
- [ ] Release workflow version check, tests, Ruff, mypy, build and wheel import
      all pass.
- [ ] GitHub Release includes wheel, sdist, release notes and real screenshots.
- [ ] Download the released wheel and verify import/version independently.

## 6. PyPI (optional and only after GitHub Release verification)

- [ ] PyPI Trusted Publisher exists for owner `ozkangrk`, repository
      `serving-verdict`, workflow `publish-pypi.yaml`, environment `pypi`.
- [ ] GitHub `pypi` environment approvals are configured if required.
- [ ] Publish workflow completes and `pip install serving-verdict==<version>`
      resolves from PyPI.
- [ ] If Trusted Publisher is absent, do not use a token shortcut; explicitly
      document that PyPI was skipped.

## 7. Post-release

- [ ] Verify tag points to the intended commit and GitHub Release is public.
- [ ] Record release URL, commit/tree hash, CI run and package SHA-256 values.
- [ ] Move completed changelog items from `Unreleased` to the versioned entry.
- [ ] Keep prior tags immutable; fix automation for the next patch release rather
      than moving a published tag.
