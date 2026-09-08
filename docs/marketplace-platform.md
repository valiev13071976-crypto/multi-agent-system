# Marketplace Platform

## Architecture

One shared Marketplace Platform under `marketplace/` for Wildberries, Ozon, and Yandex Market.

```
PANDA E-COMMERCE (Product/SKU/Price/Stock/Order)
        ↓
 MarketplaceSelection (explicit — no implicit full catalog)
        ↓
 PublicationPlan (dry run) → governed apply via adapters
        ↓
   WB Adapter | Ozon Adapter | Yandex Adapter   (live=false fixtures)
        ↓
 Listing / Price / Stock / Orders / Reviews / Analytics
        ↓
 Commission + costs → MIN ALLOWED PRICE → Profitability
        ↓
 LOSS? → AUTO_CORRECT (if capability+policy) else OPERATOR ALERT
```

E-commerce remains source of truth. MarketplaceListing is a channel projection only.

## Adapters

| Provider | Module | Price write | Review reply | Promo write | Competitor read |
|----------|--------|-------------|--------------|-------------|-----------------|
| Wildberries | `adapters/wildberries.py` | yes | yes | read | yes |
| Ozon | `adapters/ozon.py` | yes | no | yes | no |
| Yandex Market | `adapters/yandex_market.py` | **no** | no | no | no |

All fixture adapters: `live=false`. Capability-driven fail closed.

## Selective export

`MarketplaceSelection` required. Empty/absent → `MARKETPLACE_SELECTION_REQUIRED`.
`allow_all_catalog=True` only with explicit authorization.

## Economics

Decimal-only. `calculate_minimum_allowed_price` / `calculate_profitability`.
Missing costs → `UNKNOWN` / `INSUFFICIENT_DATA` — no invented fees.
Platform-funded discounts ≠ seller loss (`PROMO_PLATFORM`).

## Auto-correct

Modes: `MONITOR_ONLY` (default), `RECOMMEND`, `APPROVAL_REQUIRED`, `AUTO_CORRECT`.
Requires: capability PRICE_WRITE + policy + authorization + bounds + grounded economics.
Yandex fixture forces operator alert path (no price write).

## Loop prevention

`PriceSyncLedger` causation ACK → `MARKETPLACE_SYNC_LOOP_TERMINATED`.
Repeated external override → `REPEATED_EXTERNAL_OVERRIDE` alert; stop fight.

## Handoffs

- Content → Content Factory (`content_intel`)
- Media → Product Media Pipeline
- SEO → SEO Platform (channel card optimization only)
- Orders → canonical commerce `ingest_order`

## How to add a marketplace adapter

1. Subclass `FakeMarketplaceAdapter` or implement `MarketplaceAdapter`.
2. Declare distinct `capabilities()`.
3. Register in `MarketplacePlatformService._adapters`.
4. Keep `live=false` until real Tool Platform credentials exist.

## How to add a selection profile

Use `new_selection(product_ids=..., sku_ids=..., brands=..., category_ids=...)`.

## How to add a min-price policy

Construct `MarketplaceMinPricePolicy` with margin/cost inclusion flags; pass to `minimum_price` / `profitability`.

## Tests

`tests/test_marketplace_platform_closure.py`

## Out of scope

Live WB/Ozon/Yandex credentials, ad bidding, Telegram transport, marketplace Phase 2.

---

## Block 5.7A — canonical cross-provider Marketplace Platform bridge

The section above documents the pre-existing economics/price-protection
engine (`marketplace.service.MarketplacePlatformService`,
`marketplace.adapters.*` `FakeMarketplaceAdapter`s) — that subsystem is
unchanged by 5.7A and remains the home for 5.8 price-protection concerns
(min-price policy, promotion risk, auto-correct, competitor pricing).

5.7A adds a **separate, additive** module, `marketplace/platform.py`, which
is the canonical Marketplace Platform contract required by spec Block 5.7:

```
Panda Product Intelligence (5.5, canonical)
    -> marketplace.platform.MarketplacePlatform   (5.7 — this module)
        -> IntegrationActivationService.execute_via_gateway  (5.4, governed)
            -> integrations.{wildberries,ozon,yandex_market} adapters
```

Unlike the economics engine above, `MarketplacePlatform` never talks to a
`FakeMarketplaceAdapter` directly — every read/write goes through the same
governed `IntegrationActivationService` gateway Block 5.6 (Bitrix) uses,
against the **real** per-provider FIXTURE/LIVE adapters already registered
in `integrations.activation.providers` (`WILDBERRIES`/`OZON`/
`YANDEX_MARKET`).

### Canonical domain (`marketplace/models.py`, additive section at the
bottom of the file)

