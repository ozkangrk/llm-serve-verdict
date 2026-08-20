# LLM ServeVerdict — Product Requirements Document

**Document:** PRD v0.4 → v1.0
**Repository:** `ozkangrk/llm-serve-verdict`
**Date:** 2026-08-19
**Status:** Proposed
**Primary goal:** Evolve LLM ServeVerdict from a strong local inference-engineering prototype into a production-grade, ecosystem-integrated, cryptographically verifiable promotion authority for LLM serving changes.

---

## 1. Executive Summary

LLM ServeVerdict exists to answer one operational question:

> **Should a candidate inference-serving configuration replace the current baseline?**

The product must not become another generic benchmark runner, GPU dashboard, serving runtime, or observability platform. Its durable differentiation is to sit **between evidence producers and deployment systems** and convert benchmark/runtime evidence into a deterministic, policy-controlled, reproducible and eventually cryptographically verifiable decision:

- `PROMOTE`
- `REJECT`
- `INCONCLUSIVE`

The next stage of the product should focus on five capabilities:

1. **Statistically trustworthy verdicts** — repeated measurements, confidence intervals, effect thresholds and explicit uncertainty.
2. **Cryptographically verifiable provenance** — signed verdict bundles, signed evidence manifests and verifiable build/runtime identity.
3. **Ecosystem adapters** — ingest evidence from common inference benchmark and serving stacks without rebuilding them.
4. **Production release-gate integration** — GitHub Actions, generic CI, Argo/Kubernetes/KServe-style deployment workflows and machine-readable policy outcomes.
5. **Operational hardening** — stable schemas, branch/release protection, backward compatibility, security boundaries, observability of the tool itself and reproducible releases.

The product should remain **evidence-authoritative and fail-closed**. No LLM opinion may participate in the final verdict path.

---

# 2. Product Positioning

## 2.1 Problem

Inference teams regularly change:

- runtime versions,
- quantization schemes,
- speculative decoding modes,
- tensor/data parallelism,
- batching settings,
- KV-cache configuration,
- prefix caching,
- context limits,
- concurrency,
- GPU memory utilization,
- model revisions,
- serving flags,
- hardware placement.

A candidate can appear faster in one benchmark while being operationally worse because of:

- increased TTFT,
- tool-call failures,
- malformed structured output,
- instability/crashes,
- memory regressions,
- higher tail latency,
- non-comparable benchmark conditions,
- workload mismatch,
- insufficient sample size,
- noisy measurements,
- evidence tampering or provenance ambiguity.

Most benchmark tools answer:

> “What was faster?”

LLM ServeVerdict must answer:

> **“Is the available evidence sufficient and trustworthy enough to authorize promotion?”**

---

## 2.2 Product Category

LLM ServeVerdict should position itself as:

> **An evidence-gated promotion controller for LLM inference changes.**

It is not primarily:

- a serving runtime,
- a model gateway,
- a load generator,
- an observability backend,
- a runtime manager,
- a model optimizer,
- an LLM-based tuning agent.

Those systems are evidence producers or execution targets. LLM ServeVerdict is the **decision authority layer** between them.

---

## 2.3 Core Product Loop

```text
Evidence producers
  ├─ vLLM benchmark
  ├─ SGLang benchmark
  ├─ GuideLLM
  ├─ GenAI-Perf
  ├─ llama.cpp benchmark
  ├─ custom OpenAI-compatible runner
  ├─ production replay
  └─ correctness/stability checks
          │
          ▼
┌─────────────────────────────────────┐
│          LLM SERVEVERDICT            │
│                                     │
│ Evidence manifest + provenance      │
│ Adapter normalization               │
│ Metric semantic registry            │
│ Statistical analysis                │
│ Policy engine                       │
│ Hard correctness/stability gates    │
│ Signed decision artifact            │
└─────────────────────────────────────┘
          │
          ▼
PROMOTE / REJECT / INCONCLUSIVE
          │
          ├─ CI/CD gate
          ├─ GitHub check
          ├─ Argo/Kubernetes workflow
          ├─ Release record
          └─ Offline audit/verification
```

---

# 3. Product Principles

The following principles are non-negotiable.

## P1 — Deterministic verdict path

The same bound evidence, policy and product version must generate the same verdict.

No free-form LLM output may influence `PROMOTE`, `REJECT` or `INCONCLUSIVE`.

## P2 — Fail closed

If evidence is missing, malformed, untrusted, unsupported, statistically insufficient or semantically incomparable, the system must prefer `INCONCLUSIVE` over guessing.

## P3 — Hard gates override speed

Correctness, stability, security-sensitive validation and explicitly required policy gates must override performance improvement.

## P4 — Evidence is immutable by identity

Evidence must be referenced by digest, not only by path or filename.

## P5 — Metric semantics are explicit

`tok/s` is not one universal metric. Decode throughput, end-to-end throughput and aggregate throughput must remain distinct.

## P6 — External tools remain external

LLM ServeVerdict should integrate with mature benchmark/runtime tools through adapters rather than duplicating their entire feature sets.

## P7 — Negative results are first-class

