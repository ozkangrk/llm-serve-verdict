"""Owned Inference Lab lifecycle with fail-closed cleanup.

The backend is an injected narrow capability. This module does not import a
container SDK and exposes no generic daemon request or container command API.
"""
from __future__ import annotations

import re
import threading
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

_RUN_RE = re.compile(r"^lab-[0-9a-f]{24}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_IMAGE_RE = re.compile(r"^[a-z0-9.-]+(?:/[A-Za-z0-9._-]+)+@sha256:[0-9a-f]{64}$")


class LifecycleError(ValueError):
    """Lifecycle plan, ownership, or concurrency contract failure."""


class LabState(StrEnum):
    PLANNED = "PLANNED"
    PULLING = "PULLING"
    NETWORK_CREATING = "NETWORK_CREATING"
    STARTING = "STARTING"
    READY = "READY"
    BENCHMARKING = "BENCHMARKING"
    FINALIZING = "FINALIZING"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    STOPPING = "STOPPING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    CLEANUP_FAILED = "CLEANUP_FAILED"


@dataclass(frozen=True, slots=True)
class Ownership:
    owner: str
    run_id: str
    template_digest: str


@dataclass(frozen=True, slots=True)
class LifecyclePlan:
    run_id: str
    template_digest: str
    image: str
    startup_timeout_s: float
    run_timeout_s: float
    cleanup_timeout_s: float

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, str) or not _RUN_RE.fullmatch(self.run_id):
            raise LifecycleError("run_id is malformed")
        if not isinstance(self.template_digest, str) or not _DIGEST_RE.fullmatch(
            self.template_digest
        ):
            raise LifecycleError("template_digest is malformed")
        if not isinstance(self.image, str) or not _IMAGE_RE.fullmatch(self.image):
            raise LifecycleError("image must be digest-pinned")
        for name, value, maximum in (
            ("startup_timeout_s", self.startup_timeout_s, 3600.0),
            ("run_timeout_s", self.run_timeout_s, 86400.0),
            ("cleanup_timeout_s", self.cleanup_timeout_s, 600.0),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not 0.0 < float(value) <= maximum
            ):
                raise LifecycleError(f"{name} is out of bounds")
            object.__setattr__(self, name, float(value))


@runtime_checkable
class LabBackend(Protocol):
    def inspect_capabilities(self, ownership: Ownership, deadline_s: float) -> None: ...
    def pull_image(self, image: str, ownership: Ownership, deadline_s: float) -> None: ...
    def create_network(self, resource_id: str, ownership: Ownership, deadline_s: float) -> None: ...
    def create_container(
        self,
        resource_id: str,
        network_id: str,
        image: str,
        ownership: Ownership,
        deadline_s: float,
    ) -> None: ...
    def start_container(self, resource_id: str, ownership: Ownership, deadline_s: float) -> None: ...
    def wait_ready(self, resource_id: str, ownership: Ownership, deadline_s: float) -> None: ...
    def stop_container(self, resource_id: str, ownership: Ownership, deadline_s: float) -> None: ...
    def remove_container(self, resource_id: str, ownership: Ownership, deadline_s: float) -> None: ...
    def remove_network(self, resource_id: str, ownership: Ownership, deadline_s: float) -> None: ...
    def verify_absent(
        self,
        resource_ids: tuple[str, ...],
        ownership: Ownership,
        deadline_s: float,
    ) -> bool: ...


@dataclass(frozen=True, slots=True)
class LifecycleResult:
    state: LabState
    result: Any | None
    error_kind: str | None
    events: tuple[LabState, ...]
    cleanup_verified: bool


