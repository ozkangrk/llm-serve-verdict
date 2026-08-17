"""Canonical JSON serialization and tamper-evident digests (TDD: RED first)."""
from __future__ import annotations

import json

import pytest

from serving_verdict.canonical import (
    canonical_json_bytes,
    compute_bundle_digest,
    compute_bundle_digest_from_bytes,
    digest_payload,
    payload_without_volatile,
)
from serving_verdict.errors import CanonicalizationError


def test_canonical_json_sorted_keys_compact() -> None:
    raw = b'{"b": 1, "a": [1, 2, {"y": 0, "x": 1}]}'
    out = canonical_json_bytes(raw)
    assert out == b'{"a":[1,2,{"x":1,"y":0}],"b":1}'


def test_canonical_json_deterministic_across_key_orders() -> None:
    a = canonical_json_bytes(b'{"z": 1, "a": {"m": 3, "b": 2}}')
    b = canonical_json_bytes(b'{"a": {"b": 2, "m": 3}, "z": 1}')
    assert a == b


def test_canonical_json_lists_preserve_order() -> None:
    a = canonical_json_bytes(b'{"k": [3, 1, 2]}')
    b = canonical_json_bytes(b'{"k": [1, 3, 2]}')
    assert a != b
    assert a == b'{"k":[3,1,2]}'


def test_canonical_json_ensure_ascii() -> None:
    out = canonical_json_bytes('{"k": "çü"}'.encode())
    assert b"\\u" in out


def test_canonical_json_rejects_nan() -> None:
    with pytest.raises(CanonicalizationError):
        canonical_json_bytes(b'{"k": NaN}')


def test_canonical_json_rejects_infinity() -> None:
    with pytest.raises(CanonicalizationError):
        canonical_json_bytes(b'{"k": Infinity}')
    with pytest.raises(CanonicalizationError):
        canonical_json_bytes(b'{"k": -Infinity}')


def test_canonical_json_rejects_invalid_json() -> None:
    with pytest.raises(CanonicalizationError):
        canonical_json_bytes(b'{"k": }')


def test_canonical_json_rejects_nested_nan() -> None:
    with pytest.raises(CanonicalizationError):
        canonical_json_bytes(b'{"a": {"b": [1, NaN]}}')


def test_payload_excludes_volatile_fields() -> None:
    doc = {"case_id": "x", "created_at": "t1", "bundle_digest": "d", "verdict": "PROMOTE"}
    payload = payload_without_volatile(doc)
    assert "created_at" not in payload
    assert "bundle_digest" not in payload
    assert payload["verdict"] == "PROMOTE"


def test_digest_stable_across_created_at_and_key_order() -> None:
    doc1 = {"case_id": "x", "created_at": "t1", "verdict": "PROMOTE"}
    doc2 = {"verdict": "PROMOTE", "case_id": "x", "created_at": "2026-01-01T00:00:00Z"}
    assert compute_bundle_digest(doc1) == compute_bundle_digest(doc2)
    assert compute_bundle_digest(doc1).startswith("sha256:")
    assert len(compute_bundle_digest(doc1)) == len("sha256:") + 64


def test_substantive_mutation_changes_digest() -> None:
    doc = {"case_id": "x", "verdict": "PROMOTE", "comparisons": []}
    mutated = {"case_id": "x", "verdict": "REJECT", "comparisons": []}
    assert compute_bundle_digest(doc) != compute_bundle_digest(mutated)


def test_list_order_mutation_changes_digest() -> None:
    doc = {"a": [1, 2]}
    mutated = {"a": [2, 1]}
    assert compute_bundle_digest(doc) != compute_bundle_digest(mutated)


def test_round_trip_recompute_from_bytes() -> None:
    doc = {"case_id": "x", "verdict": "PROMOTE", "gates": [{"id": "g", "status": "pass"}]}
    digest = compute_bundle_digest(doc)
    blob = canonical_json_bytes(json.dumps(doc).encode("utf-8"))
    assert digest_payload(blob) == digest


def test_digest_rejects_nan_bytes() -> None:
    with pytest.raises(CanonicalizationError):
        compute_bundle_digest_from_bytes(b'{"k": NaN}')
