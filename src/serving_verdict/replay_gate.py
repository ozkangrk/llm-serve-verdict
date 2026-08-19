"""CI replay regression gate: baseline vs candidate decision contract.

- Requires comparable runs: identical workload hash, identical protocol
  (workload/sample identity) — otherwise INCONCLUSIVE.
- Verifies both artifact digests first (fail-closed IntegrityError).
- Hard gates on the candidate: request success, tool success, quality.
- Direction-aware regression/improvement thresholds with a numeric tolerance.
- Machine exit semantics: PASS/FAIL/INCONCLUSIVE -> exit 0/1/0.
- Canonical digest over the summary document + concise GitHub summary payload.
- No raw content is ever present: the gate operates purely on artifact
  fingerprints, hashes and measurements.
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from typing import Any

from serving_verdict.canonical import canonicalize
from serving_verdict.errors import GateError
from serving_verdict.replay import PROTOCOL_VERSION, ReplayArtifact, verify_artifact

DEFAULT_TOLERANCE: float = 1e-9
_MIN_POSITIVE_BASE: float = 0.01  # skip relative deltas when |base| < this

_MIN_SUCCESS_RATE: float = 0.99
_MIN_TOOL_RATE: float = 0.95
_MIN_QUALITY: float = 0.90

_SUMMARY_MAX_LINES = 15
_DIGEST_KEY: bytes = b"serving-verdict.replay-gate.summary.v1"


def canonical_digest(doc: dict[str, Any]) -> str:
    """Keyed canonical digest of a summary document."""
    payload = {key: value for key, value in doc.items() if key != "digest"}
    return "sha256:" + hashlib.sha256(
        _DIGEST_KEY + b"\x00" + canonicalize(payload)
    ).hexdigest()


class GateDecision:
    PASS = "PASS"
    FAIL = "FAIL"
    INCONCLUSIVE = "INCONCLUSIVE"


@dataclass(frozen=True)
class GateThreshold:
    """Direction-aware threshold bounds, each within [0, 1]."""

    regression: float = 0.10
    improvement: float = 0.20

    def __post_init__(self) -> None:
        for name in ("regression", "improvement"):
            v = getattr(self, name)
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
                raise GateError(f"{name} must be a finite number")
            if v < 0.0 or v > 1.0:
                raise GateError(f"{name} must be within [0, 1]")


DEFAULT_THRESHOLDS: dict[str, GateThreshold] = {
    "ttft_s": GateThreshold(regression=0.10, improvement=0.20),
    "decode_tokens_per_s": GateThreshold(regression=0.05, improvement=0.50),
    "e2e_tokens_per_s": GateThreshold(regression=0.05, improvement=0.50),
}


@dataclass(frozen=True)
class GateResult:
    decision: str
    exit_code: int
    reason_codes: tuple[str, ...]
    metrics: dict[str, float] = field(default_factory=dict)
    workload_hash: str = ""
    protocol_hash: str = ""
    digest: str = ""
    summary_lines: tuple[str, ...] = ()
    summary_payload: dict[str, Any] = field(default_factory=dict)

    def __repr__(self) -> str:
        return (
            f"GateResult(decision={self.decision!r}, exit_code={self.exit_code}, "
            f"reasons={list(self.reason_codes)}, digest={self.digest!r})"
        )


def _median(values: list[float]) -> float:
    s = sorted(values)
    n = len(s)
    mid = n // 2
    if n % 2:
        return float(s[mid])
    return (s[mid - 1] + s[mid]) / 2.0


def _aggregate(payload: dict[str, Any]) -> dict[str, float | None]:
    entries = payload["entries"]
    total = len(entries)
    succeeded = sum(1 for e in entries if e["status"] == "succeeded")
    tool_checked = [e for e in entries if e.get("tool_status") is not None]
    tool_ok = sum(1 for e in tool_checked if e.get("tool_status") in ("ok", "skipped"))
    quality_vals: list[float] = []
    for e in entries:
        if e.get("status") == "succeeded":
            pr = e.get("quality", {}).get("pass_rate")
            if pr is not None:
                quality_vals.append(float(pr))
    usage = [e for e in entries if e.get("usage_present")]
    agg: dict[str, float | None] = {
        "request_success_rate": succeeded / total if total else None,
        "tool_success_rate": (tool_ok / len(tool_checked)) if tool_checked else None,
        "quality_pass_rate_min": min(quality_vals) if quality_vals else None,
        "entries": float(total),
        "completed": float(succeeded),
        "usage_present": float(len(usage)),
        "prompt_tokens_total": float(
            sum(int(e.get("prompt_tokens") or 0) for e in usage)
        ),
    }
    for key in ("ttft_s", "decode_tokens_per_s", "e2e_tokens_per_s"):
        vals = [
            float(e[key])
            for e in entries
            if e.get("status") == "succeeded" and e.get(key) is not None
        ]
        agg[key] = _median(vals) if vals else None
    return agg


class ReplayGate:
    """Evaluates a candidate replay artifact against a baseline artifact."""

    def __init__(
        self,
        thresholds: dict[str, GateThreshold] | None = None,
        min_success_rate: float = _MIN_SUCCESS_RATE,
        min_tool_rate: float = _MIN_TOOL_RATE,
        min_quality: float = _MIN_QUALITY,
        tolerance: float = DEFAULT_TOLERANCE,
    ) -> None:
        self.thresholds = dict(DEFAULT_THRESHOLDS)
        if thresholds:
            for key, value in thresholds.items():
                if key not in DEFAULT_THRESHOLDS:
                    raise GateError(f"unknown metric threshold: {key}")
                if not isinstance(value, GateThreshold):
                    raise GateError("thresholds must be GateThreshold values")
                self.thresholds[key] = value
        for name in ("min_success_rate", "min_tool_rate", "min_quality", "tolerance"):
            v = locals()[name]
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
                raise GateError(f"{name} must be a finite number")
        if not (0.0 <= min_tool_rate <= 1.0 and 0.0 <= min_quality <= 1.0):
            raise GateError("rate bounds must be within [0, 1]")
        if not (min_success_rate > 0.0 and min_success_rate <= 1.0):
            raise GateError("min_success_rate must be within (0, 1]")
        if tolerance < 0.0:
            raise GateError("tolerance must be non-negative")
        self.min_success_rate = float(min_success_rate)
        self.min_tool_rate = float(min_tool_rate)
        self.min_quality = float(min_quality)
        self.tolerance = float(tolerance)

    # ------------------------------------------------------------------ #

    @staticmethod
    def _validated(payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise GateError("gate inputs must be artifact payload objects")
        if payload.get("schema_version") != ReplayArtifact.SCHEMA_VERSION:
            raise GateError("gate inputs must be replay artifact payloads")
        expected = payload.get("bundle_digest")
        if not isinstance(expected, str):
            raise GateError("artifact payload is missing its bundle_digest")
        verify_artifact(payload, expected)  # raises IntegrityError on tamper
        return payload

    def evaluate(self, *, baseline: Any, candidate: Any) -> GateResult:
        base = self._validated(baseline)
        cand = self._validated(candidate)

        workload_hash = str(base["workload_hash"])
        protocol_hash = str(base["protocol"]["protocol_hash"])
        agg_b = _aggregate(base)
        agg_c = _aggregate(cand)

        reasons: list[str] = []
        base_proto = base["protocol"]
        cand_proto = cand["protocol"]
        comparable = True
        if base_proto["version"] != PROTOCOL_VERSION or cand_proto["version"] != PROTOCOL_VERSION:
            reasons.append("protocol_version_mismatch")
            comparable = False
        if base_proto["protocol_hash"] != cand_proto["protocol_hash"]:
            reasons.append("protocol_mismatch")
            comparable = False
        if base["workload_hash"] != cand["workload_hash"]:
            reasons.append("workload_hash_mismatch")
            comparable = False
        if not comparable:
            return self._result(
                GateDecision.INCONCLUSIVE,
                reasons,
                agg_b,
                agg_c,
                workload_hash,
                protocol_hash,
                base,
                cand,
            )

        # usage parity: a *successful* request that lacks usage makes its
        # measurements unreliable -> inconclusive. Failed requests are handled
        # by the request-success hard gate, not here.
        b_missing = sum(
            1 for e in base["entries"] if e["status"] == "succeeded" and not e["usage_present"]
        )
        c_missing = sum(
            1 for e in cand["entries"] if e["status"] == "succeeded" and not e["usage_present"]
        )
        if b_missing or c_missing:
            reasons.append("usage_missing")
            return self._result(
                GateDecision.INCONCLUSIVE, reasons, agg_b, agg_c, workload_hash, protocol_hash, base, cand
            )

        # hard gates on the candidate
        hard_fail: list[str] = []
        if agg_c["request_success_rate"] is None or (
            agg_c["request_success_rate"] or 0.0
        ) < self.min_success_rate:
            hard_fail.append("request_success_below_min")
        if agg_c["tool_success_rate"] is not None and (
            agg_c["tool_success_rate"] or 0.0
        ) < self.min_tool_rate:
            hard_fail.append("tool_failure")
        if agg_c["quality_pass_rate_min"] is None or (
            agg_c["quality_pass_rate_min"] or 0.0
        ) < self.min_quality:
            hard_fail.append("quality_below_min")
        if hard_fail:
            return self._result(
                GateDecision.FAIL, hard_fail, agg_b, agg_c, workload_hash, protocol_hash, base, cand
            )

        # direction-aware threshold gates
        metric_reasons: list[str] = []
        for key, thr in self.thresholds.items():
            b_val = agg_b.get(key)
            c_val = agg_c.get(key)
            if b_val is None or c_val is None:
                continue
            base_abs = abs(b_val)
            if base_abs < _MIN_POSITIVE_BASE:
                # only absolute tolerance applies near zero
                if abs(c_val - b_val) > self.tolerance:
                    direction = "regression" if c_val > b_val else "improvement"
                    limit = thr.regression if direction == "regression" else thr.improvement
                    if abs(c_val - b_val) > limit * base_abs + self.tolerance:
                        metric_reasons.append(f"{key}_{direction}")
                continue
            delta = (c_val - b_val) / b_val
            # A delta at or within the floating-point epsilon of a threshold is
            # treated as exactly at the threshold (PASS).
            if delta - thr.regression > self.tolerance:
                metric_reasons.append(f"{key}_regression")
            elif delta + thr.improvement < -self.tolerance:
                metric_reasons.append(f"{key}_improvement")
        if metric_reasons:
            return self._result(
                GateDecision.FAIL, metric_reasons, agg_b, agg_c, workload_hash, protocol_hash, base, cand
            )

        return self._result(GateDecision.PASS, [], agg_b, agg_c, workload_hash, protocol_hash, base, cand)

    # ------------------------------------------------------------------ #

    def _result(
        self,
        decision: str,
        reasons: list[str],
        agg_b: dict[str, float | None],
        agg_c: dict[str, float | None],
        workload_hash: str,
        protocol_hash: str,
        base: dict[str, Any],
        cand: dict[str, Any],
    ) -> GateResult:
        exit_code = 1 if decision == GateDecision.FAIL else 0
        metrics: dict[str, float] = {}
        for k, v in agg_b.items():
            if v is not None:
                metrics[f"baseline_{k}"] = float(v)
        for k, v in agg_c.items():
            if v is not None:
                metrics[k] = float(v)
        lines = self._summary_lines(decision, reasons, agg_c, agg_b)
        body: dict[str, Any] = {
            "verdict": decision,
            "exit_code": exit_code,
            "reasons": list(reasons),
            "workload_hash": workload_hash,
            "protocol_hash": protocol_hash,
            "baseline_digest": base["bundle_digest"],
            "candidate_digest": cand["bundle_digest"],
            "metrics": metrics,
            "lines": lines,
        }
        digest = canonical_digest(body)
        body["digest"] = digest
        return GateResult(
            decision=decision,
            exit_code=exit_code,
            reason_codes=tuple(reasons),
            metrics=metrics,
            workload_hash=workload_hash,
            protocol_hash=protocol_hash,
            digest=digest,
            summary_lines=tuple(lines),
            summary_payload=body,
        )

    @staticmethod
    def _fmt(value: float | None) -> str:
        if value is None:
            return "n/a"
        return f"{value:.4g}"

    def _summary_lines(
        self,
        decision: str,
        reasons: list[str],
        agg_c: dict[str, float | None],
        agg_b: dict[str, float | None],
    ) -> list[str]:
        lines: list[str] = []
        lines.append(f"## Replay regression gate: {decision}")
        if reasons:
            lines.append("reasons: " + ", ".join(reasons))
        else:
            lines.append("reasons: none")
        for key in ("ttft_s", "decode_tokens_per_s", "e2e_tokens_per_s", "quality_pass_rate_min"):
            lines.append(
                f"- {key}: baseline {self._fmt(agg_b.get(key))} -> "
                f"candidate {self._fmt(agg_c.get(key))}"
            )
        lines.append(
            f"- request_success_rate: {self._fmt(agg_c.get('request_success_rate'))}"
        )
        lines.append(
            f"- tool_success_rate: {self._fmt(agg_c.get('tool_success_rate'))}"
        )
        if len(lines) > _SUMMARY_MAX_LINES:
            lines = lines[:_SUMMARY_MAX_LINES]
        return lines
