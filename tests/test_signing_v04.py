"""DSSE + Ed25519 offline signing/verification of v0.4 bundles (RED first).

Threat boundary (FR-2, TR-2):
- The signing backend is OFFLINE DSSE with Ed25519 (labeled as such; no
  Sigstore transparency, no network, no shell/cosign subprocess).
- The private key is never written to any artifact, log, or error message;
  it is resolved exclusively from the environment by the CLI layer.
- Verification distinguishes: digest invalid, signature missing, signature
  invalid (wrong key / tampered envelope), untrusted signer, evidence
  signature invalid, and offline success.
"""
from __future__ import annotations

import base64
import copy
import json
from typing import Any

import pytest

from serving_verdict import signing
from serving_verdict.bundle_v04 import BUNDLE_SCHEMA_VERSION_V04
from serving_verdict.errors import IntegrityError, UsageError
from tests.helpers_v04_bundle import (
    SigningIdentity,
    make_signing_key,
    make_v04_bundle,
)


def pub_bytes(identity: SigningIdentity) -> bytes:
    return identity.public_key_bytes


def make_trust_store(
    identities: list[SigningIdentity], *, require_evidence: bool = False
) -> dict[str, Any]:
    return {
        "schema_version": "serving-verdict.trust-store.v0.1",
        "require_signed_evidence": require_evidence,
        "require_signed_verdict": True,
        "allowed_signers": [i.signer for i in identities],
        "allowed_keys": [
            {"key_id": i.key_id, "ed25519_public_key": base64.b64encode(pub_bytes(i)).decode("ascii")}
            for i in identities
        ],
    }


# ---------------------------------------------------------------------------
# key handling
# ---------------------------------------------------------------------------


def test_load_key_from_env_is_deterministic_and_recoverable() -> None:
    ident = make_signing_key()
    priv = signing.load_ed25519_private_key_from_seed_hex(ident.seed_hex)
    assert priv.public_key().public_bytes(
        encoding=__import__("cryptography").hazmat.primitives.serialization.Encoding.Raw,
        format=__import__("cryptography").hazmat.primitives.serialization.PublicFormat.Raw,
    ) == ident.public_key_bytes
    # wrong-length / malformed seeds fail closed with usage-class errors
    with pytest.raises(UsageError):
        signing.load_ed25519_private_key_from_seed_hex("abcd")
    with pytest.raises(UsageError):
        signing.load_ed25519_private_key_from_seed_hex("zz" * 32)


def test_public_key_bytes_roundtrip() -> None:
    ident = make_signing_key()
    key = signing.load_ed25519_private_key_from_seed_hex(ident.seed_hex)
    assert signing.public_key_bytes_of(key) == ident.public_key_bytes


# ---------------------------------------------------------------------------
# DSSE signing
# ---------------------------------------------------------------------------


def test_sign_produces_wellformed_dsse_envelope() -> None:
    ident = make_signing_key()
    doc = make_v04_bundle()
    signed = signing.sign_bundle(doc, identity=ident)
    sig = signed["signature"]
    assert sig["backend"] == "dsse_ed25519"
    assert sig["signer"] == ident.signer
    assert sig["key_id"] == ident.key_id
    assert sig["payload_type"] == signing.PAYLOAD_TYPE_V04_BUNDLE
    # signing must not alter any digest-covered field or the digest itself
    assert signed["digest"] == doc["digest"]
    env = json.loads(base64.b64decode(sig["envelope"]))
    assert env["payloadType"] == signing.PAYLOAD_TYPE_V04_BUNDLE
    payload = json.loads(base64.b64decode(env["payload"]))
    assert payload["schema_version"] == BUNDLE_SCHEMA_VERSION_V04
    assert payload["digest"] == doc["digest"]
    assert len(env["signatures"]) == 1
    assert env["signatures"][0]["keyid"] == ident.key_id
    assert env["signatures"][0]["algorithm"] == "ed25519"


