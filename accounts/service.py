"""Accounts application service — facade for auth, access, owner, compliance."""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

from accounts.access_decision import AccessDecisionEngine
from accounts.compliance import ComplianceService
from accounts.complimentary import ComplimentaryService
from accounts.errors import AccountsError
from accounts.identity_service import IdentityService, generate_temporary_password
from accounts.models import ENT_CHAT_ACCESS, ROLE_OWNER, ROLE_USER, STATUS_ACTIVE
from accounts.payment_methods import PaymentMethodService
from accounts.plans import list_plans, plan_safe_view
from accounts.session_service import SessionService
from accounts.store import AccountsStore


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class AccountsService:
    def __init__(
        self,
        *,
        store: AccountsStore,
        saas_store=None,
        saas_billing=None,
        trial_days: int | None = None,
        session_hours: int = 12,
        secure_cookies: bool = False,
    ):
        self.store = store
        self.saas_store = saas_store
        self.saas_billing = saas_billing
        self.secure_cookies = secure_cookies
        self.trial_days = trial_days
        self.sessions = SessionService(store=store, session_hours=session_hours)
        self.access = AccessDecisionEngine(store=store, saas_store=saas_store, default_trial_days=trial_days or 0)
        self.identity = IdentityService(store=store, access_engine=self.access, trial_days=trial_days)
        self.complimentary = ComplimentaryService(store=store)
        self.payment_methods = PaymentMethodService(store=store)
        self.compliance = ComplianceService(store=store)

    def login(self, *, username: str, password: str, source_ip: str = "") -> tuple[dict, object]:
        user = self.identity.authenticate(username=username, password=password, source_ip=source_ip)
        # Rotate session on login (fixation resistance)
        session = self.sessions.create_session(user_id=user.user_id, tenant_id=user.tenant_id)
        view = self.access.safe_account_view(user_id=user.user_id, tenant_id=user.tenant_id)
        view["csrf_token"] = session.csrf_token
        return view, session

    def logout(self, session_id: str | None) -> None:
        if session_id:
            self.sessions.revoke(session_id)

    def me(self, *, user_id: str, tenant_id: str) -> dict:
        return self.access.safe_account_view(user_id=user_id, tenant_id=tenant_id)

    def register(
        self,
        *,
        username: str,
        password: str,
        tenant_id: str | None = None,
        accept_terms: bool = False,
        accept_privacy: bool = False,
        marketing_opt_in: bool = False,
    ) -> dict:
        if not accept_terms or not accept_privacy:
            raise AccountsError("CONSENT_REQUIRED", "Required policy acceptance missing.")
        tid = tenant_id or f"tenant-{uuid.uuid4().hex[:12]}"
        start_trial = bool(self.trial_days and self.trial_days > 0)
        user = self.identity.create_user(
            username=username,
            password=password,
            tenant_id=tid,
            role=ROLE_USER,
            actor_id="self_register",
            start_trial=start_trial,
        )
        self.compliance.record_decision(
            user_id=user.user_id,
            tenant_id=tid,
            decision_type="TERMS_ACCEPTANCE",
            decision="ACCEPTED",
            source="registration",
        )
        self.compliance.record_decision(
            user_id=user.user_id,
            tenant_id=tid,
            decision_type="PRIVACY_POLICY_ACKNOWLEDGMENT",
            decision="ACCEPTED",
            source="registration",
        )
        if marketing_opt_in:
            self.compliance.record_decision(
                user_id=user.user_id,
                tenant_id=tid,
                decision_type="MARKETING_COMMUNICATION",
                decision="ACCEPTED",
                source="registration",
            )
        # Optional marketing must never be required — absence is fine
        return self.identity.safe_user_view(user)

    def owner_list_users(self, *, actor_user_id: str) -> list[dict]:
        actor = self.store.get_user(actor_user_id)
        if actor is None or actor.role != ROLE_OWNER:
            raise AccountsError("OWNER_REQUIRED")
        users = self.store.list_users(tenant_id=None if actor.is_bootstrap_owner else actor.tenant_id)
        out = []
        for u in users:
            view = self.identity.safe_user_view(u)
            access = self.access.safe_account_view(user_id=u.user_id, tenant_id=u.tenant_id)
            view.update(
                {
                    "access_type": access["access_type"],
                    "access_status": access["access_status"],
                    "plan_id": access["plan_id"],
                    "trial_ends_at": access["trial_ends_at"],
                    "paid_until": access["paid_until"],
                    "usage": access["usage"],
                }
            )
            out.append(view)
        return out

    def owner_create_user(
        self,
        *,
        actor_user_id: str,
        username: str,
        role: str = ROLE_USER,
        tenant_id: str | None = None,
        temporary_password: str | None = None,
    ) -> dict:
        actor = self.store.get_user(actor_user_id)
        if actor is None or actor.role != ROLE_OWNER:
            raise AccountsError("OWNER_REQUIRED")
        if role == ROLE_OWNER:
            raise AccountsError("OWNER_REQUIRED", "Cannot create OWNER via management API.")
        pwd = temporary_password or generate_temporary_password()
        # password never logged / never returned if temp was auto-generated? Spec allows temp for create —
        # return once in response only for owner create flow, never audit plaintext.
        user = self.identity.create_user(
            username=username,
            password=pwd,
            tenant_id=tenant_id or actor.tenant_id,
            role=role if role in {"ADMIN", "USER"} else ROLE_USER,
            actor_id=actor.user_id,
            start_trial=bool(self.trial_days and self.trial_days > 0),
        )
        view = self.identity.safe_user_view(user)
        view["temporary_password_issued"] = True
        view["temporary_password"] = pwd  # one-time delivery to OWNER UI; not persisted elsewhere
        return view

    def assign_plan(self, *, actor_user_id: str, tenant_id: str, plan_id: str) -> dict:
        """Owner assigns product plan via complimentary or notes — does not escalate role."""
        actor = self.store.get_user(actor_user_id)
        if actor is None or actor.role != ROLE_OWNER:
            raise AccountsError("OWNER_REQUIRED")
        from datetime import timedelta

        until = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
        grant = self.complimentary.grant(
            actor_id=actor.user_id,
            actor_role=actor.role,
            tenant_id=tenant_id,
            user_id="*",
            plan_id=plan_id,
            reason="owner_plan_assignment",
            access_until=until,
            unlimited=False,
        )
        return {"grant_id": grant.grant_id, "plan_id": plan_id, "role_unchanged": True}

    def record_product_usage(self, *, tenant_id: str, user_id: str, meter: str = "requests", quantity: int = 1, idempotency_key: str = "") -> bool:
        if quantity < 0:
            raise AccountsError("INVALID_USAGE")
        stamp = datetime.now(timezone.utc)
        period = stamp.strftime("%Y-%m-%d") if meter.endswith("day") or meter == "requests" else stamp.strftime("%Y-%m")
        key = idempotency_key or f"{tenant_id}:{user_id}:{meter}:{period}:{uuid.uuid4().hex}"
        # dual period keys for day/month when meter is requests
        if meter == "requests":
            self.store.record_usage(
                idempotency_key=key + ":d",
                tenant_id=tenant_id,
                user_id=user_id,
                meter="requests",
                quantity=quantity,
                period_key=stamp.strftime("%Y-%m-%d"),
            )
            return self.store.record_usage(
                idempotency_key=key + ":m",
                tenant_id=tenant_id,
                user_id=user_id,
                meter="requests",
                quantity=quantity,
                period_key=stamp.strftime("%Y-%m"),
            )
        return self.store.record_usage(
            idempotency_key=key,
            tenant_id=tenant_id,
            user_id=user_id,
            meter=meter,
            quantity=quantity,
            period_key=period,
        )

    def plans(self) -> list[dict]:
        return [plan_safe_view(p) for p in list_plans()]

    def bootstrap_from_env(self, env: dict | None = None) -> dict:
        """
        Secure bootstrap: reads OWNER username/password from env.
        Never prints password. Idempotent if protected OWNER exists.
        Does not create production OWNER unless env explicitly provided by operator.
        """
        source = env if env is not None else os.environ
        if str(source.get("PANDA_BOOTSTRAP_OWNER") or "").lower() not in {"1", "true", "yes"}:
            return {"bootstrapped": False, "reason": "bootstrap_flag_disabled"}
        username = (source.get("PANDA_OWNER_USERNAME") or "").strip()
        password = source.get("PANDA_OWNER_PASSWORD") or ""
        tenant_id = (source.get("PANDA_OWNER_TENANT_ID") or "tenant-owner").strip()
        if not username or not password:
            return {"bootstrapped": False, "reason": "credentials_missing"}
        user = self.identity.bootstrap_owner_if_needed(username=username, password=password, tenant_id=tenant_id)
        if user is None:
            return {"bootstrapped": False, "reason": "owner_already_exists"}
        return {"bootstrapped": True, "user_id": user.user_id, "tenant_id": user.tenant_id, "username": user.username}

    def chat_access_gate(self, *, user_id: str, tenant_id: str) -> dict:
        decision = self.access.can_use(tenant_id=tenant_id, user_id=user_id, capability=ENT_CHAT_ACCESS)
        return {
            "allowed": decision.allowed(),
            "reason_code": decision.reason_code,
            "access_type": decision.access_type,
            "plan_id": decision.plan_id,
        }
