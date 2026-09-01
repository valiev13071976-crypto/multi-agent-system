"""Product identity resolution and Aspro Premier mapping."""

from __future__ import annotations

from commerce.product_platform.aspro import fixture_aspro_premier_profile, map_product_to_bitrix_payload
from integrations.bitrix.catalog import BitrixCatalogStore, normalize_product
from integrations.bitrix.errors import BitrixAmbiguousTargetError, BitrixNotFoundError, BitrixValidationError


def build_preview(*, operation: str, before: dict | None, after: dict) -> dict:
    return {
        "operation": operation,
        "before": before or {},
        "after": after,
        "safe": True,
    }


def resolve_product_target(
    store: BitrixCatalogStore,
    *,
    tenant_id: str,
    bitrix_id: str = "",
    xml_id: str = "",
    article: str = "",
    panda_product_id: str = "",
    name: str = "",
    allow_name_only: bool = False,
) -> dict:
    result = store.lookup(
        tenant_id=tenant_id,
        bitrix_id=bitrix_id,
        xml_id=xml_id,
        article=article,
        panda_product_id=panda_product_id,
        name=name,
        allow_name_only=allow_name_only,
    )
    if isinstance(result, list):
        raise BitrixAmbiguousTargetError("ambiguous_product_target")
    if not result:
        raise BitrixNotFoundError("product_not_found")
    return result


def canonical_to_bitrix_payload(*, product: dict, aspro_enabled: bool = False) -> dict:
    if aspro_enabled:
        profile = fixture_aspro_premier_profile()
        return map_product_to_bitrix_payload(product=product, profile=profile)
    return {
        "NAME": product.get("title") or product.get("name") or "",
        "PROPERTY_ARTNUMBER": product.get("sku") or product.get("article") or "",
        "DETAIL_TEXT": product.get("description") or "",
        "integration": "bitrix",
    }


def validate_create_payload(payload: dict) -> None:
    name = payload.get("name") or payload.get("NAME") or payload.get("title")
    article = payload.get("article") or payload.get("sku") or payload.get("PROPERTY_ARTNUMBER")
    if not name:
        raise BitrixValidationError("name_required")
    if not article:
        raise BitrixValidationError("article_required")


def selective_export_filter(*, all_products: list[dict], selected: list[str]) -> list[dict]:
    if not selected:
        return []
    sel = {s.casefold() for s in selected}
    out = []
    for p in all_products:
        art = str(p.get("article") or p.get("sku") or "").casefold()
        pid = str(p.get("product_id") or p.get("panda_product_id") or "").casefold()
        if art in sel or pid in sel:
            out.append(p)
    return out
