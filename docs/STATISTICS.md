# Statistical Verdict Engine — Frozen Semantics (v0.4)

This document freezes the exact semantics of the v0.4 deterministic
statistical verdict engine (`serving_verdict.statistics`). Code and tests are
the executable specification; where this document and behavior can drift,
this document wins and must be updated in the same change. It implements
PRD FR-1.1–FR-1.7 (repeated trials, minimum sample count, confidence
intervals, effect decision policy, metric direction, distribution metadata,
outlier handling).

The engine is a **pure function**:

```
(baseline sample, candidate sample, spec) -> result
```

No network, no subprocess, no LLM, no hidden state, no global RNG. Every
output bit is a deterministic function of the inputs and the Python standard
library Mersenne-Twister stream seeded with the spec seed. Arithmetic sums use
an explicit left-to-right binary64 fold rather than CPython's version-dependent
`sum()` implementation; the Python 3.11/3.12 CI matrix pins this contract.

## 1. Inputs

### 1.1 `StatisticalSample`

One arm of the experiment (baseline or candidate): the repeated, independent
trials of a single metric.

- `values`: a sequence of numbers; every value is normalized to an IEEE-754
  binary64 `float` at construction time.
- **Fail-closed validation, in this order, per value (first violation wins):**
  1. the container must be a sequence of numbers (no strings, no sets, no
     dicts); a bool value is rejected even though `bool` subclasses `int`;
  2. each value must be a real number (`int` or `float`), not `bool`, not a
     numeric string;
  3. each value must be **finite** (`nan`, `inf`, `-inf` rejected);
  4. each value must be **non-negative** (`v >= 0`).
- **No implicit outlier removal** (FR-1.7): every provided value is used in
  every statistic. v0.4 has no explicit removal mode either, so
  `removed_samples` is always the empty list. Explicit removal (method,
  threshold, removed IDs, original/final counts) is future work and must be
  an opt-in policy option when it lands.
- At least one value is required.
- **Deep immutability:** construction copies values; mutating the caller's
  list afterwards has no effect on the sample. Aliasing the same list into
  both arms is safe (each arm keeps its own copy).
