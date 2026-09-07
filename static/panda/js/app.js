/** Panda Web Interface application orchestration. */
(function () {
  const api = window.PandaApi;
  const ui = window.PandaComponents;
  const brand = window.PandaBrand;
  const presentation = window.PandaPresentation;
  const roleApi = window.PandaRoleContext;

  const state = {
    conversationId: null,
    conversations: [],
    messages: [],
    attachments: [],
    activeRequestId: null,
    activeRequest: null,
    eventCursor: null,
    seenEvents: new Set(),
    pollTimer: null,
    submitting: false,
    approving: false,
    roleContext: { loaded: false, isManagement: false, isOwner: false, role: null },
    lastResultMode: null,
    openMenuId: null,
    pendingDeleteId: null,
    // Production acceptance defect closure: canonical artifact_id of the
    // generated image currently targeted by the direct "Редактировать"
    // dialog -- set only from a specific image's own data-artifact-id
    // (see sanitize.js), never inferred/guessed.
    pendingEditArtifactId: null,
    // Block 3.5 final closure: DOM node of the transient in-conversation
    // live-generation indicator (see components.renderPendingAssistant),
    // or null when none is showing. Never persisted to state.messages.
    pendingIndicatorEl: null,
    // Block 4 — realtime voice session UI state. `controller` is the one
    // PandaRealtime.RealtimeVoiceController instance for the lifetime of the
    // page; `streamingEl`/`streamingText` track the transient in-progress
    // assistant bubble for the CURRENT voice turn only (never persisted
    // until assistant.text.completed lands it in state.messages, same
    // pattern as showPendingIndicator for REST turns).
    realtime: {
      controller: null,
      turnId: null,
      streamingEl: null,
      streamingText: "",
      preferredVoiceId: "",
    },
  };

  // Block 3.5.6/3.5.16: per-session cache so a reloaded conversation only
  // fetches each attachment's metadata (filename/kind/urls) once, even if
  // it appears on multiple messages.
  const artifactMetaCache = new Map();

  const els = {
    authGate: document.getElementById("auth-gate"),
    app: document.getElementById("app"),
    authBrand: document.getElementById("auth-brand"),
    sidebarBrand: document.getElementById("sidebar-brand"),
    apiKey: document.getElementById("api-key-input"),
    authSubmit: document.getElementById("auth-submit"),
    authError: document.getElementById("auth-error"),
    convList: document.getElementById("conversation-list"),
    convLoading: document.getElementById("conv-loading"),
    convEmpty: document.getElementById("conv-empty"),
    newChat: document.getElementById("new-chat-btn"),
    logout: document.getElementById("logout-btn"),
    account: document.getElementById("account-label"),
    ownerNav: document.getElementById("owner-nav-link"),
    sidebar: document.getElementById("sidebar"),
    sidebarToggle: document.getElementById("sidebar-toggle"),
    sidebarClose: document.getElementById("sidebar-close"),
    sidebarBackdrop: document.getElementById("sidebar-backdrop"),
    title: document.getElementById("chat-title"),
    status: document.getElementById("request-status"),
    chatScroll: document.querySelector(".chat-scroll"),
    timeline: document.getElementById("timeline"),
    diagnosticsPanel: document.getElementById("diagnostics-panel"),
    progressPanel: document.getElementById("progress-panel"),
    progressList: document.getElementById("progress-list"),
    planPanel: document.getElementById("plan-panel"),
    planContent: document.getElementById("plan-content"),
    approvalPanel: document.getElementById("approval-panel"),
    previewContent: document.getElementById("preview-content"),
    approveBtn: document.getElementById("approve-btn"),
    rejectBtn: document.getElementById("reject-btn"),
    cancelBtn: document.getElementById("cancel-btn"),
    resultPanel: document.getElementById("result-panel"),
    resultContent: document.getElementById("result-content"),
    artifactList: document.getElementById("artifact-list"),
    composer: document.getElementById("composer-input"),
    sendBtn: document.getElementById("send-btn"),
    composerError: document.getElementById("composer-error"),
    composerShell: document.getElementById("composer-shell"),
    fileInput: document.getElementById("file-input"),
    attachmentChips: document.getElementById("attachment-chips"),
    welcome: document.getElementById("welcome-state"),
    welcomeBrand: document.getElementById("welcome-brand"),
    suggestedPrompts: document.getElementById("suggested-prompts"),
    confirmDialog: document.getElementById("chat-confirm-dialog"),
    confirmText: document.getElementById("chat-confirm-text"),
    confirmOk: document.getElementById("chat-confirm-ok"),
    confirmCancel: document.getElementById("chat-confirm-cancel"),
    lightbox: document.getElementById("image-lightbox"),
    lightboxImg: document.getElementById("image-lightbox-img"),
    lightboxDownload: document.getElementById("image-lightbox-download"),
    lightboxClose: document.getElementById("image-lightbox-close"),
    imageEditDialog: document.getElementById("image-edit-dialog"),
    imageEditInput: document.getElementById("image-edit-input"),
    imageEditError: document.getElementById("image-edit-error"),
    imageEditOk: document.getElementById("image-edit-ok"),
    imageEditCancel: document.getElementById("image-edit-cancel"),
    micBtn: document.getElementById("mic-btn"),
    voiceLiveCaption: document.getElementById("voice-live-caption"),
    personalizationBtn: document.getElementById("personalization-btn"),
    realtimeAudio: document.getElementById("realtime-audio-player"),
  };

  function show(el) { if (el) el.classList.remove("hidden"); }
  function hide(el) { if (el) el.classList.add("hidden"); }

  function setStatus(text, kind) {
    els.status.textContent = text || "";
    els.status.className = "status-pill" + (kind ? ` ${kind}` : "");
  }

  function canShowDiagnostics() {
    // Ordinary Panda chat never renders workflow internals.
    // Governed diagnostics remain on /admin for management roles.
    return false;
  }

  function updateRoleUi() {
    if (state.roleContext.isManagement) {
      show(els.ownerNav);
      els.account.textContent = `Роль: ${state.roleContext.role || "—"}`;
    } else {
      hide(els.ownerNav);
      els.account.textContent = state.roleContext.loaded ? "Пользователь" : "Аккаунт";
    }
    if (!canShowDiagnostics()) hide(els.diagnosticsPanel);
  }

  function storageKey(prefix) {
    return `${prefix}:${state.conversationId || "none"}`;
  }

  function saveActiveRequest() {
    if (state.conversationId && state.activeRequestId) {
      sessionStorage.setItem(storageKey("active_request"), state.activeRequestId);
    }
  }

  function loadActiveRequest() {
    if (!state.conversationId) return null;
    return sessionStorage.getItem(storageKey("active_request"));
  }

  function assistantBubbleText(result) {
    const mode = result.structured_result?.mode || state.lastResultMode;
    const conversational = mode === "CONVERSATIONAL";
    const canonical = presentation.selectCanonicalFinalAnswer
      ? presentation.selectCanonicalFinalAnswer(result)
      : String(result.final_answer || "").trim();
    if (conversational) {
      if (!canonical || presentation.isInternalMetadata(canonical)) return "";
      return canonical;
    }
    return presentation.toUserFacingSummary(canonical || result.summary || "", {
      conversational: false,
      business: true,
    });
  }

  async function verifyAuth() {
    await api.listConversations();
    return true;
  }

  function csrfHeader() {
    const match = document.cookie.match(/(?:^|; )panda_csrf=([^;]*)/);
    return match ? decodeURIComponent(match[1]) : "";
  }

  async function loadRoleContext() {
    state.roleContext = await roleApi.resolveRoleContext(api.getApiKey());
    updateRoleUi();
  }

  async function enterApp() {
    hide(els.authGate);
    show(els.app);
    await loadRoleContext();
    await refreshConversations();
    if (!state.conversationId) await newChat();
    const saved = loadActiveRequest();
    if (saved) await trackRequest(saved, { resume: true });
    if (window.PandaPersonalizationApi) {
      try {
        const prefs = await window.PandaPersonalizationApi.getPreferences();
        state.realtime.preferredVoiceId = prefs.voice_id || "";
      } catch (_) {
        /* personalization is additive -- never blocks chat entry */
      }
    }
  }

  async function onAuth() {
    els.authError.textContent = "";
    api.setApiKey(els.apiKey.value.trim());
    try {
      await verifyAuth();
      await enterApp();
    } catch (e) {
      api.clearApiKey();
      els.authError.textContent = api.mapError(e);
    }
  }

  function logout() {
    stopPolling();
    if (state.realtime.controller) state.realtime.controller.close();
    const csrf = csrfHeader();
    fetch("/api/accounts/logout", {
      method: "POST",
      credentials: "same-origin",
      headers: { Accept: "application/json", "X-CSRF-Token": csrf },
    }).catch(function () {});
    api.clearApiKey();
    state.conversationId = null;
    state.activeRequestId = null;
    state.roleContext = { loaded: false, isManagement: false, isOwner: false, role: null };
    hide(els.app);
    show(els.authGate);
  }

  async function refreshConversations() {
    show(els.convLoading);
    hide(els.convEmpty);
    try {
      state.conversations = await api.listConversations();
    } catch (_) {
      state.conversations = [];
    }
    hide(els.convLoading);
    renderConversations();
  }

  function closeChatMenu() {
    state.openMenuId = null;
    document.querySelectorAll(".conv-menu").forEach((menu) => {
      menu.hidden = true;
      menu.classList.add("hidden");
    });
    document.querySelectorAll(".conv-menu-btn").forEach((btn) => {
      btn.setAttribute("aria-expanded", "false");
    });
    document.querySelectorAll(".conv-row.menu-open").forEach((row) => {
      row.classList.remove("menu-open");
    });
  }

  function openChatMenu(conversationId, menuBtn) {
    const already = state.openMenuId === conversationId;
    closeChatMenu();
    if (already) return;
    const row = menuBtn.closest(".conv-row");
    const menu = row ? row.querySelector(".conv-menu") : null;
    if (!menu) return;
    state.openMenuId = conversationId;
    menu.hidden = false;
    menu.classList.remove("hidden");
    menuBtn.setAttribute("aria-expanded", "true");
    if (row) row.classList.add("menu-open");
  }

  function hideConfirm() {
    state.pendingDeleteId = null;
    if (!els.confirmDialog) return;
    hide(els.confirmDialog);
    els.confirmDialog.setAttribute("hidden", "");
  }

  function showDeleteConfirm(conversationId) {
    closeChatMenu();
    state.pendingDeleteId = conversationId;
    if (els.confirmText) els.confirmText.textContent = "Удалить этот чат?";
    if (els.confirmDialog) {
      show(els.confirmDialog);
      els.confirmDialog.removeAttribute("hidden");
    }
  }

  async function applyRename(conversationId, title) {
    const renamed = await api.renameConversation(conversationId, title);
    const idx = state.conversations.findIndex((c) => c.conversation_id === conversationId);
    if (idx >= 0) state.conversations[idx] = renamed;
    else state.conversations.unshift(renamed);
    if (state.conversationId === conversationId) {
      els.title.textContent = renamed.title || "Новый чат";
    }
    renderConversations();
  }

  async function startRename(conversationId) {
    closeChatMenu();
    const conv = state.conversations.find((c) => c.conversation_id === conversationId);
    const current = conv ? conv.title : "";
    const row = Array.from(els.convList.querySelectorAll(".conv-row")).find(
      (node) => node.dataset.id === conversationId
    );
    if (!row) return;
    const openBtn = row.querySelector(".conv-open");
    if (!openBtn) return;
    const input = document.createElement("input");
    input.type = "text";
    input.className = "conv-rename-input";
    input.value = current || "";
    input.maxLength = 80;
    input.setAttribute("aria-label", "Название чата");
    openBtn.replaceWith(input);
    input.focus();
    input.select();
    let done = false;
    async function commit() {
      if (done) return;
      done = true;
      const next = input.value.trim();
      renderConversations();
      if (!next) return;
      if (next === current) return;
      try {
        await applyRename(conversationId, next);
      } catch (e) {
        els.composerError.textContent = api.mapError(e);
      }
    }
    function cancel() {
      if (done) return;
      done = true;
      renderConversations();
    }
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        commit();
      }
      if (e.key === "Escape") {
        e.preventDefault();
        cancel();
      }
    });
    input.addEventListener("blur", () => { commit(); });
  }

  async function performDelete(conversationId) {
    await api.deleteConversation(conversationId);
    state.conversations = state.conversations.filter((c) => c.conversation_id !== conversationId);
    if (state.conversationId === conversationId) {
      stopPolling();
      state.activeRequestId = null;
      state.activeRequest = null;
      const next = state.conversations[0];
      if (next) {
        await openConversation(next.conversation_id, next.title);
      } else {
        await newChat();
      }
    } else {
      renderConversations();
    }
  }

  function renderConversations() {
    closeChatMenu();
    els.convList.innerHTML = "";
    if (!state.conversations.length) {
      show(els.convEmpty);
      return;
    }
    hide(els.convEmpty);
    state.conversations.forEach((c) => {
      const li = document.createElement("li");
      li.className = "conv-item";
      const row = ui.renderConversationItem(c, state.conversationId);
      const openBtn = row.querySelector(".conv-open");
      const menuBtn = row.querySelector(".conv-menu-btn");
      const renameBtn = row.querySelector('[data-action="rename"]');
      const deleteBtn = row.querySelector('[data-action="delete"]');
      if (openBtn) {
        openBtn.onclick = () => openConversation(c.conversation_id, c.title);
      }
      if (menuBtn) {
        menuBtn.onclick = (e) => {
          e.preventDefault();
          e.stopPropagation();
          openChatMenu(c.conversation_id, menuBtn);
        };
      }
      if (renameBtn) {
        renameBtn.onclick = (e) => {
          e.stopPropagation();
          startRename(c.conversation_id);
        };
      }
      if (deleteBtn) {
        deleteBtn.onclick = (e) => {
          e.stopPropagation();
          showDeleteConfirm(c.conversation_id);
        };
      }
      li.appendChild(row);
      els.convList.appendChild(li);
    });
  }

  function scrollContainer() {
    return els.chatScroll || els.timeline;
  }

  function isNearBottom(el, threshold) {
    if (!el) return true;
    return el.scrollHeight - el.scrollTop - el.clientHeight <= (threshold || 80);
  }

  function scrollTimelineToBottom(force) {
    const el = scrollContainer();
    if (!el) return;
    if (force || isNearBottom(el)) {
      el.scrollTop = el.scrollHeight;
    }
  }

  /** Block 3.5 final closure: show/hide the transient in-conversation live-
   * generation indicator. Tied directly to the real request lifecycle --
   * callers show it right before starting the actual blocking
   * submit/edit call and hide it as soon as that same call settles
   * (success OR failure), never on a fixed timer/fake duration. Purely
   * additive DOM: never touches state.messages, so it can never be
   * persisted, resurrected on reload, or left orphaned once the request
   * that created it has settled -- renderTimeline()'s innerHTML reset also
   * clears it as a safety net. */
  function showPendingIndicator() {
    hidePendingIndicator();
    const node = ui.renderPendingAssistant();
    els.timeline.appendChild(node);
    state.pendingIndicatorEl = node;
    scrollTimelineToBottom(true);
  }

  function hidePendingIndicator() {
    if (state.pendingIndicatorEl && state.pendingIndicatorEl.parentNode) {
      state.pendingIndicatorEl.parentNode.removeChild(state.pendingIndicatorEl);
    }
    state.pendingIndicatorEl = null;
  }

  function renderTimeline(opts) {
    const force = Boolean(opts && opts.forceScroll);
    const stick = force || isNearBottom(scrollContainer());
    state.pendingIndicatorEl = null;
    els.timeline.innerHTML = "";
    state.messages.forEach((m) => {
      let role = m.role;
      let content = m.content;
      if (role === "assistant" && presentation.isInternalMetadata(content)) {
        role = "system";
        content = (window.PandaCopy && window.PandaCopy.MISSING_FINAL_ANSWER) ||
          "Panda не смогла сформировать ответ. Попробуйте ещё раз.";
      }
      els.timeline.appendChild(ui.renderMessage(role, content, null, m.attachments));
    });
    syncWelcome();
    if (stick) scrollTimelineToBottom(true);
  }

  /** Block 3.5.6/3.5.9/3.5.10/3.5.16: resolve each persisted message's
   * canonical/legacy artifact_refs into renderable attachment metadata
   * (filename/kind/view+download URLs), caching per artifact ref so a
   * reload never re-fetches the same file's metadata twice. Best-effort --
   * a single unreadable/foreign ref must never block the rest of the
   * conversation from rendering. */
  async function hydrateAttachments(messages) {
    const jobs = [];
    (messages || []).forEach((m) => {
      if (m.attachments || !m.artifact_refs || !m.artifact_refs.length) return;
      m.attachments = [];
      m.artifact_refs.forEach((ref) => {
        if (!ref) return;
        const job = (async () => {
          let meta = artifactMetaCache.get(ref);
          if (!meta) {
            try {
              meta = await api.getArtifactMetadata(ref);
            } catch (_) {
              meta = null;
            }
            artifactMetaCache.set(ref, meta);
          }
          if (meta) m.attachments.push(meta);
        })();
        jobs.push(job);
      });
    });
    if (jobs.length) await Promise.all(jobs);
  }

  function openLightbox(fullUrl, downloadUrl) {
    if (!els.lightbox || !fullUrl) return;
    els.lightboxImg.src = fullUrl;
    if (els.lightboxDownload) {
      els.lightboxDownload.href = downloadUrl || fullUrl;
    }
    show(els.lightbox);
    els.lightbox.removeAttribute("hidden");
  }

  function closeLightbox() {
    if (!els.lightbox) return;
    hide(els.lightbox);
    els.lightbox.setAttribute("hidden", "");
    if (els.lightboxImg) els.lightboxImg.src = "";
  }

  /** Production acceptance defect closure: direct "Редактировать" action.
   * Opens a small instruction dialog for the EXACT artifact_id the user
   * clicked (never the latest/any other image) -- see sanitize.js, which
   * only stamps data-artifact-id on images served from the canonical
   * /artifacts/{id}/view route. */
  function openImageEditDialog(artifactId) {
    if (!artifactId || !els.imageEditDialog) return;
    state.pendingEditArtifactId = artifactId;
    if (els.imageEditInput) els.imageEditInput.value = "";
    if (els.imageEditError) els.imageEditError.textContent = "";
    show(els.imageEditDialog);
    els.imageEditDialog.removeAttribute("hidden");
    if (els.imageEditInput) els.imageEditInput.focus();
  }

  function closeImageEditDialog() {
    state.pendingEditArtifactId = null;
    if (!els.imageEditDialog) return;
    hide(els.imageEditDialog);
    els.imageEditDialog.setAttribute("hidden", "");
  }

  async function submitImageEdit() {
    const artifactId = state.pendingEditArtifactId;
    const instruction = (els.imageEditInput ? els.imageEditInput.value : "").trim();
    if (!artifactId || !state.conversationId) return;
    if (!instruction) {
      if (els.imageEditError) els.imageEditError.textContent = "Опишите, что нужно изменить.";
      return;
    }
    const conversationId = state.conversationId;
    if (els.imageEditOk) els.imageEditOk.disabled = true;
    try {
      const res = await api.editImage(artifactId, conversationId, instruction);
      closeImageEditDialog();
      // The edit call is synchronous (no /requests polling round trip) --
      // append both turns directly, matching sendMessage's optimistic
      // rendering pattern.
      state.messages.push({
        role: "user",
        content: instruction,
        created_at: new Date().toISOString(),
        request_id: res.request_id,
      });
      state.messages.push({
        role: "assistant",
        content: res.text || "",
        created_at: new Date().toISOString(),
        request_id: res.request_id,
      });
      renderTimeline({ forceScroll: true });
      await refreshConversations();
    } catch (e) {
      if (els.imageEditError) els.imageEditError.textContent = api.mapError(e);
    } finally {
      if (els.imageEditOk) els.imageEditOk.disabled = false;
    }
  }

  function autoGrowComposer() {
    const ta = els.composer;
    if (!ta) return;
    ta.style.height = "auto";
    const max = Math.min(window.innerHeight * 0.4, 240);
    ta.style.height = `${Math.min(ta.scrollHeight, max)}px`;
  }

  function isDesktopLayout() {
    return window.innerWidth >= 1024;
  }

  function updateSidebarToggleLabel() {
    if (!els.sidebarToggle) return;
    if (isDesktopLayout()) {
      const collapsed = els.app && els.app.classList.contains("sidebar-collapsed");
      els.sidebarToggle.setAttribute("aria-expanded", collapsed ? "false" : "true");
      els.sidebarToggle.setAttribute("aria-label", collapsed ? "Показать меню" : "Скрыть меню");
      return;
    }
    const open = els.sidebar && els.sidebar.classList.contains("open");
    els.sidebarToggle.setAttribute("aria-expanded", open ? "true" : "false");
    els.sidebarToggle.setAttribute("aria-label", open ? "Закрыть меню" : "Открыть меню");
  }

  function composerHasContent() {
    const text = els.composer ? els.composer.value.trim() : "";
    return Boolean(text || state.attachments.length);
  }

  function updateSendEnabled() {
    if (!els.sendBtn) return;
    els.sendBtn.disabled = state.submitting || !composerHasContent();
  }

  function setComposerBusy(busy) {
    state.submitting = busy;
    if (els.composerShell) els.composerShell.classList.toggle("is-sending", busy);
    if (els.composer) els.composer.readOnly = busy;
    updateSendEnabled();
  }

  function syncWelcome() {
    if (!els.welcome) return;
    const hasMessages = Boolean(state.messages.length);
    if (hasMessages) hide(els.welcome);
    else show(els.welcome);
    if (els.chatScroll) els.chatScroll.classList.toggle("has-messages", hasMessages);
    if (els.app) els.app.classList.toggle("has-messages", hasMessages);
  }

  function setSidebarOpen(open) {
    if (!els.sidebar) return;
    els.sidebar.classList.toggle("open", open);
    if (els.sidebarBackdrop) {
      if (open) els.sidebarBackdrop.removeAttribute("hidden");
      else els.sidebarBackdrop.setAttribute("hidden", "");
    }
    document.body.classList.toggle("sidebar-open", open);
    updateSidebarToggleLabel();
  }

  function closeSidebar() {
    setSidebarOpen(false);
  }

  function toggleSidebar() {
    if (isDesktopLayout()) {
      if (els.app) els.app.classList.toggle("sidebar-collapsed");
      updateSidebarToggleLabel();
      return;
    }
    setSidebarOpen(!els.sidebar.classList.contains("open"));
  }

  function renderAttachmentChips() {
    els.attachmentChips.innerHTML = "";
    state.attachments.forEach((a, idx) => {
      const chip = ui.el("span", "chip", `📎 ${a.filename} (${a.size_bytes || 0} B)`);
      const rm = ui.el("button", "", "×");
      rm.type = "button";
      rm.setAttribute("aria-label", "Удалить вложение");
      rm.onclick = () => {
        state.attachments.splice(idx, 1);
        renderAttachmentChips();
      };
      chip.appendChild(rm);
      els.attachmentChips.appendChild(chip);
    });
    updateSendEnabled();
  }

  function resetPanels() {
    hide(els.approvalPanel);
    hide(els.resultPanel);
    hide(els.progressPanel);
    hide(els.planPanel);
    if (!canShowDiagnostics()) hide(els.diagnosticsPanel);
  }

  async function openConversation(id, title) {
    stopPolling();
    state.conversationId = id;
    state.activeRequestId = null;
    state.activeRequest = null;
    state.seenEvents.clear();
    state.eventCursor = null;
    resetPanels();
    els.title.textContent = title || "Новый чат";
    renderConversations();
    try {
      state.messages = await api.listMessages(id);
    } catch (_) {
      state.messages = [];
    }
    await hydrateAttachments(state.messages);
    renderTimeline({ forceScroll: true });
    closeSidebar();
    const saved = loadActiveRequest();
    const latestRequestId = findLatestRequestId(state.messages);
    const resumeId = saved || latestRequestId;
    if (resumeId) await trackRequest(resumeId, { resume: true });
  }

  function findLatestRequestId(messages) {
    for (let i = messages.length - 1; i >= 0; i -= 1) {
      const rid = messages[i].request_id;
      if (rid) return rid;
    }
    return null;
  }

  async function newChat() {
    stopPolling();
    const conv = await api.createConversation("Новый чат");
    state.conversations.unshift(conv);
    state.messages = [];
    state.attachments = [];
    renderAttachmentChips();
    await openConversation(conv.conversation_id, conv.title);
  }

  function stopPolling() {
    if (state.pollTimer) {
      clearTimeout(state.pollTimer);
      state.pollTimer = null;
    }
  }

  function schedulePoll(fn, ms) {
    stopPolling();
    state.pollTimer = setTimeout(fn, ms);
  }

  async function trackRequest(requestId, opts = {}) {
    state.activeRequestId = requestId;
    saveActiveRequest();
    state.activeRequest = await api.getRequest(requestId);
    await refreshRequestUi(opts);
    if (!api.isTerminal(state.activeRequest.status)) {
      schedulePoll(() => pollRequest(requestId), 1500);
    }
  }

  async function pollRequest(requestId) {
    if (state.activeRequestId !== requestId) return;
    try {
      await refreshRequestUi({ poll: true });
      if (!api.isTerminal(state.activeRequest.status)) {
        schedulePoll(() => pollRequest(requestId), state.activeRequest.status === "WAITING_FOR_APPROVAL" ? 4000 : 1500);
      }
    } catch (e) {
      setStatus(api.mapError(e), "error");
      schedulePoll(() => pollRequest(requestId), 3000);
    }
  }

  async function refreshRequestUi(opts = {}) {
    const requestId = state.activeRequestId;
    if (!requestId) return;
    const [summary, status] = await Promise.all([
      api.getRequest(requestId),
      api.getStatus(requestId),
    ]);
    state.activeRequest = summary;

    const label = api.statusLabel(summary.status);
    let kind = "";
    if (summary.status === "WAITING_FOR_APPROVAL") kind = "approval";
    else if (["RUNNING", "RESUMING", "PLANNING", "QUEUED", "VALIDATING"].includes(summary.status)) kind = "running";
    else if (summary.status === "COMPLETED") kind = "done";
    else if (["FAILED", "BLOCKED", "REJECTED", "CANCELLED"].includes(summary.status)) kind = "error";
    setStatus(label, kind);

    if (canShowDiagnostics()) {
      show(els.diagnosticsPanel);
      await refreshEvents(requestId);
      if (status.plan_summary) {
        show(els.planPanel);
        els.planContent.innerHTML = "";
        els.planContent.appendChild(ui.renderPlan(status.plan_summary));
      }
    } else {
      hide(els.diagnosticsPanel);
      hide(els.progressPanel);
      hide(els.planPanel);
      hide(els.resultPanel);
    }

    if (summary.status === "WAITING_FOR_APPROVAL") {
      show(els.approvalPanel);
      const preview = await api.getPreview(requestId);
      els.previewContent.innerHTML = "";
      els.previewContent.appendChild(ui.renderPreview(preview));
      state.activeRequest.approval_id = summary.approval_id;
      state.activeRequest.plan_fingerprint = status.plan_summary?.fingerprint || summary.plan_fingerprint;
    } else {
      hide(els.approvalPanel);
    }

    if (api.isTerminal(summary.status)) {
      stopPolling();
      sessionStorage.removeItem(storageKey("active_request"));
      if (summary.status === "COMPLETED") {
        const result = await api.getResult(requestId);
        state.lastResultMode = result.structured_result?.mode || null;
        const bubble = assistantBubbleText(result);
        const conversational = state.lastResultMode === "CONVERSATIONAL";

        if (canShowDiagnostics() && !conversational) {
          const artifacts = await api.listArtifacts(requestId);
          show(els.resultPanel);
          els.resultContent.innerHTML = "";
          els.resultContent.appendChild(ui.renderResult(result, { showFindings: true }));
          els.artifactList.innerHTML = "";
          els.artifactList.appendChild(ui.renderArtifacts(artifacts));
        } else {
          hide(els.resultPanel);
        }

        if (!opts.poll) {
          const already = state.messages.some(
            (m) => m.role === "assistant" && m.request_id === requestId
          );
          if (!already) {
            let content = bubble;
            const artifacts = result.artifacts || [];
            const imageLines = artifacts
              .filter((a) => a && (a.artifact_type === "image" || a.type === "image"))
              .map((a) => String(a.view_url || a.url || "").trim())
              .filter((u) => u.startsWith("/") || u.startsWith("https://"))
              // The assistant text (bubble) may already embed this exact artifact link
              // (see format_tool_user_text); never render the same image twice.
              .filter((u) => !bubble.includes(u))
              .map((u) => `![изображение](${u})`);
            if (imageLines.length) {
              content = [bubble, ...imageLines].filter(Boolean).join("\n");
            }
            if (!content || presentation.isInternalMetadata(content)) {
              state.messages.push({
                role: "system",
                content: (window.PandaCopy && window.PandaCopy.MISSING_FINAL_ANSWER) ||
                  "Panda не смогла сформировать ответ. Попробуйте ещё раз.",
                created_at: new Date().toISOString(),
                request_id: requestId,
              });
            } else {
              state.messages.push({
                role: "assistant",
                content,
                created_at: new Date().toISOString(),
                request_id: requestId,
              });
            }
            renderTimeline();
          }
        }
      } else if (summary.status === "FAILED" && !opts.poll) {
        state.messages.push({
          role: "system",
          content: api.mapError({ code: summary.error_code, message: summary.error_message, status: 500 }),
          created_at: new Date().toISOString(),
        });
        renderTimeline();
      }
    }
  }

  async function refreshEvents(requestId) {
    if (!canShowDiagnostics()) return;
    const events = await api.listEvents(requestId, state.eventCursor);
    if (!events.length) return;
    show(els.progressPanel);
    events.forEach((ev) => {
      if (state.seenEvents.has(ev.event_id)) return;
      state.seenEvents.add(ev.event_id);
      els.progressList.appendChild(ui.renderProgressItem(ev));
      state.eventCursor = ev.timestamp;
    });
  }

  async function sendMessage() {
    if (state.submitting) return;
    const text = els.composer.value.trim();
    if (!text && !state.attachments.length) return;
    if (!state.conversationId) await newChat();

    setComposerBusy(true);
    els.composerError.textContent = "";
    const idempotencyKey = api.uuid();

    // Block 3.5.16: attach the already-known upload metadata directly so
    // this message's file cards/image thumbnail render immediately -- no
    // need to wait for a reload + GET /artifacts/{id} round-trip.
    const sentAttachments = state.attachments.map((a) => ({
      artifact_id: a.artifact_id,
      filename: a.filename,
      mime_type: a.mime_type,
      size_bytes: a.size_bytes,
      kind: a.kind,
      view_url: a.view_url,
      download_url: a.download_url,
    }));
    state.messages.push({
      role: "user",
      content: text,
      created_at: new Date().toISOString(),
      attachments: sentAttachments,
    });
    renderTimeline({ forceScroll: true });
    els.composer.value = "";
    autoGrowComposer();
    updateSendEnabled();
    setStatus(window.PandaCopy.USER_THINKING, "running");
    // Block 3.5 final closure: the real "generation" latency for this
    // architecture is the blocking POST /requests call below (the backend
    // resolves the whole turn, including any image generation, synchronously
    // within that single HTTP round trip; the /requests polling further
    // down is a no-op for this fast path and only matters for
    // approval-gated turns). The top-right "Думаю..." pill alone is not
    // sufficient in-conversation feedback, so show the live indicator right
    // where the assistant's reply will land, and remove it the instant this
    // exact call settles either way -- never on a fixed timer.
    showPendingIndicator();

    try {
      const payload = {
        message: text || "Проанализируй прикреплённые файлы",
        conversation_id: state.conversationId,
        idempotency_key: idempotencyKey,
        artifact_refs: state.attachments.map((a) => a.artifact_ref),
      };
      const req = await api.submitRequest(payload);
      hidePendingIndicator();
      state.attachments = [];
      renderAttachmentChips();
      if (canShowDiagnostics()) {
        els.progressList.innerHTML = "";
        state.seenEvents.clear();
        state.eventCursor = null;
      }
      resetPanels();
      await trackRequest(req.request_id);
      await refreshConversations();
    } catch (e) {
      hidePendingIndicator();
      els.composerError.textContent = api.mapError(e);
      setStatus("", "");
    } finally {
      setComposerBusy(false);
    }
  }

  async function onApprove() {
    if (state.approving || !state.activeRequestId) return;
    state.approving = true;
    els.approveBtn.disabled = true;
    try {
      await api.approve(state.activeRequestId, {
        approval_id: state.activeRequest?.approval_id,
        plan_fingerprint: state.activeRequest?.plan_fingerprint,
      });
      await trackRequest(state.activeRequestId);
    } catch (e) {
      els.composerError.textContent = api.mapError(e);
    } finally {
      state.approving = false;
      els.approveBtn.disabled = false;
    }
  }

  async function onReject() {
    if (!state.activeRequestId) return;
    await api.reject(state.activeRequestId);
    await trackRequest(state.activeRequestId);
  }

  async function onCancel() {
    if (!state.activeRequestId) return;
    await api.cancel(state.activeRequestId);
    await trackRequest(state.activeRequestId);
  }

  async function onFiles(files) {
    for (const file of files) {
      setStatus(`Загрузка ${file.name}…`, "running");
      try {
        const ref = await api.uploadFile(file);
        state.attachments.push(ref);
      } catch (e) {
        els.composerError.textContent = api.mapError(e);
      }
    }
    renderAttachmentChips();
    setStatus("");
  }

  // --- Block 4: realtime voice ---------------------------------------------

  function micIconFor(rtState) {
    const RT = window.PandaRealtime;
    if (!RT) return "🎤";
    if (rtState === RT.STATE_LISTENING) return "⏺";
    if (rtState === RT.STATE_THINKING || rtState === RT.STATE_SPEAKING) return "⏹";
    if (rtState === RT.STATE_CONNECTING) return "…";
    return "🎤";
  }

  function updateMicUi(rtState) {
    const RT = window.PandaRealtime;
    if (!els.micBtn || !RT) return;
    els.micBtn.classList.remove("is-listening", "is-thinking", "is-speaking", "is-connecting", "is-error");
    const iconEl = els.micBtn.querySelector(".mic-icon");
    if (iconEl) iconEl.textContent = micIconFor(rtState);
    els.micBtn.setAttribute("aria-pressed", rtState === RT.STATE_IDLE ? "false" : "true");
    if (rtState === RT.STATE_LISTENING) {
      els.micBtn.classList.add("is-listening");
      els.micBtn.setAttribute("aria-label", "Остановить запись и отправить");
    } else if (rtState === RT.STATE_THINKING) {
      els.micBtn.classList.add("is-thinking");
      els.micBtn.setAttribute("aria-label", "Прервать Panda и говорить");
    } else if (rtState === RT.STATE_SPEAKING) {
      els.micBtn.classList.add("is-speaking");
      els.micBtn.setAttribute("aria-label", "Прервать Panda и говорить");
    } else if (rtState === RT.STATE_CONNECTING) {
      els.micBtn.classList.add("is-connecting");
      els.micBtn.setAttribute("aria-label", "Подключение…");
    } else if (rtState === RT.STATE_ERROR) {
      els.micBtn.classList.add("is-error");
      els.micBtn.setAttribute("aria-label", "Голосовой ввод — ошибка, повторите");
    } else {
      els.micBtn.setAttribute("aria-label", "Голосовой ввод");
    }
    if (rtState === RT.STATE_IDLE) {
      hide(els.voiceLiveCaption);
      els.voiceLiveCaption.textContent = "";
    }
  }

  function showVoiceCaption(text) {
    if (!els.voiceLiveCaption) return;
    if (!text) {
      hide(els.voiceLiveCaption);
      return;
    }
    els.voiceLiveCaption.textContent = `🎤 ${text}`;
    show(els.voiceLiveCaption);
  }

  function finalizeStreamingBubble(turnId, text) {
    const rt = state.realtime;
    if (rt.streamingEl && rt.streamingEl.parentNode) {
      rt.streamingEl.parentNode.removeChild(rt.streamingEl);
    }
    rt.streamingEl = null;
    rt.streamingText = "";
    rt.turnId = null;
    const trimmed = String(text || "").trim();
    if (!trimmed) return;
    state.messages.push({
      role: "assistant",
      content: trimmed,
      created_at: new Date().toISOString(),
      modality: "voice",
    });
    renderTimeline({ forceScroll: true });
  }

  function ensureRealtimeController() {
    if (state.realtime.controller) return state.realtime.controller;
    const RT = window.PandaRealtime;
    const controller = new RT.RealtimeVoiceController({
      onStateChange: (rtState) => updateMicUi(rtState),
      onSessionStarted: ({ conversationId }) => {
        if (conversationId && conversationId !== state.conversationId) {
          state.conversationId = conversationId;
          refreshConversations().catch(() => {});
        }
      },
      onPartialTranscript: (text) => showVoiceCaption(text),
      onUserTurnCommitted: ({ text }) => {
        showVoiceCaption("");
        if (text) {
          state.messages.push({
            role: "user",
            content: text,
            created_at: new Date().toISOString(),
            modality: "voice",
          });
          renderTimeline({ forceScroll: true });
        }
        setStatus(window.PandaCopy.USER_THINKING || "Думаю…", "running");
      },
      onStatus: (status) => setStatus(status, "running"),
      onAssistantTextDelta: ({ turnId, delta }) => {
        const rt = state.realtime;
        if (rt.turnId !== turnId) {
          rt.turnId = turnId;
          rt.streamingText = "";
          rt.streamingEl = ui.renderMessage("assistant", "", null, null);
          rt.streamingEl.classList.add("streaming");
          els.timeline.appendChild(rt.streamingEl);
          syncWelcome();
        }
        rt.streamingText += delta;
        const body = rt.streamingEl.querySelector(".body");
        if (body) window.PandaSanitize.renderRichText(body, rt.streamingText);
        scrollTimelineToBottom();
      },
      onAssistantTextCompleted: ({ turnId, text }) => {
        finalizeStreamingBubble(turnId, text);
        setStatus("", "");
      },
      onAssistantAudio: ({ url }) => {
        if (!els.realtimeAudio) return;
        els.realtimeAudio.src = url;
        els.realtimeAudio.play().catch(() => {});
      },
      onInterruption: () => {
        const rt = state.realtime;
        finalizeStreamingBubble(rt.turnId, rt.streamingText);
        if (els.realtimeAudio) {
          els.realtimeAudio.pause();
          els.realtimeAudio.removeAttribute("src");
        }
        setStatus("", "");
      },
      onError: ({ code, message }) => {
        els.composerError.textContent =
          code === "mic_permission_denied"
            ? "Доступ к микрофону запрещён. Разрешите доступ в настройках браузера."
            : message || "Ошибка голосового режима";
        setStatus("", "error");
      },
      onSessionClosed: () => {
        showVoiceCaption("");
      },
    });
    state.realtime.controller = controller;
    return controller;
  }

  async function onMicClick() {
    const RT = window.PandaRealtime;
    if (!RT || !RT.isSupported()) {
      els.composerError.textContent = "Голосовой режим не поддерживается в этом браузере.";
      return;
    }
    els.composerError.textContent = "";
    const controller = ensureRealtimeController();
    if (controller.state === RT.STATE_IDLE || controller.state === RT.STATE_ERROR) {
      if (!state.conversationId) await newChat();
      await controller.start({ conversationId: state.conversationId, voiceId: state.realtime.preferredVoiceId });
    } else if (controller.state === RT.STATE_LISTENING) {
      controller.commit();
    } else if (controller.state === RT.STATE_THINKING || controller.state === RT.STATE_SPEAKING) {
      controller.interruptAndListen();
    }
  }

  function openPersonalizationSettings() {
    if (!state.personalizationDialog) return;
    state.personalizationDialog.open((prefs) => {
      state.realtime.preferredVoiceId = prefs.voice_id || "";
      if (state.realtime.controller && state.realtime.controller.state !== window.PandaRealtime.STATE_IDLE) {
        state.realtime.controller.selectVoice(prefs.voice_id);
      }
    });
  }

  function bindEvents() {
    els.authSubmit.onclick = onAuth;
    els.logout.onclick = logout;
    els.newChat.onclick = () => newChat().catch((e) => { els.composerError.textContent = api.mapError(e); });
    els.sendBtn.onclick = sendMessage;
    if (els.micBtn) els.micBtn.onclick = () => { onMicClick().catch((e) => { els.composerError.textContent = api.mapError(e); }); };
    if (els.personalizationBtn) els.personalizationBtn.onclick = openPersonalizationSettings;
    els.approveBtn.onclick = onApprove;
    els.rejectBtn.onclick = onReject;
    els.cancelBtn.onclick = onCancel;
    els.sidebarToggle.onclick = toggleSidebar;
    if (els.sidebarClose) els.sidebarClose.onclick = closeSidebar;
    if (els.sidebarBackdrop) els.sidebarBackdrop.onclick = closeSidebar;
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") {
        if (els.imageEditDialog && !els.imageEditDialog.hasAttribute("hidden")) {
          closeImageEditDialog();
          return;
        }
        if (els.lightbox && !els.lightbox.hasAttribute("hidden")) {
          closeLightbox();
          return;
        }
        if (state.pendingDeleteId) {
          hideConfirm();
          return;
        }
        if (state.openMenuId) {
          closeChatMenu();
          return;
        }
        if (els.sidebar.classList.contains("open")) {
          closeSidebar();
        }
      }
    });
    document.addEventListener("pointerdown", (e) => {
      if (state.openMenuId && els.convList && !els.convList.contains(e.target)) {
        closeChatMenu();
      }
    });
    if (els.confirmOk) {
      els.confirmOk.onclick = async () => {
        const id = state.pendingDeleteId;
        hideConfirm();
        if (!id) return;
        try {
          await performDelete(id);
        } catch (err) {
          els.composerError.textContent = api.mapError(err);
          renderConversations();
        }
      };
    }
    if (els.confirmCancel) els.confirmCancel.onclick = hideConfirm;
    if (els.confirmDialog) {
      els.confirmDialog.addEventListener("click", (e) => {
        if (e.target === els.confirmDialog) hideConfirm();
      });
    }
    els.fileInput.onchange = (e) => onFiles(Array.from(e.target.files || []));
    // Block 3.5.8: click-to-enlarge + download for any rendered image
    // (assistant-generated inline markdown image or an uploaded image
    // attachment card) -- delegated so it works for messages rendered
    // both now and after future re-renders.
    els.timeline.addEventListener("click", (e) => {
      // Final ChatGPT 1:1 closure: Edit/Download overlay controls are
      // siblings of the <img> (absolutely positioned inside the same
      // .msg-image-wrap, on top of the image), never descendants of it, so
      // this check always intercepts their clicks before the lightbox-open
      // fallback below ever sees them.
      const editBtn = e.target.closest(".msg-image-edit-btn");
      if (editBtn) {
        openImageEditDialog(editBtn.dataset.artifactId);
        return;
      }
      const img = e.target.closest(".msg-image");
      if (!img) return;
      openLightbox(img.dataset.fullUrl || img.src, img.dataset.downloadUrl);
    });
    if (els.lightboxClose) els.lightboxClose.onclick = closeLightbox;
    if (els.lightbox) {
      els.lightbox.addEventListener("click", (e) => {
        if (e.target === els.lightbox) closeLightbox();
      });
    }
    if (els.imageEditOk) els.imageEditOk.onclick = submitImageEdit;
    if (els.imageEditCancel) els.imageEditCancel.onclick = closeImageEditDialog;
    if (els.imageEditDialog) {
      els.imageEditDialog.addEventListener("click", (e) => {
        if (e.target === els.imageEditDialog) closeImageEditDialog();
      });
    }
    if (els.imageEditInput) {
      els.imageEditInput.addEventListener("keydown", (e) => {
        if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
          e.preventDefault();
          submitImageEdit();
        }
      });
    }
    if (els.suggestedPrompts) {
      els.suggestedPrompts.addEventListener("click", (e) => {
        const chip = e.target.closest("[data-prompt]");
        if (!chip) return;
        els.composer.value = chip.getAttribute("data-prompt") || "";
        autoGrowComposer();
        updateSendEnabled();
        els.composer.focus();
      });
    }
    els.composer.addEventListener("input", () => {
      autoGrowComposer();
      updateSendEnabled();
    });
    els.composer.addEventListener("keydown", (e) => {
      if (e.isComposing || e.keyCode === 229) return;
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        sendMessage();
      }
    });
    window.addEventListener("resize", () => {
      if (isDesktopLayout()) closeSidebar();
      autoGrowComposer();
      updateSidebarToggleLabel();
    });
    updateSendEnabled();
    updateSidebarToggleLabel();
  }

  function initBrand() {
    brand.applyDocumentBrand();
    brand.renderLogo(els.authBrand, { size: 48 });
    brand.renderLogo(els.sidebarBrand, { size: 32 });
    if (els.welcomeBrand) brand.renderLogo(els.welcomeBrand, { size: 56, title: "Panda AI" });
  }

  async function boot() {
    initBrand();
    if (window.PandaPersonalizationDialog) {
      state.personalizationDialog = window.PandaPersonalizationDialog.create();
    }
    if (els.micBtn && (!window.PandaRealtime || !window.PandaRealtime.isSupported())) {
      // Block 4.6/4.36: never show a control that can only ever fail --
      // e.g. non-HTTPS/non-localhost origins where getUserMedia is not
      // exposed at all, or browsers without MediaRecorder.
      hide(els.micBtn);
    }
    bindEvents();
    if (api.hasApiKey()) {
      try {
        await verifyAuth();
        await enterApp();
        return;
      } catch (_) {
        api.clearApiKey();
      }
    }
    if (api.hasHumanSession) {
      try {
        if (await api.hasHumanSession()) {
          await verifyAuth();
          await enterApp();
          return;
        }
      } catch (_) {
        /* invalid or unusable session — show API-key gate */
      }
    }
    show(els.authGate);
    hide(els.app);
  }

  boot();
})();
