"""Shared test fixtures and helpers."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"

DSKAB_SCHEMA = "qwen38.dspark-ab.v1"
SGLANG_SCHEMA = "qwen38.sglang-vllm-ab.v1"

CASE_SCHEMA = "serving-verdict.case.v0.1"
BUNDLE_SCHEMA = "serving-verdict.bundle.v0.1"

REAL_SOURCE_ROOT = Path("/home/ozkangu/Desktop/Qwen3.8-27B-DGX-Spark-RTX-6000")


def make_dspark_ab_fixture(
    root: Path,
    filename: str = "dspark_fixture.json",
    engine: str = "vllm-dspark-k7",
    decode: float = 63.27,
    e2e: float = 59.41,
    ttft: float = 1.27,
    latency: float = 20.2,
    agg: float = 132.8,
    wall: float = 27.1,
    completion_tokens: int = 1200,
    workloads: tuple[str, ...] = ("edit_cold",),
    finish_reasons: tuple[str, ...] | None = None,
) -> Path:
    """Write a minimized dspark-ab.v1 artifact under root and return its path."""
    results: dict[str, object] = {}
    for i, workload in enumerate(workloads):
        results[workload] = {
            "requests": [
                {
                    "prompt_tokens": 2665,
                    "completion_tokens": completion_tokens,
                    "latency_s": latency,
                    "ttft_s": ttft,
                    "decode_tokens_per_s": decode,
                    "e2e_output_tokens_per_s": e2e,
                    "finish_reason": (finish_reasons or ("length",))[i % len(finish_reasons or ("length",))],
                    "gpu": {"samples": 0},
                }
                for _ in range(3)
            ],
            "median_decode_tokens_per_s": decode,
            "median_e2e_output_tokens_per_s": e2e,
            "median_ttft_s": ttft,
            "median_latency_s": latency,
        }
    if "concurrency" in str(workloads):
        for workload in list(results):
            if workload.startswith("concurrency"):
                groups = [
                    {
                        "wall_s": wall,
                        "total_completion_tokens": completion_tokens * 3,
                        "aggregate_output_tokens_per_s": agg,
                        "requests": [],
                    }
                ]
                results[workload] = {
                    "groups": groups,
                    "median_aggregate_output_tokens_per_s": agg,
                    "median_wall_s": wall,
                }
    doc = {
        "schema_version": DSKAB_SCHEMA,
        "created_at": "2026-08-17T08:00:00+00:00",
        "engine": engine,
        "profile": "fixture",
        "base_url": "http://127.0.0.1:8889/v1",
        "model": "fixture-model",
        "repeats": 3,
        "results": results,
        "claim_boundary": "fixture boundary",
    }
    path = root / filename
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def make_sglang_ab_fixture(
    root: Path,
    filename: str = "sglang_fixture.json",
    engine: str = "sglang-mtp",
    decode: float = 36.10,
    ttft: float = 0.219,
    agg: float = 96.84,
    wall: float = 10.57,
    finish_reasons: tuple[str, ...] | None = None,
) -> Path:
    """Write a minimized sglang-vllm-ab.v1 artifact under root and return its path."""
    requests = [
        {
            "prompt_tokens": 38,
            "completion_tokens": 512,
            "latency_s": 14.4,
            "ttft_s": ttft,
            "decode_tokens_per_s": decode,
            "e2e_output_tokens_per_s": decode - 0.5,
            "finish_reason": (finish_reasons or ("length",))[i % len(finish_reasons or ("length",))],
        }
        for i in range(3)
    ]
    doc = {
        "schema_version": SGLANG_SCHEMA,
        "created_at": "2026-08-16T23:00:00+00:00",
        "engine": engine,
        "profile": "fixture",
        "base_url": "http://127.0.0.1:30000/v1",
        "model": "fixture-model",
        "seed": 20260817,
        "repeats": 3,
        "results": {
            "short_decode_512": {
                "requests": requests,
                "median_decode_tokens_per_s": decode,
                "median_ttft_s": ttft,
                "median_latency_s": 14.4,
            },
            "concurrency4_short_256": {
                "groups": [
                    {
                        "wall_s": wall,
                        "total_completion_tokens": 512 * 4,
                        "aggregate_output_tokens_per_s": agg,
                        "requests": requests,
                    }
                ],
                "median_aggregate_output_tokens_per_s": agg,
                "median_wall_s": wall,
            },
        },
        "claim_boundary": "fixture boundary",
    }
    path = root / filename
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
