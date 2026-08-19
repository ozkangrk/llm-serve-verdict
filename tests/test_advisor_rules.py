"""Rules engine tests for the Config Advisor (deterministic, evidence-linked)."""
from __future__ import annotations

import math
from typing import Any

import pytest
from helpers_advisor import empty_benchmark, raw_advisor_doc

from serving_verdict.advisor import AdvisorError, advise

RULE_IDS = {
    "OOM_RISK",
    "TTFT_HIGH",
    "THROUGHPUT_LOW",
    "KV_PRESSURE",
    "CONCURRENCY_HEADROOM",
    "REQUEST_FAILURES",
    "TOOL_QUALITY_LOW",
}


def _doc(**overrides: Any) -> dict[str, Any]:
    base = raw_advisor_doc()
    for key, value in overrides.items():
        if isinstance(value, dict):
            base[key] = {**base.get(key, {}), **value}
        else:
            base[key] = value
    return base


def _rules(result: Any) -> list[dict[str, Any]]:
    return [vars(r) if not isinstance(r, dict) else r for r in result.recommendations]


def test_throughput_low_fires():
    doc = _doc(benchmark={"decode_tokens_per_s": 20.0})
    result = advise(doc)
    ids = [r.rule_id for r in result.recommendations]
    assert "THROUGHPUT_LOW" in ids
    reco = next(r for r in result.recommendations if r.rule_id == "THROUGHPUT_LOW")
    assert reco.evidence_metric == "decode_tokens_per_s"
    assert reco.expected_direction == "increase"
    assert "20.0" in reco.evidence_value
    assert reco.risk in {"low", "medium", "high"}
    assert not math.isnan(reco.rank) if isinstance(reco.rank, float) else True


def test_ttft_high_fires():
    doc = _doc(benchmark={"ttft_s": 2.5})
    result = advise(doc)
    reco = next((r for r in result.recommendations if r.rule_id == "TTFT_HIGH"), None)
    assert reco is not None
    assert reco.evidence_metric == "ttft_s"
    assert "2.5" in reco.evidence_value
    assert reco.expected_direction in {"increase", "decrease", "stabilize"}


def test_kv_pressure_fires():
    doc = _doc(capacity={"kv_cache_usage": 0.9})
    result = advise(doc)
    reco = next((r for r in result.recommendations if r.rule_id == "KV_PRESSURE"), None)
    assert reco is not None
    assert reco.evidence_metric == "kv_cache_usage"


def test_oom_risk_fires_and_suppresses_throughput_push():
    doc = _doc(benchmark={"decode_tokens_per_s": 20.0}, capacity={"memory_status": "oom"})
    result = advise(doc)
    ids = [r.rule_id for r in result.recommendations]
    assert "OOM_RISK" in ids
    # When memory is in OOM state we must not recommend pushing batch capacity.
    assert "THROUGHPUT_LOW" not in ids
    reco = next(r for r in result.recommendations if r.rule_id == "OOM_RISK")
    assert reco.evidence_metric == "memory_status"
    assert reco.expected_direction == "stabilize"


def test_tool_quality_low_fires():
    doc = _doc(benchmark={"tool_call_success_rate": 0.8})
    result = advise(doc)
    reco = next((r for r in result.recommendations if r.rule_id == "TOOL_QUALITY_LOW"), None)
    assert reco is not None
    assert reco.evidence_metric == "tool_call_success_rate"


def test_request_failure_rate_fires():
    doc = _doc(benchmark={"request_failure_rate": 0.08})
    result = advise(doc)
    reco = next((r for r in result.recommendations if r.rule_id == "REQUEST_FAILURES"), None)
    assert reco is not None
    assert reco.evidence_metric == "request_failure_rate"


def test_concurrency_headroom_fires():
    doc = _doc(
        benchmark={"max_concurrency": 4},
        capacity={"concurrency_target": 16},
    )
    result = advise(doc)
    reco = next((r for r in result.recommendations if r.rule_id == "CONCURRENCY_HEADROOM"), None)
    assert reco is not None
    assert reco.evidence_metric == "max_concurrency"


def test_healthy_baseline_emits_no_recommendations():
    doc = _doc(
        current_flags={"allowed_max_tokens": 32768},
        benchmark={
            "ttft_s": 0.2,
            "decode_tokens_per_s": 120.0,
            "e2e_tokens_per_s": 118.0,
            "max_concurrency": 16,
            "request_failure_rate": 0.0,
            "tool_call_success_rate": 0.99,
        },
        capacity={"kv_cache_usage": 0.4, "concurrency_target": 16},
    )
    result = advise(doc)
    assert result.recommendations == ()
    assert result.status == "OK"


