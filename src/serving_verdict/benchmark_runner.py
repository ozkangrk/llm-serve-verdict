"""Quick benchmark runner: orchestration of the frozen profile over real HTTP.

Pipeline (PRODUCT_V1_SPEC.md lifecycle):

    PREFLIGHT -> WARMUP -> MEASURE (serial fresh/edit) -> CONCURRENCY (size 3,
    shared wall) -> QUALITY (arithmetic + tool-call gates) -> SEALED

Rules implemented here:

- Warmups are run but EXCLUDED from every aggregate; they are kept only as
  warmup evidence in the artifact.
- Serial fresh/edit requests run one at a time.
- The concurrency group runs its requests simultaneously against a SHARED wall
  interval; ``aggregate_output_tokens_per_s = total completion tokens / wall``.
- Token counts come only from the API ``usage`` object; missing usage yields
  ``UNMEASURABLE`` (never an estimate).
- Every request outcome is classified: success / success_no_usage /
  zero_tokens / http_error / malformed_sse / timeout / connection_failure.
- The artifact is canonical and tamper-evident; it never contains the API key,
  raw remote error bodies, or raw response text.
"""
from __future__ import annotations

import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from statistics import median
from threading import Barrier
from typing import Any

from serving_verdict.artifact import (
    build_run_artifact,
    compute_artifact_digest,
    run_id,
    verify_artifact,
)
from serving_verdict.endpoint import EndpointConfig
from serving_verdict.graders import grade_arithmetic, grade_tool_call
from serving_verdict.preflight import (
    EndpointPreflightError,
    preflight_endpoint,
)
from serving_verdict.profile import (
    BenchmarkProfile,
    RequestSpec,
    protocol_hash,
    workload_hash,
)
from serving_verdict.sse import StreamMeasurement, measure_sse
from serving_verdict.transport import (
    EndpointTransportError,
    stream_chat_completions,
)

SUCCESS_STATUSES = frozenset({"success", "success_no_usage"})
CONNECTIVITY_STATUSES = frozenset({"timeout", "connection_failure"})


class BenchmarkRunError(RuntimeError):
    """A benchmark run could not be executed (usage/config/preflight level)."""


@dataclass(frozen=True, slots=True)
class RunResult:
    """Sealed benchmark run: the canonical artifact plus a public summary."""

    artifact: dict[str, Any]
    summary: dict[str, Any]

    def public_summary(self) -> dict[str, Any]:
        return dict(self.summary)


def _env_meta() -> dict[str, Any]:
    v = sys.version_info
    return {
        "python": f"{v.major}.{v.minor}.{v.micro}",
        "implementation": sys.implementation.name,
        "platform": sys.platform,
    }


def _served_from_measurement(measurement: StreamMeasurement) -> str | None:
    # Measurement records deliberately carry no remote content; model identity
    # comes from preflight only. Kept explicit so the artifact never guesses.
    del measurement
    return None


# ---------------------------------------------------------------------------
# request execution
# ---------------------------------------------------------------------------


def _run_one_request(
    config: EndpointConfig,
    api_key: str,
    spec: RequestSpec,
    prompt: str,
    model: str,
    *,
    request_timeout_s: float,
) -> StreamMeasurement:
    """Execute one request over real HTTP and measure the stream.

    Transport failures are classified per request (timeout /
    connection_failure) instead of aborting the run: a degraded run still
    yields a sealed, verifiable artifact.
    """
    payload = build_request_payload_for_prompt(model, prompt, spec)
    t0 = time.perf_counter()

    def clock() -> float:
        return time.perf_counter() - t0

    try:
        status, lines = stream_chat_completions(
            config, api_key, payload, request_timeout_s=request_timeout_s
        )
    except EndpointTransportError as exc:
        text = str(exc).lower()
        classified = "timeout" if "timeout" in text else "connection_failure"
        return StreamMeasurement(status=classified, http_status=None)

    try:
        return measure_sse(iter(lines), clock, http_status=status)
    except EndpointTransportError:
        # Timeout raised mid-stream while iterating the response body.
        return StreamMeasurement(status="timeout", http_status=None)


