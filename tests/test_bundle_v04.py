"""v0.4 signed verdict bundle: schema, canonical digest, parsing, immutability (RED first).

The v0.4 bundle is a typed, tamper-evident, signable document:

- ``schema_version == "serving-verdict.bundle.v0.4"``
- exact top-level key set (no extras, nothing missing)
- structured case / baseline / candidate sections
- ``evidence_manifest`` entries with full provenance fields
- structured ``claim_boundary``
- ``issued_at`` + ``producer`` are DIGEST-COVERED (authenticity binding;
  unlike the v0.1 compatibility digest, mutating ``issued_at`` breaks it)
- ``digest`` covers exactly ``DIGEST_COVERED_FIELDS``:
  schema_version, case, baseline, candidate, evidence_manifest,
  comparisons, statistics, gates, trust, verdict, reason_codes,
  claim_boundary, issued_at, producer.
- ``signature`` and ``digest`` themselves are NOT covered (re-signing never
  changes the digest).
"""
from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from serving_verdict.bundle_v04 import (
    BUNDLE_SCHEMA_VERSION_V04,
    DIGEST_COVERED_FIELDS,
    compute_v04_digest,
    freeze,
    parse_v04_bundle,
    verify_v04_bundle,
)
from serving_verdict.canonical import canonicalize
from serving_verdict.errors import IntegrityError
from tests.helpers_v04_bundle import ARTIFACT_A, make_v04_bundle


def test_schema_version_constant() -> None:
    assert BUNDLE_SCHEMA_VERSION_V04 == "serving-verdict.bundle.v0.4"


def test_digest_covers_exactly_the_documented_field_set() -> None:
    assert frozenset(
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
    ) == DIGEST_COVERED_FIELDS
    # digest and signature are explicitly NOT covered: re-signing is stable.
    assert "digest" not in DIGEST_COVERED_FIELDS
    assert "signature" not in DIGEST_COVERED_FIELDS


def test_digest_is_deterministic_and_matches_manual_canonical_bytes() -> None:
    doc = make_v04_bundle()
    doc2 = make_v04_bundle()
    assert doc["digest"] == doc2["digest"]
    projection = {k: doc[k] for k in DIGEST_COVERED_FIELDS}
    expected = "sha256:" + hashlib.sha256(canonicalize(projection)).hexdigest()
    assert doc["digest"] == expected


def test_mutating_issued_at_breaks_the_digest() -> None:
    """v0.4 authenticity: issued_at is digest-covered (unlike v0.1 created_at)."""
    doc = make_v04_bundle()
    doc["issued_at"] = "2026-08-02T11:00:00+00:00"
    with pytest.raises(IntegrityError):
        verify_v04_bundle(doc)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d["case"].update(case_id="tampered-case"),
        lambda d: d["baseline"].update(artifact_sha256="e" * 64),
        lambda d: d["candidate"].update(artifact_id="other.json"),
        lambda d: d["evidence_manifest"][0].update(sha256="e" * 64),
        lambda d: d["evidence_manifest"][0]["hardware"].update(gpu_model="V100"),
        lambda d: d["comparisons"][0].update(baseline_value=99.0),
        lambda d: d["statistics"].update(seed=999),
        lambda d: d["gates"][0].update(status="fail"),
        lambda d: d["trust"].update(allowed_signers=["attacker@example.com"]),
        lambda d: d.update(verdict="REJECT"),
        lambda d: d.update(reason_codes=["FORGED_REASON"]),
        lambda d: d["claim_boundary"].update(hardware_class="V100 x8"),
        lambda d: d["producer"].update(identity="not-ci@example.com"),
    ],
)
def test_mutating_any_digest_covered_section_breaks_verification(mutate: Any) -> None:
    doc = make_v04_bundle()
    mutate(doc)
    with pytest.raises(IntegrityError):
        verify_v04_bundle(doc)


def test_mutating_the_signature_section_does_not_change_the_digest() -> None:
    """Re-signing must not alter the canonical digest."""
    doc = make_v04_bundle()
    original = doc["digest"]
    doc["signature"] = {
        "backend": "dsse_ed25519",
        "signer": "someone-else@example.com",
        "key_id": "other-key",
        "envelope": "bm90LWEtcmVhbC1zaWduYXR1cmU=",
    }
    assert doc["digest"] == original
    assert compute_v04_digest(doc) == original
    # verification still fails on the digest-covered fields being intact ->
    # only the signature section is invalid, so the digest check must pass.
    try:
        verify_v04_bundle(doc)
        raise AssertionError("expected SignatureIntegrityError, got success")
    except IntegrityError as exc:
        assert "digest mismatch" not in str(exc).lower()


def test_parse_returns_frozen_object_matching_source_document() -> None:
    doc = make_v04_bundle()
    parsed = parse_v04_bundle(doc)
    assert parsed.schema_version == BUNDLE_SCHEMA_VERSION_V04
    assert parsed.case["case_id"] == "fixture-v04"
    assert parsed.baseline["artifact_sha256"] == ARTIFACT_A
    assert len(parsed.evidence_manifest) == 2
    assert parsed.evidence_manifest[0]["sha256"] == ARTIFACT_A
    assert parsed.verdict == "PROMOTE"
    assert parsed.issued_at == doc["issued_at"]
    assert parsed.digest == doc["digest"]


