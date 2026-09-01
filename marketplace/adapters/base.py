"""Provider-neutral marketplace adapter contract + fixtures."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any


@dataclass
class AdapterHealth:
    configured: bool
    authenticated: bool
    reachable: bool
    degraded: bool = False
    rate_limited: bool = False
    live: bool = False
    detail: str = ""


class MarketplaceAdapter(ABC):
    provider: str
    live: bool = False

    @abstractmethod
    def capabilities(self) -> frozenset[str]:
        ...

    @abstractmethod
    def health(self) -> AdapterHealth:
        ...

    def require_capability(self, cap: str) -> None:
        from marketplace.errors import MARKETPLACE_CAPABILITY_UNSUPPORTED, MarketplaceError

        if cap not in self.capabilities():
            raise MarketplaceError(MARKETPLACE_CAPABILITY_UNSUPPORTED, f"{self.provider}:{cap}")

    @abstractmethod
    def card_create(self, *, payload: dict, idempotency_key: str) -> dict:
        ...

    @abstractmethod
    def card_update(self, *, external_id: str, patch: dict, idempotency_key: str) -> dict:
        ...

    @abstractmethod
    def price_apply(self, *, sku: str, amount: Decimal, idempotency_key: str) -> dict:
        ...

    @abstractmethod
    def stock_apply(self, *, sku: str, quantity: Decimal, warehouse: str, idempotency_key: str) -> dict:
        ...

    @abstractmethod
    def orders_read(self) -> list[dict]:
        ...

    @abstractmethod
    def reviews_read(self) -> list[dict]:
        ...

    @abstractmethod
    def commissions_read(self) -> list[dict]:
        ...

    @abstractmethod
    def normalize_event(self, raw: dict) -> dict:
        ...


@dataclass
class FakeAdapterState:
    cards: dict[str, dict] = field(default_factory=dict)
    prices: dict[str, Decimal] = field(default_factory=dict)
    stocks: dict[str, Decimal] = field(default_factory=dict)
    orders: list[dict] = field(default_factory=list)
    reviews: list[dict] = field(default_factory=list)
    commissions: list[dict] = field(default_factory=list)
    idempotency: dict[str, dict] = field(default_factory=dict)
    unavailable: bool = False
    rate_limited: bool = False
    override_count: dict[str, int] = field(default_factory=dict)


class FakeMarketplaceAdapter(MarketplaceAdapter):
    """Shared fake implementation; subclasses set provider + capability profile."""

    def __init__(self, *, provider: str, caps: frozenset[str], state: FakeAdapterState | None = None):
        self.provider = provider
        self.live = False
        self._caps = caps
        self.state = state or FakeAdapterState()

    def capabilities(self) -> frozenset[str]:
        return self._caps

    def health(self) -> AdapterHealth:
        if self.state.unavailable:
            return AdapterHealth(True, True, False, degraded=True, live=False, detail="unavailable")
        if self.state.rate_limited:
            return AdapterHealth(True, True, True, rate_limited=True, live=False, detail="429")
        return AdapterHealth(True, True, True, live=False, detail="fixture")

    def _check_health(self) -> None:
        from marketplace.errors import MARKETPLACE_RATE_LIMIT, MARKETPLACE_UNAVAILABLE, MarketplaceError

        if self.state.unavailable:
            raise MarketplaceError(MARKETPLACE_UNAVAILABLE, self.provider)
        if self.state.rate_limited:
            raise MarketplaceError(MARKETPLACE_RATE_LIMIT, self.provider)

    def card_create(self, *, payload: dict, idempotency_key: str) -> dict:
        self.require_capability("CARD_CREATE")
        self._check_health()
        if idempotency_key in self.state.idempotency:
            return {**self.state.idempotency[idempotency_key], "idempotent": True}
        ext = f"{self.provider.lower()}-{len(self.state.cards)+1}"
        card = {"external_id": ext, "payload": payload, "status": "PUBLISHED"}
        self.state.cards[ext] = card
        self.state.idempotency[idempotency_key] = card
        return card

    def card_update(self, *, external_id: str, patch: dict, idempotency_key: str) -> dict:
        self.require_capability("CARD_UPDATE")
        self._check_health()
        if idempotency_key in self.state.idempotency:
            return {**self.state.idempotency[idempotency_key], "idempotent": True}
        card = self.state.cards.get(external_id) or {"external_id": external_id, "payload": {}}
        card["payload"] = {**card.get("payload", {}), **patch}
        card["status"] = "UPDATED"
        self.state.cards[external_id] = card
        self.state.idempotency[idempotency_key] = card
        return card

    def price_apply(self, *, sku: str, amount: Decimal, idempotency_key: str) -> dict:
        self.require_capability("PRICE_WRITE")
        self._check_health()
        if idempotency_key in self.state.idempotency:
            return {**self.state.idempotency[idempotency_key], "idempotent": True}
        self.state.prices[sku] = Decimal(str(amount))
        out = {"sku": sku, "amount": str(amount), "status": "APPLIED", "causation": idempotency_key}
        self.state.idempotency[idempotency_key] = out
        return out

    def stock_apply(self, *, sku: str, quantity: Decimal, warehouse: str, idempotency_key: str) -> dict:
        self.require_capability("STOCK_WRITE")
        self._check_health()
        if idempotency_key in self.state.idempotency:
            return {**self.state.idempotency[idempotency_key], "idempotent": True}
        key = f"{warehouse}:{sku}"
        self.state.stocks[key] = Decimal(str(quantity))
        out = {"sku": sku, "warehouse": warehouse, "quantity": str(quantity), "status": "APPLIED"}
        self.state.idempotency[idempotency_key] = out
        return out

    def orders_read(self) -> list[dict]:
        self.require_capability("ORDER_READ")
        self._check_health()
        return list(self.state.orders)

    def reviews_read(self) -> list[dict]:
        self.require_capability("REVIEW_READ")
        self._check_health()
        return list(self.state.reviews)

    def commissions_read(self) -> list[dict]:
        self.require_capability("COMMISSION_READ")
        return list(self.state.commissions)

    def normalize_event(self, raw: dict) -> dict:
        return {
            "provider": self.provider,
            "event_id": str(raw.get("event_id") or ""),
            "type": str(raw.get("type") or ""),
            "payload": dict(raw.get("payload") or {}),
            "causation_id": str(raw.get("causation_id") or ""),
        }

    def seed_order(self, order: dict) -> None:
        self.state.orders.append(order)

    def seed_review(self, review: dict) -> None:
        self.state.reviews.append(review)

    def seed_commission(self, row: dict) -> None:
        self.state.commissions.append(row)

    def observe_price(self, sku: str) -> Decimal | None:
        return self.state.prices.get(sku)

    def observe_stock(self, sku: str, warehouse: str = "main") -> Decimal | None:
        return self.state.stocks.get(f"{warehouse}:{sku}")

    def force_external_override(self, sku: str, amount: Decimal) -> None:
        self.state.prices[sku] = Decimal(str(amount))
        self.state.override_count[sku] = self.state.override_count.get(sku, 0) + 1
