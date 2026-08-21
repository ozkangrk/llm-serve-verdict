"""Bounded, environment-gated in-memory Lab jobs and Live snapshots."""
from __future__ import annotations

import copy
import math
import re
import threading
import uuid
from collections import OrderedDict
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Any

from serving_verdict.lab_lifecycle import LabState
from serving_verdict.lab_orchestrator import (
    summarize_telemetry,
    telemetry_failure_document,
    telemetry_sample_document,
)
from serving_verdict.lab_telemetry import TelemetryFailure, TelemetrySample
from serving_verdict.lab_templates import builtin_template_ids

_REQUEST_AUTHORITY = object()
_TERMINAL = frozenset(
    {LabState.SUCCEEDED, LabState.FAILED, LabState.CANCELLED, LabState.CLEANUP_FAILED}
)
_SAFE_ERROR_KINDS = frozenset(
    {
        "executor_failed",
        "lifecycle_failed",
        "work_failed",
        "cleanup_failed",
        "finalization_failed",
    }
)
_OVERRIDE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_JOB_RE = re.compile(r"^[0-9a-f]{32}$")
_PROGRESS_ORDER = {
    state: index
    for index, state in enumerate(
        (
            LabState.PULLING,
            LabState.NETWORK_CREATING,
            LabState.STARTING,
            LabState.READY,
            LabState.BENCHMARKING,
            LabState.FINALIZING,
            LabState.STOPPING,
        )
    )
}


class LabJobError(RuntimeError):
    """Safe Lab job request, state, or capacity failure."""


@dataclass(frozen=True, slots=True)
class LabStartRequest:
    schema_version: str
    template_id: str
    model_ref: str
    overrides: Mapping[str, int | float | str]
    trial_count: int
    statistical_seed: int
    telemetry_interval_s: int
    telemetry_max_samples: int
    _authority: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._authority is not _REQUEST_AUTHORITY:
            raise LabJobError("LabStartRequest must be created by the trusted parser")
        if self.schema_version != "serving-verdict.lab-start.v0.5":
            raise LabJobError("lab start payload schema is invalid")
        if self.template_id not in builtin_template_ids():
            raise LabJobError("runtime template is not allowlisted")
        object.__setattr__(self, "model_ref", _validate_model_ref(self.model_ref))
        object.__setattr__(
            self,
            "overrides",
            MappingProxyType(_normalize_overrides(self.overrides)),
        )
        _bounded_int(self.trial_count, "trial_count", 2, 20)
        _bounded_int(self.statistical_seed, "statistical_seed", 0, 2**63 - 1)
        _bounded_int(self.telemetry_interval_s, "telemetry_interval_s", 1, 60)
        _bounded_int(self.telemetry_max_samples, "telemetry_max_samples", 1, 3600)

    def public_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "template_id": self.template_id,
            "model_ref": self.model_ref,
            "overrides": dict(self.overrides),
            "trial_count": self.trial_count,
            "statistical_seed": self.statistical_seed,
            "telemetry_interval_s": self.telemetry_interval_s,
            "telemetry_max_samples": self.telemetry_max_samples,
        }


