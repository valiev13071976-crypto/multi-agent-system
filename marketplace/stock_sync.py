"""Stock projection, buffer, reconciliation."""

from __future__ import annotations

from decimal import Decimal

from marketplace.errors import MARKETPLACE_STOCK_STALE, MarketplaceError
from marketplace.models import STOCK_DRIFT, STOCK_MATCHED, STOCK_STALE, STOCK_UNKNOWN


def export_quantity(
    *,
    available: Decimal,
    buffer: Decimal = Decimal("0"),
    allow_negative: bool = False,
) -> Decimal:
    qty = Decimal(str(available)) - Decimal(str(buffer))
    if qty < 0 and not allow_negative:
        return Decimal("0")
    return qty


def reconcile_stock(
    *,
    expected: Decimal,
    observed: Decimal | None,
    stale: bool = False,
) -> dict:
    if stale:
        return {"status": STOCK_STALE, "expected": str(expected), "observed": None}
    if observed is None:
        return {"status": STOCK_UNKNOWN, "expected": str(expected), "observed": None}
    obs = Decimal(str(observed))
    if obs == Decimal(str(expected)):
        return {"status": STOCK_MATCHED, "expected": str(expected), "observed": str(obs)}
    return {
        "status": STOCK_DRIFT,
        "expected": str(expected),
        "observed": str(obs),
        "delta": str(obs - Decimal(str(expected))),
    }


def assert_export_safe(*, available: Decimal, stale: bool) -> None:
    if stale:
        raise MarketplaceError(MARKETPLACE_STOCK_STALE, "stale_stock_export_blocked")
    if Decimal(str(available)) < 0:
        raise MarketplaceError(MARKETPLACE_STOCK_STALE, "negative_stock")
