# Real 1C Integration

Production-ready 1C integration layer for Panda Multi-Agent.

## Architecture

```
User → Business Assistant → Workflow → ToolGateway → Integration Activation → 1C Adapter → configured 1C endpoint
```

The adapter handles transport, authentication, protocol translation, and canonical entity mapping. It does **not** contain business planning, pricing strategy, or autonomous approval.

## Adapter Boundary

| In scope | Out of scope |
|----------|--------------|
| HTTP/REST transport | Agent reasoning |
| Auth resolution | SEO/content generation |
| Nomenclature/price/stock/order mapping | Excel intelligence |
| Pagination, errors, verification | Marketplace strategy |
| Tenant-scoped identity | Autonomous approval |

## Transports

| Transport | Status |
|-----------|--------|
| `http_rest` | Supported (primary) |
| `odata` | Supported (structural) |
| `commerceml` | Readiness boundary only — automatic WRITE deferred |

SSRF protection: adapter calls only configured `ONEC_BASE_URL` host.

## Modes

| Mode | Behavior |
|------|----------|
| `FIXTURE` | Deterministic in-memory catalog, no network |
| `SANDBOX` | Staging when configured |
| `LIVE` | Requires explicit config; fail closed; no FIXTURE fallback |

## Configuration

| Variable | Purpose |
|----------|---------|
| `ONEC_INTEGRATION_MODE` | FIXTURE / SANDBOX / LIVE |
| `ONEC_BASE_URL` / `ONEC_API_URL` | Authorized endpoint |
| `ONEC_TRANSPORT` | http_rest / odata / commerceml |
| `ONEC_AUTH_MODE` | basic / bearer / oauth |
| `ONEC_USERNAME` | Username secret reference |
| `ONEC_PASSWORD` | Password secret reference |
| `ONEC_TOKEN` / `ONEC_API_TOKEN` | Bearer token reference |
| `ONEC_CLIENT_ID` / `ONEC_CLIENT_SECRET` | OAuth references |
| `ONEC_TIMEOUT_SECONDS` | HTTP timeout |
| `ONEC_VERIFY_TLS` | TLS verification |
| `ONEC_CATALOG_ID` | Catalog/database ID |
| `ONEC_ORGANIZATION_ID` | Organization scope |
| `ONEC_WAREHOUSE_MAPPINGS` | Panda ↔ 1C warehouse mapping |
| `ONEC_PRICE_TYPE_MAPPINGS` | Price type mapping |

## Secret Policy

Credentials resolved at call time via `secret:` refs only. Never logged, returned, persisted in evidence, or embedded in exceptions.

## Capabilities

### READ
- Nomenclature/product lookup (GUID, XML ID, article, Panda mapping)
- Variant/characteristic identity preserved
- Price read (price type, currency, effective date)
- Stock read (available/physical/reserved per warehouse)
- Order/document read
- Warehouse list

### WRITE (governed)
- Price update with preview, approval, idempotency, verify-after-write
- Document create (draft, not auto-posted)
- Selective export from canonical rows

### Unsupported / Deferred
- Direct stock mutation (requires 1C document semantics)
- Document posting (separate capability — deferred)
- LIVE mutating writes during engineering closure
- CommerceML automatic import WRITE
- Counterparty PII replication

## HITL / Idempotency / Verification

All WRITE operations require `approved_write=True` and idempotency key at Integration Activation boundary. Preview shows before/after. Verify-after-write for price mutations. Uncertain write outcomes are not blindly retried.

## 1C → Bitrix / Marketplace Boundary

No direct adapter-to-adapter sync. Cross-system flows go through Panda canonical data → Workflow → governed target integration.

## Excel → 1C

Excel artifact → Excel/Data Intelligence → normalized rows → selective operation → BA/Workflow → 1C adapter. No blind workbook import.

## XML Security

CommerceML parser rejects DOCTYPE/ENTITY declarations and enforces size bounds.

## Inbound Events

`integrations/onec/webhooks.py` provides verify, dedupe, normalize with `NO_DIRECT_WRITE` policy.

## Engineering vs Live

| Flag | Meaning |
|------|---------|
| `ONEC_ENGINEERING_READY=true` | Fixture E2E proven |
| `ONEC_LIVE_ACTIVE=false` | No verified production connection |
| `ONEC_LIVE_VERIFIED=false` | Expected without live credentials |

## Activation (no credentials in repo)

1. Set `ONEC_INTEGRATION_MODE=LIVE`
2. Configure `ONEC_BASE_URL` and auth secrets via secret infrastructure
3. Configure tenant connection with `secret:` credential ref
4. Verify via Integration Activation lifecycle
5. Controlled live mutation is a separate operational step

## Key Files

- `integrations/onec/` — adapter layer
- `integrations/activation/service.py` — gateway wiring
- `commerce/product_platform/one_c.py` — existing product platform fixture (reused conceptually)
- `tests/test_real_1c_integration_closure.py` — closure E2E
