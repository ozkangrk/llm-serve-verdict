"""Read-only hardware probing for the Serving Doctor.

Safety model (PRODUCT_V1_SPEC safety invariants):

- Read-only only: the *only* commands ever executed are the two fixed
  ``nvidia-smi`` argv tuples in :data:`ALLOWED_COMMANDS`. There is no
  ``shell=True``, no shell expansion, no operator-supplied argv, no
  mutation of servers or containers.
- The command runner is injectable (``Callable[[Sequence[str]], CommandResult]``)
  so tests never depend on a host GPU. The default runner uses
  ``subprocess.run`` with a hard timeout and output-size cap.
- All probe failures surface as sanitized :class:`ProbeError` values with a
  fixed vocabulary of categories and fixed detail strings; raw stderr is
  never embedded in any artifact (no host secrets, no host paths).
- When no NVIDIA GPU is detectable the snapshot falls back to CPU/RAM
  identity (``/proc/meminfo``) and the memory model is explicitly marked
  ``unified_or_shared`` so downstream capacity estimates carry that
  uncertainty instead of pretending to be a GPU measurement.
"""
from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

#: Upper bound on a single probe's captured output (bytes). Anything larger
#: is rejected instead of parsed, so a hostile or broken tool cannot flood
#: the artifact.
MAX_PROBE_OUTPUT_BYTES = 64 * 1024

#: Hard wall-clock budget for every probe command.
COMMAND_TIMEOUT_S = 10.0

_GPU_SCHEMA = "serving-verdict.hardware-snapshot.v1"

#: The fixed, closed allowlist of probe commands. Membership is the only
#: gate before any execution.
NVIDIA_SMI_QUERY_ARGV: tuple[str, ...] = (
    "nvidia-smi",
    "--query-gpu=index,name,driver_version,memory.total,memory.used,memory.free,pci.bus_id",
    "--format=csv,noheader,nounits",
)
NVIDIA_SMI_HEADER_ARGV: tuple[str, ...] = ("nvidia-smi",)

ALLOWED_COMMANDS: frozenset[tuple[str, ...]] = frozenset(
    {NVIDIA_SMI_QUERY_ARGV, NVIDIA_SMI_HEADER_ARGV}
)

#: Expected CSV field count for one --query-gpu row (see NVIDIA_SMI_QUERY_ARGV).
_GPU_ROW_FIELDS = 7

_MIB = 1024 * 1024

_CUDA_VERSION_RE = re.compile(r"CUDA Version:\s*([0-9][0-9A-Za-z.+\-]*)")
_MEMINFO_RE = re.compile(r"^(MemTotal|MemAvailable):\s*(-?\d+)\s*kB\s*$")


