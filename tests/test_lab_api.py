from __future__ import annotations

import json
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from serving_verdict.lab_jobs import (
    LabExecutionOutcome,
    LabJobError,
    LabStartRequest,
    LiveTelemetryUpdate,
    parse_lab_start_payload,
)
from serving_verdict.lab_lifecycle import LabState
from serving_verdict.lab_telemetry import TelemetryError, TelemetryFailure, TelemetrySample
from serving_verdict.server import ONLY_BIND_HOST, create_app

SECRET = "lab-api-secret-must-never-leak"


def payload() -> dict[str, object]:
    return {
        "schema_version": "serving-verdict.lab-start.v0.5",
        "template_id": "vllm.openai",
        "model_ref": "qwen-fast",
        "overrides": {
            "gpu_memory_utilization": 0.8,
            "max_model_len": 4096,
        },
        "trial_count": 3,
        "statistical_seed": 17,
        "telemetry_interval_s": 2,
        "telemetry_max_samples": 120,
    }


def sample(value: float = 1.0) -> TelemetrySample:
    return TelemetrySample(
        offset_s=1.0,
        metric_id="runtime.requests.running",
        source_name="vllm:num_requests_running",
        value=value,
        unit="requests",
        direction="neutral",
        procedure_version="vllm-prom-v1",
        labels=(("engine", "0"),),
    )


def wait_terminal(client: TestClient, job_id: str) -> dict:
    for _ in range(200):
        doc = client.get(f"/api/v1/lab/jobs/{job_id}").json()
        if doc["state"] in {
            "SUCCEEDED",
            "FAILED",
            "CANCELLED",
            "CLEANUP_FAILED",
        }:
            return doc
        time.sleep(0.01)
    raise AssertionError("lab job did not terminate")


def app(tmp_path: Path, executor, *, enabled: bool = True) -> TestClient:
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    environment = {"SERVING_VERDICT_ENABLE_LAB": "1"} if enabled else {}
    return TestClient(
        create_app(
            ONLY_BIND_HOST,
            0,
            data,
            lab_executor=executor,
            lab_environment=environment,
        )
    )


def test_lab_api_is_disabled_by_server_environment(tmp_path: Path) -> None:
    called = False

    def executor(*_args):
        nonlocal called
        called = True
        raise AssertionError

    client = app(tmp_path, executor, enabled=False)
    caps = client.get("/api/v1/lab/capabilities")
    assert caps.status_code == 200
    assert caps.json()["enabled"] is False
    started = client.post("/api/v1/lab/jobs", json=payload())
    assert started.status_code == 503
    assert called is False


def test_strict_payload_rejects_secret_unknown_and_unsafe_model_ref(tmp_path: Path) -> None:
    client = app(tmp_path, lambda *_args: None)
    assert client.post("/api/v1/lab/jobs", json=[]).status_code == 400
    for bad in (
        {**payload(), "api_key": SECRET},
        {**payload(), "image": "evil.example/image:latest"},
        {**payload(), "model_ref": "../escape"},
        {**payload(), "overrides": {"--privileged": True}},
    ):
        response = client.post("/api/v1/lab/jobs", json=bad)
        assert response.status_code == 400
        assert SECRET not in response.text
    request = parse_lab_start_payload(payload())
    with pytest.raises(TypeError):
        request.overrides["max_model_len"] = 1  # type: ignore[index]
    for escaped in ("../escape", r"..\escape"):
        with pytest.raises(LabJobError):
            replace(request, model_ref=escaped)
    with pytest.raises(TelemetryError):
        TelemetrySample(
            0.0,
            "runtime.requests.running",
            "vllm:num_requests_running",
            1.0,
            "requests",
            "neutral",
            "v1",
            (("engine", "sk-secret-value"),),
        )


def test_success_exposes_bounded_live_snapshot_and_cleanup_gated_result(
    tmp_path: Path,
) -> None:
    seen: list[LabStartRequest] = []

    def executor(request, _cancel, progress, live):
        seen.append(request)
        progress(LabState.READY)
        live(
            LiveTelemetryUpdate(
                samples=(sample(1.0), sample(3.0)),
                failures=(TelemetryFailure(2.0, "timeout"),),
            )
        )
        progress(LabState.BENCHMARKING)
        return LabExecutionOutcome(
            state=LabState.SUCCEEDED,
            artifact={
                "schema_version": "serving-verdict.lab-run.v0.5",
                "artifact_digest": "sha256:" + "a" * 64,
            },
            error_kind=None,
            cleanup_verified=True,
        )

    client = app(tmp_path, executor)
    caps = client.get("/api/v1/lab/capabilities").json()
    assert caps["enabled"] is True
    assert caps["max_active_jobs"] == 1
    started = client.post("/api/v1/lab/jobs", json=payload())
    assert started.status_code == 202, started.text
    job_id = started.json()["job_id"]
    done = wait_terminal(client, job_id)
    assert done["state"] == "SUCCEEDED"
    assert done["cleanup_verified"] is True
    assert done["result"]["artifact_digest"].startswith("sha256:")
    assert seen and seen[0].model_ref == "qwen-fast"
    live = client.get(f"/api/v1/lab/jobs/{job_id}/live").json()
    assert live["sequence"] == 1
    assert len(live["samples"]) == 2
    assert live["summary"][0]["mean"] == 2.0
    assert live["failures"] == [{"offset_s": 2.0, "status": "timeout"}]
    assert SECRET not in json.dumps(done)
    assert SECRET not in json.dumps(live)


