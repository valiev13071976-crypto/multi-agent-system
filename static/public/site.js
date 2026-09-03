/**
 * Privacy-respecting product analytics stub.
 * No fingerprinting. No external analytics without human approval.
 */
(function () {
  const KEY = "panda_pub_analytics_v1";
  const CONSENT_KEY = "panda_analytics_consent";

  function consented() {
    try {
      return sessionStorage.getItem(CONSENT_KEY) === "1";
    } catch (_) {
      return false;
    }
  }

  function track(event, props) {
    if (!consented()) {
      // Buffer locally only if consent later; for now record in-memory session without PII
    }
    const entry = {
      event: String(event || ""),
      ts: new Date().toISOString(),
      path: location.pathname,
      props: props || {},
    };
    try {
      const buf = JSON.parse(sessionStorage.getItem(KEY) || "[]");
      buf.push(entry);
      sessionStorage.setItem(KEY, JSON.stringify(buf.slice(-100)));
    } catch (_) {}
    // External sink intentionally NOT connected
  }

  function pageview() {
    const map = {
      "/": "landing_view",
      "/capabilities": "capability_view",
      "/plans": "plan_view",
      "/register": "signup_start_page",
      "/login": "login",
    };
    const ev = map[location.pathname];
    if (ev) track(ev);
  }

  window.PandaPublicAnalytics = {
    track,
    grantConsent: function () {
      try {
        sessionStorage.setItem(CONSENT_KEY, "1");
      } catch (_) {}
    },
    externalConnected: false,
  };

  document.addEventListener("DOMContentLoaded", pageview);
  document.addEventListener("click", function (e) {
    const t = e.target && e.target.closest && e.target.closest("[data-analytics]");
    if (t) track(t.getAttribute("data-analytics"));
  });
})();
