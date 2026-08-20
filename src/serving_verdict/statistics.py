"""Deterministic statistical verdict engine (v0.4).

Pure function of (baseline sample, candidate sample, spec) -> result, with
a deterministic seeded percentile bootstrap CI. Semantics are frozen in
docs/STATISTICS.md: independent repeated trials, no implicit outlier
removal, mean-based direction-aware relative effect, exact policy rule
order, and a canonical volatile-excluded digest artifact.

Standard library only (``random`` Mersenne-Twister is bit-stable for
integer seeds across platforms and CPython 3.x). No p-values, no
coverage claims, no parametric assumptions beyond i.i.d. resampling.
"""
from __future__ import annotations

import hashlib
import math
import random
from dataclasses import dataclass
from typing import Any

from serving_verdict.canonical import canonicalize, digest_payload
from serving_verdict.errors import StatisticalArtifactError, StatisticalError

# Exact, stable verdict vocabulary (docs/STATISTICS.md §5).
PROMOTE_ELIGIBLE = "PROMOTE_ELIGIBLE"
REJECT_INSUFFICIENT_EFFECT = "REJECT_INSUFFICIENT_EFFECT"
INCONCLUSIVE_STATISTICAL_UNCERTAINTY = "INCONCLUSIVE_STATISTICAL_UNCERTAINTY"
INCONCLUSIVE_INSUFFICIENT_SAMPLE_SIZE = "INCONCLUSIVE_INSUFFICIENT_SAMPLE_SIZE"
NONPOSITIVE_BASELINE_STATISTIC = "NONPOSITIVE_BASELINE_STATISTIC"

VERDICTS: frozenset[str] = frozenset(
    {
        PROMOTE_ELIGIBLE,
        REJECT_INSUFFICIENT_EFFECT,
        INCONCLUSIVE_STATISTICAL_UNCERTAINTY,
        INCONCLUSIVE_INSUFFICIENT_SAMPLE_SIZE,
        NONPOSITIVE_BASELINE_STATISTIC,
    }
)


@dataclass(frozen=True)
class StatisticalSample:
    """One arm (baseline or candidate): independent repeated metric trials.

    Fail-closed: rejects empty samples, bools, non-numbers, non-finite
    values, and negative values. Stores an immutable tuple of binary64
    floats, copied from the caller (deep immutability).
    """

    values: tuple[float, ...]

    def __init__(self, values: Any) -> None:
        if isinstance(values, (str, bytes)) or not hasattr(values, "__iter__"):
            raise StatisticalError("sample values must be a sequence of numbers")
        try:
            sequence = list(values)
        except TypeError as exc:
            raise StatisticalError(f"sample values are not iterable: {exc}") from exc
        if not sequence:
            raise StatisticalError("sample values must contain at least one value")
        normalized: list[float] = []
        for i, v in enumerate(sequence):
            if isinstance(v, bool):
                raise StatisticalError(f"sample value at index {i} is a bool")
            if not isinstance(v, (int, float)):
                raise StatisticalError(f"sample value at index {i} is not a number")
            f = float(v)
            if not math.isfinite(f):
                raise StatisticalError(f"sample value at index {i} is not finite")
            if f < 0.0:
                raise StatisticalError(f"sample value at index {i} is negative")
            normalized.append(f)
        object.__setattr__(self, "values", tuple(normalized))