def test_freeze_deep_immutability_blocks_nested_and_baseline_mutators() -> None:
    doc = make_v04_bundle()
    f = freeze(doc)
    with pytest.raises(TypeError):
        f["verdict"] = "REJECT"  # type: ignore[index]
    with pytest.raises(TypeError):
        f["case"]["case_id"] = "x"  # type: ignore[index]
    with pytest.raises(TypeError):
        f["evidence_manifest"][0]["sha256"] = "e" * 64  # type: ignore[index]
    with pytest.raises(AttributeError):
        f["claim_boundary"]["limitations"].append("extra")  # frozen tuple: no append
    with pytest.raises(TypeError):
        f["claim_boundary"]["limitations"] = ("extra",)  # type: ignore[index]
    with pytest.raises(TypeError):
        dict.__setitem__(f, "verdict", "REJECT")
    with pytest.raises(TypeError):
        del f["digest"]  # MappingProxyType does not support item deletion
    assert isinstance(f, Mapping)
    # the source document must remain mutable (freeze is a view, not a copy-destroy)
    doc["verdict"] = "REJECT"
    doc["verdict"] = "PROMOTE"


def test_parse_rejects_unknown_top_level_keys() -> None:
    doc = make_v04_bundle()
    doc["rogue_field"] = "injected"
    with pytest.raises(IntegrityError, match="rogue_field"):
        parse_v04_bundle(doc)


def test_parse_rejects_missing_required_key() -> None:
    doc = make_v04_bundle()
    del doc["evidence_manifest"]
    with pytest.raises(IntegrityError, match="evidence_manifest"):
        parse_v04_bundle(doc)


def test_parse_rejects_wrong_schema_version() -> None:
    doc = make_v04_bundle()
    doc["schema_version"] = "serving-verdict.bundle.v0.99"
    with pytest.raises(IntegrityError, match="schema_version"):
        parse_v04_bundle(doc)


def test_parse_rejects_non_mapping_bundle() -> None:
    with pytest.raises(IntegrityError):
        parse_v04_bundle([1, 2, 3])  # type: ignore[arg-type]


def test_parse_rejects_malformed_manifest_entry() -> None:
    doc = make_v04_bundle()
    doc["evidence_manifest"][0] = {
        "artifact_id": "baseline.json",
        "sha256": "not-a-hash",
    }
    with pytest.raises(IntegrityError, match="manifest"):
        parse_v04_bundle(doc)


def test_parse_rejects_duplicate_manifest_artifact_ids() -> None:
    doc = make_v04_bundle()
    doc["evidence_manifest"].append(copy.deepcopy(doc["evidence_manifest"][0]))
    with pytest.raises(IntegrityError, match="duplicate"):
        parse_v04_bundle(doc)


def test_parse_rejects_bad_verdict() -> None:
    doc = make_v04_bundle()
    doc["verdict"] = "MAYBE"
    with pytest.raises(IntegrityError, match="verdict"):
        parse_v04_bundle(doc)


def test_parse_rejects_bad_reason_codes_type() -> None:
    doc = make_v04_bundle()
    doc["reason_codes"] = "PRIMARY_EFFECT_PASSED"
    with pytest.raises(IntegrityError, match="reason_codes"):
        parse_v04_bundle(doc)


def test_parse_rejects_non_integer_size_bytes() -> None:
    doc = make_v04_bundle()
    doc["evidence_manifest"][0]["size_bytes"] = True
    with pytest.raises(IntegrityError, match="size_bytes"):
        parse_v04_bundle(doc)


def test_verify_reports_full_status_for_a_valid_unsigned_bundle() -> None:
    doc = make_v04_bundle()
    report = verify_v04_bundle(doc)
    assert report["valid"] is True
    assert report["digest_valid"] is True
    assert report["signature_present"] is False
    assert report["signature_valid"] is False
    assert report["signer_trusted"] is False
    assert report["evidence_signatures_valid"] is False
    assert report["digest"] == doc["digest"]


def test_verify_rejects_present_but_malformed_signature_section() -> None:
    """A signature that is present but malformed fails the signature layer,
    never the digest layer (distinct failure classes)."""
    doc = make_v04_bundle()
    doc["signature"] = {
        "backend": "dsse_ed25519",
        "signer": "someone@example.com",
        "key_id": "k1",
        "envelope": "!!!not-base64!!!",
    }
    with pytest.raises(IntegrityError) as excinfo:
        verify_v04_bundle(doc)
    msg = str(excinfo.value).lower()
    assert "signature" in msg
    assert "digest mismatch" not in msg


def test_verify_distinguishes_digest_invalid_from_signature_issues() -> None:
    doc = make_v04_bundle()
    doc["verdict"] = "REJECT"  # tamper WITHOUT resealing
    with pytest.raises(IntegrityError) as excinfo:
        verify_v04_bundle(doc)
    msg = str(excinfo.value)
    assert "digest mismatch" in msg
    # the failure is the digest, not (yet) the signature layer:
    assert "signature" not in msg.lower()


def test_round_trip_parse_then_verify_on_disk(tmp_path: Path) -> None:
    doc = make_v04_bundle()
    p = tmp_path / "v04.json"
    p.write_text(json.dumps(doc, indent=2, sort_keys=True), encoding="utf-8")
    raw = json.loads(p.read_text(encoding="utf-8"))
    parsed = parse_v04_bundle(raw)
    report = verify_v04_bundle(raw)
    assert report["valid"] is True
    assert parsed.digest == report["digest"]
