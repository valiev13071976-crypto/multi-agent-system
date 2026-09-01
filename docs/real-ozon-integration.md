# Real Ozon Integration

Production-ready Ozon Seller API integration layer for Panda Multi-Agent.

## Architecture

```
User / BA → Workflow → ToolGateway → Integration Activation → Ozon Adapter → Ozon Seller APIs
```

The adapter is an integration boundary — not a second Marketplace Platform. Canonical business logic, pricing policy, and content generation remain upstream in closed Panda modules.

## Reused Architecture

- **Marketplace Platform** — minimum-price policy, profitability, promotion risk (`marketplace/economics.py`, `price_guard.py`)
- **Integration Activation** — connection lifecycle, gateway execution, tenant isolation
- **Product Media / Content / SEO** — handoff only; no generation in adapter
- **Excel / Data Intelligence** — selective export via normalized rows

## Modes

| Mode | Behavior |
|------|----------|
| `FIXTURE` | Deterministic catalog, zero network |
| `SANDBOX` | When configured |
| `LIVE` | Requires Client-Id + Api-Key + mode; fail closed; no FIXTURE fallback |

## Configuration

| Variable | Purpose |
|----------|---------|
| `OZON_INTEGRATION_MODE` | FIXTURE / SANDBOX / LIVE |
| `OZON_API_BASE_URL` | Governed API host (default `https://api-seller.ozon.ru`) |
| `OZON_CLIENT_ID` | Secret reference (env/secret store) |
| `OZON_API_KEY` | Secret reference |
| `OZON_TIMEOUT_SECONDS` | HTTP timeout |
| `OZON_VERIFY_TLS` | TLS verification |

Credential ref format for resolver: `client_id:api_key`.

## Secret / SSRF Policy

- Client-Id and Api-Key resolved at call time only; never logged, returned, or stored in evidence
- Live HTTP calls restricted to allowlisted Ozon hosts (`api-seller.ozon.ru`, `api.ozon.ru`)
- User/conversational input cannot set host, port, or credentials

## Product Identity

Explicit mapping — never conflated:

| Field | Role |
|-------|------|
| `product_id` | Ozon internal product identifier |
| `offer_id` | Seller offer identifier (distinct from product_id) |
| `seller_article` / `sku` | Seller article |
| `barcode` | GTIN/barcode |
| `panda_product_id` | Internal Panda mapping |

## Capabilities

### READ
- Card/product lookup (product_id, offer_id, seller article, barcode)
- Price (seller_price, old_price, seller discount, customer_visible_price, control ownership)
- Stock per warehouse (FBS/FBO semantics)
- Orders (posting summaries)
- Promotion analysis (platform-controlled vs seller-controlled)
- Import task status (async verification)

### WRITE (governed)
- Price update with Marketplace Platform floor enforcement
- Stock update (exact warehouse + fulfillment boundary)
- Card import (async — SUBMITTED ≠ final success)
- Card update
- Selective export with partial failure
- Price reconciliation after uncertain write

### Unsupported / Deferred
- LIVE mutating writes during engineering closure
- Promotion JOIN/LEAVE without confirmed API semantics
- FBO stock mutation via FBS warehouse target
- Arbitrary order status mutation

## Async Import

Card import returns `status: SUBMITTED`, `verified: ACCEPTED_PENDING`. Terminal states via `import_status` read: `SUBMITTED` → `PROCESSING` → `SUCCEEDED` | `REJECTED`.

HTTP 2xx acceptance of import job is **not** represented as final publication success.

## Minimum Price Protection

Before Panda-originated price WRITE, adapter calls `enforce_price_floor()` reusing `calculate_minimum_allowed_price()` from Marketplace Platform. Below floor → `MARKETPLACE_PRICE_FLOOR` → **zero** external WRITE.

## Seller vs Platform Promotion

- **Seller discount** — seller-controlled; subject to floor on WRITE
- **Platform promo** — provider-controlled; Panda detects and alerts; does not mutate seller price blindly

## FBO / FBS

Fulfillment mode preserved in normalized state. FBS products reject FBO warehouse writes and vice versa.

## 1C / Bitrix Boundaries

No direct adapter-to-adapter sync. Cross-system flows: source → Panda canonical → Marketplace Platform → governed Ozon operation.

## Excel → Ozon

Excel → Data Intelligence → normalized rows → selective operation → preview → governed WRITE. Large batches route through batch infrastructure (`BATCH_ROW_THRESHOLD`).

## Idempotency / Uncertain Writes

All writes require idempotency keys. Timeout/uncertain outcomes raise `INTEGRATION_UNCERTAIN_WRITE_OUTCOME`; reconciliation via read-back or `reconcile_price` operation.

## Engineering vs Live

| Flag | Meaning |
|------|---------|
| `OZON_ENGINEERING_READY=true` | Fixture E2E proven |
| `OZON_LIVE_ACTIVE=false` | Expected without live credentials |
| `OZON_LIVE_VERIFIED=false` | Expected without live verification |

**OZON_ENGINEERING_READY does not mean OZON_LIVE_ACTIVE. OZON_LIVE_ACTIVE does not mean OZON_LIVE_VERIFIED.**

## Key Files

- `integrations/ozon/` — adapter layer
- `marketplace/adapters/ozon.py` — existing Marketplace Platform fixture (complementary)
- `integrations/activation/service.py` — gateway wiring
- `tests/test_real_ozon_integration_closure.py` — closure E2E
