"""v0.2 portable demo (RED first).

Contract:
- ``llm-serve-verdict demo --out-dir DIR`` writes a self-contained, in-package
  demo into DIR: two cases with evidence and pre-built bundles
  (``demo-promote`` -> PROMOTE, ``demo-reject`` -> REJECT via a failed
  hard gate). No external source tree is required.
- Both bundles pass offline ``verify`` (exit 0).
- Deterministic: running the demo into two fresh temp dirs yields byte-
  identical trees; the digest of every bundle is independent of the output
  directory and of the volatile ``created_at`` timestamp.
- Usage errors (missing parent of --out-dir) exit 2 and write nothing.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.test_cli import run_cli

PROMOTE_DIR = "demo-promote"
REJECT_DIR = "demo-reject"


def _run_demo(tmp_path: Path) -> Path:
    out = tmp_path / "demo"
    r = run_cli("demo", "--out-dir", str(out))
    assert r.returncode == 0, r.stderr
    return out


@pytest.fixture(scope="module")
def demo_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _run_demo(tmp_path_factory.mktemp("demo"))


@pytest.fixture(scope="module")
def demo_dir2(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _run_demo(tmp_path_factory.mktemp("demo2"))


def _all_files(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def test_demo_exit_0_creates_two_cases(demo_dir: Path) -> None:
    for sub in (PROMOTE_DIR, REJECT_DIR):
        assert (demo_dir / sub).is_dir(), sub
        assert (demo_dir / sub / "case.yaml").is_file(), sub
        assert (demo_dir / sub / "evidence").is_dir(), sub
    assert (demo_dir / PROMOTE_DIR / "bundle.json").is_file()
    assert (demo_dir / REJECT_DIR / "bundle.json").is_file()


def test_demo_root_lists_both_verdicts(demo_dir: Path) -> None:
    r = run_cli("list", str(demo_dir), "--json")
    assert r.returncode == 0, r.stderr
    payload = json.loads(r.stdout)
    assert {row["case_id"] for row in payload["verdicts"]} == {
        "demo-promote",
        "demo-reject",
    }


def test_demo_promote_bundle_is_promote(demo_dir: Path) -> None:
    doc = json.loads((demo_dir / PROMOTE_DIR / "bundle.json").read_text())
    assert doc["verdict"] == "PROMOTE"
    assert doc["reason_codes"] == ["PRIMARY_EFFECT_PASSED", "ALL_REQUIRED_GATES_PASSED"]
    assert doc["bundle_digest"].startswith("sha256:")


def test_demo_reject_bundle_is_reject(demo_dir: Path) -> None:
    doc = json.loads((demo_dir / REJECT_DIR / "bundle.json").read_text())
    assert doc["verdict"] == "REJECT"
    assert doc["reason_codes"] == ["HARD_GATE_FAILED"]


def test_demo_bundles_verify_offline(demo_dir: Path) -> None:
    for sub in (PROMOTE_DIR, REJECT_DIR):
        r = run_cli("verify", str(demo_dir / sub / "bundle.json"))
        assert r.returncode == 0, (sub, r.stderr)


def test_demo_cases_importable_from_out_dir(demo_dir: Path) -> None:
    """import-case re-runs each demo case (relative source_root -> evidence/)."""
    from serving_verdict.engine import import_case

    for sub, expected in ((PROMOTE_DIR, "PROMOTE"), (REJECT_DIR, "REJECT")):
        bundle = import_case(str(demo_dir / sub / "case.yaml"))
        assert bundle["verdict"] == expected, sub


def test_demo_deterministic_across_fresh_dirs(demo_dir: Path, demo_dir2: Path) -> None:
    """Fresh temp dirs produce byte-identical trees (no paths, no timestamps
    leak into committed demo files)."""
    assert _all_files(demo_dir) == _all_files(demo_dir2)
    for sub in (PROMOTE_DIR, REJECT_DIR):
        a = json.loads((demo_dir / sub / "bundle.json").read_text())
        b = json.loads((demo_dir2 / sub / "bundle.json").read_text())
        assert a["bundle_digest"] == b["bundle_digest"], sub


def test_demo_bundle_digest_independent_of_location(tmp_path: Path) -> None:
    """The bundle digest must be stable across output locations and across the
    volatile created_at timestamp."""
    from serving_verdict.canonical import payload_without_volatile

    out = tmp_path / "demo"
    r = run_cli("demo", "--out-dir", str(out))
    assert r.returncode == 0, r.stderr
    for sub in (PROMOTE_DIR, REJECT_DIR):
        doc = json.loads((out / sub / "bundle.json").read_text())
        doc["created_at"] = "1970-01-01T00:00:00+00:00"
        from serving_verdict.canonical import compute_bundle_digest

        assert compute_bundle_digest(payload_without_volatile(doc)) == doc["bundle_digest"], sub


def test_demo_missing_parent_exit_2_writes_nothing(tmp_path: Path) -> None:
    out = tmp_path / "nope" / "demo"
    r = run_cli("demo", "--out-dir", str(out))
    assert r.returncode == 2
    assert r.stdout.strip() == ""  # diagnostics go to stderr
    assert not (tmp_path / "nope").exists()
