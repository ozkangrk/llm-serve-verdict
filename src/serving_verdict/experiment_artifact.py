"""Canonical experiment artifacts with digests and provenance IDs (v0.4).

Every compare / sweep / pareto result is sealed into a canonical artifact:
  - ``provenance_id``: a stable ``prov:``-prefixed short hash of the *input*
    identity (schema version + the sealed inputs that produced the result +
    the exact configuration). It is deterministic across builds and does not
    depend on ``created_at``, so two artifacts built from the same inputs and
    config share a provenance ID even when produced at different times.
  - ``artifact_digest``: a sha256 over the canonical artifact payload
    (everything except ``created_at`` and ``artifact_digest`` itself).

Verification is fail-closed: a digest that does not recompute, a foreign
schema version, or a malformed required field raises
``ArtifactIntegrityError`` (exit 4).
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from serving_verdict.canonical import canonicalize, digest_payload
from serving_verdict.errors import ArtifactIntegrityError

_VOLATILE_KEYS: tuple[str, ...] = ("created_at", "artifact_digest")


def short_provenance(identity: Any) -> str:
    """Stable short ``prov:`` ID from the canonical identity payload."""
    raw = canonicalize(identity)
    return "prov:" + hashlib.sha256(raw).hexdigest()[:32]


def seal_artifact(schema_version: str, identity: Any, payload: dict[str, Any], created_at: str) -> dict[str, Any]:
    """Assemble a canonical artifact: provenance, created_at, sealed digest."""
    base: dict[str, Any] = {
        "schema_version": schema_version,
        "provenance_id": short_provenance(identity),
        **payload,
        "created_at": created_at,
    }
    base["artifact_digest"] = digest_payload(canonicalize(_payload_without_volatile(base)))
    return base


def _payload_without_volatile(artifact: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in artifact.items() if k not in _VOLATILE_KEYS}


def _require_fields(artifact: Any, schema_version: str, required: tuple[str, ...]) -> None:
    if not isinstance(artifact, dict):
        raise ArtifactIntegrityError("artifact is not a JSON object")
    if artifact.get("schema_version") != schema_version:
        raise ArtifactIntegrityError(
            f"unsupported artifact schema_version: {artifact.get('schema_version')!r} "
            f"(expected {schema_version})"
        )
    for key in required:
        if key not in artifact:
            raise ArtifactIntegrityError(f"artifact missing required field: {key}")
    digest = artifact.get("artifact_digest")
    if not isinstance(digest, str) or not digest.startswith("sha256:"):
        raise ArtifactIntegrityError("artifact_digest is malformed")


def verify_artifact(artifact: Any, schema_version: str, required: tuple[str, ...]) -> dict[str, Any]:
    """Fail-closed verification. Raises ``ArtifactIntegrityError`` on any drift."""
    _require_fields(artifact, schema_version, required)
    recomputed = digest_payload(canonicalize(_payload_without_volatile(artifact)))
    if recomputed != artifact["artifact_digest"]:
        raise ArtifactIntegrityError(
            f"artifact digest mismatch: recorded {artifact['artifact_digest']}, "
            f"recomputed {recomputed}"
        )
    return {"valid": True, "digest": recomputed, "provenance_id": artifact["provenance_id"]}


def roundtrip_json_bytes(artifact: dict[str, Any]) -> dict[str, Any]:
    """Serialize an artifact to canonical bytes and parse it back (strict)."""
    try:
        return json.loads(canonicalize(artifact).decode("utf-8"))
    except (ValueError, TypeError) as exc:
        raise ArtifactIntegrityError(f"artifact not canonicalizable: {exc}") from exc
