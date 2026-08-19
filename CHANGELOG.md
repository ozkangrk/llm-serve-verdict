# Changelog

All notable changes to this project are documented in this file. The format is
based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the
project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

No unreleased user-facing changes.

## [0.3.0] — 2026-08-19

### Added

- Built-in OpenAI-compatible quick benchmark runner with frozen warmup, serial,
  concurrency, arithmetic, structured tool-call, and quality-lite workloads.
- Usage-backed TTFT, decode, end-to-end and shared-wall concurrency metrics;
  missing usage is `UNMEASURABLE`, never estimated.
- Credential-safe endpoint configuration and real preflight. API keys are read
  from environment variables and excluded from artifacts, logs and UI payloads.
- Serving Doctor and Capacity Planner for bounded hardware, model-memory,
  KV-cache, context and concurrency diagnostics.
- Deterministic Config Advisor with strict runtime flag allowlists, one-variable
  experiment plans, inert launch argv and exact rollback recipes.
- Baseline/candidate comparison, seeded sweep planning, constraint filtering and
  deterministic Pareto frontiers with tamper-evident experiment artifacts.
- Privacy-safe workload replay and CI regression gates. Raw prompts and executor
  errors are not persisted in replay artifacts.
- Loopback Automation Wizard with one bounded in-memory benchmark job, progress,
  cooperative cancellation and result discard. Trial/data storage remains
  read-only.
- Desktop and 390 px mobile Automation Wizard screenshots.

### Changed

- Package version is `0.3.0`.
- README workflow, CLI/API reference, security boundaries and product scope now
  cover built-in automation.
- UI timestamps use real UTC getters instead of labeling local time as UTC.

### Verification

- Fresh clone: 619 tests, Ruff, mypy over 41 source files, sdist and wheel build.
- GitHub CI passed on Ubuntu/macOS with Python 3.11/3.12 before and after merge.
- Main merge commit: `4c78776d429512ea08d7f6863d4859de300f4ce2`.

### Claim boundaries

- The quick profile is a bounded benchmark, not a stress/load-testing platform.
- Automation never starts, stops or reconfigures a runtime.
- Cancellation is cooperative: an in-flight blocking HTTP call may finish, but
  its result is discarded after cancellation is requested.
- Windows remains untested. PyPI publication is not part of this release state.

## [0.2.0] — 2026-08-18

### Added

- Portable two-case demo with first-class PROMOTE and REJECT outcomes.
- Verdict-first responsive, self-contained UI and real desktop/mobile captures.
- Append-only SQLite trial history, content-addressed archive and offline
  verification.
- Relative evidence roots without CWD fallback; external archive roots are
  manifest-bound and verified.
- Loopback read-only verdict/trial/artifact APIs, readiness endpoint and OSS
  release surface: CI, security policy, contribution docs and issue templates.
- Linux/macOS × Python 3.11/3.12 CI plus release and guarded PyPI workflows.

### Release

- GitHub Release: https://github.com/ozkangrk/serving-verdict/releases/tag/v0.2.0
- Wheel, sdist and five verified UI screenshots are attached.
- Independent fail-closed review closed all HIGH/MEDIUM findings before tag.
- PyPI trusted publishing was not configured; `0.2.0` was not published there.

## [0.1.0] — MVP

### Added

- Initial `import-case`, `verify`, `list`, `show`, and `serve` commands.
- Path-safe evidence loader with traversal, symlink, special-file, size and
  SHA-256 guards.
- Two artifact adapters and a fixed-semantic metric registry.
- Deterministic PROMOTE/REJECT/INCONCLUSIVE engine and tamper-evident bundles.
- Loopback-only FastAPI server and self-contained UI.
- 121 unit, integration and E2E tests.
