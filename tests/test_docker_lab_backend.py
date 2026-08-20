from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from serving_verdict.docker_lab_backend import (
    DockerBackendError,
    DockerCommandResult,
    DockerLabBackend,
    DockerRunConfig,
)
from serving_verdict.lab_lifecycle import Ownership
from serving_verdict.lab_planner import LabPlanError, LabRunSpec, plan_lab_run
from serving_verdict.lab_templates import bind_builtin_template

IMAGE = "docker.io/vllm/vllm-openai@sha256:" + "a" * 64
PROFILE = "sha256:" + "b" * 64


class FakeRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], float]] = []
        self.handle_labels: dict[str, dict[str, str]] = {}

    @staticmethod
    def _labels(argv: tuple[str, ...]) -> dict[str, str]:
        labels: dict[str, str] = {}
        for item in argv:
            if item.startswith("--label="):
                key, value = item.removeprefix("--label=").split("=", 1)
                labels[key] = value
        return labels

    def __call__(self, argv: tuple[str, ...], deadline_s: float) -> DockerCommandResult:
        self.calls.append((argv, deadline_s))
        if argv[1:3] == ("version", "--format"):
            return DockerCommandResult(0, "29.2.1\n", "")
        if argv[1:4] == ("info", "--format", "{{json .Runtimes}}"):
            return DockerCommandResult(0, '{"nvidia":{}}\n', "")
        if argv[1:3] == ("pull", "--quiet"):
            return DockerCommandResult(0, IMAGE + "\n", "")
        if argv[1:3] == ("image", "inspect"):
            return DockerCommandResult(
                0, json.dumps([IMAGE.removeprefix("docker.io/")]) + "\n", ""
            )
        if argv[1:3] == ("network", "create"):
            self.handle_labels["network-handle"] = self._labels(argv)
            return DockerCommandResult(0, "network-handle\n", "")
        if argv[1] == "create":
            self.handle_labels["container-handle"] = self._labels(argv)
            return DockerCommandResult(0, "container-handle\n", "")
        if argv[1:3] == ("start", "container-handle"):
            return DockerCommandResult(0, "container-handle\n", "")
        if argv[1:3] == ("port", "container-handle"):
            return DockerCommandResult(0, "127.0.0.1:49152\n", "")
        if argv[1:3] == ("inspect", "--format"):
            return DockerCommandResult(
                0, json.dumps(self.handle_labels[argv[-1]]) + "\n", ""
            )
        if argv[1] in {"stop", "rm"}:
            return DockerCommandResult(0, "", "")
        if argv[1:3] == ("network", "inspect"):
            return DockerCommandResult(
                0, json.dumps(self.handle_labels[argv[-1]]) + "\n", ""
            )
        if argv[1:3] == ("network", "rm"):
            return DockerCommandResult(0, "", "")
        if argv[1] == "ps" or argv[1:3] == ("network", "ls"):
            return DockerCommandResult(0, "", "")
        raise AssertionError(argv)


