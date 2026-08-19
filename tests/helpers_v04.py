"""Shared builders for v0.4 experiment-planner tests.

``make_summary`` returns a *valid, sealed* normalized benchmark summary
document (a plain JSON dict with a correct digest). Test helpers mutate
the dict and re-seal when the point is to vary one field.
"""
from __future__ import annotations

import copy
from typing import Any

from serving_verdict.canonical import canonicalize, digest_payload
from serving_verdict.summary import MEASUREMENT_KEYS  # type: ignore[import-untyped]


def seal(doc: dict[str, Any]) -> dict[str, Any]:
    """Recompute and set ``doc['digest']`` over the payload without the digest."""
    payload = {k: v for k, v in doc.items() if k != "digest"}
    doc["digest"] = digest_payload(canonicalize(payload))
    return doc


def make_summary(**overrides: Any) -> dict[str, Any]:
    """Build a valid sealed summary with the given deep overrides.

    ``overrides`` may contain:
      - workload: dict overriding the whole workload section
      - model: dict overriding the whole model section
      - protocol: dict overriding the whole protocol section
      - usage: dict overriding usage
      - measurements: dict of {key: value} merged into measurements
      - drop: list of measurement keys to remove
      - tamper_marker: any value -> adds the forbidden key
    The returned document is sealed (digest set) unless the tamper_marker
    override is present (tamper tests mutate after the fact).
    """
    doc: dict[str, Any] = {
        "schema_version": "serving-verdict.summary.v0.1",
        "workload": {
            "id": "workload-a",
            "requests": 300,
            "input_tokens_mean": 1400,
            "output_tokens_mean": 307,
        },
        "model": {
            "id": "model-x-7b",
            "quantization": "fp8",
            "architecture": "moe-2x8",
        },
        "protocol": {
            "version": "bench-v2",
            "procedure": "steady-state",
            "concurrency": 8,
            "warmup_requests": 20,
        },
        "usage": {
            "requests": 300,
            "tokens_in": 410000,
            "tokens_out": 92000,
        },
        "measurements": {
            "ttft_s": 0.31,
            "e2e_latency_s": 2.85,
            "throughput_rps": 11.3,
            "success_rate": 0.999,
            "tool_accuracy": 0.94,
            "quality_score": 0.97,
            "decode_latency_ms": 12.4,
            "peak_memory_gb": 9.1,
            "concurrency_max": 8,
        },
    }
    for section in ("workload", "model", "protocol", "usage"):
        if section in overrides and isinstance(overrides[section], dict):
            doc[section] = copy.deepcopy(overrides[section])
    if "measurements" in overrides:
        for k, v in overrides["measurements"].items():
            doc["measurements"][k] = v
    for k in overrides.get("drop") or []:
        doc["measurements"].pop(k, None)
    if "tamper_marker" in overrides:
        doc["tamper_marker"] = overrides["tamper_marker"]
    return seal(doc)


def make_parsed(**overrides: Any):
    """Convenience: build a sealed summary doc and parse it to a Summary."""
    from serving_verdict.summary import parse_summary_payload

    return parse_summary_payload(make_summary(**overrides))


def assert_measurement_keys() -> None:
    assert set(MEASUREMENT_KEYS) == {
        "ttft_s",
        "e2e_latency_s",
        "throughput_rps",
        "success_rate",
        "tool_accuracy",
        "quality_score",
        "decode_latency_ms",
        "peak_memory_gb",
        "concurrency_max",
    }
