"""Connects the ``product_enrichment`` pipeline to the EXISTING, unchanged
governed Bitrix write path (PR #43/#47/#48's
``business_assistant.controlled_bitrix_write``).

This is the "missing orchestration" the enrichment task asked for -- no new
architecture, no new write path, no new approval gate. It:

1. builds a ``product_enrichment.models.ProductIdentityQuery`` from the
   SAME flat ``product_fields`` dict ``data_intel.service._row_lookup_result``
   already produces (brand/sku/ean/category/purchase_price);
2. runs the enrichment pipeline (``product_enrichment.orchestrator.
   enrich_product``), reusing the caller-supplied ``ToolGateway`` for
   research if one is available, and the caller-supplied media candidates
   for images;
3. merges the enrichment output into the EXISTING, unchanged
   ``SingleProductWriteRequest`` (never re-declaring its own write model);
4. optionally calls the EXISTING, unchanged, read-only
   ``prepare_single_product_write`` for a resolved-section/will-write
   preview -- still zero Bitrix mutation;
5. renders ONE combined, complete preview message (requirement 11).

The later, EXPLICIT confirmation turn still goes through the exact same
``execute_single_product_write(..., approved=True)`` call PR #43 already
uses -- this module only changes what request gets built, never how/when
it is written.
"""

from __future__ import annotations

import dataclasses
from typing import Mapping, Sequence

from business_assistant.controlled_bitrix_write import (
    SingleProductWriteRequest,
    build_write_request_from_fields,
    prepare_single_product_write,
)
from product_enrichment.cache import EnrichmentCache
from product_enrichment.characteristics import CANONICAL_CHARACTERISTIC_ALIASES
from product_enrichment.content import generate_content
from product_enrichment.media_fetch import ImageFetchPort
from product_enrichment.models import EnrichmentResult, MediaCandidateInput, ProductIdentityQuery
from product_enrichment.observability import EnrichmentObserver
from product_enrichment.orchestrator import enrich_product
from product_enrichment.preview import enrichment_preview_dict, format_enrichment_preview_text
from product_enrichment.research import ToolGatewayResearchAdapter, resolve_brand_from_model


def build_identity_query_from_fields(fields: Mapping[str, str]) -> ProductIdentityQuery:
    """Mirrors ``build_write_request_from_fields``'s own field-name
    contract -- the SAME flat dict, read-only, never mutated."""
    sku = str(fields.get("sku") or "")
    return ProductIdentityQuery(
        brand=str(fields.get("brand") or ""),
        model=str(fields.get("model") or sku),
        article=sku,
        ean=str(fields.get("ean") or ""),
        category=str(fields.get("category") or ""),
        subcategory=str(fields.get("subcategory") or ""),
    )


async def run_enrichment(
    *,
    tenant_id: str,
    product_fields: Mapping[str, str],
    tool_gateway=None,
    media_fetcher: ImageFetchPort | None = None,
    media_candidates: Sequence[MediaCandidateInput] = (),
    cache: EnrichmentCache | None = None,
    observer: EnrichmentObserver | None = None,
) -> EnrichmentResult:
    """Runs the enrichment pipeline for one supplier row. ``tool_gateway``
    is optional -- when absent, research is skipped (never hallucinated;
    see ``product_enrichment.orchestrator.enrich_product``'s own
    docstring), and the resulting preview reports it under
    ``missing_source_data``."""
    query = build_identity_query_from_fields(product_fields)
    search_port = fetch_port = None
    if tool_gateway is not None:
        adapter = ToolGatewayResearchAdapter(tool_gateway, tenant_id=tenant_id)
        search_port = adapter
        fetch_port = adapter

        # Search budgets are owned by ONE product-enrichment run, not by
        # the surrounding chat/request. A previous product or unrelated
        # search must never starve this product.
        if hasattr(tool_gateway, "reset_budget"):
            tool_gateway.reset_budget()

        # Supplier feeds may carry a strong exact model/article but no
        # separate brand column. Resolve ONLY the missing brand from
        # conservative search evidence before identity validation; never
        # override an explicit supplier brand and never guess on conflict.
        brand_discovery_attempted = False
        if not query.brand and (query.model or query.article):
            brand_discovery_attempted = True
            discovered_brand = await resolve_brand_from_model(
                query.model or query.article, search_port=adapter
            )
            if discovered_brand:
                query = dataclasses.replace(query, brand=discovered_brand)

        # Brand discovery and specification/media research are separate
        # bounded phases. A hard-to-identify model may legitimately consume
        # the full discovery allowance (first query + one fallback); that
        # must not leave zero budget for the actual product research.
        if brand_discovery_attempted and query.brand and hasattr(tool_gateway, "reset_budget"):
            tool_gateway.reset_budget()
    return await enrich_product(
        tenant_id=tenant_id,
        query=query,
        search_port=search_port,
        fetch_port=fetch_port,
        media_fetcher=media_fetcher,
        media_candidates=media_candidates,
        cache=cache,
        observer=observer,
    )


