"""F5/F7: DOM-level proof for the v0.2 verdict-first UI.

Executes the *actual shipped* ``ui.js`` against the QuickJS DOM harness in
``tests/helpers_ui.py`` and reads the rendered result back out of the DOM:

- element anchors are parsed from the real ``src/serving_verdict/web/index.html``
  (the test fails if an id the UI relies on is missing from the markup);
- documents are produced by the real ``import_case`` engine (one per verdict:
  PROMOTE / REJECT / INCONCLUSIVE) and shaped exactly like the
  ``verdict_detail`` API response;
- error paths use the server's flat error bodies (``{"error": ...}`` with
  status 404 / 422) and an opaque network failure (fetch rejects) — none of
  them may render a fake INCONCLUSIVE verdict pill;
- navigation drives the UI's public entry points only (hash routing + the
  ``fetch`` contract + click listeners); no test hooks in shipped code.

No Node toolchain, no npm, no CDN: the JS engine is the ``quickjs`` Python
binding (a single prebuilt wheel).
"""
from __future__ import annotations

import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from serving_verdict.engine import import_case
from tests.helpers import sha256_file
from tests.helpers_ui import (
    WEB,
    boot,
    click,
    dump,
    go_index,
    navigate,
    set_json,
)

ROOT = Path(__file__).resolve().parents[1]
DSKAB_FIXTURE = ROOT / "tests" / "fixtures" / "dspark" / "case.yaml"
SGLANG_FIXTURE = ROOT / "tests" / "fixtures" / "sglang" / "case.yaml"

ALL_VERDICT_PILLS = ("PROMOTE", "REJECT", "INCONCLUSIVE")


def _ui_js() -> str:
    return (WEB / "ui.js").read_text(encoding="utf-8")


def _utc_date(iso_timestamp: str) -> str:
    return datetime.fromisoformat(iso_timestamp).astimezone(UTC).date().isoformat()


def _detail_response(bundle: dict) -> dict:
    """Shape a bundle exactly like the server's /api/v1/verdicts/{case_id} body."""
    return {
        "bundle": bundle,
        "integrity": {"valid": True},
        "case_id": bundle["case_id"],
        "verdict": bundle["verdict"],
        "reason_codes": bundle["reason_codes"],
        "baseline": bundle["baseline"],
        "candidate": bundle["candidate"],
        "comparisons": bundle["comparisons"],
        "gates": bundle["gates"],
        "claim_boundary": bundle["claim_boundary"],
        "bundle_digest": bundle["bundle_digest"],
        "created_at": bundle["created_at"],
    }


def _inconclusive_bundle(tmp_path: Path) -> dict:
    """Real INCONCLUSIVE bundle via a deliberate baseline hash mismatch."""
    import yaml

    src = yaml.safe_load(DSKAB_FIXTURE.read_text(encoding="utf-8"))
    fixture_dir = DSKAB_FIXTURE.parent
    for name in ("baseline_mtp2.json", "candidate_k7.json"):
        (tmp_path / name).write_bytes((fixture_dir / name).read_bytes())
    src["source_root"] = str(tmp_path)
    src["baseline"] = {
        "artifact": "baseline_mtp2.json",
        "sha256": "0" * 64,  # deliberate mismatch
    }
    src["candidate"] = {
        "artifact": "candidate_k7.json",
        "sha256": sha256_file(tmp_path / "candidate_k7.json"),
    }
    src["supplemental_evidence"] = []
    case = tmp_path / "case.yaml"
    case.write_text(yaml.safe_dump(src, sort_keys=False), encoding="utf-8")
    return import_case(str(case))


def _assert_no_fake_verdict(doc: dict) -> None:
    """Error states must never masquerade as a verdict: no detail view with a
    PROMOTE/REJECT/INCONCLUSIVE pill, empty verdict label, no detail body."""
    assert doc["visibleView"] != "detail-view", doc["visibleView"]
    assert doc["verdictLabel"] == ""
    assert not any(
        t in doc["verdictClass"].split() for t in ("promote", "reject", "inconclusive")
    ), doc["verdictClass"]
    assert not doc["detailBodyVisible"]


