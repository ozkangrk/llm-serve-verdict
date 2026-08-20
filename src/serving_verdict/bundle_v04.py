"""Typed, tamper-evident verdict bundle schema v0.4 (signed bundle foundation).

Schema: ``serving-verdict.bundle.v0.4`` (PRD FR-8 / FR-2).

Design decisions
----------------
- **Digest coverage.** The canonical digest covers EXACTLY
  ``DIGEST_COVERED_FIELDS`` — every substantive section including
  ``issued_at`` and ``producer``. Unlike the v0.1 compatibility digest
  (which excludes ``created_at``), v0.4 binds the issuance timestamp so the
  signed payload commits to *when* the verdict was issued (FR-2.7). The
  ``digest`` and ``signature`` fields themselves are never covered:
  re-signing a bundle must not change its canonical digest.
- **Deep immutability.** ``parse_v04_bundle`` returns a ``ParsedV04Bundle``
  whose ``raw`` view is a recursive ``MappingProxyType``/tuple tree. Nested
  mutation is rejected; only the raw source document stays mutable, and the
  parsed object refuses to alias it.
- **Signing happens out-of-band.** The bundle carries an optional
  ``signature`` section (DSSE envelope, produced by the signing module).
  Parsing and digest verification never need a key; signature verification
  is a separate, distinguishable stage (see ``signing.py``).
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from serving_verdict.canonical import canonicalize
from serving_verdict.errors import IntegrityError

BUNDLE_SCHEMA_VERSION_V04 = "serving-verdict.bundle.v0.4"

#: The exact top-level key set. Anything else is a schema violation.
TOP_LEVEL_KEYS: frozenset[str] = frozenset(
    {
        "schema_version",
        "case",
        "baseline",
        "candidate",
        "evidence_manifest",
        "comparisons",
        "statistics",
        "gates",
        "trust",
        "verdict",
        "reason_codes",
        "claim_boundary",
        "issued_at",
        "producer",
        "digest",
        "signature",
    }
)

#: The exact fields covered by the canonical digest (documented contract).
#: ``digest`` and ``signature`` are deliberately excluded.
DIGEST_COVERED_FIELDS: frozenset[str] = frozenset(
    {
        "schema_version",
        "case",
        "baseline",
        "candidate",
        "evidence_manifest",
        "comparisons",
        "statistics",
        "gates",
        "trust",
        "verdict",
        "reason_codes",
        "claim_boundary",
        "issued_at",
        "producer",
    }
)

_VERDICTS: frozenset[str] = frozenset({"PROMOTE", "REJECT", "INCONCLUSIVE"})

_MANIFEST_REQUIRED_KEYS: frozenset[str] = frozenset(
    {
        "artifact_id",
        "sha256",
        "artifact_schema",
        "size_bytes",
        "producer",
        "produced_at",
        "tool",
        "tool_version",
        "source_type",
        "model",
        "runtime",
        "hardware",
        "procedure",
    }
)

_SHA256_LEN = 64


# ---------------------------------------------------------------------------
# deep immutability
# ---------------------------------------------------------------------------


def _deep_freeze(value: Any) -> Any:
    """Recursively convert a JSON value tree into an immutable view tree.

    dicts -> MappingProxyType, lists/tuples -> tuple. Non-JSON values are
    rejected at parse time, so this never sees odd types.
    """
    if isinstance(value, dict):
        return MappingProxyType({str(k): _deep_freeze(v) for k, v in value.items()})
    if isinstance(value, list):
        return tuple(_deep_freeze(v) for v in value)
    return value


def freeze(doc: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return a deep-immutable view of a bundle document (alias-safe).

    The returned mapping shares *value* structure with the input but
    rejects ALL mutation, including ``dict.__setitem__``-style base-class
    calls, because the view is a ``MappingProxyType``.
    """
    if not isinstance(doc, Mapping):
        raise TypeError("freeze() requires a mapping")
    return _deep_freeze(doc)


# ---------------------------------------------------------------------------
# strict value checks
# ---------------------------------------------------------------------------


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and len(value) == _SHA256_LEN and all(
        c in "0123456789abcdef" for c in value.lower()
    )


