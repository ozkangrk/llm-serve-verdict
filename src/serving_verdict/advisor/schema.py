"""Typed, normalized inputs for the Config Advisor.

``parse_advisor_input`` accepts a plain dict (e.g. decoded JSON/YAML) and
returns a frozen :class:`AdvisorInput`. It fails closed on any value that is
missing, wrong-typed, non-finite, out of range, or that smuggles secrets or
shell metacharacters into fields that end up in an argv.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

from serving_verdict.advisor import AdvisorError

RUNTIME_FAMILIES = ("vllm", "sglang", "llama.cpp")
MEMORY_STATUSES = ("ok", "low", "oom")

# Fields that may appear at the top level of the raw document.
_ALLOWED_TOP_KEYS = frozenset(
    {"runtime_family", "model_path", "benchmark", "capacity", "current_flags"}
)

# Benchmark metric bounds: (min, max) inclusive for *positive* evidence.
# A metric is only usable evidence when it is a finite number in range.
_BENCH_BOUNDS: dict[str, tuple[float, float]] = {
    "ttft_s": (0.0, 120.0),          # seconds to first token
    "decode_tokens_per_s": (0.0, 100000.0),
    "e2e_tokens_per_s": (0.0, 100000.0),
    "max_concurrency": (0, 100000),
    "request_failure_rate": (0.0, 1.0),
    "tool_call_success_rate": (0.0, 1.0),
}

# Throughput/concurrency metrics are evidence only when strictly positive;
# a zero reading means the metric was not measured, not that it is 0.
_POSITIVE_BENCH = frozenset({"decode_tokens_per_s", "e2e_tokens_per_s", "max_concurrency"})

# Capacity bounds.
_CAPACITY_BOUNDS: dict[str, tuple[float, float]] = {
    "kv_cache_usage": (0.0, 1.0),
    "context_len": (0, 10_000_000),
    "concurrency_target": (1, 100000),
}

# Character classes that must never reach an argv element.
_SHELL_METACHAR_RE = re.compile(r"[;&|`$()<>{}\[\]&\n\r\t'\"]")
_SECRET_KEY_RE = re.compile(r"(secret|token|password|passwd|api_key|apikey|credential|auth)", re.I)
_MODEL_PATH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/\-]*$")


def _fail(msg: str) -> Any:
    raise AdvisorError(msg)


def _finite_number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(f"{name} must be a number, got {type(value).__name__}")
    if not math.isfinite(value):
        _fail(f"{name} must be finite, got {value!r}")
    return float(value)


def _in_bounds(value: float, bounds: tuple[float, float], name: str) -> None:
    lo, hi = bounds
    if not (lo <= value <= hi):
        _fail(f"{name} out of allowed range [{lo}, {hi}]: {value!r}")


def _check_no_metachars(value: str, name: str) -> None:
    if not value:
        _fail(f"{name} must be a non-empty string")
    if _SHELL_METACHAR_RE.search(value):
        _fail(f"{name} contains shell metacharacters and was rejected: {value!r}")
    if value.startswith("-") or value.startswith("/") or ".." in value:
        _fail(f"{name} must not look like a path/flag/escape: {value!r}")


@dataclass(frozen=True)
class BenchmarkFacts:
    ttft_s: float | None
    decode_tokens_per_s: float | None
    e2e_tokens_per_s: float | None
    max_concurrency: float | None
    request_failure_rate: float | None
    tool_call_success_rate: float | None

    def usable(self) -> bool:
        return any(v is not None for v in (
            self.ttft_s, self.decode_tokens_per_s, self.e2e_tokens_per_s,
            self.max_concurrency, self.request_failure_rate,
            self.tool_call_success_rate,
        ))


@dataclass(frozen=True)
class CapacityFacts:
    memory_status: str | None
    kv_cache_usage: float | None
    context_len: float | None
    concurrency_target: float | None

    def usable(self) -> bool:
        return any(v is not None for v in (
            self.memory_status, self.kv_cache_usage, self.context_len,
            self.concurrency_target,
        ))


@dataclass(frozen=True)
class AdvisorInput:
    runtime_family: str
    model_path: str | None
    benchmark: BenchmarkFacts
    capacity: CapacityFacts
    current_flags: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)


def _parse_benchmark(raw: Any) -> BenchmarkFacts:
    if not isinstance(raw, dict):
        _fail("benchmark must be an object")
    known = set(_BENCH_BOUNDS)
    extra = set(raw) - known
    if extra:
        _fail(f"benchmark has unknown fields: {sorted(extra)}")
    values: dict[str, float | None] = {}
    for key in _BENCH_BOUNDS:
        v = raw.get(key, None)
        if v is None:
            values[key] = None
        else:
            num = _finite_number(v, f"benchmark.{key}")
            lo, hi = _BENCH_BOUNDS[key]
            _in_bounds(num, (lo, hi), f"benchmark.{key}")
            if key in _POSITIVE_BENCH and num <= 0:
                _fail(f"benchmark.{key} must be > 0 when present (got {num!r}); use null for no evidence")
            values[key] = num
    return BenchmarkFacts(
        ttft_s=values["ttft_s"],
        decode_tokens_per_s=values["decode_tokens_per_s"],
        e2e_tokens_per_s=values["e2e_tokens_per_s"],
        max_concurrency=values["max_concurrency"],
        request_failure_rate=values["request_failure_rate"],
        tool_call_success_rate=values["tool_call_success_rate"],
    )


def _parse_capacity(raw: Any) -> CapacityFacts:
    if not isinstance(raw, dict):
        _fail("capacity must be an object")
    known = {"memory_status"} | set(_CAPACITY_BOUNDS)
    extra = set(raw) - known
    if extra:
        _fail(f"capacity has unknown fields: {sorted(extra)}")
    ms = raw.get("memory_status", None)
    if ms is not None and (not isinstance(ms, str) or ms not in MEMORY_STATUSES):
        _fail(f"capacity.memory_status must be one of {list(MEMORY_STATUSES)} or null")
    values: dict[str, float | None] = {}
    for key in _CAPACITY_BOUNDS:
        v = raw.get(key, None)
        if v is None:
            values[key] = None
        else:
            num = _finite_number(v, f"capacity.{key}")
            lo, hi = _CAPACITY_BOUNDS[key]
            _in_bounds(num, (lo, hi), f"capacity.{key}")
            values[key] = num
    return CapacityFacts(
        memory_status=ms,
        kv_cache_usage=values["kv_cache_usage"],
        context_len=values["context_len"],
        concurrency_target=values["concurrency_target"],
    )


def _parse_model_path(raw: Any) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, str):
        _fail("model_path must be a string or null")
    if not _MODEL_PATH_RE.match(raw):
        _fail(f"model_path has invalid characters or is a traversal: {raw!r}")
    if raw.startswith("./") or raw.startswith("../") or raw[-1] == "/":
        _fail(f"model_path is a relative/escape path: {raw!r}")
    return raw


def _parse_flags(raw: Any) -> dict[str, Any]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        _fail("current_flags must be an object")
    return dict(raw)


def parse_advisor_input(doc: Any) -> AdvisorInput:
    """Validate and normalize a raw document into an :class:`AdvisorInput`."""
    if not isinstance(doc, dict):
        _fail("advisor input must be an object")
    extra = set(doc) - _ALLOWED_TOP_KEYS
    if extra:
        _fail(f"unknown top-level fields: {sorted(extra)}")
    family = doc.get("runtime_family")
    if not isinstance(family, str) or family not in RUNTIME_FAMILIES:
        _fail(f"runtime_family must be one of {list(RUNTIME_FAMILIES)}")
    family = str(family)
    model_path = _parse_model_path(doc.get("model_path", None))
    benchmark = _parse_benchmark(doc.get("benchmark", {}))
    capacity = _parse_capacity(doc.get("capacity", {}))
    flags = _parse_flags(doc.get("current_flags", {}))
    return AdvisorInput(
        runtime_family=family,
        model_path=model_path,
        benchmark=benchmark,
        capacity=capacity,
        current_flags=flags,
        raw=dict(doc),
    )
