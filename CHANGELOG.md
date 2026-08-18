# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased] — v0.2

### Added

- **Open-source release surface** for v0.2:
  - `README.md` rewritten with a portable 30-second demo, the verdict
    primitive diagram, and an honest test matrix / claim boundaries.
  - Dark-theme architecture diagram (`docs/architecture-v0.2.svg`) of the
    implemented flow: case policy + hash → path-safe loader → schema adapter →
    metric semantics → gate engine → digest bundle → loopback UI.
  - Verdict primitive diagram (`docs/verdict-primitive.svg`); `PROMOTE` and
    `REJECT` are documented as first-class, equally-visible outcomes.
- **CI** (`.github/workflows/ci.yaml`): Linux + macOS × Python 3.11 + 3.12
  running pytest, ruff, mypy, and a distribution build (practical `uv` setup:
  `setup-uv` + `uv sync --frozen`).
- **Release workflow** (`.github/workflows/release.yaml`): `v*` tag → version
  match check → gates → build sdist + wheel → GitHub release with assets.
- **PyPI publish** (`.github/workflows/publish-pypi.yaml`): manual
  (`workflow_dispatch`) or tag-triggered, gated (version match, clean tree,
  full test/lint/type/build gates), publishing via trusted publishing (OIDC) —
  no workflow secrets.
- `SECURITY.md` (supported versions, private reporting channel, in-scope
  definition, invariants).
- `CONTRIBUTING.md` (setup, ground rules, process).
- `CODE_OF_CONDUCT.md` (Contributor Covenant v2.1).
- Issue templates: bug report, adapter request, configuration/reporting
  guidance; pull request template (`.github/`).
- `docs/release-checklist.md` (pre-tag, tag, post-release steps).
- `pyproject.toml` project metadata for packaging: version `0.2.0`,
  `keywords`, `classifiers`, `[project.urls]`, MIT license metadata.

### Changed

- `README.md` "Quick start" replaced by a portable `demo`-based flow; the
  broken absolute-path quickstart moved to an explicit **Advanced** section
  with an honest note that those cases do not reproduce on other machines.

### Notes / claim boundaries

- The `demo` command and fixture-portable behavior are provided by the v0.2
  backend workstream; the README documents them as of this surface but CI for
  `demo` is not yet run.
- Windows is not tested or supported in v0.2.
- No tag, no GitHub release, no PyPI publish, and no UI screenshots have been
  cut for v0.2 yet — see `docs/release-checklist.md`.

## [0.1.0] — MVP (v0.1)

### Added

- Initial MVP: `import-case`, `verify`, `list`, `show`, `serve` CLI commands.
- Path-safe evidence loader (canonical root, traversal/symlink/size guards,
  SHA-256 pin verification).
- Two artifact adapters: `qwen38.dspark-ab.v1`, `qwen38.sglang-vllm-ab.v1`.
- Metric semantic registry (5 fixed-semantic metrics, strict comparability).
- Deterministic decision engine (fixed rule order; PROMOTE/REJECT/
  INCONCLUSIVE).
- Tamper-evident bundles: canonical-JSON digest; offline `verify`.
- Loopback-only (127.0.0.1) read-only FastAPI server + offline UI.
- Test suite: 121 tests across unit, integration, and E2E (port release on
  SIGTERM verified).
