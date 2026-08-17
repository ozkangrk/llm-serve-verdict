"""Case config v0.1 parsing + artifact adapter tests (RED first)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from serving_verdict.adapters import (
    AdapterResult,
    UnknownSchemaError,
    extract_samples,
    known_schemas,
)
from serving_verdict.caseconfig import load_case_config
from serving_verdict.errors import CaseConfigError
from tests.helpers import (
    CASE_SCHEMA,
    DSKAB_SCHEMA,
    SGLANG_SCHEMA,
    make_dspark_ab_fixture,
    make_sglang_ab_fixture,
)

# ---------------------------------------------------------------- case config


def base_case(root: Path, baseline: str, candidate: str, **kw) -> dict:
    doc = {
        "schema_version": CASE_SCHEMA,
        "id": "fixture-case",
        "source_root": str(root),
        "baseline": {"artifact": baseline, "sha256": "0" * 64},
        "candidate": {"artifact": candidate, "sha256": "1" * 64},
        "policy": {
            "primary_metric": "decode_tokens_per_s",
            "workload": "edit_cold",
            "min_relative_improvement": 0.15,
            "max_ttft_regression": 0.10,
            "required_gates": ["request_success"],
        },
        "claim_boundary": "fixture boundary",
    }
    doc.update(kw)
    return doc


def write_case(root: Path, doc: dict, name: str = "case.yaml") -> Path:
    p = root / name
    import yaml

    p.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return p


def test_loads_valid_case(tmp_path: Path) -> None:
    p = write_case(tmp_path, base_case(tmp_path, "a.json", "b.json"))
    cfg = load_case_config(p)
    assert cfg.case_id == "fixture-case"
    assert cfg.policy.primary_metric == "decode_tokens_per_s"
    assert cfg.policy.min_relative_improvement == 0.15
    assert cfg.policy.max_ttft_regression == 0.10
    assert list(cfg.policy.required_gates) == ["request_success"]
    assert cfg.claim_boundary == "fixture boundary"


def test_rejects_wrong_schema_version(tmp_path: Path) -> None:
    doc = base_case(tmp_path, "a.json", "b.json")
    doc["schema_version"] = "other.v0.1"
    p = write_case(tmp_path, doc)
    with pytest.raises(CaseConfigError):
        load_case_config(p)


def test_rejects_invalid_yaml(tmp_path: Path) -> None:
    p = tmp_path / "bad.yaml"
    p.write_text("schema_version: [unclosed", encoding="utf-8")
    with pytest.raises(CaseConfigError):
        load_case_config(p)


def test_rejects_missing_required_fields(tmp_path: Path) -> None:
    doc = base_case(tmp_path, "a.json", "b.json")
    del doc["policy"]
    p = write_case(tmp_path, doc)
    with pytest.raises(CaseConfigError):
        load_case_config(p)


def test_rejects_unknown_policy_metric(tmp_path: Path) -> None:
    doc = base_case(tmp_path, "a.json", "b.json")
    doc["policy"]["primary_metric"] = "gpu_temp_c"
    p = write_case(tmp_path, doc)
    with pytest.raises(CaseConfigError):
        load_case_config(p)


def test_rejects_negative_threshold(tmp_path: Path) -> None:
    doc = base_case(tmp_path, "a.json", "b.json")
    doc["policy"]["min_relative_improvement"] = -0.1
    p = write_case(tmp_path, doc)
    with pytest.raises(CaseConfigError):
        load_case_config(p)


def test_supplemental_evidence_parsed(tmp_path: Path) -> None:
    doc = base_case(tmp_path, "a.json", "b.json")
    doc["supplemental_evidence"] = [
        {
            "id": "arithmetic",
            "kind": "operator_attested",
            "status": "pass",
            "source": "REPORT.md",
            "sha256": "2" * 64,
        }
    ]
    p = write_case(tmp_path, doc)
    cfg = load_case_config(p)
    assert len(cfg.supplemental) == 1
    assert cfg.supplemental[0].kind == "operator_attested"
    assert cfg.supplemental[0].status == "pass"


def test_supplemental_bad_status_rejected(tmp_path: Path) -> None:
    doc = base_case(tmp_path, "a.json", "b.json")
    doc["supplemental_evidence"] = [
        {
            "id": "x",
            "kind": "operator_attested",
            "status": "maybe",
            "source": "R.md",
            "sha256": "3" * 64,
        }
    ]
    p = write_case(tmp_path, doc)
    with pytest.raises(CaseConfigError):
        load_case_config(p)


def test_supplemental_conflicting_ids_rejected(tmp_path: Path) -> None:
    """F2: two supplemental entries with the same gate id are a config error (exit 2),
    regardless of whether their statuses agree — silent last-one-wins merging is
    forbidden in a fail-closed decision system."""
    doc = base_case(tmp_path, "a.json", "b.json")
    doc["supplemental_evidence"] = [
        {
            "id": "arithmetic",
            "kind": "operator_attested",
            "status": "pass",
            "source": "R.md",
            "sha256": "2" * 64,
        },
        {
            "id": "arithmetic",
            "kind": "operator_attested",
            "status": "fail",
            "source": "R2.md",
            "sha256": "3" * 64,
        },
    ]
    p = write_case(tmp_path, doc)
    with pytest.raises(CaseConfigError) as ei:
        load_case_config(p)
    assert "arithmetic" in str(ei.value)


def test_supplemental_duplicate_ids_same_status_rejected(tmp_path: Path) -> None:
    """F2: even identical duplicate ids are rejected — uniqueness is the rule."""
    doc = base_case(tmp_path, "a.json", "b.json")
    doc["supplemental_evidence"] = [
        {
            "id": "arithmetic",
            "kind": "operator_attested",
            "status": "pass",
            "source": "R.md",
            "sha256": "2" * 64,
        },
        {
            "id": "arithmetic",
            "kind": "operator_attested",
            "status": "pass",
            "source": "R2.md",
            "sha256": "3" * 64,
        },
    ]
    p = write_case(tmp_path, doc)
    with pytest.raises(CaseConfigError):
        load_case_config(p)


def test_source_root_must_be_absolute_existing_dir(tmp_path: Path) -> None:
    """F4: source_root must be an absolute, existing directory (fail-closed exit 2)."""
    for bad in (".", "relative/path", "/nonexistent-root-xyz", str(tmp_path / "missing-dir")):
        doc = base_case(tmp_path, "a.json", "b.json")
        doc["source_root"] = bad
        p = write_case(tmp_path, doc, name=f"case-{abs(hash(bad))}.yaml")
        with pytest.raises(CaseConfigError):
            load_case_config(p)


def test_source_root_accepts_absolute_existing_dir(tmp_path: Path) -> None:
    """F4: the happy path — an absolute existing directory — must still load."""
    p = write_case(tmp_path, base_case(tmp_path, "a.json", "b.json"))
    assert load_case_config(p).source_root == str(tmp_path)


def test_supplemental_bad_sha256_rejected(tmp_path: Path) -> None:
    """F4: supplemental sha256 must be 64-hex, same as baseline/candidate refs."""
    for bad in ("xyz", "0" * 63, "z" * 64, ""):
        doc = base_case(tmp_path, "a.json", "b.json")
        doc["supplemental_evidence"] = [
            {
                "id": "arithmetic",
                "kind": "operator_attested",
                "status": "pass",
                "source": "R.md",
                "sha256": bad,
            }
        ]
        p = write_case(tmp_path, doc, name=f"case-sha-{abs(hash(bad))}.yaml")
        with pytest.raises(CaseConfigError):
            load_case_config(p)


# ------------------------------------------------------------------- adapters


def _serial_request(completion_tokens: int, **rates: float) -> dict:
    req: dict = {
        "prompt_tokens": 100,
        "completion_tokens": completion_tokens,
        "finish_reason": "length",
    }
    req.update(rates)
    return req


def test_serial_adapter_multi_workload_uses_own_block_values() -> None:
    """Regression: per-workload values must come from that workload's block.

    The adapter historically defined ``med``/``group_med`` closures over loop
    variables (``block``, ``workload_id``, ``requests``). Late-binding of those
    loop variables would silently attribute the LAST workload's values to every
    earlier workload — a silent accuracy bug. This pins the per-workload
    extraction behavior: each workload must read its own block-level medians
    and its own request list, never another workload's.
    """
    doc = {
        "schema_version": DSKAB_SCHEMA,
        "results": {
            "edit_cold": {
                "requests": [
                    _serial_request(
                        1200,
                        decode_tokens_per_s=25.0,
                        e2e_output_tokens_per_s=24.0,
                        ttft_s=1.5,
                        latency_s=50.0,
                    )
                ],
                "median_decode_tokens_per_s": 25.62,
                "median_e2e_output_tokens_per_s": 24.94,
                "median_ttft_s": 1.317,
                "median_latency_s": 48.11,
            },
            # no block-level medians: values must be medianed from this
            # workload's own request list, not the other workloads'.
            "fresh_code": {
                "requests": [
                    _serial_request(
                        400,
                        decode_tokens_per_s=9.0,
                        e2e_output_tokens_per_s=8.5,
                        ttft_s=2.0,
                        latency_s=40.0,
                    )
                ]
            },
            "edit_warm_prefix": {
                "requests": [
                    _serial_request(
                        1200,
                        decode_tokens_per_s=60.0,
                        e2e_output_tokens_per_s=59.0,
                        ttft_s=1.1,
                        latency_s=21.0,
                    )
                ],
                "median_decode_tokens_per_s": 63.27,
                "median_e2e_output_tokens_per_s": 59.41,
                "median_ttft_s": 1.27,
                "median_latency_s": 20.2,
            },
        },
    }
    result = extract_samples(doc, source_artifact="multi.json")
    by = {(s.metric_id, s.dimensions.workload_id): s.value for s in result.samples}
    # first workload keeps its own block values (not the last workload's)
    assert by[("decode_tokens_per_s", "edit_cold")] == 25.62
    assert by[("ttft_s", "edit_cold")] == 1.317
    # middle workload falls back to its own request list
    assert by[("decode_tokens_per_s", "fresh_code")] == 9.0
    assert by[("ttft_s", "fresh_code")] == 2.0
    # last workload keeps its own block values
    assert by[("decode_tokens_per_s", "edit_warm_prefix")] == 63.27
    assert by[("ttft_s", "edit_warm_prefix")] == 1.27


def test_group_adapter_multi_workload_uses_own_block_values() -> None:
    """Same late-binding regression for group (concurrency) workloads."""

    def _group(wall: float, agg: float, tokens: int) -> dict:
        return {
            "wall_s": wall,
            "total_completion_tokens": tokens,
            "aggregate_output_tokens_per_s": agg,
            "requests": [
                _serial_request(tokens // 4) for _ in range(4)
            ],
        }

    doc = {
        "schema_version": SGLANG_SCHEMA,
        "results": {
            "concurrency4_short_256": {
                "groups": [_group(10.5, 96.84, 1024)],
                "median_aggregate_output_tokens_per_s": 96.84,
                "median_wall_s": 10.5,
            },
            # block median absent: must fall back to this group's values
            "concurrency4_ctx_128": {
                "groups": [_group(20.0, 20.1, 512)],
            },
            "concurrency4_long_512": {
                "groups": [_group(30.0, 55.5, 2048)],
                "median_aggregate_output_tokens_per_s": 40.2,
                "median_wall_s": 30.0,
            },
        },
    }
    result = extract_samples(doc, source_artifact="multi_groups.json")
    by = {(s.metric_id, s.dimensions.workload_id): s.value for s in result.samples}
    assert by[("aggregate_output_tokens_per_s", "concurrency4_short_256")] == 96.84
    assert by[("aggregate_output_tokens_per_s", "concurrency4_ctx_128")] == 20.1
    assert by[("aggregate_output_tokens_per_s", "concurrency4_long_512")] == 40.2
    # dimensions must reference each workload's own concurrency/budget
    samples = {
        (s.metric_id, s.dimensions.workload_id): s for s in result.samples
    }
    assert samples[("aggregate_output_tokens_per_s", "concurrency4_ctx_128")].dimensions.output_budget == 128
    assert samples[("aggregate_output_tokens_per_s", "concurrency4_long_512")].dimensions.output_budget == 512


def test_known_schemas() -> None:
    assert set(known_schemas()) == {DSKAB_SCHEMA, SGLANG_SCHEMA}


def test_dspark_adapter_extracts_primary_metrics(tmp_path: Path) -> None:
    make_dspark_ab_fixture(tmp_path, decode=63.27, e2e=59.41, ttft=1.27)
    doc = json.loads((tmp_path / "dspark_fixture.json").read_text())
    result = extract_samples(doc, source_artifact="dspark_fixture.json")
    assert isinstance(result, AdapterResult)
    assert result.schema_version == DSKAB_SCHEMA
    samples = {(s.metric_id, s.dimensions.workload_id) for s in result.samples}
    assert ("decode_tokens_per_s", "edit_cold") in samples
    assert ("e2e_output_tokens_per_s", "edit_cold") in samples
    assert ("ttft_s", "edit_cold") in samples
    by = {(s.metric_id, s.dimensions.workload_id): s for s in result.samples}
    assert by[("decode_tokens_per_s", "edit_cold")].value == 63.27
    # dimensions match the spec's fixed procedure
    d = by[("decode_tokens_per_s", "edit_cold")].dimensions
    assert d.procedure_version == "v1"
    assert d.workload_id == "edit_cold"


def test_dspark_adapter_rejects_non_finite(tmp_path: Path) -> None:
    p = tmp_path / "nan.json"
    doc = {
        "schema_version": DSKAB_SCHEMA,
        "results": {
            "edit_cold": {
                "requests": [{"completion_tokens": 100, "decode_tokens_per_s": 1.0}],
                "median_decode_tokens_per_s": float("nan"),
            }
        },
    }
    p.write_text(json.dumps(doc), encoding="utf-8")
    from serving_verdict.errors import CanonicalizationError

    with pytest.raises(CanonicalizationError):
        extract_samples(json.loads(p.read_text()), source_artifact="nan.json")


def test_dspark_adapter_unknown_schema_raises(tmp_path: Path) -> None:
    with pytest.raises(UnknownSchemaError):
        extract_samples({"schema_version": "vendor.mystery.v9"}, source_artifact="x.json")


def test_dspark_adapter_missing_results_raises(tmp_path: Path) -> None:
    with pytest.raises(UnknownSchemaError):
        extract_samples({"schema_version": DSKAB_SCHEMA}, source_artifact="x.json")


def test_sglang_adapter_extracts(tmp_path: Path) -> None:
    make_sglang_ab_fixture(tmp_path, decode=36.10, ttft=0.219)
    doc = json.loads((tmp_path / "sglang_fixture.json").read_text())
    result = extract_samples(doc, source_artifact="sglang_fixture.json")
    assert result.schema_version == SGLANG_SCHEMA
    by = {(s.metric_id, s.dimensions.workload_id): s for s in result.samples}
    assert by[("decode_tokens_per_s", "short_decode_512")].value == 36.10
    assert by[("ttft_s", "short_decode_512")].value == 0.219
    assert by[("aggregate_output_tokens_per_s", "concurrency4_short_256")].value == 96.84


def test_adapter_output_deterministic(tmp_path: Path) -> None:
    a = make_dspark_ab_fixture(tmp_path, filename="det_a.json")
    b = make_dspark_ab_fixture(tmp_path, filename="det_b.json")
    da = extract_samples(json.loads(a.read_text()))
    db = extract_samples(json.loads(b.read_text()))
    assert da.samples == db.samples
