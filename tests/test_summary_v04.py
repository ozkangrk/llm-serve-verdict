"""Summary schema: parse/seal/verify of normalized benchmark summaries."""
from __future__ import annotations

from typing import Any

import pytest

from serving_verdict.errors import SummaryIntegrityError
from serving_verdict.summary import (
    MEASUREMENT_KEYS,
    UNMEASURABLE,
    parse_summary_payload,
)
from tests.helpers_v04 import make_summary, seal


def test_parse_valid_roundtrip():
    doc = make_summary()
    base = parse_summary_payload(doc)
    assert base.context == {
        "workload": doc["workload"],
        "model": doc["model"],
        "protocol": doc["protocol"],
    }
    assert base.usage == {"requests": 300, "tokens_in": 410000, "tokens_out": 92000}
    assert base.measurements["quality_score"] == 0.97
    assert base.digest.startswith("sha256:")
    # Deterministic: same payload -> same parsed value.
    again = parse_summary_payload(make_summary())
    assert base == again


def test_parse_rejects_unknown_schema_version():
    doc = make_summary()
    doc["schema_version"] = "serving-verdict.summary.v9.9"
    with pytest.raises(SummaryIntegrityError):
        parse_summary_payload(doc)


@pytest.mark.parametrize(
    "drop",
    [
        "schema_version",
        "workload",
        "model",
        "protocol",
        "usage",
        "measurements",
        "digest",
    ],
)
def test_parse_rejects_missing_top_level_key(drop: str):
    doc = make_summary()
    del doc[drop]
    with pytest.raises(SummaryIntegrityError):
        parse_summary_payload(doc)


def test_parse_rejects_empty_model_id():
    doc = make_summary()
    doc["model"]["id"] = "  "
    with pytest.raises(SummaryIntegrityError):
        parse_summary_payload(doc)


def test_parse_rejects_missing_protocol_version():
    doc = make_summary()
    del doc["protocol"]["version"]
    with pytest.raises(SummaryIntegrityError):
        parse_summary_payload(doc)


def test_parse_rejects_missing_usage():
    doc = make_summary()
    del doc["usage"]
    with pytest.raises(SummaryIntegrityError):
        parse_summary_payload(doc)


def test_parse_rejects_non_dict_usage():
    doc = make_summary()
    doc["usage"] = "requests=300"
    with pytest.raises(SummaryIntegrityError):
        parse_summary_payload(doc)


def test_parse_rejects_negative_usage_value():
    doc = make_summary()
    doc["usage"]["requests"] = -1
    with pytest.raises(SummaryIntegrityError):
        parse_summary_payload(doc)


def test_parse_rejects_boolean_usage_value():
    doc = make_summary()
    doc["usage"]["requests"] = True
    with pytest.raises(SummaryIntegrityError):
        parse_summary_payload(doc)


def test_parse_rejects_unmeasurable_usage_value():
    doc = make_summary()
    doc["usage"]["requests"] = UNMEASURABLE
    with pytest.raises(SummaryIntegrityError):
        parse_summary_payload(doc)


@pytest.mark.parametrize("key", list(MEASUREMENT_KEYS))
def test_parse_rejects_unknown_measurement_key(key: str):
    doc = make_summary()
    doc["measurements"][key + "_x"] = 1.0
    with pytest.raises(SummaryIntegrityError):
        parse_summary_payload(doc)


def test_parse_rejects_extra_measurement_key():
    doc = make_summary()
    doc["measurements"]["extra_metric"] = 1.0
    with pytest.raises(SummaryIntegrityError):
        parse_summary_payload(doc)


def test_parse_rejects_missing_required_measurement():
    doc = make_summary()
    del doc["measurements"]["ttft_s"]
    with pytest.raises(SummaryIntegrityError):
        parse_summary_payload(doc)


@pytest.mark.parametrize("value", [None, True, "0.5", -1.0, "banana", [1]])
def test_parse_rejects_bad_measurement_value(value: Any):
    doc = make_summary()
    doc["measurements"]["ttft_s"] = value
    with pytest.raises(SummaryIntegrityError):
        parse_summary_payload(doc)


def test_parse_rejects_nan_measurement():
    doc = make_summary()
    doc["measurements"]["ttft_s"] = float("nan")
    with pytest.raises(SummaryIntegrityError):
        parse_summary_payload(doc)


def test_parse_rejects_infinity_measurement():
    doc = make_summary()
    doc["measurements"]["ttft_s"] = float("inf")
    with pytest.raises(SummaryIntegrityError):
        parse_summary_payload(doc)


def test_parse_accepts_unmeasurable_measurement():
    doc = make_summary()
    doc["measurements"]["decode_latency_ms"] = UNMEASURABLE
    seal(doc)
    parsed = parse_summary_payload(doc)
    assert parsed.measurements["decode_latency_ms"] == UNMEASURABLE


def test_parse_rejects_tamper_marker():
    doc = make_summary()
    doc["tamper_marker"] = True
    with pytest.raises(SummaryIntegrityError):
        parse_summary_payload(doc)


def test_parse_rejects_unknown_top_level_key():
    doc = make_summary()
    doc["operator_note"] = "fast!"
    with pytest.raises(SummaryIntegrityError):
        parse_summary_payload(doc)


def test_parse_rejects_digest_mismatch():
    doc = make_summary()
    doc["measurements"]["ttft_s"] = 0.1  # value flipped after sealing
    with pytest.raises(SummaryIntegrityError):
        parse_summary_payload(doc)


def test_digest_recompute_after_value_change():
    from serving_verdict.canonical import canonicalize as _c
    from serving_verdict.canonical import digest_payload

    doc = make_summary()
    doc["measurements"]["ttft_s"] = 0.5
    payload = {k: v for k, v in doc.items() if k != "digest"}
    doc["digest"] = digest_payload(_c(payload))
    assert parse_summary_payload(doc).measurements["ttft_s"] == 0.5
