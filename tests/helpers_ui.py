"""QuickJS DOM harness for the shipped offline UI (tests only).

Runs the *real* ``src/serving_verdict/web/ui.js`` inside a minimal in-process
DOM (``quickjs`` binding — no Node toolchain, no npm, no CDN). The harness is
deliberately minimal but faithful at the API surface ``ui.js`` uses:

- ``document.getElementById`` resolved from ids parsed out of the real
  ``index.html`` (the test fails if an id the UI relies on is missing);
- ``document.createElement`` nodes with ``children``, ``className``
  (``classList`` add/remove/toggle), ``textContent``, ``innerHTML`` (reset),
  ``setAttribute``/``getAttribute``, and reflected ``title``/``href``/
  ``disabled`` properties;
- a ``fetch`` stub keyed by URL path; a route may be a bare JSON body (200)
  or ``{"status": N, "body": {...}}`` for error responses (404/422/5xx);
- ``window.location.hash`` + ``hashchange``/``DOMContentLoaded`` events;
- a ``navigator.clipboard.writeText`` stub that records the last copied text;
- ``window.__dump()`` serializing the rendered state (visible view, cards,
  metric cards, table rows, copy buttons, live region, error panels, ...)
  so tests read the DOM back as data.

No test hooks are added to the shipped UI code: tests drive the public
entry points only (hash routing + the fetch contract + click listeners).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

WEB = Path(__file__).resolve().parents[1] / "src" / "serving_verdict" / "web"

# Anchors the UI must populate; all must exist in index.html.
REQUIRED_IDS = (
    "index-view",
    "detail-view",
    "error-view",
    "list-heading",
    "verdict-count",
    "verdict-list",
    "index-loading",
    "index-empty",
    "empty-title",
    "empty-copy",
    "onboarding-cmds",
    "index-error",
    "index-retry",
    "error-title",
    "error-text",
    "error-advice",
    "error-retry",
    "error-back",
    "back-btn",
    "detail-loading",
    "detail-created",
    "detail-body",
    "verdict-label",
    "detail-case-id",
    "reason-codes",
    "gate-narrative",
    "gate-narrative-body",
    "baseline-artifact",
    "baseline-sha",
    "candidate-artifact",
    "candidate-sha",
    "bundle-digest",
    "integrity",
    "metric-cards",
    "metrics-table",
    "metrics-empty",
    "gates-table",
    "claim-boundary",
    "copy-live",
)

TABLE_IDS = {"metrics-table", "gates-table"}

HARNESS_JS = r"""
// Minimal DOM harness (same global scope as the real ui.js).
// Top-level `var`s in a sloppy script become globals, so ui.js resolves
// `document` / `window` / `fetch` / `navigator` to these implementations.
function _Node(tag, id) {
  this.tagName = tag;
  this.id = id || "";
  this.className = "";
  this.textContent = "";
  this.children = [];
  this._listeners = {};
  this.attributes = {};
  this.title = "";
  this.href = null;
  this.disabled = false;
  this.type = "button";
}
_Node.prototype.appendChild = function (c) { this.children.push(c); return c; };
Object.defineProperty(_Node.prototype, "id", {
  // Nodes created by ui.js register their id dynamically (copy buttons,
  // metric cards, expand buttons, dimension blocks).
  set: function (v) { this._id = v == null ? "" : String(v); if (this._id) __ids[this._id] = this; },
  get: function () { return this._id || ""; },
});
_Node.prototype.setAttribute = function (n, v) { this.attributes[n] = String(v); };
_Node.prototype.getAttribute = function (n) {
  if (Object.prototype.hasOwnProperty.call(this.attributes, n)) return this.attributes[n];
  if (n === "title" && this.title) return this.title;
  return null;
};
_Node.prototype.addEventListener = function (t, fn) {
  (this._listeners[t] = this._listeners[t] || []).push(fn);
};
_Node.prototype.querySelector = function (sel) {
  for (var i = 0; i < this.children.length; i++) {
    if (this.children[i].tagName === sel) return this.children[i];
  }
  return null;
};
function _childTag(parent, tag) {
  for (var i = 0; i < parent.children.length; i++) {
    if (parent.children[i].tagName === tag) return parent.children[i];
  }
  return null;
}
function _hasClass(node, name) {
  return node.className.split(" ").indexOf(name) !== -1;
}
Object.defineProperty(_Node.prototype, "classList", {
  get: function () {
    var self = this;
    return {
      add: function () {
        for (var i = 0; i < arguments.length; i++) {
          if (!_hasClass(self, arguments[i])) {
            self.className = (self.className + " " + arguments[i]).trim();
          }
        }
      },
      remove: function () {
        for (var i = 0; i < arguments.length; i++) {
          var name = arguments[i];
          self.className = self.className
            .split(" ")
            .filter(function (c) { return c && c !== name; })
            .join(" ");
        }
      },
      toggle: function (name, force) {
        var has = _hasClass(self, name);
        var want = force === undefined ? !has : !!force;
        if (want && !has) self.className = (self.className + " " + name).trim();
        else if (!want && has) {
          self.className = self.className.split(" ").filter(function (c) { return c !== name; }).join(" ");
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
var __lastCopied = "";
var __hashValue = "";

var document = {
  title: "Serving Verdict",
  createElement: function (tag) { return new _Node(tag); },
  getElementById: function (id) { return __ids[id] || null; },
  addEventListener: function (t, fn) {
    (__docListeners[t] = __docListeners[t] || []).push(fn);
  },
};
var navigator = {
  clipboard: {
    writeText: function (t) {
      __lastCopied = String(t);
      return Promise.resolve();
    },
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
  __click: function (id) {
    var n = __ids[id];
    if (!n) return false;
    var ls = n._listeners["click"] || [];
    for (var i = 0; i < ls.length; i++) ls[i]({ type: "click" });
    return true;
  },
  __dump: function () {
    function text(n) {
      if (!n) return "";
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
    function visible(id) {
      var n = __ids[id];
      return !!(n && n.className.split(" ").indexOf("hidden") === -1);
    }
    function attrOf(n, name) {
      if (!n) return null;
      if (Object.prototype.hasOwnProperty.call(n.attributes, name)) return n.attributes[name];
      if (name === "title" && n.title) return n.title;
      return null;
    }
    function buttonIn(parent) {
      for (var i = 0; i < parent.children.length; i++) {
        if (parent.children[i].tagName === "button") return parent.children[i];
      }
      return null;
    }
    var views = ["index-view", "detail-view", "error-view"];
    var visibleView = "none";
    for (var v = 0; v < views.length; v++) {
      if (visible(views[v])) { visibleView = views[v]; break; }
    }
    var list = __ids["verdict-list"];
    var listCards = [];
    for (var i = 0; i < list.children.length; i++) {
      var c = list.children[i];
      listCards.push({
        href: c.href || null,
        cls: c.className,
        text: text(c),
        ariaLabel: attrOf(c, "aria-label"),
      });
    }
    var ul = __ids["onboarding-cmds"];
    var onboardingCmds = [];
    for (var i = 0; i < ul.children.length; i++) {
      var li = ul.children[i];
      var code = _childTag(li, "code");
      var btn = buttonIn(li);
      onboardingCmds.push({
        cmd: code ? code.textContent : "",
        copyAria: btn ? attrOf(btn, "aria-label") : null,
        copyValue: btn ? (btn.getAttribute("data-copy") || "") : "",
      });
    }
    var narrativeSection = __ids["gate-narrative"];
    var narrative = __ids["gate-narrative-body"] || narrativeSection;
    function linksOf(n, out) {
      if (!n) return out;
      for (var i = 0; i < n.children.length; i++) {
        var c = n.children[i];
        if (c.tagName === "a") out.push({ href: c.href || null, text: text(c), cls: c.className });
        linksOf(c, out);
      }
      return out;
    }
    var narrativeLinks = linksOf(narrative, []);
    var copyHosts = ["baseline-sha", "candidate-sha", "bundle-digest"];
    var copyButtons = [];
    copyHosts.forEach(function (id) {
      var host = __ids[id];
      var b = buttonIn(host);
      copyButtons.push({
        host: id,
        present: !!b,
        aria: b ? attrOf(b, "aria-label") : null,
        value: b ? (b.getAttribute("data-copy") || "") : "",
      });
    });
    var wrap = __ids["metric-cards"];
    var metricCards = [];
    for (var i = 0; i < wrap.children.length; i++) {
      var card = wrap.children[i];
      var h3 = _childTag(card, "h3");
      var grid = null;
      for (var j = 0; j < card.children.length; j++) {
        var ch = card.children[j];
        if (ch.tagName === "div" && _hasClass(ch, "metric-grid")) grid = ch;
      }
      var cells = [];
      if (grid) {
        for (var k = 0; k < grid.children.length; k++) {
          var g = grid.children[k];
          var first = g.children.length > 0 ? g.children[0] : null;
          var second = g.children.length > 1 ? g.children[1] : null;
          cells.push({
            cls: g.className,
            label: first ? text(first) : "",
            value: second ? text(second) : (first ? "" : text(g)),
            title: (g.title || "") || (second && second.title ? second.title : ""),
          });
        }
      }
      var exp = null;
      for (var j = 0; j < card.children.length; j++) {
        if (card.children[j].tagName === "button") {
          exp = {
            id: card.children[j].id,
            ariaExpanded: attrOf(card.children[j], "aria-expanded"),
            ariaControls: attrOf(card.children[j], "aria-controls"),
            text: text(card.children[j]),
          };
        }
      }
      var dims = null;
      var dl = _childTag(card, "dl");
      if (dl) {
        dims = {
          id: dl.id,
          visible: dl.className.split(" ").indexOf("hidden") === -1,
          text: text(dl),
        };
      }
      metricCards.push({
        id: card.id,
        title: h3 ? h3.textContent : "",
        titleAttr: h3 ? (h3.title || "") : "",
        cls: card.className,
        cells: cells,
        expandBtn: exp,
        dims: dims,
      });
    }
    return {
      documentTitle: document.title,
      lastCopied: __lastCopied,
      visibleView: visibleView,
      detailLoadingVisible: visible("detail-loading"),
      detailBodyVisible: visible("detail-body"),
      listCards: listCards,
      listHeading: __ids["list-heading"].textContent,
      verdictCount: __ids["verdict-count"].textContent,
      indexEmptyVisible: visible("index-empty"),
      indexEmptyText: text(__ids["index-empty"]),
      onboardingCmds: onboardingCmds,
      indexErrorVisible: visible("index-error"),
      indexErrorText: __ids["index-error"].textContent,
      indexLoadingVisible: visible("index-loading"),
      indexRetryVisible: visible("index-retry"),
      errorTitle: __ids["error-title"].textContent,
      errorText: __ids["error-text"].textContent,
      errorAdvice: __ids["error-advice"].textContent,
      errorRole: attrOf(__ids["error-view"], "role"),
      errorRetryVisible: visible("error-retry"),
      backVisible: visible("back-btn"),
      verdictLabel: __ids["verdict-label"].textContent,
      verdictClass: __ids["verdict-label"].className,
      caseId: __ids["detail-case-id"].textContent,
      createdText: __ids["detail-created"].textContent,
      reasonCodes: text(__ids["reason-codes"]),
      narrative: {
        visible: visible("gate-narrative"),
        text: text(narrative),
        links: narrativeLinks,
      },
      baselineArtifact: __ids["baseline-artifact"].textContent,
      baselineSha: __ids["baseline-sha"].textContent,
      candidateArtifact: __ids["candidate-artifact"].textContent,
      candidateSha: __ids["candidate-sha"].textContent,
      bundleDigest: __ids["bundle-digest"].textContent,
      integrityText: __ids["integrity"].textContent,
      integrityClass: __ids["integrity"].className,
      copyButtons: copyButtons,
      copyLive: {
        text: __ids["copy-live"].textContent,
        role: attrOf(__ids["copy-live"], "role"),
        ariaLive: attrOf(__ids["copy-live"], "aria-live"),
      },
      metricCards: metricCards,
      metricsRows: rows("metrics-table"),
      metricsEmptyVisible: visible("metrics-empty"),
      gateRows: rows("gates-table"),
      claimBoundary: __ids["claim-boundary"].textContent,
    };
  },
};

function fetch(path) {
  return Promise.resolve().then(function () {
    var r = __routes[path];
    if (!r) {
      return {
        ok: false,
        status: 404,
        json: function () { return Promise.resolve({ error: "not found" }); },
      };
    }
    if (typeof r === "object" && r.status !== undefined) {
      return {
        ok: r.status >= 200 && r.status < 300,
        status: r.status,
        json: function () {
          return Promise.resolve(r.body !== undefined ? r.body : { error: "error" });
        },
      };
    }
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
"""


def set_json(ctx, name: str, doc) -> None:
    """Set a JSON-serializable value on the context (quickjs passes via JSON)."""
    ctx.set(name, ctx.parse_json(json.dumps(doc)))


def build_dom(ctx) -> None:
    """Populate the harness DOM from the real index.html anchors."""
    html = (WEB / "index.html").read_text(encoding="utf-8")
    ids = re.findall(r'id="([^"]+)"', html)
    for rid in REQUIRED_IDS:
        assert rid in ids, f"index.html is missing required anchor id={rid!r}"
    # Tables need a tbody child (matching index.html's static markup).
    for rid in ids:
        if rid in TABLE_IDS:
            ctx.eval(f"__ids[{json.dumps(rid)}] = new _Node('table', {json.dumps(rid)})")
            ctx.eval(f"__ids[{json.dumps(rid)}].appendChild(new _Node('tbody'))")
        else:
            ctx.eval(f"__ids[{json.dumps(rid)}] = new _Node('div', {json.dumps(rid)})")
    # Reflect static a11y attributes (role / aria-*) from the real markup so
    # tests can assert them without re-implementing HTML parsing in the harness.
    for m in re.finditer(
        r'<[a-zA-Z][^>]*?id="([^"]+)"([^>]*)>', html
    ):
        rid, rest = m.group(1), m.group(2)
        attrs = re.findall(r'(role|aria-[a-z-]+)="([^"]*)"', rest)
        for name, value in attrs:
            ctx.eval(
                f"__ids[{json.dumps(rid)}].setAttribute({json.dumps(name)}, {json.dumps(value)})"
            )


def boot(ui_js: str):
    """Fresh context: harness + real ui.js, DOM built, UI booted at #/ (index)."""
    import quickjs  # type: ignore[import-not-found]

    ctx = quickjs.Context()
    ctx.eval(HARNESS_JS)
    build_dom(ctx)
    set_json(ctx, "__routes", {})
    ctx.eval(ui_js)
    ctx.eval("window.__domReady()")  # runs route() at hash ""
    flush(ctx)
    return ctx


def flush(ctx) -> None:
    """Drain the microtask queue (fetch stubs + clipboard promises)."""
    while ctx.execute_pending_job():
        pass


def navigate(ctx, case_id: str, doc: dict | None) -> dict:
    """Route the UI to #/{case_id} serving ``doc`` via the fetch stub.

    The UI fetches the full API path ``/api/v1/verdicts/{case_id}`` for a
    hash fragment ``#{case_id}``; the stub is keyed by the fetch path.
    """
    set_json(ctx, "__routes", {f"/api/v1/verdicts/{case_id}": doc})
    ctx.eval(f"window.location.hash = {json.dumps('/' + case_id)}")
    ctx.eval("window.__fire('hashchange')")
    flush(ctx)
    return json.loads(ctx.eval("window.__dump()").json())


def go_index(ctx) -> dict:
    """Route back to the index (hash cleared) and flush."""
    ctx.eval("window.location.hash = ''")
    ctx.eval("window.__fire('hashchange')")
    flush(ctx)
    return json.loads(ctx.eval("window.__dump()").json())


def click(ctx, node_id: str) -> None:
    """Fire the click listener(s) registered on ``#node_id``; flush."""
    ctx.eval(f"window.__click({json.dumps(node_id)})")
    flush(ctx)


def dump(ctx) -> dict:
    """Serialize the current rendered DOM state (see ``window.__dump``)."""
    flush(ctx)
    return json.loads(ctx.eval("window.__dump()").json())