def model_tree(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "models"
    model = root / "qwen"
    model.mkdir(parents=True, exist_ok=True)
    (model / "config.json").write_text("{}", encoding="utf-8")
    return root, model


def planned(tmp_path: Path) -> tuple[LabRunSpec, Any, Path]:
    root, model = model_tree(tmp_path)
    template = bind_builtin_template("vllm.openai", IMAGE)
    plan = plan_lab_run(
        template=template,
        overrides={"gpu_memory_utilization": 0.8, "max_model_len": 4096},
        model_root=root,
        model_ref="qwen",
        benchmark_profile_digest=PROFILE,
        trial_count=3,
        statistical_seed=17,
        telemetry_interval_s=2,
        telemetry_max_samples=120,
    )
    return plan, template, model


def config(tmp_path: Path, *, enabled: bool = True) -> DockerRunConfig:
    plan, template, model = planned(tmp_path)
    return DockerRunConfig.from_plan(
        plan,
        template=template,
        model_host_path=model,
        environment=(
            {"SERVING_VERDICT_ENABLE_LAB": "1"} if enabled else {}
        ),
        cpu_limit=8.0,
        memory_limit_bytes=64 * 1024**3,
        tmpfs_bytes=2 * 1024**3,
    )


def ownership(cfg: DockerRunConfig) -> Ownership:
    return Ownership("serving-verdict-lab", cfg.plan.run_id, cfg.plan.template_digest)


def test_lab_run_spec_rejects_direct_or_replace_forgery(tmp_path: Path) -> None:
    plan, _template, _model = planned(tmp_path)
    with pytest.raises(LabPlanError):
        replace(plan, effective_argv=("--privileged",))
    values = {name: getattr(plan, name) for name in plan.__dataclass_fields__ if name != "_authority"}
    with pytest.raises(LabPlanError):
        LabRunSpec(**values)


def test_config_requires_explicit_operator_enablement_and_exact_plan_binding(
    tmp_path: Path,
) -> None:
    with pytest.raises(DockerBackendError, match="SERVING_VERDICT_ENABLE_LAB"):
        config(tmp_path, enabled=False)
    cfg = config(tmp_path)
    assert cfg.plan.image == IMAGE
    assert cfg.container_port == 8000
    assert cfg.readiness_path == "/v1/models"
    assert cfg.metrics_path == "/metrics"
    assert cfg.model_host_path.is_absolute()
    values = {
        name: getattr(cfg, name)
        for name in cfg.__dataclass_fields__
        if name != "_authority"
    }
    with pytest.raises(DockerBackendError, match="trusted factory"):
        DockerRunConfig(**values)
    with pytest.raises(DockerBackendError):
        replace(cfg, cpu_limit=9999.0)


def test_backend_emits_only_hardened_allowlisted_docker_commands(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    runner = FakeRunner()
    probes: list[tuple[str, float]] = []
    backend = DockerLabBackend(
        cfg,
        command_runner=runner,
        readiness_probe=lambda url, timeout: probes.append((url, timeout)) or True,
    )
    own = ownership(cfg)

    backend.inspect_capabilities(own, 5.0)
    backend.pull_image(cfg.plan.image, own, 10.0)
    assert not any(call[0][1:3] == ("pull", "--quiet") for call in runner.calls)
    backend.create_network("sv-lab-net-x-deadbeef", own, 5.0)
    backend.create_container(
        "sv-lab-ctr-x-deadbeef",
        "sv-lab-net-x-deadbeef",
        cfg.plan.image,
        own,
        10.0,
    )
    backend.start_container("sv-lab-ctr-x-deadbeef", own, 5.0)
    backend.wait_ready("sv-lab-ctr-x-deadbeef", own, 5.0)

    network = next(call[0] for call in runner.calls if call[0][1:3] == ("network", "create"))
    assert "--internal" in network
    create = next(call[0] for call in runner.calls if call[0][1] == "create")
    joined = " ".join(create)
    for required in (
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--gpus=1",
        "--publish=127.0.0.1::8000",
        "readonly",
        str(cfg.model_host_path),
        cfg.plan.image,
        *cfg.plan.effective_argv,
    ):
        assert required in joined
    for forbidden in (
        "--privileged",
        "--network=host",
        "--pid=host",
        "--ipc=host",
        "/var/run/docker.sock",
        "--cap-add",
    ):
        assert forbidden not in joined
    assert backend.endpoint_url == "http://127.0.0.1:49152/v1"
    assert backend.metrics_url == "http://127.0.0.1:49152/metrics"
    assert probes and probes[-1][0].endswith("/v1/models")


def test_cleanup_revalidates_exact_labels_and_never_removes_unowned_resource(
    tmp_path: Path,
) -> None:
    cfg = config(tmp_path)
    runner = FakeRunner()
    backend = DockerLabBackend(
        cfg,
        command_runner=runner,
        readiness_probe=lambda _url, _timeout: True,
    )
    own = ownership(cfg)
    backend.create_network("sv-lab-net-x-deadbeef", own, 5.0)
    backend.create_container(
        "sv-lab-ctr-x-deadbeef",
        "sv-lab-net-x-deadbeef",
        cfg.plan.image,
        own,
        5.0,
    )
    runner.handle_labels["container-handle"]["serving-verdict.run-id"] = (
        "lab-" + "f" * 24
    )
    with pytest.raises(DockerBackendError, match="ownership"):
        backend.stop_container("sv-lab-ctr-x-deadbeef", own, 5.0)
    assert not any(call[0][1] in {"stop", "rm"} for call in runner.calls)


def test_owned_cleanup_removes_handles_and_verifies_absence(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    runner = FakeRunner()
    backend = DockerLabBackend(
        cfg,
        command_runner=runner,
        readiness_probe=lambda _url, _timeout: True,
    )
    own = ownership(cfg)
    network_id = "sv-lab-net-x-deadbeef"
    container_id = "sv-lab-ctr-x-deadbeef"
    backend.create_network(network_id, own, 5.0)
    backend.create_container(container_id, network_id, cfg.plan.image, own, 5.0)
    backend.stop_container(container_id, own, 5.0)
    backend.remove_container(container_id, own, 5.0)
    backend.remove_network(network_id, own, 5.0)
    assert backend.verify_absent((container_id, network_id), own, 5.0) is True


def test_ownership_fence_allows_vendor_labels_but_rejects_extra_owner_namespace(
    tmp_path: Path,
) -> None:
    cfg = config(tmp_path)
    runner = FakeRunner()
    backend = DockerLabBackend(
        cfg,
        command_runner=runner,
        readiness_probe=lambda _url, _timeout: True,
    )
    own = ownership(cfg)
    network_id = "sv-lab-net-x-deadbeef"
    container_id = "sv-lab-ctr-x-deadbeef"
    backend.create_network(network_id, own, 5.0)
    backend.create_container(container_id, network_id, cfg.plan.image, own, 5.0)
    runner.handle_labels["container-handle"]["org.opencontainers.image.version"] = "1"
    backend.stop_container(container_id, own, 5.0)
    runner.handle_labels["container-handle"]["serving-verdict.unexpected"] = "x"
    with pytest.raises(DockerBackendError, match="ownership"):
        backend.remove_container(container_id, own, 5.0)


def test_resource_ids_are_validated_before_docker_calls(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    runner = FakeRunner()
    backend = DockerLabBackend(
        cfg,
        command_runner=runner,
        readiness_probe=lambda _url, _timeout: True,
    )
    own = ownership(cfg)
    with pytest.raises(DockerBackendError, match="resource ID"):
        backend.create_network("--help", own, 5.0)
    assert runner.calls == []
