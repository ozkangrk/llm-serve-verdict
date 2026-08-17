"""Case config v0.1: operator-authored YAML policy bound to evidence hashes.

The case config is policy + evidence binding, not measurement authority.
Validation is fail-closed: wrong schema version, malformed YAML, unknown
primary metric, or out-of-range policy values raise CaseConfigError (exit 2).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from serving_verdict.errors import CaseConfigError
from serving_verdict.metrics import registry as METRIC_REGISTRY

CASE_SCHEMA_VERSION = "serving-verdict.case.v0.1"
SUPPLEMENTAL_KINDS: frozenset[str] = frozenset({"operator_attested"})
SUPPLEMENTAL_STATUSES: frozenset[str] = frozenset({"pass", "fail"})


@dataclass(frozen=True)
class ArtifactRef:
    relative_path: str
    sha256: str


@dataclass(frozen=True)
class SupplementalEvidence:
    id: str
    kind: str
    status: str
    source: str
    sha256: str


@dataclass(frozen=True)
class Policy:
    primary_metric: str
    workload: str
    min_relative_improvement: float
    max_ttft_regression: float
    required_gates: tuple[str, ...]


@dataclass(frozen=True)
class CaseConfig:
    case_id: str
    source_root: str
    baseline: ArtifactRef
    candidate: ArtifactRef
    policy: Policy
    supplemental: tuple[SupplementalEvidence, ...]
    claim_boundary: str


def _as_str(doc: dict[str, Any], key: str, ctx: str) -> str:
    value = doc.get(key)
    if not isinstance(value, str) or not value.strip():
        raise CaseConfigError(f"{ctx}.{key} must be a non-empty string")
    return value


def _as_sha256(value: Any, ctx: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value.lower()):
        raise CaseConfigError(f"{ctx}.sha256 must be a 64-hex-digit SHA-256 string")
    return value.lower()


def _as_artifact_ref(doc: dict[str, Any], key: str) -> ArtifactRef:
    ref = doc.get(key)
    if not isinstance(ref, dict):
        raise CaseConfigError(f"{key} must be a mapping with 'artifact' and 'sha256'")
    relative_path = _as_str(ref, "artifact", key)
    return ArtifactRef(
        relative_path=relative_path, sha256=_as_sha256(ref.get("sha256"), key)
    )


def _as_policy(doc: dict[str, Any]) -> Policy:
    raw = doc.get("policy")
    if not isinstance(raw, dict):
        raise CaseConfigError("policy is required and must be a mapping")
    primary_metric = _as_str(raw, "primary_metric", "policy")
    if primary_metric not in METRIC_REGISTRY:
        raise CaseConfigError(f"policy.primary_metric is not in the metric registry: {primary_metric}")
    workload = _as_str(raw, "workload", "policy")
    min_imp = raw.get("min_relative_improvement")
    max_ttft = raw.get("max_ttft_regression")
    if not isinstance(min_imp, (int, float)) or isinstance(min_imp, bool) or min_imp < 0:
        raise CaseConfigError("policy.min_relative_improvement must be a number >= 0")
    if not isinstance(max_ttft, (int, float)) or isinstance(max_ttft, bool) or max_ttft < 0:
        raise CaseConfigError("policy.max_ttft_regression must be a number >= 0")
    gates = raw.get("required_gates")
    if (
        not isinstance(gates, list)
        or not gates
        or not all(isinstance(g, str) and g for g in gates)
        or len(set(gates)) != len(gates)
    ):
        raise CaseConfigError("policy.required_gates must be a non-empty list of unique strings")
    return Policy(
        primary_metric=primary_metric,
        workload=workload,
        min_relative_improvement=float(min_imp),
        max_ttft_regression=float(max_ttft),
        required_gates=tuple(gates),
    )


def _as_supplemental(doc: dict[str, Any]) -> tuple[SupplementalEvidence, ...]:
    raw = doc.get("supplemental_evidence")
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise CaseConfigError("supplemental_evidence must be a list")
    out: list[SupplementalEvidence] = []
    seen_ids: set[str] = set()
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise CaseConfigError(f"supplemental_evidence[{i}] must be a mapping")
        eid = _as_str(entry, "id", f"supplemental_evidence[{i}]")
        kind = entry.get("kind")
        status = entry.get("status")
        if kind not in SUPPLEMENTAL_KINDS:
            raise CaseConfigError(f"supplemental_evidence[{i}].kind must be one of {sorted(SUPPLEMENTAL_KINDS)}")
        if status not in SUPPLEMENTAL_STATUSES:
            raise CaseConfigError(
                f"supplemental_evidence[{i}].status must be one of {sorted(SUPPLEMENTAL_STATUSES)}"
            )
        if eid in seen_ids:
            # Fail-closed: conflicting/duplicate gate attestations are a config
            # error (exit 2), never a silent last-entry-wins merge.
            raise CaseConfigError(
                f"duplicate/conflicting supplemental gate id {eid!r}: each gate id "
                "must be attested at most once"
            )
        seen_ids.add(eid)
        out.append(
            SupplementalEvidence(
                id=eid,
                kind=kind,
                status=status,
                source=_as_str(entry, "source", f"supplemental_evidence[{i}]"),
                sha256=_as_sha256(entry.get("sha256"), f"supplemental_evidence[{i}]"),
            )
        )
    return tuple(out)


def _as_source_root(doc: dict[str, Any]) -> str:
    raw = doc.get("source_root")
    if not isinstance(raw, str) or not raw.strip():
        raise CaseConfigError("case.source_root must be a non-empty string")
    root = Path(raw)
    if not root.is_absolute():
        raise CaseConfigError(f"case.source_root must be an absolute directory: {raw!r}")
    if not root.is_dir():
        raise CaseConfigError(f"case.source_root does not exist or is not a directory: {raw!r}")
    return raw


def load_case_config(path: str | Path) -> CaseConfig:
    """Load and validate a case config. Raises CaseConfigError (exit 2) on problems."""
    p = Path(path)
    if not p.is_file():
        raise CaseConfigError(f"case config not found: {p}")
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise CaseConfigError(f"invalid YAML in case config: {exc}") from exc
    if not isinstance(raw, dict):
        raise CaseConfigError("case config must be a YAML mapping")
    if raw.get("schema_version") != CASE_SCHEMA_VERSION:
        raise CaseConfigError(
            f"unsupported case schema_version: {raw.get('schema_version')!r} "
            f"(expected {CASE_SCHEMA_VERSION})"
        )
    case_id = _as_str(raw, "id", "case")
    source_root = _as_source_root(raw)
    baseline = _as_artifact_ref(raw, "baseline")
    candidate = _as_artifact_ref(raw, "candidate")
    policy = _as_policy(raw)
    supplemental = _as_supplemental(raw)
    claim_boundary = _as_str(raw, "claim_boundary", "case")
    return CaseConfig(
        case_id=case_id,
        source_root=source_root,
        baseline=baseline,
        candidate=candidate,
        policy=policy,
        supplemental=supplemental,
        claim_boundary=claim_boundary,
    )
