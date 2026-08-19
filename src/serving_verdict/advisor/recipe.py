"""Safe, inert launch/rollback recipes for supported runtime profiles.

``build_recipe`` produces a :class:`Recipe`:

- ``launch_argv`` / ``rollback_argv`` are plain argv lists (inert data),
- ``rendered_shell`` / ``rendered_rollback_shell`` are shell-escaped text via
  :func:`shlex.join` — presentation only, **never executed**,
- ``flag_diff`` deterministically lists every flag that differs between the
  proposed launch and the current (rollback) state.

Both argvs are built exclusively from the profile's allowlisted flag specs;
unknown/unsafe/out-of-range flags and secret-looking values are rejected
before any argv token exists. The module performs no process execution.
"""
from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import Any

from serving_verdict.advisor import AdvisorError
from serving_verdict.advisor.flags import (
    FlagSpec,
    shell_metacharacters,
    validate_flag,
    validate_mapping,
)
from serving_verdict.advisor.profiles import (
    ProfileSpec,
    flag_value_to_cli,
    get_profile,
)


@dataclass(frozen=True)
class Recipe:
    family: str
    launch_argv: list[str]
    rollback_argv: list[str]
    flag_diff: tuple[RollbackDiff, ...]
    rendered_shell: str
    rendered_rollback_shell: str


@dataclass(frozen=True)
class RollbackDiff:
    flag: str  # snake_case key
    cli: str  # CLI spelling
    before: Any  # current (rollback) value, None if absent
    after: Any  # proposed (launch) value, None if removed


def _reject_model_path(model_path: str) -> None:
    if not model_path or shell_metacharacters(model_path) or model_path.startswith("-"):
        raise AdvisorError(f"model_path rejected (fail-closed): {model_path!r}")
    if ".." in model_path or model_path.startswith("/"):
        raise AdvisorError(f"model_path rejected (absolute/traversal): {model_path!r}")


def _spec(profile: ProfileSpec, key: str) -> FlagSpec:
    spec = profile.command.flag_map.get(key)
    if spec is None:
        raise AdvisorError(f"override flag {key!r} is not allowlisted for {profile.family}")
    return spec


def _validate_override(profile: ProfileSpec, key: str, value: Any) -> None:
    spec = _spec(profile, key)
    v = validate_flag(spec, value)
    if v is not None:
        raise AdvisorError(f"override flag violation (fail-closed): {v.detail}")


def _canonical_order(profile: ProfileSpec) -> list[str]:
    return list(profile.command.flag_map)


def build_recipe(
    family: str,
    model_path: str | None,
    current_flags: dict[str, Any],
    overrides: dict[str, Any],
) -> Recipe:
    """Build the inert launch + rollback recipe for a supported family.

    ``current_flags`` must be the operator's live flags (already validated by
    the caller or the schema layer); ``overrides`` are the proposed one- or
    multi-variable changes. Both mappings are validated against the profile
    allowlist; any violation fails closed. ``model_path`` must be a clean
    opaque reference (no shell metacharacters, no absolute/traversal path).
    """
    profile = get_profile(family)
    _validate_override_all(profile, current_flags)
    _validate_override_all(profile, overrides)
    for key, value in sorted(overrides.items()):
        _validate_override(profile, key, value)
    if not model_path:
        raise AdvisorError("model_path is required to build a launch recipe")
    _reject_model_path(model_path)

    # Conflicts across the two mappings (e.g. current enable + override disable).
    merged = dict(current_flags)
    for key, value in overrides.items():
        if (
            key in current_flags
            and isinstance(value, bool)
            and isinstance(current_flags[key], bool)
            and value != current_flags[key]
            and family == "vllm"
            and {
                key,
                "disable_chunked_prefill"
                if key == "enable_chunked_prefill"
                else "enable_chunked_prefill",
            }
            & current_flags.keys()
            and current_flags.get(key) is True
        ):
            raise AdvisorError(
                f"override conflicts with current state: {key} (fail-closed)"
            )
        merged[key] = value
    conflicts = validate_mapping(profile.command.flag_map, merged)
    if conflicts:
        detail = "; ".join(f"{c.flag}: {c.detail}" for c in conflicts)
        raise AdvisorError(f"merged flag state violates allowlist (fail-closed): {detail}")

    base = list(profile.command.argv_base)
    base[profile.command.model_position] = model_path

    def _argv_for(flags: dict[str, Any]) -> tuple[str, ...]:
        argv = list(base)
        for key in _canonical_order(profile):
            if key in flags:
                spec = profile.command.flag_map[key]
                argv.append(spec.cli)
                argv.append(flag_value_to_cli(spec, flags[key]))
        return tuple(argv)

    launch_argv = list(_argv_for(merged))
    rollback_argv = list(_argv_for(dict(current_flags)))

    diff: list[RollbackDiff] = []
    for key in sorted(set(current_flags) | set(overrides)):
        before = current_flags.get(key)
        after = merged.get(key)
        if before != after:
            spec = profile.command.flag_map.get(key)
            if spec is None:
                raise AdvisorError(f"flag {key!r} missing from profile allowlist during diff")
            diff.append(RollbackDiff(flag=key, cli=spec.cli, before=before, after=after))

    return Recipe(
        family=family,
        launch_argv=launch_argv,
        rollback_argv=rollback_argv,
        flag_diff=tuple(diff),
        rendered_shell=shlex.join(launch_argv),
        rendered_rollback_shell=shlex.join(rollback_argv),
    )


