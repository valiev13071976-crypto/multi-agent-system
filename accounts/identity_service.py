"""Identity + password authentication service."""

from __future__ import annotations

import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from accounts.errors import AccountsError, InvalidCredentialsError
from accounts.models import (
    ACCESS_STATUS_ACTIVE,
    AccountsAuditEvent,
    HumanUserRecord,
    ROLE_ADMIN,
    ROLE_OWNER,
    ROLE_USER,
    STATUS_ACTIVE,
    STATUS_DISABLED,
    TrialRecord,
)
from accounts.passwords import hash_password, looks_like_plaintext_password_store, validate_password_policy, verify_password
from accounts.plans import PLAN_TRIAL
from accounts.reasons import ACCOUNT_DISABLED, OWNER_REQUIRED, RATE_LIMITED, TENANT_SCOPE_DENIED

_USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{3,64}$")
LOGIN_FAIL_LIMIT = 8
LOGIN_WINDOW_SECONDS = 900
# Precomputed dummy hash for missing-user timing path (scrypt; never a real account).
_DUMMY_PASSWORD_HASH = (
    "scrypt$00000000000000000000000000000000$"
    "0000000000000000000000000000000000000000000000000000000000000000"
)


def _utc() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None = None) -> str:
    return (dt or _utc()).isoformat()


def normalize_username(username: str) -> str:
    return (username or "").strip().lower()