`REJECT` and `INCONCLUSIVE` are successful outcomes of the tool.

## P8 — Claims have boundaries

Every verdict must declare exactly what workload, runtime, model, hardware, benchmark procedure and evidence set it applies to.

---

# 4. Goals

## 4.1 v0.4 Goals

v0.4 should make the verdict scientifically and cryptographically stronger.

Required outcomes:

- repeated-trial statistical analysis,
- confidence-aware `INCONCLUSIVE`,
- signed verdict bundles,
- evidence manifest signing,
- provenance metadata model,
- adapter SDK/interface,
- at least three ecosystem adapters,
- machine-readable CI gate output,
- schema versioning rules,
- repository/release hardening.

## 4.2 v0.5 Goals

v0.5 should make the product practical inside real inference teams.

Required outcomes:

- 5–6 production-quality adapters,
- CI/CD integrations,
- representative workload replay policies,
- richer policy language,
- baseline registry/history,
- candidate experiment lineage,
- signed release artifacts,
- external contributor documentation.

## 4.3 v1.0 Goals

v1.0 should provide a stable contract that a team can place in front of production serving changes.

Required outcomes:

- stable bundle schema,
- stable CLI/API contract,
- adapter compatibility guarantees,
- backward verification of older signed verdicts,
- documented threat model,
- documented operational model,
- reproducible releases,
- at least one full end-to-end deployment integration,
- real-world case studies on multiple serving stacks.

---

# 5. Non-Goals

Unless later approved by a separate PRD, LLM ServeVerdict must not become:

1. A general-purpose Kubernetes inference platform.
2. A replacement for vLLM, SGLang, TensorRT-LLM or llama.cpp.
3. A generic model gateway.
4. A full synthetic load-testing platform.
5. A Prometheus/Grafana/OpenTelemetry backend.
6. A GPU fleet scheduler.
7. A full model quality evaluation platform.
8. An autonomous LLM agent that can promote itself to production.
9. A general-purpose experiment tracker replacing MLflow/W&B.
10. A model downloader/quantizer/optimizer.

Supporting utilities may exist, but they must remain subordinate to the evidence → verdict flow.

---

# 6. Primary Users

## 6.1 Inference Engineer

Needs to compare baseline and candidate serving configurations and obtain a reproducible decision.

## 6.2 MLOps / Platform Engineer

Needs a machine-readable release gate that can be inserted into CI/CD.

## 6.3 Performance Engineer

Needs to ensure metrics are semantically comparable and statistically meaningful.

## 6.4 Reviewer / Approver

Needs to inspect why a candidate was promoted, rejected or marked inconclusive.

## 6.5 Auditor / Security Reviewer

Needs to verify evidence integrity, provenance and signatures offline.

---

# 7. Core User Stories

## US-01 — Compare candidate against baseline

As an inference engineer, I want to bind baseline and candidate evidence to a policy so that the product can issue a deterministic verdict.

## US-02 — Avoid false promotion from noisy benchmarks

As a performance engineer, I want repeated trials and confidence intervals so that small noisy improvements do not get promoted incorrectly.

## US-03 — Reject operationally broken candidates

As a platform engineer, I want correctness and stability gates to override performance gains.

## US-04 — Verify verdict authenticity

As an approver, I want a signed verdict that proves who/what generated it and whether it changed after issuance.

## US-05 — Consume external benchmark formats

As an engineer, I want to import outputs from standard inference tools without manually transforming them.

## US-06 — Gate deployment in CI

As a platform engineer, I want a command that returns a stable machine-readable verdict and exit code suitable for CI.

## US-07 — Understand why evidence is insufficient

As an engineer, I want `INCONCLUSIVE` to explain whether the cause is missing evidence, incompatible semantics, insufficient statistical confidence or trust/provenance failure.

## US-08 — Reproduce an old decision

As a reviewer, I want to verify an old verdict offline using the bound artifacts and trust material.

---

# 8. Functional Requirements

# FR-1 — Statistical Verdict Engine

## FR-1.1 Repeated trials

The engine must support multiple baseline and candidate samples for a metric.

Minimum model:

```yaml
baseline:
  trials:
    - run-001.json
    - run-002.json
    - run-003.json

candidate:
  trials:
    - run-101.json
    - run-102.json
    - run-103.json
```

## FR-1.2 Configurable minimum sample count

Policy must define minimum acceptable sample count.

Example:

```yaml
statistics:
  min_trials: 5
```

If the requirement is not met:

```text
INCONCLUSIVE / INSUFFICIENT_SAMPLE_SIZE
```

## FR-1.3 Confidence intervals

The product must support confidence intervals for effect estimates.

Recommended initial implementation:

- bootstrap confidence interval,
- deterministic seeded resampling,
- default confidence level: 95%,
- configurable bootstrap iteration count,
- all statistical parameters included in the verdict bundle.

## FR-1.4 Effect decision policy

A policy must be able to express:

```yaml
statistics:
  confidence_level: 0.95
  min_relative_improvement: 0.05
  decision_mode: confidence_interval
```

Decision semantics:

