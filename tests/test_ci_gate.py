"""CI promotion gate core: policy, outcome, digest, summary (RED first).

Stable contract (frozen in docs/CI_INTEGRATION.md):
  exit 0 = requirement satisfied
  exit 2 = usage/config/load error (bad --require, unreadable bundle/store)
  exit 4 = integrity/signature/trust verification failure
  exit 5 = valid bundle whose verdict is REJECT, or whose verdict does not
           satisfy --require (deployment blocking)
  exit 6 = valid INCONCLUSIVE verdict, ONLY when --fail-inconclusive is set;
           otherwise INCONCLUSIVE exits 0 with blocked=false

The gate verifies bundle integrity/signature/trust through the existing
v0.1 (engine.verify_bundle) and v0.4 (bundle_v04 + signing) paths before it
ever looks at the client-claimed verdict; the verdict is always taken from
the digest-sealed bundle document.
"""
from __future__ import annotations

import copy
import json

import pytest

from serving_verdict import ci_gate
from serving_verdict.bundle_v04 import compute_v04_digest
from serving_verdict.errors import IntegrityError, UsageError
from tests.helpers_ci_gate import make_v01_bundle
from tests.helpers_v04_bundle import make_signing_key, make_v04_bundle

# ---------------------------------------------------------------------------
# builders
# ---------------------------------------------------------------------------


def make_signed_v04_bundle(verdict: str = "PROMOTE") -> dict:
    """A valid v0.4 bundle signed by the deterministic test identity."""
    from serving_verdict import signing

    doc = make_v04_bundle(verdict=verdict, reason_codes=["UNIT_TEST_VERDICT"])
    ident = make_signing_key()
    identity = signing.SignerIdentity(
        private_key=ident.private_key, key_id=ident.key_id, signer=ident.signer
    )
    return signing.sign_bundle(doc, identity=identity)


def make_trust_store(seed_hex: str, signer: str) -> dict:
    from serving_verdict import signing

    key = signing.load_ed25519_private_key_from_seed_hex(seed_hex)
    from tests.helpers_v04_bundle import b64

    pub = b64(signing.public_key_bytes_of(key))
    return {
        "schema_version": signing.TRUST_STORE_SCHEMA_VERSION,
        "require_signed_evidence": False,
        "require_signed_verdict": True,
        "allowed_signers": [signer],
        "allowed_keys": [
            {
                "key_id": signing.key_id_for_public_key(
                    signing.public_key_bytes_of(key)
                ),
                "ed25519_public_key": pub,
            }
        ],
    }


# ---------------------------------------------------------------------------
# gate_bundle: verification paths
# ---------------------------------------------------------------------------


def test_gate_v01_promote_verifies() -> None:
    outcome = ci_gate.gate_bundle(make_v01_bundle("PROMOTE"))
    assert outcome.verdict == "PROMOTE"
    assert outcome.case_id == "fixture-v01"
    assert outcome.bundle_version == "serving-verdict.bundle.v0.1"
    assert outcome.digest.startswith("sha256:")


def test_gate_v04_unsigned_digest_only() -> None:
    outcome = ci_gate.gate_bundle(make_v04_bundle())
    assert outcome.verdict == "PROMOTE"
    assert outcome.bundle_version == "serving-verdict.bundle.v0.4"
    assert outcome.signature_present is False
    assert outcome.signature_valid is False
    assert outcome.signer_trusted is False


def test_gate_v04_signed_trusted() -> None:
    doc = make_signed_v04_bundle()
    ident = make_signing_key()
    store = make_trust_store(ident.seed_hex, ident.signer)
    outcome = ci_gate.gate_bundle(doc, store=store)
    assert outcome.signature_present is True
    assert outcome.signature_valid is True
    assert outcome.signer_trusted is True
    assert outcome.signer == ident.signer
    assert outcome.key_id == ident.key_id


def test_gate_v04_signed_requires_signature_without_store() -> None:
    # require_signed=True but no store: the signature key cannot be verified
    # offline -> UNTRUSTED_SIGNER (exit 4 semantics).
    doc = make_signed_v04_bundle()
    with pytest.raises(IntegrityError) as exc:
        ci_gate.gate_bundle(doc, store=None, require_signed=True)
    assert exc.value.code == "UNTRUSTED_SIGNER"


def test_gate_v04_unsigned_requires_signature() -> None:
    doc = make_v04_bundle()
    with pytest.raises(IntegrityError) as exc:
        ci_gate.gate_bundle(doc, store=None, require_signed=True)
    assert exc.value.code == "SIGNATURE_MISSING"


