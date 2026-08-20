from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import serving_verdict.ab_experiment as ab_module
from serving_verdict.ab_experiment import (
    ABExperimentError,
    ABExperimentSpec,
    load_and_verify_ab_experiment,
    run_ab_experiment,
    verify_ab_experiment,
    write_ab_experiment,
)
from serving_verdict.artifact import compute_artifact_digest
from serving_verdict.benchmark_runner import RunResult
from serving_verdict.canonical import canonicalize, digest_payload
from serving_verdict.endpoint import parse_endpoint_config
from serving_verdict.profile import QUICK_PROFILE


def endpoint(endpoint_id: str) -> Any:
    return parse_endpoint_config(
        {
            "schema_version": "serving-verdict.endpoint.v1",
            "id": endpoint_id,
            "base_url": f"http://127.0.0.1:8{1 if endpoint_id == 'base' else 2}/v1",
            "model": "Qwen/Qwen3-8B",
            "api_key_env": f"KEY_{endpoint_id.upper()}",
        }
    )


def sealed_run(endpoint_id: str, value: float, *, status: str = "ok") -> RunResult:
    port = 81 if endpoint_id == "base" else 82
    artifact: dict[str, Any] = {
        "schema_version": "serving-verdict.benchmark-run.v1",
        "run_id": f"svrun-{endpoint_id}-{value}",
        "phases": {"lifecycle": "SEALED", "sequence": ["SEALED"]},
        "run_status": status,
        "endpoint": {
            "schema_version": "serving-verdict.endpoint.v1",
            "id": endpoint_id,
            "base_url": f"http://127.0.0.1:{port}/v1",
            "model": "Qwen/Qwen3-8B",
            "api_key_env": f"KEY_{endpoint_id.upper()}",
            "remote": False,
        },
        "model": {
            "requested": "Qwen/Qwen3-8B",
            "served": "Qwen/Qwen3-8B",
            "matches_requested": True,
        },
        "profile": {
            "name": "quick",
            "procedure_version": QUICK_PROFILE.procedure_version,
        },
        "protocol_hash": "sha256:protocol",
        "workload_hash": "sha256:workload",
        "aggregates": {
            "concurrency": [{"aggregate_output_tokens_per_s": value}],
        },
        "gates": {
            "quality_lite": {"passed": status == "ok"},
        },
    }
    artifact["artifact_digest"] = compute_artifact_digest(artifact)
    return RunResult(
        artifact=artifact,
        summary={
            "run_id": artifact["run_id"],
            "run_status": status,
            "artifact_digest": artifact["artifact_digest"],
        },
    )


def test_alternates_order_and_promotes_from_repeated_trials() -> None:
    calls: list[str] = []
    values = {
        "base": iter((100.0, 101.0, 99.0)),
        "candidate": iter((120.0, 121.0, 119.0)),
    }

    def runner(config: Any, **_kwargs: Any) -> RunResult:
        calls.append(config.endpoint_id)
        return sealed_run(config.endpoint_id, next(values[config.endpoint_id]))

    result = run_ab_experiment(
        endpoint("base"),
        endpoint("candidate"),
        baseline_api_key="base-secret",
        candidate_api_key="candidate-secret",
        profile=QUICK_PROFILE,
        spec=ABExperimentSpec(
            trials=3,
            confidence_level=0.95,
            iterations=1000,
            seed=7,
            threshold=0.05,
        ),
        runner=runner,
        created_at="2026-08-20T20:00:00+00:00",
    )

    assert calls == ["base", "candidate", "candidate", "base", "base", "candidate"]
    assert result.manifest["decision"] == {
        "verdict": "PROMOTE",
        "reason_codes": ["PROMOTE_ELIGIBLE"],
    }
    assert result.manifest["samples"] == {
        "baseline": [100.0, 101.0, 99.0],
        "candidate": [120.0, 121.0, 119.0],
    }
    assert result.manifest["execution_order"] == [
        ["baseline", "candidate"],
        ["candidate", "baseline"],
        ["baseline", "candidate"],
    ]
    assert "base-secret" not in json.dumps(result.manifest)
    assert "candidate-secret" not in json.dumps(result.manifest)
    assert verify_ab_experiment(
        result.manifest, result.run_artifacts, result.statistics_artifact
    )["valid"] is True


