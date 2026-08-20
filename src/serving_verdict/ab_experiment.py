"""Automated repeated baseline/candidate endpoint experiments.

The orchestrator alternates arm order to reduce monotonic drift, preserves every
sealed benchmark run, applies hard serving gates before statistical effect, and
writes one atomic experiment directory. It never stores API-key values.
"""
from __future__ import annotations

import json
import math
import re
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from serving_verdict.artifact import verify_artifact as verify_benchmark_artifact
from serving_verdict.benchmark_runner import RunResult, run_quick_benchmark
from serving_verdict.canonical import canonicalize, digest_payload
from serving_verdict.endpoint import EndpointConfig
from serving_verdict.errors import (
    IntegrityError,
    StatisticalArtifactError,
    StatisticalError,
)
from serving_verdict.profile import BenchmarkProfile
from serving_verdict.statistics import (
    INCONCLUSIVE_INSUFFICIENT_SAMPLE_SIZE,
    INCONCLUSIVE_STATISTICAL_UNCERTAINTY,
    NONPOSITIVE_BASELINE_STATISTIC,
    PROMOTE_ELIGIBLE,
    REJECT_INSUFFICIENT_EFFECT,
    StatisticalSample,
    StatisticalSpec,
    build_statistics_artifact,
    verify_statistics_artifact,
)

AB_EXPERIMENT_SCHEMA = "serving-verdict.ab-experiment.v0.1"
METRIC_ID = "concurrency_output_tokens_per_s"
_WORKLOAD_ID = "quick/concurrency-3"
_UNMEASURABLE = "UNMEASURABLE"
_RUN_FILENAME_RE = re.compile(r"^(baseline|candidate)-trial-([0-9]{3})\.json$")
_RUN_STATUSES = frozenset({"ok", "degraded"})
_MAX_JSON_BYTES = 16 * 1024 * 1024
_MANIFEST_KEYS = frozenset(
    {
        "schema_version",
        "created_at",
        "profile",
        "metric_id",
        "baseline",
        "candidate",
        "spec",
        "execution_order",
        "runs",
        "samples",
        "statistics",
        "decision",
        "claim_boundary",
        "artifact_digest",
    }
)
_PROFILE_KEYS = frozenset({"name", "procedure_version"})
_ENDPOINT_KEYS = frozenset({"id", "base_url", "model", "remote"})
_SPEC_KEYS = frozenset(
    {"trials", "confidence_level", "iterations", "seed", "threshold", "direction"}
)
_RUN_REF_KEYS = frozenset({"trial", "file", "artifact_digest", "run_status"})
_STATISTICS_REF_KEYS = frozenset({"file", "artifact_digest"})
_DECISION_KEYS = frozenset({"verdict", "reason_codes"})


class ABExperimentError(ValueError):
    """The A/B experiment contract, execution, or artifact is invalid."""


@dataclass(frozen=True, slots=True)
class ABExperimentSpec:
    trials: int
    confidence_level: float
    iterations: int
    seed: int
    threshold: float

    def __post_init__(self) -> None:
        if isinstance(self.trials, bool) or not isinstance(self.trials, int):
            raise ABExperimentError("trials must be an int")
        if not 2 <= self.trials <= 20:
            raise ABExperimentError("trials must be in [2, 20]")
        try:
            StatisticalSpec(
                confidence_level=self.confidence_level,
                iterations=self.iterations,
                seed=self.seed,
                min_trials=self.trials,
                threshold=self.threshold,
                direction="higher_better",
            )
        except StatisticalError as exc:
            raise ABExperimentError(str(exc)) from exc

    def statistical_spec(self) -> StatisticalSpec:
        return StatisticalSpec(
            confidence_level=self.confidence_level,
            iterations=self.iterations,
            seed=self.seed,
            min_trials=self.trials,
            threshold=self.threshold,
            direction="higher_better",
        )


@dataclass(frozen=True, slots=True)
class ABExperimentResult:
    manifest: dict[str, Any]
    run_artifacts: dict[str, dict[str, Any]]
    statistics_artifact: dict[str, Any] | None


Runner = Callable[..., RunResult]


