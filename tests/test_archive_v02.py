"""v0.2 content-addressed artifact archive (RED first).

Contract:
- ``import-case --archive DIR`` (or the default ``<data-dir>/archive`` layout
  used by the CLI) copies each referenced evidence artifact into a
  content-addressed store ``objects/<2-hex>/<64-hex>`` after hashing; the
  copy is re-hashed and the digest must match (copy-after hash verify).
- Fail-closed: files over 20 MiB, symlinked sources, and paths that escape
  the store layout all abort the import with exit 2 (no partial manifest).
- A manifest ``artifacts.json`` is written next to the bundle (the bundle
  payload itself is UNCHANGED; bundle_digest is unaffected).
- ``verify BUNDLE.json --archive`` validates the manifest and re-hashes every
  stored object; it must still pass AFTER the original source files are
  deleted (archived verification).
- Missing manifest with --archive -> exit 2; tampered/missing store object ->
  exit 4 (IntegrityError).
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import yaml

from tests.helpers import make_dspark_ab_fixture, sha256_file
from tests.test_cli import run_cli


def _build_case(dirpath: Path, source_root: str | None = None) -> tuple[Path, dict]:
    base = make_dspark_ab_fixture(dirpath, filename="base.json", decode=25.62)
    cand = make_dspark_ab_fixture(dirpath, filename="cand.json", decode=63.27)
    report = dirpath / "REPORT.md"
    report.write_text("# gates\n- arithmetic pass\n", encoding="utf-8")
    doc = {
        "schema_version": "serving-verdict.case.v0.1",
        "id": "arch-case",
        "source_root": source_root or str(dirpath),
        "baseline": {"artifact": "base.json", "sha256": sha256_file(base)},
        "candidate": {"artifact": "cand.json", "sha256": sha256_file(cand)},
        "policy": {
            "primary_metric": "decode_tokens_per_s",
            "workload": "edit_cold",
            "min_relative_improvement": 0.15,
            "max_ttft_regression": 0.10,
            "required_gates": ["request_success", "arithmetic"],
        },
        "supplemental_evidence": [
            {
                "id": "arithmetic",
                "kind": "operator_attested",
                "status": "pass",
                "source": "REPORT.md",
                "sha256": sha256_file(report),
            }
        ],
        "claim_boundary": "archive test",
    }
    p = dirpath / "case.yaml"
    p.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return p, doc


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_import_archive_copies_and_verifies(tmp_path: Path) -> None:
    case, doc = _build_case(tmp_path)
    out = tmp_path / "bundle.json"
    archive = tmp_path / "archive"
    r = run_cli("import-case", str(case), "--out", str(out), "--archive", str(archive))
    assert r.returncode == 0, r.stderr
    # content-addressed layout: objects/<first two hex>/<full sha>
    for sha in (doc["baseline"]["sha256"], doc["candidate"]["sha256"]):
        obj = archive / "objects" / sha[:2] / sha
        assert obj.is_file(), sha
        assert _sha256_bytes(obj.read_bytes()) == sha, "stored object must match its own sha"
    # manifest next to the bundle
    manifest = json.loads((tmp_path / "artifacts.json").read_text())
    assert manifest["schema_version"] == "serving-verdict.artifacts.v0.1"
    assert set(manifest["artifacts"]) == {
        "base.json",
        "cand.json",
        "REPORT.md",
    }
    for entry in manifest["artifacts"].values():
        assert entry["path"] == f"objects/{entry['sha256'][:2]}/{entry['sha256']}"
    # the bundle itself is unchanged: still verifies without --archive
    assert run_cli("verify", str(out)).returncode == 0


def test_verify_archive_survives_source_deletion(tmp_path: Path) -> None:
    case, doc = _build_case(tmp_path)
    out = tmp_path / "bundle.json"
    archive = tmp_path / "archive"
    assert run_cli("import-case", str(case), "--out", str(out), "--archive", str(archive)).returncode == 0
    # delete ALL original sources
    for name in ("base.json", "cand.json", "REPORT.md"):
        (tmp_path / name).unlink()
    r = run_cli("verify", str(out), "--archive")
    assert r.returncode == 0, r.stderr
    body = json.loads(r.stdout) if r.stdout.strip() else None
    if body is not None:
        assert body["valid"] is True
        assert body["artifacts_verified"] == 3


def test_verify_archive_no_manifest_exit_2(tmp_path: Path) -> None:
    case, _ = _build_case(tmp_path)
    out = tmp_path / "bundle.json"
    assert run_cli("import-case", str(case), "--out", str(out)).returncode == 0
    r = run_cli("verify", str(out), "--archive")
    assert r.returncode == 2, "no manifest next to bundle -> usage error, not integrity"


def test_verify_archive_tampered_object_exit_4(tmp_path: Path) -> None:
    case, doc = _build_case(tmp_path)
    out = tmp_path / "bundle.json"
    archive = tmp_path / "archive"
    assert run_cli("import-case", str(case), "--out", str(out), "--archive", str(archive)).returncode == 0
    obj = archive / "objects" / doc["baseline"]["sha256"][:2] / doc["baseline"]["sha256"]
    obj.write_bytes(obj.read_bytes() + b"TAMPERED")
    r = run_cli("verify", str(out), "--archive")
    assert r.returncode == 4


def test_verify_archive_missing_object_exit_4(tmp_path: Path) -> None:
    case, doc = _build_case(tmp_path)
    out = tmp_path / "bundle.json"
    archive = tmp_path / "archive"
    assert run_cli("import-case", str(case), "--out", str(out), "--archive", str(archive)).returncode == 0
    obj = archive / "objects" / doc["baseline"]["sha256"][:2] / doc["baseline"]["sha256"]
    obj.unlink()
    r = run_cli("verify", str(out), "--archive")
    assert r.returncode == 4


def test_import_archive_rejects_symlink_source(tmp_path: Path) -> None:
    """A source file whose symlink target ESCAPES the source root is
    fail-closed: exit 2, no manifest, no store object, no bundle."""
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    real_file = real_dir / "base.json"
    real_file.write_text("not the bound hash", encoding="utf-8")
    link_dir = tmp_path / "linked"
    link_dir.mkdir()
    base = make_dspark_ab_fixture(link_dir, filename="base.json", decode=25.62)
    cand = make_dspark_ab_fixture(link_dir, filename="cand.json", decode=63.27)
    report = link_dir / "REPORT.md"
    report.write_text("# gates\n", encoding="utf-8")
    # overwrite the real base.json with a symlink escaping the case root
    base.unlink()
    (link_dir / "base.json").symlink_to(real_file)
    doc = {
        "schema_version": "serving-verdict.case.v0.1",
        "id": "arch-case",
        "source_root": str(link_dir),
        "baseline": {"artifact": "base.json", "sha256": sha256_file(real_file)},
        "candidate": {"artifact": "cand.json", "sha256": sha256_file(cand)},
        "policy": {
            "primary_metric": "decode_tokens_per_s",
            "workload": "edit_cold",
            "min_relative_improvement": 0.15,
            "max_ttft_regression": 0.10,
            "required_gates": ["request_success"],
        },
        "claim_boundary": "archive symlink escape",
    }
    case = link_dir / "case.yaml"
    case.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    out = tmp_path / "bundle.json"
    archive = tmp_path / "archive"
    r = run_cli("import-case", str(case), "--out", str(out), "--archive", str(archive))
    assert r.returncode == 2, r.stderr
    assert not (tmp_path / "artifacts.json").exists()
    assert not out.exists()


def test_archive_put_symlink_escape_fails_closed(tmp_path: Path) -> None:
    """ArchiveStore.put itself rejects symlinks whose targets escape the given
    base dir (defense in depth beyond the evidence loader)."""
    from serving_verdict.archive import ArchiveStore
    from serving_verdict.errors import ArchiveError

    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.bin"
    secret.write_bytes(b"secret")
    inside = tmp_path / "inside"
    inside.mkdir()
    link = inside / "link.bin"
    link.symlink_to(secret)
    store = ArchiveStore(tmp_path / "archive")
    with pytest.raises(ArchiveError):
        store.put(link, base_dir=inside)
    assert not store.root.joinpath("objects").exists() or not any(
        store.root.rglob("*")
    ), "no object may be stored on a failed put"


def test_import_archive_rejects_over_size_source(tmp_path: Path) -> None:
    real_dir = tmp_path / "big"
    real_dir.mkdir()
    case, doc = _build_case(real_dir)
    big = real_dir / "base.json"
    big.write_bytes(b"0" * (20 * 1024 * 1024 + 1))
    out = tmp_path / "bundle.json"
    archive = tmp_path / "archive"
    r = run_cli("import-case", str(case), "--out", str(out), "--archive", str(archive))
    assert r.returncode == 2
    assert not (tmp_path / "artifacts.json").exists()


def test_store_escape_rejected(tmp_path: Path) -> None:
    from serving_verdict.archive import ArchiveStore

    store = ArchiveStore(tmp_path / "archive")
    with pytest.raises(TypeError):
        store.put(tmp_path / "base.json" if (tmp_path / "base.json").exists() else None, "evil")
    # the layout helper itself refuses escape
    from serving_verdict.errors import ArchiveError

    with pytest.raises(ArchiveError):
        store.object_path("../../etc/passwd")
    with pytest.raises(ArchiveError):
        store.object_path("/etc/passwd")


def test_server_artifacts_endpoint_serves_store_object(tmp_path: Path) -> None:
    """GET /artifacts/{sha} serves a stored object; unknown sha -> 404."""
    from starlette.testclient import TestClient

    from serving_verdict.server import ONLY_BIND_HOST, create_app

    case, doc = _build_case(tmp_path)
    out = tmp_path / "bundle.json"
    archive = tmp_path / "archive"
    assert run_cli("import-case", str(case), "--out", str(out), "--archive", str(archive)).returncode == 0
    data = tmp_path / "data"
    data.mkdir()
    (data / "bundle.json").write_text(out.read_text(), encoding="utf-8")
    (data / "artifacts.json").write_text((tmp_path / "artifacts.json").read_text(), encoding="utf-8")
    # move store into the data dir layout
    shutil_move(archive, data / "archive")
    client = TestClient(create_app(ONLY_BIND_HOST, 0, data))
    sha = doc["baseline"]["sha256"]
    r = client.get(f"/api/v1/artifacts/{sha}")
    assert r.status_code == 200
    assert _sha256_bytes(r.content) == sha
    assert client.get("/api/v1/artifacts/" + "0" * 64).status_code == 404
    assert client.get("/api/v1/artifacts/not-hex").status_code == 404


def shutil_move(src: Path, dst: Path) -> None:
    import shutil

    shutil.move(str(src), str(dst))
