"""Product request context — validated active tenant selection."""

from __future__ import annotations

from fastapi import Header

from saas_product.errors import SAAS_FORBIDDEN, SaaSError
from saas_product.service import SaaSProductService
from security.api_auth import get_security_context
from security.identity import RequestSecurityContext


def resolve_product_context(
    ctx: RequestSecurityContext,
    service: SaaSProductService,
    *,
    active_tenant: str | None = None,
) -> RequestSecurityContext:
    tid = (active_tenant or "").strip() or ctx.tenant_id
    if tid != ctx.tenant_id:
        mem = service.store.get_active_membership(ctx.user_id, tid)
        if mem is None:
            raise SaaSError(SAAS_FORBIDDEN, message="Invalid active tenant.")
        return RequestSecurityContext(
            user_id=ctx.user_id,
            tenant_id=tid,
            roles=ctx.roles,
            request_id=ctx.request_id,
            auth_method=ctx.auth_method,
            session_id=ctx.session_id,
            source_ip=ctx.source_ip,
            metadata=dict(ctx.metadata),
        )
    mem = service.store.get_active_membership(ctx.user_id, tid)
    if mem is None and service.store.get_tenant(tid) is not None:
        raise SaaSError(SAAS_FORBIDDEN, message="Not a member of tenant.")
    return ctx
