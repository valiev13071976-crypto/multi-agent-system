# E-commerce / Product Platform

## Architecture

One canonical commerce core under `commerce/product_platform/` (Block 11). External systems are adapters — not separate product cores.

```
Files/Excel/1C/Supplier
        ↓
 SourceProductSnapshot
        ↓
 Match (SKU/EAN/title — ambiguous never auto-merges)
        ↓
 ProductVersion (+ SKU identifiers) / Category / Brand
   ↙        ↓         ↘
Price    Stock      Attributes
   ↘        ↓         ↙
        Catalog
   ↙     ↓      ↘
Content Media   SEO   (handoffs)
        ↓
 Cart → Checkout revalidation → Order
   ↙        ↓         ↘
Payment  Delivery  Fulfillment (ops/payments packages)
```

Integrations:

```
        PANDA
     ↙         ↘
   1C (fake)   Bitrix (fake-bitrix)
                  ↓
            Aspro Premier profile (config only)
```

## Ownership

Panda owns canonical Product/SKU/price/stock/order state.  
Bitrix/Aspro/1C are Tool Platform adapters with field ownership policy.

## Product vs SKU

`ProductVersion` is the product master; SKU/EAN bound via identifiers. Variants supported on the contract. Ambiguous title matches return `MATCH_AMBIGUOUS` — no destructive merge.

## Import

`import_preview` (DRY_RUN) → `import_products` apply with checkpoint. Excel/Data Intelligence supplies rows — no second spreadsheet engine.

## Fact lock

Enrichment/content must not invent commercial claims (`COMMERCE_FACT_UNSUPPORTED`).

## Handoffs

- Content → `content_handoff` → Content Factory  
- Media → `media_handoff` → Product Media Pipeline  
- SEO → `seo_handoff` → SEO Platform  

## Price / stock

Decimal `MoneyAmount`; floor/margin policy; multi-location inventory; optimistic `try_reserve` / `release_stock` (oversale → `COMMERCE_OVERSELL`).

## Cart / checkout

`create_cart_checkout` snapshots prices; revalidation returns explicit `PRICE_CHANGED` / out-of-stock — no silent reprice.

## Orders

Idempotent `ingest_order` by external_ref; state machine including `RETURNED` after `FULFILLED`.

## Sync / loop prevention

`FieldOwnershipPolicy` + `CommerceSyncPlan` + `SyncEventLedger`: Panda-origin reflected events ACK and terminate (`COMMERCE_SYNC_LOOP_TERMINATED`).

## Aspro Premier

`AsproPremierProfile` maps fields onto Bitrix payloads — not `AsproCore`.

## 1C

`FakeOneCAdapter` — fixture only (`live: false`).

## Marketplace readiness

`MarketplaceExportView` for future Marketplace Platform — no marketplace APIs here.

## How to add a supplier source

Produce rows → `ingest_source_snapshot` → `match_product` → import apply.

## How to add Bitrix/Aspro field mapping

Extend `fixture_aspro_premier_profile` / register a new `AsproPremierProfile`.

## How to add a 1C profile

Extend `FakeOneCAdapter.normalize_product` / replace with live Tool Platform adapter.

## How to add payment/delivery providers

Use `payments/` and ops commerce gateways — not product_platform core.

## Tests

- `tests/test_commerce_block11_platform.py`
- `tests/test_commerce_block11_p1_closure.py`
- `tests/test_commerce_expansion_closure.py`

See `docs/commerce-bypass-audit.md`.
