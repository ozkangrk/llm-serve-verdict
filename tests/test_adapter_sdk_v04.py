"""v0.4 adapter SDK and ecosystem adapter contracts (RED first)."""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from serving_verdict.adapter_sdk import (
    AdapterError,
    AdapterRegistry,
    DetectionResult,
    InvalidAdapterPayload,
    NormalizedEvidence,
    UnsupportedAdapterSchema,
)
from serving_verdict.ecosystem_adapters import (
    GuideLLMReportAdapter,
    SGLangServingAdapter,
    VLLMServingAdapter,
    default_ecosystem_registry,
)

FIXTURES = Path(__file__).parent / "fixtures" / "ecosystem"


def vllm_doc() -> dict:
    return {
        "backend": "vllm",
        "endpoint_type": "vllm",
        "duration": 10.0,
        "completed": 9,
        "failed": 1,
        "total_input_tokens": 900,
        "total_output_tokens": 450,
        "request_throughput": 0.9,
        "output_throughput": 45.0,
        "median_ttft_ms": 12.0,
        "p95_ttft_ms": 18.0,
    }


def sglang_doc() -> dict:
    return {
        "backend": "sglang",
        "dataset_name": "random",
        "server_info": {"version": "0.5.x"},
        "duration": 10.0,
        "completed": 8,
        "total_input_tokens": 800,
        "total_input_text_tokens": 800,
        "total_input_vision_tokens": 0,
        "total_output_tokens": 400,
        "request_throughput": 0.8,
        "output_throughput": 40.0,
        "median_ttft_ms": 14.0,
        "p95_ttft_ms": 21.0,
        "p95_e2e_latency_ms": 900.0,
        "mean_itl_ms": 8.0,
        "p95_itl_ms": 11.0,
        "concurrency": 4.0,
    }


def dist(mean: float, median: float, p95: float, count: int = 9) -> dict:
    return {
        "mean": mean,
        "median": median,
        "mode": median,
        "variance": 1.0,
        "std_dev": 1.0,
        "min": 0.1,
        "max": p95,
        "count": count,
        "total_sum": mean * count,
        "percentiles": {
            "p001": 0.1,
            "p01": 0.1,
            "p05": 0.2,
            "p10": 0.3,
            "p25": 0.5,
            "p50": median,
            "p75": p95,
            "p90": p95,
            "p95": p95,
            "p99": p95,
            "p999": p95,
        },
        "pdf": None,
    }


def status_dist(mean: float, median: float, p95: float) -> dict:
    successful = dist(mean, median, p95)
    empty = dist(0.0, 0.0, 0.0, count=0)
    return {
        "successful": successful,
        "incomplete": empty,
        "errored": empty,
        "total": successful,
    }


def guidellm_doc() -> dict:
    return {
        "metadata": {
            "version": 2,
            "guidellm_version": "0.3.x",
            "python_version": "3.12",
            "platform": "linux",
        },
        "config": {"profile": "constant", "rate": 1.0},
        "benchmarks": [
            {
                "config": {"profile": "constant"},
                "scheduler_state": {},
                "scheduler_metrics": {
                    "requests_made": {
                        "successful": 9,
                        "incomplete": 0,
                        "errored": 1,
                        "total": 10,
                    }
                },
                "metrics": {
                    "request_totals": {
                        "successful": 9,
                        "incomplete": 0,
                        "errored": 1,
                        "total": 10,
                    },
                    "requests_per_second": status_dist(0.9, 0.9, 1.0),
                    "request_latency": status_dist(0.8, 0.7, 1.2),
                    "time_to_first_token_ms": status_dist(15.0, 14.0, 22.0),
                    "inter_token_latency_ms": status_dist(8.0, 7.0, 12.0),
                    "output_tokens_per_second": status_dist(44.0, 43.0, 50.0),
                },
            }
        ],
    }


@pytest.mark.parametrize(
    ("adapter", "doc", "adapter_id"),
    [
        (VLLMServingAdapter(), vllm_doc(), "vllm.serve"),
        (SGLangServingAdapter(), sglang_doc(), "sglang.serving"),
        (GuideLLMReportAdapter(), guidellm_doc(), "guidellm.report"),
    ],
)
def test_adapter_detects_and_parses_documented_shape(adapter, doc, adapter_id) -> None:
    detection = adapter.detect(doc)
    assert detection.matched is True
    evidence = adapter.parse(doc, source_sha256="a" * 64)
    assert evidence.adapter_id == adapter_id
    assert evidence.source_sha256 == "a" * 64
    assert evidence.metrics
    assert all(math.isfinite(metric.value) for metric in evidence.metrics)


def test_vllm_semantic_mapping_is_explicit() -> None:
    evidence = VLLMServingAdapter().parse(vllm_doc(), source_sha256="b" * 64)
    assert evidence.metric("request_throughput_rps").value == 0.9
    assert evidence.metric("output_tokens_per_s").value == 45.0
    assert evidence.metric("ttft_p50_ms").value == 12.0
    assert evidence.metric("ttft_p95_ms").value == 18.0
    assert evidence.metric("request_success_rate").value == pytest.approx(0.9)
    assert evidence.metric("ttft_p95_ms").direction == "lower_better"


