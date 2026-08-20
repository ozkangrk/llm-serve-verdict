"""Deterministic statistical verdict engine: contracts, bootstrap CI, policy.

Golden vectors here are hand-derived or double-entry verified against the
frozen algorithm in docs/STATISTICS.md; determinism properties (same seed,
different seed, byte-stable canonical artifacts) are asserted directly.
"""
from __future__ import annotations

import copy
import json
import math

import pytest

from serving_verdict.errors import StatisticalArtifactError, StatisticalError
from serving_verdict.statistics import (
    INCONCLUSIVE_INSUFFICIENT_SAMPLE_SIZE,
    INCONCLUSIVE_STATISTICAL_UNCERTAINTY,
    NONPOSITIVE_BASELINE_STATISTIC,
    PROMOTE_ELIGIBLE,
    REJECT_INSUFFICIENT_EFFECT,
    StatisticalSample,
    StatisticalSpec,
    build_statistics_artifact,
    describe_arm,
    evaluate,
    verify_statistics_artifact,
)


def _spec(**overrides) -> StatisticalSpec:
    defaults = {
        "confidence_level": 0.95,
        "iterations": 1000,
        "seed": 1,
        "min_trials": 3,
        "threshold": 0.05,
        "direction": "lower_better",
    }
    defaults.update(overrides)
    return StatisticalSpec(**defaults)


# ---------------------------------------------------------------------------
# slice 1: StatisticalSample — fail-closed validation
# ---------------------------------------------------------------------------


def test_sample_rejects_empty():
    with pytest.raises(StatisticalError):
        StatisticalSample(values=[])


def test_sample_rejects_bool_values():
    for bad in ([True], [0, False]):
        with pytest.raises(StatisticalError):
            StatisticalSample(values=bad)


def test_sample_rejects_non_numeric_values():
    for bad in (["1.5"], [1.0, None], [1.0, "2"], [1.0, [2.0]]):
        with pytest.raises(StatisticalError):
            StatisticalSample(values=bad)


def test_sample_rejects_non_finite_values():
    for bad in ([float("nan")], [1.0, float("inf")], [1.0, float("-inf")]):
        with pytest.raises(StatisticalError):
            StatisticalSample(values=bad)


def test_sample_rejects_negative_values():
    for bad in ([-1.0], [0.0, -0.5]):
        with pytest.raises(StatisticalError):
            StatisticalSample(values=bad)


def test_sample_accepts_finite_nonnegative_mixed_types():
    s = StatisticalSample(values=[1, 2.5, 0.0, 1 / 3])
    assert s.values == (1.0, 2.5, 0.0, 1 / 3)
    assert all(isinstance(v, float) for v in s.values)


def test_sample_deep_immutability():
    source = [1.0, 2.0, 3.0]
    s = StatisticalSample(values=source)
    source.append(99.0)
    source[0] = 42.0
    source.clear()
    assert s.values == (1.0, 2.0, 3.0)
    with pytest.raises((TypeError, AttributeError)):
        s.values = (9.0,)  # type: ignore[misc]


def test_sample_aliasing_two_arms_is_safe():
    source = [1.0, 2.0, 3.0]
    base = StatisticalSample(values=source)
    cand = StatisticalSample(values=source)
    source.append(7.0)
    assert base.values == cand.values == (1.0, 2.0, 3.0)


# ---------------------------------------------------------------------------
# slice 2: StatisticalSpec — fail-closed bounds and exact types
# ---------------------------------------------------------------------------


def test_spec_accepts_valid():
    s = _spec()
    assert (s.confidence_level, s.iterations, s.seed, s.min_trials, s.threshold, s.direction) == (
        0.95,
        1000,
        1,
        3,
        0.05,
        "lower_better",
    )


@pytest.mark.parametrize(
    "kw",
    [
        {"confidence_level": 0.5},  # lower bound excluded
        {"confidence_level": 1.0},  # upper bound excluded
        {"confidence_level": 0.4},
        {"confidence_level": 1.1},
        {"confidence_level": float("nan")},
        {"confidence_level": float("inf")},
        {"iterations": 99},
        {"iterations": 10_000_001},
        {"iterations": 0},
        {"iterations": -5},
        {"seed": -1},
        {"seed": 2**63},
        {"min_trials": 1},
        {"min_trials": 0},
        {"min_trials": -1},
        {"threshold": -0.001},
        {"threshold": float("nan")},
        {"threshold": float("inf")},
        {"direction": "higher_is_better"},
        {"direction": ""},
    ],
)
def test_spec_rejects_out_of_bounds(kw):
    with pytest.raises(StatisticalError):
        _spec(**kw)


