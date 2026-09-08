"""Block 5.7A — canonical cross-provider Marketplace Platform bridge.

Architecture (spec section 1):

    Panda Product Intelligence (5.5, canonical, CLOSED)
        -> Marketplace Platform (this module, 5.7)
            -> Governed Tool/Integration Platform (5.4, CLOSED)
                -> integrations.{wildberries,ozon,yandex_market} adapters

This module intentionally does **not**:
  - duplicate the canonical Product/SKU model (that lives in
    ``product_intel``; canonical products are passed in as plain dicts,
    the same convention ``integrations.bitrix.product_bridge`` already
    uses for its own channel);
  - implement 5.8 Marketplace Price Protection business policy (that is
    ``marketplace.price_guard``/``marketplace.economics``, untouched here;
    this module only supports price READ/WRITE mechanics per section 12);
  - talk to any provider HTTP client directly -- every read/write goes
    through ``IntegrationActivationService.execute_via_gateway``, the same
    governed path Block 5.6 (Bitrix) and the existing marketplace adapter
    closures already use.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from integrations.activation.models import ENV_FIXTURE, OP_READ, OP_WRITE
from integrations.activation.service import IntegrationActivationService
from security.tenant import require_tenant_id

from marketplace.errors import MARKETPLACE_CAPABILITY_UNSUPPORTED, MARKETPLACE_NOT_FOUND, MarketplaceError, classify_provider_error
from marketplace.models import (
    CATEGORY_MATCHED,
    CATEGORY_UNMAPPED,
    DIFF_CONFLICT,
    DIFF_IN_SYNC,
    DIFF_INVALID,
    DIFF_MARKETPLACE_NEWER,
    DIFF_MISSING_IN_MARKETPLACE,
    DIFF_MISSING_IN_PANDA,
    DIFF_PANDA_NEWER,
    DIFF_UNMAPPED,
    FULFILLMENT_UNKNOWN,
    ListingReadinessIssue,
    ListingReadinessResult,
    MarketplaceCategoryMap,
    MarketplaceOrder,
    MarketplaceOrderItem,
    MarketplaceOrderStatus,
    MarketplacePrice,
    MarketplaceShipment,
    MarketplaceStock,
    MarketplaceSyncState,
    MoneyAmount,
    ORDER_STATUS_CANCELLED,
    ORDER_STATUS_CONFIRMED,
    ORDER_STATUS_DELIVERED,
    ORDER_STATUS_NEW,
    ORDER_STATUS_PROCESSING,
    ORDER_STATUS_READY_FOR_SHIPMENT,
    ORDER_STATUS_RETURNED,
    ORDER_STATUS_SHIPPED,
    ORDER_STATUS_UNKNOWN,
    PROVIDER_MARKETPLACES,
    PROVIDER_OZON,
    PROVIDER_WILDBERRIES,
    PROVIDER_YANDEX_MARKET,
)
from product_intel.planner import assert_sync_product_allowed


@dataclass(frozen=True)
class _ProviderProfile:
    """The small, explicit per-provider vocabulary difference (spec
    sections 5/6/7). Everything else (governance, idempotency, diff,
    readiness, order normalization shape) is shared platform logic."""

    provider: str
    read_capability: str
    price_read_capability: str
    stock_read_capability: str
    orders_read_capability: str
    write_capability: str
    sku_param: str
    lookup_operation: str
    create_operation: str
    update_operation: str
    order_status_map: dict[str, str]


# Reuses the capability strings already registered for these three
# providers in ``integrations.activation.providers`` (WILDBERRIES/OZON/
# YANDEX_MARKET) -- no new capability contract is introduced. Each
# provider's real fixture adapter already gates every write operation
# (price_update/stock_update/card_create/card_import/offer_submission/
# card_update/offer_update/selective_export) behind that one registered
# ``*.price.write`` capability (see e.g.
# tests/test_real_ozon_integration_closure.py::test_stock_write_exact_warehouse);
# this profile table reuses that exact precedent rather than inventing a
# second write-capability contract per operation.
_PROFILES: dict[str, _ProviderProfile] = {
    PROVIDER_WILDBERRIES: _ProviderProfile(
        provider=PROVIDER_WILDBERRIES,
        read_capability="marketplace.product",
        price_read_capability="marketplace.wb.price.read",
        stock_read_capability="marketplace.wb.stock.read",
        orders_read_capability="marketplace.wb.orders.read",
        write_capability="marketplace.wb.price.write",
        sku_param="seller_article",
        lookup_operation="card_lookup",
        create_operation="card_create",
        update_operation="card_update",
        order_status_map={
            "NEW": ORDER_STATUS_NEW,
            "CONFIRMED": ORDER_STATUS_CONFIRMED,
            "SORTED": ORDER_STATUS_PROCESSING,
            "READY_FOR_SHIPMENT": ORDER_STATUS_READY_FOR_SHIPMENT,
            "SHIPPED": ORDER_STATUS_SHIPPED,
            "DELIVERED": ORDER_STATUS_DELIVERED,
            "CANCELLED": ORDER_STATUS_CANCELLED,
            "RETURNED": ORDER_STATUS_RETURNED,
        },
    ),
    PROVIDER_OZON: _ProviderProfile(
        provider=PROVIDER_OZON,
        read_capability="marketplace.product",
        price_read_capability="marketplace.ozon.price.read",
        stock_read_capability="marketplace.ozon.stock.read",
        orders_read_capability="marketplace.ozon.orders.read",
        write_capability="marketplace.ozon.price.write",
        sku_param="seller_article",
        lookup_operation="card_lookup",
        create_operation="card_import",
        update_operation="card_update",
        order_status_map={
            "awaiting_packaging": ORDER_STATUS_CONFIRMED,
            "awaiting_deliver": ORDER_STATUS_READY_FOR_SHIPMENT,
            "delivering": ORDER_STATUS_SHIPPED,
            "delivered": ORDER_STATUS_DELIVERED,
            "cancelled": ORDER_STATUS_CANCELLED,
            "returned": ORDER_STATUS_RETURNED,
        },
    ),
    PROVIDER_YANDEX_MARKET: _ProviderProfile(
        provider=PROVIDER_YANDEX_MARKET,
        read_capability="marketplace.product",
        price_read_capability="marketplace.yandex.price.read",
        stock_read_capability="marketplace.yandex.stock.read",
        orders_read_capability="marketplace.yandex.orders.read",
        write_capability="marketplace.yandex.price.write",
        sku_param="shop_sku",
        lookup_operation="offer_lookup",
        create_operation="offer_submission",
        update_operation="offer_update",
        order_status_map={
            "PROCESSING": ORDER_STATUS_PROCESSING,
            "PICKUP": ORDER_STATUS_READY_FOR_SHIPMENT,
            "DELIVERY": ORDER_STATUS_SHIPPED,
            "DELIVERED": ORDER_STATUS_DELIVERED,
            "CANCELLED": ORDER_STATUS_CANCELLED,
            "RETURNED": ORDER_STATUS_RETURNED,
        },
    ),
}


def _profile(provider: str) -> _ProviderProfile:
    profile = _PROFILES.get(provider)
    if profile is None:
        raise MarketplaceError(MARKETPLACE_CAPABILITY_UNSUPPORTED, f"unknown_provider:{provider}")
    return profile


def normalize_order_status(*, provider: str, raw_status: str, raw_substatus: str = "") -> MarketplaceOrderStatus:
    """Canonical order-status layer (spec section 15). Any provider status
    absent from the mapping table fails safely into UNKNOWN -- it never
    raises, so it can never crash synchronization."""
    profile = _profile(provider)
    canonical = profile.order_status_map.get(raw_status, ORDER_STATUS_UNKNOWN)
    return MarketplaceOrderStatus(canonical_status=canonical, provider_status=raw_status, provider_substatus=raw_substatus)


def normalize_order(*, tenant_id: str, provider: str, account_id: str, raw: dict) -> MarketplaceOrder:
    """Normalize one provider order row (as returned by each adapter's
    ``order_read``) into the canonical order model (spec section 14),
    preserving provider identifiers rather than discarding them."""
    tenant = require_tenant_id(tenant_id)
    status = normalize_order_status(provider=provider, raw_status=str(raw.get("status") or ""))
    total = raw.get("total")
    money = MoneyAmount(Decimal(str(total)), str(raw.get("currency") or "RUB")) if total is not None else None
    item_price = MoneyAmount(money.amount, money.currency) if money else None
    item = MarketplaceOrderItem(
        external_offer_id=str(raw.get("offer_id") or ""),
        external_sku=str(raw.get("seller_article") or raw.get("shop_sku") or raw.get("sku") or ""),
        quantity=int(raw.get("quantity") or 1),
        item_price=item_price,
    )
    shipment = MarketplaceShipment(
        external_shipment_id=str(raw.get("posting_number") or ""),
        fulfillment_model=str(raw.get("fulfillment") or FULFILLMENT_UNKNOWN),
        warehouse_id=str(raw.get("warehouse_id") or ""),
    )
    return MarketplaceOrder(
        tenant_id=tenant,
        provider=provider,
        account_id=account_id,
        external_order_id=str(raw.get("order_id") or raw.get("posting_number") or ""),
        status=status,
        currency=str(raw.get("currency") or "RUB"),
        total=money,
        items=(item,),
        shipment=shipment,
        raw_reference=str(raw.get("order_id") or ""),
    )


class MarketplaceCategoryMapStore:
    """Tenant+provider scoped category mapping store (spec section 10).
    Explicit lookup/create/update contract -- never a hardcoded universal
    rule such as "TV -> category X"."""

    def __init__(self):
        self._maps: dict[tuple[str, str, str], MarketplaceCategoryMap] = {}

    def lookup(self, *, tenant_id: str, provider: str, panda_category: str) -> MarketplaceCategoryMap:
        tenant = require_tenant_id(tenant_id)
        existing = self._maps.get((tenant, provider, panda_category))
        if existing is not None:
            return existing
        return MarketplaceCategoryMap(tenant_id=tenant, provider=provider, panda_category=panda_category, status=CATEGORY_UNMAPPED)

    def upsert(
        self,
        *,
        tenant_id: str,
        provider: str,
        panda_category: str,
        external_category_id: str,
        required_attributes: tuple[str, ...] = (),
    ) -> MarketplaceCategoryMap:
        tenant = require_tenant_id(tenant_id)
        mapping = MarketplaceCategoryMap(
            tenant_id=tenant,
            provider=provider,
            panda_category=panda_category,
            external_category_id=external_category_id,
            status=CATEGORY_MATCHED if external_category_id else CATEGORY_UNMAPPED,
            required_attributes=tuple(required_attributes),
        )
        self._maps[(tenant, provider, panda_category)] = mapping
        return mapping


def validate_listing_readiness(
    *,
    canonical_product: dict,
    provider: str,
    category_map: MarketplaceCategoryMap,
) -> ListingReadinessResult:
    """Publication-readiness validation (spec section 11). Returns a
    structured result; never fabricates a missing field to make a product
    look ready."""
    issues: list[ListingReadinessIssue] = []
    product_id = str(canonical_product.get("product_id") or "")
    if not product_id:
        issues.append(ListingReadinessIssue(code="MISSING_PRODUCT_IDENTITY", field="product_id"))

    sku = str(canonical_product.get("sku") or canonical_product.get("article") or "")
    if not sku:
        issues.append(ListingReadinessIssue(code="MISSING_SKU_IDENTITY", field="sku"))

    if category_map.status != CATEGORY_MATCHED or not category_map.external_category_id:
        issues.append(ListingReadinessIssue(code="CATEGORY_UNMAPPED", field="category", message=category_map.panda_category))

    title = str(canonical_product.get("title") or "")
    if not title:
        issues.append(ListingReadinessIssue(code="MISSING_TITLE", field="title"))

    price = canonical_product.get("price") or {}
    selling_price = price.get("selling_price") if isinstance(price, dict) else None
    if selling_price in (None, ""):
        issues.append(ListingReadinessIssue(code="MISSING_PRICE", field="price"))

    stock = canonical_product.get("stock") or {}
    if not isinstance(stock, dict) or stock.get("quantity") is None:
        issues.append(ListingReadinessIssue(code="MISSING_STOCK", field="stock"))

    if category_map.status == CATEGORY_MATCHED:
        provided_attrs = set((canonical_product.get("attributes") or {}).keys())
        for required in category_map.required_attributes:
            if required not in provided_attrs:
                issues.append(ListingReadinessIssue(code="MISSING_REQUIRED_ATTRIBUTE", field=required))

    return ListingReadinessResult(product_id=product_id, provider=provider, ready=not issues, issues=tuple(issues))


def classify_sync_diff(
    *,
    panda_state: dict | None,
    marketplace_state: dict | None,
    category_mapped: bool = True,
) -> str:
    """Deterministic (Panda side, marketplace side) diff classification
    (spec section 17). Each side is ``{"value": ..., "updated_at": ...}``
    or ``None`` when absent. Never auto-resolves an ambiguous conflict."""
    if not category_mapped:
        return DIFF_UNMAPPED
    if panda_state is None and marketplace_state is None:
        return DIFF_INVALID
    if panda_state is None:
        return DIFF_MISSING_IN_PANDA
    if marketplace_state is None:
        return DIFF_MISSING_IN_MARKETPLACE

    if panda_state.get("value") == marketplace_state.get("value"):
        return DIFF_IN_SYNC

    panda_updated = panda_state.get("updated_at")
    mkt_updated = marketplace_state.get("updated_at")
    if panda_updated and mkt_updated:
        if panda_updated > mkt_updated:
            return DIFF_PANDA_NEWER
        if mkt_updated > panda_updated:
            return DIFF_MARKETPLACE_NEWER
    return DIFF_CONFLICT


class MarketplacePlatform:
    """One canonical, capability-driven entry point per (tenant, provider).
    Every read/write is routed through the existing governed
    ``IntegrationActivationService`` -- there is no direct adapter/HTTP
    access here (spec section 20); this class only knows the canonical
    contract + this provider's small vocabulary difference (``_profile``)."""

    def __init__(self, *, activation: IntegrationActivationService, provider: str):
        if provider not in PROVIDER_MARKETPLACES:
            raise MarketplaceError(MARKETPLACE_CAPABILITY_UNSUPPORTED, f"unknown_provider:{provider}")
        self._activation = activation
        self.provider = provider
        self._profile = _profile(provider)
        self.category_maps = MarketplaceCategoryMapStore()

    # ---- internal governed I/O ----

    def _read(
        self,
        *,
        tenant_id: str,
        environment: str,
        capability: str,
        payload: dict,
        connection_id: str | None = None,
        correlation_id: str = "",
        page: int = 1,
    ) -> dict:
        try:
            out = self._activation.execute_via_gateway(
                tenant_id=tenant_id,
                capability=capability,
                environment=environment,
                operation_class=OP_READ,
                payload=payload,
                connection_id=connection_id,
                correlation_id=correlation_id,
                page=page,
            )
        except Exception as exc:
            raise MarketplaceError(classify_provider_error(exc), str(exc)) from exc
        return out["result"]

    def _write(
        self,
        *,
        tenant_id: str,
        environment: str,
        payload: dict,
        idempotency_key: str,
        approved_write: bool,
        connection_id: str | None = None,
        correlation_id: str = "",
    ) -> dict:
        try:
            out = self._activation.execute_via_gateway(
                tenant_id=tenant_id,
                capability=self._profile.write_capability,
                environment=environment,
                operation_class=OP_WRITE,
                payload=payload,
                idempotency_key=idempotency_key,
                approved_write=approved_write,
                connection_id=connection_id,
                correlation_id=correlation_id,
            )
        except Exception as exc:
            raise MarketplaceError(classify_provider_error(exc), str(exc)) from exc
        return out["result"]

    # ---- ACCOUNT ----

    def health(self, *, tenant_id: str, connection_id: str):
        return self._activation.health(tenant_id=tenant_id, connection_id=connection_id)

    # ---- CATALOG / CATEGORIES (read) ----

    def read_catalog_page(
        self,
        *,
        tenant_id: str,
        environment: str = ENV_FIXTURE,
        connection_id: str | None = None,
        page: int = 1,
        correlation_id: str = "",
    ) -> dict:
        return self._read(
            tenant_id=tenant_id,
            environment=environment,
            capability=self._profile.read_capability,
            payload={},
            connection_id=connection_id,
            correlation_id=correlation_id,
            page=page,
        )

    def paginated_catalog(self, *, tenant_id: str, environment: str = ENV_FIXTURE, max_pages: int = 3, connection_id: str | None = None) -> dict:
        return self._activation.paginated_read(
            tenant_id=tenant_id,
            capability=self._profile.read_capability,
            environment=environment,
            max_pages=max_pages,
            connection_id=connection_id,
        )

    def lookup_listing(
        self,
        *,
        tenant_id: str,
        environment: str = ENV_FIXTURE,
        connection_id: str | None = None,
        **identifiers: Any,
    ) -> dict:
        payload = {"operation": self._profile.lookup_operation, **identifiers}
        return self._read(tenant_id=tenant_id, environment=environment, capability=self._profile.read_capability, payload=payload, connection_id=connection_id)

    # ---- PRICE ----

    def _normalize_price(self, *, account_id: str, external_sku: str, raw: dict) -> MarketplacePrice:
        amount_raw = raw.get("seller_price") or raw.get("base_price") or raw.get("seller_effective_price") or "0"
        return MarketplacePrice(
            provider=self.provider,
            account_id=account_id,
            offer_id=external_sku,
            amount=MoneyAmount(Decimal(str(amount_raw)), "RUB"),
        )

    def read_price(
        self,
        *,
        tenant_id: str,
        external_sku: str,
        account_id: str = "",
        environment: str = ENV_FIXTURE,
        connection_id: str | None = None,
    ) -> MarketplacePrice:
        payload = {"operation": "price_read", self._profile.sku_param: external_sku}
        raw = self._read(tenant_id=tenant_id, environment=environment, capability=self._profile.price_read_capability, payload=payload, connection_id=connection_id)
        return self._normalize_price(account_id=account_id, external_sku=external_sku, raw=raw)

    def write_price(
        self,
        *,
        tenant_id: str,
        external_sku: str,
        amount: Decimal,
        idempotency_key: str,
        approved_write: bool,
        environment: str = ENV_FIXTURE,
        connection_id: str | None = None,
        correlation_id: str = "",
    ) -> dict:
        """Governed price WRITE (spec section 12). Does not decide whether
        the price is economically safe -- that is 5.8, out of scope here."""
        payload = {"operation": "price_update", self._profile.sku_param: external_sku, "new_price": str(amount)}
        return self._write(
            tenant_id=tenant_id,
            environment=environment,
            payload=payload,
            idempotency_key=idempotency_key,
            approved_write=approved_write,
            connection_id=connection_id,
            correlation_id=correlation_id,
        )

    # ---- STOCK ----

    def _normalize_stock(self, *, account_id: str, external_sku: str, warehouse: str, raw: dict) -> MarketplaceStock:
        qty = raw.get("available")
        if qty is None:
            qty = raw.get("quantity") or 0
        return MarketplaceStock(
            provider=self.provider,
            account_id=account_id,
            offer_id=external_sku,
            warehouse_id=warehouse,
            quantity=int(qty),
            available=int(qty),
        )

    def read_stock(
        self,
        *,
        tenant_id: str,
        external_sku: str,
        warehouse: str = "",
        account_id: str = "",
        environment: str = ENV_FIXTURE,
        connection_id: str | None = None,
    ) -> MarketplaceStock:
        payload = {"operation": "stock_read", self._profile.sku_param: external_sku}
        if warehouse:
            payload["warehouse"] = warehouse
        raw = self._read(tenant_id=tenant_id, environment=environment, capability=self._profile.stock_read_capability, payload=payload, connection_id=connection_id)
        return self._normalize_stock(account_id=account_id, external_sku=external_sku, warehouse=str(raw.get("warehouse") or warehouse), raw=raw)

    def write_stock(
        self,
        *,
        tenant_id: str,
        external_sku: str,
        quantity: int,
        warehouse: str,
        idempotency_key: str,
        approved_write: bool,
        environment: str = ENV_FIXTURE,
        connection_id: str | None = None,
        correlation_id: str = "",
    ) -> dict:
        """Governed stock WRITE (spec section 13)."""
        payload = {
            "operation": "stock_update",
            self._profile.sku_param: external_sku,
            "warehouse": warehouse,
            "quantity": quantity,
        }
        return self._write(
            tenant_id=tenant_id,
            environment=environment,
            payload=payload,
            idempotency_key=idempotency_key,
            approved_write=approved_write,
            connection_id=connection_id,
            correlation_id=correlation_id,
        )

    # ---- CATALOG (write / publication) ----

    def publish_listing(
        self,
        *,
        tenant_id: str,
        canonical_product: dict,
        category_map: MarketplaceCategoryMap,
        idempotency_key: str,
        approved_write: bool,
        create: bool = True,
        environment: str = ENV_FIXTURE,
        connection_id: str | None = None,
        correlation_id: str = "",
    ) -> dict:
        """Governed catalog CREATE/UPDATE (spec sections 5-7, 11). Callers
        must pass a readiness result that is ``ready`` -- this method
        re-validates and fails closed rather than trusting the caller."""
        readiness = validate_listing_readiness(canonical_product=canonical_product, provider=self.provider, category_map=category_map)
        if not readiness.ready:
            raise MarketplaceError("MARKETPLACE_NOT_READY", ",".join(i.code for i in readiness.issues))

        # NOTE (provider limitation, spec section 25): the underlying
        # FIXTURE adapters' own card_create/card_import/offer_submission
        # resolve ``category_id`` via their *own* internal
        # ``map_category(canonical_category_id=...)`` helper, which has no
        # hook for a caller-supplied override -- so the Panda *canonical*
        # category (not our external_category_id) must be passed through
        # under the ``category_id`` key for that internal lookup to work.
        # Our own ``MarketplaceCategoryMapStore``/``external_category_id``
        # remains the source of truth for readiness/diff; it is not yet
        # wired as an override into these adapters' internal mapping.
        product = {
            "seller_article": canonical_product.get("sku") or canonical_product.get("article"),
            "sku": canonical_product.get("sku") or canonical_product.get("article"),
            "title": canonical_product.get("title"),
            "category_id": category_map.panda_category,
            "canonical_category_id": category_map.panda_category,
            "external_category_id": category_map.external_category_id,
        }
        operation = self._profile.create_operation if create else self._profile.update_operation
        payload = {"operation": operation, "product": product, "panda_product_id": canonical_product.get("product_id")}
        return self._write(
            tenant_id=tenant_id,
            environment=environment,
            payload=payload,
            idempotency_key=idempotency_key,
            approved_write=approved_write,
            connection_id=connection_id,
            correlation_id=correlation_id,
        )

    # ---- ORDERS ----

    def read_orders(
        self,
        *,
        tenant_id: str,
        account_id: str = "",
        page: int = 1,
        environment: str = ENV_FIXTURE,
        connection_id: str | None = None,
    ) -> tuple[MarketplaceOrder, ...]:
        payload = {"operation": "order_read"}
        raw = self._read(
            tenant_id=tenant_id,
            environment=environment,
            capability=self._profile.orders_read_capability,
            payload=payload,
            connection_id=connection_id,
            page=page,
        )
        items = raw.get("items") or []
        return tuple(normalize_order(tenant_id=tenant_id, provider=self.provider, account_id=account_id, raw=row) for row in items)

    # ---- SYNC / DIFF ----

    def diff_price(
        self,
        *,
        tenant_id: str,
        external_sku: str,
        panda_amount: Decimal | None,
        category_mapped: bool = True,
        environment: str = ENV_FIXTURE,
        connection_id: str | None = None,
    ) -> MarketplaceSyncState:
        tenant = require_tenant_id(tenant_id)
        try:
            remote = self.read_price(tenant_id=tenant_id, external_sku=external_sku, environment=environment, connection_id=connection_id)
            mkt_state = {"value": str(remote.amount.amount)}
        except MarketplaceError as exc:
            if exc.code == MARKETPLACE_NOT_FOUND:
                mkt_state = None
            else:
                raise
        panda_state = {"value": str(panda_amount)} if panda_amount is not None else None
        diff = classify_sync_diff(panda_state=panda_state, marketplace_state=mkt_state, category_mapped=category_mapped)
        return MarketplaceSyncState(
            tenant_id=tenant,
            provider=self.provider,
            dimension="PRICE",
            subject_id=external_sku,
            diff=diff,
            panda_value=str(panda_amount) if panda_amount is not None else "",
            marketplace_value=(mkt_state or {}).get("value", "") if mkt_state else "",
        )

    def bulk_sync_gate(self, *, item_count: int, bulk: bool = False) -> None:
        """Reuses the existing Product Intelligence batch-admission gate
        (spec section 16/21 -- large marketplace sync belongs to
        batch/background execution, not a single interactive call)."""
        assert_sync_product_allowed(item_count=item_count, bulk=bulk)
