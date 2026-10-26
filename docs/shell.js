/* What every page shares: the nav, the token, and a few helpers.

   The four pages grew up separately and each carried its own copy of the API
   lookup, the token handling and a nav that linked to some of the others but
   not all. Fleet linked to none. This is the one copy, loaded by every page
   before its own script, as window.TN.

   The token used to be asked for with window.prompt. A browser that blocks
   prompts showed "Could not load invoices: prompt() is not supported", and a
   new device got a bare system dialog with no way back to change it. It is now
   a small settings panel reachable from the nav on every page. */
(function () {
  "use strict";

  var TOKEN_KEY = "turonomics.tolls.token";

  // The UI is a static site and the API a separate service. ?api= overrides it
  // for local work, and is carried on the nav links so it survives moving
  // between pages.
  var override = new URLSearchParams(location.search).get("api");
  var API = (function () {
    if (override) return override.replace(/\/$/, "");
    var meta = document.querySelector('meta[name="turonomics-api"]');
    return (meta && meta.content ? meta.content : "").replace(/\/$/, "");
  })();

  var PAGES = [
    ["invoices", "Invoices", "invoices/"],
    ["trips", "Trips", "trips/"],
    ["tolls", "Tolls", "tolls/"],
    ["fleet", "Fleet", "fleet/"]
  ];

  function token() {
    try { return localStorage.getItem(TOKEN_KEY) || ""; } catch (e) { return ""; }
  }
  function rememberToken(value) {
    try { value ? localStorage.setItem(TOKEN_KEY, value) : localStorage.removeItem(TOKEN_KEY); }
    catch (e) { /* private window: it just has to be entered again */ }
  }
  function authHeaders() {
    var value = token();
    return value ? { Authorization: "Bearer " + value } : {};
  }

  function make(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  }

  // -------------------------------------------------------------------------
  // The settings panel
  // -------------------------------------------------------------------------

  var panel = null;
  var waiting = [];

  function buildPanel() {
    if (panel) return panel;
    var dlg = make("dialog", "tn-settings");
    dlg.id = "tn-settings";
    dlg.setAttribute("aria-labelledby", "tn-settings-title");
    var form = make("form");
    form.method = "dialog";
    form.appendChild(make("h2", null, "Settings")).id = "tn-settings-title";
    var why = form.appendChild(make("p", "tn-why"));
    why.id = "tn-settings-why";
    var label = form.appendChild(make("label", "tn-field"));
    label.appendChild(make("span", null, "Tolls token"));
    var input = label.appendChild(make("input"));
    input.id = "tn-token";
    input.type = "password";
    input.autocomplete = "off";
    input.spellcheck = false;
    input.placeholder = "TOLLS_TOKEN from the API service";
    form.appendChild(make("p", "tn-help",
      "Kept in this browser only. Pages that name guests, and every change, need it."));
    var api = form.appendChild(make("p", "tn-help tn-api"));
    api.textContent = "API: " + (API || "not set");
    var row = form.appendChild(make("div", "tn-buttons"));
    var save = row.appendChild(make("button", "tn-btn tn-go", "Save"));
    save.id = "tn-save";
    save.value = "save";
    var forget = row.appendChild(make("button", "tn-btn", "Forget"));
    forget.id = "tn-forget";
    forget.type = "button";
    var cancel = row.appendChild(make("button", "tn-btn", "Cancel"));
    cancel.id = "tn-cancel";
    cancel.value = "cancel";
    dlg.appendChild(form);
    document.body.appendChild(dlg);

    forget.addEventListener("click", function () {
      rememberToken("");
      input.value = "";
      why.textContent = "Forgotten. Pages that need it will ask again.";
    });
    dlg.addEventListener("close", function () {
      var saved = dlg.returnValue === "save" && input.value.trim();
      if (saved) rememberToken(input.value.trim());
      var done = waiting;
      waiting = [];
      done.forEach(function (resolve) { resolve(!!saved); });
      // Opened from the nav rather than by a request that is waiting on it:
      // the page reloads what it could not read without the token.
      if (saved && !done.length) window.dispatchEvent(new Event("tn-token-saved"));
    });
    panel = { dlg: dlg, why: why, input: input };
    return panel;
  }

  function openPanel(reason) {
    var p = buildPanel();
    p.why.textContent = reason || "";
    p.why.hidden = !reason;
    p.input.value = token();
    if (!p.dlg.open) {
      if (typeof p.dlg.showModal === "function") p.dlg.showModal();
      else p.dlg.setAttribute("open", "");
    }
    p.input.focus();
  }

  // Resolves true once a token has been saved, false if the panel was closed
  // without one. Several callers asking at once share the one panel.
  function askToken(reason) {
    return new Promise(function (resolve) {
      waiting.push(resolve);
      openPanel(reason || "This needs the tolls token.");
    });
  }

  // For a write: true at once if no token is needed or one is saved.
  function needToken(required) {
    if (!required || token()) return Promise.resolve(true);
    return askToken("Changes need the tolls token.");
  }

  // A read or write that may need the token: tried with the saved one, and
  // once more after asking if the API refuses it. A second refusal forgets
  // the token, so the next try asks rather than failing the same way forever.
  function authed(path, init, reason) {
    init = init || {};
    var go = function () {
      return fetch(API + path, Object.assign({}, init, {
        headers: Object.assign({}, init.headers || {}, authHeaders())
      }));
    };
    return go().then(function (r) {
      if (r.status !== 401) return r;
      rememberToken("");
      return askToken(reason || "The API refused the saved token, or none is saved.")
        .then(function (ok) {
          if (!ok) throw new Error("the token is needed for this");
          return go();
        });
    }).then(function (r) {
      if (r.status === 401) { rememberToken(""); throw new Error("the token was rejected"); }
      return r;
    });
  }

  // -------------------------------------------------------------------------
  // The nav
  // -------------------------------------------------------------------------

  function root() {
    var nav = document.querySelector("[data-tn-nav]");
    return (nav && nav.getAttribute("data-tn-root")) || "../";
  }

  function withApi(href) {
    return override ? href + "?api=" + encodeURIComponent(override) : href;
  }

  function renderNav() {
    var nav = document.querySelector("[data-tn-nav]");
    if (!nav) return;
    var current = nav.getAttribute("data-tn-nav");
    nav.className = "tn-nav";
    nav.setAttribute("aria-label", "Pages");
    PAGES.forEach(function (p) {
      var a = make("a", null, p[1]);
      a.href = withApi(root() + p[2]);
      if (p[0] === current) a.setAttribute("aria-current", "page");
      nav.appendChild(a);
    });
    // An icon rather than the word, so the nav fits one line on a phone.
    var gear = make("button", "tn-gear", "\u2699");
    gear.type = "button";
    gear.id = "tn-open-settings";
    gear.title = "Settings";
    gear.setAttribute("aria-label", "Settings");
    gear.addEventListener("click", function () {
      openPanel(token() ? "" : "No token saved in this browser.");
    });
    nav.appendChild(gear);
  }

  // -------------------------------------------------------------------------
  // Helpers
  // -------------------------------------------------------------------------

  // Guest names keep whatever casing Turo and the guest typed: ALLISON,
  // linnea. A name all in one case is shown in title case; one with mixed
  // case is left alone, since McKenna or DeShawn is the guest's own spelling.
  function personName(name) {
    if (!name) return name;
    var s = String(name);
    if (s !== s.toUpperCase() && s !== s.toLowerCase()) return s;
    return s.toLowerCase().replace(/(^|[\s\-'’.])(\S)/g, function (m, sep, c) {
      return sep + c.toUpperCase();
    });
  }

  window.TN = {
    API: API,
    token: token,
    rememberToken: rememberToken,
    authHeaders: authHeaders,
    askToken: askToken,
    needToken: needToken,
    authed: authed,
    openSettings: openPanel,
    personName: personName,
    withApi: withApi
  };

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", renderNav);
  else renderNav();
})();