- Independence assumption (documented, caller's responsibility): trials
  within an arm are independent, identically-distributed measurements of the
  same metric under the same frozen workload/protocol, and baseline and
  candidate trials are independent of each other. Warmup trials must be
  excluded by the caller (FR-1.8) — the engine cannot tell warmup from
  measured runs.

### 1.2 `StatisticalSpec`

All fields are **required** (no defaults). Fail-closed validation, exact
types (bool is rejected for every field because `bool` subclasses `int`):

| field | exact type | bounds | meaning |
|---|---|---|---|
| `confidence_level` | `float` | `0.5 < cl < 1.0` | nominal bootstrap CI level |
| `iterations` | `int` | `100 <= B <= 10_000_000` | bootstrap replicates |
| `seed` | `int` | `0 <= seed <= 2**63 - 1` | RNG seed |
| `min_trials` | `int` | `>= 2` | minimum trials per arm (FR-1.2) |
| `threshold` | `float` | finite, `>= 0` | required relative improvement |
| `direction` | `str` | `"lower_better"` \| `"higher_better"` | metric direction (FR-1.5) |

Any violation raises `StatisticalError` (exit 2, no result produced).
Validation order: baseline sample, candidate sample, then spec fields in the
table order above, then `direction`.

## 2. Statistic and effect (direction-aware)

The **baseline statistic** is the arithmetic mean of the baseline values:
`mean_b = stable_left_fold(values) / n_b`, using the engine's explicit
left-to-right binary64 addition order.

The **direction-aware relative effect** is the relative improvement of the
candidate with respect to the baseline:

- `lower_better` (TTFT, latency): `effect = (mean_b - mean_c) / mean_b`
- `higher_better` (throughput): `effect = (mean_c - mean_b) / mean_b`

Positive effect = improvement. Every baseline trial must be **strictly positive**. A zero trial can produce a
zero bootstrap denominator even when the full-sample mean is positive, so any
baseline value `<= 0` yields
`NONPOSITIVE_BASELINE_STATISTIC` (INCONCLUSIVE), because relative effect is
undefined for that resample. This check runs *after* the minimum-trials
check, so an under-sampled all-zero arm reports
`INCONCLUSIVE_INSUFFICIENT_SAMPLE_SIZE`, not the baseline error.

`effect` is reported on the **full samples** (all trials, no resampling).

## 3. Descriptive statistics (FR-1.6)

Per arm, reported in the result and the artifact:

- `n` — trial count;
- `mean` — arithmetic mean (the baseline statistic for the baseline arm);
- `median` — 50th percentile via the quantile rule below;
- `sample_stdev` — sample standard deviation with **Bessel's correction**
  (`n - 1` denominator); `0.0` when `n == 1` (documented degenerate case);
- `p50` — 50th percentile (always equal to `median`);
- `p95` — 95th percentile.

### 3.1 Quantile interpolation (frozen)

For sorted values `x[0..n-1]` and probability `p` in `(0, 1)`:

```
h  = (n - 1) * p
lo = floor(h)
frac = h - lo
q  = x[lo] + frac * (x[lo + 1] - x[lo])      (when lo + 1 < n)
q  = x[lo]                                    (when lo + 1 == n)
```

This is linear interpolation between order statistics (numpy "linear" /
R-7 convention). `p50` uses `p = 0.5`, `p95` uses `p = 0.95`. Single-value
arms return the value itself for every quantile.

## 4. Deterministic seeded bootstrap CI (FR-1.3)

Percentile bootstrap on the effect statistic. **Exact algorithm** (any
deviation breaks the golden vectors):

1. `rng = random.Random(seed)` (stdlib Mersenne-Twister; integer-seed streams
   are stable across platforms and CPython 3.x).
2. Repeat exactly `B = spec.iterations` times, in order:
   1. draw `n_b` values **with replacement** from the baseline values using
      successive `rng.choice` calls (stable sample order), compute the
      bootstrap baseline mean using the explicit left-to-right fold;
   2. draw `n_c` values with replacement from the candidate values (same
      rule), compute the bootstrap candidate mean with the same fold;
   3. compute the replicate effect with the **same** direction-aware
      formula as §2. The baseline arm is always drawn before the candidate
      arm, so the RNG stream is input-order-stable.
3. Sort the `B` replicate effects ascending (stable sort).
4. `alpha = 1.0 - confidence_level`;
   `k_lo = round((alpha / 2.0) * B)`, clamped to `[0, B - 1]`;
   `k_hi = B - k_lo`, clamped to `[k_lo + 1, B - 1]`.
   (Clamping only engages for extreme small-`B`/high-confidence corners; it
   falls back to the empirical extremes of the replicate set.)
5. `ci = (estimates[k_lo], estimates[k_hi])` — the lower and upper
   percentile bounds.

**Confidence semantics (honest):** this is a *nominal* `(1 - alpha)`
percentile-bootstrap interval for the sampling variability of the mean-based
relative effect under the i.i.d. resampling assumption. It is **not** a
parametric interval, it carries **no p-value**, no coverage guarantee, no
multiple-comparison correction, and no Bayesian posterior. "95% confidence"
in this product means: the interval was constructed at the 2.5/97.5
percentile level with the recorded `B` replicates. Bootstrap quantiles are
discrete (multiples of the replicate grid), which is visible at small `B`
and is part of the contract, not a defect. The CI is sensitive to the seed;
the seed is therefore always reported and bound into the artifact digest.

## 5. Decision policy (FR-1.4) — frozen rule order

Let `lo`, `hi` be the bootstrap CI bounds and `t` the threshold.

| # | rule | verdict (exact reason code) |
|---|---|---|
| 1 | `n_b < min_trials` or `n_c < min_trials` | `INCONCLUSIVE_INSUFFICIENT_SAMPLE_SIZE` |
| 2 | any baseline trial `<= 0` | `NONPOSITIVE_BASELINE_STATISTIC` |
| 3 | `lo >= t` | `PROMOTE_ELIGIBLE` |
| 4 | `hi < t` | `REJECT_INSUFFICIENT_EFFECT` |
| 5 | otherwise (`lo < t <= hi`) | `INCONCLUSIVE_STATISTICAL_UNCERTAINTY` |

- Rule 1 is checked first (FR-1.2); when it fires, `effect` and `ci` are
  `None` — the descriptive statistics of both arms are still reported, since
  they are well-defined.
- Rules 3–5 use the **exact** comparisons shown: a lower bound **equal** to
  the threshold is `PROMOTE_ELIGIBLE`; an upper bound equal to the threshold
  is `INCONCLUSIVE_STATISTICAL_UNCERTAINTY` (overlap includes the upper
  endpoint).
- `PROMOTE_ELIGIBLE` is eligibility for promotion subject to all other gates
  (quality, provenance, comparability); it is not a promotion itself.
- `INCONCLUSIVE` results are **not** errors: they are valid, sealed verdicts
  with an exact reason code.

## 6. Result contract

`StatisticalResult` is a deep-immutable typed contract:

- `verdict`: one of the five exact reason codes above (the verdict *is* the
  reason code; `reason_codes == (verdict,)`).
- `reason_codes`: `tuple[str, ...]`.
- `effect`: `float | None` (full-sample relative improvement, or `None` when
  rule 1 fired).
- `ci`: `tuple[float, float] | None` (bootstrap bounds, or `None` when rule
  1 fired).
- `seed`, `iterations`, `confidence_level`, `threshold`, `direction`: the
  effective spec, echoed verbatim.
- `baseline`, `candidate`: per-arm summaries — `n`, `mean`, `median`,
  `sample_stdev`, `p50`, `p95` (always present, even on insufficient sample
  size).
- `removed_samples`: `tuple[float, ...]`, **always empty** in v0.4 (no
  implicit outlier removal, FR-1.7; reported so the absence of removal is
  machine-checkable).

All nested containers are tuples; every field is invariant under mutation
probes on any alias of the original inputs.

## 7. Canonical artifact (volatile-excluded digest)

`build_statistics_artifact(baseline, candidate, spec, metric_id, workload_id, created_at)`
seals a canonical artifact (schema `serving-verdict.statistics.v0.1`):

- `provenance_id`: `prov:` + 32-hex of the canonical **input identity** —
  schema version, metric/workload IDs, direction, seed, iterations, confidence
  level, threshold, min trials, and both arms' value vectors. Deterministic across time:
  identical inputs share a provenance ID regardless of `created_at`.
- `artifact_digest`: `sha256:` over the canonical payload with the volatile
  fields **`created_at` and `artifact_digest` excluded**. Two artifacts from
  the same inputs built at different times carry identical digests; mutating
  `created_at` still verifies, while mutating any substantive field
  (verdict, CI, seed, values, …) fails verification.
- Verification (`verify_statistics_artifact`) is fail-closed: foreign schema
  version, missing required field, or digest mismatch raises
  `StatisticalArtifactError` (exit 4). The verifier reconstructs the samples
  and spec, reruns the engine, and compares the exact result payload.
- Canonical encoding: UTF-8, `ensure_ascii=True`, `sort_keys=True`, compact
  separators, `allow_nan=False`, list order preserved as data.

## 8. Determinism contract

For fixed inputs, the engine is bit-for-bit reproducible:

- same samples + same spec → identical `ci` floats, identical effect,
  identical canonical artifact bytes (except `created_at`/digest exclusion);
- different seed → generally different CI (recorded and tested);
- no wall clock, no `os.urandom`, no thread-local state.

## 9. Limitations (stated, not papered over)

1. **Nominal percentile bootstrap.** No coverage guarantee, no p-value, no
   distributional assumption beyond i.i.d. within-arm trials. Small `B`
   yields coarse, seed-sensitive quantiles.
2. **Mean-based effect.** The statistic is the relative improvement of the
   **means**, not of medians or percentiles; heavy tails and outliers shift
   it (outliers are deliberately retained and must be judged from the
   reported `sample_stdev`/`p95`/`n`).
3. **No pairing.** Baseline and candidate trials are assumed independent;
   paired/delta designs are out of scope.
4. **No multiple-comparison correction.** If the engine is applied to several
   metrics, the family-wise error is uncontrolled.
5. **Warmup and autocorrelation are the caller's problem** (FR-1.8); the
   engine cannot detect contaminated trials.
6. **Floating-point arithmetic is part of the contract.** All values are
   binary64; comparisons use exact float ordering. Golden vectors pin the
   exact bits, including non-representable decimal thresholds.
7. **`PROMOTE_ELIGIBLE` ≠ promotion.** It is one gate among others
   (quality, comparability, provenance).
8. **No explicit outlier removal in v0.4.** `removed_samples` is always
   empty; an opt-in removal mode must add method/threshold/IDs to the
   contract when it lands.
