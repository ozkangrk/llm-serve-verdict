"""Inference Lab runtime → repeated benchmark → telemetry → sealed evidence.

The orchestrator publishes no lab artifact until the owned lifecycle has stopped
and removed its resources and verified their absence. Runtime/container work is
injected through the narrow lifecycle and endpoint capabilities.
"""
from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from math import floor, fsum, isfinite
from threading import Event, Thread
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlsplit

from serving_verdict.artifact import verify_artifact
from serving_verdict.canonical import canonicalize, digest_payload
from serving_verdict.errors import IntegrityError
from serving_verdict.lab_lifecycle import (
    LabLifecycle,
    LabState,
    LifecyclePlan,
)
from serving_verdict.lab_planner import LabRunSpec
from serving_verdict.lab_telemetry import (
    MetricBinding,
    TelemetryBuffer,
    TelemetryError,
    TelemetryFailure,
    TelemetrySample,
    parse_prometheus_snapshot,
)

_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

_SCHEMA = "serving-verdict.lab-run.v0.5"
_RUN_SPEC_KEYS = {
    "schema_version",
    "run_id",
    "plan_digest",
    "template_id",
    "template_version",
    "template_digest",
    "engine",
    "image",
    "effective_argv",
    "model_ref",
    "model_manifest_digest",
    "benchmark_profile_digest",
    "trial_count",
    "statistical_seed",
    "telemetry_interval_s",
    "telemetry_max_samples",
}
_TELEMETRY_SAMPLE_KEYS = {
    "offset_s",
    "metric_id",
    "source_name",
    "value",
    "unit",
    "direction",
    "procedure_version",
    "labels",
    "status",
}


class LabOrchestrationError(RuntimeError):
    """A lab orchestration contract or final artifact is invalid."""


@runtime_checkable
class LabEndpoint(Protocol):
    @property
    def endpoint_url(self) -> str: ...

    @property
    def metrics_url(self) -> str: ...


TrialRunner = Callable[[str, int, float], dict[str, Any]]
TelemetryFetcher = Callable[[str, float], bytes]


def benchmark_profile_binding_digest(
    *,
    profile_name: str,
    procedure_version: str,
    protocol_hash: str,
    workload_hash: str,
) -> str:
    if not all(
        isinstance(value, str) and value
        for value in (profile_name, procedure_version)
    ) or not all(
        isinstance(value, str) and _DIGEST_RE.fullmatch(value) is not None
        for value in (protocol_hash, workload_hash)
    ):
        raise LabOrchestrationError("benchmark profile binding is invalid")
    return digest_payload(
        canonicalize(
            {
                "schema_version": "serving-verdict.benchmark-profile-binding.v1",
                "profile_name": profile_name,
                "procedure_version": procedure_version,
                "protocol_hash": protocol_hash,
                "workload_hash": workload_hash,
            }
        )
    )


@dataclass(frozen=True, slots=True)
class LabOrchestrationResult:
    state: LabState
    artifact: dict[str, Any] | None
    error_kind: str | None
    events: tuple[LabState, ...]
    cleanup_verified: bool


def _sample_doc(sample: TelemetrySample) -> dict[str, Any]:
    return {
        "offset_s": sample.offset_s,
        "metric_id": sample.metric_id,
        "source_name": sample.source_name,
        "value": sample.value,
        "unit": sample.unit,
        "direction": sample.direction,
        "procedure_version": sample.procedure_version,
        "labels": [list(pair) for pair in sample.labels],
        "status": sample.status,
    }


def _failure_doc(failure: TelemetryFailure) -> dict[str, Any]:
    return {"offset_s": failure.offset_s, "status": failure.status}


