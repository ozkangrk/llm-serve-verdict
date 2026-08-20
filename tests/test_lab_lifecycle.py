"""Inference Lab lifecycle ownership, cancellation, and cleanup contracts."""
from __future__ import annotations

import threading

import pytest

from serving_verdict.lab_lifecycle import (
    LabBackend,
    LabLifecycle,
    LabState,
    LifecycleError,
    LifecyclePlan,
    Ownership,
)

RUN_ID = "lab-" + "a" * 24
DIGEST = "sha256:" + "b" * 64
IMAGE = "docker.io/vllm/vllm-openai@sha256:" + "c" * 64


class FakeBackend:
    def __init__(self, fail_on: set[str] | None = None) -> None:
        self.calls: list[tuple] = []
        self.fail_on = fail_on or set()
        self.resources: set[str] = set()

    def _call(self, name: str, *args) -> None:
        self.calls.append((name, *args))
        if name in self.fail_on:
            raise RuntimeError(f"raw-secret-sk-{name}")

    def inspect_capabilities(self, ownership: Ownership, deadline_s: float) -> None:
        self._call("inspect", ownership, deadline_s)

    def pull_image(self, image: str, ownership: Ownership, deadline_s: float) -> None:
        self._call("pull", image, ownership, deadline_s)

    def create_network(self, resource_id: str, ownership: Ownership, deadline_s: float) -> None:
        self._call("create_network", resource_id, ownership, deadline_s)
        self.resources.add(resource_id)

    def create_container(self, resource_id: str, network_id: str, image: str, ownership: Ownership, deadline_s: float) -> None:
        self._call("create_container", resource_id, network_id, image, ownership, deadline_s)
        self.resources.add(resource_id)

    def start_container(self, resource_id: str, ownership: Ownership, deadline_s: float) -> None:
        self._call("start", resource_id, ownership, deadline_s)

    def wait_ready(self, resource_id: str, ownership: Ownership, deadline_s: float) -> None:
        self._call("ready", resource_id, ownership, deadline_s)

    def stop_container(self, resource_id: str, ownership: Ownership, deadline_s: float) -> None:
        self._call("stop", resource_id, ownership, deadline_s)

    def remove_container(self, resource_id: str, ownership: Ownership, deadline_s: float) -> None:
        self._call("remove_container", resource_id, ownership, deadline_s)
        self.resources.discard(resource_id)

    def remove_network(self, resource_id: str, ownership: Ownership, deadline_s: float) -> None:
        self._call("remove_network", resource_id, ownership, deadline_s)
        self.resources.discard(resource_id)

    def verify_absent(self, resource_ids: tuple[str, ...], ownership: Ownership, deadline_s: float) -> bool:
        self._call("verify_absent", resource_ids, ownership, deadline_s)
        return not any(resource in self.resources for resource in resource_ids)


def plan(**changes) -> LifecyclePlan:
    values = {
        "run_id": RUN_ID,
        "template_digest": DIGEST,
        "image": IMAGE,
        "startup_timeout_s": 60.0,
        "run_timeout_s": 300.0,
        "cleanup_timeout_s": 30.0,
    }
    values.update(changes)
    return LifecyclePlan(**values)


def test_backend_capability_surface_has_no_generic_or_dangerous_methods() -> None:
    public = {name for name in LabBackend.__dict__ if not name.startswith("_")}
    assert public == {
        "inspect_capabilities", "pull_image", "create_network", "create_container",
        "start_container", "wait_ready", "stop_container", "remove_container",
        "remove_network", "verify_absent",
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"run_id": "bad"}, {"template_digest": "sha256:bad"},
        {"image": "docker.io/vllm/vllm-openai:latest"},
        {"startup_timeout_s": 0.0}, {"run_timeout_s": True},
        {"cleanup_timeout_s": 601.0},
    ],
)
def test_invalid_plan_fails_before_backend_calls(changes: dict) -> None:
    backend = FakeBackend()
    with pytest.raises(LifecycleError):
        plan(**changes)
    assert backend.calls == []


def test_success_state_order_result_and_cleanup() -> None:
    backend = FakeBackend()
    lifecycle = LabLifecycle(backend)
    result = lifecycle.execute(plan(), work=lambda _deadline: {"artifact": "safe"})
    assert result.state is LabState.SUCCEEDED
    assert result.result == {"artifact": "safe"}
    assert result.cleanup_verified is True
    assert result.error_kind is None
    assert result.events == (
        LabState.PLANNED, LabState.PULLING, LabState.NETWORK_CREATING,
        LabState.STARTING, LabState.READY, LabState.BENCHMARKING,
        LabState.FINALIZING, LabState.STOPPING, LabState.SUCCEEDED,
    )
    names = [call[0] for call in backend.calls]
    assert names == [
        "inspect", "pull", "create_network", "create_container", "start", "ready",
        "stop", "remove_container", "remove_network", "verify_absent",
    ]


def test_work_receives_effective_run_deadline() -> None:
    backend = FakeBackend()
    observed: list[float] = []
    result = LabLifecycle(backend).execute(
        plan(run_timeout_s=123.0),
        work=lambda deadline: observed.append(deadline),
    )
    assert result.state is LabState.SUCCEEDED
    assert observed == [123.0]