def _public_endpoint(config: EndpointConfig) -> dict[str, Any]:
    return {
        "id": config.endpoint_id,
        "base_url": config.base_url,
        "model": config.model,
        "remote": config.remote,
    }


def _metric_value(artifact: dict[str, Any]) -> float | str:
    try:
        value = artifact["aggregates"]["concurrency"][0][
            "aggregate_output_tokens_per_s"
        ]
    except (KeyError, IndexError, TypeError) as exc:
        raise ABExperimentError("benchmark artifact is missing the experiment metric") from exc
    if value == _UNMEASURABLE:
        return _UNMEASURABLE
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ABExperimentError("benchmark experiment metric must be numeric or UNMEASURABLE")
    normalized = float(value)
    if not math.isfinite(normalized) or normalized < 0.0:
        raise ABExperimentError("benchmark experiment metric must be finite and non-negative")
    return normalized


def _contains_exact_string(value: Any, needles: frozenset[str]) -> bool:
    if isinstance(value, str):
        return value in needles
    if isinstance(value, dict):
        return any(
            _contains_exact_string(key, needles)
            or _contains_exact_string(item, needles)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_exact_string(item, needles) for item in value)
    return False


def _run_context(artifact: dict[str, Any]) -> tuple[Any, ...]:
    try:
        return (
            artifact["protocol_hash"],
            artifact["workload_hash"],
            artifact["profile"]["name"],
            artifact["profile"]["procedure_version"],
            artifact["model"]["served"],
        )
    except (KeyError, TypeError) as exc:
        raise ABExperimentError("benchmark protocol/workload/model context is missing") from exc


def _manifest_digest(manifest: dict[str, Any]) -> str:
    payload = {
        key: value
        for key, value in manifest.items()
        if key not in {"created_at", "artifact_digest"}
    }
    return digest_payload(canonicalize(payload))


def _decision_from_statistics(verdict: str) -> dict[str, Any]:
    if verdict == PROMOTE_ELIGIBLE:
        public = "PROMOTE"
    elif verdict == REJECT_INSUFFICIENT_EFFECT:
        public = "REJECT"
    elif verdict in {
        INCONCLUSIVE_STATISTICAL_UNCERTAINTY,
        INCONCLUSIVE_INSUFFICIENT_SAMPLE_SIZE,
        NONPOSITIVE_BASELINE_STATISTIC,
    }:
        public = "INCONCLUSIVE"
    else:  # pragma: no cover - statistics owns the closed vocabulary
        raise ABExperimentError("statistics returned an unsupported verdict")
    return {"verdict": public, "reason_codes": [verdict]}


def _hard_gate_decision(
    baseline_statuses: list[str], candidate_statuses: list[str]
) -> dict[str, Any] | None:
    if any(status != "ok" for status in baseline_statuses):
        return {
            "verdict": "INCONCLUSIVE",
            "reason_codes": ["BASELINE_HARD_GATE_FAILED"],
        }
    if any(status != "ok" for status in candidate_statuses):
        return {
            "verdict": "REJECT",
            "reason_codes": ["CANDIDATE_HARD_GATE_FAILED"],
        }
    return None