def _gate_row(doc: dict, gate_id: str) -> dict:
    for row in doc["gateRows"]:
        if row and row[0]["text"] == gate_id:
            return {"status": row[1], "authority": row[2], "evidence": row[3]}
    raise AssertionError(f"gate row for {gate_id!r} not rendered; got {doc['gateRows']}")


# ---------------------------------------------------------------------------
# index: empty state (neutral onboarding + copyable commands)
# ---------------------------------------------------------------------------


def test_index_empty_state_is_neutral_with_copyable_commands() -> None:
    ctx = boot(_ui_js())
    set_json(ctx, "__routes", {"/api/v1/verdicts": {"data_dir": "/tmp/data", "verdicts": []}})
    go_index(ctx)
    doc = dump(ctx)
    assert doc["visibleView"] == "index-view"
    assert not doc["indexErrorVisible"]
    assert doc["indexEmptyVisible"], "empty state must render, not an error box"
    assert not doc["indexLoadingVisible"]
    # neutral tone: no error/failed wording on the empty state itself
    empty_text = doc["indexEmptyText"].lower()
    assert "failed" not in empty_text
    assert "error" not in empty_text
    # two onboarding commands, each with a copy button that copies the exact cmd
    cmds = doc["onboardingCmds"]
    assert [c["cmd"] for c in cmds] == [
        "serving-verdict import-case CASE.yaml --out data/BUNDLE.json",
        "serving-verdict serve --host 127.0.0.1 --port 8787 --data-dir data",
    ]
    for c in cmds:
        assert c["copyAria"], f"copy button needs aria-label: {c}"
        assert c["copyValue"] == c["cmd"], "copy button must copy the exact command"
    click(ctx, "copy-onb-0")
    doc = dump(ctx)
    assert doc["lastCopied"] == cmds[0]["cmd"]
    assert "copied" in doc["copyLive"]["text"].lower()
    assert doc["copyLive"]["ariaLive"] == "polite"
    assert doc["copyLive"]["role"] == "status"


# ---------------------------------------------------------------------------
# index: verdict-first list + document title + created_at
# ---------------------------------------------------------------------------


def test_index_list_verdict_first_with_created_at_and_document_title(tmp_path: Path) -> None:
    d = import_case(str(DSKAB_FIXTURE))
    s = import_case(str(SGLANG_FIXTURE))

    def row(b):
        return {
            "case_id": b["case_id"],
            "file": "x.json",
            "verdict": b["verdict"],
            "reason_codes": b["reason_codes"],
            "bundle_digest": b["bundle_digest"],
            "created_at": b["created_at"],
        }

    ctx = boot(_ui_js())
    set_json(
        ctx,
        "__routes",
        {"/api/v1/verdicts": {"data_dir": str(tmp_path), "verdicts": [row(d), row(s)]}},
    )
    go_index(ctx)
    doc = dump(ctx)
    assert doc["visibleView"] == "index-view"
    assert doc["verdictCount"] == "2 verdicts"
    cards = doc["listCards"]
    assert len(cards) == 2
    # verdict-first: the card's aria-label leads with the verdict text
    assert cards[0]["ariaLabel"].startswith("PROMOTE ·")
    assert cards[1]["ariaLabel"].startswith("REJECT ·")
    for card, b in zip(cards, (d, s), strict=True):
        assert card["href"] == "#/" + b["case_id"]
        assert b["case_id"] in card["text"]
        assert _utc_date(b["created_at"]) in card["text"], "created_at (UTC date) rendered"
    # visible H1 carries the view title
    assert "Verdicts" in doc["listHeading"]


