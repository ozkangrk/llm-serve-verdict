"""Quick benchmark orchestration and sealed artifact contracts."""

from __future__ import annotations

import json

import pytest

from serving_verdict.endpoint import EndpointConfig
from serving_verdict.preflight import PreflightResult
from serving_verdict.profile import QUICK_PROFILE, RequestSpec
from serving_verdict.runner import (
    BenchmarkIntegrityError,
    run_quick_benchmark,
    verify_benchmark_artifact,
)
from serving_verdict.sse import UNMEASURABLE, StreamMeasurement


class GroupClock:
    def __init__(self, values: list[float]) -> None:
        self.values = iter(values)

    def __call__(self) -> float:
        return next(self.values)


def config() -> EndpointConfig:
    return EndpointConfig(
        endpoint_id="local-test",
        base_url="http://127.0.0.1:9999/v1",
        model="test-model",
        api_key_env="SERVING_VERDICT_API_KEY_TEST",
        remote=False,
    )


def preflight() -> PreflightResult:
    return PreflightResult(
        endpoint_id="local-test",
        requested_model="test-model",
        served_model="served-test-model",
        models_probe="matched",
        chat_probe="ready",
        model_ids=("test-model",),
    )


def arithmetic_answer(spec: RequestSpec) -> str:
    case_id = spec.request_id.removeprefix("arith-")
    case = next(c for c in QUICK_PROFILE.arithmetic_cases if c.case_id == case_id)
    return str(int(case.expected))


def successful_executor(spec: RequestSpec, payload: dict[str, object], timeout_s: float) -> StreamMeasurement:
    assert payload["model"] == "test-model"
    assert payload["stream"] is True
    assert timeout_s == QUICK_PROFILE.request_budget_s
    content = "ok"
    tool_calls: list[dict[str, str]] = []
    if spec.kind == "quality_arithmetic":
        content = arithmetic_answer(spec)
    elif spec.kind == "quality_tool_call":
        content = ""
        tool_calls = [
            {
                "id": "call-1",
                "name": "get_weather",
                "arguments": '{"location":"Istanbul","units":"metric"}',
            }
        ]
    return StreamMeasurement(
        status="success",
        http_status=200,
        ttft_s=0.2,
        e2e_s=1.0,
        prompt_tokens=20,
        completion_tokens=10,
        decode_tokens_per_s=12.5,
        e2e_output_tokens_per_s=10.0,
        finish_reason="stop",
        content=content,
        tool_calls=tool_calls,
    )


def run(executor=successful_executor, clock: GroupClock | None = None) -> dict[str, object]:
    return run_quick_benchmark(
        config(),
        preflight_result=preflight(),
        executor=executor,
        group_clock=clock or GroupClock([10.0, 12.0]),
        created_at="2026-01-01T00:00:00+00:00",
    )


def test_quick_run_inventory_warmup_exclusion_and_aggregates() -> None:
    artifact = run()
    assert artifact["schema_version"] == "serving-verdict.benchmark-run.v1"
    assert artifact["profile"] == "quick"
    records = artifact["requests"]
    assert len(records) == 17
    assert sum(1 for r in records if r["warmup"]) == 2
    assert artifact["summary"]["measured_request_count"] == 15
    assert artifact["summary"]["warmup_count"] == 2
    assert artifact["summary"]["request_success_rate"] == 1.0
    assert artifact["summary"]["workloads"]["fresh_short"]["decode_tokens_per_s"] == 12.5
    # concurrency common wall: 3 * 10 completion tokens / (12 - 10) seconds
    assert artifact["summary"]["concurrency_3"]["aggregate_output_tokens_per_s"] == 15.0


def test_quality_gates_pass_and_prompts_are_frozen() -> None:
    artifact = run()
    gates = {g["id"]: g for g in artifact["gates"]}
    assert gates["request_success"]["status"] == "pass"
    assert gates["arithmetic"]["status"] == "pass"
    assert gates["arithmetic"]["passed"] == 5
    assert gates["tool_call"]["status"] == "pass"
    assert artifact["protocol_hash"].startswith("sha256:")
    assert artifact["workload_hash"].startswith("sha256:")


def test_secret_and_raw_content_are_not_persisted() -> None:
    artifact = run()
    dumped = json.dumps(artifact)
    assert "api_key" not in dumped.lower()
    assert "authorization" not in dumped.lower()
    assert "super-secret" not in dumped
    for record in artifact["requests"]:
        assert "content" not in record
        assert "messages" not in record
        assert "prompt" not in record


def test_missing_usage_is_unmeasurable_not_estimated() -> None:
    def no_usage(spec: RequestSpec, payload: dict[str, object], timeout_s: float) -> StreamMeasurement:
        result = successful_executor(spec, payload, timeout_s)
        result.status = "success_no_usage"
        result.prompt_tokens = None
        result.completion_tokens = None
        result.decode_tokens_per_s = UNMEASURABLE
        result.e2e_output_tokens_per_s = UNMEASURABLE
        return result

    artifact = run(no_usage)
    assert artifact["summary"]["workloads"]["fresh_short"]["decode_tokens_per_s"] == UNMEASURABLE
    assert artifact["summary"]["concurrency_3"]["aggregate_output_tokens_per_s"] == UNMEASURABLE
    assert artifact["gates"][0]["status"] == "pass"  # request itself still succeeded


def test_failed_arithmetic_is_a_failed_hard_gate() -> None:
    def wrong(spec: RequestSpec, payload: dict[str, object], timeout_s: float) -> StreamMeasurement:
        result = successful_executor(spec, payload, timeout_s)
        if spec.kind == "quality_arithmetic":
            result.content = "999"
        return result

    artifact = run(wrong)
    gate = next(g for g in artifact["gates"] if g["id"] == "arithmetic")
    assert gate["status"] == "fail"
    assert gate["passed"] == 0
    assert gate["total"] == 5


def test_transport_failure_is_recorded_not_raised() -> None:
    def failed(spec: RequestSpec, payload: dict[str, object], timeout_s: float) -> StreamMeasurement:
        if spec.request_id == "fresh-1":
            return StreamMeasurement(status="timeout", http_status=None)
        return successful_executor(spec, payload, timeout_s)

    artifact = run(failed)
    record = next(r for r in artifact["requests"] if r["request_id"] == "fresh-1")
    assert record["status"] == "timeout"
    assert artifact["summary"]["request_success_rate"] < 1.0
    gate = next(g for g in artifact["gates"] if g["id"] == "request_success")
    assert gate["status"] == "fail"


def test_artifact_digest_is_deterministic_and_tamper_detected() -> None:
    a = run()
    b = run()
    assert a["artifact_digest"] == b["artifact_digest"]
    report = verify_benchmark_artifact(a)
    assert report["valid"] is True
    a["summary"]["request_success_rate"] = 0.0
    with pytest.raises(BenchmarkIntegrityError):
        verify_benchmark_artifact(a)


def test_preflight_identity_is_recorded_without_credentials() -> None:
    artifact = run()
    assert artifact["endpoint"]["id"] == "local-test"
    assert artifact["endpoint"]["model"] == "test-model"
    assert artifact["served_model"] == "served-test-model"
    assert artifact["preflight"]["chat_probe"] == "ready"
