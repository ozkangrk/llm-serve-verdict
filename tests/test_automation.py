"""Automation Wizard backend contracts: ephemeral, bounded, secret-safe jobs."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from starlette.testclient import TestClient

from serving_verdict.endpoint import EndpointConfig
from serving_verdict.server import ONLY_BIND_HOST, create_app

SECRET = "automation-secret-must-never-leak"


def payload(base_url: str = "http://127.0.0.1:9999/v1") -> dict[str, str]:
    return {
        "schema_version": "serving-verdict.endpoint.v1",
        "id": "local-test",
        "base_url": base_url,
        "model": "test-model",
        "api_key_env": "SERVING_VERDICT_AUTOMATION_TEST_KEY",
    }


def wait_terminal(client: TestClient, job_id: str) -> dict:
    for _ in range(100):
        doc = client.get(f"/api/v1/automation/jobs/{job_id}").json()
        if doc["state"] in {"SUCCEEDED", "FAILED", "CANCELLED"}:
            return doc
        time.sleep(0.01)
    raise AssertionError("job did not terminate")


def test_capabilities_and_success_are_secret_free(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SERVING_VERDICT_AUTOMATION_TEST_KEY", SECRET)
    seen: list[tuple[EndpointConfig, str]] = []

    def runner(config: EndpointConfig, key: str) -> dict:
        seen.append((config, key))
        return {
            "schema_version": "serving-verdict.benchmark-run.v1",
            "run_id": "run-test",
            "run_status": "ok",
            "gates": {"quality_lite": {"passed": True}},
            "aggregates": {"requests": {"rate": 1.0}},
            "artifact_digest": "sha256:" + "a" * 64,
        }

    data = tmp_path / "data"
    data.mkdir()
    before = set(data.iterdir())
    client = TestClient(
        create_app(ONLY_BIND_HOST, 0, data, automation_runner=runner)
    )
    caps = client.get("/api/v1/automation/capabilities")
    assert caps.status_code == 200
    assert caps.json()["secret_source"] == "environment_only"
    started = client.post("/api/v1/automation/jobs", json=payload())
    assert started.status_code == 202, started.text
    done = wait_terminal(client, started.json()["job_id"])
    assert done["state"] == "SUCCEEDED"
    assert done["result"]["run_id"] == "run-test"
    assert seen and seen[0][1] == SECRET
    assert SECRET not in json.dumps(started.json())
    assert SECRET not in json.dumps(done)
    assert set(data.iterdir()) == before


def test_missing_env_remote_credentials_and_unknown_fields_rejected(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("SERVING_VERDICT_AUTOMATION_TEST_KEY", raising=False)
    data = tmp_path / "data"
    data.mkdir()
    client = TestClient(create_app(ONLY_BIND_HOST, 0, data, automation_runner=lambda c, k: {}))
    assert client.post("/api/v1/automation/jobs", json=payload()).status_code == 400
    assert client.post(
        "/api/v1/automation/jobs", json=payload("https://example.com/v1")
    ).status_code == 400
    assert client.post(
        "/api/v1/automation/jobs", json=payload("http://user:pass@127.0.0.1/v1")
    ).status_code == 400
    bad = {**payload(), "api_key": SECRET}
    body = client.post("/api/v1/automation/jobs", json=bad)
    assert body.status_code == 400
    assert SECRET not in body.text


def test_concurrent_job_rejected_and_cancel_discards_result(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("SERVING_VERDICT_AUTOMATION_TEST_KEY", SECRET)
    entered = threading.Event()
    release = threading.Event()

    def runner(_config: EndpointConfig, _key: str) -> dict:
        entered.set()
        release.wait(timeout=5)
        return {"run_id": "must-be-discarded"}

    data = tmp_path / "data"
    data.mkdir()
    client = TestClient(
        create_app(ONLY_BIND_HOST, 0, data, automation_runner=runner)
    )
    first = client.post("/api/v1/automation/jobs", json=payload())
    assert first.status_code == 202
    assert entered.wait(timeout=1)
    second = client.post("/api/v1/automation/jobs", json=payload())
    assert second.status_code == 409
    job_id = first.json()["job_id"]
    cancel = client.post(f"/api/v1/automation/jobs/{job_id}/cancel")
    assert cancel.status_code == 200
    assert cancel.json()["state"] == "CANCEL_REQUESTED"
    release.set()
    done = wait_terminal(client, job_id)
    assert done["state"] == "CANCELLED"
    assert "result" not in done


def test_unknown_job_is_404(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    client = TestClient(create_app(ONLY_BIND_HOST, 0, data, automation_runner=lambda c, k: {}))
    assert client.get("/api/v1/automation/jobs/nope").status_code == 404
    assert client.post("/api/v1/automation/jobs/nope/cancel").status_code == 404
