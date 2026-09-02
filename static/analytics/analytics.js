(function () {
  "use strict";

  var API_BASE = "/api/v1/analytics";
  var SESSION_KEY = "panda_api_key";
  var TENANT = "tenant-a";

  function $(id) { return document.getElementById(id); }

  function headers() {
    var key = sessionStorage.getItem(SESSION_KEY) || "";
    return { "X-API-Key": key, "Accept": "application/json" };
  }

  function api(path) {
    return fetch(API_BASE + path, { headers: headers() }).then(function (r) {
      if (r.status === 401 || r.status === 403) {
        var err = new Error("UNAUTHORIZED");
        err.code = "UNAUTHORIZED";
        throw err;
      }
      return r.json().then(function (body) {
        if (!r.ok) throw new Error(body.message || body.detail?.message || "ERROR");
        return body;
      });
    });
  }

  function showState(kind, msg) {
    var banner = $("state-banner");
    banner.className = "state-banner state-" + kind.toLowerCase();
    banner.textContent = msg || kind;
    banner.classList.remove("hidden");
  }

  function clearState() {
    $("state-banner").classList.add("hidden");
  }

  function renderCards(container, cards) {
    container.innerHTML = '<div class="cards"></div>';
    var grid = container.querySelector(".cards");
    Object.keys(cards).forEach(function (key) {
      var c = cards[key];
      var el = document.createElement("div");
      el.className = "card";
      var status = c.status || "OK";
      var val = c.value;
      if (val === null || val === undefined) val = status === "NO_DATA" ? "—" : "N/A";
      el.innerHTML = '<div class="label">' + key.replace(/_/g, " ") + '</div><div class="value">' + val + '</div><div class="muted">' + status + (c.currency ? " " + c.currency : "") + "</div>";
      grid.appendChild(el);
    });
  }

  function loadOverview() {
    showState("loading", "Loading overview…");
    return api("/overview?tenant_id=" + encodeURIComponent(TENANT) + "&window=30d")
      .then(function (data) {
        clearState();
        if (data.status === "PARTIAL") showState("partial", "Partial data — some sources unavailable");
        if (data.status === "STALE") showState("stale", "Data may be stale");
        if (data.status === "NO_DATA") showState("no-data", "No data for selected window");
        renderCards($("overview-section"), data.cards || {});
        $("overview-section").insertAdjacentHTML("afterbegin", '<p class="muted">Mode: ' + (data.mode || "FIXTURE") + " · Generated: " + (data.generated_at || "") + "</p>");
      })
      .catch(function (e) {
        if (e.code === "UNAUTHORIZED") showState("unauthorized", "Unauthorized");
        else showState("error", e.message || "Error");
      });
  }

  function loadSection(name) {
    document.querySelectorAll(".section").forEach(function (s) { s.classList.add("hidden"); });
    var sec = $(name + "-section");
    sec.classList.remove("hidden");
    showState("loading", "Loading " + name + "…");
    var path = "/" + name + "?tenant_id=" + encodeURIComponent(TENANT);
    if (name === "marketplaces") path += "&window=30d";
    return api(path).then(function (data) {
      clearState();
      sec.innerHTML = "<pre>" + JSON.stringify(data, null, 2) + "</pre>";
      if (data.status === "NO_DATA") showState("no-data", "No data");
      if (data.status === "PARTIAL") showState("partial", "Partial data");
    }).catch(function (e) {
      if (e.code === "UNAUTHORIZED") showState("unauthorized", "Unauthorized");
      else showState("error", e.message || "Error");
    });
  }

  function bindNav() {
    document.querySelectorAll(".nav-btn").forEach(function (btn) {
      btn.addEventListener("click", function () {
        document.querySelectorAll(".nav-btn").forEach(function (b) { b.classList.remove("active"); });
        btn.classList.add("active");
        var section = btn.getAttribute("data-section");
        if (section === "overview") loadOverview();
        else loadSection(section);
      });
    });
    $("refresh-btn").addEventListener("click", loadOverview);
  }

  function showApp() {
    $("auth-gate").classList.add("hidden");
    $("app").classList.remove("hidden");
    loadOverview();
    bindNav();
  }

  function showAuth() {
    $("auth-gate").classList.remove("hidden");
    $("app").classList.add("hidden");
  }

  $("auth-submit").addEventListener("click", function () {
    var key = $("api-key-input").value.trim();
    if (!key) { $("auth-error").textContent = "API key required"; return; }
    sessionStorage.setItem(SESSION_KEY, key);
    $("auth-error").textContent = "";
    showApp();
  });

  if (sessionStorage.getItem(SESSION_KEY)) showApp();
  else showAuth();
})();
