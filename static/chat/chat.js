/* Panda Chat UI — Block 14 client (projection only; server is authoritative). */

const state = {
  conversationId: null,
  conversations: [],
  messages: [],
  pendingAttachments: [],
  activeRunId: null,
  voiceState: "IDLE",
  composing: false,
  pollTimer: null,
};

const els = {
  list: document.getElementById("conversation-list"),
  messages: document.getElementById("messages"),
  composer: document.getElementById("composer"),
  send: document.getElementById("send-btn"),
  stop: document.getElementById("stop-btn"),
  newChat: document.getElementById("new-chat"),
  title: document.getElementById("chat-title"),
  status: document.getElementById("run-status"),
  fileInput: document.getElementById("file-input"),
  attachPreview: document.getElementById("attachments-preview"),
  mic: document.getElementById("mic-btn"),
  sidebar: document.getElementById("sidebar"),
  sidebarToggle: document.getElementById("sidebar-toggle"),
  taskList: document.getElementById("task-list"),
  ttsPlayer: document.getElementById("tts-player"),
};

async function api(path, options = {}) {
  const res = await fetch(path, {
    ...options,
    headers: {
      ...(options.body instanceof FormData ? {} : { "Content-Type": "application/json" }),
      ...(options.headers || {}),
    },
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error(data.message || data.detail?.message || res.statusText);
    err.code = data.code || data.detail?.code;
    err.status = res.status;
    throw err;
  }
  return data;
}

function uuid() {
  return crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random();
}

function setStatus(text) {
  els.status.textContent = text || "";
}

function renderConversations() {
  els.list.innerHTML = "";
  state.conversations.forEach((c) => {
    const li = document.createElement("li");
    const btn = document.createElement("button");
    btn.textContent = c.title;
    btn.className = c.conversation_id === state.conversationId ? "active" : "";
    btn.onclick = () => openConversation(c.conversation_id, c.title);
    li.appendChild(btn);
    els.list.appendChild(li);
  });
}

function renderMessages() {
  els.messages.innerHTML = "";
  state.messages.forEach((m) => {
    const div = document.createElement("article");
    div.className = `message ${m.role}`;
    div.innerHTML = m.content_html || escapeHtml(m.content);
    if (m.role === "assistant") {
      const actions = document.createElement("div");
      actions.className = "message-actions";
      const listen = document.createElement("button");
      listen.type = "button";
      listen.textContent = "Listen";
      listen.setAttribute("aria-label", "Listen to message");
      listen.onclick = () => playTts(m.message_id);
      actions.appendChild(listen);
      div.appendChild(actions);
    }
    els.messages.appendChild(div);
  });
  els.messages.scrollTop = els.messages.scrollHeight;
}

function escapeHtml(text) {
  return String(text)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function renderAttachmentPreview() {
  els.attachPreview.innerHTML = "";
  state.pendingAttachments.forEach((a, idx) => {
    const chip = document.createElement("span");
    chip.className = "chip";
    chip.textContent = `${a.filename || a.attachment_id} (${a.status})`;
    const rm = document.createElement("button");
    rm.type = "button";
    rm.textContent = "×";
    rm.setAttribute("aria-label", "Remove attachment");
    rm.onclick = () => {
      state.pendingAttachments.splice(idx, 1);
      renderAttachmentPreview();
    };
    chip.appendChild(rm);
    els.attachPreview.appendChild(chip);
  });
}

async function loadConversations() {
  state.conversations = await api("/api/chat/conversations");
  renderConversations();
}

async function loadTasks() {
  const tasks = await api("/api/chat/tasks");
  els.taskList.innerHTML = "";
  tasks.forEach((t) => {
    const li = document.createElement("li");
    li.textContent = `${t.operation_label}: ${t.status}`;
    els.taskList.appendChild(li);
  });
}

async function openConversation(id, title) {
  state.conversationId = id;
  els.title.textContent = title || "Conversation";
  renderConversations();
  state.messages = await api(`/api/chat/conversations/${id}/messages`);
  renderMessages();
  els.sidebar.classList.remove("open");
}

async function createConversation() {
  const conv = await api("/api/chat/conversations", { method: "POST", body: JSON.stringify({}) });
  state.conversations.unshift(conv);
  await openConversation(conv.conversation_id, conv.title);
}

async function sendTurn() {
  if (!state.conversationId) await createConversation();
  const text = els.composer.value.trim();
  if (!text && !state.pendingAttachments.length) return;

  const idempotencyKey = uuid();
  els.send.disabled = true;
  setStatus("Sending…");

  try {
    const run = await api(`/api/chat/conversations/${state.conversationId}/turns`, {
      method: "POST",
      body: JSON.stringify({
        text,
        attachment_ids: state.pendingAttachments.map((a) => a.attachment_id),
        idempotency_key: idempotencyKey,
      }),
    });
    state.activeRunId = run.run_id;
    els.composer.value = "";
    state.pendingAttachments = [];
    renderAttachmentPreview();
    await refreshAfterRun(run);
  } catch (e) {
    setStatus(e.message || "Send failed");
  } finally {
    els.send.disabled = false;
  }
}

async function refreshAfterRun(run) {
  setStatus(run.status);
  state.messages = await api(`/api/chat/conversations/${state.conversationId}/messages`);
  renderMessages();
  await loadConversations();
  await loadTasks();
  state.activeRunId = null;
  els.stop.classList.add("hidden");
}

async function uploadFiles(files) {
  for (const file of files) {
    const fd = new FormData();
    fd.append("file", file);
    if (state.conversationId) fd.append("conversation_id", state.conversationId);
    setStatus(`Uploading ${file.name}…`);
    const ref = await api("/api/chat/attachments", { method: "POST", body: fd });
    state.pendingAttachments.push(ref);
  }
  renderAttachmentPreview();
  setStatus("");
}

let mediaRecorder = null;
let audioChunks = [];

async function startRecording() {
  if (!navigator.mediaDevices?.getUserMedia) {
    setStatus("Microphone not supported");
    state.voiceState = "ERROR";
    return;
  }
  state.voiceState = "REQUESTING_PERMISSION";
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    mediaRecorder = new MediaRecorder(stream);
    audioChunks = [];
    mediaRecorder.ondataavailable = (e) => audioChunks.push(e.data);
    mediaRecorder.onstop = onRecordingStop;
    mediaRecorder.start();
    state.voiceState = "RECORDING";
    els.mic.classList.add("mic-recording");
    els.mic.setAttribute("aria-pressed", "true");
    setStatus("Recording…");
  } catch {
    state.voiceState = "ERROR";
    setStatus("Microphone permission denied");
  }
}

function stopRecording() {
  if (mediaRecorder && mediaRecorder.state !== "inactive") {
    mediaRecorder.stop();
    mediaRecorder.stream.getTracks().forEach((t) => t.stop());
  }
  els.mic.classList.remove("mic-recording");
  els.mic.setAttribute("aria-pressed", "false");
}

async function onRecordingStop() {
  state.voiceState = "PROCESSING";
  setStatus("Transcribing…");
  const blob = new Blob(audioChunks, { type: "audio/webm" });
  if (!blob.size) {
    state.voiceState = "ERROR";
    setStatus("Empty recording");
    return;
  }
  const fd = new FormData();
  fd.append("file", blob, "recording.webm");
  try {
    const t = await api("/api/chat/voice/transcribe", { method: "POST", body: fd });
    els.composer.value = (els.composer.value ? els.composer.value + " " : "") + t.text;
    state.voiceState = "READY";
    setStatus("Transcript ready — edit and send");
  } catch (e) {
    state.voiceState = "ERROR";
    setStatus(e.message || "Transcription failed");
  }
}

async function playTts(messageId) {
  setStatus("Loading audio…");
  try {
    const meta = await api("/api/chat/voice/synthesize", {
      method: "POST",
      body: JSON.stringify({ message_id: messageId }),
    });
    els.ttsPlayer.src = `/api/chat/voice/audio/${meta.artifact_id}`;
    await els.ttsPlayer.play();
    setStatus("");
  } catch (e) {
    setStatus(e.message || "Playback failed");
  }
}

async function cancelRun() {
  if (!state.activeRunId) return;
  await api(`/api/chat/runs/${state.activeRunId}/cancel`, { method: "POST" });
  setStatus("Cancelled");
  els.stop.classList.add("hidden");
}

els.newChat.onclick = createConversation;
els.send.onclick = sendTurn;
els.stop.onclick = cancelRun;
els.sidebarToggle.onclick = () => els.sidebar.classList.toggle("open");

els.composer.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !state.composing) {
    e.preventDefault();
    sendTurn();
  }
});
els.composer.addEventListener("compositionstart", () => { state.composing = true; });
els.composer.addEventListener("compositionend", () => { state.composing = false; });

els.fileInput.onchange = (e) => {
  if (e.target.files?.length) uploadFiles(e.target.files);
  e.target.value = "";
};

els.mic.onclick = () => {
  if (state.voiceState === "RECORDING") stopRecording();
  else startRecording();
};

loadConversations().then(() => {
  if (state.conversations.length) {
    const c = state.conversations[0];
    openConversation(c.conversation_id, c.title);
  }
});
loadTasks();
setInterval(loadTasks, 5000);
