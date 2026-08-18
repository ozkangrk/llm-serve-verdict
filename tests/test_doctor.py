"""TDD tests for the Serving Doctor: rule-based findings + machine artifacts.

Covers: the closed rule set (no LLM opinion), context-too-high, KV pressure,
concurrency infeasibility, missing endpoint capabilities, weights-exceed,
deterministic artifact digest (volatile generated_at excluded), secret/path
redaction, sanitized probe errors, unified-memory uncertainty propagation,
and the fixed limits section.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from serving_verdict import canonical
from serving_verdict.capacity import ModelGeometry
from serving_verdict.doctor import (
    KNOWN_FINDING_RULES,
    DoctorInput,
    EndpointCapabilities,
    redact_text,
    run_doctor,
)
from serving_verdict.hwprobe import (
    ALLOWED_COMMANDS,
    COMMAND_TIMEOUT_S,
    MAX_PROBE_OUTPUT_BYTES,
    NVIDIA_SMI_HEADER_ARGV,
    NVIDIA_SMI_QUERY_ARGV,
    capture_hardware_snapshot,
)
from tests.test_hwprobe import HEADER_OUT, QUERY_OUT, make_runner, ok

GEOM = ModelGeometry(
    name="test-8b-bf16",
    num_layers=32,
    num_attention_heads=32,
    num_kv_heads=8,
    head_dim=128,
    num_params=8_000_000_000,
    weight_bytes_per_param=2,
    kv_bytes_per_elem=2,
    max_context_tokens=32768,
)

MEMINFO = "MemTotal:       32768480 kB\nMemAvailable:   16384000 kB\n"


@pytest.fixture()
def meminfo(tmp_path: Path) -> Path:
    p = tmp_path / "meminfo"
    p.write_text(MEMINFO, encoding="utf-8")
    return p


def gpu_snapshot(meminfo: Path):
    runner, _ = make_runner(
        {NVIDIA_SMI_QUERY_ARGV: ok(QUERY_OUT), NVIDIA_SMI_HEADER_ARGV: ok(HEADER_OUT)}
    )
    return capture_hardware_snapshot(runner=runner, meminfo_path=str(meminfo))


def base_input(snap, **overrides) -> DoctorInput:
    kwargs = {
        "snapshot": snap,
        "geometry": GEOM,
        "memory_total_bytes": 24_000_000_000,
        "context_tokens": 8192,
        "max_output_tokens": 1024,
        "target_concurrency": 3,
        "reserve_bytes": 2_000_000_000,
        "capabilities": EndpointCapabilities(streaming=True, usage_reporting=True),
    }
    kwargs.update(overrides)
    return DoctorInput(**kwargs)


def find(rep: dict, rule: str) -> dict | None:
    for f in rep["findings"]:
        if f["rule_id"] == rule:
            return f
    return None


def test_rule_set_closed_and_exact(meminfo: Path) -> None:
    rep = run_doctor(base_input(gpu_snapshot(meminfo)), generated_at="t0")
    assert frozenset(
        {
            "context_too_high",
            "weights_exceed_memory",
            "concurrency_infeasible",
            "kv_pressure",
            "missing_endpoint_capabilities",
        }
    ) == KNOWN_FINDING_RULES
    assert all(f["rule_id"] in KNOWN_FINDING_RULES for f in rep["findings"])
    assert all(set(f) == {"rule_id", "severity", "detail"} for f in rep["findings"])


def test_report_shape_and_schema(meminfo: Path) -> None:
    rep = run_doctor(base_input(gpu_snapshot(meminfo)), generated_at="t0")
    assert rep["schema_version"] == "serving-verdict.serving-doctor.v1"
    assert set(rep) == {
        "schema_version",
        "generated_at",
        "hardware",
        "model",
        "capacity",
        "findings",
        "limits",
        "report_digest",
    }
    assert rep["model"]["num_layers"] == 32
    assert rep["hardware"]["memory_model"] == "discrete_gpu"


def test_context_too_high_fires(meminfo: Path) -> None:
    rep = run_doctor(base_input(gpu_snapshot(meminfo), context_tokens=40_000), generated_at="t0")
    f = find(rep, "context_too_high")
    assert f is not None
    assert f["severity"] == "error"
    assert "40000" in f["detail"] and "32768" in f["detail"]


def test_context_too_high_not_fires(meminfo: Path) -> None:
    rep = run_doctor(base_input(gpu_snapshot(meminfo)), generated_at="t0")
    assert find(rep, "context_too_high") is None


def test_kv_pressure_fires_on_risk(meminfo: Path) -> None:
    # golden RISK case: 24 GB, target 3 -> utilization 0.900995
    rep = run_doctor(base_input(gpu_snapshot(meminfo)), generated_at="t0")
    f = find(rep, "kv_pressure")
    assert f is not None
    assert f["severity"] == "warning"
    assert rep["capacity"]["classification"] == "RISK"
    assert "90.1" in f["detail"]


def test_concurrency_infeasible(meminfo: Path) -> None:
    rep = run_doctor(base_input(gpu_snapshot(meminfo), target_concurrency=5), generated_at="t0")
    f = find(rep, "concurrency_infeasible")
    assert f is not None
    assert f["severity"] == "error"
    assert "5" in f["detail"] and "4" in f["detail"]
    assert rep["capacity"]["classification"] == "NO_FIT"


def test_missing_endpoint_capabilities(meminfo: Path) -> None:
    rep = run_doctor(
        base_input(
            gpu_snapshot(meminfo),
            capabilities=EndpointCapabilities(streaming=False, usage_reporting=False),
        ),
        generated_at="t0",
    )
    f = find(rep, "missing_endpoint_capabilities")
    assert f is not None
    assert f["severity"] == "warning"
    assert "streaming" in f["detail"]
    assert "usage_reporting" in f["detail"]
    for metric in ("ttft_s", "decode_tokens_per_s", "aggregate_output_tokens_per_s"):
        assert metric in f["detail"]
    assert "UNMEASURABLE" in f["detail"]


def test_capabilities_present_no_finding(meminfo: Path) -> None:
    rep = run_doctor(base_input(gpu_snapshot(meminfo)), generated_at="t0")
    assert find(rep, "missing_endpoint_capabilities") is None


def test_weights_exceed_memory(meminfo: Path) -> None:
    # 15 GB pool vs 16 GB exact weights (reserve 0): model does not fit.
    rep = run_doctor(
        base_input(
            gpu_snapshot(meminfo), memory_total_bytes=15_000_000_000, reserve_bytes=0
        ),
        generated_at="t0",
    )
    f = find(rep, "weights_exceed_memory")
    assert f is not None
    assert f["severity"] == "error"
    assert rep["capacity"]["classification"] == "NO_FIT"


def test_fit_report_has_no_findings(meminfo: Path) -> None:
    # golden FIT case: 26 GB, target 2
    rep = run_doctor(
        base_input(gpu_snapshot(meminfo), memory_total_bytes=26_000_000_000, target_concurrency=2),
        generated_at="t0",
    )
    assert rep["capacity"]["classification"] == "FIT"
    assert rep["findings"] == []


def test_digest_deterministic_across_generated_at(meminfo: Path) -> None:
    inp = base_input(gpu_snapshot(meminfo))
    a = run_doctor(inp, generated_at="2026-01-01T00:00:00Z")
    b = run_doctor(inp, generated_at="2026-12-31T23:59:59Z")
    assert a["report_digest"] == b["report_digest"]
    payload = {k: v for k, v in a.items() if k not in ("generated_at", "report_digest")}
    assert a["report_digest"] == canonical.digest_payload(canonical.canonicalize(payload))
    assert a["report_digest"].startswith("sha256:")


def test_redaction_of_paths_and_secrets() -> None:
    s = redact_text(
        "see /home/ozkangu/secret.yaml and Bearer abc12345xyz and sk-abcdef1234567890 end"
    )
    assert "/home/ozkangu" not in s
    assert "abc12345xyz" not in s
    assert "sk-abcdef1234567890" not in s
    assert "***" in s


def test_artifact_never_contains_host_secrets(meminfo: Path) -> None:
    # failing probe whose stderr carries a secret + model name carrying a path
    secret_err = ok("", rc=9, stderr="leak /var/lib/models/secret.yaml sk-token1234567890")
    runner, _ = make_runner(
        {
            NVIDIA_SMI_QUERY_ARGV: secret_err,
            NVIDIA_SMI_HEADER_ARGV: ok(HEADER_OUT),
        }
    )
    snap = capture_hardware_snapshot(runner=runner, meminfo_path=str(meminfo))
    geom = ModelGeometry(
        name="weights at /var/lib/models/secret-weights",
        num_layers=32,
        num_attention_heads=32,
        num_kv_heads=8,
        head_dim=128,
        num_params=8_000_000_000,
        weight_bytes_per_param=2,
        kv_bytes_per_elem=2,
        max_context_tokens=32768,
    )
    rep = run_doctor(base_input(snap, geometry=geom), generated_at="t0")
    dumped = json.dumps(rep)
    assert "sk-token1234567890" not in dumped
    assert "/var/lib" not in dumped
    assert "secret.yaml" not in dumped


def test_malformed_probe_surfaces_sanitized(meminfo: Path) -> None:
    runner, _ = make_runner(
        {
            NVIDIA_SMI_QUERY_ARGV: ok("GARBAGE_SECRET_MARKER\n"),
            NVIDIA_SMI_HEADER_ARGV: ok(HEADER_OUT),
        }
    )
    snap = capture_hardware_snapshot(runner=runner, meminfo_path=str(meminfo))
    assert snap.probe_errors[0].category == "malformed_output"
    rep = run_doctor(base_input(snap), generated_at="t0")
    assert "GARBAGE_SECRET_MARKER" not in json.dumps(rep)


def test_unified_memory_uncertainty_propagates(meminfo: Path) -> None:
    err = FileNotFoundError(2, "nvidia-smi not found")
    runner, _ = make_runner(
        {NVIDIA_SMI_QUERY_ARGV: err, NVIDIA_SMI_HEADER_ARGV: FileNotFoundError(2, "nope")}
    )
    snap = capture_hardware_snapshot(runner=runner, meminfo_path=str(meminfo))
    assert snap.memory_model == "unified_or_shared"
    rep = run_doctor(
        base_input(snap, memory_total_bytes=32_768_000_000), generated_at="t0"
    )
    assert rep["hardware"]["memory_model"] == "unified_or_shared"
    assert any("unified" in a.lower() for a in rep["capacity"]["assumptions"])


def test_limits_section_fixed(meminfo: Path) -> None:
    rep = run_doctor(base_input(gpu_snapshot(meminfo)), generated_at="t0")
    assert rep["limits"]["command_allowlist"] == sorted(map(list, ALLOWED_COMMANDS))
    assert rep["limits"]["command_timeout_s"] == COMMAND_TIMEOUT_S
    assert rep["limits"]["max_probe_output_bytes"] == MAX_PROBE_OUTPUT_BYTES
