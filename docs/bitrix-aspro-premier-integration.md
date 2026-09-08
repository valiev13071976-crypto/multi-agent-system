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
| `BITRIX_ACCOUNT_LOGIN` | **Non-secret** account/login identifier (e.g. the Bitrix account owner's login) — identifies *which* account is connected, never *how* to authenticate. Safe in metadata/logs/UI. |
| `BITRIX_WEBHOOK_URL` | Webhook URL (secret — env/secret store only) |
| `BITRIX_CLIENT_ID` | OAuth client ID reference |
| `BITRIX_CLIENT_SECRET` | OAuth secret reference |
| `BITRIX_TIMEOUT_SECONDS` | HTTP timeout |
| `BITRIX_VERIFY_TLS` | TLS verification (default true) |
| `BITRIX_CATALOG_ID` | Products IBLOCK ID (this installation: `14`) |
| `BITRIX_OFFERS_IBLOCK_ID` | Offers/SKU IBLOCK ID (this installation: `15`) — required for `offer_read` |
| `BITRIX_SITE_ID` | Site identifier |
| `ASPRO_PREMIER_ENABLED` | Enable Aspro field mapping |
| `ASPRO_PREMIER_FIELD_MAPPINGS` | Optional mapping config reference |

Never hardcode URLs, tokens, or license keys in code or SQLite business records.
`BITRIX_ACCOUNT_LOGIN` is the one exception documented above: it is a
plain identifier, not a secret, so it may be set directly as configuration
(surfaced via `BitrixIntegrationConfig.safe_metadata()`'s `account_login`
field). Setting it alone does **not** satisfy `live_configured` — a
webhook URL or OAuth client id/secret must still be supplied through
protected environment/secrets before any LIVE call is attempted.

## Secret Policy

- Credentials resolved only at provider call time via `secret:` references
- Never logged, returned via API, persisted in action evidence, or included in exceptions
- Plaintext credential refs rejected at configuration boundary

## Supported Capabilities

### READ
- Catalog/product list (paginated)
- Section/category list (external IDs — never replace Panda canonical category IDs)
- Product lookup by Bitrix ID, XML ID, article/SKU, Panda mapping
- Price read (with price type/currency)
- Stock read
- Order read (fixture normalized summaries)

### WRITE (governed — approval + idempotency required)
- Product create/update
- Price update with verify-after-write
- Stock update with verify-after-write
- Media attach (idempotent — never re-attaches an already-associated ref)
- SEO title/description update
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

## Real Production Schema Binding (panda.msk.ru)

`integrations/bitrix/schema.py` binds this connector to the REAL, installed
panda.msk.ru schema — catalog IBLOCK `14`, offers IBLOCK `15` — without a
second connector/architecture:

- **Known real properties** (catalog IBLOCK 14: 97–115, 136; offers IBLOCK
  15: 278–297) are declared with an explicit ownership label
  (`PANDA_MANAGED` / `BITRIX_MANAGED` / `ASPRO_MANAGED` / `DERIVED` /
  `READ_ONLY` / `UNMANAGED_PRESERVE`). Any property NOT in this table is
  conservatively treated as `UNMANAGED_PRESERVE` — never assumed writable.
- **CML2_LINK (property 279)** models the offer → parent-product
  relationship. The REST method `catalog.product.offer.list` exposes this
  same relationship as a `parentId` filter/select field, not a raw
  `property279` value — offer ID is never assumed to equal product ID.
- **Category/section resolution is dynamic**: `resolve_section_ancestors()`
  walks the real `catalog.section.list` parent chain for whatever section a
  product actually has — there is no hardcoded "product X is category Y"
  assumption anywhere in this binding.
- **Prices** (`catalog.price.list`) are kept one row per
  `(productId, catalogGroupId)` pair — regional/price-type variants (e.g.
  base retail vs. MSC/EKB/MAGNITOGORSK) are never collapsed into one value.
- **SEO**: `seo_effective_status()` reports explicit per-product SEO
  overrides when present, and explicitly classifies *inherited/effective*
  IPROPERTY SEO as unavailable via `catalog.product.list` (a genuine
  REST-surface gap, not a missing scope) — it is never fabricated.
- `LiveBitrixAdapter.read()` supports `product_lookup`, `section_read` /
  `category_read`, `offer_read`, and `price_read` operations against the
  real self-hosted REST surface (`catalog.product.list`,
  `catalog.section.list`, `catalog.product.offer.list`,
  `catalog.price.list` — all scope `catalog`, already granted), each
  failing closed (no network call) without the required IBLOCK
  configuration.
- `BitrixProductBridge.verify_schema_binding(...)` runs the full bounded,
  READ-ONLY verification sequence for one known product through the
  existing governed gateway and returns a report with an explicit
  `limitations` list for anything the current REST scope/surface cannot
  prove (never silently fabricated).

## Product Intelligence Bridge (Block 5.6)

`integrations/bitrix/product_bridge.py`'s `BitrixProductBridge` is the one
explicit seam between Block 5.5 Product Intelligence (`product_intel/`, kept
vendor-neutral) and this Bitrix connector. It never bypasses
`IntegrationActivationService.execute_via_gateway` (no raw HTTP client
access) and provides:

- `import_catalog(...)` — bounded/paginated Bitrix → canonical Product
  Intelligence import; each Bitrix offer becomes its own canonical
  `Product` row (variants never collapse), external Bitrix
  product/offer IDs are preserved via the existing
  `BitrixCatalogStore.bind_mapping`/`get_mapping` persistence (no new
  database technology).
- `plan_sync(...)` — sync diff before any mutation: `CREATE` / `UPDATE` /
  `UNCHANGED` / `AMBIGUOUS` / `INVALID`. `UNCHANGED` never triggers a remote
  write.
- `sync_product(...)` / `sync_price(...)` / `sync_stock(...)` /
  `associate_media(...)` / `sync_seo(...)` — governed writes from canonical
  Product Intelligence fields, each going through the same
  capability/approval/idempotency path as every other Bitrix write. Unknown
  stock is never written as zero.
- `bulk_sync(...)` — large catalogs must pass `bulk=True` or raise
  `product_intel.errors.ProductBatchRequired`, reusing Block 5.5's existing
  batch-admission gate (`product_intel.planner.assert_sync_product_allowed`)
  instead of a second job queue/worker.

Business Assistant chat surfaces two representative bounded flows through
the existing recipe/action-continuation path (`business_assistant/service.py`,
gated the same way as every other governed external write —
`req.constraints.show_before_publication` + HITL approval before any Bitrix
mutation): catalog import (read-only, no Bitrix mutation) and a
sync-preview → approve → apply flow for a single product, mirroring the
pre-existing `onec_price_preview`/`onec_price_apply` pattern.

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

0. (Optional, non-secret) Set `BITRIX_ACCOUNT_LOGIN` to identify which
   Bitrix account is connected -- informational only, never a credential
1. Set `BITRIX_INTEGRATION_MODE=LIVE`
2. Configure `BITRIX_WEBHOOK_URL` via secret infrastructure
3. Configure tenant connection with `secret:` credential ref
4. Verify connection via Integration Activation lifecycle
5. Enable write capabilities explicitly on connection
6. Controlled live mutation verification is a separate operational step

## Key Files

- `integrations/bitrix/` — adapter, catalog, client, config, mapping, webhooks, `product_bridge.py` (Block 5.6 Product Intelligence bridge)
- `integrations/activation/service.py` — gateway wiring
- `commerce/product_platform/aspro.py` — Aspro profile mapping
- `tests/test_bitrix_aspro_premier_closure.py` — closure E2E
- `tests/test_block5_6_bitrix_aspro_integration.py` — Block 5.6 bridge closure E2E (Acceptance A–U)
