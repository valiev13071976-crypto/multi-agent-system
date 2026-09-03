"""CSRF helpers for cookie-authenticated state-changing requests."""

from __future__ import annotations

import hmac
import secrets

from accounts.errors import AccountsError
from accounts.models import CSRF_COOKIE_NAME, CSRF_HEADER_NAME
from accounts.reasons import CSRF_INVALID
from fastapi import Request


def new_csrf_token() -> str:
    return secrets.token_urlsafe(24)


def validate_csrf(request: Request, *, session_csrf: str) -> None:
    header = request.headers.get(CSRF_HEADER_NAME) or ""
    cookie = request.cookies.get(CSRF_COOKIE_NAME) or ""
    if not session_csrf or not header:
        raise AccountsError(CSRF_INVALID)
    if not hmac.compare_digest(header, session_csrf):
        raise AccountsError(CSRF_INVALID)
    # Cookie twin optional but if present must match
    if cookie and not hmac.compare_digest(cookie, session_csrf):
        raise AccountsError(CSRF_INVALID)


def origin_allowed(request: Request, *, allowed_origins: tuple[str, ...] = ()) -> bool:
    """Basic Origin check for state-changing cookie auth."""
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return True
    origin = request.headers.get("Origin") or ""
    if not origin:
        # Same-site navigations may omit Origin; rely on CSRF token
        return True
    if not allowed_origins:
        # Dev: allow same host
        host = request.headers.get("Host") or ""
        return origin.rstrip("/").endswith(host) or origin.startswith("http://localhost") or origin.startswith("http://127.0.0.1")
    return origin in allowed_origins
