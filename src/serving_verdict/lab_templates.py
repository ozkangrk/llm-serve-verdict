"""Inert, digest-bound runtime templates for Inference Lab.

This module is declarative and pure. It cannot start processes or containers.
"""
from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from serving_verdict.canonical import canonicalize, digest_payload

_IMAGE_RE = re.compile(
    r"^(?P<repo>[a-z0-9.-]+(?:/[A-Za-z0-9._-]+)+)@sha256:(?P<digest>[0-9a-f]{64})$"
)
_TEMPLATE_AUTHORITY = object()
_DENIED_ARG_TOKENS = frozenset(
    {"--privileged", "--network=host", "--pid=host", "--ipc=host", "--cap-add", "--mount", "--volume", "-v", "/var/run/docker.sock"}
)


class TemplateError(ValueError):
    """A runtime template or typed override is invalid."""


@dataclass(frozen=True, slots=True)
class ParameterSpec:
    name: str
    flag: str
    value_type: str
    default: int | float | str
    minimum: int | float | None = None
    maximum: int | float | None = None
    choices: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()

    def normalize(self, value: object) -> int | float | str:
        if self.value_type == "int":
            if isinstance(value, bool) or not isinstance(value, int):
                raise TemplateError(f"parameter {self.name} must be an int")
            normalized: int | float | str = value
        elif self.value_type == "float":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TemplateError(f"parameter {self.name} must be a number")
            try:
                normalized = float(value)
            except OverflowError as exc:
                raise TemplateError(f"parameter {self.name} is outside float range") from exc
            if not math.isfinite(normalized):
                raise TemplateError(f"parameter {self.name} must be finite")
        elif self.value_type == "enum":
            if not isinstance(value, str) or value not in self.choices:
                raise TemplateError(f"parameter {self.name} is outside its allowlist")
            normalized = value
        elif self.value_type == "str":
            if not isinstance(value, str) or not value or len(value) > 128:
                raise TemplateError(f"parameter {self.name} must be bounded text")
            normalized = value
        else:
            raise TemplateError(f"parameter {self.name} has unsupported type")
        if isinstance(normalized, (int, float)):
            if self.minimum is not None and normalized < self.minimum:
                raise TemplateError(f"parameter {self.name} is below its minimum")
            if self.maximum is not None and normalized > self.maximum:
                raise TemplateError(f"parameter {self.name} exceeds its maximum")
        return normalized


@dataclass(frozen=True, slots=True)
class RuntimeTemplate:
    schema_version: str
    template_id: str
    template_version: str
    engine: str
    image: str
    entrypoint: tuple[str, ...]
    fixed_args: tuple[str, ...]
    parameters: Mapping[str, ParameterSpec]
    container_port: int
    readiness_path: str
    metrics_path: str
    gpu_count: int
    template_digest: str
    _authority: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._authority is not _TEMPLATE_AUTHORITY:
            raise TemplateError("RuntimeTemplate must be created by the trusted factory")
        if self.schema_version != "serving-verdict.runtime-template.v0.5":
            raise TemplateError("runtime template schema is invalid")
        image_match = _IMAGE_RE.fullmatch(self.image)
        if image_match is None:
            raise TemplateError("runtime image is not digest-pinned")
        blueprint = _BLUEPRINTS.get(self.template_id)
        if blueprint is None:
            raise TemplateError("runtime template is not in the built-in registry")
        if (
            self.template_version != "1"
            or
            self.engine != blueprint.engine
            or image_match.group("repo") != blueprint.repository
            or tuple(self.entrypoint) != blueprint.entrypoint
            or tuple(self.fixed_args) != blueprint.fixed_args
            or self.container_port != blueprint.container_port
            or self.readiness_path != blueprint.readiness_path
            or self.metrics_path != blueprint.metrics_path
            or self.gpu_count != 1
            or tuple(self.parameters.values()) != blueprint.parameters
        ):
            raise TemplateError("runtime template does not match its trusted blueprint")
        if not self.entrypoint or not all(isinstance(v, str) and v for v in self.entrypoint):
            raise TemplateError("runtime entrypoint is invalid")
        if not all(isinstance(v, str) and v for v in self.fixed_args):
            raise TemplateError("runtime fixed args are invalid")
        if any(token in _DENIED_ARG_TOKENS for token in (*self.entrypoint, *self.fixed_args)):
            raise TemplateError("runtime template requests a prohibited capability")
        object.__setattr__(self, "entrypoint", tuple(self.entrypoint))
        object.__setattr__(self, "fixed_args", tuple(self.fixed_args))
        object.__setattr__(self, "parameters", MappingProxyType(dict(self.parameters)))
        if self.template_digest != digest_payload(canonicalize(_template_body(self))):
            raise TemplateError("runtime template digest mismatch")

    def effective_argv(self, overrides: Mapping[str, object]) -> tuple[str, ...]:
        if not isinstance(overrides, Mapping):
            raise TemplateError("overrides must be a mapping")
        alias_to_name: dict[str, str] = {}
        for name, spec in self.parameters.items():
            alias_to_name[name] = name
            for alias in spec.aliases:
                if alias in alias_to_name:
                    raise TemplateError("template contains conflicting aliases")
                alias_to_name[alias] = name
        effective: dict[str, object] = {
            name: spec.default for name, spec in self.parameters.items()
        }
        supplied: set[str] = set()
        for key, value in overrides.items():
            if not isinstance(key, str) or key not in alias_to_name:
                raise TemplateError(f"unknown parameter: {key!r}")
            canonical = alias_to_name[key]
            if canonical in supplied:
                raise TemplateError(f"parameter alias conflict: {canonical}")
            supplied.add(canonical)
            effective[canonical] = value
        argv = [*self.entrypoint, *self.fixed_args]
        for name in sorted(self.parameters):
            spec = self.parameters[name]
            normalized = spec.normalize(effective[name])
            argv.extend((spec.flag, str(normalized)))
        return tuple(argv)