def test_sglang_semantic_mapping_keeps_latency_types_distinct() -> None:
    evidence = SGLangServingAdapter().parse(sglang_doc(), source_sha256="c" * 64)
    assert evidence.metric("ttft_p95_ms").value == 21.0
    assert evidence.metric("e2e_latency_p95_ms").value == 900.0
    assert evidence.metric("itl_p95_ms").value == 11.0
    assert evidence.metric("output_tokens_per_s").value == 40.0


def test_guidellm_v2_uses_successful_distribution_and_request_totals() -> None:
    evidence = GuideLLMReportAdapter().parse(guidellm_doc(), source_sha256="d" * 64)
    assert evidence.source_schema_version == "2"
    assert evidence.metric("request_success_rate").value == pytest.approx(0.9)
    assert evidence.metric("ttft_p50_ms").value == 14.0
    assert evidence.metric("ttft_p95_ms").value == 22.0
    assert evidence.metric("itl_p95_ms").value == 12.0


def test_normalized_evidence_is_deeply_immutable() -> None:
    evidence = VLLMServingAdapter().parse(vllm_doc(), source_sha256="e" * 64)
    with pytest.raises(FrozenInstanceError):
        evidence.adapter_id = "changed"  # type: ignore[misc]
    with pytest.raises(TypeError):
        evidence.metrics[0] = evidence.metrics[0]  # type: ignore[index]
    assert isinstance(evidence.metrics[0].dimensions, tuple)


def test_registry_is_immutable_and_duplicate_safe() -> None:
    registry = AdapterRegistry((VLLMServingAdapter(), SGLangServingAdapter()))
    assert registry.adapter_ids == ("sglang.serving", "vllm.serve")
    with pytest.raises(AdapterError, match="duplicate"):
        AdapterRegistry((VLLMServingAdapter(), VLLMServingAdapter()))
    with pytest.raises(TypeError):
        registry.adapters["x"] = VLLMServingAdapter()  # type: ignore[index]


def test_default_registry_autodetects_each_format() -> None:
    registry = default_ecosystem_registry()
    assert registry.detect_one(vllm_doc()).adapter_id == "vllm.serve"
    assert registry.detect_one(sglang_doc()).adapter_id == "sglang.serving"
    assert registry.detect_one(guidellm_doc()).adapter_id == "guidellm.report"


@pytest.mark.parametrize(
    "name",
    [
        "vllm-serve-current.min.json",
        "sglang-serving-current.min.json",
        "guidellm-report-v2.min.json",
    ],
)
def test_minimized_documented_fixture_roundtrip(name: str) -> None:
    raw = (FIXTURES / name).read_bytes()
    doc = json.loads(raw)
    evidence = default_ecosystem_registry().parse_one(
        doc, source_sha256=hashlib.sha256(raw).hexdigest()
    )
    assert evidence.metrics


def test_ambiguous_detection_fails_closed() -> None:
    class Always:
        adapter_id = "always.one"
        capabilities = VLLMServingAdapter().capabilities

        def detect(self, artifact):
            return DetectionResult(True, self.adapter_id, "test")

        def parse(self, artifact, *, source_sha256):
            return NormalizedEvidence.empty(self.adapter_id, source_sha256)

    class Also(Always):
        adapter_id = "always.two"

    registry = AdapterRegistry((Always(), Also()))
    with pytest.raises(AdapterError, match="ambiguous"):
        registry.detect_one({})


@pytest.mark.parametrize(
    "mutator",
    [
        lambda d: d.pop("duration"),
        lambda d: d.__setitem__("request_throughput", float("nan")),
        lambda d: d.__setitem__("completed", True),
        lambda d: d.__setitem__("failed", -1),
        lambda d: d.__setitem__("secret", "sk-live-must-not-leak"),
    ],
)
def test_vllm_malformed_payload_fails_safely(mutator) -> None:
    doc = vllm_doc()
    mutator(doc)
    with pytest.raises(InvalidAdapterPayload) as exc:
        VLLMServingAdapter().parse(doc, source_sha256="f" * 64)
    assert "sk-live" not in str(exc.value)


def test_wrong_guidellm_version_is_unsupported() -> None:
    doc = guidellm_doc()
    doc["metadata"]["version"] = 3
    assert GuideLLMReportAdapter().detect(doc).matched is False
    with pytest.raises(UnsupportedAdapterSchema):
        GuideLLMReportAdapter().parse(doc, source_sha256="1" * 64)


def test_nested_secret_key_is_rejected_without_value_leak() -> None:
    doc = vllm_doc()
    doc["config"] = {"api_key": "sk-nested-must-not-leak"}
    with pytest.raises(InvalidAdapterPayload) as exc:
        VLLMServingAdapter().parse(doc, source_sha256="2" * 64)
    assert "sk-nested" not in str(exc.value)


def test_guidellm_version_provenance_rejects_credential_like_text() -> None:
    doc = guidellm_doc()
    doc["metadata"]["guidellm_version"] = "Bearer sk-must-not-persist"
    with pytest.raises(InvalidAdapterPayload) as exc:
        GuideLLMReportAdapter().parse(doc, source_sha256="3" * 64)
    assert "sk-must" not in str(exc.value)


def test_cross_adapter_shape_uses_explicit_backend_identity() -> None:
    both = {**vllm_doc(), **sglang_doc()}
    assert default_ecosystem_registry().detect_one(both).adapter_id == "sglang.serving"
