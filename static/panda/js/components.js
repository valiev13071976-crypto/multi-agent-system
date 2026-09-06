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

  const FILE_ICONS = {
    pdf: "📕",
    spreadsheet: "📊",
    document: "📄",
    text: "📝",
  };

  function fileIconFor(kind) {
    return FILE_ICONS[String(kind || "")] || "📎";
  }

  function formatFileSize(bytes) {
    const n = Number(bytes) || 0;
    if (n <= 0) return "";
    if (n < 1024) return `${n} B`;
    if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} КБ`;
    return `${(n / 1024 / 1024).toFixed(1)} МБ`;
  }

  /** Block 3.5.8/3.5.9/3.5.10: one attachment -- image thumbnail (lightbox-
   * ready, matching the markdown-image wrapper in sanitize.js) or a generic
   * file card (pdf/spreadsheet/document/text/other) with a download link. */
  function renderAttachmentCard(meta) {
    const a = meta || {};
    const kind = String(a.kind || "");
    const isImage = kind === "image" || String(a.mime_type || "").startsWith("image/");
    const viewUrl = String(a.view_url || "").trim();
    const downloadUrl = String(a.download_url || "").trim();
    if (isImage && (viewUrl || downloadUrl)) {
      const wrap = el("span", "msg-image-wrap");
      const img = document.createElement("img");
      img.className = "msg-image";
      img.alt = a.filename || "изображение";
      img.src = viewUrl || downloadUrl;
      img.dataset.fullUrl = viewUrl || downloadUrl;
      img.dataset.downloadUrl = downloadUrl || viewUrl;
      wrap.appendChild(img);
      return wrap;
    }
    const card = document.createElement("a");
    card.className = "file-card";
    card.href = downloadUrl || viewUrl || "#";
    card.target = "_blank";
    card.rel = "noopener noreferrer";
    if (downloadUrl) card.setAttribute("download", a.filename || "");
    card.appendChild(el("span", "file-card-icon", fileIconFor(kind)));
    const info = el("span", "file-card-info");
    info.appendChild(el("span", "file-card-name", a.filename || "Файл"));
    const size = formatFileSize(a.size_bytes);
    if (size) info.appendChild(el("span", "file-card-size", size));
    card.appendChild(info);
    return card;
  }

  function renderMessage(role, content, meta, attachments) {
    const wrap = el("article", `msg ${role}`);
    if (meta) wrap.appendChild(el("div", "meta", meta));
    const body = el("div", "body");
    renderRichText(body, content);
    wrap.appendChild(body);
    if (attachments && attachments.length) {
      const list = el("div", "msg-attachments");
      attachments.forEach((a) => {
        if (a) list.appendChild(renderAttachmentCard(a));
      });
      wrap.appendChild(list);
    }
    return wrap;
  }

  /** Block 3.5 final closure: transient in-conversation live-generation
   * placeholder (ChatGPT-like) -- rendered directly in the timeline flow
   * right where the assistant's result will appear, so the user can never
   * mistake a slow request for a frozen app. Not part of the persisted
   * message model: app.js appends/removes this node directly and it is
   * never written into state.messages, so a reload/history restore can
   * never resurrect a stale "generating" bubble. */
  function renderPendingAssistant() {
    const wrap = el("article", "msg assistant pending");
    const body = el("div", "body");
    const indicator = document.createElement("span");
    indicator.className = "pending-indicator";
    indicator.setAttribute("role", "status");
    indicator.setAttribute("aria-live", "polite");
    indicator.setAttribute("aria-label", "Panda генерирует ответ");
    const dot = document.createElement("span");
    dot.className = "pending-dot";
    dot.setAttribute("aria-hidden", "true");
    indicator.appendChild(dot);
    body.appendChild(indicator);
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
    const payload = result || {};
    const canonical = presentation.selectCanonicalFinalAnswer
      ? presentation.selectCanonicalFinalAnswer(payload)
      : "";
    const mode = payload.structured_result?.mode || opts.mode;
    const conversational = mode === "CONVERSATIONAL" || opts.conversational;
    const raw = canonical || (conversational ? "" : (payload.summary || ""));
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
      if (!a || typeof a !== "object") return;
      const item = el("div", "artifact-item");
      const kind = a.artifact_type || a.type || a.ref || "файл";
      if (String(kind) === "image" || String(a.mime_type || "").startsWith("image/")) {
        const url = String(a.view_url || a.url || "").trim();
        if (url.startsWith("/") || url.startsWith("https://")) {
          const img = document.createElement("img");
          img.alt = "изображение";
          img.src = url;
          item.appendChild(img);
        } else {
          const left = el("div");
          left.appendChild(el("div", "", "изображение"));
          item.appendChild(left);
        }
      } else {
        const left = el("div");
        left.appendChild(el("div", "", String(kind)));
        left.appendChild(el("div", "muted", a.filename || a.ref || ""));
        item.appendChild(left);
      }
      item.appendChild(el("div", "muted", a.created_at || ""));
      root.appendChild(item);
    });
    if (!artifacts?.length) root.appendChild(el("p", "muted", "Файлов нет."));
    return root;
  }

  function renderConversationItem(conv, activeId) {
    const row = el("div", "conv-row");
    row.dataset.id = conv.conversation_id;
    const openBtn = el("button", "conv-open");
    openBtn.type = "button";
    if (conv.conversation_id === activeId) openBtn.classList.add("active");
    setText(openBtn, conv.title || "Разговор");
    openBtn.dataset.id = conv.conversation_id;
    const menuBtn = el("button", "conv-menu-btn");
    menuBtn.type = "button";
    menuBtn.setAttribute("aria-label", "Действия с чатом");
    menuBtn.setAttribute("aria-haspopup", "menu");
    menuBtn.setAttribute("aria-expanded", "false");
    menuBtn.dataset.id = conv.conversation_id;
    setText(menuBtn, "⋯");
    const menu = el("div", "conv-menu hidden");
    menu.setAttribute("role", "menu");
    menu.hidden = true;
    const renameBtn = el("button", "conv-menu-item");
    renameBtn.type = "button";
    renameBtn.setAttribute("role", "menuitem");
    renameBtn.dataset.action = "rename";
    setText(renameBtn, "Переименовать");
    const deleteBtn = el("button", "conv-menu-item danger-item");
    deleteBtn.type = "button";
    deleteBtn.setAttribute("role", "menuitem");
    deleteBtn.dataset.action = "delete";
    setText(deleteBtn, "Удалить");
    menu.appendChild(renameBtn);
    menu.appendChild(deleteBtn);
    row.appendChild(openBtn);
    row.appendChild(menuBtn);
    row.appendChild(menu);
    return row;
  }

  function renderConversationButton(conv, activeId) {
    const btn = el("button", "conv-open");
    btn.type = "button";
    btn.className = conv.conversation_id === activeId ? "conv-open active" : "conv-open";
    setText(btn, conv.title || "Разговор");
    btn.dataset.id = conv.conversation_id;
    return btn;
  }

  global.PandaComponents = {
    renderMessage,
    renderPendingAssistant,
    renderProgressItem,
    renderPlan,
    renderPreview,
    renderResult,
    renderArtifacts,
    renderAttachmentCard,
    renderConversationButton,
    renderConversationItem,
    el,
  };
})(window);