- lower confidence bound ≥ required improvement → `PROMOTE`, subject to all gates.
- upper confidence bound < required improvement → `REJECT` for insufficient effect.
- interval overlaps threshold → `INCONCLUSIVE`.

## FR-1.5 Lower-is-better metrics

Statistical effect calculation must preserve metric direction.

Examples:

- throughput: higher is better,
- TTFT: lower is better,
- latency: lower is better.

## FR-1.6 Distribution metadata

Verdict bundle should include:

- sample count,
- median,
- mean if used,
- standard deviation,
- p50/p95 where relevant,
- effect estimate,
- confidence interval,
- outlier policy,
- test/procedure version.

## FR-1.7 Outlier handling

Outlier removal must never happen implicitly.

If supported, policy must explicitly select it and bundle must record:

- method,
- threshold,
- removed sample IDs,
- original sample count,
- final sample count.

Default: no outlier removal.

## FR-1.8 Warmup treatment

Warmups must not be mixed with measured runs.

Warmup procedure and measured-run procedure must be identified separately.

---

# FR-2 — Strong Provenance and Cryptographic Verification

## FR-2.1 Signed verdict bundles

A plain SHA-256 self-digest is not sufficient for authenticity.

The product must support cryptographic signing of verdict bundles.

Preferred options:

- Sigstore/cosign,
- DSSE envelope,
- in-toto-style attestation.

Implementation may start with one signing backend, but the bundle schema should allow future backends.

## FR-2.2 Signature verification

CLI must provide:

```bash
llm-serve-verdict verify verdict.json
llm-serve-verdict verify verdict.json --require-signature
```

Verification should distinguish:

- digest valid,
- signature valid,
- signer trusted,
- provenance valid,
- evidence available/unavailable.

## FR-2.3 Evidence manifest

Each verdict must bind an evidence manifest containing at minimum:

- artifact ID,
- SHA-256,
- artifact schema,
- size,
- producer,
- production timestamp,
- tool version,
- source type,
- model identity,
- runtime identity,
- hardware identity or hardware class,
- benchmark procedure version.

## FR-2.4 Runtime identity

Where available, bind:

- runtime name,
- runtime version,
- container image digest,
- executable version,
- relevant serving flags.

Do not rely only on mutable tags such as `latest`.

## FR-2.5 Model identity

Where available, bind:

- model repository/name,
- revision/commit,
- local artifact digest,
- quantization format,
- tokenizer revision.

## FR-2.6 Hardware provenance

Capture enough information to detect invalid comparisons, for example:

- GPU model,
- GPU count,
- driver version,
- CUDA/runtime version,
- host architecture,
- relevant memory capacity.

The system should avoid collecting unnecessary machine secrets/identifiers.

## FR-2.7 Timestamp integrity

Signed payload should cover the issued timestamp.

If an unsigned compatibility digest excludes volatile timestamps, the signed envelope must still bind the issuance time.

## FR-2.8 Trust policy

Policy should be able to require:

```yaml
trust:
  require_signed_evidence: true
  require_signed_verdict: true
  allowed_signers:
    - ci@company.example
```

## FR-2.9 Offline verification

A verifier must be able to validate stored bundles without contacting the original benchmark endpoint.

Network-free verification should be supported when trust roots are locally available.

---

# FR-3 — Adapter Architecture

## FR-3.1 Adapter contract

Create a documented adapter interface.

Conceptual interface:

```python
class EvidenceAdapter(Protocol):
    adapter_id: str
    schema_versions: set[str]

    def detect(self, artifact: EvidenceBlob) -> DetectionResult: ...
    def parse(self, artifact: EvidenceBlob) -> NormalizedEvidence: ...
```

## FR-3.2 Adapter responsibilities

Adapters may:

- detect supported source format,
- validate source schema,
- extract metrics,
- map metric semantics,
- extract runtime/model metadata,
- extract test status,
- preserve original source digest.

Adapters must not:

- silently infer missing semantics,
- fabricate missing metrics,
- normalize incompatible metrics as equivalent,
- execute artifact content.

## FR-3.3 Adapter capability declaration

Every adapter must declare:

- supported source versions,
- metrics emitted,
- semantic limitations,
- known unsupported fields,
- test fixtures,
- compatibility status.

## FR-3.4 Required adapters for v0.4

At least three of:

1. vLLM benchmark output
2. SGLang benchmark output
3. GuideLLM
4. NVIDIA GenAI-Perf
5. llama.cpp benchmark output

Recommended v0.4 minimum:

- vLLM,
- SGLang,
- GuideLLM.

## FR-3.5 Required adapters for v0.5

Target at least 5–6 production-quality adapters.

## FR-3.6 Generic custom adapter

Support a documented normalized JSON format so internal teams can integrate custom runners without modifying core code.

## FR-3.7 Adapter isolation

Malformed adapter input must not crash the core process.

Unsupported or invalid input must become a structured failure such as:

```text
INCONCLUSIVE / UNSUPPORTED_SCHEMA
INCONCLUSIVE / INVALID_ADAPTER_PAYLOAD
```

---

# FR-4 — Metric Semantic Registry v2