def _is_finite_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _require_str(section: str, doc: Mapping[str, Any], key: str) -> str:
    v = doc.get(key)
    if not isinstance(v, str) or not v:
        raise IntegrityError(f"{section}.{key} must be a non-empty string")
    return v


def _require_nonnegative_int(section: str, doc: Mapping[str, Any], key: str) -> int:
    v = doc.get(key)
    if isinstance(v, bool) or not isinstance(v, int) or v < 0:
        raise IntegrityError(f"{section}.{key} must be a non-negative integer")
    return v


def _validate_manifest_entry(index: int, entry: Any) -> None:
    if not isinstance(entry, Mapping):
        raise IntegrityError(f"evidence_manifest[{index}] must be an object")
    missing = _MANIFEST_REQUIRED_KEYS - entry.keys()
    if missing:
        raise IntegrityError(
            f"evidence_manifest[{index}] missing required keys: {sorted(missing)}"
        )
    label = f"evidence_manifest[{index}]"
    if not isinstance(entry["artifact_id"], str) or not entry["artifact_id"]:
        raise IntegrityError(f"{label}.artifact_id must be a non-empty string")
    if not _is_sha256(entry["sha256"]):
        raise IntegrityError(f"{label}.sha256 must be a 64-hex SHA-256 string")
    for key in ("artifact_schema", "producer", "produced_at", "tool", "tool_version", "source_type"):
        _require_str(label, entry, key)
    _require_nonnegative_int(label, entry, "size_bytes")
    for key in ("model", "runtime", "hardware", "procedure"):
        if not isinstance(entry[key], Mapping) or not entry[key]:
            raise IntegrityError(f"{label}.{key} must be a non-empty object")


def _validate_bundle(doc: Mapping[str, Any]) -> None:
    """Fail-closed structural validation of a v0.4 bundle document.

    Raises IntegrityError with a field-specific message.
    """
    if not isinstance(doc, Mapping):
        raise IntegrityError("bundle is not a JSON object")
    if doc.get("schema_version") != BUNDLE_SCHEMA_VERSION_V04:
        raise IntegrityError(
            f"unsupported bundle schema_version: {doc.get('schema_version')!r} "
            f"(expected {BUNDLE_SCHEMA_VERSION_V04!r})"
        )
    missing = TOP_LEVEL_KEYS - doc.keys()
    if missing:
        raise IntegrityError(f"bundle missing required field(s): {sorted(missing)}")
    extra = doc.keys() - TOP_LEVEL_KEYS
    if extra:
        raise IntegrityError(f"bundle has unknown field(s): {sorted(extra)}")

    # case
    if not isinstance(doc["case"], Mapping):
        raise IntegrityError("case must be an object")
    _require_str("case", doc["case"], "case_id")

    # baseline / candidate
    for side in ("baseline", "candidate"):
        ref = doc[side]
        if not isinstance(ref, Mapping):
            raise IntegrityError(f"{side} must be an object")
        _require_str(side, ref, "artifact_id")
        if not _is_sha256(ref.get("artifact_sha256")):
            raise IntegrityError(f"{side}.artifact_sha256 must be a 64-hex SHA-256 string")

    # evidence manifest
    manifest = doc["evidence_manifest"]
    if not isinstance(manifest, list):
        raise IntegrityError("evidence_manifest must be a list")
    seen: set[str] = set()
    for i, entry in enumerate(manifest):
        _validate_manifest_entry(i, entry)
        aid = entry["artifact_id"]
        if aid in seen:
            raise IntegrityError(f"evidence_manifest has duplicate artifact_id: {aid!r}")
        seen.add(aid)

    # comparisons / statistics / gates / trust
    if not isinstance(doc["comparisons"], list):
        raise IntegrityError("comparisons must be a list")
    if not isinstance(doc["statistics"], Mapping):
        raise IntegrityError("statistics must be an object")
    if not isinstance(doc["gates"], list):
        raise IntegrityError("gates must be a list")
    for i, gate in enumerate(doc["gates"]):
        if not isinstance(gate, Mapping) or not isinstance(gate.get("id"), str):
            raise IntegrityError(f"gates[{i}] must be an object with a string id")
        if gate.get("status") not in ("pass", "fail", "missing"):
            raise IntegrityError(f"gates[{i}].status must be pass|fail|missing")
    if not isinstance(doc["trust"], Mapping):
        raise IntegrityError("trust must be an object")

    # verdict / reasons
    if doc["verdict"] not in _VERDICTS:
        raise IntegrityError(f"unknown verdict: {doc['verdict']!r}")
    reasons = doc["reason_codes"]
    if not isinstance(reasons, list) or not all(isinstance(r, str) for r in reasons):
        raise IntegrityError("reason_codes must be a list of strings")

    # claim boundary (structured)
    cb = doc["claim_boundary"]
    if not isinstance(cb, Mapping):
        raise IntegrityError("claim_boundary must be an object")
    if not isinstance(cb.get("workload_set"), list):
        raise IntegrityError("claim_boundary.workload_set must be a list")

    # issued_at / producer
    _require_str("issued_at", doc, "issued_at")
    producer = doc["producer"]
    if not isinstance(producer, Mapping):
        raise IntegrityError("producer must be an object")
    _require_str("producer", producer, "identity")
    _require_str("producer", producer, "tool")
    _require_str("producer", producer, "tool_version")

    # digest
    digest = doc["digest"]
    if not isinstance(digest, str) or not digest.startswith("sha256:") or len(digest) != 71:
        raise IntegrityError("digest is malformed")
    try:
        int(digest[7:], 16)
    except ValueError as exc:
        raise IntegrityError("digest is malformed") from exc

    # signature: optional; must be null or a well-formed DSSE section
    _validate_signature_section(doc["signature"])


