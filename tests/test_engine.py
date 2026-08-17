"""Decision engine + tamper-evident bundle + offline verify tests (RED first)."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from serving_verdict.engine import (
    Verdict,
    import_case,
    load_bundle,
    verify_bundle,
)
from serving_verdict.errors import (
    IntegrityError,
    UsageError,
)
from tests.helpers import (
    BUNDLE_SCHEMA,
    make_dspark_ab_fixture,
    sha256_file,
)

PROMOTE = Verdict.PROMOTE
REJECT = Verdict.REJECT
INCONCLUSIVE = Verdict.INCONCLUSIVE

GATES_ALL = ["request_success", "arithmetic", "tool_call", "process_stability", "rollback"]


def build_case(tmp_path: Path, **overrides) -> Path:
    """Build a DSpark-style fixture case (both artifacts + attested report)."""
    import yaml


    base = make_dspark_ab_fixture(
        tmp_path,
        filename="base.json",
        decode=25.62,
        e2e=24.94,
        ttft=1.317,
        latency=48.11,
        agg=65.64,
        wall=54.85,
    )
    cand = make_dspark_ab_fixture(
        tmp_path,
        filename="cand.json",
        engine="vllm-dspark-k7",
        decode=63.27,
        e2e=59.41,
        ttft=1.272,
        latency=20.20,
        agg=132.83,
        wall=27.10,
    )
    report = tmp_path / "REPORT.md"
    report.write_text("# gates pass\n", encoding="utf-8")
    doc = {
        "schema_version": "serving-verdict.case.v0.1",
        "id": "fixture-dspark",
        "source_root": str(tmp_path),
        "baseline": {"artifact": "base.json", "sha256": sha256_file(base)},
        "candidate": {"artifact": "cand.json", "sha256": sha256_file(cand)},
        "policy": {
            "primary_metric": "decode_tokens_per_s",
            "workload": "edit_cold",
            "min_relative_improvement": 0.15,
            "max_ttft_regression": 0.10,
            "required_gates": ["request_success", "arithmetic", "rollback"],
        },
        "supplemental_evidence": [
            {
                "id": "arithmetic",
                "kind": "operator_attested",
                "status": "pass",
                "source": "REPORT.md",
                "sha256": sha256_file(report),
            },
            {
                "id": "rollback",
                "kind": "operator_attested",
                "status": "pass",
                "source": "REPORT.md",
                "sha256": sha256_file(report),
            },
        ],
        "claim_boundary": "fixture boundary",
    }
    doc.update(overrides)
    p = tmp_path / "case.yaml"
    p.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return p


def attested(gate_id: str, status: str, report: Path) -> dict:
    return {
        "id": gate_id,
        "kind": "operator_attested",
        "status": status,
        "source": "REPORT.md",
        "sha256": sha256_file(report),
    }


# ---------------------------------------------------------------------- basic


def test_promote_happy_path(tmp_path: Path) -> None:
    case = build_case(tmp_path)
    bundle = import_case(case)
    assert bundle["verdict"] == PROMOTE.value
    assert bundle["schema_version"] == BUNDLE_SCHEMA
    assert bundle["case_id"] == "fixture-dspark"
    assert "PRIMARY_EFFECT_PASSED" in bundle["reason_codes"]
    assert "ALL_REQUIRED_GATES_PASSED" in bundle["reason_codes"]
    assert "bundle_digest" in bundle and bundle["bundle_digest"].startswith("sha256:")
    assert "created_at" in bundle
    # comparisons include the primary metric with both values
    prim = [c for c in bundle["comparisons"] if c["metric"] == "decode_tokens_per_s"]
    assert len(prim) == 1
    assert prim[0]["baseline_value"] == 25.62
    assert prim[0]["candidate_value"] == 63.27
    # gate authorities are visible
    statuses = {g["id"]: g["status"] for g in bundle["gates"]}
    assert statuses == {
        "request_success": "pass",
        "arithmetic": "pass",
        "rollback": "pass",
    }
    authorities = {g["id"]: g["authority"] for g in bundle["gates"]}
    assert authorities["request_success"] == "machine_measured"
    assert authorities["arithmetic"] == "operator_attested"
    # artifact hashes recorded
    assert bundle["baseline"]["sha256"] == bundle["baseline"]["sha256"].lower()


def test_ttft_compared_and_reported(tmp_path: Path) -> None:
    bundle = import_case(build_case(tmp_path))
    ttft = [c for c in bundle["comparisons"] if c["metric"] == "ttft_s"]
    assert len(ttft) == 1
    assert ttft[0]["baseline_value"] == pytest.approx(1.317)
    assert ttft[0]["candidate_value"] == pytest.approx(1.272)
    assert ttft[0]["relative_delta"] < 0  # lower TTFT is better


def test_bundle_recompute_stable(tmp_path: Path) -> None:
    bundle = import_case(build_case(tmp_path))
    payload = {k: v for k, v in bundle.items() if k not in ("created_at", "bundle_digest")}
    from serving_verdict.canonical import compute_bundle_digest

    assert compute_bundle_digest(payload) == bundle["bundle_digest"]
    assert verify_bundle(bundle)["valid"] is True


def test_created_at_not_in_digest(tmp_path: Path) -> None:
    bundle = import_case(build_case(tmp_path))
    mutated = copy.deepcopy(bundle)
    mutated["created_at"] = "1999-01-01T00:00:00Z"
    from serving_verdict.canonical import compute_bundle_digest

    payload = {k: v for k, v in mutated.items() if k not in ("created_at", "bundle_digest")}
    assert compute_bundle_digest(payload) == bundle["bundle_digest"]


# --------------------------------------------------------------------- rules


def test_hard_gate_fail_rejects_despite_performance_win(tmp_path: Path) -> None:
    case = build_case(tmp_path)
    import yaml

    doc = yaml.safe_load(case.read_text())
    doc["policy"]["required_gates"] = ["request_success", "arithmetic", "process_stability"]
    doc["supplemental_evidence"] = [attested("process_stability", "fail", tmp_path / "REPORT.md")]
    case.write_text(yaml.safe_dump(doc), encoding="utf-8")
    bundle = import_case(case)
    assert bundle["verdict"] == REJECT.value
    assert "HARD_GATE_FAILED" in bundle["reason_codes"]


def test_missing_required_gate_inconclusive(tmp_path: Path) -> None:
    case = build_case(tmp_path)
    import yaml

    doc = yaml.safe_load(case.read_text())
    doc["policy"]["required_gates"] = ["request_success", "arithmetic", "tool_call"]
    # tool_call not attested anywhere and no machine gate -> absent
    doc["supplemental_evidence"] = [attested("arithmetic", "pass", tmp_path / "REPORT.md")]
    case.write_text(yaml.safe_dump(doc), encoding="utf-8")
    bundle = import_case(case)
    assert bundle["verdict"] == INCONCLUSIVE.value
    assert "REQUIRED_GATE_MISSING" in bundle["reason_codes"]


def test_attested_gate_hash_mismatch_counts_as_missing(tmp_path: Path) -> None:
    case = build_case(tmp_path)
    import yaml

    doc = yaml.safe_load(case.read_text())
    doc["supplemental_evidence"][0]["sha256"] = "0" * 64  # wrong hash -> unverifiable
    case.write_text(yaml.safe_dump(doc), encoding="utf-8")
    bundle = import_case(case)
    assert bundle["verdict"] == INCONCLUSIVE.value
    assert "REQUIRED_GATE_MISSING" in bundle["reason_codes"]


def test_ttft_regression_reject(tmp_path: Path) -> None:
    import yaml

    cand = make_dspark_ab_fixture(
        tmp_path,
        filename="cand.json",
        engine="vllm-dspark-k7",
        decode=99.0,  # huge win
        e2e=90.0,
        ttft=3.0,  # but TTFT 3x worse
        latency=9.0,
    )
    base = make_dspark_ab_fixture(
        tmp_path, filename="base.json", decode=50.0, e2e=45.0, ttft=1.0, latency=20.0
    )
    report = tmp_path / "REPORT.md"
    report.write_text("gates\n", encoding="utf-8")
    doc = {
        "schema_version": "serving-verdict.case.v0.1",
        "id": "ttft-case",
        "source_root": str(tmp_path),
        "baseline": {"artifact": "base.json", "sha256": sha256_file(base)},
        "candidate": {"artifact": "cand.json", "sha256": sha256_file(cand)},
        "policy": {
            "primary_metric": "decode_tokens_per_s",
            "workload": "edit_cold",
            "min_relative_improvement": 0.15,
            "max_ttft_regression": 0.10,
            "required_gates": ["request_success"],
        },
        "claim_boundary": "fixture boundary",
    }
    p = tmp_path / "case.yaml"
    p.write_text(yaml.safe_dump(doc), encoding="utf-8")
    bundle = import_case(p)
    assert bundle["verdict"] == REJECT.value
    assert "TTFT_REGRESSION" in bundle["reason_codes"]


def test_insufficient_effect_reject(tmp_path: Path) -> None:
    cand = make_dspark_ab_fixture(
        tmp_path,
        filename="cand.json",
        engine="vllm-dspark-k7",
        decode=26.0,  # only ~1.6% over baseline 25.62
        e2e=25.0,
        ttft=1.30,
        latency=47.0,
    )
    base = make_dspark_ab_fixture(
        tmp_path, filename="base.json", decode=25.62, e2e=24.94, ttft=1.317, latency=48.1
    )
    import yaml

    doc = {
        "schema_version": "serving-verdict.case.v0.1",
        "id": "small-effect",
        "source_root": str(tmp_path),
        "baseline": {"artifact": "base.json", "sha256": sha256_file(base)},
        "candidate": {"artifact": "cand.json", "sha256": sha256_file(cand)},
        "policy": {
            "primary_metric": "decode_tokens_per_s",
            "workload": "edit_cold",
            "min_relative_improvement": 0.15,
            "max_ttft_regression": 0.10,
            "required_gates": ["request_success"],
        },
        "claim_boundary": "fixture boundary",
    }
    p = tmp_path / "case.yaml"
    p.write_text(yaml.safe_dump(doc), encoding="utf-8")
    bundle = import_case(p)
    assert bundle["verdict"] == REJECT.value
    assert "INSUFFICIENT_EFFECT" in bundle["reason_codes"]


def _latency_case(tmp_path: Path, cand_latency: float, min_imp: float, case_id: str = "lat-primary") -> Path:
    """Case with lower_better primary (api_latency_s); all other gates pass."""
    import yaml

    base = make_dspark_ab_fixture(
        tmp_path, filename="base.json", decode=25.62, e2e=24.94, ttft=1.317, latency=20.0
    )
    cand = make_dspark_ab_fixture(
        tmp_path,
        filename="cand.json",
        engine="vllm-dspark-k7",
        decode=63.27,
        e2e=59.41,
        ttft=1.27,
        latency=cand_latency,
    )
    doc = {
        "schema_version": "serving-verdict.case.v0.1",
        "id": case_id,
        "source_root": str(tmp_path),
        "baseline": {"artifact": "base.json", "sha256": sha256_file(base)},
        "candidate": {"artifact": "cand.json", "sha256": sha256_file(cand)},
        "policy": {
            "primary_metric": "api_latency_s",
            "workload": "edit_cold",
            "min_relative_improvement": min_imp,
            "max_ttft_regression": 0.10,
            "required_gates": ["request_success"],
        },
        "claim_boundary": "fixture boundary",
    }
    p = tmp_path / "case.yaml"
    p.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return p


def test_lower_better_primary_worsening_rejects(tmp_path: Path) -> None:
    """F1: candidate 2x WORSE on a lower_better primary must REJECT, not PROMOTE."""
    bundle = import_case(_latency_case(tmp_path, cand_latency=40.0, min_imp=0.15))
    assert bundle["verdict"] == REJECT.value
    assert bundle["reason_codes"] == ["INSUFFICIENT_EFFECT"]


def test_lower_better_primary_exact_threshold_promotes(tmp_path: Path) -> None:
    """F1: exactly 15% improvement on a lower_better primary is the PROMOTE boundary."""
    # baseline 20.0s -> candidate 17.0s is exactly -15.0% (gain == threshold)
    bundle = import_case(_latency_case(tmp_path, cand_latency=17.0, min_imp=0.15))
    assert bundle["verdict"] == PROMOTE.value
    assert "PRIMARY_EFFECT_PASSED" in bundle["reason_codes"]


def test_lower_better_primary_just_below_threshold_rejects(tmp_path: Path) -> None:
    """F1: 14.9% improvement on a lower_better primary must REJECT (just below 15%)."""
    # baseline 20.0s -> candidate 17.02s is -14.9% (< 0.15 threshold)
    bundle = import_case(_latency_case(tmp_path, cand_latency=17.02, min_imp=0.15))
    assert bundle["verdict"] == REJECT.value
    assert "INSUFFICIENT_EFFECT" in bundle["reason_codes"]


# -------------------------------------------------------------- inconclusive


def test_evidence_hash_mismatch_inconclusive(tmp_path: Path) -> None:
    case = build_case(tmp_path)
    import yaml

    doc = yaml.safe_load(case.read_text())
    doc["candidate"]["sha256"] = "0" * 64
    case.write_text(yaml.safe_dump(doc), encoding="utf-8")
    bundle = import_case(case)
    assert bundle["verdict"] == INCONCLUSIVE.value
    assert "EVIDENCE_HASH_MISMATCH" in bundle["reason_codes"]


def test_missing_artifact_inconclusive(tmp_path: Path) -> None:
    case = build_case(tmp_path)
    import yaml

    doc = yaml.safe_load(case.read_text())
    doc["candidate"]["artifact"] = "does-not-exist.json"
    case.write_text(yaml.safe_dump(doc), encoding="utf-8")
    bundle = import_case(case)
    assert bundle["verdict"] == INCONCLUSIVE.value
    assert "EVIDENCE_UNAVAILABLE" in bundle["reason_codes"]


def test_unsupported_schema_inconclusive(tmp_path: Path) -> None:
    import yaml

    base = make_dspark_ab_fixture(tmp_path, filename="base.json")
    weird = tmp_path / "weird.json"
    weird.write_text('{"schema_version": "vendor.mystery.v9"}', encoding="utf-8")
    report = tmp_path / "REPORT.md"
    report.write_text("x", encoding="utf-8")
    doc = {
        "schema_version": "serving-verdict.case.v0.1",
        "id": "unsupported",
        "source_root": str(tmp_path),
        "baseline": {"artifact": "base.json", "sha256": sha256_file(base)},
        "candidate": {"artifact": "weird.json", "sha256": sha256_file(weird)},
        "policy": {
            "primary_metric": "decode_tokens_per_s",
            "workload": "edit_cold",
            "min_relative_improvement": 0.15,
            "max_ttft_regression": 0.10,
            "required_gates": ["request_success"],
        },
        "claim_boundary": "fixture boundary",
    }
    p = tmp_path / "case.yaml"
    p.write_text(yaml.safe_dump(doc), encoding="utf-8")
    bundle = import_case(p)
    assert bundle["verdict"] == INCONCLUSIVE.value
    assert "UNSUPPORTED_SCHEMA" in bundle["reason_codes"]


def test_nan_in_artifact_inconclusive(tmp_path: Path) -> None:
    base = make_dspark_ab_fixture(tmp_path, filename="base.json")
    bad = tmp_path / "bad.json"
    bad.write_text(
        json.dumps(
            {
                "schema_version": "qwen38.dspark-ab.v1",
                "results": {
                    "edit_cold": {
                        "requests": [
                            {
                                "completion_tokens": 1200,
                                "decode_tokens_per_s": float("nan"),
                                "ttft_s": 1.0,
                                "finish_reason": "length",
                            }
                        ],
                        "median_decode_tokens_per_s": float("nan"),
                        "median_ttft_s": 1.0,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    import yaml

    doc = {
        "schema_version": "serving-verdict.case.v0.1",
        "id": "nan-case",
        "source_root": str(tmp_path),
        "baseline": {"artifact": "base.json", "sha256": sha256_file(base)},
        "candidate": {"artifact": "bad.json", "sha256": sha256_file(bad)},
        "policy": {
            "primary_metric": "decode_tokens_per_s",
            "workload": "edit_cold",
            "min_relative_improvement": 0.15,
            "max_ttft_regression": 0.10,
            "required_gates": ["request_success"],
        },
        "claim_boundary": "fixture boundary",
    }
    p = tmp_path / "case.yaml"
    p.write_text(yaml.safe_dump(doc), encoding="utf-8")
    bundle = import_case(p)
    assert bundle["verdict"] == INCONCLUSIVE.value
    assert "NONFINITE_EVIDENCE" in bundle["reason_codes"]


def test_metric_workload_mismatch_inconclusive(tmp_path: Path) -> None:
    case = build_case(tmp_path)
    import yaml

    doc = yaml.safe_load(case.read_text())
    doc["policy"]["workload"] = "fresh_code"  # not present in either fixture
    case.write_text(yaml.safe_dump(doc), encoding="utf-8")
    bundle = import_case(case)
    assert bundle["verdict"] == INCONCLUSIVE.value
    assert "METRIC_NOT_COMPARABLE" in bundle["reason_codes"]


# ------------------------------------------------------------ minimized cases


def test_minimized_dspark_fixture_promotes(tmp_path: Path) -> None:
    fixture_root = Path("tests/fixtures/dspark")
    bundle = import_case(fixture_root / "case.yaml")
    assert bundle["verdict"] == PROMOTE.value
    assert bundle["case_id"] == "fixture-dspark"


def test_minimized_sglang_fixture_rejects(tmp_path: Path) -> None:
    fixture_root = Path("tests/fixtures/sglang")
    bundle = import_case(fixture_root / "case.yaml")
    assert bundle["verdict"] == REJECT.value
    assert bundle["case_id"] == "fixture-sglang"
    assert "HARD_GATE_FAILED" in bundle["reason_codes"]


def test_minimized_fixture_bundles_verify(tmp_path: Path) -> None:
    for name in ("dspark", "sglang"):
        bundle = import_case(Path(f"tests/fixtures/{name}/case.yaml"))
        report = verify_bundle(bundle)
        assert report["valid"] is True, name
        assert report["digest"] == bundle["bundle_digest"]


# -------------------------------------------------------------------- verify


def write_bundle(tmp_path: Path, bundle: dict) -> Path:
    p = tmp_path / "bundle.json"
    p.write_text(json.dumps(bundle), encoding="utf-8")
    return p


def test_verify_detects_tampered_verdict(tmp_path: Path) -> None:
    bundle = import_case(build_case(tmp_path))
    p = write_bundle(tmp_path, bundle)
    doc = json.loads(p.read_text())
    doc["verdict"] = "PROMOTE" if doc["verdict"] != "PROMOTE" else "REJECT"
    p.write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(IntegrityError):
        verify_bundle(load_bundle(p))


def test_verify_detects_tampered_hash(tmp_path: Path) -> None:
    bundle = import_case(build_case(tmp_path))
    p = write_bundle(tmp_path, bundle)
    doc = json.loads(p.read_text())
    doc["candidate"]["sha256"] = "0" * 64
    p.write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(IntegrityError):
        verify_bundle(load_bundle(p))


def test_verify_detects_tampered_comparison_value(tmp_path: Path) -> None:
    bundle = import_case(build_case(tmp_path))
    p = write_bundle(tmp_path, bundle)
    doc = json.loads(p.read_text())
    doc["comparisons"][0]["candidate_value"] = 999.0
    p.write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(IntegrityError):
        verify_bundle(load_bundle(p))


def test_verify_rejects_wrong_schema_version(tmp_path: Path) -> None:
    bundle = import_case(build_case(tmp_path))
    p = write_bundle(tmp_path, bundle)
    doc = json.loads(p.read_text())
    doc["schema_version"] = "serving-verdict.bundle.v9.9"
    p.write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(IntegrityError):
        verify_bundle(load_bundle(p))


def test_verify_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(UsageError):
        load_bundle(tmp_path / "nope.json")


def test_bundle_file_round_trip_canonical(tmp_path: Path) -> None:
    bundle = import_case(build_case(tmp_path))
    p = write_bundle(tmp_path, bundle)
    loaded = load_bundle(p)
    assert loaded == bundle


# ------------------------------------------------------------- gate ordering


def test_hard_gate_fail_wins_over_ttft_and_effect(tmp_path: Path) -> None:
    """Rule 3 (hard gates) is evaluated before rules 5/6."""
    import yaml

    cand = make_dspark_ab_fixture(
        tmp_path,
        filename="cand.json",
        engine="vllm-dspark-k7",
        decode=200.0,  # massive perf win
        e2e=200.0,
        ttft=10.0,  # massive TTFT regression
        latency=1.0,
    )
    base = make_dspark_ab_fixture(
        tmp_path, filename="base.json", decode=50.0, e2e=45.0, ttft=1.0, latency=20.0
    )
    report = tmp_path / "REPORT.md"
    report.write_text("crash", encoding="utf-8")
    doc = {
        "schema_version": "serving-verdict.case.v0.1",
        "id": "hard-first",
        "source_root": str(tmp_path),
        "baseline": {"artifact": "base.json", "sha256": sha256_file(base)},
        "candidate": {"artifact": "cand.json", "sha256": sha256_file(cand)},
        "policy": {
            "primary_metric": "decode_tokens_per_s",
            "workload": "edit_cold",
            "min_relative_improvement": 0.15,
            "max_ttft_regression": 0.10,
            "required_gates": ["request_success", "process_stability"],
        },
        "supplemental_evidence": [
            {"id": "process_stability", "kind": "operator_attested", "status": "fail", "source": "REPORT.md", "sha256": sha256_file(report)},
        ],
        "claim_boundary": "fixture boundary",
    }
    p = tmp_path / "case.yaml"
    p.write_text(yaml.safe_dump(doc), encoding="utf-8")
    bundle = import_case(p)
    assert bundle["verdict"] == REJECT.value
    assert bundle["reason_codes"] == ["HARD_GATE_FAILED"]