## FR-4.1 Stable metric definitions

Each metric must have:

- canonical ID,
- unit,
- direction,
- procedure version,
- aggregation semantics,
- definition.

## FR-4.2 Expanded metrics

Candidate v0.4/v0.5 registry additions:

- ITL / inter-token latency,
- p95 TTFT,
- p95 request latency,
- request success rate,
- tool-call success rate,
- structured-output success rate,
- tokens/sec/GPU,
- peak GPU memory,
- KV-cache utilization,
- process crash count,
- request error count.

Only add a metric if semantics are precise.

## FR-4.3 Comparability dimensions

Comparability should consider at minimum:

- metric ID,
- unit,
- procedure version,
- workload ID,
- concurrency,
- input/output token budget,
- thinking/reasoning mode,
- warm/cold state,
- aggregation,
- streaming/non-streaming mode,
- model revision,
- runtime class where required.

## FR-4.4 Explicit compatibility rules

If two procedure versions can be compared, compatibility must be declared explicitly.

No implicit cross-version comparison.

---

# FR-5 — Policy Engine v2

## FR-5.1 Policy schema

Introduce a versioned policy schema.

Example:

```yaml
policy_version: serving-verdict.policy.v0.4

primary_metric:
  id: decode_tokens_per_s
  min_relative_improvement: 0.05

statistics:
  min_trials: 5
  confidence_level: 0.95
  method: bootstrap

hard_gates:
  - tool_call_success_rate >= 0.99
  - process_crashes == 0
  - request_success_rate >= 0.995

regression_gates:
  ttft_s:
    max_relative_regression: 0.10

trust:
  require_signed_evidence: true
  require_signed_verdict: true
```

## FR-5.2 Stable evaluation order

Rule order must remain explicit and documented.

Recommended v0.4 order:

1. config validity,
2. evidence availability,
3. evidence integrity,
4. trust/signature policy,
5. schema support,
6. semantic comparability,
7. minimum sample requirement,
8. hard correctness/stability gates,
9. required-gate completeness,
10. latency/regression gates,
11. statistical effect gate,
12. final verdict.

## FR-5.3 Reason codes

Reason codes are part of the public contract.

Examples:

- `EVIDENCE_UNAVAILABLE`
- `EVIDENCE_HASH_MISMATCH`
- `SIGNATURE_REQUIRED`
- `SIGNATURE_INVALID`
- `UNTRUSTED_SIGNER`
- `UNSUPPORTED_SCHEMA`
- `METRIC_NOT_COMPARABLE`
- `INSUFFICIENT_SAMPLE_SIZE`
- `STATISTICAL_UNCERTAINTY`
- `HARD_GATE_FAILED`
- `REQUIRED_GATE_MISSING`
- `TTFT_REGRESSION`
- `INSUFFICIENT_EFFECT`
- `PRIMARY_EFFECT_PASSED`
- `ALL_REQUIRED_GATES_PASSED`

Reason-code behavior must be tested for backward compatibility.

---

# FR-6 — Workload Replay and Correctness Gates

## FR-6.1 Frozen workload sets

Representative workloads must be versioned and immutable by digest.

## FR-6.2 Privacy-safe replay

Stored artifacts must avoid persisting raw sensitive prompts by default.

Supported strategies:

- redacted prompts,
- hashed identifiers,
- fixture IDs,
- encrypted external storage references,
- non-reversible workload fingerprints.

## FR-6.3 Tool-call correctness

Provide a normalized gate for:

- valid tool-call format,
- expected tool name,
- required argument presence,
- JSON validity,
- schema validity.

## FR-6.4 Structured output

Support JSON/schema compliance as a hard gate.

## FR-6.5 Application-level gates

Allow external gate evidence to be attached, for example:

- domain eval pass/fail,
- response contract validation,
- safety test pass/fail,
- regression suite pass/fail.

LLM ServeVerdict should consume these as evidence, not implement all domain evaluation itself.

---

# FR-7 — CI/CD Integration

## FR-7.1 Stable exit codes

Define stable exit codes suitable for automation.

Recommended:

- `0`: valid verdict produced and command executed successfully,
- `2`: usage/config failure,
- `4`: integrity/signature verification failure,
- `5`: policy explicitly blocks deployment (`REJECT`), optional CI mode,
- `6`: evidence insufficient (`INCONCLUSIVE`), optional CI mode.

CLI must preserve current compatibility unless a major-version change is made.

## FR-7.2 CI mode

Provide:

```bash
llm-serve-verdict gate verdict.json --require PROMOTE
```

or equivalent.

## FR-7.3 GitHub Actions integration

Provide an official reusable workflow or action that:

- runs verification,
- prints summary,
- optionally uploads the verdict artifact,
- fails on `REJECT`,
- optionally fails on `INCONCLUSIVE`,
- exposes verdict/reason codes as outputs.

## FR-7.4 Generic JSON output

Every automation-facing command must support one JSON object on stdout and diagnostics on stderr.

## FR-7.5 Deployment integration contract

Document generic integration for:

```text
benchmark job
→ serving-verdict
→ deployment approval/gate
```

