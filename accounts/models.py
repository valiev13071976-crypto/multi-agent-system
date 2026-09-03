"""Canonical accounts / access / compliance models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Product roles (administrative authority) — NOT access types
ROLE_OWNER = "OWNER"
ROLE_ADMIN = "ADMIN"
ROLE_USER = "USER"
# Backward-compatible alias used by existing saas_product memberships
ROLE_MEMBER = "MEMBER"

PRODUCT_ROLES = frozenset({ROLE_OWNER, ROLE_ADMIN, ROLE_USER, ROLE_MEMBER})

# Account status
STATUS_ACTIVE = "ACTIVE"
STATUS_DISABLED = "DISABLED"

# Access types (commercial) — never roles
ACCESS_TRIAL = "TRIAL"
ACCESS_PAID = "PAID"
ACCESS_COMPLIMENTARY = "COMPLIMENTARY"
ACCESS_NONE = "NONE"

ACCESS_STATUS_ACTIVE = "ACTIVE"
ACCESS_STATUS_EXPIRED = "EXPIRED"
ACCESS_STATUS_REVOKED = "REVOKED"
ACCESS_STATUS_PENDING = "PENDING"

# Subscription lifecycle (align with saas_product where possible)
SUB_PENDING = "PENDING"
SUB_ACTIVE = "ACTIVE"
SUB_PAST_DUE = "PAST_DUE"
SUB_CANCELLED = "CANCELLED"
SUB_EXPIRED = "EXPIRED"

# Canonical product entitlements
ENT_CHAT_ACCESS = "chat_access"
ENT_WEB_SEARCH = "web_search"
ENT_FILE_UPLOAD = "file_upload"
ENT_EXCEL_ANALYSIS = "excel_analysis"
ENT_IMAGE_GENERATION = "image_generation"
ENT_BUSINESS_ASSISTANT = "business_assistant"
ENT_MARKETPLACE_TOOLS = "marketplace_tools"
ENT_SCHEDULED_AUTOMATION = "scheduled_automation"
ENT_VOICE = "voice"
ENT_TELEGRAM = "telegram"
ENT_ADVANCED_ANALYTICS = "advanced_analytics"

ALL_ENTITLEMENTS = frozenset(
    {
        ENT_CHAT_ACCESS,
        ENT_WEB_SEARCH,
        ENT_FILE_UPLOAD,
        ENT_EXCEL_ANALYSIS,
        ENT_IMAGE_GENERATION,
        ENT_BUSINESS_ASSISTANT,
        ENT_MARKETPLACE_TOOLS,
        ENT_SCHEDULED_AUTOMATION,
        ENT_VOICE,
        ENT_TELEGRAM,
        ENT_ADVANCED_ANALYTICS,
    }
)

# Legal document types
DOC_TERMS = "TERMS_OF_SERVICE"
DOC_PRIVACY = "PRIVACY_POLICY"
DOC_PERSONAL_DATA = "PERSONAL_DATA_POLICY"
DOC_CONSENT_TEXT = "CONSENT_TEXT"
DOC_AI_DISCLOSURE = "AI_DISCLOSURE"

# User decision types
DEC_PERSONAL_DATA = "PERSONAL_DATA_PROCESSING"
DEC_MARKETING = "MARKETING_COMMUNICATION"
DEC_OPTIONAL_ANALYTICS = "OPTIONAL_ANALYTICS"
DEC_TERMS = "TERMS_ACCEPTANCE"
DEC_PRIVACY_ACK = "PRIVACY_POLICY_ACKNOWLEDGMENT"

# Cookie classification
COOKIE_STRICTLY_NECESSARY = "STRICTLY_NECESSARY"
COOKIE_FUNCTIONAL = "FUNCTIONAL"
COOKIE_ANALYTICS = "ANALYTICS"
COOKIE_MARKETING = "MARKETING"
COOKIE_UNKNOWN = "UNKNOWN"

AGE_POLICY_STATUS = "REQUIRES_PRODUCT_LEGAL_DECISION"

SESSION_COOKIE_NAME = "panda_session"
CSRF_COOKIE_NAME = "panda_csrf"
CSRF_HEADER_NAME = "X-CSRF-Token"


@dataclass
class HumanUserRecord:
    user_id: str
    tenant_id: str
    username: str
    normalized_username: str
    password_hash: str
    role: str
    status: str
    created_at: str
    updated_at: str
    last_login_at: str = ""
    password_changed_at: str = ""
    email: str = ""
    display_name: str = ""
    is_bootstrap_owner: bool = False
    protected: bool = False


@dataclass
class SessionRecord:
    session_id: str
    user_id: str
    tenant_id: str
    created_at: str
    expires_at: str
    last_seen_at: str
    revoked_at: str = ""
    csrf_token: str = ""
    auth_method: str = "password"


@dataclass
class TrialRecord:
    trial_id: str
    tenant_id: str
    user_id: str
    plan_id: str
    trial_started_at: str
    trial_ends_at: str
    created_at: str
    status: str = ACCESS_STATUS_ACTIVE


@dataclass
class ComplimentaryAccessRecord:
    grant_id: str
    tenant_id: str
    user_id: str
    plan_id: str
    access_started_at: str
    access_until: str  # empty string means unlimited only when unlimited=True
    unlimited: bool
    reason: str
    granted_by: str
    created_at: str
    revoked_at: str = ""


@dataclass
class PlanDefinition:
    plan_id: str
    code: str
    name: str
    status: str
    display_order: int
    entitlements: frozenset[str]
    limits: dict[str, int]
    created_at: str = ""
    updated_at: str = ""


@dataclass
class AccessDecision:
    decision: str  # ALLOW / DENY / DEGRADE
    reason_code: str
    access_type: str
    access_status: str
    plan_id: str
    role: str
    entitlements: frozenset[str] = frozenset()
    trial_ends_at: str = ""
    paid_until: str = ""
    usage_summary: dict[str, Any] = field(default_factory=dict)
    details: dict[str, Any] = field(default_factory=dict)

    def allowed(self) -> bool:
        return self.decision == "ALLOW"


@dataclass
class PolicyDocument:
    document_id: str
    document_type: str
    version: str
    effective_from: str
    status: str
    content_reference: str
    title: str
    created_at: str
    draft_requires_legal_review: bool = True


@dataclass
class UserDecisionRecord:
    decision_id: str
    user_id: str
    tenant_id: str
    document_type: str
    document_version: str
    decision_type: str
    decision: str  # ACCEPTED / DECLINED / WITHDRAWN
    timestamp: str
    source: str
    withdrawn_at: str = ""


@dataclass
class PaymentMethodControl:
    control_id: str
    tenant_id: str
    user_id: str
    provider: str
    provider_reference: str
    usage_status: str  # PAYMENT_METHOD_USAGE_ALLOWED | PAYMENT_METHOD_USAGE_REVOKED
    created_at: str
    revoked_at: str = ""
    revocation_source: str = ""


@dataclass
class DeletionRequestRecord:
    request_id: str
    tenant_id: str
    user_id: str
    status: str
    created_at: str
    updated_at: str
    completed_at: str = ""
    retention_hold: bool = False


@dataclass
class AccountsAuditEvent:
    event_id: str
    timestamp: str
    actor_id: str
    target_id: str
    tenant_id: str
    action: str
    result: str
    correlation_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
