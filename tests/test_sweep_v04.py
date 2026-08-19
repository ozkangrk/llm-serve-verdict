"""Seeded one-variable sweep planner: allowlisted ranges, determinism, bounds.

The planner is pure: it only *plans* trials (parameter point sets). It must
never execute shell commands or run any runtime; planning output is a list of
trials each differing from the baseline in exactly one variable.
"""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

from serving_verdict.errors import ArtifactIntegrityError, PlanError
from serving_verdict.summary import parse_summary_payload
from serving_verdict.sweep import (
    MAX_TRIALS,
    SWEEP_SCHEMA_VERSION,
    build_sweep_artifact,
    plan_sweep,
    verify_sweep_artifact,
)
from tests.helpers_v04 import make_parsed, make_summary

BASELINE_PARAMS: dict[str, float | int | str] = {
    "page_size": 64,
    "num_sms": 12,
    "kv_cache_dtype": "fp8",
}


def _ranges() -> dict[str, Any]:
    return {
        "page_size": [16, 32, 64, 128],
        "num_sms": [8, 12, 16],
        "kv_cache_dtype": ["fp8", "int8"],
    }


def _summary(**overrides: Any) -> dict[str, Any]:
    return make_summary(**overrides)


# ---------------------------------------------------------------------------
# allowlisted ranges + injection rejection
# ---------------------------------------------------------------------------


def test_plan_requires_all_range_keys_to_be_in_allowlist():
    baseline = make_parsed()
    with pytest.raises(PlanError, match="not in allowlist"):
        plan_sweep(
            baseline=baseline,
            baseline_params=BASELINE_PARAMS,
            parameter_ranges={"page_size": [16, 32], "evil_param": [1, 2]},
            seed=1,
            max_trials=10,
        )


def test_plan_rejects_empty_allowlist():
    baseline = make_parsed()
    with pytest.raises(PlanError):
        plan_sweep(
            baseline=baseline,
            baseline_params=BASELINE_PARAMS,
            parameter_ranges={},
            seed=1,
            max_trials=10,
        )


@pytest.mark.parametrize(
    "bad_value",
    [
        [1, "rm -rf /"],
        ["; drop table users"],
        ["$HOME"],
        [True],
        [None],
        [1.5, 2.5],  # non-int for an int baseline
        [[1, 2]],
    ],
)
def test_plan_rejects_non_scalar_or_non_matching_range_values(bad_value):
    baseline = make_parsed()
    with pytest.raises(PlanError, match="range value"):
        plan_sweep(
            baseline=baseline,
            baseline_params=BASELINE_PARAMS,
            parameter_ranges={"page_size": bad_value},
            seed=1,
            max_trials=10,
        )


def test_plan_rejects_range_type_mismatch_with_baseline():
    baseline = make_parsed()
    # kv_cache_dtype is a str baseline; an int range mismatches.
    with pytest.raises(PlanError, match="type mismatch"):
        plan_sweep(
            baseline=baseline,
            baseline_params=BASELINE_PARAMS,
            parameter_ranges={"kv_cache_dtype": [1, 2]},
            seed=1,
            max_trials=10,
        )


def test_plan_rejects_duplicate_range_values():
    baseline = make_parsed()
    with pytest.raises(PlanError, match="duplicate"):
        plan_sweep(
            baseline=baseline,
            baseline_params=BASELINE_PARAMS,
            parameter_ranges={"page_size": [16, 16, 32]},
            seed=1,
            max_trials=10,
        )


def test_plan_rejects_range_without_baseline_value():
    baseline = make_parsed()
    with pytest.raises(PlanError, match="must contain baseline value"):
        plan_sweep(
            baseline=baseline,
            baseline_params=BASELINE_PARAMS,
            parameter_ranges={"page_size": [16, 32]},
            seed=1,
            max_trials=10,
        )


def test_plan_rejects_baseline_param_not_covered_by_ranges():
    baseline = make_parsed()
    # num_sms and kv_cache_dtype have no ranges -> nothing to sweep them;
    # ranges must exactly cover the keys of baseline_params.
    with pytest.raises(PlanError, match="cover exactly"):
        plan_sweep(
            baseline=baseline,
            baseline_params=BASELINE_PARAMS,
            parameter_ranges={"page_size": [16, 32, 64]},
            seed=1,
            max_trials=10,
        )