At least one reference integration should be implemented before v1.0.

Candidates:

- Argo Workflows / Argo CD,
- KServe deployment pipeline,
- Kubernetes Job + admission-style gate,
- generic GitHub Actions deployment.

---

# FR-8 — Verdict Bundle v2

## FR-8.1 Versioned schema

Create a new bundle schema version with explicit compatibility rules.

## FR-8.2 Required sections

Suggested structure:

```json
{
  "schema_version": "serving-verdict.bundle.v0.4",
  "case": {},
  "baseline": {},
  "candidate": {},
  "evidence_manifest": [],
  "comparisons": [],
  "statistics": {},
  "gates": [],
  "trust": {},
  "verdict": "PROMOTE",
  "reason_codes": [],
  "claim_boundary": {},
  "issued_at": "...",
  "producer": {},
  "digest": "sha256:...",
  "signature": {}
}
```

## FR-8.3 Claim boundary structure

Replace free-text-only claim boundaries with structured fields where possible:

- workload set,
- runtime,
- model,
- hardware class,
- benchmark procedure,
- candidate delta,
- observed period,
- limitations.

Optional human-readable summary may remain.

## FR-8.4 Backward verification

New releases must continue to verify older supported bundle versions.

---

# FR-9 — Experiment Lineage

## FR-9.1 Candidate delta

Every experiment should record exactly what changed.

Example:

```yaml
candidate_delta:
  parameter: speculative_num_steps
  baseline: 5
  candidate: 7
```

## FR-9.2 One-variable experiment support

Config Advisor may continue to generate one-variable experiments, but the verdict system must not require the advisor.

## FR-9.3 Multi-variable experiments

If multiple variables change, the bundle must record all changes and identify the experiment as multi-variable.

## FR-9.4 Baseline lineage

A promoted candidate may become the next baseline.

Maintain lineage:

```text
baseline-A
  ↓ promote
baseline-B
  ↓ promote
baseline-C
```

This should support audit and rollback reasoning.

---

# FR-10 — Rollback Evidence

## FR-10.1 Rollback recipe

Promotion artifact should optionally include a machine-readable rollback recipe or rollback reference.

## FR-10.2 Rollback proof

For systems supporting runtime lifecycle integration, a promotion can optionally require proof that the prior baseline can be restored and health-checked.

## FR-10.3 Scope boundary

Core LLM ServeVerdict should not directly mutate arbitrary runtimes in v0.4.

Runtime mutation must remain behind an adapter/integration boundary and require separate explicit enablement.

---

# FR-11 — UI Requirements

The UI is useful, but it must not become the primary product architecture.

## FR-11.1 Verdict-first UI

The main screen must prioritize:

- verdict,
- reason,
- confidence/statistical status,
- failed gates,
- evidence trust status,
- candidate delta.

## FR-11.2 Evidence drill-down

Users should inspect:

- exact evidence artifacts,
- hashes,
- adapter used,
- semantic dimensions,
- statistical samples,
- signatures,
- claim boundary.

## FR-11.3 Comparison visualization

Optional charts may show:

- baseline vs candidate distributions,
- confidence interval,
- Pareto trade-offs,
- latency/throughput deltas.

The visual layer must never change decision semantics.

## FR-11.4 Negative-result UX

`REJECT` and `INCONCLUSIVE` should have equivalent detail quality to `PROMOTE`.

---

# 9. Non-Functional Requirements

# NFR-1 — Determinism

Given the same:

- evidence bytes,
- policy bytes,
- product version,
- statistical seed,

verdict output must be byte-stable except explicitly volatile envelope metadata.

# NFR-2 — Security

- Never execute imported evidence.
- Reject path traversal.
- Reject symlink escape.
- Reject special files.
- Bound input sizes.
- Avoid shell execution from untrusted input.
- Keep secrets environment-only.
- Redact credentials from errors/logs.
- Loopback-only UI remains default.
- Any remote/server mode requires a separate explicit security model.

# NFR-3 — Performance

LLM ServeVerdict should not become the bottleneck relative to benchmark execution.

Target:

- decision calculation for typical local evidence: <1 second,
- verification of normal bundles: <1 second excluding external transparency-log access,
- bounded memory behavior on malformed artifacts.

# NFR-4 — Portability

v1.0 target:

- Linux supported,
- macOS supported,
- Windows support either tested or explicitly unsupported.

# NFR-5 — Reproducibility

- lock dependencies,
- reproducible package build as far as practical,
- release artifacts include checksums/signatures,
- exact source commit linked to release.

# NFR-6 — Compatibility

- schemas are versioned,
- adapters declare supported source versions,
- CLI breaking changes require major-version policy,
- reason codes are documented API.

# NFR-7 — Testability

Every decision rule must have:

- positive test,
- negative test,
- boundary test,
- adversarial/malformed-input test where applicable.

---

# 10. Security and Threat Model

A formal `THREAT_MODEL.md` should be added.

Threats to cover:

## T1 — Evidence modification

Attacker changes source benchmark evidence.

Mitigation:

- digest binding,
- signed evidence manifest,
- trust policy.

