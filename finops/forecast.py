"""Deterministic lightweight forecasting foundation (advisory only)."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal, ROUND_DOWN

from finops.budget_models import BudgetForecast, utc_now
from finops.models import UsageRecord


def forecast_from_usage(
    records: tuple[UsageRecord, ...],
    *,
    remaining_budget: Decimal | None,
    window_limit: Decimal | None = None,
    now: datetime | None = None,
    lookback: int = 20,
) -> BudgetForecast:
    stamp = now or utc_now()
    known = [r for r in records if r.estimated_cost is not None and r.estimated_cost > 0]
    known = known[-max(1, lookback) :]
    if not known:
        return BudgetForecast(
            estimated_remaining_calls=None,
            projected_window_spend=None,
            projected_exhaustion=None,
            sample_size=0,
        )
    total = sum((r.estimated_cost for r in known), Decimal("0"))
    avg = (total / Decimal(len(known))).quantize(Decimal("0.0001"))
    remaining_calls = None
    if remaining_budget is not None and avg > 0:
        remaining_calls = int((remaining_budget / avg).to_integral_value(rounding=ROUND_DOWN))
        if remaining_calls < 0:
            remaining_calls = 0
    projected = avg * Decimal(len(known)) if window_limit is not None else None
    exhaustion = None
    if remaining_calls is not None and remaining_calls == 0:
        exhaustion = stamp
    elif remaining_calls is not None and remaining_calls > 0:
        exhaustion = stamp + timedelta(hours=remaining_calls)
    return BudgetForecast(
        estimated_remaining_calls=remaining_calls,
        projected_window_spend=projected,
        projected_exhaustion=exhaustion,
        sample_size=len(known),
        metadata_safe={"avg_cost": str(avg)},
    )