def _validate_signature_section(sig: Any) -> None:
    """Structural (key-free) validation of the optional signature section.

    Cryptographic verification is a separate stage (signing module); this
    only proves the section is a well-formed DSSE envelope container so that
    a present-but-garbage signature can never be silently accepted.
    """
    if sig is None:
        return
    if not isinstance(sig, Mapping):
        raise IntegrityError("signature must be null or an object")
    required = {"backend", "signer", "key_id", "envelope"}
    missing = required - sig.keys()
    if missing:
        raise IntegrityError(f"signature section missing keys: {sorted(missing)}")
    if sig["backend"] != "dsse_ed25519":
        raise IntegrityError(
            f"unsupported signature backend: {sig['backend']!r} (only dsse_ed25519)"
        )
    for key in ("signer", "key_id"):
        _require_str(f"signature.{key}", sig, key)
    envelope = sig["envelope"]
    if not isinstance(envelope, str) or not envelope:
        raise IntegrityError("signature.envelope must be a non-empty base64 string")
    try:
        blob = base64.b64decode(envelope.encode("ascii"), validate=True)
        env = json.loads(blob)
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise IntegrityError(f"signature.envelope is not a valid base64/JSON DSSE blob: {exc}") from exc
    if not isinstance(env, dict) or not isinstance(env.get("payload"), str):
        raise IntegrityError("DSSE envelope must carry a base64 'payload'")
    try:
        base64.b64decode(env["payload"].encode("ascii"), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise IntegrityError("DSSE envelope payload is not valid base64") from exc
    if not isinstance(env.get("signatures"), list) or not env["signatures"]:
        raise IntegrityError("DSSE envelope must carry a non-empty 'signatures' list")
    for i, s in enumerate(env["signatures"]):
        if not isinstance(s, dict):
            raise IntegrityError(f"DSSE signature[{i}] must be an object")
        for key in ("keyid", "sig"):
            if not isinstance(s.get(key), str) or not s.get(key):
                raise IntegrityError(f"DSSE signature[{i}].{key} must be a non-empty string")
        try:
            base64.b64decode(s["sig"].encode("ascii"), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise IntegrityError(f"DSSE signature[{i}].sig is not valid base64") from exc


# ---------------------------------------------------------------------------
# typed parsed object
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParsedV04Bundle:
    """A parsed, deep-immutable v0.4 verdict bundle."""

    schema_version: str
    case: Mapping[str, Any]
    baseline: Mapping[str, Any]
    candidate: Mapping[str, Any]
    evidence_manifest: tuple[Mapping[str, Any], ...]
    comparisons: tuple[Mapping[str, Any], ...]
    statistics: Mapping[str, Any]
    gates: tuple[Mapping[str, Any], ...]
    trust: Mapping[str, Any]
    verdict: str
    reason_codes: tuple[str, ...]
    claim_boundary: Mapping[str, Any]
    issued_at: str
    producer: Mapping[str, Any]
    digest: str
    signature: Mapping[str, Any] | None
    raw: Mapping[str, Any]

    @property
    def manifest_artifact_ids(self) -> tuple[str, ...]:
        return tuple(e["artifact_id"] for e in self.evidence_manifest)


def _digest_projection(doc: Mapping[str, Any]) -> dict[str, Any]:
    return {k: doc[k] for k in DIGEST_COVERED_FIELDS}


def compute_v04_digest(doc: Mapping[str, Any]) -> str:
    """Canonical digest over exactly ``DIGEST_COVERED_FIELDS``.

    UTF-8, ensure_ascii, sorted keys, compact separators, list order
    preserved (canonicalize contract). Raises IntegrityError when a covered
    field is absent or not canonicalizable (non-finite values, etc.).
    """
    projection = _digest_projection(doc)
    try:
        payload = canonicalize(projection)
    except (ValueError, TypeError) as exc:
        raise IntegrityError(f"bundle payload not canonicalizable: {exc}") from exc
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def parse_v04_bundle(doc: Any) -> ParsedV04Bundle:
    """Parse a v0.4 bundle document into a deep-immutable typed object.

    Fail-closed: structural violations and digest mismatch raise
    ``IntegrityError``. Returns the parsed bundle only when the document is
    a complete, well-formed, digest-valid v0.4 bundle.
    """
    _validate_bundle(doc)
    expected = doc["digest"]
    actual = compute_v04_digest(doc)
    if actual != expected:
        raise IntegrityError(f"bundle digest mismatch: recorded {expected}, recomputed {actual}")
    raw = freeze(doc)
    sig = raw["signature"]
    return ParsedV04Bundle(
        schema_version=raw["schema_version"],
        case=raw["case"],
        baseline=raw["baseline"],
        candidate=raw["candidate"],
        evidence_manifest=tuple(raw["evidence_manifest"]),
        comparisons=tuple(raw["comparisons"]),
        statistics=raw["statistics"],
        gates=tuple(raw["gates"]),
        trust=raw["trust"],
        verdict=raw["verdict"],
        reason_codes=tuple(raw["reason_codes"]),
        claim_boundary=raw["claim_boundary"],
        issued_at=raw["issued_at"],
        producer=raw["producer"],
        digest=raw["digest"],
        signature=None if sig is None else MappingProxyType(dict(sig)),
        raw=raw,
    )


def verify_v04_bundle(doc: Any) -> dict[str, Any]:
    """Offline verification of a v0.4 bundle (digest layer, no keys needed).

    Returns a status dict on success::

        {"valid": True, "digest_valid": True, "digest": ...,
         "signature_present": bool, ...}

    Raises IntegrityError on structural violations or digest mismatch.
    Signature/trust status fields are informational here; the full
    signature-aware pipeline lives in ``signing.verify_signed_bundle``.
    """
    _validate_bundle(doc)
    actual = compute_v04_digest(doc)
    expected = doc["digest"]
    if actual != expected:
        raise IntegrityError(
            f"bundle digest mismatch: recorded {expected}, recomputed {actual}",
            code="DIGEST_INVALID",
        )
    sig = doc["signature"]
    present = sig is not None and isinstance(sig, Mapping) and bool(sig)
    return {
        "valid": True,
        "digest_valid": True,
        "digest": actual,
        "signature_present": present,
        "signature_valid": False,  # digest layer alone can never validate a signature
        "signer_trusted": False,
        "evidence_signatures_valid": False,
    }
