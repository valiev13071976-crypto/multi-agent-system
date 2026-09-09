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
invented Bitrix/Aspro property codes: only the fields with a schema-verified
destination (``integrations.bitrix.schema.CATALOG_PRODUCT_PROPERTIES`` /
``OFFER_PROPERTIES``) are ever written. Fields this production installation
has no verified destination for (EAN/GTIN, purchase/wholesale price -- see
module docstring in ``integrations/bitrix/schema.py``: neither IBLOCK 14 nor
15's known real properties include one) are reported to the user as
*sourced from the file* but explicitly **not written**, never guessed onto
an invented property id.

Purchase price and retail price are kept structurally separate: the
canonical payload built here never carries a ``purchase_price`` key
anywhere a write path could read it, so the known
``BitrixFixtureAdapter._write_product_create`` fallback (selling_price ->
purchase_price when selling_price is blank) can never trigger -- retail
price is a required field for this flow and is validated non-empty before
any write is attempted.

The created product is always written **inactive** (``active=False``): a
controlled first production write must not go live on the storefront
without a further, separate, explicit publish decision.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
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
_NO_PURCHASE_PRICE_DESTINATION = (
    "no_verified_bitrix_destination_for_purchase_price_on_this_installation "
    "(catalog.price.list rows are regional selling prices, not a wholesale/"
    "purchase price type -- see integrations.bitrix.schema)"
)
_NO_CATEGORY_MAPPING = (
    "no_established_category_name_to_bitrix_section_mapping_for_this_tenant "
    "(unlike marketplaces' MarketplaceCategoryMap, Bitrix sections have no "
    "equivalent verified name->id table here -- resolving one would require "
    "guessing)"
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
    )


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


def _canonical_payload(request: SingleProductWriteRequest, *, retail_amount: str) -> dict:
    # Deliberately NEVER includes a ``purchase_price`` key anywhere in this
    # payload -- retail (selling) price is the only price value a write can
    # ever read, structurally preventing purchase price from leaking into
    # the public selling-price field.
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
    return canonical


def prepare_single_product_write(
    bridge: BitrixProductBridge,
    *,
    tenant_id: str,
    request: SingleProductWriteRequest,
) -> dict:
    """Read-only. Resolves the duplicate check and destination mapping and
    returns a preview for OWNER approval. Never mutates anything."""
    title = clean_text(request.title) or ""
    sku = clean_text(request.sku) or ""
    if not title or not sku:
        return {"status": STATUS_UNRESOLVED, "reason": "missing_title_or_sku"}

    retail_amount = _normalize_price(request.retail_price)
    if retail_amount is None:
        return {"status": STATUS_UNRESOLVED, "reason": "missing_or_invalid_retail_price"}

    canonical = _canonical_payload(request, retail_amount=retail_amount)
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

    not_written = [
        {"field": "ean", "value": request.ean, "reason": _NO_EAN_DESTINATION}
        if request.ean
        else None,
        {"field": "purchase_price", "value": request.purchase_price, "reason": _NO_PURCHASE_PRICE_DESTINATION}
        if request.purchase_price
        else None,
        {"field": "category", "value": request.category_source, "reason": _NO_CATEGORY_MAPPING}
        if request.category_source
        else None,
    ]
    not_written = [item for item in not_written if item]

    will_write = ["name", "article/sku", "retail_selling_price"]
    if request.brand:
        will_write.append("brand (property 100 / BRAND, verified PANDA_MANAGED)")

    return {
        "status": STATUS_REQUIRES_APPROVAL,
        "plan_action": SYNC_CREATE,
        "max_products": 1,
        "target_product": {
            "title": title,
            "sku": sku,
            "brand": request.brand or None,
            "category_source": request.category_source or None,
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

    preview = prepare_single_product_write(bridge, tenant_id=tenant_id, request=request)
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
            "idempotency_key": key,
        }

    expected = {"name": preview["target_product"]["title"], "active": False}
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
        "retail_price": preview["retail_price"],
        "purchase_price_source": request.purchase_price or None,
        "purchase_price_written": False,
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
        return f"Не удалось подготовить запись в Bitrix: {result.get('reason', 'unresolved')}."
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