## T2 — Verdict modification

Attacker changes `REJECT` to `PROMOTE` and recomputes a plain hash.

Mitigation:

- cryptographic signature over the canonical verdict payload.

## T3 — Path escape

Malicious config references files outside approved evidence root.

Mitigation:

- existing canonical path protections,
- tests retained.

## T4 — Adapter confusion

Artifact is interpreted under the wrong schema.

Mitigation:

- strict adapter detection,
- explicit schema version,
- no ambiguous silent fallback.

## T5 — Semantic metric confusion

Two values have the same unit but different benchmark semantics.

Mitigation:

- semantic registry,
- strict comparability dimensions.

## T6 — CI impersonation

Untrusted actor issues a valid-looking verdict.

Mitigation:

- signer allowlist,
- OIDC/Sigstore identity,
- signed releases.

## T7 — Secret leakage

API keys appear in evidence or logs.

Mitigation:

- environment-only secrets,
- redaction tests,
- prohibited persisted fields.

---

# 11. Repository and Release Engineering Requirements

## RR-1 — Protect main branch

Enable:

- required pull request review,
- required CI checks,
- no direct push,
- branch protection,
- optional signed commits,
- required linear/squash policy if desired.

## RR-2 — Required CI

At minimum:

- pytest,
- Ruff,
- mypy,
- build wheel/sdist,
- wheel import test,
- schema compatibility tests,
- signature verification tests,
- adapter fixture tests.

## RR-3 — Security automation

Add:

- dependency vulnerability scanning,
- secret scanning,
- CodeQL or equivalent static analysis,
- pinned GitHub Actions where practical.

## RR-4 — Signed releases

Release artifacts should include:

- wheel,
- sdist,
- SHA-256 checksums,
- signature/attestation,
- source commit identity,
- changelog.

## RR-5 — PyPI publication

Before public v1.0:

- trusted publishing configured,
- package ownership documented,
- release process tested from tag to PyPI.

## RR-6 — Version consistency

One source of truth for package version.

CI should fail if:

- package metadata,
- changelog,
- tag,
- runtime `__version__`

disagree.

---

# 12. Documentation Requirements

Required docs:

1. `README.md` — concise product story and quickstart.
2. `ARCHITECTURE.md` — core architecture and trust boundaries.
3. `THREAT_MODEL.md` — attacker model and mitigations.
4. `ADAPTERS.md` — adapter contract and support matrix.
5. `POLICY_REFERENCE.md` — full policy schema.
6. `BUNDLE_SCHEMA.md` — bundle format and compatibility.
7. `STATISTICS.md` — statistical methodology and limitations.
8. `CI_INTEGRATION.md` — GitHub/generic CI usage.
9. `SECURITY.md` — vulnerability reporting.
10. `CONTRIBUTING.md` — contribution standards.
11. `RELEASE.md` — release process.
12. `CASE_STUDIES.md` — real promote/reject/inconclusive examples.

---

# 13. Test Requirements

## TR-1 Statistical engine

Tests must cover:

- clear promote,
- clear reject,
- confidence interval crossing threshold,
- insufficient trials,
- lower-is-better metrics,
- deterministic bootstrap seed,
- zero/negative invalid baseline values,
- extreme outliers,
- non-finite values.

## TR-2 Signatures

Tests must cover:

- valid signature,
- mutated verdict,
- mutated evidence manifest,
- wrong signer,
- missing signer,
- expired/revoked trust policy if supported,
- offline verification.

## TR-3 Adapters

Every adapter requires:

- real or minimized fixture,
- supported version test,
- malformed payload test,
- unsupported version test,
- semantic mapping test,
- no-secret-leak test.

## TR-4 Policy

Cover every reason code and evaluation-order interaction.

Particularly:

- hard gate fails while effect is positive,
- missing hard gate,
- signature invalid before effect computation,
- metric incomparable before statistics,
- statistically uncertain candidate.

## TR-5 Security

Retain and expand adversarial tests for:

- traversal,
- symlink escape,
- special files,
- oversized files,
- malformed Unicode/JSON,
- credential redaction,
- untrusted artifact schemas.

---

# 14. Acceptance Criteria by Release

# v0.4 Acceptance Criteria

v0.4 is complete only when all are true:

- [ ] Repeated-trial evidence is supported.
- [ ] Confidence-aware verdict logic exists.
- [ ] `STATISTICAL_UNCERTAINTY` can produce `INCONCLUSIVE`.
- [ ] Statistical method is documented and deterministic.
- [ ] Verdict bundle can be cryptographically signed.
- [ ] `verify --require-signature` works.
- [ ] Signed payload binds issuance timestamp and evidence manifest.
- [ ] Adapter interface is documented.
- [ ] At least 3 external adapters are implemented and tested.
- [ ] Metric registry supports required tail/correctness metrics or explicitly documents deferment.
- [ ] Policy schema is versioned.
- [ ] New reason codes are documented.
- [ ] CI mode can fail deployment on `REJECT` and optionally `INCONCLUSIVE`.
- [ ] Main branch protection is enabled.
- [ ] Security scanning is active.
- [ ] Release artifacts are signed/attested.
- [ ] Existing v0.1–v0.3 bundles remain verifiable where compatibility is promised.