@pytest.mark.parametrize(
    "field, bad",
    [
        ("confidence_level", 1),  # int where float required
        ("iterations", 1000.0),  # float where int required
        ("seed", 1.0),
        ("min_trials", 3.0),
        ("threshold", "0.05"),
        ("confidence_level", True),  # bool where float required
        ("iterations", True),  # bool where int required
        ("seed", True),
        ("min_trials", True),
        ("threshold", True),
    ],
)
def test_spec_rejects_wrong_types(field, bad):
    with pytest.raises(StatisticalError):
        _spec(**{field: bad})


def test_spec_accepts_boundary_values():
    # boundary values must be accepted exactly
    _spec(confidence_level=0.5000001, iterations=100, seed=0, min_trials=2, threshold=0.0)
    _spec(confidence_level=0.9999999, iterations=10_000_000, seed=2**63 - 1, threshold=1.5)


# ---------------------------------------------------------------------------
# slice 3: descriptive statistics — hand-computed goldens (docs §3)
# ---------------------------------------------------------------------------
#
# Arm A = [1.0, 2.0, 3.0, 4.0, 5.0] (n=5):
#   mean = 15/5 = 3.0 (exact)
#   p50: h = 4*0.5 = 2.0 -> x[2] = 3.0
#   p95: h = 4*0.95 -> lo=3, frac = h-3 -> 4.0 + frac*(5.0-4.0)
#       (expected written with the IDENTICAL float expression, exact-by-
#        construction; 3.8 is not exactly representable, so 4.8 is NOT
#        asserted as a decimal literal)
#   sample stdev: sqrt(sum((x-3)^2)/4) = sqrt((4+1+0+1+4)/4) = sqrt(2.5)
#
# Arm B = [0.5, 0.5, 1.5, 2.5] (n=4, unsorted on purpose):
#   mean = 5/4 = 1.25 (exact)
#   p50: h = 3*0.5 = 1.5 -> x[1] + 0.5*(x[2]-x[1]) = 0.5 + 0.5*1.0 = 1.0
#   p95: h = 3*0.95 -> lo=2, frac = h-2 -> 1.5 + frac*(2.5-1.5)
#   sample stdev: deviations -0.75,-0.75,0.25,1.25 -> sum of squares 2.75
#       (all exact in binary) -> sqrt(2.75/3)


def test_describe_arm_golden_arm_a():
    s = StatisticalSample(values=[1.0, 2.0, 3.0, 4.0, 5.0])
    d = describe_arm(s)
    assert d.n == 5
    assert d.mean == 3.0
    assert d.median == 3.0
    assert d.p50 == 3.0
    assert d.p95 == 4.0 + (4 * 0.95 - 3) * (5.0 - 4.0)
    assert d.sample_stdev == math.sqrt(2.5)


def test_describe_arm_golden_arm_b_unsorted_input():
    s = StatisticalSample(values=[0.5, 0.5, 1.5, 2.5])
    d = describe_arm(s)
    assert d.n == 4
    assert d.mean == 1.25
    assert d.median == 1.0
    assert d.p50 == 1.0
    assert d.p95 == 1.5 + (3 * 0.95 - 2) * (2.5 - 1.5)
    assert d.sample_stdev == math.sqrt(2.75 / 3)


def test_describe_arm_single_value_degenerate():
    s = StatisticalSample(values=[7.5])
    d = describe_arm(s)
    assert d.n == 1
    assert d.mean == 7.5
    assert d.median == 7.5
    assert d.sample_stdev == 0.0
    assert d.p50 == 7.5
    assert d.p95 == 7.5


def test_describe_arm_quantile_interpolation_two_values():
    # n=2: p50 h=0.5 -> x[0] + 0.5*(x[1]-x[0]) = 1.0 + 1.5 = 2.5 (exact)
    #      p95 h=0.95 -> x[0] + frac*(x[1]-x[0]), frac = h - 0
    s = StatisticalSample(values=[1.0, 4.0])
    d = describe_arm(s)
    assert d.p50 == 2.5
    assert d.p95 == 1.0 + (1 * 0.95 - 0) * (4.0 - 1.0)


