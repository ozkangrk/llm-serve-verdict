"""F5: DOM-level proof that the shipped UI renders verdicts, authority and hashes.

Unlike the token-presence test in ``test_server.py`` (which only checks that
strings appear in ``ui.js``/``index.html``), this test executes the *actual
shipped* ``ui.js`` against a minimal in-process DOM and reads the rendered
text back out of the DOM:

- element anchors are parsed from the real ``src/serving_verdict/web/index.html``
  (the test fails if an id the UI relies on is missing from the markup);
- the documents served to the UI are produced by the real ``import_case``
  engine (one per verdict: PROMOTE / REJECT / INCONCLUSIVE) and shaped
  exactly like the ``verdict_detail`` API response;
- navigation drives the UI's public entry point only (hash routing + the
  ``fetch`` contract); no test hooks are added to the shipped UI code.

The JS engine is the ``quickjs`` Python binding (a single prebuilt wheel):
no Node toolchain, no npm, no CDN — and the served UI is untouched (still a
self-contained offline HTML/JS/CSS app with same-origin relative asset
references), so the MVP "no Node toolchain / no CDN" contract holds.

Malformed-detail contract: a detail document with ``baseline: null``,
``candidate: null``, a null ``integrity`` and incomplete gate/comparison rows
must render fail-closed (real verdict text, no crash, no "load failed"
downgrade), not throw a TypeError.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from serving_verdict.engine import import_case
from tests.helpers import sha256_file

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "src" / "serving_verdict" / "web"
DSKAB_FIXTURE = ROOT / "tests" / "fixtures" / "dspark" / "case.yaml"
SGLANG_FIXTURE = ROOT / "tests" / "fixtures" / "sglang" / "case.yaml"

# Anchors the UI must populate; all must exist in index.html.
REQUIRED_IDS = (
    "verdict-label",
    "detail-case-id",
    "reason-codes",
    "baseline-artifact",
    "baseline-sha",
    "candidate-artifact",
    "candidate-sha",
    "bundle-digest",
    "integrity",
    "metrics-table",
    "gates-table",
    "claim-boundary",
)

HARNESS_JS = r"""
// Minimal DOM harness (same context as the real ui.js).
// Top-level `var`s in a sloppy script become globals, so ui.js resolves
// `document` / `window` / `fetch` to these implementations.
function _Node(tag, id) {
  this.tagName = tag;
  this.id = id || "";
  this.className = "";
  this.textContent = "";
  this.children = [];
  this._listeners = {};
}
_Node.prototype.appendChild = function (c) { this.children.push(c); return c; };
_Node.prototype.addEventListener = function (t, fn) {
  (this._listeners[t] = this._listeners[t] || []).push(fn);
};
_Node.prototype.querySelector = function (sel) {
  // Only "tbody" is needed by ui.js; match by tag name on direct children.
  for (var i = 0; i < this.children.length; i++) {
    if (this.children[i].tagName === sel) return this.children[i];
  }
  return null;
};
Object.defineProperty(_Node.prototype, "classList", {
  get: function () {
    var self = this;
    return {
      add: function () {
        for (var i = 0; i < arguments.length; i++) {
          if (self.className.split(" ").indexOf(arguments[i]) === -1) {
            self.className = (self.className + " " + arguments[i]).trim();
          }
        }
      },
      remove: function () {
        for (var i = 0; i < arguments.length; i++) {
          self.className = self.className
            .split(" ")
            .filter(function (c) { return c && c !== arguments[0]; })
            .join(" ");
        }
      },
      toggle: function (name, force) {
        var has = self.className.split(" ").indexOf(name) !== -1;
        var want = force === undefined ? !has : !!force;
        if (want && !has) self.className = (self.className + " " + name).trim();
        else if (!want && has) {
          self.className = self.className
            .split(" ")
            .filter(function (c) { return c !== name; })
            .join(" ");
        }
      },
    };
  },
});
Object.defineProperty(_Node.prototype, "innerHTML", {
  get: function () { return this._innerHTML || ""; },
  set: function (v) { this._innerHTML = v; if (v === "") this.children = []; },
});

var __ids = {};
var __docListeners = {};
var __winListeners = {};
var __routes = {};

