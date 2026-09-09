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
| `BITRIX_RETAIL_PRICE_TYPE_ID` | `catalogGroupId` for this installation's RETAIL selling price (see `catalog.priceType.list`) — required for any real retail-price CREATE/write; fails closed (never guesses e.g. `1`) if unset. **For panda.msk.ru, the business owner has explicitly confirmed the value is `1` (`catalogGroupId 1` / price type name `BASE`) is the intended retail/base selling price** — this is a per-installation configuration fact, not hardcoded into the general integration logic, which remains entirely environment-driven |
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
  **This table is intentionally NOT a complete inventory of the live
  IBLOCK.** A follow-up LIVE READ-ONLY discovery pass found this
  installation actually exposes roughly 179 distinct custom property IDs
  on IBLOCK 14 (97–277) and up to property 298 on IBLOCK 15 — far more
  than are enumerated above. `iblock.property.list` (the REST method that
  would resolve their CODE names) returns `ERROR_METHOD_NOT_FOUND` on this
  installation, so those extra property IDs' semantic meaning cannot
  currently be verified via REST — only properties actually verified are
  listed here; every other one correctly falls through to
  `UNMANAGED_PRESERVE` and is left untouched (existing unmanaged
  Aspro/custom properties are never guessed at or mutated).
- **Property value envelopes are unwrapped centrally.** LIVE
  `catalog.product.list`/`catalog.product.offer.list` responses wrap most
  non-boolean custom property values (including BRAND/`property100`,
  ARTICLE/`property283`, and the CML2_LINK/`parentId` relationship itself)
  as `{"value": ..., "valueId": ...}` — or a list of such envelopes for a
  multi-value property — rather than a bare scalar.
  `schema.unwrap_property_value()` centralizes extracting the real value
  from either shape (envelope or already-scalar, for backward
  compatibility with older/fixture responses); every mapping function in
  `integrations/bitrix/schema.py` routes through it instead of each doing
  its own ad-hoc unwrap.
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
  REST-surface gap, not a missing scope) — it is never fabricated. A
  follow-up LIVE READ-ONLY discovery pass confirmed this installation's
  real responses carry no seo/meta/title/description/keyword-shaped key
  at all — there is currently no writable REST destination for SEO here,
  explicit or inherited; SEO write remains unresolved/deferred.