# ---------------------------------------------------------------------------
# slice 4: evaluate() — bootstrap CI goldens, determinism, policy
# ---------------------------------------------------------------------------
#
# DEGENERATE golden (fully hand-derived, seed-independent):
#   baseline  = [1.0, 1.0, 1.0] -> bootstrap mean ALWAYS 1.0
#   candidate = [0.9, 0.9, 0.9] -> bootstrap mean ALWAYS 0.9
#   lower_better: EVERY replicate effect = (1.0 - 0.9) / 1.0 = 0.09999999999999998
#   (1.0 - 0.9 in binary64; the float division by 1.0 preserves it.)
#   So the CI is EXACTLY (0.09999999999999998, 0.09999999999999998) for any
#   seed and any B in the validated range. Hand-derived, not measured.
#
# SPREAD golden (determinism pin, double-entry):
#   baseline  = [1.1, 1.08, 1.04, 1.02, 1.0, 0.98, 0.96]
#   candidate = [0.9, 0.88, 0.86, 0.84, 0.82, 0.8, 0.78]
#   effect (full samples) = 0.18105849582172712
#   seed=1, B=1000, 95% -> (0.14040114613180504, 0.22282608695652178)
#   seed=2, B=1000, 95% -> (0.13675213675213674, 0.21823204419889497)
# Both CIs were re-derived by an independent reference of the frozen
# algorithm (random.Random(seed); rng.choice draws; stable sort; index
# round((1-cl)/2 * B)) and pinned to the exact binary64 bits (double-entry).
_DEG_BASE = [1.0, 1.0, 1.0]
_DEG_CAND = [0.9, 0.9, 0.9]
_DEG_CI = (0.09999999999999998, 0.09999999999999998)
_SPREAD_BASE = [1.1, 1.08, 1.04, 1.02, 1.0, 0.98, 0.96]
_SPREAD_CAND = [0.9, 0.88, 0.86, 0.84, 0.82, 0.8, 0.78]
_SPREAD_EFFECT = 0.18105849582172712
_SPREAD_CI_S1 = (0.14040114613180504, 0.22282608695652178)
_SPREAD_CI_S2 = (0.13675213675213674, 0.21823204419889497)


def _degenerate_samples():
    return (
        StatisticalSample(values=list(_DEG_BASE)),
        StatisticalSample(values=list(_DEG_CAND)),
    )


def _spread_samples():
    return (
        StatisticalSample(values=list(_SPREAD_BASE)),
        StatisticalSample(values=list(_SPREAD_CAND)),
    )


def test_degenerate_ci_hand_derived_golden():
    # CI is exactly (0.1-as-float, 0.1-as-float) by construction: every
    # replicate effect equals (1.0 - 0.9)/1.0 in binary64.
    base, cand = _degenerate_samples()
    r = evaluate(base, cand, _spec())  # threshold 0.05
    assert r.verdict == PROMOTE_ELIGIBLE
    assert r.reason_codes == (PROMOTE_ELIGIBLE,)
    assert r.effect == 0.09999999999999998
    assert r.ci == _DEG_CI
    assert r.removed_samples == ()
    assert (r.seed, r.iterations, r.confidence_level, r.threshold, r.direction) == (
        1,
        1000,
        0.95,
        0.05,
        "lower_better",
    )
    assert r.baseline == describe_arm(base)
    assert r.candidate == describe_arm(cand)
    # hand-derived arm goldens
    assert r.baseline.n == 3
    assert r.baseline.mean == 1.0
    assert r.baseline.sample_stdev == 0.0
    assert r.candidate.mean == 0.9


def test_degenerate_ci_is_seed_and_iteration_independent():
    b1, c1 = _degenerate_samples()
    b2, c2 = _degenerate_samples()
    r1 = evaluate(b1, c1, _spec(seed=1, iterations=100))
    r2 = evaluate(b2, c2, _spec(seed=999, iterations=10_000))
    assert r1.ci == r2.ci == _DEG_CI


def test_spread_ci_golden_seed_1_pinned():
    base, cand = _spread_samples()
    r = evaluate(base, cand, _spec(seed=1))
    assert r.verdict == PROMOTE_ELIGIBLE  # lo 0.1404 >= t 0.05
    assert r.effect == _SPREAD_EFFECT
    assert r.ci == _SPREAD_CI_S1
    assert r.ci[0] < r.ci[1]  # non-degenerate interval actually spread


def test_spread_ci_golden_seed_2_pinned():
    base, cand = _spread_samples()
    r = evaluate(base, cand, _spec(seed=2))
    assert r.effect == _SPREAD_EFFECT
    assert r.ci == _SPREAD_CI_S2


def test_spread_ci_different_seed_changes_interval():
    base, cand = _spread_samples()
    r1 = evaluate(base, cand, _spec(seed=1))
    r2 = evaluate(base, cand, _spec(seed=2))
    assert r1.ci != r2.ci
    assert r1.seed == 1
    assert r2.seed == 2