def test_candidate_hard_gate_overrides_statistical_speedup() -> None:
    values = {"base": iter((100.0, 100.0)), "candidate": iter((200.0, 200.0))}

    def runner(config: Any, **_kwargs: Any) -> RunResult:
        status = "degraded" if config.endpoint_id == "candidate" else "ok"
        return sealed_run(config.endpoint_id, next(values[config.endpoint_id]), status=status)

    result = run_ab_experiment(
        endpoint("base"), endpoint("candidate"),
        baseline_api_key="a", candidate_api_key="b", profile=QUICK_PROFILE,
        spec=ABExperimentSpec(2, 0.95, 1000, 1, 0.05), runner=runner,
        created_at="2026-08-20T20:00:00+00:00",
    )
    assert result.manifest["decision"] == {
        "verdict": "REJECT",
        "reason_codes": ["CANDIDATE_HARD_GATE_FAILED"],
    }


def test_degraded_baseline_is_inconclusive() -> None:
    values = {"base": iter((100.0, 100.0)), "candidate": iter((120.0, 120.0))}

    def runner(config: Any, **_kwargs: Any) -> RunResult:
        status = "degraded" if config.endpoint_id == "base" else "ok"
        return sealed_run(config.endpoint_id, next(values[config.endpoint_id]), status=status)

    result = run_ab_experiment(
        endpoint("base"), endpoint("candidate"),
        baseline_api_key="a", candidate_api_key="b", profile=QUICK_PROFILE,
        spec=ABExperimentSpec(2, 0.95, 1000, 1, 0.05), runner=runner,
        created_at="2026-08-20T20:00:00+00:00",
    )
    assert result.manifest["decision"] == {
        "verdict": "INCONCLUSIVE",
        "reason_codes": ["BASELINE_HARD_GATE_FAILED"],
    }


def test_write_is_atomic_and_tamper_is_detected(tmp_path: Path) -> None:
    values = {"base": iter((100.0, 100.0)), "candidate": iter((120.0, 120.0))}

    def runner(config: Any, **_kwargs: Any) -> RunResult:
        return sealed_run(config.endpoint_id, next(values[config.endpoint_id]))

    result = run_ab_experiment(
        endpoint("base"), endpoint("candidate"),
        baseline_api_key="a", candidate_api_key="b", profile=QUICK_PROFILE,
        spec=ABExperimentSpec(2, 0.95, 1000, 1, 0.05), runner=runner,
        created_at="2026-08-20T20:00:00+00:00",
    )
    out = tmp_path / "experiment"
    write_ab_experiment(result, out)
    assert sorted(p.name for p in out.iterdir()) == [
        "baseline-trial-001.json",
        "baseline-trial-002.json",
        "candidate-trial-001.json",
        "candidate-trial-002.json",
        "experiment.json",
        "statistics.json",
    ]
    manifest = json.loads((out / "experiment.json").read_text())
    artifacts = {
        p.name: json.loads(p.read_text())
        for p in out.glob("*-trial-*.json")
    }
    statistics = json.loads((out / "statistics.json").read_text())
    assert verify_ab_experiment(manifest, artifacts, statistics)["valid"] is True

    artifacts["candidate-trial-001.json"]["run_status"] = "degraded"
    with pytest.raises(ABExperimentError, match="benchmark artifact"):
        verify_ab_experiment(manifest, artifacts, statistics)


