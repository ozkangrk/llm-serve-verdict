"""Pareto frontier over TTFT/decode/concurrency/memory/quality.

Objective vector per trial (all derived from the summary measurements):
  - ttft_s          lower_better
  - decode_latency_ms lower_better
  - concurrency_max higher_better
  - peak_memory_gb  lower_better
  - quality_score   higher_better

Rules under test:
  - constraints filter infeasible trials FIRST (before frontier math);
  - domination is strict on at least one objective and >= on all;
  - deterministic tie handling (lexicographic on trial_id);
  - when the frontier holds multiple incomparable trials the result is an
    honest NO_SINGLE_WINNER, never a fabricated single winner.
"""
from __future__ import annotations

from typing import Any

import pytest

from serving_verdict.errors import ArtifactIntegrityError, PlanError
from serving_verdict.pareto import (
    NO_FEASIBLE_TRIALS,
    NO_SINGLE_WINNER,
    OBJECTIVE_METRICS,
    PARETO_SCHEMA_VERSION,
    SINGLE_WINNER,
    Trial,
    build_pareto_artifact,
    compute_pareto_frontier,
    verify_pareto_artifact,
)
from serving_verdict.summary import UNMEASURABLE, parse_summary_payload
from tests.helpers_v04 import make_summary, seal


def _trial(
    trial_id: str,
    ttft: float = 0.30,
    decode: float = 12.0,
    concurrency: float = 8.0,
    memory: float = 9.0,
    quality: float = 0.95,
    params: dict[str, Any] | None = None,
) -> Trial:
    return Trial(
        trial_id=trial_id,
        params=params or {},
        objectives={
            "ttft_s": ttft,
            "decode_latency_ms": decode,
            "concurrency_max": concurrency,
            "peak_memory_gb": memory,
            "quality_score": quality,
        },
    )


# ---------------------------------------------------------------------------
# objective extraction
# ---------------------------------------------------------------------------


def test_objective_metrics_are_fixed():
    assert set(OBJECTIVE_METRICS) == {
        "ttft_s",
        "decode_latency_ms",
        "concurrency_max",
        "peak_memory_gb",
        "quality_score",
    }


def _summary_for(
    ttft: float = 0.3,
    decode: float = 12.0,
    conc: float = 8.0,
    mem: float = 9.0,
    qual: float = 0.95,
) -> dict[str, Any]:
    return make_summary(
        **{
            "measurements": {
                "ttft_s": ttft,
                "decode_latency_ms": decode,
                "concurrency_max": conc,
                "peak_memory_gb": mem,
                "quality_score": qual,
            }
        }
    )


def _trial_from_doc(trial_id: str, ttft: float = 0.3, decode: float = 12.0, conc: float = 8.0, mem: float = 9.0, qual: float = 0.95) -> Trial:
    doc = _summary_for(ttft=ttft, decode=decode, conc=conc, mem=mem, qual=qual)
    s = parse_summary_payload(doc)
    return Trial(
        trial_id=trial_id,
        params={"id": trial_id},
        objectives={m: s.measurements[m] for m in OBJECTIVE_METRICS},
    )


# ---------------------------------------------------------------------------
# constraint filtering happens first
# ---------------------------------------------------------------------------


def test_constraints_filter_infeasible_before_frontier():
    # t-infeasible dominates everything numerically but violates the
    # memory constraint; it must be excluded before frontier computation.
    trials = [
        _trial("t-infeasible", ttft=0.05, decode=5.0, concurrency=64.0, memory=100.0, quality=1.0),
        _trial("t-good", ttft=0.3, decode=12.0, concurrency=8.0, memory=9.0, quality=0.95),
    ]
    result = compute_pareto_frontier(
        trials=trials,
        constraints={"peak_memory_gb": (None, 32.0)},
        seed=1,
    )
    assert "t-infeasible" not in [t.trial_id for t in result.frontier]
    assert "t-infeasible" in [t.trial_id for t in result.infeasible]
    assert [t.trial_id for t in result.frontier] == ["t-good"]


