/* LLM ServeVerdict — self-contained offline UI (v0.2, verdict-first).
 *
 * Design contract:
 * - Verdict first: every list card and detail view leads with the verdict as
 *   a TEXT label (PROMOTE / REJECT / INCONCLUSIVE); color is an accent only.
 * - Error states are NEVER verdicts: a 404, a 422 (integrity failure) or a
 *   load failure renders a dedicated NotFound / Invalid / Error panel. The
 *   detail view is hidden and no verdict pill is ever shown on error.
 * - Numbers: 4 significant digits; the full value lives in the title attr.
 *   Delta polarity is direction-aware (higher_better / lower_better):
 *   good / bad / neutral, rendered as an arrow (ok) plus text.
 * - REJECT: a "Why this verdict" narrative names each failing gate and links
 *   (marker) to the gates table.
 * - Gate authority (machine_measured vs operator_attested) and the claim
 *   boundary are always rendered exactly as the bundle carries them.
 * - a11y: visible H1, table caption/scope, aria-live copy announcements,
 *   >=44px tap targets, :focus-visible outlines, prefers-reduced-motion.
 * - No network access beyond the same loopback origin (no CDN, no Node).
 */
(function () {
  "use strict";

  var VERDICTS = ["PROMOTE", "REJECT", "INCONCLUSIVE"];
  var ONBOARDING_COMMANDS = [
    "serving-verdict import-case CASE.yaml --out data/BUNDLE.json",
    "serving-verdict serve --host 127.0.0.1 --port 8787 --data-dir data"
  ];
  // Authority vocabulary: machine_measured values come from recognized
  // artifact adapters; operator_attested values are attested by the operator
  // and are never relabeled. Rendered exactly as the bundle carries them.
  var AUTHORITY_KINDS = ["machine_measured", "operator_attested"];
  var state = { caseId: null, lastError: null, automationJob: null };

  function $(id) { return document.getElementById(id); }

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }

  function api(path, options) {
    return fetch(path, options || {}).then(function (r) {
      if (!r.ok) {
        var err = new Error("HTTP " + r.status);
        err.status = r.status;
        err.bodyPromise = r.json().catch(function () { return {}; });
        throw err;
      }
      return r.json();
    });
  }

  function verdictClass(v) {
    if (v === "PROMOTE") return "verdict promote";
    if (v === "REJECT") return "verdict reject";
    return "verdict inconclusive";
  }

  function authorityClass(a) {
    var known = a && AUTHORITY_KINDS.indexOf(a) !== -1;
    return "authority authority-" + (known ? a : "none");
  }

  /* ------------------------- number formatting ------------------------- */

  function _addOneDigits(d) {
    // Decimal (string) increment of a plain digit stream, exact by design.
    var out = d.split("");
    for (var i = out.length - 1; i >= 0; i--) {
      if (out[i] === "9") { out[i] = "0"; continue; }
      out[i] = String(Number(out[i]) + 1);
      return out.join("");
    }
    return "1" + out.join("");
  }

  // 4 significant digits on the number's canonical decimal string
  // (String(v)), rounded half-up on the 5th significant digit.
  // 25 -> "25.00", 1.2345 -> "1.235", 0.0012345 -> "0.001235", 999.9995 -> "1000".
  function sig4(v) {
    if (v === null || v === undefined || typeof v !== "number" || !isFinite(v)) return "—";
    if (v === 0) return "0.00";
    var s = String(v);
    if (s.indexOf("e") !== -1 || s.indexOf("E") !== -1) return v.toExponential(3);
    var neg = s.charAt(0) === "-";
    if (neg) s = s.slice(1);
    var dot = s.indexOf(".");
    var intPart = dot === -1 ? s : s.slice(0, dot);
    var frac = dot === -1 ? "" : s.slice(dot + 1);
    var digits = intPart + frac;
    var dotPos = intPart.length;
    var firstNZ = -1;
    for (var i = 0; i < digits.length; i++) {
      if (digits.charAt(i) !== "0") { firstNZ = i; break; }
    }
    if (firstNZ === -1) return "0.00";
    var sig = digits.slice(firstNZ);
    while (sig.length < 4) sig += "0";
    var keep = sig.slice(0, 4);
    var rest = sig.slice(4);
    if (rest && rest.charAt(0) >= "5") keep = _addOneDigits(keep);
    var intSigCount = dotPos - firstNZ; // significant digits left of the dot
    var ipCount = intSigCount + (keep.length - 4);
    if (ipCount <= 0) {
      var lead = "";
      for (var z = 0; z < -ipCount + 1; z++) lead += "0";
      return (neg ? "-" : "") + "0." + lead + keep;
    }
    var ip = keep.slice(0, ipCount);
    var fpLen = Math.max(0, 4 - ipCount);
    var fp = keep.slice(ipCount, ipCount + fpLen);
    return (neg ? "-" : "") + ip + (fp ? "." + fp : "");
  }

  function fmtValue(v) {
    if (v === null || v === undefined) return "—";
    if (typeof v === "number") return sig4(v);
    return String(v);
  }

  function fullValue(v) {
    return v === null || v === undefined ? "" : String(v);
  }

  // Direction-aware delta: arrow (ok marker) + signed percentage text.
  function fmtDelta(v, direction) {
    if (v === null || v === undefined) return { text: "—", cls: "metric-delta delta-neutral" };
    var pct = v * 100;
    var signed = (pct >= 0 ? "+" : "-") + sig4(Math.abs(pct)) + "%";
    var cls = "delta-neutral";
    var arrow = "·";
    if (Math.abs(pct) > 1e-9) {
      var improved = direction === "lower_better" ? v < 0 : v > 0;
      cls = improved ? "delta-good" : "delta-bad";
      arrow = improved ? "▲" : "▼";
    }
    return { text: arrow + " " + signed, cls: "metric-delta " + cls };
  }

  function fmtDate(iso) {
    if (!iso) return "";
    var d = new Date(iso);
    if (isNaN(d.getTime())) return String(iso);
    var p = function (n) { return (n < 10 ? "0" : "") + n; };
    return d.getUTCFullYear() + "-" + p(d.getUTCMonth() + 1) + "-" + p(d.getUTCDate()) +
      " " + p(d.getUTCHours()) + ":" + p(d.getUTCMinutes()) + " UTC";
  }

  function setText(id, value) {
    var node = $(id);
    node.textContent = value === null || value === undefined ? "" : String(value);
  }

  /* --------------------------- copy to clipboard --------------------------- */

  function announceCopy(what) {
    var live = $("copy-live");
    live.textContent = what + " copied to clipboard";
  }

  function copyText(text, what) {
    if (!text) return;
    var done = function () { announceCopy(what); };
    var nav = typeof navigator !== "undefined" ? navigator : null;
    if (nav && nav.clipboard && nav.clipboard.writeText) {
      nav.clipboard.writeText(text).then(done, function () { fallbackCopy(text); done(); });
    } else {
      fallbackCopy(text);
      done();
    }
  }

  function fallbackCopy(text) {
    try {
      if (!document.body) return;
      var ta = document.createElement("textarea");
      ta.textContent = text;
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      document.body.removeChild(ta);
    } catch (_e) { /* clipboard unavailable — the live region still announces */ }
  }

  function makeCopyButton(value, what, btnId) {
    var b = el("button", "copy-btn", "copy");
    b.type = "button";
    if (btnId) b.id = btnId;
    b.setAttribute("aria-label", "Copy " + what);
    b.setAttribute("data-copy", value == null ? "" : String(value));
    b.addEventListener("click", function (e) {
      if (e && e.stopPropagation) e.stopPropagation();
      copyText(b.getAttribute("data-copy"), what);
    });
    return b;
  }

  // sha256 digests are copyable; the full 64-hex value is always shown.
  function hashCell(id, value, what) {
    var node = $(id);
    node.textContent = value == null ? "" : String(value);
    for (var i = node.children.length - 1; i >= 0; i--) {
      if (node.children[i].tagName === "button") node.removeChild(node.children[i]);
    }
    if (value) node.appendChild(makeCopyButton(String(value), what, "copy-" + id));
  }

  /* ------------------------------- index ------------------------------- */

  function renderIndex() {
    $("nav-verdicts").setAttribute("aria-current", "page");
    $("nav-automation").setAttribute("aria-current", "false");
    $("index-view").classList.remove("hidden");
    $("detail-view").classList.add("hidden");
    $("error-view").classList.add("hidden");
    $("automation-view").classList.add("hidden");
    $("list-heading").textContent = "Verdicts";
    state.caseId = null;
    document.title = "LLM ServeVerdict";

    var list = $("verdict-list");
    list.innerHTML = "";
    $("index-error").classList.add("hidden");
    $("index-retry").classList.add("hidden");
    $("index-empty").classList.add("hidden");
    $("verdict-count").textContent = "";
    $("index-loading").classList.remove("hidden");

    api("/api/v1/verdicts").then(function (body) {
      $("index-loading").classList.add("hidden");
      var verdicts = (body && body.verdicts) || [];
      if (verdicts.length === 0) {
        $("index-empty").classList.remove("hidden");
        renderOnboarding();
        return;
      }
      $("verdict-count").textContent = verdicts.length + (verdicts.length === 1 ? " verdict" : " verdicts");
      verdicts.forEach(function (v) {
        v = v || {};
        var card = el("a", "card");
        card.href = "#/" + encodeURIComponent(v.case_id || "");
        card.setAttribute("aria-label", (v.verdict || "?") + " · " + (v.case_id || ""));
        var label = el("div", verdictClass(v.verdict), v.verdict || "—");
        var idNode = el("div", "card-id", v.case_id || "");
        var reason = el("div", "card-reason", (v.reason_codes || []).join(" · "));
        var when = el("div", "card-when", v.created_at ? "created " + fmtDate(v.created_at) : "");
        var digest = el("div", "card-digest", v.bundle_digest || "");
        card.appendChild(label);
        card.appendChild(idNode);
        card.appendChild(reason);
        card.appendChild(when);
        card.appendChild(digest);
        card.addEventListener("click", function (e) {
          if (e && e.preventDefault) e.preventDefault();
          showDetail(v.case_id);
        });
        list.appendChild(card);
      });
    }).catch(function (e) {
      $("index-loading").classList.add("hidden");
      var err = $("index-error");
      err.textContent = "Failed to load verdicts: " + (e && e.message ? e.message : String(e)) +
        " — the data directory may not exist or may be unreadable.";
      err.classList.remove("hidden");
      $("index-retry").classList.remove("hidden");
    });
  }

  // Neutral onboarding empty state: what to do next, with copyable commands.
  function renderOnboarding() {
    $("empty-title").textContent = "No verdicts yet";
    $("empty-copy").textContent = "This is expected on a fresh data directory. Import a case config " +
      "from local inference evidence, then serve the bundles on loopback:";
    var ul = $("onboarding-cmds");
    ul.innerHTML = "";
    ONBOARDING_COMMANDS.forEach(function (cmd, i) {
      var li = el("li", "onboarding-cmd");
      var code = el("code", "onboarding-code", cmd);
      code.title = cmd;
      li.appendChild(code);
      var btn = el("button", "copy-btn", "copy");
      btn.type = "button";
      btn.id = "copy-onb-" + i;
      btn.setAttribute("aria-label", "Copy command " + (i + 1));
      btn.setAttribute("data-copy", cmd);
      btn.addEventListener("click", function (e) {
        if (e && e.stopPropagation) e.stopPropagation();
        copyText(cmd, "command");
      });
      li.appendChild(btn);
      ul.appendChild(li);
    });
  }

  /* ------------------------------ error views ------------------------------ */

  // Dedicated NotFound / Invalid / Error panels. Never a verdict pill:
  // an error is an error, not an INCONCLUSIVE.
  function showErrorView(kind, caseId, message, advice) {
    $("index-view").classList.add("hidden");
    $("detail-view").classList.add("hidden");
    $("error-view").classList.remove("hidden");
    document.title = "LLM ServeVerdict · " + kind;

    // Defensively clear any stale verdict state.
    $("verdict-label").textContent = "";
    $("verdict-label").className = "";
    $("detail-body").classList.add("hidden");

    var title = $("error-title");
    var text = $("error-text");
    var adv = $("error-advice");
    if (kind === "notfound") {
      title.textContent = "Case not found";
      text.textContent = "No verdict bundle matches \u201c" + caseId + "\u201d in the data directory." +
        (message ? " (" + message + ")" : "");
      adv.textContent = "Check the case ID and the data directory path, then retry. " +
        "List all bundles with: serving-verdict list --data-dir DATA_DIR";
    } else if (kind === "invalid") {
      title.textContent = "Invalid bundle";
      text.textContent = message || "bundle integrity verification failed";
      adv.textContent = "This bundle failed offline integrity verification, so it is not served " +
        "and never downgraded to a verdict. Re-import the case from pristine artifacts; " +
        "a tampered or corrupted file is not evidence.";
    } else {
      title.textContent = "Failed to load";
      text.textContent = message || "unknown error";
      adv.textContent = "The request to the loopback API failed. Check that the server is running " +
        "and the data directory is readable, then retry.";
    }
    $("error-retry").classList.remove("hidden");
    state.lastError = { caseId: caseId };
  }

  /* ------------------------------ detail ------------------------------ */

  function renderDetail(doc) {
    doc = doc || {};
    var verdict = doc.verdict;
    $("index-view").classList.add("hidden");
    $("error-view").classList.add("hidden");
    $("detail-view").classList.remove("hidden");
    $("detail-loading").classList.add("hidden");
    $("detail-body").classList.remove("hidden");
    document.title = (verdict || "Case") + " · " + (doc.case_id || state.caseId || "") + " · LLM ServeVerdict";

    var label = $("verdict-label");
    label.textContent = verdict || "—";
    label.className = verdictClass(verdict);
    setText("detail-case-id", doc.case_id);

    var created = $("detail-created");
    if (doc.created_at) {
      created.textContent = "created " + fmtDate(doc.created_at);
      created.title = doc.created_at;
    } else {
      created.textContent = "";
      created.title = "";
    }

    var rc = $("reason-codes");
    rc.innerHTML = "";
    (doc.reason_codes || []).forEach(function (code) {
      rc.appendChild(el("span", "chip", code));
    });

    renderNarrative(doc);

    // Fail-closed for null/malformed references: empty cells, never a crash.
    setText("baseline-artifact", doc.baseline ? doc.baseline.artifact_id : null);
    hashCell("baseline-sha", doc.baseline ? doc.baseline.sha256 : null, "baseline hash (sha256)");
    setText("candidate-artifact", doc.candidate ? doc.candidate.artifact_id : null);
    hashCell("candidate-sha", doc.candidate ? doc.candidate.sha256 : null, "candidate hash (sha256)");
    hashCell("bundle-digest", doc.bundle_digest, "bundle digest");
    var integ = $("integrity");
    integ.textContent = doc.integrity && doc.integrity.valid
      ? "verified (offline digest recompute)" : "INTEGRITY FAILURE";
    integ.className = doc.integrity && doc.integrity.valid ? "ok" : "bad";

    renderMetrics(doc);
    renderGates(doc);
    setText("claim-boundary", doc.claim_boundary);
  }

  // REJECT narrative: name each failing gate with a marker link into the
  // gates table, so the fail reason and its evidence row are connected.
  function renderNarrative(doc) {
    var section = $("gate-narrative");
    var body = $("gate-narrative-body");
    body.innerHTML = "";
    section.classList.add("hidden");
    if (!doc || doc.verdict !== "REJECT" || !doc.gates) return;
    var failed = doc.gates.filter(function (g) {
      return g && g.status === "fail";
    });
    if (failed.length === 0) return;
    section.classList.remove("hidden");
    body.appendChild(el("p", "muted", "Verdict REJECT: " + failed.length + " required gate" +
      (failed.length === 1 ? "" : "s") + " measured FAIL. A failed required gate blocks promotion."));
    var ul = el("ul", "narrative-list");
    failed.forEach(function (g) {
      var li = el("li", null, g.id + " — status FAIL (" + (g.authority || "unspecified authority") + "). ");
      var a = el("a", "gate-link marker", g.id + " · FAIL");
      a.href = "#gates-table";
      a.setAttribute("aria-label", "Jump to gate " + g.id + " in the gates table");
      a.addEventListener("click", function (e) {
        if (e && e.preventDefault) e.preventDefault();
        var target = $("gates-table");
        if (target && target.scrollIntoView) target.scrollIntoView();
      });
      li.appendChild(a);
      ul.appendChild(li);
    });
    body.appendChild(ul);
  }

  function renderMetrics(doc) {
    var wrap = $("metric-cards");
    wrap.innerHTML = "";
    var mBody = $("metrics-table").querySelector("tbody");
    mBody.innerHTML = "";
    var comparisons = (doc && doc.comparisons) || [];
    $("metrics-empty").classList.toggle("hidden", comparisons.length > 0);
    comparisons.forEach(function (c) {
      c = c || {};
      var idSafe = String(c.metric == null ? "metric" : c.metric).replace(/[^A-Za-z0-9_-]/g, "_");
      var delta = fmtDelta(c.relative_delta, c.direction);

      /* mobile metric card */
      var card = el("div", "metric-card");
      card.id = "mcard-" + idSafe;
      var h3 = el("h3", "metric-title", c.metric || "—");
      h3.title = "direction: " + (c.direction || "—");
      card.appendChild(h3);

      var grid = el("div", "metric-grid");
      grid.appendChild(metricCell("metric-baseline", "Baseline", c.baseline_value, c.unit));
      grid.appendChild(metricCell("metric-candidate", "Candidate", c.candidate_value, c.unit));
      var dLbl = el("div", "metric-label", "Δ rel · " + (c.direction || "—"));
      var dCell = el("div", delta.cls, delta.text);
      dCell.title = c.relative_delta == null ? "" :
        "relative delta " + (c.relative_delta * 100).toFixed(6) + "% (" + (c.direction || "—") + ")";
      grid.appendChild(dLbl);
      grid.appendChild(dCell);
      card.appendChild(grid);

      // Matched comparison dimensions (why these two values are comparable).
      var dims = el("dl", "metric-dims hidden");
      dims.id = "mdims-" + idSafe;
      var dimsSrc = c.dimensions || {};
      ["unit", "procedure_version", "workload_id", "concurrency", "output_budget",
       "thinking_mode", "warm_cold", "aggregation"].forEach(function (k) {
        dims.appendChild(el("dt", null, k));
        dims.appendChild(el("dd", null, dimsSrc[k] == null ? "—" : String(dimsSrc[k])));
      });
      card.appendChild(dims);

      var exp = el("button", "metric-expand", "Dimensions");
      exp.type = "button";
      exp.id = "mexp-" + idSafe;
      exp.setAttribute("aria-expanded", "false");
      exp.setAttribute("aria-controls", "mdims-" + idSafe);
      exp.addEventListener("click", function () {
        var open = exp.getAttribute("aria-expanded") === "true";
        exp.setAttribute("aria-expanded", open ? "false" : "true");
        dims.classList.toggle("hidden", open);
        exp.textContent = open ? "Dimensions" : "Hide dimensions";
      });
      card.appendChild(exp);
      wrap.appendChild(card);

      /* desktop table row (same data, denser layout) */
      var tr = el("tr");
      tr.appendChild(el("td", "mono", c.metric || "—"));
      tr.appendChild(el("td", null, c.direction || "—"));
      tr.appendChild(el("td", null, fmtValue(c.baseline_value)));
      tr.appendChild(el("td", null, fmtValue(c.candidate_value)));
      tr.appendChild(el("td", delta.cls, delta.text));
      tr.appendChild(el("td", null, c.unit || "—"));
      tr.appendChild(el("td", null, c.dimensions ? c.dimensions.aggregation : "—"));
      mBody.appendChild(tr);
    });
  }

  function metricCell(cls, label, value, unit) {
    var cell = el("div", cls);
    cell.appendChild(el("div", "metric-label", label + (unit ? " · " + unit : "")));
    var val = el("div", "metric-value", fmtValue(value));
    val.title = fullValue(value);
    cell.appendChild(val);
    return cell;
  }

  function renderGates(doc) {
    var gBody = $("gates-table").querySelector("tbody");
    gBody.innerHTML = "";
    ((doc && doc.gates) || []).forEach(function (g) {
      g = g || {};
      var tr = el("tr");
      tr.appendChild(el("th", "gate-id", g.id || "—"));
      tr.appendChild(el("td", "status-" + (g.status || "missing"), g.status || "missing"));
      tr.appendChild(el("td", authorityClass(g.authority), g.authority || "none"));
      tr.appendChild(el("td", "evidence", (g.evidence || []).join(", ")));
      gBody.appendChild(tr);
    });
  }

  function failDetail(e, caseId) {
    var finish = function (msg) {
      if (e && e.status === 404) showErrorView("notfound", caseId, msg);
      else if (e && e.status === 422) showErrorView("invalid", caseId, msg);
      else showErrorView("load", caseId, msg || "network error");
    };
    if (e && e.bodyPromise) {
      e.bodyPromise.then(
        function (body) { finish((body && body.error) ? body.error : (e.message || "")); },
        function () { finish(e.message || "error"); }
      );
    } else {
      finish(e && e.message ? e.message : String(e));
    }
  }

  function showDetail(caseId) {
    state.caseId = caseId;
    $("index-view").classList.add("hidden");
    $("error-view").classList.add("hidden");
    $("automation-view").classList.add("hidden");
    $("detail-view").classList.remove("hidden");
    $("detail-loading").classList.remove("hidden");
    $("detail-body").classList.add("hidden");

    api("/api/v1/verdicts/" + encodeURIComponent(caseId)).then(function (body) {
      renderDetail(body);
    }).catch(function (e) {
      // Never a fake INCONCLUSIVE pill: route to the dedicated error panels.
      failDetail(e, caseId);
    });
  }

  /* --------------------------- automation wizard --------------------------- */

  function showAutomation() {
    $("nav-automation").setAttribute("aria-current", "page");
    $("nav-verdicts").setAttribute("aria-current", "false");
    $("index-view").classList.add("hidden");
    $("detail-view").classList.add("hidden");
    $("error-view").classList.add("hidden");
    $("automation-view").classList.remove("hidden");
    document.title = "Automation · LLM ServeVerdict";
    api("/api/v1/automation/capabilities").then(function (caps) {
      if (!caps.quick_benchmark) automationFail("Quick benchmark is unavailable.");
    }).catch(function () { automationFail("Automation capability check failed."); });
  }

  function automationFail(message) {
    $("auto-error").textContent = message || "Automation request failed.";
    $("auto-error").classList.remove("hidden");
    $("auto-status").textContent = "Error";
  }

  function renderAutomationJob(job) {
    state.automationJob = job.job_id || state.automationJob;
    $("auto-status").textContent = (job.state || "UNKNOWN") + " · " + (job.phase || "—");
    var active = ["QUEUED", "RUNNING", "CANCEL_REQUESTED"].indexOf(job.state) !== -1;
    $("auto-cancel").classList.toggle("hidden", !active);
    $("auto-refresh").classList.toggle("hidden", !active);
    if (job.state === "FAILED") automationFail("Benchmark failed. Secret and remote error text were suppressed.");
    if (job.state === "CANCELLED") $("auto-status").textContent = "CANCELLED · result discarded";
    if (job.state === "SUCCEEDED" && job.result) {
      $("auto-result-empty").classList.add("hidden");
      $("auto-result").classList.remove("hidden");
      $("auto-result").textContent = JSON.stringify({
        run_id: job.result.run_id,
        run_status: job.result.run_status,
        gates: job.result.gates,
        aggregates: job.result.aggregates,
        artifact_digest: job.result.artifact_digest
      }, null, 2);
    }
    if (active && typeof setTimeout === "function") setTimeout(refreshAutomation, 750);
  }

  function refreshAutomation() {
    if (!state.automationJob) return;
    api("/api/v1/automation/jobs/" + encodeURIComponent(state.automationJob))
      .then(renderAutomationJob)
      .catch(function () { automationFail("Could not refresh benchmark status."); });
  }

  function startAutomation() {
    $("auto-error").classList.add("hidden");
    $("auto-result").classList.add("hidden");
    $("auto-result-empty").classList.remove("hidden");
    $("auto-status").textContent = "Starting…";
    var body = {
      schema_version: "serving-verdict.endpoint.v1",
      id: $("auto-endpoint-id").value,
      base_url: $("auto-base-url").value,
      model: $("auto-model").value,
      api_key_env: $("auto-api-env").value
    };
    api("/api/v1/automation/jobs", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(body)
    }).then(renderAutomationJob).catch(function (e) {
      if (e.bodyPromise) {
        e.bodyPromise.then(function (b) { automationFail(b.error || "Invalid automation request."); });
      } else automationFail("Automation request failed.");
    });
  }

  function cancelAutomation() {
    if (!state.automationJob) return;
    api("/api/v1/automation/jobs/" + encodeURIComponent(state.automationJob) + "/cancel", {
      method: "POST"
    }).then(renderAutomationJob).catch(function () { automationFail("Cancel request failed."); });
  }

  function route() {
    var m = /^#\/([^/]+)/.exec(window.location.hash || "");
    if (m) { showDetail(decodeURIComponent(m[1])); } else { renderIndex(); }
  }

  window.addEventListener("hashchange", route);
  document.addEventListener("DOMContentLoaded", function () {
    $("nav-verdicts").addEventListener("click", function () {
      window.location.hash = "";
      renderIndex();
    });
    $("nav-automation").addEventListener("click", showAutomation);
    $("auto-start").addEventListener("click", startAutomation);
    $("auto-cancel").addEventListener("click", cancelAutomation);
    $("auto-refresh").addEventListener("click", refreshAutomation);
    $("back-btn").addEventListener("click", function () {
      window.location.hash = "";
      renderIndex();
    });
    $("index-retry").addEventListener("click", renderIndex);
    $("error-retry").addEventListener("click", function () {
      if (state.lastError && state.lastError.caseId) {
        showDetail(state.lastError.caseId);
      } else {
        window.location.hash = "";
        renderIndex();
      }
    });
    $("error-back").addEventListener("click", function () {
      window.location.hash = "";
      renderIndex();
    });
    route();
  });
})();