def test_index_loading_then_empty_state() -> None:
    """After the fetch settles, the loading state is removed and the empty
    state (role-carrying panel) is the only content shown."""
    ctx = boot(_ui_js())
    set_json(ctx, "__routes", {"/api/v1/verdicts": {"verdicts": []}})
    go_index(ctx)
    doc = dump(ctx)
    assert not doc["indexLoadingVisible"]
    assert doc["indexEmptyVisible"]


# ---------------------------------------------------------------------------
# detail: the three verdict states actually render into the DOM
# ---------------------------------------------------------------------------


def test_ui_renders_promote_verdict_authority_and_hashes(tmp_path: Path) -> None:
    ctx = boot(_ui_js())
    bundle = import_case(str(DSKAB_FIXTURE))
    assert bundle["verdict"] == "PROMOTE"
    doc = navigate(ctx, "fixture-dspark", _detail_response(bundle))

    # verdict-first: detail view visible, text label AND state class in the DOM
    assert doc["visibleView"] == "detail-view"
    assert doc["verdictLabel"] == "PROMOTE"
    assert "promote" in doc["verdictClass"].split()
    assert doc["caseId"] == "fixture-dspark"
    # reason codes rendered as chips
    assert "PRIMARY_EFFECT_PASSED" in doc["reasonCodes"]
    # created_at rendered as a human date, full ISO in the title attribute
    assert _utc_date(bundle["created_at"]) in doc["createdText"]
    # hashes rendered into the DOM (full 64-hex values)
    assert doc["baselineSha"] == bundle["baseline"]["sha256"]
    assert len(doc["baselineSha"]) == 64
    assert doc["candidateSha"] == bundle["candidate"]["sha256"]
    assert doc["bundleDigest"] == bundle["bundle_digest"]
    assert doc["bundleDigest"].startswith("sha256:")
    assert doc["integrityText"].startswith("verified")
    # document title is verdict-first
    assert doc["documentTitle"] == "PROMOTE · fixture-dspark · LLM ServeVerdict"
    # gate authority: machine vs operator attested, rendered per gate
    req = _gate_row(doc, "request_success")
    assert req["status"]["text"] == "pass"
    assert req["authority"]["text"] == "machine_measured"
    att = _gate_row(doc, "arithmetic")
    assert att["authority"]["text"] == "operator_attested"
    assert att["evidence"]["text"]  # evidence source shown, not empty
    # PROMOTE: no reject narrative section
    assert not doc["narrative"]["visible"]
    # claim boundary rendered
    assert doc["claimBoundary"]
    # document title set
    assert doc["documentTitle"].startswith("PROMOTE ·")


def test_ui_renders_reject_verdict_in_dom_with_narrative_gate_link(tmp_path: Path) -> None:
    ctx = boot(_ui_js())
    bundle = import_case(str(SGLANG_FIXTURE))
    assert bundle["verdict"] == "REJECT"
    doc = navigate(ctx, "fixture-sglang", _detail_response(bundle))
    assert doc["visibleView"] == "detail-view"
    assert doc["verdictLabel"] == "REJECT"
    assert "reject" in doc["verdictClass"].split()
    assert doc["caseId"] == "fixture-sglang"
    assert "HARD_GATE_FAILED" in doc["reasonCodes"]
    # the failing attested gate must be visible with its authority
    row = _gate_row(doc, "process_stability")
    assert row["status"]["text"] == "fail"
    assert row["authority"]["text"] == "operator_attested"
    assert len(doc["baselineSha"]) == 64 and len(doc["candidateSha"]) == 64
    # REJECT narrative: fail gate linked (marker) into the gates table
    assert doc["narrative"]["visible"]
    links = [
        item
        for item in doc["narrative"]["links"]
        if "process_stability" in item["text"]
    ]
    assert links, f"narrative must name the failing gate: {doc['narrative']['links']}"
    link = links[0]
    assert link["href"] == "#gates-table"
    assert "FAIL" in link["text"].upper()
    assert "marker" in link["cls"].split()
    # narrative prose explains the fail gate consequence
    assert "process_stability" in doc["narrative"]["text"]
    assert "FAIL" in doc["narrative"]["text"]
    assert doc["documentTitle"].startswith("REJECT ·")


