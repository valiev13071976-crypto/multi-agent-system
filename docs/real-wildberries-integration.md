# Real Wildberries Integration

Production-ready Wildberries Seller API integration layer for Panda Multi-Agent.

## Architecture

```
User / BA → Workflow → ToolGateway → Integration Activation → Wildberries Adapter → WB Seller APIs
```

The adapter is an integration boundary — not a second Marketplace Platform. Canonical business logic, pricing policy, and content generation remain upstream in closed Panda modules.

## Reused Architecture

- **Marketplace Platform** — minimum-price policy, profitability, promotion risk (`marketplace/economics.py`, `price_guard.py`)
- **Integration Activation** — connection lifecycle, gateway execution, tenant isolation
- **Product Media / Content / SEO** — handoff only; no generation in adapter

## Modes

| Mode | Behavior |
|------|----------|
| `FIXTURE` | Deterministic catalog, zero network |
| `SANDBOX` | When configured |
| `LIVE` | Requires token + mode; fail closed; no FIXTURE fallback |

## Configuration

| Variable | Purpose |
|----------|---------|
| `WILDBERRIES_INTEGRATION_MODE` | FIXTURE / SANDBOX / LIVE |
| `WILDBERRIES_API_BASE_URL` | Governed API host |
| `WILDBERRIES_API_TOKEN` | Secret reference (env/secret store) |
| `WILDBERRIES_TIMEOUT_SECONDS` | HTTP timeout |
| `WILDBERRIES_VERIFY_TLS` | TLS verification |

## Secret / SSRF Policy

- Token resolved at call time only; never logged, returned, or stored in evidence
- Live HTTP calls restricted to allowlisted Wildberries hosts
- User/conversational input cannot set host, port, or credentials

## Capabilities

### READ
- Card/product lookup (nmID, chrtID, seller article, barcode)
- Price (base, seller discount, effective seller price, platform promo distinction)
- Stock per warehouse (FBS semantics)
- Orders (fixture summaries)
- Promotion analysis (provider-controlled vs seller-controlled)

### WRITE (governed)
- Price update with Marketplace Platform floor enforcement
- Stock update (exact warehouse required)
- Card create/update
- Selective export with partial failure

### Unsupported / Deferred
- LIVE mutating writes during engineering closure
- Direct stock mutation without warehouse mapping
- Platform-funded promotion control (READ_ONLY / alert only)
- Arbitrary order status mutation

## Minimum Price Protection

Before Panda-originated price WRITE, adapter calls `enforce_price_floor()` which reuses `calculate_minimum_allowed_price()` from Marketplace Platform. Proposed price below floor → `MARKETPLACE_PRICE_FLOOR` → zero external WRITE.

## Seller vs Platform Promotion

- **Seller discount** — seller-controlled; subject to floor on WRITE
- **Platform promo** — provider-controlled; Panda detects and alerts; does not mutate seller price blindly

## 1C / Bitrix Boundaries

No direct adapter-to-adapter sync. Cross-system flows: source → Panda canonical → Marketplace Platform → governed Wildberries operation.

## Excel → Wildberries

Excel → Data Intelligence → normalized rows → selective operation → preview → governed WRITE. No blind full-catalog publish.

## Engineering vs Live

| Flag | Meaning |
|------|---------|
| `WILDBERRIES_ENGINEERING_READY=true` | Fixture E2E proven |
| `WILDBERRIES_LIVE_ACTIVE=false` | Expected without live credentials |
| `WILDBERRIES_LIVE_VERIFIED=false` | Expected without live verification |

## Key Files

- `integrations/wildberries/` — adapter layer
- `marketplace/adapters/wildberries.py` — existing Marketplace Platform fixture (complementary)
- `integrations/activation/service.py` — gateway wiring
- `tests/test_real_wildberries_integration_closure.py` — closure E2E
