"""Block 13 canonical B2B domain contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

# Source classes
SOURCE_SUPPLIER_FILE = "SUPPLIER_FILE"
SOURCE_SUPPLIER_API = "SUPPLIER_API"
SOURCE_SUPPLIER_SITE = "SUPPLIER_SITE"
SOURCE_MANUAL = "MANUAL"
SOURCE_DOCUMENT = "DOCUMENT"
SOURCE_EMAIL_ATTACHMENT = "EMAIL_ATTACHMENT"
SOURCE_UNKNOWN = "UNKNOWN"

# Match states
MATCH_CONFIRMED = "CONFIRMED"
MATCH_CANDIDATE = "CANDIDATE"
MATCH_AMBIGUOUS = "AMBIGUOUS"
MATCH_UNMATCHED = "UNMATCHED"
MATCH_CONFLICT = "CONFLICT"

# VAT
VAT_INCLUDED = "VAT_INCLUDED"
VAT_EXCLUDED = "VAT_EXCLUDED"
NO_VAT = "NO_VAT"
VAT_UNKNOWN = "UNKNOWN"

# Customer verification
CUSTOMER_UNVERIFIED = "UNVERIFIED"
CUSTOMER_CANDIDATE = "CANDIDATE"
CUSTOMER_VERIFIED = "VERIFIED"

# Conversation states
CONV_NEW = "NEW"
CONV_QUALIFYING = "QUALIFYING"
CONV_PRODUCT_SEARCH = "PRODUCT_SEARCH"
CONV_QUANTITY_REQUIRED = "QUANTITY_REQUIRED"
CONV_QUOTE_PREPARATION = "QUOTE_PREPARATION"
CONV_QUOTE_READY = "QUOTE_READY"
CONV_AWAITING_CONFIRMATION = "AWAITING_CONFIRMATION"
CONV_ORDER_DRAFTED = "ORDER_DRAFTED"
CONV_HUMAN_HANDOFF = "HUMAN_HANDOFF"
CONV_CLOSED = "CLOSED"

# Quote approval
QUOTE_ALLOW = "ALLOW"
QUOTE_REQUIRE_APPROVAL = "REQUIRE_APPROVAL"
QUOTE_DENY = "DENY"

# Assistant actions
ACTION_ANSWER = "ANSWER"
ACTION_ASK_CLARIFICATION = "ASK_CLARIFICATION"
ACTION_SEARCH_PRODUCT = "SEARCH_PRODUCT"
ACTION_CHECK_AVAILABILITY = "CHECK_AVAILABILITY"
ACTION_COMPARE_WHOLESALE = "COMPARE_WHOLESALE"
ACTION_CREATE_QUOTE = "CREATE_QUOTE"
ACTION_REQUEST_QUOTE_APPROVAL = "REQUEST_QUOTE_APPROVAL"
ACTION_SEND_QUOTE = "SEND_QUOTE"
ACTION_CREATE_ORDER_DRAFT = "CREATE_ORDER_DRAFT"
ACTION_REQUEST_ORDER_CONFIRMATION = "REQUEST_ORDER_CONFIRMATION"
ACTION_SUBMIT_ORDER = "SUBMIT_ORDER"
ACTION_HANDOFF = "HANDOFF"

ALLOWED_ASSISTANT_ACTIONS = frozenset(
    {
        ACTION_ANSWER,
        ACTION_ASK_CLARIFICATION,
        ACTION_SEARCH_PRODUCT,
        ACTION_CHECK_AVAILABILITY,
        ACTION_COMPARE_WHOLESALE,
        ACTION_CREATE_QUOTE,
        ACTION_REQUEST_QUOTE_APPROVAL,
        ACTION_SEND_QUOTE,
        ACTION_CREATE_ORDER_DRAFT,
        ACTION_REQUEST_ORDER_CONFIRMATION,
        ACTION_SUBMIT_ORDER,
        ACTION_HANDOFF,
    }
)

# Data scopes
SCOPE_INTERNAL = "INTERNAL"
SCOPE_CUSTOMER = "CUSTOMER"

# Offer freshness
OFFER_FRESH = "FRESH"
OFFER_STALE = "STALE"

# Job status
JOB_PENDING = "PENDING"
JOB_RUNNING = "RUNNING"
JOB_COMPLETED = "COMPLETED"
JOB_FAILED = "FAILED"
JOB_CANCELLED = "CANCELLED"


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class B2BProvenance:
    tenant_id: str
    supplier_id: str
    source_class: str
    source_artifact_id: str
    source_row_ref: str
    observed_at: str
    ingested_at: str
    currency: str
    vat_status: str
    source_version_hash: str
    execution_id: str = ""


@dataclass
class Supplier:
    supplier_id: str
    tenant_id: str
    name: str
    status: str = "ACTIVE"
    source_bindings: tuple[str, ...] = ()
    created_at: str = field(default_factory=_utc)


@dataclass
class SupplierPriceListVersion:
    version_id: str
    tenant_id: str
    supplier_id: str
    source_class: str
    artifact_hash: str
    currency: str
    vat_status: str
    row_count: int
    observed_at: str
    ingested_at: str
    schema_profile: str = ""


@dataclass
class WholesaleOfferVersion:
    offer_id: str
    version_id: str
    tenant_id: str
    supplier_id: str
    price_list_version_id: str
    supplier_sku: str
    description: str
    unit_price: str
    currency: str
    vat_status: str
    moq: int | None
    quantity_tiers: tuple[dict[str, Any], ...]
    available_quantity: int | None
    lead_time_days: int | None
    match_state: str
    product_id: str = ""
    product_version_id: str = ""
    match_candidates: tuple[str, ...] = ()
    freshness: str = OFFER_FRESH
    provenance: B2BProvenance | None = None


@dataclass
class WholesaleProductMatch:
    match_id: str
    tenant_id: str
    offer_version_id: str
    product_id: str
    product_version_id: str
    state: str
    evidence: tuple[str, ...] = ()
    candidates: tuple[str, ...] = ()


@dataclass
class WholesaleComparison:
    comparison_id: str
    tenant_id: str
    product_id: str
    requested_quantity: int
    best_offer_id: str
    ranking_reason: str
    components: tuple[str, ...]
    offers: tuple[dict[str, Any], ...]


@dataclass
class WholesalePriceChange:
    change_id: str
    tenant_id: str
    supplier_id: str
    offer_id: str
    old_price: str
    new_price: str
    delta_abs: str
    delta_pct: str
    direction: str


@dataclass
class B2BCustomer:
    customer_id: str
    tenant_id: str
    display_name: str
    verification_state: str = CUSTOMER_UNVERIFIED
    price_tier: str = ""
    discount_ceiling: str = ""
    currency: str = ""
    vat_status: str = VAT_UNKNOWN
    deleted: bool = False
    created_at: str = field(default_factory=_utc)


@dataclass
class B2BConversation:
    conversation_id: str
    tenant_id: str
    customer_id: str
    chat_binding_id: str
    state: str = CONV_NEW
    created_at: str = field(default_factory=_utc)
    updated_at: str = field(default_factory=_utc)


@dataclass
class B2BConversationMessage:
    message_id: str
    tenant_id: str
    conversation_id: str
    direction: str
    text: str
    trust_level: str = "UNTRUSTED_USER_INPUT"
    created_at: str = field(default_factory=_utc)


@dataclass
class B2BInquiry:
    inquiry_id: str
    tenant_id: str
    conversation_id: str
    customer_id: str
    state: str = "OPEN"
    created_at: str = field(default_factory=_utc)


@dataclass
class B2BInquiryItem:
    item_id: str
    tenant_id: str
    inquiry_id: str
    product_query: str
    quantity: int | None
    product_id: str = ""
    match_state: str = MATCH_UNMATCHED
    candidates: tuple[str, ...] = ()


@dataclass
class CommercialQuoteVersion:
    quote_id: str
    version_id: str
    tenant_id: str
    customer_id: str
    conversation_id: str
    inquiry_id: str
    currency: str
    vat_status: str
    subtotal: str
    discount: str
    vat_amount: str
    total: str
    approval_status: str = QUOTE_ALLOW
    sent: bool = False
    valid_until: str = ""
    pricing_policy_version: str = "v1"
    stale: bool = False
    items: tuple[dict[str, Any], ...] = ()
    created_at: str = field(default_factory=_utc)


@dataclass
class B2BOrderDraft:
    draft_id: str
    tenant_id: str
    customer_id: str
    conversation_id: str
    quote_id: str
    quote_version_id: str
    confirmation_token: str = ""
    confirmed: bool = False
    submitted: bool = False
    platform_order_id: str = ""
    created_at: str = field(default_factory=_utc)


@dataclass
class TelegramAccountBinding:
    binding_id: str
    tenant_id: str
    bot_id: str
    secret_ref: str = ""


@dataclass
class TelegramChatBinding:
    binding_id: str
    tenant_id: str
    account_binding_id: str
    chat_id: str
    customer_id: str = ""
    conversation_id: str = ""


@dataclass
class TelegramMessageReceipt:
    receipt_id: str
    tenant_id: str
    chat_binding_id: str
    provider_message_id: str
    operation: str
    idempotency_key: str
    status: str
    sent_at: str = field(default_factory=_utc)


@dataclass
class AssistantProposal:
    proposal_id: str
    tenant_id: str
    conversation_id: str
    action: str
    payload: dict[str, Any]
    data_scope: str = SCOPE_CUSTOMER
    evidence_refs: tuple[str, ...] = ()


@dataclass
class B2BJob:
    job_id: str
    tenant_id: str
    operation: str
    supplier_id: str
    source_artifact_id: str
    status: str = JOB_PENDING
    checkpoint: int = 0
    processed: int = 0
    matched: int = 0
    ambiguous: int = 0
    unmatched: int = 0
    failed: int = 0
    price_list_version_id: str = ""
    created_at: str = field(default_factory=_utc)
    updated_at: str = field(default_factory=_utc)


def money_str(value: Decimal | str | int | float) -> str:
    if isinstance(value, Decimal):
        return format(value, "f")
    return str(value)


def parse_money(value: str | Decimal | int | float) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))
