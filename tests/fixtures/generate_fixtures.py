#!/usr/bin/env python3
"""Generate the minimized test fixture case trees with self-consistent hashes.

Run from the repository root. These fixtures are committed as-is; the case
configs bind the exact SHA-256 of the committed fixture artifacts.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DSKAB = "qwen38.dspark-ab.v1"
SGLANG = "qwen38.sglang-vllm-ab.v1"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write(path: Path, doc: dict) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    return sha(path)


def dspark_side(
    engine: str,
    model: str,
    decode: float,
    e2e: float,
    ttft: float,
    latency: float,
    agg: float,
    wall: float,
) -> dict:
    serial = {
        "edit_cold": {
            "requests": [
                {
                    "prompt_tokens": 2665,
                    "completion_tokens": 1200,
                    "latency_s": latency,
                    "ttft_s": ttft,
                    "decode_tokens_per_s": decode,
                    "e2e_output_tokens_per_s": e2e,
                    "finish_reason": "length",
                    "gpu": {"samples": 1},
                }
            ],
            "median_decode_tokens_per_s": decode,
            "median_e2e_output_tokens_per_s": e2e,
            "median_ttft_s": ttft,
            "median_latency_s": latency,
        },
        "concurrency3_edit_cold": {
            "groups": [
                {
                    "wall_s": wall,
                    "total_completion_tokens": 3600,
                    "aggregate_output_tokens_per_s": agg,
                    "requests": [
                        {
                            "prompt_tokens": 2665,
                            "completion_tokens": 1200,
                            "latency_s": wall,
                            "ttft_s": ttft * 4,
                            "decode_tokens_per_s": decode * 0.85,
                            "e2e_output_tokens_per_s": e2e * 0.75,
                            "finish_reason": "length",
                        }
                    ],
                }
            ],
            "median_aggregate_output_tokens_per_s": agg,
            "median_wall_s": wall,
        },
    }
    return {
        "schema_version": DSKAB,
        "created_at": "2026-08-17T08:00:00+00:00",
        "engine": engine,
        "profile": "fixture-minimized",
        "base_url": "http://127.0.0.1:8889/v1",
        "model": model,
        "repeats": 1,
        "results": serial,
        "claim_boundary": "Minimized unit-test fixture, not real measurement.",
    }


def sglang_side(engine: str, model: str, decode: float, ttft: float, agg: float, wall: float) -> dict:
    return {
        "schema_version": SGLANG,
        "created_at": "2026-08-16T23:00:00+00:00",
        "engine": engine,
        "profile": "fixture-minimized",
        "base_url": "http://127.0.0.1:30000/v1",
        "model": model,
        "seed": 20260817,
        "repeats": 1,
        "results": {
            "short_decode_512": {
                "requests": [
                    {
                        "prompt_tokens": 38,
                        "completion_tokens": 512,
                        "latency_s": 14.4,
                        "ttft_s": ttft,
                        "decode_tokens_per_s": decode,
                        "e2e_output_tokens_per_s": decode - 0.5,
                        "finish_reason": "length",
                    }
                ],
                "median_decode_tokens_per_s": decode,
                "median_ttft_s": ttft,
                "median_latency_s": 14.4,
            },
            "concurrency4_short_256": {
                "groups": [
                    {
                        "wall_s": wall,
                        "total_completion_tokens": 2048,
                        "aggregate_output_tokens_per_s": agg,
                        "requests": [
                            {
                                "prompt_tokens": 38,
                                "completion_tokens": 512,
                                "latency_s": wall,
                                "ttft_s": ttft,
                                "decode_tokens_per_s": decode,
                                "e2e_output_tokens_per_s": decode - 0.5,
                                "finish_reason": "length",
                            }
                        ],
                    }
                ],
                "median_aggregate_output_tokens_per_s": agg,
                "median_wall_s": wall,
            },
        },
        "claim_boundary": "Minimized unit-test fixture, not real measurement.",
    }


def main() -> int:
    # ---- DSpark minimized case: expect PROMOTE ----
    ds = ROOT / "tests" / "fixtures" / "dspark"
    base_sha = write(
        ds / "baseline_mtp2.json",
        dspark_side("vllm-mtp2", "fixture-mtp2", 25.62, 24.94, 1.317, 48.11, 65.64, 54.85),
    )
    cand_sha = write(
        ds / "candidate_k7.json",
        dspark_side("vllm-dspark-k7", "fixture-dspark-k7", 63.27, 59.41, 1.272, 20.20, 132.83, 27.10),
    )
    report_text = (
        "# Minimized DSpark gates (operator attestation fixture)\n\n"
        "- arithmetic: `37 x 41 = 1517` — pass\n"
        "- tool_call: `get_weather({\"city\":\"Istanbul\"})` — pass\n"
        "- process_stability: restarts=0, oom_killed=false — pass\n"
        "- rollback: rollback container preserved and exercised — pass\n"
    )
    (ds / "DSPARK_REPORT.md").write_text(report_text, encoding="utf-8")
    report_sha = sha(ds / "DSPARK_REPORT.md")
    case = f"""schema_version: "serving-verdict.case.v0.1"