@dataclass(frozen=True, slots=True)
class _Blueprint:
    template_id: str
    engine: str
    repository: str
    entrypoint: tuple[str, ...]
    fixed_args: tuple[str, ...]
    parameters: tuple[ParameterSpec, ...]
    container_port: int
    readiness_path: str
    metrics_path: str


_COMMON = (
    ParameterSpec(
        "gpu_memory_utilization",
        "--gpu-memory-utilization",
        "float",
        0.9,
        0.1,
        1.0,
        aliases=("gpu-memory-utilization",),
    ),
    ParameterSpec(
        "max_model_len",
        "--max-model-len",
        "int",
        4096,
        1,
        1_048_576,
        aliases=("max-model-len",),
    ),
    ParameterSpec(
        "tensor_parallel_size",
        "--tensor-parallel-size",
        "int",
        1,
        1,
        64,
        aliases=("tensor-parallel-size",),
    ),
)

_BLUEPRINTS: dict[str, _Blueprint] = {
    "vllm.openai": _Blueprint(
        "vllm.openai",
        "vllm",
        "docker.io/vllm/vllm-openai",
        ("python", "-m", "vllm.entrypoints.openai.api_server"),
        ("--host", "0.0.0.0", "--port", "8000"),
        _COMMON,
        8000,
        "/v1/models",
        "/metrics",
    ),
    "sglang.openai": _Blueprint(
        "sglang.openai",
        "sglang",
        "docker.io/lmsysorg/sglang",
        ("python", "-m", "sglang.launch_server"),
        ("--host", "0.0.0.0", "--port", "30000"),
        _COMMON,
        30000,
        "/v1/models",
        "/metrics",
    ),
    "llamacpp.server": _Blueprint(
        "llamacpp.server",
        "llamacpp",
        "ghcr.io/ggml-org/llama.cpp",
        ("llama-server",),
        ("--host", "0.0.0.0", "--port", "8080"),
        (
            ParameterSpec(
                "context_size",
                "--ctx-size",
                "int",
                4096,
                1,
                1_048_576,
                aliases=("ctx-size",),
            ),
        ),
        8080,
        "/health",
        "/metrics",
    ),
}


def builtin_template_ids() -> tuple[str, ...]:
    return tuple(sorted(_BLUEPRINTS))


def _parameter_payload(parameter: ParameterSpec) -> dict[str, Any]:
    return {
        "name": parameter.name,
        "flag": parameter.flag,
        "value_type": parameter.value_type,
        "default": parameter.default,
        "minimum": parameter.minimum,
        "maximum": parameter.maximum,
        "choices": list(parameter.choices),
        "aliases": list(parameter.aliases),
    }


def _template_body(template: RuntimeTemplate) -> dict[str, Any]:
    return {
        "schema_version": template.schema_version,
        "template_id": template.template_id,
        "template_version": template.template_version,
        "engine": template.engine,
        "image": template.image,
        "entrypoint": list(template.entrypoint),
        "fixed_args": list(template.fixed_args),
        "parameters": [_parameter_payload(template.parameters[name]) for name in template.parameters],
        "container_port": template.container_port,
        "readiness_path": template.readiness_path,
        "metrics_path": template.metrics_path,
        "gpu_count": template.gpu_count,
    }


def bind_builtin_template(template_id: str, image: str) -> RuntimeTemplate:
    blueprint = _BLUEPRINTS.get(template_id)
    if blueprint is None:
        raise TemplateError("unknown runtime template")
    if not isinstance(image, str):
        raise TemplateError("image must be text")
    match = _IMAGE_RE.fullmatch(image)
    if match is None or match.group("repo") != blueprint.repository:
        raise TemplateError("image must match the template repository and include a sha256 digest")
    parameters = MappingProxyType({p.name: p for p in blueprint.parameters})
    body: dict[str, Any] = {
        "schema_version": "serving-verdict.runtime-template.v0.5",
        "template_id": blueprint.template_id,
        "template_version": "1",
        "engine": blueprint.engine,
        "image": image,
        "entrypoint": list(blueprint.entrypoint),
        "fixed_args": list(blueprint.fixed_args),
        "parameters": [
            {
                "name": p.name,
                "flag": p.flag,
                "value_type": p.value_type,
                "default": p.default,
                "minimum": p.minimum,
                "maximum": p.maximum,
                "choices": list(p.choices),
                "aliases": list(p.aliases),
            }
            for p in blueprint.parameters
        ],
        "container_port": blueprint.container_port,
        "readiness_path": blueprint.readiness_path,
        "metrics_path": blueprint.metrics_path,
        "gpu_count": 1,
    }
    template_digest = digest_payload(canonicalize(body))
    return RuntimeTemplate(
        schema_version=body["schema_version"],
        template_id=blueprint.template_id,
        template_version="1",
        engine=blueprint.engine,
        image=image,
        entrypoint=blueprint.entrypoint,
        fixed_args=blueprint.fixed_args,
        parameters=parameters,
        container_port=blueprint.container_port,
        readiness_path=blueprint.readiness_path,
        metrics_path=blueprint.metrics_path,
        gpu_count=1,
        template_digest=template_digest,
        _authority=_TEMPLATE_AUTHORITY,
    )
