# Threat Model

Formal threat model for **Serving Verdict** v0.4. This document is the
security contract for the release pipeline and the verdict product: it names
the attackers, the assets, and — for each threat — the concrete mitigation,
where it lives, and what remains the *operator's* job. It deliberately does
**not** claim guarantees the implementation does not provide (see
[Claim boundary](#claim-boundary)).

Related documents: `SECURITY.md` (vulnerability reporting and runtime
invariants), `RELEASE.md` (release process, checksums, attestation, branch
protection), `scripts/check_workflows.py` (machine-enforced workflow
security contract).

---

## Actors

| Actor | Trust | Notes |
|---|---|---|
| **Maintainer** | Trusted | Runs the release pipeline, curates the trust store, manages repo settings. |
| **Operator** (downstream user) | Trusted locally | Owns the machine, chooses the trust store, selects source roots, holds signing keys. |
| **Adversary** | Untrusted | May control: benchmark evidence files, third-party artifacts, a compromised dependency (supply chain), a malicious fork, CI metadata on a public fork. |
| **GitHub Actions** | Partially trusted | Hosted runner + GitHub's attestation service. Treated as a *capability we depend on but do not control*: runner images, action code at pinned SHAs, the attestation transparency log. |

## Assets

1. **Evidence integrity** — benchmark artifacts (JSON/YAML) bound into a bundle.
2. **Verdict correctness** — `PROMOTE`/`REJECT`/`INCONCLUSIVE` computed by a
   deterministic function of bound evidence + policy.
3. **Supply chain** — source code, lockfile, built wheel/sdist, release assets.
4. **Secrets** — API keys, signing keys. Never in artifacts, logs, or CI summaries.

---

## T1 — Evidence modification

**Threat.** An adversary alters source benchmark evidence (e.g. rewrites
`baseline.json` or a candidate report) so the verdict is computed over data
the operator did not actually measure.

**Attack surface.** `case.yaml` artifact references, the evidence loader,
canonical digests.

**Mitigations (implemented, tested).**

- **Digest binding**: every evidence file referenced by a bundle carries a
  canonical digest; `verify` recomputes digests from the files and fails
  closed on any mismatch (`IntegrityError.code == DIGEST_INVALID`).
- **Signed evidence manifest**: the v0.4 verdict bundle signs (offline DSSE +
  Ed25519) the canonical bytes of the whole document, including every
  digest-covered field and `issued_at`. A tampered digest fails both the
  digest recomputation and the signature.
- **Trust policy**: verification only accepts signers present in the
  operator's trust store (see T6 and [Trust-store boundary](#trust-store--operator-boundary)).
- Path escape / symlink / special-file / size protections are retained and
  tested (`tests/test_evidence_loader.py`, `tests/test_canonical.py`).

**Residual risk.** The loader only sees what the operator points it at. If
the operator's own measurement run was already compromised, digests and
signatures authenticate *tamper-resistance*, not *measurement truthfulness*.
Out of scope by design (documented, not a flaw).

**Tests.** `tests/test_evidence_loader.py`, `tests/test_canonical.py`,
`tests/test_signing_v04.py`, `tests/test_bundle_v04.py`.

## T2 — Verdict modification

**Threat.** An adversary flips a `REJECT` to `PROMOTE` (or edits the
policy) and recomputes the plain digest/hash so the bundle looks consistent.

**Mitigations (implemented, tested).**

- **Cryptographic signature over the canonical verdict payload**: the
  signature is computed over the canonical JSON of the full bundle minus the
  `signature` section. Recomputing a plain hash is useless: the Ed25519
  signature does not verify without the private key (`SIGNATURE_INVALID`).
- **Deterministic verdict engine**: the verdict is a pure function of the
  bound evidence and case policy; no flag, environment variable, or model
  call can flip a `REJECT` to a `PROMOTE` (tested in
  `tests/test_statistics_v04.py`).
- **Fail-closed verification**: any structural or cryptographic defect
  excludes the bundle from indexing/serving; a failing bundle is never
  presented as evidence for a promotion.

**Residual risk.** An operator who *adds a malicious key to their own trust
store* can verify a forged bundle. The trust store is the operator's root of
trust; its curation is the operator's responsibility (see
[Trust-store boundary](#trust-store--operator-boundary)).

## T3 — Path escape (retained)

**Threat.** Malicious config references files outside the approved evidence
root.

**Mitigation.** Canonical source-root resolution; rejection of absolute child
paths, `..` traversal, symlink escape, special files, and >20 MiB files.
Covered by the retained v0.2/v0.3 loader tests.

## T4 — Adapter confusion

**Threat.** An artifact is interpreted under the wrong schema/adapter: e.g.
an sglang report parsed as a vLLM one, so fields are silently misread.

**Mitigations (implemented, tested).**

- **Strict adapter detection**: adapters declare the artifact schema they
  understand (versioned marker fields). Detection is explicit per adapter;
  there is no ambiguous silent fallback to a second adapter when the primary
  parse fails — unsupported artifacts fail closed with a stable error code.
- **Explicit schema versioning**: artifact and adapter interfaces carry
  version identifiers; mismatches are rejected rather than coerced.
- **Fixture tests**: `tests/test_adapters.py` and
  `tests/test_adapter_sdk_v04.py` pin behavior on real fixture files
  (`tests/fixtures/`), including negative cases for wrong-schema inputs.

**Residual risk.** A *new* upstream artifact format with an identical-looking
marker could be mis-detected if its version field is spoofed; adapters are
conservative (fail closed) and version-bounded, and the adapter surface is
small and reviewable.

## T5 — Semantic metric confusion

**Threat.** Two measurements share a unit (e.g. "latency, ms") but have
different benchmark semantics (different workload, warmup handling,
concurrency), and the engine compares them as if comparable.

**Mitigations (implemented, tested).**

- **Semantic registry**: metrics are first-class named objects in
  `serving_verdict.metrics` with declared direction (lower/upper is better)
  and measurement semantics, not free-form strings.
- **Strict comparability dimensions**: the comparison engine (and the v0.4
  statistics path) requires baseline and candidate to be measured under the
  same workload, warmup policy, and concurrency dimensions; mismatched
  dimensions produce `INCONCLUSIVE` with a reason code, never a fabricated
  comparison.
- **Privacy/semantic fixtures**: comparison tests use the committed
  fixtures (`tests/test_compare_v04.py`, `tests/test_statistics_v04.py`).

**Residual risk.** Semantic mismatch *within the same declared dimension*
(e.g. two "same" workloads generated by different harness versions) is not
detected automatically; the comparability contract is declarative. This is
documented in `STATISTICS.md` limitations.

## T6 — CI impersonation / untrusted signer

**Threat.** An untrusted actor (compromised dependency, malicious fork,
rogue CI run) produces a verdict bundle or release that *looks* valid, or
spoofs the identity of the official pipeline.

**Mitigations (implemented).**

- **Signer allowlist (trust store)**: offline verification accepts only
  `key_id`s and identities present in the operator's trust store. Unknown
  signer ⇒ `UNTRUSTED_SIGNER`, fail-closed. The tool never trusts a key it
  cannot check.
- **Pinned CI actions (immutable SHAs)**: every third-party action in
  `.github/workflows/` is pinned to a 40-hex commit SHA verified against the
  official upstream repository (record and enforcement:
  `scripts/check_workflows.py::PINNED_REFS`). Floating `@v*`/branch refs are
  rejected by the repo's own CI, so a tag rename upstream cannot change the
  code our pipelines run.
- **Least-privilege tokens**: workflows declare explicit `permissions:`
  maps (contents: read by default; write only where required) and machine
  checks fail any escalation.
- **Release identity**: releases are produced only by the tag-triggered
  `release.yaml` (guarded by branch protection + required status checks on
  `main`, see `RELEASE.md`), and release assets carry GitHub artifact
  **provenance attestation** linking them to the exact source commit and
  build job. A verdict bundle or asset not produced by that pipeline lacks
  the corresponding attestation/signature and fails verification.
- **Secret scanning and dependency auditing** in CI (T7, T8) reduce the
  chance that impersonation comes via leaked credentials or a vulnerable
  dependency.

**Residual risk (honest limits).**

- This is *not* an OIDC/Sigstore *signer identity* claim: the release
  pipeline uses GitHub's artifact attestation service. We do **not** claim
  cosign/Sigstore signatures, a transparency-log entry in a Sigstore
  instance, or a public-key certificate chain.
- If the `ozkangrk/serving-verdict` repository itself is fully compromised
  (maintainer account takeover + repo admin), an attacker can rewrite the
  workflows before branch protection blocks the merge. Mitigations are
  organizational: required PR reviews, protected environment approvals for
  PyPI, and the operator-side trust store which never accepts keys the
  operator did not add.

## T7 — Secret leakage

**Threat.** API keys or signing keys end up in evidence artifacts, logs, CI
summaries, error messages, or the git history.

**Mitigations (implemented, tested).**

- **Environment-only secrets**: endpoint credentials and signing keys are
  read exclusively from environment variables at the CLI layer; they are
  never persisted in artifacts, trial stores, or logs (invariants in
  `SECURITY.md`, tests in `tests/test_endpoint.py`,
  `tests/test_cli_signing_v04.py`).
- **Redaction tests**: error messages and payloads are built from stable
  identifiers only; tests assert that credential values never appear in
  artifacts, API responses, or logs.
- **Secret scanning**: `gitleaks` runs in CI over the full clone on every
  push/PR to `main` (`secret-scan.yaml`), catching accidentally committed
  secrets.
- **Prohibited persisted fields**: artifact schemas have no free-form
  "extra" fields that could smuggle a credential.

**Residual risk.** gitleaks is pattern-based; a novel credential format may
pass. Operators must rotate any secret that is ever exposed.

## T8 — Docker / runtime execution (explicit non-goal)

**Threat.** An attacker induces the tool to execute artifact content,
spawn a shell, or run container/remote workloads.

**Position: non-goal by design.**

- Serving Verdict **never executes artifact content**, never invokes a
  shell or Docker, and never runs remote jobs on the operator's behalf.
  There is no runtime container surface to protect, and none will be added
  without a new, separately-reviewed security model.
- The loopback server binds `127.0.0.1` only and its APIs are read-only
  (bounded in-memory automation jobs are the sole write path and never
  touch the data directory or runtime).
- Any future remote/server or container mode requires a **separate explicit
  security model** (PRD NFR-2) and is out of scope for v0.4.

**Mitigation.** The non-goal is enforced in code and pinned by tests
(`tests/test_server.py`, `tests/test_automation.py`); the release checklist
(`RELEASE.md`) forbids adding execution surface without this document being
updated.

## T9 — Supply chain: dependency vulnerability

**Threat.** A pinned or floating dependency has a known vulnerability or a
malicious new version is pulled in.

**Mitigations (implemented).**

- **Frozen lockfile**: `uv.lock` is committed; CI and release workflows sync
  with `--frozen`, so a malicious or vulnerable *new* version cannot be
  pulled silently.
- **Dependency vulnerability audit**: `uv audit --frozen` runs in CI on push
  to `main` and weekly on a schedule (`dependency-audit.yaml`), checking the
  lockfile against the OSV.dev database.
- **Pinned actions** (T6) cover the CI tooling supply chain.

**Residual risk.** The audit only covers *known* published advisories; a
zero-day in a dependency is not caught by scanning.

## T10 — Trust-store / operator boundary

**Threat.** Misunderstanding who controls what: an operator expects the
tool (or the maintainer) to decide which signers are trustworthy, or a
maintainer action implicitly changes operator trust.

**Position.**

- The **trust store is operator-owned and operator-curated**: public keys
  and the `allowed_signers` allowlist are the operator's root of trust. The
  project ships no pre-seeded keys that the tool would trust by default;
  nothing in the repository, its releases, or its CI can add keys to an
  operator's store.
- **The maintainer's CI identity is not the operator's signer**: the
  maintainer's release pipeline proves *where* artifacts came from (GitHub
  provenance attestation, SHA256SUMS, tag→commit linkage); the operator's
  verdict trust decision rests on *their own* trust store. These are
  independent boundaries and neither subsumes the other.
- **Fail-closed default**: absent trust store entry ⇒ `UNTRUSTED_SIGNER`;
  there is no "trust by default" mode.

**Operator obligations (see `RELEASE.md` and `SECURITY.md`).** Keep keys out
of artifacts and logs, curate the allowlist deliberately, verify release
artifacts against `SHA256SUMS`, and treat a bundle that fails verification
as *excluded* — not as a tool defect.

## T11 — DSSE offline limits

**Threat/limit.** Users over-estimate what the offline DSSE + Ed25519
signing backend proves.

**What it is.** `signing.py` implements an explicitly-labeled, simplified
DSSE-style envelope (RFC 9448 layout) with Ed25519: payload + signatures in
a self-contained bundle, verified **fully offline** with the local trust
store. This is FR-2.1's first backend; the schema's `signature.backend`
field is reserved for future backends.

**What it is NOT (limits).**

- **No transparency log.** Verification does not and cannot consult any
  network service; there is no public log entry proving *who* signed.
  Trust in a signature is bounded by the operator's local trust store only.
- **No key infrastructure.** No certificate authority, no key rotation
  service, no revocation list. Revocation is procedural: the operator removes
  a key from the trust store (old bundles then fail as `UNTRUSTED_SIGNER`).
- **Not Sigstore/cosign-compatible.** The envelope is a project-defined
  format; cosign/Sigstore tooling cannot verify these bundles and we make no
  claim of interoperability.
- **Offline by construction ⇒ bounded assurance.** A signer who later claims
  they never produced a bundle cannot be refuted from the bundle itself; the
  bundle proves authenticity *relative to the trust store at verification
  time*, not relative to a global registry.

**Mitigation of the over-trust risk.** This limit is stated here, in the
`signing.py` module docstring, and in release notes; the bundle schema's
backend label (`dsse_ed25519`) makes the capability visible to consumers.

---

## Claim boundary

This threat model **claims**:

- fail-closed verification (digest + signature + trust store),
- deterministic verdicts, strict adapter/metric comparability,
- pinned and least-privilege CI, checksummed and provenance-attested release
  artifacts,
- environment-only secrets with redaction tests.

It **does not claim**:

- Sigstore/cosign signatures or a Sigstore transparency-log entry for
  releases (GitHub artifact attestation only),
- reproducible bit-identical builds (verification is by `SHA256SUMS` +
  attestation linkage to commit/job, not by rebuild),
- any guarantee about the truthfulness of the *measurements* themselves
  (integrity of evidence, not ground truth),
- protection against compromise of the maintainer's GitHub account or of the
  operator's own machine (trust-store and organizational controls apply).

## Verification map

| Threat | Machine enforcement |
|---|---|
| T1, T2 | `tests/test_signing_v04.py`, `tests/test_bundle_v04.py`, `tests/test_canonical.py`, `tests/test_evidence_loader.py` |
| T4, T5 | `tests/test_adapters.py`, `tests/test_adapter_sdk_v04.py`, `tests/test_compare_v04.py`, `tests/test_statistics_v04.py` |
| T6, T9 | `scripts/check_workflows.py` (run in CI), `codeql.yaml`, `dependency-audit.yaml` |
| T7 | `secret-scan.yaml` (gitleaks), `tests/test_endpoint.py`, `tests/test_cli_signing_v04.py` |
| T8 | `tests/test_server.py`, `tests/test_automation.py` |
| Release integrity | `release.yaml` (SHA256SUMS + `actions/attest-build-provenance`), `RELEASE.md` |
