"""Pure Inference Lab planning and model-manifest provenance."""
from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from serving_verdict.canonical import canonicalize, digest_payload
from serving_verdict.lab_templates import RuntimeTemplate, TemplateError


class LabPlanError(ValueError):
    """A lab plan cannot be constructed safely; no backend may be called."""


@dataclass(frozen=True, slots=True)
class ModelFile:
    relative_path: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class ModelManifest:
    schema_version: str
    model_ref: str
    files: tuple[ModelFile, ...]
    total_bytes: int
    manifest_digest: str


@dataclass(frozen=True, slots=True)
class LabRunSpec:
    schema_version: str
    run_id: str
    plan_digest: str
    template_id: str
    template_version: str
    template_digest: str
    engine: str
    image: str
    effective_argv: tuple[str, ...]
    model_ref: str
    model_manifest_digest: str
    benchmark_profile_digest: str
    trial_count: int
    statistical_seed: int
    telemetry_interval_s: int
    telemetry_max_samples: int


def _valid_positive_int(value: object, name: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise LabPlanError(f"{name} must be an integer in [1, {maximum}]")
    return value


def build_model_manifest(
    model_root: str | Path,
    model_ref: str,
    *,
    max_files: int,
    max_bytes: int,
) -> ModelManifest:
    """Hash regular model files below an operator root without persisting host paths."""
    _valid_positive_int(max_files, "max_files", 1_000_000)
    _valid_positive_int(max_bytes, "max_bytes", 1 << 50)
    root = Path(model_root)
    if root.is_symlink() or not root.is_dir():
        raise LabPlanError("model_root must be a real directory")
    if not isinstance(model_ref, str) or not model_ref or len(model_ref) > 256:
        raise LabPlanError("model_ref must be bounded relative text")
    ref = Path(model_ref)
    if ref.is_absolute() or any(part in ("", ".", "..") for part in ref.parts):
        raise LabPlanError("model_ref must stay under model_root")
    canonical_root = root.resolve(strict=True)
    target = root / ref
    if target.is_symlink() or not target.is_dir():
        raise LabPlanError("model_ref must identify a real model directory")
    canonical_target = target.resolve(strict=True)
    try:
        canonical_target.relative_to(canonical_root)
    except ValueError as exc:
        raise LabPlanError("model_ref escapes model_root") from exc

    paths = sorted(target.rglob("*"), key=lambda item: item.as_posix())
    files: list[ModelFile] = []
    total = 0
    for path in paths:
        if path.is_symlink():
            raise LabPlanError("model tree contains a symlink")
        try:
            mode = path.lstat().st_mode
        except OSError as exc:
            raise LabPlanError("model tree cannot be inspected") from exc
        if stat.S_ISDIR(mode):
            continue
        if not stat.S_ISREG(mode):
            raise LabPlanError("model tree contains a special file")
        if len(files) + 1 > max_files:
            raise LabPlanError("model file count exceeds safety bound")
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except OSError as exc:
            raise LabPlanError("model file cannot be opened safely") from exc
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode):
                raise LabPlanError("model file changed type during inspection")
            size = opened.st_size
            digest = hashlib.sha256()
            while chunk := os.read(descriptor, 1024 * 1024):
                digest.update(chunk)
        except OSError as exc:
            raise LabPlanError("model file cannot be read") from exc
        finally:
            os.close(descriptor)
        total += size
        if total > max_bytes:
            raise LabPlanError("model byte size exceeds safety bound")
        relative = path.relative_to(target).as_posix()
        files.append(ModelFile(relative, size, digest.hexdigest()))
    if not files:
        raise LabPlanError("model directory contains no regular files")
    body = {
        "schema_version": "serving-verdict.model-manifest.v0.5",
        "model_ref": ref.as_posix(),
        "files": [
            {
                "relative_path": item.relative_path,
                "size_bytes": item.size_bytes,
                "sha256": item.sha256,
            }
            for item in files
        ],
        "total_bytes": total,
    }
    return ModelManifest(
        schema_version="serving-verdict.model-manifest.v0.5",
        model_ref=ref.as_posix(),
        files=tuple(files),
        total_bytes=total,
        manifest_digest=digest_payload(canonicalize(body)),
    )


def plan_lab_run(
    *,
    template: RuntimeTemplate,
    overrides: dict[str, object],
    model_root: str | Path,
    model_ref: str,
    benchmark_profile_digest: str,
    trial_count: int,
    statistical_seed: int,
    telemetry_interval_s: int,
    telemetry_max_samples: int,
    model_max_files: int = 100_000,
    model_max_bytes: int = 1 << 40,
) -> LabRunSpec:
    """Construct a deterministic, side-effect-free lab run spec."""
    if not isinstance(template, RuntimeTemplate):
        raise LabPlanError("template must be a validated RuntimeTemplate")
    if (
        not isinstance(benchmark_profile_digest, str)
        or not benchmark_profile_digest.startswith("sha256:")
        or len(benchmark_profile_digest) != 71
    ):
        raise LabPlanError("benchmark_profile_digest is malformed")
    try:
        int(benchmark_profile_digest[7:], 16)
    except ValueError as exc:
        raise LabPlanError("benchmark_profile_digest is malformed") from exc
    trials = _valid_positive_int(trial_count, "trial_count", 100)
    if trials < 2:
        raise LabPlanError("trial_count must be >= 2")
    if (
        isinstance(statistical_seed, bool)
        or not isinstance(statistical_seed, int)
        or not 0 <= statistical_seed <= 2**63 - 1
    ):
        raise LabPlanError("statistical_seed is out of bounds")
    interval = _valid_positive_int(telemetry_interval_s, "telemetry_interval_s", 60)
    sample_limit = _valid_positive_int(
        telemetry_max_samples, "telemetry_max_samples", 3600
    )
    try:
        base_argv = template.effective_argv(overrides)
    except TemplateError as exc:
        raise LabPlanError(str(exc)) from exc
    manifest = build_model_manifest(
        model_root,
        model_ref,
        max_files=model_max_files,
        max_bytes=model_max_bytes,
    )
    effective_argv = (*base_argv, "--model", "/models/current")
    body: dict[str, Any] = {
        "schema_version": "serving-verdict.lab-run-spec.v0.5",
        "template_id": template.template_id,
        "template_version": template.template_version,
        "template_digest": template.template_digest,
        "engine": template.engine,
        "image": template.image,
        "effective_argv": list(effective_argv),
        "model_ref": manifest.model_ref,
        "model_manifest_digest": manifest.manifest_digest,
        "benchmark_profile_digest": benchmark_profile_digest,
        "trial_count": trials,
        "statistical_seed": statistical_seed,
        "telemetry_interval_s": interval,
        "telemetry_max_samples": sample_limit,
    }
    plan_digest = digest_payload(canonicalize(body))
    run_id = "lab-" + hashlib.sha256(canonicalize(body)).hexdigest()[:24]
    return LabRunSpec(
        run_id=run_id,
        plan_digest=plan_digest,
        **{key: value for key, value in body.items() if key != "effective_argv"},
        effective_argv=effective_argv,
    )
