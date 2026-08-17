"""Artifact adapters for the two recognized benchmark schemas.

Only these adapters may produce authoritative metrics (MVP spec):
  - qwen38.dspark-ab.v1
  - qwen38.sglang-vllm-ab.v1

Unknown schemas raise UnknownSchemaError; the importer indexes them as
UNSUPPORTED and no decision can be produced. Content is parsed as data only;
nothing in an artifact is ever executed.
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Any

from serving_verdict.errors import CanonicalizationError
from serving_verdict.metrics import MetricDimensions, MetricSample

DSKAB_SCHEMA = "qwen38.dspark-ab.v1"
SGLANG_SCHEMA = "qwen38.sglang-vllm-ab.v1"

KNOWN_SCHEMAS: frozenset[str] = frozenset({DSKAB_SCHEMA, SGLANG_SCHEMA})

# Both documented benchmark protocols ran with thinking disabled and T=0.
THINKING_MODE = "disabled"
PROCEDURE_VERSION = "v1"


class UnknownSchemaError(Exception):
    """Artifact schema_version is not recognized by any adapter."""


@dataclass(frozen=True)
class AdapterResult:
    schema_version: str
    samples: tuple[MetricSample, ...]
    machine_gates: dict[str, str]


def known_schemas() -> frozenset[str]:
    return KNOWN_SCHEMAS


def _finite(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CanonicalizationError(f"non-numeric value where a metric is expected: {where}")
    if not math.isfinite(float(value)):
        raise CanonicalizationError(f"non-finite metric value at {where}")
    return float(value)


def _warm_cold(workload_id: str) -> str:
    return "warm" if "warm" in workload_id else "cold"


def _median_finite(values: list[Any], where: str) -> float:
    if not values:
        raise CanonicalizationError(f"no values to median at {where}")
    return float(statistics.median(_finite(v, where) for v in values))


def _request_success(doc: dict[str, Any]) -> str:
    """request_success gate: every measured request reached its token budget."""
    results = doc.get("results")
    if not isinstance(results, dict):
        return "fail"
    for block in results.values():
        if not isinstance(block, dict):
            return "fail"
        request_lists = block.get("requests")
        groups = block.get("groups")
        if isinstance(request_lists, list) and request_lists:
            for req in request_lists:
                if not isinstance(req, dict) or req.get("finish_reason") != "length":
                    return "fail"
        if isinstance(groups, list):
            if not groups:
                return "fail"
            for g in groups:
                reqs = g.get("requests") if isinstance(g, dict) else None
                if not isinstance(reqs, list) or not reqs:
                    return "fail"
                for req in reqs:
                    if not isinstance(req, dict) or req.get("finish_reason") != "length":
                        return "fail"
    return "pass"


def _iter_blocks(doc: dict[str, Any]):
    results = doc.get("results")
    if not isinstance(results, dict):
        raise UnknownSchemaError("artifact has no 'results' mapping")
    for workload_id, block in results.items():
        if not isinstance(block, dict):
            continue
        if isinstance(block.get("requests"), list) and block.get("requests"):
            yield workload_id, block, "serial"
        elif isinstance(block.get("groups"), list) and block.get("groups"):
            yield workload_id, block, "group"


def _dims(
    unit: str,
    workload_id: str,
    concurrency: int,
    output_budget: int,
    aggregation: str,
) -> MetricDimensions:
    return MetricDimensions(
        unit=unit,
        procedure_version=PROCEDURE_VERSION,
        workload_id=workload_id,
        concurrency=concurrency,
        output_budget=output_budget,
        thinking_mode=THINKING_MODE,
        warm_cold=_warm_cold(workload_id),
        aggregation=aggregation,
    )


def _block_median(
    workload_id: str,
    key: str,
    block: dict[str, Any],
    rows: list[dict[str, Any]],
) -> float | None:
    """Median for one workload block, taken at call time from explicit inputs.

    Prefers the block-level scalar ``key``; otherwise medians the per-row
    values. All inputs are function parameters (deliberately NOT a closure
    over loop variables) so each workload block is read independently — see
    the multi-workload regression tests in ``tests/test_adapters.py``.
    """
    raw = block.get(key)
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return _finite(raw, f"{workload_id}.{key}")
    vals = [row.get(key) for row in rows if row.get(key) is not None]
    if not vals:
        return None
    return _median_finite(vals, f"{workload_id}.{key}")


def _extract_shared(doc: dict[str, Any], source_artifact: str | None) -> AdapterResult:
    samples: list[MetricSample] = []
    for workload_id, block, kind in _iter_blocks(doc):
        if kind == "serial":
            requests = block["requests"]
            row_dicts = [r for r in requests if isinstance(r, dict)]
            completion_tokens = [r.get("completion_tokens", 0) for r in row_dicts]
            output_budget = int(max(completion_tokens)) if completion_tokens else 0
            decode = _block_median(
                workload_id, "median_decode_tokens_per_s", block, row_dicts
            ) or _block_median(workload_id, "decode_tokens_per_s", block, row_dicts)
            e2e = _block_median(
                workload_id, "median_e2e_output_tokens_per_s", block, row_dicts
            ) or _block_median(workload_id, "e2e_output_tokens_per_s", block, row_dicts)
            ttft = _block_median(
                workload_id, "median_ttft_s", block, row_dicts
            ) or _block_median(workload_id, "ttft_s", block, row_dicts)
            latency = _block_median(
                workload_id, "median_latency_s", block, row_dicts
            ) or _block_median(workload_id, "latency_s", block, row_dicts)
            for metric_id, value, unit, agg in (
                ("decode_tokens_per_s", decode, "tok/s", "median"),
                ("e2e_output_tokens_per_s", e2e, "tok/s", "median"),
                ("ttft_s", ttft, "s", "median"),
                ("api_latency_s", latency, "s", "median"),
            ):
                if value is not None:
                    samples.append(
                        MetricSample(
                            metric_id=metric_id,
                            value=value,
                            dimensions=_dims(unit, workload_id, 1, output_budget, agg),
                            source_artifact=source_artifact,
                        )
                    )
        else:  # group
            groups = [g for g in block["groups"] if isinstance(g, dict)]
            concurrency = len(groups[0].get("requests") or []) if groups else 0
            group_completion_tokens: list[int] = []
            for g in groups:
                for r in g.get("requests") or []:
                    if isinstance(r, dict):
                        group_completion_tokens.append(int(r.get("completion_tokens", 0) or 0))
            output_budget = (
                int(max(group_completion_tokens)) if group_completion_tokens else 0
            )
            aggregate = _block_median(
                workload_id, "median_aggregate_output_tokens_per_s", block, groups
            ) or _block_median(
                workload_id, "aggregate_output_tokens_per_s", block, groups
            )
            if aggregate is not None:
                samples.append(
                    MetricSample(
                        metric_id="aggregate_output_tokens_per_s",
                        value=aggregate,
                        dimensions=_dims("tok/s", workload_id, concurrency, output_budget, "group_wall"),
                        source_artifact=source_artifact,
                    )
                )
    return AdapterResult(
        schema_version=str(doc.get("schema_version")),
        samples=tuple(samples),
        machine_gates={"request_success": _request_success(doc)},
    )


def extract_samples(doc: Any, source_artifact: str | None = None) -> AdapterResult:
    """Extract metric samples from a parsed artifact document.

    Raises UnknownSchemaError for unrecognized schema_version and
    CanonicalizationError for non-finite metric values.
    """
    if not isinstance(doc, dict) or not isinstance(doc.get("schema_version"), str):
        raise UnknownSchemaError("artifact missing schema_version")
    schema = doc["schema_version"]
    if schema not in KNOWN_SCHEMAS:
        raise UnknownSchemaError(f"unsupported artifact schema: {schema}")
    return _extract_shared(doc, source_artifact)
