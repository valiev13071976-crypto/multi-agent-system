"""Price guard: auto-correct policy, bounds, loop prevention."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from marketplace.errors import (
    MARKETPLACE_AUTO_CORRECT_DENIED,
    MARKETPLACE_ECONOMICS_UNKNOWN,
    MARKETPLACE_PRICE_FLOOR,
    MARKETPLACE_SYNC_LOOP_TERMINATED,
    MarketplaceError,
)
from marketplace.models import (
    MODE_APPROVAL_REQUIRED,
    MODE_AUTO_CORRECT,
    MODE_MONITOR_ONLY,
    MODE_RECOMMEND,
    PROFIT_LOSS,
    PROFIT_UNKNOWN,
    MarketplaceProfitabilityResult,
)


@dataclass
class PriceSyncLedger:
    _outbound: dict[str, str] = field(default_factory=dict)
    _acked: set[str] = field(default_factory=set)
    _override_streak: dict[str, int] = field(default_factory=dict)

    def record_outbound(self, *, causation_id: str, sku: str) -> None:
        self._outbound[causation_id] = sku

    def acknowledge_inbound(self, *, causation_id: str, origin: str = "marketplace") -> dict:
        if causation_id in self._outbound or causation_id in self._acked or origin == "panda":
            self._acked.add(causation_id)
            return {"terminated": True, "code": MARKETPLACE_SYNC_LOOP_TERMINATED}
        return {"terminated": False}

    def note_external_override(self, *, sku: str) -> int:
        self._override_streak[sku] = self._override_streak.get(sku, 0) + 1
        return self._override_streak[sku]


@dataclass(frozen=True)
class PriceBounds:
    max_delta_pct: Decimal = Decimal("15")
    max_delta_abs: Decimal = Decimal("5000")
    cooldown_writes: int = 3


def decide_auto_correct(
    *,
    profitability: MarketplaceProfitabilityResult,
    mode: str,
    price_write_supported: bool,
    authorized: bool,
    current_price: Decimal,
    proposed_price: Decimal,
    bounds: PriceBounds | None = None,
) -> dict:
    bounds = bounds or PriceBounds()
    if profitability.status == PROFIT_UNKNOWN:
        return {
            "action": "DENY",
            "code": MARKETPLACE_ECONOMICS_UNKNOWN,
            "mutate": False,
            "alert": True,
            "reason": "unknown_economics",
        }
    if profitability.status != PROFIT_LOSS:
        return {"action": "NONE", "mutate": False, "alert": False, "reason": "not_loss"}

    if not price_write_supported:
        return {
            "action": "ALERT",
            "code": MARKETPLACE_AUTO_CORRECT_DENIED,
            "mutate": False,
            "alert": True,
            "reason": "capability_missing",
        }
    if mode in {MODE_MONITOR_ONLY, MODE_RECOMMEND}:
        return {
            "action": "ALERT",
            "code": MARKETPLACE_AUTO_CORRECT_DENIED,
            "mutate": False,
            "alert": True,
            "reason": f"mode_{mode}",
        }
    if mode == MODE_APPROVAL_REQUIRED:
        return {
            "action": "APPROVAL",
            "code": "MARKETPLACE_APPROVAL_REQUIRED",
            "mutate": False,
            "alert": True,
            "reason": "approval_required",
        }
    if mode != MODE_AUTO_CORRECT or not authorized:
        return {
            "action": "DENY",
            "code": MARKETPLACE_AUTO_CORRECT_DENIED,
            "mutate": False,
            "alert": True,
            "reason": "not_authorized_or_mode",
        }

    floor = profitability.minimum_allowed.amount if profitability.minimum_allowed else None
    if floor is None:
        return {
            "action": "DENY",
            "code": MARKETPLACE_ECONOMICS_UNKNOWN,
            "mutate": False,
            "alert": True,
            "reason": "floor_unavailable",
        }
    if proposed_price < floor:
        return {
            "action": "DENY",
            "code": MARKETPLACE_PRICE_FLOOR,
            "mutate": False,
            "alert": True,
            "reason": "proposed_below_floor",
        }
    delta = abs(proposed_price - current_price)
    pct = (delta / current_price * Decimal("100")) if current_price else Decimal("100")
    # Raising up to the calculated floor is always in-policy (floor recovery).
    floor_recovery = proposed_price >= floor and current_price < floor and proposed_price <= floor
    if not floor_recovery and (pct > bounds.max_delta_pct or delta > bounds.max_delta_abs):
        return {
            "action": "DENY",
            "code": MARKETPLACE_AUTO_CORRECT_DENIED,
            "mutate": False,
            "alert": True,
            "reason": "bounds_exceeded",
        }
    return {
        "action": "AUTO_CORRECT",
        "mutate": True,
        "alert": False,
        "proposed": str(proposed_price),
        "reason": "loss_auto_correct",
    }
