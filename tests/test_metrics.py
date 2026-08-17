"""Metric semantic registry tests: fixed semantics + comparability (RED)."""
from __future__ import annotations

import pytest

from serving_verdict.metrics import (
    MetricDefinition,
    MetricDimensions,
    MetricSample,
    comparable,
)
from serving_verdict.metrics import (
    registry as default_registry,
)


def test_registry_contains_all_five_metrics() -> None:
    ids = {m.metric_id for m in default_registry.values()}
    assert ids == {
        "decode_tokens_per_s",
        "e2e_output_tokens_per_s",
        "aggregate_output_tokens_per_s",
        "ttft_s",
        "api_latency_s",
    }


def test_directions() -> None:
    d = default_registry
    assert d["decode_tokens_per_s"].direction == "higher_better"
    assert d["e2e_output_tokens_per_s"].direction == "higher_better"
    assert d["aggregate_output_tokens_per_s"].direction == "higher_better"
    assert d["ttft_s"].direction == "lower_better"
    assert d["api_latency_s"].direction == "lower_better"


def test_registry_is_immutable() -> None:
    with pytest.raises(TypeError):
        default_registry["decode_tokens_per_s"] = MetricDefinition(
            metric_id="decode_tokens_per_s",
            unit="tok/s",
            direction="lower_better",
            procedure_version="v9",
            aggregation="bogus",
        )


def test_comparable_when_all_dimensions_match() -> None:
    a = MetricSample(
        metric_id="decode_tokens_per_s",
        value=30.0,
        dimensions=MetricDimensions(
            unit="tok/s",
            procedure_version="v1",
            workload_id="edit_cold",
            concurrency=1,
            output_budget=1200,
            thinking_mode="disabled",
            warm_cold="cold",
            aggregation="median",
        ),
    )
    b = MetricSample(
        metric_id="decode_tokens_per_s",
        value=60.0,
        dimensions=MetricDimensions(
            unit="tok/s",
            procedure_version="v1",
            workload_id="edit_cold",
            concurrency=1,
            output_budget=1200,
            thinking_mode="disabled",
            warm_cold="cold",
            aggregation="median",
        ),
    )
    assert a.dimensions == b.dimensions
    assert comparable(a, b)


def test_incomparable_across_metric_ids() -> None:
    a = _sample("decode_tokens_per_s")
    b = _sample("e2e_output_tokens_per_s")
    assert a.dimensions == b.dimensions
    assert not comparable(a, b)


def test_incomparable_when_workload_differs() -> None:
    a = _sample("decode_tokens_per_s")
    b = _sample("decode_tokens_per_s", workload_id="fresh_code")
    assert not comparable(a, b)


def test_incomparable_when_aggregation_differs() -> None:
    a = _sample("decode_tokens_per_s", aggregation="median")
    b = _sample("aggregate_output_tokens_per_s", aggregation="group_wall")
    assert not comparable(a, b)


def _sample(
    metric_id: str,
    workload_id: str = "edit_cold",
    aggregation: str = "median",
) -> MetricSample:
    return MetricSample(
        metric_id=metric_id,
        value=1.0,
        dimensions=MetricDimensions(
            unit="tok/s",
            procedure_version="v1",
            workload_id=workload_id,
            concurrency=1,
            output_budget=1200,
            thinking_mode="disabled",
            warm_cold="cold",
            aggregation=aggregation,
        ),
    )
