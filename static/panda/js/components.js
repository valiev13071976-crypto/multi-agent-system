/** UI components — render-only helpers (no business logic). */
(function (global) {
  const { setText, renderRichText } = global.PandaSanitize;
  const presentation = global.PandaPresentation;

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
    renderRichText(body, content);
    wrap.appendChild(body);
    return wrap;
  }

  function renderProgressItem(event) {
    const li = el("li");
    const label = event.message || event.event_type || "событие";
    setText(li, label);
    return li;
  }

  function renderPlan(planSummary) {
    const root = document.createDocumentFragment();
    if (!planSummary) {
      root.appendChild(el("p", "muted", "План недоступен."));
      return root;
    }
    root.appendChild(el("p", "", `Шагов: ${(planSummary.steps || []).length}`));
    const ul = el("ul");
    (planSummary.steps || []).forEach((s) => {
      ul.appendChild(el("li", "", s.name || s.id || "шаг"));
    });
    root.appendChild(ul);
    return root;
  }

  function renderPreview(preview) {
    const root = document.createDocumentFragment();
    if (!preview) {
      root.appendChild(el("p", "muted", "Предпросмотр недоступен."));
      return root;
    }
    const changes = preview.changes || [];
    if (!changes.length) {
      root.appendChild(el("p", "", "Подготовлено внешнее действие — требуется подтверждение."));
    } else {
      const ul = el("ul", "preview-list");
      changes.slice(0, 20).forEach((c) => {
        const label = c.summary || c.description || c.action || c.type || "изменение";
        ul.appendChild(el("li", "", String(label)));
      });
      root.appendChild(ul);
    }
    (preview.warnings || []).forEach((w) => root.appendChild(el("p", "error-text", String(w))));
    return root;
  }

  function renderResult(result, options) {
    const opts = options || {};
    const root = document.createDocumentFragment();
    const raw = result.summary || "";
    const mode = result.structured_result?.mode || opts.mode;
    const conversational = mode === "CONVERSATIONAL" || opts.conversational;
    const text = presentation.toUserFacingSummary(raw, {
      conversational,
      business: !conversational,
    });
    root.appendChild(el("p", "", text));
    if (opts.showFindings && !conversational) {
      const findings = result.structured_result?.findings || [];
      if (findings.length) {
        const table = el("table", "data-table");
        const thead = el("thead");
        const hr = el("tr");
        ["Описание", "Тип", "SKU"].forEach((h) => hr.appendChild(el("th", "", h)));
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
        const wrap = el("div", "table-scroll");
        wrap.appendChild(table);
        root.appendChild(wrap);
      }
    }
    return root;
  }

  function renderArtifacts(artifacts) {
    const root = document.createDocumentFragment();
    (artifacts || []).forEach((a) => {
      const item = el("div", "artifact-item");
      const left = el("div");
      left.appendChild(el("div", "", a.artifact_type || a.ref || "файл"));
      left.appendChild(el("div", "muted", a.filename || a.ref || ""));
      item.appendChild(left);
      item.appendChild(el("div", "muted", a.created_at || ""));
      root.appendChild(item);
    });
    if (!artifacts?.length) root.appendChild(el("p", "muted", "Файлов нет."));
    return root;
  }

  function renderConversationButton(conv, activeId) {
    const btn = el("button");
    btn.type = "button";
    btn.className = conv.conversation_id === activeId ? "active" : "";
    setText(btn, conv.title || "Разговор");
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
