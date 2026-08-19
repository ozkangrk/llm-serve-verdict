"""Baseline vs candidate compare for normalized benchmark summaries (v0.4).

Inputs are *sealed* benchmark summaries (see ``summary.py``). The compare is
a pure function:

  1. Comparability — workload, model and protocol sections must match
     exactly (frozen comparability context). Any mismatch is INCONCLUSIVE
     with the exact reason code(s).
  2. Missing / UNMEASURABLE gated metrics — INCONCLUSIVE with the exact
     reason code; never treated as zero.
  3. Zero baseline + relative gate — the relative improvement is undefined;
     INCONCLUSIVE unless the gate is absolute-only.
  4. Quality hard gate (if configured) — any regression or tie on the
     quality metric is a REJECT that precedes all performance gates.
  5. Performance gates — configurable absolute and/or relative thresholds,
     direction-aware: TTFT / latency are lower-better, throughput /
     success / tool / quality are higher-better.
  6. Otherwise PROMOTE.

No network, no subprocess, no LLM: the verdict and its reason codes are a
deterministic function of the two sealed summaries and the gate config. The
``claim_boundary`` is echoed verbatim into the result and the artifact; it
states what the decision does and does not claim.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from serving_verdict.experiment_artifact import seal_artifact, verify_artifact
from serving_verdict.summary import UNMEASURABLE, Summary

COMPARE_SCHEMA_VERSION = "serving-verdict.compare.v0.1"

# Reason codes (exact, stable vocabulary).
INCOMPARABLE_WORKLOAD = "INCOMPARABLE_WORKLOAD"
INCOMPARABLE_MODEL = "INCOMPARABLE_MODEL"
INCOMPARABLE_PROTOCOL = "INCOMPARABLE_PROTOCOL"
METRIC_MISSING = "METRIC_MISSING"
METRIC_UNMEASURABLE = "METRIC_UNMEASURABLE"
ZERO_BASELINE = "ZERO_BASELINE"
QUALITY_GATE_FAILED = "QUALITY_GATE_FAILED"
REL_GATE_FAILED = "REL_GATE_FAILED"
ABS_GATE_FAILED = "ABS_GATE_FAILED"
ALL_GATES_PASSED = "ALL_GATES_PASSED"

#: Direction of each comparable metric. Lower-better: TTFT and any latency.
#: Higher-better: throughput, success, tool accuracy, quality, decode rate,
#: concurrency headroom.
METRIC_DIRECTIONS: dict[str, str] = {
    "ttft_s": "lower_better",
    "e2e_latency_s": "lower_better",
    "decode_latency_ms": "lower_better",
    "peak_memory_gb": "lower_better",
    "throughput_rps": "higher_better",
    "success_rate": "higher_better",
    "tool_accuracy": "higher_better",
    "quality_score": "higher_better",
    "concurrency_max": "higher_better",
}


@dataclass(frozen=True)
class MetricGate:
    """A configurable gate on one metric.

    ``min_relative_improvement`` gates the relative improvement
    ``improvement / 1`` (see ``CompareResult.deltas``); ``min_absolute_improvement``
    gates the absolute improvement in the metric's units. A gate with both set
    must pass both. A gate with neither is an error.
    """

    metric: str
    min_relative_improvement: float | None = None
    min_absolute_improvement: float | None = None


@dataclass(frozen=True)
class CompareConfig:
    gates: tuple[MetricGate, ...]
    quality_gate: MetricGate | None = None
    claim_boundary: str = "frozen-workload-only"


@dataclass(frozen=True)
class MetricDelta:
    metric: str
    direction: str
    baseline: float
    candidate: float
    delta: float
    relative_delta: float | None
    improvement: float | None


@dataclass(frozen=True)
class CompareResult:
    verdict: str  # "PROMOTE" | "REJECT" | "INCONCLUSIVE"
    reason_codes: tuple[str, ...]
    deltas: tuple[MetricDelta, ...]
    claim_boundary: str


def _direction(metric: str) -> str:
    if metric not in METRIC_DIRECTIONS:
        raise ValueError(f"unknown metric in gate: {metric!r}")
    return METRIC_DIRECTIONS[metric]


def _gate_metrics(cfg: CompareConfig) -> tuple[MetricGate, ...]:
    """Quality gate first (hard gate), then performance gates in config order."""
    quality = (cfg.quality_gate,) if cfg.quality_gate is not None else ()
    return quality + tuple(cfg.gates)


def _value(summary: Summary, metric: str) -> float | None | str:
    return summary.measurements.get(metric)


def _delta_row(metric: str, base: float, cand: float) -> MetricDelta:
    direction = _direction(metric)
    delta = cand - base
    if base == 0:
        relative = None
        improvement: float | None = None
    else:
        improvement = (
            (base - cand) / base
            if direction == "lower_better"
            else (cand - base) / base
        )
        relative = (cand - base) / base
    return MetricDelta(
        metric=metric,
        direction=direction,
        baseline=base,
        candidate=cand,
        delta=delta,
        relative_delta=relative,
        improvement=improvement,
    )


def _gate_outcome(row: MetricDelta, gate: MetricGate) -> str | None:
    """Return the failing reason code for one gate, or None if it passes.

    The absolute improvement is the signed improvement in the metric's own
    units: ``(baseline - candidate)`` for lower-better metrics and
    ``(candidate - baseline)`` for higher-better metrics. It is well-defined
    for a zero baseline. The relative improvement requires a non-zero
    baseline and is ``improvement / baseline``; the caller handles the zero
    baseline case before calling with a relative gate active.
    """
    if gate.min_absolute_improvement is not None:
        if row.direction == "lower_better":
            absolute_improvement = row.baseline - row.candidate
        else:
            absolute_improvement = row.candidate - row.baseline
        if absolute_improvement < gate.min_absolute_improvement:
            return ABS_GATE_FAILED
    if gate.min_relative_improvement is not None:
        assert row.improvement is not None  # zero baseline excluded by caller
        if row.improvement < gate.min_relative_improvement:
            return REL_GATE_FAILED
    return None


def compare_summaries(baseline: Summary, candidate: Summary, cfg: CompareConfig) -> CompareResult:
    """Compare two sealed summaries. Pure, deterministic, fail-closed."""
    # -- Rule 1: frozen comparability context --------------------------------
    incomparability: list[str] = []
    if baseline.workload != candidate.workload:
        incomparability.append(INCOMPARABLE_WORKLOAD)
    if baseline.model != candidate.model:
        incomparability.append(INCOMPARABLE_MODEL)
    if baseline.protocol != candidate.protocol:
        incomparability.append(INCOMPARABLE_PROTOCOL)
    if incomparability:
        return CompareResult(
            verdict="INCONCLUSIVE",
            reason_codes=tuple(incomparability),
            deltas=(),
            claim_boundary=cfg.claim_boundary,
        )

    gates = _gate_metrics(cfg)

    def inconclusive(code: str, deltas: tuple[MetricDelta, ...] = ()) -> CompareResult:
        return CompareResult("INCONCLUSIVE", (code,), deltas, cfg.claim_boundary)

    # -- Rules 2-3: gated metrics must exist, be measured, and (for relative
    #    gates) have a non-zero baseline. -------------------------------------
    rows: dict[str, MetricDelta] = {}
    for gate in gates:
        base_v = _value(baseline, gate.metric)
        cand_v = _value(candidate, gate.metric)
        if base_v is None or cand_v is None:
            return inconclusive(METRIC_MISSING)
        if base_v == UNMEASURABLE or cand_v == UNMEASURABLE:
            return inconclusive(METRIC_UNMEASURABLE)
        base_f, cand_f = float(base_v), float(cand_v)
        rows[gate.metric] = _delta_row(gate.metric, base_f, cand_f)
        if base_f == 0 and gate.min_relative_improvement is not None:
            return inconclusive(ZERO_BASELINE, tuple(rows.values()))

    # Deltas are reported for exactly the gated metrics, in stable sorted
    # metric-name order. (No auto-conversion, no extra metrics.)
    ordered_rows = tuple(rows[m] for m in sorted(rows))

    # -- Rule 4: quality hard gate precedes performance gates -----------------
    if cfg.quality_gate is not None:
        code = _gate_outcome(rows[cfg.quality_gate.metric], cfg.quality_gate)
        if code is not None:
            return CompareResult("REJECT", (QUALITY_GATE_FAILED,), ordered_rows, cfg.claim_boundary)

    # -- Rule 5: performance gates --------------------------------------------
    failures: list[str] = []
    for gate in cfg.gates:
        code = _gate_outcome(rows[gate.metric], gate)
        if code is not None and code not in failures:
            failures.append(code)
    if failures:
        return CompareResult(
            "REJECT", tuple(sorted(set(failures))), ordered_rows, cfg.claim_boundary
        )

    # -- Rule 6: promote ------------------------------------------------------
    return CompareResult("PROMOTE", (ALL_GATES_PASSED,), ordered_rows, cfg.claim_boundary)


# ---------------------------------------------------------------------------
# canonical artifact
# ---------------------------------------------------------------------------


def _gate_dict(gate: MetricGate) -> dict[str, Any]:
    return {
        "metric": gate.metric,
        "min_relative_improvement": gate.min_relative_improvement,
        "min_absolute_improvement": gate.min_absolute_improvement,
    }


def build_compare_artifact(
    result: CompareResult,
    baseline: Summary,
    candidate: Summary,
    cfg: CompareConfig,
    created_at: str,
) -> dict[str, Any]:
    """Seal a compare result into a canonical artifact with provenance ID."""
    identity = {
        "kind": "compare",
        "schema_version": COMPARE_SCHEMA_VERSION,
        "baseline_digest": baseline.digest,
        "candidate_digest": candidate.digest,
        "config": {
            "gates": [_gate_dict(g) for g in cfg.gates],
            "quality_gate": _gate_dict(cfg.quality_gate) if cfg.quality_gate else None,
            "claim_boundary": cfg.claim_boundary,
        },
    }
    payload: dict[str, Any] = {
        "verdict": result.verdict,
        "reason_codes": list(result.reason_codes),
        "deltas": [
            {
                "metric": d.metric,
                "direction": d.direction,
                "baseline": d.baseline,
                "candidate": d.candidate,
                "delta": d.delta,
                "relative_delta": d.relative_delta,
                "improvement": d.improvement,
            }
            for d in result.deltas
        ],
        "baseline": {"sha256": baseline.digest, "context": baseline.context},
        "candidate": {"sha256": candidate.digest, "context": candidate.context},
        "gates": {
            "performance": [_gate_dict(g) for g in cfg.gates],
            "quality": _gate_dict(cfg.quality_gate) if cfg.quality_gate else None,
        },
        "claim_boundary": result.claim_boundary,
    }
    return seal_artifact(COMPARE_SCHEMA_VERSION, identity, payload, created_at)


def verify_compare_artifact(artifact: Any) -> dict[str, Any]:
    """Fail-closed verification of a compare artifact."""
    return verify_artifact(
        artifact,
        COMPARE_SCHEMA_VERSION,
        (
            "schema_version",
            "provenance_id",
            "verdict",
            "reason_codes",
            "deltas",
            "baseline",
            "candidate",
            "gates",
            "claim_boundary",
            "created_at",
            "artifact_digest",
        ),
    )
