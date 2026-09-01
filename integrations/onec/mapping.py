"""1C identity resolution, preview, selective operations."""

from __future__ import annotations

from integrations.onec.catalog import OneCCatalogStore
from integrations.onec.errors import OneCAmbiguousTargetError, OneCNotFoundError, OneCValidationError


def build_preview(*, operation: str, before: dict | None, after: dict) -> dict:
    return {"operation": operation, "before": before or {}, "after": after, "safe": True}


def resolve_nomenclature_target(
    store: OneCCatalogStore,
    *,
    tenant_id: str,
    guid: str = "",
    xml_id: str = "",
    article: str = "",
    panda_product_id: str = "",
    name: str = "",
    allow_name_only: bool = False,
) -> dict:
    result = store.lookup(
        tenant_id=tenant_id,
        guid=guid,
        xml_id=xml_id,
        article=article,
        panda_product_id=panda_product_id,
        name=name,
        allow_name_only=allow_name_only,
    )
    if isinstance(result, list):
        raise OneCAmbiguousTargetError("ambiguous_nomenclature_target")
    if not result:
        raise OneCNotFoundError("nomenclature_not_found")
    return result


def selective_rows(*, all_rows: list[dict], selected: list[str]) -> list[dict]:
    if not selected:
        return []
    sel = {s.casefold() for s in selected}
    return [
        r
        for r in all_rows
        if str(r.get("sku") or r.get("article") or "").casefold() in sel
        or str(r.get("product_id") or "").casefold() in sel
    ]


def validate_document_payload(payload: dict) -> None:
    if not payload.get("items"):
        raise OneCValidationError("document_items_required")