def build_enriched_write_request(
    product_fields: Mapping[str, str],
    *,
    tenant_id: str,
    retail_price: str,
    enrichment: EnrichmentResult,
    currency: str = "RUB",
    product_id: str = "",
) -> SingleProductWriteRequest:
    """Builds the EXISTING ``SingleProductWriteRequest`` from the flat
    ``product_fields`` dict (unchanged base behavior -- see
    ``build_write_request_from_fields``), then layers verified enrichment
    output on top. A field the raw XLSX row already supplied always wins
    over the enrichment-derived value (the supplier's own explicit data is
    never silently overridden by research); enrichment only FILLS GAPS."""
    base = build_write_request_from_fields(
        dict(product_fields), tenant_id=tenant_id, retail_price=retail_price, currency=currency, product_id=product_id
    )
    content = generate_content(enrichment.identity, enrichment.characteristics)
    characteristics = {key: c.value for key, c in enrichment.characteristics.items()
                       if c.bitrix_writable and c.confidence == "verified"}
    merged_characteristics = {**characteristics, **dict(base.characteristics)}

    preview_asset = next((a for a in enrichment.media.assets if a.role == "preview"), None)
    detail_asset = next((a for a in enrichment.media.assets if a.role == "detail"), None)
    # Gallery follow-up defect closure: previously enrichment's gallery
    # assets were computed (see ``enrichment.media.assets``, role
    # "gallery") but never forwarded into the write request at all --
    # only reported in the preview as "will NOT be written" (see the
    # OLD ``format_write_plan_text`` gallery line this same change set
    # updates). ``gallery_pictures`` now has a real, verified write
    # destination (MORE_PHOTO, offer property 280 --
    # ``integrations.bitrix.schema.OFFER_PROPERTIES``/
    # ``LiveBitrixAdapter._gallery_offer_fields``), so it is passed
    # through here exactly like preview/detail above.
    gallery_assets = [a for a in enrichment.media.assets if a.role == "gallery"]

    resolved_title = base.title or f"{enrichment.identity.brand} {enrichment.identity.model}".strip()

    return dataclasses.replace(
        base,
        title=resolved_title,
        brand=base.brand or enrichment.identity.brand,
        subcategory=base.subcategory or enrichment.identity.subcategory,
        category_source=base.category_source or enrichment.identity.category,
        short_description=base.short_description or content.short_description,
        detailed_description=base.detailed_description or content.detailed_description,
        characteristics=merged_characteristics,
        preview_picture=dict(base.preview_picture)
        or ({"filename": preview_asset.filename, "base64": preview_asset.base64_content} if preview_asset else {}),
        detail_picture=dict(base.detail_picture)
        or ({"filename": detail_asset.filename, "base64": detail_asset.base64_content} if detail_asset else {}),
        gallery_pictures=tuple(base.gallery_pictures or ())
        or tuple({"filename": a.filename, "base64": a.base64_content} for a in gallery_assets),
    )


