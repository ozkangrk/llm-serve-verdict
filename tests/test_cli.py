"""CLI tests: commands, exit codes, single-JSON stdout contract (RED first)."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from serving_verdict import cli

ROOT = Path(__file__).resolve().parents[1]
DSKAB_FIXTURE = ROOT / "tests" / "fixtures" / "dspark" / "case.yaml"
SGLANG_FIXTURE = ROOT / "tests" / "fixtures" / "sglang" / "case.yaml"


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(ROOT / ".venv" / "bin" / "serving-verdict"), *args],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )


def test_import_promote_exit_0_writes_bundle(tmp_path: Path) -> None:
    out = tmp_path / "d.verdict.json"
    r = run_cli("import-case", str(DSKAB_FIXTURE), "--out", str(out))
    assert r.returncode == 0, r.stderr
    doc = json.loads(out.read_text())
    assert doc["verdict"] == "PROMOTE"
    assert doc["case_id"] == "fixture-dspark"


def test_import_reject_exit_0(tmp_path: Path) -> None:
    out = tmp_path / "s.verdict.json"
    r = run_cli("import-case", str(SGLANG_FIXTURE), "--out", str(out))
    assert r.returncode == 0, r.stderr
    doc = json.loads(out.read_text())
    assert doc["verdict"] == "REJECT"


def test_import_json_mode_single_object(tmp_path: Path) -> None:
    out = tmp_path / "x.json"
    r = run_cli("import-case", str(DSKAB_FIXTURE), "--out", str(out), "--json")
    assert r.returncode == 0, r.stderr
    # exactly one JSON object on stdout, and nothing after it
    decoder = json.JSONDecoder()
    obj, end = decoder.raw_decode(r.stdout)
    assert obj is not None
    assert r.stdout[end:].strip() == ""
    assert obj["verdict"] == "PROMOTE"
    assert r.stdout.strip().startswith("{")
    assert r.stdout.strip().endswith("}")


def test_import_inconclusive_exit_0(tmp_path: Path) -> None:
    """Hash-mismatch case still produces a valid INCONCLUSIVE bundle (exit 0)."""
    import yaml

    case_src = yaml.safe_load(DSKAB_FIXTURE.read_text())
    case_src["candidate"]["sha256"] = "0" * 64
    p = tmp_path / "badcase.yaml"
    p.write_text(yaml.safe_dump(case_src), encoding="utf-8")
    out = tmp_path / "bad.verdict.json"
    r = run_cli("import-case", str(p), "--out", str(out))
    assert r.returncode == 0, r.stderr
    doc = json.loads(out.read_text())
    assert doc["verdict"] == "INCONCLUSIVE"
    assert "EVIDENCE_HASH_MISMATCH" in doc["reason_codes"]


def test_import_bad_config_exit_2(tmp_path: Path) -> None:
    p = tmp_path / "bad.yaml"
    p.write_text("schema_version: nope\n", encoding="utf-8")
    r = run_cli("import-case", str(p), "--out", str(tmp_path / "o.json"))
    assert r.returncode == 2
    assert r.stdout.strip() == ""  # diagnostics go to stderr


def test_import_missing_case_exit_2(tmp_path: Path) -> None:
    r = run_cli("import-case", str(tmp_path / "nope.yaml"), "--out", str(tmp_path / "o.json"))
    assert r.returncode == 2


def test_verify_valid_exit_0(tmp_path: Path) -> None:
    out = tmp_path / "d.json"
    assert run_cli("import-case", str(DSKAB_FIXTURE), "--out", str(out)).returncode == 0
    r = run_cli("verify", str(out))
    assert r.returncode == 0, r.stderr


def test_verify_tampered_exit_4(tmp_path: Path) -> None:
    out = tmp_path / "d.json"
    assert run_cli("import-case", str(DSKAB_FIXTURE), "--out", str(out)).returncode == 0
    doc = json.loads(out.read_text())
    doc["verdict"] = "REJECT"
    out.write_text(json.dumps(doc), encoding="utf-8")
    r = run_cli("verify", str(out))
    assert r.returncode == 4
    assert r.stdout.strip() == ""
    assert "digest" in r.stderr.lower()


def test_verify_missing_file_exit_2(tmp_path: Path) -> None:
    r = run_cli("verify", str(tmp_path / "nope.json"))
    assert r.returncode == 2


def test_list_and_show(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    for name, case in (("d.json", DSKAB_FIXTURE), ("s.json", SGLANG_FIXTURE)):
        assert (
            run_cli("import-case", str(case), "--out", str(data / name)).returncode == 0
        )
    r = run_cli("list", str(data))
    assert r.returncode == 0, r.stderr
    listed = json.loads(r.stdout)
    ids = {v["case_id"] for v in listed["verdicts"]}
    assert ids == {"fixture-dspark", "fixture-sglang"}
    rd = run_cli("list", str(data), "--json")
    assert rd.returncode == 0
    assert json.loads(rd.stdout) == listed

    rs = run_cli("show", str(data / "d.json"))
    assert rs.returncode == 0
    assert json.loads(rs.stdout)["case_id"] == "fixture-dspark"
    r404 = run_cli("show", str(tmp_path / "missing.json"))
    assert r404.returncode == 2


def test_list_ignores_non_bundle_json(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    (data / "not-a-bundle.json").write_text('{"hello": "world"}', encoding="utf-8")
    r = run_cli("list", str(data))
    assert r.returncode == 0
    assert json.loads(r.stdout)["verdicts"] == []


def test_list_hides_tampered_bundle(tmp_path: Path) -> None:
    """F3: list must only index bundles that pass verify_bundle (digest recompute).
    A hand-edited verdict (PROMOTE -> REJECT) must not appear in `list` output."""
    data = tmp_path / "data"
    data.mkdir()
    good = tmp_path / "good.json"
    assert run_cli("import-case", str(DSKAB_FIXTURE), "--out", str(good)).returncode == 0
    (data / "good.json").write_text(good.read_text(encoding="utf-8"), encoding="utf-8")
    (data / "bad.json").write_text(good.read_text(encoding="utf-8"), encoding="utf-8")
    doc = json.loads((data / "bad.json").read_text(encoding="utf-8"))
    doc["verdict"] = "REJECT"  # hand edit breaks the digest
    (data / "bad.json").write_text(json.dumps(doc), encoding="utf-8")

    r = run_cli("list", str(data))
    assert r.returncode == 0, r.stderr
    listed = {v["file"] for v in json.loads(r.stdout)["verdicts"]}
    assert listed == {"good.json"}  # tampered bundle is hidden


def test_api_list_hides_tampered_bundle(tmp_path: Path) -> None:
    """F3: the HTTP index must not list tampered bundles either (server parity with CLI)."""
    from starlette.testclient import TestClient

    from serving_verdict.server import ONLY_BIND_HOST, create_app

    data = tmp_path / "data"
    data.mkdir()
    good = tmp_path / "good.json"
    assert run_cli("import-case", str(DSKAB_FIXTURE), "--out", str(good)).returncode == 0
    (data / "good.json").write_text(good.read_text(encoding="utf-8"), encoding="utf-8")
    (data / "bad.json").write_text(good.read_text(encoding="utf-8"), encoding="utf-8")
    doc = json.loads((data / "bad.json").read_text(encoding="utf-8"))
    doc["verdict"] = "REJECT"
    (data / "bad.json").write_text(json.dumps(doc), encoding="utf-8")

    client = TestClient(create_app(ONLY_BIND_HOST, 0, data))
    listed = client.get("/api/v1/verdicts").json()
    assert [v["file"] for v in listed["verdicts"]] == ["good.json"]
    # tampered detail stays a 422 (integrity failure), not a served document
    r = client.get("/api/v1/verdicts/fixture-dspark")
    assert r.status_code == 422


# ---------------------------------------------------------------- argparse level


def test_serve_non_loopback_rejected() -> None:
    assert cli.main(["serve", "--host", "0.0.0.0", "--port", "8787", "--data-dir", "data"]) == 2


def test_main_import_bad_out_dir_exit_2(tmp_path: Path) -> None:
    """import-case never creates the output parent directory: exit 2, no file."""
    missing_parent = tmp_path / "no-such-dir" / "x.json"
    assert cli.main(["import-case", str(DSKAB_FIXTURE), "--out", str(missing_parent)]) == 2
    assert not missing_parent.exists()
    assert not (tmp_path / "no-such-dir").exists()


def test_main_import_existing_dir_ok(tmp_path: Path) -> None:
    out = tmp_path / "ok.json"  # parent exists
    assert cli.main(["import-case", str(DSKAB_FIXTURE), "--out", str(out)]) == 0
    assert out.is_file()


def test_main_no_command_exits_2() -> None:
    with pytest.raises(SystemExit) as ei:
        cli.main([])
    assert ei.value.code == 2
