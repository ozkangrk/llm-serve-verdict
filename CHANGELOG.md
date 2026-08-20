# Changelog

All notable changes to this project are documented in this file. The format is
based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the
project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `bench ab` automated endpoint experiment runner: alternating baseline/candidate
  order, bounded repeated trials, hard-gate precedence, deterministic bootstrap
  decision, sealed per-trial/statistics artifacts and atomic output publishing.
- Fail-closed A/B verification binds safe filenames, endpoint/model/protocol/
  workload context, run statuses, metric samples, statistical policy and final
  decision; `bench ab-verify` checks the bounded directory before CI consumes
  it, and API-key values are rejected from runner artifacts.

### Changed

- README scenario actor is now Alex and the landing page includes measured test
  results plus the end-to-end A/B automation command.

### Verification

- Exact-tree collection: 989 tests; 16 focused A/B unit/CLI tests.
- Ruff, mypy over 52 source files, wheel and sdist build pass.

## [0.4.0] — 2026-08-20

### Added

- Repeated-trial statistical engine with deterministic seeded bootstrap
  confidence intervals, explicit sample-size/uncertainty outcomes, direction-
  aware effects and sealed statistical artifacts.
- Structured `serving-verdict.bundle.v0.4` evidence manifest and claim boundary.
- Offline DSSE + Ed25519 verdict signing, strict local trust stores and
  `verify --require-signature` with distinct integrity/trust failure codes.
- Immutable adapter SDK plus strict experimental vLLM, SGLang and GuideLLM
  saved-result adapters with upstream commit-pinned semantic documentation.
- Stable `gate --require PROMOTE` CI contract, exit codes 0/2/4/5/6, bounded
  GitHub summaries and a fail-closed composite GitHub Action.
- CodeQL, dependency audit and secret-scan workflows; release SHA256SUMS and
  GitHub build-provenance attestation foundation.
- v0.5 Inference Lab preview foundation: digest-pinned inert runtime templates,
  pure typed planner/model manifest, owned lifecycle cleanup state machine and
  bounded Prometheus-text telemetry normalization.
- v0.5 architecture SVG/HTML and normative Inference Lab product/runtime spec.

### Changed

- Package version is `0.4.0`.
- Product, repository and distribution are renamed to `LLM ServeVerdict`,
  `llm-serve-verdict`; the `serving_verdict` import package, schema IDs and
  legacy `serving-verdict` CLI alias remain compatible.
- Promotion claims now distinguish digest integrity from signer authenticity.
- Runtime, adapter, statistics, trust and CI behavior are documented as stable
  machine-facing contracts with explicit residual limitations.

### Verification

- Integrated collection: 973 tests.
- Ruff and mypy over 51 source files; sdist and wheel build.
- Python 3.11 cross-version statistics golden verification.
- Independent fail-closed reviews closed every reported HIGH/MEDIUM finding in
  statistics, signing/trust, adapters, runtime planning, lifecycle, telemetry
  and CI-gate slices before integration.

### Claim boundaries

- DSSE + Ed25519 is offline allowlist verification; no Sigstore transparency,
  OIDC identity, revocation or timestamp-authority claim.
- Ecosystem adapters are experimental because upstream saved-result formats do
  not all provide stable versioned JSON schemas.
- Inference Lab Docker execution, Lab/Live/Decide APIs and real GPU E2E remain
  v0.5 release work; v0.4 ships safe planning/lifecycle/telemetry foundations,
  not a production container control plane.
- Windows remains untested. PyPI trusted publishing remains intentionally
  unconfigured.

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

- GitHub Release: https://github.com/ozkangrk/llm-serve-verdict/releases/tag/v0.2.0
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
