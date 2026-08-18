"""Deterministic decision engine and tamper-evident bundle lifecycle.

Decision rules are evaluated in this exact order (MVP spec):
  1. Load/integrity  -> INCONCLUSIVE (case identity known) or exit 2
  2. Comparability   -> INCONCLUSIVE
  3. Hard gates      -> REJECT
  4. Missing gates   -> INCONCLUSIVE
  5. TTFT gate       -> REJECT
  6. Effect gate     -> REJECT
  7. Otherwise       -> PROMOTE

No LLM opinion participates; the verdict is a pure function of the bound
evidence and the case policy.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from serving_verdict.adapters import UnknownSchemaError, extract_samples
from serving_verdict.canonical import canonical_json_bytes, compute_bundle_digest
from serving_verdict.caseconfig import CaseConfig, load_case_config
from serving_verdict.errors import (
    CanonicalizationError,
    EvidenceError,
    IntegrityError,
    UsageError,
)
from serving_verdict.evidence import EvidenceBlob, EvidenceLoader
from serving_verdict.metrics import (
    MetricDimensions,
    MetricSample,
)
from serving_verdict.metrics import (
    comparable as _comparable,
)
from serving_verdict.metrics import (
    registry as _REGISTRY,
)

BUNDLE_SCHEMA_VERSION = "serving-verdict.bundle.v0.1"

# ---------------------------------------------------------------------------
# reason codes
# ---------------------------------------------------------------------------
RC_PRIMARY_EFFECT_PASSED = "PRIMARY_EFFECT_PASSED"
RC_ALL_GATES_PASSED = "ALL_REQUIRED_GATES_PASSED"
RC_HARD_GATE_FAILED = "HARD_GATE_FAILED"
RC_REQUIRED_GATE_MISSING = "REQUIRED_GATE_MISSING"
RC_TTFT_REGRESSION = "TTFT_REGRESSION"
RC_INSUFFICIENT_EFFECT = "INSUFFICIENT_EFFECT"
RC_EVIDENCE_HASH_MISMATCH = "EVIDENCE_HASH_MISMATCH"
RC_EVIDENCE_UNAVAILABLE = "EVIDENCE_UNAVAILABLE"
RC_UNSUPPORTED_SCHEMA = "UNSUPPORTED_SCHEMA"
RC_NONFINITE_EVIDENCE = "NONFINITE_EVIDENCE"
RC_METRIC_NOT_COMPARABLE = "METRIC_NOT_COMPARABLE"


class Verdict(StrEnum):
    PROMOTE = "PROMOTE"
    REJECT = "REJECT"
    INCONCLUSIVE = "INCONCLUSIVE"


class InconclusiveSignal(Exception):
    """Internal control-flow signal: evidence problem -> INCONCLUSIVE bundle."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class SideEvidence:
    artifact_id: str
    sha256: str
    schema_status: str  # "recognized" | "unsupported"
    samples: tuple[MetricSample, ...]
    machine_gates: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class GateEvidence:
    gate_id: str
    status: str  # "pass" | "fail" | "missing"
    authority: str  # "machine_measured" | "operator_attested" | "none"
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class DecisionInput:
    case_id: str
    claim_boundary: str
    primary_metric: str
    workload: str
    min_relative_improvement: float
    max_ttft_regression: float
    required_gates: tuple[str, ...]
    baseline: SideEvidence
    candidate: SideEvidence
    gates: tuple[GateEvidence, ...]
    unsupported_schemas: tuple[str, ...] = ()


@dataclass(frozen=True)
class Decision:
    verdict: Verdict
    reason_codes: tuple[str, ...]
    comparisons: tuple[dict[str, Any], ...]
    gates: tuple[GateEvidence, ...]
    source_index: tuple[dict[str, str], ...]


# ---------------------------------------------------------------------------
# strict parsing
# ---------------------------------------------------------------------------


def strict_parse_json(text: str) -> Any:
    """Parse JSON strictly (rejects NaN/Infinity). Raises CanonicalizationError."""
    canonical = canonical_json_bytes(text.encode("utf-8"))
    return json.loads(canonical.decode("utf-8"))


def _find_sample(samples: tuple[MetricSample, ...], metric_id: str, workload: str) -> MetricSample | None:
    for s in samples:
        if s.metric_id == metric_id and s.dimensions.workload_id == workload:
            return s
    return None


