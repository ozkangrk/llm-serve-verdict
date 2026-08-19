"""Baseline vs candidate compare: comparability, direction-aware deltas, gates.

Golden vectors here are hand-computed; every number below was derived by
hand from the definitions, not read off an implementation run.
"""
from __future__ import annotations

import math

import pytest

from serving_verdict.compare import (
    ABS_GATE_FAILED,
    ALL_GATES_PASSED,
    COMPARE_SCHEMA_VERSION,
    INCOMPARABLE_MODEL,
    INCOMPARABLE_PROTOCOL,
    INCOMPARABLE_WORKLOAD,
    METRIC_UNMEASURABLE,
    QUALITY_GATE_FAILED,
    REL_GATE_FAILED,
    ZERO_BASELINE,
    CompareConfig,
    MetricGate,
    build_compare_artifact,
    compare_summaries,
    verify_compare_artifact,
)
from serving_verdict.errors import ArtifactIntegrityError
from serving_verdict.summary import UNMEASURABLE, parse_summary_payload
from tests.helpers_v04 import make_parsed, make_summary


def _cfg(**kw) -> CompareConfig:
    defaults = {
        "gates": (MetricGate(metric="ttft_s", min_relative_improvement=0.05),),
        "quality_gate": None,
        "claim_boundary": "frozen-workload-only",
    }
    defaults.update(kw)
    return CompareConfig(**defaults)


# ---------------------------------------------------------------------------
# comparability matrix: every one of the frozen contexts must match
# ---------------------------------------------------------------------------


def test_comparable_promote():
    base = make_parsed()
    cand = make_parsed(**{"measurements": {"ttft_s": 0.25}})
    result = compare_summaries(base, cand, _cfg())
    assert result.verdict == "PROMOTE"
    assert result.reason_codes == (ALL_GATES_PASSED,)
    assert result.claim_boundary == "frozen-workload-only"


@pytest.mark.parametrize(
    "override",
    [
        {"model": {"id": "other-model", "quantization": "fp8", "architecture": "moe-2x8"}},
        {"workload": {"id": "workload-b", "requests": 300, "input_tokens_mean": 1400, "output_tokens_mean": 307}},
        {"protocol": {"version": "bench-v3", "procedure": "steady-state", "concurrency": 8, "warmup_requests": 20}},
    ],
    ids=["model", "workload", "protocol"],
)
def test_incomparable_contexts_fail_closed(override):
    base = make_parsed()
    cand = make_parsed(**override)
    result = compare_summaries(base, cand, _cfg())
    assert result.verdict == "INCONCLUSIVE"
    expected = {
        "model": INCOMPARABLE_MODEL,
        "workload": INCOMPARABLE_WORKLOAD,
        "protocol": INCOMPARABLE_PROTOCOL,
    }
    key = next(iter(override))
    assert result.reason_codes == (expected[key],)


def test_all_three_mismatched_reports_all_three():
    base = make_parsed()
    cand = make_parsed(
        **{
            "model": {"id": "m2", "quantization": "int4", "architecture": "dense"},
            "workload": {"id": "w2", "requests": 10, "input_tokens_mean": 1, "output_tokens_mean": 1},
            "protocol": {"version": "p2", "procedure": "burst", "concurrency": 1, "warmup_requests": 0},
        }
    )
    result = compare_summaries(base, cand, _cfg())
    assert result.verdict == "INCONCLUSIVE"
    assert set(result.reason_codes) == {INCOMPARABLE_MODEL, INCOMPARABLE_WORKLOAD, INCOMPARABLE_PROTOCOL}
    assert len(result.reason_codes) == 3


def test_comparable_even_with_different_measurement_values():
    base = make_parsed()
    cand = make_parsed(**{"measurements": {"ttft_s": 0.99, "throughput_rps": 1.0}})
    result = compare_summaries(base, cand, _cfg())
    assert result.verdict in ("PROMOTE", "REJECT", "INCONCLUSIVE")
    # A value difference alone must never make the pair incomparable.
    assert INCOMPARABLE_MODEL not in result.reason_codes
    assert INCOMPARABLE_WORKLOAD not in result.reason_codes
    assert INCOMPARABLE_PROTOCOL not in result.reason_codes