`MarketplaceOffer`, `MarketplaceCategoryMap`, `MarketplaceAttribute`,
`ListingReadinessResult`, `MarketplacePrice`, `MarketplaceStock`,
`MarketplaceWarehouse`, `MarketplaceOrder`/`MarketplaceOrderItem`/
`MarketplaceOrderStatus`, `MarketplaceShipment`, `MarketplaceSyncState`, plus
the `ORDER_STATUS_*` and `DIFF_*` constant vocabularies. `MarketplaceAccount`
and `MarketplaceListing` are **reused as-is** from the existing section
above — not duplicated.

### Provider profile (`marketplace/platform.py`)

One small, explicit per-provider vocabulary table (`_PROFILES`) captures the
only real differences between Wildberries/Ozon/Yandex Market: capability
strings (reused unchanged from `integrations.activation.providers`), the
SKU parameter name (`seller_article` vs `shop_sku`), lookup/create/update
operation names, and the raw→canonical order-status map. Everything else —
governance, idempotency, readiness, diff — is one shared implementation.

### Category mapping / readiness

`MarketplaceCategoryMapStore` is an explicit, tenant+provider-scoped
lookup/upsert store (`UNMAPPED` until explicitly mapped) — never a
hardcoded "category X → vendor Y" rule. `validate_listing_readiness(...)`
returns a structured `ListingReadinessResult` (product identity, SKU,
mapped category, title, price, stock, required attributes); `publish_listing`
re-validates and fails closed (`MARKETPLACE_NOT_READY`) rather than trusting
the caller.

**Known provider limitation**: the underlying FIXTURE adapters' own
`card_create`/`card_import`/`offer_submission` resolve `category_id` via
their *own* internal `map_category(canonical_category_id=...)` helper
(`integrations.{provider}.mapping`), which has no hook for a caller-supplied
override map. `publish_listing` therefore passes the Panda canonical
category through (not our `external_category_id`) so that internal lookup
still resolves; our own category-map store remains the source of truth for
readiness/diff. Wiring such an override into those adapters is out of scope for
5.7A (they are part of the existing CLOSED marketplace-adapter surface).

### Price / stock / orders

`read_price`/`write_price`, `read_stock`/`write_stock` are thin governed
wrappers that normalize into `MarketplacePrice`/`MarketplaceStock`; they do
not decide whether a price is economically safe (that is the pre-existing
5.8-adjacent economics engine documented above, untouched here). Every
provider's write already carries its own idempotency, price-floor and
warehouse-boundary checks (unchanged, reused).

`read_orders` normalizes each raw order row into `MarketplaceOrder` +
`MarketplaceOrderItem` + `MarketplaceOrderStatus`, preserving
`raw_reference`/provider status/substatus. Any status not in a provider's
mapping table normalizes to `ORDER_STATUS_UNKNOWN` rather than raising.

### Sync / diff

`classify_sync_diff(panda_state, marketplace_state, category_mapped)`
returns one of `IN_SYNC` / `PANDA_NEWER` / `MARKETPLACE_NEWER` / `CONFLICT`
/ `MISSING_IN_PANDA` / `MISSING_IN_MARKETPLACE` / `INVALID` / `UNMAPPED`.
`diff_price(...)` is the governed end-to-end example (reads the remote
price, classifies against a caller-supplied Panda-side value). Conflicts are
returned as structured `MarketplaceSyncState`, never auto-resolved.

### Governance / idempotency / tenant isolation

All reads/writes route through `IntegrationActivationService.execute_via_gateway`
— the same `approved_write`/`idempotency_key` contract, tenant-scoped
connection resolution, and telemetry (`_evidence`) Block 5.6 already uses.
`bulk_sync_gate(...)` reuses `product_intel.planner.assert_sync_product_allowed`
for the existing large-batch admission gate (spec sections 16/21) — no
second batch/approval system.

### Error taxonomy (`marketplace/errors.py`)

`classify_provider_error(exc)` normalizes any adapter/governance exception
into the section-19 vocabulary (`MARKETPLACE_AUTHENTICATION_FAILED`/
`_AUTHORIZATION_FAILED`/`_VALIDATION_FAILED`/`MARKETPLACE_NOT_FOUND`/
`_CONFLICT`/`_TIMEOUT`/`_PROVIDER_UNAVAILABLE`/`_TRANSIENT_PROVIDER_ERROR`/
`_PERMANENT_PROVIDER_ERROR`), reusing `integrations.production.errors.
ProviderErrorCategory` and the existing adapter exception *type names*
(these classes are raised with a call-site-specific reason string as their
`code`, so classification is by exception type, not by `.code` string).

### LIVE mode

Unchanged: `integrations.{wildberries,ozon,yandex_market}.live_adapter`
already fail closed (`IntegrationNotConfiguredError`) without real
`*_API_TOKEN`/`*_API_KEY`/`*_OAUTH_TOKEN` configuration, and writes remain
blocked pending 5.7B production acceptance. See `.env.example` for the
(empty-valued) configuration names each provider will need then.

### Tests

`tests/test_marketplace_platform_5_7a.py`.
