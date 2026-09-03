"""Server-side session management."""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timedelta, timezone

from accounts.errors import AccountsError
from accounts.models import SESSION_COOKIE_NAME, AccountsAuditEvent, SessionRecord
from accounts.reasons import AUTH_REQUIRED, SESSION_EXPIRED
from security.config import ROLE_ADMIN, ROLE_PLATFORM_ADMIN, ROLE_TENANT_ADMIN, ROLE_USER
from security.identity import RequestSecurityContext


def _utc() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None = None) -> str:
    return (dt or _utc()).isoformat()


DEFAULT_SESSION_HOURS = 12


def product_role_to_security_roles(product_role: str) -> tuple[str, ...]:
    if product_role == "OWNER":
        return (ROLE_PLATFORM_ADMIN, ROLE_ADMIN, ROLE_USER)
    if product_role == "ADMIN":
        return (ROLE_TENANT_ADMIN, ROLE_USER)
    return (ROLE_USER,)


class SessionService:
    def __init__(self, *, store, session_hours: int = DEFAULT_SESSION_HOURS):
        self.store = store
        self.session_hours = session_hours

    def create_session(self, *, user_id: str, tenant_id: str, auth_method: str = "password") -> SessionRecord:
        now = _utc()
        session = SessionRecord(
            session_id=secrets.token_urlsafe(32),
            user_id=user_id,
            tenant_id=tenant_id,
            created_at=_iso(now),
            expires_at=_iso(now + timedelta(hours=self.session_hours)),
            last_seen_at=_iso(now),
            csrf_token=secrets.token_urlsafe(24),
            auth_method=auth_method,
        )
        self.store.create_session(session)
        self.store.append_audit(
            AccountsAuditEvent(
                event_id=str(uuid.uuid4()),
                timestamp=_iso(now),
                actor_id=user_id,
                target_id=session.session_id,
                tenant_id=tenant_id,
                action="session.created",
                result="ok",
            )
        )
        return session

    def resolve(self, session_id: str | None) -> SessionRecord:
        if not session_id:
            raise AccountsError(AUTH_REQUIRED)
        session = self.store.get_session(session_id)
        if session is None:
            raise AccountsError(AUTH_REQUIRED)
        if session.revoked_at:
            raise AccountsError(SESSION_EXPIRED)
        expires = datetime.fromisoformat(session.expires_at)
        if expires <= _utc():
            raise AccountsError(SESSION_EXPIRED)
        # Sliding activity update
        updated = SessionRecord(**{**session.__dict__, "last_seen_at": _iso()})
        self.store.update_session(updated)
        return updated

    def revoke(self, session_id: str, *, actor_id: str = "") -> None:
        session = self.store.get_session(session_id)
        if session is None:
            return
        if session.revoked_at:
            return
        now = _iso()
        self.store.update_session(SessionRecord(**{**session.__dict__, "revoked_at": now}))
        self.store.append_audit(
            AccountsAuditEvent(
                event_id=str(uuid.uuid4()),
                timestamp=now,
                actor_id=actor_id or session.user_id,
                target_id=session.session_id,
                tenant_id=session.tenant_id,
                action="session.revoked",
                result="ok",
            )
        )

    def revoke_all_for_user(self, user_id: str, *, actor_id: str) -> int:
        now = _iso()
        count = self.store.revoke_sessions_for_user(user_id, now=now)
        user = self.store.get_user(user_id)
        self.store.append_audit(
            AccountsAuditEvent(
                event_id=str(uuid.uuid4()),
                timestamp=now,
                actor_id=actor_id,
                target_id=user_id,
                tenant_id=user.tenant_id if user else "",
                action="session.revoke_all",
                result="ok",
                metadata={"count": count},
            )
        )
        return count

    def to_security_context(self, *, session: SessionRecord, product_role: str, request_id: str, source_ip: str = "") -> RequestSecurityContext:
        return RequestSecurityContext(
            user_id=session.user_id,
            tenant_id=session.tenant_id,
            roles=product_role_to_security_roles(product_role),
            request_id=request_id,
            auth_method="session",
            session_id=session.session_id,
            source_ip=source_ip or None,
            metadata={"product_role": product_role, "csrf_token_present": bool(session.csrf_token)},
        )

    @staticmethod
    def cookie_params(*, secure: bool) -> dict:
        return {
            "key": SESSION_COOKIE_NAME,
            "httponly": True,
            "secure": secure,
            "samesite": "lax",
            "path": "/",
        }
