/** Panda Owner management dashboard — business-readable, no fake metrics. */
(function () {
  "use strict";

  const SESSION_KEY = "panda_api_key";

  const els = {
    authGate: document.getElementById("auth-gate"),
    app: document.getElementById("app"),
    authBrand: document.getElementById("auth-brand"),
    sidebarBrand: document.getElementById("sidebar-brand"),
    apiKey: document.getElementById("api-key-input"),
    authSubmit: document.getElementById("auth-submit"),
    authError: document.getElementById("auth-error"),
    content: document.getElementById("content"),
    viewTitle: document.getElementById("view-title"),
    status: document.getElementById("status"),
    account: document.getElementById("account-label"),
    accessDenied: document.getElementById("access-denied"),
    sidebar: document.getElementById("sidebar"),
    sidebarToggle: document.getElementById("sidebar-toggle"),
    logout: document.getElementById("logout-btn"),
  };

  let roleContext = { loaded: false, isManagement: false, tenantId: null };

  function show(el) { el.classList.remove("hidden"); }
  function hide(el) { el.classList.add("hidden"); }

  function esc(text) {
    return String(text ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function headers() {
    const key = sessionStorage.getItem(SESSION_KEY) || "";
    const h = { Accept: "application/json", "X-API-Key": key };
    if (roleContext.tenantId) h["X-Active-Tenant"] = roleContext.tenantId;
    return h;
  }

  async function fetchJson(path) {
    const res = await fetch(path, { headers: headers() });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      const err = new Error(data.message || data.detail?.message || res.statusText);
      err.status = res.status;
      throw err;
    }
    return data;
  }

  function setStatus(msg) {
    els.status.textContent = msg || "";
  }

  function card(label, value, hint) {
    return `<div class="card"><div class="label">${esc(label)}</div><div class="value">${esc(value)}</div>${hint ? `<div class="hint">${esc(hint)}</div>` : ""}</div>`;
  }

  function unavailableCard(label) {
    return card(label, "—", "Данные пока недоступны");
  }

  function formatCardValue(cardData) {
    if (!cardData) return { value: "—", hint: "Нет данных" };
    if (cardData.status === "NO_DATA" || cardData.status === "UNAVAILABLE") {
      return { value: "—", hint: "Данные пока недоступны" };
    }
    const val = cardData.value;
    if (val === null || val === undefined || val === "") {
      return { value: "0", hint: cardData.unit || "" };
    }
    return { value: String(val), hint: cardData.currency || cardData.unit || "" };
  }

  async function renderHome() {
    els.viewTitle.textContent = "Главная";
    setStatus("Загрузка…");
    els.content.innerHTML = "";
    const cards = [];

    try {
      const dash = await fetchJson("/api/admin/ops/dashboard");
      cards.push(card("Состояние Panda", dash.health?.overall || "—"));
      cards.push(card("Активные задачи", dash.active_runs ?? "—"));
      cards.push(card("Ожидают подтверждения", dash.pending_approvals ?? "—"));
    } catch (_) {
      cards.push(unavailableCard("Состояние Panda"));
    }

    if (roleContext.tenantId) {
      try {
        const overview = await fetchJson(`/api/v1/analytics/overview?tenant_id=${encodeURIComponent(roleContext.tenantId)}&window=30d`);
        const c = overview.cards || {};
        const orders = formatCardValue(c.orders);
        cards.push(card("Заказы (30 дн.)", orders.value, orders.hint));
        const ai = formatCardValue(c.ai_cost_usd);
        cards.push(card("Расходы AI", ai.value, ai.hint || "USD"));
      } catch (_) {
        cards.push(unavailableCard("Аналитика"));
      }
    }

    els.content.innerHTML = `<div class="card-grid">${cards.join("")}</div>`;
    setStatus("");
  }

  async function renderUsers() {
    els.viewTitle.textContent = "Пользователи";
    setStatus("Загрузка…");
    try {
      const data = await fetchJson("/api/product/members");
      const rows = (data.items || []).map(
        (m) => `<tr><td>${esc(m.user_id)}</td><td>${esc(m.role)}</td><td>${esc(m.status || "active")}</td></tr>`
      ).join("");
      els.content.innerHTML = rows
        ? `<div class="responsive-table-wrap"><table class="mgmt-table"><thead><tr><th>Пользователь</th><th>Роль</th><th>Статус</th></tr></thead><tbody>${rows}</tbody></table></div>`
        : `<div class="empty-state">Пользователей пока нет.</div>`;
    } catch (_) {
      els.content.innerHTML = `<div class="empty-state">Данные о пользователях пока недоступны.</div>`;
    }
    setStatus("");
  }

  async function renderUsage() {
    els.viewTitle.textContent = "Использование Panda";
    setStatus("Загрузка…");
    if (!roleContext.tenantId) {
      els.content.innerHTML = `<div class="empty-state">Данных пока недостаточно.</div>`;
      setStatus("");
      return;
    }
    try {
      const data = await fetchJson(`/api/v1/analytics/workflows?tenant_id=${encodeURIComponent(roleContext.tenantId)}`);
      const cards = [];
      const metrics = data.metrics || data.cards || {};
      Object.keys(metrics).slice(0, 6).forEach((key) => {
        const formatted = formatCardValue(metrics[key]);
        cards.push(card(key.replace(/_/g, " "), formatted.value, formatted.hint));
      });
      els.content.innerHTML = cards.length
        ? `<div class="card-grid">${cards.join("")}</div>`
        : `<div class="empty-state">Данных пока недостаточно.</div>`;
    } catch (_) {
      els.content.innerHTML = `<div class="empty-state">Данных пока недостаточно.</div>`;
    }
    setStatus("");
  }

  async function renderAiCost() {
    els.viewTitle.textContent = "Расходы AI";
    setStatus("Загрузка…");
    if (!roleContext.tenantId) {
      els.content.innerHTML = `<div class="empty-state">Данные о расходах пока недоступны.</div>`;
      setStatus("");
      return;
    }
    try {
      const data = await fetchJson(`/api/v1/analytics/finops?tenant_id=${encodeURIComponent(roleContext.tenantId)}`);
      const cards = [];
      if (data.cards) {
        Object.keys(data.cards).forEach((key) => {
          const formatted = formatCardValue(data.cards[key]);
          cards.push(card(key.replace(/_/g, " "), formatted.value, formatted.hint));
        });
      } else if (data.total_cost != null) {
        cards.push(card("Итого", String(data.total_cost), data.currency || ""));
      }
      els.content.innerHTML = cards.length
        ? `<div class="card-grid">${cards.join("")}</div>`
        : `<div class="empty-state">Данные о расходах пока недоступны.</div>`;
    } catch (_) {
      els.content.innerHTML = `<div class="empty-state">Данные о расходах пока недоступны.</div>`;
    }
    setStatus("");
  }

  async function renderHealth() {
    els.viewTitle.textContent = "Состояние системы";
    setStatus("Загрузка…");
    try {
      const dash = await fetchJson("/api/admin/ops/dashboard");
      const alerts = await fetchJson("/api/admin/ops/alerts").catch(() => []);
      const cards = [
        card("Panda", dash.health?.overall || "—"),
        card("Очередь", dash.queued_jobs ?? "—"),
        card("Ошибки", dash.failed_jobs ?? "—"),
      ];
      let alertHtml = "";
      if (Array.isArray(alerts) && alerts.length) {
        alertHtml = `<ul>${alerts.slice(0, 10).map((a) => `<li>${esc(a.message || a.source)}</li>`).join("")}</ul>`;
      } else {
        alertHtml = `<p class="muted">Предупреждений нет.</p>`;
      }
      els.content.innerHTML = `<div class="card-grid">${cards.join("")}</div>${alertHtml}`;
    } catch (_) {
      els.content.innerHTML = `<div class="empty-state">Данные о состоянии системы пока недоступны.</div>`;
    }
    setStatus("");
  }

  async function renderActivity() {
    els.viewTitle.textContent = "События";
    setStatus("Загрузка…");
    try {
      const audit = await fetchJson("/api/admin/ops/audit?limit=30");
      const items = audit.items || audit || [];
      if (!items.length) {
        els.content.innerHTML = `<div class="empty-state">Событий пока нет.</div>`;
      } else {
        els.content.innerHTML = `<div class="responsive-table-wrap"><table class="mgmt-table"><thead><tr><th>Время</th><th>Действие</th><th>Результат</th></tr></thead><tbody>${items.map((a) => `<tr><td>${esc(a.timestamp)}</td><td>${esc(a.action)}</td><td>${esc(a.result)}</td></tr>`).join("")}</tbody></table></div>`;
      }
    } catch (_) {
      els.content.innerHTML = `<div class="empty-state">Журнал событий пока недоступен.</div>`;
    }
    setStatus("");
  }

  const views = {
    home: renderHome,
    users: renderUsers,
    usage: renderUsage,
    "ai-cost": renderAiCost,
    health: renderHealth,
    activity: renderActivity,
  };

  async function loadView(name) {
    document.querySelectorAll(".app-nav a[data-view]").forEach((a) => {
      a.classList.toggle("active", a.dataset.view === name);
    });
    const fn = views[name] || renderHome;
    await fn();
    els.sidebar.classList.remove("open");
  }

  async function enterApp() {
    roleContext = await window.PandaRoleContext.resolveRoleContext(sessionStorage.getItem(SESSION_KEY));
    if (!roleContext.isManagement) {
      hide(els.app);
      show(els.accessDenied);
      hide(els.authGate);
      return;
    }
    els.account.textContent = `Роль: ${roleContext.role}`;
    hide(els.authGate);
    hide(els.accessDenied);
    show(els.app);
    await loadView("home");
  }

  async function onAuth() {
    els.authError.textContent = "";
    sessionStorage.setItem(SESSION_KEY, els.apiKey.value.trim());
    try {
      await fetchJson("/api/v1/business-assistant/conversations");
      await enterApp();
    } catch (e) {
      sessionStorage.removeItem(SESSION_KEY);
      els.authError.textContent = window.PandaCopy.mapError(e);
    }
  }

  function logout() {
    sessionStorage.removeItem(SESSION_KEY);
    show(els.authGate);
    hide(els.app);
    hide(els.accessDenied);
  }

  function initBrand() {
    window.PandaBrand.applyDocumentBrand();
    window.PandaBrand.renderLogo(els.authBrand, { size: 48 });
    window.PandaBrand.renderLogo(els.sidebarBrand, { size: 32 });
  }

  document.getElementById("auth-submit").onclick = onAuth;
  els.logout.onclick = logout;
  els.sidebarToggle.onclick = () => els.sidebar.classList.toggle("open");
  document.querySelectorAll(".app-nav a[data-view]").forEach((a) => {
    a.addEventListener("click", (e) => {
      e.preventDefault();
      loadView(a.dataset.view).catch((err) => {
        setStatus("");
        els.content.innerHTML = `<div class="empty-state">${esc(window.PandaCopy.mapError(err))}</div>`;
      });
    });
  });

  initBrand();
  if (sessionStorage.getItem(SESSION_KEY)) {
    enterApp().catch(() => show(els.authGate));
  } else {
    show(els.authGate);
  }
})();
