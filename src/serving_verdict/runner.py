"""Built-in quick benchmark orchestration and sealed run artifacts."""

from __future__ import annotations

import hashlib
import json
import statistics
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any

from serving_verdict.endpoint import EndpointConfig, resolve_api_key
from serving_verdict.graders import grade_arithmetic, grade_tool_call
from serving_verdict.preflight import PreflightResult, preflight_endpoint
from serving_verdict.profile import (
    ARTIFACT_SCHEMA_VERSION,
    QUICK_PROFILE,
    BenchmarkProfile,
    RequestSpec,
    build_request_payload,
    protocol_hash,
    workload_hash,
)
from serving_verdict.sse import UNMEASURABLE, StreamMeasurement, measure_sse
from serving_verdict.transport import EndpointTransportError, stream_chat_completions

Executor = Callable[[RequestSpec, dict[str, object], float], StreamMeasurement]


class BenchmarkIntegrityError(ValueError):
    """A benchmark artifact failed canonical integrity verification."""


def _canonical_digest(document: dict[str, Any]) -> str:
    payload = {
        key: value
        for key, value in document.items()
        if key not in {"created_at", "artifact_digest"}
    }
    encoded = json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def verify_benchmark_artifact(document: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(document, dict):
        raise BenchmarkIntegrityError("benchmark artifact must be an object")
    if document.get("schema_version") != ARTIFACT_SCHEMA_VERSION:
        raise BenchmarkIntegrityError("unsupported benchmark artifact schema")
    claimed = document.get("artifact_digest")
    if not isinstance(claimed, str) or claimed != _canonical_digest(document):
        raise BenchmarkIntegrityError("benchmark artifact digest mismatch")
    return {"valid": True, "digest": claimed}


def _default_executor(config: EndpointConfig, api_key: str) -> Executor:
    def execute(
        spec: RequestSpec, payload: dict[str, object], timeout_s: float
    ) -> StreamMeasurement:
        try:
            status, lines = stream_chat_completions(
                config, api_key, payload, request_timeout_s=timeout_s
            )
            return measure_sse(iter(lines), time.perf_counter, http_status=status)
        except EndpointTransportError as exc:
            text = str(exc).lower()
            error_status = "timeout" if "timeout" in text else "connection_failure"
            return StreamMeasurement(status=error_status, http_status=None)

    return execute


def _payload(
    profile: BenchmarkProfile,
    spec: RequestSpec,
    model: str,
    index_in_kind: int,
) -> dict[str, object]:
    payload = build_request_payload(profile, spec, model)
    payload["messages"] = [
        {"role": "user", "content": profile.prompt_for(spec, index_in_kind)}
    ]
    return payload


def _public_record(spec: RequestSpec, result: StreamMeasurement) -> dict[str, Any]:
    record = result.public_record(warmup=spec.kind == "warmup")
    # Raw content and tool arguments are consumed by deterministic graders but
    # never persisted in exported benchmark artifacts.
    record.pop("tool_calls", None)
    return {
        "request_id": spec.request_id,
        "kind": spec.kind,
        "workload": spec.workload,
        "concurrency": spec.concurrency,
        "output_budget": spec.output_budget,
        **record,
    }


def _median_numeric(records: list[dict[str, Any]], key: str) -> float | str:
    values = [
        float(record[key])
        for record in records
        if isinstance(record.get(key), (int, float))
        and not isinstance(record.get(key), bool)
    ]
    return statistics.median(values) if values else UNMEASURABLE


def _workload_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for workload in sorted({str(record["workload"]) for record in records}):
        rows = [record for record in records if record["workload"] == workload]
        result[workload] = {
            "request_count": len(rows),
            "ttft_s": _median_numeric(rows, "ttft_s"),
            "decode_tokens_per_s": _median_numeric(rows, "decode_tokens_per_s"),
            "e2e_output_tokens_per_s": _median_numeric(
                rows, "e2e_output_tokens_per_s"
            ),
        }
    return result


def run_quick_benchmark(
    config: EndpointConfig,
    *,
    preflight_result: PreflightResult | None = None,
    executor: Executor | None = None,
    group_clock: Callable[[], float] = time.perf_counter,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Run the server-controlled quick profile and seal an artifact."""
    profile = QUICK_PROFILE
    if preflight_result is None:
        preflight_result = preflight_endpoint(config)
    if executor is None:
        executor = _default_executor(config, resolve_api_key(config))

    specs = profile.request_specs()
    results: dict[str, StreamMeasurement] = {}
    kind_indices: dict[str, int] = {}
    concurrency_specs: list[tuple[RequestSpec, int]] = []

    for spec in specs:
        index = kind_indices.get(spec.kind, 0)
        kind_indices[spec.kind] = index + 1
        if spec.kind == "concurrency":
            concurrency_specs.append((spec, index))
            continue
        payload = _payload(profile, spec, config.model, index)
        results[spec.request_id] = executor(
            spec, payload, profile.request_budget_s
        )

    group_start = group_clock()
    if concurrency_specs:
        with ThreadPoolExecutor(max_workers=profile.concurrency_size) as pool:
            futures = [
                (
                    spec,
                    pool.submit(
                        executor,
                        spec,
                        _payload(profile, spec, config.model, index),
                        profile.request_budget_s,
                    ),
                )
                for spec, index in concurrency_specs
            ]
            for spec, future in futures:
                try:
                    results[spec.request_id] = future.result(
                        timeout=profile.group_budget_s
                    )
                except TimeoutError:
                    results[spec.request_id] = StreamMeasurement(status="timeout")
    group_wall = group_clock() - group_start

    records = [_public_record(spec, results[spec.request_id]) for spec in specs]
    measured = [record for record in records if not record["warmup"]]
    successful_statuses = {"success", "success_no_usage"}
    successes = sum(record["status"] in successful_statuses for record in measured)

    arithmetic_grades: list[dict[str, Any]] = []
    for spec in specs:
        if spec.kind != "quality_arithmetic":
            continue
        case_id = spec.request_id.removeprefix("arith-")
        case = next(case for case in profile.arithmetic_cases if case.case_id == case_id)
        grade = grade_arithmetic(
            case.case_id,
            case.prompt,
            results[spec.request_id].content,
            expected=case.expected,
        )
        item = grade.to_dict()
        item.pop("prompt", None)
        arithmetic_grades.append(item)

    tool_spec = next(spec for spec in specs if spec.kind == "quality_tool_call")
    tool_grade = grade_tool_call(
        profile.tool_schemata[0],
        tool_calls=results[tool_spec.request_id].tool_calls,
    ).to_dict()

    concurrency_records = [
        record for record in measured if record["kind"] == "concurrency"
    ]
    completion_values = [record.get("completion_tokens") for record in concurrency_records]
    numeric_completion_values = [
        value
        for value in completion_values
        if isinstance(value, int) and not isinstance(value, bool)
    ]
    if (
        group_wall > 0
        and completion_values
        and len(numeric_completion_values) == len(completion_values)
    ):
        aggregate: float | str = sum(numeric_completion_values) / group_wall
    else:
        aggregate = UNMEASURABLE

    request_gate = "pass" if successes == len(measured) else "fail"
    arithmetic_passed = sum(bool(item["passed"]) for item in arithmetic_grades)
    gates = [
        {
            "id": "request_success",
            "status": request_gate,
            "passed": successes,
            "total": len(measured),
            "authority": "machine_measured",
        },
        {
            "id": "arithmetic",
            "status": "pass" if arithmetic_passed == len(arithmetic_grades) else "fail",
            "passed": arithmetic_passed,
            "total": len(arithmetic_grades),
            "authority": "machine_measured",
            "cases": arithmetic_grades,
        },
        {
            "id": "tool_call",
            "status": "pass" if tool_grade["passed"] else "fail",
            "passed": 1 if tool_grade["passed"] else 0,
            "total": 1,
            "authority": "machine_measured",
            "case": tool_grade,
        },
    ]

    endpoint_public = config.public_payload()
    endpoint_public.pop("api_key_env", None)
    artifact: dict[str, Any] = {
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "created_at": created_at or datetime.now(UTC).isoformat(),
        "profile": profile.name,
        "procedure_version": profile.procedure_version,
        "protocol_hash": "sha256:" + protocol_hash(profile),
        "workload_hash": "sha256:" + workload_hash(profile),
        "endpoint": endpoint_public,
        "served_model": preflight_result.served_model,
        "preflight": preflight_result.public_payload(),
        "requests": records,
        "summary": {
            "warmup_count": sum(bool(record["warmup"]) for record in records),
            "measured_request_count": len(measured),
            "request_success_rate": successes / len(measured) if measured else 0.0,
            "workloads": _workload_summary(measured),
            "concurrency_3": {
                "group_wall_s": group_wall,
                "aggregate_output_tokens_per_s": aggregate,
            },
        },
        "gates": gates,
        "claim_boundary": (
            "Built-in quality-lite regression and serving performance profile; "
            "not a general model-quality benchmark."
        ),
    }
    artifact["artifact_digest"] = _canonical_digest(artifact)
    return artifact