# ---------------------------------------------------------------------------
# direction-aware deltas — hand-computed golden vectors
# ---------------------------------------------------------------------------


def _delta_of(result, metric: str) -> dict:
    entry = next(d for d in result.deltas if d.metric == metric)
    return {
        "metric": entry.metric,
        "direction": entry.direction,
        "baseline": entry.baseline,
        "candidate": entry.candidate,
        "delta": entry.delta,
        "relative_delta": entry.relative_delta,
        "improvement": entry.improvement,
    }


def test_golden_lower_better_delta():
    # ttft: base 1.00, cand 0.80 -> delta -0.20, relative -0.20, improvement +0.20
    base = make_parsed(**{"measurements": {"ttft_s": 1.00}})
    cand = make_parsed(**{"measurements": {"ttft_s": 0.80}})
    d = _delta_of(compare_summaries(base, cand, _cfg()), "ttft_s")
    assert d["direction"] == "lower_better"
    assert d["baseline"] == 1.0
    assert d["candidate"] == 0.8
    assert math.isclose(d["delta"], -0.2, rel_tol=0, abs_tol=1e-12)
    assert math.isclose(d["relative_delta"], -0.2, rel_tol=0, abs_tol=1e-12)
    assert math.isclose(d["improvement"], 0.2, rel_tol=0, abs_tol=1e-12)


def test_golden_higher_better_delta():
    # throughput: base 10.0, cand 13.5 -> delta +3.5, relative +0.35, improvement +0.35
    cfg = _cfg(gates=(MetricGate(metric="throughput_rps", min_relative_improvement=0.0),))
    base = make_parsed(**{"measurements": {"throughput_rps": 10.0}})
    cand = make_parsed(**{"measurements": {"throughput_rps": 13.5}})
    d = _delta_of(compare_summaries(base, cand, cfg), "throughput_rps")
    assert d["direction"] == "higher_better"
    assert math.isclose(d["delta"], 3.5, rel_tol=0, abs_tol=1e-12)
    assert math.isclose(d["relative_delta"], 0.35, rel_tol=0, abs_tol=1e-12)
    assert math.isclose(d["improvement"], 0.35, rel_tol=0, abs_tol=1e-12)


def test_golden_regressing_higher_better_is_negative_improvement():
    # success_rate: base 0.99, cand 0.95 -> improvement = -0.0404...
    cfg = _cfg(gates=(MetricGate(metric="success_rate", min_relative_improvement=0.0),))
    base = make_parsed(**{"measurements": {"success_rate": 0.99}})
    cand = make_parsed(**{"measurements": {"success_rate": 0.95}})
    d = _delta_of(compare_summaries(base, cand, cfg), "success_rate")
    expected_improvement = (0.95 - 0.99) / 0.99
    assert math.isclose(d["improvement"], expected_improvement, rel_tol=0, abs_tol=1e-12)
    assert d["improvement"] < 0


def test_golden_negative_lower_better_regression():
    # e2e_latency_s: base 2.0, cand 3.0 -> delta +1.0, improvement -0.5
    cfg = _cfg(gates=(MetricGate(metric="e2e_latency_s", min_relative_improvement=0.0),))
    base = make_parsed(**{"measurements": {"e2e_latency_s": 2.0}})
    cand = make_parsed(**{"measurements": {"e2e_latency_s": 3.0}})
    d = _delta_of(compare_summaries(base, cand, cfg), "e2e_latency_s")
    assert math.isclose(d["delta"], 1.0, rel_tol=0, abs_tol=1e-12)
    assert math.isclose(d["improvement"], -0.5, rel_tol=0, abs_tol=1e-12)


# ---------------------------------------------------------------------------
# zero baseline
# ---------------------------------------------------------------------------


