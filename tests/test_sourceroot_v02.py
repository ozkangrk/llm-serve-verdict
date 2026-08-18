"""v0.2 source-root semantics (RED first).

Contract:
- A RELATIVE ``source_root`` in a case config resolves against the PARENT
  directory of the case file (portable case trees); it must still be an
  existing directory and is canonicalized exactly like an absolute root.
- An ABSOLUTE ``source_root`` keeps the exact v0.1 behavior (back-compat).
- The CLI-only ``--source-root`` override (import-case) replaces the config's
  source root; a bad override is a hard usage error (exit 2). There is no
  HTTP path that accepts a user-provided source root.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from serving_verdict.caseconfig import load_case_config
from serving_verdict.engine import import_case
from tests.helpers import (
    CASE_SCHEMA,
    make_dspark_ab_fixture,
    sha256_file,
)


def _write_case(dirpath: Path, name: str, source_root: str, case_id: str = "sr-case") -> Path:
    base = make_dspark_ab_fixture(dirpath, filename="base.json", decode=25.62)
    cand = make_dspark_ab_fixture(dirpath, filename="cand.json", decode=63.27)
    doc = {
        "schema_version": CASE_SCHEMA,
        "id": case_id,
        "source_root": source_root,
        "baseline": {"artifact": "base.json", "sha256": sha256_file(base)},
        "candidate": {"artifact": "cand.json", "sha256": sha256_file(cand)},
        "policy": {
            "primary_metric": "decode_tokens_per_s",
            "workload": "edit_cold",
            "min_relative_improvement": 0.15,
            "max_ttft_regression": 0.10,
            "required_gates": ["request_success"],
        },
        "claim_boundary": "sourceroot test",
    }
    p = dirpath / name
    p.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return p


def test_absolute_root_back_compat(tmp_path: Path) -> None:
    case = _write_case(tmp_path, "case.yaml", str(tmp_path))
    cfg = load_case_config(case)
    assert cfg.source_root == str(tmp_path)
    bundle = import_case(case)
    assert bundle["verdict"] == "PROMOTE"


def test_relative_root_resolves_against_case_parent(tmp_path: Path) -> None:
    """relative source_root 'evidence' + case at tmp/sub/case.yaml -> tmp/evidence."""
    sub = tmp_path / "sub"
    sub.mkdir()
    evidence = sub / "evidence"
    evidence.mkdir()
    base = make_dspark_ab_fixture(evidence, filename="base.json", decode=25.62)
    cand = make_dspark_ab_fixture(evidence, filename="cand.json", decode=63.27)
    doc = {
        "schema_version": CASE_SCHEMA,
        "id": "rel-case",
        "source_root": "evidence",
        "baseline": {"artifact": "base.json", "sha256": sha256_file(base)},
        "candidate": {"artifact": "cand.json", "sha256": sha256_file(cand)},
        "policy": {
            "primary_metric": "decode_tokens_per_s",
            "workload": "edit_cold",
            "min_relative_improvement": 0.15,
            "max_ttft_regression": 0.10,
            "required_gates": ["request_success"],
        },
        "claim_boundary": "relative root",
    }
    sub_case = sub / "case.yaml"
    sub_case.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    cfg = load_case_config(sub_case)
    assert cfg.source_root == "evidence"  # raw value preserved in the config
    bundle = import_case(sub_case)
    assert bundle["verdict"] == "PROMOTE"


def test_relative_root_missing_dir_is_inconclusive(tmp_path: Path) -> None:
    """Missing relative root is evidence-level failure -> INCONCLUSIVE (exit 0)."""
    sub = tmp_path / "sub"
    sub.mkdir()
    p = sub / "case.yaml"
    p.write_text(
        yaml.safe_dump(
            {
                "schema_version": CASE_SCHEMA,
                "id": "rel-missing",
                "source_root": "no-such-evidence",
                "baseline": {"artifact": "b.json", "sha256": "0" * 64},
                "candidate": {"artifact": "c.json", "sha256": "1" * 64},
                "policy": {
                    "primary_metric": "decode_tokens_per_s",
                    "workload": "edit_cold",
                    "min_relative_improvement": 0.15,
                    "max_ttft_regression": 0.10,
                    "required_gates": ["request_success"],
                },
                "claim_boundary": "x",
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    bundle = import_case(p)
    assert bundle["verdict"] == "INCONCLUSIVE"
    assert "EVIDENCE_UNAVAILABLE" in bundle["reason_codes"]


def test_missing_relative_root_never_falls_back_to_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case_dir = tmp_path / "case-dir"
    case_dir.mkdir()
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    decoy = cwd / "evidence"
    decoy.mkdir()
    base = make_dspark_ab_fixture(decoy, filename="base.json", decode=25.62)
    cand = make_dspark_ab_fixture(decoy, filename="cand.json", decode=63.27)
    doc = {
        "schema_version": CASE_SCHEMA,
        "id": "no-cwd-fallback",
        "source_root": "evidence",
        "baseline": {"artifact": "base.json", "sha256": sha256_file(base)},
        "candidate": {"artifact": "cand.json", "sha256": sha256_file(cand)},
        "policy": {
            "primary_metric": "decode_tokens_per_s",
            "workload": "edit_cold",
            "min_relative_improvement": 0.15,
            "max_ttft_regression": 0.10,
            "required_gates": ["request_success"],
        },
        "claim_boundary": "missing case-relative root",
    }
    case = case_dir / "case.yaml"
    case.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    monkeypatch.chdir(cwd)
    bundle = import_case(case)
    assert bundle["verdict"] == "INCONCLUSIVE"
    assert bundle["reason_codes"] == ["EVIDENCE_UNAVAILABLE"]


def test_relative_root_traversal_rejected(tmp_path: Path) -> None:
    """'../outside' must not escape upward past the case-file parent chain in a way
    that bypasses the child-path safety model: traversal roots are a config error."""
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    base = make_dspark_ab_fixture(evidence, filename="base.json", decode=25.62)
    cand = make_dspark_ab_fixture(evidence, filename="cand.json", decode=63.27)
    p = evidence / "case.yaml"
    p.write_text(
        yaml.safe_dump(
            {
                "schema_version": CASE_SCHEMA,
                "id": "trav-case",
                "source_root": "../../escape",
                "baseline": {"artifact": "base.json", "sha256": sha256_file(base)},
                "candidate": {"artifact": "cand.json", "sha256": sha256_file(cand)},
                "policy": {
                    "primary_metric": "decode_tokens_per_s",
                    "workload": "edit_cold",
                    "min_relative_improvement": 0.15,
                    "max_ttft_regression": 0.10,
                    "required_gates": ["request_success"],
                },
                "claim_boundary": "x",
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    from serving_verdict.errors import CaseConfigError

    with pytest.raises(CaseConfigError):
        load_case_config(p)


def test_cli_source_root_override(tmp_path: Path) -> None:
    """--source-root replaces the config root; the bundle is produced from the
    overridden tree (not from the case file's parent)."""
    from tests.test_cli import run_cli

    other = tmp_path / "other"
    other.mkdir()
    base = make_dspark_ab_fixture(other, filename="base.json", decode=25.62)
    cand = make_dspark_ab_fixture(other, filename="cand.json", decode=63.27)
    case_dir = tmp_path / "casedir"
    case_dir.mkdir()
    doc = {
        "schema_version": CASE_SCHEMA,
        "id": "override-case",
        "source_root": str(other),
        "baseline": {"artifact": "base.json", "sha256": sha256_file(base)},
        "candidate": {"artifact": "cand.json", "sha256": sha256_file(cand)},
        "policy": {
            "primary_metric": "decode_tokens_per_s",
            "workload": "edit_cold",
            "min_relative_improvement": 0.15,
            "max_ttft_regression": 0.10,
            "required_gates": ["request_success"],
        },
        "claim_boundary": "override",
    }
    case = case_dir / "case.yaml"
    case.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    out = tmp_path / "bundle.json"
    # copy the evidence into a second tree and point --source-root there
    alt = tmp_path / "alt"
    alt.mkdir()
    for f in ("base.json", "cand.json"):
        (alt / f).write_bytes((other / f).read_bytes())
    r = run_cli("import-case", str(case), "--out", str(out), "--source-root", str(alt))
    assert r.returncode == 0, r.stderr
    assert json.loads(out.read_text())["verdict"] == "PROMOTE"


def test_cli_source_root_bad_override_exit_2(tmp_path: Path) -> None:
    from serving_verdict import cli

    case = _write_case(tmp_path, "case.yaml", str(tmp_path))
    assert (
        cli.main(["import-case", str(case), "--out", str(tmp_path / "b.json"),
                  "--source-root", str(tmp_path / "missing")])
        == 2
    )
    assert not (tmp_path / "b.json").exists()
