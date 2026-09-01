/** Panda Web Interface application orchestration. */
(function () {
  const api = window.PandaApi;
  const ui = window.PandaComponents;

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
  };

  const els = {
    authGate: document.getElementById("auth-gate"),
    app: document.getElementById("app"),
    apiKey: document.getElementById("api-key-input"),
    authSubmit: document.getElementById("auth-submit"),
    authError: document.getElementById("auth-error"),
    convList: document.getElementById("conversation-list"),
    convLoading: document.getElementById("conv-loading"),
    convEmpty: document.getElementById("conv-empty"),
    newChat: document.getElementById("new-chat-btn"),
    logout: document.getElementById("logout-btn"),
    account: document.getElementById("account-label"),
    sidebar: document.getElementById("sidebar"),
    sidebarToggle: document.getElementById("sidebar-toggle"),
    title: document.getElementById("chat-title"),
    status: document.getElementById("request-status"),
    timeline: document.getElementById("timeline"),
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

  function show(el) { el.classList.remove("hidden"); }
  function hide(el) { el.classList.add("hidden"); }

  function setStatus(text, kind) {
    els.status.textContent = text || "";
    els.status.className = "status-pill" + (kind ? ` ${kind}` : "");
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

  async function verifyAuth() {
    await api.listConversations();
    return true;
  }

  async function enterApp() {
    hide(els.authGate);
    show(els.app);
    els.account.textContent = "Authenticated";
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
    api.clearApiKey();
    state.conversationId = null;
    state.activeRequestId = null;
    hide(els.app);
    show(els.authGate);
  }

  async function refreshConversations() {
    show(els.convLoading);
    hide(els.convEmpty);
    try {
      state.conversations = await api.listConversations();
    } catch (e) {
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

  function renderTimeline() {
    els.timeline.innerHTML = "";
    state.messages.forEach((m) => {
      els.timeline.appendChild(ui.renderMessage(m.role, m.content, m.created_at));
    });
    els.timeline.scrollTop = els.timeline.scrollHeight;
  }

  function renderAttachmentChips() {
    els.attachmentChips.innerHTML = "";
    state.attachments.forEach((a, idx) => {
      const chip = ui.el("span", "chip", `${a.filename} (${a.size_bytes || 0} B)`);
      const rm = ui.el("button", "", "×");
      rm.type = "button";
      rm.setAttribute("aria-label", "Remove attachment");
      rm.onclick = () => {
        state.attachments.splice(idx, 1);
        renderAttachmentChips();
      };
      chip.appendChild(rm);
      els.attachmentChips.appendChild(chip);
    });
  }

  async function openConversation(id, title) {
    stopPolling();
    state.conversationId = id;
    state.activeRequestId = null;
    state.activeRequest = null;
    state.seenEvents.clear();
    state.eventCursor = null;
    hide(els.approvalPanel);
    hide(els.resultPanel);
    hide(els.progressPanel);
    hide(els.planPanel);
    els.title.textContent = title || "Conversation";
    renderConversations();
    try {
      state.messages = await api.listMessages(id);
    } catch (e) {
      state.messages = [];
    }
    renderTimeline();
    els.sidebar.classList.remove("open");
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
    const conv = await api.createConversation("New chat");
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
    else if (summary.status === "RUNNING" || summary.status === "RESUMING" || summary.status === "PLANNING") kind = "running";
    else if (summary.status === "COMPLETED") kind = "done";
    else if (["FAILED", "BLOCKED", "REJECTED", "CANCELLED"].includes(summary.status)) kind = "error";
    setStatus(label, kind);

    await refreshEvents(requestId);

    if (status.plan_summary) {
      show(els.planPanel);
      els.planContent.innerHTML = "";
      els.planContent.appendChild(ui.renderPlan(status.plan_summary));
    }

    if (summary.status === "WAITING_FOR_APPROVAL") {
      show(els.approvalPanel);
      hide(els.resultPanel);
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
        const artifacts = await api.listArtifacts(requestId);
        show(els.resultPanel);
        els.resultContent.innerHTML = "";
        els.resultContent.appendChild(ui.renderResult(result));
        els.artifactList.innerHTML = "";
        els.artifactList.appendChild(ui.renderArtifacts(artifacts));
        if (!opts.poll) {
          state.messages.push({ role: "assistant", content: result.summary || "Completed.", created_at: new Date().toISOString() });
          renderTimeline();
        }
      }
    }
  }

  async function refreshEvents(requestId) {
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
    renderTimeline();
    els.composer.value = "";

    try {
      const payload = {
        message: text || "Analyze attached files",
        conversation_id: state.conversationId,
        idempotency_key: idempotencyKey,
        artifact_refs: state.attachments.map((a) => a.artifact_ref),
      };
      const req = await api.submitRequest(payload);
      state.attachments = [];
      renderAttachmentChips();
      els.progressList.innerHTML = "";
      state.seenEvents.clear();
      state.eventCursor = null;
      show(els.progressPanel);
      hide(els.resultPanel);
      await trackRequest(req.request_id);
      await refreshConversations();
    } catch (e) {
      els.composerError.textContent = api.mapError(e);
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
      const body = {
        approval_id: state.activeRequest?.approval_id,
        plan_fingerprint: state.activeRequest?.plan_fingerprint,
      };
      await api.approve(state.activeRequestId, body);
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
      setStatus(`Uploading ${file.name}…`, "running");
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
    els.sidebarToggle.onclick = () => els.sidebar.classList.toggle("open");
    els.fileInput.onchange = (e) => onFiles(Array.from(e.target.files || []));
    els.composer.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        sendMessage();
      }
    });
  }

  async function boot() {
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
    show(els.authGate);
    hide(els.app);
  }

  boot();
})();
