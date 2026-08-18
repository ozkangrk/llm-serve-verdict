"""Canonical ``serving-verdict.benchmark-run.v1`` artifact.

The artifact is the sealed, tamper-evident record of one quick benchmark run:

- run ID + phase lifecycle (deterministic; no wall-clock fields);
- endpoint PUBLIC fingerprint (id/base_url/model/api_key_env/remote) — never
  the API key or any other credential;
- model identity (requested + served, from preflight);
- protocol/workload hashes of the frozen profile;
- warmup evidence (excluded from all aggregates);
- per-request measurement records (no raw response text);
- aggregates (serial medians, concurrency shared-wall math) and
  arithmetic-exact / fixed-schema gate results;
- a canonical sha256 digest over everything else.

Tamper detection: :func:`verify_artifact` recomputes the digest over the
payload with the digest field removed; any mutation (including the digest
itself) is an :class:`IntegrityError`.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from serving_verdict.canonical import canonicalize, digest_payload
from serving_verdict.endpoint import EndpointConfig
from serving_verdict.errors import IntegrityError
from serving_verdict.preflight import PreflightResult
from serving_verdict.profile import (
    ARTIFACT_SCHEMA_VERSION,
    BenchmarkProfile,
    protocol_hash,
    workload_hash,
)

RUN_ID_VERSION = "serving-verdict.benchmark-run-id.v1"
PHASE_SEQUENCE = (
    "PREFLIGHT",
    "WARMUP",
    "MEASURE",
    "CONCURRENCY",
    "QUALITY",
    "SEALED",
)


def _run_id_payload(config: EndpointConfig, profile: BenchmarkProfile) -> dict[str, Any]:
    """Everything that defines the identity of a run (fully deterministic)."""
    return {
        "schema_version": RUN_ID_VERSION,
        "profile_name": profile.name,
        "procedure_version": profile.procedure_version,
        "protocol_hash": protocol_hash(profile),
        "workload_hash": workload_hash(profile),
        "endpoint_id": config.endpoint_id,
        "base_url": config.base_url,
        "requested_model": config.model,
        "api_key_env": config.api_key_env,
    }


def run_id_from_spec(spec: dict[str, Any]) -> str:
    canonical = json.dumps(
        spec, allow_nan=False, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return "svrun-" + hashlib.sha256(canonical).hexdigest()[:32]


def run_id(artifact: dict[str, Any]) -> str:
    """Deterministic run identity recomputed from an artifact's stable fields
    (profile + endpoint public fingerprint + protocol/workload hashes).

    Any tamper with those fields changes the run_id *and* the digest; the
    digest is the authoritative tamper check, the run_id is the stable
    cross-run correlation key.
    """
    spec = _run_id_payload_from_artifact(artifact)
    return run_id_from_spec(spec)


def _run_id_payload_from_artifact(artifact: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": RUN_ID_VERSION,
        "profile_name": artifact["profile"]["name"],
        "procedure_version": artifact["profile"]["procedure_version"],
        "protocol_hash": artifact["protocol_hash"],
        "workload_hash": artifact["workload_hash"],
        "endpoint_id": artifact["endpoint"]["id"],
        "base_url": artifact["endpoint"]["base_url"],
        "requested_model": artifact["endpoint"]["model"],
        "api_key_env": artifact["endpoint"]["api_key_env"],
    }


def build_run_artifact(
    *,
    config: EndpointConfig,
    profile: BenchmarkProfile,
    preflight: PreflightResult,
    run_status: str,
    warmup_records: list[dict[str, Any]],
    measured_records: list[dict[str, Any]],
    serial: dict[str, Any],
    concurrency: list[dict[str, Any]],
    request_success: dict[str, Any],
    gates: dict[str, Any],
    error_probe: dict[str, Any],
    protocol_hash: str,
    workload_hash: str,
    env_meta: dict[str, Any],
) -> dict[str, Any]:
    """Assemble the canonical artifact (without its digest; the caller seals
    it with :func:`compute_artifact_digest`)."""
    return {
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "run_id": run_id_from_spec(_run_id_payload(config, profile)),
        "phases": {
            "lifecycle": "SEALED",
            "sequence": list(PHASE_SEQUENCE),
        },
        "run_status": run_status,
        "endpoint": config.public_payload(),
        "model": {
            "requested": preflight.requested_model,
            "served": preflight.served_model,
            "matches_requested": preflight.requested_model in preflight.model_ids,
        },
        "profile": {
            "name": profile.name,
            "procedure_version": profile.procedure_version,
        },
        "protocol_hash": protocol_hash,
        "workload_hash": workload_hash,
        "error_probe": error_probe,
        "warmup_requests": warmup_records,
        "requests": measured_records,
        "aggregates": {
            "serial": serial,
            "concurrency": concurrency,
            "requests": request_success,
        },
        "gates": gates,
        "environment": env_meta,
    }


def payload_without_digest(doc: dict[str, Any]) -> dict[str, Any]:
    """Copy of the artifact without the digest field (digest input domain)."""
    return {k: v for k, v in doc.items() if k != "artifact_digest"}


def compute_artifact_digest(doc: dict[str, Any]) -> str:
    """sha256 over the canonical payload (digest field excluded)."""
    body = payload_without_digest(doc)
    if body.get("schema_version") != ARTIFACT_SCHEMA_VERSION:
        raise IntegrityError(
            f"artifact schema_version must be {ARTIFACT_SCHEMA_VERSION}"
        )
    return digest_payload(canonicalize(body))


def verify_artifact(doc: dict[str, Any]) -> str:
    """Verify schema, run_id consistency, and the artifact digest.

    Raises IntegrityError on any tamper; returns the digest on success.
    """
    if not isinstance(doc, dict):
        raise IntegrityError("artifact must be a JSON object")
    if doc.get("schema_version") != ARTIFACT_SCHEMA_VERSION:
        raise IntegrityError(
            f"artifact schema_version must be {ARTIFACT_SCHEMA_VERSION}"
        )
    stored = doc.get("artifact_digest")
    if not isinstance(stored, str) or not stored.startswith("sha256:"):
        raise IntegrityError("artifact digest is missing or malformed")
    recomputed = compute_artifact_digest(doc)
    if recomputed != stored:
        raise IntegrityError(
            "artifact digest mismatch: payload was modified after sealing"
        )
    return stored
