"""Shared builders for Config Advisor tests."""
from __future__ import annotations

from typing import Any


def full_benchmark() -> dict[str, Any]:
    return {
        "ttft_s": 0.6,
        "decode_tokens_per_s": 20.0,
        "e2e_tokens_per_s": 19.0,
        "max_concurrency": 8,
        "request_failure_rate": 0.01,
        "tool_call_success_rate": 0.99,
    }


def empty_benchmark() -> dict[str, Any]:
    return {
        "ttft_s": None,
        "decode_tokens_per_s": None,
        "e2e_tokens_per_s": None,
        "max_concurrency": None,
        "request_failure_rate": None,
        "tool_call_success_rate": None,
    }


def full_capacity() -> dict[str, Any]:
    return {
        "memory_status": "ok",
        "kv_cache_usage": 0.4,
        "context_len": 32768,
        "concurrency_target": 16,
    }


def raw_advisor_doc(
    family: str = "vllm",
    benchmark: dict[str, Any] | None = None,
    capacity: dict[str, Any] | None = None,
    current_flags: dict[str, Any] | None = None,
    model_path: str | None = "org/model-7b",
) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "runtime_family": family,
        "benchmark": full_benchmark() if benchmark is None else benchmark,
        "capacity": full_capacity() if capacity is None else capacity,
        "current_flags": {} if current_flags is None else current_flags,
    }
    if model_path is not None:
        doc["model_path"] = model_path
    return doc
