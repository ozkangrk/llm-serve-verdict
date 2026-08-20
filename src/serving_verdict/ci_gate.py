"""Stable production CI promotion gate (FR-7).

``gate_bundle`` verifies a verdict bundle through the existing
integrity/signature/trust paths (v0.1: ``engine.verify_bundle``; v0.4:
``bundle_v04`` structural validation + ``signing.verify_signed_bundle``)
and then evaluates the *sealed* verdict against a deployment requirement.
The gate never accepts a forged client verdict: the verdict is read only
from the digest-sealed bundle document, and any hand-edit (including a
PROMOTE<->REJECT flip) breaks the digest and fails with exit 4 semantics.

Frozen semantics (docs/CI_INTEGRATION.md)
------------------------------------------
Exit codes for ``serving-verdict gate``:

- ``0``  requirement satisfied. A PROMOTE verdict under ``--require
         PROMOTE`` is a satisfied gate. An INCONCLUSIVE verdict is also a
         satisfied *command run* (exit 0, ``blocked: false``) unless
         ``--fail-inconclusive`` is given: the command succeeded, it simply
         did not clear the promotion requirement.
- ``2``  usage/config/load error: bad ``--require`` value, an unreadable or
         structurally foreign bundle file, or ``--require-signature`` /
         ``--trust-store`` applied to a v0.1 compatibility bundle (those
         flags are only defined for v0.4).
- ``4``  integrity/signature/trust failure: digest mismatch, structural
         violation, missing required signature, invalid signature, or an
         untrusted signer/key. The stable failure ``code`` (``DIGEST_INVALID``,
         ``SIGNATURE_MISSING``, ``SIGNATURE_INVALID``, ``UNTRUSTED_SIGNER``,
         ``EVIDENCE_SIGNATURES_INVALID``) is reported.
- ``5``  valid bundle whose verdict is REJECT, or whose verdict does not
         satisfy ``--require`` (any non-PROMOTE verdict is
         deployment-blocking under ``--require PROMOTE``).
- ``6``  valid INCONCLUSIVE verdict, ONLY when ``--fail-inconclusive`` is
         set. Otherwise an INCONCLUSIVE verdict exits 0 with
         ``blocked: false``.

``--require PROMOTE`` makes any non-PROMOTE deployment-blocking: REJECT
maps to exit 5; INCONCLUSIVE maps to exit 6 under ``--fail-inconclusive``
and to exit 0 with ``blocked: false`` otherwise (the bundle is valid but
cannot prove promotion, so a CI pipeline must treat ``blocked: false``
under a non-zero-or-requirement outcome as "do not deploy" — see docs).

JSON contract (FR-7.4): ``--json`` emits exactly one JSON object on stdout;
all diagnostics go to stderr. The result object is self-digesting
(``digest`` = sha256 over its canonical body) so CI logs are tamper-evident.
The GitHub summary (``--github-summary``) is line-bounded, HTML/quote
escaped, and contains no raw evidence (no claim text, no artifact content,
no filesystem paths beyond the summary file itself).
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from serving_verdict import bundle_v04, engine
from serving_verdict.canonical import canonicalize
from serving_verdict.errors import UsageError
from serving_verdict.signing import TrustStore

RESULT_SCHEMA_VERSION = "serving-verdict.gate-result.v0.1"
RESULT_COMMAND = "gate"

#: The only requirement tokens the gate understands.
VALID_REQUIREMENTS: frozenset[str] = frozenset({"PROMOTE", "REJECT", "INCONCLUSIVE"})

#: Upper bound for the human-facing ``reason`` string in JSON output.
MAX_REASON_CHARS = 200

#: Upper bound for the GitHub summary file size (lines, total chars).
MAX_SUMMARY_LINES = 24
MAX_SUMMARY_CHARS = 2000

#: Digest domain separation for the self-digesting result object.
_RESULT_DIGEST_KEY: bytes = b"serving-verdict.gate-result.v0.1"


# ---------------------------------------------------------------------------
# typed outcome
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GateOutcome:
    """The gate's decision over a verified bundle (frozen, machine-facing)."""

    case_id: str
    bundle_version: str
    digest: str
    verdict: str
    required: str
    decision: str  # PASS | FAIL | PASS_WITHOUT_BLOCK
    blocked: bool  # True only when the requirement is satisfied (deploy ok)
    exit_code: int
    reason_codes: tuple[str, ...]
    signature_present: bool = False
    signature_valid: bool = False
    signer_trusted: bool = False
    key_id: str | None = None
    signer: str | None = None
    integrity_code: str | None = None  # set when verification failed (exit 4)
    _error: str = ""  # structural/verification message (never raw evidence)

    # -- construction ------------------------------------------------------

    @staticmethod
    def verified(
        *,
        case_id: str,
        bundle_version: str,
        digest: str,
        verdict: str,
        required: str,
        fail_inconclusive: bool,
        signature_present: bool = False,
        signature_valid: bool = False,
        signer_trusted: bool = False,
        key_id: str | None = None,
        signer: str | None = None,
    ) -> GateOutcome:
        if required not in VALID_REQUIREMENTS:
            raise UsageError(f"invalid --require value: {required!r}")
        if verdict not in VALID_REQUIREMENTS:
            # unreachable: bundle validation already rejected unknown verdicts
            raise UsageError(f"invalid sealed verdict: {verdict!r}")
        if fail_inconclusive and required == "INCONCLUSIVE":
            raise UsageError(
                "--fail-inconclusive is only defined with --require PROMOTE "
                "(--require INCONCLUSIVE already accepts INCONCLUSIVE)"
            )
        satisfied = verdict == required
        if verdict == "INCONCLUSIVE" and not fail_inconclusive and not satisfied:
            # valid command run, not a promotion, not a blocking failure
            exit_code, decision = 0, "PASS_WITHOUT_BLOCK"
        else:
            exit_code = 0 if satisfied else (6 if verdict == "INCONCLUSIVE" else 5)
            decision = "PASS" if satisfied else "FAIL"
        blocked = satisfied
        if satisfied:
            codes: tuple[str, ...] = ()
        elif verdict == "INCONCLUSIVE":
            codes = ("VERDICT_INCONCLUSIVE",)
            if fail_inconclusive:
                codes = codes + ("FAIL_INCONCLUSIVE",)
        else:
            codes = ("REQUIREMENT_NOT_MET",)
        return GateOutcome(
            case_id=case_id,
            bundle_version=bundle_version,
            digest=digest,
            verdict=verdict,
            required=required,
            decision=decision,
            blocked=blocked,
            exit_code=exit_code,
            reason_codes=codes,
            signature_present=signature_present,
            signature_valid=signature_valid,
            signer_trusted=signer_trusted,
            key_id=key_id,
            signer=signer,
        )

    @staticmethod
    def integrity_failure(
        *,
        case_id: str | None,
        bundle_version: str,
        error: str,
        code: str,
    ) -> GateOutcome:
        return GateOutcome(
            case_id=case_id if isinstance(case_id, str) else "",
            bundle_version=bundle_version,
            digest="",
            verdict="",
            required="",
            decision="FAIL",
            blocked=False,
            exit_code=4,
            reason_codes=(f"INTEGRITY_{code}",),
            integrity_code=code,
            _error=error,
        )

    # -- serialization -----------------------------------------------------

    def to_json_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "schema_version": RESULT_SCHEMA_VERSION,
            "command": RESULT_COMMAND,
            "case_id": self.case_id,
            "bundle_version": self.bundle_version,
            "bundle_digest": self.digest,
            "verdict": self.verdict,
            "required": self.required,
            "blocked": self.blocked,
            "decision": self.decision,
            "exit_code": self.exit_code,
            "reason_codes": list(self.reason_codes),
            "signature_present": self.signature_present,
            "signature_valid": self.signature_valid,
            "signer_trusted": self.signer_trusted,
            "key_id": self.key_id,
            "signer": self.signer,
        }
        if self.integrity_code is not None:
            body["code"] = self.integrity_code
            body["error"] = _bounded(self._error, MAX_REASON_CHARS)
        body["reason"] = self.reason
        body["result_digest"] = _self_digest(body)
        return body

    @property
    def reason(self) -> str:
        if self.integrity_code is not None:
            base = f"integrity failure ({self.integrity_code})"
        elif self.reason_codes:
            base = "requirement not met: " + ", ".join(self.reason_codes)
        else:
            base = "requirement satisfied"
        if len(base) <= MAX_REASON_CHARS:
            return base
        return base[: MAX_REASON_CHARS - 3] + "..."

    def summary_lines(self) -> tuple[str, ...]:
        """Bounded, escaped GitHub summary lines (no raw evidence)."""
        lines: list[str] = []
        lines.append(f"## Serving Verdict gate: {self.decision}")
        lines.append(f"verdict: {self.verdict or 'n/a'}")
        lines.append(f"required: {self.required or 'n/a'}")
        lines.append(f"blocked: {str(self.blocked).lower()}")
        if self.integrity_code is not None:
            lines.append(f"code: {self.integrity_code}")
        lines.append(f"reason: {_escape_summary_text(self.reason)}")
        if self.reason_codes and self.integrity_code is None:
            lines.append("reason codes: " + _escape_summary_text(", ".join(self.reason_codes)))
        lines.append(f"bundle digest: {self.digest or 'n/a'}")
        if self.signature_present:
            lines.append(
                "signature: "
                f"present=yes valid={str(self.signature_valid).lower()} "
                f"trusted={str(self.signer_trusted).lower()}"
            )
        if self.key_id:
            lines.append(f"key_id: {_escape_summary_text(self.key_id)}")
        if self.signer:
            lines.append(f"signer: {_escape_summary_text(self.signer)}")
        lines.append(f"case_id: {_escape_summary_text(self.case_id)}")
        return _bounded_lines(lines, MAX_SUMMARY_LINES, MAX_SUMMARY_CHARS)