def test_ui_renders_inconclusive_verdict_in_dom(tmp_path: Path) -> None:
    ctx = boot(_ui_js())
    bundle = _inconclusive_bundle(tmp_path)
    assert bundle["verdict"] == "INCONCLUSIVE"
    doc = navigate(ctx, "inconclusive-case", _detail_response(bundle))
    assert doc["visibleView"] == "detail-view"
    assert doc["verdictLabel"] == "INCONCLUSIVE"
    assert "inconclusive" in doc["verdictClass"].split()
    assert "EVIDENCE_HASH_MISMATCH" in doc["reasonCodes"]
    # bound-ref hashes still render even for an inconclusive import
    assert len(doc["baselineSha"]) == 64 and len(doc["candidateSha"]) == 64
    assert doc["bundleDigest"] == bundle["bundle_digest"]
    # INCONCLUSIVE: no reject narrative
    assert not doc["narrative"]["visible"]
    assert doc["documentTitle"].startswith("INCONCLUSIVE ·")


# ---------------------------------------------------------------------------
# error panels: 404 / 422 / network failure — never a fake verdict pill
# ---------------------------------------------------------------------------


def test_detail_404_renders_not_found_panel_without_fake_verdict() -> None:
    ctx = boot(_ui_js())
    doc = navigate(ctx, "missing-case", {"status": 404, "body": {"error": "case not found"}})
    assert doc["visibleView"] == "error-view"
    assert doc["errorTitle"] == "Case not found"
    assert "missing-case" in doc["errorText"]
    assert doc["errorRole"] == "alert"
    assert doc["errorRetryVisible"]
    assert doc["backVisible"]
    _assert_no_fake_verdict(doc)
    # back button returns to a working index
    set_json(ctx, "__routes", {"/api/v1/verdicts": {"verdicts": []}})
    go_index(ctx)
    doc = dump(ctx)
    assert doc["visibleView"] == "index-view"


def test_detail_422_renders_invalid_panel_without_fake_verdict() -> None:
    ctx = boot(_ui_js())
    doc = navigate(
        ctx,
        "tampered-case",
        {
            "status": 422,
            "body": {"error": "bundle integrity verification failed: bundle digest mismatch"},
        },
    )
    assert doc["visibleView"] == "error-view"
    assert doc["errorTitle"] == "Invalid bundle"
    # stable machine-readable error surfaced, not invented prose
    assert "bundle digest mismatch" in doc["errorText"]
    # advice explains the tamper-evidence contract (claim boundary intact)
    assert "integrity" in doc["errorAdvice"].lower()
    assert doc["errorRole"] == "alert"
    _assert_no_fake_verdict(doc)


def test_detail_network_failure_renders_error_panel_without_fake_verdict() -> None:
    """Opaque fetch rejection: a dedicated Error panel, never a verdict pill
    and never a stale detail fallback."""
    ctx = boot(_ui_js())
    set_json(ctx, "__routes", {})
    ctx.eval(
        "var realFetch = fetch; "
        "fetch = function (p) { return Promise.reject(new Error('network down')); };"
    )
    try:
        doc = navigate(ctx, "down-case", None)
    finally:
        ctx.eval("fetch = realFetch;")
    assert doc["visibleView"] == "error-view"
    assert doc["errorTitle"] == "Failed to load"
    assert "network down" in doc["errorText"]
    assert doc["errorRetryVisible"]
    _assert_no_fake_verdict(doc)


def test_error_retry_and_back_restore_index() -> None:
    ctx = boot(_ui_js())
    doc = navigate(ctx, "missing", {"status": 404, "body": {"error": "case not found"}})
    assert doc["visibleView"] == "error-view"
    set_json(ctx, "__routes", {"/api/v1/verdicts": {"verdicts": []}})
    click(ctx, "error-back")
    doc = dump(ctx)
    assert doc["visibleView"] == "index-view"
    assert doc["indexEmptyVisible"]


