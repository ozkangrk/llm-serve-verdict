"""Seeded one-variable sweep planner (v0.4).

The planner turns a baseline (a sealed summary plus its parameter point) and
an *allowlisted* map of parameter ranges into an ordered list of trials.
Each trial differs from the baseline in **exactly one** variable. The order
is a deterministic function of the seed only (never of dict insertion
order); duplicates are removed; the plan is capped at ``max_trials`` and at
the hard bound ``MAX_TRIALS``.

The planner is pure data: it imports no execution facilities and performs no
shell, process-spawning, or runtime work. It only *plans*; nothing is run
here.
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any

from serving_verdict.experiment_artifact import seal_artifact, verify_artifact
from serving_verdict.errors import PlanError
from serving_verdict.summary import Summary

SWEEP_SCHEMA_VERSION = "serving-verdict.sweep.v0.1"

#: Hard bound on planned trials, independent of the caller's ``max_trials``.
MAX_TRIALS = 1024

#: The allowlist of parameters that may ever appear in a sweep. Anything else
#: is a plan error (injection/out-of-allowlist rejection).
ALLOWLISTED_PARAMETERS: frozenset[str] = frozenset(
    {
        "page_size",
        "num_sms",
        "kv_cache_dtype",
        "max_num_seqs",
        "chunked_prefill_size",
        "mem_fraction_static",
        "cuda_graph_max_bs",
        "schedule_policy",
    }
)

Scalar = int | float | str


@dataclass(frozen=True)
class Trial:
    trial_id: str
    params: dict[str, Scalar]
    changed_param: str
    changed_value: Scalar


@dataclass(frozen=True)
class SweepPlan:
    seed: int
    baseline_sha256: str
    baseline_params: dict[str, Scalar]
    trials: tuple[Trial, ...]
    truncated: bool
    total_points: int


def _validate_scalar(value: Any, param: str, ctx: str) -> Scalar:
    if isinstance(value, bool):
        raise PlanError(
            f"{ctx} range value for {param!r} must be a scalar (int, float, or str), got {value!r}"
        )
    if isinstance(value, (int, str)):
        return value
    if isinstance(value, float):
        return value
    raise PlanError(
        f"{ctx} range value for {param!r} must be a scalar (int, float, or str), got {value!r}"
    )


def _type_mismatch(param: str, value: Scalar, baseline_value: Scalar) -> bool:
    del param
    return type(value) is not type(baseline_value)


def _validate_ranges(
    baseline_params: dict[str, Scalar],
    parameter_ranges: dict[str, Any],
) -> None:
    if not isinstance(parameter_ranges, dict) or not parameter_ranges:
        raise PlanError("parameter_ranges must be a non-empty mapping")
    unknown = set(parameter_ranges) - ALLOWLISTED_PARAMETERS
    if unknown:
        raise PlanError(f"parameter(s) not in allowlist: {sorted(unknown)}")
    extra = set(parameter_ranges) - set(baseline_params)
    if extra:
        raise PlanError(
            f"parameter_ranges must cover exactly the baseline parameters: unexpected {sorted(extra)}"
        )
    for param in sorted(parameter_ranges):
        values = parameter_ranges[param]
        if not isinstance(values, list) or not values:
            raise PlanError(f"range for {param!r} must be a non-empty list")
        if len({repr(v) for v in values}) != len(values):
            raise PlanError(f"range for {param!r} contains duplicate values")
        for value in values:
            _validate_scalar(value, param, "range")
            if _type_mismatch(param, value, baseline_params[param]):
                raise PlanError(
                    f"range value type mismatch for {param!r}: baseline is "
                    f"{baseline_params[param]!r} but range contains {value!r}"
                )
        if baseline_params[param] not in values:
            raise PlanError(
                f"range for {param!r} must contain baseline value {baseline_params[param]!r}"
            )
    missing = set(baseline_params) - set(parameter_ranges)
    if missing:
        raise PlanError(
            f"parameter_ranges must cover exactly the baseline parameters: missing {sorted(missing)}"
        )


def plan_sweep(
    baseline: Summary,
    baseline_params: dict[str, Scalar],
    parameter_ranges: dict[str, Any],
    seed: int,
    max_trials: int,
) -> SweepPlan:
    """Plan a seeded one-variable sweep. Pure; never executes anything."""
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise PlanError("seed must be a non-negative integer")
    if isinstance(max_trials, bool) or not isinstance(max_trials, int):
        raise PlanError("max_trials must be a positive integer")
    if max_trials <= 0:
        raise PlanError(f"max_trials must be >= 1, got {max_trials}")
    if max_trials > MAX_TRIALS:
        raise PlanError(f"max_trials exceeds the hard bound MAX_TRIALS={MAX_TRIALS}")
    if isinstance(baseline_params, bool) or not isinstance(baseline_params, dict):
        raise PlanError("baseline_params must be a mapping")
    _validate_ranges(baseline_params, parameter_ranges)

    # Enumerate single-variable mutation points deterministically (params
    # sorted, range values in given order). The baseline point itself is
    # never a trial.
    points: list[dict[str, Scalar]] = []
    for param in sorted(baseline_params):
        for value in parameter_ranges[param]:
            if value == baseline_params[param]:
                continue  # baseline point: one-variable sweeps change something
            point = dict(baseline_params)
            point[param] = value
            points.append(point)
    total_points = len(points)

    # Seeded shuffle: order is a function of the seed and the canonical
    # (sorted) enumeration, never of dict insertion order.
    rng = random.Random(seed)
    rng.shuffle(points)

    trials: list[Trial] = []
    seen: set[tuple] = set()
    for point in points:
        key = tuple(sorted(point.items(), key=lambda kv: str(kv[0])))
        if key in seen:
            continue
        seen.add(key)
        changed = [k for k in point if point[k] != baseline_params[k]]
        assert len(changed) == 1  # guaranteed by construction
        trials.append(
            Trial(
                trial_id=f"t{len(trials):03d}",
                params=point,
                changed_param=changed[0],
                changed_value=point[changed[0]],
            )
        )
        if len(trials) >= max_trials:
            break
    truncated = total_points > len(trials)
    return SweepPlan(
        seed=seed,
        baseline_sha256=baseline.digest,
        baseline_params=dict(baseline_params),
        trials=tuple(trials),
        truncated=truncated,
        total_points=total_points,
    )


# ---------------------------------------------------------------------------
# canonical artifact
# ---------------------------------------------------------------------------


def _trial_dict(trial: Trial) -> dict[str, Any]:
    return {
        "trial_id": trial.trial_id,
        "params": dict(trial.params),
        "changed_param": trial.changed_param,
        "changed_value": trial.changed_value,
    }


def build_sweep_artifact(plan: SweepPlan, created_at: str) -> dict[str, Any]:
    """Seal a sweep plan into a canonical artifact with a provenance ID."""
    identity = {
        "kind": "sweep",
        "schema_version": SWEEP_SCHEMA_VERSION,
        "baseline_digest": plan.baseline_sha256,
        "baseline_params": plan.baseline_params,
        "seed": plan.seed,
        "trials": [_trial_dict(t) for t in plan.trials],
    }
    payload: dict[str, Any] = {
        "seed": plan.seed,
        "baseline": {"sha256": plan.baseline_sha256, "params": plan.baseline_params},
        "trials": [_trial_dict(t) for t in plan.trials],
        "truncated": plan.truncated,
        "total_points": plan.total_points,
    }
    return seal_artifact(SWEEP_SCHEMA_VERSION, identity, payload, created_at)


def verify_sweep_artifact(artifact: Any) -> dict[str, Any]:
    """Fail-closed verification of a sweep artifact."""
    return verify_artifact(
        artifact,
        SWEEP_SCHEMA_VERSION,
        (
            "schema_version",
            "provenance_id",
            "seed",
            "baseline",
            "trials",
            "truncated",
            "total_points",
            "created_at",
            "artifact_digest",
        ),
    )