def _dims_dict(d: MetricDimensions) -> dict[str, Any]:
    return {
        "unit": d.unit,
        "procedure_version": d.procedure_version,
        "workload_id": d.workload_id,
        "concurrency": d.concurrency,
        "output_budget": d.output_budget,
        "thinking_mode": d.thinking_mode,
        "warm_cold": d.warm_cold,
        "aggregation": d.aggregation,
    }


def _comparison(metric_id: str, base_value: float, cand_value: float, sample: MetricSample) -> dict[str, Any]:
    relative_delta = None
    if base_value > 0 and math.isfinite(base_value) and math.isfinite(cand_value):
        relative_delta = (cand_value - base_value) / base_value
    return {
        "metric": metric_id,
        "baseline_value": base_value,
        "candidate_value": cand_value,
        "relative_delta": relative_delta,
        "direction": _REGISTRY[metric_id].direction,
        "unit": sample.dimensions.unit,
        "dimensions": _dims_dict(sample.dimensions),
    }


# ---------------------------------------------------------------------------
# decide
# ---------------------------------------------------------------------------


def decide(inp: DecisionInput) -> Decision:
    """Apply the deterministic rules in spec order. Pure function."""
    source_index = (
        {"artifact_id": inp.baseline.artifact_id, "status": inp.baseline.schema_status},
        {"artifact_id": inp.candidate.artifact_id, "status": inp.candidate.schema_status},
    )
    gates = tuple(inp.gates)

    def inconclusive(code: str, comparisons: tuple[dict[str, Any], ...] = ()) -> Decision:
        return Decision(
            verdict=Verdict.INCONCLUSIVE,
            reason_codes=(code,),
            comparisons=comparisons,
            gates=gates,
            source_index=source_index,
        )

    # Rule 1 residue: an unsupported schema cannot produce a decision.
    if inp.baseline.schema_status == "unsupported" or inp.candidate.schema_status == "unsupported":
        return inconclusive(RC_UNSUPPORTED_SCHEMA)

    # Rule 2: comparability of the primary metric under identical dimensions.
    base_s = _find_sample(inp.baseline.samples, inp.primary_metric, inp.workload)
    cand_s = _find_sample(inp.candidate.samples, inp.primary_metric, inp.workload)
    if base_s is None or cand_s is None or not _comparable(base_s, cand_s):
        return inconclusive(RC_METRIC_NOT_COMPARABLE)

    comparisons: list[dict[str, Any]] = [
        _comparison(inp.primary_metric, base_s.value, cand_s.value, base_s)
    ]
    # All other shared, comparable metrics are reported (never auto-converted).
    for metric_id in ("e2e_output_tokens_per_s", "aggregate_output_tokens_per_s", "api_latency_s"):
        b2 = _find_sample(inp.baseline.samples, metric_id, inp.workload)
        c2 = _find_sample(inp.candidate.samples, metric_id, inp.workload)
        if b2 is not None and c2 is not None and _comparable(b2, c2):
            comparisons.append(_comparison(metric_id, b2.value, c2.value, b2))

    ttft_base = _find_sample(inp.baseline.samples, "ttft_s", inp.workload)
    ttft_cand = _find_sample(inp.candidate.samples, "ttft_s", inp.workload)
    ttft_ok = False
    if ttft_base is not None and ttft_cand is not None and _comparable(ttft_base, ttft_cand):
        comparisons.append(_comparison("ttft_s", ttft_base.value, ttft_cand.value, ttft_base))
        ttft_ok = True

    # Rule 3: hard gates — any required gate failing rejects regardless of speed.
    if any(g.status == "fail" for g in gates):
        return Decision(
            verdict=Verdict.REJECT,
            reason_codes=(RC_HARD_GATE_FAILED,),
            comparisons=tuple(comparisons),
            gates=gates,
            source_index=source_index,
        )

    # Rule 4: missing/unverifiable required gates -> INCONCLUSIVE.
    if any(g.status == "missing" for g in gates):
        return inconclusive(RC_REQUIRED_GATE_MISSING, tuple(comparisons))

    # Rule 5: TTFT regression gate.
    if inp.max_ttft_regression >= 0:
        if not ttft_ok or ttft_base is None or ttft_cand is None:
            return inconclusive(RC_METRIC_NOT_COMPARABLE, tuple(comparisons))
        if ttft_base.value > 0:
            relative_regression = (ttft_cand.value - ttft_base.value) / ttft_base.value
            if relative_regression > inp.max_ttft_regression:
                return Decision(
                    verdict=Verdict.REJECT,
                    reason_codes=(RC_TTFT_REGRESSION,),
                    comparisons=tuple(comparisons),
                    gates=gates,
                    source_index=source_index,
                )

    # Rule 6: effect gate. The gain is direction-aware: for a lower_better
    # primary (e.g. api_latency_s) a *decrease* is the improvement; using the
    # higher_better formula here would let a 2x-worse candidate PROMOTE.
    if base_s.value <= 0:
        return inconclusive(RC_METRIC_NOT_COMPARABLE, tuple(comparisons))
    if _REGISTRY[inp.primary_metric].direction == "lower_better":
        gain = (base_s.value - cand_s.value) / base_s.value
    else:
        gain = (cand_s.value - base_s.value) / base_s.value
    if gain < inp.min_relative_improvement:
        return Decision(
            verdict=Verdict.REJECT,
            reason_codes=(RC_INSUFFICIENT_EFFECT,),
            comparisons=tuple(comparisons),
            gates=gates,
            source_index=source_index,
        )

    # Rule 7: promote.
    return Decision(
        verdict=Verdict.PROMOTE,
        reason_codes=(RC_PRIMARY_EFFECT_PASSED, RC_ALL_GATES_PASSED),
        comparisons=tuple(comparisons),
        gates=gates,
        source_index=source_index,
    )


