"""Centralized access decision engine."""

from __future__ import annotations

from datetime import datetime, timezone

from accounts.models import (
    ACCESS_COMPLIMENTARY,
    ACCESS_NONE,
    ACCESS_PAID,
    ACCESS_STATUS_ACTIVE,
    ACCESS_STATUS_EXPIRED,
    ACCESS_STATUS_REVOKED,
    ACCESS_TRIAL,
    AccessDecision,
    ROLE_ADMIN,
    ROLE_MEMBER,
    ROLE_OWNER,
    ROLE_USER,
    STATUS_ACTIVE,
    STATUS_DISABLED,
)
from accounts.plans import PLAN_BASIC, PLAN_TRIAL, get_plan, normalize_plan_id
from accounts.reasons import (
    ACCOUNT_DISABLED,
    ALLOW,
    AUTH_REQUIRED,
    CAPABILITY_DENIED,
    DENY,
    ENTITLEMENT_REQUIRED,
    PRODUCT_LIMIT_REACHED,
    SUBSCRIPTION_INACTIVE,
    TRIAL_EXPIRED,
    USAGE_LIMIT_REACHED,
)


def _parse_ts(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _now(now: datetime | None) -> datetime:
    return now or datetime.now(timezone.utc)


def normalize_product_role(role: str) -> str:
    if role == ROLE_MEMBER:
        return ROLE_USER
    return role


class AccessDecisionEngine:
    """
    Identity + account + role + access type + plan + entitlements + usage + security capability
    → ALLOW / DENY / DEGRADE
    """

    def __init__(self, *, store, saas_store=None, default_trial_days: int = 0):
        self.store = store
        self.saas_store = saas_store
        # 0 means configured externally; engine does not invent commercial duration
        self.default_trial_days = default_trial_days

    def decide(
        self,
        *,
        user_id: str | None,
        tenant_id: str | None,
        capability: str | None = None,
        security_allowed: bool = True,
        now: datetime | None = None,
        usage_delta: int = 1,
    ) -> AccessDecision:
        stamp = _now(now)
        if not user_id or not tenant_id:
            return AccessDecision(
                decision=DENY,
                reason_code=AUTH_REQUIRED,
                access_type=ACCESS_NONE,
                access_status=ACCESS_STATUS_EXPIRED,
                plan_id="",
                role="",
            )

        user = self.store.get_user(user_id)
        if user is None:
            return AccessDecision(
                decision=DENY,
                reason_code=AUTH_REQUIRED,
                access_type=ACCESS_NONE,
                access_status=ACCESS_STATUS_EXPIRED,
                plan_id="",
                role="",
            )
        if user.status == STATUS_DISABLED or user.status != STATUS_ACTIVE:
            return AccessDecision(
                decision=DENY,
                reason_code=ACCOUNT_DISABLED,
                access_type=ACCESS_NONE,
                access_status=ACCESS_STATUS_REVOKED,
                plan_id="",
                role=normalize_product_role(user.role),
            )
        if user.tenant_id != tenant_id and normalize_product_role(user.role) != ROLE_OWNER:
            # OWNER may manage across platform tenants only via explicit owner APIs;
            # product access remains tenant-bound to user's tenant_id.
            if user.tenant_id != tenant_id:
                from accounts.reasons import TENANT_SCOPE_DENIED

                return AccessDecision(
                    decision=DENY,
                    reason_code=TENANT_SCOPE_DENIED,
                    access_type=ACCESS_NONE,
                    access_status=ACCESS_STATUS_REVOKED,
                    plan_id="",
                    role=normalize_product_role(user.role),
                )

        role = normalize_product_role(user.role)
        access_type, access_status, plan_id, trial_ends, paid_until = self._resolve_access(
            tenant_id=tenant_id, user_id=user_id, now=stamp
        )
        if access_type == ACCESS_NONE or access_status != ACCESS_STATUS_ACTIVE:
            reason = TRIAL_EXPIRED if access_type == ACCESS_TRIAL else SUBSCRIPTION_INACTIVE
            if access_type == ACCESS_NONE:
                reason = TRIAL_EXPIRED
            return AccessDecision(
                decision=DENY,
                reason_code=reason,
                access_type=access_type or ACCESS_NONE,
                access_status=access_status or ACCESS_STATUS_EXPIRED,
                plan_id=plan_id,
                role=role,
                trial_ends_at=trial_ends,
                paid_until=paid_until,
            )

        plan = get_plan(plan_id) or get_plan(PLAN_TRIAL)
        entitlements = plan.entitlements if plan else frozenset()
        if capability and capability not in entitlements:
            return AccessDecision(
                decision=DENY,
                reason_code=ENTITLEMENT_REQUIRED,
                access_type=access_type,
                access_status=access_status,
                plan_id=plan.plan_id if plan else plan_id,
                role=role,
                entitlements=entitlements,
                trial_ends_at=trial_ends,
                paid_until=paid_until,
                details={"capability": capability},
            )

        if not security_allowed:
            return AccessDecision(
                decision=DENY,
                reason_code=CAPABILITY_DENIED,
                access_type=access_type,
                access_status=access_status,
                plan_id=plan.plan_id if plan else plan_id,
                role=role,
                entitlements=entitlements,
                trial_ends_at=trial_ends,
                paid_until=paid_until,
            )

        usage_summary = self._usage_summary(tenant_id=tenant_id, user_id=user_id, plan=plan, stamp=stamp)
        if plan:
            day_limit = plan.limits.get("requests_per_day")
            if day_limit is not None and usage_summary.get("requests_today", 0) + usage_delta > day_limit:
                return AccessDecision(
                    decision=DENY,
                    reason_code=PRODUCT_LIMIT_REACHED,
                    access_type=access_type,
                    access_status=access_status,
                    plan_id=plan.plan_id,
                    role=role,
                    entitlements=entitlements,
                    trial_ends_at=trial_ends,
                    paid_until=paid_until,
                    usage_summary=usage_summary,
                    details={"limit": "requests_per_day"},
                )
            month_limit = plan.limits.get("requests_per_month")
            if month_limit is not None and usage_summary.get("requests_month", 0) + usage_delta > month_limit:
                return AccessDecision(
                    decision=DENY,
                    reason_code=USAGE_LIMIT_REACHED,
                    access_type=access_type,
                    access_status=access_status,
                    plan_id=plan.plan_id,
                    role=role,
                    entitlements=entitlements,
                    trial_ends_at=trial_ends,
                    paid_until=paid_until,
                    usage_summary=usage_summary,
                    details={"limit": "requests_per_month"},
                )

        return AccessDecision(
            decision=ALLOW,
            reason_code=ALLOW,
            access_type=access_type,
            access_status=access_status,
            plan_id=plan.plan_id if plan else plan_id,
            role=role,
            entitlements=entitlements,
            trial_ends_at=trial_ends,
            paid_until=paid_until,
            usage_summary=usage_summary,
        )

    def can_use(self, *, tenant_id: str, user_id: str, capability: str, security_allowed: bool = True) -> AccessDecision:
        return self.decide(
            user_id=user_id,
            tenant_id=tenant_id,
            capability=capability,
            security_allowed=security_allowed,
        )

    def _resolve_access(self, *, tenant_id: str, user_id: str, now: datetime) -> tuple[str, str, str, str, str]:
        # Priority: complimentary → paid subscription → trial
        for grant in self.store.list_complimentary(tenant_id):
            if grant.revoked_at:
                continue
            if grant.user_id and grant.user_id not in {"", user_id, "*"}:
                # tenant-scoped grants may target specific user or all
                if grant.user_id != user_id:
                    continue
            if grant.unlimited:
                return ACCESS_COMPLIMENTARY, ACCESS_STATUS_ACTIVE, normalize_plan_id(grant.plan_id), "", ""
            until = _parse_ts(grant.access_until)
            if until and until > now:
                return ACCESS_COMPLIMENTARY, ACCESS_STATUS_ACTIVE, normalize_plan_id(grant.plan_id), "", grant.access_until
            if until and until <= now:
                continue

        paid_until = ""
        plan_id = ""
        if self.saas_store is not None:
            sub = self.saas_store.get_subscription_for_tenant(tenant_id)
            if sub is not None:
                plan_id = normalize_plan_id(sub.plan_id)
                paid_until = sub.current_period_end or ""
                end = _parse_ts(sub.current_period_end)
                status = (sub.status or "").upper()
                # Map saas statuses
                if status in {"ACTIVE", "TRIALING", "FREE"} and (end is None or end > now):
                    if status == "TRIALING":
                        return ACCESS_TRIAL, ACCESS_STATUS_ACTIVE, plan_id or PLAN_TRIAL, paid_until, paid_until
                    return ACCESS_PAID, ACCESS_STATUS_ACTIVE, plan_id or PLAN_BASIC, "", paid_until
                if status in {"CANCEL_PENDING", "CANCELED", "CANCELLED", "PAST_DUE", "SUSPENDED", "EXPIRED"}:
                    if end and end > now and status in {"CANCEL_PENDING"}:
                        return ACCESS_PAID, ACCESS_STATUS_ACTIVE, plan_id or PLAN_BASIC, "", paid_until
                    # fall through to trial check

        trial = self.store.get_trial(tenant_id)
        if trial is not None:
            ends = _parse_ts(trial.trial_ends_at)
            if ends and ends > now and trial.status == ACCESS_STATUS_ACTIVE:
                return ACCESS_TRIAL, ACCESS_STATUS_ACTIVE, normalize_plan_id(trial.plan_id) or PLAN_TRIAL, trial.trial_ends_at, ""
            return ACCESS_TRIAL, ACCESS_STATUS_EXPIRED, normalize_plan_id(trial.plan_id) or PLAN_TRIAL, trial.trial_ends_at, ""

        return ACCESS_NONE, ACCESS_STATUS_EXPIRED, "", "", ""

    def _usage_summary(self, *, tenant_id: str, user_id: str, plan, stamp: datetime) -> dict:
        day_key = stamp.strftime("%Y-%m-%d")
        month_key = stamp.strftime("%Y-%m")
        return {
            "requests_today": self.store.usage_sum(tenant_id=tenant_id, meter="requests", period_key=day_key),
            "requests_month": self.store.usage_sum(tenant_id=tenant_id, meter="requests", period_key=month_key),
            "limits": dict(plan.limits) if plan else {},
        }

    def safe_account_view(self, *, user_id: str, tenant_id: str) -> dict:
        decision = self.decide(user_id=user_id, tenant_id=tenant_id)
        user = self.store.get_user(user_id)
        return {
            "authenticated": True,
            "user_id": user_id,
            "username": user.username if user else "",
            "display_name": user.display_name if user else "",
            "tenant_id": tenant_id,
            "role": decision.role,
            "access_type": decision.access_type,
            "access_status": decision.access_status,
            "plan_id": decision.plan_id,
            "trial_ends_at": decision.trial_ends_at,
            "paid_until": decision.paid_until,
            "entitlements": sorted(decision.entitlements),
            "usage": decision.usage_summary,
            "decision": decision.decision,
            "reason_code": decision.reason_code,
        }
