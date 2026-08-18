"""Strict per-runtime flag allowlists, types, and ranges.

The advisor only ever sees flags through this module. Any flag that is not
explicitly allowlisted, has a wrong type, falls outside its allowed range,
is marked unsafe, forms an incompatible pair, or carries secret material is
a :class:`FlagViolation` and the advisor fails closed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

_SHELL_METACHAR_RE = re.compile(r"[;&|`$()<>{}\[\]&\n\r\t'\"]")
_SECRET_KEY_RE = re.compile(r"(^|_)(secret|password|passw?d|api_?key|credential|auth|token)(_|$)")

INT = "int"
FLOAT = "float"
BOOL = "bool"


@dataclass(frozen=True)
class FlagSpec:
    """One allowlisted flag for a runtime family."""

    name: str  # snake_case key used in current_flags/overrides
    cli: str  # CLI spelling, e.g. "--allowed-max-tokens"
    kind: str  # "int" | "float" | "bool"
    min: float
    max: float
    unsafe: bool = False
    note: str = ""


@dataclass(frozen=True)
class FlagViolation:
    flag: str
    code: str  # "unsafe" | "unknown" | "bad_type" | "out_of_range" | "conflict" | "secret" | "metachar"
    detail: str


def _type_name(v: Any) -> str:
    return type(v).__name__


def validate_flag(spec: FlagSpec, value: Any) -> FlagViolation | None:
    if spec.unsafe:
        return FlagViolation(spec.name, "unsafe", f"{spec.name} is unsafe and never allowed in dry-run mode")
    if spec.kind == BOOL:
        if not isinstance(value, bool):
            return FlagViolation(spec.name, "bad_type", f"{spec.name} must be a bool, got {_type_name(value)}")
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return FlagViolation(spec.name, "bad_type", f"{spec.name} must be a number, got {_type_name(value)}")
    num = float(value)
    if not (spec.min <= num <= spec.max):
        lo, hi = spec.min, spec.max
        return FlagViolation(
            spec.name, "out_of_range", f"{spec.name}={value!r} outside allowed range [{lo:g}, {hi:g}]"
        )
    return None


def validate_mapping(flag_map: dict[str, FlagSpec], flags: dict[str, Any]) -> list[FlagViolation]:
    """Validate a full flag mapping. Deterministic order (sorted keys)."""
    violations: list[FlagViolation] = []
    for key in sorted(flags):
        value = flags[key]
        if _SECRET_KEY_RE.search(key):
            violations.append(
                FlagViolation(key, "secret", f"flag key {key!r} looks like secret material; rejected")
            )
            continue
        spec = flag_map.get(key)
        if spec is None:
            violations.append(
                FlagViolation(key, "unknown", f"{key!r} is not in the allowlist for this runtime family")
            )
            continue
        v = validate_flag(spec, value)
        if v is not None:
            violations.append(v)
    # Incompatible pairs: both sides of a toggle set to true.
    for a, b in (("enable_chunked_prefill", "disable_chunked_prefill"),):
        if flags.get(a) is True and flags.get(b) is True:
            violations.append(
                FlagViolation(
                    a,
                    "conflict",
                    f"{a} and {b} are incompatible; both cannot be enabled",
                )
            )
    # De-duplicate while preserving deterministic order.
    seen: set[tuple[str, str]] = set()
    unique: list[FlagViolation] = []
    for v in violations:
        k = (v.flag, v.code)
        if k not in seen:
            seen.add(k)
            unique.append(v)
    return unique


def format_violations(violations: list[FlagViolation]) -> str:
    return "; ".join(f"{v.flag}: {v.detail}" for v in violations)


def shell_metacharacters(value: str) -> bool:
    return bool(_SHELL_METACHAR_RE.search(value))
