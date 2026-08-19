"""TDD tests for read-only hardware probing (hwprobe module).

Covers: nvidia-smi CSV/header parsing, fixed command allowlist, malformed
output, nonzero exit / missing binary / timeout with sanitized errors,
oversized output rejection, no-GPU CPU/RAM fallback with explicit
unified-memory uncertainty, and /proc/meminfo parsing (incl. negatives).
No test depends on a host GPU: the command runner is fully injected.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from serving_verdict.hwprobe import (
    ALLOWED_COMMANDS,
    COMMAND_TIMEOUT_S,
    MAX_PROBE_OUTPUT_BYTES,
    NVIDIA_SMI_HEADER_ARGV,
    NVIDIA_SMI_QUERY_ARGV,
    CommandResult,
    capture_hardware_snapshot,
    host_cpu_ram,
    run_allowlisted,
)

HEADER_OUT = (
    "Tue Aug 18 22:17:57 2026\n"
    "+-----------------------------------------------------------------------------------------+\n"
    "| NVIDIA-SMI 580.173.02             Driver Version: 580.173.02     CUDA Version: 13.0     |\n"
    "+-----------------------------------------------------------------------------------------+\n"
)

QUERY_OUT = (
    "0, NVIDIA H100 80GB HBM3, 580.173.02, 81559, 3, 81556, 00000000:41:00.0\n"
    "1, NVIDIA A100 80GB, 580.173.02, 81920, 120, 81800, 00000000:C1:00.0\n"
)

MEMINFO = "MemTotal:       32768480 kB\nMemFree:           1000 kB\nMemAvailable:   16384000 kB\n"


def ok(stdout: str, rc: int = 0, stderr: str = "") -> CommandResult:
    return CommandResult(rc, stdout, stderr)


def make_runner(responses: dict[tuple[str, ...], object]) -> tuple:
    calls: list[tuple[str, ...]] = []

    def runner(argv: object) -> CommandResult:
        key = tuple(argv)  # type: ignore[arg-type]
        calls.append(key)
        resp = responses.get(key)
        if resp is None:
            raise AssertionError(f"unexpected probe argv: {key!r}")
        if isinstance(resp, BaseException):
            raise resp
        return resp  # type: ignore[return-value]

    return runner, calls


@pytest.fixture()
def meminfo(tmp_path: Path) -> Path:
    p = tmp_path / "meminfo"
    p.write_text(MEMINFO, encoding="utf-8")
    return p


def test_full_nvidia_snapshot(meminfo: Path) -> None:
    runner, calls = make_runner(
        {NVIDIA_SMI_QUERY_ARGV: ok(QUERY_OUT), NVIDIA_SMI_HEADER_ARGV: ok(HEADER_OUT)}
    )
    snap = capture_hardware_snapshot(runner=runner, meminfo_path=str(meminfo))
    assert snap.gpu_probe == "nvidia-smi"
    assert snap.nvidia is not None
    assert snap.nvidia.cuda_version == "13.0"
    assert len(snap.nvidia.devices) == 2
    d0 = snap.nvidia.devices[0]
    assert d0.index == 0
    assert d0.name == "NVIDIA H100 80GB HBM3"
    assert d0.driver_version == "580.173.02"
    assert d0.memory_total_bytes == 81559 * 1024 * 1024
    assert d0.memory_used_bytes == 3 * 1024 * 1024
    assert d0.memory_free_bytes == 81556 * 1024 * 1024
    assert d0.pci_bus_id == "00000000:41:00.0"
    d1 = snap.nvidia.devices[1]
    assert d1.index == 1
    assert d1.memory_total_bytes == 81920 * 1024 * 1024
    assert snap.memory_model == "discrete_gpu"
    assert snap.uncertainty_notes == ()
    assert snap.probe_errors == ()
    ident = snap.host_identity
    assert ident.device_name == "NVIDIA H100 80GB HBM3"
    assert ident.device_index == 0
    assert ident.driver_version == "580.173.02"
    assert ident.cuda_version == "13.0"
    assert ident.device_memory_total_bytes == 81559 * 1024 * 1024
    assert set(calls) == {NVIDIA_SMI_QUERY_ARGV, NVIDIA_SMI_HEADER_ARGV}
    assert len(calls) == 2


def test_allowlist_exactly_two_fixed_commands() -> None:
    assert frozenset({NVIDIA_SMI_QUERY_ARGV, NVIDIA_SMI_HEADER_ARGV}) == ALLOWED_COMMANDS
    assert NVIDIA_SMI_QUERY_ARGV[0] == "nvidia-smi"
    assert any(a.startswith("--query-gpu=") for a in NVIDIA_SMI_QUERY_ARGV)
    assert any(a.startswith("--format=csv,noheader,nounits") for a in NVIDIA_SMI_QUERY_ARGV)
    assert any("memory.total" in a for a in NVIDIA_SMI_QUERY_ARGV)
    assert NVIDIA_SMI_HEADER_ARGV == ("nvidia-smi",)


@pytest.mark.parametrize(
    "argv",
    [
        ("nvidia-smi", "--help"),
        ("nvidia-smi", "--version"),
        ("rm", "-rf", "/"),
        ("sh", "-c", "nvidia-smi"),
    ],
)
def test_disallowed_command_rejected(argv: tuple[str, ...]) -> None:
    runner, _ = make_runner({argv: ok("")})
    with pytest.raises(Exception) as ei:
        run_allowlisted(argv, runner)
    assert ei.value.category == "disallowed_command"


def test_nonzero_exit_sanitized(meminfo: Path) -> None:
    runner, _ = make_runner(
        {
            NVIDIA_SMI_QUERY_ARGV: ok("", rc=9, stderr="fatal: /home/ozkangu/secret.yaml sk-abcdef1234567890"),
            NVIDIA_SMI_HEADER_ARGV: ok(HEADER_OUT),
        }
    )
    snap = capture_hardware_snapshot(runner=runner, meminfo_path=str(meminfo))
    assert snap.gpu_probe == "fallback"
    assert snap.nvidia is None
    errs = snap.probe_errors
    assert len(errs) == 1
    assert errs[0].category == "command_failed"
    assert "9" in errs[0].detail
    dumped = json.dumps(snap.to_dict())
    assert "sk-abcdef1234567890" not in dumped
    assert "/home/ozkangu" not in dumped
    assert snap.memory_model == "unified_or_shared"
    assert any("unified" in n.lower() for n in snap.uncertainty_notes)


def test_missing_binary_fallback(meminfo: Path) -> None:
    err = FileNotFoundError(2, "nvidia-smi not found")
    runner, _ = make_runner(
        {NVIDIA_SMI_QUERY_ARGV: err, NVIDIA_SMI_HEADER_ARGV: FileNotFoundError(2, "nope")}
    )
    snap = capture_hardware_snapshot(runner=runner, meminfo_path=str(meminfo))
    assert snap.gpu_probe == "fallback"
    assert snap.nvidia is None
    assert [e.category for e in snap.probe_errors] == ["missing_binary"]
    assert snap.cpu_ram.source == "host"
    assert snap.cpu_ram.cpu_count == os.cpu_count()
    assert snap.cpu_ram.memory_total_bytes == 32768480 * 1024
    assert snap.cpu_ram.memory_available_bytes == 16384000 * 1024
    assert snap.memory_model == "unified_or_shared"
    assert any("unified" in n.lower() for n in snap.uncertainty_notes)


def test_timeout_sanitized(meminfo: Path) -> None:
    err = subprocess.TimeoutExpired(cmd="nvidia-smi", timeout=COMMAND_TIMEOUT_S)
    runner, _ = make_runner(
        {NVIDIA_SMI_QUERY_ARGV: err, NVIDIA_SMI_HEADER_ARGV: subprocess.TimeoutExpired("nvidia-smi", 10)}
    )
    snap = capture_hardware_snapshot(runner=runner, meminfo_path=str(meminfo))
    assert snap.gpu_probe == "fallback"
    assert [e.category for e in snap.probe_errors] == ["timeout"]
    assert "nvidia-smi" not in snap.probe_errors[0].detail


@pytest.mark.parametrize(
    "bad",
    [
        "",  # no rows at all
        "0, NVIDIA X\n",  # too few fields
        "0, NVIDIA X, 1.0, 8192, 1, 8191, PCI, extra\n",  # too many fields
        "0, NVIDIA X, 1.0, 8192, -5, 8197, PCI\n",  # negative used
        "0, NVIDIA X, 1.0, oops, 1, 8191, PCI\n",  # non-numeric total
        "x, NVIDIA X, 1.0, 8192, 1, 8191, PCI\n",  # non-numeric index
        "0, , 1.0, 8192, 1, 8191, PCI\n",  # empty name
        "0, NVIDIA X, 1.0, 0, 0, 0, PCI\n",  # zero memory total
    ],
)
def test_malformed_rows_fallback(meminfo: Path, bad: str) -> None:
    runner, _ = make_runner(
        {NVIDIA_SMI_QUERY_ARGV: ok(bad), NVIDIA_SMI_HEADER_ARGV: ok(HEADER_OUT)}
    )
    snap = capture_hardware_snapshot(runner=runner, meminfo_path=str(meminfo))
    assert snap.nvidia is None
    assert snap.gpu_probe == "fallback"
    assert snap.probe_errors[0].category == "malformed_output"
    dumped = json.dumps(snap.to_dict())
    assert "oops" not in dumped
    assert "NVIDIA X" not in dumped


def test_oversized_output_rejected(meminfo: Path) -> None:
    big = "0," + "x" * (MAX_PROBE_OUTPUT_BYTES + 1)
    runner, _ = make_runner(
        {NVIDIA_SMI_QUERY_ARGV: ok(big), NVIDIA_SMI_HEADER_ARGV: ok(HEADER_OUT)}
    )
    snap = capture_hardware_snapshot(runner=runner, meminfo_path=str(meminfo))
    assert snap.probe_errors[0].category == "output_too_large"
    assert "xxx" not in json.dumps(snap.to_dict())


def test_header_without_cuda_version(meminfo: Path) -> None:
    hdr = "| NVIDIA-SMI 535.104.05 Driver Version: 535.104.05 |\n"
    runner, _ = make_runner(
        {NVIDIA_SMI_QUERY_ARGV: ok(QUERY_OUT), NVIDIA_SMI_HEADER_ARGV: ok(hdr)}
    )
    snap = capture_hardware_snapshot(runner=runner, meminfo_path=str(meminfo))
    assert snap.nvidia is not None
    assert snap.nvidia.cuda_version is None
    assert snap.memory_model == "discrete_gpu"
    assert any("cuda" in n.lower() for n in snap.uncertainty_notes)
    # device-row driver takes precedence over the header when both exist
    assert snap.host_identity.driver_version == "580.173.02"


def test_host_cpu_ram_parses_meminfo(meminfo: Path) -> None:
    cr = host_cpu_ram(meminfo_path=str(meminfo))
    assert cr.source == "host"
    assert cr.cpu_count == os.cpu_count()
    assert cr.memory_total_bytes == 32768480 * 1024
    assert cr.memory_available_bytes == 16384000 * 1024


def test_host_cpu_ram_unavailable(tmp_path: Path) -> None:
    cr = host_cpu_ram(meminfo_path=str(tmp_path / "nope"))
    assert cr.memory_total_bytes is None
    assert cr.memory_available_bytes is None
    assert cr.cpu_count == os.cpu_count()
    assert cr.source != "host"


def test_negative_meminfo_rejected(tmp_path: Path) -> None:
    p = tmp_path / "m"
    p.write_text("MemTotal: -5 kB\nMemAvailable: 100 kB\n", encoding="utf-8")
    cr = host_cpu_ram(meminfo_path=str(p))
    assert cr.memory_total_bytes is None
    assert cr.source != "host"


def test_snapshot_to_dict_shape(meminfo: Path) -> None:
    runner, _ = make_runner(
        {NVIDIA_SMI_QUERY_ARGV: ok(QUERY_OUT), NVIDIA_SMI_HEADER_ARGV: ok(HEADER_OUT)}
    )
    snap = capture_hardware_snapshot(runner=runner, meminfo_path=str(meminfo))
    d = snap.to_dict()
    assert d["schema"] == "serving-verdict.hardware-snapshot.v1"
    assert {
        "gpu_probe",
        "nvidia",
        "cpu_ram",
        "memory_model",
        "uncertainty_notes",
        "host_identity",
        "probe_errors",
    } <= set(d)
    json.dumps(d, sort_keys=True)  # must be JSON-serializable
