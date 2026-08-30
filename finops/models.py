from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal


CURRENCY_USD = "USD"

UNKNOWN_COST_ALLOW = "allow"
UNKNOWN_COST_DENY = "deny"
UNKNOWN_COST_POLICIES = (UNKNOWN_COST_ALLOW, UNKNOWN_COST_DENY)
DEFAULT_UNKNOWN_COST_POLICY = UNKNOWN_COST_ALLOW


@dataclass(frozen=True)
class UsageRecord:
    task_id: str
    provider_id: str
    model_id: str
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    estimated_cost: Decimal | None
    currency: str
    timestamp: datetime
    # Attribution (optional defaults preserve existing call sites)
    workflow_id: str = ""
    tenant_id: str = ""
    user_id: str = ""
    request_id: str = ""
    # P1-USAGE ownership (envelope-sourced on live Core path; empty for legacy)
    actor_ref: str = ""
    execution_id: str = ""


@dataclass(frozen=True)
class PriceQuote:
    provider_id: str
    model_id: str
    input_price_per_million: Decimal
    output_price_per_million: Decimal
    currency: str
    enabled: bool


@dataclass(frozen=True)
class BudgetLimits:
    per_task: Decimal | None
    per_day: Decimal | None
    per_month: Decimal | None
    unknown_cost_policy: str = DEFAULT_UNKNOWN_COST_POLICY


@dataclass(frozen=True)
class BudgetDecision:
    allowed: bool
    reason: str
    estimated_cost: Decimal | None
    currency: str


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
