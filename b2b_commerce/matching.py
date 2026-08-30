"""Wholesale product matching — reuses Block 7 data_intel."""

from __future__ import annotations

import uuid
from typing import Any

from data_intel.product_match import match_products

from b2b_commerce.platform_models import (
    MATCH_AMBIGUOUS,
    MATCH_CANDIDATE,
    MATCH_CONFIRMED,
    MATCH_CONFLICT,
    MATCH_UNMATCHED,
    WholesaleProductMatch,
)


def _catalog_row(product: dict[str, Any]) -> dict[str, Any]:
    return {
        "sku": product.get("sku") or product.get("article"),
        "ean": product.get("ean") or product.get("gtin"),
        "mpn": product.get("mpn"),
        "product_name": product.get("title") or product.get("name") or product.get("product_name"),
        "brand": product.get("brand"),
    }


def match_supplier_row(
    row: dict[str, Any],
    catalog: list[dict[str, Any]],
    *,
    tenant_id: str,
    offer_version_id: str,
    supplier_context: str | None = None,
) -> WholesaleProductMatch:
    candidates: list[str] = []
    best_state = MATCH_UNMATCHED
    best_product_id = ""
    best_version_id = ""
    evidence: list[str] = []

    for product in catalog:
        result = match_products(
            row,
            _catalog_row(product),
            left_ref=row.get("sku") or row.get("supplier_sku") or "row",
            right_ref=product.get("product_id") or product.get("sku") or "catalog",
            supplier_context=supplier_context,
        )
        pid = str(product.get("product_id") or "")
        if result.conflicts:
            candidates.append(pid)
            best_state = MATCH_CONFLICT
            evidence.append("conflict")
            continue
        if result.same_entity and result.confidence in {"exact", "high"}:
            if best_state == MATCH_CONFIRMED and best_product_id and best_product_id != pid:
                best_state = MATCH_AMBIGUOUS
                candidates.extend([best_product_id, pid])
                break
            best_state = MATCH_CONFIRMED
            best_product_id = pid
            best_version_id = str(product.get("version_id") or product.get("product_version_id") or "")
            evidence.append(result.match_method)
        elif result.confidence in {"medium", "low"}:
            candidates.append(pid)
            if best_state not in {MATCH_CONFIRMED, MATCH_CONFLICT}:
                best_state = MATCH_CANDIDATE
            evidence.append(result.match_method)

    if len(set(candidates)) > 1 and best_state != MATCH_CONFLICT:
        best_state = MATCH_AMBIGUOUS

    if best_state == MATCH_UNMATCHED and len(candidates) == 1:
        best_state = MATCH_CANDIDATE
        best_product_id = candidates[0]

    return WholesaleProductMatch(
        match_id=f"match_{uuid.uuid4().hex[:12]}",
        tenant_id=tenant_id,
        offer_version_id=offer_version_id,
        product_id=best_product_id,
        product_version_id=best_version_id,
        state=best_state,
        evidence=tuple(evidence),
        candidates=tuple(sorted(set(candidates))),
    )
