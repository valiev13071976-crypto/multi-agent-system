"""Main SaaS product service — tenants, membership, billing, privacy."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from saas_product.access import ProductAuthorizationPolicy
from saas_product.billing import BillingService
from saas_product.capabilities import (
    PERM_BILLING_READ,
    PERM_BILLING_WRITE,
    PERM_MEMBERS_READ,
    PERM_MEMBERS_WRITE,
    PERM_PRIVACY_READ,
    PERM_PRIVACY_WRITE,
    ROLE_ADMIN,
    ROLE_MEMBER,
    ROLE_OWNER,
)
from saas_product.entitlements import EntitlementService
from saas_product.errors import (
    SAAS_CONFLICT,
    SAAS_ENTITLEMENT_DENIED,
    SAAS_FORBIDDEN,
    SAAS_LAST_OWNER,
    SAAS_NOT_FOUND,
    SAAS_QUOTA_EXCEEDED,
    SAAS_SELF_ESCALATION,
    SaaSError,
)
from saas_product.metering import MeteringService
from saas_product.models import (
    ACCOUNT_ACTIVE,
    INVITE_ACCEPTED,
    INVITE_EXPIRED,
    INVITE_PENDING,
    INVITE_REVOKED,
    MEMBERSHIP_ACTIVE,
    MEMBERSHIP_REMOVED,
    TENANT_ACTIVE,
    InvitationRecord,
    MembershipRecord,
    ProductAuditEvent,
    TenantRecord,
    UserAccount,
)
from saas_product.plans import PLAN_FREE, list_plans
from saas_product.privacy import PrivacyService
from saas_product.providers.fake_billing import FakeBillingProvider
from security.identity import RequestSecurityContext


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


class SaaSProductService:
    def __init__(
        self,
        *,
        store,
        access: ProductAuthorizationPolicy | None = None,
        billing: BillingService | None = None,
        entitlements: EntitlementService | None = None,
        metering: MeteringService | None = None,
        privacy: PrivacyService | None = None,
        finops=None,
        email_provider=None,
    ):
        self.store = store
        self.access = access or ProductAuthorizationPolicy(store.get_active_membership)
        self.entitlements = entitlements or EntitlementService()
        self.billing = billing or BillingService(store=store, entitlements=self.entitlements)
        self.metering = metering or MeteringService(store=store, finops=finops)
        self.privacy = privacy or PrivacyService(store=store)
        self.email_provider = email_provider
        self._accepted_invites: set[str] = set()

    def _audit(self, ctx: RequestSecurityContext, *, action: str, target_type: str, target_id: str, result: str, reason: str = ""):
        self.store.append_audit(
            ProductAuditEvent(
                event_id=str(uuid.uuid4()),
                timestamp=_utc(),
                actor_ref=ctx.actor_ref(),
                tenant_id=ctx.tenant_id,
                action=action,
                target_type=target_type,
                target_id=target_id,
                result=result,
                reason=reason,
            )
        )

    def ensure_user(self, ctx: RequestSecurityContext) -> UserAccount:
        user = self.store.get_user(ctx.user_id)
        if user is None:
            user = UserAccount(user_id=ctx.user_id, status=ACCOUNT_ACTIVE, created_at=_utc(), updated_at=_utc())
            self.store.create_user(user)
        return user

    def onboarding(self, ctx: RequestSecurityContext) -> dict:
        self.ensure_user(ctx)
        tenants = self.store.list_tenants_for_user(ctx.user_id)
        sub = self.store.get_subscription_for_tenant(ctx.tenant_id)
        ent = self.entitlements.resolve(tenant_id=ctx.tenant_id, subscription=sub)
        return {
            "user_id": ctx.user_id,
            "tenant_id": ctx.tenant_id,
            "tenants": [t.__dict__ for t in tenants],
            "entitlements": ent.__dict__,
            "plans": [p.__dict__ for p in list_plans()],
        }

    def create_tenant(self, ctx: RequestSecurityContext, *, name: str) -> TenantRecord:
        self.ensure_user(ctx)
        tenant_id = f"tenant-{uuid.uuid4().hex[:12]}"
        tenant = TenantRecord(
            tenant_id=tenant_id,
            name=name[:200],
            status=TENANT_ACTIVE,
            owner_user_id=ctx.user_id,
            created_at=_utc(),
            updated_at=_utc(),
        )
        self.store.create_tenant(tenant)
        membership = MembershipRecord(
            membership_id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            user_id=ctx.user_id,
            role=ROLE_OWNER,
            status=MEMBERSHIP_ACTIVE,
            created_at=_utc(),
        )
        self.store.create_membership(membership)
        self._audit(ctx, action="tenant.create", target_type="tenant", target_id=tenant_id, result="ok")
        return tenant

    def list_my_tenants(self, ctx: RequestSecurityContext) -> list[TenantRecord]:
        self.ensure_user(ctx)
        return list(self.store.list_tenants_for_user(ctx.user_id))

    def switch_tenant(self, ctx: RequestSecurityContext, *, tenant_id: str) -> dict:
        mem = self.store.get_active_membership(ctx.user_id, tenant_id)
        if mem is None:
            raise SaaSError(SAAS_FORBIDDEN, message="Not a member of tenant.")
        tenant = self.store.get_tenant(tenant_id)
        if tenant is None or tenant.status != TENANT_ACTIVE:
            raise SaaSError(SAAS_NOT_FOUND)
        sub = self.store.get_subscription_for_tenant(tenant_id)
        ent = self.entitlements.resolve(tenant_id=tenant_id, subscription=sub)
        return {"tenant_id": tenant_id, "role": mem.role, "entitlements": ent.__dict__}

    def list_members(self, ctx: RequestSecurityContext, *, limit: int = 50, offset: int = 0):
        self.access.require(ctx, PERM_MEMBERS_READ)
        return self.store.list_memberships(ctx.tenant_id, limit=limit, offset=offset)

    def invite_member(self, ctx: RequestSecurityContext, *, email: str, role: str = ROLE_MEMBER) -> tuple[InvitationRecord, str]:
        self.access.require(ctx, PERM_MEMBERS_WRITE)
        if role == ROLE_OWNER:
            raise SaaSError(SAAS_FORBIDDEN, message="Cannot invite as owner.")
        token, token_hash = FakeBillingProvider.generate_invitation_token()
        invitation = InvitationRecord(
            invitation_id=str(uuid.uuid4()),
            tenant_id=ctx.tenant_id,
            email=email.strip().lower()[:320],
            role=role,
            invited_by=ctx.user_id,
            token_hash=token_hash,
            status=INVITE_PENDING,
            expires_at=(datetime.now(timezone.utc) + timedelta(days=7)).isoformat(),
            created_at=_utc(),
        )
        self.store.create_invitation(invitation)
        if self.email_provider is not None:
            from integrations.production.adapters.email import TransactionalEmailMessage

            self.email_provider.send(
                TransactionalEmailMessage(
                    recipient=invitation.email,
                    event_type="tenant_invitation",
                    template_data={"tenant_id": ctx.tenant_id},
                    idempotency_key=invitation.invitation_id,
                    tenant_id=ctx.tenant_id,
                    user_id=ctx.user_id,
                    correlation_id=invitation.invitation_id,
                )
            )
        self._audit(ctx, action="member.invite", target_type="invitation", target_id=invitation.invitation_id, result="ok")
        return invitation, token

    def revoke_invitation(self, ctx: RequestSecurityContext, invitation_id: str) -> InvitationRecord:
        self.access.require(ctx, PERM_MEMBERS_WRITE)
        inv = self.store.get_invitation(invitation_id)
        if inv is None or inv.tenant_id != ctx.tenant_id:
            raise SaaSError(SAAS_NOT_FOUND)
        updated = InvitationRecord(**{**inv.__dict__, "status": INVITE_REVOKED})
        self.store.update_invitation(updated)
        self._audit(ctx, action="member.invite_revoke", target_type="invitation", target_id=invitation_id, result="ok")
        return updated

    def accept_invitation(self, ctx: RequestSecurityContext, *, token: str) -> MembershipRecord:
        import hashlib

        token_hash = hashlib.sha256(token.encode()).hexdigest()
        if token_hash in self._accepted_invites:
            raise SaaSError(SAAS_CONFLICT, message="Invitation already accepted.")
        inv = self.store.get_invitation_by_token_hash(token_hash)
        if inv is None:
            raise SaaSError(SAAS_NOT_FOUND)
        if inv.status != INVITE_PENDING:
            raise SaaSError(SAAS_CONFLICT)
        if inv.expires_at < _utc():
            self.store.update_invitation(InvitationRecord(**{**inv.__dict__, "status": INVITE_EXPIRED}))
            raise SaaSError(SAAS_CONFLICT, message="Invitation expired.")
        existing = self.store.get_active_membership(ctx.user_id, inv.tenant_id)
        if existing:
            self._accepted_invites.add(token_hash)
            return existing
        membership = MembershipRecord(
            membership_id=str(uuid.uuid4()),
            tenant_id=inv.tenant_id,
            user_id=ctx.user_id,
            role=inv.role,
            status=MEMBERSHIP_ACTIVE,
            created_at=_utc(),
        )
        self.store.create_membership(membership)
        self.store.update_invitation(InvitationRecord(**{**inv.__dict__, "status": INVITE_ACCEPTED}))
        self._accepted_invites.add(token_hash)
        self._audit(ctx, action="member.invite_accept", target_type="membership", target_id=membership.membership_id, result="ok", reason=inv.tenant_id)
        return membership

    def change_role(self, ctx: RequestSecurityContext, membership_id: str, *, role: str, expected_version: int) -> MembershipRecord:
        self.access.require(ctx, PERM_MEMBERS_WRITE)
        mem = self.store.get_membership(membership_id)
        if mem is None or mem.tenant_id != ctx.tenant_id:
            raise SaaSError(SAAS_NOT_FOUND)
        if mem.user_id == ctx.user_id and role == ROLE_OWNER:
            raise SaaSError(SAAS_SELF_ESCALATION)
        if mem.role == ROLE_OWNER and role != ROLE_OWNER:
            if self.store.count_active_owners(ctx.tenant_id) <= 1:
                raise SaaSError(SAAS_LAST_OWNER)
        if mem.version != expected_version:
            raise SaaSError(SAAS_CONFLICT, message="Stale membership version.")
        updated = MembershipRecord(**{**mem.__dict__, "role": role})
        result = self.store.update_membership(updated)
        self._audit(ctx, action="member.role_change", target_type="membership", target_id=membership_id, result="ok")
        return result

    def remove_member(self, ctx: RequestSecurityContext, membership_id: str, *, expected_version: int) -> MembershipRecord:
        self.access.require(ctx, PERM_MEMBERS_WRITE)
        mem = self.store.get_membership(membership_id)
        if mem is None or mem.tenant_id != ctx.tenant_id:
            raise SaaSError(SAAS_NOT_FOUND)
        if mem.user_id == ctx.user_id:
            raise SaaSError(SAAS_SELF_ESCALATION, message="Cannot remove self.")
        if mem.role == ROLE_OWNER and self.store.count_active_owners(ctx.tenant_id) <= 1:
            raise SaaSError(SAAS_LAST_OWNER)
        if mem.version != expected_version:
            raise SaaSError(SAAS_CONFLICT)
        updated = MembershipRecord(**{**mem.__dict__, "status": MEMBERSHIP_REMOVED})
        result = self.store.update_membership(updated)
        self._audit(ctx, action="member.remove", target_type="membership", target_id=membership_id, result="ok")
        return result

    def get_entitlements(self, ctx: RequestSecurityContext) -> dict:
        self.access.require_product_read(ctx)
        sub = self.store.get_subscription_for_tenant(ctx.tenant_id)
        ent = self.entitlements.resolve(tenant_id=ctx.tenant_id, subscription=sub)
        used = self.metering.usage_for_tenant(ctx.tenant_id)
        return {**ent.__dict__, "usage": {"requests_per_month": used}}

    def enforce_entitlement(self, ctx: RequestSecurityContext, *, feature: str, meter: str | None = None, idempotency_key: str | None = None) -> None:
        sub = self.store.get_subscription_for_tenant(ctx.tenant_id)
        ent = self.entitlements.resolve(tenant_id=ctx.tenant_id, subscription=sub)
        if not ent.allows_feature(feature):
            raise SaaSError(SAAS_ENTITLEMENT_DENIED)
        if meter:
            limit = ent.quota_limit(meter)
            if limit is None:
                raise SaaSError(SAAS_ENTITLEMENT_DENIED)
            if idempotency_key:
                if not self.metering.try_reserve_request(
                    tenant_id=ctx.tenant_id,
                    user_id=ctx.user_id,
                    limit=limit,
                    idempotency_key=idempotency_key,
                ):
                    raise SaaSError(SAAS_QUOTA_EXCEEDED)
            else:
                used = self.metering.usage_for_tenant(ctx.tenant_id, meter_id=meter)
                if not self.entitlements.check_quota(ent, meter, used):
                    raise SaaSError(SAAS_QUOTA_EXCEEDED)

    def create_checkout(self, ctx: RequestSecurityContext, *, plan_id: str, plan_version: str, idempotency_key: str) -> dict:
        self.access.require(ctx, PERM_BILLING_WRITE)
        return self.billing.create_checkout(tenant_id=ctx.tenant_id, plan_id=plan_id, plan_version=plan_version, idempotency_key=idempotency_key)

    def billing_status(self, ctx: RequestSecurityContext) -> dict:
        self.access.require(ctx, PERM_BILLING_READ)
        sub = self.store.get_subscription_for_tenant(ctx.tenant_id)
        invoices, total = self.store.list_invoices(ctx.tenant_id, limit=20)
        return {
            "subscription": sub.__dict__ if sub else None,
            "invoices": [i.__dict__ for i in invoices],
            "invoice_total": total,
        }

    def cancel_subscription(self, ctx: RequestSecurityContext, *, at_period_end: bool = True) -> dict:
        self.access.require(ctx, PERM_BILLING_WRITE)
        result = self.billing.cancel_subscription(tenant_id=ctx.tenant_id, at_period_end=at_period_end)
        self._audit(ctx, action="billing.cancel", target_type="subscription", target_id=result["subscription_id"], result="ok")
        return result

    def privacy_inventory(self, ctx: RequestSecurityContext) -> list[dict]:
        self.access.require(ctx, PERM_PRIVACY_READ)
        return [d.__dict__ for d in self.privacy.inventory()]

    def request_export(self, ctx: RequestSecurityContext) -> dict:
        self.access.require(ctx, PERM_PRIVACY_WRITE)
        job = self.privacy.request_export(tenant_id=ctx.tenant_id, user_id=ctx.user_id)
        self._audit(ctx, action="privacy.export", target_type="export_job", target_id=job.job_id, result="ok")
        return job.__dict__

    def request_account_deletion(self, ctx: RequestSecurityContext) -> dict:
        self.access.require(ctx, PERM_PRIVACY_WRITE)
        self.ensure_user(ctx)
        mem = self.store.get_active_membership(ctx.user_id, ctx.tenant_id)
        if mem and mem.role == ROLE_OWNER and self.store.count_active_owners(ctx.tenant_id) <= 1:
            tenant = self.store.get_tenant(ctx.tenant_id)
            if tenant and tenant.status == TENANT_ACTIVE:
                raise SaaSError(SAAS_LAST_OWNER, message="Transfer ownership or delete tenant first.")
        job, token = self.privacy.request_account_deletion(tenant_id=ctx.tenant_id, user_id=ctx.user_id, actor_ref=ctx.actor_ref())
        return {"job_id": job.job_id, "status": job.status, "confirmation_token": token}

    def confirm_deletion(self, ctx: RequestSecurityContext, job_id: str, *, confirmation_token: str) -> dict:
        self.access.require(ctx, PERM_PRIVACY_WRITE)
        job = self.privacy.confirm_deletion(job_id, confirmation_token=confirmation_token, actor_ref=ctx.actor_ref())
        self._audit(ctx, action="privacy.delete_confirm", target_type="deletion_job", target_id=job_id, result="ok")
        return job.__dict__

    def request_tenant_deletion(self, ctx: RequestSecurityContext) -> dict:
        self.access.require(ctx, PERM_PRIVACY_WRITE)
        if not self.access.is_owner(ctx):
            raise SaaSError(SAAS_FORBIDDEN, message="Owner required for tenant deletion.")
        sub = self.store.get_subscription_for_tenant(ctx.tenant_id)
        if sub and sub.status in {"ACTIVE", "TRIALING", "PAST_DUE"}:
            self.billing.cancel_subscription(tenant_id=ctx.tenant_id, at_period_end=False)
        job, token = self.privacy.request_tenant_deletion(
            tenant_id=ctx.tenant_id, user_id=ctx.user_id, actor_ref=ctx.actor_ref()
        )
        self._audit(ctx, action="privacy.tenant_delete", target_type="tenant", target_id=ctx.tenant_id, result="requested")
        return {"job_id": job.job_id, "status": job.status, "confirmation_token": token}
