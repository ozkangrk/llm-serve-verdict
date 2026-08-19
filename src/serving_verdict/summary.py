"""Normalized, sealed benchmark summary schema (v0.4).

A benchmark summary is a *normalized* measurement record: one model under one
frozen workload and protocol, with usage counters and a fixed set of
measurements. The document is sealed with a canonical digest over everything
except the ``digest`` field itself; parsing is fail-closed — any schema
drift, missing/extra key, non-finite or mistyped value, forbidden tamper
marker, or digest mismatch raises ``SummaryIntegrityError`` (exit 4).

The summary carries *data only*. There is no execution, network, or LLM
involvement anywhere in this path.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from serving_verdict.canonical import canonicalize, digest_payload
from serving_verdict.errors import SummaryIntegrityError

SUMMARY_SCHEMA_VERSION = "serving-verdict.summary.v0.1"

#: Sentinel measurement value: the quantity was not measurable. It is the only
#: allowed non-float measurement value, and it blocks any gate that depends
#: on the metric (INCONCLUSIVE), rather than being treated as zero.
UNMEASURABLE = "UNMEASURABLE"

#: The fixed measurement vocabulary. The exact set is part of the schema:
#: an extra or missing key makes the summary unusable.
MEASUREMENT_KEYS: frozenset[str] = frozenset(
    {
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
)

_TOP_LEVEL_KEYS: frozenset[str] = frozenset(
    {
        "schema_version",
        "workload",
        "model",
        "protocol",
        "usage",
        "measurements",
        "digest",
    }
)
_FORBIDDEN_KEYS: frozenset[str] = frozenset({"tamper_marker"})

_WORKLOAD_KEYS: frozenset[str] = frozenset({"id", "requests", "input_tokens_mean", "output_tokens_mean"})
_MODEL_KEYS: frozenset[str] = frozenset({"id", "quantization", "architecture"})
_PROTOCOL_KEYS: frozenset[str] = frozenset(
    {"version", "procedure", "concurrency", "warmup_requests"}
)
_USAGE_KEYS: frozenset[str] = frozenset({"requests", "tokens_in", "tokens_out"})


@dataclass(frozen=True)
class Summary:
    """A parsed, verified normalized benchmark summary."""

    schema_version: str
    workload: dict[str, Any]
    model: dict[str, Any]
    protocol: dict[str, Any]
    usage: dict[str, float]
    measurements: dict[str, float | str]
    digest: str

    @property
    def context(self) -> dict[str, Any]:
        """Frozen comparability context: workload + model + protocol."""
        return {"workload": self.workload, "model": self.model, "protocol": self.protocol}


def _require_keys_exact(doc: dict[str, Any], allowed: frozenset[str], ctx: str) -> None:
    missing = allowed - doc.keys()
    if missing:
        raise SummaryIntegrityError(f"{ctx} missing required key(s): {sorted(missing)}")
    extra = doc.keys() - allowed
    if extra:
        raise SummaryIntegrityError(f"{ctx} has unknown key(s): {sorted(extra)}")


def _nonempty_str(doc: dict[str, Any], key: str, ctx: str) -> str:
    value = doc.get(key)
    if not isinstance(value, str) or not value.strip():
        raise SummaryIntegrityError(f"{ctx}.{key} must be a non-empty string")
    return value


def _nonneg_int(doc: dict[str, Any], key: str, ctx: str) -> int:
    value = doc.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SummaryIntegrityError(f"{ctx}.{key} must be a non-negative integer")
    return value


def _measurements(doc: Any, ctx: str) -> dict[str, float | str]:
    if not isinstance(doc, dict):
        raise SummaryIntegrityError(f"{ctx} must be a JSON object")
    _require_keys_exact(doc, MEASUREMENT_KEYS, ctx)
    out: dict[str, float | str] = {}
    for key in sorted(MEASUREMENT_KEYS):
        value = doc[key]
        if value == UNMEASURABLE:
            out[key] = UNMEASURABLE
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise SummaryIntegrityError(
                f"{ctx}.{key} must be a finite number or the UNMEASURABLE sentinel"
            )
        if value < 0:
            raise SummaryIntegrityError(f"{ctx}.{key} must be >= 0")
        out[key] = float(value)
    return out


def _parse_section(doc: dict[str, Any], key: str) -> dict[str, Any]:
    section = doc.get(key)
    if not isinstance(section, dict):
        raise SummaryIntegrityError(f"{key} must be a JSON object")
    allowed = {
        "workload": _WORKLOAD_KEYS,
        "model": _MODEL_KEYS,
        "protocol": _PROTOCOL_KEYS,
    }[key]
    _require_keys_exact(section, allowed, key)
    if key == "workload":
        _nonempty_str(section, "id", key)
        _nonneg_int(section, "requests", key)
        _nonneg_int(section, "input_tokens_mean", key)
        _nonneg_int(section, "output_tokens_mean", key)
    elif key == "model":
        _nonempty_str(section, "id", key)
        _nonempty_str(section, "quantization", key)
        _nonempty_str(section, "architecture", key)
    else:  # protocol
        _nonempty_str(section, "version", key)
        _nonempty_str(section, "procedure", key)
        _nonneg_int(section, "concurrency", key)
        _nonneg_int(section, "warmup_requests", key)
    return dict(section)


def _usage(doc: Any, ctx: str) -> dict[str, float]:
    if not isinstance(doc, dict):
        raise SummaryIntegrityError(f"{ctx} must be a JSON object")
    _require_keys_exact(doc, _USAGE_KEYS, ctx)
    out: dict[str, float] = {}
    for key in sorted(_USAGE_KEYS):
        value = doc[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise SummaryIntegrityError(
                f"{ctx}.{key} must be a non-negative number (usage is required to be measured)"
            )
        if value < 0:
            raise SummaryIntegrityError(f"{ctx}.{key} must be >= 0")
        out[key] = float(value)
    return out


def parse_summary_payload(doc: Any) -> Summary:
    """Parse and verify a sealed summary document. Fail-closed.

    Raises ``SummaryIntegrityError`` on any violation; returns a frozen
    ``Summary`` otherwise.
    """
    if not isinstance(doc, dict):
        raise SummaryIntegrityError("summary must be a JSON object")
    for forbidden in _FORBIDDEN_KEYS:
        if forbidden in doc:
            raise SummaryIntegrityError(f"summary carries forbidden key {forbidden!r}")
    _require_keys_exact(doc, _TOP_LEVEL_KEYS, "summary")
    if doc.get("schema_version") != SUMMARY_SCHEMA_VERSION:
        raise SummaryIntegrityError(
            f"unsupported summary schema_version: {doc.get('schema_version')!r} "
            f"(expected {SUMMARY_SCHEMA_VERSION})"
        )
    workload = _parse_section(doc, "workload")
    model = _parse_section(doc, "model")
    protocol = _parse_section(doc, "protocol")
    usage = _usage(doc.get("usage"), "usage")
    measurements = _measurements(doc.get("measurements"), "measurements")
    digest = doc.get("digest")
    if not isinstance(digest, str) or not digest.startswith("sha256:"):
        raise SummaryIntegrityError("summary.digest must be a 'sha256:...' string")
    payload = {k: v for k, v in doc.items() if k != "digest"}
    if digest_payload(canonicalize(payload)) != digest:
        raise SummaryIntegrityError(
            "summary digest mismatch: payload does not re-hash to the recorded digest"
        )
    return Summary(
        schema_version=SUMMARY_SCHEMA_VERSION,
        workload=workload,
        model=model,
        protocol=protocol,
        usage=usage,
        measurements=measurements,
        digest=digest,
    )
