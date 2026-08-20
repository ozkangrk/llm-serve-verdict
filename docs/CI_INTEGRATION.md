# CI promotion gate

`serving-verdict gate` verifies a sealed bundle before evaluating its verdict.
A client-side verdict edit therefore fails integrity verification; it can never
turn REJECT into PROMOTE.

## CLI

```bash
serving-verdict gate verdict.json \
  --require PROMOTE \
  --fail-inconclusive \
  --require-signature \
  --trust-store trust.json \
  --json \
  --github-summary "$GITHUB_STEP_SUMMARY"
```

### Exit codes

| Code | Meaning |
|---:|---|
| 0 | Required verdict satisfied; or valid INCONCLUSIVE when `--fail-inconclusive` is absent |
| 2 | Usage/config/load failure |
| 4 | Digest/signature/trust/integrity failure |
| 5 | Valid REJECT or another non-INCONCLUSIVE requirement mismatch |
| 6 | Valid INCONCLUSIVE with `--fail-inconclusive` |

For deployment approval, use both `--require PROMOTE` and
`--fail-inconclusive`. This makes every non-PROMOTE result non-zero. The official
composite action defaults `fail-inconclusive` to `true`.

v0.1 compatibility bundles support digest verification but cannot satisfy
signature flags. v0.4 bundles use the offline DSSE/Ed25519 trust path.

## JSON contract

`--json` writes exactly one object to stdout. Important fields:

- `bundle_digest`: verified digest of the source verdict bundle;
- `result_digest`: digest of the gate-result object itself;
- `verdict`, `required`, `decision`, `blocked`, `exit_code`;
- structural reason codes and bounded reason text;
- signature presence/validity/trust fields where applicable.

Diagnostics go to stderr. No raw evidence, prompt/output, claim text or secret is
included. The optional GitHub summary is bounded to 24 lines and 2,000
characters and escapes control/markup characters.

## Composite GitHub Action

The caller must check out this repository before invoking the local action:

```yaml
steps:
  - uses: actions/checkout@<PINNED_SHA>
  - uses: ./.github/actions/gate
    id: verdict
    with:
      bundle: artifacts/verdict.json
      require: PROMOTE
      fail-inconclusive: "true"
      require-signature: "true"
      trust-store: policy/trust.json
```

Outputs: `verdict`, `decision`, `reason`, `result-digest`.

Inputs are passed through environment variables and Bash arrays, never evaluated
as shell source. The action uses a SHA-pinned setup-uv action and executes the
checked-out package with `uv run --frozen`.

## Claim boundary

A passing gate proves that the exact sealed bundle satisfied the requested
verdict/trust policy. It does not prove workload representativeness, benchmark
quality or deployment success beyond what the signed claim boundary records.
