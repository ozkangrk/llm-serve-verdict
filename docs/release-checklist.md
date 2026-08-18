# Release checklist (v0.2)

Checklist for cutting a tag and publishing a release. A release is done only
when every box below is checked and the evidence is recorded (commit SHAs,
CI run URLs) in the release notes.

## 0. Preconditions (before starting)

- [ ] The backend workstream has merged: `demo --out-dir DIR` runs both
      bundled fixture cases portably; `list`/history and reindex behavior
      are present and tested.
- [ ] `serving-verdict demo --out-dir demo` then `serving-verdict list demo`
      shows one `PROMOTE` and one `REJECT` bundle on a fresh clone.
- [ ] No UI screenshot is committed until the UI is final; if screenshots are
      added to the README, they are real captures of the released build.

## 1. Code state

- [ ] `pyproject.toml` version is the exact target version (e.g. `0.2.0`) —
      the release workflow hard-fails on tag/version mismatch.
- [ ] `CHANGELOG.md` has no open `Unreleased` items that are actually done;
      the release entry is written with real, unexaggerated claims.
- [ ] `git status` is clean; branch is up to date with `main`.
- [ ] Working tree is clean (`git diff --exit-code` passes).

## 2. Local gates (all must pass)

```bash
uv run pytest -q            # full test suite green
uv run ruff check src tests # lint clean
uv run mypy src             # type check clean
uv build                    # sdist + wheel build
```

- [ ] All four pass locally, and the resulting wheel imports
      (`serving_verdict.__version__` prints the target version).
- [ ] `uv run serving-verdict demo --out-dir /tmp/sv-demo` works and both
      bundles verify.

## 3. CI

- [ ] Push to `main` and watch the CI matrix (Linux + macOS × 3.11 + 3.12):
      pytest, ruff, mypy, build — all four green on all four legs.
- [ ] Record the CI run URL for the release notes.

## 4. Tag

- [ ] Create an annotated tag **exactly** `v<version>` (e.g. `v0.2.0`) on the
      release commit, and push the tag.
- [ ] The release workflow: version-match check passes → gates pass →
      sdist + wheel built → GitHub release created with both artifacts.
- [ ] Download the released wheel from the GitHub release and verify it
      imports and prints the target version.

## 5. PyPI (only after the GitHub release is verified)

- [ ] Trusted publishing is configured (PyPI pipeline + GitHub OIDC identity
      for `repository:ozkangrk/serving-verdict`, audience `https://pypi.org`).
- [ ] If using the `pypi` environment guard, the environment approval has
      been granted.
- [ ] `Publish to PyPI` workflow (manual dispatch with the exact version, or
      tag-triggered) completes; `pip install serving-verdict==<version>`
      resolves and imports.
- [ ] If trusted publishing is **not** configured yet, do not force a manual
      token publish as a shortcut — finish the OIDC setup instead.

## 6. Post-release

- [ ] Move the changelog entry from `Unreleased` to the versioned entry.
- [ ] Link the GitHub release from the README if a pinned "latest" link is
      added.
- [ ] Note anything that could not be verified in the release notes (e.g.
      Windows) — no exaggerated claims.
- [ ] Archive: record the release commit SHA, the CI run URL, and the PyPI
      version in the release notes or a project note.
