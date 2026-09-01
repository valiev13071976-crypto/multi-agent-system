"""Selective marketplace export — no implicit full catalog."""

from __future__ import annotations

import uuid
from decimal import Decimal

from marketplace.errors import MARKETPLACE_SELECTION_REQUIRED, MarketplaceError
from marketplace.models import MarketplaceSelection


def require_explicit_selection(selection: MarketplaceSelection | None) -> MarketplaceSelection:
    if selection is None:
        raise MarketplaceError(MARKETPLACE_SELECTION_REQUIRED, "selection_absent")
    has_scope = bool(
        selection.product_ids
        or selection.sku_ids
        or selection.category_ids
        or selection.brands
        or selection.filters
        or selection.allow_all_catalog
    )
    if not has_scope:
        raise MarketplaceError(MARKETPLACE_SELECTION_REQUIRED, "selection_empty")
    return selection


def _dec(v) -> Decimal:
    try:
        return Decimal(str(v or 0))
    except Exception:
        return Decimal("0")


def resolve_selection(
    *,
    selection: MarketplaceSelection,
    catalog: list[dict],
) -> dict:
    """catalog items: {product_id, sku_id, category_id, brand, stock}"""
    require_explicit_selection(selection)
    if selection.allow_all_catalog:
        selected = list(catalog)
        return {
            "selected": selected,
            "excluded": [],
            "ineligible": [],
            "count": len(selected),
            "mode": "ALL_CATALOG_AUTHORIZED",
        }

    selected: list[dict] = []
    excluded: list[dict] = []
    for item in catalog:
        ok = False
        if selection.product_ids and item.get("product_id") in selection.product_ids:
            ok = True
        if selection.sku_ids and item.get("sku_id") in selection.sku_ids:
            ok = True
        if selection.category_ids and item.get("category_id") in selection.category_ids:
            ok = True
        if selection.brands and str(item.get("brand") or "") in selection.brands:
            ok = True
        for key, val in selection.filters:
            if key == "stock_gt" and _dec(item.get("stock")) > _dec(val):
                ok = True
        if ok:
            selected.append(item)
        else:
            excluded.append(item)
    ineligible = [i for i in selected if not i.get("sku_id") or not i.get("title")]
    eligible = [i for i in selected if i not in ineligible]
    return {
        "selected": eligible,
        "excluded": excluded,
        "ineligible": ineligible,
        "count": len(eligible),
        "mode": "EXPLICIT",
    }


def new_selection(
    *,
    tenant_id: str,
    product_ids: tuple[str, ...] = (),
    sku_ids: tuple[str, ...] = (),
    category_ids: tuple[str, ...] = (),
    brands: tuple[str, ...] = (),
    allow_all_catalog: bool = False,
) -> MarketplaceSelection:
    return MarketplaceSelection(
        selection_id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        product_ids=product_ids,
        sku_ids=sku_ids,
        category_ids=category_ids,
        brands=brands,
        allow_all_catalog=allow_all_catalog,
    )