def test_plan_rejects_baseline_param_missing_from_baseline_params():
    baseline = make_parsed()
    with pytest.raises(PlanError, match="cover exactly"):
        plan_sweep(
            baseline=baseline,
            baseline_params={"page_size": 64},
            parameter_ranges={"page_size": [16, 32, 64], "num_sms": [8, 12]},
            seed=1,
            max_trials=10,
        )


# ---------------------------------------------------------------------------
# seeded determinism + fixed order
# ---------------------------------------------------------------------------


def test_plan_is_seed_deterministic():
    baseline = make_parsed()
    p1 = plan_sweep(
        baseline=baseline,
        baseline_params=BASELINE_PARAMS,
        parameter_ranges=_ranges(),
        seed=42,
        max_trials=50,
    )
    p2 = plan_sweep(
        baseline=baseline,
        baseline_params=BASELINE_PARAMS,
        parameter_ranges=_ranges(),
        seed=42,
        max_trials=50,
    )
    assert p1.trials == p2.trials
    assert [t.trial_id for t in p1.trials] == [t.trial_id for t in p2.trials]


def test_plan_order_is_fixed_for_seed_and_stable_across_keys():
    baseline = make_parsed()
    p1 = plan_sweep(
        baseline=baseline,
        baseline_params=BASELINE_PARAMS,
        parameter_ranges=_ranges(),
        seed=7,
        max_trials=50,
    )
    # Shuffling the dict insertion order of the same ranges must not change
    # the plan (order derives from the seed, not from dict ordering).
    shuffled = {k: _ranges()[k] for k in reversed(list(_ranges()))}
    p2 = plan_sweep(
        baseline=baseline,
        baseline_params=BASELINE_PARAMS,
        parameter_ranges=shuffled,
        seed=7,
        max_trials=50,
    )
    assert [t.params for t in p1.trials] == [t.params for t in p2.trials]


def test_plan_seed_changes_order():
    baseline = make_parsed()
    p1 = plan_sweep(
        baseline=baseline,
        baseline_params=BASELINE_PARAMS,
        parameter_ranges=_ranges(),
        seed=1,
        max_trials=8,
    )
    p2 = plan_sweep(
        baseline=baseline,
        baseline_params=BASELINE_PARAMS,
        parameter_ranges=_ranges(),
        seed=2,
        max_trials=8,
    )
    assert [t.params for t in p1.trials] != [t.params for t in p2.trials]


# ---------------------------------------------------------------------------
# exactly one variable changed from baseline, per trial
# ---------------------------------------------------------------------------


def test_every_trial_changes_exactly_one_variable():
    baseline = make_parsed()
    plan = plan_sweep(
        baseline=baseline,
        baseline_params=BASELINE_PARAMS,
        parameter_ranges=_ranges(),
        seed=3,
        max_trials=40,
    )
    assert plan.trials, "expected at least one trial"
    for trial in plan.trials:
        diffs = [
            k
            for k in BASELINE_PARAMS
            if trial.params[k] != BASELINE_PARAMS[k]
        ]
        assert len(diffs) == 1, f"trial {trial.trial_id} changed {diffs}"
        # All non-changed keys are exactly the baseline values.
        for k, v in trial.params.items():
            if k in diffs:
                assert v != BASELINE_PARAMS[k]
            else:
                assert v == BASELINE_PARAMS[k]


def test_baseline_point_itself_is_never_a_trial():
    baseline = make_parsed()
    plan = plan_sweep(
        baseline=baseline,
        baseline_params=BASELINE_PARAMS,
        parameter_ranges=_ranges(),
        seed=3,
        max_trials=100,
    )
    for trial in plan.trials:
        assert trial.params != BASELINE_PARAMS


# ---------------------------------------------------------------------------
# dedupe + bounded max trials
# ---------------------------------------------------------------------------