# ---------------------------------------------------------------------------
# bundle build / verify / load
# ---------------------------------------------------------------------------


def build_bundle(decision: Decision, inp: DecisionInput, created_at: str) -> dict[str, Any]:
    """Assemble the full bundle dict with the canonical digest."""
    payload: dict[str, Any] = {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "case_id": inp.case_id,
        "verdict": decision.verdict.value,
        "reason_codes": list(decision.reason_codes),
        "baseline": {"artifact_id": inp.baseline.artifact_id, "sha256": inp.baseline.sha256},
        "candidate": {"artifact_id": inp.candidate.artifact_id, "sha256": inp.candidate.sha256},
        "comparisons": list(decision.comparisons),
        "gates": [
            {
                "id": g.gate_id,
                "status": g.status,
                "authority": g.authority,
                "evidence": list(g.evidence),
            }
            for g in decision.gates
        ],
        "claim_boundary": inp.claim_boundary,
        "source_index": list(decision.source_index),
        "created_at": created_at,
    }
    payload["bundle_digest"] = compute_bundle_digest(payload)
    return payload


def _bundle_digest_of(bundle: dict[str, Any]) -> str:
    payload = {k: v for k, v in bundle.items() if k not in ("created_at", "bundle_digest")}
    return compute_bundle_digest(payload)


def verify_bundle(bundle: dict[str, Any]) -> dict[str, Any]:
    """Offline integrity verification. Raises IntegrityError on any violation."""
    if not isinstance(bundle, dict):
        raise IntegrityError("bundle is not a JSON object")
    if bundle.get("schema_version") != BUNDLE_SCHEMA_VERSION:
        raise IntegrityError(f"unsupported bundle schema_version: {bundle.get('schema_version')!r}")
    for key in (
        "case_id",
        "verdict",
        "reason_codes",
        "baseline",
        "candidate",
        "comparisons",
        "gates",
        "claim_boundary",
        "created_at",
        "bundle_digest",
    ):
        if key not in bundle:
            raise IntegrityError(f"bundle missing required field: {key}")
    if bundle["verdict"] not in Verdict._value2member_map_:
        raise IntegrityError(f"unknown verdict: {bundle['verdict']!r}")
    if not isinstance(bundle["reason_codes"], list) or not all(
        isinstance(r, str) for r in bundle["reason_codes"]
    ):
        raise IntegrityError("reason_codes must be a list of strings")
    for side in ("baseline", "candidate"):
        ref = bundle[side]
        if (
            not isinstance(ref, dict)
            or not isinstance(ref.get("artifact_id"), str)
            or not isinstance(ref.get("sha256"), str)
            or len(ref["sha256"]) != 64
        ):
            raise IntegrityError(f"{side} reference is malformed")
    expected = bundle.get("bundle_digest")
    if not isinstance(expected, str) or not expected.startswith("sha256:"):
        raise IntegrityError("bundle_digest is malformed")
    actual = _bundle_digest_of(bundle)
    if actual != expected:
        raise IntegrityError(f"bundle digest mismatch: recorded {expected}, recomputed {actual}")
    return {"valid": True, "digest": actual}