def run_ab_experiment(
    baseline: EndpointConfig,
    candidate: EndpointConfig,
    *,
    baseline_api_key: str,
    candidate_api_key: str,
    profile: BenchmarkProfile,
    spec: ABExperimentSpec,
    runner: Runner = run_quick_benchmark,
    transport_timeout_s: float = 120.0,
    created_at: str,
) -> ABExperimentResult:
    """Run repeated alternating A/B trials and seal one decision manifest."""
    if baseline.model != candidate.model:
        raise ABExperimentError("baseline and candidate must request the same requested model")
    if baseline.endpoint_id == candidate.endpoint_id:
        raise ABExperimentError("baseline and candidate endpoint IDs must differ")
    if not isinstance(baseline_api_key, str) or not baseline_api_key:
        raise ABExperimentError("baseline API key is missing")
    if not isinstance(candidate_api_key, str) or not candidate_api_key:
        raise ABExperimentError("candidate API key is missing")
    if not isinstance(created_at, str) or not created_at:
        raise ABExperimentError("created_at must be a non-empty string")

    artifacts: dict[str, dict[str, Any]] = {}
    entries: dict[str, list[dict[str, Any]]] = {"baseline": [], "candidate": []}
    samples: dict[str, list[float | str]] = {"baseline": [], "candidate": []}
    execution_order: list[list[str]] = []
    configs = {"baseline": baseline, "candidate": candidate}
    keys = {"baseline": baseline_api_key, "candidate": candidate_api_key}
    secret_values = frozenset({baseline_api_key, candidate_api_key})
    expected_context: tuple[Any, ...] | None = None

    for trial_index in range(spec.trials):
        order = ["baseline", "candidate"] if trial_index % 2 == 0 else ["candidate", "baseline"]
        execution_order.append(order)
        for arm in order:
            result = runner(
                configs[arm],
                api_key=keys[arm],
                profile=profile,
                transport_timeout_s=transport_timeout_s,
            )
            artifact = result.artifact
            try:
                digest = verify_benchmark_artifact(artifact)
            except IntegrityError as exc:
                raise ABExperimentError("benchmark artifact failed verification") from exc
            if _contains_exact_string(artifact, secret_values):
                raise ABExperimentError("benchmark artifact contains a secret value")
            status = artifact.get("run_status")
            if status not in _RUN_STATUSES:
                raise ABExperimentError("benchmark run_status is unsupported")
            context = _run_context(artifact)
            if expected_context is None:
                expected_context = context
            elif context != expected_context:
                raise ABExperimentError(
                    "benchmark protocol/workload/model context changed between trials"
                )
            filename = f"{arm}-trial-{trial_index + 1:03d}.json"
            artifacts[filename] = artifact
            entries[arm].append(
                {
                    "trial": trial_index + 1,
                    "file": filename,
                    "artifact_digest": digest,
                    "run_status": status,
                }
            )
            samples[arm].append(_metric_value(artifact))

    baseline_statuses = [str(item["run_status"]) for item in entries["baseline"]]
    candidate_statuses = [str(item["run_status"]) for item in entries["candidate"]]
    decision = _hard_gate_decision(baseline_statuses, candidate_statuses)
    statistics_artifact: dict[str, Any] | None = None

    all_numeric = all(
        isinstance(value, float)
        for arm_values in samples.values()
        for value in arm_values
    )
    if decision is None and not all_numeric:
        decision = {
            "verdict": "INCONCLUSIVE",
            "reason_codes": ["METRIC_UNMEASURABLE"],
        }
    elif all_numeric:
        statistics_artifact = build_statistics_artifact(
            StatisticalSample(samples["baseline"]),
            StatisticalSample(samples["candidate"]),
            spec.statistical_spec(),
            metric_id=METRIC_ID,
            workload_id=_WORKLOAD_ID,
            created_at=created_at,
        )
        if decision is None:
            decision = _decision_from_statistics(
                str(statistics_artifact["result"]["verdict"])
            )
    assert decision is not None

    statistics_ref = (
        {
            "file": "statistics.json",
            "artifact_digest": statistics_artifact["artifact_digest"],
        }
        if statistics_artifact is not None
        else None
    )
    manifest: dict[str, Any] = {
        "schema_version": AB_EXPERIMENT_SCHEMA,
        "created_at": created_at,
        "profile": {
            "name": profile.name,
            "procedure_version": profile.procedure_version,
        },
        "metric_id": METRIC_ID,
        "baseline": _public_endpoint(baseline),
        "candidate": _public_endpoint(candidate),
        "spec": {
            "trials": spec.trials,
            "confidence_level": spec.confidence_level,
            "iterations": spec.iterations,
            "seed": spec.seed,
            "threshold": spec.threshold,
            "direction": "higher_better",
        },
        "execution_order": execution_order,
        "runs": entries,
        "samples": samples,
        "statistics": statistics_ref,
        "decision": decision,
        "claim_boundary": (
            "Built-in quick-profile concurrency throughput under the exact recorded "
            "endpoints, model, workload, protocol, hard gates and statistical policy."
        ),
    }
    manifest["artifact_digest"] = _manifest_digest(manifest)
    verify_ab_experiment(manifest, artifacts, statistics_artifact)
    return ABExperimentResult(manifest, artifacts, statistics_artifact)