def _percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * quantile
    lower = floor(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    try:
        result = fsum(
            (
                ordered[lower] * (1.0 - fraction),
                ordered[upper] * fraction,
            )
        )
    except OverflowError as exc:
        raise TelemetryError("telemetry percentile overflowed") from exc
    if not isfinite(result):
        raise TelemetryError("telemetry percentile is not finite")
    return result


def _telemetry_summary(samples: tuple[TelemetrySample, ...]) -> list[dict[str, Any]]:
    groups: dict[
        tuple[str, tuple[tuple[str, str], ...], str, str], list[TelemetrySample]
    ] = {}
    for sample in samples:
        key = (sample.metric_id, sample.labels, sample.unit, sample.direction)
        groups.setdefault(key, []).append(sample)
    result: list[dict[str, Any]] = []
    for (metric_id, labels, unit, direction), series in sorted(groups.items()):
        ordered = sorted(series, key=lambda item: item.offset_s)
        values = [item.value for item in ordered]
        try:
            mean = fsum(value / len(values) for value in values)
        except OverflowError as exc:
            raise TelemetryError("telemetry mean overflowed") from exc
        if not isfinite(mean):
            raise TelemetryError("telemetry mean is not finite")
        result.append(
            {
                "metric_id": metric_id,
                "labels": [list(pair) for pair in labels],
                "unit": unit,
                "direction": direction,
                "count": len(values),
                "min": min(values),
                "mean": mean,
                "p50": _percentile(values, 0.50),
                "p95": _percentile(values, 0.95),
                "p99": _percentile(values, 0.99),
                "max": max(values),
                "latest": ordered[-1].value,
            }
        )
    return result


def _plan_doc(plan: LabRunSpec) -> dict[str, Any]:
    return {
        "schema_version": plan.schema_version,
        "run_id": plan.run_id,
        "plan_digest": plan.plan_digest,
        "template_id": plan.template_id,
        "template_version": plan.template_version,
        "template_digest": plan.template_digest,
        "engine": plan.engine,
        "image": plan.image,
        "effective_argv": list(plan.effective_argv),
        "model_ref": plan.model_ref,
        "model_manifest_digest": plan.model_manifest_digest,
        "benchmark_profile_digest": plan.benchmark_profile_digest,
        "trial_count": plan.trial_count,
        "statistical_seed": plan.statistical_seed,
        "telemetry_interval_s": plan.telemetry_interval_s,
        "telemetry_max_samples": plan.telemetry_max_samples,
    }


def compute_lab_artifact_digest(document: dict[str, Any]) -> str:
    if not isinstance(document, dict) or document.get("schema_version") != _SCHEMA:
        raise LabOrchestrationError("lab artifact schema is invalid")
    return digest_payload(
        canonicalize({key: value for key, value in document.items() if key != "artifact_digest"})
    )


def verify_lab_artifact(document: dict[str, Any]) -> str:
    if not isinstance(document, dict):
        raise LabOrchestrationError("lab artifact must be an object")
    expected_keys = {
        "schema_version",
        "run_spec",
        "runtime",
        "benchmark_trials",
        "telemetry",
        "lifecycle",
        "cleanup",
        "claim_boundary",
        "artifact_digest",
    }
    if set(document) != expected_keys or document.get("schema_version") != _SCHEMA:
        raise LabOrchestrationError("lab artifact schema is invalid")
    stored = document.get("artifact_digest")
    if not isinstance(stored, str) or _DIGEST_RE.fullmatch(stored) is None:
        raise LabOrchestrationError("lab artifact digest is malformed")
    if compute_lab_artifact_digest(document) != stored:
        raise LabOrchestrationError("lab artifact digest mismatch")
    spec = document.get("run_spec")
    trials = document.get("benchmark_trials")
    telemetry = document.get("telemetry")
    lifecycle = document.get("lifecycle")
    cleanup = document.get("cleanup")
    if not isinstance(spec, dict) or not isinstance(trials, list):
        raise LabOrchestrationError("lab artifact run evidence is invalid")
    if set(spec) != _RUN_SPEC_KEYS:
        raise LabOrchestrationError("lab artifact run spec identity is invalid")
    identity_body = {
        key: value for key, value in spec.items() if key not in {"run_id", "plan_digest"}
    }
    expected_plan_digest = digest_payload(canonicalize(identity_body))
    expected_run_id = (
        "lab-" + hashlib.sha256(canonicalize(identity_body)).hexdigest()[:24]
    )
    if (
        spec.get("schema_version") != "serving-verdict.lab-run-spec.v0.5"
        or spec.get("plan_digest") != expected_plan_digest
        or spec.get("run_id") != expected_run_id
    ):
        raise LabOrchestrationError("lab artifact run spec identity is invalid")
    runtime = document.get("runtime")
    if (
        not isinstance(runtime, dict)
        or set(runtime) != {"engine", "image", "template_digest"}
        or runtime.get("engine") != spec.get("engine")
        or runtime.get("image") != spec.get("image")
        or runtime.get("template_digest") != spec.get("template_digest")
    ):
        raise LabOrchestrationError("lab artifact runtime binding is invalid")
    count = spec.get("trial_count")
    if isinstance(count, bool) or not isinstance(count, int) or len(trials) != count:
        raise LabOrchestrationError("lab artifact trial count is invalid")
    for expected_index, trial in enumerate(trials, start=1):
        if (
            not isinstance(trial, dict)
            or set(trial) != {"trial", "run_id", "artifact_digest", "run_status"}
            or trial.get("trial") != expected_index
            or trial.get("run_status") != "ok"
            or not isinstance(trial.get("run_id"), str)
            or re.fullmatch(r"svrun-[0-9a-f]{32}", trial["run_id"]) is None
            or not isinstance(trial.get("artifact_digest"), str)
            or _DIGEST_RE.fullmatch(trial["artifact_digest"]) is None
        ):
            raise LabOrchestrationError("lab artifact trial evidence is invalid")
    if (
        not isinstance(telemetry, dict)
        or set(telemetry) != {"samples", "failures", "summary"}
        or not isinstance(telemetry.get("samples"), list)
        or not isinstance(telemetry.get("failures"), list)
    ):
        raise LabOrchestrationError("lab artifact telemetry is invalid")
    telemetry_limit = spec.get("telemetry_max_samples")
    if (
        isinstance(telemetry_limit, bool)
        or not isinstance(telemetry_limit, int)
        or not 1 <= telemetry_limit <= 3600
        or len(telemetry["samples"]) > telemetry_limit
        or len(telemetry["failures"]) > telemetry_limit
    ):
        raise LabOrchestrationError("lab artifact telemetry is invalid")
    offsets: list[float] = []
    validated_samples: list[TelemetrySample] = []
    for sample in telemetry["samples"]:
        if not isinstance(sample, dict) or set(sample) != _TELEMETRY_SAMPLE_KEYS:
            raise LabOrchestrationError("lab artifact telemetry sample is invalid")
        labels = sample.get("labels")
        if not isinstance(labels, list) or not all(
            isinstance(pair, list) and len(pair) == 2 for pair in labels
        ):
            raise LabOrchestrationError("lab artifact telemetry sample is invalid")
        try:
            validated = TelemetrySample(
                offset_s=sample["offset_s"],
                metric_id=sample["metric_id"],
                source_name=sample["source_name"],
                value=sample["value"],
                unit=sample["unit"],
                direction=sample["direction"],
                procedure_version=sample["procedure_version"],
                labels=tuple((pair[0], pair[1]) for pair in labels),
                status=sample["status"],
            )
        except (TelemetryError, KeyError, TypeError) as exc:
            raise LabOrchestrationError("lab artifact telemetry sample is invalid") from exc
        offsets.append(validated.offset_s)
        validated_samples.append(validated)
    if offsets != sorted(offsets):
        raise LabOrchestrationError("lab artifact telemetry sample order is invalid")
    for failure in telemetry["failures"]:
        if not isinstance(failure, dict) or set(failure) != {"offset_s", "status"}:
            raise LabOrchestrationError("lab artifact telemetry failure is invalid")
        try:
            TelemetryFailure(failure["offset_s"], failure["status"])
        except (TelemetryError, KeyError, TypeError) as exc:
            raise LabOrchestrationError("lab artifact telemetry failure is invalid") from exc
    if telemetry.get("summary") != _telemetry_summary(tuple(validated_samples)):
        raise LabOrchestrationError("lab artifact telemetry summary is invalid")
    if (
        not isinstance(lifecycle, dict)
        or lifecycle.get("terminal_state") != LabState.SUCCEEDED.value
        or not isinstance(lifecycle.get("events"), list)
        or not isinstance(cleanup, dict)
        or cleanup != {"verified": True}
    ):
        raise LabOrchestrationError("lab artifact cleanup proof is invalid")
    if not isinstance(document.get("claim_boundary"), str):
        raise LabOrchestrationError("lab artifact claim boundary is invalid")
    return stored


class LabRunOrchestrator:
    def __init__(
        self,
        *,
        lifecycle: LabLifecycle,
        endpoint: LabEndpoint,
        trial_runner: TrialRunner,
        telemetry_fetcher: TelemetryFetcher,
        telemetry_bindings: tuple[MetricBinding, ...],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not isinstance(lifecycle, LabLifecycle):
            raise LabOrchestrationError("validated LabLifecycle is required")
        if not isinstance(endpoint, LabEndpoint):
            raise LabOrchestrationError("runtime endpoint capability is required")
        if not callable(trial_runner) or not callable(telemetry_fetcher) or not callable(clock):
            raise LabOrchestrationError("orchestrator callbacks must be callable")
        bindings = tuple(telemetry_bindings)
        if not bindings or not all(isinstance(item, MetricBinding) for item in bindings):
            raise LabOrchestrationError("telemetry bindings are invalid")
        if len({item.source_name for item in bindings}) != len(bindings):
            raise LabOrchestrationError("telemetry bindings contain duplicates")
        self._lifecycle = lifecycle
        self._endpoint = endpoint
        self._trial_runner = trial_runner
        self._telemetry_fetcher = telemetry_fetcher
        self._bindings = bindings
        self._clock = clock

    @staticmethod
    def _validate_plan_binding(plan: LabRunSpec, lifecycle: LifecyclePlan) -> None:
        if not isinstance(plan, LabRunSpec) or not isinstance(lifecycle, LifecyclePlan):
            raise LabOrchestrationError("validated run and lifecycle plans are required")
        if (
            lifecycle.run_id != plan.run_id
            or lifecycle.template_digest != plan.template_digest
            or lifecycle.image != plan.image
        ):
            raise LabOrchestrationError("lifecycle plan does not match lab run spec")

    @staticmethod
    def _loopback_url(value: object, name: str) -> str:
        if not isinstance(value, str) or any(ord(char) < 32 for char in value):
            raise LabOrchestrationError(f"runtime {name} URL must be loopback-only")
        try:
            parsed = urlsplit(value)
            port = parsed.port
        except ValueError as exc:
            raise LabOrchestrationError(
                f"runtime {name} URL must be loopback-only"
            ) from exc
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "::1"}
            or parsed.username is not None
            or parsed.password is not None
            or port is None
            or not 1 <= port <= 65535
            or parsed.query
            or parsed.fragment
            or not parsed.path.startswith("/")
        ):
            raise LabOrchestrationError(f"runtime {name} URL must be loopback-only")
        return value

    @staticmethod
    def _context(artifact: dict[str, Any]) -> tuple[Any, ...]:
        try:
            return (
                artifact["endpoint"]["id"],
                artifact["endpoint"]["base_url"],
                artifact["endpoint"]["model"],
                artifact["endpoint"]["remote"],
                artifact["model"]["served"],
                artifact["profile"]["name"],
                artifact["profile"]["procedure_version"],
                artifact["protocol_hash"],
                artifact["workload_hash"],
            )
        except (KeyError, TypeError) as exc:
            raise LabOrchestrationError("benchmark trial context is invalid") from exc

    def execute(
        self, plan: LabRunSpec, lifecycle_plan: LifecyclePlan
    ) -> LabOrchestrationResult:
        self._validate_plan_binding(plan, lifecycle_plan)

        def work(deadline_s: float) -> dict[str, Any]:
            endpoint_url = self._loopback_url(self._endpoint.endpoint_url, "inference")
            metrics_url = self._loopback_url(self._endpoint.metrics_url, "metrics")
            per_trial_deadline = float(deadline_s) / plan.trial_count
            buffer = TelemetryBuffer(
                max_samples=plan.telemetry_max_samples,
                max_series=min(64, max(1, len(self._bindings) * 8)),
            )
            trial_refs: list[dict[str, Any]] = []
            seen_trial_digests: set[str] = set()
            expected_context: tuple[Any, ...] | None = None
            telemetry_started = self._clock()

            def fetch_bounded(timeout_s: float) -> bytes:
                done = Event()
                value: dict[str, Any] = {}

                def invoke() -> None:
                    try:
                        value["body"] = self._telemetry_fetcher(metrics_url, timeout_s)
                    except Exception as exc:
                        value["error"] = exc
                    finally:
                        done.set()

                worker = Thread(
                    target=invoke,
                    name="serving-verdict-lab-telemetry-fetch",
                    daemon=True,
                )
                worker.start()
                if not done.wait(timeout_s):
                    raise TimeoutError("telemetry fetch deadline exceeded")
                if "error" in value:
                    raise RuntimeError("telemetry fetch failed") from value["error"]
                body = value.get("body")
                if not isinstance(body, bytes):
                    raise TelemetryError("telemetry fetch result must be bytes")
                return body

            def scrape_once() -> None:
                offset = max(0.0, float(self._clock() - telemetry_started))
                try:
                    raw = fetch_bounded(
                        min(float(plan.telemetry_interval_s), per_trial_deadline)
                    )
                    samples = parse_prometheus_snapshot(
                        raw,
                        offset_s=offset,
                        bindings=self._bindings,
                        max_response_bytes=64 * 1024,
                        max_series=64,
                    )
                    buffer.append(samples)
                except TelemetryError:
                    buffer.record_failure(offset_s=offset, status="invalid")
                except Exception:
                    buffer.record_failure(offset_s=offset, status="unavailable")

            stop_telemetry = Event()

            def collect_live() -> None:
                while not stop_telemetry.is_set():
                    scrape_once()
                    if stop_telemetry.wait(float(plan.telemetry_interval_s)):
                        return

            collector = Thread(
                target=collect_live,
                name="serving-verdict-lab-telemetry",
                daemon=True,
            )
            collector.start()
            try:
                for index in range(1, plan.trial_count + 1):
                    artifact = self._trial_runner(endpoint_url, index, per_trial_deadline)
                    try:
                        digest = verify_artifact(artifact)
                    except (IntegrityError, KeyError, TypeError) as exc:
                        raise LabOrchestrationError("benchmark trial integrity failed") from exc
                    if artifact.get("run_status") != "ok":
                        raise LabOrchestrationError("benchmark trial hard gate failed")
                    if digest in seen_trial_digests:
                        raise LabOrchestrationError("benchmark trial artifact was replayed")
                    seen_trial_digests.add(digest)
                    context = self._context(artifact)
                    if context[1] != endpoint_url or context[3] is not False:
                        raise LabOrchestrationError("benchmark trial endpoint binding failed")
                    profile_binding = benchmark_profile_binding_digest(
                        profile_name=context[5],
                        procedure_version=context[6],
                        protocol_hash=context[7],
                        workload_hash=context[8],
                    )
                    if profile_binding != plan.benchmark_profile_digest:
                        raise LabOrchestrationError("benchmark trial profile binding failed")
                    if expected_context is None:
                        expected_context = context
                    elif context != expected_context:
                        raise LabOrchestrationError("benchmark trial context drift detected")
                    trial_refs.append(
                        {
                            "trial": index,
                            "run_id": artifact["run_id"],
                            "artifact_digest": digest,
                            "run_status": artifact["run_status"],
                        }
                    )
            finally:
                stop_telemetry.set()
                collector.join(timeout=min(5.0, float(plan.telemetry_interval_s) + 2.0))
            if collector.is_alive():
                raise LabOrchestrationError("telemetry collector did not stop")
            scrape_once()
            return {
                "benchmark_trials": trial_refs,
                "telemetry": {
                    "samples": [_sample_doc(value) for value in buffer.snapshot()],
                    "failures": [_failure_doc(value) for value in buffer.failures()],
                    "summary": _telemetry_summary(buffer.snapshot()),
                },
            }

        lifecycle_result = self._lifecycle.execute(lifecycle_plan, work=work)
        if lifecycle_result.state is not LabState.SUCCEEDED or not lifecycle_result.cleanup_verified:
            return LabOrchestrationResult(
                lifecycle_result.state,
                None,
                lifecycle_result.error_kind,
                lifecycle_result.events,
                lifecycle_result.cleanup_verified,
            )
        if not isinstance(lifecycle_result.result, dict):
            return LabOrchestrationResult(
                LabState.FAILED,
                None,
                "finalization_failed",
                lifecycle_result.events,
                True,
            )
        document: dict[str, Any] = {
            "schema_version": _SCHEMA,
            "run_spec": _plan_doc(plan),
            "runtime": {
                "engine": plan.engine,
                "image": plan.image,
                "template_digest": plan.template_digest,
            },
            "benchmark_trials": lifecycle_result.result["benchmark_trials"],
            "telemetry": lifecycle_result.result["telemetry"],
            "lifecycle": {
                "events": [event.value for event in lifecycle_result.events],
                "terminal_state": lifecycle_result.state.value,
            },
            "cleanup": {"verified": True},
            "claim_boundary": (
                "Bound to the exact local runtime plan, repeated quick-profile trials, "
                "bounded allowlisted telemetry and verified owned-resource cleanup."
            ),
        }
        document["artifact_digest"] = compute_lab_artifact_digest(document)
        verify_lab_artifact(document)
        return LabOrchestrationResult(
            LabState.SUCCEEDED,
            document,
            None,
            lifecycle_result.events,
            True,
        )
