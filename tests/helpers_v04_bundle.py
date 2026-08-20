"""Shared builders for v0.4 signed-bundle tests.

``make_v04_bundle`` returns a *valid, digest-sealed* v0.4 bundle document
(``signature`` is null). ``make_signing_key`` returns a deterministic
Ed25519 signing identity (fixed test seed — never a production key) plus the
derived ``key_id``.
"""
from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from serving_verdict.bundle_v04 import BUNDLE_SCHEMA_VERSION_V04, compute_v04_digest

ARTIFACT_A = "a" * 64
ARTIFACT_B = "b" * 64


def make_manifest_entry(artifact_id: str, sha256: str) -> dict[str, Any]:
    return {
        "artifact_id": artifact_id,
        "sha256": sha256,
        "artifact_schema": "serving-verdict.benchmark.v0.1",
        "size_bytes": 1234,
        "producer": "ci@company.example",
        "produced_at": "2026-08-01T10:00:00+00:00",
        "tool": "serving-verdict",
        "tool_version": "0.4.0",
        "source_type": "benchmark_run",
        "model": {
            "name": "hf://org/model",
            "revision": "abc123",
            "quantization": "bf16",
            "artifact_digest": "sha256:" + "c" * 64,
            "tokenizer_revision": "abc123",
        },
        "runtime": {
            "name": "vllm",
            "version": "0.6.0",
            "image_digest": "sha256:" + "d" * 64,
            "flags": ["--max-model-len=4096"],
        },
        "hardware": {
            "gpu_model": "GH200",
            "gpu_count": 1,
            "driver_version": "550.54",
            "host_arch": "aarch64",
            "memory_capacity_gb": 48,
        },
        "procedure": {"id": "quick", "version": "1.0.0"},
    }


def make_v04_bundle(**overrides: Any) -> dict[str, Any]:
    """Build a valid sealed v0.4 bundle with optional top-level overrides."""
    doc: dict[str, Any] = {
        "schema_version": BUNDLE_SCHEMA_VERSION_V04,
        "case": {
            "case_id": "fixture-v04",
            "description": "v0.4 fixture case",
            "workload_id": "workload-a",
        },
        "baseline": {"artifact_id": "baseline.json", "artifact_sha256": ARTIFACT_A},
        "candidate": {"artifact_id": "candidate.json", "artifact_sha256": ARTIFACT_B},
        "evidence_manifest": [
            make_manifest_entry("baseline.json", ARTIFACT_A),
            make_manifest_entry("candidate.json", ARTIFACT_B),
        ],
        "comparisons": [
            {
                "metric": "decode_tokens_per_s",
                "baseline_value": 1.0,
                "candidate_value": 0.75,
                "relative_delta": -0.25,
                "direction": "higher_better",
                "unit": "tok/s",
            }
        ],
        "statistics": {"primary_metric": "decode_tokens_per_s", "trials": 3, "seed": 7},
        "gates": [
            {
                "id": "request_success",
                "status": "pass",
                "authority": "machine_measured",
                "evidence": ["baseline.json", "candidate.json"],
            }
        ],
        "trust": {
            "require_signed_evidence": False,
            "require_signed_verdict": True,
            "allowed_signers": ["ci@company.example"],
        },
        "verdict": "PROMOTE",
        "reason_codes": ["PRIMARY_EFFECT_PASSED", "ALL_REQUIRED_GATES_PASSED"],
        "claim_boundary": {
            "workload_set": ["workload-a"],
            "runtime": {"name": "vllm", "version": "0.6.0"},
            "model": "hf://org/model@abc123",
            "hardware_class": "GH200 x1",
            "procedure": {"id": "quick", "version": "1.0.0"},
            "candidate_delta": {
                "parameter": "speculative_num_steps",
                "baseline": 5,
                "candidate": 7,
            },
            "observed_period": {
                "start": "2026-08-01T09:00:00+00:00",
                "end": "2026-08-01T10:00:00+00:00",
            },
            "limitations": ["single hardware class", "offline DSSE backend"],
            "summary": "Promotion claim limited to workload-a on GH200 x1.",
        },
        "issued_at": "2026-08-01T11:00:00+00:00",
        "producer": {
            "tool": "serving-verdict",
            "tool_version": "0.4.0",
            "identity": "ci@company.example",
            "code_revision": "1234567890abcdef1234567890abcdef12345678",
        },
        "digest": None,
        "signature": None,
    }
    for key, value in overrides.items():
        doc[key] = value
    doc["digest"] = compute_v04_digest(doc)
    return doc


@dataclass(frozen=True)
class SigningIdentity:
    """An Ed25519 signing identity: seed, key material, key_id, signer."""

    seed_hex: str
    private_key: Ed25519PrivateKey
    public_key_bytes: bytes
    key_id: str
    signer: str


#: PKCS8 DER prefix for an Ed25519 private key (RFC 8410) wrapping a 32-byte seed.
_PKCS8_ED25519_PREFIX = bytes.fromhex("302e020100300506032b657004220420")


def _from_seed(seed: bytes) -> Ed25519PrivateKey:
    assert len(seed) == 32
    der = _PKCS8_ED25519_PREFIX + seed
    key = serialization.load_der_private_key(der, password=None)
    assert isinstance(key, Ed25519PrivateKey)
    return key


def make_signing_key(signer: str = "ci@company.example") -> SigningIdentity:
    """Deterministic test identity (fixed seed, never a production key)."""
    seed = bytes.fromhex("00" * 16 + "11" * 16)
    private_key = _from_seed(seed)
    pub = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    key_id = "ed25519-" + hashlib.sha256(pub).hexdigest()[:32]
    return SigningIdentity(
        seed_hex=seed.hex(),
        private_key=private_key,
        public_key_bytes=pub,
        key_id=key_id,
        signer=signer,
    )


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")
