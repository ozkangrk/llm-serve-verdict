"""v0.2 server endpoints (RED first).

Contract (read-only, loopback-only preserved):
- GET /api/v1/ready  -> 200 {"status":"ready", database:{available,...}} when
  the data dir is readable; 503 {"status":"not_ready"} when it is not.
- GET /api/v1/trials -> current per-case state from the trial registry,
  augmented by on-disk bundle validity (file remains source of truth).
- GET /api/v1/trials/{case_id} -> trial state + append-only event history +
  current bundle with integrity; 404 unknown/missing; 422 tampered current
  bundle (same fail-closed rule as v0.1 /verdicts/{id}).
- GET /api/v1/artifacts/{sha} -> serves a manifest-listed store object whose
  bytes re-hash to the requested sha; 404 unknown/malformed sha; 422 when a
  listed object fails its hash check.
- No POST/PUT/PATCH/DELETE anywhere; unknown API paths -> 404.
- The server never writes: it opens the registry read-only.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from serving_verdict.engine import import_case
from serving_verdict.server import ONLY_BIND_HOST, create_app
from serving_verdict.trialstore import TrialStore
from tests.helpers import FIXTURES

DSKAB_FIXTURE = FIXTURES / "dspark" / "case.yaml"
SGLANG_FIXTURE = FIXTURES / "sglang" / "case.yaml"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture()
def data_dir(tmp_path: Path) -> Path:
    data = tmp_path / "data"
    data.mkdir()
    for name, case in (("d.json", DSKAB_FIXTURE), ("s.json", SGLANG_FIXTURE)):
        bundle = import_case(str(case))
        (data / name).write_text(
            json.dumps(bundle, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return data


@pytest.fixture()
def seeded(data_dir: Path) -> Path:
    TrialStore(data_dir).reindex()
    return data_dir


@pytest.fixture()
def client(data_dir: Path) -> TestClient:
    return TestClient(create_app(ONLY_BIND_HOST, 0, data_dir))


def test_ready_reports_database(seeded: Path, client: TestClient) -> None:
    r = client.get("/api/v1/ready")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ready"
    assert body["read_only"] is True
    db = body["database"]
    assert db["available"] is True
    assert db["user_version"] >= 1
    assert db["trials"] == 2
    assert db["events"] == 2


def test_ready_without_store_is_still_ready(data_dir: Path, client: TestClient) -> None:
    r = client.get("/api/v1/ready")
    assert r.status_code == 200
    assert r.json()["database"]["available"] is False


def test_ready_missing_data_dir_503(tmp_path: Path) -> None:
    c = TestClient(create_app(ONLY_BIND_HOST, 0, tmp_path / "missing"))
    r = c.get("/api/v1/ready")
    assert r.status_code == 503
    assert r.json()["status"] == "not_ready"


def test_trials_list_reflects_state(seeded: Path, client: TestClient) -> None:
    r = client.get("/api/v1/trials")
    assert r.status_code == 200
    body = r.json()
    by_id = {t["case_id"]: t for t in body["trials"]}
    assert set(by_id) == {"fixture-dspark", "fixture-sglang"}
    assert by_id["fixture-dspark"]["verdict"] == "PROMOTE"
    assert by_id["fixture-dspark"]["status"] == "valid"
    assert by_id["fixture-sglang"]["verdict"] == "REJECT"
    assert by_id["fixture-dspark"]["bundle_file"] == "d.json"
    assert by_id["fixture-dspark"]["events"] == 1


def test_trials_after_tamper_and_reindex(seeded: Path, client: TestClient) -> None:
    doc = json.loads((seeded / "s.json").read_text())
    doc["verdict"] = "PROMOTE"  # break the digest
    (seeded / "s.json").write_text(json.dumps(doc), encoding="utf-8")
    TrialStore(seeded).reindex()
    by_id = {t["case_id"]: t for t in client.get("/api/v1/trials").json()["trials"]}
    assert by_id["fixture-sglang"]["status"] == "invalid"
    assert by_id["fixture-dspark"]["status"] == "valid"


def test_trial_detail_with_history(seeded: Path, client: TestClient) -> None:
    r = client.get("/api/v1/trials/fixture-dspark")
    assert r.status_code == 200
    doc = r.json()
    assert doc["case_id"] == "fixture-dspark"
    assert doc["trial"]["status"] == "valid"
    assert doc["integrity"] == {"valid": True}
    assert doc["bundle"]["verdict"] == "PROMOTE"
    assert doc["events"]
    assert doc["events"][0]["bundle_digest"].startswith("sha256:")


def test_trial_detail_404(client: TestClient) -> None:
    r = client.get("/api/v1/trials/nope")
    assert r.status_code == 404
    assert "error" in r.json()


def test_trial_detail_tampered_current_bundle_422(seeded: Path, client: TestClient) -> None:
    doc = json.loads((seeded / "d.json").read_text())
    doc["verdict"] = "REJECT"
    (seeded / "d.json").write_text(json.dumps(doc), encoding="utf-8")
    r = client.get("/api/v1/trials/fixture-dspark")
    assert r.status_code == 422
    assert "integrity" in r.json()["error"].lower()


def test_artifact_serving_round_trip(tmp_path: Path) -> None:
    from tests.test_archive_v02 import _build_case, run_cli, shutil_move

    case, doc = _build_case(tmp_path)
    out = tmp_path / "bundle.json"
    archive = tmp_path / "archive"
    assert run_cli("import-case", str(case), "--out", str(out), "--archive", str(archive)).returncode == 0
    data = tmp_path / "data"
    data.mkdir()
    (data / "bundle.json").write_text(out.read_text(), encoding="utf-8")
    shutil_move(archive, data / "archive")
    (data / "artifacts.json").write_text((tmp_path / "artifacts.json").read_text(), encoding="utf-8")
    c = TestClient(create_app(ONLY_BIND_HOST, 0, data))
    sha = doc["baseline"]["sha256"]
    r = c.get(f"/api/v1/artifacts/{sha}")
    assert r.status_code == 200
    assert _sha256(r.content) == sha
    assert c.get("/api/v1/artifacts/" + "0" * 64).status_code == 404
    assert c.get("/api/v1/artifacts/not-hex").status_code == 404
    assert c.get("/api/v1/artifacts").status_code == 404


def test_artifact_tampered_object_422(tmp_path: Path) -> None:
    from tests.test_archive_v02 import _build_case, run_cli

    case, doc = _build_case(tmp_path)
    out = tmp_path / "bundle.json"
    archive = tmp_path / "archive"
    assert run_cli("import-case", str(case), "--out", str(out), "--archive", str(archive)).returncode == 0
    data = tmp_path / "data"
    data.mkdir()
    (data / "bundle.json").write_text(out.read_text(), encoding="utf-8")
    (data / "archive").mkdir()
    import shutil

    shutil.move(str(archive / "objects"), str(data / "archive" / "objects"))
    (data / "artifacts.json").write_text((tmp_path / "artifacts.json").read_text(), encoding="utf-8")
    sha = doc["baseline"]["sha256"]
    obj = data / "archive" / "objects" / sha[:2] / sha
    obj.write_bytes(obj.read_bytes() + b"X")
    c = TestClient(create_app(ONLY_BIND_HOST, 0, data))
    assert c.get(f"/api/v1/artifacts/{sha}").status_code == 422


def test_no_mutation_apis_v02(seeded: Path, client: TestClient) -> None:
    for method, path in (
        ("POST", "/api/v1/trials"),
        ("PUT", "/api/v1/trials/fixture-dspark"),
        ("DELETE", "/api/v1/trials/fixture-dspark"),
        ("POST", "/api/v1/artifacts/" + "0" * 64),
        ("POST", "/api/v1/ready"),
    ):
        r = client.request(method, path)
        assert r.status_code in (405, 404), (method, path, r.status_code)


def test_server_never_writes(data_dir: Path, seeded: Path, client: TestClient) -> None:
    """The served registry stays byte-identical across requests (read-only
    convention: the server must not create/modify the sqlite file)."""
    before = (seeded / "trial_store.sqlite3").read_bytes()
    client.get("/api/v1/trials")
    client.get("/api/v1/trials/fixture-dspark")
    client.get("/api/v1/ready")
    after = (seeded / "trial_store.sqlite3").read_bytes()
    # read-only connections may touch sqlite's wal/hot-journal metadata only on
    # writes; a pure read changes nothing.
    assert before == after