def format_combined_preview_text(enrichment: EnrichmentResult, write_preview: Mapping | None) -> str:
    """Combines the enrichment-specific preview (identity/content/
    characteristics/media/SEO -- ``product_enrichment.preview``) with the
    EXISTING controlled-write preview's classification/pricing summary
    (resolved Bitrix section, will_write/will_not_write) into ONE message
    -- requirement 11's "complete preview", without duplicating either
    module's own rendering logic."""
    lines = [format_enrichment_preview_text(enrichment)]
    write_preview = write_preview or {}
    if write_preview.get("status") == "REQUIRES_APPROVAL":
        target = write_preview.get("target_product") or {}
        if target.get("resolved_section_id") is not None:
            lines.append(f"Раздел каталога (Bitrix): ID {target.get('resolved_section_id')}")
        retail = write_preview.get("retail_price") or {}
        if retail.get("amount"):
            lines.append(f"Розничная цена: {retail.get('amount')} {retail.get('currency')}")
        will_write = write_preview.get("will_write") or []
        if will_write:
            lines.append(f"Будет записано в Bitrix: {', '.join(will_write)}")
        will_not_write = write_preview.get("will_not_write") or []
        if will_not_write:
            fields = ", ".join(str(item.get("field")) for item in will_not_write)
            lines.append(f"НЕ будет записано (нет проверенного назначения в Bitrix): {fields}")
    elif write_preview.get("status"):
        lines.append(f"Статус подготовки записи в Bitrix: {write_preview.get('status')} ({write_preview.get('reason', '')})".rstrip(" ()"))
    lines.append(
        "Обогащение карточки — это НЕ подтверждение записи. Запись в Bitrix "
        "выполняется только после отдельного явного подтверждения."
    )
    return "\n".join(lines)


def serialize_characteristic_status(enrichment: EnrichmentResult) -> dict:
    """Dict-safe per-characteristic status for persisting across turns
    (``ActiveTask.parameters``), so the read-only "what exactly would be
    written?" follow-up can report each characteristic's ALREADY computed
    confidence and Bitrix destination without re-running enrichment. Pure
    projection -- confidence is never recomputed here."""
    return {
        key: {
            "value": c.value,
            "unit": c.unit,
            "confidence": c.confidence,
            "bitrix_property_id": c.bitrix_property_id,
            "bitrix_writable": bool(c.bitrix_writable),
        }
        for key, c in enrichment.characteristics.items()
    }


def _characteristic_label(key: str) -> str:
    aliases = CANONICAL_CHARACTERISTIC_ALIASES.get(key)
    return aliases[1][0] if aliases else key


def _characteristic_value(key: str, value: object, info: Mapping) -> str:
    text = str(value)
    aliases = CANONICAL_CHARACTERISTIC_ALIASES.get(key)
    unit = info.get("unit") or (aliases[0] if aliases else "")
    if unit and text.strip().lstrip("+-").replace(".", "", 1).replace(",", "", 1).isdigit():
        unit = {"cm": "см", "mm": "мм", "kg": "кг", "Hz": "Гц", "W": "Вт"}.get(unit, unit)
        return f"{text} {unit}"
    return text


def _confidence_label(value: str) -> str:
    return {
        "verified": "подтверждено источниками",
        "probable": "предположительно — требуется проверка",
        "unverified": "не подтверждено",
        "conflicting": "источники противоречат друг другу",
    }.get(value, "не подтверждено")