# v0.5 Acceptance Criteria

- [ ] At least 5–6 ecosystem adapters exist.
- [ ] GitHub Actions integration is published.
- [ ] Representative workload replay is versioned by digest.
- [ ] Application-level external gate evidence is supported.
- [ ] Experiment lineage is visible.
- [ ] Promoted baseline lineage is tracked.
- [ ] At least 3 real case studies exist: promote, reject, inconclusive.
- [ ] At least two different serving stacks are covered by case studies.
- [ ] Adapter contribution guide is validated by adding one adapter from outside the core path.

# v1.0 Acceptance Criteria

- [ ] Bundle schema declared stable.
- [ ] CLI contract declared stable.
- [ ] Policy schema declared stable.
- [ ] Threat model completed and reviewed.
- [ ] Reproducible/signed release process is documented and automated.
- [ ] PyPI release works through trusted publishing.
- [ ] At least one deployment pipeline integration is demonstrated end-to-end.
- [ ] Offline verification of signed historical verdicts is supported.
- [ ] Backward compatibility policy is documented.
- [ ] Windows is either tested or clearly excluded from v1 support.
- [ ] Public documentation cleanly distinguishes core product from auxiliary tooling.

---

# 15. Recommended Roadmap

## Phase A — Core Trust Upgrade

**Priority: P0**

1. Bundle schema v0.4.
2. Statistical sample model.
3. Bootstrap confidence interval engine.
4. Confidence-aware decision rules.
5. DSSE/Sigstore signing.
6. Trust-policy verification.
7. Signed release artifact pipeline.

This phase should happen before adding new UI or tuning features.

## Phase B — Adapter Ecosystem

**Priority: P0**

1. Formal adapter protocol.
2. vLLM adapter.
3. SGLang adapter.
4. GuideLLM adapter.
5. GenAI-Perf adapter.
6. llama.cpp adapter.
7. Generic normalized JSON adapter.

## Phase C — CI / Promotion Controller

**Priority: P0/P1**

1. `gate` command.
2. stable CI exit semantics.
3. GitHub Action.
4. machine-readable result summary.
5. verdict artifact upload.
6. deployment integration reference.

## Phase D — Operational Evidence

**Priority: P1**

1. representative workload packs,
2. tool/structured-output correctness evidence,
3. external quality-gate ingestion,
4. baseline lineage,
5. rollback evidence.

## Phase E — UI and Product Polish

**Priority: P1/P2**

1. statistical confidence UI,
2. signer/trust UI,
3. evidence lineage UI,
4. adapter support matrix,
5. real case-study screens.

---

# 16. Priority Matrix

| Requirement | Priority | Release |
|---|---:|---:|
| Statistical verdicts | P0 | v0.4 |
| Signed verdict bundles | P0 | v0.4 |
| Evidence provenance | P0 | v0.4 |
| Adapter contract | P0 | v0.4 |
| vLLM/SGLang/GuideLLM adapters | P0 | v0.4 |
| CI gate command | P0 | v0.4 |
| Branch/release protection | P0 | v0.4 |
| 5–6 adapters | P1 | v0.5 |
| Workload replay hardening | P1 | v0.5 |
| GitHub Action | P1 | v0.5 |
| Baseline lineage | P1 | v0.5 |
| Rollback proof | P1 | v0.5/v1.0 |
| Deployment integration | P1 | v1.0 |
| Advanced UI polish | P2 | v0.5+ |
| Autonomous tuning agent | Out of scope | — |

---

# 17. Success Metrics

The project should avoid measuring success only through feature count.

Recommended metrics:

## Product quality

- 100% of verdict paths covered by deterministic tests.
- 100% of supported adapters with fixtures and malformed-input tests.
- 0 silent metric-semantic conversions.
- 0 unsigned verdict accepted when policy requires signatures.

## Adoption

- number of external adapter users,
- number of distinct serving runtimes represented,
- number of real verdict bundles generated,
- external contributors,
- CI integrations.

## Decision quality

- percentage of marginal benchmark wins resulting in `INCONCLUSIVE` rather than unstable promote/reject oscillation,
- percentage of promotions backed by representative workload evidence,
- number of caught “synthetic win, operational regression” cases.

## Operational quality

- CI pass rate,
- release verification success,
- bundle backward-verification success,
- mean time to identify reason for rejection/inconclusive.

---

# 18. Product Risks

## Risk 1 — Feature creep

Doctor, Advisor, benchmark runner, Pareto/sweep and UI can consume development effort while diluting the core product.

**Mitigation:** Every feature must justify how it improves evidence quality, policy quality or promotion safety.

## Risk 2 — Overclaiming statistical certainty

Small-sample inference benchmarks can remain noisy even with confidence intervals.

**Mitigation:** document methodology, default to `INCONCLUSIVE`, avoid “scientifically proven” claims.

## Risk 3 — Adapter maintenance burden

External benchmark schemas change.