def build_request_payload_for_prompt(model: str, prompt: str, spec: RequestSpec) -> dict[str, Any]:
    """Fixed payload for a prompt; identical shape to the frozen template."""
    from serving_verdict.profile import (  # local import: no cycle at module load
        ToolSchema,
    )

    payload: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "stream": True,
        "stream_options": {"include_usage": True},
        "max_tokens": spec.output_budget,
    }
    if spec.kind == "quality_tool_call" and spec.tool_schemata:
        schema: ToolSchema = spec.tool_schemata[0]
        payload["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": schema.name,
                    "description": schema.description,
                    "parameters": schema.schema,
                },
            }
        ]
        payload["tool_choice"] = {"type": "function", "function": {"name": schema.name}}
    return payload


# ---------------------------------------------------------------------------
# measurement phases
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _RequestOutcome:
    spec: RequestSpec
    prompt: str
    index_in_kind: int
    measurement: StreamMeasurement


def _run_serial(
    config: EndpointConfig,
    api_key: str,
    specs: list[RequestSpec],
    profile: BenchmarkProfile,
    *,
    request_timeout_s: float,
) -> list[_RequestOutcome]:
    outcomes: list[_RequestOutcome] = []
    for index, spec in enumerate(specs):
        prompt = profile.prompt_for(spec, index)
        measurement = _run_one_request(
            config, api_key, spec, prompt, config.model,
            request_timeout_s=request_timeout_s,
        )
        outcomes.append(_RequestOutcome(spec, prompt, index, measurement))
    return outcomes


def _run_concurrency_group(
    config: EndpointConfig,
    api_key: str,
    specs: list[RequestSpec],
    profile: BenchmarkProfile,
    *,
    request_timeout_s: float,
) -> tuple[list[_RequestOutcome], float]:
    """Run one group simultaneously against a SHARED wall interval."""
    prompts = {id(spec): profile.prompt_for(spec, i) for i, spec in enumerate(specs)}

    def worker(
        spec: RequestSpec, index: int
    ) -> tuple[RequestSpec, int, StreamMeasurement]:
        prompt = profile.prompt_for(spec, index)
        measurement = _run_one_request(
            config, api_key, spec, prompt, config.model,
            request_timeout_s=request_timeout_s,
        )
        return spec, index, measurement

    size = len(specs)
    barrier = Barrier(size)

    def barriered_worker(spec: RequestSpec, index: int):
        barrier.wait(timeout=request_timeout_s + 30.0)  # alignment, then fire together
        return worker(spec, index)

    results: list[tuple[RequestSpec, int, StreamMeasurement]] = []
    wall_start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=size) as pool:
        futures = [
            pool.submit(barriered_worker, spec, i) for i, spec in enumerate(specs)
        ]
        for future in futures:
            results.append(future.result(timeout=request_timeout_s + 300.0))
    wall_s = time.perf_counter() - wall_start

    results.sort(key=lambda item: item[0].request_id)
    outcomes = [
        _RequestOutcome(spec, prompts[id(spec)], index, measurement)
        for spec, index, measurement in results
    ]
    return outcomes, wall_s


# ---------------------------------------------------------------------------
# aggregation (arithmetic-exact)
# ---------------------------------------------------------------------------


def _numeric_median(values: list[float]) -> float:
    return median(values)


def _measured_values(
    outcomes: list[_RequestOutcome], field: str
) -> list[float]:
    """Float field values for SUCCESS-only requests with a real number."""
    values: list[float] = []
    for outcome in outcomes:
        value = getattr(outcome.measurement, field)
        if (
            outcome.measurement.status in SUCCESS_STATUSES
            and isinstance(value, float)
            and value is not None
        ):
            values.append(value)
    return values