def load_bundle(path: str | Path) -> dict[str, Any]:
    """Load a bundle file with strict JSON parsing."""
    p = Path(path)
    if not p.is_file():
        raise UsageError(f"bundle file not found: {p}")
    try:
        text = p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise UsageError(f"cannot read bundle: {exc}") from exc
    try:
        return strict_parse_json(text)
    except CanonicalizationError as exc:
        raise UsageError(f"bundle is not valid strict JSON: {exc}") from exc


# ---------------------------------------------------------------------------
# import_case
# ---------------------------------------------------------------------------


def _combine_gate_statuses(statuses: list[str]) -> str:
    if not statuses:
        return "missing"
    if "fail" in statuses:
        return "fail"
    return "pass" if all(s == "pass" for s in statuses) else "missing"


def _resolve_source_root(case_file: Path, source_root: str) -> Path | None:
    """Resolve a case config's source_root to an existing directory.

    Absolute roots are used as-is; relative roots resolve against the parent
    directory of the case file (v0.2 portable). Returns None when the
    resolved directory does not exist (callers decide how to fail: the
    importer yields an INCONCLUSIVE bundle, the archiver fails closed).
    """
    root = Path(source_root)
    if root.is_absolute():
        resolved = root.resolve()
    else:
        base = case_file.resolve().parent
        resolved = (base / root).resolve()
    return resolved


