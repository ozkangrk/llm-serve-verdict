from __future__ import annotations

import math
import time
from copy import deepcopy
from pathlib import Path
from threading import Event
from typing import Any

import pytest

from serving_verdict.artifact import compute_artifact_digest
from serving_verdict.lab_lifecycle import (
    LabLifecycle,
    LabState,
    LifecyclePlan,
    Ownership,
)
from serving_verdict.lab_orchestrator import (
    LabOrchestrationError,
    LabRunOrchestrator,
    benchmark_profile_binding_digest,
    compute_lab_artifact_digest,
    verify_lab_artifact,
)
from serving_verdict.lab_planner import LabRunSpec, plan_lab_run
from serving_verdict.lab_telemetry import MetricBinding
from serving_verdict.lab_templates import bind_builtin_template

IMAGE = "docker.io/vllm/vllm-openai@sha256:" + "a" * 64
PROFILE_DIGEST = benchmark_profile_binding_digest(
    profile_name="quick",
    procedure_version="quick-v1",
    protocol_hash="sha256:" + "c" * 64,
    workload_hash="sha256:" + "d" * 64,
)


class FakeLabBackend:
    def __init__(self, *, cleanup_absent: bool = True) -> None:
        self.calls: list[str] = []
        self.resources: set[str] = set()
        self.cleanup_absent = cleanup_absent
        self.endpoint_url = "http://127.0.0.1:49152/v1"
        self.metrics_url = "http://127.0.0.1:49152/metrics"

    def inspect_capabilities(self, ownership: Ownership, deadline_s: float) -> None:
        self.calls.append("inspect")

    def pull_image(self, image: str, ownership: Ownership, deadline_s: float) -> None:
        self.calls.append("pull")

    def create_network(
        self, resource_id: str, ownership: Ownership, deadline_s: float
    ) -> None:
        self.calls.append("create_network")
        self.resources.add(resource_id)

    def create_container(
        self,
        resource_id: str,
        network_id: str,
        image: str,
        ownership: Ownership,
        deadline_s: float,
    ) -> None:
        self.calls.append("create_container")
        self.resources.add(resource_id)

    def start_container(
        self, resource_id: str, ownership: Ownership, deadline_s: float
    ) -> None:
        self.calls.append("start")

    def wait_ready(
        self, resource_id: str, ownership: Ownership, deadline_s: float
    ) -> None:
        self.calls.append("ready")

    def stop_container(
        self, resource_id: str, ownership: Ownership, deadline_s: float
    ) -> None:
        self.calls.append("stop")

    def remove_container(
        self, resource_id: str, ownership: Ownership, deadline_s: float
    ) -> None:
        self.calls.append("remove_container")
        self.resources.discard(resource_id)

    def remove_network(
        self, resource_id: str, ownership: Ownership, deadline_s: float
    ) -> None:
        self.calls.append("remove_network")
        self.resources.discard(resource_id)

    def verify_absent(
        self,
        resource_ids: tuple[str, ...],
        ownership: Ownership,
        deadline_s: float,
    ) -> bool:
        self.calls.append("verify_absent")
        return self.cleanup_absent and not any(v in self.resources for v in resource_ids)


def planned(
    tmp_path: Path, *, trials: int = 3, profile_digest: str = PROFILE_DIGEST
) -> tuple[LabRunSpec, LifecyclePlan]:
    root = tmp_path / "models"
    model = root / "qwen"
    model.mkdir(parents=True)
    (model / "config.json").write_text("{}", encoding="utf-8")
    template = bind_builtin_template("vllm.openai", IMAGE)
    plan = plan_lab_run(
        template=template,
        overrides={"gpu_memory_utilization": 0.8},
        model_root=root,
        model_ref="qwen",
        benchmark_profile_digest=profile_digest,
        trial_count=trials,
        statistical_seed=17,
        telemetry_interval_s=2,
        telemetry_max_samples=20,
    )
    lifecycle = LifecyclePlan(
        run_id=plan.run_id,
        template_digest=plan.template_digest,
        image=plan.image,
        startup_timeout_s=30,
        run_timeout_s=120,
        cleanup_timeout_s=10,
    )
    return plan, lifecycle