# ---------------------------------------------------------------------------
# verification pipeline
# ---------------------------------------------------------------------------


def _extract_case_id(doc: Any) -> str | None:
    if not isinstance(doc, dict):
        return None
    if doc.get("schema_version") == bundle_v04.BUNDLE_SCHEMA_VERSION_V04:
        case = doc.get("case")
        if isinstance(case, dict) and isinstance(case.get("case_id"), str):
            return case["case_id"]
        return None
    cid = doc.get("case_id")
    return cid if isinstance(cid, str) else None


def gate_bundle(
    doc: Any,
    *,
    required_verdict: str = "PROMOTE",
    fail_inconclusive: bool = False,
    store: TrustStore | dict[str, Any] | str | Path | None = None,
    require_signed: bool = False,
) -> GateOutcome:
    """Verify ``doc`` and evaluate its sealed verdict against the requirement.

    Verification order (fail-closed, first failure wins):
      0. ``--require`` token valid, payload is a JSON object      (exit 2)
      1. v0.1: ``engine.verify_bundle``                            (exit 4)
         v0.4: structural + digest via the signing pipeline; with a
         trust store / ``require_signed`` the full signature+trust path;
         otherwise digest-only (signature fields informational)        (exit 4)
      2. verdict policy evaluation against ``required_verdict``
         (exit 0 / 5 / 6)

    ``store`` may be a loaded ``TrustStore``, a strict-JSON dict, or a
    path (str/Path); dict/path forms are validated via
    ``signing.load_trust_store`` (UsageError on any schema drift).

    Raises UsageError (exit 2) on bad flags/payload, IntegrityError (exit 4)
    on any integrity/signature/trust violation. Callers map the exceptions
    to the stable exit codes.
    """
    if required_verdict not in VALID_REQUIREMENTS:
        raise UsageError(f"invalid --require value: {required_verdict!r}")
    if not isinstance(doc, dict):
        raise UsageError("bundle is not a JSON object")
    from serving_verdict.signing import load_trust_store

    loaded_store: TrustStore | None
    if store is None:
        loaded_store = None
    elif isinstance(store, TrustStore):
        loaded_store = store
    else:
        loaded_store = load_trust_store(store)

    is_v04 = doc.get("schema_version") == bundle_v04.BUNDLE_SCHEMA_VERSION_V04
    case_id = _extract_case_id(doc)

    if is_v04:
        if require_signed or loaded_store is not None:
            report = _signed_verify(doc, store=loaded_store, require_signed=require_signed)
            verdict = str(doc["verdict"])
            return GateOutcome.verified(
                case_id=case_id or "",
                bundle_version=bundle_v04.BUNDLE_SCHEMA_VERSION_V04,
                digest=str(report["digest"]),
                verdict=verdict,
                required=required_verdict,
                fail_inconclusive=fail_inconclusive,
                signature_present=bool(report["signature_present"]),
                signature_valid=bool(report["signature_valid"]),
                signer_trusted=bool(report["signer_trusted"]),
                key_id=report.get("key_id"),
                signer=report.get("signer"),
            )
        from serving_verdict.bundle_v04 import verify_v04_bundle

        report = verify_v04_bundle(doc)
        return GateOutcome.verified(
            case_id=case_id or "",
            bundle_version=bundle_v04.BUNDLE_SCHEMA_VERSION_V04,
            digest=str(report["digest"]),
            verdict=str(doc["verdict"]),
            required=required_verdict,
            fail_inconclusive=fail_inconclusive,
            signature_present=bool(report["signature_present"]),
            signature_valid=False,
            signer_trusted=False,
            key_id=None,
            signer=None,
        )

    # v0.1 (compatibility) bundles: signature flags are undefined for them.
    if require_signed or loaded_store is not None:
        raise UsageError(
            "--require-signature/--trust-store apply to v0.4 bundles only; "
            "this is a v0.1 compatibility bundle"
        )
    report = engine.verify_bundle(doc)
    return GateOutcome.verified(
        case_id=case_id or "",
        bundle_version=engine.BUNDLE_SCHEMA_VERSION,
        digest=str(report["digest"]),
        verdict=str(doc["verdict"]),
        required=required_verdict,
        fail_inconclusive=fail_inconclusive,
    )