class IdentityService:
    def __init__(self, *, store, access_engine=None, trial_days: int | None = None):
        self.store = store
        self.access_engine = access_engine
        # Configurable; None/0 means do not auto-start trial without explicit config
        self.trial_days = trial_days

    def _audit(self, *, actor_id: str, target_id: str, tenant_id: str, action: str, result: str, **meta):
        self.store.append_audit(
            AccountsAuditEvent(
                event_id=str(uuid.uuid4()),
                timestamp=_iso(),
                actor_id=actor_id,
                target_id=target_id,
                tenant_id=tenant_id,
                action=action,
                result=result,
                metadata=meta,
            )
        )

    def create_user(
        self,
        *,
        username: str,
        password: str,
        tenant_id: str,
        role: str = ROLE_USER,
        actor_id: str = "system",
        email: str = "",
        display_name: str = "",
        is_bootstrap_owner: bool = False,
        protected: bool = False,
        start_trial: bool = False,
    ) -> HumanUserRecord:
        if role == ROLE_OWNER and not (is_bootstrap_owner or actor_id == "bootstrap"):
            # Only bootstrap or existing OWNER path may create OWNER
            raise AccountsError(OWNER_REQUIRED, "OWNER creation denied.")
        if role not in {ROLE_OWNER, ROLE_ADMIN, ROLE_USER}:
            raise AccountsError("INVALID_ROLE", "Invalid role.")
        norm = normalize_username(username)
        if not _USERNAME_RE.match(norm):
            raise AccountsError("INVALID_USERNAME", "Invalid username.")
        validate_password_policy(password)
        if self.store.get_user_by_username(norm) is not None:
            # generic — avoid enumeration in public register; owner path may differ
            raise AccountsError("USERNAME_TAKEN", "Unable to create account.")
        now = _iso()
        user = HumanUserRecord(
            user_id=f"user-{uuid.uuid4().hex[:16]}",
            tenant_id=tenant_id,
            username=username.strip(),
            normalized_username=norm,
            password_hash=hash_password(password),
            role=role,
            status=STATUS_ACTIVE,
            created_at=now,
            updated_at=now,
            password_changed_at=now,
            email=email.strip()[:320],
            display_name=(display_name or username).strip()[:120],
            is_bootstrap_owner=is_bootstrap_owner,
            protected=protected or is_bootstrap_owner,
        )
        if looks_like_plaintext_password_store(user.password_hash):
            raise AccountsError("PASSWORD_HASH_INVALID", "Password storage failed.")
        self.store.create_user(user)
        if start_trial and self.trial_days and self.trial_days > 0:
            self.start_trial(tenant_id=tenant_id, user_id=user.user_id, days=self.trial_days)
        self._audit(
            actor_id=actor_id,
            target_id=user.user_id,
            tenant_id=tenant_id,
            action="user.created",
            result="ok",
            role=role,
        )
        return user

    def start_trial(self, *, tenant_id: str, user_id: str, days: int, plan_id: str = PLAN_TRIAL) -> TrialRecord:
        if days <= 0:
            raise AccountsError("TRIAL_CONFIG_REQUIRED", "Trial duration must be configured.")
        existing = self.store.get_trial(tenant_id)
        if existing is not None:
            # Do not silently extend or reset
            return existing
        now = _utc()
        trial = TrialRecord(
            trial_id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            user_id=user_id,
            plan_id=plan_id,
            trial_started_at=_iso(now),
            trial_ends_at=_iso(now + timedelta(days=days)),
            created_at=_iso(now),
            status=ACCESS_STATUS_ACTIVE,
        )
        return self.store.upsert_trial(trial)

    def authenticate(self, *, username: str, password: str, source_ip: str = "") -> HumanUserRecord:
        norm = normalize_username(username)
        bucket = f"login:{norm}:{source_ip or 'unknown'}"
        fails, window = self.store.get_login_fails(bucket)
        now = _utc()
        if window:
            started = datetime.fromisoformat(window)
            if (now - started).total_seconds() > LOGIN_WINDOW_SECONDS:
                fails = 0
                window = _iso(now)
        if fails >= LOGIN_FAIL_LIMIT:
            raise AccountsError(RATE_LIMITED, "Too many attempts. Try later.")
        user = self.store.get_user_by_username(norm)
        # Constant-ish path: verify against dummy hash if missing (no enumeration)
        ok = False
        if user is not None:
            ok = verify_password(password, user.password_hash)
        else:
            verify_password(password, _DUMMY_PASSWORD_HASH)
        if not ok or user is None:
            self.store.set_login_fails(bucket, fails + 1, window or _iso(now))
            raise InvalidCredentialsError()
        if user.status != STATUS_ACTIVE:
            raise AccountsError(ACCOUNT_DISABLED, "Account disabled.")
        self.store.clear_login_fails(bucket)
        updated = HumanUserRecord(**{**user.__dict__, "last_login_at": _iso(now), "updated_at": _iso(now)})
        self.store.update_user(updated)
        self._audit(
            actor_id=user.user_id,
            target_id=user.user_id,
            tenant_id=user.tenant_id,
            action="user.login",
            result="ok",
        )
        return updated

    def set_status(self, *, actor: HumanUserRecord, target_user_id: str, status: str) -> HumanUserRecord:
        target = self.store.get_user(target_user_id)
        if target is None:
            raise AccountsError("NOT_FOUND", "User not found.")
        if actor.role != ROLE_OWNER:
            raise AccountsError(OWNER_REQUIRED)
        if actor.tenant_id != target.tenant_id and not actor.is_bootstrap_owner:
            raise AccountsError(TENANT_SCOPE_DENIED)
        if target.protected and target.is_bootstrap_owner and status == STATUS_DISABLED:
            raise AccountsError("PROTECTED_OWNER", "Protected OWNER cannot be disabled.")
        updated = HumanUserRecord(**{**target.__dict__, "status": status, "updated_at": _iso()})
        self.store.update_user(updated)
        self._audit(
            actor_id=actor.user_id,
            target_id=target.user_id,
            tenant_id=target.tenant_id,
            action="user.status_changed",
            result="ok",
            status=status,
        )
        return updated

    def change_role(self, *, actor: HumanUserRecord, target_user_id: str, role: str) -> HumanUserRecord:
        if actor.role != ROLE_OWNER:
            raise AccountsError(OWNER_REQUIRED)
        if role == ROLE_OWNER:
            raise AccountsError(OWNER_REQUIRED, "Cannot grant OWNER via management API.")
        if role not in {ROLE_ADMIN, ROLE_USER}:
            raise AccountsError("INVALID_ROLE")
        target = self.store.get_user(target_user_id)
        if target is None:
            raise AccountsError("NOT_FOUND")
        if target.protected and target.is_bootstrap_owner:
            raise AccountsError("PROTECTED_OWNER", "Protected OWNER cannot be demoted.")
        if actor.tenant_id != target.tenant_id and not actor.is_bootstrap_owner:
            raise AccountsError(TENANT_SCOPE_DENIED)
        updated = HumanUserRecord(**{**target.__dict__, "role": role, "updated_at": _iso()})
        self.store.update_user(updated)
        self._audit(
            actor_id=actor.user_id,
            target_id=target.user_id,
            tenant_id=target.tenant_id,
            action="user.role_changed",
            result="ok",
            role=role,
        )
        return updated

    def set_password(self, *, actor_id: str, user_id: str, new_password: str) -> None:
        validate_password_policy(new_password)
        user = self.store.get_user(user_id)
        if user is None:
            raise AccountsError("NOT_FOUND")
        updated = HumanUserRecord(
            **{
                **user.__dict__,
                "password_hash": hash_password(new_password),
                "password_changed_at": _iso(),
                "updated_at": _iso(),
            }
        )
        self.store.update_user(updated)
        self._audit(actor_id=actor_id, target_id=user_id, tenant_id=user.tenant_id, action="user.password_changed", result="ok")

    def safe_user_view(self, user: HumanUserRecord) -> dict:
        return {
            "user_id": user.user_id,
            "tenant_id": user.tenant_id,
            "username": user.username,
            "display_name": user.display_name,
            "email": user.email,
            "role": user.role,
            "status": user.status,
            "created_at": user.created_at,
            "last_login_at": user.last_login_at,
            "is_bootstrap_owner": user.is_bootstrap_owner,
            # never password / hash
        }

    def bootstrap_owner_if_needed(self, *, username: str, password: str, tenant_id: str) -> HumanUserRecord | None:
        """Idempotent bootstrap. Returns None if protected OWNER already exists. Never prints password."""
        if self.store.count_protected_owners() > 0:
            return None
        return self.create_user(
            username=username,
            password=password,
            tenant_id=tenant_id,
            role=ROLE_OWNER,
            actor_id="bootstrap",
            is_bootstrap_owner=True,
            protected=True,
            display_name="Owner",
            start_trial=False,
        )


def generate_temporary_password() -> str:
    """Caller must deliver out-of-band; never log/persist plaintext."""
    return secrets.token_urlsafe(18)