def test_plan_dedupes_parameter_points():
    baseline = make_parsed()
    plan = plan_sweep(
        baseline=baseline,
        baseline_params=BASELINE_PARAMS,
        parameter_ranges=_ranges(),
        seed=3,
        max_trials=MAX_TRIALS,
    )
    seen: set[tuple] = set()
    for trial in plan.trials:
        key = tuple(sorted(trial.params.items()))
        assert key not in seen
        seen.add(key)
    # Single-mutation points: 3 page_size variants + 2 num_sms + 1 dtype = 6.
    assert len(plan.trials) == 6  # 3 + 2 + 1 single-variable mutations


def test_plan_caps_at_max_trials():
    baseline = make_parsed()
    plan = plan_sweep(
        baseline=baseline,
        baseline_params=BASELINE_PARAMS,
        parameter_ranges=_ranges(),
        seed=3,
        max_trials=2,
    )
    assert len(plan.trials) == 2
    assert plan.truncated is True


def test_max_trials_bounded_by_constant():
    baseline = make_parsed()
    with pytest.raises(PlanError, match="max_trials"):
        plan_sweep(
            baseline=baseline,
            baseline_params=BASELINE_PARAMS,
            parameter_ranges=_ranges(),
            seed=3,
            max_trials=MAX_TRIALS + 1,
        )
    ok = plan_sweep(
        baseline=baseline,
        baseline_params=BASELINE_PARAMS,
        parameter_ranges=_ranges(),
        seed=3,
        max_trials=MAX_TRIALS,
    )
    assert len(ok.trials) <= MAX_TRIALS


def test_max_trials_must_be_positive():
    baseline = make_parsed()
    for bad in (0, -1):
        with pytest.raises(PlanError, match="max_trials"):
            plan_sweep(
                baseline=baseline,
                baseline_params=BASELINE_PARAMS,
                parameter_ranges=_ranges(),
                seed=3,
                max_trials=bad,
            )


# ---------------------------------------------------------------------------
# provenance + artifact + no runtime execution
# ---------------------------------------------------------------------------


def test_sweep_provenance_and_artifact_roundtrip():
    baseline = make_parsed()
    plan = plan_sweep(
        baseline=baseline,
        baseline_params=BASELINE_PARAMS,
        parameter_ranges=_ranges(),
        seed=3,
        max_trials=10,
    )
    assert plan.baseline_sha256 == baseline.digest
    assert plan.seed == 3
    art = build_sweep_artifact(plan, created_at="2026-08-19T00:00:00+00:00")
    assert art["schema_version"] == SWEEP_SCHEMA_VERSION
    assert art["provenance_id"].startswith("prov:")
    art2 = build_sweep_artifact(plan, created_at="2027-01-01T00:00:00+00:00")
    assert art2["provenance_id"] == art["provenance_id"]
    assert art2["artifact_digest"] == art["artifact_digest"]
    assert verify_sweep_artifact(art)["valid"] is True


def test_sweep_artifact_detects_tamper():
    baseline = make_parsed()
    plan = plan_sweep(
        baseline=baseline,
        baseline_params=BASELINE_PARAMS,
        parameter_ranges=_ranges(),
        seed=3,
        max_trials=10,
    )
    art = build_sweep_artifact(plan, created_at="t")
    bad = copy.deepcopy(art)
    bad["trials"][0]["params"]["page_size"] = 4096
    with pytest.raises(ArtifactIntegrityError):
        verify_sweep_artifact(bad)


def test_planner_has_no_execution_capability():
    # The planner module must not import subprocess/os/shell facilities for
    # execution; planning is pure data.
    import serving_verdict.sweep as sweep_mod

    path = sweep_mod.__file__
    assert path is not None
    source = Path(path).read_text(encoding="utf-8")
    for forbidden in ("subprocess", "popen", "os.system", "eval(", "exec("):
        assert forbidden not in source, f"forbidden execution facility {forbidden!r}"


def test_plan_from_raw_documents_end_to_end():
    doc = _summary()
    baseline = parse_summary_payload(doc)
    plan = plan_sweep(
        baseline=baseline,
        baseline_params=BASELINE_PARAMS,
        parameter_ranges=_ranges(),
        seed=11,
        max_trials=10,
    )
    assert plan.baseline_sha256 == doc["digest"]
    assert all(len(t.params) == 3 for t in plan.trials)
