"""CLI tests for `sign` and `verify --require-signature --trust-store` (RED first).

Exit-code contract (extended, old codes preserved):
  0 = success (incl. passing verify, any valid verdict)
  2 = usage/config/load error (missing env var, bad paths, bad trust store)
  4 = integrity OR signature verification failure
The private key lives ONLY in the environment variable named by --key-env;
it must never appear in stdout, stderr, or written files.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from serving_verdict import signing
from tests.helpers_v04_bundle import (
    SigningIdentity,
    b64,
    make_v04_bundle,
)

ROOT = Path(__file__).resolve().parents[1]
CLI = str(ROOT / ".venv" / "bin" / "serving-verdict")

TEST_SEED_HEX = bytes.fromhex("00" * 16 + "11" * 16).hex()


def run_cli(*args: str, env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    return subprocess.run([CLI, *args], capture_output=True, text=True, cwd=ROOT, env=env)


def key_id_for_seed(seed_hex: str) -> str:
    key = signing.load_ed25519_private_key_from_seed_hex(seed_hex)
    return signing.key_id_for_public_key(signing.public_key_bytes_of(key))


def make_trust_store_file(tmp_path: Path, seed_hex: str, signer: str) -> Path:
    key = signing.load_ed25519_private_key_from_seed_hex(seed_hex)
    pub = b64(signing.public_key_bytes_of(key))
    store = {
        "schema_version": signing.TRUST_STORE_SCHEMA_VERSION,
        "require_signed_evidence": False,
        "require_signed_verdict": True,
        "allowed_signers": [signer],
        "allowed_keys": [{"key_id": key_id_for_seed(seed_hex), "ed25519_public_key": pub}],
    }
    p = tmp_path / "trust.json"
    p.write_text(json.dumps(store, indent=2), encoding="utf-8")
    return p


def write_bundle(tmp_path: Path, doc: dict[str, Any], name: str = "bundle.json") -> Path:
    p = tmp_path / name
    p.write_text(json.dumps(doc, indent=2, sort_keys=True), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# sign command
# ---------------------------------------------------------------------------


def test_sign_roundtrip_then_verify(tmp_path: Path) -> None:
    doc = make_v04_bundle()
    bundle_p = write_bundle(tmp_path, doc)
    out_p = tmp_path / "signed.json"
    r = run_cli(
        "sign", str(bundle_p),
        "--key-env", "SV_TEST_KEY",
        "--signer", "ci@company.example",
        "--out", str(out_p),
        env_extra={"SV_TEST_KEY": TEST_SEED_HEX},
    )
    assert r.returncode == 0, r.stderr
    signed = json.loads(out_p.read_text())
    assert signed["signature"]["backend"] == "dsse_ed25519"
    assert signed["signature"]["signer"] == "ci@company.example"
    assert signed["signature"]["key_id"] == key_id_for_seed(TEST_SEED_HEX)
    assert signed["digest"] == doc["digest"]  # signing never alters the digest

    store_p = make_trust_store_file(tmp_path, TEST_SEED_HEX, "ci@company.example")
    r2 = run_cli(
        "verify", str(out_p), "--require-signature", "--trust-store", str(store_p)
    )
    assert r2.returncode == 0, r2.stderr
    payload = json.loads(r2.stdout)
    assert payload["valid"] is True
    assert payload["signature_valid"] is True
    assert payload["signer_trusted"] is True
    assert payload["offline"] is True


def test_sign_missing_env_var_is_exit_2_and_no_file(tmp_path: Path) -> None:
    doc = make_v04_bundle()
    bundle_p = write_bundle(tmp_path, doc)
    out_p = tmp_path / "signed.json"
    env = dict(os.environ)
    env.pop("SV_MISSING_KEY", None)
    r = subprocess.run(
        [CLI, "sign", str(bundle_p), "--key-env", "SV_MISSING_KEY",
         "--signer", "s@x", "--out", str(out_p)],
        capture_output=True, text=True, cwd=ROOT, env=env,
    )
    assert r.returncode == 2, r.stderr
    assert not out_p.exists()
    assert TEST_SEED_HEX not in r.stderr
    assert "SV_MISSING_KEY" in r.stderr  # the var NAME is fine to echo


def test_sign_rejects_invalid_seed_with_exit_2_and_no_key_leak(tmp_path: Path) -> None:
    doc = make_v04_bundle()
    bundle_p = write_bundle(tmp_path, doc)
    r = run_cli(
        "sign", str(bundle_p),
        "--key-env", "SV_BAD_KEY",
        "--signer", "s@x",
        "--out", str(tmp_path / "nope.json"),
        env_extra={"SV_BAD_KEY": "not-hex"},
    )
    assert r.returncode == 2, r.stderr
    assert "not-hex" not in r.stderr  # seed content must never be echoed


def test_sign_refuses_bundle_with_existing_signature(tmp_path: Path) -> None:
    doc = make_v04_bundle()
    doc["signature"] = {
        "backend": "dsse_ed25519",
        "signer": "someone@else",
        "key_id": "ed25519-x",
        "payload_type": signing.PAYLOAD_TYPE_V04_BUNDLE,
        "envelope": b64(json.dumps({"payloadType": "x", "payload": b64(b"y"),
                                    "signatures": [{"keyid": "k", "sig": b64(bytes(64)),
                                                    "algorithm": "ed25519"}]}).encode()),
    }
    from serving_verdict.bundle_v04 import compute_v04_digest

    doc["digest"] = compute_v04_digest(doc)
    bundle_p = write_bundle(tmp_path, doc)
    r = run_cli(
        "sign", str(bundle_p),
        "--key-env", "SV_TEST_KEY",
        "--signer", "s@x",
        "--out", str(tmp_path / "x.json"),
        env_extra={"SV_TEST_KEY": TEST_SEED_HEX},
    )
    assert r.returncode == 2, r.stderr
    assert not (tmp_path / "x.json").exists()


def test_sign_does_not_write_private_key_anywhere(tmp_path: Path) -> None:
    doc = make_v04_bundle()
    bundle_p = write_bundle(tmp_path, doc)
    out_p = tmp_path / "signed.json"
    r = run_cli(
        "sign", str(bundle_p),
        "--key-env", "SV_TEST_KEY",
        "--signer", "ci@company.example",
        "--out", str(out_p),
        env_extra={"SV_TEST_KEY": TEST_SEED_HEX},
    )
    assert r.returncode == 0, r.stderr
    assert TEST_SEED_HEX not in r.stdout
    assert TEST_SEED_HEX not in r.stderr
    written = json.loads(out_p.read_text())
    assert TEST_SEED_HEX not in json.dumps(written)


# ---------------------------------------------------------------------------
# verify with signature requirements
# ---------------------------------------------------------------------------


def test_verify_mutated_bundle_exit_4(tmp_path: Path) -> None:
    doc = make_v04_bundle()
    bundle_p = write_bundle(tmp_path, doc)
    out_p = tmp_path / "signed.json"
    run_cli("sign", str(bundle_p), "--key-env", "SV_TEST_KEY",
            "--signer", "ci@company.example", "--out", str(out_p),
            env_extra={"SV_TEST_KEY": TEST_SEED_HEX})
    signed = json.loads(out_p.read_text())
    signed["verdict"] = "REJECT"  # tamper after signing
    mutated_p = write_bundle(tmp_path, signed, name="mutated.json")

    store_p = make_trust_store_file(tmp_path, TEST_SEED_HEX, "ci@company.example")
    r = run_cli("verify", str(mutated_p), "--require-signature",
                "--trust-store", str(store_p), "--json")
    assert r.returncode == 4, r.stderr
    payload = json.loads(r.stdout)
    assert payload["valid"] is False
    assert "DIGEST_INVALID" in r.stdout or "digest" in r.stdout.lower()


def test_verify_wrong_key_env_signer_exit_4(tmp_path: Path) -> None:
    """Bundle signed with key A, trust store holding key B -> exit 4."""
    doc = make_v04_bundle()
    other_seed = bytes.fromhex("99" * 32).hex()
    other_key = signing.load_ed25519_private_key_from_seed_hex(other_seed)
    other_id = SigningIdentity(
        seed_hex=other_seed,
        private_key=other_key,
        public_key_bytes=signing.public_key_bytes_of(other_key),
        key_id=signing.key_id_for_public_key(signing.public_key_bytes_of(other_key)),
        signer="ci@company.example",
    )
    from serving_verdict import signing as sig

    signed = sig.sign_bundle(doc, identity=other_id)
    signed_p = write_bundle(tmp_path, signed, name="signed_other.json")
    store_p = make_trust_store_file(tmp_path, TEST_SEED_HEX, "ci@company.example")
    r = run_cli("verify", str(signed_p), "--require-signature",
                "--trust-store", str(store_p), "--json")
    assert r.returncode == 4, r.stderr


def test_verify_untrusted_signer_identity_exit_4(tmp_path: Path) -> None:
    """Correct key but signer identity not in the allowlist -> exit 4."""
    doc = make_v04_bundle()
    bundle_p = write_bundle(tmp_path, doc)
    out_p = tmp_path / "signed.json"
    run_cli("sign", str(bundle_p), "--key-env", "SV_TEST_KEY",
            "--signer", "ci@company.example", "--out", str(out_p),
            env_extra={"SV_TEST_KEY": TEST_SEED_HEX})
    # trust store allows a DIFFERENT signer identity with the same key
    key = signing.load_ed25519_private_key_from_seed_hex(TEST_SEED_HEX)
    store = {
        "schema_version": signing.TRUST_STORE_SCHEMA_VERSION,
        "require_signed_evidence": False,
        "require_signed_verdict": True,
        "allowed_signers": ["somebody-else@company.example"],
        "allowed_keys": [
            {"key_id": key_id_for_seed(TEST_SEED_HEX),
             "ed25519_public_key": b64(signing.public_key_bytes_of(key))}
        ],
    }
    store_p = tmp_path / "trust2.json"
    store_p.write_text(json.dumps(store), encoding="utf-8")
    r = run_cli("verify", str(out_p), "--require-signature",
                "--trust-store", str(store_p), "--json")
    assert r.returncode == 4, r.stderr


def test_verify_unsigned_bundle_with_require_exit_4(tmp_path: Path) -> None:
    doc = make_v04_bundle()
    bundle_p = write_bundle(tmp_path, doc)
    store_p = make_trust_store_file(tmp_path, TEST_SEED_HEX, "ci@company.example")
    r = run_cli("verify", str(bundle_p), "--require-signature",
                "--trust-store", str(store_p), "--json")
    assert r.returncode == 4, r.stderr
    payload = json.loads(r.stdout)
    assert payload["valid"] is False


def test_verify_without_signature_flags_still_verifies_digest_only(tmp_path: Path) -> None:
    doc = make_v04_bundle()
    bundle_p = write_bundle(tmp_path, doc)
    r = run_cli("verify", str(bundle_p), "--json")
    assert r.returncode == 0, r.stderr
    payload = json.loads(r.stdout)
    assert payload["valid"] is True


def test_verify_v01_bundle_unaffected(tmp_path: Path) -> None:
    """Old v0.1 bundles verify exactly as before (parity, no signature flags)."""
    from serving_verdict import demo

    demo_dir = tmp_path / "demo"
    bundles = demo.build_demo(demo_dir)
    v01 = None
    for p in bundles:
        doc = json.loads(p.read_text(encoding="utf-8"))
        if doc["schema_version"] == "serving-verdict.bundle.v0.1":
            v01 = p
    assert v01 is not None
    r = run_cli("verify", str(v01), "--json")
    assert r.returncode == 0, r.stderr
    payload = json.loads(r.stdout)
    assert payload["valid"] is True
    # and an old bundle with --require-signature fails closed (exit 4):
    store_p = make_trust_store_file(tmp_path, TEST_SEED_HEX, "ci@company.example")
    r2 = run_cli("verify", str(v01), "--require-signature",
                 "--trust-store", str(store_p), "--json")
    assert r2.returncode == 4, r2.stderr


def test_verify_missing_trust_store_exit_2(tmp_path: Path) -> None:
    doc = make_v04_bundle()
    bundle_p = write_bundle(tmp_path, doc)
    r = run_cli("verify", str(bundle_p), "--require-signature",
                "--trust-store", str(tmp_path / "absent.json"), "--json")
    assert r.returncode == 2, r.stderr


def test_verify_malformed_trust_store_exit_2(tmp_path: Path) -> None:
    doc = make_v04_bundle()
    bundle_p = write_bundle(tmp_path, doc)
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"schema_version": "nope"}), encoding="utf-8")
    r = run_cli("verify", str(bundle_p), "--require-signature",
                "--trust-store", str(bad), "--json")
    assert r.returncode == 2, r.stderr


def test_verify_timestamp_mutation_exit_4(tmp_path: Path) -> None:
    doc = make_v04_bundle()
    bundle_p = write_bundle(tmp_path, doc)
    out_p = tmp_path / "signed.json"
    run_cli("sign", str(bundle_p), "--key-env", "SV_TEST_KEY",
            "--signer", "ci@company.example", "--out", str(out_p),
            env_extra={"SV_TEST_KEY": TEST_SEED_HEX})
    signed = json.loads(out_p.read_text())
    signed["issued_at"] = "2027-12-31T00:00:00+00:00"  # timestamp mutation
    mutated_p = write_bundle(tmp_path, signed, name="ts.json")
    store_p = make_trust_store_file(tmp_path, TEST_SEED_HEX, "ci@company.example")
    r = run_cli("verify", str(mutated_p), "--require-signature",
                "--trust-store", str(store_p), "--json")
    assert r.returncode == 4, r.stderr


def test_verify_manifest_mutation_exit_4(tmp_path: Path) -> None:
    doc = make_v04_bundle()
    bundle_p = write_bundle(tmp_path, doc)
    out_p = tmp_path / "signed.json"
    run_cli("sign", str(bundle_p), "--key-env", "SV_TEST_KEY",
            "--signer", "ci@company.example", "--out", str(out_p),
            env_extra={"SV_TEST_KEY": TEST_SEED_HEX})
    signed = json.loads(out_p.read_text())
    signed["evidence_manifest"][0]["sha256"] = "e" * 64
    mutated_p = write_bundle(tmp_path, signed, name="man.json")
    store_p = make_trust_store_file(tmp_path, TEST_SEED_HEX, "ci@company.example")
    r = run_cli("verify", str(mutated_p), "--require-signature",
                "--trust-store", str(store_p), "--json")
    assert r.returncode == 4, r.stderr


def test_verify_trust_store_policy_applies_without_require_flag(tmp_path: Path) -> None:
    bundle_p = write_bundle(tmp_path, make_v04_bundle())
    store_p = make_trust_store_file(tmp_path, TEST_SEED_HEX, "ci@company.example")
    r = run_cli("verify", str(bundle_p), "--trust-store", str(store_p), "--json")
    assert r.returncode == 4, r.stderr
    assert json.loads(r.stdout)["code"] == "UNTRUSTED_SIGNER"