def test_no_evidence_is_inconclusive_with_no_recommendations():
    doc = raw_advisor_doc(
        benchmark=empty_benchmark(),
        capacity={
            "memory_status": None,
            "kv_cache_usage": None,
            "context_len": None,
            "concurrency_target": None,
        },
    )
    result = advise(doc)
    assert result.status == "INCONCLUSIVE"
    assert result.recommendations == ()
    assert result.recipe is None
    assert result.digest.startswith("sha256:")


def test_deterministic_rule_ordering():
    doc = _doc(
        benchmark={
            "ttft_s": 2.5,
            "decode_tokens_per_s": 20.0,
            "request_failure_rate": 0.08,
        },
        capacity={"memory_status": "oom", "kv_cache_usage": 0.9},
    )
    r1 = advise(doc)
    r2 = advise(doc)
    ids1 = [r.rule_id for r in r1.recommendations]
    ids2 = [r.rule_id for r in r2.recommendations]
    assert ids1 == ids2
    assert len(ids1) == len(set(ids1))
    ranks = [r.rank for r in r1.recommendations]
    assert ranks == sorted(ranks)
    # Fixed priority: OOM first, then TTFT, KV (THROUGHPUT suppressed by OOM).
    assert ids1[0] == "OOM_RISK"
    assert ids1.index("TTFT_HIGH") < ids1.index("KV_PRESSURE")
    assert ids1.index("KV_PRESSURE") < ids1.index("REQUEST_FAILURES")
    assert "THROUGHPUT_LOW" not in ids1


def test_throughput_rules_ordered_before_kv_when_no_oom():
    doc = _doc(
        benchmark={"decode_tokens_per_s": 20.0},
        capacity={"kv_cache_usage": 0.9},
    )
    ids = [r.rule_id for r in advise(doc).recommendations]
    assert ids.index("THROUGHPUT_LOW") < ids.index("KV_PRESSURE")


def test_digest_is_deterministic_and_evidence_sensitive():
    doc = _doc(benchmark={"decode_tokens_per_s": 20.0})
    a = advise(doc)
    b = advise(doc)
    assert a.digest == b.digest
    c = advise(_doc(benchmark={"decode_tokens_per_s": 21.0}))
    assert c.digest != a.digest


def test_recommendations_carry_full_contract():
    doc = _doc(
        current_flags={"max_num_batched_tokens": 8192},
        benchmark={"ttft_s": 2.5, "decode_tokens_per_s": 20.0, "tool_call_success_rate": 0.8},
        capacity={"kv_cache_usage": 0.9},
    )
    result = advise(doc)
    assert result.recommendations
    for r in result.recommendations:
        assert r.rule_id in RULE_IDS | {"UNSAFE_FLAGS"}
        assert r.title.strip()
        assert r.reason.strip()
        assert r.evidence_metric.strip()
        assert r.expected_direction in {"increase", "decrease", "stabilize"}
        assert r.risk in {"low", "medium", "high"}
        assert r.confidence_boundary.strip()
        plan = r.experiment
        assert plan.variable.strip()
        assert plan.to_value.strip()
        assert plan.hold_fixed
        assert plan.success_metric.strip()
        assert plan.abort_condition.strip()
        rb = r.rollback
        assert rb.command_label.strip()
        assert rb.argv
        # Never claim guaranteed gains.
        combined = f"{r.reason} {r.title} {r.confidence_boundary}".lower()
        assert "guaranteed" not in combined
        assert "guarantee" not in combined


def test_invalid_evidence_values_fail_closed():
    empty = empty_benchmark()
    cap_empty = {
        "memory_status": None,
        "kv_cache_usage": None,
        "context_len": None,
        "concurrency_target": None,
    }

    def doc_with(**overrides: Any) -> dict[str, Any]:
        doc = raw_advisor_doc()
        for key, value in overrides.items():
            doc[key] = value
        return doc

    def bench(**vals: Any) -> dict[str, Any]:
        b = dict(empty)
        b.update(vals)
        return b

    bad_docs = [
        doc_with(benchmark=bench(ttft_s=-1.0)),
        doc_with(benchmark=bench(decode_tokens_per_s=0.0)),
        doc_with(benchmark=bench(request_failure_rate=1.5)),
        doc_with(benchmark=bench(ttft_s=float("nan"))),
        doc_with(capacity={**cap_empty, "memory_status": "maybe"}),
        doc_with(capacity={**cap_empty, "kv_cache_usage": 1.7}),
        doc_with(capacity={**cap_empty, "concurrency_target": 0}),
        doc_with(runtime_family="torchserve"),
        doc_with(extra_field="surprise"),
    ]
    for doc in bad_docs:
        with pytest.raises(AdvisorError):
            advise(doc)