- **Purchase price** (Block 5.6 follow-up defect closure): the same LIVE
  discovery pass confirmed `catalog.product.list`/`catalog.product.offer
  .list` responses include two NATIVE, first-class Bitrix catalog fields
  — `purchasingPrice` and `purchasingCurrency` — distinct from the custom
  `propertyN` table above. They were observed present but unpopulated
  (`null`) on every sampled live product; this installation has never
  used them yet, but the destination itself is real and verified (this
  corrects earlier documentation that claimed purchase price had no
  verified destination). `LiveBitrixAdapter._write_product_create_live`
  now maps a supplied purchase price onto these fields on the SAME
  `catalog.product.add` call as the base product, structurally
  independent of the retail selling price (`catalog.price.add`) — neither
  can ever substitute for the other. EAN/GTIN still has **no** verified
  Bitrix destination on this installation and is never written.
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
| product create | `catalog.product.add` | base product: `name`, `active`, BRAND (`property100`), and (if supplied) purchase price via `purchasingPrice`/`purchasingCurrency`; response nests the created product under `"element"` (per Bitrix's documented contract — NOT `"product"`) |
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
- purchase price (optional; Block 5.6 follow-up defect closure) → native
  `purchasingPrice`/`purchasingCurrency` fields on the SAME
  `catalog.product.add` call as the base product — structurally
  independent of retail selling price, never substitutable for it.
  Validated up front (a positive number); malformed purchase price data
  fails closed (`invalid_purchase_price`, no write attempted) rather than
  being guessed or silently dropped. Currently implemented on the LIVE
  adapter only — the FIXTURE adapter/store does not yet persist it, so
  `prepare_single_product_write` only reports it under `will_write` for a
  LIVE-environment bridge.

**Never written**: EAN, SEO (META TITLE/KEYWORDS/DESCRIPTION), and
gallery/additional images — no verified destination exists for these on
this installation; `controlled_bitrix_write` never includes them in the
payload this adapter reads, so there is nothing to guess. Category/section,
weight/dimensions, preview/detail text, preview/detail pictures, and a
small verified set of characteristics DO now have verified destinations —
see "Complete Product Card Follow-up Pass" below.

**Example — controlled create for a real LG test product**, given
title `"Телевизор LG 32LQ63006LA.ARUG"`, SKU `32LQ63006LA.ARUG`, brand
`LG`, purchase price `22513.70 RUB`, retail price `29990 RUB`, subcategory
`"Телевизоры"`, weight `12000` (g), dimensions `720x420x60` (mm), a short
and detailed description, and verified characteristics:

```
catalog.section.list (read, resolves subcategory "Телевизоры" -> id 70)

catalog.product.add fields (IBLOCK 14):
  name              = "Телевизор LG 32LQ63006LA.ARUG"
  active            = "N"
  property100       = "LG"                # BRAND
  purchasingPrice   = "22513.70"
  purchasingCurrency = "RUB"
  iblockSectionId   = 70                  # resolved "Телевизоры", never guessed
  weight            = "12000"
  length            = "720"
  width             = "420"
  height            = "60"
  previewText       = "<short description>"
  previewTextType   = "text"
  detailText        = "<detailed description>"
  detailTextType    = "text"
  property154       = "81"                # screen_diagonal_cm
  property156       = "3840x2160"         # screen_resolution
  property206       = "webOS"             # operating_system
  property209       = "Да"                # smart_tv_support
  property246       = "Черный"            # color
  xmlId             = <deterministic idempotency-key-derived id>

catalog.product.offer.add fields (IBLOCK 15):
  parentId    = <id returned by the product create above>
  name        = "Телевизор LG 32LQ63006LA.ARUG"
  active      = "N"
  property283 = "32LQ63006LA.ARUG"        # ARTICLE

catalog.price.add fields:
  productId      = <id returned by the product create above>
  catalogGroupId = <BITRIX_RETAIL_PRICE_TYPE_ID; = 1 on panda.msk.ru>
  price          = "29990"
  currency       = "RUB"
```

Preview/detail pictures (when supplied as `{"filename", "base64"}`) are
sent as `previewPicture`/`detailPicture` = `{"fileData": ["<filename>",
"<base64>"]}` on the same `catalog.product.add` call. EAN and SEO have no
verified destination and are never included in any of the above (see
"Complete Product Card Follow-up Pass" above).

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

## Complete Product Card Follow-up Pass (real products 992/993 — closing the "skeleton" defect)

The first real production controlled create (Bitrix product 992 / offer
993, `Телевизор LG 32LQ63006LA.ARUG`) succeeded but produced only a
**skeleton** product card: no catalog section, no characteristics, no
weight/dimensions, no preview/detail content or images, and the visible
Aspro admin SEO controls had no verified write path. A second, bounded
LIVE READ-ONLY discovery pass (real webhook, read-only
`catalog.section.list`/`catalog.product(.offer).list` explicit-select
calls, and — newly discovered this pass — `catalog.productProperty.list`/
`.get`, a different method family from the previously-tried and still
`ERROR_METHOD_NOT_FOUND` `iblock.property.list`) resolved most of the
still-missing product-card data. Real products 992/993 were never
modified during this pass; every discovery call was read-only, and every
write mapping below is exercised only by mocked-transport tests.

### A. Category / section — RESOLVED

`catalog.section.list` (filter `iblockId=14`) works and returns the real
section tree. The verified "Телевизоры" section:

| Field | Value |
|-------|-------|
| id | `70` |
| name | Телевизоры |
| code | `televizory` |
| parent (`iblockSectionId`) | `61` (Электроника) |

`catalog.product.add` accepts `iblockSectionId` as a normal writable
field. Panda never guesses this id: `schema.resolve_section_id(category,
subcategory, sections)` deterministically matches a Panda
category/subcategory string against an EXACT (trimmed, case-insensitive)
section name from an already-fetched live `catalog.section.list`
snapshot. `subcategory` (e.g. `"Телевизоры"`) is tried first if supplied;
`category` is used only as a fallback when no subcategory was supplied at
all — a supplied-but-unmatched subcategory fails closed rather than
silently falling back to a broader parent section. Zero matches or more
than one section sharing that exact name both fail closed
(`no_matching_section_found` / `ambiguous_section_name`) via
`prepare_single_product_write` returning `UNRESOLVED` — the product is
**never** left at catalog root when a category/subcategory was actually
supplied. No category/subcategory supplied at all = unchanged prior
behavior (field simply omitted).

### B. EAN / barcode — BLOCKED (deferred)

Probed `catalog.*Barcode.list`, `crm.product.list`, `catalog.measure.*`,
`iblock.element.get`, explicit `select=["barcode"]` on
`catalog.product.list`/`catalog.product.offer.list` — every method either
returns `ERROR_METHOD_NOT_FOUND` or `insufficient_scope`, and no barcode-
shaped key appears in any real product/offer response on this
installation. No verified writable destination exists for EAN on this
installation. EAN remains sourced-but-unwritten (`not_written`, reason
`no_verified_bitrix_property_for_ean_on_this_installation`) — never
guessed onto an arbitrary property.

### C. Characteristics / specifications — RESOLVED (bounded set)

`catalog.productProperty.list` (filter `iblockId`) — a distinct REST
method family from `iblock.property.list` (still
`ERROR_METHOD_NOT_FOUND`) — DOES work on this installation and returns
full metadata (`id`, `code`, `name`, `propertyType`, `multiple`, …) for
every one of IBLOCK 14's ~179 custom properties. Cross-referencing that
metadata against real populated products verified exactly these
semantically-unambiguous, scalar, `PANDA_MANAGED` characteristics (added
to `schema.CATALOG_CHARACTERISTICS`):

| Panda key | Property ID | Bitrix code | Bitrix name |
|-----------|------------|-------------|-------------|
| `screen_diagonal_cm` | 154 | `PROP_2053` | Диагональ дисплея, см |
| `screen_resolution` | 156 | `PROP_2054` | Разрешение экрана, пикс |
| `operating_system` | 206 | `PROP_301` | Операционная система |
| `smart_tv_support` | 209 | `PROP_304` | Поддержка Smart TV |
| `color` | 246 | `COLOR_REF2` | Цвет |

`display_technology`, `refresh_rate_hz`, and `model_year` were explicitly
checked against the full property list and have **no** matching property
on this installation — deferred, not guessed. `schema.
map_characteristics_to_properties({key: value})` is the ONLY place that
resolves a Panda characteristic key to a real `propertyN` write field (or
drops it); an unrecognized key is never written, only reported
sourced-but-unwritten (`not_written` field `characteristic:<key>`) so
Panda still preserves the source value. Only the base product (IBLOCK 14)
is covered — no offer-level (IBLOCK 15) characteristic was verified this
pass, so none is implemented for offers.

### D. Weight and dimensions — RESOLVED (fields verified; units NOT independently verifiable)

`weight`, `length`, `width`, `height` are native, selectable, writable
fields on `catalog.product.add`/`catalog.product.list` (per Bitrix's own
REST reference, typed `double`/`float`) — confirmed present but `null` on
every sampled live product; this installation has never populated them.
**Bitrix's own REST reference does not state a unit for any of the four**
(the long-standing informal Bitrix convention is grams/millimeters, but
that could not be independently confirmed against real non-null data on
this installation). To make that ambiguity explicit at every call site
rather than silently assuming a unit deep in the write path, the Panda
canonical request fields are unit-suffixed: `weight_g`, `length_mm`,
`width_mm`, `height_mm`. Values are passed through **verbatim, with zero
unit conversion** — `LiveBitrixAdapter._physical_fields`/
`business_assistant.controlled_bitrix_write._normalize_physical_fields`
only validate "positive decimal string", never converts. A missing
dimension is simply omitted from the write (never forced to `0`, which
Bitrix would treat as a real, meaningfully-zero value). A malformed
supplied value fails closed (`invalid_weight_g`/`invalid_length_mm`/etc.)
before any HTTP call.

### E. Preview / announcement — RESOLVED

`previewText`, `previewTextType`, and `previewPicture` are confirmed
real, writable fields on `catalog.product.add`. Panda maps
`description.short` (canonical `content.short_description`, cleaned via
the existing `data_intel.cleaning.clean_text`) onto `previewText`, always
paired with `previewTextType="text"` (plain text, not HTML) — the adapter
never fabricates marketing copy, it only passes through already-prepared
Panda content. `previewPicture`'s confirmed WRITE shape (per Bitrix's own
REST reference) is `{"fileData": [filename, base64_content]}`, never a
bare URL — Panda must supply an already-encoded `base64` string plus a
`filename`; a media entry missing either fails closed before any HTTP
call. No LIVE upload was ever attempted from the agent.

