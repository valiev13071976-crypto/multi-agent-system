"""SaaS product domain models."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

# Account states
ACCOUNT_ACTIVE = "ACTIVE"
ACCOUNT_SUSPENDED = "SUSPENDED"
ACCOUNT_PENDING_DELETION = "PENDING_DELETION"
ACCOUNT_DELETED = "DELETED"

# Tenant states
TENANT_ACTIVE = "ACTIVE"
TENANT_SUSPENDED = "SUSPENDED"
TENANT_PENDING_DELETION = "PENDING_DELETION"
TENANT_DELETED = "DELETED"

# Membership states
MEMBERSHIP_ACTIVE = "ACTIVE"
MEMBERSHIP_REMOVED = "REMOVED"

# Invitation states
INVITE_PENDING = "PENDING"
INVITE_ACCEPTED = "ACCEPTED"
INVITE_REVOKED = "REVOKED"
INVITE_EXPIRED = "EXPIRED"

# Subscription states
SUB_TRIALING = "TRIALING"
SUB_ACTIVE = "ACTIVE"
SUB_PAST_DUE = "PAST_DUE"
SUB_CANCEL_PENDING = "CANCEL_PENDING"
SUB_CANCELED = "CANCELED"
SUB_SUSPENDED = "SUSPENDED"
SUB_FREE = "FREE"

# Invoice states
INVOICE_OPEN = "OPEN"
INVOICE_PAID = "PAID"
INVOICE_VOID = "VOID"

# Privacy job states
PRIVACY_REQUESTED = "REQUESTED"
PRIVACY_CONFIRMATION_REQUIRED = "CONFIRMATION_REQUIRED"
PRIVACY_IN_PROGRESS = "IN_PROGRESS"
PRIVACY_COMPLETED = "COMPLETED"
PRIVACY_FAILED = "FAILED"
PRIVACY_CANCELED = "CANCELED"

# Data classification
CLASS_EXPORTABLE = "EXPORTABLE"
CLASS_DELETABLE = "DELETABLE"
CLASS_TOMBSTONE = "TOMBSTONE_REQUIRED"
CLASS_RETENTION = "RETENTION_REQUIRED"
CLASS_AGGREGATED = "AGGREGATED"


@dataclass
class UserAccount:
    user_id: str
    status: str
    display_name: str = ""
    email: str = ""
    created_at: str = ""
    updated_at: str = ""
    version: int = 1


@dataclass
class TenantRecord:
    tenant_id: str
    name: str
    status: str
    owner_user_id: str
    created_at: str = ""
    updated_at: str = ""
    version: int = 1


@dataclass
class MembershipRecord:
    membership_id: str
    tenant_id: str
    user_id: str
    role: str
    status: str
    created_at: str = ""
    version: int = 1


@dataclass
class InvitationRecord:
    invitation_id: str
    tenant_id: str
    email: str
    role: str
    invited_by: str
    token_hash: str
    status: str
    expires_at: str
    created_at: str = ""
    version: int = 1


@dataclass
class PlanRecord:
    plan_id: str
    plan_version: str
    name: str
    status: str
    billing_interval: str
    currency: str
    price_minor: int
    entitlements: dict[str, Any] = field(default_factory=dict)
    quotas: dict[str, int] = field(default_factory=dict)
    effective_from: str = ""


@dataclass
class SubscriptionRecord:
    subscription_id: str
    tenant_id: str
    provider: str
    provider_customer_ref: str
    provider_subscription_ref: str
    plan_id: str
    plan_version: str
    status: str
    current_period_start: str = ""
    current_period_end: str = ""
    cancel_at_period_end: bool = False
    created_at: str = ""
    updated_at: str = ""
    version: int = 1


@dataclass
class InvoiceRecord:
    invoice_id: str
    tenant_id: str
    subscription_id: str
    provider_invoice_ref: str
    amount_minor: int
    currency: str
    status: str
    period_start: str = ""
    period_end: str = ""
    created_at: str = ""
    paid_at: str | None = None


@dataclass
class BillingEventRecord:
    event_id: str
    provider: str
    event_type: str
    tenant_id: str
    subscription_id: str
    payload_hash: str
    processed_at: str
    result: str


@dataclass
class EffectiveEntitlements:
    tenant_id: str
    plan_id: str
    plan_version: str
    subscription_status: str
    features: frozenset[str] = frozenset()
    quotas: dict[str, int] = field(default_factory=dict)
    generated_at: str = ""

    def allows_feature(self, feature: str) -> bool:
        if self.subscription_status not in {SUB_ACTIVE, SUB_TRIALING, SUB_FREE}:
            return False
        return feature in self.features or "all" in self.features

    def quota_limit(self, meter: str) -> int | None:
        return self.quotas.get(meter)


@dataclass
class UsageMeterRecord:
    meter_id: str
    tenant_id: str
    user_id: str
    quantity: int
    period_key: str
    idempotency_key: str
    created_at: str = ""


@dataclass
class PrivacyExportJob:
    job_id: str
    tenant_id: str
    user_id: str
    status: str
    artifact_ref: str = ""
    manifest_version: str = "1"
    created_at: str = ""
    completed_at: str | None = None
    error_code: str | None = None


@dataclass
class DeletionJob:
    job_id: str
    scope: str
    tenant_id: str
    user_id: str
    status: str
    confirmation_token_hash: str = ""
    phases_completed: tuple[str, ...] = ()
    created_at: str = ""
    completed_at: str | None = None
    error_code: str | None = None


@dataclass
class DataClassInfo:
    data_class: str
    classification: str
    description: str = ""


@dataclass
class ProductAuditEvent:
    event_id: str
    timestamp: str
    actor_ref: str
    tenant_id: str
    action: str
    target_type: str
    target_id: str
    result: str
    reason: str | None = None


def money_from_minor(amount_minor: int, currency: str = "USD") -> str:
    return f"{Decimal(amount_minor) / Decimal(100):.2f} {currency}"
