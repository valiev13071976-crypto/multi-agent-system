from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from finops.models import (
    DEFAULT_UNKNOWN_COST_POLICY,
    UNKNOWN_COST_DENY,
    UNKNOWN_COST_POLICIES,
    BudgetDecision,
    BudgetLimits,
    PriceQuote,
    UsageRecord,
)
from finops.storage import InMemoryUsageStore, UsageStore


MILLION = Decimal("1000000")


class InvalidBudgetPolicyError(ValueError):
    pass


def parse_decimal(raw: str | None) -> Decimal | None:
    if raw is None or not str(raw).strip():
        return None
    try:
        return Decimal(str(raw).strip())
    except InvalidOperation as exc:
        raise InvalidBudgetPolicyError(f"Invalid decimal amount: {raw!r}") from exc


def parse_unknown_cost_policy(raw: str | None) -> str:
    if raw is None or not str(raw).strip():
        return DEFAULT_UNKNOWN_COST_POLICY
    value = str(raw).strip()
    if value not in UNKNOWN_COST_POLICIES:
        raise InvalidBudgetPolicyError(
            f"Invalid unknown-cost policy {value!r}. "
            f"Allowed: {', '.join(UNKNOWN_COST_POLICIES)}."
        )
    return value


def estimate_cost(
    quote: PriceQuote | None,
    input_tokens: int | None,
    output_tokens: int | None,
) -> Decimal | None:
    if quote is None or not quote.enabled:
        return None
    if input_tokens is None or output_tokens is None:
        return None
    input_cost = (Decimal(input_tokens) / MILLION) * quote.input_price_per_million
    output_cost = (Decimal(output_tokens) / MILLION) * quote.output_price_per_million
    return input_cost + output_cost


def _known_costs(records: tuple[UsageRecord, ...]) -> Decimal:
    total = Decimal("0")
    for record in records:
        if record.estimated_cost is not None:
            total += record.estimated_cost
    return total


def _day_bounds(moment: datetime) -> tuple[datetime, datetime]:
    start = datetime(moment.year, moment.month, moment.day, tzinfo=timezone.utc)
    return start, start + timedelta(days=1)