### F. Detail content — RESOLVED for text/main images; gallery deferred

`detailText`, `detailTextType`, and `detailPicture` are confirmed real,
writable fields with the identical shape/semantics as their preview
counterparts above (`detailText`/`detailTextType="text"` from
`content.detailed_description`; `detailPicture` via the same
`{"fileData": [filename, base64]}` shape). The offer-level `MORE_PHOTO`
gallery/additional-image property's multi-value `fileData` WRITE format
was not independently confirmed by documentation this pass (only its
READ shape was previously known) — implementing it speculatively would
risk a wrong upload shape, so gallery/additional images remain deferred.
No LIVE upload was ever attempted from the agent for any of these.

### G. SEO — BLOCKED (deferred)

Re-confirmed `iblock.element.get`/`iblock.elementproperty.list` still
return `ERROR_METHOD_NOT_FOUND`, and `lists.element.get` returns
`insufficient_scope` on this installation's webhook. No real
`metaTitle`/`seoTitle`-shaped field exists on `catalog.product.add`, and
no alternative inherited-property/template SEO REST method could be
found that this webhook's granted scopes can call. No verified writable
mechanism exists for META TITLE/KEYWORDS/DESCRIPTION, element/page title,
or image ALT/TITLE on this installation. SEO write remains entirely
deferred — no speculative write was implemented.

