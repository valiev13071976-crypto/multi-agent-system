/** UI components — render-only helpers (no business logic). */
(function (global) {
  const { escapeHtml, setText, renderMultiline } = global.PandaSanitize;

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) setText(node, text);
    return node;
  }

  function renderMessage(role, content, meta) {
    const wrap = el("article", `msg ${role}`);
    if (meta) wrap.appendChild(el("div", "meta", meta));
    const body = el("div", "body");
    renderMultiline(body, content);
    wrap.appendChild(body);
    return wrap;
  }

  function renderProgressItem(event) {
    const li = el("li");
    setText(li, `${event.event_type}: ${event.message || ""}`.trim());
    return li;
  }

  function renderPlan(planSummary) {
    const root = document.createDocumentFragment();
    if (!planSummary) {
      root.appendChild(el("p", "muted", "No plan available."));
      return root;
    }
    root.appendChild(el("p", "", `Recipe: ${planSummary.recipe || "—"}`));
    const steps = planSummary.steps || [];
    const ul = el("ul");
    steps.forEach((s) => ul.appendChild(el("li", "", `${s.name || s.id} (${s.class || ""})`)));
    root.appendChild(ul);
    return root;
  }

  function renderPreview(preview) {
    const root = document.createDocumentFragment();
    if (!preview) {
      root.appendChild(el("p", "muted", "Preview unavailable."));
      return root;
    }
    const changes = preview.changes || [];
    if (!changes.length) {
      root.appendChild(el("p", "", "Proposed external action pending approval."));
    } else {
      const ul = el("ul");
      changes.slice(0, 50).forEach((c) => {
        ul.appendChild(el("li", "", JSON.stringify(c)));
      });
      root.appendChild(ul);
    }
    const warnings = preview.warnings || [];
    warnings.forEach((w) => root.appendChild(el("p", "error-text", w)));
    return root;
  }

  function renderResult(result) {
    const root = document.createDocumentFragment();
    root.appendChild(el("p", "", result.summary || "Done."));
    const findings = result.structured_result?.findings || [];
    if (findings.length) {
      const table = el("table", "data-table");
      const thead = el("thead");
      const hr = el("tr");
      ["Summary", "Kind", "SKU"].forEach((h) => hr.appendChild(el("th", "", h)));
      thead.appendChild(hr);
      table.appendChild(thead);
      const tbody = el("tbody");
      findings.slice(0, 100).forEach((f) => {
        const tr = el("tr");
        tr.appendChild(el("td", "", f.summary || ""));
        tr.appendChild(el("td", "", f.kind || ""));
        tr.appendChild(el("td", "", f.sku_id || ""));
        tbody.appendChild(tr);
      });
      table.appendChild(tbody);
      root.appendChild(table);
    }
    return root;
  }

  function renderArtifacts(artifacts) {
    const root = document.createDocumentFragment();
    (artifacts || []).forEach((a) => {
      const item = el("div", "artifact-item");
      const left = el("div");
      left.appendChild(el("div", "", a.artifact_type || a.ref || "artifact"));
      left.appendChild(el("div", "muted", a.filename || a.ref || ""));
      item.appendChild(left);
      const meta = el("div", "muted", a.created_at || "");
      item.appendChild(meta);
      root.appendChild(item);
    });
    if (!artifacts?.length) root.appendChild(el("p", "muted", "No artifacts."));
    return root;
  }

  function renderConversationButton(conv, activeId) {
    const btn = el("button");
    btn.type = "button";
    btn.className = conv.conversation_id === activeId ? "active" : "";
    setText(btn, conv.title || "Conversation");
    btn.dataset.id = conv.conversation_id;
    return btn;
  }

  global.PandaComponents = {
    renderMessage,
    renderProgressItem,
    renderPlan,
    renderPreview,
    renderResult,
    renderArtifacts,
    renderConversationButton,
    el,
  };
})(window);
