/* Serving Verdict — self-contained offline UI.
 * Verdict first (text labels, not just color), reason codes, comparable
 * metrics, gate authority (machine_measured vs operator_attested), hashes,
 * claim boundary. No network access beyond the same loopback origin. */
(function () {
  "use strict";

  var VERDICTS = ["PROMOTE", "REJECT", "INCONCLUSIVE"];
  var state = { caseId: null };

  function $(id) { return document.getElementById(id); }

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }

  function api(path) {
    return fetch(path).then(function (r) {
      if (!r.ok) throw new Error(r.status + " " + path);
      return r.json();
    });
  }

  function verdictClass(v) {
    if (v === "PROMOTE") return "verdict promote";
    if (v === "REJECT") return "verdict reject";
    return "verdict inconclusive";
  }

  function fmtDelta(v) {
    if (v === null || v === undefined) return "—";
    var pct = (v * 100).toFixed(1) + "%";
    return (v >= 0 ? "+" : "") + pct;
  }

  function fmtValue(v) {
    if (v === null || v === undefined) return "—";
    return String(v);
  }

  function setText(id, value) {
    var node = $(id);
    node.textContent = value === null || value === undefined ? "" : String(value);
  }

  function renderIndex() {
    $("index-view").classList.remove("hidden");
    $("detail-view").classList.add("hidden");
    var list = $("verdict-list");
    list.innerHTML = "";
    var err = $("index-error");
    err.classList.add("hidden");
    api("/api/v1/verdicts").then(function (body) {
      if (!body.verdicts || body.verdicts.length === 0) {
        err.textContent = "No verdict bundles in the data directory.";
        err.classList.remove("hidden");
        return;
      }
      body.verdicts.forEach(function (v) {
        var card = el("a", "card");
        card.href = "#/" + encodeURIComponent(v.case_id || "");
        var label = el("div", verdictClass(v.verdict), v.verdict);
        var idNode = el("div", "card-id", v.case_id);
        var reason = el("div", "card-reason", (v.reason_codes || []).join(" · "));
        var digest = el("div", "card-digest", v.bundle_digest || "");
        card.appendChild(label);
        card.appendChild(idNode);
        card.appendChild(reason);
        card.appendChild(digest);
        card.addEventListener("click", function () { showDetail(v.case_id); });
        list.appendChild(card);
      });
    }).catch(function (e) {
      err.textContent = "Failed to load verdicts: " + e.message;
      err.classList.remove("hidden");
    });
  }

  function renderDetail(doc) {
    doc = doc || {};
    var verdict = doc.verdict;
    var label = $("verdict-label");
    label.textContent = verdict || "INCONCLUSIVE";
    label.className = verdictClass(verdict);
    setText("detail-case-id", doc.case_id);

    var rc = $("reason-codes");
    rc.innerHTML = "";
    (doc.reason_codes || []).forEach(function (code) {
      rc.appendChild(el("span", "chip", code));
    });

    // Fail-closed for null/malformed references: empty cells, never a crash.
    setText("baseline-artifact", doc.baseline ? doc.baseline.artifact_id : null);
    setText("baseline-sha", doc.baseline ? doc.baseline.sha256 : null);
    setText("candidate-artifact", doc.candidate ? doc.candidate.artifact_id : null);
    setText("candidate-sha", doc.candidate ? doc.candidate.sha256 : null);
    setText("bundle-digest", doc.bundle_digest);
    var integ = $("integrity");
    integ.textContent = doc.integrity && doc.integrity.valid
      ? "verified (offline digest recompute)" : "INTEGRITY FAILURE";
    integ.className = doc.integrity && doc.integrity.valid ? "ok" : "bad";

    var mBody = $("metrics-table").querySelector("tbody");
    mBody.innerHTML = "";
    var comparisons = doc.comparisons || [];
    $("metrics-empty").classList.toggle("hidden", comparisons.length > 0);
    comparisons.forEach(function (c) {
      c = c || {};
      var tr = el("tr");
      tr.appendChild(el("td", "mono", c.metric));
      tr.appendChild(el("td", null, c.direction));
      tr.appendChild(el("td", null, fmtValue(c.baseline_value)));
      tr.appendChild(el("td", null, fmtValue(c.candidate_value)));
      tr.appendChild(el("td", null, fmtDelta(c.relative_delta)));
      tr.appendChild(el("td", null, c.unit));
      tr.appendChild(el("td", null, c.dimensions ? c.dimensions.aggregation : "—"));
      mBody.appendChild(tr);
    });

    var gBody = $("gates-table").querySelector("tbody");
    gBody.innerHTML = "";
    (doc.gates || []).forEach(function (g) {
      g = g || {};
      var tr = el("tr");
      tr.appendChild(el("td", "mono", g.id));
      var statusCell = el("td", "status-" + (g.status || "missing"), g.status);
      tr.appendChild(statusCell);
      tr.appendChild(el("td", "authority authority-" + (g.authority || "none"), g.authority));
      tr.appendChild(el("td", "evidence", (g.evidence || []).join(", ")));
      gBody.appendChild(tr);
    });

    setText("claim-boundary", doc.claim_boundary);
  }

  function showDetail(caseId) {
    state.caseId = caseId;
    $("index-view").classList.add("hidden");
    $("detail-view").classList.remove("hidden");
    api("/api/v1/verdicts/" + encodeURIComponent(caseId)).then(renderDetail).catch(function (e) {
      $("verdict-label").textContent = "INCONCLUSIVE";
      $("verdict-label").className = verdictClass("INCONCLUSIVE");
      $("detail-case-id").textContent = caseId;
      $("integrity").textContent = "load failed: " + e.message;
      $("integrity").className = "bad";
      renderIndex();
    });
  }

  function route() {
    var m = /^#\/([^/]+)/.exec(window.location.hash || "");
    if (m) { showDetail(decodeURIComponent(m[1])); } else { renderIndex(); }
  }

  window.addEventListener("hashchange", route);
  document.addEventListener("DOMContentLoaded", function () {
    $("back-btn").addEventListener("click", function () {
      window.location.hash = "";
      renderIndex();
    });
    route();
  });
})();
