"""FastAPI authentication dependency and HTTP security helpers."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request

from security.audit import SecurityAuditLog
from security.auth import AuthService
from security.errors import (
    DisabledAccountError,
    RateLimitedError,
    ResourceNotFoundError,
    SecurityError,
    UnauthorizedError,
    UnauthenticatedError,
)
from security.identity import RequestSecurityContext
from security.rate_limit import RateLimiter
from security.resource_auth import ResourceAuthorizer

# Module-level singletons wired at app startup
_auth_service: AuthService | None = None
_rate_limiter: RateLimiter | None = None
_audit_log: SecurityAuditLog | None = None
_resource_authorizer: ResourceAuthorizer | None = None


def configure_security(
    *,
    auth: AuthService | None = None,
    rate_limiter: RateLimiter | None = None,
    audit: SecurityAuditLog | None = None,
    authorizer: ResourceAuthorizer | None = None,
) -> None:
    global _auth_service, _rate_limiter, _audit_log, _resource_authorizer
    _auth_service = auth or AuthService()
    _rate_limiter = rate_limiter or RateLimiter()
    _audit_log = audit or SecurityAuditLog()
    _resource_authorizer = authorizer or ResourceAuthorizer()
    _auth_service.require_production_keys()


def get_auth_service() -> AuthService:
    if _auth_service is None:
        configure_security()
    return _auth_service  # type: ignore[return-value]


def get_rate_limiter() -> RateLimiter:
    if _rate_limiter is None:
        configure_security()
    return _rate_limiter  # type: ignore[return-value]


def get_audit_log() -> SecurityAuditLog:
    if _audit_log is None:
        configure_security()
    return _audit_log  # type: ignore[return-value]


def get_resource_authorizer() -> ResourceAuthorizer:
    if _resource_authorizer is None:
        configure_security()
    return _resource_authorizer  # type: ignore[return-value]


def _client_ip(request: Request) -> str:
    # Do not trust X-Forwarded-For without verified proxy config.
    if request.client is not None:
        return request.client.host or ""
    return ""


def _http_error_for(exc: SecurityError) -> HTTPException:
    if isinstance(exc, UnauthenticatedError):
        return HTTPException(status_code=401, detail={"error": exc.error_code})
    if isinstance(exc, RateLimitedError):
        headers = {}
        if exc.retry_after_seconds is not None:
            headers["Retry-After"] = str(int(exc.retry_after_seconds) + 1)
        return HTTPException(
            status_code=429,
            detail={"error": exc.error_code},
            headers=headers,
        )
    if isinstance(exc, DisabledAccountError):
        return HTTPException(status_code=403, detail={"error": exc.error_code})
    if isinstance(exc, (UnauthorizedError,)):
        return HTTPException(status_code=403, detail={"error": exc.error_code})
    if isinstance(exc, ResourceNotFoundError):
        return HTTPException(status_code=404, detail={"error": exc.error_code})
    return HTTPException(status_code=403, detail={"error": getattr(exc, "error_code", "security_error")})


async def get_security_context(
    request: Request,
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
    authorization: Annotated[str | None, Header()] = None,
) -> RequestSecurityContext:
    auth = get_auth_service()
    audit = get_audit_log()
    limiter = get_rate_limiter()
    request_id = str(uuid.uuid4())
    bearer = None
    if authorization and authorization.lower().startswith("bearer "):
        bearer = authorization[7:].strip()

    try:
        if auth.mode == "disabled":
            ctx = auth.dev_context(request_id=request_id)
        else:
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
    except SecurityError as exc:
        audit.record(
            "auth.failure",
            tenant_ref="",
            outcome="denied",
            reason_code=getattr(exc, "error_code", "auth_failed"),
        )
        raise _http_error_for(exc) from exc

    request.state.security_context = ctx
    return ctx


async def get_optional_security_context(
    request: Request,
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
    authorization: Annotated[str | None, Header()] = None,
) -> RequestSecurityContext | None:
    """For endpoints that remain public but accept optional identity."""
    try:
        return await get_security_context(request, x_api_key, authorization)
    except HTTPException:
        return None