def test_directory_loader_rejects_extra_directory_and_symlink(tmp_path: Path) -> None:
    result = completed_result()
    out = tmp_path / "experiment"
    write_ab_experiment(result, out)
    extra = out / "unexpected"
    extra.mkdir()
    with pytest.raises(ABExperimentError, match="missing or extra"):
        load_and_verify_ab_experiment(out)
    extra.rmdir()

    target = out / "baseline-trial-001.json"
    original = out / "original.json"
    target.rename(original)
    target.symlink_to(original.name)
    with pytest.raises(ABExperimentError, match="regular file"):
        load_and_verify_ab_experiment(out)


def test_directory_loader_enforces_actual_byte_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = completed_result()
    out = tmp_path / "experiment"
    write_ab_experiment(result, out)
    monkeypatch.setattr(ab_module, "_MAX_JSON_BYTES", 32)
    with pytest.raises(ABExperimentError, match="size"):
        load_and_verify_ab_experiment(out)


def test_rejects_invalid_spec_and_incomparable_models() -> None:
    with pytest.raises(ABExperimentError, match="trials"):
        ABExperimentSpec(1, 0.95, 1000, 1, 0.05)
    with pytest.raises(ABExperimentError, match="confidence_level"):
        ABExperimentSpec(2, 1.0, 1000, 1, 0.05)
    with pytest.raises(ABExperimentError, match="seed"):
        ABExperimentSpec(2, 0.95, 1000, -1, 0.05)
    other = parse_endpoint_config(
        {
            "schema_version": "serving-verdict.endpoint.v1",
            "id": "other",
            "base_url": "http://127.0.0.1:83/v1",
            "model": "different-model",
            "api_key_env": "KEY_OTHER",
        }
    )
    with pytest.raises(ABExperimentError, match="same requested model"):
        run_ab_experiment(
            endpoint("base"), other,
            baseline_api_key="a", candidate_api_key="b", profile=QUICK_PROFILE,
            spec=ABExperimentSpec(2, 0.95, 1000, 1, 0.05),
            runner=lambda *_args, **_kwargs: sealed_run("base", 1.0),
            created_at="2026-08-20T20:00:00+00:00",
        )


def reseal_manifest(manifest: dict[str, Any]) -> None:
    payload = {
        key: value
        for key, value in manifest.items()
        if key not in {"created_at", "artifact_digest"}
    }
    manifest["artifact_digest"] = digest_payload(canonicalize(payload))


def completed_result() -> Any:
    values = {"base": iter((100.0, 100.0)), "candidate": iter((120.0, 120.0))}

    def runner(config: Any, **_kwargs: Any) -> RunResult:
        return sealed_run(config.endpoint_id, next(values[config.endpoint_id]))

    return run_ab_experiment(
        endpoint("base"), endpoint("candidate"),
        baseline_api_key="a", candidate_api_key="b", profile=QUICK_PROFILE,
        spec=ABExperimentSpec(2, 0.95, 1000, 1, 0.05), runner=runner,
        created_at="2026-08-20T20:00:00+00:00",
    )


def test_verifier_binds_samples_to_referenced_runs() -> None:
    result = completed_result()
    forged = json.loads(json.dumps(result.manifest))
    forged["samples"]["candidate"][0] = 999.0
    reseal_manifest(forged)
    with pytest.raises(ABExperimentError, match="samples"):
        verify_ab_experiment(forged, result.run_artifacts, result.statistics_artifact)


def test_verifier_rejects_self_consistent_schema_drift() -> None:
    result = completed_result()
    forged = json.loads(json.dumps(result.manifest))
    forged["extra"] = "field"
    reseal_manifest(forged)
    with pytest.raises(ABExperimentError, match="shape"):
        verify_ab_experiment(forged, result.run_artifacts, result.statistics_artifact)

    forged = json.loads(json.dumps(result.manifest))
    forged["runs"]["baseline"][0]["extra"] = True
    reseal_manifest(forged)
    with pytest.raises(ABExperimentError, match="reference"):
        verify_ab_experiment(forged, result.run_artifacts, result.statistics_artifact)