def sealed_trial(endpoint: str, index: int, *, served: str = "qwen") -> dict[str, Any]:
    artifact: dict[str, Any] = {
        "schema_version": "serving-verdict.benchmark-run.v1",
        "run_id": f"svrun-{'a' * 30}{index:02d}",
        "phases": {
            "lifecycle": "SEALED",
            "sequence": [
                "PREFLIGHT",
                "WARMUP",
                "MEASURE",
                "CONCURRENCY",
                "QUALITY",
                "SEALED",
            ],
        },
        "run_status": "ok",
        "endpoint": {
            "id": "lab-runtime",
            "base_url": endpoint,
            "model": "qwen",
            "api_key_env": "LAB_API_KEY",
            "remote": False,
        },
        "model": {"requested": "qwen", "served": served, "matches_requested": True},
        "profile": {"name": "quick", "procedure_version": "quick-v1"},
        "protocol_hash": "sha256:" + "c" * 64,
        "workload_hash": "sha256:" + "d" * 64,
        "error_probe": {"expected_behavior_met": True},
        "warmup_requests": [],
        "requests": [],
        "aggregates": {
            "serial": {},
            "concurrency": [
                {
                    "size": 3,
                    "aggregate_output_tokens_per_s": 20.0 + index,
                    "request_ids": ["a", "b", "c"],
                }
            ],
            "requests": {"rate": 1.0},
        },
        "gates": {"quality_lite": {"passed": True}},
        "environment": {"python": "3.12", "implementation": "cpython", "platform": "linux"},
    }
    artifact["artifact_digest"] = compute_artifact_digest(artifact)
    return artifact


BINDINGS = (
    MetricBinding(
        "vllm:num_requests_running",
        "runtime.requests.running",
        "requests",
        "neutral",
        "vllm-prom-v1",
        ("engine",),
    ),
)


def test_success_runs_repeated_trials_scrapes_telemetry_and_seals_after_cleanup(
    tmp_path: Path,
) -> None:
    plan, lifecycle_plan = planned(tmp_path)
    backend = FakeLabBackend()
    trial_calls: list[tuple[str, int, float]] = []
    scrapes: list[str] = []

    def trial(endpoint: str, index: int, deadline: float) -> dict[str, Any]:
        trial_calls.append((endpoint, index, deadline))
        return sealed_trial(endpoint, index)

    def scrape(url: str, deadline: float) -> bytes:
        scrapes.append(url)
        return b'vllm:num_requests_running{engine="0",model_name="secret/raw"} 1\n'

    result = LabRunOrchestrator(
        lifecycle=LabLifecycle(backend, _resource_suffix="deadbeef"),
        endpoint=backend,
        trial_runner=trial,
        telemetry_fetcher=scrape,
        telemetry_bindings=BINDINGS,
    ).execute(plan, lifecycle_plan)

    assert result.state is LabState.SUCCEEDED
    assert result.error_kind is None
    assert result.artifact is not None
    assert result.artifact["cleanup"]["verified"] is True
    assert [call[1] for call in trial_calls] == [1, 2, 3]
    assert len(scrapes) >= 2
    assert len(result.artifact["benchmark_trials"]) == 3
    assert len(result.artifact["telemetry"]["samples"]) >= 2
    assert result.artifact["telemetry"]["samples"][0]["labels"] == [["engine", "0"]]
    summary = result.artifact["telemetry"]["summary"][0]
    assert summary == {
        "metric_id": "runtime.requests.running",
        "labels": [["engine", "0"]],
        "unit": "requests",
        "direction": "neutral",
        "count": len(result.artifact["telemetry"]["samples"]),
        "min": 1.0,
        "mean": 1.0,
        "p50": 1.0,
        "p95": 1.0,
        "p99": 1.0,
        "max": 1.0,
        "latest": 1.0,
    }
    serialized = repr(result.artifact)
    assert "secret/raw" not in serialized
    assert "/tmp/" not in serialized
    assert verify_lab_artifact(result.artifact) == result.artifact["artifact_digest"]
    assert backend.calls[-4:] == [
        "stop",
        "remove_container",
        "remove_network",
        "verify_absent",
    ]