id: fixture-dspark
source_root: .
baseline:
  artifact: baseline_mtp2.json
  sha256: "{base_sha}"
candidate:
  artifact: candidate_k7.json
  sha256: "{cand_sha}"
policy:
  primary_metric: decode_tokens_per_s
  workload: edit_cold
  min_relative_improvement: 0.15
  max_ttft_regression: 0.10
  required_gates:
    - request_success
    - arithmetic
    - tool_call
    - process_stability
    - rollback
supplemental_evidence:
  - id: arithmetic
    kind: operator_attested
    status: pass
    source: DSPARK_REPORT.md
    sha256: "{report_sha}"
  - id: tool_call
    kind: operator_attested
    status: pass
    source: DSPARK_REPORT.md
    sha256: "{report_sha}"
  - id: process_stability
    kind: operator_attested
    status: pass
    source: DSPARK_REPORT.md
    sha256: "{report_sha}"
  - id: rollback
    kind: operator_attested
    status: pass
    source: DSPARK_REPORT.md
    sha256: "{report_sha}"
claim_boundary: "Minimized unit-test fixture; deterministic decision check only, not a real measurement."
"""
    (ds / "case.yaml").write_text(case, encoding="utf-8")

    # ---- SGLang minimized case: expect REJECT (hard gate fail) ----
    sg = ROOT / "tests" / "fixtures" / "sglang"
    vllm_sha = write(
        sg / "baseline_vllm.json",
        sglang_side("vllm", "fixture-vllm", 26.28, 0.264, 91.86, 11.15),
    )
    mtp_sha = write(
        sg / "candidate_sglang_eagle.json",
        sglang_side("sglang-mtp", "fixture-sglang-eagle", 36.10, 0.219, 96.84, 10.57),
    )
    sglang_report = (
        "# Minimized SGLang production replay (operator attestation fixture)\n\n"
        "- request_success: synthetic suite finished at token budget — pass\n"
        "- process_stability: production replay failed. Two concurrent delegation "
        "requests triggered HTTP 500 followed by the engine SIGQUIT handler "
        "killing the process tree; Docker recorded exit and auto-restart. — fail\n"
        "- rollback: production backend rolled back to the incumbent vLLM profile — pass\n"
    )
    (sg / "REPORT.md").write_text(sglang_report, encoding="utf-8")
    sg_report_sha = sha(sg / "REPORT.md")
    sgcase = f"""schema_version: "serving-verdict.case.v0.1"
id: fixture-sglang
source_root: .
baseline:
  artifact: baseline_vllm.json
  sha256: "{vllm_sha}"
candidate:
  artifact: candidate_sglang_eagle.json
  sha256: "{mtp_sha}"
policy:
  primary_metric: decode_tokens_per_s
  workload: short_decode_512
  min_relative_improvement: 0.15
  max_ttft_regression: 0.10
  required_gates:
    - request_success
    - process_stability
    - rollback
supplemental_evidence:
  - id: process_stability
    kind: operator_attested
    status: fail
    source: REPORT.md
    sha256: "{sg_report_sha}"
  - id: rollback
    kind: operator_attested
    status: pass
    source: REPORT.md
    sha256: "{sg_report_sha}"
claim_boundary: "Minimized unit-test fixture; synthetic throughput win overridden by a failed production process-stability gate."
"""
    (sg / "case.yaml").write_text(sgcase, encoding="utf-8")
    print(f"dspark base={base_sha[:12]} cand={cand_sha[:12]} report={report_sha[:12]}")
    print(f"sglang vllm={vllm_sha[:12]} mtp={mtp_sha[:12]} report={sg_report_sha[:12]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