class ProbeError(Exception):
    """A fixed-argv probe failed in a way that is safe to report.

    ``detail`` is always a fixed, sanitized string; nothing from the child
    process output is included.
    """

    def __init__(self, category: str, detail: str) -> None:
        super().__init__(f"{category}: {detail}")
        self.category = category
        self.detail = detail


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Normalized result of one allowlisted probe command."""

    returncode: int
    stdout: str
    stderr: str


#: Injectable command runner. Must be side-effect limited to the fixed argv.
CommandRunner = Callable[[Sequence[str]], CommandResult]


@dataclass(frozen=True, slots=True)
class GpuDevice:
    """One NVIDIA device as reported by the fixed nvidia-smi query."""

    index: int
    name: str
    driver_version: str
    memory_total_bytes: int
    memory_used_bytes: int
    memory_free_bytes: int
    pci_bus_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "name": self.name,
            "driver_version": self.driver_version,
            "memory_total_bytes": self.memory_total_bytes,
            "memory_used_bytes": self.memory_used_bytes,
            "memory_free_bytes": self.memory_free_bytes,
            "pci_bus_id": self.pci_bus_id,
        }


@dataclass(frozen=True, slots=True)
class GpuProbe:
    """Aggregated NVIDIA probe: driver/CUDA identity + device list."""

    cuda_version: str | None
    devices: tuple[GpuDevice, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "cuda_version": self.cuda_version,
            "devices": [d.to_dict() for d in self.devices],
        }


@dataclass(frozen=True, slots=True)
class CpuRam:
    """Host CPU/RAM identity (fallback when no NVIDIA GPU is present).

    ``source`` is ``"host"`` when both memory values were parsed from
    ``/proc/meminfo`` and ``"unavailable"`` otherwise (missing file,
    malformed or negative values). CPU count always comes from the host.
    """

    cpu_count: int
    memory_total_bytes: int | None
    memory_available_bytes: int | None
    source: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "cpu_count": self.cpu_count,
            "memory_total_bytes": self.memory_total_bytes,
            "memory_available_bytes": self.memory_available_bytes,
            "source": self.source,
        }


@dataclass(frozen=True, slots=True)
class HostIdentity:
    """Best-known serving device identity for the capacity planner."""

    device_name: str | None
    device_index: int | None
    driver_version: str | None
    cuda_version: str | None
    device_memory_total_bytes: int | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "device_name": self.device_name,
            "device_index": self.device_index,
            "driver_version": self.driver_version,
            "cuda_version": self.cuda_version,
            "device_memory_total_bytes": self.device_memory_total_bytes,
        }


@dataclass(frozen=True, slots=True)
class ProbeErrorInfo:
    """Sanitized probe failure record (fixed category + fixed detail)."""

    category: str
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {"category": self.category, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class HardwareSnapshot:
    """Typed, read-only hardware snapshot (GPU probe + CPU/RAM fallback)."""

    gpu_probe: str  # "nvidia-smi" | "fallback"
    nvidia: GpuProbe | None
    cpu_ram: CpuRam
    uncertainty_notes: tuple[str, ...]
    probe_errors: tuple[ProbeErrorInfo, ...]

    @property
    def memory_model(self) -> str:
        """``discrete_gpu`` when an NVIDIA device was measured, otherwise
        the pool is treated as unified/shared memory and every downstream
        capacity number inherits that uncertainty."""
        return "discrete_gpu" if self.nvidia is not None else "unified_or_shared"

    @property
    def host_identity(self) -> HostIdentity:
        if self.nvidia is not None and self.nvidia.devices:
            dev = self.nvidia.devices[0]
            return HostIdentity(
                device_name=dev.name,
                device_index=dev.index,
                driver_version=dev.driver_version,
                cuda_version=self.nvidia.cuda_version,
                device_memory_total_bytes=dev.memory_total_bytes,
            )
        return HostIdentity(None, None, None, None, None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": _GPU_SCHEMA,
            "gpu_probe": self.gpu_probe,
            "nvidia": self.nvidia.to_dict() if self.nvidia is not None else None,
            "cpu_ram": self.cpu_ram.to_dict(),
            "memory_model": self.memory_model,
            "uncertainty_notes": list(self.uncertainty_notes),
            "host_identity": self.host_identity.to_dict(),
            "probe_errors": [e.to_dict() for e in self.probe_errors],
        }


def run_allowlisted(argv: Sequence[str], runner: CommandRunner) -> CommandResult:
    """Execute ``argv`` only if it is one of the fixed allowlist tuples.

    Raises :class:`ProbeError` with a sanitized category for every failure
    mode: disallowed command, missing binary, timeout, oversized output,
    nonzero exit.
    """
    if tuple(argv) not in ALLOWED_COMMANDS:
        raise ProbeError("disallowed_command", "command is not in the fixed probe allowlist")
    try:
        result = runner(list(argv))
    except subprocess.TimeoutExpired as exc:
        raise ProbeError("timeout", "probe command timed out and was terminated") from exc
    except OSError as exc:
        raise ProbeError(
            "missing_binary", "probe command is not installed on this host"
        ) from exc
    if len(result.stdout) > MAX_PROBE_OUTPUT_BYTES or len(result.stderr) > MAX_PROBE_OUTPUT_BYTES:
        raise ProbeError("output_too_large", "probe output exceeds the maximum allowed size")
    if result.returncode != 0:
        raise ProbeError("command_failed", f"probe command exited with code {result.returncode}")
    return result


def _default_runner(argv: Sequence[str]) -> CommandResult:
    """Default runner: subprocess.run with a fixed argv, hard timeout, no shell."""
    proc = subprocess.run(  # no shell=True, argv is already allowlisted
        list(argv),
        capture_output=True,
        timeout=COMMAND_TIMEOUT_S,
        check=False,
    )
    return CommandResult(
        proc.returncode,
        proc.stdout.decode("utf-8", "replace"),
        proc.stderr.decode("utf-8", "replace"),
    )


def _parse_gpu_rows(stdout: str) -> tuple[GpuDevice, ...]:
    """Parse the fixed 7-field CSV rows; any deviation is a ProbeError."""
    rows = [line for line in stdout.splitlines() if line.strip()]
    devices: list[GpuDevice] = []
    for row in rows:
        parts = [p.strip() for p in row.split(",")]
        if len(parts) != _GPU_ROW_FIELDS:
            raise ProbeError("malformed_output", _MALFORMED_DETAIL)
        try:
            index = int(parts[0])
            total_mib = int(parts[3])
            used_mib = int(parts[4])
            free_mib = int(parts[5])
        except ValueError as exc:
            raise ProbeError("malformed_output", _MALFORMED_DETAIL) from exc
        if (
            index < 0
            or not parts[1]
            or not parts[2]
            or total_mib <= 0
            or used_mib < 0
            or free_mib < 0
            or used_mib > total_mib
            or free_mib > total_mib
            or not parts[6]
        ):
            raise ProbeError("malformed_output", _MALFORMED_DETAIL)
        devices.append(
            GpuDevice(
                index=index,
                name=parts[1],
                driver_version=parts[2],
                memory_total_bytes=total_mib * _MIB,
                memory_used_bytes=used_mib * _MIB,
                memory_free_bytes=free_mib * _MIB,
                pci_bus_id=parts[6],
            )
        )
    if not devices:
        raise ProbeError("malformed_output", _MALFORMED_DETAIL)
    return tuple(devices)


_MALFORMED_DETAIL = (
    "nvidia-smi output did not match the expected fixed query format "
    "(7 numeric/string CSV fields per row)"
)


def _cuda_version_from_header(stdout: str) -> str | None:
    match = _CUDA_VERSION_RE.search(stdout)
    return match.group(1) if match else None


def host_cpu_ram(meminfo_path: str | None = None) -> CpuRam:
    """Read host CPU/RAM identity from ``/proc/meminfo`` (fail-soft).

    Malformed or negative values yield ``None`` memory fields and a
    non-``"host"`` source instead of fabricated numbers.
    """
    cpu_count = int(os.cpu_count() or 0)
    path = meminfo_path if meminfo_path is not None else "/proc/meminfo"
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read(MAX_PROBE_OUTPUT_BYTES)
    except OSError:
        return CpuRam(cpu_count=cpu_count, memory_total_bytes=None,
                      memory_available_bytes=None, source="unavailable")
    fields: dict[str, int] = {}
    for line in text.splitlines():
        match = _MEMINFO_RE.match(line)
        if match:
            fields[match.group(1)] = int(match.group(2))
    total_kib = fields.get("MemTotal")
    avail_kib = fields.get("MemAvailable")
    if total_kib is None or avail_kib is None or total_kib < 0 or avail_kib < 0:
        return CpuRam(cpu_count=cpu_count, memory_total_bytes=None,
                      memory_available_bytes=None, source="unavailable")
    return CpuRam(
        cpu_count=cpu_count,
        memory_total_bytes=total_kib * 1024,
        memory_available_bytes=avail_kib * 1024,
        source="host",
    )


def capture_hardware_snapshot(
    runner: CommandRunner | None = None,
    meminfo_path: str | None = None,
) -> HardwareSnapshot:
    """Run the fixed read-only probes and build a typed hardware snapshot.

    Every probe failure is recorded as a sanitized error and the snapshot
    degrades to the CPU/RAM fallback instead of raising, so the doctor can
    still report *why* it is uncertain.
    """
    active_runner = runner if runner is not None else _default_runner
    probe_errors: list[ProbeErrorInfo] = []
    notes: list[str] = []
    nvidia: GpuProbe | None = None

    try:
        query = run_allowlisted(NVIDIA_SMI_QUERY_ARGV, active_runner)
        devices = _parse_gpu_rows(query.stdout)
        header: str = ""
        try:
            header_result = run_allowlisted(NVIDIA_SMI_HEADER_ARGV, active_runner)
            header = header_result.stdout
        except ProbeError as header_err:
            probe_errors.append(ProbeErrorInfo(header_err.category, header_err.detail))
        cuda = _cuda_version_from_header(header)
        if cuda is None:
            notes.append(
                "CUDA version could not be determined from the nvidia-smi header; "
                "driver/CUDA compatibility is unverified."
            )
        nvidia = GpuProbe(cuda_version=cuda, devices=devices)
    except ProbeError as query_err:
        probe_errors.append(ProbeErrorInfo(query_err.category, query_err.detail))
        notes.append(
            "No NVIDIA GPU was detected: memory capacity planning falls back to "
            "host CPU/RAM (unified/shared memory) and is therefore uncertain; "
            "re-check against the actual accelerator before relying on concurrency."
        )

    return HardwareSnapshot(
        gpu_probe="nvidia-smi" if nvidia is not None else "fallback",
        nvidia=nvidia,
        cpu_ram=host_cpu_ram(meminfo_path),
        uncertainty_notes=tuple(notes),
        probe_errors=tuple(probe_errors),
    )