# ---------------------------------------------------------------------------
# metrics: 4-significant-digit values, full titles, delta good/bad/neutral
# ---------------------------------------------------------------------------


def _dims() -> dict:
    return {
        "unit": "tok/s",
        "procedure_version": "v1",
        "workload_id": "edit_cold",
        "concurrency": 1,
        "output_budget": 1200,
        "thinking_mode": "disabled",
        "warm_cold": "cold",
        "aggregation": "median",
    }


def _dspark_doc() -> dict:
    """Deterministic comparison rows covering all three delta polarities."""
    return {
        "bundle": None,
        "integrity": {"valid": True},
        "case_id": "fixture-dspark",
        "verdict": "PROMOTE",
        "reason_codes": ["PRIMARY_EFFECT_PASSED"],
        "baseline": {"artifact_id": "baseline_mtp2.json", "sha256": "a" * 64},
        "candidate": {"artifact_id": "candidate_k7.json", "sha256": "b" * 64},
        "comparisons": [
            {  # higher_better, improved -> good (+20%)
                "metric": "decode_tokens_per_s",
                "direction": "higher_better",
                "baseline_value": 25.0,
                "candidate_value": 30.0,
                "relative_delta": 0.2,
                "unit": "tok/s",
                "dimensions": _dims(),
            },
            {  # lower_better, improved -> good (-50%)
                "metric": "api_latency_s",
                "direction": "lower_better",
                "baseline_value": 40.0,
                "candidate_value": 20.0,
                "relative_delta": -0.5,
                "unit": "s",
                "dimensions": dict(_dims(), unit="s"),
            },
            {  # exactly flat -> neutral (0%)
                "metric": "e2e_output_tokens_per_s",
                "direction": "higher_better",
                "baseline_value": 1.2345,
                "candidate_value": 1.2345,
                "relative_delta": 0.0,
                "unit": "tok/s",
                "dimensions": _dims(),
            },
        ],
        "gates": [],
        "claim_boundary": "fixture",
        "bundle_digest": "sha256:" + "c" * 64,
        "created_at": "2026-08-18T07:59:07+00:00",
    }


def _cell(doc: dict, card_idx: int, cls: str) -> dict:
    card = doc["metricCards"][card_idx]
    for c in card["cells"]:
        if cls in c["cls"].split():
            return c
    raise AssertionError(f"no cell with class {cls!r} in card {card!r}")


def test_metric_cards_4sig_digits_full_title_and_delta_direction() -> None:
    ctx = boot(_ui_js())
    doc = navigate(ctx, "fixture-dspark", _dspark_doc())
    assert doc["visibleView"] == "detail-view"
    assert len(doc["metricCards"]) == 3
    # mobile metric cards are rendered alongside the (desktop) table
    assert len(doc["metricsRows"]) == 3

    # card 0: higher_better +20% -> good; values at 4 significant digits
    assert doc["metricCards"][0]["id"] == "mcard-decode_tokens_per_s"
    assert doc["metricCards"][0]["title"] == "decode_tokens_per_s"
    b = _cell(doc, 0, "metric-baseline")
    c = _cell(doc, 0, "metric-candidate")
    assert b["value"] == "25.00"
    assert c["value"] == "30.00"
    # full-precision values live in the title attribute
    assert b["title"] == "25"
    assert c["title"] == "30"
    d = _cell(doc, 0, "metric-delta")
    assert d["value"] == "▲ +20.00%"
    assert "delta-good" in d["cls"].split()
    # arrow (ok marker) plus text
    assert d["value"].startswith(("▲", "·", "▼"))

    # card 1: lower_better -50% -> good (decrease improves)
    d = _cell(doc, 1, "metric-delta")
    assert d["value"] == "▲ -50.00%"
    assert "delta-good" in d["cls"].split()

    # card 2: flat -> neutral, and 4-significant-digit rounding (1.2345 -> 1.235)
    d = _cell(doc, 2, "metric-delta")
    assert d["value"] == "· +0.00%"
    assert "delta-neutral" in d["cls"].split()
    assert "delta-good" not in d["cls"].split() and "delta-bad" not in d["cls"].split()
    b = _cell(doc, 2, "metric-baseline")
    assert b["value"] == "1.235"
    assert b["title"] == "1.2345"
    # unit visible on the card labels; aggregation present in the dimensions block
    card2 = doc["metricCards"][2]
    joined = " ".join(c["label"] + " " + c["value"] for c in card2["cells"])
    assert "tok/s" in joined
    assert card2["dims"] is not None
    assert "median" in card2["dims"]["text"]
    # dimensions block starts collapsed
    assert card2["dims"]["visible"] is False


