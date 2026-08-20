"""Bounded Prometheus telemetry normalization for Inference Lab."""
from __future__ import annotations

import math

import pytest

from serving_verdict.lab_telemetry import (
    MetricBinding,
    TelemetryBuffer,
    TelemetryError,
    TelemetryFailure,
    TelemetrySample,
    parse_prometheus_snapshot,
)

BINDINGS = (
    MetricBinding(
        source_name="vllm:num_requests_running",
        metric_id="request_concurrency",
        unit="request",
        direction="lower_better",
        procedure_version="vllm.metrics.v1",
        allowed_labels=("model_name",),
    ),
    MetricBinding(
        source_name="vllm:gpu_cache_usage_perc",
        metric_id="kv_cache_utilization",
        unit="ratio",
        direction="lower_better",
        procedure_version="vllm.metrics.v1",
        allowed_labels=(),
    ),
)


def test_parse_allowlisted_metrics_only() -> None:
    body = b"""# HELP ignored text
vllm:num_requests_running{model_name=\"served\",pod=\"secret-pod\"} 3
vllm:gpu_cache_usage_perc 0.75
python_gc_objects_collected_total 999
"""
    samples = parse_prometheus_snapshot(body, offset_s=2.0, bindings=BINDINGS)
    assert [sample.metric_id for sample in samples] == [
        "kv_cache_utilization",
        "request_concurrency",
    ]
    concurrency = samples[1]
    assert concurrency.labels == (("model_name", "served"),)
    assert "pod" not in repr(samples)


def test_sample_contract_is_immutable_and_finite() -> None:
    sample = parse_prometheus_snapshot(
        b"vllm:gpu_cache_usage_perc 0.5\n", offset_s=0.0, bindings=BINDINGS
    )[0]
    with pytest.raises((AttributeError, TypeError)):
        sample.value = 1.0  # type: ignore[misc]
    assert math.isfinite(sample.value)


@pytest.mark.parametrize(
    "body",
    [
        b"vllm:gpu_cache_usage_perc NaN\n",
        b"vllm:gpu_cache_usage_perc +Inf\n",
        b"vllm:gpu_cache_usage_perc nope\n",
        b"vllm:gpu_cache_usage_perc{bad\"label=\"x\"} 1\n",
        b"vllm:gpu_cache_usage_perc 1 2 3\n",
    ],
)
def test_malformed_or_nonfinite_allowlisted_metric_fails_closed(body: bytes) -> None:
    with pytest.raises(TelemetryError):
        parse_prometheus_snapshot(body, offset_s=1.0, bindings=BINDINGS)


def test_unallowlisted_malformed_line_is_ignored_without_value_leak() -> None:
    result = parse_prometheus_snapshot(
        b'untrusted_metric{secret="sk-do-not-leak"} not-a-number\n',
        offset_s=1.0,
        bindings=BINDINGS,
    )
    assert result == ()


def test_response_and_cardinality_bounds() -> None:
    with pytest.raises(TelemetryError, match="64 KiB"):
        parse_prometheus_snapshot(b"x" * (64 * 1024 + 1), offset_s=1, bindings=BINDINGS)
    many = b"\n".join(
        f'vllm:num_requests_running{{model_name="m{i}"}} {i}'.encode()
        for i in range(65)
    )
    with pytest.raises(TelemetryError, match="series"):
        parse_prometheus_snapshot(many, offset_s=1, bindings=BINDINGS, max_series=64)


@pytest.mark.parametrize("offset", [-1.0, float("nan"), float("inf"), True])
def test_offset_must_be_finite_nonnegative(offset: object) -> None:
    with pytest.raises(TelemetryError):
        parse_prometheus_snapshot(b"", offset_s=offset, bindings=BINDINGS)  # type: ignore[arg-type]


def test_duplicate_binding_or_source_series_fails_closed() -> None:
    with pytest.raises(TelemetryError, match="duplicate binding"):
        parse_prometheus_snapshot(
            b"", offset_s=0, bindings=(BINDINGS[0], BINDINGS[0])
        )
    with pytest.raises(TelemetryError, match="duplicate series"):
        parse_prometheus_snapshot(
            b"vllm:gpu_cache_usage_perc 0.5\nvllm:gpu_cache_usage_perc 0.6\n",
            offset_s=0,
            bindings=BINDINGS,
        )


def test_label_values_are_bounded_and_credential_like_values_rejected() -> None:
    long_value = "x" * 129
    for value in (long_value, "Bearer-sk-secret", "/home/user/model"):
        body = f'vllm:num_requests_running{{model_name="{value}"}} 1\n'.encode()
        with pytest.raises(TelemetryError):
            parse_prometheus_snapshot(body, offset_s=0, bindings=BINDINGS)


def test_ring_buffer_evicts_oldest_and_bounds_series() -> None:
    buffer = TelemetryBuffer(max_samples=3, max_series=2)
    for offset in range(4):
        sample = parse_prometheus_snapshot(
            f"vllm:gpu_cache_usage_perc {offset / 10}\n".encode(),
            offset_s=float(offset),
            bindings=BINDINGS,
        )[0]
        buffer.append((sample,))
    snapshot = buffer.snapshot()
    assert len(snapshot) == 3
    assert [sample.offset_s for sample in snapshot] == [1.0, 2.0, 3.0]


def test_ring_buffer_rejects_too_many_series_without_partial_append() -> None:
    buffer = TelemetryBuffer(max_samples=10, max_series=1)
    first = parse_prometheus_snapshot(
        b"vllm:gpu_cache_usage_perc 0.5\n", offset_s=0, bindings=BINDINGS
    )
    buffer.append(first)
    second = parse_prometheus_snapshot(
        b'vllm:num_requests_running{model_name="m"} 1\n', offset_s=1, bindings=BINDINGS
    )
    with pytest.raises(TelemetryError):
        buffer.append(second)
    assert buffer.snapshot() == first


def test_failure_events_are_fixed_categories_not_raw_errors() -> None:
    buffer = TelemetryBuffer(max_samples=10, max_series=2)
    buffer.record_failure(offset_s=1.0, status="timeout")
    assert buffer.failures()[0].status == "timeout"
    with pytest.raises(TelemetryError):
        buffer.record_failure(offset_s=2.0, status="sk-secret-error")


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_direct_sample_rejects_nonfinite_values(bad: float) -> None:
    with pytest.raises(TelemetryError):
        TelemetrySample(0.0, "m", "m", bad, "s", "neutral", "v1", ())
    with pytest.raises(TelemetryError):
        TelemetrySample(bad, "m", "m", 1.0, "s", "neutral", "v1", ())


def test_direct_types_reject_invalid_direction_raw_error_and_labels() -> None:
    with pytest.raises(TelemetryError):
        TelemetrySample(0.0, "m", "m", 1.0, "s", "sideways", "v1", ())
    with pytest.raises(TelemetryError):
        TelemetrySample(
            0.0,
            "m",
            "m",
            1.0,
            "s",
            "neutral",
            "v1",
            (("z", "1"), ("a", "2")),
        )
    with pytest.raises(TelemetryError):
        TelemetryFailure(float("-inf"), "raw-exception-sk-secret")
