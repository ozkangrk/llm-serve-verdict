"""Supported runtime launch profiles (dry-run only).

A :class:`RuntimeProfile` describes how a launch command is *assembled* for a
runtime family from an allowlisted set of flags. It never runs anything: the
recipe layer turns these into inert argv arrays and shell-escaped text.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from serving_verdict.advisor import AdvisorError
from serving_verdict.advisor.flags import (
    BOOL,
    FLOAT,
    INT,
    FlagSpec,
    validate_flag,
    validate_mapping,
)

MODEL_TOKEN = "{model_path}"


class UnsupportedProfileError(AdvisorError):
    """Runtime family has no supported launch profile."""


@dataclass(frozen=True)
class LaunchCommand:
    argv_base: tuple[str, ...]
    model_position: int  # index in argv_base where the model path token sits
    flag_map: dict[str, FlagSpec]


@dataclass(frozen=True)
class ProfileSpec:
    family: str
    command: LaunchCommand


# Backwards-friendly alias.
RuntimeProfile = ProfileSpec


_VLLM_FLAGS: dict[str, FlagSpec] = {
    "allowed_max_tokens": FlagSpec("allowed_max_tokens", "--allowed-max-tokens", INT, 256, 1048576),
    "max_num_batched_tokens": FlagSpec("max_num_batched_tokens", "--max-num-batched-tokens", INT, 256, 1048576),
    "max_model_len": FlagSpec("max_model_len", "--max-model-len", INT, 512, 1048576),
    "max_num_seqs": FlagSpec("max_num_seqs", "--max-num-seqs", INT, 1, 256),
    "gpu_memory_utilization": FlagSpec("gpu_memory_utilization", "--gpu-memory-utilization", FLOAT, 0.1, 0.95),
    "enable_chunked_prefill": FlagSpec("enable_chunked_prefill", "--enable-chunked-prefill", BOOL, 0, 1),
    "disable_chunked_prefill": FlagSpec("disable_chunked_prefill", "--disable-chunked-prefill", BOOL, 0, 1),
    # Deliberately listed and refused: never allowed in dry-run mode.
    "trust_remote_code": FlagSpec("trust_remote_code", "--trust-remote-code", BOOL, 0, 1, unsafe=True),
}

_SGLANG_FLAGS: dict[str, FlagSpec] = {
    "context_length": FlagSpec("context_length", "--context-length", INT, 512, 1048576),
    "max_running_requests": FlagSpec("max_running_requests", "--max-running-requests", INT, 1, 256),
    "mem_fraction_static": FlagSpec("mem_fraction_static", "--mem-fraction-static", FLOAT, 0.1, 0.95),
    "chunked_prefill_size": FlagSpec("chunked_prefill_size", "--chunked-prefill-size", INT, 64, 1048576),
    "trust_remote_code": FlagSpec("trust_remote_code", "--trust-remote-code", BOOL, 0, 1, unsafe=True),
}

_LLAMA_CPP_FLAGS: dict[str, FlagSpec] = {
    "ctx_size": FlagSpec("ctx_size", "--ctx-size", INT, 512, 1048576),
    "n_parallel": FlagSpec("n_parallel", "--n-parallel", INT, 1, 64),
    "n_batch": FlagSpec("n_batch", "--n-batch", INT, 1, 65536),
    "flash_attn": FlagSpec("flash_attn", "--flash-attn", BOOL, 0, 1),
}

_PROFILES: dict[str, ProfileSpec] = {
    "vllm": ProfileSpec(
        "vllm",
        LaunchCommand(
            argv_base=(
                "python",
                "-m",
                "vllm.entrypoints.openai.api_server",
                MODEL_TOKEN,
            ),
            model_position=3,
            flag_map=_VLLM_FLAGS,
        ),
    ),
    "sglang": ProfileSpec(
        "sglang",
        LaunchCommand(
            argv_base=(
                "python",
                "-m",
                "sglang.launch_server",
                MODEL_TOKEN,
            ),
            model_position=3,
            flag_map=_SGLANG_FLAGS,
        ),
    ),
    "llama.cpp": ProfileSpec(
        "llama.cpp",
        LaunchCommand(
            argv_base=(
                "llama-server",
                MODEL_TOKEN,
            ),
            model_position=1,
            flag_map=_LLAMA_CPP_FLAGS,
        ),
    ),
}


def get_profile(family: str) -> ProfileSpec:
    profile = _PROFILES.get(family)
    if profile is None:
        raise UnsupportedProfileError(f"no supported launch profile for runtime family {family!r}")
    return profile


def validate_flags(profile: ProfileSpec, flags: dict[str, Any]) -> None:
    """Fail closed on any violation in the given flag mapping."""
    from serving_verdict.advisor.flags import format_violations

    violations = validate_mapping(profile.command.flag_map, flags)
    if violations:
        raise AdvisorError(f"flag violations (fail-closed): {format_violations(violations)}")


def check_flag_value(spec: FlagSpec, value: Any) -> None:
    v = validate_flag(spec, value)
    if v is not None:
        raise AdvisorError(f"flag violation (fail-closed): {v.detail}")


def flag_value_to_cli(spec: FlagSpec, value: Any) -> str:
    if spec.kind == BOOL:
        return "1" if value else "0"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)