var document = {
  createElement: function (tag) { return new _Node(tag); },
  getElementById: function (id) { return __ids[id] || null; },
  addEventListener: function (t, fn) {
    (__docListeners[t] = __docListeners[t] || []).push(fn);
  },
};
var window = {
  addEventListener: function (t, fn) {
    (__winListeners[t] = __winListeners[t] || []).push(fn);
  },
  __fire: function (t) {
    var ls = __winListeners[t] || [];
    for (var i = 0; i < ls.length; i++) ls[i]({ type: t });
  },
  __domReady: function () {
    var ls = __docListeners["DOMContentLoaded"] || [];
    for (var i = 0; i < ls.length; i++) ls[i]();
  },
  __dump: function () {
    function text(n) {
      var out = n.textContent || "";
      for (var i = 0; i < n.children.length; i++) out += " " + text(n.children[i]);
      return out.replace(/\s+/g, " ").trim();
    }
    function rows(id) {
      var table = __ids[id];
      var body = table && table.querySelector("tbody");
      var out = [];
      if (body) {
        for (var i = 0; i < body.children.length; i++) {
          var cells = [];
          var tr = body.children[i];
          for (var j = 0; j < tr.children.length; j++) {
            cells.push({ text: text(tr.children[j]), cls: tr.children[j].className });
          }
          out.push(cells);
        }
      }
      return out;
    }
    return {
      verdictLabel: __ids["verdict-label"].textContent,
      verdictClass: __ids["verdict-label"].className,
      caseId: __ids["detail-case-id"].textContent,
      reasonCodes: text(__ids["reason-codes"]),
      baselineArtifact: __ids["baseline-artifact"].textContent,
      baselineSha: __ids["baseline-sha"].textContent,
      candidateArtifact: __ids["candidate-artifact"].textContent,
      candidateSha: __ids["candidate-sha"].textContent,
      bundleDigest: __ids["bundle-digest"].textContent,
      integrityText: __ids["integrity"].textContent,
      integrityClass: __ids["integrity"].className,
      metricsRows: rows("metrics-table"),
      gateRows: rows("gates-table"),
      claimBoundary: __ids["claim-boundary"].textContent,
    };
  },
};

function fetch(path) {
  return Promise.resolve().then(function () {
    var r = __routes[path];
    if (!r) return { ok: false, status: 404, json: function () { return Promise.resolve({}); } };
    return {
      ok: true,
      status: 200,
      json: function () { return Promise.resolve(JSON.parse(JSON.stringify(r))); },
    };
  });
}

// Browser-like location.hash: the getter always includes the leading "#"
// (and returns "" for no fragment); the setter normalizes user input.
Object.defineProperty(window, "location", {
  get: function () {
    return {
      get hash() {
        var h = __hashValue;
        return h ? "#" + (h.charAt(0) === "#" ? h.slice(1) : h) : "";
      },
      set hash(v) {
        var s = String(v == null ? "" : v);
        if (s.charAt(0) === "#") s = s.slice(1);
        __hashValue = s;
      },
    };
  },
});
var __hashValue = "";
"""


def _set_json(ctx, name: str, doc: dict) -> None:
    """Set a JSON-serializable value on the context (quickjs passes via JSON)."""
    ctx.set(name, ctx.parse_json(json.dumps(doc)))


def _build_dom(ctx) -> None:
    """Populate the harness DOM from the real index.html anchors."""
    html = (WEB / "index.html").read_text(encoding="utf-8")
    ids = re.findall(r'id="([^"]+)"', html)
    for rid in REQUIRED_IDS:
        assert rid in ids, f"index.html is missing required anchor id={rid!r}"
    # The two tables need a tbody child (matching index.html's static markup).
    table_ids = {"metrics-table", "gates-table"}
    for rid in ids:
        if rid in table_ids:
            ctx.eval(f"__ids[{json.dumps(rid)}] = new _Node('table', {json.dumps(rid)})")
            ctx.eval(f"__ids[{json.dumps(rid)}].appendChild(new _Node('tbody'))")
        else:
            ctx.eval(f"__ids[{json.dumps(rid)}] = new _Node('div', {json.dumps(rid)})")


def _boot(ui_js: str):
    """Fresh context: harness + real ui.js, DOM built, UI booted at #/ (index)."""
    import quickjs  # type: ignore[import-not-found]

    ctx = quickjs.Context()
    ctx.eval(HARNESS_JS)
    _build_dom(ctx)
    _set_json(ctx, "__routes", {})
    ctx.eval(ui_js)
    ctx.eval("window.__domReady()")  # runs route() at hash ""
    while ctx.execute_pending_job():
        pass
    return ctx


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
    }


def _inconclusive_bundle(tmp_path: Path) -> dict:
    """Real INCONCLUSIVE bundle via a deliberate baseline hash mismatch."""
    import yaml

    src = yaml.safe_load(DSKAB_FIXTURE.read_text(encoding="utf-8"))
    # Copy the bound artifacts under tmp_path so the root can move; the
    # baseline's expected sha is then deliberately wrong -> HASH_MISMATCH.
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


def _navigate(ctx, case_id: str, doc: dict) -> dict:
    """Route the UI to #/case_id serving ``doc`` via the fetch stub; flush microtasks.

    The UI fetches the full API path ``/api/v1/verdicts/{case_id}`` for a
    hash fragment ``#{case_id}``; the stub is keyed by the fetch path.
    """
    _set_json(ctx, "__routes", {f"/api/v1/verdicts/{case_id}": doc})
    ctx.eval(f"window.location.hash = {json.dumps('/' + case_id)}")
    ctx.eval("window.__fire('hashchange')")
    while ctx.execute_pending_job():
        pass
    return json.loads(ctx.eval("window.__dump()").json())


def _gate_row(doc: dict, gate_id: str) -> dict:
    for row in doc["gateRows"]:
        if row and row[0]["text"] == gate_id:
            return {"status": row[1], "authority": row[2], "evidence": row[3]}
    raise AssertionError(f"gate row for {gate_id!r} not rendered; got {doc['gateRows']}")