def _serial_block(
    outcomes: list[_RequestOutcome],
) -> dict[str, Any]:
    ids = [o.spec.request_id for o in outcomes]
    ttft = _measured_values(outcomes, "ttft_s")
    decode = _measured_values(outcomes, "decode_tokens_per_s")
    e2e_rate = _measured_values(outcomes, "e2e_output_tokens_per_s")
    e2e = _measured_values(outcomes, "e2e_s")
    success = sum(
        1 for o in outcomes if o.measurement.status in SUCCESS_STATUSES
    )
    block: dict[str, Any] = {
        "request_ids": ids,
        "count": len(outcomes),
        "success_count": success,
        "measured_denominator": len(outcomes),
        "median_ttft_s": _numeric_median(ttft) if ttft else "UNMEASURABLE",
        "median_decode_tokens_per_s": (
            _numeric_median(decode) if decode else "UNMEASURABLE"
        ),
        "median_e2e_output_tokens_per_s": (
            _numeric_median(e2e_rate) if e2e_rate else "UNMEASURABLE"
        ),
        "median_e2e_s": _numeric_median(e2e) if e2e else "UNMEASURABLE",
    }
    return block


def _concurrency_block(
    outcomes: list[_RequestOutcome], wall_s: float
) -> dict[str, Any]:
    ids = [o.spec.request_id for o in outcomes]
    success = [o for o in outcomes if o.measurement.status in SUCCESS_STATUSES]
    total_tokens: int | None
    if all(o.measurement.completion_tokens is not None for o in success):
        total_tokens = sum(o.measurement.completion_tokens or 0 for o in success)
    else:
        total_tokens = None
    if total_tokens is None or wall_s <= 0:
        aggregate: float | str = "UNMEASURABLE"
    else:
        aggregate = total_tokens / wall_s
    ttft = _measured_values(outcomes, "ttft_s")
    return {
        "request_ids": ids,
        "size": len(outcomes),
        "success_count": len(success),
        "wall_s": wall_s,
        "total_completion_tokens": total_tokens,
        "aggregate_output_tokens_per_s": aggregate,
        "median_ttft_s": _numeric_median(ttft) if ttft else "UNMEASURABLE",
    }


# ---------------------------------------------------------------------------
# quality gates (arithmetic-exact + fixed tool schema)
# ---------------------------------------------------------------------------


def _arithmetic_gate(
    outcomes: list[_RequestOutcome], profile: BenchmarkProfile
) -> dict[str, Any]:
    cases = []
    passed = 0
    for case in profile.arithmetic_cases:
        match = [
            o for o in outcomes if o.spec.request_id == f"arith-{case.case_id}"
        ]
        grade = None
        if match:
            outcome = match[0]
            if outcome.measurement.status in SUCCESS_STATUSES:
                grade = grade_arithmetic(
                    case.case_id,
                    outcome.prompt,
                    outcome.measurement.content,
                    expected=case.expected,
                )
        if grade is not None and grade.passed:
            passed += 1
        cases.append(
            {
                "case_id": case.case_id,
                "expected": case.expected,
                "passed": bool(grade.passed) if grade is not None else False,
                "normalized": grade.normalized if grade is not None else None,
            }
        )
    total = len(profile.arithmetic_cases)
    return {
        "passed_cases": passed,
        "total_cases": total,
        "pass_rate": passed / total if total else 0.0,
        "passed": total > 0 and passed == total,
        "cases": cases,
    }


def _tool_call_gate(
    outcomes: list[_RequestOutcome], profile: BenchmarkProfile
) -> dict[str, Any]:
    tool = profile.tool_schemata[0]
    match = [
        o for o in outcomes if o.spec.request_id == f"tool-call-{tool.name}"
    ]
    grade = None
    if match:
        outcome = match[0]
        if outcome.measurement.status in SUCCESS_STATUSES:
            grade = grade_tool_call(tool, tool_calls=outcome.measurement.tool_calls)
    grade_dict = grade.to_dict() if grade is not None else None
    return {
        "passed": bool(grade.passed) if grade is not None else False,
        "grade": grade_dict,
    }


