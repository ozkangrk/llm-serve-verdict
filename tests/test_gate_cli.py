"""CLI tests for `llm-serve-verdict gate` (FR-7): exit codes, JSON contract,
parity with `verify`, GitHub summary file, no shell interpolation.

Exit-code contract (docs/CI_INTEGRATION.md):
  0 = requirement satisfied (PROMOTE under --require PROMOTE; also a valid
      INCONCLUSIVE without --fail-inconclusive: exit 0, blocked=false)
  2 = usage/config/load error
  4 = integrity/signature/trust failure
  5 = valid REJECT / requirement not met
  6 = valid INCONCLUSIVE, only with --fail-inconclusive
"""
from __future__ import annotations

import copy
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from serving_verdict import ci_gate
from tests.helpers_ci_gate import make_v01_bundle
from tests.helpers_v04_bundle import make_signing_key, make_v04_bundle

ROOT = Path(__file__).resolve().parents[1]
CLI = str(ROOT / ".venv" / "bin" / "serving-verdict")
DSKAB_FIXTURE = ROOT / "tests" / "fixtures" / "dspark" / "case.yaml"


def run_cli(*args: str, cwd: Path = ROOT) -> subprocess.CompletedProcess[str]:
    return subprocess.run([CLI, *args], capture_output=True, text=True, cwd=cwd)


def write_json(tmp_path: Path, doc: dict[str, Any], name: str = "bundle.json") -> Path:
    p = tmp_path / name
    p.write_text(json.dumps(doc, indent=2, sort_keys=True), encoding="utf-8")
    return p