def test_concurrent_start_rejected_and_cancel_discards_result(tmp_path: Path) -> None:
    entered = threading.Event()
    release = threading.Event()

    def executor(_request, cancel, progress, _live):
        progress(LabState.STARTING)
        entered.set()
        release.wait(5.0)
        assert cancel.is_set()
        return LabExecutionOutcome(
            LabState.SUCCEEDED,
            {"must": "be discarded"},
            None,
            True,
        )

    client = app(tmp_path, executor)
    first = client.post("/api/v1/lab/jobs", json=payload())
    assert first.status_code == 202
    assert entered.wait(1.0)
    assert client.post("/api/v1/lab/jobs", json=payload()).status_code == 409
    job_id = first.json()["job_id"]
    cancel = client.post(f"/api/v1/lab/jobs/{job_id}/cancel")
    assert cancel.status_code == 200
    assert cancel.json()["state"] == "CANCEL_REQUESTED"
    release.set()
    done = wait_terminal(client, job_id)
    assert done["state"] == "CANCELLED"
    assert "result" not in done


def test_cleanup_failure_never_exposes_result(tmp_path: Path) -> None:
    def executor(_request, _cancel, _progress, _live):
        return LabExecutionOutcome(
            LabState.CLEANUP_FAILED,
            None,
            "cleanup_failed",
            False,
        )

    client = app(tmp_path, executor)
    job_id = client.post("/api/v1/lab/jobs", json=payload()).json()["job_id"]
    done = wait_terminal(client, job_id)
    assert done["state"] == "CLEANUP_FAILED"
    assert done["cleanup_verified"] is False
    assert "result" not in done


def test_live_bound_backward_progress_and_late_callbacks_fail_closed(
    tmp_path: Path,
) -> None:
    callbacks: dict[str, object] = {}

    def executor(_request, _cancel, progress, live):
        callbacks["progress"] = progress
        callbacks["live"] = live
        progress(LabState.BENCHMARKING)
        progress(LabState.STARTING)
        return LabExecutionOutcome(LabState.SUCCEEDED, {"x": 1}, None, True)

    client = app(tmp_path, executor)
    request = payload()
    request["telemetry_max_samples"] = 1
    job_id = client.post("/api/v1/lab/jobs", json=request).json()["job_id"]
    done = wait_terminal(client, job_id)
    assert done["state"] == "FAILED"
    assert done["error_kind"] == "executor_failed"
    progress = callbacks["progress"]
    live = callbacks["live"]
    with pytest.raises(LabJobError):
        progress(LabState.READY)  # type: ignore[operator]
    with pytest.raises(LabJobError):
        live(LiveTelemetryUpdate((sample(), sample(2.0)), ()))  # type: ignore[operator]
    assert client.get(f"/api/v1/lab/jobs/{job_id}/live").json()["sequence"] == 0


def test_live_update_cannot_exceed_request_sample_bound(tmp_path: Path) -> None:
    def executor(_request, _cancel, _progress, live):
        live(LiveTelemetryUpdate((sample(), sample(2.0)), ()))
        return LabExecutionOutcome(LabState.SUCCEEDED, {"x": 1}, None, True)

    client = app(tmp_path, executor)
    request = payload()
    request["telemetry_max_samples"] = 1
    job_id = client.post("/api/v1/lab/jobs", json=request).json()["job_id"]
    done = wait_terminal(client, job_id)
    assert done["state"] == "FAILED"
    assert done["error_kind"] == "executor_failed"
    assert client.get(f"/api/v1/lab/jobs/{job_id}/live").json()["sequence"] == 0


def test_unknown_lab_job_is_404(tmp_path: Path) -> None:
    client = app(tmp_path, lambda *_args: None)
    assert client.get("/api/v1/lab/jobs/nope").status_code == 404
    assert client.get("/api/v1/lab/jobs/nope/live").status_code == 404
    assert client.post("/api/v1/lab/jobs/nope/cancel").status_code == 404
