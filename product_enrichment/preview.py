"""Human-facing complete enrichment preview (requirement 11).

Renders the enrichment-specific sections (identity/content/characteristics/
media/SEO + READY TO WRITE / NOT WRITABLE / MISSING SOURCE DATA). Section/
category resolution against the real Bitrix catalog and retail-price/
purchase-price display remain
``business_assistant.controlled_bitrix_write.prepare_single_product_write``'s
job (unchanged) -- ``business_assistant.product_enrichment_bridge`` combines
both into the single message the user sees, never duplicating that logic
here.

No Bitrix mutation happens anywhere in this module -- it only ever reads
an already-computed ``EnrichmentResult``.
"""

from __future__ import annotations

from product_enrichment.models import CONFIDENCE_VERIFIED, EnrichmentResult, MediaResult

_IMPORTANT_CHARACTERISTIC_LIMIT = 5


def enrichment_preview_dict(result: EnrichmentResult) -> dict:
    """Structured preview payload -- easy to unit test and to attach to
    conversational metadata without re-parsing rendered text."""
    identity = result.identity
    characteristics = result.characteristics
    important = sorted(
        (
            {"key": key, "value": c.value, "unit": c.unit, "confidence": c.confidence}
            for key, c in characteristics.items()
        ),
        key=lambda item: item["key"],
    )[:_IMPORTANT_CHARACTERISTIC_LIMIT]

    media = result.media
    main_image_prepared = any(a.role in ("preview", "detail") for a in media.assets)
    gallery_count = sum(1 for a in media.assets if a.role == "gallery")

    ready: list[str] = []
    not_writable: list[str] = []
    missing: list[str] = []

    if identity.brand and identity.model:
        ready.append("identity")
    else:
        missing.append("identity")
    if characteristics:
        ready.append("characteristics")
    else:
        missing.append("characteristics")
    if result.content.short_description:
        ready.append("content")
    else:
        missing.append("content")
    if main_image_prepared:
        ready.append("media")
    elif media.status == MediaResult.STATUS_UNRESOLVED:
        missing.append("media")
    else:
        not_writable.append("media")
    if not result.research_available:
        missing.append("research (search/fetch capability not available for this run)")
    for conflict in result.conflicts:
        not_writable.append(f"conflict:{conflict.code}")

    return {
        "identity": {
            "brand": identity.brand,
            "model": identity.model,
            "article": identity.article,
            "ean": identity.ean,
        },
        "classification_source": {"category": identity.category, "subcategory": identity.subcategory},
        "content": {
            "short_description": result.content.short_description,
            "detailed_description": result.content.detailed_description,
            "detailed_description_status": "prepared" if result.content.detailed_description else "missing",
            "detailed_description_facts_used": list(result.content.facts_used),
            "seo_title": result.content.seo_title,
            "seo_description": result.content.seo_description,
            "seo_keywords": list(result.content.seo_keywords),
        },
        "characteristics": {
            "count": len(characteristics),
            "important": important,
            "verified_count": sum(1 for c in characteristics.values() if c.confidence == CONFIDENCE_VERIFIED),
        },
        "media": {
            "main_image_prepared": main_image_prepared,
            "gallery_image_count": gallery_count,
            "rejected_count": len(media.rejected_candidates),
            # Why no image was prepared is otherwise invisible to the user
            # (and to production diagnostics): every candidate rejection
            # already carries a stable reason code, so surface them.
            "rejected_reasons": sorted({r.reason for r in media.rejected_candidates}),
            "status": media.status,
        },
        "seo": {
            "title": result.content.seo_title,
            "description": result.content.seo_description,
            "keywords": list(result.content.seo_keywords),
            "image_alt": result.content.image_alt,
            "image_title": result.content.image_title,
        },
        "ready_to_write": ready,
        "not_writable_or_unresolved": not_writable,
        "missing_source_data": missing,
        "cache_hit": result.cache_hit,
    }


def format_enrichment_preview_text(result: EnrichmentResult) -> str:
    data = enrichment_preview_dict(result)
    lines: list[str] = []
    identity = data["identity"]
    lines.append("ПОЛНАЯ КАРТОЧКА ТОВАРА (предпросмотр, запись в Bitrix НЕ выполнена):")
    lines.append(f"Товар: {identity['brand']} {identity['model']}".strip())
    if identity["article"]:
        lines.append(f"Артикул: {identity['article']}")
    if identity["ean"]:
        lines.append(f"EAN: {identity['ean']}")

    content = data["content"]
    if content["short_description"]:
        lines.append(f"Короткое описание: {content['short_description']}")
    if content["detailed_description"]:
        # The generated text itself, never just the word "prepared": it is
        # composed strictly from the verified facts listed above, so the
        # user must be able to read (and approve) exactly what would be
        # written to Bitrix.
        lines.append("Подробное описание:")
        lines.extend(f"  {line}" for line in content["detailed_description"].splitlines())
    else:
        lines.append(f"Подробное описание: {content['detailed_description_status']}")
    if content["seo_title"]:
        lines.append(f"SEO заголовок: {content['seo_title']}")

    chars = data["characteristics"]
    lines.append(
        f"Характеристики: {chars['count']} (подтверждено: {chars['verified_count']})"
    )
    for item in chars["important"]:
        unit = f" {item['unit']}" if item["unit"] else ""
        lines.append(f"  - {item['key']}: {item['value']}{unit} ({item['confidence']})")

    media = data["media"]
    lines.append(
        "Главное изображение подготовлено: "
        + ("да" if media["main_image_prepared"] else "нет")
        + f"; изображений в галерее: {media['gallery_image_count']}"
    )
    if not media["main_image_prepared"] and media["rejected_reasons"]:
        lines.append(f"Изображения отклонены: {', '.join(media['rejected_reasons'])}")

    lines.append(f"READY TO WRITE: {', '.join(data['ready_to_write']) or '(нет)'}")
    lines.append(f"NOT WRITABLE / UNRESOLVED: {', '.join(data['not_writable_or_unresolved']) or '(нет)'}")
    lines.append(f"MISSING SOURCE DATA: {', '.join(data['missing_source_data']) or '(нет)'}")

    if data["cache_hit"]:
        lines.append("(использованы ранее подготовленные данные обогащения — повторный поиск не выполнялся)")

    return "\n".join(lines)
