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


class ArchiveError(ServingVerdictError):
    """Content-addressed artifact store operation failed (fail-closed).

    Raised when an artifact cannot be stored safely: symlink/special source,
    size over the 20 MiB bound, escape outside the base dir or the store
    layout, or a copy that does not re-hash to its digest. No partial store
    or manifest is produced; the caller exits 2.
    """

    exit_code = 2


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
