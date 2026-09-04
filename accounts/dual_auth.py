"""Dual authentication: human session cookie OR machine API key."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import Header, HTTPException, Request

from accounts.errors import AccountsError
from accounts.models import SESSION_COOKIE_NAME
from accounts.reasons import ACCOUNT_DISABLED, AUTH_REQUIRED, SESSION_EXPIRED
from security.api_auth import _client_ip, _http_error_for, get_auth_service, get_audit_log, get_rate_limiter
from security.errors import SecurityError
from security.identity import RequestSecurityContext

_accounts_service = None


def configure_accounts_auth(service) -> None:
    global _accounts_service
    _accounts_service = service


def get_accounts_service():
    return _accounts_service


async def get_security_context_dual(
    request: Request,
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
    authorization: Annotated[str | None, Header()] = None,
) -> RequestSecurityContext:
    """
    Order:
    1) Session cookie if present (human)
    2) API key / bearer if present (machine/workspace)
    3) Disabled-mode legacy context
    """
    request_id = str(uuid.uuid4())
    audit = get_audit_log()
    limiter = get_rate_limiter()
    auth = get_auth_service()
    bearer = None
    if authorization and authorization.lower().startswith("bearer "):
        bearer = authorization[7:].strip()

    session_id = request.cookies.get(SESSION_COOKIE_NAME)
    if session_id and _accounts_service is not None:
        return _session_context(request, session_id, request_id)

    if x_api_key or bearer:
        try:
            ctx = auth.authenticate(
                api_key=x_api_key,
                bearer=bearer,
                request_id=request_id,
                source_ip=_client_ip(request),
            )
            limiter.check_authenticated(tenant_id=ctx.tenant_id, user_id=ctx.user_id)
            audit.record(
                "auth.success",
                actor_ref=ctx.actor_ref(),
                tenant_ref=ctx.tenant_id,
                outcome="ok",
                metadata={"auth_method": ctx.auth_method},
            )
            request.state.security_context = ctx
            return ctx
        except SecurityError as exc:
            audit.record(
                "auth.failure",
                tenant_ref="",
                outcome="denied",
                reason_code=getattr(exc, "error_code", "auth_failed"),
            )
            raise _http_error_for(exc) from exc

    if auth.mode == "disabled":
        ctx = auth.dev_context(request_id=request_id)
        request.state.security_context = ctx
        return ctx

    raise HTTPException(status_code=401, detail={"error": AUTH_REQUIRED, "code": AUTH_REQUIRED})


def _session_context(request: Request, session_id: str, request_id: str) -> RequestSecurityContext:
    assert _accounts_service is not None
    try:
        session = _accounts_service.sessions.resolve(session_id)
        user = _accounts_service.store.get_user(session.user_id)
        if user is None:
            raise AccountsError(AUTH_REQUIRED)
        if user.status != "ACTIVE":
            raise AccountsError(ACCOUNT_DISABLED)
        ctx = _accounts_service.sessions.to_security_context(
            session=session,
            product_role=user.role,
            request_id=request_id,
            source_ip=_client_ip(request),
        )
        request.state.security_context = ctx
        request.state.accounts_session = session
        request.state.accounts_user = user
        return ctx
    except AccountsError as exc:
        code = 401 if exc.code in {AUTH_REQUIRED, SESSION_EXPIRED} else 403
        raise HTTPException(status_code=code, detail={"error": exc.code, "code": exc.code}) from exc


def install_dual_auth() -> None:
    import security.api_auth as api_auth

    # Mutate the same Depends() callable (import-time bindings) plus the module attr.
    api_auth.set_security_context_override(get_security_context_dual)
    api_auth.get_security_context = get_security_context_dual