def test_zero_baseline_with_relative_gate_is_inconclusive():
    cfg = _cfg(gates=(MetricGate(metric="ttft_s", min_relative_improvement=0.05),))
    base = make_parsed(**{"measurements": {"ttft_s": 0.0}})
    cand = make_parsed(**{"measurements": {"ttft_s": 0.1}})
    result = compare_summaries(base, cand, cfg)
    assert result.verdict == "INCONCLUSIVE"
    assert result.reason_codes == (ZERO_BASELINE,)
    d = _delta_of(result, "ttft_s")
    assert d["relative_delta"] is None
    assert d["improvement"] is None
    assert math.isclose(d["delta"], 0.1, rel_tol=0, abs_tol=1e-12)


def test_zero_baseline_with_only_absolute_gate_decides():
    cfg = _cfg(gates=(MetricGate(metric="ttft_s", min_absolute_improvement=0.05),))
    base = make_parsed(**{"measurements": {"ttft_s": 0.0}})
    cand = make_parsed(**{"measurements": {"ttft_s": 0.01}})
    result = compare_summaries(base, cand, cfg)
    # lower_better: 0.00 -> 0.01 is a 0.01 absolute *regression* < 0.05 improvement
    assert result.verdict == "REJECT"
    assert result.reason_codes == (ABS_GATE_FAILED,)


def test_zero_baseline_equal_values_fail_absolute_gate():
    cfg = _cfg(gates=(MetricGate(metric="ttft_s", min_absolute_improvement=0.05),))
    # equal values: absolute improvement 0.0 < 0.05 -> reject.
    base2 = make_parsed(**{"measurements": {"ttft_s": 0.0}})
    cand2 = make_parsed(**{"measurements": {"ttft_s": 0.0}})
    result = compare_summaries(base2, cand2, cfg)
    assert result.verdict == "REJECT"
    assert result.reason_codes == (ABS_GATE_FAILED,)


# ---------------------------------------------------------------------------
# missing / UNMEASURABLE metrics
# ---------------------------------------------------------------------------


def test_missing_gated_metric_is_inconclusive():
    cfg = _cfg(gates=(MetricGate(metric="ttft_s", min_relative_improvement=0.05),))
    base = make_parsed(measurements={"ttft_s": UNMEASURABLE})
    cand = make_parsed()
    result = compare_summaries(base, cand, cfg)
    assert result.verdict == "INCONCLUSIVE"
    assert result.reason_codes == (METRIC_UNMEASURABLE,)


def test_unmeasurable_gated_metric_is_inconclusive():
    cfg = _cfg(gates=(MetricGate(metric="ttft_s", min_relative_improvement=0.05),))
    base = make_parsed()
    cand = make_parsed(**{"measurements": {"ttft_s": UNMEASURABLE}})
    result = compare_summaries(base, cand, cfg)
    assert result.verdict == "INCONCLUSIVE"
    assert result.reason_codes == (METRIC_UNMEASURABLE,)


def test_unmeasurable_on_baseline_side_also_inconclusive():
    cfg = _cfg(gates=(MetricGate(metric="throughput_rps", min_relative_improvement=0.0),))
    base = make_parsed(**{"measurements": {"throughput_rps": UNMEASURABLE}})
    cand = make_parsed()
    result = compare_summaries(base, cand, cfg)
    assert result.verdict == "INCONCLUSIVE"
    assert result.reason_codes == (METRIC_UNMEASURABLE,)


# ---------------------------------------------------------------------------
# gates: absolute, relative, multiple failures, quality hard gate
# ---------------------------------------------------------------------------


def test_relative_gate_reject_when_improvement_below_threshold():
    cfg = _cfg(gates=(MetricGate(metric="ttft_s", min_relative_improvement=0.05),))
    base = make_parsed(**{"measurements": {"ttft_s": 1.0}})
    cand = make_parsed(**{"measurements": {"ttft_s": 0.96}})  # 4% < 5%
    result = compare_summaries(base, cand, cfg)
    assert result.verdict == "REJECT"
    assert result.reason_codes == (REL_GATE_FAILED,)