def test_spread_ci_same_seed_deterministic_across_reconstruction():
    # fresh sample objects from fresh lists -> identical bits
    b1, c1 = _spread_samples()
    b2, c2 = _spread_samples()
    r1 = evaluate(b1, c1, _spec(seed=1))
    r2 = evaluate(b2, c2, _spec(seed=1))
    assert r1.ci == r2.ci
    assert r1.effect == r2.effect


def test_degenerate_ci_boundary_overlap_inconclusive():
    # CI (0.09999999999999998, 0.09999999999999998); t = 0.1:
    # lo (0.09999999999999998) < 0.1 -> not promote; hi (0.09999999999999998)
    # < 0.1 -> REJECT. (0.1-as-float 0.10000000000000000 > 0.09999999999999998.)
    base, cand = _degenerate_samples()
    r = evaluate(base, cand, _spec(threshold=0.1))
    assert r.verdict == REJECT_INSUFFICIENT_EFFECT


def test_degenerate_ci_lower_equal_threshold_promotes():
    # t = 0.09999999999999998 exactly: lo >= t -> PROMOTE_ELIGIBLE
    base, cand = _degenerate_samples()
    r = evaluate(base, cand, _spec(threshold=0.09999999999999998))
    assert r.verdict == PROMOTE_ELIGIBLE


def test_degenerate_ci_upper_equal_threshold_rejects():
    # A single-point CI (lo == hi) can NEVER straddle a threshold: for
    # t just above the point, hi < t -> REJECT. This is the mathematical
    # consequence of a degenerate interval; the overlap (INCONCLUSIVE)
    # boundary is exercised on the spread fixture instead.
    t = math.nextafter(0.1, math.inf)  # 0.10000000000000002 > 0.09999999999999998
    base, cand = _degenerate_samples()
    r = evaluate(base, cand, _spec(threshold=t))
    assert r.verdict == REJECT_INSUFFICIENT_EFFECT


def test_spread_ci_upper_equal_threshold_inconclusive():
    # Spread CI at seed 1 is (0.14040114613180504, 0.22282608695652178).
    # Set t = hi EXACTLY: hi < t is False (equal), lo >= t is False (lo < t)
    # -> falls through to overlap -> INCONCLUSIVE_STATISTICAL_UNCERTAINTY.
    base, cand = _spread_samples()
    r = evaluate(base, cand, _spec(seed=1, threshold=_SPREAD_CI_S1[1]))
    assert r.verdict == INCONCLUSIVE_STATISTICAL_UNCERTAINTY
    assert r.reason_codes == (INCONCLUSIVE_STATISTICAL_UNCERTAINTY,)


def test_spread_ci_lower_equal_threshold_promotes():
    # Set t = lo EXACTLY: lo >= t is True -> PROMOTE_ELIGIBLE.
    base, cand = _spread_samples()
    r = evaluate(base, cand, _spec(seed=1, threshold=_SPREAD_CI_S1[0]))
    assert r.verdict == PROMOTE_ELIGIBLE


def test_spread_ci_reject_when_upper_below_threshold():
    # t = 0.3: hi (0.2228... at seed 1) < 0.3 -> REJECT
    base, cand = _spread_samples()
    r = evaluate(base, cand, _spec(seed=1, threshold=0.3))
    assert r.verdict == REJECT_INSUFFICIENT_EFFECT
    assert r.reason_codes == (REJECT_INSUFFICIENT_EFFECT,)


def test_spread_ci_threshold_crossing_inconclusive():
    # t = 0.15: lo 0.1404 < 0.15 <= hi 0.2228 -> overlap
    base, cand = _spread_samples()
    r = evaluate(base, cand, _spec(seed=1, threshold=0.15))
    assert r.verdict == INCONCLUSIVE_STATISTICAL_UNCERTAINTY


def test_spread_ci_threshold_crossing_promote():
    # t = 0.14 (exact dyadic): lo 0.14040114613180504 >= 0.14 -> PROMOTE
    base, cand = _spread_samples()
    r = evaluate(base, cand, _spec(seed=1, threshold=0.14))
    assert r.verdict == PROMOTE_ELIGIBLE


def test_effect_is_full_sample_direction_aware():
    # higher_better: (mean_c - mean_b) / mean_b
    base = StatisticalSample(values=[1.0, 1.0, 1.0])
    cand = StatisticalSample(values=[1.1, 1.1, 1.1])
    r = evaluate(base, cand, _spec(direction="higher_better"))
    assert r.effect == (1.1 - 1.0) / 1.0


