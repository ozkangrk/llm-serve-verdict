"""Narrow, opt-in Docker backend for the local Inference Lab.

No shell, Compose, exec, attach, arbitrary Docker request, writable host mount,
or caller-supplied image/argv capability is exposed. All executable values come
from a trusted ``LabRunSpec`` bound to a built-in runtime template.
"""
from __future__ import annotations

import json
import math
import re
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from serving_verdict.lab_lifecycle import Ownership
from serving_verdict.lab_planner import LabRunSpec
from serving_verdict.lab_templates import RuntimeTemplate

_HANDLE_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_RESOURCE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,127}$")
_PORT_RE = re.compile(r"^127\.0\.0\.1:([0-9]{1,5})$")
_MAX_COMMAND_OUTPUT = 1 << 20
_LABEL_OWNER = "serving-verdict.owner"
_LABEL_RUN = "serving-verdict.run-id"
_LABEL_TEMPLATE = "serving-verdict.template-digest"
_LABEL_RESOURCE = "serving-verdict.resource-id"
_CONFIG_AUTHORITY = object()


class DockerBackendError(RuntimeError):
    """Docker capability or owned-resource operation failed safely."""


@dataclass(frozen=True, slots=True)
class DockerCommandResult:
    returncode: int
    stdout: str
    stderr: str


CommandRunner = Callable[[tuple[str, ...], float], DockerCommandResult]
ReadinessProbe = Callable[[str, float], bool]


def _default_command_runner(argv: tuple[str, ...], deadline_s: float) -> DockerCommandResult:
    try:
        result = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=deadline_s,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DockerBackendError("Docker command could not complete") from exc
    return DockerCommandResult(result.returncode, result.stdout, result.stderr)


def _default_readiness_probe(url: str, timeout_s: float) -> bool:
    from urllib.error import HTTPError, URLError
    from urllib.request import Request, urlopen

    try:
        with urlopen(Request(url, method="GET"), timeout=timeout_s) as response:
            return 200 <= int(response.status) < 300
    except (HTTPError, URLError, TimeoutError, OSError):
        return False


@dataclass(frozen=True, slots=True)
class DockerRunConfig:
    plan: LabRunSpec
    model_host_path: Path
    container_port: int
    readiness_path: str
    metrics_path: str
    gpu_count: int
    cpu_limit: float
    memory_limit_bytes: int
    tmpfs_bytes: int
    operator_enabled: bool
    _authority: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._authority is not _CONFIG_AUTHORITY:
            raise DockerBackendError("DockerRunConfig must be created by the trusted factory")
        if not isinstance(self.plan, LabRunSpec) or not self.operator_enabled:
            raise DockerBackendError("DockerRunConfig plan/enablement is invalid")
        if (
            self.model_host_path.is_symlink()
            or not self.model_host_path.is_absolute()
            or not self.model_host_path.is_dir()
        ):
            raise DockerBackendError("DockerRunConfig model path is invalid")
        if not 1 <= self.container_port <= 65535 or self.gpu_count != 1:
            raise DockerBackendError("DockerRunConfig runtime capability is invalid")
        if not self.readiness_path.startswith("/") or not self.metrics_path.startswith("/"):
            raise DockerBackendError("DockerRunConfig endpoint paths are invalid")
        if not 0.1 <= self.cpu_limit <= 1024.0:
            raise DockerBackendError("DockerRunConfig CPU limit is invalid")
        if not 1 << 20 <= self.memory_limit_bytes <= 1 << 50:
            raise DockerBackendError("DockerRunConfig memory limit is invalid")
        if not 1 << 20 <= self.tmpfs_bytes <= 1 << 40:
            raise DockerBackendError("DockerRunConfig tmpfs limit is invalid")

    @classmethod
    def from_plan(
        cls,
        plan: LabRunSpec,
        *,
        template: RuntimeTemplate,
        model_host_path: str | Path,
        environment: Mapping[str, str],
        cpu_limit: float,
        memory_limit_bytes: int,
        tmpfs_bytes: int,
    ) -> DockerRunConfig:
        if (
            not isinstance(environment, Mapping)
            or environment.get("SERVING_VERDICT_ENABLE_LAB") != "1"
        ):
            raise DockerBackendError(
                "SERVING_VERDICT_ENABLE_LAB=1 operator enablement is required"
            )
        if not isinstance(plan, LabRunSpec) or not isinstance(template, RuntimeTemplate):
            raise DockerBackendError("validated plan and template are required")
        if (
            plan.template_id != template.template_id
            or plan.template_version != template.template_version
            or plan.template_digest != template.template_digest
            or plan.engine != template.engine
            or plan.image != template.image
            or not plan.effective_argv
            or tuple(plan.effective_argv[: len(template.entrypoint)])
            != tuple(template.entrypoint)
        ):
            raise DockerBackendError("plan does not match its trusted runtime template")
        model = Path(model_host_path)
        if model.is_symlink() or not model.is_dir():
            raise DockerBackendError("model_host_path must be a real directory")
        try:
            resolved = model.resolve(strict=True)
        except OSError as exc:
            raise DockerBackendError("model_host_path cannot be resolved") from exc
        if (
            isinstance(cpu_limit, bool)
            or not isinstance(cpu_limit, (int, float))
            or not math.isfinite(float(cpu_limit))
            or not 0.1 <= float(cpu_limit) <= 1024.0
        ):
            raise DockerBackendError("cpu_limit is out of bounds")
        for name, value, maximum in (
            ("memory_limit_bytes", memory_limit_bytes, 1 << 50),
            ("tmpfs_bytes", tmpfs_bytes, 1 << 40),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 1 << 20 <= value <= maximum
            ):
                raise DockerBackendError(f"{name} is out of bounds")
        if not isinstance(template.container_port, int) or not 1 <= template.container_port <= 65535:
            raise DockerBackendError("container_port is invalid")
        return cls(
            plan=plan,
            model_host_path=resolved,
            container_port=template.container_port,
            readiness_path=template.readiness_path,
            metrics_path=template.metrics_path,
            gpu_count=template.gpu_count,
            cpu_limit=float(cpu_limit),
            memory_limit_bytes=memory_limit_bytes,
            tmpfs_bytes=tmpfs_bytes,
            operator_enabled=True,
            _authority=_CONFIG_AUTHORITY,
        )


