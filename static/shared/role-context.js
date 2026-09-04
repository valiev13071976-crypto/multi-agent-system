/** Resolve product role from backend — UX only; backend authorization remains authoritative. */
(function (global) {
  const MANAGEMENT_ROLES = new Set(["OWNER", "ADMIN"]);

  async function fetchJson(path, headers) {
    const res = await fetch(path, {
      credentials: "same-origin",
      headers: { Accept: "application/json", ...headers },
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      const err = new Error(data.message || data.detail?.message || res.statusText);
      err.status = res.status;
      err.code = data.code || data.detail?.code;
      throw err;
    }
    return data;
  }

  async function resolveRoleContext(apiKey) {
    try {
      const meHeaders = apiKey ? { "X-API-Key": apiKey } : {};
      const me = await fetchJson("/api/accounts/me", meHeaders);
      if (me && me.authenticated && me.auth_method === "session") {
        const role = me.role || null;
        return {
          loaded: true,
          role,
          isOwner: role === "OWNER",
          isManagement: role != null && MANAGEMENT_ROLES.has(role),
          tenantId: me.tenant_id || null,
          userId: me.user_id || null,
          entitlements: me.entitlements || null,
        };
      }
    } catch (_) {
      /* fall through to API-key product role lookup */
    }
    if (!apiKey) {
      return { loaded: false, role: null, isManagement: false, isOwner: false, tenantId: null, userId: null };
    }
    const headers = { "X-API-Key": apiKey };
    try {
      const onboarding = await fetchJson("/api/product/onboarding", headers);
      const tenantId = onboarding.tenant_id || (onboarding.tenants && onboarding.tenants[0]?.tenant_id) || null;
      if (tenantId) headers["X-Active-Tenant"] = tenantId;
      let role = null;
      try {
        const members = await fetchJson("/api/product/members", headers);
        const me = (members.items || []).find((m) => m.user_id === onboarding.user_id);
        role = me?.role || null;
      } catch (_) {
        role = null;
      }
      const isOwner = role === "OWNER";
      const isManagement = role != null && MANAGEMENT_ROLES.has(role);
      return {
        loaded: true,
        role,
        isOwner,
        isManagement,
        tenantId,
        userId: onboarding.user_id,
        entitlements: onboarding.entitlements || null,
      };
    } catch (_) {
      return { loaded: false, role: null, isManagement: false, isOwner: false, tenantId: null, userId: null };
    }
  }

  global.PandaRoleContext = { resolveRoleContext, MANAGEMENT_ROLES };
})(window);