def verify_ab_experiment(
    manifest: object,
    run_artifacts: dict[str, dict[str, Any]],
    statistics_artifact: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Verify manifest, referenced runs, statistics, and decision fail-closed."""
    if (
        not isinstance(manifest, dict)
        or set(manifest) != _MANIFEST_KEYS
        or manifest.get("schema_version") != AB_EXPERIMENT_SCHEMA
    ):
        raise ABExperimentError("experiment manifest shape is invalid")
    profile_doc = manifest.get("profile")
    if not isinstance(profile_doc, dict) or set(profile_doc) != _PROFILE_KEYS:
        raise ABExperimentError("experiment profile shape is invalid")
    for arm in ("baseline", "candidate"):
        endpoint_doc = manifest.get(arm)
        if not isinstance(endpoint_doc, dict) or set(endpoint_doc) != _ENDPOINT_KEYS:
            raise ABExperimentError("experiment endpoint shape is invalid")
    decision_doc = manifest.get("decision")
    if not isinstance(decision_doc, dict) or set(decision_doc) != _DECISION_KEYS:
        raise ABExperimentError("experiment decision shape is invalid")
    if manifest.get("metric_id") != METRIC_ID:
        raise ABExperimentError("experiment metric is unsupported")
    if not isinstance(manifest.get("claim_boundary"), str):
        raise ABExperimentError("experiment manifest schema is invalid")
    if not isinstance(manifest.get("created_at"), str):
        raise ABExperimentError("experiment created_at is invalid")
    if manifest.get("artifact_digest") != _manifest_digest(manifest):
        raise ABExperimentError("experiment manifest digest mismatch")
    runs = manifest.get("runs")
    if not isinstance(runs, dict) or set(runs) != {"baseline", "candidate"}:
        raise ABExperimentError("experiment run references are invalid")
    spec_doc = manifest.get("spec")
    if not isinstance(spec_doc, dict) or set(spec_doc) != _SPEC_KEYS:
        raise ABExperimentError("experiment spec is invalid")
    if spec_doc.get("direction") != "higher_better":
        raise ABExperimentError("experiment direction is unsupported")
    try:
        ABExperimentSpec(
            trials=cast(Any, spec_doc.get("trials")),
            confidence_level=cast(Any, spec_doc.get("confidence_level")),
            iterations=cast(Any, spec_doc.get("iterations")),
            seed=cast(Any, spec_doc.get("seed")),
            threshold=cast(Any, spec_doc.get("threshold")),
        )
    except (ABExperimentError, TypeError) as exc:
        raise ABExperimentError("experiment spec is invalid") from exc
    trials = spec_doc.get("trials")
    if isinstance(trials, bool) or not isinstance(trials, int) or not 2 <= trials <= 20:
        raise ABExperimentError("experiment trials are invalid")
    expected_order = [
        ["baseline", "candidate"] if index % 2 == 0 else ["candidate", "baseline"]
        for index in range(trials)
    ]
    if manifest.get("execution_order") != expected_order:
        raise ABExperimentError("experiment execution order is invalid")
    referenced: set[str] = set()
    statuses: dict[str, list[str]] = {"baseline": [], "candidate": []}
    measured_samples: dict[str, list[float | str]] = {
        "baseline": [],
        "candidate": [],
    }
    expected_context: tuple[Any, ...] | None = None
    for arm in ("baseline", "candidate"):
        if not isinstance(runs[arm], list) or len(runs[arm]) != trials:
            raise ABExperimentError("experiment run references are invalid")
        endpoint_doc = manifest.get(arm)
        if not isinstance(endpoint_doc, dict):
            raise ABExperimentError("experiment endpoint identity is invalid")
        for expected_trial, item in enumerate(runs[arm], start=1):
            if not isinstance(item, dict):
                raise ABExperimentError("experiment run reference is invalid")
            if set(item) != _RUN_REF_KEYS:
                raise ABExperimentError("experiment run reference shape is invalid")
            filename = item.get("file")
            if not isinstance(filename, str) or filename not in run_artifacts:
                raise ABExperimentError("experiment referenced benchmark artifact is missing")
            match = _RUN_FILENAME_RE.fullmatch(filename)
            if (
                match is None
                or match.group(1) != arm
                or int(match.group(2)) != expected_trial
                or item.get("trial") != expected_trial
            ):
                raise ABExperimentError("experiment run filename is invalid")
            referenced.add(filename)
            artifact = run_artifacts[filename]
            try:
                digest = verify_benchmark_artifact(artifact)
            except IntegrityError as exc:
                raise ABExperimentError("benchmark artifact failed verification") from exc
            if digest != item.get("artifact_digest") or artifact.get("run_status") != item.get("run_status"):
                raise ABExperimentError("benchmark artifact does not match experiment reference")
            status = item["run_status"]
            if status not in _RUN_STATUSES:
                raise ABExperimentError("benchmark run_status is unsupported")
            statuses[arm].append(str(status))
            try:
                artifact_endpoint = artifact["endpoint"]
                if (
                    artifact_endpoint["id"] != endpoint_doc.get("id")
                    or artifact_endpoint["model"] != endpoint_doc.get("model")
                    or artifact_endpoint["base_url"] != endpoint_doc.get("base_url")
                    or artifact_endpoint["remote"] != endpoint_doc.get("remote")
                ):
                    raise ABExperimentError(
                        "benchmark endpoint identity does not match experiment arm"
                    )
            except (KeyError, TypeError) as exc:
                raise ABExperimentError("benchmark endpoint identity is invalid") from exc
            context = _run_context(artifact)
            if (
                context[2] != profile_doc.get("name")
                or context[3] != profile_doc.get("procedure_version")
            ):
                raise ABExperimentError(
                    "benchmark profile does not match experiment profile"
                )
            if expected_context is None:
                expected_context = context
            elif context != expected_context:
                raise ABExperimentError(
                    "benchmark protocol/workload/model context changed between trials"
                )
            measured_samples[arm].append(_metric_value(artifact))
    if referenced != set(run_artifacts):
        raise ABExperimentError("experiment contains unreferenced benchmark artifacts")
    if manifest.get("samples") != measured_samples:
        raise ABExperimentError("experiment samples do not match benchmark artifacts")

    expected = _hard_gate_decision(statuses["baseline"], statuses["candidate"])
    stats_ref = manifest.get("statistics")
    if stats_ref is None:
        if statistics_artifact is not None:
            raise ABExperimentError("unexpected statistics artifact")
        if all(
            isinstance(value, float)
            for arm_values in measured_samples.values()
            for value in arm_values
        ):
            raise ABExperimentError(
                "statistics artifact is required for numeric experiment samples"
            )
        if expected is None:
            expected = {
                "verdict": "INCONCLUSIVE",
                "reason_codes": ["METRIC_UNMEASURABLE"],
            }
    else:
        if (
            not isinstance(stats_ref, dict)
            or set(stats_ref) != _STATISTICS_REF_KEYS
            or stats_ref.get("file") != "statistics.json"
            or statistics_artifact is None
        ):
            raise ABExperimentError("statistics artifact is missing")
        try:
            statistical_result = verify_statistics_artifact(statistics_artifact)
        except StatisticalArtifactError as exc:
            raise ABExperimentError("statistics artifact failed verification") from exc
        if statistics_artifact.get("artifact_digest") != stats_ref.get("artifact_digest"):
            raise ABExperimentError("statistics artifact does not match experiment reference")
        if (
            statistics_artifact.get("baseline_values") != measured_samples["baseline"]
            or statistics_artifact.get("candidate_values") != measured_samples["candidate"]
            or statistics_artifact.get("metric_id") != manifest.get("metric_id")
        ):
            raise ABExperimentError("statistics samples do not match experiment samples")
        statistics_spec = statistics_artifact.get("spec")
        if not isinstance(statistics_spec, dict) or any(
            statistics_spec.get(key) != spec_doc.get(key)
            for key in (
                "confidence_level",
                "iterations",
                "seed",
                "threshold",
                "direction",
            )
        ) or statistics_spec.get("min_trials") != trials:
            raise ABExperimentError("statistics spec does not match experiment spec")
        if expected is None:
            expected = _decision_from_statistics(statistical_result.verdict)
    if manifest.get("decision") != expected:
        raise ABExperimentError("experiment decision does not match bound evidence")
    return {"valid": True, "digest": manifest["artifact_digest"]}


def write_ab_experiment(result: ABExperimentResult, out_dir: str | Path) -> None:
    """Atomically publish all run/statistics/manifest artifacts into a new dir."""
    destination = Path(out_dir)
    if destination.exists():
        raise ABExperimentError("experiment output directory already exists")
    if not destination.parent.is_dir():
        raise ABExperimentError("experiment output parent directory does not exist")
    temp = Path(tempfile.mkdtemp(prefix=f".{destination.name}-", dir=destination.parent))
    try:
        for filename, artifact in sorted(result.run_artifacts.items()):
            (temp / filename).write_text(
                json.dumps(artifact, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        if result.statistics_artifact is not None:
            (temp / "statistics.json").write_text(
                json.dumps(
                    result.statistics_artifact,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
        (temp / "experiment.json").write_text(
            json.dumps(result.manifest, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
        temp.rename(destination)
    except (OSError, TypeError, ValueError) as exc:
        shutil.rmtree(temp, ignore_errors=True)
        raise ABExperimentError("experiment output could not be written atomically") from exc


def _load_bounded_json(path: Path) -> dict[str, Any]:
    try:
        if path.is_symlink() or not path.is_file():
            raise ABExperimentError("experiment artifact must be a regular file")
        with path.open("rb") as handle:
            raw = handle.read(_MAX_JSON_BYTES + 1)
        if not 0 < len(raw) <= _MAX_JSON_BYTES:
            raise ABExperimentError("experiment artifact size is invalid")
        document = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ABExperimentError("experiment artifact could not be loaded") from exc
    if not isinstance(document, dict):
        raise ABExperimentError("experiment artifact must be a JSON object")
    return document


def load_and_verify_ab_experiment(directory: str | Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """Load an atomically published experiment directory and verify every file."""
    root = Path(directory)
    if root.is_symlink() or not root.is_dir():
        raise ABExperimentError("experiment directory does not exist or is unsafe")
    manifest = _load_bounded_json(root / "experiment.json")
    runs = manifest.get("runs")
    if not isinstance(runs, dict) or set(runs) != {"baseline", "candidate"}:
        raise ABExperimentError("experiment run references are invalid")
    filenames: list[str] = []
    for arm in ("baseline", "candidate"):
        entries = runs.get(arm)
        if not isinstance(entries, list):
            raise ABExperimentError("experiment run references are invalid")
        for item in entries:
            if not isinstance(item, dict) or not isinstance(item.get("file"), str):
                raise ABExperimentError("experiment run reference is invalid")
            filename = item["file"]
            if _RUN_FILENAME_RE.fullmatch(filename) is None:
                raise ABExperimentError("experiment run filename is invalid")
            filenames.append(filename)
    if len(filenames) != len(set(filenames)):
        raise ABExperimentError("experiment run filenames are duplicated")
    run_artifacts = {
        filename: _load_bounded_json(root / filename) for filename in filenames
    }
    statistics_ref = manifest.get("statistics")
    statistics_artifact: dict[str, Any] | None
    expected_files = {"experiment.json", *filenames}
    if statistics_ref is None:
        statistics_artifact = None
    else:
        if not isinstance(statistics_ref, dict) or statistics_ref.get("file") != "statistics.json":
            raise ABExperimentError("statistics reference is invalid")
        statistics_artifact = _load_bounded_json(root / "statistics.json")
        expected_files.add("statistics.json")
    try:
        actual_files = {item.name for item in root.iterdir()}
    except OSError as exc:
        raise ABExperimentError("experiment directory could not be listed") from exc
    if actual_files != expected_files:
        raise ABExperimentError("experiment directory contains missing or extra files")
    verification = verify_ab_experiment(
        manifest, run_artifacts, statistics_artifact
    )
    return manifest, verification