@dataclass(frozen=True)
class StatisticalSpec:
    """Required, bounded statistical parameters (no defaults).

    Fail-closed exact-type validation (bool is rejected everywhere because
    ``bool`` subclasses ``int``); validation order is the field order below.
    """

    confidence_level: float
    iterations: int
    seed: int
    min_trials: int
    threshold: float
    direction: str

    def __init__(
        self,
        confidence_level: Any,
        iterations: Any,
        seed: Any,
        min_trials: Any,
        threshold: Any,
        direction: Any,
    ) -> None:
        if isinstance(confidence_level, bool) or not isinstance(confidence_level, float):
            raise StatisticalError("confidence_level must be a float")
        if not (0.5 < confidence_level < 1.0) or not math.isfinite(confidence_level):
            raise StatisticalError("confidence_level must be finite and in (0.5, 1.0)")
        if isinstance(iterations, bool) or not isinstance(iterations, int):
            raise StatisticalError("iterations must be an int")
        if not (100 <= iterations <= 10_000_000):
            raise StatisticalError("iterations must be in [100, 10_000_000]")
        if isinstance(seed, bool) or not isinstance(seed, int):
            raise StatisticalError("seed must be an int")
        if not (0 <= seed <= 2**63 - 1):
            raise StatisticalError("seed must be in [0, 2**63 - 1]")
        if isinstance(min_trials, bool) or not isinstance(min_trials, int):
            raise StatisticalError("min_trials must be an int")
        if min_trials < 2:
            raise StatisticalError("min_trials must be >= 2")
        if isinstance(threshold, bool) or not isinstance(threshold, float):
            raise StatisticalError("threshold must be a float")
        if not math.isfinite(threshold) or threshold < 0.0:
            raise StatisticalError("threshold must be finite and >= 0.0")
        if direction not in ("lower_better", "higher_better"):
            raise StatisticalError("direction must be 'lower_better' or 'higher_better'")
        object.__setattr__(self, "confidence_level", confidence_level)
        object.__setattr__(self, "iterations", iterations)
        object.__setattr__(self, "seed", seed)
        object.__setattr__(self, "min_trials", min_trials)
        object.__setattr__(self, "threshold", threshold)
        object.__setattr__(self, "direction", direction)


@dataclass(frozen=True)
class ArmSummary:
    """Per-arm descriptive statistics (always reported)."""

    n: int
    mean: float
    median: float
    sample_stdev: float
    p50: float
    p95: float


@dataclass(frozen=True)
class StatisticalResult:
    """Deep-immutable engine output."""

    verdict: str
    reason_codes: tuple[str, ...]
    effect: float | None
    ci: tuple[float, float] | None
    seed: int
    iterations: int
    confidence_level: float
    threshold: float
    direction: str
    baseline: ArmSummary
    candidate: ArmSummary
    removed_samples: tuple[float, ...]


def _percentile(sorted_values: tuple[float, ...], p: float) -> float:
    """Linear-interpolation quantile (docs/STATISTICS.md §3.1, R-7)."""
    n = len(sorted_values)
    if n == 1:
        return sorted_values[0]
    h = (n - 1) * p
    lo = math.floor(h)
    frac = h - lo
    if lo + 1 < n:
        return sorted_values[lo] + frac * (sorted_values[lo + 1] - sorted_values[lo])
    return sorted_values[lo]


def _stable_sum(values: tuple[float, ...]) -> float:
    """Explicit binary64 left fold, independent of built-in sum changes."""
    total = 0.0
    for value in values:
        total += value
    return total


def describe_arm(sample: StatisticalSample) -> ArmSummary:
    """Per-arm descriptive statistics (docs/STATISTICS.md §3)."""
    values = sample.values
    n = len(values)
    mean = _stable_sum(values) / n
    ordered = tuple(sorted(values))
    stdev = (
        0.0
        if n == 1
        else math.sqrt(sum((v - mean) ** 2 for v in values) / (n - 1))
    )
    return ArmSummary(
        n=n,
        mean=mean,
        median=_percentile(ordered, 0.5),
        sample_stdev=stdev,
        p50=_percentile(ordered, 0.5),
        p95=_percentile(ordered, 0.95),
    )


def _relative_improvement(base_mean: float, cand_mean: float, direction: str) -> float:
    """Direction-aware relative improvement (docs/STATISTICS.md §2)."""
    if direction == "lower_better":
        return (base_mean - cand_mean) / base_mean
    return (cand_mean - base_mean) / base_mean