def make_trust_store_file(tmp_path: Path, seed_hex: str, signer: str) -> Path:
    from serving_verdict import signing

    key = signing.load_ed25519_private_key_from_seed_hex(seed_hex)
    from tests.helpers_v04_bundle import b64

    pub = b64(signing.public_key_bytes_of(key))
    store = {
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
    p = tmp_path / "trust.json"
    p.write_text(json.dumps(store, indent=2), encoding="utf-8")
    return p


def make_signed_v04_file(tmp_path: Path, verdict: str = "PROMOTE") -> tuple[Path, Any]:
    from serving_verdict import signing

    doc = make_v04_bundle(verdict=verdict, reason_codes=["UNIT_TEST_VERDICT"])
    ident = make_signing_key()
    identity = signing.SignerIdentity(
        private_key=ident.private_key, key_id=ident.key_id, signer=ident.signer
    )
    doc = signing.sign_bundle(doc, identity=identity)
    return write_json(tmp_path, doc), ident


def parse_stdout_json(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    """Exactly one JSON object on stdout."""
    assert result.stdout.strip(), f"expected one JSON object on stdout: {result!r}"
    return json.loads(result.stdout)  # raises on >1 object or trailing junk


# ---------------------------------------------------------------------------
# exit codes: PROMOTE / REJECT / INCONCLUSIVE under --require PROMOTE
# ---------------------------------------------------------------------------


def test_gate_promote_require_promote_exit_0(tmp_path: Path) -> None:
    p = write_json(tmp_path, make_v01_bundle("PROMOTE"))
    r = run_cli("gate", str(p), "--require", "PROMOTE")
    assert r.returncode == 0, r.stderr
    assert "llm-serve-verdict:" in r.stderr  # diagnostics on stderr only


def test_gate_reject_require_promote_exit_5(tmp_path: Path) -> None:
    p = write_json(tmp_path, make_v01_bundle("REJECT"))
    r = run_cli("gate", str(p), "--require", "PROMOTE")
    assert r.returncode == 5, (r.returncode, r.stdout, r.stderr)


def test_gate_inconclusive_default_exit_0_blocked_false(tmp_path: Path) -> None:
    p = write_json(tmp_path, make_v01_bundle("INCONCLUSIVE"))
    r = run_cli("gate", str(p), "--require", "PROMOTE", "--json")
    assert r.returncode == 0
    payload = parse_stdout_json(r)
    assert payload["blocked"] is False
    assert payload["verdict"] == "INCONCLUSIVE"
    assert payload["reason_codes"] == ["VERDICT_INCONCLUSIVE"]


def test_gate_inconclusive_fail_inconclusive_exit_6(tmp_path: Path) -> None:
    p = write_json(tmp_path, make_v01_bundle("INCONCLUSIVE"))
    r = run_cli("gate", str(p), "--require", "PROMOTE", "--fail-inconclusive", "--json")
    assert r.returncode == 6, (r.returncode, r.stdout, r.stderr)
    payload = parse_stdout_json(r)
    assert payload["blocked"] is False
    assert payload["reason_codes"] == ["VERDICT_INCONCLUSIVE", "FAIL_INCONCLUSIVE"]


def test_gate_promote_require_reject_exit_5(tmp_path: Path) -> None:
    p = write_json(tmp_path, make_v01_bundle("PROMOTE"))
    r = run_cli("gate", str(p), "--require", "REJECT")
    assert r.returncode == 5


def test_gate_require_inconclusive_accepts_inconclusive(tmp_path: Path) -> None:
    p = write_json(tmp_path, make_v01_bundle("INCONCLUSIVE"))
    r = run_cli("gate", str(p), "--require", "INCONCLUSIVE", "--json")
    assert r.returncode == 0
    assert parse_stdout_json(r)["blocked"] is True


# ---------------------------------------------------------------------------
# integrity / signature / trust failures -> exit 4
# ---------------------------------------------------------------------------


def test_gate_mutated_bundle_exit_4(tmp_path: Path) -> None:
    doc = make_v01_bundle("REJECT")
    p = write_json(tmp_path, doc)
    mutated = copy.deepcopy(doc)
    mutated["verdict"] = "PROMOTE"  # forged client verdict
    p = write_json(tmp_path, mutated, "forged.json")
    r = run_cli("gate", str(p), "--require", "PROMOTE", "--json")
    assert r.returncode == 4, (r.returncode, r.stdout, r.stderr)
    payload = parse_stdout_json(r)
    assert payload["blocked"] is False
    assert payload["code"] == "INTEGRITY_FAILURE"


def test_gate_mutated_v04_bundle_exit_4(tmp_path: Path) -> None:
    doc = make_v04_bundle()
    p = write_json(tmp_path, doc)
    mutated = copy.deepcopy(doc)
    mutated["verdict"] = "PROMOTE" if doc["verdict"] != "PROMOTE" else "REJECT"
    p = write_json(tmp_path, mutated, "mutated.json")
    r = run_cli("gate", str(p), "--require", "PROMOTE", "--json")
    assert r.returncode == 4
    assert parse_stdout_json(r)["code"] == "DIGEST_INVALID"


def test_gate_unsigned_v04_require_signature_exit_4(tmp_path: Path) -> None:
    p = write_json(tmp_path, make_v04_bundle())
    r = run_cli("gate", str(p), "--require", "PROMOTE", "--require-signature", "--json")
    assert r.returncode == 4
    assert parse_stdout_json(r)["code"] == "SIGNATURE_MISSING"


def test_gate_signed_v04_trusted_exit_0(tmp_path: Path) -> None:
    p, ident = make_signed_v04_file(tmp_path)
    store = make_trust_store_file(tmp_path, ident.seed_hex, ident.signer)
    r = run_cli(
        "gate", str(p), "--require", "PROMOTE",
        "--require-signature", "--trust-store", str(store), "--json",
    )
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    payload = parse_stdout_json(r)
    assert payload["signature_present"] is True
    assert payload["signature_valid"] is True
    assert payload["signer_trusted"] is True
    assert payload["key_id"] == ident.key_id
    assert payload["blocked"] is True


def test_gate_signed_v04_missing_store_exit_4(tmp_path: Path) -> None:
    # --require-signature without a trust store: offline key trust is
    # impossible -> UNTRUSTED_SIGNER, exit 4 (fail closed).
    p, _ = make_signed_v04_file(tmp_path)
    r = run_cli("gate", str(p), "--require", "PROMOTE", "--require-signature", "--json")
    assert r.returncode == 4
    assert parse_stdout_json(r)["code"] == "UNTRUSTED_SIGNER"


def test_gate_missing_trust_store_path_exit_2(tmp_path: Path) -> None:
    p, _ = make_signed_v04_file(tmp_path)
    r = run_cli(
        "gate", str(p), "--require", "PROMOTE",
        "--trust-store", str(tmp_path / "nope.json"), "--json",
    )
    assert r.returncode == 2
    payload = parse_stdout_json(r)
    assert payload["decision"] == "ERROR"
    assert payload["exit_code"] == 2


def test_gate_signed_reject_v04_exit_5(tmp_path: Path) -> None:
    p, ident = make_signed_v04_file(tmp_path, verdict="REJECT")
    store = make_trust_store_file(tmp_path, ident.seed_hex, ident.signer)
    r = run_cli(
        "gate", str(p), "--require", "PROMOTE",
        "--require-signature", "--trust-store", str(store),
    )
    assert r.returncode == 5


# ---------------------------------------------------------------------------
# usage errors -> exit 2
# ---------------------------------------------------------------------------


def test_gate_missing_bundle_exit_2(tmp_path: Path) -> None:
    r = run_cli("gate", str(tmp_path / "nope.json"), "--require", "PROMOTE")
    assert r.returncode == 2
    assert r.stdout == ""  # diagnostics only on stderr (non-JSON mode)


def test_gate_bad_require_choice_exit_2(tmp_path: Path) -> None:
    p = write_json(tmp_path, make_v01_bundle())
    r = run_cli("gate", str(p), "--require", "MAYBE")
    assert r.returncode == 2
    assert r.stdout == ""


def test_gate_missing_require_exit_2(tmp_path: Path) -> None:
    p = write_json(tmp_path, make_v01_bundle())
    r = run_cli("gate", str(p))
    assert r.returncode == 2


def test_gate_v01_with_require_signature_exit_2(tmp_path: Path) -> None:
    p = write_json(tmp_path, make_v01_bundle())
    r = run_cli("gate", str(p), "--require", "PROMOTE", "--require-signature", "--json")
    assert r.returncode == 2
    assert parse_stdout_json(r)["decision"] == "ERROR"


# ---------------------------------------------------------------------------
# JSON contract: exactly one object on stdout, diagnostics on stderr
# ---------------------------------------------------------------------------


def test_gate_json_stdout_exactly_one_object(tmp_path: Path) -> None:
    p = write_json(tmp_path, make_v01_bundle("PROMOTE"))
    r = run_cli("gate", str(p), "--require", "PROMOTE", "--json")
    payload = parse_stdout_json(r)
    assert payload["schema_version"] == ci_gate.RESULT_SCHEMA_VERSION
    assert payload["command"] == "gate"
    assert payload["exit_code"] == 0
    assert payload["reason"] == "requirement satisfied"
    # stderr is diagnostics, never part of the JSON contract
    assert "llm-serve-verdict:" not in r.stdout
    assert r.stdout.count("schema_version") == 1


def test_gate_json_digest_self_consistent(tmp_path: Path) -> None:
    import hashlib

    from serving_verdict.canonical import canonicalize

    p = write_json(tmp_path, make_v01_bundle("REJECT"))
    r = run_cli("gate", str(p), "--require", "PROMOTE", "--json")
    payload = parse_stdout_json(r)
    body = {k: v for k, v in payload.items() if k != "result_digest"}
    expected = (
        "sha256:"
        + hashlib.sha256(ci_gate._RESULT_DIGEST_KEY + b"\x00" + canonicalize(body)).hexdigest()
    )
    assert payload["result_digest"] == expected


def test_gate_non_json_mode_stdout_empty(tmp_path: Path) -> None:
    for verdict, code in (("PROMOTE", 0), ("REJECT", 5), ("INCONCLUSIVE", 0)):
        p = write_json(tmp_path, make_v01_bundle(verdict), f"b-{verdict}.json")
        r = run_cli("gate", str(p), "--require", "PROMOTE")
        assert r.stdout == "", (verdict, r.stdout)
        assert r.returncode == code
        assert "llm-serve-verdict: gate" in r.stderr


# ---------------------------------------------------------------------------
# v0.1 parity: gate uses the same verify path as `verify`
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mutate", [True, False])
def test_gate_v01_parity_with_verify(tmp_path: Path, mutate: bool) -> None:
    doc = make_v01_bundle("PROMOTE")
    if mutate:
        doc["verdict"] = "REJECT"
        doc["reason_codes"] = ["FORGED"]
    p = write_json(tmp_path, doc)
    r_verify = run_cli("verify", str(p), "--json")
    r_gate = run_cli("gate", str(p), "--require", "PROMOTE", "--json")
    if mutate:
        assert r_verify.returncode == 4
        assert r_gate.returncode == 4
        assert parse_stdout_json(r_verify)["valid"] is False
        assert parse_stdout_json(r_gate)["blocked"] is False
    else:
        assert r_verify.returncode == 0
        assert r_gate.returncode == 0
        v = parse_stdout_json(r_verify)
        g = parse_stdout_json(r_gate)
        assert v["digest"] == g["bundle_digest"]  # same verify path, same digest


def test_gate_v01_real_fixture_promote(tmp_path: Path) -> None:
    out = tmp_path / "d.verdict.json"
    assert run_cli("import-case", str(DSKAB_FIXTURE), "--out", str(out)).returncode == 0
    r = run_cli("gate", str(out), "--require", "PROMOTE", "--json")
    assert r.returncode == 0
    payload = parse_stdout_json(r)
    assert payload["verdict"] == "PROMOTE"
    assert payload["bundle_version"] == "serving-verdict.bundle.v0.1"
    assert payload["case_id"] == "fixture-dspark"


def test_gate_v01_real_fixture_mismatched_hash(tmp_path: Path) -> None:
    """A valid INCONCLUSIVE import (hash mismatch) gates as exit 0 / blocked
    false by default and exit 6 with --fail-inconclusive."""
    case_src = (ROOT / "tests" / "fixtures" / "dspark" / "case.yaml")
    import yaml

    cfg = yaml.safe_load(case_src.read_text())
    cfg["baseline"]["sha256"] = "0" * 64  # force EVIDENCE_HASH_MISMATCH
    cfg_p = tmp_path / "case-badhash.yaml"
    cfg_p.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    out = tmp_path / "inconclusive.verdict.json"
    assert (
        run_cli("import-case", str(cfg_p), "--out", str(out), "--json").returncode == 0
    )
    r = run_cli("gate", str(out), "--require", "PROMOTE", "--json")
    assert r.returncode == 0
    assert parse_stdout_json(r)["blocked"] is False
    r = run_cli("gate", str(out), "--require", "PROMOTE", "--fail-inconclusive")
    assert r.returncode == 6


# ---------------------------------------------------------------------------
# GitHub summary file: escaped, bounded, no raw evidence
# ---------------------------------------------------------------------------


def test_gate_github_summary_written_and_clean(tmp_path: Path) -> None:
    p = write_json(tmp_path, make_v01_bundle("REJECT"))
    summary = tmp_path / "summary.md"
    r = run_cli("gate", str(p), "--require", "PROMOTE", "--github-summary", str(summary))
    assert r.returncode == 5
    text = summary.read_text(encoding="utf-8")
    assert len(text) <= ci_gate.MAX_SUMMARY_CHARS + 64
    assert "<" not in text
    assert "`" not in text
    assert "REQUIREMENT_NOT_MET" in text
    # no raw evidence: no claim boundary text, no artifact sha values
    assert "unit-test bundle" not in text
    assert "a" * 64 not in text


def test_gate_github_summary_missing_parent_exit_2(tmp_path: Path) -> None:
    p = write_json(tmp_path, make_v01_bundle("PROMOTE"))
    r = run_cli(
        "gate", str(p), "--require", "PROMOTE",
        "--github-summary", str(tmp_path / "no" / "dir" / "s.md"),
    )
    assert r.returncode == 2
    assert not (tmp_path / "no").exists()  # the gate never creates directories


# ---------------------------------------------------------------------------
# command parity: `verify` and `gate` agree on validity for the same file
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "doc,expected_valid",
    [
        (make_v04_bundle(), True),
        (make_v04_bundle(verdict="REJECT"), True),
        (make_v04_bundle(verdict="INCONCLUSIVE"), True),
    ],
)
def test_gate_verify_command_parity_v04(
    tmp_path: Path, doc: dict[str, Any], expected_valid: bool
) -> None:
    p = write_json(tmp_path, doc)
    r_verify = run_cli("verify", str(p), "--json")
    r_gate = run_cli("gate", str(p), "--require", "PROMOTE", "--json")
    v = parse_stdout_json(r_verify)
    g = parse_stdout_json(r_gate)
    assert v["valid"] is expected_valid
    assert v["digest"] == g["bundle_digest"]
    assert v["verdict"] == g["verdict"]
    # verdict policy applied on top of the same verification result
    assert g["blocked"] is (doc["verdict"] == "PROMOTE")
    expected = {
        "PROMOTE": 0,
        "REJECT": 5,
        "INCONCLUSIVE": 0,
    }[doc["verdict"]]
    assert r_gate.returncode == expected


def test_gate_never_accepts_forged_verdict_field(tmp_path: Path) -> None:
    """Even with a matching-looking digest field, a tampered verdict is a
    digest failure: the gate reads the verdict only from the sealed doc."""
    doc = make_v01_bundle("PROMOTE")
    doc["bundle_digest"] = "sha256:" + "0" * 64  # attacker overwrites digest too
    p = write_json(tmp_path, doc)
    r = run_cli("gate", str(p), "--require", "PROMOTE", "--json")
    assert r.returncode == 4
    assert parse_stdout_json(r)["code"] == "INTEGRITY_FAILURE"


# ---------------------------------------------------------------------------
# no shell interpolation of untrusted paths
# ---------------------------------------------------------------------------


def test_gate_path_with_shell_metachars(tmp_path: Path) -> None:
    evil_name = tmp_path / "bundle;touch pwned.json"
    evil_name.write_text(
        json.dumps(make_v01_bundle("PROMOTE"), sort_keys=True), encoding="utf-8"
    )
    # run through a shell: the path must survive quoting as one token
    r = subprocess.run(
        [CLI, "gate", str(evil_name), "--require", "PROMOTE", "--json"],
        capture_output=True,
        text=True,
        shell=False,
        cwd=ROOT,
    )
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    assert parse_stdout_json(r)["case_id"] == "fixture-v01"
    # and the subprocess list form is what the tests use; a raw shell would
    # execute $(...) — prove the file itself was found, not interpreted:
    assert not (tmp_path / "pwned").exists()
    env = dict(os.environ)
    r2 = subprocess.run(
        f'{CLI} gate "{evil_name}" --require PROMOTE --json',
        capture_output=True,
        text=True,
        shell=True,
        cwd=ROOT,
        env=env,
    )
    assert r2.returncode == 0, (r2.returncode, r2.stdout, r2.stderr)
    assert parse_stdout_json(r2)["case_id"] == "fixture-v01"
    assert not (tmp_path / "pwned").exists()