def test_verifier_requires_statistics_for_numeric_samples() -> None:
    result = completed_result()
    forged = json.loads(json.dumps(result.manifest))
    forged["statistics"] = None
    forged["decision"] = {
        "verdict": "INCONCLUSIVE",
        "reason_codes": ["METRIC_UNMEASURABLE"],
    }
    reseal_manifest(forged)
    with pytest.raises(ABExperimentError, match="statistics artifact is required"):
        verify_ab_experiment(forged, result.run_artifacts, None)


def test_verifier_binds_manifest_endpoint_and_profile_to_runs() -> None:
    result = completed_result()
    forged = json.loads(json.dumps(result.manifest))
    forged["baseline"]["base_url"] = "http://127.0.0.1:9999/v1"
    reseal_manifest(forged)
    with pytest.raises(ABExperimentError, match="endpoint identity"):
        verify_ab_experiment(forged, result.run_artifacts, result.statistics_artifact)

    forged = json.loads(json.dumps(result.manifest))
    forged["profile"]["procedure_version"] = "forged"
    reseal_manifest(forged)
    with pytest.raises(ABExperimentError, match="profile"):
        verify_ab_experiment(forged, result.run_artifacts, result.statistics_artifact)


def test_verifier_rejects_path_like_run_reference() -> None:
    result = completed_result()
    forged = json.loads(json.dumps(result.manifest))
    old = forged["runs"]["baseline"][0]["file"]
    forged["runs"]["baseline"][0]["file"] = "../baseline-trial-001.json"
    artifacts = dict(result.run_artifacts)
    artifacts["../baseline-trial-001.json"] = artifacts.pop(old)
    reseal_manifest(forged)
    with pytest.raises(ABExperimentError, match="filename"):
        verify_ab_experiment(forged, artifacts, result.statistics_artifact)


def test_rejects_protocol_drift_and_unknown_status() -> None:
    def drift_runner(config: Any, **_kwargs: Any) -> RunResult:
        run = sealed_run(config.endpoint_id, 100.0)
        if config.endpoint_id == "candidate":
            run.artifact["protocol_hash"] = "sha256:different"
            run.artifact["artifact_digest"] = compute_artifact_digest(run.artifact)
        return run

    with pytest.raises(ABExperimentError, match="protocol/workload/model context"):
        run_ab_experiment(
            endpoint("base"), endpoint("candidate"),
            baseline_api_key="a", candidate_api_key="b", profile=QUICK_PROFILE,
            spec=ABExperimentSpec(2, 0.95, 1000, 1, 0.05), runner=drift_runner,
            created_at="2026-08-20T20:00:00+00:00",
        )

    def status_runner(config: Any, **_kwargs: Any) -> RunResult:
        return sealed_run(config.endpoint_id, 100.0, status="mystery")

    with pytest.raises(ABExperimentError, match="run_status"):
        run_ab_experiment(
            endpoint("base"), endpoint("candidate"),
            baseline_api_key="a", candidate_api_key="b", profile=QUICK_PROFILE,
            spec=ABExperimentSpec(2, 0.95, 1000, 1, 0.05), runner=status_runner,
            created_at="2026-08-20T20:00:00+00:00",
        )


def test_rejects_api_key_value_in_runner_artifact() -> None:
    def leaky_runner(config: Any, api_key: str, **_kwargs: Any) -> RunResult:
        run = sealed_run(config.endpoint_id, 100.0)
        run.artifact["leak"] = api_key
        run.artifact["artifact_digest"] = compute_artifact_digest(run.artifact)
        return run

    with pytest.raises(ABExperimentError, match="secret value"):
        run_ab_experiment(
            endpoint("base"), endpoint("candidate"),
            baseline_api_key="base-secret", candidate_api_key="candidate-secret",
            profile=QUICK_PROFILE,
            spec=ABExperimentSpec(2, 0.95, 1000, 1, 0.05), runner=leaky_runner,
            created_at="2026-08-20T20:00:00+00:00",
        )