def _bootstrap_ci(
    base: StatisticalSample, cand: StatisticalSample, spec: StatisticalSpec
) -> tuple[float, float]:
    """Deterministic seeded percentile bootstrap CI (docs/STATISTICS.md §4)."""
    base_values = base.values
    cand_values = cand.values
    n_b = len(base_values)
    n_c = len(cand_values)
    rng = random.Random(spec.seed)
    estimates: list[float] = []
    for _ in range(spec.iterations):
        b_draws = tuple(rng.choice(base_values) for _ in range(n_b))
        c_draws = tuple(rng.choice(cand_values) for _ in range(n_c))
        b_mean = _stable_sum(b_draws) / n_b
        c_mean = _stable_sum(c_draws) / n_c
        estimates.append(_relative_improvement(b_mean, c_mean, spec.direction))
    estimates.sort()
    alpha = 1.0 - spec.confidence_level
    k_lo = round((alpha / 2.0) * spec.iterations)
    k_lo = max(0, min(k_lo, spec.iterations - 1))
    k_hi = spec.iterations - k_lo
    k_hi = max(k_lo + 1, min(k_hi, spec.iterations - 1))
    return (estimates[k_lo], estimates[k_hi])


def evaluate(
    baseline: StatisticalSample, candidate: StatisticalSample, spec: StatisticalSpec
) -> StatisticalResult:
    """Run the deterministic statistical verdict (docs/STATISTICS.md §5).

    Frozen rule order:
      1. n_b < min_trials or n_c < min_trials -> INCONCLUSIVE_INSUFFICIENT_SAMPLE_SIZE
      2. baseline mean <= 0 -> NONPOSITIVE_BASELINE_STATISTIC
      3. ci_lo >= threshold -> PROMOTE_ELIGIBLE
      4. ci_hi < threshold  -> REJECT_INSUFFICIENT_EFFECT
      5. otherwise          -> INCONCLUSIVE_STATISTICAL_UNCERTAINTY
    """
    base_summary = describe_arm(baseline)
    cand_summary = describe_arm(candidate)
    common: dict[str, Any] = {
        "seed": spec.seed,
        "iterations": spec.iterations,
        "confidence_level": spec.confidence_level,
        "threshold": spec.threshold,
        "direction": spec.direction,
        "baseline": base_summary,
        "candidate": cand_summary,
        "removed_samples": (),  # v0.4 never removes samples (FR-1.7)
    }
    if len(baseline.values) < spec.min_trials or len(candidate.values) < spec.min_trials:
        verdict = INCONCLUSIVE_INSUFFICIENT_SAMPLE_SIZE
        return StatisticalResult(verdict, (verdict,), None, None, **common)
    if any(value <= 0.0 for value in baseline.values):
        verdict = NONPOSITIVE_BASELINE_STATISTIC
        return StatisticalResult(verdict, (verdict,), None, None, **common)
    effect = _relative_improvement(base_summary.mean, cand_summary.mean, spec.direction)
    lo, hi = _bootstrap_ci(baseline, candidate, spec)
    if lo >= spec.threshold:
        verdict = PROMOTE_ELIGIBLE
    elif hi < spec.threshold:
        verdict = REJECT_INSUFFICIENT_EFFECT
    else:
        verdict = INCONCLUSIVE_STATISTICAL_UNCERTAINTY
    return StatisticalResult(verdict, (verdict,), effect, (lo, hi), **common)


STATISTICS_ARTIFACT_SCHEMA = "serving-verdict.statistics.v0.1"
_ARTIFACT_KEYS = frozenset(
    {
        "schema_version",
        "provenance_id",
        "metric_id",
        "workload_id",
        "baseline_values",
        "candidate_values",
        "spec",
        "result",
        "created_at",
        "artifact_digest",
    }
)


def _spec_payload(spec: StatisticalSpec) -> dict[str, Any]:
    return {
        "confidence_level": spec.confidence_level,
        "iterations": spec.iterations,
        "seed": spec.seed,
        "min_trials": spec.min_trials,
        "threshold": spec.threshold,
        "direction": spec.direction,
    }


def _arm_payload(arm: ArmSummary) -> dict[str, Any]:
    return {
        "n": arm.n,
        "mean": arm.mean,
        "median": arm.median,
        "sample_stdev": arm.sample_stdev,
        "p50": arm.p50,
        "p95": arm.p95,
    }