def test_constraint_range_excludes_below_min():
    trials = [_trial("low", concurrency=2.0), _trial("ok", concurrency=8.0)]
    result = compute_pareto_frontier(
        trials=trials, constraints={"concurrency_max": (4.0, None)}, seed=1
    )
    assert [t.trial_id for t in result.frontier] == ["ok"]
    assert [t.trial_id for t in result.infeasible] == ["low"]


def test_unknown_constrained_metric_rejected():
    with pytest.raises(PlanError, match="constraint"):
        compute_pareto_frontier(
            trials=[_trial("a")], constraints={"bogus_metric": (0.0, 1.0)}, seed=1
        )


def test_bad_constraint_bounds_rejected():
    for bad in ((5.0, 1.0), (None, None), ("lo", None)):
        with pytest.raises(PlanError, match="constraint"):
            compute_pareto_frontier(
                trials=[_trial("a")], constraints={"ttft_s": bad}, seed=1
            )


def test_no_feasible_trials_is_honest():
    trials = [_trial("a", memory=50.0), _trial("b", memory=60.0)]
    result = compute_pareto_frontier(
        trials=trials, constraints={"peak_memory_gb": (None, 32.0)}, seed=1
    )
    assert result.result == NO_FEASIBLE_TRIALS
    assert result.frontier == ()


def test_unmeasurable_objective_makes_trial_infeasible():
    doc = make_summary()
    doc["measurements"]["ttft_s"] = UNMEASURABLE
    seal(doc)
    s = parse_summary_payload(doc)
    trial = Trial(
        trial_id="u",
        params={},
        objectives={m: (UNMEASURABLE if m == "ttft_s" else s.measurements[m]) for m in OBJECTIVE_METRICS},
    )
    result = compute_pareto_frontier(trials=[trial], constraints={}, seed=1)
    assert result.result == NO_FEASIBLE_TRIALS
    assert [t.trial_id for t in result.infeasible] == ["u"]


# ---------------------------------------------------------------------------
# domination + frontier shape
# ---------------------------------------------------------------------------


def test_strict_dominator_is_excluded():
    # d beats b on every objective strictly.
    trials = [
        _trial("b", ttft=0.5, decode=20.0, concurrency=4.0, memory=16.0, quality=0.90),
        _trial("d", ttft=0.4, decode=15.0, concurrency=8.0, memory=12.0, quality=0.95),
    ]
    result = compute_pareto_frontier(trials=trials, constraints={}, seed=1)
    assert result.result == SINGLE_WINNER
    assert [t.trial_id for t in result.frontier] == ["d"]
    assert [t.trial_id for t in result.dominated] == ["b"]


def test_equal_trials_are_tied_not_dominating():
    a = _trial("a", ttft=0.3, decode=12.0, concurrency=8.0, memory=9.0, quality=0.95)
    b = _trial("b", ttft=0.3, decode=12.0, concurrency=8.0, memory=9.0, quality=0.95)
    result = compute_pareto_frontier(trials=[a, b], constraints={}, seed=1)
    # Neither dominates the other (no strict improvement anywhere): both
    # survive as tied frontier members, deterministic by trial_id order.
    assert result.result == NO_SINGLE_WINNER
    assert [t.trial_id for t in result.frontier] == ["a", "b"]


def test_partial_dominance_leaves_both_on_frontier():
    # fast but low quality vs slow but high quality: incomparable.
    trials = [
        _trial("fast", ttft=0.1, decode=8.0, concurrency=16.0, memory=6.0, quality=0.80),
        _trial("quality", ttft=0.6, decode=25.0, concurrency=4.0, memory=18.0, quality=0.99),
    ]
    result = compute_pareto_frontier(trials=trials, constraints={}, seed=1)
    assert result.result == NO_SINGLE_WINNER
    assert [t.trial_id for t in result.frontier] == ["fast", "quality"]


