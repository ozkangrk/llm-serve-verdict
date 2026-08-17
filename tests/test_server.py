"""Server/UI tests: read-only API, loopback gate, lifecycle, offline UI (RED first).

Covers the MVP spec:
- GET /api/v1/health, /api/v1/verdicts, /api/v1/verdicts/{case_id}, /api/v1/metrics, /
- read-only APIs (no POST/PUT/PATCH/DELETE)
- strict loopback-only bind (no override)
- self-contained offline UI showing verdict, reason codes, comparable metrics,
  gate authority (machine_measured vs operator_attested), hashes, claim boundary
- server start/health/shutdown with ephemeral-port release (E2E subprocess)
"""
from __future__ import annotations

import json
import socket
import subprocess
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from serving_verdict import cli
from serving_verdict.engine import import_case
from serving_verdict.errors import UsageError
from serving_verdict.server import (
    ONLY_BIND_HOST,
    create_app,
)
from serving_verdict.web import web_root

ROOT = Path(__file__).resolve().parents[1]
DSKAB_FIXTURE = ROOT / "tests" / "fixtures" / "dspark" / "case.yaml"
SGLANG_FIXTURE = ROOT / "tests" / "fixtures" / "sglang" / "case.yaml"


@pytest.fixture()
def data_dir(tmp_path: Path) -> Path:
    data = tmp_path / "data"
    data.mkdir()
    for name, case in (
        ("d.json", DSKAB_FIXTURE),
        ("s.json", SGLANG_FIXTURE),
    ):
        bundle = import_case(str(case))
        (data / name).write_text(
            json.dumps(bundle, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    (data / "junk.json").write_text('{"hello": "world"}', encoding="utf-8")
    return data


@pytest.fixture()
def client(data_dir: Path) -> TestClient:
    return TestClient(create_app(ONLY_BIND_HOST, 0, data_dir))


# ---------------------------------------------------------------------------
# API contracts
# ---------------------------------------------------------------------------


def test_health(client: TestClient) -> None:
    r = client.get("/api/v1/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["schema_version"] == "serving-verdict.bundle.v0.1"
    assert body["bind_host"] == "127.0.0.1"


def test_verdicts_list(client: TestClient) -> None:
    r = client.get("/api/v1/verdicts")
    assert r.status_code == 200
    body = r.json()
    ids = {v["case_id"] for v in body["verdicts"]}
    assert ids == {"fixture-dspark", "fixture-sglang"}
    for v in body["verdicts"]:
        assert set(v) >= {"case_id", "file", "verdict", "reason_codes", "bundle_digest"}


def test_verdict_detail(client: TestClient) -> None:
    r = client.get("/api/v1/verdicts/fixture-dspark")
    assert r.status_code == 200
    doc = r.json()
    assert doc["verdict"] == "PROMOTE"
    # the API must confirm offline integrity of what it serves
    assert doc["integrity"] == {"valid": True}
    # metric authority: comparable metrics carry their dimensions
    assert doc["comparisons"]
    first = doc["comparisons"][0]
    assert first["dimensions"]["workload_id"] == "edit_cold"
    # gate authority is surfaced, not flattened
    gate_ids = {g["id"] for g in doc["gates"]}
    assert {"request_success", "arithmetic"} <= gate_ids
    authorities = {g["id"]: g["authority"] for g in doc["gates"]}
    assert authorities["request_success"] == "machine_measured"
    assert authorities["arithmetic"] == "operator_attested"
    # hashes present
    assert len(doc["baseline"]["sha256"]) == 64
    assert len(doc["candidate"]["sha256"]) == 64
    assert doc["bundle_digest"].startswith("sha256:")
    # claim boundary present
    assert doc["claim_boundary"]


def test_verdict_detail_404(client: TestClient) -> None:
    r = client.get("/api/v1/verdicts/nope")
    assert r.status_code == 404
    body = r.json()
    assert body["error"] == "case not found"
    # error bodies never leak file contents
    assert "fixture" not in json.dumps(body) or "nope" not in json.dumps(body)


def test_metrics_registry_endpoint(client: TestClient) -> None:
    r = client.get("/api/v1/metrics")
    assert r.status_code == 200
    body = r.json()
    assert {"decode_tokens_per_s", "ttft_s", "api_latency_s"} <= set(body["metrics"])
    entry = body["metrics"]["decode_tokens_per_s"]
    assert entry["direction"] == "higher_better"
    assert entry["unit"] == "tok/s"


def test_index_serves_offline_ui(client: TestClient) -> None:
    r = client.get("/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    html = r.text
    assert "<!doctype html" in html.lower()
    # self-contained: no external http(s) references
    for token in ("http://", "https://", "cdn.", "//fonts"):
        assert token not in html


def test_ui_static_asset_served(client: TestClient) -> None:
    r = client.get("/ui.js")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/javascript")


def test_read_only_no_mutation_apis(client: TestClient) -> None:
    for method, path in (
        ("POST", "/api/v1/verdicts"),
        ("PUT", "/api/v1/verdicts/fixture-dspark"),
        ("PATCH", "/api/v1/verdicts/fixture-dspark"),
        ("DELETE", "/api/v1/verdicts/fixture-dspark"),
        ("POST", "/"),
    ):
        r = client.request(method, path)
        assert r.status_code in (405, 404), (method, path, r.status_code)


def test_unknown_api_path_404(client: TestClient) -> None:
    assert client.get("/api/v1/nope").status_code == 404


# ---------------------------------------------------------------------------
# loopback gate
# ---------------------------------------------------------------------------


def test_create_app_rejects_non_loopback_host() -> None:
    with pytest.raises(UsageError):
        create_app("0.0.0.0", 8787, ROOT)


def test_create_app_rejects_ipv6_any_host() -> None:
    with pytest.raises(UsageError):
        create_app("::", 8787, ROOT)


def test_cli_serve_non_loopback_exit_2() -> None:
    assert cli.main(["serve", "--host", "0.0.0.0", "--port", "8787", "--data-dir", "."]) == 2


# ---------------------------------------------------------------------------
# frontend content contract (offline, self-contained)
# ---------------------------------------------------------------------------


def test_frontend_covers_all_verdict_states() -> None:
    root = web_root()
    js = (root / "ui.js").read_text(encoding="utf-8")
    html = (root / "index.html").read_text(encoding="utf-8")
    for token in ("PROMOTE", "REJECT", "INCONCLUSIVE"):
        assert token in js, f"UI must render {token}"
    # verdict-first text labels, not only color
    for token in ("verdict-label", "reason-codes", "gate-authority", "claim-boundary"):
        assert token in html, f"UI markup must contain {token}"
    # gate authority rendered explicitly
    for token in ("machine_measured", "operator_attested"):
        assert token in js, f"UI must render authority {token}"
    # hashes rendered
    assert "sha256" in js
    # no external network in JS either
    for token in ("fetch('http", "fetch(\"http", "XMLHttpRequest"):
        assert token not in js


# ---------------------------------------------------------------------------
# lifecycle E2E: start / health / shutdown / port release (ephemeral port)
# ---------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_http(url: str, timeout: float = 15.0) -> str:
    import urllib.request

    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                return resp.read().decode()
        except Exception as exc:  # noqa: BLE001 - retry loop
            last = str(exc)
            time.sleep(0.25)
    raise AssertionError(f"server did not come up at {url}: {last}")


def test_serve_ephemeral_port_lifecycle_and_cleanup(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    bundle = import_case(str(DSKAB_FIXTURE))
    (data / "d.json").write_text(json.dumps(bundle), encoding="utf-8")
    port = _free_port()
    proc = subprocess.Popen(
        [
            str(ROOT / ".venv" / "bin" / "serving-verdict"),
            "serve",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--data-dir",
            str(data),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(ROOT),
    )
    try:
        health = json.loads(_wait_http(f"http://127.0.0.1:{port}/api/v1/health"))
        assert health["status"] == "ok"
        listed = json.loads(_wait_http(f"http://127.0.0.1:{port}/api/v1/verdicts"))
        assert {v["case_id"] for v in listed["verdicts"]} == {"fixture-dspark"}
        index = _wait_http(f"http://127.0.0.1:{port}/")
        assert "<!doctype html" in index.lower()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
    # SIGTERM triggers uvicorn's graceful shutdown; exit 0 or -15 (SIGTERM)
    # both mean the process stopped rather than crashed mid-request.
    assert proc.returncode in (0, -15), (
        proc.stdout.read()[-400:] + proc.stderr.read()[-800:]
    )
    # port must be released: we can rebind it
    with socket.socket() as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", port))
        assert s.getsockname()[1] == port
