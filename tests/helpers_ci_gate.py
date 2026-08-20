"""Shared builders for CI gate tests (v0.1 bundles; v0.4 via helpers_v04_bundle)."""
from __future__ import annotations

from typing import Any

from serving_verdict.canonical import compute_bundle_digest


def make_v01_bundle(verdict: str = "PROMOTE", case_id: str = "fixture-v01") -> dict[str, Any]:
    """A minimal valid v0.1 bundle document (digest-sealed)."""
    doc: dict[str, Any] = {
        "schema_version": "serving-verdict.bundle.v0.1",
        "case_id": case_id,
        "verdict": verdict,
        "reason_codes": ["PRIMARY_EFFECT_PASSED", "ALL_REQUIRED_GATES_PASSED"],
        "baseline": {"artifact_id": "baseline.json", "sha256": "a" * 64},
        "candidate": {"artifact_id": "candidate.json", "sha256": "b" * 64},
        "comparisons": [
            {
                "metric": "decode_tokens_per_s",
                "baseline_value": 1.0,
                "candidate_value": 1.3,
                "relative_delta": 0.3,
                "direction": "higher_better",
            }
        ],
        "gates": [{"id": "request_success", "status": "pass"}],
        "claim_boundary": "unit-test bundle",
        "created_at": "2026-08-01T11:00:00+00:00",
    }
    doc["bundle_digest"] = compute_bundle_digest(doc)
    return doc
