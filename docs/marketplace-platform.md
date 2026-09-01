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