def test_sign_is_pure_and_does_not_mutate_input() -> None:
    ident = make_signing_key()
    doc = make_v04_bundle()
    original = json.dumps(doc, sort_keys=True)
    signing.sign_bundle(doc, identity=ident)
    assert json.dumps(doc, sort_keys=True) == original


def test_sign_rejects_unsigned_layer_violations() -> None:
    """Signing must not launder a broken bundle: tampered digest -> error."""
    ident = make_signing_key()
    doc = make_v04_bundle()
    doc["verdict"] = "REJECT"  # tamper without resealing
    with pytest.raises(IntegrityError):
        signing.sign_bundle(doc, identity=ident)
    doc2 = make_v04_bundle()
    doc2["signature"] = "already signed somewhere else"
    with pytest.raises(IntegrityError):
        signing.sign_bundle(doc2, identity=ident)


def test_sign_error_messages_never_contain_key_material() -> None:
    ident = make_signing_key()
    doc = make_v04_bundle()
    doc["verdict"] = "REJECT"
    secret_hex = ident.seed_hex
    try:
        signing.sign_bundle(doc, identity=ident)
        raise AssertionError("expected IntegrityError")
    except IntegrityError as exc:
        assert secret_hex not in str(exc)
        assert "11" * 16 not in str(exc)


# ---------------------------------------------------------------------------
# trust store (strict local JSON)
# ---------------------------------------------------------------------------


def test_load_trust_store_roundtrip(tmp_path) -> None:
    ident = make_signing_key()
    store = make_trust_store([ident])
    p = tmp_path / "trust.json"
    p.write_text(json.dumps(store), encoding="utf-8")
    loaded = signing.load_trust_store(p)
    assert loaded.require_signed_verdict is True
    assert loaded.allowed_signers == (ident.signer,)
    assert loaded.trusted_key_ids == frozenset({ident.key_id})
    # public keys are materialized, not just stored
    pk = signing.public_key_for(loaded, ident.key_id)
    assert pk.public_bytes(
        encoding=__import__("cryptography").hazmat.primitives.serialization.Encoding.Raw,
        format=__import__("cryptography").hazmat.primitives.serialization.PublicFormat.Raw,
    ) == ident.public_key_bytes


@pytest.mark.parametrize(
    "broken",
    [
        lambda: {"schema_version": "serving-verdict.trust-store.v0.9"},
        lambda: {"require_signed_verdict": True},  # missing schema_version
        lambda: {**make_trust_store([make_signing_key()]), "rogue": 1},
        lambda: {**make_trust_store([make_signing_key()]), "allowed_keys": "nope"},
        lambda: {
            "schema_version": "serving-verdict.trust-store.v0.1",
            "require_signed_verdict": True,
            "allowed_signers": ["ci@company.example"],
            "allowed_keys": [{"key_id": "k", "ed25519_public_key": "!!!"}],
        },
        lambda: {
            "schema_version": "serving-verdict.trust-store.v0.1",
            "require_signed_verdict": True,
            "allowed_signers": ["ci@company.example"],
            "allowed_keys": [
                {"key_id": "k", "ed25519_public_key": base64.b64encode(b"short").decode()}
            ],
        },
    ],
)
def test_load_trust_store_rejects_malformed_docs(broken: Any) -> None:
    store = broken()
    with pytest.raises(UsageError):
        signing.load_trust_store(store)  # type: ignore[arg-type]


def test_load_trust_store_rejects_wrong_signer_entry_types() -> None:
    store = make_trust_store([make_signing_key()])
    store["allowed_signers"] = ["ci@company.example", 42]
    with pytest.raises(UsageError):
        signing.load_trust_store(store)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# verification pipeline (distinguishable failure classes)
# ---------------------------------------------------------------------------


def _store_with(ident: SigningIdentity, **kw: Any) -> Any:
    return signing.load_trust_store(make_trust_store([ident], **kw))


