# Automated baseline/candidate A/B experiments

`llm-serve-verdict bench ab` automates the repeated-measurement half of an LLM
serving promotion decision.

## What it does

1. Loads two credential-safe endpoint configs for the same requested model.
2. Runs the frozen `quick` profile repeatedly against both endpoints.
3. Alternates order by round (`baseline→candidate`, then
   `candidate→baseline`) to reduce monotonic time/order drift.
4. Preserves every sealed run artifact; no trial or outlier is silently removed.
5. Applies serving correctness/stability hard gates before performance.
6. Evaluates concurrency output-token throughput with a deterministic seeded
   bootstrap confidence interval.
7. Atomically publishes one self-verifiable experiment directory and returns
   `PROMOTE`, `REJECT`, or `INCONCLUSIVE`.

It does not deploy or mutate either endpoint.

## Endpoint configs

```yaml
# baseline.yaml
schema_version: serving-verdict.endpoint.v1
id: baseline
base_url: http://127.0.0.1:8001/v1
model: Qwen/Qwen3-8B
api_key_env: BASELINE_API_KEY
```

```yaml
# candidate.yaml
schema_version: serving-verdict.endpoint.v1
id: candidate
base_url: http://127.0.0.1:8002/v1
model: Qwen/Qwen3-8B
api_key_env: CANDIDATE_API_KEY
```

Only environment-variable names are stored. Secret values are read at runtime
and rejected if a runner attempts to persist them.

## Run

```bash
export BASELINE_API_KEY='...'
export CANDIDATE_API_KEY='...'

llm-serve-verdict bench ab \
  --baseline-endpoint baseline.yaml \
  --candidate-endpoint candidate.yaml \
  --trials 3 \
  --threshold 0.05 \
  --confidence-level 0.95 \
  --iterations 1000 \
  --seed 17 \
  --out-dir evidence/qwen-vllm-ab \
  --json
```

`--trials` is bounded to 2–20. Remote endpoints remain blocked unless the
operator explicitly supplies `--allow-remote`.

## Output

```text
evidence/qwen-vllm-ab/
├── baseline-trial-001.json
├── baseline-trial-002.json
├── baseline-trial-003.json
├── candidate-trial-001.json
├── candidate-trial-002.json
├── candidate-trial-003.json
├── statistics.json
└── experiment.json
```

Publishing is atomic: the destination appears only after all files are written.
An existing destination is never overwritten.

Verify the complete directory before CI/deployment consumes its decision:

```bash
llm-serve-verdict bench ab-verify evidence/qwen-vllm-ab --json
```

Require a promotion decision in CI:

```bash
llm-serve-verdict bench ab-verify evidence/qwen-vllm-ab \
  --require PROMOTE --fail-inconclusive --json
```

Exit codes follow the deployment-gate convention: `0` verified/requirement
satisfied, `2` usage, `4` integrity, `5` valid `REJECT`, and `6` valid
`INCONCLUSIVE` when `--fail-inconclusive` is set.

`experiment.json` binds:

- endpoint public identities and exact model;
- frozen profile/procedure;
- alternating execution order;
- every run filename, status and digest;
- metric samples extracted back from those runs;
- exact statistical policy and artifact;
- final decision and claim boundary.

Verification is bounded, rejects symlinks/extra files/path-like filenames,
unreferenced runs, run/status/context
drift, sample/statistics mismatches, digest tampering and unsupported status
vocabulary.

## Decision order

1. Any degraded baseline trial → `INCONCLUSIVE / BASELINE_HARD_GATE_FAILED`.
2. Otherwise any degraded candidate trial →
   `REJECT / CANDIDATE_HARD_GATE_FAILED`.
3. Otherwise an unmeasurable metric → `INCONCLUSIVE / METRIC_UNMEASURABLE`.
4. Otherwise deterministic statistics map to:
   - `PROMOTE_ELIGIBLE` → `PROMOTE`;
   - `REJECT_INSUFFICIENT_EFFECT` → `REJECT`;
   - insufficient sample size, nonpositive baseline, or confidence interval
     crossing the threshold → `INCONCLUSIVE`.

The v0.1 contract intentionally gates one metric:
`concurrency_output_tokens_per_s` from the built-in quick profile. Broader metric
policies remain a future, separately reviewed schema change.
