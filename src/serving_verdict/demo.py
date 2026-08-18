"""Portable in-package demo (v0.2).

``serving-verdict demo --out-dir DIR`` materializes a self-contained demo
tree with exactly two cases:

    DIR/
      demo-promote/
        case.yaml        # relative source_root: "evidence"
        evidence/        # deterministic, in-package-generated artifacts
          baseline.json
          candidate.json
          REPORT.md
        bundle.json      # pre-built, offline-verifiable bundle (PROMOTE)
      demo-reject/
        ...              # same shape; hard-gate failure -> REJECT

The demo is deterministic: artifact content is fixed (no timestamps, no
paths), so the bundle digests are identical no matter where the tree is
written. Everything is generated from this module — no external source
tree, no network, no subprocess.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from serving_verdict.canonical import compute_bundle_digest
from serving_verdict.errors import UsageError

DEMO_SCHEMA_VERSION = "serving-verdict.case.v0.1"
DEMO_CREATED_AT = "2026-01-01T00:00:00+00:00"  # fixed: demo bundles are deterministic
PROMOTE_DIR = "demo-promote"
REJECT_DIR = "demo-reject"


@dataclass(frozen=True)
class DemoSide:
    name: str
    decode: float
    e2e: float
    ttft: float
    latency: float


def _dspark_doc(name: str, side: DemoSide) -> dict[str, Any]:
    return {
        "schema_version": "qwen38.dspark-ab.v1",
        "created_at": DEMO_CREATED_AT,
        "engine": name,
        "profile": "demo-minimized",
        "base_url": "http://127.0.0.1:8889/v1",
        "model": f"demo-{name}",
        "repeats": 1,
        "results": {
            "edit_cold": {
                "requests": [
                    {
                        "prompt_tokens": 2665,
                        "completion_tokens": 1200,
                        "latency_s": side.latency,
                        "ttft_s": side.ttft,
                        "decode_tokens_per_s": side.decode,
                        "e2e_output_tokens_per_s": side.e2e,
                        "finish_reason": "length",
                        "gpu": {"samples": 1},
                    }
                ],
                "median_decode_tokens_per_s": side.decode,
                "median_e2e_output_tokens_per_s": side.e2e,
                "median_ttft_s": side.ttft,
                "median_latency_s": side.latency,
            }
        },
        "claim_boundary": "Deterministic in-package demo, not a real measurement.",
    }


def _write_json(path: Path, doc: dict[str, Any]) -> str:
    text = json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    path.write_text(text, encoding="utf-8")
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_demo(out_dir: Path) -> list[Path]:
    """Materialize the demo tree into ``out_dir`` (created if needed).

    Returns the two bundle paths written. The parent of ``out_dir`` must
    already exist (the CLI mirrors import-case's no-implicit-parent rule);
    the out dir itself is created by the demo command (it is the command's
    own deliverable).
    """
    out = Path(out_dir)
    if not out.parent.is_dir():
        raise UsageError(
            f"output directory parent does not exist: {out.parent} "
            "(create it first)"
        )
    out.mkdir(parents=True, exist_ok=True)
    bundles: list[Path] = []
    for sub in (PROMOTE_DIR, REJECT_DIR):
        nested = _build_one(out / sub)
        portable = out / f"{sub}.verdict.json"
        portable.write_bytes(nested.read_bytes())
        bundles.append(portable)
    return bundles


def _build_one(case_dir: Path) -> Path:
    evidence = case_dir / "evidence"
    evidence.mkdir(parents=True, exist_ok=True)
    if case_dir.name == PROMOTE_DIR:
        base_side = DemoSide("demo-incumbent", 25.62, 24.94, 1.317, 48.11)
        cand_side = DemoSide("demo-candidate", 63.27, 59.41, 1.272, 20.20)
        case_id = "demo-promote"
        report = "# Demo gates (operator attestation fixture)\n\n- arithmetic: pass\n- rollback: pass\n"
        attestation = [
            ("arithmetic", "pass"),
            ("rollback", "pass"),
        ]
        claim = "Deterministic in-package demo case: expected PROMOTE."
    else:
        base_side = DemoSide("demo-incumbent", 26.28, 25.78, 0.264, 14.40)
        cand_side = DemoSide("demo-candidate", 36.10, 35.60, 0.219, 14.40)
        case_id = "demo-reject"
        report = (
            "# Demo production replay (operator attestation fixture)\n\n"
            "- process_stability: engine process tree killed during concurrent "
            "replay; container auto-restarted. — fail\n"
            "- rollback: incumbent profile restored and exercised. — pass\n"
        )
        attestation = [
            ("process_stability", "fail"),
            ("rollback", "pass"),
        ]
        claim = "Deterministic in-package demo case: expected REJECT (hard gate failure)."

    base_sha = _write_json(evidence / "baseline.json", _dspark_doc(base_side.name, base_side))
    cand_sha = _write_json(evidence / "candidate.json", _dspark_doc(cand_side.name, cand_side))
    report_path = evidence / "REPORT.md"
    report_path.write_text(report, encoding="utf-8")
    import hashlib

    report_sha = hashlib.sha256(report_path.read_bytes()).hexdigest()

    case_doc: dict[str, Any] = {
        "schema_version": DEMO_SCHEMA_VERSION,
        "id": case_id,
        "source_root": "evidence",
        "baseline": {"artifact": "baseline.json", "sha256": base_sha},
        "candidate": {"artifact": "candidate.json", "sha256": cand_sha},
        "policy": {
            "primary_metric": "decode_tokens_per_s",
            "workload": "edit_cold",
            "min_relative_improvement": 0.15,
            "max_ttft_regression": 0.10,
            "required_gates": ["request_success", "arithmetic", "rollback"]
            if case_dir.name == PROMOTE_DIR
            else ["request_success", "process_stability", "rollback"],
        },
        "supplemental_evidence": [
            {
                "id": gate,
                "kind": "operator_attested",
                "status": status,
                "source": "REPORT.md",
                "sha256": report_sha,
            }
            for gate, status in attestation
        ],
        "claim_boundary": claim,
    }
    case_path = case_dir / "case.yaml"
    case_path.write_text(yaml.safe_dump(case_doc, sort_keys=False), encoding="utf-8")

    # Build the bundle through the real engine (relative root resolution +
    # verify happens here); created_at is normalized afterwards so the
    # committed demo bundle is byte-stable across runs and locations.
    from serving_verdict.engine import import_case

    bundle = import_case(case_path)
    bundle["created_at"] = DEMO_CREATED_AT
    payload = {k: v for k, v in bundle.items() if k != "bundle_digest"}
    payload["bundle_digest"] = compute_bundle_digest(payload)
    bundle_path = case_dir / "bundle.json"
    bundle_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return bundle_path