def test_tampered_or_context_drift_trial_fails_and_publishes_no_artifact(
    tmp_path: Path,
) -> None:
    plan, lifecycle_plan = planned(tmp_path)
    backend = FakeLabBackend()

    def trial(endpoint: str, index: int, deadline: float) -> dict[str, Any]:
        artifact = sealed_trial(endpoint, index, served="other" if index == 2 else "qwen")
        if index == 3:
            artifact["run_status"] = "degraded"
        return artifact

    result = LabRunOrchestrator(
        lifecycle=LabLifecycle(backend, _resource_suffix="deadbeef"),
        endpoint=backend,
        trial_runner=trial,
        telemetry_fetcher=lambda _url, _deadline: b"",
        telemetry_bindings=BINDINGS,
    ).execute(plan, lifecycle_plan)
    assert result.state is LabState.FAILED
    assert result.error_kind == "work_failed"
    assert result.artifact is None
    assert result.cleanup_verified is True


def test_cleanup_failure_suppresses_otherwise_valid_artifact(tmp_path: Path) -> None:
    plan, lifecycle_plan = planned(tmp_path, trials=2)
    backend = FakeLabBackend(cleanup_absent=False)
    result = LabRunOrchestrator(
        lifecycle=LabLifecycle(backend, _resource_suffix="deadbeef"),
        endpoint=backend,
        trial_runner=lambda endpoint, index, _deadline: sealed_trial(endpoint, index),
        telemetry_fetcher=lambda _url, _deadline: b"",
        telemetry_bindings=BINDINGS,
    ).execute(plan, lifecycle_plan)
    assert result.state is LabState.CLEANUP_FAILED
    assert result.artifact is None
    assert result.cleanup_verified is False


def test_telemetry_failure_is_fixed_category_without_raw_error(tmp_path: Path) -> None:
    plan, lifecycle_plan = planned(tmp_path, trials=2)
    backend = FakeLabBackend()

    def fail_scrape(_url: str, _deadline: float) -> bytes:
        raise RuntimeError("Bearer raw-secret-sk-live")

    result = LabRunOrchestrator(
        lifecycle=LabLifecycle(backend, _resource_suffix="deadbeef"),
        endpoint=backend,
        trial_runner=lambda endpoint, index, _deadline: sealed_trial(endpoint, index),
        telemetry_fetcher=fail_scrape,
        telemetry_bindings=BINDINGS,
    ).execute(plan, lifecycle_plan)
    assert result.state is LabState.SUCCEEDED
    assert result.artifact is not None
    failures = result.artifact["telemetry"]["failures"]
    assert len(failures) >= 2
    assert all(item["status"] == "unavailable" for item in failures)
    assert "raw-secret" not in repr(result.artifact)


def test_telemetry_is_collected_while_a_trial_is_running(tmp_path: Path) -> None:
    plan, lifecycle_plan = planned(tmp_path, trials=2)
    backend = FakeLabBackend()
    scraped = Event()

    def trial(endpoint: str, index: int, _deadline: float) -> dict[str, Any]:
        assert scraped.wait(2.0), "telemetry did not start during benchmark execution"
        return sealed_trial(endpoint, index)

    def scrape(_url: str, _deadline: float) -> bytes:
        scraped.set()
        return b'vllm:num_requests_running{engine="0"} 1\n'

    result = LabRunOrchestrator(
        lifecycle=LabLifecycle(backend, _resource_suffix="deadbeef"),
        endpoint=backend,
        trial_runner=trial,
        telemetry_fetcher=scrape,
        telemetry_bindings=BINDINGS,
    ).execute(plan, lifecycle_plan)
    assert result.state is LabState.SUCCEEDED
    assert result.artifact is not None
    offsets = [item["offset_s"] for item in result.artifact["telemetry"]["samples"]]
    assert offsets == sorted(offsets)
    assert all(offset >= 0 for offset in offsets)