def test_gate_v04_untrusted_signer() -> None:
    from serving_verdict import signing
    from tests.helpers_v04_bundle import _from_seed

    ident = make_signing_key()
    store = make_trust_store(ident.seed_hex, ident.signer)
    # A bundle signed by a key the store does not know -> UNTRUSTED_SIGNER.
    other_seed = bytes.fromhex("22" * 32)
    other_key = _from_seed(other_seed)
    other_key_id = signing.key_id_for_public_key(signing.public_key_bytes_of(other_key))
    assert other_key_id != ident.key_id
    other_ident = signing.SignerIdentity(
        private_key=other_key, key_id=other_key_id, signer="ci@company.example"
    )
    doc = signing.sign_bundle(make_v04_bundle(), identity=other_ident)
    with pytest.raises(IntegrityError) as exc:
        ci_gate.gate_bundle(doc, store=store)
    assert exc.value.code == "UNTRUSTED_SIGNER"


def test_gate_v04_mutated_bundle_fails_digest() -> None:
    doc = make_v04_bundle()
    mutated = copy.deepcopy(doc)
    mutated["verdict"] = "REJECT"
    with pytest.raises(IntegrityError) as exc:
        ci_gate.gate_bundle(mutated)
    assert exc.value.code == "DIGEST_INVALID"


def test_gate_v01_mutated_bundle_fails_digest() -> None:
    doc = make_v01_bundle("REJECT")
    mutated = copy.deepcopy(doc)
    mutated["verdict"] = "PROMOTE"  # forged client verdict: digest breaks
    with pytest.raises(IntegrityError):
        ci_gate.gate_bundle(mutated)


def test_gate_v04_mutated_signed_fails() -> None:
    doc = make_signed_v04_bundle()
    mutated = copy.deepcopy(doc)
    mutated["verdict"] = "INCONCLUSIVE"
    ident = make_signing_key()
    store = make_trust_store(ident.seed_hex, ident.signer)
    with pytest.raises(IntegrityError) as exc:
        ci_gate.gate_bundle(mutated, store=store)
    assert exc.value.code == "DIGEST_INVALID"


def test_gate_v01_with_require_signature_is_unsupported() -> None:
    with pytest.raises(UsageError):
        ci_gate.gate_bundle(make_v01_bundle(), store=None, require_signed=True)


def test_gate_v01_with_trust_store_is_unsupported() -> None:
    with pytest.raises(UsageError):
        ci_gate.gate_bundle(make_v01_bundle(), store=make_trust_store("00" * 32, "s"))


def test_gate_rejects_non_bundle_payload() -> None:
    with pytest.raises(UsageError):
        ci_gate.gate_bundle([1, 2, 3])


# ---------------------------------------------------------------------------
# verdict policy: decision / blocked / exit code / reason codes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "verdict,required,fail_inconclusive,exit_code,blocked,decision",
    [
        ("PROMOTE", "PROMOTE", False, 0, True, "PASS"),
        ("PROMOTE", "PROMOTE", True, 0, True, "PASS"),
        ("REJECT", "PROMOTE", False, 5, False, "FAIL"),
        ("REJECT", "PROMOTE", True, 5, False, "FAIL"),
        ("INCONCLUSIVE", "PROMOTE", False, 0, False, "PASS_WITHOUT_BLOCK"),
        ("INCONCLUSIVE", "PROMOTE", True, 6, False, "FAIL"),
        ("REJECT", "REJECT", False, 0, True, "PASS"),
        ("INCONCLUSIVE", "INCONCLUSIVE", False, 0, True, "PASS"),
        ("PROMOTE", "REJECT", False, 5, False, "FAIL"),
        ("PROMOTE", "INCONCLUSIVE", False, 5, False, "FAIL"),
    ],
)
def test_policy_matrix(
    verdict: str,
    required: str,
    fail_inconclusive: bool,
    exit_code: int,
    blocked: bool,
    decision: str,
) -> None:
    outcome = ci_gate.gate_bundle(
        make_v01_bundle(verdict),
        required_verdict=required,
        fail_inconclusive=fail_inconclusive,
    )
    assert outcome.exit_code == exit_code
    assert outcome.blocked is blocked
    assert outcome.decision == decision
    assert outcome.verdict == verdict
    assert outcome.required == required


def test_policy_require_PROMOTE_any_non_PROMOTE_blocks() -> None:
    for verdict, code in (("REJECT", 5), ("INCONCLUSIVE", 0)):
        outcome = ci_gate.gate_bundle(make_v01_bundle(verdict), required_verdict="PROMOTE")
        # only a verifiably valid REJECT blocks with exit 5; INCONCLUSIVE
        # without --fail-inconclusive exits 0 but is still not promotable.
        assert outcome.exit_code == code
        assert outcome.blocked is (verdict == "PROMOTE")