**Mitigation:** adapter version declarations, fixture-based compatibility tests, clear support matrix.

## Risk 4 — Security branding stronger than implementation

Using terms such as “tamper-evident” without external trust/signatures can overstate guarantees.

**Mitigation:** distinguish digest integrity from authenticity; require cryptographic signing for authenticity claims.

## Risk 5 — Becoming a benchmark suite

Internal benchmark runner can expand endlessly.

**Mitigation:** keep quick benchmark bounded; favor adapters for serious external load generation.

---

# 19. Recommended Architecture Boundary

```text
┌────────────────────────────────────────────────────────────┐
│                    Evidence Producers                      │
│ vLLM │ SGLang │ GuideLLM │ GenAI-Perf │ llama.cpp │ Custom │
└──────────────────────────────┬─────────────────────────────┘
                               │ immutable artifacts
                               ▼
┌────────────────────────────────────────────────────────────┐
│                    Evidence Ingestion                      │
│ path-safe loader │ hash validation │ adapter detection    │
└──────────────────────────────┬─────────────────────────────┘
                               ▼
┌────────────────────────────────────────────────────────────┐
│                  Normalized Evidence Model                 │
│ metrics │ dimensions │ runtime │ model │ hardware │ gates  │
└──────────────────────────────┬─────────────────────────────┘
                               ▼
┌────────────────────────────────────────────────────────────┐
│                     Trust / Provenance                     │
│ evidence manifest │ signatures │ signer policy │ identity │
└──────────────────────────────┬─────────────────────────────┘
                               ▼
┌────────────────────────────────────────────────────────────┐
│                   Decision Authority                       │
│ comparability │ hard gates │ statistics │ policy engine   │
└──────────────────────────────┬─────────────────────────────┘
                               ▼
┌────────────────────────────────────────────────────────────┐
│                      Verdict Bundle                        │
│ PROMOTE / REJECT / INCONCLUSIVE │ reasons │ claim boundary│
│ digest │ signature │ provenance │ experiment lineage      │
└──────────────────────────────┬─────────────────────────────┘
                               ▼
┌────────────────────────────────────────────────────────────┐
│                         Consumers                          │
│ CLI │ UI │ GitHub Actions │ CI/CD │ Argo │ Audit archive   │
└────────────────────────────────────────────────────────────┘
```

The **Decision Authority** package should remain small, pure and highly tested.

---

# 20. Definition of Done for Any New Core Feature

A new core feature is not done until:

- [ ] product behavior is specified,
- [ ] threat/security implications are considered,
- [ ] schema impact is documented,
- [ ] reason codes are defined if needed,
- [ ] deterministic unit tests exist,
- [ ] malformed-input tests exist,
- [ ] CLI/API output is documented,
- [ ] backward compatibility is assessed,
- [ ] README/reference docs are updated,
- [ ] release notes describe the claim boundary.

---

# 21. Immediate Engineering Backlog

## P0 — Next implementation batch

- [ ] Create `STATISTICS.md` and freeze statistical semantics before implementation.
- [ ] Add repeated-trial normalized evidence model.
- [ ] Implement deterministic bootstrap confidence intervals.
- [ ] Add `INSUFFICIENT_SAMPLE_SIZE` and `STATISTICAL_UNCERTAINTY` reason codes.
- [ ] Introduce bundle schema v0.4.
- [ ] Add structured claim boundary.
- [ ] Add evidence manifest object.
- [ ] Add signer metadata.
- [ ] Implement DSSE/Sigstore signing path.
- [ ] Implement `verify --require-signature`.
- [ ] Add trust policy to case policy.
- [ ] Formalize adapter protocol.
- [ ] Implement vLLM adapter.
- [ ] Implement SGLang adapter.
- [ ] Implement GuideLLM adapter.
- [ ] Add `gate` CI command.
- [ ] Protect `main` branch and require CI.
- [ ] Add security/dependency scanning.
- [ ] Sign release artifacts.

## P1 — Following batch

- [ ] GenAI-Perf adapter.
- [ ] llama.cpp adapter.
- [ ] generic normalized JSON adapter.
- [ ] GitHub Action/reusable workflow.
- [ ] application-level gate ingestion.
- [ ] baseline/candidate lineage.
- [ ] workload-pack versioning.
- [ ] rollback evidence schema.
- [ ] statistics/trust UI.

## P2 — Later

- [ ] richer deployment integrations,
- [ ] optional transparency-log UX,
- [ ] organization policy packs,
- [ ] adapter plugin discovery,
- [ ] multi-repository verdict registry,
- [ ] signed policy bundles.

---

# 22. Final Product Statement

LLM ServeVerdict should become the layer that teams trust when a benchmark result is not enough.

Its long-term value is not that it can run one more performance test. Its value is that it can say:

> **“These exact artifacts, under these exact semantics and this explicit policy, provide enough trustworthy evidence to promote this candidate.”**

or:

> **“The candidate is faster, but the evidence is operationally unsafe, semantically invalid, statistically weak or cryptographically untrusted — therefore it must not be promoted.”**

That is the product boundary that should remain stable from v0.4 through v1.0.
