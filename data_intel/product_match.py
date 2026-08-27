"""Product SKU/EAN/MPN matching — reuses Acquisition identifier layer."""

from __future__ import annotations

from acquisition.entity import resolve_entities
from acquisition.identifiers import identifier_bundle, normalize_name
from data_intel.contracts import (
    CONF_CONFLICT,
    CONF_EXACT,
    CONF_HIGH,
    CONF_LOW,
    CONF_MEDIUM,
    CONF_UNRESOLVED,
    MatchResult,
)


def _level_to_conf(level: str) -> str:
    mapping = {
        "exact": CONF_EXACT,
        "high": CONF_HIGH,
        "medium": CONF_MEDIUM,
        "low": CONF_LOW,
        "unresolved": CONF_UNRESOLVED,
    }
    return mapping.get(level, CONF_UNRESOLVED)


def match_products(
    left: dict,
    right: dict,
    *,
    left_ref: str = "left",
    right_ref: str = "right",
    supplier_context: str | None = None,
) -> MatchResult:
    """Match products. Conflicting EAN/MPN never silently merge on name."""
    lf = dict(left)
    rf = dict(right)
    if supplier_context:
        lf.setdefault("supplier", supplier_context)
        rf.setdefault("supplier", supplier_context)
    result = resolve_entities(lf, rf, left_id=left_ref, right_id=right_ref)
    conflicts = tuple(result.evidence.conflicts or ())
    conf = CONF_CONFLICT if conflicts else _level_to_conf(result.level)
    review = (not result.same_entity) or conf in {CONF_LOW, CONF_MEDIUM, CONF_UNRESOLVED, CONF_CONFLICT}
    if conf == CONF_EXACT:
        review = False
    return MatchResult(
        entity_type="product",
        left_ref=left_ref,
        right_ref=right_ref,
        match_method=result.evidence.method,
        confidence=conf,
        evidence={
            "matched_fields": list(result.evidence.matched_fields or ()),
            "left_ids": identifier_bundle(lf),
            "right_ids": identifier_bundle(rf),
            "left_name": normalize_name(lf.get("product_name") or lf.get("name")),
            "right_name": normalize_name(rf.get("product_name") or rf.get("name")),
        },
        conflicts=conflicts,
        same_entity=bool(result.same_entity) and not conflicts,
        review_required=review and not (result.same_entity and conf in {CONF_EXACT, CONF_HIGH}),
    )