def test_resource_ids_and_ownership_are_fenced() -> None:
    backend = FakeBackend()
    LabLifecycle(backend, _resource_suffix="deadbeef").execute(
        plan(), work=lambda _deadline: None
    )
    network_call = next(call for call in backend.calls if call[0] == "create_network")
    container_call = next(call for call in backend.calls if call[0] == "create_container")
    assert network_call[1] == f"sv-lab-net-{RUN_ID}-deadbeef"
    assert container_call[1] == f"sv-lab-ctr-{RUN_ID}-deadbeef"
    ownership = network_call[2]
    assert ownership.owner == "serving-verdict-lab"
    assert ownership.run_id == RUN_ID
    assert ownership.template_digest == DIGEST


def test_resource_suffix_is_strict_and_two_instances_do_not_collide() -> None:
    with pytest.raises(LifecycleError, match="resource suffix"):
        LabLifecycle(FakeBackend(), _resource_suffix="../bad")
    first = FakeBackend()
    second = FakeBackend()
    LabLifecycle(first, _resource_suffix="11111111").execute(
        plan(), work=lambda _deadline: None
    )
    LabLifecycle(second, _resource_suffix="22222222").execute(
        plan(), work=lambda _deadline: None
    )
    first_id = next(call[1] for call in first.calls if call[0] == "create_container")
    second_id = next(call[1] for call in second.calls if call[0] == "create_container")
    assert first_id != second_id


@pytest.mark.parametrize("failure", ["inspect", "pull", "create_network", "create_container", "start", "ready"])
def test_stage_failure_is_sanitized_and_cleanup_attempted(failure: str) -> None:
    backend = FakeBackend({failure})
    result = LabLifecycle(backend).execute(plan(), work=lambda _deadline: None)
    assert result.state in (LabState.FAILED, LabState.CLEANUP_FAILED)
    assert result.result is None
    assert result.error_kind == "lifecycle_failed"
    assert "sk-" not in repr(result)
    names = [call[0] for call in backend.calls]
    assert "verify_absent" in names
    if failure in {"create_network", "create_container", "start", "ready"}:
        assert "remove_network" in names


def test_work_failure_suppresses_result_and_cleans() -> None:
    backend = FakeBackend()
    def broken(_deadline: float):
        raise RuntimeError("Bearer raw-secret")
    result = LabLifecycle(backend).execute(plan(), work=broken)
    assert result.state is LabState.FAILED
    assert result.result is None
    assert result.error_kind == "work_failed"
    assert "raw-secret" not in repr(result)
    assert result.cleanup_verified is True


def test_cancel_before_start_has_no_backend_calls() -> None:
    backend = FakeBackend()
    cancel = threading.Event()
    cancel.set()
    result = LabLifecycle(backend).execute(plan(), work=lambda _deadline: object(), cancel_event=cancel)
    assert result.state is LabState.CANCELLED
    assert result.result is None
    assert backend.calls == []


def test_cancel_during_noninterruptible_work_discards_late_result() -> None:
    backend = FakeBackend()
    cancel = threading.Event()
    def work(_deadline: float):
        cancel.set()
        return {"must": "be discarded"}
    result = LabLifecycle(backend).execute(plan(), work=work, cancel_event=cancel)
    assert result.state is LabState.CANCELLED
    assert result.result is None
    assert LabState.CANCEL_REQUESTED in result.events
    assert result.cleanup_verified is True


@pytest.mark.parametrize("cleanup_failure", ["stop", "remove_container", "remove_network", "verify_absent"])
def test_cleanup_failure_is_terminal_not_success(cleanup_failure: str) -> None:
    backend = FakeBackend({cleanup_failure})
    result = LabLifecycle(backend).execute(plan(), work=lambda _deadline: {"x": 1})
    assert result.state is LabState.CLEANUP_FAILED
    assert result.result is None
    assert result.cleanup_verified is False
    assert result.error_kind == "cleanup_failed"
    names = [call[0] for call in backend.calls]
    assert "remove_container" in names
    assert "remove_network" in names


def test_base_exception_still_attempts_cleanup_then_reraises() -> None:
    backend = FakeBackend()
    def interrupt(_deadline: float):
        raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        LabLifecycle(backend).execute(plan(), work=interrupt)
    names = [call[0] for call in backend.calls]
    assert "remove_container" in names and "remove_network" in names


def test_one_lifecycle_instance_rejects_concurrent_execution() -> None:
    backend = FakeBackend()
    lifecycle = LabLifecycle(backend)
    entered = threading.Event()
    release = threading.Event()
    def blocking(_deadline: float):
        entered.set()
        release.wait(2)
        return None
    thread = threading.Thread(target=lambda: lifecycle.execute(plan(), work=blocking))
    thread.start()
    assert entered.wait(2)
    try:
        with pytest.raises(LifecycleError, match="active"):
            lifecycle.execute(plan(), work=lambda _deadline: None)
    finally:
        release.set()
        thread.join(3)
    assert not thread.is_alive()
