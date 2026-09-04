/** Canonical Business Assistant API client — single backend boundary. */
(function (global) {
  const BASE = "/api/v1/business-assistant";
  const copy = global.PandaCopy;

  class ApiError extends Error {
    constructor(message, code, status) {
      super(message);
      this.code = code;
      this.status = status;
    }
  }

  function authHeaders() {
    const key = sessionStorage.getItem("panda_api_key") || "";
    const h = {};
    if (key) h["X-API-Key"] = key;
    return h;
  }

  async function request(path, options = {}) {
    const isForm = options.body instanceof FormData;
    const res = await fetch(`${BASE}${path}`, {
      ...options,
      credentials: "same-origin",
      headers: {
        ...(isForm ? {} : { "Content-Type": "application/json" }),
        ...authHeaders(),
        ...(options.headers || {}),
      },
    });
    const data = res.status === 204 ? {} : await res.json().catch(() => ({}));
    if (!res.ok) {
      const detail = data.detail || data;
      throw new ApiError(
        detail.message || detail.error || res.statusText,
        detail.code || detail.error || "request_failed",
        res.status
      );
    }
    return data;
  }

  const client = {
    ApiError,
    setApiKey(key) {
      sessionStorage.setItem("panda_api_key", key || "");
    },
    clearApiKey() {
      sessionStorage.removeItem("panda_api_key");
    },
    hasApiKey() {
      return Boolean(sessionStorage.getItem("panda_api_key"));
    },
    getApiKey() {
      return sessionStorage.getItem("panda_api_key") || "";
    },
    async hasHumanSession() {
      const res = await fetch("/api/accounts/me", {
        credentials: "same-origin",
        headers: { Accept: "application/json" },
      });
      if (!res.ok) return false;
      const data = await res.json().catch(() => ({}));
      return Boolean(data.authenticated && data.auth_method === "session");
    },
    listConversations() {
      return request("/conversations");
    },
    createConversation(title) {
      return request("/conversations", { method: "POST", body: JSON.stringify({ title }) });
    },
    listMessages(conversationId) {
      return request(`/conversations/${encodeURIComponent(conversationId)}/messages`);
    },
    renameConversation(conversationId, title) {
      return request(`/conversations/${encodeURIComponent(conversationId)}`, {
        method: "PATCH",
        body: JSON.stringify({ title }),
      });
    },
    deleteConversation(conversationId) {
      return request(`/conversations/${encodeURIComponent(conversationId)}`, { method: "DELETE" });
    },
    uploadFile(file) {
      const fd = new FormData();
      fd.append("file", file);
      return request("/attachments", { method: "POST", body: fd });
    },
    submitRequest(payload) {
      return request("/requests", { method: "POST", body: JSON.stringify(payload) });
    },
    getRequest(requestId) {
      return request(`/requests/${encodeURIComponent(requestId)}`);
    },
    getStatus(requestId) {
      return request(`/requests/${encodeURIComponent(requestId)}/status`);
    },
    listEvents(requestId, after) {
      const q = after ? `?after=${encodeURIComponent(after)}` : "";
      return request(`/requests/${encodeURIComponent(requestId)}/events${q}`);
    },
    getPreview(requestId) {
      return request(`/requests/${encodeURIComponent(requestId)}/preview`);
    },
    getResult(requestId) {
      return request(`/requests/${encodeURIComponent(requestId)}/result`);
    },
    listArtifacts(requestId) {
      return request(`/requests/${encodeURIComponent(requestId)}/artifacts`);
    },
    approve(requestId, body) {
      return request(`/requests/${encodeURIComponent(requestId)}/approve`, {
        method: "POST",
        body: JSON.stringify(body || {}),
      });
    },
    reject(requestId) {
      return request(`/requests/${encodeURIComponent(requestId)}/reject`, { method: "POST" });
    },
    cancel(requestId) {
      return request(`/requests/${encodeURIComponent(requestId)}/cancel`, { method: "POST" });
    },
    mapError(err) {
      return copy ? copy.mapError(err) : (err.message || "Ошибка");
    },
    statusLabel(status) {
      return copy ? copy.userFacingStatus(status) : status;
    },
    isTerminal(status) {
      return ["COMPLETED", "FAILED", "REJECTED", "CANCELLED", "BLOCKED"].includes(status);
    },
    uuid() {
      return crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`;
    },
  };

  global.PandaApi = client;
})(window);
