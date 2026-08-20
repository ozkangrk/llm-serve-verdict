"""Strict ecosystem adapters for documented benchmark result formats.

These adapters parse saved result data only. They do not execute benchmark
content and never infer absent semantic dimensions.
"""
from __future__ import annotations

import math
import re
from typing import Any

from serving_verdict.adapter_sdk import (
    AdapterCapabilities,
    AdapterRegistry,
    DetectionResult,
    InvalidAdapterPayload,
    MetricSample,
    NormalizedEvidence,
    UnsupportedAdapterSchema,
)

_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
_SECRET_KEYS = frozenset({"secret", "password", "api_key", "authorization", "credential"})


def _reject_secret_fields(value: object) -> None:
    stack: list[tuple[object, int]] = [(value, 0)]
    nodes = 0
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > 100_000 or depth > 64:
            raise InvalidAdapterPayload("adapter payload nesting/size exceeds safety bound")
        if isinstance(current, dict):
            for key, child in current.items():
                if not isinstance(key, str):
                    raise InvalidAdapterPayload("adapter payload keys must be strings")
                if key.lower() in _SECRET_KEYS:
                    raise InvalidAdapterPayload(
                        "adapter payload contains a prohibited secret field"
                    )
                stack.append((child, depth + 1))
        elif isinstance(current, list):
            stack.extend((child, depth + 1) for child in current)