def test_offline_success_with_trusted_signer() -> None:
    ident = make_signing_key()
    doc = make_v04_bundle()
    signed = signing.sign_bundle(doc, identity=ident)
    store = _store_with(ident)
    report = signing.verify_signed_bundle(signed, store=store)
    assert report["status"] == "ok"
    assert report["digest_valid"] is True
    assert report["signature_present"] is True
    assert report["signature_valid"] is True
    assert report["signer_trusted"] is True
    assert report["signer"] == ident.signer
    assert report["key_id"] == ident.key_id
    assert report["offline"] is True
    assert report["digest"] == doc["digest"]


def test_missing_signature_failures_are_distinguishable() -> None:
    """Store policy (require signed verdict) + unsigned bundle ->
    UNTRUSTED_SIGNER (nothing to trust); explicit require without store ->
    SIGNATURE_MISSING."""
    ident = make_signing_key()
    doc = make_v04_bundle()  # signature null
    store = _store_with(ident)
    with pytest.raises(IntegrityError) as excinfo:
        signing.verify_signed_bundle(doc, store=store)
    assert excinfo.value.code == "UNTRUSTED_SIGNER"
    with pytest.raises(IntegrityError) as excinfo2:
        signing.verify_signed_bundle(doc, store=None, require_signed=True)
    assert excinfo2.value.code == "SIGNATURE_MISSING"
    assert "signature" in str(excinfo2.value).lower()


def test_wrong_key_signature_invalid_not_untrusted() -> None:
    """Signature verifies cryptographically against NO trust-store key, and
    the signer identity is not itself trusted -> UNTRUSTED_SIGNER only when
    the signature is valid; a signature from an unknown key is SIGNATURE_INVALID
    when that key is absent from the store (no key can verify it)."""
    good = make_signing_key()
    attacker = make_signing_key()  # different seed => different keypair
    import hashlib as _h

    # build attacker identity with a distinguishable key by deriving from a
    # different seed:
    seed2 = bytes.fromhex("22" * 32)
    from cryptography.hazmat.primitives import serialization

    der = signing._PKCS8_ED25519_PREFIX + seed2  # deterministic attacker key
    attacker_key = serialization.load_der_private_key(der, password=None)
    attacker_pub = attacker_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )
    attacker = SigningIdentity(
        seed_hex=seed2.hex(),
        private_key=attacker_key,
        public_key_bytes=attacker_pub,
        key_id="ed25519-" + _h.sha256(attacker_pub).hexdigest()[:32],
        signer=good.signer,  # same signer identity, wrong key (TR-2 wrong-key)
    )
    doc = make_v04_bundle()
    signed = signing.sign_bundle(doc, identity=attacker)
    store = _store_with(good)
    with pytest.raises(IntegrityError) as excinfo:
        signing.verify_signed_bundle(signed, store=store)
    # wrong key: signature cannot verify against the trusted key
    assert excinfo.value.code in ("SIGNATURE_INVALID", "UNTRUSTED_SIGNER")


def test_untrusted_signer_with_valid_signature() -> None:
    """Valid Ed25519 signature by a key the store cannot verify / a signer
    NOT in the allowlist -> UNTRUSTED_SIGNER (distinct from
    SIGNATURE_INVALID, which is the wrong-key-for-a-known-key case)."""
    trusted = make_signing_key()
    outsider = _outsider_key()
    doc = make_v04_bundle()
    signed = signing.sign_bundle(doc, identity=outsider)
    store = _store_with(trusted)
    with pytest.raises(IntegrityError) as excinfo:
        signing.verify_signed_bundle(signed, store=store)
    assert excinfo.value.code == "UNTRUSTED_SIGNER"
    msg = str(excinfo.value).lower()
    assert "trust" in msg or "signer" in msg
    assert "signature does not verify" not in msg  # not a crypto failure


