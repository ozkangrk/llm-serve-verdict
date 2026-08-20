"""Inference Lab runtime-template and pure-planner contracts."""
from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

import pytest

from serving_verdict.lab_planner import LabPlanError, build_model_manifest, plan_lab_run
from serving_verdict.lab_templates import (
    TemplateError,
    bind_builtin_template,
    builtin_template_ids,
)

IMAGE_DIGEST = "a" * 64


def model_tree(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "models"
    model = root / "tiny"
    model.mkdir(parents=True)
    (model / "config.json").write_text('{"model_type":"tiny"}', encoding="utf-8")
    (model / "weights.bin").write_bytes(b"weights")
    return root, "tiny"


def bound_vllm():
    return bind_builtin_template(
        "vllm.openai",
        f"docker.io/vllm/vllm-openai@sha256:{IMAGE_DIGEST}",
    )


def test_builtin_templates_are_inert_and_known() -> None:
    assert builtin_template_ids() == (
        "llamacpp.server",
        "sglang.openai",
        "vllm.openai",
    )


def test_binding_requires_exact_repository_and_digest() -> None:
    template = bound_vllm()
    assert template.image.endswith(IMAGE_DIGEST)
    assert template.template_digest.startswith("sha256:")
    with pytest.raises(TemplateError):
        bind_builtin_template("vllm.openai", "docker.io/vllm/vllm-openai:latest")
    with pytest.raises(TemplateError):
        bind_builtin_template(
            "vllm.openai", f"evil.example/vllm@sha256:{IMAGE_DIGEST}"
        )
    with pytest.raises(TemplateError):
        bind_builtin_template(
            "vllm.openai", f"docker.io/other/image@sha256:{IMAGE_DIGEST}"
        )


def test_template_is_deeply_immutable_and_digest_deterministic() -> None:
    first = bound_vllm()
    second = bound_vllm()
    assert first == second
    assert first.template_digest == second.template_digest
    with pytest.raises((AttributeError, TypeError)):
        first.fixed_args += ("--evil",)  # type: ignore[misc]
    with pytest.raises(TypeError):
        first.parameters["x"] = object()  # type: ignore[index]


def test_template_cannot_be_forged_via_dataclass_replace() -> None:
    template = bound_vllm()
    with pytest.raises(TemplateError):
        replace(template, image=f"evil.example/x@sha256:{IMAGE_DIGEST}")
    with pytest.raises(TemplateError):
        replace(template, fixed_args=("--privileged",))


def test_typed_overrides_produce_stable_argv() -> None:
    template = bound_vllm()
    argv = template.effective_argv(
        {"max_model_len": 4096, "gpu_memory_utilization": 0.8}
    )
    assert argv[:4] == (
        "python",
        "-m",
        "vllm.entrypoints.openai.api_server",
        "--host",
    )
    assert argv[-6:] == (
        "--gpu-memory-utilization",
        "0.8",
        "--max-model-len",
        "4096",
        "--tensor-parallel-size",
        "1",
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"unknown": 1},
        {"max_model_len": True},
        {"max_model_len": 0},
        {"gpu_memory_utilization": 1.1},
        {"gpu_memory_utilization": float("nan")},
        {"gpu_memory_utilization": float("inf")},
        {"gpu_memory_utilization": 10**400},
        {"gpu_memory_utilization": "0.8"},
        {"tensor_parallel_size": 1.5},
    ],
)
def test_bad_overrides_fail_closed(overrides: dict) -> None:
    with pytest.raises(TemplateError):
        bound_vllm().effective_argv(overrides)


def test_alias_conflict_fails_closed() -> None:
    with pytest.raises(TemplateError, match="conflict"):
        bound_vllm().effective_argv({"max_model_len": 4096, "max-model-len": 8192})


