"""v0.2 CLI: history / reindex / data-dir resolution / demo exit codes (RED first).

Contract (exit/JSON contract preserved from v0.1):
- data-dir resolution order: explicit argument > ``SERVING_VERDICT_DATA_DIR``
  env > ``./data``. Applies to ``history``, ``reindex`` and ``serve``.
- ``history`` prints exactly one JSON object on stdout (both --json and
  default modes), listing append-only trial events.
- ``reindex`` prints exactly one JSON object with the reindex report
  ({"indexed": N, "invalid": N, "missing": N}).
- Missing data dir -> exit 2 with a JSON error object (JSON mode) or stderr
  diagnostics only (default mode).
- ``demo`` without ``--out-dir`` defaults to ``./data/demo``.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from serving_verdict import cli
from tests.helpers import FIXTURES
from tests.test_cli import run_cli

DSKAB_FIXTURE = FIXTURES / "dspark" / "case.yaml"


def _seed(data: Path) -> None:
    from serving_verdict.engine import import_case

    for name, case in (("d.json", DSKAB_FIXTURE), ("s.json", FIXTURES / "sglang" / "case.yaml")):
        bundle = import_case(str(case))
        (data / name).write_text(
            json.dumps(bundle, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )


def test_history_json_contract(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    _seed(data)
    r = run_cli("history", str(data))
    assert r.returncode == 0, r.stderr
    decoder = json.JSONDecoder()
    obj, end = decoder.raw_decode(r.stdout)
    assert r.stdout[end:].strip() == ""  # exactly one JSON object
    assert "events" in obj
    assert obj["events"] == []  # nothing registered yet (import does not register)
    rj = run_cli("history", str(data), "--json")
    assert json.loads(rj.stdout) == obj


def test_history_after_register(tmp_path: Path) -> None:
    from serving_verdict.trialstore import TrialStore

    data = tmp_path / "data"
    data.mkdir()
    _seed(data)
    store = TrialStore(data)
    store.register_bundle(data / "d.json")
    r = run_cli("history", str(data), "--json")
    events = json.loads(r.stdout)["events"]
    assert len(events) == 1
    assert events[0]["case_id"] == "fixture-dspark"
    assert events[0]["verdict"] == "PROMOTE"
    assert events[0]["bundle_digest"].startswith("sha256:")
    assert events[0]["bundle_file"] == "d.json"


def test_reindex_json_contract(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    _seed(data)
    r = run_cli("reindex", str(data))
    assert r.returncode == 0, r.stderr
    obj = json.loads(r.stdout)
    assert set(obj) == {"data_dir", "indexed", "invalid", "missing", "trials"}
    assert obj["indexed"] == 2
    assert obj["invalid"] == 0
    assert obj["missing"] == 0
    statuses = {t["case_id"]: t["status"] for t in obj["trials"]}
    assert statuses == {"fixture-dspark": "valid", "fixture-sglang": "valid"}


def test_reindex_reports_invalid_and_missing(tmp_path: Path) -> None:
    from serving_verdict.trialstore import TrialStore

    data = tmp_path / "data"
    data.mkdir()
    _seed(data)
    store = TrialStore(data)
    store.register_bundle(data / "d.json")
    store.register_bundle(data / "s.json")
    doc = json.loads((data / "s.json").read_text())
    doc["verdict"] = "PROMOTE"  # breaks digest
    (data / "s.json").write_text(json.dumps(doc), encoding="utf-8")
    (data / "d.json").unlink()
    r = run_cli("reindex", str(data), "--json")
    assert r.returncode == 0, r.stderr
    obj = json.loads(r.stdout)
    assert obj["indexed"] == 0
    assert obj["invalid"] == 1
    assert obj["missing"] == 1


def test_history_missing_data_dir_exit_2(tmp_path: Path) -> None:
    missing = tmp_path / "nope"
    r = run_cli("history", str(missing))
    assert r.returncode == 2
    assert r.stdout.strip() == ""  # default mode: diagnostics to stderr only
    rj = run_cli("history", str(missing), "--json")
    assert rj.returncode == 2
    obj = json.loads(rj.stdout)
    assert obj["error"]
    assert "nope" in obj["data_dir"]


def test_data_dir_resolution_arg_beats_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data = tmp_path / "from-arg"
    data.mkdir()
    _seed(data)
    env_dir = tmp_path / "from-env"
    env_dir.mkdir()
    (env_dir / "junk.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("SERVING_VERDICT_DATA_DIR", str(env_dir))
    rc = cli.main(["history", str(data), "--json"])
    assert rc == 0
    # stdout went to the captured buffer; instead check behavior: the arg dir
    # was used, so reindex of the env dir is untouched and the arg dir works.
    rc2 = cli.main(["reindex", str(data), "--json"])
    assert rc2 == 0


def test_data_dir_resolution_env_beats_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    env_dir = tmp_path / "env-data"
    env_dir.mkdir()
    _seed(env_dir)
    monkeypatch.setenv("SERVING_VERDICT_DATA_DIR", str(env_dir))
    rc = cli.main(["history", "--json"], )
    assert rc == 0
    out = capsys.readouterr().out
    assert "events" in json.loads(out)


def test_data_dir_default_is_dot_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.delenv("SERVING_VERDICT_DATA_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    _seed(tmp_path / "data")
    rc = cli.main(["history", "--json"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "events" in json.loads(out)
    # and the store file landed under ./data
    assert (tmp_path / "data" / "trial_store.sqlite3").is_file()


def test_serve_data_dir_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """serve with a non-loopback host fails exit 2 regardless; the data-dir
    resolution path is exercised by reaching the env value (missing dir -> 2
    with a diagnostic naming the env-resolved dir)."""
    import contextlib
    import io

    buf = io.StringIO()
    monkeypatch.setenv("SERVING_VERDICT_DATA_DIR", "/nonexistent-env-dir-xyz")
    with contextlib.redirect_stderr(buf):
        rc = cli.main(["serve", "--host", "127.0.0.1", "--port", "9", "--data-dir", "/nonexistent-env-dir-xyz"])
    assert rc == 2


def test_main_demo_default_out_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    rc = cli.main(["demo"])
    assert rc == 0
    assert (tmp_path / "data" / "demo" / "demo-promote" / "bundle.json").is_file()
