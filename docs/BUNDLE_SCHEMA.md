# Verdict Bundle v0.4 and Offline Signing

Schema: `serving-verdict.bundle.v0.4`

v0.4 adds structured provenance and authenticity without changing v0.1 bundle
verification. A v0.1 bundle remains verifiable with the existing digest path;
it cannot satisfy `--require-signature`.

## Required top-level sections

```text
schema_version
case
baseline
candidate
evidence_manifest
comparisons
statistics
gates
trust
verdict
reason_codes
claim_boundary
issued_at
producer
digest
signature
```

Unknown or missing top-level fields fail closed. `parse_v04_bundle()` validates
the complete JSON value tree and returns a recursively immutable view.

## Evidence manifest

Each entry binds:

- artifact ID, SHA-256, schema and byte size;
- producer and production timestamp;
- tool and tool version;
- source type;
- structured model, runtime, hardware and benchmark procedure identity.

Artifact IDs are unique. Mutable tags such as `latest` are metadata only and
must not replace immutable image/model revisions or evidence hashes.

## Structured claim boundary

`claim_boundary` is an object rather than only free text. It includes the
frozen workload set and may bind runtime, model, hardware class, benchmark
procedure, candidate delta, observation window and explicit limitations.
A human-readable summary may accompany those fields but is not authority.

## Canonical digest

The digest covers exactly these 14 fields:

```text
schema_version, case, baseline, candidate, evidence_manifest,
comparisons, statistics, gates, trust, verdict, reason_codes,
claim_boundary, issued_at, producer
```

`digest` and `signature` are excluded. Unlike the v0.1 compatibility digest,
`issued_at` is substantive and covered: changing issuance time invalidates the
digest and signature. Re-signing an unchanged bundle does not change its digest.
Canonical encoding uses the repository canonical JSON contract.

## Offline DSSE + Ed25519 backend

The first signing backend is explicitly limited to **offline DSSE with
Ed25519**. It does not claim Sigstore identity, transparency-log inclusion,
keyless OIDC signing, timestamp authority or revocation checking.

The bundle signature section contains:

- `backend: dsse_ed25519`;
- signer identity;
- deterministic key ID derived from the raw Ed25519 public key;
- DSSE payload type `serving-verdict/bundle/v0.4`;
- a base64 DSSE envelope.

The DSSE payload is the canonical bundle excluding only `signature`, so it
binds the recorded digest and every other section. Algorithm, payload type,
payload bytes, key ID and signature count are checked strictly; algorithm
confusion and malformed base64 fail closed.

## CLI

```bash
export SV_SIGNING_KEY='<64 lowercase/uppercase hex chars: 32-byte Ed25519 seed>'
serving-verdict sign verdict-v04.json \
  --key-env SV_SIGNING_KEY \
  --signer ci@example.com \
  --out verdict-v04.signed.json

serving-verdict verify verdict-v04.signed.json \
  --require-signature \
  --trust-store trust-store.json
```

Private key material is environment-only. It is never written to the signed
bundle, stdout, stderr or verification reports. Structural key errors do not
echo the seed value.

## Trust store v0.1

```json
{
  "schema_version": "serving-verdict.trust-store.v0.1",
  "require_signed_evidence": false,
  "require_signed_verdict": true,
  "allowed_signers": ["ci@example.com"],
  "allowed_keys": [
    {
      "key_id": "ed25519-...",
      "ed25519_public_key": "<base64 raw 32-byte public key>"
    }
  ]
}
```

Trust-store JSON is strict, bounded to 1 MiB, rejects duplicate key IDs and
accepts only raw 32-byte Ed25519 public keys. Verification is network-free.

`require_signed_evidence=true` currently fails closed with
`EVIDENCE_SIGNATURES_INVALID`: the first backend signs verdict bundles, but no
per-evidence DSSE envelope backend is shipped yet. This is an explicit v0.4
limitation, not a silent downgrade.

## Verification stages and stable failure codes

Order is fail closed:

1. structural schema validation;
2. canonical digest (`DIGEST_INVALID`);
3. required signature presence (`SIGNATURE_MISSING`);
4. DSSE payload/algorithm/key binding (`SIGNATURE_INVALID`);
5. trusted public key and signer allowlist (`UNTRUSTED_SIGNER`);
6. Ed25519 cryptographic verification (`SIGNATURE_INVALID`);
7. evidence-signature policy (`EVIDENCE_SIGNATURES_INVALID`).

CLI exits:

- `0`: verification/signing succeeded;
- `2`: usage, path, trust-store or private-key configuration failure;
- `4`: digest, signature or trust verification failure.

## Threat-boundary matrix

| Threat | Result |
|---|---|
| Verdict/reason/manifest/issued_at mutation | digest invalid, exit 4 |
| Signature/envelope mutation | signature invalid, exit 4 |
| Correct signature from unknown key | untrusted signer, exit 4 |
| Trusted key with disallowed signer label | untrusted signer, exit 4 |
| Missing signature when required | signature missing, exit 4 |
| Wrong payload type/key ID/algorithm | signature invalid, exit 4 |
| Missing/malformed trust-store file | usage error, exit 2 |
| v0.1 bundle with plain verify | existing compatibility verification |
| v0.1 bundle with `--require-signature` | signature missing, exit 4 |

## Operational limitations

- Protect private seeds outside the repository; environment-only transport is
  not a hardware security module.
- Trust-store paths are operator-supplied local files; filesystem symlinks are
  followed under the operator's OS permissions. Do not place trust material in
  an attacker-writable directory.
- Offline allowlists have no revocation/expiry/transparency semantics.
- Sigstore/cosign and in-toto transparency integration remain future backends.
- Authenticity proves control of an allowed key, not correctness of benchmark
  methodology. Statistical/comparability/hard gates remain independent.