def _quality_lite_gate(
    arithmetic: dict[str, Any], tool_call: dict[str, Any]
) -> dict[str, Any]:
    total_cases = arithmetic["total_cases"] + 1
    passed_cases = arithmetic["passed_cases"] + (1 if tool_call["passed"] else 0)
    return {
        "passed_cases": passed_cases,
        "total_cases": total_cases,
        "pass_rate": passed_cases / total_cases if total_cases else 0.0,
        "passed": total_cases > 0 and passed_cases == total_cases,
    }


# ---------------------------------------------------------------------------
# error probe
# ---------------------------------------------------------------------------


def _run_error_probe(
    config: EndpointConfig,
    api_key: str,
    probe: dict[str, Any],
    *,
    request_timeout_s: float,
) -> dict[str, Any]:
    suffix = probe["api_key_suffix"]
    probe_key = f"{api_key}{suffix}"
    payload = {
        "model": config.model,
        "messages": [{"role": "user", "content": "Reply exactly: PROBE"}],
        "temperature": 0,
        "max_tokens": 8,
    }
    try:
        status, lines = stream_chat_completions(
            config, probe_key, payload, request_timeout_s=request_timeout_s
        )
        for _ in lines:  # drain; remote body is never surfaced
            pass
        rejected = not (200 <= status < 300)
        http_status: int | None = status
    except EndpointTransportError:
        # Timeout / connection failure: the probe cannot confirm the endpoint
        # rejected the bad key, so the expected behavior is NOT met.
        http_status = None
        rejected = False
    expected = probe["expect"]
    met = rejected if expected == "rejected" else not rejected
    return {
        "kind": probe["kind"],
        "expect": expected,
        "http_status": http_status,
        "rejected": rejected,
        "expected_behavior_met": met,
    }


# ---------------------------------------------------------------------------
# public run entry point
# ---------------------------------------------------------------------------