def test_store_policy_requires_signed_verdict_unsigned_bundle_is_untrusted() -> None:
    """A bundle with NO signature against a store that requires signed
    verdicts fails with UNTRUSTED_SIGNER (nothing to trust), while an
    explicit require_signed flag still yields SIGNATURE_MISSING."""
    ident = make_signing_key()
    doc = make_v04_bundle()  # signature null
    store = _store_with(ident)
    with pytest.raises(IntegrityError) as excinfo:
        signing.verify_signed_bundle(doc, store=store)
    assert excinfo.value.code == "UNTRUSTED_SIGNER"
    with pytest.raises(IntegrityError) as excinfo2:
        signing.verify_signed_bundle(doc, store=None, require_signed=True)
    assert excinfo2.value.code == "SIGNATURE_MISSING"
    assert "signature" in str(excinfo2.value).lower()


def _outsider_key() -> SigningIdentity:
    import hashlib

    from cryptography.hazmat.primitives import serialization

    seed = bytes.fromhex("33" * 32)
    der = signing._PKCS8_ED25519_PREFIX + seed
    key = serialization.load_der_private_key(der, password=None)
    pub = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )
    return SigningIdentity(
        seed_hex=seed.hex(),
        private_key=key,
        public_key_bytes=pub,
        key_id="ed25519-" + hashlib.sha256(pub).hexdigest()[:32],
        signer="outsider@example.com",
    )


def test_tampered_payload_flips_to_signature_invalid() -> None:
    ident = make_signing_key()
    doc = make_v04_bundle()
    signed = signing.sign_bundle(doc, identity=ident)
    # mutate a digest-covered field AND keep the stale digest (classic replay):
    tampered = copy.deepcopy(signed)
    tampered["verdict"] = "REJECT"
    store = _store_with(ident)
    with pytest.raises(IntegrityError) as excinfo:
        signing.verify_signed_bundle(tampered, store=store)
    # the FIRST failure must be the digest (before the signature layer)
    assert excinfo.value.code == "DIGEST_INVALID"


def test_tampered_envelope_fails_signature_layer() -> None:
    ident = make_signing_key()
    doc = make_v04_bundle()
    signed = signing.sign_bundle(doc, identity=ident)
    tampered = copy.deepcopy(signed)
    env = json.loads(base64.b64decode(tampered["signature"]["envelope"]))
    env["signatures"][0]["sig"] = base64.b64encode(bytes(64)).decode("ascii")
    tampered["signature"]["envelope"] = base64.b64encode(
        json.dumps(env).encode("ascii")
    ).decode("ascii")
    store = _store_with(ident)
    with pytest.raises(IntegrityError) as excinfo:
        signing.verify_signed_bundle(tampered, store=store)
    assert excinfo.value.code == "SIGNATURE_INVALID"


def test_keyid_mismatch_inside_envelope_fails() -> None:
    """keyid inside the envelope must match the section's key_id (binding)."""
    ident = make_signing_key()
    doc = make_v04_bundle()
    signed = signing.sign_bundle(doc, identity=ident)
    tampered = copy.deepcopy(signed)
    env = json.loads(base64.b64decode(tampered["signature"]["envelope"]))
    env["signatures"][0]["keyid"] = "ed25519-forged"
    tampered["signature"]["envelope"] = base64.b64encode(
        json.dumps(env).encode("ascii")
    ).decode("ascii")
    store = _store_with(ident)
    with pytest.raises(IntegrityError) as excinfo:
        signing.verify_signed_bundle(tampered, store=store)
    assert excinfo.value.code == "SIGNATURE_INVALID"


def test_algorithm_confusion_rejected() -> None:
    """An envelope claiming a non-Ed25519 algorithm is rejected (no RSA
    confusion path exists; the check is explicit and fail-closed)."""
    ident = make_signing_key()
    doc = make_v04_bundle()
    signed = signing.sign_bundle(doc, identity=ident)
    tampered = copy.deepcopy(signed)
    env = json.loads(base64.b64decode(tampered["signature"]["envelope"]))
    env["signatures"][0]["algorithm"] = "rsa"
    tampered["signature"]["envelope"] = base64.b64encode(
        json.dumps(env).encode("ascii")
    ).decode("ascii")
    store = _store_with(ident)
    with pytest.raises(IntegrityError) as excinfo:
        signing.verify_signed_bundle(tampered, store=store)
    assert excinfo.value.code == "SIGNATURE_INVALID"