def test_relative_gate_promote_at_exactly_threshold():
    # 1.0 -> 0.75 is *exactly* a 25% improvement in binary float (0.25 is
    # exactly representable), so the gate passes at the threshold boundary.
    cfg = _cfg(gates=(MetricGate(metric="ttft_s", min_relative_improvement=0.25),))
    base = make_parsed(**{"measurements": {"ttft_s": 1.0}})
    cand = make_parsed(**{"measurements": {"ttft_s": 0.75}})
    result = compare_summaries(base, cand, cfg)
    assert result.verdict == "PROMOTE"
    assert result.reason_codes == (ALL_GATES_PASSED,)


def test_absolute_gate_reject():
    cfg = _cfg(gates=(MetricGate(metric="ttft_s", min_absolute_improvement=0.1),))
    base = make_parsed(**{"measurements": {"ttft_s": 1.0}})
    cand = make_parsed(**{"measurements": {"ttft_s": 0.95}})  # 0.05 < 0.1 absolute
    result = compare_summaries(base, cand, cfg)
    assert result.verdict == "REJECT"
    assert result.reason_codes == (ABS_GATE_FAILED,)


def test_both_gates_on_one_metric_both_must_pass():
    cfg = _cfg(
        gates=(
            MetricGate(metric="ttft_s", min_relative_improvement=0.2, min_absolute_improvement=0.1),
        )
    )
    base = make_parsed(**{"measurements": {"ttft_s": 1.0}})
    cand = make_parsed(**{"measurements": {"ttft_s": 0.85}})  # 15% rel, 0.15 abs
    result = compare_summaries(base, cand, cfg)
    assert result.verdict == "REJECT"
    assert result.reason_codes == (REL_GATE_FAILED,)  # absolute passes, relative fails


def test_multiple_failing_gates_report_all_codes_sorted():
    cfg = _cfg(
        gates=(
            MetricGate(metric="ttft_s", min_relative_improvement=0.5),
            MetricGate(metric="throughput_rps", min_absolute_improvement=10.0),
        )
    )
    base = make_parsed(**{"measurements": {"ttft_s": 1.0}})
    cand = make_parsed(**{"measurements": {"ttft_s": 0.9, "throughput_rps": 5.0}})
    result = compare_summaries(base, cand, cfg)
    assert result.verdict == "REJECT"
    assert list(result.reason_codes) == sorted([REL_GATE_FAILED, ABS_GATE_FAILED])


def test_quality_hard_gate_rejects_any_regression():
    cfg = _cfg(
        gates=(MetricGate(metric="ttft_s", min_relative_improvement=0.05),),
        quality_gate=MetricGate(metric="quality_score", min_relative_improvement=0.0),
    )
    base = make_parsed(**{"measurements": {"ttft_s": 0.3, "quality_score": 0.80}})
    cand = make_parsed(**{"measurements": {"ttft_s": 0.2, "quality_score": 0.79}})
    result = compare_summaries(base, cand, cfg)
    assert result.verdict == "REJECT"
    assert QUALITY_GATE_FAILED in result.reason_codes


def test_quality_hard_gate_promotes_when_quality_holds():
    cfg = _cfg(
        gates=(MetricGate(metric="ttft_s", min_relative_improvement=0.05),),
        quality_gate=MetricGate(metric="quality_score", min_relative_improvement=0.0),
    )
    base = make_parsed(**{"measurements": {"ttft_s": 0.3, "quality_score": 0.80}})
    cand = make_parsed(**{"measurements": {"ttft_s": 0.2, "quality_score": 0.81}})
    result = compare_summaries(base, cand, cfg)
    assert result.verdict == "PROMOTE"
    assert result.reason_codes == (ALL_GATES_PASSED,)


def test_quality_hard_gate_rejects_before_performance_gates():
    # Massive performance win must not outrank a quality regression.
    cfg = _cfg(
        gates=(MetricGate(metric="ttft_s", min_relative_improvement=0.05),),
        quality_gate=MetricGate(metric="quality_score", min_relative_improvement=0.0),
    )
    base = make_parsed(**{"measurements": {"ttft_s": 1.0, "quality_score": 0.9}})
    cand = make_parsed(**{"measurements": {"ttft_s": 0.05, "quality_score": 0.89}})
    result = compare_summaries(base, cand, cfg)
    assert result.verdict == "REJECT"
    assert result.reason_codes == (QUALITY_GATE_FAILED,)