def test_delta_bad_when_direction_disfavored() -> None:
    ctx = boot(_ui_js())
    doc = _dspark_doc()
    row = dict(doc["comparisons"][0])
    row["relative_delta"] = -0.1  # higher_better but decreased -> bad
    doc["comparisons"] = [row] + doc["comparisons"][1:]
    out = navigate(ctx, "fixture-dspark", doc)
    d = _cell(out, 0, "metric-delta")
    assert d["value"] == "▼ -10.00%"
    assert "delta-bad" in d["cls"].split()
    assert "delta-good" not in d["cls"].split()


def test_metric_delta_title_carries_full_precision() -> None:
    ctx = boot(_ui_js())
    doc = _dspark_doc()
    row = dict(doc["comparisons"][0])
    row["relative_delta"] = 1.4695550351288058  # full-precision engine value
    doc["comparisons"] = [row]
    out = navigate(ctx, "fixture-dspark", doc)
    d = _cell(out, 0, "metric-delta")
    # displayed at 4 significant digits (146.955503...% -> 147.0%):
    assert d["value"] == "▲ +147.0%"
    # the exact engine value (6-dp percentage) survives in the title
    assert "146.955504" in d["title"], d["title"]


def test_metric_card_dimensions_expand_collapse() -> None:
    ctx = boot(_ui_js())
    doc = navigate(ctx, "fixture-dspark", _dspark_doc())
    card = doc["metricCards"][0]
    assert card["expandBtn"], "dimensions expand button must exist"
    assert card["expandBtn"]["ariaExpanded"] == "false"
    assert card["expandBtn"]["ariaControls"] == "mdims-decode_tokens_per_s"
    assert card["dims"] is not None and card["dims"]["visible"] is False
    click(ctx, card["expandBtn"]["id"])
    doc = dump(ctx)
    card = doc["metricCards"][0]
    assert card["expandBtn"]["ariaExpanded"] == "true"
    assert card["dims"]["visible"] is True
    # dimensions content includes the matched comparison dimensions
    assert "procedure_version" in card["dims"]["text"].lower()
    assert "edit_cold" in card["dims"]["text"]
    assert "median" in card["dims"]["text"]
    # collapse again
    click(ctx, card["expandBtn"]["id"])
    doc = dump(ctx)
    assert doc["metricCards"][0]["dims"]["visible"] is False
    assert doc["metricCards"][0]["expandBtn"]["ariaExpanded"] == "false"


def test_metric_cards_degrade_fail_closed_for_missing_fields() -> None:
    """A comparison row with missing values/direction must render — em-dash
    cells, neutral delta — never a crash or a fabricated number."""
    ctx = boot(_ui_js())
    doc = _dspark_doc()
    doc["comparisons"] = [
        {"metric": "decode_tokens_per_s"},
    ]
    out = navigate(ctx, "fixture-dspark", doc)
    card = out["metricCards"][0]
    joined = " ".join(c["value"] for c in card["cells"])
    assert "—" in joined
    d = _cell(out, 0, "metric-delta")
    assert "delta-neutral" in d["cls"].split()
    assert "0" not in d["value"]  # no fabricated zero
    # table row also degrades
    assert out["metricsRows"] and out["metricsRows"][0][0]["text"] == "decode_tokens_per_s"