def test_model_manifest_is_path_free_and_deterministic(tmp_path: Path) -> None:
    root, ref = model_tree(tmp_path)
    first = build_model_manifest(root, ref, max_files=10, max_bytes=1024)
    second = build_model_manifest(root, ref, max_files=10, max_bytes=1024)
    assert first == second
    assert first.manifest_digest.startswith("sha256:")
    assert first.model_ref == "tiny"
    assert all(str(root) not in item.relative_path for item in first.files)


def test_model_manifest_detects_content_change(tmp_path: Path) -> None:
    root, ref = model_tree(tmp_path)
    before = build_model_manifest(root, ref, max_files=10, max_bytes=1024)
    (root / ref / "weights.bin").write_bytes(b"changed")
    after = build_model_manifest(root, ref, max_files=10, max_bytes=1024)
    assert before.manifest_digest != after.manifest_digest


@pytest.mark.parametrize("ref", ["../outside", "/etc", "tiny/../../outside", ""])
def test_model_manifest_rejects_escape_and_invalid_refs(tmp_path: Path, ref: str) -> None:
    root, _ = model_tree(tmp_path)
    with pytest.raises(LabPlanError):
        build_model_manifest(root, ref, max_files=10, max_bytes=1024)


def test_model_manifest_rejects_symlink_and_special_file(tmp_path: Path) -> None:
    root, ref = model_tree(tmp_path)
    outside = tmp_path / "outside"
    outside.write_text("secret", encoding="utf-8")
    (root / ref / "link").symlink_to(outside)
    with pytest.raises(LabPlanError):
        build_model_manifest(root, ref, max_files=10, max_bytes=1024)
    (root / ref / "link").unlink()
    fifo = root / ref / "pipe"
    os.mkfifo(fifo)
    try:
        with pytest.raises(LabPlanError):
            build_model_manifest(root, ref, max_files=10, max_bytes=1024)
    finally:
        fifo.unlink()


def test_model_manifest_bounds(tmp_path: Path) -> None:
    root, ref = model_tree(tmp_path)
    with pytest.raises(LabPlanError, match="file count"):
        build_model_manifest(root, ref, max_files=1, max_bytes=1024)
    with pytest.raises(LabPlanError, match="byte"):
        build_model_manifest(root, ref, max_files=10, max_bytes=2)


def test_plan_is_deterministic_secret_free_and_pure(tmp_path: Path) -> None:
    root, ref = model_tree(tmp_path)
    template = bound_vllm()
    kwargs = {
        "template": template,
        "overrides": {"max_model_len": 4096},
        "model_root": root,
        "model_ref": ref,
        "benchmark_profile_digest": "sha256:" + "b" * 64,
        "trial_count": 5,
        "statistical_seed": 7,
        "telemetry_interval_s": 2,
        "telemetry_max_samples": 120,
    }
    first = plan_lab_run(**kwargs)
    second = plan_lab_run(**kwargs)
    assert first == second
    assert first.run_id.startswith("lab-")
    assert first.plan_digest.startswith("sha256:")
    serialized = repr(first)
    assert str(root) not in serialized
    assert "api_key" not in serialized.lower()
    with pytest.raises((AttributeError, TypeError)):
        first.effective_argv += ("--evil",)  # type: ignore[misc]


@pytest.mark.parametrize(
    "change",
    [
        {"trial_count": 1},
        {"trial_count": True},
        {"statistical_seed": -1},
        {"telemetry_interval_s": 0},
        {"telemetry_max_samples": 3601},
        {"benchmark_profile_digest": "sha256:bad"},
    ],
)
def test_plan_bounds_fail_before_artifact(tmp_path: Path, change: dict) -> None:
    root, ref = model_tree(tmp_path)
    kwargs = {
        "template": bound_vllm(),
        "overrides": {},
        "model_root": root,
        "model_ref": ref,
        "benchmark_profile_digest": "sha256:" + "b" * 64,
        "trial_count": 5,
        "statistical_seed": 7,
        "telemetry_interval_s": 2,
        "telemetry_max_samples": 120,
    }
    kwargs.update(change)
    with pytest.raises(LabPlanError):
        plan_lab_run(**kwargs)