def format_write_plan_text(
    *,
    write_request: SingleProductWriteRequest,
    write_preview: Mapping | None,
    characteristic_status: Mapping | None = None,
    enrichment_preview: Mapping | None = None,
) -> str:
    """Renders the read-only answer to "покажи точно, что именно будет
    записано в Bitrix/Aspro, если я подтвержу запись" from the ALREADY
    prepared card state (production defect closure). Reports the EXISTING
    write path's own verdict -- ``write_preview`` is whatever the unchanged,
    read-only ``prepare_single_product_write`` returned -- plus each
    characteristic's already-computed verified/probable status. Decides
    nothing itself and writes nothing."""
    status_map = dict(characteristic_status or {})
    preview = dict(enrichment_preview or {})
    write_preview = dict(write_preview or {})

    lines = [
        "ЧТО БУДЕТ ЗАПИСАНО В BITRIX/ASPRO ПРИ ПОДТВЕРЖДЕНИИ (сейчас ничего не записано):",
        f"Товар: {write_request.title}",
        f"Артикул: {write_request.sku}",
    ]
    product_model = write_preview.get("product_model") or {}
    if product_model.get("model") == "SKU_WITH_OFFER":
        lines.append("Модель товара: товар с торговым предложением")
    elif product_model.get("model") == "SIMPLE_PRODUCT":
        lines.append(
            "Модель товара: обычный товар без торговых предложений — "
            "нет данных о вариантах товара для этой записи"
        )
    target_product = dict(write_preview.get("target_product") or {})
    if target_product.get("code"):
        lines.append(f"Код товара в адресе страницы: {target_product.get('code')}")
    brand_plan = dict(write_preview.get("brand_plan") or {})
    if write_request.brand:
        if brand_plan.get("action") == "create":
            lines.append(
                f"Бренд: {write_request.brand} — будет создан после подтверждения, "
                "до создания товара. Связь с товаром Panda заполнит автоматически."
            )
        elif brand_plan.get("action") == "existing" or write_request.brand_id:
            lines.append(f"Бренд: {write_request.brand} — будет использован существующий бренд.")
        else:
            lines.append(f"Бренд: {write_request.brand} — привязка не подтверждена; бренд не будет записан.")
    if write_preview.get("status") == "REQUIRES_APPROVAL":
        active = write_preview.get("active_after_create")
        if active is False:
            lines.append("Статус после создания: неактивный — товар не будет опубликован на сайте.")
        elif active is True:
            lines.append("Статус после создания: активный — товар будет опубликован на сайте.")
        else:
            lines.append("Статус активности после создания: не указан в плане.")
    if write_request.ean:
        lines.append(f"EAN: {write_request.ean}")
    if write_request.purchase_price:
        lines.append(f"Закупочная цена: {write_request.purchase_price} {write_request.currency}")
    if write_request.retail_price:
        lines.append(f"Розничная цена: {write_request.retail_price} {write_request.currency}")

    written = dict(write_request.characteristics or {})
    lines.append(f"ХАРАКТЕРИСТИКИ, КОТОРЫЕ БУДУТ ЗАПИСАНЫ: {len(written)}")
    for key in sorted(written):
        info = dict(status_map.get(key) or {})
        confidence = str(info.get("confidence") or "")
        lines.append(f"  - {_characteristic_label(key)}: {_characteristic_value(key, written[key], info)} ({_confidence_label(confidence)})")

    skipped = [key for key in sorted(status_map) if key not in written]
    lines.append(f"ХАРАКТЕРИСТИКИ, КОТОРЫЕ НЕ БУДУТ ЗАПИСАНЫ: {len(skipped)}")
    for key in skipped:
        info = dict(status_map.get(key) or {})
        confidence = str(info.get("confidence") or "")
        if not info.get("bitrix_writable"):
            reason = "нет проверенного свойства в Bitrix" if not info.get("bitrix_property_id") else "не подтверждено для записи"
        else:
            reason = _confidence_label(confidence)
        lines.append(f"  - {_characteristic_label(key)}: {_characteristic_value(key, info.get('value', ''), info)} ({_confidence_label(confidence)}) — {reason}")

    if skipped:
        lines.append("Эти характеристики не заполнят отдельные поля каталога, но могут присутствовать в описании ниже.")
    uncertain = [key for key in set(status_map) | set(written)
                 if (status_map.get(key) or {}).get("confidence") != "verified"]
    if uncertain:
        lines.append(
            "ТРЕБУЕТ ПРОВЕРКИ: в исходных данных есть неподтверждённые характеристики: "
            + ", ".join(_characteristic_label(key) for key in sorted(uncertain))
            + ". Проверьте их также в описании перед подтверждением записи."
        )
    for field, label in (
        ("weight_g", "Вес, г"), ("length_mm", "Длина, мм"),
        ("width_mm", "Ширина, мм"), ("height_mm", "Высота, мм"),
    ):
        value = getattr(write_request, field)
        if value:
            lines.append(f"{label}: {value}")

    preview_picture = dict(write_request.preview_picture or {})
    detail_picture = dict(write_request.detail_picture or {})
    lines.append("ИЗОБРАЖЕНИЯ, КОТОРЫЕ БУДУТ ЗАПИСАНЫ:")
    if preview_picture.get("filename"):
        lines.append(f"  - превью: {preview_picture['filename']} (файл загружается в Bitrix, не ссылка)")
    if detail_picture.get("filename"):
        lines.append(f"  - детальное: {detail_picture['filename']} (файл загружается в Bitrix, не ссылка)")
    if not preview_picture.get("filename") and not detail_picture.get("filename"):
        lines.append("  - нет подготовленных изображений")
    gallery_pictures = tuple(write_request.gallery_pictures or ())
    if gallery_pictures:
        # SIMPLE_PRODUCT TV contract-alignment pass (ticket T-F79758
        # follow-up): MORE_PHOTO now has a verified destination on the
        # LIVE bridge regardless of product model -- base-product
        # property 124 for the default SIMPLE_PRODUCT model, or offer
        # property 280 when the request genuinely has a variant dimension
        # (``has_variant_offer=True``). Only a non-LIVE bridge (FIXTURE/
        # SANDBOX, which does not yet persist gallery at all) still
        # reports it under ``will_not_write``.
        gallery_not_written = any(
            item.get("field") == "gallery_pictures" for item in write_preview.get("will_not_write") or []
        )
        if not gallery_not_written:
            lines.append(
                f"  - галерея: {len(gallery_pictures)} изображений (файлы будут загружены в карточку товара)"
            )
        else:
            lines.append(
                f"  - галерея: {len(gallery_pictures)} изображени(й) подготовлено, но НЕ будет записано "
                "(текущее подключение не поддерживает запись галереи)"
            )
    else:
        gallery_count = int((preview.get("media") or {}).get("gallery_image_count") or 0)
        if gallery_count:
            lines.append(f"  - галерея: {gallery_count} — не подготовлена для записи (нет обработанных файлов)")

    lines.append("ОПИСАНИЕ, КОТОРОЕ БУДЕТ ЗАПИСАНО:")
    lines.append(f"  Короткое: {write_request.short_description or '(нет)'}")
    detailed = write_request.detailed_description or "(нет)"
    lines.append("  Подробное:")
    for line in detailed.splitlines() or [detailed]:
        lines.append(f"    {line}")

    # The resolved category/section can be present either on a successful
    # REQUIRES_APPROVAL preview (nested under ``target_product``) or on an
    # otherwise-UNRESOLVED preview that still resolved the category before
    # failing closed on a still-unknown retail price (top-level key --
    # production defect closure: category must not be hidden just because
    # the price is not yet known). Rendered once, regardless of status, so
    # neither branch below needs to repeat it.
    resolved_section_id = write_preview.get("resolved_section_id")
    if resolved_section_id is None:
        resolved_section_id = (write_preview.get("target_product") or {}).get("resolved_section_id")
    if resolved_section_id is not None:
        lines.append(f"Раздел каталога: №{resolved_section_id}")
        category_source = write_request.subcategory or write_request.category_source
        if category_source:
            lines.append(f"Категория из карточки: {category_source}")

    if write_preview.get("status") == "REQUIRES_APPROVAL":
        lines.append("План подготовлен. Создание выполнится только после вашего подтверждения.")
        for item in write_preview.get("will_not_write") or []:
            field = str(item.get("field") or "")
            label, reason = {
                "ean": ("Штрихкод", "не настроено поле для записи"),
                "purchase_price": ("Закупочная цена", "текущее подключение не поддерживает запись закупочной цены"),
                "category": ("Раздел каталога", "текущее подключение не поддерживает запись раздела"),
                "gallery_pictures": ("Галерея", "текущее подключение не поддерживает запись галереи"),
                "brand": ("Бренд", "привязка не подтверждена"),
            }.get(field, (field, str(item.get("reason") or "")))
            if field.startswith("characteristic:"):
                label = _characteristic_label(field.partition(":")[2])
                reason = "нет проверенного поля для записи"
            lines.append(f"НЕ будет записано: {label} = {item.get('value')} — {reason}")
    elif write_preview.get("status"):
        lines.append(
            f"Статус подготовки записи в Bitrix: {write_preview.get('status')} "
            + (f"({write_preview['reason']})" if write_preview.get("reason") else "")
        )
    else:
        lines.append(
            "Предпросмотр записи Bitrix недоступен (нет розничной цены или интеграция не настроена) — "
            "состав полей выше взят из подготовленной карточки."
        )

    lines.append(
        "Ничего в Bitrix не записано: это только предпросмотр. Запись выполняется "
        "только после отдельного явного подтверждения."
    )
    return "\n".join(lines)