# ---------------------------------------------------------------------------
# copy buttons + aria-live (hashes and onboarding commands)
# ---------------------------------------------------------------------------


def test_hash_copy_buttons_with_aria_live(tmp_path: Path) -> None:
    ctx = boot(_ui_js())
    bundle = import_case(str(DSKAB_FIXTURE))
    doc = navigate(ctx, "fixture-dspark", _detail_response(bundle))
    for b in doc["copyButtons"]:
        assert b["present"], f"copy button missing for {b['host']}"
        assert b["aria"] and "copy" in b["aria"].lower()
        # copy value is the full hash (digests carry the canonical sha256: prefix)
        assert re.fullmatch(r"(sha256:)?[0-9a-f]{64}", b["value"]), f"copy value not a full hash: {b}"
    click(ctx, "copy-baseline-sha")
    doc = dump(ctx)
    assert doc["lastCopied"] == bundle["baseline"]["sha256"]
    assert "baseline hash" in doc["copyLive"]["text"].lower()
    assert "copied" in doc["copyLive"]["text"].lower()
    click(ctx, "copy-bundle-digest")
    doc = dump(ctx)
    assert doc["lastCopied"] == bundle["bundle_digest"]
    assert "bundle digest" in doc["copyLive"]["text"].lower()


# ---------------------------------------------------------------------------
# malformed detail must render fail-closed, not crash / fake verdict
# ---------------------------------------------------------------------------


def test_ui_renders_null_baseline_without_crash() -> None:
    """A detail document with null baseline/candidate must not crash the UI —
    it renders the real verdict with empty hash cells (fail-closed)."""
    ctx = boot(_ui_js())
    malformed = {
        "bundle": None,
        "integrity": None,
        "case_id": "malformed-case",
        "verdict": "REJECT",
        "reason_codes": ["INSUFFICIENT_EFFECT"],
        "baseline": None,
        "candidate": None,
        "comparisons": [{"metric": "decode_tokens_per_s"}],  # missing values/dims
        "gates": [{"id": "g1", "status": "pass"}],  # missing authority/evidence
        "claim_boundary": None,
        "bundle_digest": "sha256:" + "0" * 64,
    }
    doc = navigate(ctx, "malformed-case", malformed)
    # no crash: the real verdict is rendered, NOT an error view or load-failed pill
    assert doc["visibleView"] == "detail-view"
    assert doc["verdictLabel"] == "REJECT"
    assert doc["caseId"] == "malformed-case"
    assert "load failed" not in doc["integrityText"].lower()
    # null refs degrade to empty cells, not the string "null" or a throw
    assert doc["baselineSha"] == "" and doc["candidateSha"] == ""
    assert doc["baselineArtifact"] == "" and doc["candidateArtifact"] == ""
    # integrity field absent -> explicit INTEGRITY FAILURE, class bad
    assert "INTEGRITY FAILURE" in doc["integrityText"]
    assert "bad" in doc["integrityClass"].split()
    # comparison row with missing values degrades to em-dash cells, not a crash
    assert doc["metricCards"] and doc["metricsRows"]
    # gate row still renders its id and status (missing authority is not fatal)
    row = _gate_row(doc, "g1")
    assert row["status"]["text"] == "pass"
    # malformed gate narrative input (no fail gates) -> no narrative section
    assert not doc["narrative"]["visible"]
    # claim boundary null -> empty, not "None"/"null"
    assert doc["claimBoundary"] == ""


def test_ui_served_assets_remain_offline_self_contained() -> None:
    """The no-CDN / no-external-network contract must hold on the served assets."""
    js = (WEB / "ui.js").read_text(encoding="utf-8")
    html = (WEB / "index.html").read_text(encoding="utf-8")
    for token in ("http://", "https://", "cdn.", "XMLHttpRequest"):
        assert token not in js, f"ui.js must not reference external network: {token}"
        assert token not in html, f"index.html must not reference external network: {token}"
    # relative same-origin references only
    assert 'src="/ui.js"' in html and 'href="/ui.css"' in html