### Mapping table (this pass's additions)

| Panda field | Bitrix destination | Method | Status |
|-------------|--------------------|--------|--------|
| `subcategory`/`category_source` → resolved section id | `iblockSectionId` (IBLOCK 14) | `catalog.section.list` (read) + `catalog.product.add` (write) | RESOLVED |
| `weight_g` | `weight` | `catalog.product.add` | RESOLVED (unit unverified) |
| `length_mm` | `length` | `catalog.product.add` | RESOLVED (unit unverified) |
| `width_mm` | `width` | `catalog.product.add` | RESOLVED (unit unverified) |
| `height_mm` | `height` | `catalog.product.add` | RESOLVED (unit unverified) |
| `short_description` | `previewText` (+`previewTextType="text"`) | `catalog.product.add` | RESOLVED |
| `detailed_description` | `detailText` (+`detailTextType="text"`) | `catalog.product.add` | RESOLVED |
| preview image (`{filename, base64}`) | `previewPicture` (`{"fileData": [name, base64]}`) | `catalog.product.add` | RESOLVED (no LIVE upload tested) |
| detail image (`{filename, base64}`) | `detailPicture` (`{"fileData": [name, base64]}`) | `catalog.product.add` | RESOLVED (no LIVE upload tested) |
| `characteristics.screen_diagonal_cm` | `property154` (`PROP_2053`) | `catalog.product.add` | RESOLVED |
| `characteristics.screen_resolution` | `property156` (`PROP_2054`) | `catalog.product.add` | RESOLVED |
| `characteristics.operating_system` | `property206` (`PROP_301`) | `catalog.product.add` | RESOLVED |
| `characteristics.smart_tv_support` | `property209` (`PROP_304`) | `catalog.product.add` | RESOLVED |
| `characteristics.color` | `property246` (`COLOR_REF2`) | `catalog.product.add` | RESOLVED |
| any other `characteristics.<key>` | — | — | preserved in Panda, reported `not_written`, never guessed |
| gallery/additional images | `MORE_PHOTO` (offer, IBLOCK 15) | — | deferred (write shape unconfirmed) |
| EAN/barcode | — | — | BLOCKED (no verified destination) |
| SEO (title/keywords/description/ALT) | — | — | BLOCKED (no verified method) |

### Extended read-back verification

`execute_single_product_write`'s independent post-write read-back
(`bridge.read_product`, a fresh governed READ, never the write's own
embedded echo) is extended to also assert `iblockSectionId` whenever this
write actually resolved a section — an already-existing product with no
category data supplied is never false-mismatched against an unset
expectation.

### Canonical model extension

`business_assistant.controlled_bitrix_write.SingleProductWriteRequest`
gained `subcategory`, `weight_g`, `length_mm`, `width_mm`, `height_mm`,
`short_description`, `detailed_description`, and `characteristics`
(`Mapping[str, str]`, keyed by the semantic keys above). None of these
force an unavailable field to `null`/`0` — every one is simply omitted
from the canonical payload (and therefore from the Bitrix write) when not
supplied.

### Tests

`tests/test_bitrix_live_product_create_write.py` (`SectionAssignmentTests`,
`PhysicalDimensionsTests`, `ContentAndMediaFieldsTests`,
`CharacteristicsWriteTests`) covers the `LiveBitrixAdapter` field-
construction/validation layer directly (an already-resolved section id;
malformed/missing physical values; preview/detail text and the
`fileData` picture shape; verified vs. unverified characteristic keys).
`tests/test_bitrix_complete_product_card_followup.py` covers the layer
above that — `schema.resolve_section_id`/`schema.
map_characteristics_to_properties` as pure functions, plus full
`prepare_single_product_write`/`execute_single_product_write`
orchestration on a mocked-transport LIVE bridge: unambiguous resolution,
category fallback, ambiguous/no-match/lookup-failure all failing closed
with zero product-create calls, and the full weight/dimensions/content/
characteristics payload actually reaching the mocked `catalog.product.add`
call. All real Bitrix HTTP interaction in every test is a mocked
transport — zero real network calls, zero real Bitrix mutations, and real
products 992/993 were never touched by any test or discovery call.

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