def test_quality_hard_gate_unmeasurable_is_inconclusive():
    cfg = _cfg(
        quality_gate=MetricGate(metric="quality_score", min_relative_improvement=0.0),
        gates=(),
    )
    base = make_parsed(**{"measurements": {"quality_score": UNMEASURABLE}})
    cand = make_parsed()
    result = compare_summaries(base, cand, cfg)
    assert result.verdict == "INCONCLUSIVE"
    assert result.reason_codes == (METRIC_UNMEASURABLE,)


def test_quality_hard_gate_zero_baseline_quality_inconclusive():
    cfg = _cfg(
        quality_gate=MetricGate(metric="quality_score", min_relative_improvement=0.0),
        gates=(),
    )
    base = make_parsed(**{"measurements": {"quality_score": 0.0}})
    cand = make_parsed()
    result = compare_summaries(base, cand, cfg)
    assert result.verdict == "INCONCLUSIVE"
    assert result.reason_codes == (ZERO_BASELINE,)


def test_no_gates_at_all_is_promote_with_no_gate_evidence():
    base = make_parsed()
    cand = make_parsed()
    result = compare_summaries(base, cand, _cfg(gates=(), quality_gate=None))
    assert result.verdict == "PROMOTE"
    assert result.reason_codes == (ALL_GATES_PASSED,)
    assert result.deltas == ()


# ---------------------------------------------------------------------------
# canonical artifact: digest + provenance id + verification
# ---------------------------------------------------------------------------


def test_artifact_roundtrip_and_provenance():
    base = make_parsed()
    cand = make_parsed(**{"measurements": {"ttft_s": 0.25}})
    cfg = _cfg()
    result = compare_summaries(base, cand, cfg)
    art = build_compare_artifact(result, base, cand, cfg, created_at="2026-08-19T00:00:00+00:00")
    assert art["schema_version"] == COMPARE_SCHEMA_VERSION
    assert art["claim_boundary"] == cfg.claim_boundary
    assert art["baseline"]["sha256"] == base.digest
    assert art["candidate"]["sha256"] == cand.digest
    assert art["provenance_id"].startswith("prov:")
    # Deterministic provenance across builds, stable to created_at.
    art2 = build_compare_artifact(result, base, cand, cfg, created_at="2026-12-01T00:00:00+00:00")
    assert art2["provenance_id"] == art["provenance_id"]
    assert art2["artifact_digest"] == art["artifact_digest"]
    assert verify_compare_artifact(art)["valid"] is True


def test_artifact_digest_covers_verdict_and_deltas():
    base = make_parsed()
    cand = make_parsed(**{"measurements": {"ttft_s": 0.25}})
    cfg = _cfg()
    result = compare_summaries(base, cand, cfg)
    art = build_compare_artifact(result, base, cand, cfg, created_at="2026-08-19T00:00:00+00:00")
    tampered = dict(art)
    tampered["verdict"] = "REJECT"
    with pytest.raises(ArtifactIntegrityError):
        verify_compare_artifact(tampered)
    tampered2 = dict(art)
    first = dict(art["deltas"][0])
    first["baseline"] = 99.0
    tampered2["deltas"] = [first]
    with pytest.raises(ArtifactIntegrityError):
        verify_compare_artifact(tampered2)


def test_artifact_rejects_wrong_schema_version():
    base = make_parsed()
    cand = make_parsed()
    cfg = _cfg()
    result = compare_summaries(base, cand, cfg)
    art = build_compare_artifact(result, base, cand, cfg, created_at="t")
    bad = dict(art, schema_version="serving-verdict.compare.v9")
    with pytest.raises(ArtifactIntegrityError):
        verify_compare_artifact(bad)


def test_parse_then_compare_full_path():
    base_doc = make_summary()
    cand_doc = make_summary(**{"measurements": {"ttft_s": 0.20}})
    result = compare_summaries(
        parse_summary_payload(base_doc), parse_summary_payload(cand_doc), _cfg()
    )
    assert result.verdict == "PROMOTE"