class LabLifecycle:
    """Single-flight lifecycle owner; one instance runs at most one lab at a time."""

    def __init__(self, backend: LabBackend) -> None:
        if not isinstance(backend, LabBackend):
            raise LifecycleError("backend does not implement the lab capability")
        self._backend = backend
        self._active_lock = threading.Lock()

    def execute(
        self,
        plan: LifecyclePlan,
        *,
        work: Callable[[float], Any],
        cancel_event: threading.Event | None = None,
    ) -> LifecycleResult:
        if not isinstance(plan, LifecyclePlan):
            raise LifecycleError("plan must be a validated LifecyclePlan")
        if not callable(work):
            raise LifecycleError("work must be callable")
        cancel = cancel_event or threading.Event()
        if cancel.is_set():
            return LifecycleResult(
                LabState.CANCELLED, None, None, (LabState.PLANNED, LabState.CANCELLED), True
            )
        if not self._active_lock.acquire(blocking=False):
            raise LifecycleError("a lab lifecycle is already active")
        try:
            return self._execute_owned(plan, work, cancel)
        finally:
            self._active_lock.release()

    def _execute_owned(
        self, plan: LifecyclePlan, work: Callable[[float], Any], cancel: threading.Event
    ) -> LifecycleResult:
        events: list[LabState] = [LabState.PLANNED]
        ownership = Ownership("serving-verdict-lab", plan.run_id, plan.template_digest)
        network_id = f"sv-lab-net-{plan.run_id}"
        container_id = f"sv-lab-ctr-{plan.run_id}"
        network_attempted = False
        container_attempted = False
        result: Any | None = None
        error_kind: str | None = None
        cancelled = False
        pending_base: BaseException | None = None

        try:
            events.append(LabState.PULLING)
            self._backend.inspect_capabilities(ownership, plan.startup_timeout_s)
            self._backend.pull_image(plan.image, ownership, plan.startup_timeout_s)
            if cancel.is_set():
                cancelled = True
            else:
                events.append(LabState.NETWORK_CREATING)
                network_attempted = True
                self._backend.create_network(network_id, ownership, plan.startup_timeout_s)
                events.append(LabState.STARTING)
                container_attempted = True
                self._backend.create_container(
                    container_id,
                    network_id,
                    plan.image,
                    ownership,
                    plan.startup_timeout_s,
                )
                self._backend.start_container(
                    container_id, ownership, plan.startup_timeout_s
                )
                self._backend.wait_ready(container_id, ownership, plan.startup_timeout_s)
                events.append(LabState.READY)
                if cancel.is_set():
                    cancelled = True
                else:
                    events.append(LabState.BENCHMARKING)
                    try:
                        candidate_result = work(plan.run_timeout_s)
                    except Exception:
                        error_kind = "work_failed"
                    else:
                        if cancel.is_set():
                            events.append(LabState.CANCEL_REQUESTED)
                            cancelled = True
                        else:
                            events.append(LabState.FINALIZING)
                            result = candidate_result
        except Exception:
            error_kind = "lifecycle_failed"
        except BaseException as exc:
            pending_base = exc
        finally:
            cleanup_ok = True
            events.append(LabState.STOPPING)
            if container_attempted:
                try:
                    self._backend.stop_container(
                        container_id, ownership, plan.cleanup_timeout_s
                    )
                except Exception:
                    cleanup_ok = False
                try:
                    self._backend.remove_container(
                        container_id, ownership, plan.cleanup_timeout_s
                    )
                except Exception:
                    cleanup_ok = False
            if network_attempted:
                try:
                    self._backend.remove_network(
                        network_id, ownership, plan.cleanup_timeout_s
                    )
                except Exception:
                    cleanup_ok = False
            try:
                absent = self._backend.verify_absent(
                    (container_id, network_id), ownership, plan.cleanup_timeout_s
                )
                cleanup_ok = cleanup_ok and bool(absent)
            except Exception:
                cleanup_ok = False

        if pending_base is not None:
            raise pending_base
        if not cleanup_ok:
            return LifecycleResult(
                LabState.CLEANUP_FAILED,
                None,
                "cleanup_failed",
                (*events, LabState.CLEANUP_FAILED),
                False,
            )
        if cancelled:
            return LifecycleResult(
                LabState.CANCELLED,
                None,
                None,
                (*events, LabState.CANCELLED),
                True,
            )
        if error_kind is not None:
            return LifecycleResult(
                LabState.FAILED,
                None,
                error_kind,
                (*events, LabState.FAILED),
                True,
            )
        return LifecycleResult(
            LabState.SUCCEEDED,
            result,
            None,
            (*events, LabState.SUCCEEDED),
            True,
        )