async def prepare_complete_card(
    *,
    tenant_id: str,
    product_fields: Mapping[str, str],
    retail_price: str = "",
    bitrix_bridge=None,
    connection_id: str | None = None,
    tool_gateway=None,
    media_fetcher: ImageFetchPort | None = None,
    media_candidates: Sequence[MediaCandidateInput] = (),
    cache: EnrichmentCache | None = None,
    observer: EnrichmentObserver | None = None,
) -> dict:
    """The single entry point the conversational layer calls for
    "Подготовь полную карточку...": runs enrichment, merges it into the
    existing write request shape, optionally previews the write (read-only,
    zero mutation) if a retail price is already known, and renders the
    combined complete-preview text."""
    enrichment = await run_enrichment(
        tenant_id=tenant_id,
        product_fields=product_fields,
        tool_gateway=tool_gateway,
        media_fetcher=media_fetcher,
        media_candidates=media_candidates,
        cache=cache,
        observer=observer,
    )
    write_request = build_enriched_write_request(
        product_fields, tenant_id=tenant_id, retail_price=retail_price, enrichment=enrichment
    )
    write_preview: dict = {}
    if bitrix_bridge is not None and retail_price:
        try:
            write_preview = prepare_single_product_write(
                bitrix_bridge, tenant_id=tenant_id, request=write_request, connection_id=connection_id
            )
        except Exception:  # noqa: BLE001 -- the enrichment preview must never fail because the write-preview call did
            write_preview = {}
    text = format_combined_preview_text(enrichment, write_preview)
    return {
        "enrichment": enrichment,
        "enrichment_preview": enrichment_preview_dict(enrichment),
        "write_request": write_request,
        "write_preview": write_preview,
        "text": text,
    }


def serialize_write_request(request: SingleProductWriteRequest) -> dict:
    """JSON/dict-safe serialization for persisting the enriched write
    request across conversational turns (e.g. on
    ``business_assistant.action_continuation.ActiveTask.parameters`` --
    mirrors how ``bitrix_product_fields`` is already persisted there)."""
    data = dataclasses.asdict(request)
    data["characteristics"] = dict(data.get("characteristics") or {})
    data["preview_picture"] = dict(data.get("preview_picture") or {})
    data["detail_picture"] = dict(data.get("detail_picture") or {})
    return data


def deserialize_write_request(data: Mapping) -> SingleProductWriteRequest:
    known_fields = {f.name for f in dataclasses.fields(SingleProductWriteRequest)}
    kwargs = {k: v for k, v in dict(data).items() if k in known_fields}
    return SingleProductWriteRequest(**kwargs)