def _month_bounds(moment: datetime) -> tuple[datetime, datetime]:
    start = datetime(moment.year, moment.month, 1, tzinfo=timezone.utc)
    if moment.month == 12:
        end = datetime(moment.year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        end = datetime(moment.year, moment.month + 1, 1, tzinfo=timezone.utc)
    return start, end


class FinOpsService:
    def __init__(
        self,
        prices: dict[tuple[str, str], PriceQuote] | None = None,
        limits: BudgetLimits | None = None,
        store: UsageStore | None = None,
    ):
        self._prices = prices or {}
        self._limits = limits or BudgetLimits(
            per_task=None,
            per_day=None,
            per_month=None,
            unknown_cost_policy=DEFAULT_UNKNOWN_COST_POLICY,
        )
        self._store = store or InMemoryUsageStore()
        self.observability = None

    def quote(self, provider_id: str, model_id: str) -> PriceQuote | None:
        return self._prices.get((provider_id, model_id))

    def estimate(
        self,
        provider_id: str,
        model_id: str,
        input_tokens: int | None,
        output_tokens: int | None,
    ) -> Decimal | None:
        return estimate_cost(
            self.quote(provider_id, model_id),
            input_tokens,
            output_tokens,
        )

    def check_budget(
        self,
        estimated_cost: Decimal | None,
        *,
        when: datetime | None = None,
        currency: str = "USD",
        task_id: str | None = None,
    ) -> BudgetDecision:
        decision = self._check_budget_impl(
            estimated_cost, when=when, currency=currency, task_id=task_id
        )
        if self.observability is not None:
            from observability.helpers import safe_emit

            if not decision.allowed:
                event = (
                    "finops.unknown_cost"
                    if decision.reason == "unknown_cost_denied"
                    else "finops.budget_denied"
                )
                safe_emit(
                    self.observability,
                    event,
                    context=self.observability.create_context(),
                    component="finops",
                    status="denied",
                    error_code=decision.reason,
                    metadata={
                        "currency": currency,
                        "cost": str(decision.estimated_cost)
                        if decision.estimated_cost is not None
                        else None,
                        "budget_category": decision.reason,
                    },
                )
        return decision

    def _check_budget_impl(
        self,
        estimated_cost: Decimal | None,
        *,
        when: datetime | None = None,
        currency: str = "USD",
        task_id: str | None = None,
    ) -> BudgetDecision:
        moment = when or datetime.now(timezone.utc)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)

        if estimated_cost is None:
            if self._limits.unknown_cost_policy == UNKNOWN_COST_DENY:
                return BudgetDecision(
                    allowed=False,
                    reason="unknown_cost_denied",
                    estimated_cost=None,
                    currency=currency,
                )
            return BudgetDecision(
                allowed=True,
                reason="unknown_cost_allowed",
                estimated_cost=None,
                currency=currency,
            )

        if self._limits.per_task is not None:
            task_so_far = Decimal("0")
            if task_id:
                task_so_far = _known_costs(self._store.records_for_task(task_id))
            if task_so_far + estimated_cost > self._limits.per_task:
                return BudgetDecision(
                    allowed=False,
                    reason="per_task_limit",
                    estimated_cost=estimated_cost,
                    currency=currency,
                )

        day_start, day_end = _day_bounds(moment)
        day_total = _known_costs(self._store.records_between(day_start, day_end))
        if self._limits.per_day is not None and day_total + estimated_cost > self._limits.per_day:
            return BudgetDecision(
                allowed=False,
                reason="per_day_limit",
                estimated_cost=estimated_cost,
                currency=currency,
            )

        month_start, month_end = _month_bounds(moment)
        month_total = _known_costs(self._store.records_between(month_start, month_end))
        if (
            self._limits.per_month is not None
            and month_total + estimated_cost > self._limits.per_month
        ):
            return BudgetDecision(
                allowed=False,
                reason="per_month_limit",
                estimated_cost=estimated_cost,
                currency=currency,
            )

        return BudgetDecision(
            allowed=True,
            reason="within_budget",
            estimated_cost=estimated_cost,
            currency=currency,
        )

    def record(self, usage: UsageRecord) -> None:
        self._store.add(usage)
        if self.observability is not None:
            from observability.helpers import safe_emit

            safe_emit(
                self.observability,
                "finops.recorded",
                context=self.observability.create_context(),
                component="finops",
                provider=getattr(usage, "provider_id", "") or "",
                model=getattr(usage, "model_id", "") or "",
                status="recorded",
                metadata={
                    "tokens": getattr(usage, "total_tokens", None),
                    "cost": str(getattr(usage, "cost", None)),
                    "currency": getattr(usage, "currency", "USD"),
                },
            )

    def record_usage(self, usage: UsageRecord) -> None:
        self.record(usage)

    def task_total(self, task_id: str) -> Decimal:
        return _known_costs(self._store.records_for_task(task_id))

    def is_over_limit(
        self,
        *,
        when: datetime | None = None,
        task_id: str | None = None,
    ) -> bool:
        moment = when or datetime.now(timezone.utc)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        if self._limits.per_task is not None and task_id:
            if self.task_total(task_id) > self._limits.per_task:
                return True
        if self._limits.per_day is not None and self.day_total(moment) > self._limits.per_day:
            return True
        if self._limits.per_month is not None and self.month_total(moment) > self._limits.per_month:
            return True
        return False

    def day_total(self, moment: datetime) -> Decimal:
        start, end = _day_bounds(moment)
        return _known_costs(self._store.records_between(start, end))

    def month_total(self, moment: datetime) -> Decimal:
        start, end = _month_bounds(moment)
        return _known_costs(self._store.records_between(start, end))