# ---------------------------------------------------------------------------
# a11y structural contract: H1s, table caption/scope, tap targets, motion
# ---------------------------------------------------------------------------


def test_a11y_structural_contract_in_shipped_markup_and_css() -> None:
    html = (WEB / "index.html").read_text(encoding="utf-8")
    css = (WEB / "ui.css").read_text(encoding="utf-8")

    # one H1 per top-level view (index, error, detail, automation)
    assert re.search(r'<h1[^>]*id="list-heading"', html)
    assert re.search(r'<h1[^>]*id="error-title"', html)
    assert re.search(r'<h1[^>]*id="detail-case-id"', html)
    assert re.search(r'<h1[^>]*id="automation-heading"', html)
    assert html.count("<h1") == 4, "exactly one H1 per top-level view"
    # tables have captions + th scope (structural a11y)
    assert "metrics-caption" in html and "gates-caption" in html
    assert html.count("<caption") >= 2
    assert 'scope="col"' in html and 'scope="row"' in html
    # copy live region announced politely
    assert 'id="copy-live"' in html and 'aria-live="polite"' in html
    # error view is an alert
    assert 'id="error-view"' in html and 'role="alert"' in html
    # metric dimensions disclosure uses button + aria-expanded (in JS)
    js = (WEB / "ui.js").read_text(encoding="utf-8")
    assert "aria-expanded" in js and "aria-controls" in js
    # focus-visible styles present
    assert ":focus-visible" in css
    # reduced motion honored
    assert "prefers-reduced-motion" in css
    # >=44px tap targets on interactive elements
    for sel in (
        "#back-btn",
        ".copy-btn",
        ".card",
        "#index-retry",
        "#error-retry",
        "#error-back",
        ".metric-expand",
    ):
        assert re.search(re.escape(sel) + r"\s*\{", css), f"tap-target rule missing for {sel}"
    assert "44px" in css


def test_detail_gate_rows_use_scope_row_headers() -> None:
    """The gates table first column is a row header (th[scope=row]) so screen
    readers pair each row with its label."""
    ctx = boot(_ui_js())
    bundle = import_case(str(DSKAB_FIXTURE))
    doc = navigate(ctx, "fixture-dspark", _detail_response(bundle))
    assert doc["gateRows"], "gates table rendered"
    row = _gate_row(doc, "request_success")
    assert row["status"]["text"] == "pass"


# ---------------------------------------------------------------------------
# engine smoke (guards the documents the DOM test consumes)
# ---------------------------------------------------------------------------


def test_fixture_bundles_stable_for_dom_documents() -> None:
    """import_case on the real fixtures stays PROMOTE/REJECT — the DOM test
    documents must not drift from the engine's real output."""
    d = import_case(str(DSKAB_FIXTURE))
    s = import_case(str(SGLANG_FIXTURE))
    assert d["verdict"] == "PROMOTE"
    assert s["verdict"] == "REJECT"
    for b in (d, s):
        assert re.fullmatch(r"[0-9a-f]{64}", b["baseline"]["sha256"])
        assert re.fullmatch(r"[0-9a-f]{64}", b["candidate"]["sha256"])
        assert b["bundle_digest"].startswith("sha256:")


def test_detail_response_shape_matches_server_contract() -> None:
    """The DOM-test document builder must stay in lockstep with the server's
    /api/v1/verdicts/{case_id} body shape."""
    from fastapi.testclient import TestClient

    from serving_verdict.server import create_app

    with tempfile.TemporaryDirectory() as td:
        app = create_app("127.0.0.1", 8787, Path(td))
        client = TestClient(app)
        r = client.get("/api/v1/verdicts/nope")
        assert r.status_code == 404
        assert r.json() == {"error": "case not found"}
