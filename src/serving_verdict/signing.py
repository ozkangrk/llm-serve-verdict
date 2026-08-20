"""Offline DSSE + Ed25519 signing and verification for v0.4 bundles (FR-2).

Signing backend: **offline DSSE with Ed25519**. This is the first,
explicitly-labeled backend (PRD FR-2.1): no Sigstore/cosign transparency
log, no network access, no shell subprocesses. The bundle schema keeps the
``signature.backend`` field so future backends can be added without a
schema break.

Security invariants
-------------------
- The private key is **environment-only** at the CLI layer; this module
  never writes key material to disk, artifacts, logs, or error messages.
  Every exception message is built from stable identifiers only.
- Verification is **offline and fail-closed**, and distinguishes failure
  classes via ``IntegrityError.code``:

  * ``DIGEST_INVALID``              – canonical digest does not recompute
    * ``SIGNATURE_MISSING``         – required signature absent
    * ``SIGNATURE_INVALID``         – cryptographically bad / unbound envelope
    * ``UNTRUSTED_SIGNER``          – key or identity outside the trust store
    * ``EVIDENCE_SIGNATURES_INVALID`` – policy requires signed evidence that
      the bundle cannot demonstrate

- Trust evaluation is a two-layer check:
  1. *Cryptographic*: the signature must verify against the **trust
     store's** public key for the claimed ``key_id``. A key_id absent from
     the store can never be verified offline -> ``UNTRUSTED_SIGNER``
     (fail-closed; we never trust a key we cannot check).
  2. *Identity*: the signer identity must be in the store's
     ``allowed_signers`` allowlist.

DSSE envelope layout (RFC 9448 style, simplified to Ed25519):

    envelope = base64(JSON({
        "payloadType": "serving-verdict/bundle/v0.4",
        "payload":     base64(canonical_bytes),
        "signatures":  [{"keyid": ..., "sig": base64(64-byte-sig),
                         "algorithm": "ed25519"}],
    }))

``canonical_bytes`` is the canonical JSON (sorted keys, compact separators,
ensure_ascii, UTF-8) of the bundle document **excluding only the
``signature`` section** — i.e. the signature binds the digest and every
digest-covered field, including ``issued_at``.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from serving_verdict import bundle_v04
from serving_verdict.bundle_v04 import compute_v04_digest
from serving_verdict.canonical import canonicalize
from serving_verdict.errors import IntegrityError, UsageError

PAYLOAD_TYPE_V04_BUNDLE = "serving-verdict/bundle/v0.4"
TRUST_STORE_SCHEMA_VERSION = "serving-verdict.trust-store.v0.1"
SIGNATURE_BACKEND = "dsse_ed25519"
ALGORITHM = "ed25519"

#: PKCS8 DER prefix for an Ed25519 private key (RFC 8410) wrapping a 32-byte seed.
_PKCS8_ED25519_PREFIX = bytes.fromhex("302e020100300506032b657004220420")


# ---------------------------------------------------------------------------
# key handling
# ---------------------------------------------------------------------------


def load_ed25519_private_key_from_seed_hex(seed_hex: str) -> Ed25519PrivateKey:
    """Recover an Ed25519 private key from a 32-byte seed given as 64 hex chars.

    Raises UsageError on malformed input. Error messages are structural
    (length / charset) and never echo the seed.
    """
    if not isinstance(seed_hex, str) or len(seed_hex) != 64:
        raise UsageError("Ed25519 seed must be exactly 64 hex characters (32 bytes)")
    try:
        seed = bytes.fromhex(seed_hex)
    except ValueError as exc:
        raise UsageError("Ed25519 seed is not valid hexadecimal") from exc
    return _private_key_from_seed(seed)


def _private_key_from_seed(seed: bytes) -> Ed25519PrivateKey:
    key = serialization.load_der_private_key(_PKCS8_ED25519_PREFIX + seed, password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise UsageError("derived key is not Ed25519")
    return key


def public_key_bytes_of(key: Ed25519PrivateKey | Ed25519PublicKey) -> bytes:
    pub = key.public_key() if isinstance(key, Ed25519PrivateKey) else key
    return pub.public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )


def compute_signed_digest(doc: Any) -> str:
    """Alias for the v0.4 canonical digest (documented in bundle_v04)."""
    return compute_v04_digest(doc)


# ---------------------------------------------------------------------------
# signer identity
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SignerIdentity:
    """Typed signing identity; private key is memory-only and never serialized."""

    private_key: Ed25519PrivateKey
    key_id: str
    signer: str


# ---------------------------------------------------------------------------
# DSSE signing
# ---------------------------------------------------------------------------


def _payload_bytes(doc: dict[str, Any]) -> bytes:
    """Canonical bytes of the bundle excluding only the signature section."""
    return canonicalize({k: v for k, v in doc.items() if k != "signature"})


def sign_bundle(doc: dict[str, Any], *, identity: SignerIdentity) -> dict[str, Any]:
    """Return a NEW dict: ``doc`` + a DSSE Ed25519 ``signature`` section.

    The input is never mutated. Fails closed (IntegrityError) when the
    bundle fails structural validation or its digest does not recompute —
    a broken bundle must never be laundered by signing it.

    Raises IntegrityError (no key material in messages) on failure.
    """
    bundle_v04._validate_bundle(doc)  # structural, incl. signature section
    expected = doc["digest"]
    actual = compute_v04_digest(doc)
    if actual != expected:
        raise IntegrityError(
            f"refusing to sign: bundle digest mismatch (recorded {expected})",
            code="DIGEST_INVALID",
        )
    if doc["signature"] is not None:
        raise IntegrityError("refusing to sign: bundle already carries a signature")

    payload = _payload_bytes(doc)
    signature = identity.private_key.sign(payload)
    envelope = {
        "payloadType": PAYLOAD_TYPE_V04_BUNDLE,
        "payload": base64.b64encode(payload).decode("ascii"),
        "signatures": [
            {
                "keyid": identity.key_id,
                "sig": base64.b64encode(signature).decode("ascii"),
                "algorithm": ALGORITHM,
            }
        ],
    }
    envelope_b64 = base64.b64encode(
        json.dumps(envelope, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    ).decode("ascii")

    signed = dict(doc)
    signed["signature"] = {
        "backend": SIGNATURE_BACKEND,
        "signer": identity.signer,
        "key_id": identity.key_id,
        "payload_type": PAYLOAD_TYPE_V04_BUNDLE,
        "envelope": envelope_b64,
    }
    return signed


# ---------------------------------------------------------------------------
# trust store
# ---------------------------------------------------------------------------

_TRUST_STORE_KEYS: frozenset[str] = frozenset(
    {"schema_version", "require_signed_evidence", "require_signed_verdict", "allowed_signers", "allowed_keys"}
)


@dataclass(frozen=True)
class TrustStore:
    """A validated local trust store (strict JSON bounds, no path traversal)."""

    schema_version: str
    require_signed_evidence: bool
    require_signed_verdict: bool
    allowed_signers: tuple[str, ...]
    trusted_key_ids: frozenset[str]
    _public_keys: Mapping[str, Ed25519PublicKey]  # noqa: SLF001

    def __post_init__(self) -> None:
        object.__setattr__(self, "allowed_signers", tuple(self.allowed_signers))
        object.__setattr__(self, "trusted_key_ids", frozenset(self.trusted_key_ids))
        frozen_keys = MappingProxyType(dict(self._public_keys))
        if frozenset(frozen_keys) != self.trusted_key_ids:
            raise UsageError("trust store key index is inconsistent")
        object.__setattr__(self, "_public_keys", frozen_keys)


def load_trust_store(source: str | Path | dict[str, Any]) -> TrustStore:
    """Load and strictly validate a trust store.

    Accepts a JSON file path (string/Path) or an already-parsed JSON dict.
    Fail-closed: any schema drift, type violation, malformed base64 public
    key, wrong key length, or duplicate key_id raises UsageError. Messages
    are structural only (field names, counts) — never file contents.
    """
    doc: Any
    if isinstance(source, (str, Path)):
        p = Path(source)
        if not p.is_file():
            raise UsageError(f"trust store file not found: {p}")
        if p.stat().st_size > 1024 * 1024:
            raise UsageError("trust store file exceeds the 1 MiB bound")
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise UsageError(f"trust store is not valid JSON: structural error at {exc.__class__.__name__}") from exc
    else:
        doc = source

    if not isinstance(doc, dict):
        raise UsageError("trust store must be a JSON object")
    if doc.get("schema_version") != TRUST_STORE_SCHEMA_VERSION:
        raise UsageError(
            f"unsupported trust store schema_version: {doc.get('schema_version')!r}"
        )
    missing = _TRUST_STORE_KEYS - doc.keys()
    if missing:
        raise UsageError(f"trust store missing required key(s): {sorted(missing)}")
    extra = doc.keys() - _TRUST_STORE_KEYS
    if extra:
        raise UsageError(f"trust store has unknown key(s): {sorted(extra)}")

    req_evidence = doc["require_signed_evidence"]
    req_verdict = doc["require_signed_verdict"]
    if not isinstance(req_evidence, bool) or not isinstance(req_verdict, bool):
        raise UsageError("require_signed_evidence/require_signed_verdict must be booleans")

    signers = doc["allowed_signers"]
    if not isinstance(signers, list) or not all(
        isinstance(s, str) and s for s in signers
    ):
        raise UsageError("allowed_signers must be a list of non-empty strings")

    keys = doc["allowed_keys"]
    if not isinstance(keys, list):
        raise UsageError("allowed_keys must be a list")
    public_keys: dict[str, Ed25519PublicKey] = {}
    for i, entry in enumerate(keys):
        if not isinstance(entry, dict):
            raise UsageError(f"allowed_keys[{i}] must be an object")
        if set(entry.keys()) != {"key_id", "ed25519_public_key"}:
            raise UsageError(f"allowed_keys[{i}] must have exactly key_id + ed25519_public_key")
        key_id = entry["key_id"]
        if not isinstance(key_id, str) or not key_id:
            raise UsageError(f"allowed_keys[{i}].key_id must be a non-empty string")
        if key_id in public_keys:
            raise UsageError(f"allowed_keys has duplicate key_id: {key_id!r}")
        b64 = entry["ed25519_public_key"]
        if not isinstance(b64, str) or not b64:
            raise UsageError(f"allowed_keys[{i}].ed25519_public_key must be base64 text")
        try:
            raw = base64.b64decode(b64.encode("ascii"), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise UsageError(f"allowed_keys[{i}].ed25519_public_key is not valid base64") from exc
        if len(raw) != 32:
            raise UsageError(f"allowed_keys[{i}] public key must be 32 raw Ed25519 bytes")
        public_keys[key_id] = Ed25519PublicKey.from_public_bytes(raw)

    return TrustStore(
        schema_version=TRUST_STORE_SCHEMA_VERSION,
        require_signed_evidence=req_evidence,
        require_signed_verdict=req_verdict,
        allowed_signers=tuple(signers),
        trusted_key_ids=frozenset(public_keys),
        _public_keys=public_keys,
    )


def public_key_for(store: TrustStore, key_id: str) -> Ed25519PublicKey:
    """The trusted public key for a key_id (LookupError when unknown)."""
    return store._public_keys[key_id]  # noqa: SLF001


# ---------------------------------------------------------------------------
# verification pipeline
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VerifyReport:
    status: str
    digest: str
    digest_valid: bool
    signature_present: bool
    signature_valid: bool
    signer_trusted: bool
    signer: str | None
    key_id: str | None
    offline: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "digest": self.digest,
            "digest_valid": self.digest_valid,
            "signature_present": self.signature_present,
            "signature_valid": self.signature_valid,
            "signer_trusted": self.signer_trusted,
            "signer": self.signer,
            "key_id": self.key_id,
            "offline": self.offline,
        }


def verify_signed_bundle(
    doc: Any,
    *,
    store: TrustStore | None = None,
    require_signed: bool | None = None,
) -> dict[str, Any]:
    """Offline verification of a v0.4 bundle with distinguishable failures.

    Stage order (fail-closed, first failure wins):
      1. structural validation (schema, sections, DSSE section shape)
      2. canonical digest recompute            -> DIGEST_INVALID
      3. required-signature presence          -> SIGNATURE_MISSING
      4. envelope binding (payloadType, payload bytes, keyid, algorithm)
         5. key trust: key_id must be in the store  -> UNTRUSTED_SIGNER
         6. Ed25519 verification                 -> SIGNATURE_INVALID
      7. signer identity allowlist             -> UNTRUSTED_SIGNER
      8. evidence-signature policy             -> EVIDENCE_SIGNATURES_INVALID

    Returns a flat status dict (dict-form of VerifyReport) on success.
    Raises IntegrityError (exit 4) on any failure; the message carries a
    stable code and never key material.
    """
    bundle_v04._validate_bundle(doc)  # 1: structural (bad DSSE shape fails here)

    # 2: digest
    actual = compute_v04_digest(doc)
    expected = doc["digest"]
    if actual != expected:
        raise IntegrityError(
            f"bundle digest mismatch: recorded {expected}, recomputed {actual}",
            code="DIGEST_INVALID",
        )

    sig = doc["signature"]
    has_sig = sig is not None and isinstance(sig, dict) and bool(sig)

    # 3: required presence
    required = require_signed if require_signed is not None else (
        bool(store and store.require_signed_verdict)
    )
    if not has_sig and required:
        if store is not None and store.require_signed_verdict:
            # Store policy requires a signed verdict: an unsigned bundle is
            # equivalent to "untrusted signer" (no signature to trust).
            raise IntegrityError(
                "unsigned bundle: trust store requires a signed verdict "
                "(no signature present to trust)",
                code="UNTRUSTED_SIGNER",
            )
        raise IntegrityError(
            "bundle signature is missing but a signed verdict is required",
            code="SIGNATURE_MISSING",
        )

    if not has_sig:
        # unsigned bundle, not required: offline success (digest-only trust).
        return VerifyReport(
            status="ok",
            digest=actual,
            digest_valid=True,
            signature_present=False,
            signature_valid=False,
            signer_trusted=False,
            signer=None,
            key_id=None,
        ).to_dict()

    # 4: envelope binding
    if not isinstance(sig, dict):
        raise IntegrityError("signature section is malformed", code="SIGNATURE_INVALID")
    envelope = _decode_envelope(sig["envelope"])
    if sig.get("backend") != SIGNATURE_BACKEND:
        raise IntegrityError(
            f"unsupported signature backend: {sig.get('backend')!r}",
            code="SIGNATURE_INVALID",
        )
    key_id = sig.get("key_id")
    if not isinstance(key_id, str) or not key_id:
        raise IntegrityError("signature.key_id is missing or malformed", code="SIGNATURE_INVALID")
    if envelope.get("payloadType") != PAYLOAD_TYPE_V04_BUNDLE:
        raise IntegrityError(
            "DSSE payloadType does not match the v0.4 bundle payload type",
            code="SIGNATURE_INVALID",
        )
    try:
        envelope_payload = base64.b64decode(envelope["payload"], validate=True)
    except (binascii.Error, ValueError) as exc:
        raise IntegrityError(
            "DSSE envelope payload is not valid base64", code="SIGNATURE_INVALID"
        ) from exc
    if envelope_payload != _payload_bytes(doc):
        raise IntegrityError(
            "DSSE envelope payload does not match the canonical bundle bytes",
            code="SIGNATURE_INVALID",
        )
    signatures = envelope.get("signatures")
    if not isinstance(signatures, list) or len(signatures) != 1:
        raise IntegrityError("DSSE envelope must carry exactly one signature", code="SIGNATURE_INVALID")
    s = signatures[0]
    if not isinstance(s, dict) or s.get("keyid") != key_id:
        raise IntegrityError(
            "DSSE signature keyid does not match the signature section key_id",
            code="SIGNATURE_INVALID",
        )
    if s.get("algorithm") != ALGORITHM:
        raise IntegrityError(
            f"unsupported DSSE signature algorithm: {s.get('algorithm')!r} (only {ALGORITHM!r})",
            code="SIGNATURE_INVALID",
        )
    try:
        sig_bytes = base64.b64decode(s["sig"], validate=True)
    except (binascii.Error, ValueError) as exc:
        raise IntegrityError(
            "DSSE signature bytes are not valid base64", code="SIGNATURE_INVALID"
        ) from exc
    if len(sig_bytes) != 64:
        raise IntegrityError("Ed25519 signature must be 64 bytes", code="SIGNATURE_INVALID")

    # 5: key trust — offline we can only verify against the local store.
    if store is None or key_id not in store.trusted_key_ids:
        raise IntegrityError(
            "signing key is not present in the local trust store; cannot verify offline",
            code="UNTRUSTED_SIGNER",
        )
    public_key = public_key_for(store, key_id)

    # 6: cryptographic verification
    try:
        public_key.verify(sig_bytes, envelope_payload)
    except InvalidSignature as exc:
        raise IntegrityError(
            "Ed25519 signature does not verify against the trusted public key",
            code="SIGNATURE_INVALID",
        ) from exc

    # 7: signer identity allowlist
    signer = sig.get("signer")
    if not isinstance(signer, str) or not signer:
        raise IntegrityError("signature.signer is missing or malformed", code="SIGNATURE_INVALID")
    if store.allowed_signers and signer not in store.allowed_signers:
        raise IntegrityError(
            f"signer identity {signer!r} is not in the trust store allowlist",
            code="UNTRUSTED_SIGNER",
        )

    # 8: evidence-signature policy. The bundle signature covers the evidence
    # manifest (digest-covered), but per-artifact *evidence* signatures
    # (attestations) are not carried by the v0.4 bundle yet; requiring them
    # must fail closed until a backend provides them.
    if store.require_signed_evidence:
        raise IntegrityError(
            "trust store requires signed evidence; this bundle carries no per-artifact "
            "evidence signatures (future signing backend)",
            code="EVIDENCE_SIGNATURES_INVALID",
        )

    return VerifyReport(
        status="ok",
        digest=actual,
        digest_valid=True,
        signature_present=True,
        signature_valid=True,
        signer_trusted=True,
        signer=signer,
        key_id=key_id,
    ).to_dict()


def _decode_envelope(envelope_b64: Any) -> dict[str, Any]:
    if not isinstance(envelope_b64, str) or not envelope_b64:
        raise IntegrityError("signature.envelope is missing or malformed", code="SIGNATURE_INVALID")
    try:
        blob = base64.b64decode(envelope_b64.encode("ascii"), validate=True)
        env = json.loads(blob)
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise IntegrityError(
            "signature.envelope is not valid base64/JSON", code="SIGNATURE_INVALID"
        ) from exc
    if not isinstance(env, dict):
        raise IntegrityError("DSSE envelope must be a JSON object", code="SIGNATURE_INVALID")
    return env


def key_id_for_public_key(public_bytes: bytes) -> str:
    """Deterministic key_id derived from a raw 32-byte Ed25519 public key."""
    if len(public_bytes) != 32:
        raise UsageError("public key must be 32 raw Ed25519 bytes")
    return "ed25519-" + hashlib.sha256(public_bytes).hexdigest()[:32]
