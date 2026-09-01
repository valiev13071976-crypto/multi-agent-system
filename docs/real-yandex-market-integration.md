# Real Yandex Market Integration

Production-ready Yandex Market Partner API integration layer for Panda Multi-Agent.

## Architecture

```
User / BA → Workflow → ToolGateway → Integration Activation → Yandex Market Adapter → Partner API
```

## Reused Architecture

- **Marketplace Platform** — minimum-price policy, profitability, promotion risk
- **Integration Activation** — connection lifecycle, gateway execution, tenant isolation
- **Wildberries / Ozon** — architectural siblings (not cloned semantics)

## Modes

| Mode | Behavior |
|------|----------|
| `FIXTURE` | Deterministic catalog, zero network |
| `SANDBOX` | When configured |
| `LIVE` | Requires OAuth token + mode; fail closed; no FIXTURE fallback |

## Configuration

| Variable | Purpose |
|----------|---------|
| `YANDEX_MARKET_INTEGRATION_MODE` | FIXTURE / SANDBOX / LIVE |
| `YANDEX_MARKET_API_BASE_URL` | Governed API host |
| `YANDEX_MARKET_OAUTH_TOKEN` | OAuth token reference |
| `YANDEX_MARKET_TIMEOUT_SECONDS` | HTTP timeout |
| `YANDEX_MARKET_VERIFY_TLS` | TLS verification |

## Business / Campaign Identity

Explicit scope — never conflated:

| Field | Role |
|-------|------|
| `business_id` | Yandex business identifier |
| `campaign_id` | Campaign/shop scope (distinct from business_id) |
| `offer_id` | Seller offer identifier |
| `shop_sku` | Seller SKU |
| `market_sku` | Provider-assigned SKU |
| `warehouse_id` | Fulfillment warehouse |

## Capabilities

### READ
- Offer lookup, price, stock, orders, promotion analysis, submission status

### WRITE (governed)
- Price update (Marketplace Platform floor)
- Stock update (explicit warehouse + DBS/FBY boundary)
- Offer submission (async — SUBMITTED ≠ PUBLISHED)
- Selective export with partial failure

### Deferred
- LIVE mutating writes during engineering closure
- Promotion JOIN/LEAVE without confirmed API semantics
- Order status mutation

## Price Floor

`enforce_price_floor()` reuses Marketplace Platform — zero provider writes below floor.

## Engineering vs Live

| Flag | Meaning |
|------|---------|
| `YANDEX_MARKET_ENGINEERING_READY=true` | Fixture E2E proven |
| `YANDEX_MARKET_LIVE_ACTIVE=false` | Expected without live credentials |
| `YANDEX_MARKET_LIVE_VERIFIED=false` | Expected without live verification |

**YANDEX_MARKET_ENGINEERING_READY does not mean YANDEX_MARKET_LIVE_ACTIVE.**

## Key Files

- `integrations/yandex_market/` — adapter layer
- `integrations/activation/service.py` — gateway wiring
- `tests/test_real_yandex_market_integration_closure.py` — closure E2E
