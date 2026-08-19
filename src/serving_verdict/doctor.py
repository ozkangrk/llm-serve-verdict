"""Serving Doctor: rule-based diagnostics + machine-readable artifacts.

The doctor is the v0.3 "first vertical slice": it takes a read-only
hardware snapshot (``hwprobe``), a typed model geometry (``capacity``),
the operator's context/output budget and concurrency target, and optional
endpoint capabilities, and produces ONE machine JSON artifact:

- exact capacity math and FIT / RISK / NO_FIT classification;
- rule-based findings from a CLOSED set of rules (no LLM opinion, no free
  text, no invented metrics);
- explicit assumptions and uncertainty notes;
- a deterministic ``report_digest`` (canonical JSON, volatile
  ``generated_at`` and the digest itself excluded);
- a sanitized view of the hardware snapshot: probe failures are fixed
  category/detail pairs, and the whole payload is scrubbed for host paths
  and secret patterns as a final redaction pass.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from serving_verdict import canonical
from serving_verdict.capacity import ModelGeometry, plan_capacity
from serving_verdict.hwprobe import HardwareSnapshot

DOCTOR_SCHEMA = "serving-verdict.serving-doctor.v1"

#: The closed rule set. Anything not in this set is not a finding: the
#: doctor never emits free-form/LLM-generated opinions.
KNOWN_FINDING_RULES: frozenset[str] = frozenset(
    {
        "context_too_high",
        "weights_exceed_memory",
        "concurrency_infeasible",
        "kv_pressure",
        "missing_endpoint_capabilities",
    }
)

#: Benchmarks that depend on stream/usage capability; missing capability
#: makes them UNMEASURABLE (spec: never an invented number).
_STREAM_METRICS: tuple[str, ...] = ("ttft_s", "decode_tokens_per_s")
_USAGE_METRICS: tuple[str, ...] = ("e2e_output_tokens_per_s", "aggregate_output_tokens_per_s")

_SEVERITY_ERROR = "error"
_SEVERITY_WARNING = "warning"

# -- redaction: host paths -----------------------------------------------------

_ABS_PATH_RE = re.compile(r"(?<![\w./-])(/[\w.+-]+){2,}")
_REL_PATH_RE = re.compile(r"(?<![\w.-])(?:\.{1,2}/[\w.+-]+)+")
_WINDOWS_PATH_RE = re.compile(r"[A-Za-z]:\\(?:[\w.-]+\\)*[\w.-]+")
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]{8,}")
_SK_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b")
# A leaked credential/blob: 32+ contiguous alphanumerics that mix case AND
# digit. Deliberately contiguous (no dot/hyphen/underscore inside) so
# legitimate identifiers -- schema versions, rule IDs, metric names -- never
# match. This is a final defensive pass; the primary safety property is that
# raw probe output is never carried into the artifact at all.
_LEAKED_KEY_RE = re.compile(r"\b[A-Za-z0-9]{32,}\b")


def _is_leaked_key(token: str) -> bool:
    has_lower = any(c.islower() for c in token)
    has_upper = any(c.isupper() for c in token)
    has_digit = any(c.isdigit() for c in token)
    return sum((has_lower, has_upper, has_digit)) >= 2


def redact_text(text: str) -> str:
    """Best-effort scrubbing of host paths and secret-looking material.

    This is a *redaction* layer, not a guarantee: the primary safety property
    is that probe output is never carried into the artifact at all
    (``hwprobe`` sanitizes errors into fixed category/detail strings).
    ``redact_text`` is applied as a final pass over every string field of the
    report to catch host paths and leaked tokens in operator-supplied text.
    """
    out = _ABS_PATH_RE.sub("***", text)
    out = _REL_PATH_RE.sub("***", out)
    out = _WINDOWS_PATH_RE.sub("***", out)
    out = _BEARER_RE.sub("Bearer ***", out)
    out = _SK_RE.sub("***", out)
    out = _LEAKED_KEY_RE.sub(
        lambda m: "***" if _is_leaked_key(m.group(0)) else m.group(0), out
    )
    return out


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return [_redact_value(v) for v in value]
    if isinstance(value, dict):
        return {k: _redact_value(v) for k, v in value.items()}
    return value


@dataclass(frozen=True, slots=True)
class EndpointCapabilities:
    """Server-attested endpoint capabilities relevant to doctoring.

    Defaults to "unknown" (both False) so a doctor run *without* capability
    evidence still emits the ``missing_endpoint_capabilities`` finding
    instead of silently assuming a fully capable endpoint.
    """

    streaming: bool = False
    usage_reporting: bool = False

    def missing(self) -> tuple[str, ...]:
        missing: list[str] = []
        if not self.streaming:
            missing.append("streaming")
        if not self.usage_reporting:
            missing.append("usage_reporting")
        return tuple(missing)

    def to_dict(self) -> dict[str, object]:
        return {
            "streaming": self.streaming,
            "usage_reporting": self.usage_reporting,
            "unmeasurable_metrics": list(_STREAM_METRICS + _USAGE_METRICS),
        }


@dataclass(frozen=True, slots=True)
class DoctorInput:
    """All operator-supplied inputs for one doctor run.

    Every value is validated: invalid values raise
    :class:`CapacityInputError` and no artifact is produced (fail-closed).
    """

    snapshot: HardwareSnapshot
    geometry: ModelGeometry
    memory_total_bytes: int
    context_tokens: int
    max_output_tokens: int
    target_concurrency: int
    reserve_bytes: int
    capabilities: EndpointCapabilities


def _finding(rule_id: str, severity: str, detail: str) -> dict[str, str]:
    if rule_id not in KNOWN_FINDING_RULES:
        raise AssertionError(f"unknown finding rule: {rule_id!r}")
    return {"rule_id": rule_id, "severity": severity, "detail": detail}


def _rule_findings(
    *,
    geometry: ModelGeometry,
    memory_total_bytes: int,
    context_tokens: int,
    plan_classification: str,
    target_concurrency: int,
    safe_max_concurrency: int,
    utilization: float,
    capabilities: EndpointCapabilities,
    weights_bytes: int,
) -> list[dict[str, str]]:
    """Evaluate the closed rule set. Deterministic; no LLM; no free text."""
    findings: list[dict[str, str]] = []

    if context_tokens > geometry.max_context_tokens:
        findings.append(
            _finding(
                "context_too_high",
                _SEVERITY_ERROR,
                f"Requested context budget {context_tokens} tokens exceeds the model's "
                f"maximum context of {geometry.max_context_tokens} tokens; requests "
                f"will be truncated or rejected.",
            )
        )

    if weights_bytes > memory_total_bytes:
        findings.append(
            _finding(
                "weights_exceed_memory",
                _SEVERITY_ERROR,
                f"Exact weight footprint {weights_bytes} bytes exceeds the assumed "
                f"serving pool of {memory_total_bytes} bytes; the model does not fit "
                f"without quantization or offloading that were not declared.",
            )
        )

    if plan_classification == "NO_FIT":
        findings.append(
            _finding(
                "concurrency_infeasible",
                _SEVERITY_ERROR,
                f"Target concurrency {target_concurrency} is infeasible: the exact "
                f"memory math allows at most {safe_max_concurrency} simultaneous "
                f"request(s) at this context/output budget.",
            )
        )
    elif plan_classification == "RISK":
        pct = utilization * 100.0
        findings.append(
            _finding(
                "kv_pressure",
                _SEVERITY_WARNING,
                f"KV-cache pressure: the target fits only with {pct:.1f}% pool "
                f"utilization (weights + reserve + target KV), above the {0.85:.2f} "
                f"safe bound; headroom for prefill spikes and fragmentation is "
                f"insufficient.",
            )
        )

    missing = capabilities.missing()
    if missing:
        findings.append(
            _finding(
                "missing_endpoint_capabilities",
                _SEVERITY_WARNING,
                "Endpoint lacks required capabilities: "
                + ", ".join(missing)
                + ". Affected benchmark metrics would be UNMEASURABLE (never "
                + "estimated): "
                + ", ".join(_STREAM_METRICS + _USAGE_METRICS)
                + ".",
            )
        )

    return findings


def run_doctor(
    input: DoctorInput,
    *,
    generated_at: str,
) -> dict[str, Any]:
    """Produce the serving-doctor machine artifact.

    ``generated_at`` is injected (never wall-clock here) and is volatile:
    the ``report_digest`` is computed over the canonical payload without
    ``generated_at`` and ``report_digest``, so the digest is deterministic
    for identical doctor inputs.
    """
    plan = plan_capacity(
        input.geometry,
        input.memory_total_bytes,
        input.reserve_bytes,
        input.context_tokens,
        input.max_output_tokens,
        input.target_concurrency,
        uncertainty_notes=input.snapshot.uncertainty_notes,
    )
    findings = _rule_findings(
        geometry=input.geometry,
        memory_total_bytes=input.memory_total_bytes,
        context_tokens=input.context_tokens,
        plan_classification=plan.classification,
        target_concurrency=input.target_concurrency,
        safe_max_concurrency=plan.safe_max_concurrency,
        utilization=plan.utilization,
        capabilities=input.capabilities,
        weights_bytes=plan.weights_bytes,
    )

    payload: dict[str, Any] = {
        "schema_version": DOCTOR_SCHEMA,
        "generated_at": generated_at,
        "hardware": input.snapshot.to_dict(),
        "model": {
            "name": input.geometry.name,
            "num_layers": input.geometry.num_layers,
            "num_attention_heads": input.geometry.num_attention_heads,
            "num_kv_heads": input.geometry.num_kv_heads,
            "head_dim": input.geometry.head_dim,
            "num_params": input.geometry.num_params,
            "weight_bytes_per_param": input.geometry.weight_bytes_per_param,
            "kv_bytes_per_elem": input.geometry.kv_bytes_per_elem,
            "max_context_tokens": input.geometry.max_context_tokens,
        },
        "capacity": plan.to_dict(),
        "findings": findings,
        "limits": _limits_section(),
    }
    # Final redaction pass: scrub every string field (host paths, secrets).
    payload = _redact_value(payload)
    # Volatile fields: generated_at and the digest itself are excluded so the
    # digest is deterministic for identical doctor inputs.
    volatile: dict[str, Any] = {
        k: v for k, v in payload.items() if k not in ("generated_at", "report_digest")
    }
    digest = canonical.digest_payload(canonical.canonicalize(volatile))
    payload["report_digest"] = digest
    return payload


def _limits_section() -> dict[str, Any]:
    from serving_verdict.hwprobe import (
        ALLOWED_COMMANDS,
        COMMAND_TIMEOUT_S,
        MAX_PROBE_OUTPUT_BYTES,
    )

    return {
        "command_allowlist": sorted(list(argv) for argv in ALLOWED_COMMANDS),
        "command_timeout_s": COMMAND_TIMEOUT_S,
        "max_probe_output_bytes": MAX_PROBE_OUTPUT_BYTES,
        "max_safe_utilization": 0.85,
        "read_only": True,
    }


def doctor_report_json(report: dict[str, Any]) -> str:
    """Serialize a doctor report for export (deterministic, human-readable)."""
    return json.dumps(_redact_value(report), ensure_ascii=True, indent=2, sort_keys=True)