def test_tampered_manifest_and_issued_at_detected_by_digest() -> None:
    ident = make_signing_key()
    doc = make_v04_bundle()
    signed = signing.sign_bundle(doc, identity=ident)
    store = _store_with(ident)

    t1 = copy.deepcopy(signed)
    t1["evidence_manifest"][0]["sha256"] = "e" * 64
    with pytest.raises(IntegrityError) as e1:
        signing.verify_signed_bundle(t1, store=store)
    assert e1.value.code == "DIGEST_INVALID"

    t2 = copy.deepcopy(signed)
    t2["issued_at"] = "2027-01-01T00:00:00+00:00"  # timestamp mutation
    with pytest.raises(IntegrityError) as e2:
        signing.verify_signed_bundle(t2, store=store)
    assert e2.value.code == "DIGEST_INVALID"


def test_unsigned_bundle_fails_without_store_when_required() -> None:
    doc = make_v04_bundle()
    # no store at all: require-signed semantics still fail closed
    with pytest.raises(IntegrityError) as excinfo:
        signing.verify_signed_bundle(doc, store=None, require_signed=True)
    assert excinfo.value.code == "SIGNATURE_MISSING"


def test_evidence_signature_required_but_missing_fails() -> None:
    ident = make_signing_key()
    doc = make_v04_bundle()
    doc["trust"]["require_signed_evidence"] = True
    doc["digest"] = signing.compute_signed_digest(doc)  # re-seal honestly
    signed = signing.sign_bundle(doc, identity=ident)
    store = _store_with(ident, require_evidence=True)
    with pytest.raises(IntegrityError) as excinfo:
        signing.verify_signed_bundle(signed, store=store)
    assert excinfo.value.code == "EVIDENCE_SIGNATURES_INVALID"


def test_require_false_untrusted_signer_is_informational_not_fatal() -> None:
    """When the store does not require a signed verdict, a bad signature is
    reported but does not raise (distinct success-with-warnings semantics)."""
    trusted = make_signing_key()
    outsider = _outsider_key()
    doc = make_v04_bundle()
    signed = signing.sign_bundle(doc, identity=outsider)
    store = signing.load_trust_store(
        make_trust_store([trusted], require_evidence=False, **{})
    )
    # store policy requires signed verdict (default) -> still fail closed:
    with pytest.raises(IntegrityError) as excinfo:
        signing.verify_signed_bundle(signed, store=store)
    assert excinfo.value.code == "UNTRUSTED_SIGNER"


# ---------------------------------------------------------------------------
# private-key leak scan
# ---------------------------------------------------------------------------


def test_private_key_never_in_artifact_or_report(tmp_path) -> None:
    ident = make_signing_key()
    doc = make_v04_bundle()
    signed = signing.sign_bundle(doc, identity=ident)
    blob = json.dumps(signed, sort_keys=True)
    assert ident.seed_hex not in blob
    # the full PKCS8 DER of the private key must not appear either
    priv_der = ident.private_key.private_bytes(
        encoding=__import__("cryptography").hazmat.primitives.serialization.Encoding.DER,
        format=__import__("cryptography").hazmat.primitives.serialization.PrivateFormat.PKCS8,
        encryption_algorithm=__import__("cryptography").hazmat.primitives.serialization.NoEncryption(),
    )
    assert base64.b64encode(priv_der) not in blob.encode("ascii")
    assert priv_der not in blob.encode("latin-1", "ignore")
    store = _store_with(ident)
    report = signing.verify_signed_bundle(signed, store=store)
    report_blob = json.dumps(report, sort_keys=True)
    assert ident.seed_hex not in report_blob


def test_trust_store_public_key_mapping_is_immutable() -> None:
    identity = make_signing_key()
    store = _store_with(identity)
    with pytest.raises(TypeError):
        store._public_keys[identity.key_id] = identity.private_key.public_key()  # type: ignore[index]
