# Bitrix / Aspro Premier Integration

Engineering-ready integration layer for 1C-Bitrix and Aspro Premier within Panda Multi-Agent.

## Architecture

```
User / Business Assistant
  → Business Assistant (plan, preview, HITL)
  → Durable Workflow
  → ToolGateway
  → Integration Activation
  → Bitrix/Aspro Adapter
  → Bitrix / Aspro Premier site
```

The adapter translates canonical Panda operations into Bitrix REST/webhook calls and normalizes responses. It does **not** contain business planning, SEO strategy, marketplace logic, or autonomous approval decisions.

## Bitrix vs Aspro

| Layer | Role |
|-------|------|
| **1C-Bitrix** | CMS/platform: catalog, products/offers, prices, stock, orders, users |
| **Aspro Premier** | Solution/template profile over Bitrix — field mappings, presentation config |

Aspro is **not** a separate commerce backend. Aspro-specific mapping is configurable via profile (`commerce/product_platform/aspro.py`).

## Connection Modes

| Mode | Behavior |
|------|----------|
| `FIXTURE` | Deterministic in-memory catalog — no network |
| `SANDBOX` | Staging when configured |
| `LIVE` | Real webhook/OAuth — fail closed without credentials |

Rules:
- No automatic FIXTURE fallback from LIVE
- Missing LIVE configuration = fail closed
- Mode visible in safe provider metadata (`live`, `mode`, `live_configured`)

## Configuration (names only)

| Variable | Purpose |
|----------|---------|
| `BITRIX_INTEGRATION_MODE` | `FIXTURE` / `SANDBOX` / `LIVE` |
| `BITRIX_BASE_URL` | Site base URL |
| `BITRIX_AUTH_MODE` | `webhook` or `oauth` |
| `BITRIX_WEBHOOK_URL` | Webhook URL (secret — env/secret store only) |
| `BITRIX_CLIENT_ID` | OAuth client ID reference |
| `BITRIX_CLIENT_SECRET` | OAuth secret reference |
| `BITRIX_TIMEOUT_SECONDS` | HTTP timeout |
| `BITRIX_VERIFY_TLS` | TLS verification (default true) |
| `BITRIX_CATALOG_ID` | Catalog identifier |
| `BITRIX_SITE_ID` | Site identifier |
| `ASPRO_PREMIER_ENABLED` | Enable Aspro field mapping |
| `ASPRO_PREMIER_FIELD_MAPPINGS` | Optional mapping config reference |

Never hardcode URLs, tokens, or license keys in code or SQLite business records.

## Secret Policy

- Credentials resolved only at provider call time via `secret:` references
- Never logged, returned via API, persisted in action evidence, or included in exceptions
- Plaintext credential refs rejected at configuration boundary

## Supported Capabilities

### READ
- Catalog/product list (paginated)
- Product lookup by Bitrix ID, XML ID, article/SKU, Panda mapping
- Price read (with price type/currency)
- Stock read
- Order read (fixture normalized summaries)

### WRITE (governed — approval + idempotency required)
- Product create/update
- Price update with verify-after-write
- Stock update with verify-after-write
- Publish/activate
- Selective export (Excel → subset only)

### Deferred / Unsupported
- LIVE mutating writes during engineering closure (structurally blocked)
- Broad order mutations
- Production media upload (reuse Image/Product Media Pipeline when activated)
- Autonomous price/product changes from conversational requests

## Product Identity

Stable mapping uses:
- Panda product/artifact ID → Bitrix product ID (tenant-scoped)
- Bitrix product ID, offer/SKU ID, XML ID, article/vendor code

Name-only lookup does not authorize WRITE. Ambiguous targets fail closed.

## HITL / Approval / Idempotency

WRITE operations require:
1. `approved_write=True` at Integration Activation boundary
2. Idempotency key binding
3. Preview with before/after where applicable
4. Verify-after-write READ for price/stock/publish/create

Duplicate approval, workflow resume, and HTTP retry return cached idempotent result — no duplicate external mutation.

## HTTP Client

`integrations/bitrix/client.py` wraps `BoundedHttpClient`:
- Timeout, TLS verification, bounded response size
- Normalized 429/timeout/auth errors
- No secret-bearing URL logging
- LIVE dormant without configuration

## Tenant Isolation

All catalog state, mappings, and connections are tenant-scoped. Cross-tenant connection access raises `INTEGRATION_CROSS_TENANT`.

## Observability

Integration Activation emits safe evidence: operation, capability, tenant, duration, status, error category, verification result. No secrets or raw payloads.

FinOps: integration usage recorded with `cost: None` — no invented monetary cost.

## Webhook Readiness

`integrations/bitrix/webhooks.py` provides signature verification, deduplication, normalization, and canonical event routing with `NO_DIRECT_WRITE` policy. Live webhook activation is not required for engineering closure.

## Fixture vs Live Status

| Flag | Engineering closure |
|------|---------------------|
| `BITRIX_ASPRO_ENGINEERING_READY` | Proven by fixture E2E tests |
| `BITRIX_LIVE_ACTIVE` | Requires verified production connection |
| `ASPRO_PREMIER_LIVE_VERIFIED` | Requires live Aspro site verification |

Without production credentials: `BITRIX_LIVE_ACTIVE=false` and `ASPRO_PREMIER_LIVE_VERIFIED=false` — expected, not a closure blocker.

## Activation Procedure (no credentials in repo)

1. Set `BITRIX_INTEGRATION_MODE=LIVE`
2. Configure `BITRIX_WEBHOOK_URL` via secret infrastructure
3. Configure tenant connection with `secret:` credential ref
4. Verify connection via Integration Activation lifecycle
5. Enable write capabilities explicitly on connection
6. Controlled live mutation verification is a separate operational step

## Key Files

- `integrations/bitrix/` — adapter, catalog, client, config, mapping, webhooks
- `integrations/activation/service.py` — gateway wiring
- `commerce/product_platform/aspro.py` — Aspro profile mapping
- `tests/test_bitrix_aspro_premier_closure.py` — closure E2E