def _signed_verify(
    doc: dict[str, Any], *, store: TrustStore | None, require_signed: bool
) -> dict[str, Any]:
    """The v0.4 signature-aware verification path (exit 4 on any failure)."""
    from serving_verdict.signing import verify_signed_bundle

    return verify_signed_bundle(
        doc, store=store, require_signed=True if require_signed else None
    )


# ---------------------------------------------------------------------------
# bounded / escaped output helpers
# ---------------------------------------------------------------------------


def _bounded(text: str, limit: int) -> str:
    text = _escape_summary_text(text)
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


def _escape_summary_text(text: str) -> str:
    """Escape for a markdown/HTML-ish summary: no tags, no quotes/backticks.

    Newlines become literal ``\\n`` so one field never becomes two lines.
    """
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("`", "'")
        .replace('"', "'")
        .replace("\r\n", "\n")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
    )


def _bounded_lines(lines: list[str], max_lines: int, max_chars: int) -> tuple[str, ...]:
    kept: list[str] = []
    total = 0
    for line in lines:
        line = _escape_summary_text(line)
        if len(kept) >= max_lines:
            kept.append("... (summary truncated)")
            break
        if total + len(line) + 1 > max_chars:
            kept.append("... (summary truncated)")
            break
        kept.append(line)
        total += len(line) + 1
    return tuple(kept[:max_lines])


