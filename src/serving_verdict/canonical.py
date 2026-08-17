"""Canonical JSON serialization and tamper-evident bundle digests.

Canonical form (MVP spec): UTF-8, ensure_ascii=True, sorted keys, compact
separators (',', ':'), no NaN/Infinity. Lists preserve order.
"""
from __future__ import annotations

import hashlib
import json
import math
from typing import Any

from serving_verdict.errors import CanonicalizationError

VOLATILE_FIELDS: frozenset[str] = frozenset({"created_at", "bundle_digest"})


def _reject_non_finite(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        raise CanonicalizationError(f"non-finite value in JSON payload: {value!r}")
    return value


def canonical_json_bytes(raw: bytes) -> bytes:
    """Parse raw JSON and re-serialize in canonical form.

    Raises CanonicalizationError on invalid JSON or non-finite floats.
    """
    try:
        doc = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise CanonicalizationError(f"invalid JSON: {exc}") from exc
    try:
        return canonicalize(doc)
    except (ValueError, TypeError) as exc:
        raise CanonicalizationError(f"non-finite or invalid JSON payload: {exc}") from exc


def canonicalize(doc: Any) -> bytes:
    """Serialize a Python value (from strict JSON parsing) canonically."""
    return (
        json.dumps(doc, allow_nan=False, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    ).encode("utf-8")


def digest_payload(canonical_bytes: bytes) -> str:
    """sha256 over canonical bytes, prefixed 'sha256:'."""
    return "sha256:" + hashlib.sha256(canonical_bytes).hexdigest()


def payload_without_volatile(doc: dict[str, Any]) -> dict[str, Any]:
    """Copy of the bundle payload without volatile fields (created_at, digest)."""
    return {k: v for k, v in doc.items() if k not in VOLATILE_FIELDS}


def compute_bundle_digest(doc: dict[str, Any]) -> str:
    """Canonical digest over the payload excluding created_at and bundle_digest."""
    try:
        return digest_payload(canonicalize(payload_without_volatile(doc)))
    except (ValueError, TypeError) as exc:
        raise CanonicalizationError(f"payload not canonicalizable: {exc}") from exc


def compute_bundle_digest_from_bytes(raw: bytes) -> str:
    """Canonical digest from raw JSON bytes (parses strictly first)."""
    return compute_bundle_digest(json.loads(canonical_json_bytes(raw)))
