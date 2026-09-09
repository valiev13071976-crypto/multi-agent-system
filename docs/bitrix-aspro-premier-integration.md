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
| `BITRIX_OFFERS_IBLOCK_ID` | Offers/SKU IBLOCK ID (this installation: `15`) — required for `offer_read` and any offer/SKU CREATE |
| `BITRIX_RETAIL_PRICE_TYPE_ID` | `catalogGroupId` for this installation's RETAIL selling price (see `catalog.priceType.list`) — required for any real retail-price CREATE/write; fails closed (never guesses e.g. `1`) if unset |
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
- LIVE mutating writes for every operation OTHER than the one governed,
  single-product `product_create` used by
  `business_assistant.controlled_bitrix_write` (product/offer
  update, price update, stock update, media attach, SEO update, publish
  all still raise the pre-existing `bitrix_live_write_blocked_engineering`
  placeholder in LIVE — see "First Controlled Production Write" below for
  what IS implemented)
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

## Standalone Production Verification (Block 5.6 final defect closure)

`BitrixProductBridge.verify_schema_binding(...)` always routes through
`IntegrationActivationService.execute_via_gateway`, which requires an
existing, active `IntegrationConnection` record — a bare
`IntegrationActivationService()` has none by default, even when
`LiveBitrixAdapter`/`BitrixIntegrationConfig` are already correctly
configured from protected env. `integrations/bitrix/production_verification.py`
adds the one missing seam, reusing the exact same governed
`configure_connection` → `verify_connection` → `activate_connection`
lifecycle every other provider already uses (no new connector):

- `ensure_live_bitrix_connection(activation, tenant_id=...)` — idempotently
  registers/activates a LIVE Bitrix connection from existing env config.
  Fails closed if LIVE isn't actually configured; never fabricates
  activation. The connection's `credential_ref` is a fixed, non-secret
  reference name — the real webhook URL is still resolved exclusively by
  `LiveBitrixAdapter` from protected environment configuration.
- `run_production_schema_verification(bitrix_product_id="477")` — the
  single supported standalone entry point: bootstraps (or reuses) the LIVE
  connection, then runs the bounded READ-only schema-binding verification.

This does not change `execute_via_gateway`/`resolve_connection` or any
other provider's behavior — see
`tests/test_bitrix_production_verification_bootstrap.py`.

## First Controlled Production Write — real LIVE `product_create`

`LiveBitrixAdapter.write()` implements exactly one real LIVE operation,
`product_create` — the only one `business_assistant.controlled_bitrix_write.
execute_single_product_write` ever calls for its governed, single-product,
approval-gated flow. Every other write operation is unchanged (see
"Deferred / Unsupported" above).

**REST methods used** (same `catalog` scope, same `BitrixHttpClient`
transport, every other LIVE read already uses — no second HTTP
client/architecture):

