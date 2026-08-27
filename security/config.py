"""Security configuration — auth mode, rate limits, CORS."""

from __future__ import annotations

import os

AUTH_MODE_DISABLED = "disabled"
AUTH_MODE_REQUIRED = "required"

DEFAULT_LEGACY_TENANT = "legacy-default"
DEFAULT_LEGACY_USER = "legacy-user"

# RBAC role names
ROLE_USER = "user"
ROLE_OPERATOR = "operator"
ROLE_APPROVER = "approver"
ROLE_ADMIN = "admin"
ROLE_SERVICE = "service"

ALL_ROLES = frozenset(
    {ROLE_USER, ROLE_OPERATOR, ROLE_APPROVER, ROLE_ADMIN, ROLE_SERVICE}
)


def panda_env(env: dict | None = None) -> dict:
    return env if env is not None else os.environ


def security_auth_mode(env: dict | None = None) -> str:
    source = panda_env(env)
    raw = (source.get("SECURITY_AUTH_MODE") or "").strip().lower()
    if raw in {AUTH_MODE_DISABLED, AUTH_MODE_REQUIRED}:
        return raw
    # Production default: required; dev default: disabled for local iteration.
    panda = (source.get("PANDA_ENV") or source.get("ENVIRONMENT") or "").strip().lower()
    if panda in {"production", "prod"}:
        return AUTH_MODE_REQUIRED
    return AUTH_MODE_DISABLED


def security_default_tenant(env: dict | None = None) -> str:
    return (panda_env(env).get("SECURITY_DEFAULT_TENANT") or DEFAULT_LEGACY_TENANT).strip()


def rate_limit_per_user_per_minute(env: dict | None = None) -> int:
    return max(1, int(panda_env(env).get("SECURITY_RATE_LIMIT_USER_PER_MIN") or "120"))


def rate_limit_per_tenant_per_minute(env: dict | None = None) -> int:
    return max(1, int(panda_env(env).get("SECURITY_RATE_LIMIT_TENANT_PER_MIN") or "600"))


def rate_limit_unauthenticated_per_minute(env: dict | None = None) -> int:
    return max(1, int(panda_env(env).get("SECURITY_RATE_LIMIT_IP_PER_MIN") or "30"))


def cors_allow_origins(env: dict | None = None) -> tuple[str, ...]:
    raw = (panda_env(env).get("SECURITY_CORS_ORIGINS") or "").strip()
    if not raw:
        return ()
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def max_request_body_bytes(env: dict | None = None) -> int:
    return max(1024, int(panda_env(env).get("SECURITY_MAX_REQUEST_BODY_BYTES") or "65536"))
