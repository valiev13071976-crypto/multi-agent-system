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
  };

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
    fileInput: document.getElementById("file-input"),
    attachmentChips: document.getElementById("attachment-chips"),
  };

  function show(el) { if (el) el.classList.remove("hidden"); }
  function hide(el) { if (el) el.classList.add("hidden"); }

  function setStatus(text, kind) {
    els.status.textContent = text || "";
    els.status.className = "status-pill" + (kind ? ` ${kind}` : "");
  }

  function canShowDiagnostics() {
    return presentation.shouldShowDiagnostics(state.roleContext);
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
    return presentation.toUserFacingSummary(result.summary || "", { conversational, business: !conversational });
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

  function renderConversations() {
    els.convList.innerHTML = "";
    if (!state.conversations.length) {
      show(els.convEmpty);
      return;
    }
    hide(els.convEmpty);
    state.conversations.forEach((c) => {
      const li = document.createElement("li");
      const btn = ui.renderConversationButton(c, state.conversationId);
      btn.onclick = () => openConversation(c.conversation_id, c.title);
      li.appendChild(btn);
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

  function renderTimeline(opts) {
    const force = Boolean(opts && opts.forceScroll);
    const stick = force || isNearBottom(scrollContainer());
    els.timeline.innerHTML = "";
    state.messages.forEach((m) => {
      els.timeline.appendChild(ui.renderMessage(m.role, m.content, null));
    });
    if (stick) scrollTimelineToBottom(true);
  }

  function autoGrowComposer() {
    const ta = els.composer;
    if (!ta) return;
    ta.style.height = "auto";
    const max = Math.min(window.innerHeight * 0.4, 200);
    ta.style.height = `${Math.min(ta.scrollHeight, max)}px`;
  }

  function setSidebarOpen(open) {
    if (!els.sidebar) return;
    els.sidebar.classList.toggle("open", open);
    if (els.sidebarToggle) els.sidebarToggle.setAttribute("aria-expanded", open ? "true" : "false");
    if (els.sidebarBackdrop) {
      if (open) els.sidebarBackdrop.removeAttribute("hidden");
      else els.sidebarBackdrop.setAttribute("hidden", "");
    }
    document.body.classList.toggle("sidebar-open", open);
  }

  function closeSidebar() {
    setSidebarOpen(false);
  }

  function toggleSidebar() {
    setSidebarOpen(!els.sidebar.classList.contains("open"));
  }

  function renderAttachmentChips() {
    els.attachmentChips.innerHTML = "";
    state.attachments.forEach((a, idx) => {
      const chip = ui.el("span", "chip", `${a.filename} (${a.size_bytes || 0} B)`);
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
            state.messages.push({
              role: "assistant",
              content: bubble,
              created_at: new Date().toISOString(),
              request_id: requestId,
            });
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

    state.submitting = true;
    els.sendBtn.disabled = true;
    els.composerError.textContent = "";
    const idempotencyKey = api.uuid();

    state.messages.push({ role: "user", content: text, created_at: new Date().toISOString() });
    renderTimeline({ forceScroll: true });
    els.composer.value = "";
    autoGrowComposer();
    setStatus(window.PandaCopy.USER_THINKING, "running");

    try {
      const payload = {
        message: text || "Проанализируй прикреплённые файлы",
        conversation_id: state.conversationId,
        idempotency_key: idempotencyKey,
        artifact_refs: state.attachments.map((a) => a.artifact_ref),
      };
      const req = await api.submitRequest(payload);
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
      els.composerError.textContent = api.mapError(e);
      setStatus("", "");
    } finally {
      state.submitting = false;
      els.sendBtn.disabled = false;
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

  function bindEvents() {
    els.authSubmit.onclick = onAuth;
    els.logout.onclick = logout;
    els.newChat.onclick = () => newChat().catch((e) => { els.composerError.textContent = api.mapError(e); });
    els.sendBtn.onclick = sendMessage;
    els.approveBtn.onclick = onApprove;
    els.rejectBtn.onclick = onReject;
    els.cancelBtn.onclick = onCancel;
    els.sidebarToggle.onclick = toggleSidebar;
    if (els.sidebarClose) els.sidebarClose.onclick = closeSidebar;
    if (els.sidebarBackdrop) els.sidebarBackdrop.onclick = closeSidebar;
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && els.sidebar.classList.contains("open")) {
        closeSidebar();
      }
    });
    els.fileInput.onchange = (e) => onFiles(Array.from(e.target.files || []));
    els.composer.addEventListener("input", autoGrowComposer);
    els.composer.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        sendMessage();
      }
    });
    window.addEventListener("resize", () => {
      if (window.innerWidth >= 1024) closeSidebar();
      autoGrowComposer();
    });
  }

  function initBrand() {
    brand.applyDocumentBrand();
    brand.renderLogo(els.authBrand, { size: 48 });
    brand.renderLogo(els.sidebarBrand, { size: 32 });
  }

  async function boot() {
    initBrand();
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
