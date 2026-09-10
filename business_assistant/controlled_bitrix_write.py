"""PANDA — first controlled production Bitrix/Aspro product write.

A deliberately narrow, additive orchestration layer for creating **exactly
one** governed product in the real Bitrix/Aspro Premier catalog, on top of
the already-existing, already-governed Block 5.4/5.6 machinery:

    XLSX product row
        -> canonical write request (this module)
        -> verified Bitrix/Aspro schema mapping (integrations.bitrix.schema)
        -> duplicate check (BitrixProductBridge.plan_sync)
        -> write preview (this module, ``prepare_single_product_write``)
        -> explicit OWNER approval (caller-supplied ``approved`` flag)
        -> governed write (BitrixProductBridge.sync_product, unchanged
           idempotency/HITL/ToolGateway contract -- ``approved_write`` must
           be True or ``IntegrationActivationService.execute_via_gateway``
           itself raises before any adapter call)
        -> explicit read-back verification (BitrixProductBridge.read_product)
        -> user-facing result

No new HTTP client, no second write path, no bypass of
``IntegrationActivationService``'s approval/idempotency gate, and no
invented Bitrix/Aspro property codes: only fields with a schema-verified
destination (``integrations.bitrix.schema.CATALOG_PRODUCT_PROPERTIES`` /
``OFFER_PROPERTIES``, plus the native ``purchasingPrice``/
``purchasingCurrency`` product fields -- see below) are ever written.
Fields this production installation has no verified destination for
(EAN/GTIN, category -- see module docstring in
``integrations/bitrix/schema.py``) are reported to the user as *sourced
from the file* but explicitly **not written**, never guessed onto an
invented property id.

Purchase price and retail price are kept structurally separate: the
canonical payload built here carries any purchase price under its own,
dedicated top-level ``purchase_price`` key (``{"amount", "currency"}``) --
never inside the ``price`` dict a write path reads for the retail selling
price, so the known ``BitrixFixtureAdapter._write_product_create`` fallback
(selling_price -> purchase_price when selling_price is blank, which only
ever looks *inside* the ``price`` dict) can never trigger, and purchase
price can never be substituted for retail price. Retail price remains a
required field for this flow and is validated non-empty before any write
is attempted; purchase price is optional, but validated (fails closed --
``invalid_purchase_price``, no write attempted) whenever it is present but
not a valid positive number. Purchase price is a real LIVE production
Bitrix write via the native ``purchasingPrice``/``purchasingCurrency``
fields on ``catalog.product.add`` (Block 5.6 follow-up defect closure --
see ``integrations.bitrix.live_adapter.LiveBitrixAdapter
._write_product_create_live``); it is not yet persisted by the FIXTURE
adapter/store, so ``prepare_single_product_write`` only reports it under
``will_write`` for a LIVE-environment bridge, and still reports it under
``will_not_write``/``not_written`` otherwise.

The created product is always written **inactive** (``active=False``): a
controlled first production write must not go live on the storefront
without a further, separate, explicit publish decision.

Product enrichment pipeline follow-up (``product_enrichment`` package):
``SingleProductWriteRequest`` also carries ``preview_picture``/
``detail_picture`` (each ``{"filename", "base64"}`` -- ALREADY downloaded/
validated/processed bytes, never a bare external URL/hotlink -- see
``product_enrichment.media``) so the enrichment pipeline's output can flow
straight into this same, unchanged write/approval/read-back path via
``business_assistant.product_enrichment_bridge``.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Mapping, Sequence

from data_intel.cleaning import clean_text, normalize_decimal_string
from data_intel.contracts import (
    ROLE_ARTICLE,
    ROLE_BRAND,
    ROLE_CATEGORY,
    ROLE_EAN,
    ROLE_PRODUCT_NAME,
    ROLE_PURCHASE_PRICE,
    ROLE_SKU,
)
from integrations.activation.models import ENV_LIVE
from integrations.bitrix import schema
from integrations.bitrix.product_bridge import (
    SYNC_AMBIGUOUS,
    SYNC_CREATE,
    SYNC_INVALID,
    SYNC_UNCHANGED,
    SYNC_UPDATE,
    BitrixProductBridge,
)

STATUS_REQUIRES_APPROVAL = "REQUIRES_APPROVAL"
STATUS_UNRESOLVED = "UNRESOLVED"
STATUS_EXISTING_PRODUCT_FOUND = "EXISTING_PRODUCT_FOUND"
STATUS_AMBIGUOUS = "AMBIGUOUS"
STATUS_APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
STATUS_WRITE_VERIFIED = "WRITE_VERIFIED"
STATUS_WRITE_VERIFICATION_MISMATCH = "WRITE_VERIFICATION_MISMATCH"
STATUS_WRITE_FAILED = "WRITE_FAILED"
STATUS_WRITE_PARTIAL_FAILURE = "WRITE_PARTIAL_FAILURE"
STATUS_WRITE_NOT_PERFORMED = "WRITE_NOT_PERFORMED"

# Reasons reported to the user for fields that are sourced from the file but
# have no verified write destination on this production installation --
# never a guessed/invented Bitrix property id.
_NO_EAN_DESTINATION = (
    "no_verified_bitrix_property_for_ean_on_this_installation "
    "(neither IBLOCK 14 nor 15's known real properties include one)"
)
# Purchase price DOES have a verified native Bitrix destination
# (purchasingPrice/purchasingCurrency -- see integrations.bitrix.schema's
# module docstring and LiveBitrixAdapter._write_product_create_live), but
# only the LIVE adapter implements writing it so far -- the FIXTURE
# adapter/store this reason is used for still does not persist it.
_PURCHASE_PRICE_ENV_UNSUPPORTED = (
    "purchasing_price_has_a_verified_native_bitrix_destination_"
    "(purchasingPrice/purchasingCurrency)_but_this_environment's_adapter_"
    "does_not_yet_persist_it_(fixture/sandbox_only;_live_writes_it)"
)
_NO_CATEGORY_MAPPING = (
    "no_established_category_name_to_bitrix_section_mapping_for_this_tenant "
    "(unlike marketplaces' MarketplaceCategoryMap, Bitrix sections have no "
    "equivalent verified name->id table here -- resolving one would require "
    "guessing)"
)
# Category/section DOES have a verified native destination now
# (``iblockSectionId`` -- see integrations.bitrix.schema's module
# docstring item A and ``resolve_section_id``), but resolving *which*
# section requires a real ``catalog.section.list`` read, which only the
# LIVE bridge can do -- the FIXTURE store's section shape is unrelated
# (deterministic slug ids derived from its own in-memory catalog, not a
# real Bitrix section tree), so this reason is still used for a non-LIVE
# bridge.
_CATEGORY_ENV_UNSUPPORTED = (
    "category/section_has_a_verified_native_bitrix_destination_"
    "(iblockSectionId)_but_resolving_it_requires_a_real_catalog.section.list_"
    "read,_which_only_the_LIVE_bridge_performs_(fixture/sandbox_only;_live_"
    "resolves_and_writes_it)"
)
# Characteristics with no verified Bitrix property destination on this
# installation (integrations.bitrix.schema.CATALOG_CHARACTERISTICS) --
# Panda still keeps the source value, it is just never sent to a guessed
# property.
_UNVERIFIED_CHARACTERISTIC = (
    "no_verified_bitrix_property_for_this_characteristic_key_on_this_"
    "installation (see integrations.bitrix.schema.CATALOG_CHARACTERISTICS "
    "for the currently verified set)"
)

# Human-readable hints for the raw error codes a failed write can surface,
# keyed on ``result["error"]`` (production defect closure: this write path
# used to collapse every real failure into just the raw capability string,
# e.g. "cms.bitrix.catalog.write", which tells an operator nothing about
# *why* the write was rejected). Never guesses/fabricates a reason not
# actually implied by the error code -- unmapped codes fall back to showing
# the raw code plus the exception class name (see
# ``_describe_write_failure`` below), never silently hidden.
_WRITE_FAILURE_HINTS: dict[str, str] = {
    "bitrix_live_not_configured": (
        "интеграция Bitrix не настроена в LIVE-режиме на этом сервере "
        "(переменные окружения BITRIX_INTEGRATION_MODE=LIVE и "
        "BITRIX_WEBHOOK_URL не заданы или заданы неверно)"
    ),
    "cms.bitrix.catalog.write": (
        "нет активного подключения к Bitrix с правом записи для этого "
        "запроса (LIVE-подключение не настроено/не активировано, либо "
        "явно не разрешает эту операцию записи)"
    ),
    "cms.bitrix.catalog.read": (
        "нет активного подключения к Bitrix с правом чтения для этого "
        "запроса (LIVE-подключение не настроено/не активировано)"
    ),
    "INTEGRATION_NOT_CONFIGURED": "подключение к Bitrix не настроено для этого тенанта",
    "INTEGRATION_NOT_ACTIVE": "подключение к Bitrix существует, но не активно",
    "INTEGRATION_WRITE_DENIED": "этому подключению к Bitrix не разрешена запись",
    "INTEGRATION_AUTH_FAILED": "ошибка авторизации в Bitrix (неверные учётные данные/webhook)",
    "INTEGRATION_PROVIDER_UNAVAILABLE": "Bitrix временно недоступен (сбой на стороне провайдера)",
    "INTEGRATION_TIMEOUT": "превышено время ожидания ответа от Bitrix",
    "INTEGRATION_RATE_LIMITED": "Bitrix временно ограничивает частоту запросов",
    "INTEGRATION_LIVE_FALLBACK_FORBIDDEN": (
        "LIVE-подключение недоступно, а автоматический откат на тестовый "
        "режим запрещён"
    ),
    "INTEGRATION_ENVIRONMENT_MISMATCH": "несоответствие окружения (LIVE/FIXTURE) для этого подключения",
    "INTEGRATION_CROSS_TENANT": "подключение принадлежит другому тенанту",
    "product_create_malformed_response": (
        "Bitrix вернул HTTP 200 на создание товара, но без распознаваемого "
        "ID созданного товара — товар НЕ считается созданным; см. "
        "application-логи (Railway) с меткой bitrix_malformed_create_response "
        "для точной формы фактического ответа Bitrix"
    ),
    "offer_create_malformed_response": (
        "Bitrix вернул HTTP 200 на создание торгового предложения (SKU), но "
        "без распознаваемого ID — см. application-логи с меткой "
        "bitrix_malformed_create_response"
    ),
    "price_create_malformed_response": (
        "Bitrix вернул HTTP 200 на запись цены, но без распознаваемого ID — "
        "см. application-логи с меткой bitrix_malformed_create_response"
    ),
}


def _describe_write_failure(result: Mapping) -> str:
    code = str(result.get("error") or "write_failed")
    exc_type = str(result.get("error_type") or "")
    hint = _WRITE_FAILURE_HINTS.get(code)
    if hint:
        return f"{code} — {hint}"
    if exc_type and exc_type != code:
        return f"{code} ({exc_type})"
    return code


class ControlledWriteBatchNotAllowedError(Exception):
    """Raised when more (or fewer) than exactly one row/product is supplied
    to this controlled single-product write path -- batch/bulk imports must
    use the existing governed ``BitrixProductBridge.bulk_sync`` path, never
    this one."""


def assert_single_row(rows: Sequence[dict]) -> dict:
    """Guard used by any caller assembling candidate rows before invoking
    this module: this path is single-product only, by construction."""
    if len(rows) != 1:
        raise ControlledWriteBatchNotAllowedError(
            f"controlled_write_requires_exactly_one_row_got_{len(rows)}"
        )
    return rows[0]


@dataclass(frozen=True)
class SingleProductWriteRequest:
    tenant_id: str
    title: str
    sku: str
    retail_price: str
    currency: str = "RUB"
    ean: str = ""
    category_source: str = ""
    brand: str = ""
    purchase_price: str = ""
    product_id: str = ""
    # Complete-product-card follow-up pass (integrations.bitrix.schema
    # module docstring, items A/C/D/E/F). ``subcategory`` is tried first
    # for section resolution (more specific -- e.g. "Телевизоры"),
    # ``category_source`` above is the fallback. The four dimension
    # fields are named with their assumed unit (grams/millimeters --
    # Bitrix's own REST reference does not state one; see schema.py) so
    # the unit is always explicit at the call site, never silently
    # assumed deeper in the write path. ``characteristics`` is a flat
    # ``{key: value}`` mapping keyed by the semantic keys in
    # ``integrations.bitrix.schema.CATALOG_CHARACTERISTICS`` (e.g.
    # ``"screen_diagonal_cm"``); unrecognized keys are reported
    # sourced-but-unwritten, never guessed onto a property.
    subcategory: str = ""
    weight_g: str = ""
    length_mm: str = ""
    width_mm: str = ""
    height_mm: str = ""
    short_description: str = ""
    detailed_description: str = ""
    characteristics: Mapping[str, str] = field(default_factory=dict)
    # Product enrichment pipeline follow-up: verified Bitrix write shape for
    # both picture fields is ``{"filename": str, "base64": str}`` (see
    # ``integrations.bitrix.live_adapter.LiveBitrixAdapter._media_fields``
    # / ``integrations.bitrix.schema.PICTURE_FILE_DATA_KEY``). This module
    # never fetches/encodes an image itself -- callers (e.g.
    # ``business_assistant.product_enrichment_bridge``) must already have
    # downloaded, validated and base64-encoded the exact bytes to send;
    # an empty dict here means "no image supplied", never a guessed one.
    preview_picture: Mapping[str, str] = field(default_factory=dict)
    detail_picture: Mapping[str, str] = field(default_factory=dict)


def _first_column_value(row: dict, columns, role: str) -> str:
    for col in columns:
        if col.semantic_role == role:
            value = row.get(col.source_name)
            if value not in (None, ""):
                return str(value)
    return ""


def build_write_request_from_row(
    row: dict,
    columns,
    *,
    tenant_id: str,
    retail_price: str,
    currency: str = "RUB",
    product_id: str = "",
) -> SingleProductWriteRequest:
    """Build a canonical single-product write request from one already-
    resolved dataset row (e.g. the unique match ``data_intel``'s row lookup
    already found) plus the retail price the OWNER supplied/confirmed.

    Reads roles dynamically from the dataset's own inferred schema
    (``columns``) -- never assumes a fixed column name or invents a value
    for a field the row does not actually carry.
    """
    title = _first_column_value(row, columns, ROLE_PRODUCT_NAME)
    sku = _first_column_value(row, columns, ROLE_SKU) or _first_column_value(row, columns, ROLE_ARTICLE)
    return SingleProductWriteRequest(
        tenant_id=tenant_id,
        title=title,
        sku=sku,
        retail_price=str(retail_price or ""),
        currency=currency,
        ean=_first_column_value(row, columns, ROLE_EAN),
        category_source=_first_column_value(row, columns, ROLE_CATEGORY),
        brand=_first_column_value(row, columns, ROLE_BRAND),
        purchase_price=_first_column_value(row, columns, ROLE_PURCHASE_PRICE),
        product_id=product_id,
    )


def build_write_request_from_fields(
    fields: Mapping[str, str],
    *,
    tenant_id: str,
    retail_price: str,
    currency: str = "RUB",
    product_id: str = "",
) -> SingleProductWriteRequest:
    """Build a canonical single-product write request from an already
    role-resolved flat field dict (``data_intel``'s row-lookup preview's
    ``product_fields`` -- see ``data_intel.service._row_lookup_result``)
    plus the retail price the OWNER supplied/confirmed in their approval
    message. Mirrors ``build_write_request_from_row`` for the conversational
    approval path, which only has the persisted flat fields (from the
    ActiveTask), not the raw row + column schema objects."""
    raw_characteristics = fields.get("characteristics") or {}
    return SingleProductWriteRequest(
        tenant_id=tenant_id,
        title=str(fields.get("title") or ""),
        sku=str(fields.get("sku") or ""),
        retail_price=str(retail_price or ""),
        currency=currency,
        ean=str(fields.get("ean") or ""),
        category_source=str(fields.get("category") or ""),
        brand=str(fields.get("brand") or ""),
        purchase_price=str(fields.get("purchase_price") or ""),
        product_id=product_id,
        subcategory=str(fields.get("subcategory") or ""),
        weight_g=str(fields.get("weight_g") or ""),
        length_mm=str(fields.get("length_mm") or ""),
        width_mm=str(fields.get("width_mm") or ""),
        height_mm=str(fields.get("height_mm") or ""),
        short_description=str(fields.get("short_description") or ""),
        detailed_description=str(fields.get("detailed_description") or ""),
        characteristics={str(k): str(v) for k, v in dict(raw_characteristics).items() if v not in (None, "")},
        preview_picture=_clean_picture_field(fields.get("preview_picture")),
        detail_picture=_clean_picture_field(fields.get("detail_picture")),
    )


def _clean_picture_field(raw) -> dict:
    if not isinstance(raw, Mapping):
        return {}
    filename = str(raw.get("filename") or "")
    base64_content = str(raw.get("base64") or "")
    if not filename or not base64_content:
        return {}
    return {"filename": filename, "base64": base64_content}


def _normalize_price(raw: str) -> str | None:
    text = normalize_decimal_string(raw)
    if text is None:
        return None
    try:
        if Decimal(text) <= 0:
            return None
    except (InvalidOperation, ValueError):
        return None
    return text


def _default_idempotency_key(tenant_id: str, request: SingleProductWriteRequest) -> str:
    basis = f"controlled-bitrix-write|{tenant_id}|{request.sku}|{request.title}"
    return "cbw-" + hashlib.sha256(basis.encode("utf-8")).hexdigest()[:24]


# Panda-facing (unit-suffixed) SingleProductWriteRequest attribute -> native
# Bitrix wire field name (integrations.bitrix.schema.WEIGHT_FIELD etc.).
# The unit itself is never converted here or anywhere downstream -- see
# schema.py's module docstring item D for why the unit could not be
# independently verified beyond Bitrix's own long-standing convention.
_PHYSICAL_DIMENSION_ATTRS: tuple[tuple[str, str], ...] = (
    ("weight_g", schema.WEIGHT_FIELD),
    ("length_mm", schema.LENGTH_FIELD),
    ("width_mm", schema.WIDTH_FIELD),
    ("height_mm", schema.HEIGHT_FIELD),
)


def _normalize_physical_fields(request: SingleProductWriteRequest) -> tuple[dict, str | None]:
    """Deterministic pass-through validation for the native weight/length/
    width/height Bitrix fields (schema.py module docstring item D) -- a
    positive-number check only, never a unit conversion. Returns
    ``(fields, error_reason)``; a supplied-but-malformed value fails
    closed (``error_reason`` set, ``fields`` empty) rather than being
    written or silently dropped. A field the caller did not supply is
    simply omitted (never forced to 0)."""
    fields: dict = {}
    for attr, wire_key in _PHYSICAL_DIMENSION_ATTRS:
        raw = getattr(request, attr)
        if not raw:
            continue
        normalized = _normalize_price(raw)  # same "positive decimal string" rule
        if normalized is None:
            return {}, f"invalid_{attr}"
        fields[wire_key] = normalized
    return fields, None


def _normalize_media_fields(request: SingleProductWriteRequest) -> tuple[dict, str | None]:
    """Fail-closed validation for the two picture fields (schema.py items
    E/F): each, if supplied at all, MUST already carry both ``filename``
    and non-empty ``base64`` content -- this module never fetches/encodes
    an image itself, so a malformed/partial entry can only mean a caller
    bug, and is refused (no write attempted) rather than silently dropped
    or sent half-formed."""
    fields: dict = {}
    for attr, media_key in (("preview_picture", "preview_picture"), ("detail_picture", "detail_picture")):
        entry = getattr(request, attr) or {}
        if not entry:
            continue
        filename = str(entry.get("filename") or "")
        base64_content = str(entry.get("base64") or "")
        if not filename or not base64_content:
            return {}, f"invalid_{attr}"
        fields[media_key] = {"filename": filename, "base64": base64_content}
    return fields, None


def _canonical_payload(
    request: SingleProductWriteRequest,
    *,
    retail_amount: str,
    purchase_price_amount: str | None = None,
    section_id=None,
    physical_fields: dict | None = None,
    characteristics: Mapping[str, str] | None = None,
    media_fields: dict | None = None,
) -> dict:
    # ``price.selling_price`` is the ONLY key any write path reads for the
    # retail price. Any purchase price lives in its own, sibling
    # ``purchase_price`` key (never nested inside ``price``), so it can
    # never be substituted for -- or read as -- the retail selling price.
    canonical: dict = {
        "product_id": request.product_id or f"panda-controlled:{request.sku}",
        "title": request.title,
        "sku": request.sku,
        "price": {"currency": request.currency, "selling_price": retail_amount},
    }
    if request.brand:
        # Property 100 / code BRAND is the verified, PANDA_MANAGED binding
        # for brand on this installation (integrations.bitrix.schema) --
        # not a guessed property; the fixture/live create path already
        # merges any ``properties`` dict supplied on the canonical product.
        canonical["properties"] = {"brand": request.brand}
    if purchase_price_amount is not None:
        # Native purchasingPrice/purchasingCurrency destination (Block 5.6
        # follow-up defect closure) -- structurally separate top-level key,
        # never read by the retail-price write path above.
        canonical["purchase_price"] = {"amount": purchase_price_amount, "currency": request.currency}
    if section_id is not None:
        # Native iblockSectionId destination (schema.py module docstring
        # item A) -- ALREADY resolved deterministically against a live
        # catalog.section.list snapshot in ``prepare_single_product_write``
        # below; never re-resolved/guessed downstream.
        canonical["classification"] = {"section_id": section_id}
    if physical_fields:
        canonical["physical"] = physical_fields
    content: dict = {}
    if request.short_description:
        content["short_description"] = clean_text(request.short_description)
    if request.detailed_description:
        content["detailed_description"] = clean_text(request.detailed_description)
    if content:
        canonical["content"] = content
    if characteristics:
        # Raw Panda semantic-key characteristics -- the ONLY place that
        # resolves a key to an actual ``propertyN`` (or drops an
        # unrecognized one) is ``schema.map_characteristics_to_properties``,
        # invoked by the LIVE adapter itself; never duplicated here.
        canonical["characteristics"] = dict(characteristics)
    if media_fields:
        # Native previewPicture/detailPicture destination (schema.py items
        # E/F) -- ALREADY validated/base64-encoded by the caller (e.g. the
        # product enrichment pipeline's downloaded/processed media, never
        # a bare external URL/hotlink); this module never fetches an image
        # itself, it only passes the already-prepared bytes through.
        canonical["media"] = media_fields
    return canonical


def prepare_single_product_write(
    bridge: BitrixProductBridge,
    *,
    tenant_id: str,
    request: SingleProductWriteRequest,
    connection_id: str | None = None,
) -> dict:
    """Read-only. Resolves the duplicate check and destination mapping and
    returns a preview for OWNER approval. Never mutates anything -- section
    resolution below only ever performs a READ (``catalog.section.list``),
    same governed READ path everything else in this module already uses."""
    title = clean_text(request.title) or ""
    sku = clean_text(request.sku) or ""
    if not title or not sku:
        return {"status": STATUS_UNRESOLVED, "reason": "missing_title_or_sku"}

    retail_amount = _normalize_price(request.retail_price)
    if retail_amount is None:
        return {"status": STATUS_UNRESOLVED, "reason": "missing_or_invalid_retail_price"}

    # Purchase price is optional, but if the source data supplied one it
    # must be a valid positive number -- fail closed (never write a
    # malformed/guessed value, never silently drop it either) rather than
    # proceeding with bad data.
    purchase_price_amount = None
    if request.purchase_price:
        purchase_price_amount = _normalize_price(request.purchase_price)
        if purchase_price_amount is None:
            return {"status": STATUS_UNRESOLVED, "reason": "invalid_purchase_price"}

    # Purchase price's native destination (purchasingPrice/purchasingCurrency)
    # is only implemented on the LIVE adapter so far (see
    # LiveBitrixAdapter._write_product_create_live) -- the FIXTURE
    # adapter/store does not yet persist it, so the preview must not claim
    # a write that will not actually happen for a non-LIVE bridge.
    purchase_price_has_destination = bridge.environment == ENV_LIVE and purchase_price_amount is not None

    # Weight/length/width/height (schema.py module docstring item D) --
    # fail closed on a malformed supplied value, before any sync/write.
    physical_fields, physical_error = _normalize_physical_fields(request)
    if physical_error:
        return {"status": STATUS_UNRESOLVED, "reason": physical_error}

    # Preview/detail pictures (schema.py items E/F, product enrichment
    # pipeline follow-up) -- fail closed on a malformed supplied entry,
    # before any sync/write, same as physical dimensions above.
    media_fields, media_error = _normalize_media_fields(request)
    if media_error:
        return {"status": STATUS_UNRESOLVED, "reason": media_error}

    # Category/section (schema.py module docstring item A) -- resolving
    # WHICH section requires a real ``catalog.section.list`` read, which
    # only the LIVE bridge can meaningfully do (the FIXTURE store's
    # section shape is an unrelated, deterministic slug-id tree -- see
    # ``_CATEGORY_ENV_UNSUPPORTED`` above). Panda must never guess a
    # section id: if a category/subcategory WAS supplied but does not
    # resolve to exactly one existing LIVE section, this fails closed
    # (STATUS_UNRESOLVED) instead of ever proceeding to catalog root.
    section_id = None
    category_has_destination = False
    if request.subcategory or request.category_source:
        if bridge.environment == ENV_LIVE:
            try:
                # Idempotent, LIVE-only bootstrap (no-op if already active)
                # -- section resolution below is the first governed
                # operation this preview performs, and it needs an ACTIVE
                # connection just as much as the later actual write does
                # (``execute_single_product_write`` calls this again right
                # before ``sync_product``; calling it twice is harmless).
                # Without this, a preview-only call (before approval) would
                # always fail closed with IntegrationNotConfiguredError on
                # a tenant's very first request, even with correctly
                # configured LIVE credentials.
                bridge.ensure_live_connection_ready(tenant_id=tenant_id)
                sections_result = bridge.read_sections(tenant_id=tenant_id, connection_id=connection_id)
            except Exception as exc:  # noqa: BLE001 -- normalize, never leak a raw adapter exception from a preview
                return {
                    "status": STATUS_UNRESOLVED,
                    "reason": "section_lookup_failed",
                    "error": getattr(exc, "code", type(exc).__name__),
                }
            sections = sections_result.get("items") or sections_result.get("sections") or []
            try:
                resolved = schema.resolve_section_id(
                    category=request.category_source, subcategory=request.subcategory, sections=sections
                )
            except schema.SectionResolutionError as exc:
                return {"status": STATUS_UNRESOLVED, "reason": exc.code, "detail": str(exc)}
            section_id = resolved["section_id"]
            category_has_destination = True

    # Characteristics (schema.py module docstring item C) -- only report/
    # write the ones with a verified property destination; an
    # unrecognized key is never guessed onto a property, only reported as
    # sourced-but-unwritten below.
    _mapped_characteristic_fields, unmapped_characteristics = schema.map_characteristics_to_properties(
        request.characteristics
    )
    characteristics_have_destination = bridge.environment == ENV_LIVE and bool(request.characteristics)

    canonical = _canonical_payload(
        request,
        retail_amount=retail_amount,
        purchase_price_amount=purchase_price_amount,
        section_id=section_id,
        physical_fields=physical_fields or None,
        characteristics=request.characteristics,
        media_fields=media_fields or None,
    )
    plan = bridge.plan_sync(tenant_id=tenant_id, canonical_product=canonical)
    action = plan.get("action")
    if action == SYNC_AMBIGUOUS:
        return {
            "status": STATUS_AMBIGUOUS,
            "plan_action": action,
            "plan": plan,
            "requires_separate_decision": True,
        }
    if action == SYNC_INVALID:
        return {"status": STATUS_UNRESOLVED, "reason": "sync_plan_invalid", "plan": plan}
    if action in (SYNC_UPDATE, SYNC_UNCHANGED):
        return {
            "status": STATUS_EXISTING_PRODUCT_FOUND,
            "plan_action": action,
            "plan": plan,
            "existing_target": plan.get("target"),
            "requires_separate_decision": True,
            "note": (
                "A product with this article/SKU already exists in Bitrix. "
                "This controlled single-product write never updates an "
                "existing product automatically -- a separate explicit "
                "decision is required before any update."
            ),
        }
    assert action == SYNC_CREATE

    category_display = request.subcategory or request.category_source
    not_written = [
        {"field": "ean", "value": request.ean, "reason": _NO_EAN_DESTINATION}
        if request.ean
        else None,
        {"field": "purchase_price", "value": request.purchase_price, "reason": _PURCHASE_PRICE_ENV_UNSUPPORTED}
        if request.purchase_price and not purchase_price_has_destination
        else None,
        {"field": "category", "value": category_display, "reason": _CATEGORY_ENV_UNSUPPORTED}
        if category_display and not category_has_destination
        else None,
    ]
    for key in unmapped_characteristics:
        not_written.append(
            {"field": f"characteristic:{key}", "value": request.characteristics.get(key), "reason": _UNVERIFIED_CHARACTERISTIC}
        )
    not_written = [item for item in not_written if item]

    will_write = ["name", "article/sku", "retail_selling_price"]
    if request.brand:
        will_write.append("brand (property 100 / BRAND, verified PANDA_MANAGED)")
    if purchase_price_has_destination:
        will_write.append(
            "purchase_price (native purchasingPrice/purchasingCurrency fields, verified -- LIVE only)"
        )
    if category_has_destination:
        will_write.append(
            f"category/section (native iblockSectionId={section_id}, resolved from {category_display!r} -- LIVE only)"
        )
    if physical_fields:
        will_write.append(f"physical ({', '.join(sorted(physical_fields))}, native fields, no unit conversion)")
    if request.short_description or request.detailed_description:
        will_write.append("content (previewText/detailText, native fields)")
    if media_fields:
        will_write.append(
            f"media ({', '.join(sorted(media_fields))}, native previewPicture/detailPicture fileData fields, uploaded bytes -- never a hotlink)"
        )
    written_characteristics = [k for k in request.characteristics if k not in unmapped_characteristics]
    if written_characteristics and characteristics_have_destination:
        will_write.append(f"characteristics ({', '.join(sorted(written_characteristics))}, verified -- LIVE only)")

    return {
        "status": STATUS_REQUIRES_APPROVAL,
        "plan_action": SYNC_CREATE,
        "max_products": 1,
        "target_product": {
            "title": title,
            "sku": sku,
            "brand": request.brand or None,
            "category_source": request.category_source or None,
            "subcategory": request.subcategory or None,
            "resolved_section_id": section_id,
            "ean_source": request.ean or None,
            "purchase_price_source": request.purchase_price or None,
        },
        "retail_price": {"amount": retail_amount, "currency": request.currency},
        "will_write": will_write,
        "will_not_write": not_written,
        "active_after_create": False,
        "publish_status_note": (
            "Product will be created INACTIVE (not published) for this "
            "controlled first write; a separate explicit publish decision "
            "is required before it appears live."
        ),
        "canonical_payload": canonical,
    }


def execute_single_product_write(
    bridge: BitrixProductBridge,
    *,
    tenant_id: str,
    request: SingleProductWriteRequest,
    approved: bool,
    idempotency_key: str = "",
    connection_id: str | None = None,
) -> dict:
    """Governed, single-product Bitrix create. Zero mutation unless
    ``approved`` is True -- and even then, ``BitrixProductBridge.sync_product``
    ultimately routes through ``IntegrationActivationService.execute_via_gateway``,
    which itself refuses any WRITE operation_class call without
    ``approved_write=True`` and a non-empty idempotency key (unchanged,
    pre-existing Block 5.4 gate -- not bypassed or duplicated here)."""
    if not approved:
        return {"status": STATUS_APPROVAL_REQUIRED, "mutated": False}

    preview = prepare_single_product_write(bridge, tenant_id=tenant_id, request=request, connection_id=connection_id)
    if preview["status"] != STATUS_REQUIRES_APPROVAL:
        return {**preview, "mutated": False}

    key = idempotency_key or _default_idempotency_key(tenant_id, request)
    canonical = preview["canonical_payload"]

    try:
        # Production defect closure: ensure this tenant has an
        # ACTIVE LIVE Bitrix connection registered on this bridge's own
        # activation service before the actual write (no-op for
        # FIXTURE/SANDBOX). Without this, the first-ever real write for a
        # tenant fails at IntegrationActivationService.resolve_connection
        # with IntegrationNotConfiguredError even though LIVE Bitrix
        # credentials are correctly configured -- see
        # BitrixProductBridge.ensure_live_connection_ready.
        bridge.ensure_live_connection_ready(tenant_id=tenant_id)
        result = bridge.sync_product(
            tenant_id=tenant_id,
            canonical_product=canonical,
            idempotency_key=key,
            approved_write=True,
            active=False,
            connection_id=connection_id,
        )
    except Exception as exc:  # noqa: BLE001 -- normalize, never leak raw adapter/HTTP exceptions
        return {
            "status": STATUS_WRITE_FAILED,
            "mutated": False,
            "error": getattr(exc, "code", type(exc).__name__),
            "error_type": type(exc).__name__,
            "idempotency_key": key,
        }

    if result.get("action") != SYNC_CREATE or not result.get("mutated"):
        return {"status": STATUS_WRITE_NOT_PERFORMED, "mutated": False, "result": result, "idempotency_key": key}

    write_result = result.get("result") or {}
    created_product = write_result.get("product") or {}
    bitrix_id = created_product.get("external_product_id") or (write_result.get("mapping") or {}).get(
        "bitrix_id"
    )

    if write_result.get("status") == "PARTIAL_FAILURE":
        # LiveBitrixAdapter's multi-step CREATE (product -> offer/SKU ->
        # retail price) already got PAST the base product create but a
        # later required step failed -- never claim SUCCESS, and never
        # lose the product id a retry needs to resume from (same
        # idempotency_key: LiveBitrixAdapter itself never repeats an
        # already-succeeded step; see its own idempotency-key cache).
        return {
            "status": STATUS_WRITE_PARTIAL_FAILURE,
            "mutated": True,
            "bitrix_product_id": bitrix_id,
            "name": preview["target_product"]["title"],
            "sku": request.sku,
            "failed_step": write_result.get("failed_step"),
            "error": write_result.get("error"),
            "purchase_price_written": bool(created_product.get("purchase_price_written")),
            "section_id_written": created_product.get("section_id_written"),
            "characteristics_written": created_product.get("characteristics_written") or [],
            "media_written": created_product.get("media_written") or [],
            "idempotency_key": key,
        }

    # Read-back verification extended to cover the resolved section
    # (native ``iblockSectionId``, schema.py module docstring item A) --
    # only when this write actually resolved/sent one, so an
    # already-existing product with no category data supplied is never
    # false-mismatched against an unset expectation.
    expected = {"name": preview["target_product"]["title"], "active": False}
    resolved_section_id = preview["target_product"].get("resolved_section_id")
    if resolved_section_id is not None:
        expected[schema.SECTION_FIELD] = resolved_section_id
    read_back = _read_back_and_compare(
        bridge, tenant_id=tenant_id, bitrix_id=bitrix_id, connection_id=connection_id, expected=expected
    )

    return {
        "status": STATUS_WRITE_VERIFIED if read_back["matches"] else STATUS_WRITE_VERIFICATION_MISMATCH,
        "mutated": True,
        "bitrix_product_id": bitrix_id,
        "name": preview["target_product"]["title"],
        "sku": request.sku,
        "ean_source": request.ean or None,
        "brand": request.brand or None,
        "category_source": request.category_source or None,
        "resolved_section_id": resolved_section_id,
        "retail_price": preview["retail_price"],
        "purchase_price_source": request.purchase_price or None,
        "purchase_price_written": bool(created_product.get("purchase_price_written")),
        "section_id_written": created_product.get("section_id_written"),
        "characteristics_written": created_product.get("characteristics_written") or [],
        "media_written": created_product.get("media_written") or [],
        "active": False,
        "published": False,
        "not_written": preview["will_not_write"],
        "read_back": read_back,
        "idempotency_key": key,
        "idempotent_replay": bool(write_result.get("idempotent")),
    }


def _read_back_and_compare(
    bridge: BitrixProductBridge,
    *,
    tenant_id: str,
    bitrix_id: str | None,
    connection_id: str | None,
    expected: dict,
) -> dict:
    if not bitrix_id:
        return {"matches": False, "reason": "no_bitrix_id_returned_from_write", "observed": {}, "expected": expected}
    try:
        observed = bridge.read_product(tenant_id=tenant_id, bitrix_id=bitrix_id, connection_id=connection_id)
    except Exception as exc:  # noqa: BLE001
        return {
            "matches": False,
            "reason": getattr(exc, "code", type(exc).__name__),
            "observed": {},
            "expected": expected,
        }
    mismatched = [k for k, v in expected.items() if observed.get(k) != v]
    return {
        "matches": not mismatched,
        "observed": {k: observed.get(k) for k in expected},
        "expected": expected,
        "mismatched_fields": mismatched,
    }


def format_bitrix_write_result_text(result: Mapping) -> str:
    """Render the real ``execute_single_product_write`` outcome for the
    Panda UI -- the exact same status/read-back this module already
    produces, never a canned "done" message. Used by the conversational
    approval path (WorkflowPandaConversationGateway) so the user always
    sees the real Bitrix id, retail price, unwritten fields and read-back
    verification result of the write that was just attempted."""
    status = str(result.get("status") or "")

    if status == STATUS_WRITE_VERIFIED or status == STATUS_WRITE_VERIFICATION_MISMATCH:
        retail = result.get("retail_price") or {}
        lines = [
            "Товар создан в Bitrix (неактивен — публикация требует отдельного решения)."
            if status == STATUS_WRITE_VERIFIED
            else "Товар создан в Bitrix, но проверка после записи (read-back) не совпала с ожидаемым.",
            f"Bitrix ID: {result.get('bitrix_product_id')}",
            f"Название: {result.get('name')}",
            f"Артикул/SKU: {result.get('sku')}",
            f"Розничная цена: {retail.get('amount')} {retail.get('currency')}",
        ]
        if result.get("brand"):
            lines.append(f"Бренд: {result.get('brand')}")
        if result.get("purchase_price_written"):
            lines.append(f"Закупочная цена: {result.get('purchase_price_source')} {retail.get('currency')}")
        if result.get("section_id_written"):
            lines.append(f"Раздел каталога: ID {result.get('section_id_written')}")
        if result.get("characteristics_written"):
            lines.append(f"Характеристики записаны: {', '.join(result.get('characteristics_written'))}")
        if result.get("media_written"):
            lines.append(f"Изображения загружены (не hotlink): {', '.join(result.get('media_written'))}")
        not_written = result.get("not_written") or []
        if not_written:
            fields = ", ".join(str(item.get("field")) for item in not_written)
            lines.append(f"Не записано (нет проверенного назначения в Bitrix): {fields}.")
        read_back = result.get("read_back") or {}
        lines.append(
            "Проверка после записи: подтверждена."
            if read_back.get("matches")
            else f"Проверка после записи: НЕ подтверждена ({read_back.get('reason', 'mismatch')})."
        )
        return "\n".join(lines)

    if status == STATUS_EXISTING_PRODUCT_FOUND:
        return (
            "Товар с этим артикулом/SKU уже существует в Bitrix. "
            "Эта операция не обновляет существующие товары автоматически — "
            "нужно отдельное явное решение перед обновлением."
        )
    if status == STATUS_AMBIGUOUS:
        return (
            "Не удалось однозначно определить, существует ли этот товар в Bitrix "
            "(найдено несколько похожих записей). Запись не выполнена."
        )
    if status == STATUS_UNRESOLVED:
        # The reason code alone ("no_matching_section_found") never said
        # WHICH value failed to resolve, so a production failure could not
        # be diagnosed from the owner's own transcript. The detail carries
        # exactly that (candidate + how many sections were read).
        detail = str(result.get("detail") or "").strip()
        text = f"Не удалось подготовить запись в Bitrix: {result.get('reason', 'unresolved')}."
        return f"{text} {detail}" if detail else text
    if status == STATUS_WRITE_PARTIAL_FAILURE:
        step_labels = {
            "offer_create": "создание торгового предложения/артикула (SKU)",
            "price_create": "запись розничной цены",
        }
        step = step_labels.get(str(result.get("failed_step") or ""), str(result.get("failed_step") or "неизвестный шаг"))
        return (
            "ЧАСТИЧНАЯ ОШИБКА: товар создан в Bitrix (неактивен), но следующий "
            f"обязательный шаг не выполнен — {step}. "
            f"Bitrix ID: {result.get('bitrix_product_id')}. "
            f"Причина: {_describe_write_failure(result)}. "
            "Товар НЕ считается полностью записанным; повторное подтверждение "
            "с тем же запросом продолжит запись с этого шага, не создавая "
            "второй товар."
        )
    if status == STATUS_WRITE_FAILED:
        return f"Запись в Bitrix не удалась: {_describe_write_failure(result)}. Товар не создан."
    if status == STATUS_WRITE_NOT_PERFORMED:
        return "Запись в Bitrix не выполнена."
    if status == STATUS_APPROVAL_REQUIRED:
        return "Это действие требует явного подтверждения перед записью в Bitrix."
    return "Не удалось выполнить запись в Bitrix."