# ---------------------------------------------------------------------------
# three verdict states actually render into the DOM
# ---------------------------------------------------------------------------


def test_ui_renders_promote_verdict_authority_and_hashes(tmp_path: Path) -> None:
    ui_js = (WEB / "ui.js").read_text(encoding="utf-8")
    ctx = _boot(ui_js)
    bundle = import_case(str(DSKAB_FIXTURE))
    assert bundle["verdict"] == "PROMOTE"
    doc = _navigate(ctx, "fixture-dspark", _detail_response(bundle))

    # verdict-first: text label AND state class in the DOM
    assert doc["verdictLabel"] == "PROMOTE"
    assert "promote" in doc["verdictClass"].split()
    assert doc["caseId"] == "fixture-dspark"
    # reason codes rendered as chips
    assert "PRIMARY_EFFECT_PASSED" in doc["reasonCodes"]
    # hashes rendered into the DOM (full 64-hex values)
    assert doc["baselineSha"] == bundle["baseline"]["sha256"]
    assert len(doc["baselineSha"]) == 64
    assert doc["candidateSha"] == bundle["candidate"]["sha256"]
    assert doc["bundleDigest"] == bundle["bundle_digest"]
    assert doc["bundleDigest"].startswith("sha256:")
    assert doc["integrityText"].startswith("verified")
    # gate authority: machine vs operator attested, rendered per gate
    req = _gate_row(doc, "request_success")
    assert req["status"]["text"] == "pass"
    assert req["authority"]["text"] == "machine_measured"
    att = _gate_row(doc, "arithmetic")
    assert att["authority"]["text"] == "operator_attested"
    assert att["evidence"]["text"]  # evidence source shown, not empty
    # comparable metrics rendered with direction and values
    assert doc["metricsRows"], "metrics table body is empty"
    first = doc["metricsRows"][0]
    assert first[0]["text"] == "decode_tokens_per_s"
    assert first[1]["text"] == "higher_better"
    # claim boundary rendered
    assert doc["claimBoundary"]


def test_ui_renders_reject_verdict_in_dom(tmp_path: Path) -> None:
    ui_js = (WEB / "ui.js").read_text(encoding="utf-8")
    ctx = _boot(ui_js)
    bundle = import_case(str(SGLANG_FIXTURE))
    assert bundle["verdict"] == "REJECT"
    doc = _navigate(ctx, "fixture-sglang", _detail_response(bundle))
    assert doc["verdictLabel"] == "REJECT"
    assert "reject" in doc["verdictClass"].split()
    assert doc["caseId"] == "fixture-sglang"
    assert "HARD_GATE_FAILED" in doc["reasonCodes"]
    # the failing attested gate must be visible with its authority
    row = _gate_row(doc, "process_stability")
    assert row["status"]["text"] == "fail"
    assert row["authority"]["text"] == "operator_attested"
    assert len(doc["baselineSha"]) == 64 and len(doc["candidateSha"]) == 64


def test_ui_renders_inconclusive_verdict_in_dom(tmp_path: Path) -> None:
    ui_js = (WEB / "ui.js").read_text(encoding="utf-8")
    ctx = _boot(ui_js)
    bundle = _inconclusive_bundle(tmp_path)
    assert bundle["verdict"] == "INCONCLUSIVE"
    doc = _navigate(ctx, "inconclusive-case", _detail_response(bundle))
    assert doc["verdictLabel"] == "INCONCLUSIVE"
    assert "inconclusive" in doc["verdictClass"].split()
    assert "EVIDENCE_HASH_MISMATCH" in doc["reasonCodes"]
    # bound-ref hashes still render even for an inconclusive import
    assert len(doc["baselineSha"]) == 64 and len(doc["candidateSha"]) == 64
    assert doc["bundleDigest"] == bundle["bundle_digest"]


# ---------------------------------------------------------------------------
# malformed detail must render fail-closed, not crash / downgrade
# ---------------------------------------------------------------------------


def test_ui_renders_null_baseline_without_crash() -> None:
    """A detail document with null baseline/candidate must not crash the UI or
    fall back to the 'load failed' error path — it renders the real verdict
    with empty hash cells (fail-closed)."""
    ui_js = (WEB / "ui.js").read_text(encoding="utf-8")
    ctx = _boot(ui_js)
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
    doc = _navigate(ctx, "malformed-case", malformed)
    # no crash: the real verdict is rendered, NOT the load-failed fallback
    assert doc["verdictLabel"] == "REJECT"
    assert doc["caseId"] == "malformed-case"
    assert "load failed" not in doc["integrityText"].lower()
    # null refs degrade to empty cells, not the string "null" or a throw
    assert doc["baselineSha"] == "" and doc["candidateSha"] == ""
    assert doc["baselineArtifact"] == "" and doc["candidateArtifact"] == ""
    # integrity field absent -> explicit INTEGRITY FAILURE, class bad
    assert "INTEGRITY FAILURE" in doc["integrityText"]
    assert "bad" in doc["integrityClass"].split()
    # gate row still renders its id and status (missing authority is not fatal)
    row = _gate_row(doc, "g1")
    assert row["status"]["text"] == "pass"


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