def test_extreme_finite_telemetry_summary_stays_finite(tmp_path: Path) -> None:
    plan, lifecycle_plan = planned(tmp_path, trials=2)
    backend = FakeLabBackend()
    values = iter((1.7e308, -1.7e308, 1.7e308, -1.7e308))

    def scrape(_url: str, _deadline: float) -> bytes:
        try:
            value = next(values)
        except StopIteration:
            value = -1.7e308
        return f'vllm:num_requests_running{{engine="0"}} {value}\n'.encode()

    result = LabRunOrchestrator(
        lifecycle=LabLifecycle(backend, _resource_suffix="deadbeef"),
        endpoint=backend,
        trial_runner=lambda endpoint, index, _deadline: sealed_trial(endpoint, index),
        telemetry_fetcher=scrape,
        telemetry_bindings=BINDINGS,
    ).execute(plan, lifecycle_plan)
    assert result.state is LabState.SUCCEEDED
    assert result.artifact is not None
    summary = result.artifact["telemetry"]["summary"][0]
    assert all(
        math.isfinite(summary[key])
        for key in ("min", "mean", "p50", "p95", "p99", "max", "latest")
    )


def test_duplicate_trial_artifact_is_not_repeated_evidence(tmp_path: Path) -> None:
    plan, lifecycle_plan = planned(tmp_path, trials=2)
    backend = FakeLabBackend()
    duplicate = sealed_trial(backend.endpoint_url, 1)
    result = LabRunOrchestrator(
        lifecycle=LabLifecycle(backend, _resource_suffix="deadbeef"),
        endpoint=backend,
        trial_runner=lambda _endpoint, _index, _deadline: deepcopy(duplicate),
        telemetry_fetcher=lambda _url, _deadline: b"",
        telemetry_bindings=BINDINGS,
    ).execute(plan, lifecycle_plan)
    assert result.state is LabState.FAILED
    assert result.artifact is None


def test_malformed_loopback_url_is_rejected_before_trials(tmp_path: Path) -> None:
    plan, lifecycle_plan = planned(tmp_path, trials=2)
    backend = FakeLabBackend()
    backend.endpoint_url = "http://127.0.0.1:99999/x\r\nHost:evil"
    calls = 0

    def trial(_endpoint: str, _index: int, _deadline: float) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return {}

    result = LabRunOrchestrator(
        lifecycle=LabLifecycle(backend, _resource_suffix="deadbeef"),
        endpoint=backend,
        trial_runner=trial,
        telemetry_fetcher=lambda _url, _deadline: b"",
        telemetry_bindings=BINDINGS,
    ).execute(plan, lifecycle_plan)
    assert result.state is LabState.FAILED
    assert result.artifact is None
    assert calls == 0


def test_final_telemetry_fetch_is_actually_deadline_bounded(tmp_path: Path) -> None:
    plan, lifecycle_plan = planned(tmp_path, trials=2)
    backend = FakeLabBackend()
    calls = 0
    blocker = Event()

    def scrape(_url: str, _deadline: float) -> bytes:
        nonlocal calls
        calls += 1
        if calls == 1:
            return b""
        blocker.wait(30.0)
        return b""

    started = time.monotonic()
    result = LabRunOrchestrator(
        lifecycle=LabLifecycle(backend, _resource_suffix="deadbeef"),
        endpoint=backend,
        trial_runner=lambda endpoint, index, _deadline: sealed_trial(endpoint, index),
        telemetry_fetcher=scrape,
        telemetry_bindings=BINDINGS,
    ).execute(plan, lifecycle_plan)
    elapsed = time.monotonic() - started
    assert elapsed < 5.0
    assert result.state is LabState.SUCCEEDED
    assert result.artifact is not None
    assert any(
        failure["status"] == "unavailable"
        for failure in result.artifact["telemetry"]["failures"]
    )


