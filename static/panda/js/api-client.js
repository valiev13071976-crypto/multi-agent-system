/** Canonical Business Assistant API client — single backend boundary. */
(function (global) {
  const BASE = "/api/v1/business-assistant";

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
      headers: {
        ...(isForm ? {} : { "Content-Type": "application/json" }),
        ...authHeaders(),
        ...(options.headers || {}),
      },
    });
    const data = await res.json().catch(() => ({}));
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
    listConversations() {
      return request("/conversations");
    },
    createConversation(title) {
      return request("/conversations", { method: "POST", body: JSON.stringify({ title }) });
    },
    listMessages(conversationId) {
      return request(`/conversations/${encodeURIComponent(conversationId)}/messages`);
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
      if (err instanceof ApiError) {
        const map = {
          BAA_AUTH_FAILED: "Authentication failed.",
          BAA_ACCESS_DENIED: "You do not have permission for this action.",
          BAA_NOT_FOUND: "Request not found.",
          BAA_APPROVAL_STALE: "Approval is stale — refresh and review the preview again.",
          BAA_INVALID_STATE: "This action is not available in the current state.",
          BAA_IDEMPOTENCY_CONFLICT: "Duplicate submission conflict.",
          BAA_PROVIDER_UNAVAILABLE: "External provider is temporarily unavailable.",
          BAA_INTEGRATION_UNAVAILABLE: "Required integration is not configured.",
        };
        return map[err.code] || err.message || "Request failed.";
      }
      if (err.status === 401) return "Please sign in again.";
      if (err.status === 429) return "Too many requests — try again shortly.";
      return err.message || "Network error.";
    },
    uuid() {
      return crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`;
    },
    statusLabel(status) {
      const labels = {
        RECEIVED: "Received",
        VALIDATING: "Validating",
        PLANNING: "Planning",
        QUEUED: "Queued",
        RUNNING: "Running",
        WAITING_FOR_APPROVAL: "Waiting for approval",
        RESUMING: "Resuming",
        COMPLETED: "Completed",
        FAILED: "Failed",
        REJECTED: "Rejected",
        CANCELLED: "Cancelled",
        BLOCKED: "Blocked",
      };
      return labels[status] || status;
    },
    isTerminal(status) {
      return ["COMPLETED", "FAILED", "REJECTED", "CANCELLED", "BLOCKED"].includes(status);
    },
  };

  global.PandaApi = client;
})(window);