def import_case(case_path: str | Path, source_root_override: str | Path | None = None) -> dict[str, Any]:
    """Import a case config, load bound evidence, decide, and build a bundle.

    Returns the full bundle dict for any valid verdict (PROMOTE/REJECT/
    INCONCLUSIVE). Raises CaseConfigError (exit 2) when the case config itself
    is unusable.

    ``source_root_override`` (CLI-only, v0.2) replaces the config's source
    root before the evidence loader is constructed; it must be an absolute
    existing directory. A relative ``source_root`` in the config resolves
    against the parent directory of the case file.
    """
    from serving_verdict.errors import CaseConfigError

    case_file = Path(case_path)
    cfg: CaseConfig = load_case_config(case_file)
    if source_root_override is not None:
        override = Path(source_root_override)
        if not override.is_absolute():
            raise CaseConfigError(
                f"--source-root must be an absolute directory: {source_root_override!r}"
            )
        if not override.is_dir():
            raise CaseConfigError(
                f"--source-root does not exist or is not a directory: {source_root_override!r}"
            )
        cfg = _with_source_root(cfg, str(override.resolve()))
    else:
        root = _resolve_source_root(case_file, cfg.source_root)
        if root is not None and not Path(cfg.source_root).is_absolute():
            cfg = _with_source_root(cfg, str(root))
        # A missing/absolute root is used as-is: EvidenceLoader raises
        # EvidenceError, which import_case maps to an INCONCLUSIVE bundle.
    def inconclusive_bundle(reason_codes: list[str], detail: str = "") -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": BUNDLE_SCHEMA_VERSION,
            "case_id": cfg.case_id,
            "verdict": Verdict.INCONCLUSIVE.value,
            "reason_codes": reason_codes,
            "baseline": {"artifact_id": cfg.baseline.relative_path, "sha256": cfg.baseline.sha256},
            "candidate": {"artifact_id": cfg.candidate.relative_path, "sha256": cfg.candidate.sha256},
            "comparisons": [],
            "gates": [],
            "claim_boundary": cfg.claim_boundary,
            "source_index": [],
            "created_at": datetime.now(UTC).isoformat(),
        }
        if detail:
            payload["inconclusive_detail"] = detail
        payload["bundle_digest"] = compute_bundle_digest(payload)
        return payload

    try:
        loader = EvidenceLoader(cfg.source_root)
    except EvidenceError as exc:
        return inconclusive_bundle([RC_EVIDENCE_UNAVAILABLE], str(exc))

    def load_side(ref: Any) -> EvidenceBlob:
        try:
            return loader.load_artifact(ref.relative_path, expected_sha256=ref.sha256)
        except EvidenceError as exc:
            if "sha256 mismatch" in str(exc):
                raise InconclusiveSignal(RC_EVIDENCE_HASH_MISMATCH, str(exc)) from exc
            raise InconclusiveSignal(RC_EVIDENCE_UNAVAILABLE, str(exc)) from exc

    try:
        base_blob = load_side(cfg.baseline)
        cand_blob = load_side(cfg.candidate)
    except CaseConfigError:
        raise
    except InconclusiveSignal as sig:
        return inconclusive_bundle([sig.code], sig.detail)

    def adapt(blob: EvidenceBlob) -> SideEvidence:
        try:
            doc = strict_parse_json(blob.text)
        except CanonicalizationError as exc:
            raise InconclusiveSignal(RC_NONFINITE_EVIDENCE, str(exc)) from exc
        try:
            result = extract_samples(doc, source_artifact=blob.relative_path)
        except UnknownSchemaError:
            return SideEvidence(
                artifact_id=blob.relative_path,
                sha256=blob.sha256,
                schema_status="unsupported",
                samples=(),
            )
        except CanonicalizationError as exc:
            raise InconclusiveSignal(RC_NONFINITE_EVIDENCE, str(exc)) from exc
        return SideEvidence(
            artifact_id=blob.relative_path,
            sha256=blob.sha256,
            schema_status="recognized",
            samples=result.samples,
            machine_gates=dict(result.machine_gates),
        )

    try:
        baseline = adapt(base_blob)
        candidate = adapt(cand_blob)
    except InconclusiveSignal as sig:
        return inconclusive_bundle([sig.code], sig.detail)

    # Resolve required gates: machine evidence first, then operator attestation.
    supplemental: dict[str, GateEvidence] = {}
    for entry in cfg.supplemental:
        try:
            loader.load_artifact(entry.source, expected_sha256=entry.sha256)
        except EvidenceError:
            continue  # unverifiable attested evidence -> gate stays missing
        status = "pass" if entry.status == "pass" else "fail"
        supplemental[entry.id] = GateEvidence(
            gate_id=entry.id,
            status=status,
            authority="operator_attested",
            evidence=(entry.source,),
        )

    gates: list[GateEvidence] = []
    for gate_id in cfg.policy.required_gates:
        statuses: list[str] = []
        evidence: list[str] = []
        for side in (baseline, candidate):
            if side.schema_status == "recognized" and gate_id in side.machine_gates:
                statuses.append(side.machine_gates[gate_id])
                evidence.append(side.artifact_id)
        if statuses:
            gates.append(
                GateEvidence(
                    gate_id=gate_id,
                    status=_combine_gate_statuses(statuses),
                    authority="machine_measured",
                    evidence=tuple(evidence),
                )
            )
            continue
        if gate_id in supplemental:
            gates.append(supplemental[gate_id])
        else:
            gates.append(GateEvidence(gate_id=gate_id, status="missing", authority="none"))

    inp = DecisionInput(
        case_id=cfg.case_id,
        claim_boundary=cfg.claim_boundary,
        primary_metric=cfg.policy.primary_metric,
        workload=cfg.policy.workload,
        min_relative_improvement=cfg.policy.min_relative_improvement,
        max_ttft_regression=cfg.policy.max_ttft_regression,
        required_gates=cfg.policy.required_gates,
        baseline=baseline,
        candidate=candidate,
        gates=tuple(gates),
        unsupported_schemas=tuple(
            s.artifact_id for s in (baseline, candidate) if s.schema_status == "unsupported"
        ),
    )
    decision = decide(inp)
    return build_bundle(decision, inp, datetime.now(UTC).isoformat())


def _with_source_root(cfg: CaseConfig, root: str) -> CaseConfig:
    """Return a copy of the config with a different source_root (v0.2)."""
    return CaseConfig(
        case_id=cfg.case_id,
        source_root=root,
        baseline=cfg.baseline,
        candidate=cfg.candidate,
        policy=cfg.policy,
        supplemental=cfg.supplemental,
        claim_boundary=cfg.claim_boundary,
    )
