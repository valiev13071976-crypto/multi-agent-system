"""Marketplace payload mapping — reuse provider map_category; no invented facts."""

from __future__ import annotations

from integrations.ozon.mapping import map_category as ozon_map_category
from integrations.wildberries.mapping import map_category as wb_map_category
from integrations.yandex_market.mapping import map_category as ym_map_category
from product_content.contracts import ProductContentPackage

from governed_publish.contracts import MAP_AMBIGUOUS, MAP_MAPPED, MAP_MISSING, MAP_UNSUPPORTED, TARGET_OZON, TARGET_WILDBERRIES, TARGET_YANDEX_MARKET


DEFAULT_MAPS = {
    TARGET_WILDBERRIES: {"smartphone": "phones", "headphones": "accessories"},
    TARGET_OZON: {"smartphone": "phones", "headphones": "accessories"},
    TARGET_YANDEX_MARKET: {"smartphone": "phones", "headphones": "accessories"},
}

_MAPPERS = {
    TARGET_WILDBERRIES: wb_map_category,
    TARGET_OZON: ozon_map_category,
    TARGET_YANDEX_MARKET: ym_map_category,
}

MANDATORY = ("sku", "name")
SMARTPHONE_EXTRA = ("brand",)


def map_marketplace_category(target: str, canonical: str, *, category_map: dict | None = None, ambiguous: set[str] | None = None) -> tuple[str, str]:
    if canonical in (ambiguous or ()):
        return MAP_AMBIGUOUS, ""
    mapping = dict(DEFAULT_MAPS.get(target, {}))
    if category_map:
        mapping.update(category_map)
    raw = mapping.get(canonical)
    if isinstance(raw, (list, tuple)):
        return MAP_AMBIGUOUS, ""
    if not raw:
        return MAP_MISSING, ""
    mapper = _MAPPERS.get(target)
    if mapper is None:
        return MAP_UNSUPPORTED, ""
    mapped = mapper(canonical_category_id=str(raw), category_map=None)
    return MAP_MAPPED, mapped


def marketplace_payload(package: ProductContentPackage, *, target: str, category_map: dict | None = None, ambiguous: set[str] | None = None) -> dict:
    card = package.card
    cat_status, cat_id = map_marketplace_category(target, card.category, category_map=category_map, ambiguous=ambiguous)
    media = [
        {"asset_id": a.asset_id, "role": a.role, "sort_order": a.sort_order, "checksum": a.checksum}
        for a in sorted(package.media.assets, key=lambda x: x.sort_order)
        if a.validation_status == "VALID"
    ]
    attrs = {k: {"value": v.normalized, "provenance": v.provenance} for k, v in card.specifications.items() if v.normalized}
    payload = {
        "seller_sku": card.sku,
        "article": card.article or card.sku,
        "title": card.canonical_title or card.product_name,
        "brand": card.brand or None,
        "category_canonical": card.category,
        "category_status": cat_status,
        "category_id": cat_id or None,
        "description": card.short_description,
        "attributes": attrs,
        "media": media,
        "barcode": card.barcode or None,
        "dimensions": card.dimensions or None,
        "weight": card.weight or None,
        "content_version": package.version,
        "seo_grounded": True,
        "price_preview_only": card.selling_price,
        "stock": None,
        "target": target,
        "mode": "FIXTURE",
    }
    return {k: v for k, v in payload.items() if v not in (None, "", [], {})}


def validate_marketplace_payload(payload: dict, *, category: str) -> list[str]:
    issues: list[str] = []
    if payload.get("category_status") == MAP_MISSING:
        issues.append("missing_category_mapping")
    if payload.get("category_status") == MAP_AMBIGUOUS:
        issues.append("ambiguous_category_mapping")
    if payload.get("category_status") == MAP_UNSUPPORTED:
        issues.append("unsupported_category_mapping")
    for f in MANDATORY:
        key = "seller_sku" if f == "sku" else ("title" if f == "name" else f)
        if not payload.get(key):
            issues.append(f"missing_mandatory:{f}")
    if category == "smartphone":
        for f in SMARTPHONE_EXTRA:
            if not payload.get(f):
                issues.append(f"missing_mandatory:{f}")
    return issues