def test_policy_reason_codes_stable() -> None:
    o = ci_gate.gate_bundle(make_v01_bundle("PROMOTE"))
    assert o.reason_codes == ()
    o = ci_gate.gate_bundle(make_v01_bundle("REJECT"))
    assert o.reason_codes == ("REQUIREMENT_NOT_MET",)
    o = ci_gate.gate_bundle(make_v01_bundle("INCONCLUSIVE"))
    assert o.reason_codes == ("VERDICT_INCONCLUSIVE",)
    o = ci_gate.gate_bundle(
        make_v01_bundle("INCONCLUSIVE"), fail_inconclusive=True
    )
    assert o.reason_codes == ("VERDICT_INCONCLUSIVE", "FAIL_INCONCLUSIVE")


def test_policy_bad_require_is_usage_error() -> None:
    with pytest.raises(UsageError):
        ci_gate.gate_bundle(make_v01_bundle(), required_verdict="MAYBE")


def test_policy_fail_inconclusive_with_require_inconclusive_is_usage_error() -> None:
    with pytest.raises(UsageError):
        ci_gate.gate_bundle(
            make_v01_bundle("INCONCLUSIVE"),
            required_verdict="INCONCLUSIVE",
            fail_inconclusive=True,
        )


# ---------------------------------------------------------------------------
# outcome JSON: single object, digest, bounded reason, no raw evidence
# ---------------------------------------------------------------------------


def test_outcome_json_single_object_and_fields() -> None:
    outcome = ci_gate.gate_bundle(make_v01_bundle("REJECT"))
    payload = outcome.to_json_dict()
    text = json.dumps(payload)
    assert json.loads(text) == payload
    for key in (
        "schema_version",
        "command",
        "case_id",
        "bundle_version",
        "bundle_digest",
        "result_digest",
        "verdict",
        "required",
        "blocked",
        "decision",
        "exit_code",
        "reason_codes",
        "signature_present",
        "signature_valid",
        "signer_trusted",
        "key_id",
        "signer",
    ):
        assert key in payload
    assert payload["schema_version"] == ci_gate.RESULT_SCHEMA_VERSION
    assert payload["command"] == "gate"
    assert payload["exit_code"] == 5
    assert payload["reason_codes"] == ["REQUIREMENT_NOT_MET"]
    # no raw evidence: no bundle document body, no claim text, no paths
    assert "claim_boundary" not in payload
    assert "reason" not in payload or isinstance(payload["reason"], str)


def test_outcome_json_digest_stable_and_bounded() -> None:
    a = ci_gate.gate_bundle(make_v01_bundle("PROMOTE")).to_json_dict()
    b = ci_gate.gate_bundle(make_v01_bundle("PROMOTE")).to_json_dict()
    assert a["result_digest"] == b["result_digest"]
    assert a["result_digest"].startswith("sha256:")
    # digest recomputes over everything except the digest itself
    from serving_verdict.canonical import canonicalize

    body = {k: v for k, v in a.items() if k != "result_digest"}
    expected = (
        "sha256:" + __import__("hashlib").sha256(
            ci_gate._RESULT_DIGEST_KEY + b"\x00" + canonicalize(body)
        ).hexdigest()
    )
    assert a["result_digest"] == expected


def test_outcome_reason_bounded() -> None:
    outcome = ci_gate.gate_bundle(make_v01_bundle("REJECT"))
    payload = outcome.to_json_dict()
    assert isinstance(payload["reason"], str)
    assert len(payload["reason"]) <= ci_gate.MAX_REASON_CHARS
    assert "REQUIREMENT_NOT_MET" in payload["reason"]


def test_outcome_summary_escaped_and_bounded() -> None:
    outcome = ci_gate.gate_bundle(make_v01_bundle("REJECT"))
    lines = outcome.summary_lines()
    assert len(lines) <= ci_gate.MAX_SUMMARY_LINES
    joined = "\n".join(lines)
    assert "<" not in joined
    assert "`" not in joined
    # nothing raw: the verdict words and codes are structural constants
    assert "REJECT" in joined


def test_outcome_summary_escaping_injection() -> None:
    # case_id values are schema-constrained strings; verify escaping works
    # on any operator-facing free text the summary emits.
    text = ci_gate._escape_summary_text("a<b>&`c\nd")
    assert "<" not in text and ">" not in text
    assert "&lt;" in text and "&gt;" in text and "&amp;" in text
    assert "`" not in text
    assert "\n" not in text


# ---------------------------------------------------------------------------
# digest of the v0.4 signed path is the v0.4 canonical digest
# ---------------------------------------------------------------------------


def test_outcome_digest_matches_bundle_digest() -> None:
    doc = make_v04_bundle()
    outcome = ci_gate.gate_bundle(doc)
    assert outcome.digest == compute_v04_digest(doc)