def _self_digest(body: dict[str, Any]) -> str:
    payload = {k: v for k, v in body.items() if k != "digest"}
    return "sha256:" + hashlib.sha256(_RESULT_DIGEST_KEY + b"\x00" + canonicalize(payload)).hexdigest()


#: Keep the import honest for type checkers that tree-shake re-exported names.
__all__ = [
    "RESULT_SCHEMA_VERSION",
    "VALID_REQUIREMENTS",
    "MAX_REASON_CHARS",
    "MAX_SUMMARY_LINES",
    "MAX_SUMMARY_CHARS",
    "GateOutcome",
    "gate_bundle",
    "render_github_summary",
    "write_github_summary",
]


# ---------------------------------------------------------------------------
# GitHub summary rendering (escaped, bounded, no raw evidence)
# ---------------------------------------------------------------------------

_SUMMARY_FOOTER = (
    "Generated by serving-verdict gate; full machine payload via `--json` "
    "(one JSON object on stdout)."
)


def render_github_summary(outcome: GateOutcome) -> str:
    lines = list(outcome.summary_lines())
    lines.append(_SUMMARY_FOOTER)
    return "\n".join(_bounded_lines(lines, MAX_SUMMARY_LINES, MAX_SUMMARY_CHARS)) + "\n"


def write_github_summary(outcome: GateOutcome, path: str | Path) -> None:
    """Write the summary file. The parent directory must exist (CLI rule:
    the gate never creates directories). Raises UsageError on I/O failure.
    """
    target = Path(path)
    if not target.parent.is_dir():
        raise UsageError(f"summary directory does not exist: {target.parent}")
    try:
        target.write_text(render_github_summary(outcome), encoding="utf-8")
    except OSError as exc:
        raise UsageError(f"cannot write GitHub summary: {exc.__class__.__name__}") from exc