def _result_payload(result: StatisticalResult) -> dict[str, Any]:
    return {
        "verdict": result.verdict,
        "reason_codes": list(result.reason_codes),
        "effect": result.effect,
        "ci": list(result.ci) if result.ci is not None else None,
        "seed": result.seed,
        "iterations": result.iterations,
        "confidence_level": result.confidence_level,
        "threshold": result.threshold,
        "direction": result.direction,
        "baseline": _arm_payload(result.baseline),
        "candidate": _arm_payload(result.candidate),
        "removed_samples": list(result.removed_samples),
    }


def _artifact_substantive(artifact: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in artifact.items()
        if key not in {"created_at", "artifact_digest"}
    }


def _provenance_identity(artifact: dict[str, Any]) -> dict[str, Any]:
    return {
        key: artifact[key]
        for key in (
            "schema_version",
            "metric_id",
            "workload_id",
            "baseline_values",
            "candidate_values",
            "spec",
        )
    }


def _provenance_id(identity: dict[str, Any]) -> str:
    return "prov:" + hashlib.sha256(canonicalize(identity)).hexdigest()[:32]


def build_statistics_artifact(
    baseline: StatisticalSample,
    candidate: StatisticalSample,
    spec: StatisticalSpec,
    *,
    metric_id: str,
    workload_id: str,
    created_at: str,
) -> dict[str, Any]:
    """Evaluate and seal exact samples/spec into a self-verifiable artifact."""
    if not isinstance(metric_id, str) or not metric_id.strip():
        raise StatisticalError("metric_id must be a non-empty string")
    if not isinstance(workload_id, str) or not workload_id.strip():
        raise StatisticalError("workload_id must be a non-empty string")
    if not isinstance(created_at, str):
        raise StatisticalError("created_at must be a string")
    result = evaluate(baseline, candidate, spec)
    artifact: dict[str, Any] = {
        "schema_version": STATISTICS_ARTIFACT_SCHEMA,
        "provenance_id": "",
        "metric_id": metric_id.strip(),
        "workload_id": workload_id.strip(),
        "baseline_values": list(baseline.values),
        "candidate_values": list(candidate.values),
        "spec": _spec_payload(spec),
        "result": _result_payload(result),
        "created_at": created_at,
    }
    artifact["provenance_id"] = _provenance_id(_provenance_identity(artifact))
    artifact["artifact_digest"] = digest_payload(
        canonicalize(_artifact_substantive(artifact))
    )
    return artifact


def verify_statistics_artifact(artifact: object) -> StatisticalResult:
    """Recompute digest, provenance, inputs and statistical result fail-closed."""
    if not isinstance(artifact, dict) or set(artifact) != _ARTIFACT_KEYS:
        raise StatisticalArtifactError("statistics artifact shape is invalid")
    if artifact.get("schema_version") != STATISTICS_ARTIFACT_SCHEMA:
        raise StatisticalArtifactError("statistics artifact schema is unsupported")
    try:
        expected_digest = digest_payload(canonicalize(_artifact_substantive(artifact)))
    except Exception as exc:
        raise StatisticalArtifactError("statistics artifact is not canonical") from exc
    if artifact.get("artifact_digest") != expected_digest:
        raise StatisticalArtifactError("statistics artifact digest mismatch")
    try:
        expected_provenance = _provenance_id(_provenance_identity(artifact))
    except Exception as exc:
        raise StatisticalArtifactError("statistics provenance is malformed") from exc
    if artifact.get("provenance_id") != expected_provenance:
        raise StatisticalArtifactError("statistics provenance mismatch")
    spec_doc = artifact.get("spec")
    if not isinstance(spec_doc, dict) or set(spec_doc) != {
        "confidence_level",
        "iterations",
        "seed",
        "min_trials",
        "threshold",
        "direction",
    }:
        raise StatisticalArtifactError("statistics spec is malformed")
    try:
        baseline = StatisticalSample(artifact.get("baseline_values"))
        candidate = StatisticalSample(artifact.get("candidate_values"))
        spec = StatisticalSpec(**spec_doc)
        result = evaluate(baseline, candidate, spec)
    except (StatisticalError, TypeError) as exc:
        raise StatisticalArtifactError("statistics inputs are malformed") from exc
    if artifact.get("result") != _result_payload(result):
        raise StatisticalArtifactError("statistics result does not match bound inputs")
    return result