def _bounded_int(value: object, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise LabJobError(f"{name} is out of bounds")
    return value


def _validate_model_ref(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 256
        or "\\" in value
        or "\x00" in value
    ):
        raise LabJobError("model_ref is invalid")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        raise LabJobError("model_ref is invalid")
    return value


def _normalize_overrides(value: object) -> dict[str, int | float | str]:
    if not isinstance(value, Mapping) or len(value) > 16:
        raise LabJobError("runtime overrides are invalid")
    normalized: dict[str, int | float | str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or _OVERRIDE_RE.fullmatch(key) is None:
            raise LabJobError("runtime override key is invalid")
        if isinstance(item, bool) or not isinstance(item, (int, float, str)):
            raise LabJobError("runtime override value is invalid")
        if isinstance(item, float) and not math.isfinite(item):
            raise LabJobError("runtime override value is invalid")
        if isinstance(item, str) and (not item or len(item) > 128):
            raise LabJobError("runtime override value is invalid")
        normalized[key] = item
    return normalized


def parse_lab_start_payload(payload: object) -> LabStartRequest:
    if not isinstance(payload, dict):
        raise LabJobError("lab start payload must be an object")
    expected = {
        "schema_version",
        "template_id",
        "model_ref",
        "overrides",
        "trial_count",
        "statistical_seed",
        "telemetry_interval_s",
        "telemetry_max_samples",
    }
    if set(payload) != expected:
        raise LabJobError("lab start payload schema is invalid")
    if payload.get("schema_version") != "serving-verdict.lab-start.v0.5":
        raise LabJobError("lab start payload schema is invalid")
    template_id = payload.get("template_id")
    if not isinstance(template_id, str) or template_id not in builtin_template_ids():
        raise LabJobError("runtime template is not allowlisted")
    model_ref = _validate_model_ref(payload.get("model_ref"))
    normalized = _normalize_overrides(payload.get("overrides"))
    return LabStartRequest(
        schema_version="serving-verdict.lab-start.v0.5",
        template_id=template_id,
        model_ref=model_ref,
        overrides=normalized,
        trial_count=_bounded_int(payload.get("trial_count"), "trial_count", 2, 20),
        statistical_seed=_bounded_int(
            payload.get("statistical_seed"), "statistical_seed", 0, 2**63 - 1
        ),
        telemetry_interval_s=_bounded_int(
            payload.get("telemetry_interval_s"), "telemetry_interval_s", 1, 60
        ),
        telemetry_max_samples=_bounded_int(
            payload.get("telemetry_max_samples"), "telemetry_max_samples", 1, 3600
        ),
        _authority=_REQUEST_AUTHORITY,
    )


@dataclass(frozen=True, slots=True)
class LiveTelemetryUpdate:
    samples: tuple[TelemetrySample, ...]
    failures: tuple[TelemetryFailure, ...]

    def __post_init__(self) -> None:
        samples = tuple(self.samples)
        failures = tuple(self.failures)
        if (
            len(samples) > 3600
            or len(failures) > 3600
            or not all(isinstance(item, TelemetrySample) for item in samples)
            or not all(isinstance(item, TelemetryFailure) for item in failures)
            or list(samples) != sorted(samples, key=lambda item: item.offset_s)
            or list(failures) != sorted(failures, key=lambda item: item.offset_s)
        ):
            raise LabJobError("live telemetry update is invalid")
        object.__setattr__(self, "samples", samples)
        object.__setattr__(self, "failures", failures)

    def public_payload(self, sequence: int) -> dict[str, Any]:
        return {
            "sequence": sequence,
            "samples": [telemetry_sample_document(item) for item in self.samples],
            "failures": [telemetry_failure_document(item) for item in self.failures],
            "summary": summarize_telemetry(self.samples),
        }


@dataclass(frozen=True, slots=True)
class LabExecutionOutcome:
    state: LabState
    artifact: dict[str, Any] | None
    error_kind: str | None
    cleanup_verified: bool

    def __post_init__(self) -> None:
        if not isinstance(self.state, LabState) or self.state not in _TERMINAL:
            raise LabJobError("lab execution outcome state is invalid")
        if not isinstance(self.cleanup_verified, bool):
            raise LabJobError("lab execution cleanup proof is invalid")
        if self.state is LabState.SUCCEEDED:
            if not self.cleanup_verified or not isinstance(self.artifact, dict):
                raise LabJobError("successful lab outcome requires artifact and cleanup proof")
            if self.error_kind is not None:
                raise LabJobError("successful lab outcome cannot contain an error")
        elif self.artifact is not None:
            raise LabJobError("non-success lab outcome cannot contain an artifact")
        if self.error_kind is not None and self.error_kind not in _SAFE_ERROR_KINDS:
            raise LabJobError("lab execution error kind is invalid")


ProgressSink = Callable[[LabState], None]
LiveSink = Callable[[LiveTelemetryUpdate], None]
LabExecutor = Callable[
    [LabStartRequest, threading.Event, ProgressSink, LiveSink], LabExecutionOutcome
]


@dataclass(slots=True)
class LabJob:
    job_id: str
    request: LabStartRequest
    state: LabState = LabState.PLANNED
    phase: LabState = LabState.PLANNED
    cancel_requested: bool = False
    cleanup_verified: bool = False
    result: dict[str, Any] | None = None
    error_kind: str | None = None
    events: list[str] = field(default_factory=lambda: [LabState.PLANNED.value])
    live: dict[str, Any] = field(
        default_factory=lambda: {"sequence": 0, "samples": [], "failures": [], "summary": []}
    )
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def public_payload(self) -> dict[str, Any]:
        with self.lock:
            payload: dict[str, Any] = {
                "job_id": self.job_id,
                "state": self.state.value,
                "phase": self.phase.value,
                "cancel_requested": self.cancel_requested,
                "cleanup_verified": self.cleanup_verified,
                "request": self.request.public_payload(),
                "events": list(self.events),
                "error_kind": self.error_kind,
            }
            if self.state is LabState.SUCCEEDED and self.result is not None:
                payload["result"] = copy.deepcopy(self.result)
            return payload

    def live_payload(self) -> dict[str, Any]:
        with self.lock:
            return {"job_id": self.job_id, **copy.deepcopy(self.live)}


class LabJobManager:
    def __init__(
        self,
        executor: LabExecutor | None,
        *,
        environment: Mapping[str, str],
        max_jobs: int = 32,
    ) -> None:
        if isinstance(max_jobs, bool) or not isinstance(max_jobs, int) or not 1 <= max_jobs <= 256:
            raise ValueError("max_jobs must be within [1, 256]")
        if not isinstance(environment, Mapping):
            raise ValueError("environment must be a mapping")
        self._executor = executor
        self._enabled = environment.get("SERVING_VERDICT_ENABLE_LAB") == "1" and callable(executor)
        self._jobs: OrderedDict[str, LabJob] = OrderedDict()
        self._max_jobs = max_jobs
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sv-lab")

    def capabilities(self) -> dict[str, Any]:
        with self._lock:
            active = sum(job.state not in _TERMINAL for job in self._jobs.values())
        return {
            "enabled": self._enabled,
            "operator_gate": "SERVING_VERDICT_ENABLE_LAB=1",
            "executor_available": callable(self._executor),
            "max_active_jobs": 1,
            "stored_jobs_limit": self._max_jobs,
            "active_jobs": active,
            "remote_docker": False,
            "arbitrary_images": False,
            "arbitrary_argv": False,
            "live_snapshot": True,
            "cancellation": "cooperative_cleanup_required",
        }

    def start(self, request: LabStartRequest) -> LabJob:
        if not self._enabled or self._executor is None:
            raise LabJobError("Inference Lab is disabled or unavailable")
        if not isinstance(request, LabStartRequest):
            raise LabJobError("validated LabStartRequest is required")
        with self._lock:
            if any(job.state not in _TERMINAL for job in self._jobs.values()):
                raise LabJobError("an Inference Lab job is already active")
            while len(self._jobs) >= self._max_jobs:
                self._jobs.popitem(last=False)
            job = LabJob(uuid.uuid4().hex, request)
            self._jobs[job.job_id] = job
            self._pool.submit(self._run, job)
            return job

    def _run(self, job: LabJob) -> None:
        executor = self._executor
        if executor is None:
            return
        with job.lock:
            if job.cancel_requested:
                job.state = LabState.CANCELLED
                job.phase = LabState.CANCELLED
                job.events.append(LabState.CANCELLED.value)
                return
            job.state = LabState.PULLING
            job.phase = LabState.PULLING
            job.events.append(LabState.PULLING.value)
        callbacks_open = True

        def progress(state: LabState) -> None:
            if not isinstance(state, LabState) or state not in _PROGRESS_ORDER:
                raise LabJobError("lab progress state is invalid")
            with job.lock:
                if not callbacks_open:
                    raise LabJobError("lab executor callback is closed")
                if job.cancel_requested:
                    return
                current = _PROGRESS_ORDER.get(job.phase)
                if current is None or _PROGRESS_ORDER[state] < current:
                    raise LabJobError("lab progress cannot move backwards")
                job.state = state
                job.phase = state
                if not job.events or job.events[-1] != state.value:
                    if len(job.events) >= 64:
                        raise LabJobError("lab progress event bound exceeded")
                    job.events.append(state.value)

        def live(update: LiveTelemetryUpdate) -> None:
            if not isinstance(update, LiveTelemetryUpdate):
                raise LabJobError("live telemetry update is invalid")
            with job.lock:
                if not callbacks_open:
                    raise LabJobError("lab executor callback is closed")
                if job.cancel_requested:
                    return
                if (
                    len(update.samples) > job.request.telemetry_max_samples
                    or len(update.failures) > job.request.telemetry_max_samples
                ):
                    raise LabJobError("live telemetry exceeds the requested bound")
                sequence = int(job.live["sequence"]) + 1
                job.live = update.public_payload(sequence)

        try:
            outcome = executor(job.request, job.cancel_event, progress, live)
            if not isinstance(outcome, LabExecutionOutcome):
                raise LabJobError("lab executor returned an invalid outcome")
        except Exception:
            with job.lock:
                callbacks_open = False
                job.state = LabState.FAILED
                job.phase = LabState.FAILED
                job.result = None
                job.error_kind = "executor_failed"
                job.cleanup_verified = False
                job.events.append(LabState.FAILED.value)
            return
        with job.lock:
            callbacks_open = False
            job.cleanup_verified = outcome.cleanup_verified
            if outcome.state is LabState.CLEANUP_FAILED or not outcome.cleanup_verified:
                job.state = LabState.CLEANUP_FAILED
                job.phase = LabState.CLEANUP_FAILED
                job.result = None
                job.error_kind = "cleanup_failed"
            elif job.cancel_requested or outcome.state is LabState.CANCELLED:
                job.state = LabState.CANCELLED
                job.phase = LabState.CANCELLED
                job.result = None
                job.error_kind = None
            else:
                job.state = outcome.state
                job.phase = outcome.state
                job.result = copy.deepcopy(outcome.artifact)
                job.error_kind = outcome.error_kind
            if len(job.events) < 64:
                job.events.append(job.state.value)

    def get(self, job_id: str) -> LabJob:
        if not isinstance(job_id, str) or _JOB_RE.fullmatch(job_id) is None:
            raise LabJobError("lab job not found")
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            raise LabJobError("lab job not found")
        return job

    def cancel(self, job_id: str) -> LabJob:
        job = self.get(job_id)
        with job.lock:
            if job.state in _TERMINAL:
                return job
            job.cancel_requested = True
            job.cancel_event.set()
            job.state = LabState.CANCEL_REQUESTED
            job.phase = LabState.CANCEL_REQUESTED
            if len(job.events) < 64:
                job.events.append(LabState.CANCEL_REQUESTED.value)
        return job