def _validate_override_all(profile: ProfileSpec, flags: dict[str, Any]) -> None:
    violations = validate_mapping(profile.command.flag_map, flags)
    if violations:
        detail = "; ".join(f"{v.flag}: {v.detail}" for v in violations)
        raise AdvisorError(f"flag violations (fail-closed): {detail}")


# ---------------------------------------------------------------------------
# canonical artifact + digest
# ---------------------------------------------------------------------------

_VOLATILE_ARTIFACT_KEYS = ("created_at", "digest")


def artifact_to_dict(result: Any) -> dict[str, Any]:  # noqa: F821
    """Serialize an :class:`AdvisorResult` into a plain-JSON artifact dict.

    The digest is computed over the canonical JSON of the payload *without*
    the volatile ``created_at`` / ``digest`` fields (same contract as the
    verdict bundle digests), so the digest is deterministic for identical
    inputs and sensitive to any change in the payload.
    """
    from serving_verdict.advisor.advice import AdvisorResult, to_dict  # local: avoid cycle

    if not isinstance(result, AdvisorResult):
        raise AdvisorError("artifact_to_dict expects an AdvisorResult")
    payload = to_dict(result)
    for key in _VOLATILE_ARTIFACT_KEYS:
        payload.pop(key, None)
    from serving_verdict.canonical import canonicalize, digest_payload

    payload["digest"] = digest_payload(canonicalize(payload))
    return payload


def digest_of_artifact(artifact: dict[str, Any]) -> str:
    """Return the canonical digest of a serialized artifact payload.

    Raises :class:`AdvisorError` when the artifact is missing its recorded
    digest (call :func:`artifact_to_dict` to produce one first).
    """
    verify_artifact(artifact)
    return str(artifact["digest"])


def verify_artifact(artifact: dict[str, Any]) -> dict[str, Any]:
    """Re-compute the digest over the payload and verify it matches.

    Raises :class:`AdvisorError` on any violation (missing fields, bad
    digest, tampered payload).
    """
    from serving_verdict.advisor import ADVISOR_SCHEMA_VERSION
    from serving_verdict.canonical import canonicalize, digest_payload

    if not isinstance(artifact, dict):
        raise AdvisorError("artifact is not a JSON object")
    if artifact.get("schema_version") != ADVISOR_SCHEMA_VERSION:
        raise AdvisorError(f"unsupported artifact schema_version: {artifact.get('schema_version')!r}")
    for key in ("runtime_family", "status", "recommendations", "flag_state", "recipe"):
        if key not in artifact:
            raise AdvisorError(f"artifact missing required field: {key}")
    expected = artifact.get("digest")
    if not isinstance(expected, str) or not expected.startswith("sha256:"):
        raise AdvisorError("artifact digest is malformed")
    payload = {k: v for k, v in artifact.items() if k not in _VOLATILE_ARTIFACT_KEYS}
    try:
        actual = digest_payload(canonicalize(payload))
    except (ValueError, TypeError) as exc:
        raise AdvisorError(f"artifact payload not canonicalizable: {exc}") from exc
    if actual != expected:
        raise AdvisorError(f"artifact digest mismatch: recorded {expected}, recomputed {actual}")
    return {"valid": True, "digest": actual}