def run_quick_benchmark(
    config: EndpointConfig,
    *,
    api_key: str,
    profile: BenchmarkProfile,
    transport_timeout_s: float = 120.0,
    preflight_timeout_s: float = 10.0,
) -> RunResult:
    """Run the full frozen profile against one endpoint; always seals an
    artifact for any executable run (degraded runs seal too).

    Raises BenchmarkRunError for usage/config/preflight-level failures where
    no valid artifact can be produced (exit code 2 territory).
    """
    if transport_timeout_s <= 0 or preflight_timeout_s <= 0:
        raise BenchmarkRunError("timeouts must be positive")

    try:
        preflight = preflight_endpoint(
            config, timeout_s=preflight_timeout_s, api_key=api_key
        )
    except EndpointPreflightError as exc:
        raise BenchmarkRunError("endpoint preflight failed") from exc

    specs = list(profile.request_specs())
    by_kind: dict[str, list[RequestSpec]] = {}
    for spec in specs:
        by_kind.setdefault(spec.kind, []).append(spec)

    # -- WARMUP: run, measure, but never aggregate ---------------------------
    warmup_specs = by_kind.get("warmup", [])
    warmup_outcomes = _run_serial(
        config, api_key, warmup_specs, profile,
        request_timeout_s=transport_timeout_s,
    )

    # -- MEASURE: serial fresh + edit (one request at a time) ---------------
    fresh_specs = by_kind.get("serial_fresh", [])
    edit_specs = by_kind.get("serial_edit", [])
    fresh_outcomes = _run_serial(
        config, api_key, fresh_specs, profile,
        request_timeout_s=transport_timeout_s,
    )
    edit_outcomes = _run_serial(
        config, api_key, edit_specs, profile,
        request_timeout_s=transport_timeout_s,
    )

    # -- CONCURRENCY: one size-3 group against a shared wall ----------------
    conc_specs = by_kind.get("concurrency", [])
    if conc_specs:
        conc_outcomes, conc_wall = _run_concurrency_group(
            config, api_key, conc_specs, profile,
            request_timeout_s=transport_timeout_s,
        )
    else:  # pragma: no cover - quick always has a group
        conc_outcomes, conc_wall = [], 0.0

    # -- QUALITY: arithmetic (exact) + tool-call (fixed schema) -------------
    arith_specs = by_kind.get("quality_arithmetic", [])
    arith_outcomes = _run_serial(
        config, api_key, arith_specs, profile,
        request_timeout_s=transport_timeout_s,
    )
    tool_specs = by_kind.get("quality_tool_call", [])
    tool_outcomes = _run_serial(
        config, api_key, tool_specs, profile,
        request_timeout_s=transport_timeout_s,
    )

    measured_outcomes = (
        fresh_outcomes + edit_outcomes + conc_outcomes + arith_outcomes + tool_outcomes
    )

    # -- error probe (invalid key must be rejected by the endpoint) ---------
    error_probe = _run_error_probe(
        config, api_key, profile.error_probe,
        request_timeout_s=min(transport_timeout_s, 10.0),
    )

    # -- aggregates (warmups excluded everywhere) ---------------------------
    serial = {
        "fresh": _serial_block(fresh_outcomes),
        "edit": _serial_block(edit_outcomes),
    }
    concurrency_blocks = [
        _concurrency_block(conc_outcomes, conc_wall)
    ] if conc_outcomes else []
    successful = sum(
        1 for o in measured_outcomes if o.measurement.status in SUCCESS_STATUSES
    )
    measured_attempts = len(measured_outcomes)
    request_success = {
        "measured_attempts": measured_attempts,
        "successful": successful,
        "measured_denominator": measured_attempts,
        "rate": successful / measured_attempts if measured_attempts else 0.0,
    }

    # -- gates ----------------------------------------------------------------
    arithmetic_gate = _arithmetic_gate(arith_outcomes, profile)
    tool_call_gate = _tool_call_gate(tool_outcomes, profile)
    quality_lite = _quality_lite_gate(arithmetic_gate, tool_call_gate)
    gates = {
        "arithmetic": arithmetic_gate,
        "tool_call": tool_call_gate,
        "quality_lite": quality_lite,
    }

    connectivity = sum(
        1 for o in measured_outcomes
        if o.measurement.status in CONNECTIVITY_STATUSES
    )
    warmup_ok = all(
        outcome.measurement.status in SUCCESS_STATUSES
        for outcome in warmup_outcomes
    )
    if (
        warmup_ok
        and error_probe["expected_behavior_met"]
        and connectivity == 0
        and quality_lite["passed"]
        and request_success["rate"] == 1.0
    ):
        run_status = "ok"
    else:
        run_status = "degraded"

    # -- sealed artifact -------------------------------------------------------
    def records_for(outcomes: list[_RequestOutcome], warmup: bool) -> list[dict[str, Any]]:
        return [
            {
                "request_id": o.spec.request_id,
                "kind": o.spec.kind,
                "workload": o.spec.workload,
                "warmup": warmup,
                "measurement": o.measurement.public_record(warmup=warmup),
            }
            for o in outcomes
        ]

    artifact = build_run_artifact(
        config=config,
        profile=profile,
        preflight=preflight,
        run_status=run_status,
        warmup_records=records_for(warmup_outcomes, warmup=True),
        measured_records=records_for(measured_outcomes, warmup=False),
        serial=serial,
        concurrency=concurrency_blocks,
        request_success=request_success,
        gates=gates,
        error_probe=error_probe,
        protocol_hash=protocol_hash(profile),
        workload_hash=workload_hash(profile),
        env_meta=_env_meta(),
    )
    digest = compute_artifact_digest(artifact)
    artifact["artifact_digest"] = digest
    verify_artifact(artifact)  # sealed artifacts must self-verify before return

    summary = {
        "run_id": run_id(artifact),
        "run_status": run_status,
        "profile": profile.name,
        "procedure_version": profile.procedure_version,
        "model": {
            "requested": preflight.requested_model,
            "served": preflight.served_model,
        },
        "preflight": {
            "models_probe": preflight.models_probe,
            "chat_probe": preflight.chat_probe,
            "served_model": preflight.served_model,
        },
        "request_success_rate": request_success["rate"],
        "quality_lite": quality_lite,
        "artifact_digest": digest,
    }
    return RunResult(artifact=artifact, summary=summary)