def test_plan_lifecycle_binding_and_final_tamper_are_fail_closed(tmp_path: Path) -> None:
    plan, lifecycle_plan = planned(tmp_path, trials=2)
    backend = FakeLabBackend()
    bad_lifecycle = LifecyclePlan(
        run_id="lab-" + "f" * 24,
        template_digest=lifecycle_plan.template_digest,
        image=lifecycle_plan.image,
        startup_timeout_s=30,
        run_timeout_s=120,
        cleanup_timeout_s=10,
    )
    orchestrator = LabRunOrchestrator(
        lifecycle=LabLifecycle(backend, _resource_suffix="deadbeef"),
        endpoint=backend,
        trial_runner=lambda endpoint, index, _deadline: sealed_trial(endpoint, index),
        telemetry_fetcher=lambda _url, _deadline: b"",
        telemetry_bindings=BINDINGS,
    )
    with pytest.raises(LabOrchestrationError, match="lifecycle plan"):
        orchestrator.execute(plan, bad_lifecycle)
    assert backend.calls == []

    wrong_plan, wrong_lifecycle = planned(
        tmp_path / "wrong-profile",
        trials=2,
        profile_digest="sha256:" + "e" * 64,
    )
    wrong_result = orchestrator.execute(wrong_plan, wrong_lifecycle)
    assert wrong_result.state is LabState.FAILED
    assert wrong_result.artifact is None

    result = orchestrator.execute(plan, lifecycle_plan)
    assert result.artifact is not None
    tampered = deepcopy(result.artifact)
    tampered["benchmark_trials"][0]["artifact_digest"] = "sha256:" + "0" * 64
    with pytest.raises(LabOrchestrationError, match="digest"):
        verify_lab_artifact(tampered)


def test_self_consistent_lab_manifest_forges_are_rejected(tmp_path: Path) -> None:
    plan, lifecycle_plan = planned(tmp_path, trials=2)
    backend = FakeLabBackend()
    result = LabRunOrchestrator(
        lifecycle=LabLifecycle(backend, _resource_suffix="deadbeef"),
        endpoint=backend,
        trial_runner=lambda endpoint, index, _deadline: sealed_trial(endpoint, index),
        telemetry_fetcher=lambda _url, _deadline: (
            b'vllm:num_requests_running{engine="0"} 1\n'
        ),
        telemetry_bindings=BINDINGS,
    ).execute(plan, lifecycle_plan)
    assert result.artifact is not None

    forged_spec = deepcopy(result.artifact)
    forged_spec["run_spec"]["trial_count"] = 1
    forged_spec["benchmark_trials"] = forged_spec["benchmark_trials"][:1]
    forged_spec["artifact_digest"] = compute_lab_artifact_digest(forged_spec)
    with pytest.raises(LabOrchestrationError, match="run spec identity"):
        verify_lab_artifact(forged_spec)

    forged_runtime = deepcopy(result.artifact)
    forged_runtime["runtime"]["engine"] = "sglang"
    forged_runtime["artifact_digest"] = compute_lab_artifact_digest(forged_runtime)
    with pytest.raises(LabOrchestrationError, match="runtime binding"):
        verify_lab_artifact(forged_runtime)

    forged_telemetry = deepcopy(result.artifact)
    forged_telemetry["telemetry"]["samples"][0]["status"] = "invented"
    forged_telemetry["artifact_digest"] = compute_lab_artifact_digest(
        forged_telemetry
    )
    with pytest.raises(LabOrchestrationError, match="telemetry sample"):
        verify_lab_artifact(forged_telemetry)
