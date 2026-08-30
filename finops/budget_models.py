"""Advanced Budget Guardrails models (P11).

Note: finops.models.BudgetDecision remains the legacy allow/deny check result
from FinOpsService.check_budget. This module defines the guard action decision
(CONTINUE / SKIP_MODEL / DEGRADE / TERMINATE) under the same conceptual name
for the guard API.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from types import MappingProxyType
from typing import Mapping

from autonomy.models import sanitize_metadata


BUDGET_POLICY_VERSION = "1.0.0"

DECISION_CONTINUE = "CONTINUE"
DECISION_SKIP_MODEL = "SKIP_MODEL"
DECISION_DEGRADE = "DEGRADE"
DECISION_TERMINATE = "TERMINATE"
BUDGET_DECISIONS = (
    DECISION_CONTINUE,
    DECISION_SKIP_MODEL,
    DECISION_DEGRADE,
    DECISION_TERMINATE,
)

# Explicit precedence — do not rely on enum/string ordering.
# TERMINATE > DEGRADE > SKIP_MODEL > CONTINUE
BUDGET_DECISION_PRECEDENCE = {
    DECISION_CONTINUE: 0,
    DECISION_SKIP_MODEL: 1,
    DECISION_DEGRADE: 2,
    DECISION_TERMINATE: 3,
}


def merge_budget_decision(current: str, incoming: str) -> str:
    """Return the stronger of two budget decisions by canonical precedence."""

    cur = current if current in BUDGET_DECISION_PRECEDENCE else DECISION_CONTINUE
    nxt = incoming if incoming in BUDGET_DECISION_PRECEDENCE else DECISION_CONTINUE
    if BUDGET_DECISION_PRECEDENCE[nxt] > BUDGET_DECISION_PRECEDENCE[cur]:
        return nxt
    return cur

SCOPE_GLOBAL = "global"
SCOPE_TENANT = "tenant"
SCOPE_AGENT = "agent"
SCOPE_TASK = "task"
SCOPE_PROVIDER = "provider"
SCOPE_MODEL = "model"
SCOPE_DAILY = "daily"
SCOPE_MONTHLY = "monthly"
BUDGET_SCOPES = (
    SCOPE_GLOBAL,
    SCOPE_TENANT,
    SCOPE_AGENT,
    SCOPE_TASK,
    SCOPE_PROVIDER,
    SCOPE_MODEL,
    SCOPE_DAILY,
    SCOPE_MONTHLY,
)

RES_RESERVED = "reserved"
RES_COMMITTED = "committed"
RES_RELEASED = "released"
RES_EXPIRED = "expired"
RES_RECONCILED = "reconciled"
RES_UNCERTAIN = "uncertain"
RESERVATION_STATUSES = (
    RES_RESERVED,
    RES_COMMITTED,
    RES_RELEASED,
    RES_EXPIRED,
    RES_RECONCILED,
    RES_UNCERTAIN,
)

ACTIVE_RESERVATION_STATUSES = frozenset({RES_RESERVED, RES_UNCERTAIN})


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _meta(value) -> Mapping[str, object]:
    return MappingProxyType(sanitize_metadata(value or {}))


def _dec(value) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


@dataclass(frozen=True)
class BudgetPolicy:
    """Deterministic budget policy row (no AI)."""

    policy_id: str
    scope: str
    scope_key: str = ""
    hard_limit: Decimal | None = None
    soft_limit: Decimal | None = None
    currency: str = "USD"
    window: str = "none"
    degrade_threshold: Decimal | None = None
    enabled: bool = True
    policy_version: str = BUDGET_POLICY_VERSION
    version: int = 1
    metadata_safe: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        if self.scope not in BUDGET_SCOPES:
            raise ValueError(f"invalid_budget_scope:{self.scope}")
        object.__setattr__(self, "hard_limit", _dec(self.hard_limit))
        object.__setattr__(self, "soft_limit", _dec(self.soft_limit))
        object.__setattr__(self, "degrade_threshold", _dec(self.degrade_threshold))
        object.__setattr__(self, "metadata_safe", _meta(self.metadata_safe))


@dataclass(frozen=True)
class BudgetReservation:
    reservation_id: str
    scope_refs: tuple[str, ...]
    task_id: str
    provider: str
    model: str
    estimated_cost: Decimal
    currency: str
    status: str
    created_at: datetime
    expires_at: datetime
    agent_id: str | None = None
    committed_at: datetime | None = None
    released_at: datetime | None = None
    actual_cost: Decimal | None = None
    usage_record_key: str | None = None
    metadata_safe: Mapping[str, object] = field(default_factory=dict)
    version: int = 1

    def __post_init__(self):
        if self.status not in RESERVATION_STATUSES:
            raise ValueError(f"invalid_reservation_status:{self.status}")
        object.__setattr__(self, "scope_refs", tuple(self.scope_refs))
        object.__setattr__(self, "estimated_cost", Decimal(str(self.estimated_cost)))
        if self.actual_cost is not None:
            object.__setattr__(self, "actual_cost", Decimal(str(self.actual_cost)))
        object.__setattr__(self, "metadata_safe", _meta(self.metadata_safe))


@dataclass(frozen=True)
class BudgetDecision:
    """BudgetGuard action decision: CONTINUE / SKIP_MODEL / DEGRADE / TERMINATE."""

    decision: str
    reason_code: str
    scope: str
    requested_cost: Decimal | None
    reserved_cost: Decimal | None
    remaining_budget: Decimal | None
    hard_limit: Decimal | None = None
    soft_limit: Decimal | None = None
    recommended_provider: str | None = None
    recommended_model: str | None = None
    max_affordable_cost: Decimal | None = None
    excluded_providers: tuple[str, ...] = ()
    excluded_models: tuple[str, ...] = ()
    scope_reasons: tuple[str, ...] = ()
    reservation_id: str | None = None
    metadata_safe: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        if self.decision not in BUDGET_DECISIONS:
            raise ValueError(f"invalid_budget_decision:{self.decision}")
        object.__setattr__(self, "excluded_providers", tuple(self.excluded_providers))
        object.__setattr__(self, "excluded_models", tuple(self.excluded_models))
        object.__setattr__(self, "scope_reasons", tuple(self.scope_reasons))
        object.__setattr__(self, "metadata_safe", _meta(self.metadata_safe))
        for name in (
            "requested_cost",
            "reserved_cost",
            "remaining_budget",
            "hard_limit",
            "soft_limit",
            "max_affordable_cost",
        ):
            value = getattr(self, name)
            if value is not None and not isinstance(value, Decimal):
                object.__setattr__(self, name, Decimal(str(value)))


@dataclass(frozen=True)
class BudgetForecast:
    estimated_remaining_calls: int | None
    projected_window_spend: Decimal | None
    projected_exhaustion: datetime | None
    sample_size: int = 0
    metadata_safe: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        if self.projected_window_spend is not None and not isinstance(
            self.projected_window_spend, Decimal
        ):
            object.__setattr__(
                self, "projected_window_spend", Decimal(str(self.projected_window_spend))
            )
        object.__setattr__(self, "metadata_safe", _meta(self.metadata_safe))


@dataclass(frozen=True)
class BudgetConstraints:
    """Read-only router-facing budget contract (no reservation state).

    Built by BudgetGuard for ModelRouter selection. ``excluded_providers`` and
    optional ``candidate_costs`` / ``max_affordable_cost`` drive eligibility.
    ``preferred_cheaper`` is soft-DEGRADE preference: when set, selection is
    restricted to those providers (monotonic — no silent re-upgrade).
    ``skipped_providers`` names candidates excluded by SKIP_MODEL.
    """

    max_affordable_cost: Decimal | None = None
    remaining_budget: Decimal | None = None
    excluded_providers: tuple[str, ...] = ()
    excluded_models: tuple[str, ...] = ()
    skipped_providers: tuple[str, ...] = ()
    preferred_cheaper: tuple[tuple[str, str], ...] = ()
    candidate_costs: Mapping[str, Decimal | None] = field(default_factory=dict)
    unknown_cost_policy: str | None = None
    decision: str = DECISION_CONTINUE
    reason_code: str = "within_budget"
    metadata_safe: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "excluded_providers", tuple(self.excluded_providers))
        object.__setattr__(self, "excluded_models", tuple(self.excluded_models))
        skipped = tuple(self.skipped_providers or ())
        if not skipped and self.excluded_providers:
            # Compatibility: hard exclusions are SKIP_MODEL candidates.
            skipped = tuple(self.excluded_providers)
        object.__setattr__(self, "skipped_providers", skipped)
        object.__setattr__(self, "preferred_cheaper", tuple(self.preferred_cheaper))
        if self.decision not in BUDGET_DECISIONS:
            object.__setattr__(self, "decision", DECISION_CONTINUE)
        if self.max_affordable_cost is not None and not isinstance(
            self.max_affordable_cost, Decimal
        ):
            object.__setattr__(
                self, "max_affordable_cost", Decimal(str(self.max_affordable_cost))
            )
        if self.remaining_budget is not None and not isinstance(
            self.remaining_budget, Decimal
        ):
            object.__setattr__(
                self, "remaining_budget", Decimal(str(self.remaining_budget))
            )
        costs: dict[str, Decimal | None] = {}
        for key, value in dict(self.candidate_costs or {}).items():
            if value is None:
                costs[str(key)] = None
            elif isinstance(value, Decimal):
                costs[str(key)] = value
            else:
                costs[str(key)] = Decimal(str(value))
        object.__setattr__(self, "candidate_costs", MappingProxyType(costs))
        object.__setattr__(self, "metadata_safe", _meta(self.metadata_safe))
