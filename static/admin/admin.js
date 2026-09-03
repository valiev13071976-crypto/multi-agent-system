/* Panda Operations Admin UI — server-authoritative projections only */

const state = { view: "overview" };

function authHeaders() {
  const key = localStorage.getItem("panda_admin_api_key") || "";
  return key ? { "X-API-Key": key } : {};
}

async function api(path) {
  const res = await fetch(path, { headers: authHeaders() });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error(data.message || data.detail?.message || res.statusText);
    err.status = res.status;
    throw err;
  }
  return data;
}

function esc(text) {
  return String(text ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function setStatus(msg) {
  document.getElementById("status").textContent = msg || "";
}

function setView(name, title) {
  state.view = name;
  document.getElementById("view-title").textContent = title;
  document.querySelectorAll(".nav a").forEach((a) => a.classList.toggle("active", a.dataset.view === name));
}

async function renderOverview() {
  setView("overview", "Обзор");
  setStatus("Загрузка…");
  const d = await api("/api/admin/ops/dashboard");
  setStatus("");
  document.getElementById("content").innerHTML = `
    <div class="cards">
      <div class="card"><div class="label">Health</div><div class="value">${esc(d.health.overall)}</div></div>
      <div class="card"><div class="label">Active runs</div><div class="value">${esc(d.active_runs)}</div></div>
      <div class="card"><div class="label">Queued jobs</div><div class="value">${esc(d.queued_jobs)}</div></div>
      <div class="card"><div class="label">Failed jobs</div><div class="value">${esc(d.failed_jobs)}</div></div>
      <div class="card"><div class="label">DLQ</div><div class="value">${esc(d.dlq_count)}</div></div>
      <div class="card"><div class="label">Pending approvals</div><div class="value">${esc(d.pending_approvals)}</div></div>
    </div>`;
}

async function renderTable(path, columns) {
  setStatus("Loading…");
  const data = await api(path);
  setStatus("");
  const rows = (data.items || data || []).map((row) => `<tr>${columns.map((c) => `<td>${esc(row[c])}</td>`).join("")}</tr>`).join("");
  document.getElementById("content").innerHTML = `<table><thead><tr>${columns.map((c) => `<th>${esc(c)}</th>`).join("")}</tr></thead><tbody>${rows || "<tr><td colspan='99'>No data</td></tr>"}</tbody></table>`;
}

async function loadView(name) {
  try {
    if name === "overview") return renderOverview();
    if (name === "runs") { setView("runs", "Запуски"); return renderTable("/api/admin/ops/runs?limit=50", ["workflow_id", "tenant_id", "status", "workflow_type"]); }
    if (name === "queues") { setView("queues", "Очереди"); const q = await api("/api/admin/ops/queues"); document.getElementById("content").innerHTML = `<table><thead><tr><th>lane</th><th>depth</th><th>active</th></tr></thead><tbody>${q.map((r) => `<tr><td>${esc(r.lane)}</td><td>${esc(r.depth)}</td><td>${esc(r.active)}</td></tr>`).join("")}</tbody></table>`; return; }
    if (name === "routing") { setView("routing", "Маршрутизация"); const r = await api("/api/admin/ops/routing"); document.getElementById("content").innerHTML = `<p>Статус: ${esc(r.status)}</p><p>Активная политика: ${esc(r.active_policy_version || "нет")}</p>`; return; }
    if (name === "tools") { setView("tools", "Инструменты"); return renderTable("/api/admin/ops/tools", ["tool_id", "version", "status"]); }
    if (name === "costs") { setView("costs", "Стоимость"); const c = await api("/api/admin/ops/costs"); document.getElementById("content").innerHTML = `<p>Окно: ${esc(c.window)}</p><p>Итого: ${esc(c.total_cost)}</p>`; return; }
    if (name === "dlq") { setView("dlq", "DLQ"); return renderTable("/api/admin/ops/dlq?limit=50", ["task_id", "tenant_id", "status", "error_code"]); }
    if (name === "approvals") { setView("approvals", "Подтверждения"); return renderTable("/api/admin/ops/approvals", ["approval_id", "workflow_id", "tenant_id", "status"]); }
    if (name === "tenants") { setView("tenants", "Арендаторы"); return renderTable("/api/admin/ops/tenants", ["tenant_id", "active_runs", "failed_jobs"]); }
    if (name === "audit") { setView("audit", "Аудит"); return renderTable("/api/admin/ops/audit?limit=50", ["timestamp", "actor_ref", "action", "target_id", "result"]); }
    if (name === "alerts") {
      setView("alerts", "Alerts");
      const alerts = await api("/api/admin/ops/alerts");
      document.getElementById("content").innerHTML = alerts.length
        ? `<ul>${alerts.map((a) => `<li class="sev-${esc(a.severity)}">${esc(a.source)}: ${esc(a.message)} (${esc(a.status)})</li>`).join("")}</ul>`
        : "<p>No active alerts</p>";
    }
  } catch (e) {
    setStatus(e.status === 403 ? "Unauthorized — admin capability required" : e.message);
    document.getElementById("content").innerHTML = `<p>${esc(e.message)}</p>`;
  }
}

document.querySelectorAll(".nav a").forEach((a) => {
  a.addEventListener("click", (e) => {
    e.preventDefault();
    loadView(a.dataset.view);
  });
});

loadView("overview");