def test_insufficient_sample_size_precedes_statistics():
    base = StatisticalSample(values=[1.0, 1.0])  # n=2 < min_trials=3
    cand = StatisticalSample(values=[0.5])
    r = evaluate(base, cand, _spec())
    assert r.verdict == INCONCLUSIVE_INSUFFICIENT_SAMPLE_SIZE
    assert r.reason_codes == (INCONCLUSIVE_INSUFFICIENT_SAMPLE_SIZE,)
    assert r.effect is None
    assert r.ci is None
    # descriptive stats still reported for both arms
    assert r.baseline.n == 2
    assert r.candidate.n == 1
    assert r.removed_samples == ()


def test_nonpositive_baseline_statistic():
    base = StatisticalSample(values=[0.0, 0.0, 0.0])
    cand = StatisticalSample(values=[0.0, 0.0, 0.0])
    r = evaluate(base, cand, _spec())
    assert r.verdict == NONPOSITIVE_BASELINE_STATISTIC
    assert r.reason_codes == (NONPOSITIVE_BASELINE_STATISTIC,)
    assert r.effect is None
    assert r.ci is None


def test_zero_baseline_trial_never_crashes_bootstrap() -> None:
    base = StatisticalSample(values=[0.0, 1.0])
    cand = StatisticalSample(values=[0.5, 1.5])
    r = evaluate(base, cand, _spec(min_trials=2, iterations=10_000))
    assert r.verdict == NONPOSITIVE_BASELINE_STATISTIC
    assert r.effect is None
    assert r.ci is None


def test_outlier_is_retained_and_reported_in_descriptives() -> None:
    baseline = StatisticalSample([1.0, 1.0, 1.0, 100.0])
    candidate = StatisticalSample([0.9, 0.9, 0.9, 0.9])
    result = evaluate(baseline, candidate, _spec(min_trials=3, iterations=100))
    assert result.baseline.n == 4
    assert result.baseline.mean == 25.75
    assert result.removed_samples == ()


def test_statistics_artifact_roundtrip_and_volatile_created_at() -> None:
    base, cand = _spread_samples()
    spec = _spec(seed=1)
    a = build_statistics_artifact(
        base,
        cand,
        spec,
        metric_id="ttft_s",
        workload_id="frozen-edit-v1",
        created_at="2026-08-20T00:00:00Z",
    )
    b = build_statistics_artifact(
        base,
        cand,
        spec,
        metric_id="ttft_s",
        workload_id="frozen-edit-v1",
        created_at="2026-08-21T00:00:00Z",
    )
    assert a["artifact_digest"] == b["artifact_digest"]
    assert a["provenance_id"] == b["provenance_id"]
    verified = verify_statistics_artifact(json.loads(json.dumps(a)))
    assert verified == evaluate(base, cand, spec)


def test_statistics_artifact_tamper_fails_and_input_aliases_are_copied() -> None:
    base_values = [1.0, 1.1, 0.9]
    cand_values = [0.8, 0.9, 0.85]
    artifact = build_statistics_artifact(
        StatisticalSample(base_values),
        StatisticalSample(cand_values),
        _spec(iterations=100),
        metric_id="ttft_s",
        workload_id="w1",
        created_at="t1",
    )
    base_values[0] = 999.0
    cand_values.clear()
    assert artifact["baseline_values"][0] == 1.0
    tampered = copy.deepcopy(artifact)
    tampered["candidate_values"][0] = 0.01
    with pytest.raises(StatisticalArtifactError):
        verify_statistics_artifact(tampered)
    volatile = copy.deepcopy(artifact)
    volatile["created_at"] = "different"
    verify_statistics_artifact(volatile)


def test_statistics_artifact_rejects_result_or_provenance_forgery() -> None:
    base, cand = _degenerate_samples()
    artifact = build_statistics_artifact(
        base,
        cand,
        _spec(iterations=100),
        metric_id="ttft_s",
        workload_id="w1",
        created_at="t1",
    )
    forged = copy.deepcopy(artifact)
    forged["result"]["verdict"] = REJECT_INSUFFICIENT_EFFECT
    with pytest.raises(StatisticalArtifactError):
        verify_statistics_artifact(forged)
    forged = copy.deepcopy(artifact)
    forged["provenance_id"] = "prov:" + "0" * 32
    with pytest.raises(StatisticalArtifactError):
        verify_statistics_artifact(forged)


def test_insufficient_sample_precedes_nonpositive_baseline():
    # all-zero baseline but n < min_trials: rule 1 must fire first
    base = StatisticalSample(values=[0.0])
    cand = StatisticalSample(values=[0.0])
    r = evaluate(base, cand, _spec())
    assert r.verdict == INCONCLUSIVE_INSUFFICIENT_SAMPLE_SIZE
