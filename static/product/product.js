/* Panda Product settings — server-authoritative SaaS UI */

function headers() {
  const key = localStorage.getItem("panda_api_key") || "";
  const tenant = localStorage.getItem("panda_active_tenant") || "";
  const h = {};
  if (key) h["X-API-Key"] = key;
  if (tenant) h["X-Active-Tenant"] = tenant;
  return h;
}

function esc(text) {
  return String(text ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

async function api(path, options = {}) {
  const res = await fetch(path, { ...options, headers: { "Content-Type": "application/json", ...headers(), ...(options.headers || {}) } });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.message || data.detail?.message || res.statusText);
  return data;
}

function setStatus(msg) { document.getElementById("status").textContent = msg || ""; }

async function renderWorkspace() {
  document.getElementById("view-title").textContent = "Workspace";
  const d = await api("/api/product/onboarding");
  if (d.tenants?.length && !localStorage.getItem("panda_active_tenant")) {
    localStorage.setItem("panda_active_tenant", d.tenants[0].tenant_id);
  }
  document.getElementById("content").innerHTML = `
    <div class="card"><strong>Tenant</strong>: ${esc(d.tenant_id)}</div>
    <div class="card"><strong>Plan</strong>: ${esc(d.entitlements?.plan_id || "free")}</div>
    <button id="create-tenant">Create workspace</button>`;
  document.getElementById("create-tenant").onclick = async () => {
    const name = prompt("Workspace name");
    if (!name) return;
    const t = await api("/api/product/tenants", { method: "POST", body: JSON.stringify({ name }) });
    localStorage.setItem("panda_active_tenant", t.tenant_id);
    renderWorkspace();
  };
}

async function renderMembers() {
  document.getElementById("view-title").textContent = "Members";
  const d = await api("/api/product/members");
  const rows = (d.items || []).map((m) => `<tr><td>${esc(m.user_id)}</td><td>${esc(m.role)}</td></tr>`).join("");
  document.getElementById("content").innerHTML = `<table><thead><tr><th>User</th><th>Role</th></tr></thead><tbody>${rows}</tbody></table>`;
}

async function renderBilling() {
  document.getElementById("view-title").textContent = "Billing";
  const [plans, status] = await Promise.all([api("/api/product/plans"), api("/api/product/billing/status")]);
  document.getElementById("content").innerHTML = `
    <div class="card">Subscription: ${esc(status.subscription?.status || "free")}</div>
    <ul>${plans.map((p) => `<li>${esc(p.name)} — ${esc(p.price_minor / 100)} ${esc(p.currency)}</li>`).join("")}</ul>`;
}

async function renderPrivacy() {
  document.getElementById("view-title").textContent = "Privacy";
  const inv = await api("/api/product/privacy/inventory");
  document.getElementById("content").innerHTML = `
    <ul>${inv.map((i) => `<li>${esc(i.data_class)} (${esc(i.classification)})</li>`).join("")}</ul>
    <button id="export-btn">Request export</button>`;
  document.getElementById("export-btn").onclick = async () => {
    await api("/api/product/privacy/export", { method: "POST" });
    setStatus("Export requested");
  };
}

async function loadView(name) {
  document.querySelectorAll(".nav a[data-view]").forEach((a) => a.classList.toggle("active", a.dataset.view === name));
  try {
    setStatus("Loading…");
    if (name === "workspace") await renderWorkspace();
    if (name === "members") await renderMembers();
    if (name === "billing") await renderBilling();
    if (name === "privacy") await renderPrivacy();
    setStatus("");
  } catch (e) {
    setStatus(e.message);
  }
}

document.querySelectorAll(".nav a[data-view]").forEach((a) => a.addEventListener("click", (e) => {
  e.preventDefault();
  loadView(a.dataset.view);
}));

loadView("workspace");