class DockerLabBackend:
    """Stateful capability that owns only handles created by this instance."""

    def __init__(
        self,
        config: DockerRunConfig,
        *,
        command_runner: CommandRunner = _default_command_runner,
        readiness_probe: ReadinessProbe = _default_readiness_probe,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not isinstance(config, DockerRunConfig) or not config.operator_enabled:
            raise DockerBackendError("validated enabled DockerRunConfig is required")
        if not callable(command_runner) or not callable(readiness_probe) or not callable(sleep):
            raise DockerBackendError("backend dependencies must be callable")
        self._config = config
        self._runner = command_runner
        self._probe = readiness_probe
        self._sleep = sleep
        self._networks: dict[str, tuple[str, Ownership]] = {}
        self._containers: dict[str, tuple[str, Ownership]] = {}
        self._host_port: int | None = None

    @property
    def endpoint_url(self) -> str:
        if self._host_port is None:
            raise DockerBackendError("container endpoint is not ready")
        return f"http://127.0.0.1:{self._host_port}/v1"

    @property
    def metrics_url(self) -> str:
        if self._host_port is None:
            raise DockerBackendError("container endpoint is not ready")
        return f"http://127.0.0.1:{self._host_port}{self._config.metrics_path}"

    def _run(
        self,
        argv: tuple[str, ...],
        deadline_s: float,
        operation: str,
        *,
        allow_failure: bool = False,
    ) -> DockerCommandResult:
        if (
            not argv
            or argv[0] != "docker"
            or isinstance(deadline_s, bool)
            or not isinstance(deadline_s, (int, float))
            or not 0.0 < float(deadline_s) <= 86400.0
        ):
            raise DockerBackendError("Docker command contract is invalid")
        result = self._runner(argv, float(deadline_s))
        if not isinstance(result, DockerCommandResult):
            raise DockerBackendError("Docker runner returned an invalid result")
        if len(result.stdout) + len(result.stderr) > _MAX_COMMAND_OUTPUT:
            raise DockerBackendError("Docker command output exceeded its bound")
        if result.returncode != 0 and not allow_failure:
            raise DockerBackendError(f"Docker {operation} failed")
        return result

    @staticmethod
    def _labels(ownership: Ownership, resource_id: str) -> dict[str, str]:
        return {
            _LABEL_OWNER: "lab",
            _LABEL_RUN: ownership.run_id,
            _LABEL_TEMPLATE: ownership.template_digest,
            _LABEL_RESOURCE: resource_id,
        }

    @staticmethod
    def _validate_resource_id(resource_id: str) -> None:
        if (
            not isinstance(resource_id, str)
            or _RESOURCE_ID_RE.fullmatch(resource_id) is None
        ):
            raise DockerBackendError("Docker resource ID is invalid")

    @staticmethod
    def _label_args(labels: dict[str, str]) -> tuple[str, ...]:
        return tuple(f"--label={key}={value}" for key, value in sorted(labels.items()))

    def inspect_capabilities(self, ownership: Ownership, deadline_s: float) -> None:
        del ownership
        self._run(("docker", "version", "--format", "{{.Server.Version}}"), deadline_s, "version")
        result = self._run(
            ("docker", "info", "--format", "{{json .Runtimes}}"),
            deadline_s,
            "capability inspection",
        )
        try:
            runtimes = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise DockerBackendError("Docker runtime report is invalid") from exc
        if not isinstance(runtimes, dict) or "nvidia" not in runtimes:
            raise DockerBackendError("NVIDIA container runtime is unavailable")

    def _image_has_required_digest(self, image: str, deadline_s: float) -> bool:
        result = self._run(
            ("docker", "image", "inspect", image, "--format", "{{json .RepoDigests}}"),
            deadline_s,
            "image digest inspection",
            allow_failure=True,
        )
        if result.returncode != 0:
            return False
        try:
            digests = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise DockerBackendError("Docker image digest report is invalid") from exc
        if not isinstance(digests, list) or not all(isinstance(item, str) for item in digests):
            raise DockerBackendError("Docker image digest report is invalid")
        accepted = {image}
        if image.startswith("docker.io/"):
            accepted.add(image.removeprefix("docker.io/"))
        return any(item in accepted for item in digests)

    def pull_image(self, image: str, ownership: Ownership, deadline_s: float) -> None:
        del ownership
        if image != self._config.plan.image:
            raise DockerBackendError("image does not match the validated plan")
        if self._image_has_required_digest(image, deadline_s):
            return
        self._run(("docker", "pull", "--quiet", image), deadline_s, "image pull")
        if not self._image_has_required_digest(image, deadline_s):
            raise DockerBackendError("pulled image does not expose the required digest")

    def create_network(
        self, resource_id: str, ownership: Ownership, deadline_s: float
    ) -> None:
        self._validate_resource_id(resource_id)
        if resource_id in self._networks:
            raise DockerBackendError("network resource already exists")
        labels = self._labels(ownership, resource_id)
        result = self._run(
            (
                "docker",
                "network",
                "create",
                "--internal",
                *self._label_args(labels),
                resource_id,
            ),
            deadline_s,
            "network create",
        )
        handle = result.stdout.strip()
        if _HANDLE_RE.fullmatch(handle) is None:
            raise DockerBackendError("Docker network handle is invalid")
        self._networks[resource_id] = (handle, ownership)

    def create_container(
        self,
        resource_id: str,
        network_id: str,
        image: str,
        ownership: Ownership,
        deadline_s: float,
    ) -> None:
        if resource_id in self._containers:
            raise DockerBackendError("container resource already exists")
        network = self._networks.get(network_id)
        if network is None or network[1] != ownership:
            raise DockerBackendError("owned network is unavailable")
        if image != self._config.plan.image:
            raise DockerBackendError("image does not match the validated plan")
        labels = self._labels(ownership, resource_id)
        argv = self._config.plan.effective_argv
        result = self._run(
            (
                "docker",
                "create",
                f"--name={resource_id}",
                f"--network={network[0]}",
                *self._label_args(labels),
                "--read-only",
                "--cap-drop=ALL",
                "--security-opt=no-new-privileges",
                f"--gpus={self._config.gpu_count}",
                f"--cpus={self._config.cpu_limit}",
                f"--memory={self._config.memory_limit_bytes}b",
                "--pids-limit=512",
                f"--shm-size={self._config.tmpfs_bytes}b",
                f"--tmpfs=/tmp:rw,noexec,nosuid,size={self._config.tmpfs_bytes}",
                (
                    "--mount=type=bind,"
                    f"src={self._config.model_host_path},"
                    "dst=/models/current,readonly"
                ),
                f"--publish=127.0.0.1::{self._config.container_port}",
                f"--entrypoint={argv[0]}",
                image,
                *argv[1:],
            ),
            deadline_s,
            "container create",
        )
        handle = result.stdout.strip()
        if _HANDLE_RE.fullmatch(handle) is None:
            raise DockerBackendError("Docker container handle is invalid")
        self._containers[resource_id] = (handle, ownership)

    def start_container(
        self, resource_id: str, ownership: Ownership, deadline_s: float
    ) -> None:
        handle = self._owned_handle(self._containers, resource_id, ownership, "container")
        self._assert_container_labels(handle, resource_id, ownership, deadline_s)
        self._run(("docker", "start", handle), deadline_s, "container start")
        result = self._run(
            ("docker", "port", handle, f"{self._config.container_port}/tcp"),
            deadline_s,
            "container port inspection",
        )
        match = _PORT_RE.fullmatch(result.stdout.strip())
        if match is None:
            raise DockerBackendError("Docker published port is invalid")
        port = int(match.group(1))
        if not 1 <= port <= 65535:
            raise DockerBackendError("Docker published port is out of bounds")
        self._host_port = port

    def wait_ready(
        self, resource_id: str, ownership: Ownership, deadline_s: float
    ) -> None:
        self._owned_handle(self._containers, resource_id, ownership, "container")
        if self._host_port is None:
            raise DockerBackendError("container port is unavailable")
        stop_at = time.monotonic() + float(deadline_s)
        url = f"http://127.0.0.1:{self._host_port}{self._config.readiness_path}"
        while True:
            remaining = stop_at - time.monotonic()
            if remaining <= 0:
                raise DockerBackendError("container readiness deadline exceeded")
            if self._probe(url, min(remaining, 2.0)):
                return
            self._sleep(min(0.2, remaining))

    @staticmethod
    def _owned_handle(
        resources: dict[str, tuple[str, Ownership]],
        resource_id: str,
        ownership: Ownership,
        kind: str,
    ) -> str:
        value = resources.get(resource_id)
        if value is None or value[1] != ownership:
            raise DockerBackendError(f"owned {kind} handle is unavailable")
        return value[0]

    def _assert_container_labels(
        self,
        handle: str,
        resource_id: str,
        ownership: Ownership,
        deadline_s: float,
    ) -> None:
        result = self._run(
            ("docker", "inspect", "--format", "{{json .Config.Labels}}", handle),
            deadline_s,
            "container ownership inspection",
        )
        self._assert_labels(result.stdout, resource_id, ownership)

    def _assert_network_labels(
        self,
        handle: str,
        resource_id: str,
        ownership: Ownership,
        deadline_s: float,
    ) -> None:
        result = self._run(
            ("docker", "network", "inspect", "--format", "{{json .Labels}}", handle),
            deadline_s,
            "network ownership inspection",
        )
        self._assert_labels(result.stdout, resource_id, ownership)

    def _assert_labels(self, raw: str, resource_id: str, ownership: Ownership) -> None:
        try:
            labels = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise DockerBackendError("Docker ownership labels are invalid") from exc
        if not isinstance(labels, dict):
            raise DockerBackendError("Docker ownership labels are invalid")
        expected = self._labels(ownership, resource_id)
        if any(labels.get(key) != value for key, value in expected.items()):
            raise DockerBackendError("Docker ownership fence mismatch")
        owned_namespace = {
            key for key in labels if isinstance(key, str) and key.startswith("serving-verdict.")
        }
        if owned_namespace != set(expected):
            raise DockerBackendError("Docker ownership fence mismatch")

    def stop_container(
        self, resource_id: str, ownership: Ownership, deadline_s: float
    ) -> None:
        value = self._containers.get(resource_id)
        if value is None:
            return
        handle = self._owned_handle(self._containers, resource_id, ownership, "container")
        self._assert_container_labels(handle, resource_id, ownership, deadline_s)
        self._run(("docker", "stop", "--time=5", handle), deadline_s, "container stop")

    def remove_container(
        self, resource_id: str, ownership: Ownership, deadline_s: float
    ) -> None:
        value = self._containers.get(resource_id)
        if value is None:
            return
        handle = self._owned_handle(self._containers, resource_id, ownership, "container")
        self._assert_container_labels(handle, resource_id, ownership, deadline_s)
        self._run(("docker", "rm", "--force", handle), deadline_s, "container remove")
        del self._containers[resource_id]
        self._host_port = None

    def remove_network(
        self, resource_id: str, ownership: Ownership, deadline_s: float
    ) -> None:
        value = self._networks.get(resource_id)
        if value is None:
            return
        handle = self._owned_handle(self._networks, resource_id, ownership, "network")
        self._assert_network_labels(handle, resource_id, ownership, deadline_s)
        self._run(("docker", "network", "rm", handle), deadline_s, "network remove")
        del self._networks[resource_id]

    def verify_absent(
        self,
        resource_ids: tuple[str, ...],
        ownership: Ownership,
        deadline_s: float,
    ) -> bool:
        if any(resource_id in self._containers or resource_id in self._networks for resource_id in resource_ids):
            return False
        for resource_id in resource_ids:
            labels = self._labels(ownership, resource_id)
            label_filters = tuple(
                item
                for key, value in sorted(labels.items())
                for item in ("--filter", f"label={key}={value}")
            )
            if "-ctr-" in resource_id:
                result = self._run(
                    ("docker", "ps", "--all", "--quiet", *label_filters),
                    deadline_s,
                    "container absence verification",
                )
            else:
                result = self._run(
                    ("docker", "network", "ls", "--quiet", *label_filters),
                    deadline_s,
                    "network absence verification",
                )
            if result.stdout.strip():
                return False
        return True