| Step | REST method | Purpose |
|------|-------------|---------|
| idempotency check | `catalog.product.list` (filter `xmlId`) | has this idempotency key already created a product? |
| product create | `catalog.product.add` | base product: `name`, `active`, BRAND (`property100`); response nests the created product under `"element"` (per Bitrix's documented contract — NOT `"product"`) |
| idempotency check | `catalog.product.offer.list` (filter `parentId`) | does this product already have an offer? |
| offer create | `catalog.product.offer.add` | SKU/article (`property283`) linked via `parentId` |
| idempotency check | `catalog.price.list` (filter `productId`+`catalogGroupId`) | is the retail price already recorded? |
| price create | `catalog.price.add` | retail selling price only |
| read-back | `catalog.product.list` (filter `id`) | independent, governed verification (unchanged, pre-existing) |

**Fields written** (only fields with a schema-verified destination —
never a guessed property ID/code):
- `name` → base product name (IBLOCK 14)
- SKU/article → offer property 283 (`ARTICLE`, IBLOCK 15) — this
  installation's schema binding (`integrations/bitrix/schema.py`) verifies
  ARTICLE only on the OFFERS IBLOCK, not the base product, so a real SKU
  requires creating one offer per product here
- brand → catalog property 100 (`BRAND`, IBLOCK 14)
- retail selling price → `catalog.price.add` against `BITRIX_RETAIL_PRICE_TYPE_ID`

**Never written**: EAN, purchase price, category/section — no verified
destination exists for these on this installation; `controlled_bitrix_write`
never includes them in the payload this adapter reads, so there is nothing
to guess.

**Product visibility**: created `active="N"` unless the caller explicitly
passes `active=True` — `controlled_bitrix_write` always passes `active=False`
for this first controlled write.

**Idempotency/duplicate protection**: a fresh `LiveBitrixAdapter` instance
is constructed on every `execute_via_gateway` call (see
`IntegrationActivationService._adapter_for`), so nothing here relies on
in-process adapter memory surviving a retry. Instead each step checks
Bitrix itself (via the idempotency-check reads above) before creating —
the product is tagged with a deterministic `xmlId` derived from the
caller's idempotency key, and the offer/price steps are matched by
parent/product id. A retry with the same idempotency key never creates a
second product, offer, or price row; if an earlier attempt partially
failed (e.g. product created but offer/price failed), the retry only
performs the remaining step(s).

**Partial failure**: if product creation succeeds but a later required
step (offer or price) fails, the result reports `PARTIAL_FAILURE` with the
already-created Bitrix product ID and the specific `failed_step` — never
silently reported as success, and the product is never recreated on
retry.

See `tests/test_bitrix_live_product_create_write.py` for full deterministic
coverage (mocked HTTP transport only — zero real network calls).

### Production defect closure — `catalog.product.list` HTTP 400 on the idempotency lookup

The first real LIVE controlled write hit a real Bitrix `400 Bad Request`
on the very first step above (`catalog.product.list` filtered by
`xmlId`), so `catalog.product.add` was never reached and nothing was
created. Root cause: Bitrix's documented REST contract for
`catalog.product.list` **and** `catalog.product.offer.list` requires both
`"id"` and `"iblockId"` to be present in the `select` array (not merely
usable in `filter`) — omitting either returns error `200040300010`
("Fields id, iblockId are not specified in the selection fields") over
HTTP 400. The idempotency-lookup `select` lists for both methods omitted
`"iblockId"`. Fixed by adding it to both; no other request shape,
filter field, or JSON encoding was wrong. Separately, `BoundedHttpClient`
(the shared transport every production provider adapter uses) discarded
the response body entirely on any 4xx/5xx before raising, so the actual
Bitrix `error`/`error_description` could never reach the caller — every
such failure collapsed into a bare category name (e.g. `BAD_REQUEST`).
Fixed generically (not Bitrix-specific) by attaching a bounded response
body to `ProductionProviderError.metadata`; `BitrixHttpClient.call` now
parses it and surfaces the real, bounded `error`/`error_description` in
`BitrixIntegrationError` messages instead of only the category name.
Regression coverage: `tests/test_bitrix_live_product_create_write.py`'s
`ProductionRegression400Tests` (mocked transport that enforces the real
Bitrix required-`select`-field contract, plus a diagnostics assertion
that a genuine 400 now surfaces `error_description`/error code instead of
bare `BAD_REQUEST`).

### Production defect closure — `product_create_malformed_response` despite HTTP 200

After the above fix shipped, the first real LIVE controlled write got past
the idempotency lookup and reached `catalog.product.add` — both
`catalog.product.list` and `catalog.product.add` returned HTTP 200 — yet
Panda reported `product_create_malformed_response (BitrixValidationError)`
and no product appeared in Bitrix. Root cause: Bitrix's documented REST
contract for `catalog.product.add` nests the created product under
**`"element"`**, not `"product"` (unlike `catalog.product.offer.add` ->
`"offer"` and `catalog.price.add` -> `"price"`, which were already
correct). The adapter's own fail-closed check (never treat HTTP 200 alone
as success; only a concretely extracted id counts) worked exactly as
designed — it correctly refused to claim success it could not verify —
but was checking the wrong key, so a genuinely successful create was
never recognized. Fixed by extracting the created product id from
`result.element.id` instead of `result.product.id`; the offer/price
extraction keys were already correct and untouched. Also adds a bounded,
sanitized diagnostic log (`bitrix_malformed_create_response`, REST
method + top-level/`result` key names + Bitrix `error`/`error_description`
if present — never the webhook URL, credentials, or authorization data)
whenever any of the three create steps returns HTTP 200 without a
recognizable id, so any future contract mismatch is immediately
observable in application logs without waiting for another production
report. Regression coverage: `tests/test_bitrix_live_product_create_write.py`'s
`MalformedHttp200ResponseTests` (reproduces the exact wrong-key defect,
an empty/malformed `result`, and confirms the real `"element"` shape is
now recognized as success — plus that no false `WRITE_VERIFIED` is ever
returned without a concrete id).

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
