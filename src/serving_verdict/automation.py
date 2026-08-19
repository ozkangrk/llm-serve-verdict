"""Bounded in-memory automation jobs for loopback benchmark runs.

Jobs are ephemeral and never mutate the trial/data store. API keys exist only
inside the worker call and are never fields of a job or public payload.
"""
from __future__ import annotations

import threading
import uuid
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from serving_verdict.endpoint import EndpointConfig

Runner = Callable[[EndpointConfig, str], dict[str, Any]]
TERMINAL_STATES = frozenset({"CANCELLED", "SUCCEEDED", "FAILED"})


class AutomationError(RuntimeError):
    """Safe, machine-facing automation error."""


@dataclass(slots=True)
class AutomationJob:
    job_id: str
    endpoint: dict[str, object]
    state: str = "QUEUED"
    phase: str = "QUEUED"
    cancel_requested: bool = False
    result: dict[str, Any] | None = None
    error_kind: str | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def public_payload(self) -> dict[str, Any]:
        with self._lock:
            payload: dict[str, Any] = {
                "job_id": self.job_id,
                "state": self.state,
                "phase": self.phase,
                "cancel_requested": self.cancel_requested,
                "endpoint": dict(self.endpoint),
                "error_kind": self.error_kind,
            }
            if self.state == "SUCCEEDED" and self.result is not None:
                payload["result"] = self.result
            return payload


class JobManager:
    def __init__(self, runner: Runner, *, max_jobs: int = 32) -> None:
        if max_jobs < 1 or max_jobs > 256:
            raise ValueError("max_jobs must be within [1, 256]")
        self._runner = runner
        self._max_jobs = max_jobs
        self._jobs: OrderedDict[str, AutomationJob] = OrderedDict()
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sv-automation")

    def start(self, config: EndpointConfig, api_key: str) -> AutomationJob:
        with self._lock:
            if any(job.state not in TERMINAL_STATES for job in self._jobs.values()):
                raise AutomationError("an automation job is already active")
            self._evict_terminal()
            job = AutomationJob(
                job_id=uuid.uuid4().hex,
                endpoint=config.public_payload(),
            )
            self._jobs[job.job_id] = job
            self._pool.submit(self._run, job, config, api_key)
            return job

    def _evict_terminal(self) -> None:
        while len(self._jobs) >= self._max_jobs:
            oldest_id, oldest = next(iter(self._jobs.items()))
            if oldest.state not in TERMINAL_STATES:
                raise AutomationError("automation job capacity is exhausted")
            self._jobs.pop(oldest_id)

    def _run(self, job: AutomationJob, config: EndpointConfig, api_key: str) -> None:
        with job._lock:
            if job.cancel_requested:
                job.state = "CANCELLED"
                job.phase = "CANCELLED"
                return
            job.state = "RUNNING"
            job.phase = "BENCHMARK"
        try:
            result = self._runner(config, api_key)
        except Exception:  # sanitized: remote/local exception text is never exposed
            with job._lock:
                job.state = "CANCELLED" if job.cancel_requested else "FAILED"
                job.phase = job.state
                job.error_kind = None if job.cancel_requested else "benchmark_failed"
            return
        with job._lock:
            if job.cancel_requested:
                job.state = "CANCELLED"
                job.phase = "CANCELLED"
                job.result = None
            else:
                job.state = "SUCCEEDED"
                job.phase = "SEALED"
                job.result = result

    def get(self, job_id: str) -> AutomationJob:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            raise AutomationError("automation job not found")
        return job

    def cancel(self, job_id: str) -> AutomationJob:
        job = self.get(job_id)
        with job._lock:
            if job.state in TERMINAL_STATES:
                return job
            job.cancel_requested = True
            job.state = "CANCEL_REQUESTED"
            job.phase = "CANCEL_REQUESTED"
        return job

    def capabilities(self) -> dict[str, Any]:
        with self._lock:
            active = sum(job.state not in TERMINAL_STATES for job in self._jobs.values())
        return {
            "quick_benchmark": True,
            "ephemeral_jobs": True,
            "max_active_jobs": 1,
            "stored_jobs_limit": self._max_jobs,
            "active_jobs": active,
            "remote_endpoints": False,
            "secret_source": "environment_only",
            "cancellation": "cooperative_result_discard",
        }


def default_benchmark_runner(config: EndpointConfig, api_key: str) -> dict[str, Any]:
    from serving_verdict.benchmark_runner import run_quick_benchmark
    from serving_verdict.profile import get_profile

    result = run_quick_benchmark(
        config,
        api_key=api_key,
        profile=get_profile("quick"),
    )
    return result.artifact
