"""v0.2 append-only SQLite trial registry (RED first).

Contract:
- stdlib ``sqlite3`` only (no ORM, no third-party driver); the DB file lives
  at ``<data_dir>/trial_store.sqlite3``.
- Schema migration via ``PRAGMA user_version`` (v1 at first use); all SQL is
  parameterized (no string-formatted values).
- The bundle file is the SOURCE OF TRUTH: registering verifies the bundle and
  stores only identity/digest/verdict/reasons plus a bundle-file pointer.
- ``register_bundle`` is idempotent on the bundle digest: re-registering the
  same digest updates nothing and reports ``action='duplicate'``; a NEW
  bundle for the same case_id appends a NEW event row (history is append-only;
  rows are never updated or deleted by the registry itself).
- ``reindex`` rebuilds the current state from the data dir: files missing or
  failing verify are marked ``missing``/``invalid``; valid bundles keep or
  create their events; rows for vanished bundles are marked missing (kept,
  not deleted).
- ``list_events`` returns the append-only event history ordered by sequence.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from serving_verdict.engine import import_case, verify_bundle
from serving_verdict.trialstore import TrialStore
from tests.helpers import FIXTURES

DSKAB_FIXTURE = FIXTURES / "dspark" / "case.yaml"
SGLANG_FIXTURE = FIXTURES / "sglang" / "case.yaml"

STORE_NAME = "trial_store.sqlite3"


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


def _store(data_dir: Path) -> TrialStore:
    return TrialStore(data_dir)


def test_user_version_migration(data_dir: Path) -> None:
    _store(data_dir)
    conn = sqlite3.connect(data_dir / STORE_NAME)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()
    assert version >= 1


def test_register_verify_and_store(data_dir: Path) -> None:
    store = _store(data_dir)
    ev = store.register_bundle(data_dir / "d.json")
    assert ev["action"] == "registered"
    assert ev["verdict"] == "PROMOTE"
    assert ev["bundle_digest"].startswith("sha256:")
    assert ev["case_id"] == "fixture-dspark"
    assert ev["bundle_file"] == "d.json"
    # the bundle file remains the source of truth: stored fields derive from it
    rows = store.list_trials()
    assert rows and rows[0]["case_id"] == "fixture-dspark"


def test_register_requires_valid_bundle(data_dir: Path) -> None:
    from serving_verdict.errors import IntegrityError

    tampered = data_dir / "d.json"
    doc = json.loads(tampered.read_text())
    doc["verdict"] = "REJECT"
    bad = data_dir / "bad.json"
    bad.write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(IntegrityError):
        _store(data_dir).register_bundle(bad)


def test_duplicate_digest_idempotent(data_dir: Path) -> None:
    store = _store(data_dir)
    first = store.register_bundle(data_dir / "d.json")
    second = store.register_bundle(data_dir / "d.json")
    assert second["action"] == "duplicate"
    assert second["event_id"] == first["event_id"]
    # the s.json bundle registers normally too
    third = store.register_bundle(data_dir / "s.json")
    assert third["action"] == "registered"
    events = store.list_events("fixture-dspark")
    assert len(events) == 1  # no duplicate row
    assert store.list_events("fixture-sglang")[0]["verdict"] == "REJECT"


def test_new_bundle_same_case_appends_history(data_dir: Path, tmp_path: Path) -> None:
    """A second (different) bundle for the same case id appends a new event:
    the registry is an append-only history, not a key-value map."""
    store = _store(data_dir)
    store.register_bundle(data_dir / "d.json")
    # re-import: created_at differs -> different digest (volatile excluded? no:
    # created_at IS excluded from the digest; so re-import yields the same
    # digest). Force a NEW bundle by changing case identity input: import a
    # mutated case with the same id but a changed claim boundary.
    import yaml

    case_doc = yaml.safe_load(DSKAB_FIXTURE.read_text())
    case_doc["claim_boundary"] = "second run, same id"
    case_path = tmp_path / "d2.yaml"
    case_path.write_text(yaml.safe_dump(case_doc), encoding="utf-8")
    bundle = import_case(str(case_path))
    (data_dir / "d2.json").write_text(json.dumps(bundle), encoding="utf-8")
    verify_bundle(bundle)
    ev = store.register_bundle(data_dir / "d2.json")
    assert ev["action"] == "registered"
    events = store.list_events("fixture-dspark")
    assert len(events) == 2
    assert events[0]["seq"] < events[1]["seq"]
    assert events[1]["bundle_file"] == "d2.json"


def test_reindex_rebuilds_state(data_dir: Path) -> None:
    store = _store(data_dir)
    store.register_bundle(data_dir / "d.json")
    store.register_bundle(data_dir / "s.json")
    # tamper one bundle -> reindex must mark it invalid
    doc = json.loads((data_dir / "s.json").read_text())
    doc["verdict"] = "INCONCLUSIVE"
    (data_dir / "s.json").write_text(json.dumps(doc), encoding="utf-8")
    # delete the other one
    (data_dir / "d.json").unlink()
    report = store.reindex()
    statuses = {t["case_id"]: t["status"] for t in store.list_trials()}
    assert statuses["fixture-sglang"] == "invalid"
    assert statuses["fixture-dspark"] == "missing"
    assert report["indexed"] == 0
    assert report["invalid"] == 1
    assert report["missing"] == 1
    # history rows are kept (append-only), not deleted
    assert store.list_events("fixture-dspark")


def test_reindex_recovers_valid_bundle(data_dir: Path) -> None:
    store = _store(data_dir)
    store.register_bundle(data_dir / "d.json")
    report = store.reindex()
    assert report["indexed"] == 2
    statuses = {t["case_id"]: t["status"] for t in store.list_trials()}
    assert statuses["fixture-dspark"] == "valid"
    assert statuses["fixture-sglang"] == "valid"


def test_sql_is_parameterized(data_dir: Path) -> None:
    """A case_id that would break string-formatted SQL is harmless: it round
    trips through parameterized statements and is simply not found."""
    store = _store(data_dir)
    evil = "x'); DROP TABLE trials; --"
    assert store.get_trial(evil) is None
    assert store.get_trial(evil) is None  # tables still exist afterwards


def test_store_is_reopenable_and_persistent(data_dir: Path) -> None:
    a = _store(data_dir)
    a.register_bundle(data_dir / "d.json")
    b = _store(data_dir)  # fresh connection, same file
    assert [e["case_id"] for e in b.list_events("fixture-dspark")] == ["fixture-dspark"]