def _doc(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise InvalidAdapterPayload("adapter payload must be a JSON object")
    _reject_secret_fields(value)
    return value


def _number(doc: dict[str, Any], key: str, *, positive: bool = False) -> float:
    value = doc.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidAdapterPayload(f"required numeric field is invalid: {key}")
    number = float(value)
    if not math.isfinite(number) or number < 0 or (positive and number <= 0):
        raise InvalidAdapterPayload(f"required numeric field is invalid: {key}")
    return number


def _integer(doc: dict[str, Any], key: str) -> int:
    value = doc.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise InvalidAdapterPayload(f"required integer field is invalid: {key}")
    return value


def _sha(value: str) -> str:
    if not isinstance(value, str) or not _SHA_RE.fullmatch(value):
        raise InvalidAdapterPayload("source_sha256 must be 64 lowercase hex characters")
    return value


def _metric(
    metric_id: str,
    value: float,
    unit: str,
    direction: str,
    source_field: str,
    *,
    aggregation: str,
    procedure: str,
    dimensions: tuple[tuple[str, str], ...] = (),
) -> MetricSample:
    return MetricSample(
        metric_id=metric_id,
        value=value,
        unit=unit,
        direction=direction,
        procedure_version=procedure,
        aggregation=aggregation,
        dimensions=tuple(sorted((("source_field", source_field), *dimensions))),
    )


class VLLMServingAdapter:
    adapter_id = "vllm.serve"
    capabilities = AdapterCapabilities(
        source_versions=("vllm.benchmarks.serve@d626108b",),
        metric_ids=(
            "request_throughput_rps",
            "output_tokens_per_s",
            "request_success_rate",
            "ttft_p50_ms",
            "ttft_p95_ms",
        ),
        limitations=(
            "Saved result has no stable explicit schema_version; detection requires backend+endpoint_type.",
            "Only fields present in the saved result are emitted.",
        ),
        compatibility_status="experimental",
    )

    def detect(self, artifact: object) -> DetectionResult:
        if not isinstance(artifact, dict):
            return DetectionResult(False, self.adapter_id, "not_object")
        matched = (
            artifact.get("backend") == "vllm"
            and artifact.get("endpoint_type") == "vllm"
            and "server_info" not in artifact
            and all(
                key in artifact
                for key in (
                    "duration",
                    "completed",
                    "request_throughput",
                    "output_throughput",
                )
            )
        )
        return DetectionResult(matched, self.adapter_id, "strict_shape", "current")

    def parse(self, artifact: object, *, source_sha256: str) -> NormalizedEvidence:
        doc = _doc(artifact)
        if not (
            doc.get("backend") == "vllm"
            and doc.get("endpoint_type") == "vllm"
            and "server_info" not in doc
        ):
            raise UnsupportedAdapterSchema("unsupported vLLM serving result schema")
        _number(doc, "duration", positive=True)
        completed = _integer(doc, "completed")
        failed = _integer(doc, "failed")
        metrics = [
            _metric(
                "request_throughput_rps",
                _number(doc, "request_throughput"),
                "request/s",
                "higher_better",
                "request_throughput",
                aggregation="run_rate",
                procedure="vllm.serve.current",
            ),
            _metric(
                "output_tokens_per_s",
                _number(doc, "output_throughput"),
                "token/s",
                "higher_better",
                "output_throughput",
                aggregation="run_rate",
                procedure="vllm.serve.current",
            ),
        ]
        total = completed + failed
        if total:
            metrics.append(
                _metric(
                    "request_success_rate",
                    completed / total,
                    "ratio",
                    "higher_better",
                    "completed+failed",
                    aggregation="run_ratio",
                    procedure="vllm.serve.current",
                )
            )
        for field, metric_id, aggregation in (
            ("median_ttft_ms", "ttft_p50_ms", "p50"),
            ("p95_ttft_ms", "ttft_p95_ms", "p95"),
        ):
            if field in doc:
                metrics.append(
                    _metric(
                        metric_id,
                        _number(doc, field),
                        "ms",
                        "lower_better",
                        field,
                        aggregation=aggregation,
                        procedure="vllm.serve.current",
                    )
                )
        return NormalizedEvidence(
            self.adapter_id,
            "current",
            _sha(source_sha256),
            tuple(metrics),
            (("backend", "vllm"),),
        )


class SGLangServingAdapter:
    adapter_id = "sglang.serving"
    capabilities = AdapterCapabilities(
        source_versions=("sglang.benchmark.serving@32d98aad",),
        metric_ids=(
            "request_throughput_rps",
            "output_tokens_per_s",
            "ttft_p50_ms",
            "ttft_p95_ms",
            "e2e_latency_p95_ms",
            "itl_p95_ms",
        ),
        limitations=(
            "Saved result has no stable explicit schema_version; unique SGLang fields are required.",
            "Request success rate is not emitted because failed-request denominator is absent.",
        ),
        compatibility_status="experimental",
    )

    def detect(self, artifact: object) -> DetectionResult:
        if not isinstance(artifact, dict):
            return DetectionResult(False, self.adapter_id, "not_object")
        matched = (
            artifact.get("backend") == "sglang"
            and isinstance(artifact.get("server_info"), dict)
            and all(
                key in artifact
                for key in (
                    "total_input_text_tokens",
                    "total_input_vision_tokens",
                    "median_ttft_ms",
                    "mean_itl_ms",
                    "output_throughput",
                )
            )
        )
        return DetectionResult(matched, self.adapter_id, "strict_shape", "current")

    def parse(self, artifact: object, *, source_sha256: str) -> NormalizedEvidence:
        doc = _doc(artifact)
        if not self.detect(doc).matched:
            raise UnsupportedAdapterSchema("unsupported SGLang serving result schema")
        _number(doc, "duration", positive=True)
        _integer(doc, "completed")
        fields = (
            ("request_throughput", "request_throughput_rps", "request/s", "higher_better", "run_rate"),
            ("output_throughput", "output_tokens_per_s", "token/s", "higher_better", "run_rate"),
            ("median_ttft_ms", "ttft_p50_ms", "ms", "lower_better", "p50"),
            ("p95_ttft_ms", "ttft_p95_ms", "ms", "lower_better", "p95"),
            ("p95_e2e_latency_ms", "e2e_latency_p95_ms", "ms", "lower_better", "p95"),
            ("p95_itl_ms", "itl_p95_ms", "ms", "lower_better", "p95"),
        )
        metrics = tuple(
            _metric(
                metric_id,
                _number(doc, field),
                unit,
                direction,
                field,
                aggregation=aggregation,
                procedure="sglang.serving.current",
            )
            for field, metric_id, unit, direction, aggregation in fields
            if field in doc
        )
        return NormalizedEvidence(
            self.adapter_id,
            "current",
            _sha(source_sha256),
            metrics,
            (("backend", "sglang"),),
        )


class GuideLLMReportAdapter:
    adapter_id = "guidellm.report"
    capabilities = AdapterCapabilities(
        source_versions=("guidellm.report.v2@b3c4c420",),
        metric_ids=(
            "request_throughput_rps",
            "request_success_rate",
            "ttft_p50_ms",
            "ttft_p95_ms",
            "itl_p95_ms",
            "output_tokens_per_s",
        ),
        limitations=(
            "Exactly one benchmark entry is accepted per normalized evidence object.",
            "Only successful-request distributions are mapped to latency/throughput metrics.",
            "GuideLLM request_latency is not emitted until an explicit seconds-to-canonical compatibility rule exists.",
        ),
        compatibility_status="experimental",
    )

    def detect(self, artifact: object) -> DetectionResult:
        if not isinstance(artifact, dict):
            return DetectionResult(False, self.adapter_id, "not_object")
        metadata = artifact.get("metadata")
        matched = (
            isinstance(metadata, dict)
            and metadata.get("version") == 2
            and isinstance(metadata.get("guidellm_version"), str)
            and isinstance(artifact.get("benchmarks"), list)
        )
        return DetectionResult(matched, self.adapter_id, "metadata_version", "2" if matched else None)

    @staticmethod
    def _successful(metrics: dict[str, Any], key: str) -> dict[str, Any]:
        summary = metrics.get(key)
        if not isinstance(summary, dict) or not isinstance(summary.get("successful"), dict):
            raise InvalidAdapterPayload(f"GuideLLM metric distribution is invalid: {key}")
        return summary["successful"]

    def parse(self, artifact: object, *, source_sha256: str) -> NormalizedEvidence:
        doc = _doc(artifact)
        metadata = doc.get("metadata")
        if not isinstance(metadata, dict) or metadata.get("version") != 2:
            raise UnsupportedAdapterSchema("unsupported GuideLLM report schema")
        guidellm_version = metadata.get("guidellm_version")
        if not isinstance(guidellm_version, str) or not _VERSION_RE.fullmatch(
            guidellm_version
        ):
            raise InvalidAdapterPayload("GuideLLM version is invalid")
        benchmarks = doc.get("benchmarks")
        if not isinstance(benchmarks, list) or len(benchmarks) != 1:
            raise InvalidAdapterPayload("GuideLLM report must contain exactly one benchmark")
        benchmark = benchmarks[0]
        if not isinstance(benchmark, dict) or not isinstance(benchmark.get("metrics"), dict):
            raise InvalidAdapterPayload("GuideLLM benchmark metrics are invalid")
        metrics_doc: dict[str, Any] = benchmark["metrics"]
        totals = metrics_doc.get("request_totals")
        if not isinstance(totals, dict):
            raise InvalidAdapterPayload("GuideLLM request_totals is invalid")
        successful = _integer(totals, "successful")
        total = _integer(totals, "total")
        if total == 0 or successful > total:
            raise InvalidAdapterPayload("GuideLLM request totals are invalid")
        metrics = [
            _metric(
                "request_success_rate",
                successful / total,
                "ratio",
                "higher_better",
                "request_totals",
                aggregation="run_ratio",
                procedure="guidellm.report.v2",
            )
        ]
        specs = (
            ("requests_per_second", "mean", "request_throughput_rps", "request/s", "higher_better", "mean"),
            ("time_to_first_token_ms", "median", "ttft_p50_ms", "ms", "lower_better", "p50"),
            ("time_to_first_token_ms", "percentiles.p95", "ttft_p95_ms", "ms", "lower_better", "p95"),
            ("inter_token_latency_ms", "percentiles.p95", "itl_p95_ms", "ms", "lower_better", "p95"),
            ("output_tokens_per_second", "mean", "output_tokens_per_s", "token/s", "higher_better", "mean"),
        )
        for source, path, metric_id, unit, direction, aggregation in specs:
            summary = self._successful(metrics_doc, source)
            current: object = summary
            for part in path.split("."):
                if not isinstance(current, dict) or part not in current:
                    raise InvalidAdapterPayload(f"GuideLLM distribution field is invalid: {source}")
                current = current[part]
            metrics.append(
                _metric(
                    metric_id,
                    _number({"value": current}, "value"),
                    unit,
                    direction,
                    source,
                    aggregation=aggregation,
                    procedure="guidellm.report.v2",
                    dimensions=(("status", "successful"),),
                )
            )
        return NormalizedEvidence(
            self.adapter_id,
            "2",
            _sha(source_sha256),
            tuple(metrics),
            (("guidellm_version", guidellm_version),),
        )


def default_ecosystem_registry() -> AdapterRegistry:
    return AdapterRegistry(
        (VLLMServingAdapter(), SGLangServingAdapter(), GuideLLMReportAdapter())
    )
