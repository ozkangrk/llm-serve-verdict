"""Typed error hierarchy for Serving Verdict.

Exit-code mapping (MVP spec):
  - UsageError / CaseConfigError / EvidenceError -> exit 2 (no valid bundle)
  - IntegrityError -> exit 4 (bundle verification failure)
  - InconclusiveError is NOT an error exit: it drives an INCONCLUSIVE bundle.
"""
from __future__ import annotations


class ServingVerdictError(Exception):
    """Base class for all Serving Verdict errors."""

    exit_code: int = 2


class UsageError(ServingVerdictError):
    """CLI usage error (bad arguments/paths that prevent any bundle)."""

    exit_code = 2


class CaseConfigError(ServingVerdictError):
    """Case config is unusable: invalid YAML, wrong schema version, bad policy."""

    exit_code = 2


class EvidenceError(ServingVerdictError):
    """Evidence cannot be loaded safely (path escape, missing, special, size)."""

    exit_code = 2


class CanonicalizationError(ServingVerdictError):
    """JSON is not canonicalizable (NaN/Infinity, invalid, non-finite)."""

    exit_code = 2


class IntegrityError(ServingVerdictError):
    """Bundle or artifact integrity check failed."""

    exit_code = 4

    #: Stable machine-readable failure code (v0.4 signature pipeline sets
    #: DIGEST_INVALID / SIGNATURE_MISSING / SIGNATURE_INVALID /
    #: UNTRUSTED_SIGNER / EVIDENCE_SIGNATURES_INVALID). Defaults to the
    #: generic class so v0.1 callers are unaffected.
    code: str = "INTEGRITY_FAILURE"

    def __init__(self, message: str = "", code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code


class ArchiveError(ServingVerdictError):
    """Content-addressed artifact store operation failed (fail-closed).

    Raised when an artifact cannot be stored safely: symlink/special source,
    size over the 20 MiB bound, escape outside the base dir or the store
    layout, or a copy that does not re-hash to its digest. No partial store
    or manifest is produced; the caller exits 2.
    """

    exit_code = 2


class PlanError(ServingVerdictError):
    """Experiment planning failed on bad input (exit 2, nothing was planned).

    Raised by the v0.4 sweep planner and Pareto frontier for out-of-allowlist
    parameter ranges, non-scalar or type-mismatched range values, duplicate
    values, unbounded trial counts, or malformed constraints. The planner is
    pure: a PlanError means no plan was produced and nothing was executed.
    """

    exit_code = 2


class SummaryIntegrityError(IntegrityError):
    """A normalized benchmark summary is unusable or tampered (exit 4).

    Raised when a sealed summary fails fail-closed validation: wrong schema
    version, missing/extra sections or keys, bad numeric types, a forbidden
    tamper marker, or a digest that does not recompute over the payload.
    """

    exit_code = 4


class ArtifactIntegrityError(IntegrityError):
    """A canonical compare/sweep/pareto artifact failed verification (exit 4).

    The artifact is not the one that was sealed: its recorded artifact_digest
    does not recompute, its schema version is foreign, or required fields
    are missing/malformed.
    """

    exit_code = 4


class StatisticalArtifactError(IntegrityError):
    """A sealed statistical artifact failed schema, provenance, or digest verification."""

    exit_code = 4


class InconclusiveError(ServingVerdictError):
    """Evidence integrity/comparability problem that yields an INCONCLUSIVE bundle.

    Carries reason codes so the engine can record *why* the verdict is
    INCONCLUSIVE. This is a structured signal, not a process failure.
    """

    exit_code = 0

    def __init__(self, reason_codes: list[str], detail: str = "") -> None:
        super().__init__(detail or "; ".join(reason_codes))
        self.reason_codes = reason_codes
        self.detail = detail


class WorkloadError(ServingVerdictError):
    """Workload loading/schema violation (strict, fail-closed, sanitized).

    Message text is structural only: bounds, key names and line numbers.
    Never raw message content, values, credentials or filesystem paths.
    """

    exit_code = 2


class ReplayError(ServingVerdictError):
    """Replay execution/normalization failure.

    All messages are sanitized: they never carry raw prompts, remote output,
    executor exception text, credentials or filesystem paths.
    """

    exit_code = 2


class GateError(ServingVerdictError):
    """CI regression gate configuration or usage error."""

    exit_code = 2


class StatisticalError(ServingVerdictError):
    """Statistical verdict engine rejected its inputs (exit 2, no result).

    Raised when a sample or spec violates the fail-closed contract:
    empty or malformed samples, non-numeric/bool/non-finite/negative
    values, or out-of-bounds spec fields. Nothing is computed and no
    result or artifact is produced.
    """

    exit_code = 2