def test_frontier_order_is_deterministic_by_trial_id():
    # All three trials are mutually incomparable (each is best on at least
    # one objective the others are worse on: z on raw speed, m on decode
    # rate, a on quality+memory). The frontier must list them in
    # deterministic trial_id order regardless of input order or seed.
    trials = [
        _trial("z", ttft=0.10, decode=8.0, concurrency=16.0, memory=6.0, quality=0.60),
        _trial("m", ttft=0.20, decode=10.0, concurrency=8.0, memory=12.0, quality=0.80),
        _trial("a", ttft=0.30, decode=12.0, concurrency=8.0, memory=9.0, quality=0.90),
    ]
    r1 = compute_pareto_frontier(trials=trials, constraints={}, seed=1)
    r2 = compute_pareto_frontier(trials=list(reversed(trials)), constraints={}, seed=99)
    assert [t.trial_id for t in r1.frontier] == ["a", "m", "z"]
    assert [t.trial_id for t in r2.frontier] == ["a", "m", "z"]


def test_duplicate_trial_ids_rejected():
    a = _trial("same")
    with pytest.raises(PlanError, match="duplicate"):
        compute_pareto_frontier(trials=[a, _trial("same")], constraints={}, seed=1)


def test_single_trial_is_single_winner():
    result = compute_pareto_frontier(trials=[_trial("only")], constraints={}, seed=1)
    assert result.result == SINGLE_WINNER
    assert [t.trial_id for t in result.frontier] == ["only"]


# ---------------------------------------------------------------------------
# golden vector from real summary documents
# ---------------------------------------------------------------------------


def test_frontier_from_summary_documents_golden():
    fast = _trial_from_doc("t-fast", ttft=0.10, decode=9.0, conc=16.0, mem=7.0, qual=0.99)
    slow = _trial_from_doc("t-slow", ttft=0.50, decode=20.0, conc=4.0, mem=15.0, qual=0.99)
    mem = _trial_from_doc("t-mem", ttft=0.20, decode=11.0, conc=12.0, mem=3.0, qual=0.93)
    result = compute_pareto_frontier(trials=[fast, slow, mem], constraints={}, seed=1)
    # t-fast dominates t-slow (all objectives). t-mem is incomparable with
    # t-fast (lower memory + concurrency, worse quality + ttft).
    assert [t.trial_id for t in result.frontier] == ["t-fast", "t-mem"]
    assert [t.trial_id for t in result.dominated] == ["t-slow"]
    assert result.result == NO_SINGLE_WINNER


# ---------------------------------------------------------------------------
# artifact + tamper
# ---------------------------------------------------------------------------


def test_pareto_artifact_roundtrip_and_tamper():
    trials = [
        _trial("fast", ttft=0.1, decode=8.0, concurrency=16.0, memory=6.0, quality=0.99),
        _trial("quality", ttft=0.6, decode=4.0, concurrency=4.0, memory=18.0, quality=0.995),
    ]
    result = compute_pareto_frontier(trials=trials, constraints={"peak_memory_gb": (None, 40.0)}, seed=5)
    art = build_pareto_artifact(result, created_at="2026-08-19T00:00:00+00:00")
    assert art["schema_version"] == PARETO_SCHEMA_VERSION
    assert art["result"] == NO_SINGLE_WINNER
    assert art["provenance_id"].startswith("prov:")
    art2 = build_pareto_artifact(result, created_at="2028-01-01T00:00:00+00:00")
    assert art2["provenance_id"] == art["provenance_id"]
    assert verify_pareto_artifact(art)["valid"] is True

    bad = dict(art)
    bad["result"] = SINGLE_WINNER
    with pytest.raises(ArtifactIntegrityError):
        verify_pareto_artifact(bad)
