"""Pareto frontier over TTFT/decode/concurrency/memory/quality (v0.4).

Each trial contributes a five-objective vector, all derived from the sealed
summary measurements:

  - ``ttft_s``            lower_better
  - ``decode_latency_ms`` lower_better
  - ``concurrency_max``   higher_better
  - ``peak_memory_gb``    lower_better
  - ``quality_score``     higher_better

Pipeline (deterministic, pure):
  1. **Constraints filter infeasible trials first.** Any trial with an
     UNMEASURABLE objective, or an objective outside its inclusive
     constraint range, is dropped into ``infeasible`` before any frontier
     math. An empty or malformed constraint set is a ``PlanError``.
  2. **Domination.** Trial ``a`` dominates ``b`` iff ``a`` is >= on every
     objective (direction-aware) and strictly better on at least one.
     Equal objectives never dominate.
  3. **Frontier.** Non-dominated feasible trials, ordered deterministically
     by ``trial_id`` (the seed is recorded for provenance; the frontier order
     itself is a function of the data, so results are stable across seeds).
  4. **Honest outcome.** Exactly one frontier trial -> ``SINGLE_WINNER``;
     more than one -> ``NO_SINGLE_WINNER`` (a real multi-objective tie,
     never collapsed into a fabricated winner); none feasible ->
     ``NO_FEASIBLE_TRIALS``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from serving_verdict.experiment_artifact import seal_artifact, verify_artifact
from serving_verdict.errors import PlanError
from serving_verdict.summary import UNMEASURABLE

PARETO_SCHEMA_VERSION = "serving-verdict.pareto.v0.1"

SINGLE_WINNER = "SINGLE_WINNER"
NO_SINGLE_WINNER = "NO_SINGLE_WINNER"
NO_FEASIBLE_TRIALS = "NO_FEASIBLE_TRIALS"

#: Objective -> direction. Fixed; part of the schema.
OBJECTIVE_METRICS: frozenset[str] = frozenset(
    {
        "ttft_s",
        "decode_latency_ms",
        "concurrency_max",
        "peak_memory_gb",
        "quality_score",
    }
)
OBJECTIVE_DIRECTIONS: dict[str, str] = {
    "ttft_s": "lower_better",
    "decode_latency_ms": "lower_better",
    "concurrency_max": "higher_better",
    "peak_memory_gb": "lower_better",
    "quality_score": "higher_better",
}

Bound = float | None


@dataclass(frozen=True)
class Trial:
    """One evaluated (or planned-for-evaluation) configuration."""

    trial_id: str
    params: dict[str, Any]
    objectives: dict[str, float | str]


@dataclass(frozen=True)
class ParetoResult:
    result: str  # SINGLE_WINNER | NO_SINGLE_WINNER | NO_FEASIBLE_TRIALS
    frontier: tuple[Trial, ...]
    dominated: tuple[Trial, ...]
    infeasible: tuple[Trial, ...]
    seed: int
    constraints: dict[str, tuple[Bound, Bound]] = field(default_factory=dict)


def _better(a: float, b: float, direction: str) -> bool:
    return a < b if direction == "lower_better" else a > b


def _at_least_as_good(a: float, b: float, direction: str) -> bool:
    return a <= b if direction == "lower_better" else a >= b


def _validate_constraints(constraints: dict[str, Any]) -> None:
    if not isinstance(constraints, dict):
        raise PlanError("constraints must be a mapping of objective -> (min, max)")
    for metric, bounds in constraints.items():
        if metric not in OBJECTIVE_METRICS:
            raise PlanError(f"constraint on unknown objective metric: {metric!r}")
        if not isinstance(bounds, (tuple, list)) or len(bounds) != 2:
            raise PlanError(f"constraint for {metric!r} must be a (min, max) pair")
        lo, hi = bounds
        for v in (lo, hi):
            if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float))):
                raise PlanError(f"constraint bound for {metric!r} must be a number or null")
        if lo is not None and hi is not None and float(lo) > float(hi):
            raise PlanError(f"constraint for {metric!r} has min > max")
        if lo is None and hi is None:
            raise PlanError(f"constraint for {metric!r} must set at least one bound")


def _feasible(trial: Trial, constraints: dict[str, tuple[Bound, Bound]]) -> bool:
    for metric, bounds in constraints.items():
        value = trial.objectives.get(metric)
        if value == UNMEASURABLE or value is None:
            return False
        lo, hi = bounds
        if lo is not None and float(value) < float(lo):
            return False
        if hi is not None and float(value) > float(hi):
            return False
    # A trial with any UNMEASURABLE objective cannot be ranked honestly.
    return all(
        trial.objectives.get(metric) != UNMEASURABLE for metric in OBJECTIVE_METRICS
    )


def _dominates(a: Trial, b: Trial) -> bool:
    """True iff a strictly dominates b (better on all, strictly better on one)."""
    any_strict = False
    for metric in sorted(OBJECTIVE_METRICS):
        direction = OBJECTIVE_DIRECTIONS[metric]
        va, vb = float(a.objectives[metric]), float(b.objectives[metric])
        if not _at_least_as_good(va, vb, direction):
            return False
        if _better(va, vb, direction):
            any_strict = True
    return any_strict


def compute_pareto_frontier(
    trials: list[Trial],
    constraints: dict[str, tuple[Bound, Bound]],
    seed: int,
) -> ParetoResult:
    """Constraints first, then domination, then deterministic frontier order."""
    _validate_constraints(constraints)
    seen: set[str] = set()
    for trial in trials:
        if trial.trial_id in seen:
            raise PlanError(f"duplicate trial_id: {trial.trial_id!r}")
        seen.add(trial.trial_id)

    feasible: list[Trial] = []
    infeasible: list[Trial] = []
    for trial in sorted(trials, key=lambda t: t.trial_id):
        (feasible if _feasible(trial, constraints) else infeasible).append(trial)

    if not feasible:
        return ParetoResult(
            result=NO_FEASIBLE_TRIALS,
            frontier=(),
            dominated=(),
            infeasible=tuple(infeasible),
            seed=seed,
            constraints=constraints,
        )

    dominated = [b for b in feasible if any(_dominates(a, b) for a in feasible if a is not b)]
    dominated_ids = {t.trial_id for t in dominated}
    frontier = tuple(t for t in feasible if t.trial_id not in dominated_ids)
    dominated_sorted = tuple(t for t in sorted(dominated, key=lambda t: t.trial_id))

    result = SINGLE_WINNER if len(frontier) == 1 else NO_SINGLE_WINNER
    return ParetoResult(
        result=result,
        frontier=frontier,
        dominated=dominated_sorted,
        infeasible=tuple(infeasible),
        seed=seed,
        constraints=constraints,
    )


# ---------------------------------------------------------------------------
# canonical artifact
# ---------------------------------------------------------------------------


def _trial_dict(trial: Trial) -> dict[str, Any]:
    return {
        "trial_id": trial.trial_id,
        "params": dict(trial.params),
        "objectives": {m: trial.objectives.get(m) for m in sorted(OBJECTIVE_METRICS)},
    }


def build_pareto_artifact(result: ParetoResult, created_at: str) -> dict[str, Any]:
    """Seal a Pareto result into a canonical artifact with a provenance ID."""
    identity = {
        "kind": "pareto",
        "schema_version": PARETO_SCHEMA_VERSION,
        "seed": result.seed,
        "constraints": {
            m: [lo, hi] for m, (lo, hi) in sorted(result.constraints.items())
        },
        "trials": [_trial_dict(t) for t in sorted(
            list(result.frontier) + list(result.dominated) + list(result.infeasible),
            key=lambda t: t.trial_id,
        )],
    }
    payload: dict[str, Any] = {
        "result": result.result,
        "seed": result.seed,
        "constraints": {m: [lo, hi] for m, (lo, hi) in sorted(result.constraints.items())},
        "frontier": [_trial_dict(t) for t in result.frontier],
        "dominated": [_trial_dict(t) for t in result.dominated],
        "infeasible": [_trial_dict(t) for t in result.infeasible],
    }
    return seal_artifact(PARETO_SCHEMA_VERSION, identity, payload, created_at)


def verify_pareto_artifact(artifact: Any) -> dict[str, Any]:
    """Fail-closed verification of a Pareto artifact."""
    return verify_artifact(
        artifact,
        PARETO_SCHEMA_VERSION,
        (
            "schema_version",
            "provenance_id",
            "result",
            "seed",
            "constraints",
            "frontier",
            "dominated",
            "infeasible",
            "created_at",
            "artifact_digest",
        ),
    )
